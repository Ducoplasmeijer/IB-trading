"""Command line entry point: `ibt <command>`."""

import argparse
import sys

from ib_async import ContFuture, Stock
from rich.console import Console
from rich.table import Table

from .config import get_settings
from .connection import connect
from .portfolio import account_summary, positions
from .safety import OrderBlockedError
from .strategies import vertical_spread as vs

console = Console()


def cmd_check(_: argparse.Namespace) -> None:
    s = get_settings()
    with connect(s) as ib:
        console.print(f"[green]Connected[/] to {s.host}:{s.port} (server v{ib.client.serverVersion()})")
        console.print(f"Mode: [bold]{s.mode.value}[/]  readonly={s.readonly}  accounts={ib.managedAccounts()}")


def cmd_portfolio(_: argparse.Namespace) -> None:
    with connect() as ib:
        for acct, tags in account_summary(ib).items():
            t = Table(title=f"Account {acct}")
            t.add_column("Metric")
            t.add_column("Value", justify="right")
            for k, v in tags.items():
                t.add_row(k, v)
            console.print(t)
        t = Table(title="Positions")
        for col in ("Account", "Contract", "Type", "Qty", "Avg cost", "Mkt price", "Mkt value", "Unrl PnL"):
            t.add_column(col)
        for p in positions(ib):
            t.add_row(p.account, p.description, p.sec_type, f"{p.quantity:g}", f"{p.avg_cost:,.2f}",
                      f"{p.market_price:,.2f}", f"{p.market_value:,.2f}", f"{p.unrealized_pnl:,.2f}")
        console.print(t)


def cmd_chain(a: argparse.Namespace) -> None:
    with connect() as ib:
        if a.sec_type == "FOP":
            under = ContFuture(a.symbol, exchange=a.exchange)
            fut_exchange, under_type = a.exchange, "FUT"
        else:
            under = Stock(a.symbol, "SMART", "USD")
            fut_exchange, under_type = "", "STK"
        ib.qualifyContracts(under)
        chains = ib.reqSecDefOptParams(a.symbol, fut_exchange, under_type, under.conId)
        for c in chains:
            if a.trading_class and c.tradingClass != a.trading_class:
                continue
            console.print(f"[bold]{c.exchange} {c.tradingClass}[/] multiplier={c.multiplier}")
            console.print("  expiries:", ", ".join(sorted(c.expirations)[:12]), "...")
            console.print(f"  strikes: {min(c.strikes)} .. {max(c.strikes)} ({len(c.strikes)} total)")


def _print_spread(spec: vs.SpreadSpec, multiplier: float) -> None:
    console.print(
        f"BUY {spec.quantity}x {spec.symbol} {spec.expiry} {spec.long_strike}/{spec.short_strike}{spec.right} "
        f"spread @ {spec.limit_price} debit ({spec.tif})"
    )
    console.print(f"  Max loss (debit): {vs.max_cost(spec, multiplier):,.2f} {spec.currency}")
    console.print(f"  Max profit:       {vs.max_profit(spec, multiplier):,.2f} {spec.currency}")


def cmd_spread(a: argparse.Namespace) -> None:
    spec = vs.SpreadSpec.from_yaml(a.spec)
    s = get_settings()
    with connect(s) as ib:
        state, mult = vs.preview(ib, spec)
        _print_spread(spec, mult)
        console.print(f"  What-if: init margin change={state.initMarginChange} "
                      f"commission={state.commission} warning={state.warningText or '-'}")
        if a.action != "place":
            return
        console.print(f"\n[bold red]About to send a {s.mode.value.upper()} order.[/]")
        if input("Type PLACE to confirm: ").strip() != "PLACE":
            console.print("Cancelled.")
            return
        trade = vs.place(ib, s, spec)
        ib.sleep(2)
        console.print(f"Order {trade.order.orderId} status: {trade.orderStatus.status}")


def cmd_dashboard(_: argparse.Namespace) -> None:
    from .dashboard.app import run

    run()


def main() -> None:
    p = argparse.ArgumentParser(prog="ibt", description="Interactive Brokers toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Test connection and show mode/accounts").set_defaults(fn=cmd_check)
    sub.add_parser("portfolio", help="Account summary and positions").set_defaults(fn=cmd_portfolio)

    c = sub.add_parser("chain", help="List option expiries/strikes")
    c.add_argument("--symbol", required=True)
    c.add_argument("--sec-type", choices=["OPT", "FOP"], default="OPT")
    c.add_argument("--exchange", default="SMART")
    c.add_argument("--trading-class")
    c.set_defaults(fn=cmd_chain)

    sub.add_parser("dashboard", help="Start the read-only web dashboard on 127.0.0.1").set_defaults(fn=cmd_dashboard)

    sp = sub.add_parser("spread", help="Preview or place a vertical spread from a YAML spec")
    sp.add_argument("action", choices=["preview", "place"])
    sp.add_argument("--spec", required=True)
    sp.set_defaults(fn=cmd_spread)

    args = p.parse_args()
    try:
        args.fn(args)
    except OrderBlockedError as e:
        console.print(f"[bold red]BLOCKED:[/] {e}")
        sys.exit(2)
    except ConnectionRefusedError:
        console.print("[red]Connection refused.[/] Is IB Gateway/TWS running with the API enabled on this port?")
        sys.exit(1)


if __name__ == "__main__":
    main()
