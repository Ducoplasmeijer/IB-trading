"""Read-only portfolio / account snapshot."""

from dataclasses import dataclass

from ib_async import IB

SUMMARY_TAGS = (
    "NetLiquidation",
    "TotalCashValue",
    "BuyingPower",
    "AvailableFunds",
    "MaintMarginReq",
    "UnrealizedPnL",
    "RealizedPnL",
)


@dataclass
class PositionRow:
    account: str
    symbol: str
    sec_type: str
    description: str
    quantity: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float


def account_summary(ib: IB) -> dict[str, dict[str, str]]:
    """{account: {tag: 'value currency'}} for the key summary tags."""
    out: dict[str, dict[str, str]] = {}
    for v in ib.accountSummary():
        if v.tag in SUMMARY_TAGS and v.currency:
            out.setdefault(v.account, {})[v.tag] = f"{float(v.value):,.2f} {v.currency}"
    return out


def positions(ib: IB) -> list[PositionRow]:
    rows = []
    for p in ib.portfolio():
        c = p.contract
        rows.append(
            PositionRow(
                account=p.account,
                symbol=c.symbol,
                sec_type=c.secType,
                description=c.localSymbol or c.symbol,
                quantity=p.position,
                avg_cost=p.averageCost,
                market_price=p.marketPrice,
                market_value=p.marketValue,
                unrealized_pnl=p.unrealizedPNL,
            )
        )
    return rows
