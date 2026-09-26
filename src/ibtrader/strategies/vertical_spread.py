"""Vertical (debit) spread as a single IB combo (BAG) order: long lower strike, short higher strike.

Works for stock/ETF options (sec_type OPT, e.g. GLD on SMART) and futures options
(sec_type FOP, e.g. GC gold options on COMEX, trading class OG).
"""

from pathlib import Path
from typing import Literal

import yaml
from ib_async import IB, Bag, ComboLeg, Contract, FuturesOption, LimitOrder, Option, OrderState, Trade
from pydantic import BaseModel, model_validator

from ..config import Settings
from ..safety import assert_can_trade, assert_within_limits


class SpreadSpec(BaseModel):
    symbol: str
    sec_type: Literal["OPT", "FOP"]
    exchange: str
    currency: str = "USD"
    trading_class: str | None = None
    expiry: str  # YYYYMMDD
    right: Literal["C", "P"] = "C"
    long_strike: float
    short_strike: float
    quantity: int = 1
    limit_price: float  # net debit per spread, in option price points
    tif: Literal["DAY", "GTC"] = "DAY"

    @model_validator(mode="after")
    def _validate(self) -> "SpreadSpec":
        width = abs(self.short_strike - self.long_strike)
        if self.right == "C" and not self.long_strike < self.short_strike:
            raise ValueError("Bull call spread: long_strike must be below short_strike.")
        if self.right == "P" and not self.long_strike > self.short_strike:
            raise ValueError("Bear put spread: long_strike must be above short_strike.")
        if not 0 < self.limit_price < width:
            raise ValueError(f"limit_price must be a positive debit smaller than the strike width ({width}).")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SpreadSpec":
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def _leg(spec: SpreadSpec, strike: float) -> Contract:
    cls = FuturesOption if spec.sec_type == "FOP" else Option
    return cls(
        symbol=spec.symbol,
        lastTradeDateOrContractMonth=spec.expiry,
        strike=strike,
        right=spec.right,
        exchange=spec.exchange,
        currency=spec.currency,
        tradingClass=spec.trading_class or "",
    )


def qualify_legs(ib: IB, spec: SpreadSpec) -> tuple[Contract, Contract]:
    long_leg, short_leg = _leg(spec, spec.long_strike), _leg(spec, spec.short_strike)
    ib.qualifyContracts(long_leg, short_leg)
    for leg in (long_leg, short_leg):
        if not leg.conId:
            raise ValueError(f"Could not uniquely resolve contract {leg}. Check expiry/strike/trading_class.")
    return long_leg, short_leg


def build_combo(ib: IB, spec: SpreadSpec) -> tuple[Bag, float]:
    """Return the BAG contract and the contract multiplier."""
    long_leg, short_leg = qualify_legs(ib, spec)
    combo = Bag(
        symbol=spec.symbol,
        exchange=spec.exchange,
        currency=spec.currency,
        comboLegs=[
            ComboLeg(conId=long_leg.conId, ratio=1, action="BUY", exchange=spec.exchange),
            ComboLeg(conId=short_leg.conId, ratio=1, action="SELL", exchange=spec.exchange),
        ],
    )
    return combo, float(long_leg.multiplier or 100)


def max_cost(spec: SpreadSpec, multiplier: float) -> float:
    """Worst-case loss of a debit spread = debit paid (excluding commissions)."""
    return spec.limit_price * multiplier * spec.quantity


def max_profit(spec: SpreadSpec, multiplier: float) -> float:
    return (abs(spec.short_strike - spec.long_strike) - spec.limit_price) * multiplier * spec.quantity


def _order(spec: SpreadSpec) -> LimitOrder:
    return LimitOrder("BUY", spec.quantity, spec.limit_price, tif=spec.tif)


def preview(ib: IB, spec: SpreadSpec) -> tuple[OrderState, float]:
    """IB what-if: margin/commission impact without placing anything."""
    combo, multiplier = build_combo(ib, spec)
    return ib.whatIfOrder(combo, _order(spec)), multiplier


def place(ib: IB, settings: Settings, spec: SpreadSpec) -> Trade:
    """Place the combo order after all safety checks. Callers must obtain explicit user confirmation first."""
    assert_can_trade(settings, ib.managedAccounts())
    combo, multiplier = build_combo(ib, spec)
    assert_within_limits(settings, spec.quantity, max_cost(spec, multiplier))
    order = _order(spec)
    if settings.account:
        order.account = settings.account
    return ib.placeOrder(combo, order)
