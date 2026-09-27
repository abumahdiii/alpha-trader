"""Gap classification against the fixture's planted anomalies (exact counts), never filling."""

from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest

from alpha_engine import logging_setup
from alpha_engine.data.gaps import find_gaps
from alpha_engine.data.schema import Timeframe, normalize_frame
from alpha_engine.data.timezone import OffsetModel
from fixtures.synthetic_ohlcv import SYMBOLS, generate_symbol


@pytest.mark.parametrize("symbol", SYMBOLS)
@pytest.mark.parametrize("tf", list(Timeframe))
def test_counts_match_schedule_oracle(synthetic, symbol: str, tf: Timeframe) -> None:
    series = synthetic[(symbol, tf)]
    report = find_gaps(series.utc, tf)
    assert report.counts == {k: series.meta["expected_gaps"][k] for k in report.counts}


def test_exact_h1_counts_and_details(synthetic) -> None:
    series = synthetic[("XAUUSD.x", Timeframe.H1)]
    report = find_gaps(series.utc, Timeframe.H1)
    assert report.counts == {"weekend": 9, "holiday": 1, "session_break": 39, "missing": 5}
    # the 17:00 New York break, learned per DST regime: 22:00Z in winter, 21:00Z once New York is on DST
    # (both in the 3 weeks when only the US is on summer time and after the EU switch)
    assert report.session_break_slots == [
        "21:00Z x1 @NY-dst/LDN-dst", "21:00Z x1 @NY-dst/LDN-std", "22:00Z x1 @NY-std/LDN-std",
    ]
    missing = [g for g in report.gaps if g.kind == "missing"]
    planted = {e["start"]: e["bars_h1"] for e in series.meta["missing_events"]}
    assert {g.start: g.missing_bars for g in missing} == planted
    assert report.missing_bars_total == 11
    holiday = next(g for g in report.gaps if g.kind == "holiday")
    assert holiday.after == "2025-04-17T20:00:00Z"  # Thu 16:00 NY: last bar before Good Friday
    assert holiday.before == "2025-04-20T22:00:00Z"  # Sun 18:00 NY
    assert holiday.duration_hours == 73.0


def test_exact_h4_counts(synthetic) -> None:
    series = synthetic[("XAUUSD.x", Timeframe.H4)]
    report = find_gaps(series.utc, Timeframe.H4)
    assert report.counts == {"weekend": 8, "holiday": 1, "session_break": 0, "missing": 2}
    assert sorted(g.start for g in report.gaps if g.kind == "missing") == sorted(series.meta["missing_bars"])


@pytest.mark.parametrize("model", [OffsetModel.fixed(3), OffsetModel.eu_dst(2)])
def test_other_broker_models(model: OffsetModel) -> None:
    for tf, series in generate_symbol("XAUUSD.x", offset_model=model).items():
        assert find_gaps(series.utc, tf).counts == {
            k: series.meta["expected_gaps"][k] for k in ("weekend", "holiday", "session_break", "missing")
        }


def test_weekday_holidays_christmas_new_year() -> None:
    for tf, series in generate_symbol("BRNUSD.x", start="2024-12-02", end="2025-01-20").items():
        report = find_gaps(series.utc, tf)
        assert report.counts["holiday"] == 2 == series.meta["expected_gaps"]["holiday"]
        assert report.counts == {k: series.meta["expected_gaps"][k] for k in report.counts}


def test_no_bars_are_ever_added(synthetic) -> None:
    frame = synthetic[("XAUUSD.x", Timeframe.H1)].utc
    before = frame.copy()
    find_gaps(frame, "H1")
    pd.testing.assert_frame_equal(frame, before)


def test_within_filters_by_range(synthetic) -> None:
    report = find_gaps(synthetic[("XAUUSD.x", Timeframe.H1)].utc, "H1")
    week = report.within(pd.Timestamp("2025-03-10T00:00Z"), pd.Timestamp("2025-03-14T12:00Z"))
    assert {g.kind for g in week} <= {"session_break", "missing"}
    assert sum(g.kind == "session_break" for g in week) == 4


def test_empty_and_single_bar_frames(synthetic) -> None:
    frame = synthetic[("XAUUSD.x", Timeframe.H1)].utc
    assert find_gaps(frame.iloc[:0], "H1").gaps == []
    assert find_gaps(frame.iloc[:1], "H1").counts["missing"] == 0


def test_long_hole_is_missing_not_weekend(synthetic) -> None:
    frame = synthetic[("XAUUSD.x", Timeframe.H1)].utc
    cut = frame[(frame["time"] < "2025-03-03") | (frame["time"] >= "2025-03-20")]
    report = find_gaps(cut.reset_index(drop=True), "H1")
    big = [g for g in report.gaps if g.duration_hours > 200]
    assert len(big) == 1 and big[0].kind == "missing"


# --- seasonal (DST-regime) session breaks -----------------------------------------------------------

NY, LDN = "America/New_York", "Europe/London"


def _grid(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"), freq="h", inclusive="left")


def _weekend(grid: pd.DatetimeIndex) -> np.ndarray:
    """Closed Fri 21:00Z .. Sun 23:00Z (the live broker opens Sunday 23:00 UTC all year)."""
    wd, hour = np.asarray(grid.weekday), np.asarray(grid.hour)
    return (wd == 5) | ((wd == 4) & (hour >= 21)) | ((wd == 6) & (hour < 23))


def _ny_break(grid: pd.DatetimeIndex) -> np.ndarray:
    """Gold-like: 17:00 New York (Mon-Thu) until the fixed 23:00Z reopen -> 1 bar in winter, 2 in summer."""
    ny = grid.tz_convert(NY)
    hour = np.asarray(grid.hour)
    return (np.asarray(ny.weekday) <= 3) & (np.asarray(ny.hour) >= 17) & (hour >= 20) & (hour < 23)


def _ldn_break(grid: pd.DatetimeIndex) -> np.ndarray:
    """Brent-like: 22:00-24:00 London (Mon-Thu): 22:00Z in UK winter, 21:00Z in UK summer."""
    ldn = grid.tz_convert(LDN)
    return (np.asarray(ldn.weekday) <= 3) & np.isin(np.asarray(ldn.hour), (22, 23))


def _frame(grid: pd.DatetimeIndex, closed: np.ndarray) -> pd.DataFrame:
    times = grid[~closed]
    n = len(times)
    return normalize_frame(pd.DataFrame({
        "time": times, "open": np.ones(n), "high": np.ones(n), "low": np.ones(n), "close": np.ones(n),
        "tick_volume": np.ones(n, dtype="int64"), "spread": np.ones(n, dtype="int64"),
        "real_volume": np.zeros(n, dtype="int64"),
    }))


def _runs(mask: np.ndarray) -> int:
    return int(mask[0]) + int(np.sum(mask[1:] & ~mask[:-1]))


def _at(grid: pd.DatetimeIndex, *stamps: str) -> np.ndarray:
    return np.isin(grid, pd.DatetimeIndex([pd.Timestamp(s, tz="UTC") for s in stamps]))


def test_winter_only_break_is_session_break_and_one_off_holes_are_missing() -> None:
    # Two US winters with a summer between; the break exists only while New York is on standard time.
    # The old whole-span rule saw slot 22:00Z x1 on ~40% of the trading days between its first and
    # last occurrence and called every one of them "missing".
    grid = _grid("2024-11-04", "2026-03-07")
    ny_dst = np.asarray(grid.tz_convert(NY).tz_localize(None) - grid.tz_localize(None)) == np.timedelta64(-4, "h")
    brk = _ny_break(grid) & ~ny_dst
    # one-off 1-bar holes, and a same-time hole on 3 dates weeks apart (not recurring either)
    holes = _at(grid, "2025-01-15T09:00Z", "2025-06-11T13:00Z",
                "2025-04-08T15:00Z", "2025-05-14T15:00Z", "2025-06-18T15:00Z")
    report = find_gaps(_frame(grid, _weekend(grid) | brk | holes), "H1")
    assert report.counts["session_break"] == _runs(brk) == 144
    assert report.counts["missing"] == 5 and report.missing_bars_total == 5
    assert report.session_break_slots == ["22:00Z x1 @NY-std/LDN-std"]
    sb = next(g for g in report.gaps if g.start == "2026-03-03T22:00:00Z")
    assert sb.kind == "session_break" and sb.missing_bars == 1
    assert {g.start for g in report.gaps if g.kind == "missing"} == {
        "2025-01-15T09:00:00Z", "2025-06-11T13:00:00Z", "2025-04-08T15:00:00Z", "2025-05-14T15:00:00Z",
        "2025-06-18T15:00:00Z"}


def test_london_anchored_break_follows_uk_dst() -> None:
    # 22:00 London: in the weeks when only the US is on DST it stays at 22:00Z (a New York-anchored
    # break would already sit at 21:00Z) -- the regime split keeps each slot on its own UTC time.
    grid = _grid("2024-09-02", "2025-12-20")
    brk = _ldn_break(grid)
    report = find_gaps(_frame(grid, _weekend(grid) | brk | _at(grid, "2025-02-12T04:00Z")), "H1")
    assert report.counts["session_break"] == _runs(brk)
    assert report.counts["missing"] == 1
    assert report.session_break_slots == [
        "21:00Z x2 @NY-dst/LDN-dst", "22:00Z x2 @NY-dst/LDN-std", "22:00Z x2 @NY-std/LDN-std",
    ]


def test_schedule_change_inside_a_regime_keeps_both_slots() -> None:
    # Like the live broker in 2026: the summer reopen moves from 23:00Z to 22:00Z on July 1st, so the
    # break shrinks from 2 bars to 1 in the middle of a regime. One-sided windows recognise both sides.
    grid = _grid("2025-04-07", "2025-09-27")
    hour = np.asarray(grid.hour)
    brk = _ny_break(grid) & ~((grid >= pd.Timestamp("2025-07-01", tz="UTC")) & (hour == 22))
    report = find_gaps(_frame(grid, _weekend(grid) | brk), "H1")
    assert report.counts["session_break"] == _runs(brk) and report.counts["missing"] == 0
    assert report.session_break_slots == ["21:00Z x1 @NY-dst/LDN-dst", "21:00Z x2 @NY-dst/LDN-dst"]


def test_short_segment_at_data_start_uses_the_pooled_regime_rule() -> None:
    # Data starts Thursday 2025-10-30, inside the one-week "NY-dst/LDN-std" autumn segment: its single
    # break cannot recur within that segment, but the same slot recurs in the spring 2026 segment of
    # the same regime state (12 of 15 trading days) -> session_break.
    grid = _grid("2025-10-30", "2026-03-28")
    brk = _ny_break(grid)
    report = find_gaps(_frame(grid, _weekend(grid) | brk), "H1")
    first = report.gaps[0]
    assert first.start == "2025-10-30T21:00:00Z" and first.kind == "session_break"
    assert report.counts["missing"] == 0 and report.counts["session_break"] == _runs(brk)


def test_dev_mode_logs_learned_slots_with_regime(make_settings) -> None:
    stream = io.StringIO()
    logging_setup.configure_logging(make_settings("DEV_MODE=true\n"), stream=stream, force=True)
    grid = _grid("2025-01-06", "2025-02-01")
    find_gaps(_frame(grid, _weekend(grid) | _ny_break(grid)), "H1")
    assert "gap slot 22:00Z x1 @NY-std/LDN-std: 16 gaps -> 16 session_break (rolling" in stream.getvalue()
