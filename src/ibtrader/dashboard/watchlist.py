"""Metals watchlist (config/watchlist.yaml), grouped per metal."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

from ..analysis.costs import CommissionModel


class Product(BaseModel):
    key: str
    name: str
    kind: Literal["spot", "future", "etf"]
    contract: dict
    group: str = ""
    what_to_show: str = "TRADES"
    multiplier: float = 1
    commission: CommissionModel = CommissionModel()
    typical_spread: float = 0.0
    expense_ratio: float = 0.0
    rolls_per_year: int = 0


class Group(BaseModel):
    name: str
    unit: str = ""
    reference: str | None = None
    products: list[Product]


class Watchlist(BaseModel):
    fx: dict | None = None
    groups: list[Group]

    @model_validator(mode="after")
    def _validate(self) -> "Watchlist":
        seen: set[str] = set()
        for g in self.groups:
            keys = {p.key for p in g.products}
            if g.reference and g.reference not in keys:
                raise ValueError(f"Group {g.name}: reference {g.reference} is not one of its products")
            for p in g.products:
                if p.key in seen:
                    raise ValueError(f"Duplicate product key {p.key}")
                seen.add(p.key)
                p.group = g.name
        return self

    @property
    def products(self) -> dict[str, Product]:
        return {p.key: p for g in self.groups for p in g.products}

    def group(self, name: str) -> Group:
        for g in self.groups:
            if g.name.lower() == name.lower():
                return g
        raise KeyError(name)

    @classmethod
    def load(cls, path: Path) -> "Watchlist":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
