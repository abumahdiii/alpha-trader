"""A "live" fake market for the live-signal tests: synthetic bars that appear as a test clock advances.

* :class:`MarketClock` holds the TRUE time (= the broker server clock, in UTC). The PC clock the engine sees is
  ``true + pc_ahead_s`` (``pc_clock``), so a PC clock running ahead of the server can be simulated.
* :class:`LiveFakeMt5` (read-only :class:`~fixtures.fake_mt5.FakeMt5`) serves only bars that have STARTED at the
  true time (the bar in progress is returned with its full synthetic OHLC -- exactly why the adapter must drop
  it with the SERVER "now"); ``withhold`` hides chosen bars (late arrival at the broker); ``revise`` replaces a
  bar's prices (broker revision). ``symbol_info_tick`` returns the tick at the true time: during a bar its
  server time is the true time (bid = the bar's open, ask = bid + spread * point); while the market is closed,
  the last tick of the last bar (open + 3599 s, bid = its close).
* :class:`ScheduledBuy` -- a deterministic test strategy (a buy on chosen bars) for expiry / revision scenarios.
* :func:`seed_mt5_cache` writes the initial cache the way ``fetch_history`` would (a full MT5 fetch up to the
  true time + the symbol spec), so the scheduler's incremental updates are allowed.

Never a terminal, never the network, never the real ``data/``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from alpha_engine.data.schema import Timeframe
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.data.timezone import OffsetModel
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategy.base import Strategy, StrategyContext, bar_open_times
from alpha_engine.strategy.params import ParamSchema, ParamSpec, params_hash
from alpha_engine.strategy.signal import SignalCandidate

from .fake_mt5 import _TF_BY_CONST, FakeMt5
from .synthetic_ohlcv import MT5_RATES_DTYPE, SyntheticSeries, symbol_info_dict


@dataclass
class MarketClock:
    true_utc: datetime
    pc_ahead_s: float = 0.0

    def set(self, when: datetime) -> None:
        self.true_utc = when

    def advance(self, **kwargs: float) -> None:
        self.true_utc = self.true_utc + timedelta(**kwargs)

    def pc_clock(self) -> datetime:
        return self.true_utc + timedelta(seconds=self.pc_ahead_s)


class LiveFakeMt5(FakeMt5):
    """See the module docstring."""

    def __init__(self, series: Iterable[SyntheticSeries], clock: MarketClock, model: OffsetModel,
                 **kwargs: Any) -> None:
        series = list(series)
        super().__init__(series, **kwargs)
        self.clock = clock
        self.model = model
        self.withhold: set[tuple[str, Timeframe, int]] = set()  # (symbol, tf, server open epoch)
        self.ticks_off: set[str] = set()
        self.offline = False  # every data call fails like a lost terminal (returns None)
        self.rates = {k: v.copy() for k, v in self.rates.items()}

    def server_now(self) -> int:
        return int(self.model.utc_to_server([int(self.clock.true_utc.timestamp())])[0])

    def revise(self, symbol: str, tf: Timeframe, server_open: int, **prices: float) -> None:
        raw = self.rates[(symbol, tf)]
        idx = int(np.flatnonzero(raw["time"] == server_open)[0])
        for key, value in prices.items():
            raw[key][idx] = value

    def copy_rates_range(self, symbol: str, timeframe: int, date_from: Any, date_to: Any) -> np.ndarray | None:
        self._enter("copy_rates_range", (symbol, timeframe, date_from, date_to), {})
        try:
            if self.offline or symbol in self.fail_rates_for:
                return None
            tf = _TF_BY_CONST.get(timeframe)
            raw = self.rates.get((symbol, tf)) if tf is not None else None
            if raw is None:
                return None if symbol not in self.symbols else np.zeros(0, dtype=MT5_RATES_DTYPE)
            now = self.server_now()
            mask = (raw["time"] >= int(date_from)) & (raw["time"] <= int(date_to)) & (raw["time"] <= now)
            if self.withhold:
                hidden = np.array([(symbol, tf, int(t)) in self.withhold for t in raw["time"]], dtype=bool)
                mask &= ~hidden
            return raw[mask].copy()
        finally:
            self._exit()

    def symbol_info_tick(self, name: str) -> Any:
        self._enter("symbol_info_tick", (name,), {})
        self._exit()
        if self.offline or name not in self.symbols or name in self.ticks_off:
            return None
        raw = self.rates.get((name, Timeframe.H1))
        if raw is None or len(raw) == 0:
            return None
        now = self.server_now()
        started = raw[raw["time"] <= now]
        if len(started) == 0:
            return None
        bar = started[-1]
        point = 10.0 ** -symbol_info_dict(name)["digits"]
        if now < int(bar["time"]) + 3600:  # market open: the tick of this instant
            bid, when = float(bar["open"]), now
        else:  # closed since the end of that bar
            bid, when = float(bar["close"]), int(bar["time"]) + 3599
        ask = bid + int(bar["spread"]) * point
        return SimpleNamespace(time=when, time_msc=when * 1000, bid=bid, ask=ask, last=0.0, volume=0)


def seed_mt5_cache(service: Any, symbols: Iterable[str], start: datetime) -> None:
    """Full MT5 fetch of H1/H4 up to the clock's true time (+ the spec), like ``fetch_history``."""
    for symbol in symbols:
        for tf in (Timeframe.H1, Timeframe.H4):
            service.update(symbol, tf, start=start)
        service.cache.write_spec(SymbolSpec.from_mt5(symbol_info_dict(symbol)), "mt5")


_H1 = timedelta(hours=1)


class ScheduledBuy(Strategy):
    """Deterministic test strategy: a BUY on every H1 bar whose open time is in ``bars`` (ISO ``...Z``);
    SL = low - ``sl_buffer``, reference = close. O(n) scan == per-bar evaluate."""

    name: ClassVar[str] = "scheduled_buy"
    version: ClassVar[int] = 1
    title_fa: ClassVar[str] = "خرید زمان‌بندی‌شده (آزمایشی)"
    param_schema: ClassVar[ParamSchema] = ParamSchema([
        ParamSpec(name="sl_buffer", type="float", default=1.0, min=0.0, max=100.0, label_fa="فاصله حد ضرر"),
    ])
    bars: ClassVar[frozenset[str]] = frozenset()

    def _cand(self, symbol: str, open_t: pd.Timestamp, low: float, close: float, p: Mapping[str, Any],
              account: AccountSettings) -> SignalCandidate | None:
        if open_t.strftime("%Y-%m-%dT%H:%M:%SZ") not in self.bars:
            return None
        sl = low - float(p["sl_buffer"])
        if not 0 < sl < close:
            return None
        open_dt = open_t.to_pydatetime()
        return SignalCandidate(
            strategy_name=self.name, strategy_version=self.version, params_hash=params_hash(dict(p)), symbol=symbol,
            direction="buy", setup="scheduled", line=None, setup_title_fa="ستاپ زمان‌بندی‌شده",
            decision_time_utc=open_dt + _H1, confirmation_bar_open_utc=open_dt, reference_price=close, stop_loss=sl,
            rr=account.rr, pattern="scheduled", reason_fa="کندل زمان‌بندی‌شده آزمایشی", extra={"low": low})

    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        if ctx.has_open_trade or len(ctx.h1) == 0:
            return None
        p = self.clean_params(ctx.params)
        last = ctx.h1.iloc[-1]
        return self._cand(ctx.symbol, pd.Timestamp(bar_open_times(ctx.h1)[-1]), float(last["low"]),
                          float(last["close"]), p, ctx.account)

    def scan(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None, account: AccountSettings,
             *, symbol: str) -> list[SignalCandidate]:
        p = self.clean_params(params)
        times = bar_open_times(h1)
        lows, closes = h1["low"].tolist(), h1["close"].tolist()
        out = []
        for i, open_t in enumerate(times):
            cand = self._cand(symbol, pd.Timestamp(open_t), float(lows[i]), float(closes[i]), p, account)
            if cand is not None:
                out.append(cand)
        return out


__all__ = ["LiveFakeMt5", "MarketClock", "ScheduledBuy", "seed_mt5_cache", "utc"]


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)
