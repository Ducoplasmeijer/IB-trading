"""Gold product watchlist (config/watchlist.yaml)."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from ..analysis.costs import CommissionModel


class Product(BaseModel):
    key: str
    name: str
    kind: Literal["spot", "future", "etf"]
    contract: dict
    what_to_show: str = "TRADES"
    multiplier: float = 1
    commission: CommissionModel = CommissionModel()
    typical_spread: float = 0.0
    expense_ratio: float = 0.0
    rolls_per_year: int = 0


class Watchlist(BaseModel):
    fx: dict | None = None
    reference: str = "XAUUSD"
    products: list[Product]

    @classmethod
    def load(cls, path: Path) -> "Watchlist":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
