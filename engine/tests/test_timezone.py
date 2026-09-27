"""Broker offset models, server->UTC conversion, live estimate and the historical fit."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from alpha_engine import logging_setup
from alpha_engine.data.schema import COLUMNS, Timeframe, to_epoch_seconds
from alpha_engine.data.timezone import (
    LiveOffset,
    OffsetError,
    OffsetModel,
    estimate_live_offset,
    fit_offset_model,
    market_probably_closed,
    server_frame_to_utc,
)
from fixtures.synthetic_ohlcv import generate_symbol


def _server(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").timestamp())  # server wall clock as an "epoch", like MT5


def _utc(epoch: int) -> str:
    return pd.Timestamp(int(epoch), unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%MZ")


# --- worked examples (from the brief) -------------------------------------------------------------

@pytest.mark.parametrize(("server", "expected"), [
    ("2025-01-06 01:00", "2025-01-05T23:00Z"),  # US standard time: +2
    ("2025-07-07 01:00", "2025-07-06T22:00Z"),  # US DST: +3
])
def test_worked_examples_us_dst_plus2(server: str, expected: str) -> None:
    assert _utc(OffsetModel.us_dst(2).server_to_utc([_server(server)])[0]) == expected


def test_week_between_us_and_eu_dst_start_distinguishes_models() -> None:
    # 2025-03-17 lies after the US DST start (03-09) and before the EU one (03-30).
    s = [_server("2025-03-17 01:00")]
    assert _utc(OffsetModel.us_dst(2).server_to_utc(s)[0]) == "2025-03-16T22:00Z"
    assert _utc(OffsetModel.eu_dst(2).server_to_utc(s)[0]) == "2025-03-16T23:00Z"
    # outside that window the two agree
    for text in ("2025-03-03 01:00", "2025-04-07 01:00"):
        e = [_server(text)]
        assert OffsetModel.us_dst(2).server_to_utc(e)[0] == OffsetModel.eu_dst(2).server_to_utc(e)[0]


def test_fixed_and_labels() -> None:
    assert _utc(OffsetModel.fixed(3).server_to_utc([_server("2025-07-07 01:00")])[0]) == "2025-07-06T22:00Z"
    assert OffsetModel.fixed(-4.5).label == "fixed(-4.5)" and OffsetModel.fixed(0).label == "fixed(0)"
    for label in ("us_dst(+2)", "eu_dst(+3)", "fixed(-4.5)", "fixed(0)"):
        assert OffsetModel.parse(label).label == label
    with pytest.raises(ValueError):
        OffsetModel.fixed(2.25)
    with pytest.raises(ValueError):
        OffsetModel.parse("bogus")


def test_roundtrip_except_fall_back_hour() -> None:
    utc = np.arange(_server("2025-01-01"), _server("2026-01-01"), 1800, dtype="int64")
    for model in (OffsetModel.us_dst(2), OffsetModel.eu_dst(2), OffsetModel.fixed(3)):
        back = model.server_to_utc(model.utc_to_server(utc))
        bad = utc[back != utc]
        # only the repeated server hour after the autumn switch (a Sunday, market closed) is ambiguous
        assert bad.size <= 2
        assert all(pd.Timestamp(int(b), unit="s", tz="UTC").weekday() == 6 for b in bad)


def test_offset_seconds_vectorized() -> None:
    model = OffsetModel.us_dst(2)
    utc = np.array([_server("2025-03-09 06:59"), _server("2025-03-09 07:00"),
                    _server("2025-11-02 05:59"), _server("2025-11-02 06:00")])
    assert (model.offset_seconds_at_utc(utc) // 3600).tolist() == [2, 3, 3, 2]
    assert model.offset_hours_at(datetime(2025, 7, 1, tzinfo=timezone.utc)) == 3.0


def test_server_frame_to_utc_and_dev_log(make_settings) -> None:
    stream = io.StringIO()
    logging_setup.configure_logging(make_settings("DEV_MODE=true\n"), stream=stream, force=True)
    raw = pd.DataFrame({c: [1.0] for c in COLUMNS})
    raw["time"] = [_server("2025-01-06 01:00")]
    for col in ("tick_volume", "spread", "real_volume"):
        raw[col] = raw[col].astype("int64")
    out = server_frame_to_utc(raw, OffsetModel.us_dst(2))
    assert out["time"].iloc[0] == pd.Timestamp("2025-01-05T23:00Z")
    assert "server->UTC with us_dst(+2)" in stream.getvalue()


# --- live offset ----------------------------------------------------------------------------------

def test_live_offset_trusted_on_fresh_tick() -> None:
    now = datetime(2025, 7, 9, 12, 0, 20, tzinfo=timezone.utc)  # Wednesday
    tick = int(now.timestamp()) + 3 * 3600 - 5
    live = estimate_live_offset(tick, now)
    assert live.trusted and live.hours == 3.0 and live.residual_seconds == 5 and live.raw_seconds == 10795


def test_live_offset_untrusted_cases() -> None:
    wed = datetime(2025, 7, 9, 12, 0, tzinfo=timezone.utc)
    stale = estimate_live_offset(int(wed.timestamp()) + 3 * 3600 - 600, wed)
    assert not stale.trusted and "not fresh" in stale.reason
    sat = datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc)
    weekend = estimate_live_offset(int(sat.timestamp()) + 3 * 3600, sat)
    assert not weekend.trusted and "weekend" in weekend.reason
    assert estimate_live_offset(None, wed).hours is None
    assert market_probably_closed(datetime(2025, 7, 11, 21, 0, tzinfo=timezone.utc))
    assert not market_probably_closed(datetime(2025, 7, 13, 23, 0, tzinfo=timezone.utc))


# --- historical fit -------------------------------------------------------------------------------

@pytest.mark.parametrize("planted", [OffsetModel.us_dst(2), OffsetModel.fixed(3), OffsetModel.eu_dst(2)])
def test_fit_recovers_planted_model(planted: OffsetModel) -> None:
    raw = generate_symbol("XAUUSD.x", offset_model=planted)[Timeframe.H1].raw
    fit = fit_offset_model(raw["time"], now_utc=datetime(2025, 5, 5, tzinfo=timezone.utc))
    assert fit.inferred == planted and fit.effective == planted and fit.source == "fit"
    # 11 week segments -> 10 weekly closes (the Good Friday week closes Thursday 17:00 NY, accepted) and
    # 39 recurring daily breaks (40 Mon-Thu minus the Good-Friday-eve Thursday; the 5 planted data holes
    # are one-off slots, so they are not break checks). Primary = close + break; open = tie-break only.
    assert fit.weeks == 11 and fit.checks == 49 and fit.matched == 49
    assert fit.match_ratio == 1.0 and not fit.low_confidence
    anchors = {a.name: (a.role, a.matched, a.checks) for a in fit.anchors}
    assert anchors == {"weekly_close": ("primary", 10, 10), "daily_break": ("primary", 39, 39),
                       "weekly_open": ("tie_break", 10, 10)}


def _fixed_utc_open_times(planted: OffsetModel) -> np.ndarray:
    """The fixture as a broker that reopens at 23:00 UTC all year (like the live broker's gold).

    Drops every 22:00Z bar Sunday..Thursday: in US summer that is the Sunday open bar (the week now
    opens 23:00Z = 19:00 New York) and the first bar after the 17:00 NY daily break (2-bar break).
    """
    series = generate_symbol("XAUUSD.x", offset_model=planted)[Timeframe.H1]
    utc = pd.DatetimeIndex(series.utc["time"])
    drop = np.asarray((utc.hour == 22) & np.isin(utc.weekday, (6, 0, 1, 2, 3)))
    return series.raw["time"][~drop]


@pytest.mark.parametrize("planted", [OffsetModel.us_dst(2), OffsetModel.fixed(3), OffsetModel.eu_dst(2)])
def test_fit_with_fixed_utc_open_broker(planted: OffsetModel) -> None:
    fit = fit_offset_model(_fixed_utc_open_times(planted), now_utc=datetime(2025, 5, 5, tzinfo=timezone.utc))
    assert fit.inferred == planted and not fit.low_confidence
    anchors = {a.name: a for a in fit.anchors}
    assert (anchors["weekly_close"].matched, anchors["weekly_close"].checks) == (10, 10)
    assert (anchors["daily_break"].matched, anchors["daily_break"].checks) == (39, 39)
    # only the one full US-winter week (opening 2025-03-02) still opens at 18:00 NY; under the old
    # close+open score this was 10/20 = 50% confidence, now open is just the tie-breaker
    assert anchors["weekly_open"].role == "tie_break"
    assert (anchors["weekly_open"].matched, anchors["weekly_open"].checks) == (1, 10)
    assert fit.match_ratio == 1.0


def test_daily_break_at_another_local_time_does_not_vote() -> None:
    # Move the us_dst(+2) fixture's daily break from 00:00 server (17:00 NY) to 10:00 server: only a
    # model that the weekly closes reject (us_dst(-12)) would put it on 17:00 NY, so the anchor is unused.
    s = np.asarray(generate_symbol("XAUUSD.x")[Timeframe.H1].raw["time"], dtype="int64")
    one_bar_holes = s[:-1][np.diff(s) == 7200] + 3600
    s = np.union1d(s, one_bar_holes)
    server_wd = ((s // 86400) + 3) % 7
    s = s[~(((s % 86400) // 3600 == 10) & (server_wd <= 3))]
    fit = fit_offset_model(s, now_utc=datetime(2025, 5, 5, tzinfo=timezone.utc))
    assert fit.inferred == OffsetModel.us_dst(2)
    anchors = {a.name: a for a in fit.anchors}
    assert anchors["daily_break"].role == "unused" and anchors["daily_break"].matched == 0
    assert anchors["daily_break"].checks >= 39
    assert fit.checks == 10 and fit.matched == 10  # primary = weekly close only


def test_fit_us_vs_eu_needs_the_march_window() -> None:
    raw = generate_symbol("XAUUSD.x")[Timeframe.H1].raw
    fit = fit_offset_model(raw["time"])
    scores = {c.label: c.matched for c in fit.top_candidates}
    assert scores["us_dst(+2)"] > scores["eu_dst(+2)"]


def test_override_wins_but_inferred_is_reported() -> None:
    raw = generate_symbol("XAUUSD.x")[Timeframe.H1].raw
    fit = fit_offset_model(raw["time"], override_hours=3)
    assert fit.effective == OffsetModel.fixed(3) and fit.source == "override"
    assert fit.inferred == OffsetModel.us_dst(2)


def test_live_consistency_and_fallbacks() -> None:
    raw = generate_symbol("XAUUSD.x")[Timeframe.H1].raw
    now = datetime(2025, 7, 9, 12, tzinfo=timezone.utc)
    good = LiveOffset(3.0, True, 10800, 0, "ok")
    assert fit_offset_model(raw["time"], live=good, now_utc=now).consistent_with_live is True
    bad = LiveOffset(5.0, True, 18000, 0, "ok")
    assert fit_offset_model(raw["time"], live=bad, now_utc=now).consistent_with_live is False
    # nothing to fit: live fallback, then error
    fit = fit_offset_model([], live=good, now_utc=now)
    assert fit.source == "live" and fit.effective == OffsetModel.fixed(3)
    with pytest.raises(OffsetError):
        fit_offset_model([])


def test_fit_logs_under_dev_mode(make_settings) -> None:
    stream = io.StringIO()
    logging_setup.configure_logging(make_settings("DEV_MODE=true\n"), stream=stream, force=True)
    fit_offset_model(generate_symbol("XAUUSD.x")[Timeframe.H1].raw["time"])
    assert "offset fit: effective=us_dst(+2)" in stream.getvalue()
    assert "offset fit anchors: [('weekly_close', 'primary', 10, 10, 1.0)" in stream.getvalue()


def test_conversion_of_fixture_matches_utc_frame(synthetic) -> None:
    s = synthetic[("BRNUSD.x", Timeframe.H4)]
    utc = OffsetModel.us_dst(2).server_to_utc(s.raw["time"])
    assert np.array_equal(utc, to_epoch_seconds(s.utc["time"]))
