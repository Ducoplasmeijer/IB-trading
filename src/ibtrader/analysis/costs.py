"""Trading-cost math. Pure functions, no IB connection, so everything here is unit-tested."""

import math
from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel


class CommissionModel(BaseModel):
    """Per-side commission estimate in the product's currency."""

    per_unit: float = 0.0  # per contract / per share
    pct: float = 0.0  # fraction of notional
    min: float = 0.0
    max_pct: float | None = None  # cap as fraction of notional

    def per_side(self, qty: float, price: float, multiplier: float) -> float:
        if qty <= 0:
            return 0.0
        notional = qty * price * multiplier
        c = max(self.per_unit * qty + self.pct * notional, self.min)
        if self.max_pct is not None:
            c = min(c, self.max_pct * notional)
        return c


@dataclass
class RoundTrip:
    qty: float
    notional: float
    commission: float
    spread: float

    @property
    def total(self) -> float:
        return self.commission + self.spread

    @property
    def bps(self) -> float | None:
        return self.total / self.notional * 1e4 if self.notional else None


def round_trip(model: CommissionModel, qty: float, price: float, multiplier: float, spread: float) -> RoundTrip:
    """Buy then sell: commission on both sides plus crossing the full bid/ask spread once (half per side)."""
    return RoundTrip(
        qty=qty,
        notional=qty * price * multiplier,
        commission=2 * model.per_side(qty, price, multiplier),
        spread=max(spread, 0.0) * qty * multiplier,
    )


def units_for_exposure(target_notional: float, price: float, multiplier: float, kind: str) -> int:
    """Whole units closest to the target. Futures round to nearest; shares round down (never overspend)."""
    unit = price * multiplier
    if unit <= 0 or target_notional <= 0:
        return 0
    return round(target_notional / unit) if kind == "future" else math.floor(target_notional / unit)


def annual_holding_cost(
    kind: str,
    notional: float,
    expense_ratio: float = 0.0,
    rolls_per_year: int = 0,
    cost_per_roll: float = 0.0,
) -> float:
    """ETFs: fund fee. Futures: roll transaction costs (financing/carry is reported separately)."""
    if kind == "etf":
        return notional * expense_ratio
    if kind == "future":
        return rolls_per_year * cost_per_roll
    return 0.0


def implied_annual_carry(future_price: float, spot_price: float, days_to_expiry: int) -> float | None:
    """Annualized premium of the future over spot (≈ interest rate minus lease rate)."""
    if not (future_price > 0 and spot_price > 0 and days_to_expiry > 0):
        return None
    return (future_price / spot_price - 1) * 365 / days_to_expiry


def tracking_difference(prod_start: float, prod_end: float, ref_start: float, ref_end: float) -> float | None:
    """Product return minus reference return over the same window (negative = lagging, e.g. fees)."""
    if min(prod_start, ref_start) <= 0:
        return None
    return (prod_end / prod_start) / (ref_end / ref_start) - 1


def summarize_trades(rows: list[dict]) -> dict:
    """Aggregate normalized trade rows (see flex.parse_trades / service executions) in base currency.

    Row keys: date (YYYYMMDD...), symbol, asset, notional_base, commission_base (positive = cost).
    """

    def bucket():
        return {"trades": 0, "notional": 0.0, "commission": 0.0}

    by_symbol: dict[str, dict] = defaultdict(bucket)
    by_asset: dict[str, dict] = defaultdict(bucket)
    by_month: dict[str, dict] = defaultdict(bucket)
    total = bucket()
    for r in rows:
        month = str(r["date"]).replace("-", "")[:6]
        for b in (by_symbol[r["symbol"]], by_asset[r["asset"]], by_month[month], total):
            b["trades"] += 1
            b["notional"] += r["notional_base"]
            b["commission"] += r["commission_base"]

    def finish(d: dict) -> dict:
        d["bps"] = d["commission"] / d["notional"] * 1e4 if d["notional"] else None
        return d

    return {
        "total": finish(total),
        "by_symbol": sorted(({"key": k, **finish(v)} for k, v in by_symbol.items()), key=lambda x: -x["commission"]),
        "by_asset": sorted(({"key": k, **finish(v)} for k, v in by_asset.items()), key=lambda x: -x["commission"]),
        "by_month": sorted(({"key": k, **finish(v)} for k, v in by_month.items()), key=lambda x: x["key"]),
    }
