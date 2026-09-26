"""Dashboard sessions: one IB connection per Gateway login (paper and/or live).

Market data, portfolio and analysis are read-only. Orders go only through `Session.preview_order` /
`Session.submit_order`, which enforce `safety.assert_session_can_trade` + `assert_within_limits`.
"""

import asyncio
import datetime as dt
import json
import logging
import math
import secrets
import time
from collections import deque
from typing import Literal

from ib_async import IB, ContFuture, Contract, Future, LimitOrder, Ticker, Trade
from pydantic import BaseModel, Field, model_validator

from ..analysis import costs, flex
from ..config import Settings, TradingMode
from ..safety import (
    OrderBlockedError,
    assert_account_matches_mode,
    assert_session_can_trade,
    assert_within_limits,
    session_orders_enabled,
)
from .watchlist import Product, Watchlist

log = logging.getLogger(__name__)

INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158, 2100, 2150, 399}  # "data farm OK", order warnings shown elsewhere
SUMMARY_TAGS = (
    "NetLiquidation", "TotalCashValue", "GrossPositionValue", "BuyingPower",
    "AvailableFunds", "MaintMarginReq", "UnrealizedPnL", "RealizedPnL",
)
RANGES = {  # key: (durationStr, barSize)
    "5D": ("5 D", "30 mins"), "1M": ("1 M", "4 hours"), "3M": ("3 M", "1 day"), "6M": ("6 M", "1 day"),
    "1Y": ("1 Y", "1 day"), "2Y": ("2 Y", "1 day"), "5Y": ("5 Y", "1 week"), "LAST": ("5 D", "1 day"),
}
HIST_TTL = 15 * 60
PREVIEW_TTL = 90  # seconds a previewed order stays submittable
DEPTH_IDLE = 30  # cancel an order-book subscription after this many seconds without polling


def num(x) -> float | None:
    """JSON-safe number: IB uses nan / -1 / UNSET_DOUBLE for 'no value'."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) or abs(x) > 1e300 else x


def pos(x) -> float | None:
    x = num(x)
    return x if x is not None and x > 0 else None


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def describe(c: Contract) -> str:
    if c.secType == "BOND":
        return f"{c.symbol} {c.lastTradeDateOrContractMonth}".strip()
    return c.localSymbol or c.symbol


class InstrumentRef(BaseModel):
    """What to trade / show: a watchlist key, a conId (e.g. from a position), or a plain stock/ETF."""

    key: str | None = None
    con_id: int | None = None
    symbol: str | None = Field(None, max_length=20, pattern=r"^[A-Za-z0-9.\- ]+$")
    sec_type: Literal["STK"] = "STK"
    exchange: str = Field("SMART", max_length=12, pattern=r"^[A-Z0-9]+$")
    currency: str = Field("USD", max_length=3, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def _one(self) -> "InstrumentRef":
        if not (self.key or self.con_id or self.symbol):
            raise ValueError("Give a watchlist key, a con_id or a symbol")
        return self


class OrderRequest(BaseModel):
    instrument: InstrumentRef
    action: Literal["BUY", "SELL"]
    quantity: float = Field(gt=0, le=1_000_000)
    limit_price: float = Field(gt=0)
    tif: Literal["DAY", "GTC"] = "DAY"
    outside_rth: bool = False


class Session:
    def __init__(self, settings: Settings, mode: TradingMode, watchlist: Watchlist):
        self.s = settings
        self.mode = mode
        self.port = settings.paper_port if mode is TradingMode.PAPER else settings.live_port
        self.watchlist = watchlist
        self.products: dict[str, Product] = watchlist.products
        self.ib = IB()
        self.market_contracts: dict[str, Contract] = {}
        self.hist_contracts: dict[str, Contract] = {}
        self.tickers: dict[str, Ticker] = {}
        self.extra_tickers: dict[int, Ticker] = {}
        self.depth: dict[int, tuple[Contract, Ticker, float, bool]] = {}
        self.account: str | None = None
        self.pnl = None
        self.errors: deque[dict] = deque(maxlen=60)
        self.last_connect_error: str | None = None
        self._hist_cache: dict[tuple, tuple[float, list]] = {}
        self._hist_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.fallback_close: dict[str, tuple[float, str]] = {}
        self.fx_ticker: Ticker | None = None
        self.fx_fallback: float | None = None
        self.pending: dict[str, dict] = {}
        self.ib.errorEvent += self._on_error

    # ---------- lifecycle ----------
    @property
    def orders_enabled(self) -> tuple[bool, str | None]:
        return session_orders_enabled(self.s, self.mode)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self.ib.disconnect()

    async def _run(self) -> None:
        while True:
            if not self.ib.isConnected():
                try:
                    await self.ib.connectAsync(
                        self.s.host, self.port, clientId=self.s.dashboard_client_id,
                        # read-only unless this session may trade (then open orders are synced too)
                        readonly=not self.orders_enabled[0], timeout=20,
                    )
                    assert_account_matches_mode(self.s, self.ib.managedAccounts(), self.mode)
                    await self._after_connect()
                    self.last_connect_error = None
                    log.info("%s session connected on port %s", self.mode.value, self.port)
                except OrderBlockedError as e:
                    self.last_connect_error = f"Safety check failed: {e}"
                    self.ib.disconnect()
                except ConnectionRefusedError:
                    self.last_connect_error = f"No Gateway/TWS listening on port {self.port}"
                    self.ib.disconnect()
                except Exception as e:  # noqa: BLE001 - keep retrying, surface the error in the UI
                    self.last_connect_error = f"{type(e).__name__}: {e}"
                    self.ib.disconnect()
            self._expire()
            await asyncio.sleep(10)

    async def _after_connect(self) -> None:
        self.account = self.ib.managedAccounts()[0]
        self.ib.reqMarketDataType(self.s.market_data_type)
        self.pnl = self.ib.reqPnL(self.account)
        await self.ib.accountSummaryAsync(self.account)
        if self.orders_enabled[0]:
            await self.ib.reqAllOpenOrdersAsync()
        self.tickers.clear()
        self.extra_tickers.clear()
        self.depth.clear()
        for p in self.products.values():
            try:
                await self._resolve(p)
                self.tickers[p.key] = self.ib.reqMktData(self.market_contracts[p.key])
            except Exception as e:  # noqa: BLE001
                self.errors.append({"time": _now(), "code": None, "msg": f"{p.key}: {e}", "symbol": p.key})
        if self.watchlist.fx:
            fx = Contract(**self.watchlist.fx["contract"])
            await self.ib.qualifyContractsAsync(fx)
            if fx.conId:
                self.fx_ticker = self.ib.reqMktData(fx)
        asyncio.create_task(self._load_fallback_closes())

    async def _load_fallback_closes(self) -> None:
        """Last daily close per product, for products without a (live or delayed) market data subscription."""
        try:
            fx = await self._fx_history("LAST")
            if fx:
                self.fx_fallback = fx[max(fx)]
        except Exception:  # noqa: BLE001, S110
            pass
        for key in list(self.hist_contracts):
            try:
                bars = await self.history(key, "LAST")
                if bars:
                    self.fallback_close[key] = (bars[-1]["c"], bars[-1]["t"][:10])
            except Exception:  # noqa: BLE001, S112
                continue

    async def _resolve(self, p: Product) -> None:
        spec = dict(p.contract)
        if spec.get("secType") == "CONTFUT":
            cont = ContFuture(spec["symbol"], exchange=spec.get("exchange", ""), currency=spec.get("currency", ""))
            await self.ib.qualifyContractsAsync(cont)
            if not cont.conId:
                raise ValueError("could not resolve continuous future")
            front = Future(conId=cont.conId)
            await self.ib.qualifyContractsAsync(front)
            self.hist_contracts[p.key], self.market_contracts[p.key] = cont, front
        else:
            c = Contract(**spec)
            await self.ib.qualifyContractsAsync(c)
            if not c.conId:
                raise ValueError("could not resolve contract")
            self.hist_contracts[p.key] = self.market_contracts[p.key] = c

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        if errorCode in INFO_CODES:
            return
        sym = getattr(contract, "symbol", None) if contract else None
        self.errors.append({"time": _now(), "code": errorCode, "msg": errorString, "symbol": sym, "req": reqId})

    def _expire(self) -> None:
        now = time.monotonic()
        for pid in [k for k, v in self.pending.items() if now - v["created"] > PREVIEW_TTL]:
            del self.pending[pid]
        for con_id, (c, _t, last, smart) in list(self.depth.items()):
            if now - last > DEPTH_IDLE:
                self.ib.cancelMktDepth(c, isSmartDepth=smart)
                del self.depth[con_id]

    # ---------- helpers ----------
    def _account_values(self) -> list:
        return self.ib.accountValues(self.account) if self.account else []

    def base_currency(self) -> str | None:
        for v in self._account_values():
            if v.tag == "NetLiquidation" and v.currency not in ("", "BASE"):
                return v.currency
        return None

    def fx_to_base(self) -> dict[str, float]:
        """{currency: rate} so that amount_ccy * rate = amount_base."""
        rates = {}
        for v in self._account_values():
            if v.tag == "ExchangeRate" and v.currency and v.currency != "BASE":
                r = num(v.value)
                if r:
                    rates[v.currency] = r
        base = self.base_currency()
        if base:
            rates.setdefault(base, 1.0)
        # IB only reports rates for currencies held; fill the gap from the configured FX pair (e.g. EUR.USD).
        fx = self.watchlist.fx["contract"] if self.watchlist.fx else None
        if fx and base:
            t = self.fx_ticker
            bid, ask = (pos(t.bid), pos(t.ask)) if t else (None, None)
            px = ((bid + ask) / 2 if bid and ask else None) or (pos(t.close) if t else None) or self.fx_fallback
            sym, quote = fx["symbol"], fx["currency"]
            if px and base == sym:
                rates.setdefault(quote, 1 / px)
            elif px and base == quote:
                rates.setdefault(sym, px)
        return rates

    def status(self) -> dict:
        acct = self.account or ""
        ok, why = self.orders_enabled
        return {
            "session": self.mode.value,
            "connected": self.ib.isConnected(),
            "port": self.port,
            "host": f"{self.s.host}:{self.port}",
            "account": f"{acct[:2]}•••{acct[-3:]}" if len(acct) > 5 else acct,
            "base_currency": self.base_currency(),
            "market_data_type": self.s.market_data_type,
            "orders_enabled": ok,
            "orders_blocked_reason": why,
            "connect_error": self.last_connect_error,
            "errors": list(self.errors)[-20:][::-1],
            "flex_configured": bool(self.s.flex_token and self.s.flex_query_id),
            "server_time": _now(),
        }

    # ---------- portfolio ----------
    def portfolio(self) -> dict:
        base = self.base_currency()
        fx = self.fx_to_base()
        vals = self._account_values()
        summary = {}
        for tag in SUMMARY_TAGS:
            match = [v for v in vals if v.tag == tag and v.currency in (base, "BASE")]
            if match:
                summary[tag] = num(match[0].value)
        cash = []
        for tag in ("TotalCashBalance", "$LEDGER-TotalCashBalance", "CashBalance", "$LEDGER-CashBalance"):
            cash = [
                {"currency": v.currency, "amount": num(v.value), "amount_base": num(v.value) * fx.get(v.currency, 0)}
                for v in vals
                if v.tag == tag and v.currency not in ("BASE", "") and num(v.value)
            ]
            if cash:
                break
        nlv = summary.get("NetLiquidation") or 0
        rows = []
        for it in self.ib.portfolio(self.account or ""):
            c = it.contract
            rate = fx.get(c.currency)
            mv_base = num(it.marketValue) * rate if rate and num(it.marketValue) is not None else None
            cost_basis = num(it.averageCost) * it.position if num(it.averageCost) is not None else None
            upnl = num(it.unrealizedPNL)
            rows.append({
                "con_id": c.conId, "symbol": c.symbol, "sec_type": c.secType, "local_symbol": describe(c),
                "currency": c.currency, "exchange": c.primaryExchange or c.exchange,
                "position": it.position, "avg_cost": num(it.averageCost), "market_price": num(it.marketPrice),
                "market_value": num(it.marketValue), "market_value_base": mv_base,
                "unrealized_pnl": upnl, "realized_pnl": num(it.realizedPNL),
                "unrealized_pct": (upnl / abs(cost_basis) * 100) if upnl is not None and cost_basis else None,
                "weight": (mv_base / nlv * 100) if mv_base is not None and nlv else None,
            })
        rows.sort(key=lambda r: -(abs(r["market_value_base"] or 0)))
        pnl = {
            "daily": num(self.pnl.dailyPnL) if self.pnl else None,
            "unrealized": num(self.pnl.unrealizedPnL) if self.pnl else None,
            "realized": num(self.pnl.realizedPnL) if self.pnl else None,
        }
        return {"base_currency": base, "summary": summary, "pnl": pnl, "cash": cash, "positions": rows}

    # ---------- metals quotes ----------
    def _quote(self, key: str) -> dict:
        p, t = self.products[key], self.tickers.get(key)
        c = self.market_contracts.get(key)
        bid, ask = (pos(t.bid), pos(t.ask)) if t else (None, None)
        last, close = (pos(t.last), pos(t.close)) if t else (None, None)
        mid = (bid + ask) / 2 if bid and ask else None
        hist, hist_date = self.fallback_close.get(key, (None, None))
        price = mid or last or close or hist
        source = "mid" if mid else "last" if last else "close" if close else "history" if hist else None
        close = close or hist
        live_spread = (ask - bid) if bid and ask else None
        return {
            "key": key, "name": p.name, "kind": p.kind, "group": p.group,
            "con_id": c.conId if c else None,
            "currency": (c.currency if c else p.contract.get("currency")),
            "local_symbol": (c.localSymbol if c else None) or key,
            "expiry": (c.lastTradeDateOrContractMonth if c and p.kind == "future" else None),
            "bid": bid, "ask": ask, "last": last, "close": close, "price": price,
            "price_source": source, "history_date": hist_date if source == "history" else None,
            "change_pct": (price / close - 1) * 100 if price and close and source in ("mid", "last") else None,
            "spread": live_spread if live_spread is not None else p.typical_spread,
            "spread_source": "live" if live_spread is not None else "assumed",
            "market_data_type": t.marketDataType if t else None,
            "multiplier": p.multiplier,
        }

    def metal_quotes(self, group_name: str) -> dict:
        g = self.watchlist.group(group_name)
        fx = self.fx_to_base()
        quotes = [self._quote(p.key) for p in g.products]
        ref = next((q for q in quotes if q["key"] == g.reference), None)
        ref_base = (
            ref["price"] * fx[ref["currency"]] if ref and ref["price"] and ref["currency"] in fx else None
        )
        for q in quotes:
            rate = fx.get(q["currency"])
            q["price_base"] = q["price"] * rate if q["price"] and rate else None
            q["notional_unit_base"] = q["price_base"] * q["multiplier"] if q["price_base"] else None
            q["underlying_per_unit"] = (
                q["notional_unit_base"] / ref_base if q["notional_unit_base"] and ref_base else None
            )
            q["spread_bps"] = q["spread"] / q["price"] * 1e4 if q["price"] and q["spread"] is not None else None
            q["carry_annual_pct"] = None
            q["days_to_expiry"] = None
            if q["kind"] == "future" and q["expiry"]:
                q["days_to_expiry"] = (dt.datetime.strptime(q["expiry"][:8], "%Y%m%d").date() - dt.date.today()).days
            if q["kind"] == "future" and ref and ref["kind"] == "spot" and ref["price"] and q["price"]:
                fut_px, spot_px = q["price"], ref["price"]
                live = {"mid", "last"}
                if not (q["price_source"] in live and ref["price_source"] in live):
                    # Market closed / no live data: compare closes from the SAME day, never mixed snapshots.
                    fh, sh = self.fallback_close.get(q["key"]), self.fallback_close.get(ref["key"])
                    fut_px, spot_px = (fh[0], sh[0]) if fh and sh and fh[1] == sh[1] else (None, None)
                carry = costs.implied_annual_carry(fut_px, spot_px, q["days_to_expiry"]) if fut_px else None
                q["carry_annual_pct"] = carry * 100 if carry is not None else None
        return {
            "group": g.name, "unit": g.unit, "reference": g.reference, "base_currency": self.base_currency(),
            "quotes": quotes,
        }

    # ---------- history ----------
    async def history(self, key: str, range_key: str) -> list[dict]:
        if key not in self.products or range_key not in RANGES:
            raise KeyError(key)
        cache_key = (key, range_key)
        hit = self._hist_cache.get(cache_key)
        if hit and time.monotonic() - hit[0] < HIST_TTL:
            return hit[1]
        async with self._hist_lock:  # serialize requests: IB paces historical data
            contract = self.hist_contracts.get(key)
            if contract is None:
                raise ConnectionError("not connected / contract not resolved")
            duration, bar = RANGES[range_key]
            bars = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract, "", duration, bar, self.products[key].what_to_show, useRTH=True, formatDate=2
                ),
                timeout=40,
            )
        data = [{"t": b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date), "c": b.close} for b in bars]
        self._hist_cache[cache_key] = (time.monotonic(), data)
        return data

    async def tracking(self, group_name: str, range_key: str = "1Y") -> dict:
        """ETF/ETC return vs the group's reference (spot or front future) in the same currency."""
        g = self.watchlist.group(group_name)
        if not g.reference:
            return {"reference": None, "rows": []}
        ref = await self.history(g.reference, range_key)
        ref_ccy = self.products[g.reference].contract.get("currency", "USD")
        eurusd = await self._fx_history(range_key)
        ref_map = {r["t"][:10]: r["c"] for r in ref}
        out = []
        for p in g.products:
            if p.kind != "etf":
                continue
            try:
                h = await self.history(p.key, range_key)
            except Exception as e:  # noqa: BLE001
                out.append({"key": p.key, "name": p.name, "error": str(e)})
                continue
            common = [r for r in h if r["t"][:10] in ref_map]
            if len(common) < 2:
                out.append({"key": p.key, "name": p.name, "error": "no overlapping history"})
                continue
            a, b = common[0], common[-1]
            rs, re_ = ref_map[a["t"][:10]], ref_map[b["t"][:10]]
            ccy = p.contract.get("currency", "USD")
            if ccy != ref_ccy:  # e.g. EUR product vs USD spot: convert reference with EUR.USD
                fa, fb = eurusd.get(a["t"][:10]), eurusd.get(b["t"][:10])
                if not (fa and fb and {ccy, ref_ccy} == {"EUR", "USD"}):
                    out.append({"key": p.key, "name": p.name, "error": f"no FX history for {ccy}"})
                    continue
                rs, re_ = (rs / fa, re_ / fb) if ccy == "EUR" else (rs * fa, re_ * fb)
            td = costs.tracking_difference(a["c"], b["c"], rs, re_)
            years = max((dt.date.fromisoformat(b["t"][:10]) - dt.date.fromisoformat(a["t"][:10])).days / 365, 1e-9)
            out.append({
                "key": p.key, "name": p.name, "from": a["t"][:10], "to": b["t"][:10],
                "product_return_pct": (b["c"] / a["c"] - 1) * 100, "ref_return_pct": (re_ / rs - 1) * 100,
                "tracking_diff_pct": td * 100 if td is not None else None,
                "tracking_diff_annual_pct": ((1 + td) ** (1 / years) - 1) * 100 if td is not None else None,
                "expense_ratio_pct": p.expense_ratio * 100,
            })
        return {"reference": g.reference, "reference_kind": self.products[g.reference].kind, "rows": out}

    async def _fx_history(self, range_key: str) -> dict[str, float]:
        if not self.watchlist.fx:
            return {}
        key = ("__fx__", range_key)
        hit = self._hist_cache.get(key)
        if hit and time.monotonic() - hit[0] < HIST_TTL:
            return dict(hit[1])
        c = Contract(**self.watchlist.fx["contract"])
        async with self._hist_lock:
            await self.ib.qualifyContractsAsync(c)
            duration, bar = RANGES[range_key]
            bars = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(c, "", duration, bar, "MIDPOINT", useRTH=True, formatDate=2), 40
            )
        data = [(str(b.date)[:10], b.close) for b in bars]
        self._hist_cache[key] = (time.monotonic(), data)
        return dict(data)

    # ---------- costs ----------
    def cost_estimates(self, group_name: str, target_base: float) -> dict:
        fx = self.fx_to_base()
        g = self.watchlist.group(group_name)
        quotes = {q["key"]: q for q in self.metal_quotes(group_name)["quotes"]}
        rows = []
        for p in g.products:
            q = quotes[p.key]
            if p.kind == "spot":
                continue
            if not q["price"] or q["currency"] not in fx:
                rows.append({"key": p.key, "name": p.name, "kind": p.kind, "qty": 0, "note": "No price available"})
                continue
            rate = fx[q["currency"]]
            qty = costs.units_for_exposure(target_base / rate, q["price"], p.multiplier, p.kind)
            rt = costs.round_trip(p.commission, qty, q["price"], p.multiplier, q["spread"] or 0)
            roll = costs.round_trip(p.commission, qty, q["price"], p.multiplier, p.typical_spread)
            hold = costs.annual_holding_cost(p.kind, rt.notional, p.expense_ratio, p.rolls_per_year, roll.total)
            rows.append({
                "key": p.key, "name": p.name, "kind": p.kind, "currency": q["currency"],
                "unit_notional_base": q["price"] * p.multiplier * rate,
                "qty": qty, "exposure_base": rt.notional * rate,
                "commission_rt_base": rt.commission * rate, "spread_rt_base": rt.spread * rate,
                "round_trip_base": rt.total * rate, "round_trip_bps": rt.bps,
                "annual_hold_base": hold * rate,
                "first_year_base": (rt.total + hold) * rate,
                "spread_source": q["spread_source"],
                "carry_annual_pct": q.get("carry_annual_pct"),
                "note": None if qty else f"Minimum size is 1 unit ≈ {q['price'] * p.multiplier * rate:,.0f}",
            })
        return {"group": g.name, "base_currency": self.base_currency(), "target": target_base, "rows": rows}

    def executions(self) -> dict:
        """Recent fills known to the Gateway (typically today / last days). Full history: Flex."""
        fx = self.fx_to_base()
        rows = []
        for f in self.ib.fills():
            c, e, cr = f.contract, f.execution, f.commissionReport
            rate = fx.get(c.currency, 1.0)
            mult = float(c.multiplier or 1)
            rows.append({
                "date": e.time.strftime("%Y-%m-%d") if e.time else "", "symbol": c.symbol, "asset": c.secType,
                "currency": c.currency, "side": e.side, "qty": e.shares, "price": e.price,
                "notional_base": abs(e.shares * e.price * mult) * rate,
                "commission_base": (num(cr.commission) or 0) * fx.get(cr.currency or c.currency, rate),
            })
        return {"rows": rows, "summary": costs.summarize_trades(rows)}

    # ---------- instruments, quotes & order book ----------
    def instruments(self) -> dict:
        positions = [
            {"con_id": it.contract.conId, "label": describe(it.contract), "sec_type": it.contract.secType,
             "currency": it.contract.currency, "position": it.position}
            for it in self.ib.portfolio(self.account or "")
        ]
        metals = [
            {"key": p.key, "group": p.group, "label": f"{p.key}: {p.name}", "kind": p.kind,
             "tradable": p.kind != "spot" and p.key in self.market_contracts}
            for p in self.products.values()
        ]
        return {"positions": positions, "metals": metals}

    async def resolve_instrument(self, ref: InstrumentRef) -> Contract:
        if ref.key:
            if ref.key not in self.products:
                raise KeyError(ref.key)
            c = self.market_contracts.get(ref.key)
            if c is None:
                raise ValueError(f"{ref.key} is not resolved (no connection or unknown contract)")
            return c
        if ref.con_id:
            c = Contract(conId=ref.con_id)
        else:
            c = Contract(secType=ref.sec_type, symbol=ref.symbol.upper(), exchange=ref.exchange, currency=ref.currency)
        await self.ib.qualifyContractsAsync(c)
        if not c.conId:
            raise ValueError("Instrument not found or ambiguous; check symbol / exchange / currency")
        if c.secType == "STK" or not c.exchange:
            c.exchange = "SMART"  # smart routing for stocks/ETFs, also when resolved from a position's conId
        return c

    def _ticker_for(self, c: Contract) -> Ticker:
        for key, mc in self.market_contracts.items():
            if mc.conId == c.conId and key in self.tickers:
                return self.tickers[key]
        if c.conId not in self.extra_tickers:
            self.extra_tickers[c.conId] = self.ib.reqMktData(c)
        return self.extra_tickers[c.conId]

    async def book(self, ref: InstrumentRef, rows: int = 10) -> dict:
        """L1 quote + L2 order book (depth needs a depth-of-book subscription; one book at a time)."""
        c = await self.resolve_instrument(ref)
        t = self._ticker_for(c)
        now = time.monotonic()
        smart = c.exchange == "SMART"
        if c.conId in self.depth:
            dc, dt_, _, sm = self.depth[c.conId]
            self.depth[c.conId] = (dc, dt_, now, sm)
        else:
            for other in list(self.depth):  # IB allows only a few depth subscriptions; keep one
                oc, _, _, osm = self.depth.pop(other)
                self.ib.cancelMktDepth(oc, isSmartDepth=osm)
            self.depth[c.conId] = (c, self.ib.reqMktDepth(c, numRows=rows, isSmartDepth=smart), now, smart)
        depth_ticker = self.depth[c.conId][1]
        data_codes = (354, 10092, 10089, 2152, 309, 10167)
        errors = [e for e in self.errors if e.get("symbol") == c.symbol and e.get("code") in data_codes]
        return {
            "con_id": c.conId, "label": describe(c), "sec_type": c.secType, "exchange": c.exchange,
            "currency": c.currency, "multiplier": float(c.multiplier or 1),
            "bid": pos(t.bid), "ask": pos(t.ask), "last": pos(t.last), "close": pos(t.close),
            "bid_size": num(t.bidSize), "ask_size": num(t.askSize), "market_data_type": t.marketDataType,
            "bids": [{"price": d.price, "size": d.size, "mm": d.marketMaker} for d in depth_ticker.domBids],
            "asks": [{"price": d.price, "size": d.size, "mm": d.marketMaker} for d in depth_ticker.domAsks],
            "messages": [e["msg"] for e in errors][-3:],
        }

    # ---------- orders ----------
    def _trade_row(self, tr: Trade) -> dict:
        o, st, c = tr.order, tr.orderStatus, tr.contract
        return {
            "order_id": o.orderId, "perm_id": o.permId, "client_id": o.clientId, "label": describe(c),
            "action": o.action, "quantity": num(o.totalQuantity), "type": o.orderType,
            "limit": num(o.lmtPrice), "tif": o.tif, "status": st.status, "filled": num(st.filled),
            "remaining": num(st.remaining), "avg_fill": num(st.avgFillPrice),
            "cancellable": st.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive")
            and o.clientId == self.s.dashboard_client_id,
            "time": tr.log[0].time.isoformat(timespec="seconds") if tr.log else None,
            "last_message": tr.log[-1].message if tr.log else "",
        }

    def orders(self) -> dict:
        ok, why = self.orders_enabled
        trades = sorted(self.ib.trades(), key=lambda t: t.log[0].time if t.log else dt.datetime.min, reverse=True)
        return {
            "session": self.mode.value, "orders_enabled": ok, "orders_blocked_reason": why,
            "open": [self._trade_row(t) for t in self.ib.openTrades()],
            "recent": [self._trade_row(t) for t in trades[:30]],
        }

    async def preview_order(self, req: OrderRequest) -> dict:
        c = await self.resolve_instrument(req.instrument)
        if c.secType in ("CMDTY", "CONTFUT", "IND"):
            raise ValueError(f"{c.secType} contracts can't be traded here")
        order = LimitOrder(req.action, req.quantity, req.limit_price, tif=req.tif, outsideRth=req.outside_rth)
        order.account = self.account or ""
        mult = float(c.multiplier or 1)
        notional = req.quantity * req.limit_price * mult
        rate = self.fx_to_base().get(c.currency)
        notional_base = notional * rate if rate else None
        blocked = None
        try:
            assert_session_can_trade(self.s, self.mode, self.ib.managedAccounts())
            if notional_base is None:
                raise OrderBlockedError(f"No FX rate for {c.currency}; can't check the order value limit.")
            assert_within_limits(self.s, req.quantity, notional_base, c.secType)
        except OrderBlockedError as e:
            blocked = str(e)
        what_if, what_if_error = None, None
        try:
            if blocked:  # don't touch IB's order system at all for a blocked order (e.g. live while disabled)
                raise RuntimeError("skipped")
            st = await asyncio.wait_for(self.ib.whatIfOrderAsync(c, order), 15)
            what_if = {
                "commission": num(st.commission), "min_commission": num(st.minCommission),
                "max_commission": num(st.maxCommission), "commission_currency": st.commissionCurrency,
                "init_margin_change": num(st.initMarginChange), "maint_margin_change": num(st.maintMarginChange),
                "equity_with_loan_after": num(st.equityWithLoanAfter), "warning": st.warningText or None,
            }
        except Exception as e:  # noqa: BLE001
            if not blocked:
                recent = [x["msg"] for x in list(self.errors)[-3:]]
                what_if_error = f"What-if preview unavailable ({type(e).__name__}). {' | '.join(recent)}"
        preview_id = secrets.token_urlsafe(16)
        if not blocked:
            self.pending[preview_id] = {"created": time.monotonic(), "contract": c, "order": order, "req": req,
                                        "notional_base": notional_base}
        self._audit("preview", c, req, notional_base, blocked=blocked)
        return {
            "preview_id": None if blocked else preview_id, "session": self.mode.value, "blocked": blocked,
            "label": describe(c), "sec_type": c.secType, "exchange": c.exchange, "currency": c.currency,
            "action": req.action, "quantity": req.quantity, "limit_price": req.limit_price, "tif": req.tif,
            "multiplier": mult, "notional": notional, "notional_base": notional_base,
            "base_currency": self.base_currency(), "what_if": what_if, "what_if_error": what_if_error,
            "expires_in": PREVIEW_TTL, "confirm_word": "LIVE" if self.mode is TradingMode.LIVE else None,
        }

    async def submit_order(self, preview_id: str, confirm: str | None) -> dict:
        p = self.pending.pop(preview_id, None)
        if p is None or time.monotonic() - p["created"] > PREVIEW_TTL:
            raise OrderBlockedError("Preview expired or unknown. Preview the order again.")
        if self.mode is TradingMode.LIVE and confirm != "LIVE":
            raise OrderBlockedError('Live order not confirmed: type "LIVE" to confirm.')
        c, order, req = p["contract"], p["order"], p["req"]
        # Re-check everything at submit time (settings/kill switch may have changed since the preview).
        assert_session_can_trade(self.s, self.mode, self.ib.managedAccounts())
        assert_within_limits(self.s, req.quantity, p["notional_base"], c.secType)
        trade = self.ib.placeOrder(c, order)
        self._audit("submit", c, req, p["notional_base"], order_id=order.orderId)
        await asyncio.sleep(1.0)
        return self._trade_row(trade)

    def cancel_order(self, order_id: int) -> dict:
        for tr in self.ib.openTrades():
            if tr.order.orderId == order_id and tr.order.clientId == self.s.dashboard_client_id:
                self.ib.cancelOrder(tr.order)
                self._audit("cancel", tr.contract, None, None, order_id=order_id)
                return self._trade_row(tr)
        raise KeyError(order_id)

    def _audit(self, event: str, c: Contract, req: OrderRequest | None, notional_base, **extra) -> None:
        self.s.audit_log.parent.mkdir(exist_ok=True)
        rec = {"time": _now(), "event": event, "session": self.mode.value, "contract": describe(c),
               "con_id": c.conId, "order": req.model_dump(exclude={"instrument"}) if req else None,
               "notional_base": notional_base, **extra}
        with self.s.audit_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


class SessionManager:
    """Holds the paper and live sessions; the UI picks one per request (?session=paper|live)."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.watchlist = Watchlist.load(settings.watchlist_file)
        self.sessions = {m.value: Session(settings, m, self.watchlist) for m in (TradingMode.PAPER, TradingMode.LIVE)}
        self.flex_data: dict | None = None

    def start(self) -> None:
        for s in self.sessions.values():
            s.start()

    async def stop(self) -> None:
        for s in self.sessions.values():
            await s.stop()

    def get(self, name: str) -> Session:
        return self.sessions[name]

    def overview(self) -> list[dict]:
        return [
            {"session": k, "connected": s.ib.isConnected(), "port": s.port,
             "orders_enabled": s.orders_enabled[0], "error": s.last_connect_error}
            for k, s in self.sessions.items()
        ]

    def groups(self) -> list[dict]:
        return [{"name": g.name, "unit": g.unit, "reference": g.reference, "keys": [p.key for p in g.products]}
                for g in self.watchlist.groups]

    # Flex history is per IBKR login (token), independent of the Gateway session.
    async def refresh_flex(self) -> dict:
        if not (self.s.flex_token and self.s.flex_query_id):
            raise RuntimeError("Flex not configured: set IB_FLEX_TOKEN and IB_FLEX_QUERY_ID in .env")
        xml = await asyncio.to_thread(flex.fetch_statement, self.s.flex_token.get_secret_value(), self.s.flex_query_id)
        self.s.data_dir.mkdir(exist_ok=True)
        (self.s.data_dir / "flex_latest.xml").write_text(xml, encoding="utf-8")
        self.flex_data = None
        return self.flex_summary()

    def flex_summary(self) -> dict | None:
        if self.flex_data is None:
            path = self.s.data_dir / "flex_latest.xml"
            if not path.exists():
                return None
            xml = path.read_text(encoding="utf-8")
            trades, fees = flex.parse_trades(xml), flex.parse_fees(xml)
            fee_by_type: dict[str, float] = {}
            for r in fees:
                fee_by_type[r["type"]] = fee_by_type.get(r["type"], 0) + r["amount_base"]
            self.flex_data = {
                "as_of": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes"),
                "summary": costs.summarize_trades(trades),
                "fees": [{"type": k, "amount": v} for k, v in sorted(fee_by_type.items(), key=lambda x: -x[1])],
                "first": min((r["date"] for r in trades), default=None),
                "last": max((r["date"] for r in trades), default=None),
            }
        return self.flex_data
