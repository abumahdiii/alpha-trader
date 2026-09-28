"""Backtest periods: manual window validation and seeded random windows (phase-4 decision 7).

Earliest allowed window start (warm-up)
---------------------------------------
Strategy-neutral: the strategy gives the first *valid* H1 bar ``t_v`` (``Strategy.first_valid_index``, stored
in ``history.ScanResult.first_valid_index``) and a warm-up margin ``M`` in H4 bars
(``Strategy.warmup_margin_h4_bars``; path-dependent indicators such as Wilder ATR still carry their seed at
``t_v``). For the StdDev channel ``t_v`` is the first bar with the channel (``n`` closed H4 bars) and both ATRs
defined and ``M = 10 * atr_period`` (140 H4 bars by default; ``(13/14)^140 ~ 3e-5``)::

    k        = H4 bars closed at the decision time of t_v (open_tv + 1h)
    j        = k - 1 + M                                      (index of the last warm-up H4 bar)
    earliest = open(H4 bar j) + 4h                            (its close)

Worked example (continuous synthetic data from Mon 2026-01-05 00:00 UTC, StdDev n = 100, atr_period = 14 ->
M = 140): ``t_v`` = H1 bar 399 (open 2026-01-21 15:00), decision 16:00 sees k = 100 closed H4 bars;
j = 99 + 140 = 239 -> earliest = 2026-01-05 + 239 * 4h + 4h = 2026-02-14 00:00 UTC. With ``M = 0`` (a strategy
without warm-up) the earliest start is the close of the last H4 bar closed at ``t_v``'s decision:
j = 99 -> 2026-01-05 + 100 * 4h = 2026-01-21 16:00 UTC.

The Persian wording of the messages that mention the warm-up comes from the strategy
(``Strategy.warmup_texts_fa``, :class:`~alpha_engine.strategy.base.WarmupTextsFa`; strategy-neutral defaults).

Windows are half-open ``[start, end)`` on H1 bar open times; the data ends at ``data_end`` = close of
the last cached H1 bar.

Manual window: ``start < end``, ``start >= earliest``, ``end <= data_end`` and at least one H1 bar inside;
otherwise :class:`PeriodError` with a Persian message (the API maps it to 422).

Random windows: ``numpy.random.default_rng(seed)`` (PCG64). Eligible starts = cached H1 bar OPEN times
``s`` with ``s >= earliest`` and ``s + DateOffset(months=L) <= data_end``. ``N`` starts are drawn uniformly
with replacement (``rng.integers(0, M, size=N)``; overlaps allowed), each window is
``[s, s + DateOffset(months=L))``; windows are then sorted by start (stable) and numbered 0..N-1. A missing
seed is generated with ``secrets.randbits(63)`` (:func:`new_seed`) and returned, so every run can be
regenerated exactly. Through the API the seed is drawn at SUBMIT time (``POST /backtests`` returns and stores
it before the run starts); the job then marks the stored plan ``seed_generated``.
The numpy version is stored too (Generator streams are only guaranteed within a numpy version).
"""

from __future__ import annotations

import secrets
from datetime import datetime

import numpy as np
import pandas as pd

from ..logging_setup import get_logger, is_dev_mode
from ..strategy.base import WarmupTextsFa
from .models import Window, WindowPlan

logger = get_logger(__name__)

DEFAULT_WARMUP_TEXTS_FA = WarmupTextsFa()
RANDOM_ALGORITHM = "numpy.PCG64"
H1 = pd.Timedelta(hours=1)
H4 = pd.Timedelta(hours=4)


class PeriodError(ValueError):
    def __init__(self, code: str, message_fa: str) -> None:
        super().__init__(message_fa)
        self.code = code
        self.message_fa = message_fa


def _fmt(ts: pd.Timestamp | datetime) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%d %H:%M UTC")


def _utc(value: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def new_seed() -> int:
    """A fresh random-window seed, ``0 <= seed < 2**63`` (``models.SEED_MAX``). ``POST /backtests`` draws it at
    submit time when the request has none (stored and returned); ``random_windows`` uses it only when called
    directly without a seed."""
    return secrets.randbits(63)


def earliest_start(
    h1_times_ns: np.ndarray,
    h4_times_ns: np.ndarray,
    first_valid_index: int | None,
    margin_h4_bars: int,
    *,
    texts: WarmupTextsFa | None = None,
) -> pd.Timestamp:
    """First allowed window start (see the module docstring). ``margin_h4_bars`` =
    ``Strategy.warmup_margin_h4_bars(params)``, ``texts`` = ``Strategy.warmup_texts_fa``. Raises
    :class:`PeriodError` when the data is too short for the first valid bar plus the warm-up margin."""
    margin = int(margin_h4_bars)
    if margin < 0:
        raise ValueError("margin_h4_bars must be >= 0")
    tx = texts if texts is not None else DEFAULT_WARMUP_TEXTS_FA
    if first_valid_index is None:
        raise PeriodError("data_too_short", tx.no_valid_bar)
    decision = int(h1_times_ns[first_valid_index]) + H1.value
    k = int(np.searchsorted(np.asarray(h4_times_ns, dtype=np.int64) + H4.value, decision, side="right"))
    j = k - 1 + margin
    if j >= len(h4_times_ns):
        raise PeriodError("data_too_short", tx.too_short.format(margin=margin))
    if j < 0:  # no H4 bar closed at the first decision (margin 0): the first valid decision itself
        return pd.Timestamp(decision, tz="UTC")
    return pd.Timestamp(int(h4_times_ns[j]) + H4.value, tz="UTC")


def data_end(h1_times_ns: np.ndarray) -> pd.Timestamp:
    return pd.Timestamp(int(h1_times_ns[-1]) + H1.value, tz="UTC")


def manual_window(
    start: datetime,
    end: datetime,
    h1_times_ns: np.ndarray,
    earliest: pd.Timestamp,
    *,
    warmup_bars: int,
    texts: WarmupTextsFa | None = None,
) -> WindowPlan:
    """Validate a manual ``[start, end)`` window (``warmup_bars`` / ``texts``: the strategy's margin and wording)."""
    s, e = _utc(start), _utc(end)
    last = data_end(h1_times_ns)
    tx = texts if texts is not None else DEFAULT_WARMUP_TEXTS_FA
    if not s < e:
        raise PeriodError("invalid_range", "ابتدای بازه باید قبل از انتهای آن باشد.")
    if s < earliest:
        raise PeriodError(
            "window_too_early",
            f"ابتدای بازه ({_fmt(s)}) زودتر از اولین زمان مجاز ({_fmt(earliest)}) است: "
            + tx.window_too_early.format(margin=warmup_bars))
    if e > last:
        raise PeriodError(
            "window_beyond_data",
            f"انتهای بازه ({_fmt(e)}) بعد از آخرین داده کش ({_fmt(last)}) است.")
    lo = int(np.searchsorted(h1_times_ns, s.as_unit("ns").value, side="left"))
    hi = int(np.searchsorted(h1_times_ns, e.as_unit("ns").value, side="left"))
    if hi <= lo:
        raise PeriodError("window_empty", "در این بازه هیچ کندل H1 در کش وجود ندارد.")
    if is_dev_mode():
        logger.debug("backtest manual window %s..%s: %d H1 bars (earliest %s, data end %s)", s.isoformat(),
                     e.isoformat(), hi - lo, earliest.isoformat(), last.isoformat())
    return WindowPlan(mode="manual", windows=[Window(index=0, start=s.to_pydatetime(), end=e.to_pydatetime())],
                      earliest_start=earliest.to_pydatetime(), data_end=last.to_pydatetime(),
                      warmup_h4_bars=warmup_bars)


def random_windows(
    h1_times_ns: np.ndarray,
    earliest: pd.Timestamp,
    *,
    count: int,
    months: int,
    seed: int | None,
    warmup_bars: int,
) -> WindowPlan:
    """``count`` seeded random windows of ``months`` calendar months (see the module docstring)."""
    if count < 1 or months < 1:
        raise ValueError("count and months must be >= 1")
    generated = seed is None
    used_seed = new_seed() if seed is None else int(seed)
    last = data_end(h1_times_ns)
    times = pd.DatetimeIndex(pd.to_datetime(np.asarray(h1_times_ns, dtype=np.int64), utc=True))
    offset = pd.DateOffset(months=months)
    after = times[times >= earliest]
    eligible = after[(after + offset) <= last] if len(after) else after
    if len(eligible) == 0:
        raise PeriodError(
            "data_too_short",
            f"داده کش برای حتی یک پنجره {months} ماهه کافی نیست (از {_fmt(earliest)} تا {_fmt(last)}).")
    rng = np.random.default_rng(used_seed)
    picks = rng.integers(0, len(eligible), size=count)
    starts = sorted(eligible[picks])  # stable, deterministic ordering by start
    windows = [Window(index=i, start=s.to_pydatetime(), end=(s + offset).to_pydatetime()) for i, s in enumerate(starts)]
    if is_dev_mode():
        logger.debug("backtest random windows: seed=%d (generated=%s) %s numpy %s, %d eligible starts %s..%s, "
                     "picks=%s", used_seed, generated, RANDOM_ALGORITHM, np.__version__, len(eligible),
                     eligible[0].isoformat(), eligible[-1].isoformat(), picks.tolist())
    return WindowPlan(
        mode="random", windows=windows, seed=used_seed, seed_generated=generated, algorithm=RANDOM_ALGORITHM,
        numpy_version=np.__version__, windows_count=count, window_months=months,
        earliest_start=earliest.to_pydatetime(), data_end=last.to_pydatetime(), eligible_starts=len(eligible),
        warmup_h4_bars=warmup_bars,
    )


__all__ = [
    "DEFAULT_WARMUP_TEXTS_FA",
    "RANDOM_ALGORITHM",
    "PeriodError",
    "data_end",
    "earliest_start",
    "manual_window",
    "new_seed",
    "random_windows",
]
