"""Gap detection and classification (decision D5). Gaps are reported, NEVER filled.

A gap is any step between consecutive bars larger than one bar interval. Each gap is classified as:

* ``weekend``       -- covers a Saturday (UTC) and lasts at most ``weekend_max_hours`` (default 60 h;
  a normal FX/metals weekend is ~48-50 h).
* ``holiday``       -- a whole trading session is missing: either a weekday gap of
  ``holiday_min_hours`` (18 h) .. ``holiday_max_hours`` (50 h), or a weekend stretched by up to two
  extra days (e.g. Good Friday: ~73 h).
* ``session_break`` -- a short gap that recurs at the same UTC time-of-day with the same length on most
  trading days (learned from the data itself; e.g. gold's daily 17:00-18:00 New York break, which is
  22:00 UTC in winter and 21:00 UTC in summer -- two separate recurring slots).
* ``missing``       -- everything else (data holes).

Durations are measured from the end of the bar before the gap to the open of the bar after it.
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

logger = get_logger(__name__)

GapKind = Literal["weekend", "holiday", "session_break", "missing"]
GAP_KINDS: tuple[GapKind, ...] = ("weekend", "holiday", "session_break", "missing")
DAY = 86400


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
    session_break_slots: list[str]  # e.g. "22:00Z x1"

    def within(self, start: pd.Timestamp, end: pd.Timestamp) -> list[Gap]:
        """Gaps that overlap ``[start, end]``."""
        s, e = iso_z(start), iso_z(end)
        return [g for g in self.gaps if g.before > s and g.after < e]  # ISO Z strings sort as time


def _covers_saturday(start: int, end: int) -> bool:
    for day in range(start // DAY, (max(end, start + 1) - 1) // DAY + 1):
        if (day + 3) % 7 == 5:  # 1970-01-01 was a Thursday; Monday == 0
            return True
    return False


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

    # Learn recurring short gaps: same UTC time-of-day and length on most trading days in their span.
    trading_days = np.unique(t // DAY) if t.size else np.zeros(0, dtype="int64")
    trading_days = trading_days[((trading_days + 3) % 7) < 5]
    by_slot: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for g in raw:
        if g["kind"] == "short":
            by_slot[(g["start"] % DAY, g["bars"])].append(g)
    break_slots: list[str] = []
    for (tod, bars), items in sorted(by_slot.items()):
        first_day, last_day = items[0]["start"] // DAY, items[-1]["start"] // DAY
        days_in_span = int(((trading_days >= first_day) & (trading_days <= last_day)).sum()) or 1
        recurring = len(items) >= break_min_occurrences and len(items) / days_in_span >= break_min_share
        for g in items:
            g["kind"] = "session_break" if recurring else "missing"
        if recurring:
            break_slots.append(f"{tod // 3600:02d}:{(tod % 3600) // 60:02d}Z x{bars}")
        if is_dev_mode():
            logger.debug("gap slot %02d:%02dZ x%d: %d occurrences over %d trading days -> %s",
                         tod // 3600, (tod % 3600) // 60, bars, len(items), days_in_span,
                         "session_break" if recurring else "missing")

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
