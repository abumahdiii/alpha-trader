"""Gap classification against the fixture's planted anomalies (exact counts), never filling."""

from __future__ import annotations

import pandas as pd
import pytest

from alpha_engine.data.gaps import find_gaps
from alpha_engine.data.schema import Timeframe
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
    # both DST seasons of the 17:00 New York break were learned as separate slots
    assert report.session_break_slots == ["21:00Z x1", "22:00Z x1"]
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
