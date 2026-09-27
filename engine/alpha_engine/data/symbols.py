"""Typed contract specification of a symbol (from ``mt5.symbol_info``) and symbol-name validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

# Broker symbol names such as "XAUUSD.x", "BRNUSD.x", "US30#", "EURUSD-ECN". No path separators, so a
# validated name is also safe to use in a cache file name.
SYMBOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._#+\-]{0,31}$")

SPEC_FIELDS: tuple[str, ...] = (
    "name", "digits", "point", "trade_contract_size", "trade_tick_value", "trade_tick_size",
    "volume_min", "volume_step", "volume_max", "currency_profit", "currency_base", "description",
)


class InvalidSymbolName(ValueError):
    pass


def validate_symbol_name(name: str) -> str:
    text = (name or "").strip()
    if not SYMBOL_NAME_RE.fullmatch(text):
        raise InvalidSymbolName(f"invalid symbol name {name!r}")
    return text


class SymbolSpec(BaseModel):
    """Contract data needed for pricing and position sizing.

    Value of a 1.00 price move for 1 lot (decision D9, used from phase 2 on):
    ``trade_tick_value / trade_tick_size``; cross-check with ``trade_contract_size``.
    Worked example (XAUUSD-like): tick_size 0.01, tick_value 1.00 -> 100 USD per 1.00 move per lot,
    which equals the contract size of 100 oz.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    digits: int
    point: float
    trade_contract_size: float
    trade_tick_value: float
    trade_tick_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    currency_profit: str
    currency_base: str
    description: str = ""

    @classmethod
    def from_mt5(cls, info: Any) -> SymbolSpec:
        """Build from an ``mt5.symbol_info`` result (named tuple), a mapping or an attribute object."""
        if isinstance(info, Mapping):
            data = dict(info)
        elif hasattr(info, "_asdict"):
            data = dict(info._asdict())
        else:
            data = {f: getattr(info, f) for f in SPEC_FIELDS if hasattr(info, f)}
        return cls.model_validate({k: data[k] for k in SPEC_FIELDS if k in data})

    @property
    def value_per_price_unit(self) -> float:
        """Account-currency value of a 1.00 price move for 1 lot (D9)."""
        return self.trade_tick_value / self.trade_tick_size if self.trade_tick_size else 0.0
