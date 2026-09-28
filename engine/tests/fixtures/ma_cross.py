"""A tiny NON-channel strategy for the strategy-contract tests (S1): two simple moving averages crossing.

Decision on the closed H1 bar ``t`` (only bars ``<= t`` and H4 bars closed at ``open_t + 1h``):

* ``fast_t`` / ``slow_t`` = plain means of the last ``fast`` / ``slow`` H1 closes (``sum(...) / n`` on Python
  floats, so the single-pass scan below gives bit-identical values);
* buy when ``fast_{t-1} <= slow_{t-1}`` and ``fast_t > slow_t`` (cross up), sell on the mirror cross down;
* optional H4 filter: a buy needs the last CLOSED H4 bar to be bullish (close >= open), a sell bearish; no
  closed H4 bar yet -> no decision;
* SL = lowest low of the last ``sl_lookback`` H1 bars - ``sl_buffer`` (buy) / highest high + ``sl_buffer``
  (sell); a candidate whose SL is not on the risk side of the close is dropped.

``setup = "ma_cross"`` (``"ma_fade"`` for the direction-flipped twin), ``line = None``, ``setup_title_fa`` set.
The first decision needs ``slow + 1`` closes: ``first_valid_index = slow``. No warm-up margin (plain means).

Worked example (fast 2, slow 3): closes 10, 11, 9, 12 -> at t=3 fast = (9+12)/2 = 10.5, slow = (11+9+12)/3
= 10.667 (no cross yet: fast < slow); t=2: fast (11+9)/2 = 10 vs slow 10 -> equal; a later close 14 gives
fast 13 > slow 11.667 with fast_{t-1} 10.5 <= slow_{t-1} 10.667 -> cross up -> buy.

:class:`MaCrossFast` overrides ``scan`` with one O(n) pass (the pattern a plugin should follow); the tests
prove it returns exactly what the default per-bar ``Strategy.scan`` returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategy.base import H4_DURATION, Strategy, StrategyContext, bar_open_times
from alpha_engine.strategy.params import ParamSchema, ParamSpec, ParamValue, params_hash
from alpha_engine.strategy.signal import SignalCandidate

SCHEMA = ParamSchema([
    ParamSpec(name="fast", type="int", default=3, min=1, max=50, label_fa="دوره میانگین سریع"),
    ParamSpec(name="slow", type="int", default=8, min=2, max=200, label_fa="دوره میانگین کند"),
    ParamSpec(name="sl_lookback", type="int", default=5, min=1, max=50, label_fa="کندل‌های حد ضرر"),
    ParamSpec(name="sl_buffer", type="float", default=0.3, min=0.0, max=100.0, label_fa="فاصله حد ضرر"),
    ParamSpec(name="use_h4_filter", type="bool", default=True, label_fa="فیلتر جهت کندل H4"),
])
TITLE_FA = {"ma_cross": "کراس میانگین‌های متحرک", "ma_fade": "خلاف کراس میانگین‌ها"}
_H1 = timedelta(hours=1)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


class MaCross(Strategy):
    name: ClassVar[str] = "ma_cross"
    version: ClassVar[int] = 1
    title_fa: ClassVar[str] = "کراس دو میانگین (آزمایشی)"
    param_schema: ClassVar[ParamSchema] = SCHEMA
    flip: ClassVar[bool] = False  # MaCrossFade: trade against the cross

    @classmethod
    def validate_params(cls, values: Mapping[str, Any] | None) -> tuple[dict[str, ParamValue], list[str]]:
        clean, errors = cls.param_schema.validate(values)
        if not errors and not clean["fast"] < clean["slow"]:
            return {}, ["پارامتر «دوره میانگین سریع» باید کوچک‌تر از «دوره میانگین کند» باشد."]
        return clean, errors

    def first_valid_index(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None) -> int | None:
        slow = int(self.clean_params(params)["slow"])
        return slow if len(h1) > slow else None

    # ------------------------------------------------------------------ decision on one closed bar
    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        if ctx.has_open_trade:
            return None
        p = self.clean_params(ctx.params)
        h1 = ctx.h1
        slow, fast = int(p["slow"]), int(p["fast"])
        if len(h1) < slow + 1:
            return None
        closes = h1["close"].to_numpy(dtype=np.float64)[-(slow + 1):].tolist()
        lows = h1["low"].to_numpy(dtype=np.float64)[-int(p["sl_lookback"]):].tolist()
        highs = h1["high"].to_numpy(dtype=np.float64)[-int(p["sl_lookback"]):].tolist()
        h4_bull = None
        if len(ctx.h4):
            last = ctx.h4.iloc[-1]
            h4_bull = bool(float(last["close"]) >= float(last["open"]))
        open_t = pd.Timestamp(bar_open_times(h1)[-1])
        return self._decide(ctx.symbol, open_t, closes, lows, highs, h4_bull, p, ctx.account)

    def _decide(self, symbol: str, open_t: pd.Timestamp, closes: list[float], lows: list[float],
                highs: list[float], h4_bull: bool | None, p: Mapping[str, Any],
                account: AccountSettings) -> SignalCandidate | None:
        fast, slow = int(p["fast"]), int(p["slow"])
        fast_now, slow_now = _mean(closes[-fast:]), _mean(closes[-slow:])
        fast_prev, slow_prev = _mean(closes[-fast - 1:-1]), _mean(closes[-slow - 1:-1])
        if fast_prev <= slow_prev and fast_now > slow_now:
            cross = "buy"
        elif fast_prev >= slow_prev and fast_now < slow_now:
            cross = "sell"
        else:
            return None
        direction = cross if not self.flip else ("sell" if cross == "buy" else "buy")
        if p["use_h4_filter"]:
            if h4_bull is None or h4_bull != (direction == "buy"):
                return None
        ref = closes[-1]
        buffer = float(p["sl_buffer"])
        sl = min(lows) - buffer if direction == "buy" else max(highs) + buffer
        if not (sl > 0 and ((direction == "buy" and sl < ref) or (direction == "sell" and sl > ref))):
            return None
        slug = "ma_fade" if self.flip else "ma_cross"
        open_dt = open_t.to_pydatetime()
        return SignalCandidate(
            strategy_name=self.name, strategy_version=self.version, params_hash=params_hash(dict(p)), symbol=symbol,
            direction=direction, setup=slug, line=None, setup_title_fa=TITLE_FA[slug],
            decision_time_utc=open_dt + _H1, confirmation_bar_open_utc=open_dt, reference_price=ref, stop_loss=sl,
            rr=account.rr, pattern=f"ma_cross_{'up' if cross == 'buy' else 'down'}",
            reason_fa=(f"میانگین {fast} کندلی ({fast_now:.5f}) میانگین {slow} کندلی ({slow_now:.5f}) را "
                       f"{'به بالا' if cross == 'buy' else 'به پایین'} قطع کرد."),
            extra={"fast_ma": fast_now, "slow_ma": slow_now, "fast_ma_prev": fast_prev, "slow_ma_prev": slow_prev,
                   "h4_bullish": h4_bull},
        )


class MaCrossFade(MaCross):
    """Same params (hence the same params hash) as :class:`MaCross`, opposite trades."""

    name: ClassVar[str] = "ma_fade"
    title_fa: ClassVar[str] = "خلاف کراس دو میانگین (آزمایشی)"
    flip: ClassVar[bool] = True


class MaCrossFast(MaCross):
    """:class:`MaCross` with a single-pass ``scan`` (must equal the default per-bar scan)."""

    def scan(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None, account: AccountSettings,
             *, symbol: str) -> list[SignalCandidate]:
        p = self.clean_params(params)
        slow, look = int(p["slow"]), int(p["sl_lookback"])
        times = bar_open_times(h1)
        closes = h1["close"].to_numpy(dtype=np.float64).tolist()
        lows = h1["low"].to_numpy(dtype=np.float64).tolist()
        highs = h1["high"].to_numpy(dtype=np.float64).tolist()
        h4_close_ns = bar_open_times(h4).as_unit("ns").asi8 + pd.Timedelta(H4_DURATION).value
        h4_bull_all = (h4["close"].to_numpy(dtype=np.float64) >= h4["open"].to_numpy(dtype=np.float64)).tolist()
        out: list[SignalCandidate] = []
        for t in range(slow, len(closes)):
            decision_ns = times[t].value + pd.Timedelta(_H1).value
            k = int(np.searchsorted(h4_close_ns, decision_ns, side="right"))
            h4_bull = bool(h4_bull_all[k - 1]) if k else None
            lo = max(0, t + 1 - look)
            cand = self._decide(symbol, times[t], closes[t - slow:t + 1], lows[lo:t + 1], highs[lo:t + 1], h4_bull,
                                p, account)
            if cand is not None:
                out.append(cand)
        return out


def plugin_class(base: type[MaCross], sha256: str, version: int = 1) -> type[MaCross]:
    """A plugin-flavoured copy of ``base`` (``source = "plugin"``, source hash, code version)."""
    return type(f"{base.__name__}Plugin{version}", (base,), {"source": "plugin", "code_sha256": sha256,
                                                             "version": version})


__all__ = ["SCHEMA", "TITLE_FA", "MaCross", "MaCrossFade", "MaCrossFast", "plugin_class"]
