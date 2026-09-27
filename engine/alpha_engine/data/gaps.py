"""Gap detection and classification (decision D5). Gaps are reported, NEVER filled.

A gap is any step between consecutive bars larger than one bar interval. Each gap is classified as:

* ``weekend``       -- covers a Saturday (UTC) and lasts at most ``weekend_max_hours`` (default 60 h;
  a normal FX/metals weekend is ~48-50 h).
* ``holiday``       -- a whole trading session is missing: either a weekday gap of
  ``holiday_min_hours`` (18 h) .. ``holiday_max_hours`` (50 h), or a weekend stretched by up to two
  extra days (e.g. Good Friday: ~73 h).
* ``session_break`` -- a short gap that recurs at the same UTC time-of-day with the same length on most
  trading days *of its DST regime* (learned from the data itself, see below).
* ``missing``       -- everything else (data holes).

Durations are measured from the end of the bar before the gap to the open of the bar after it.

Seasonal session breaks
-----------------------
Exchange breaks follow a local clock, not UTC: gold's daily break starts 17:00 New York (22:00 UTC in
US winter, 21:00 UTC in US summer) and Brent's follows ICE/London hours, while a broker may reopen on a
fixed UTC time (live broker: gold reopens 23:00 UTC all year, so the break is 1 bar in winter and 2 in
summer). The timeline is therefore cut into **DST regimes**: contiguous runs of days with the same
(America/New_York DST, Europe/London DST) state -- ``NY-std/LDN-std`` (winter), ``NY-dst/LDN-std`` (the
2-3 spring weeks and ~1 autumn week when only the US is on summer time), ``NY-dst/LDN-dst`` (summer).
Inside one regime New York local time, London local time and UTC are fixed shifts of each other, so a
break anchored on any of those clocks sits on one UTC slot ``(time-of-day, bar count)``.

A short gap is a ``session_break`` if its slot is recurring by either rule:

1. **rolling, within its regime segment** -- in the ``break_window_days`` (28) days before it OR the 28
   days after it (clipped to its own contiguous regime segment), the same slot occurs at least
   ``break_min_occurrences`` (3) times and on at least ``break_min_share`` (50%) of the trading days.
   One-sided windows keep both sides of a broker schedule change (e.g. gold's summer reopen moving
   from 23:00 to 22:00 UTC in 2026) and of a regime boundary recognised.
2. **pooled over the regime state** -- over all segments of the same state (e.g. all winters), the slot
   occurs >= 3 times and on >= 50% of that state's trading days (covers segments cut short by the data
   start/end).

Worked example (real cache, XAUUSD.x H1): the gap ``2026-03-03T22:00Z`` (1 bar) lies in the
``NY-std/LDN-std`` segment 2025-11-02 .. 2026-03-07. In the 28 days before it (2026-02-03 .. 03-03)
there are 21 trading days and 16 gaps on slot ``22:00Z x1`` (16/21 = 76% >= 50%) -> ``session_break``.
The old whole-span rule saw 272 such gaps over the 873 trading days between the slot's first
(2022-11-07) and last (2026-03-17) occurrence (31% < 50%) and called all of them ``missing``. A one-off
1-bar hole has 1 occurrence in either window -> ``missing``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from ..logging_setup import get_logger, is_dev_mode
from .schema import Timeframe, iso_z, to_epoch_seconds
from .timezone import dst_in_effect

logger = get_logger(__name__)

GapKind = Literal["weekend", "holiday", "session_break", "missing"]
GAP_KINDS: tuple[GapKind, ...] = ("weekend", "holiday", "session_break", "missing")
DAY = 86400
# DST state code = 2 * (New York on DST) + (London on DST)
REGIME_LABELS: dict[int, str] = {0: "NY-std/LDN-std", 1: "NY-std/LDN-dst", 2: "NY-dst/LDN-std",
                                 3: "NY-dst/LDN-dst"}


class Gap(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: GapKind
    after: str  # open time (UTC, ISO Z) of the last bar before the gap
    before: str  # open time of the first bar after the gap
    start: str  # open time of the first missing bar slot (= after + timeframe)
    missing_bars: int  # number of absent bar slots
    duration_hours: float


class GapReport(BaseModel):
    timeframe: str
    gaps: list[Gap]
    counts: dict[str, int]
    missing_bars_total: int  # absent slots in gaps of kind "missing"
    session_break_slots: list[str]  # learned slot and its DST regime, e.g. "22:00Z x1 @NY-std/LDN-std"

    def within(self, start: pd.Timestamp, end: pd.Timestamp) -> list[Gap]:
        """Gaps that overlap ``[start, end]``."""
        s, e = iso_z(start), iso_z(end)
        return [g for g in self.gaps if g.before > s and g.after < e]  # ISO Z strings sort as time


def _covers_saturday(start: int, end: int) -> bool:
    for day in range(start // DAY, (max(end, start + 1) - 1) // DAY + 1):
        if (day + 3) % 7 == 5:  # 1970-01-01 was a Thursday; Monday == 0
            return True
    return False


def dst_regimes(days: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(state, segment)`` for each UTC day number.

    ``state`` is the DST state code (see :data:`REGIME_LABELS`) at 12:00 UTC of the day -- all DST
    switches happen early on a Sunday, while FX/metals are closed. ``segment`` numbers the contiguous
    runs of equal state over the calendar span of ``days``.
    """
    days = np.asarray(days, dtype="int64")
    if days.size == 0:
        return np.zeros(0, dtype="int64"), np.zeros(0, dtype="int64")
    lo, hi = int(days.min()), int(days.max())
    noon = np.arange(lo, hi + 1, dtype="int64") * DAY + DAY // 2
    state = dst_in_effect(noon, "us_dst").astype("int64") * 2 + dst_in_effect(noon, "eu_dst").astype("int64")
    segment = np.concatenate(([0], np.cumsum(np.diff(state) != 0))).astype("int64")
    return state[days - lo], segment[days - lo]


def _count_in(sorted_values: np.ndarray, lo: int, hi: int) -> int:
    return int(np.searchsorted(sorted_values, hi, "right") - np.searchsorted(sorted_values, lo, "left"))


def _fmt_slot(tod: int, bars: int) -> str:
    return f"{tod // 3600:02d}:{(tod % 3600) // 60:02d}Z x{bars}"


def find_gaps(
    frame: pd.DataFrame,
    timeframe: Timeframe | str,
    *,
    weekend_max_hours: float = 60.0,
    holiday_min_hours: float = 18.0,
    holiday_max_hours: float = 50.0,
    holiday_extra_hours: float = 48.0,
    break_min_occurrences: int = 3,
    break_min_share: float = 0.5,
    break_window_days: int = 28,
) -> GapReport:
    tf = Timeframe.parse(timeframe)
    step = tf.seconds
    t = to_epoch_seconds(frame["time"]) if len(frame) else np.zeros(0, dtype="int64")
    positions = np.flatnonzero(np.diff(t) > step)

    raw: list[dict] = []
    for i in positions.tolist():
        after, before = int(t[i]), int(t[i + 1])
        start = after + step
        dur = before - start
        dur_h = dur / 3600
        if _covers_saturday(start, before):
            if dur_h <= weekend_max_hours:
                kind = "weekend"
            elif dur_h <= weekend_max_hours + holiday_extra_hours:
                kind = "holiday"
            else:
                kind = "missing"
        elif dur_h >= holiday_min_hours:
            kind = "holiday" if dur_h <= holiday_max_hours else "missing"
        else:
            kind = "short"
        raw.append({"after": after, "before": before, "start": start, "dur": dur, "kind": kind,
                    "bars": math.ceil(dur / step)})

    break_slots = _classify_short_gaps(
        raw, t, min_occurrences=break_min_occurrences, min_share=break_min_share, window_days=break_window_days)

    def _iso(epoch: int) -> str:
        return pd.Timestamp(epoch, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")

    gaps = [
        Gap(kind=g["kind"], after=_iso(g["after"]), before=_iso(g["before"]), start=_iso(g["start"]),
            missing_bars=g["bars"], duration_hours=round(g["dur"] / 3600, 4))
        for g in raw
    ]
    counts = {k: 0 for k in GAP_KINDS}
    for g in gaps:
        counts[g.kind] += 1
    report = GapReport(
        timeframe=tf.value,
        gaps=gaps,
        counts=counts,
        missing_bars_total=sum(g.missing_bars for g in gaps if g.kind == "missing"),
        session_break_slots=break_slots,
    )
    if is_dev_mode():
        logger.debug("gaps %s over %d bars: %s missing_bars=%d break_slots=%s", tf.value, len(t), counts,
                     report.missing_bars_total, break_slots)
        for g in gaps:
            if g.kind == "missing":
                logger.debug("missing gap: %s .. %s (%d bars)", g.start, g.before, g.missing_bars)
    return report


def _classify_short_gaps(
    raw: list[dict], t: np.ndarray, *, min_occurrences: int, min_share: float, window_days: int,
) -> list[str]:
    """Label every ``short`` gap in ``raw`` as ``session_break`` or ``missing`` (in place).

    Returns the learned slots as ``"HH:MMZ xN @<regime>"``, sorted by slot then regime.
    """
    short = [g for g in raw if g["kind"] == "short"]
    if not short:
        return []
    trading_days = np.unique(t // DAY)
    trading_days = trading_days[((trading_days + 3) % 7) < 5]  # Monday..Friday (UTC)
    gap_days = np.array([g["start"] // DAY for g in short], dtype="int64")
    state_all, seg_all = dst_regimes(np.concatenate((trading_days, gap_days)))
    td_state, td_seg = state_all[: trading_days.size], seg_all[: trading_days.size]
    g_state, g_seg = state_all[trading_days.size:], seg_all[trading_days.size:]

    days_by_seg = {int(s): trading_days[td_seg == s] for s in np.unique(td_seg)}
    days_by_state = {int(s): int((td_state == s).sum()) for s in np.unique(td_state)}
    occ_by_seg: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    occ_by_state: dict[tuple[int, int, int], int] = defaultdict(int)
    keyed: list[tuple[dict, int, int, int, int]] = []  # gap, day, state, segment, time-of-day
    for g, day, st, sg in zip(short, gap_days.tolist(), g_state.tolist(), g_seg.tolist(), strict=True):
        tod = g["start"] % DAY
        keyed.append((g, day, st, sg, tod))
        occ_by_seg[(sg, tod, g["bars"])].append(day)
        occ_by_state[(st, tod, g["bars"])] += 1
    occ_sorted = {k: np.array(sorted(v), dtype="int64") for k, v in occ_by_seg.items()}
    empty = np.zeros(0, dtype="int64")

    def _recurring(occ: int, days: int) -> bool:
        return occ >= min_occurrences and days > 0 and occ / days >= min_share

    # per (state, tod, bars): [gaps, session_break via rolling window, via pooled rule only]
    stats: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0, 0, 0])
    for g, day, st, sg, tod in keyed:
        occ = occ_sorted[(sg, tod, g["bars"])]
        seg_days = days_by_seg.get(sg, empty)
        rolling = any(
            _recurring(_count_in(occ, lo, hi), _count_in(seg_days, lo, hi))
            for lo, hi in ((day - window_days, day), (day, day + window_days))
        )
        pooled = _recurring(occ_by_state[(st, tod, g["bars"])], days_by_state.get(st, 0))
        g["kind"] = "session_break" if rolling or pooled else "missing"
        entry = stats[(st, tod, g["bars"])]
        entry[0] += 1
        entry[1] += int(rolling)
        entry[2] += int(pooled and not rolling)

    learned = sorted((tod, bars, REGIME_LABELS[st]) for (st, tod, bars), (_, r, p) in stats.items() if r + p)
    if is_dev_mode():
        for (st, tod, bars), (n, r, p) in sorted(stats.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
            logger.debug("gap slot %s @%s: %d gaps -> %d session_break (rolling %dd window in segment), "
                         "%d session_break (pooled over %d trading days of the regime), %d missing",
                         _fmt_slot(tod, bars), REGIME_LABELS[st], n, r, window_days, p,
                         days_by_state.get(st, 0), n - r - p)
    return [f"{_fmt_slot(tod, bars)} @{label}" for tod, bars, label in learned]
