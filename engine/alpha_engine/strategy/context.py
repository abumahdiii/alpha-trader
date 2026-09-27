"""Build a :class:`StrategyContext` from what the user stored (params + account settings) and the bars.

Callers (live signals, later the backtester's per-decision path) should not assemble contexts by hand:

* the strategy's **active params version** comes from the params store (``strategies_repo``, the same
  data ``PUT /strategies/{name}`` writes); the first access creates version 1 from the defaults;
* the **account settings** (balance, risk %, leverage, R:R) come from the settings store (the same row
  ``PUT /settings`` writes; defaults until the first update);
* **no look-ahead:** H4 bars are filtered to those CLOSED at the decision time
  (``open + 4h <= decision time``, :func:`~alpha_engine.strategy.base.slice_closed_bars`), where the
  decision time is the close of the last H1 bar. With ``now_utc`` (live use) H1 bars still forming at
  ``now_utc`` are dropped first (``open + 1h <= now_utc``); without it the caller guarantees that ``h1``
  holds closed bars only.

Worked example: H1 bars open 08:00..11:00 UTC -> decision time 12:00; H4 bars open 04:00 (closes 08:00),
08:00 (closes 12:00) and 12:00 (forming) -> the context gets the 04:00 and 08:00 bars only. With
``now_utc = 11:30`` the 11:00 H1 bar is still forming: decision time 11:00, H4 = the 04:00 bar only.

Provenance: ``params_hash(ctx.params)`` equals the active record's ``params_hash`` (the stored params are
the validated set); :func:`load_active_params` returns the record itself (params version, code version).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from ..data.symbols import SymbolSpec, validate_symbol_name
from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettingsRepo
from ..storage.db import EngineConnection
from .base import H1_DURATION, H4_DURATION, StrategyContext, decision_time_for, slice_closed_bars
from .registry import StrategyRegistry, default_registry

if TYPE_CHECKING:
    from ..storage.strategies_repo import StrategyVersionRecord
    from .base import Strategy

logger = get_logger(__name__)

DEFAULT_STRATEGY = "stddev_channel"


class NoClosedBarError(ValueError):
    """There is no closed H1 bar to decide on."""


def _registry(registry: StrategyRegistry | None) -> StrategyRegistry:
    if registry is not None:
        return registry
    from .. import strategies  # noqa: F401  (import registers the code-defined strategies)

    return default_registry


def load_active_params(
    db: EngineConnection,
    name: str = DEFAULT_STRATEGY,
    *,
    registry: StrategyRegistry | None = None,
) -> tuple[type[Strategy], StrategyVersionRecord]:
    """``(strategy class, active params record)``; raises ``UnknownStrategyError`` /
    ``StoredParamsInvalidError`` (never silently falls back to other params)."""
    from ..storage.strategies_repo import StrategiesRepo  # storage -> strategy import order (see storage/__init__)

    reg = _registry(registry)
    record = StrategiesRepo(db, reg).get_active(name)
    return reg.get(name), record


def build_context(
    symbol: str,
    h1: pd.DataFrame,
    h4: pd.DataFrame,
    *,
    db: EngineConnection,
    registry_name: str = DEFAULT_STRATEGY,
    has_open_trade: bool = False,
    symbol_spec: SymbolSpec | None = None,
    now_utc: datetime | None = None,
    registry: StrategyRegistry | None = None,
    indicators: Any = None,
) -> StrategyContext:
    """Context for deciding on the last closed H1 bar of ``h1`` with the stored params and settings.

    Raises :class:`NoClosedBarError` when no closed H1 bar is left, ``ValueError`` for a symbol/spec
    mismatch or naive timestamps, and the params-store errors of :func:`load_active_params`.
    """
    symbol = validate_symbol_name(symbol)
    if symbol_spec is not None and symbol_spec.name != symbol:
        raise ValueError(f"symbol_spec is for {symbol_spec.name!r}, not {symbol!r}")

    h1_closed = h1 if now_utc is None else slice_closed_bars(h1, H1_DURATION, now_utc)
    if len(h1_closed) == 0:
        raise NoClosedBarError(f"{symbol}: no closed H1 bar to decide on")
    decision_time = decision_time_for(h1_closed)
    h4_closed = slice_closed_bars(h4, H4_DURATION, decision_time)

    cls, record = load_active_params(db, registry_name, registry=registry)
    account = AccountSettingsRepo(db).get()
    ctx = StrategyContext(
        symbol=symbol,
        h1=h1_closed,
        h4=h4_closed,
        account=account,
        params=dict(record.params),
        has_open_trade=has_open_trade,
        indicators=dict(indicators or {}),
        symbol_spec=symbol_spec,
    )
    if is_dev_mode():
        logger.debug(
            "build_context %s @ %s: %s code v%d params v%d hash=%s account=%s h1=%d (forming dropped %d) "
            "h4=%d (not closed dropped %d) open_trade=%s spec=%s",
            symbol, decision_time.isoformat(), cls.name, cls.version, record.version, record.params_hash[:12],
            account.model_dump(), len(h1_closed), len(h1) - len(h1_closed), len(h4_closed),
            len(h4) - len(h4_closed), has_open_trade, None if symbol_spec is None else symbol_spec.name,
        )
    return ctx


__all__ = ["DEFAULT_STRATEGY", "NoClosedBarError", "build_context", "load_active_params"]
