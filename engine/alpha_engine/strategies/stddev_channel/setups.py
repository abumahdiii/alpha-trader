"""Per-bar building blocks of the StdDev-channel setups: indicator arrays, touch rule, direction filter,
bounce setups.

Everything here is a function of CLOSED bars only (see ``strategy/base.py`` for the decision-time rule).

Indicator arrays (:func:`compute_channel_bars`), one value per H1 bar ``i`` (decided at ``open_i + 1h``):

* ``mid/upper/lower/slope/sigma`` -- the rolling regression channel of the last H4 bar closed by
  ``open_i + 1h`` (``mtf.project_channel_to_h1``: ``j* = searchsorted(h4_open + 4h, h1_open + 1h,
  'right') - 1``), projected to bar ``i`` (bar_count mode by default). ``slope`` is price per H4 bar.
* ``atr_h4`` / ``is_flat`` -- Wilder ATR on H4 at ``j*``; flat if ``|slope| * n < atr_h4 * flat_mult``.
* ``atr_h1`` -- Wilder ATR on H1 at ``i``; ``tol_i = atr_h1_i * touch_atr_mult``.
* ``bull/bear`` -- reversal pattern (``patterns.candles.detect_reversals``) at bar ``i``.

Direction filter (knowledge file section 4, user decision for flat channels)::

    valid_i   = mid, atr_h1 and atr_h4 are all finite at i
    buy_ok_i  = valid_i and (is_flat_i or slope_i > 0)
    sell_ok_i = valid_i and (is_flat_i or slope_i < 0)

``slope == 0`` on a non-flat channel (only possible with ``flat_mult = 0``) allows neither side.

Touch rule: bar ``i`` touches line ``L`` when its range comes within ``tol_i`` of the line::

    low_i <= L_i + tol_i  and  high_i >= L_i - tol_i        (both inclusive)

Each bar is judged with its OWN ``tol_i`` (the ATR known when that bar closed); the decision at bar ``t``
reads these per-bar results, so it never depends on anything after ``t``.

Worked example: L = 2000.00, ATR_H1 = 2.50, touch_atr_mult = 0.1 -> tol = 0.25.
A bar with low 2000.25 touches (distance exactly tol, inclusive); low 2000.2500001 does not.
A bar with high 1999.75 touches from below; high 1999.7499999 does not.

Bounce setups at the confirmation bar ``t`` (``touch_lookback`` = TL, ``approach_lookback`` = a)::

    bounce_lower  (buy):  buy_ok_t,  bull_t, lower touched at some i in [t-TL, t], close_t >= lower_t - tol_t
    bounce_upper  (sell): sell_ok_t, bear_t, upper touched at some i in [t-TL, t], close_t <= upper_t + tol_t
    bounce_mid    (buy, price came from above):  buy_ok_t, bull_t, mid touched in [t-TL, t],
                  close_{t-a} > mid_{t-a} + tol_{t-a},  close_t >= mid_t - tol_t
    bounce_mid    (sell, price came from below): sell_ok_t, bear_t, mid touched in [t-TL, t],
                  close_{t-a} < mid_{t-a} - tol_{t-a},  close_t <= mid_t + tol_t

The last condition of each rule rejects a confirmation candle that closed THROUGH the line.

Projection note: the H4 -> H1 projection is assembled from the public building blocks of
``indicators.mtf`` / ``indicators.regression_channel`` (``last_closed_index``, ``h1_bars_since``,
``line_value_at``, ``is_flat``) with exactly the arithmetic of ``mtf.project_channel_to_h1`` (a test asserts
bitwise equality). The wrapper itself is not called because it builds a DataFrame column by column
(~10 ms per call with pandas 3), which made per-bar ``evaluate`` far too slow; here only the H4 channel
rows actually needed are computed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
import pandas as pd

from ...indicators.atr import wilder_atr
from ...indicators.mtf import h1_bars_since, last_closed_index
from ...indicators.regression_channel import is_flat, line_value_at, rolling_regression_channel
from ...logging_setup import get_logger, is_dev_mode
from ...patterns.candles import detect_reversals
from ...strategy.base import bar_open_times
from ...strategy.signal import Setup
from .params import StdDevParams

logger = get_logger(__name__)

Side = Literal["buy", "sell"]
LineName = Literal["lower", "mid", "upper"]

# Persian labels used in reason_fa.
SETUP_TITLE_FA: dict[Setup, str] = {
    Setup.BOUNCE_LOWER: "برگشت از خط پایین",
    Setup.BOUNCE_MID: "برگشت از خط میانی",
    Setup.BOUNCE_UPPER: "برگشت از خط بالا",
    Setup.BREAKOUT_UPPER_PULLBACK: "شکست خط بالا و پولبک",
    Setup.BREAKOUT_MID_PULLBACK: "شکست خط میانی (شکست کانال کوچک‌تر) و پولبک",
    Setup.BREAKOUT_LOWER_PULLBACK: "شکست خط پایین و پولبک",
}
LINE_FA: dict[str, str] = {"lower": "خط پایین", "mid": "خط میانی", "upper": "خط بالا"}
SIDE_FA: dict[str, str] = {"buy": "خرید", "sell": "فروش"}
CHANNEL_DIR_FA: dict[str, str] = {"up": "صعودی", "down": "نزولی", "flat": "افقی", "none": "نامعتبر"}
PATTERN_FA: dict[str, str] = {
    "bullish_pin_bar": "پین‌بار صعودی (چکش)",
    "bearish_pin_bar": "پین‌بار نزولی (ستاره دنباله‌دار)",
    "bullish_engulfing": "اینگالف صعودی",
    "bearish_engulfing": "اینگالف نزولی",
}


@dataclass(frozen=True, eq=False)
class ChannelBars:
    """Causal per-H1-bar arrays used by the setup logic (all length = number of H1 bars)."""

    times: pd.DatetimeIndex  # H1 bar open times, UTC
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    mid: np.ndarray
    upper: np.ndarray
    lower: np.ndarray
    slope: np.ndarray
    sigma: np.ndarray
    bars_ahead: np.ndarray
    h4_open: np.ndarray  # open time (int64 ns UTC) of the H4 bar j* used, or -1
    atr_h4: np.ndarray
    is_flat: np.ndarray
    atr_h1: np.ndarray
    tol: np.ndarray
    valid: np.ndarray
    buy_ok: np.ndarray
    sell_ok: np.ndarray
    bull: np.ndarray
    bear: np.ndarray
    bull_pattern: np.ndarray  # object: "" or e.g. "bullish_pin_bar+bullish_engulfing"
    bear_pattern: np.ndarray

    def __len__(self) -> int:
        return len(self.close)

    def line(self, name: str) -> np.ndarray:
        return {"lower": self.lower, "mid": self.mid, "upper": self.upper}[name]

    def channel_direction(self, i: int) -> str:
        """``up`` / ``down`` / ``flat`` / ``none`` (invalid) of the channel at bar ``i``."""
        if not self.valid[i]:
            return "none"
        if self.is_flat[i]:
            return "flat"
        if self.slope[i] > 0:
            return "up"
        if self.slope[i] < 0:
            return "down"
        return "none"

    def to_frame(self) -> pd.DataFrame:
        """Chart/debug view (UTC open time index)."""
        return pd.DataFrame(
            {
                "open": self.open, "high": self.high, "low": self.low, "close": self.close,
                "mid": self.mid, "upper": self.upper, "lower": self.lower, "slope": self.slope,
                "sigma": self.sigma, "bars_ahead": self.bars_ahead, "atr_h4": self.atr_h4,
                "is_flat": self.is_flat, "atr_h1": self.atr_h1, "tol": self.tol, "buy_ok": self.buy_ok,
                "sell_ok": self.sell_ok, "bull": self.bull, "bear": self.bear,
            },
            index=self.times,
        )


def _ohlc(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    missing = [c for c in ("open", "high", "low", "close") if c not in frame.columns]
    if missing:
        raise ValueError(f"bar frame is missing column(s) {missing}")
    return tuple(frame[c].to_numpy(dtype=np.float64) for c in ("open", "high", "low", "close"))  # type: ignore[return-value]


_H4_NS = int(pd.Timedelta(hours=4).value)
_H1_NS = int(pd.Timedelta(hours=1).value)


def compute_channel_bars(
    h1: pd.DataFrame,
    h4: pd.DataFrame,
    p: StdDevParams,
    *,
    start: int = 0,
    pattern_from: int | None = 0,
) -> ChannelBars:
    """Vectorised, causal indicator arrays for the H1 bars of ``h1``.

    ``h4`` may contain H4 bars that are not closed yet at some H1 bars (``scan`` passes the full
    history): only ``j*`` (the last H4 bar closed at each H1 decision time) is ever read, and the
    rolling channel / Wilder ATR at ``j*`` depend on H4 bars ``<= j*`` only.

    ``start``: channel lines are produced for bars ``>= start`` only (earlier bars are NaN); ATR_H1 is
    always computed over the whole history (Wilder recursion). ``pattern_from``: reversal patterns for
    bars ``>= pattern_from`` (``None`` = none; see :func:`with_patterns`). Both are pure speed-ups for
    ``evaluate``, which reads only its last ``history_bars`` bars.
    """
    times = bar_open_times(h1)
    h4_times = bar_open_times(h4)
    o, h, lo, c = _ohlc(h1)
    size = len(c)
    start = max(0, min(start, size))
    nan = np.full(size, np.nan)

    atr_h1 = wilder_atr(h, lo, c, p.atr_period).to_numpy() if size else nan.copy()
    h4_ns = h4_times.as_unit("ns").asi8
    h1_ns = times.as_unit("ns").asi8

    # j* and m are computed on the H1 tail [s:], where s is the last H1 bar opening at or before the
    # open of j*(start): m counts H1 bars in [open_j*, T), so every count for bars >= start is the same
    # as on the full history. When j*(start) does not exist (start of data), s = 0 (full history).
    # Bars in [s, start) get j = -1: m of a bar depends only on its own j*, and they are blanked anyway.
    j = np.full(size, -1, dtype=np.int64)
    if size:
        j[start:] = last_closed_index(h4_times, times[start:])
    s = start
    if start and size and start < size:
        s = 0
        if j[start] >= 0:
            s = int(np.searchsorted(h1_ns, h4_ns[j[start]], side="right")) - 1
            s = min(max(s, 0), start)

    if p.projection_mode == "bar_count":
        m = np.full(size, -1, dtype=np.int64)
        if size:
            m[s:] = h1_bars_since(h4_times, j[s:], times[s:])
        bars_ahead = np.where(m >= 0, m / (_H4_NS / _H1_NS), np.nan)  # same arithmetic as mtf
    else:
        jc0 = np.where(j >= 0, j, 0)
        elapsed = h1_ns - (h4_ns[jc0] if len(h4_ns) else np.zeros(size, dtype=np.int64))
        bars_ahead = np.where(j >= 0, elapsed / _H4_NS, np.nan)

    # H4 channel rows needed by bars >= start (j* >= n-1), computed in one vectorised call.
    chan = np.full((len(h4_ns), 5), np.nan)
    atr_h4_all = np.full(len(h4_ns), np.nan)
    needed = j[start:]
    needed = needed[needed >= p.n - 1]
    if len(needed):
        j_lo, j_hi = int(needed.min()), int(needed.max())
        h4_o, h4_h, h4_l, h4_c = _ohlc(h4.iloc[: j_hi + 1])
        block = rolling_regression_channel(h4_c[j_lo - p.n + 1: j_hi + 1], p.n, p.k, p.sigma_ddof).to_numpy()
        chan[j_lo: j_hi + 1] = block[p.n - 1:]
        atr_h4_all[: j_hi + 1] = wilder_atr(h4_h, h4_l, h4_c, p.atr_period).to_numpy()

    has_j = j >= 0
    jc = np.where(has_j, j, 0)

    def pick(values: np.ndarray) -> np.ndarray:
        if not len(values):
            return nan.copy()
        return np.where(has_j, values[jc], np.nan)

    if start:
        bars_ahead = bars_ahead.copy()
        bars_ahead[:start] = np.nan
    usable = ~np.isnan(bars_ahead)
    slope_j = pick(chan[:, 3])
    mid = line_value_at(pick(chan[:, 0]), slope_j, bars_ahead)
    upper = line_value_at(pick(chan[:, 1]), slope_j, bars_ahead)
    lower = line_value_at(pick(chan[:, 2]), slope_j, bars_ahead)
    slope = np.where(usable, slope_j, np.nan)
    sigma = np.where(usable, pick(chan[:, 4]), np.nan)
    atr_h4 = np.where(usable, pick(atr_h4_all), np.nan)
    flat = np.asarray(is_flat(slope, p.n, atr_h4, p.flat_mult), dtype=bool).reshape(size)
    h4_open = np.where(has_j, h4_ns[jc], -1) if len(h4_ns) else np.full(size, -1, dtype=np.int64)

    tol = atr_h1 * p.touch_atr_mult
    valid = np.isfinite(mid) & np.isfinite(upper) & np.isfinite(lower) & np.isfinite(atr_h1) & np.isfinite(atr_h4)
    with np.errstate(invalid="ignore"):
        buy_ok = valid & (flat | (slope > 0))
        sell_ok = valid & (flat | (slope < 0))

    cb = ChannelBars(
        times=times, open=o, high=h, low=lo, close=c, mid=mid, upper=upper, lower=lower, slope=slope,
        sigma=sigma, bars_ahead=bars_ahead, h4_open=h4_open, atr_h4=atr_h4, is_flat=flat,
        atr_h1=atr_h1, tol=tol, valid=valid, buy_ok=buy_ok, sell_ok=sell_ok,
        bull=np.zeros(size, dtype=bool), bear=np.zeros(size, dtype=bool),
        bull_pattern=np.full(size, "", dtype=object), bear_pattern=np.full(size, "", dtype=object),
    )
    if pattern_from is not None:
        cb = with_patterns(cb, p, pattern_from)
    if is_dev_mode():
        logger.debug(
            "stddev compute_channel_bars: h1=%d h4=%d start=%d n=%d k=%g ddof=%d mode=%s valid=%d buy_ok=%d "
            "sell_ok=%d flat=%d bull=%d bear=%d",
            size, len(h4), start, p.n, p.k, p.sigma_ddof, p.projection_mode, int(valid.sum()),
            int(buy_ok.sum()), int(sell_ok.sum()), int(flat.sum()), int(cb.bull.sum()), int(cb.bear.sum()),
        )
    return cb


def with_patterns(cb: ChannelBars, p: StdDevParams, pattern_from: int) -> ChannelBars:
    """Copy of ``cb`` with reversal patterns for bars ``>= pattern_from`` (bar i uses bars i-1 and i)."""
    size = len(cb)
    start = max(0, min(pattern_from, size))
    bull = np.zeros(size, dtype=bool)
    bear = np.zeros(size, dtype=bool)
    bull_pattern = np.full(size, "", dtype=object)
    bear_pattern = np.full(size, "", dtype=object)
    if start < size:
        lead = 1 if start > 0 else 0  # engulfing at `start` needs bar start-1
        lo_i = start - lead
        ohlc = np.column_stack([cb.open[lo_i:], cb.high[lo_i:], cb.low[lo_i:], cb.close[lo_i:]])
        rev = detect_reversals(ohlc, p.patterns, wick_body_ratio=p.pin_wick_body_ratio,
                               opposite_wick_max=p.pin_opposite_wick_max).iloc[lead:]
        bull[start:] = rev["bullish"].to_numpy(dtype=bool)
        bear[start:] = rev["bearish"].to_numpy(dtype=bool)
        names = rev["pattern"].to_numpy(dtype=object)
        bull_pattern[start:] = [_side_names(x, "bullish") for x in names]
        bear_pattern[start:] = [_side_names(x, "bearish") for x in names]
    return replace(cb, bull=bull, bear=bear, bull_pattern=bull_pattern, bear_pattern=bear_pattern)


def any_touch(cb: ChannelBars, t: int, lookback: int) -> bool:
    """Some bar in ``[t-lookback, t]`` touches some channel line (a necessary condition of every setup)."""
    return any(touch_offset(cb, line, t, lookback) is not None for line in ("lower", "mid", "upper"))


def _side_names(joined: str, side: str) -> str:
    return "+".join(part for part in str(joined).split("+") if part.startswith(side))


def touches(low: float, high: float, line: float, tol: float) -> bool:
    """The touch rule for one bar (inclusive at distance exactly ``tol``). NaN anywhere -> False."""
    return bool(low <= line + tol and high >= line - tol)


def touch_offset(cb: ChannelBars, line: str, t: int, lookback: int, first_allowed: int = 0) -> int | None:
    """Offset ``k`` (0 = bar t) of the most recent bar in ``[max(t-lookback, first_allowed), t]``
    touching ``line``; ``None`` if none."""
    values = cb.line(line)
    for k in range(0, lookback + 1):
        i = t - k
        if i < first_allowed or i < 0:
            break
        if touches(cb.low[i], cb.high[i], values[i], cb.tol[i]):
            return k
    return None


def closed_on_side(cb: ChannelBars, line: str, i: int, side: Side) -> bool:
    """Confirmation close did not go THROUGH the line: buy ``close >= L - tol``, sell ``close <= L + tol``."""
    value = cb.line(line)[i]
    if side == "buy":
        return bool(cb.close[i] >= value - cb.tol[i])
    return bool(cb.close[i] <= value + cb.tol[i])


def confirmed(cb: ChannelBars, t: int, side: Side) -> bool:
    """Direction allowed by the channel at ``t`` AND a reversal pattern in that direction at ``t``."""
    if side == "buy":
        return bool(cb.buy_ok[t] and cb.bull[t])
    return bool(cb.sell_ok[t] and cb.bear[t])


@dataclass(frozen=True)
class SetupHit:
    """One setup whose rule holds at bar ``t``."""

    setup: Setup
    side: Side
    line: LineName
    touch_offset: int
    breakout_bar: int | None = None  # index of the breakout bar (breakout-pullback setups)
    approach_bar: int | None = None  # index of the approach bar (mid bounces)


def bounce_hits(cb: ChannelBars, t: int, p: StdDevParams) -> list[SetupHit]:
    """All bounce setups valid at bar ``t`` (see the module docstring)."""
    hits: list[SetupHit] = []
    for side in ("buy", "sell"):
        if not confirmed(cb, t, side):
            continue
        outer: LineName = "lower" if side == "buy" else "upper"
        setup = Setup.BOUNCE_LOWER if side == "buy" else Setup.BOUNCE_UPPER
        k = touch_offset(cb, outer, t, p.touch_lookback)
        if k is not None and closed_on_side(cb, outer, t, side):
            hits.append(SetupHit(setup, side, outer, k))

        a = t - p.approach_lookback
        if a < 0:
            continue
        k = touch_offset(cb, "mid", t, p.touch_lookback)
        if k is None or not closed_on_side(cb, "mid", t, side):
            continue
        if side == "buy":
            approached = cb.close[a] > cb.mid[a] + cb.tol[a]  # came from above
        else:
            approached = cb.close[a] < cb.mid[a] - cb.tol[a]  # came from below
        if approached:
            hits.append(SetupHit(Setup.BOUNCE_MID, side, "mid", k, approach_bar=a))
    return hits
