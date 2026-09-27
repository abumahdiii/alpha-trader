"""``StdDevChannelStrategy`` -- trading system 1 (stddev-channel-system.md), registered as ``stddev_channel``.

Decision on the closed H1 bar ``t`` (decision time ``D = open_t + 1h``):

1. indicators (``setups.compute_channel_bars``): channel of the last H4 bar closed by ``D`` projected to
   ``t``; flatness from ATR_H4; ATR_H1; reversal patterns;
2. setups (``setups.bounce_hits`` + ``state_machine.breakout_hits``), direction filter by the channel at t;
3. one candidate by priority (``state_machine.PRIORITY``); both directions at once -> none;
4. levels (``levels``): SL from bar t's low/high -/+ ATR_H1 * sl_atr_mult; entry = open of t+1 (not known
   now -> ``reference_price = close_t``); TP from ``rr`` of the account settings.

``evaluate(ctx)`` is a pure function of ``ctx`` (bars <= t). ``scan`` computes the indicators once over a
whole history and returns what ``evaluate`` would return at every bar, IGNORING the open-trade state (the
backtester applies "max one open trade" itself).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from ...logging_setup import get_logger, is_dev_mode
from ...storage.account_settings import AccountSettings
from ...strategy.base import H4_DURATION, Strategy, StrategyContext, bar_open_times, slice_closed_bars
from ...strategy.params import ParamSchema, ParamValue, params_hash
from ...strategy.registry import register
from ...strategy.signal import SignalCandidate
from .levels import stop_loss_price
from .params import SCHEMA, InvalidParamsError, StdDevParams, resolve_params
from .setups import (
    CHANNEL_DIR_FA,
    LINE_FA,
    PATTERN_FA,
    SETUP_TITLE_FA,
    SIDE_FA,
    ChannelBars,
    SetupHit,
    any_touch,
    compute_channel_bars,
    with_patterns,
)
from .state_machine import PRIORITY, Decision, decide_at

logger = get_logger(__name__)

_H1 = timedelta(hours=1)


def _closed_h4(h4: pd.DataFrame, decision_time: pd.Timestamp) -> pd.DataFrame:
    """Rows of ``h4`` with ``open + 4h <= decision_time`` (``slice_closed_bars``, via searchsorted).

    Rows must be sorted by open time (``mtf`` rejects unsorted input later anyway); for unsorted input
    this falls back to the boolean-mask slice.
    """
    times = bar_open_times(h4)
    if not times.is_monotonic_increasing:
        return slice_closed_bars(h4, H4_DURATION, decision_time)
    count = int(np.searchsorted(times + H4_DURATION, decision_time, side="right"))
    return h4 if count == len(h4) else h4.iloc[:count]


def _fmt(value: float) -> str:
    """Price/number for reason_fa: up to 5 decimals, trailing zeros trimmed."""
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _pattern_slug(joined: str) -> str:
    """``bullish_pin_bar+bullish_engulfing`` -> ``pin_bar_and_engulfing`` (SignalCandidate.pattern)."""
    parts = [part.split("_", 1)[1] for part in joined.split("+") if part]
    return "_and_".join(parts)


def _pattern_fa(joined: str) -> str:
    return " و ".join(PATTERN_FA[part] for part in joined.split("+") if part)


def _ts(cb: ChannelBars, i: int) -> pd.Timestamp:
    return cb.times[i]


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M UTC")


class StdDevChannelStrategy(Strategy):
    """StdDev (linear regression) channel on H4, touches/breakouts/confirmations on H1."""

    name: ClassVar[str] = "stddev_channel"
    version: ClassVar[int] = 1
    title_fa: ClassVar[str] = "کانال انحراف معیار"
    param_schema: ClassVar[ParamSchema] = SCHEMA

    @classmethod
    def validate_params(cls, values: Mapping[str, Any] | None) -> tuple[dict[str, ParamValue], list[str]]:
        """Schema + cross-field rules of :func:`resolve_params` (e.g. at least one confirmation pattern)."""
        try:
            _, clean = resolve_params(values)
        except InvalidParamsError as exc:
            return {}, list(exc.errors_fa)
        return clean, []

    # ------------------------------------------------------------------ evaluate (one closed bar)
    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        if ctx.has_open_trade:
            if is_dev_mode():
                logger.debug("%s %s: open trade exists -> no new suggestion", self.name, ctx.symbol)
            return None
        p, clean = resolve_params(ctx.params)
        h1 = ctx.h1
        if len(h1) == 0:
            return None
        decision_time = ctx.decision_time_utc
        # Defensive: never use an H4 bar that is still forming at D, even if a caller passed one
        # (same rule as strategy.base.slice_closed_bars: open + 4h <= D).
        h4 = _closed_h4(ctx.h4, decision_time)
        if is_dev_mode() and len(h4) != len(ctx.h4):
            logger.debug("%s %s @ %s: dropped %d H4 bar(s) not closed at decision time",
                         self.name, ctx.symbol, decision_time.isoformat(), len(ctx.h4) - len(h4))
        t = len(h1) - 1
        # Only the last `history_bars` bars are read by the setup logic; patterns only at t, and only
        # when a setup is still possible (direction allowed and some line touched) -- pure speed-ups.
        cb = compute_channel_bars(h1, h4, p, start=t - p.history_bars + 1, pattern_from=None)
        patterns_checked = bool((cb.buy_ok[t] or cb.sell_ok[t]) and any_touch(cb, t, p.touch_lookback))
        if patterns_checked:
            cb = with_patterns(cb, p, t)
        decision = decide_at(cb, t, p)
        _log_decision(self.name, ctx.symbol, cb, decision, patterns_checked)
        return _candidate(self, ctx.symbol, cb, decision, p, params_hash(clean), ctx.account)

    # ------------------------------------------------------------------ scan (whole history at once)
    def scan(
        self,
        h1: pd.DataFrame,
        h4: pd.DataFrame,
        params: Mapping[str, Any] | None,
        account: AccountSettings,
        *,
        symbol: str,
    ) -> list[SignalCandidate]:
        """Candidates for every H1 bar of ``h1`` (chronological), ignoring the open-trade state.

        ``h4`` may be the full H4 history: at each H1 bar only H4 bars closed at that bar's decision
        time are used. The result at bar ``t`` equals ``evaluate`` on ``h1[:t+1]`` and
        ``slice_closed_bars(h4, 4h, open_t + 1h)`` with ``has_open_trade=False`` (tests/test_no_lookahead.py).

        Exact: ``rolling_regression_channel`` is bit-identical on a prefix and on the full history (no
        BLAS products), so every field -- channel floats in ``extra`` included -- equals ``evaluate``.
        """
        p, clean = resolve_params(params)
        if len(h1) == 0:
            return []
        cb = compute_channel_bars(h1, h4, p, pattern_from=0)
        phash = params_hash(clean)
        maybe = np.flatnonzero((cb.buy_ok & cb.bull) | (cb.sell_ok & cb.bear))  # confirmation needed
        out: list[SignalCandidate] = []
        conflicts = 0
        for t in maybe:
            decision = decide_at(cb, int(t), p)
            _log_decision(self.name, symbol, cb, decision, True)
            conflicts += decision.note == "direction_conflict"
            cand = _candidate(self, symbol, cb, decision, p, phash, account)
            if cand is not None:
                out.append(cand)
        if is_dev_mode():
            logger.debug("%s %s scan: bars=%d evaluated=%d candidates=%d direction_conflicts=%d",
                         self.name, symbol, len(cb), len(maybe), len(out), conflicts)
        return out

    def indicator_frame(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None) -> pd.DataFrame:
        """Per-H1-bar channel lines, ATRs, flags and patterns (for charts / debugging)."""
        p, _ = resolve_params(params)
        return compute_channel_bars(h1, h4, p).to_frame()


register(StdDevChannelStrategy)


def scan(h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None, account: AccountSettings,
         *, symbol: str) -> list[SignalCandidate]:
    """Module-level shortcut for :meth:`StdDevChannelStrategy.scan`."""
    return StdDevChannelStrategy().scan(h1, h4, params, account, symbol=symbol)


# ---------------------------------------------------------------------- candidate construction
def _line_values(cb: ChannelBars, i: int) -> dict[str, float]:
    return {"upper": float(cb.upper[i]), "mid": float(cb.mid[i]), "lower": float(cb.lower[i])}


def _candidate(strategy: StdDevChannelStrategy, symbol: str, cb: ChannelBars, decision: Decision,
               p: StdDevParams, phash: str, account: AccountSettings) -> SignalCandidate | None:
    hit = decision.chosen
    if hit is None:
        return None
    t = decision.t
    side = hit.side
    atr = float(cb.atr_h1[t])
    sl = stop_loss_price(side, float(cb.high[t]), float(cb.low[t]), atr, p.sl_atr_mult)
    ref = float(cb.close[t])
    if not (sl > 0 and ((side == "buy" and sl < ref) or (side == "sell" and sl > ref))):
        if is_dev_mode():
            logger.debug("%s %s @ %s: %s rejected, SL %.5f not on the risk side of close %.5f",
                         strategy.name, symbol, _iso(_ts(cb, t) + _H1), hit.setup.value, sl, ref)
        return None
    joined = str(cb.bull_pattern[t] if side == "buy" else cb.bear_pattern[t])
    extra = _extra(cb, decision, p, sl)
    open_t = _ts(cb, t).to_pydatetime()
    return SignalCandidate(
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        params_hash=phash,
        symbol=symbol,
        direction=side,
        setup=hit.setup,
        line=hit.line,
        decision_time_utc=open_t + _H1,
        confirmation_bar_open_utc=open_t,
        reference_price=ref,
        stop_loss=sl,
        rr=account.rr,
        pattern=_pattern_slug(joined),
        reason_fa=_reason_fa(cb, decision, p, sl, joined),
        extra=extra,
    )


def _extra(cb: ChannelBars, decision: Decision, p: StdDevParams, sl: float) -> dict[str, Any]:
    """Indicator values the decision used (traceability, rule 01 section 2). All finite."""
    t = decision.t
    hit = decision.chosen
    assert hit is not None
    touch_i = t - hit.touch_offset
    line_vals = cb.line(hit.line)
    extra: dict[str, Any] = {
        "channel_direction": cb.channel_direction(t),
        "line_value": float(line_vals[t]),
        **_line_values(cb, t),
        "slope_per_h4_bar": float(cb.slope[t]),
        "sigma": float(cb.sigma[t]),
        "channel_change_abs": float(abs(cb.slope[t]) * p.n),
        "atr_h4": float(cb.atr_h4[t]),
        "flat_threshold": float(cb.atr_h4[t] * p.flat_mult),
        "is_flat": bool(cb.is_flat[t]),
        "h4_bar_open_utc": _iso(pd.Timestamp(int(cb.h4_open[t]), tz="UTC")),
        "bars_ahead": float(cb.bars_ahead[t]),
        "atr_h1": float(cb.atr_h1[t]),
        "touch_tol": float(cb.tol[t]),
        "sl_buffer": float(cb.atr_h1[t] * p.sl_atr_mult),
        "bar_open": float(cb.open[t]),
        "bar_high": float(cb.high[t]),
        "bar_low": float(cb.low[t]),
        "bar_close": float(cb.close[t]),
        "touch_bar_open_utc": _iso(_ts(cb, touch_i)),
        "touch_offset": int(hit.touch_offset),
        "touch_bar_line_value": float(line_vals[touch_i]),
        "touch_bar_tol": float(cb.tol[touch_i]),
        "priority_rank": int(_priority_rank(hit)),
        "other_setups": ",".join(f"{h.setup.value}:{h.side}" for h in decision.others),
    }
    if hit.breakout_bar is not None:
        b = hit.breakout_bar
        extra.update({
            "breakout_bar_open_utc": _iso(_ts(cb, b)),
            "breakout_close": float(cb.close[b]),
            "breakout_line_value": float(line_vals[b]),
            "bars_since_breakout": int(t - b),
            "pullback_window": int(p.pullback_window),
        })
    if hit.approach_bar is not None:
        a = hit.approach_bar
        extra.update({
            "approach_bar_open_utc": _iso(_ts(cb, a)),
            "approach_close": float(cb.close[a]),
            "approach_mid": float(cb.mid[a]),
        })
    return extra


def _priority_rank(hit: SetupHit) -> int:
    return PRIORITY.index(hit.setup) + 1


def _reason_fa(cb: ChannelBars, decision: Decision, p: StdDevParams, sl: float, joined: str) -> str:
    t = decision.t
    hit = decision.chosen
    assert hit is not None
    side_fa = SIDE_FA[hit.side]
    line_fa = LINE_FA[hit.line]
    direction = cb.channel_direction(t)
    title = SETUP_TITLE_FA[hit.setup]
    if hit.setup.value == "bounce_mid":
        title += " (رسیدن از بالا)" if hit.side == "buy" else " (رسیدن از پایین)"
    touch_i = t - hit.touch_offset
    line_vals = cb.line(hit.line)
    parts = [
        f"{side_fa} — ستاپ «{title}» در کانال {CHANNEL_DIR_FA[direction]} "
        f"(شیب {_fmt(float(cb.slope[t]))} در هر کندل H4، تغییر کل {_fmt(abs(float(cb.slope[t])) * p.n)} "
        f"در برابر آستانه افقی {_fmt(float(cb.atr_h4[t]) * p.flat_mult)}).",
    ]
    if hit.breakout_bar is not None:
        b = hit.breakout_bar
        parts.append(
            f"کندل {_iso(_ts(cb, b))} بالای {line_fa} بسته شد ({_fmt(float(cb.close[b]))} در برابر {_fmt(float(line_vals[b]))})"
            if hit.side == "buy" else
            f"کندل {_iso(_ts(cb, b))} زیر {line_fa} بسته شد ({_fmt(float(cb.close[b]))} در برابر {_fmt(float(line_vals[b]))})"
        )
        parts[-1] += f" و قیمت پس از {t - b} کندل H1 (حداکثر {p.pullback_window}) برای پولبک به خط برگشت."
    touch_word = "همین کندل" if hit.touch_offset == 0 else f"کندل {_iso(_ts(cb, touch_i))}"
    parts.append(
        f"{touch_word} {line_fa} ({_fmt(float(line_vals[touch_i]))}) را با فاصله مجاز "
        f"ATR(H1)×{_fmt(p.touch_atr_mult)} = {_fmt(float(cb.tol[touch_i]))} لمس کرد."
    )
    parts.append(
        f"کندل تایید {_iso(_ts(cb, t))}: {_pattern_fa(joined)}، close = {_fmt(float(cb.close[t]))}."
    )
    extreme = "کف" if hit.side == "buy" else "سقف"
    sign = "−" if hit.side == "buy" else "+"
    bar_extreme = float(cb.low[t]) if hit.side == "buy" else float(cb.high[t])
    parts.append(
        f"حد ضرر = {extreme} کندل تایید {_fmt(bar_extreme)} {sign} ATR(H1) {_fmt(float(cb.atr_h1[t]))}×{_fmt(p.sl_atr_mult)} "
        f"= {_fmt(sl)}. ورود با open کندل H1 بعدی."
    )
    if decision.others:
        names = "، ".join(f"«{SETUP_TITLE_FA[h.setup]}»" for h in decision.others)
        parts.append(f"ستاپ‌های هم‌زمان دیگر ({names}) طبق اولویت (شکست-پولبک، سپس خط بیرونی، سپس خط میانی) کنار گذاشته شدند.")
    return " ".join(parts)


def _log_decision(name: str, symbol: str, cb: ChannelBars, decision: Decision, patterns_checked: bool) -> None:
    """Per-decision debug log (rule 01 section 2): bar time UTC, indicator values, action."""
    if not is_dev_mode():
        return
    t = decision.t
    action = "none"
    if decision.chosen is not None:
        action = f"{decision.chosen.side}:{decision.chosen.setup.value}"
    elif decision.note == "direction_conflict":
        action = "none(direction_conflict)"
    elif not patterns_checked:
        action = "none(no touch or direction blocked; patterns not evaluated)"
    logger.debug(
        "%s %s bar=%s decision=%s dir=%s mid=%.5f upper=%.5f lower=%.5f slope=%.6f flat=%s atr_h4=%.5f "
        "atr_h1=%.5f tol=%.5f o=%.5f h=%.5f l=%.5f c=%.5f bull=%s(%s) bear=%s(%s) hits=%s action=%s",
        name, symbol, _iso(_ts(cb, t)), _iso(_ts(cb, t) + _H1), cb.channel_direction(t), cb.mid[t], cb.upper[t],
        cb.lower[t], cb.slope[t], bool(cb.is_flat[t]), cb.atr_h4[t], cb.atr_h1[t], cb.tol[t], cb.open[t],
        cb.high[t], cb.low[t], cb.close[t], bool(cb.bull[t]), cb.bull_pattern[t], bool(cb.bear[t]),
        cb.bear_pattern[t], [f"{h.setup.value}:{h.side}" for h in decision.hits], action,
    )


__all__ = ["StdDevChannelStrategy", "scan"]
