"""H4 -> H1 alignment (last closed H4 bar) and channel projection: worked examples, DST, gaps, properties."""

from __future__ import annotations

import io
import logging

import numpy as np
import pandas as pd
import pytest

from alpha_engine.indicators.atr import wilder_atr
from alpha_engine.indicators.mtf import h1_bars_since, last_closed_index, project_channel_to_h1
from alpha_engine.indicators.regression_channel import rolling_regression_channel
from alpha_engine.logging_setup import configure_logging, get_logger

H1 = pd.Timedelta(hours=1)
H4 = pd.Timedelta(hours=4)


def utc(*stamps: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(list(stamps), tz="UTC")


def hourly(start: str, periods: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=periods, freq="h", tz="UTC")


def h1_frame(times: pd.DatetimeIndex) -> pd.DataFrame:
    """Canonical-shaped H1 frame (only `time` matters for alignment)."""
    size = len(times)
    price = 2000.0 + np.arange(size, dtype=float)
    return pd.DataFrame({
        "time": times, "open": price, "high": price + 1, "low": price - 1, "close": price + 0.5,
        "tick_volume": np.ones(size, dtype=np.int64), "spread": np.zeros(size, dtype=np.int64),
        "real_volume": np.zeros(size, dtype=np.int64),
    })


# Worked example channel for H4 bars opening 04:00 and 08:00.
H4_TIMES = utc("2024-03-05 04:00", "2024-03-05 08:00")
H4_CHANNEL = pd.DataFrame({
    "mid": [100.0, 110.0], "upper": [104.0, 114.0], "lower": [96.0, 106.0],
    "slope": [2.0, 3.0], "sigma": [2.0, 2.0],
})


# --- last_closed_index ---------------------------------------------------------------------------

def test_worked_example_last_closed_index() -> None:
    """H4 04:00 closes 08:00, H4 08:00 closes 12:00. H1 bar T is decided at T+1h.
    08:00 -> D=09:00 -> j*=0 (04:00) ; 09:00 -> 0 ; 10:00 -> 0 ; 11:00 -> D=12:00 -> j*=1 (08:00, closes exactly at D).
    """
    h1 = utc("2024-03-05 08:00", "2024-03-05 09:00", "2024-03-05 10:00", "2024-03-05 11:00")
    assert last_closed_index(H4_TIMES, h1).tolist() == [0, 0, 0, 1]


def test_no_closed_h4_bar_yet_is_minus_one() -> None:
    h1 = utc("2024-03-05 04:00", "2024-03-05 05:00", "2024-03-05 06:00", "2024-03-05 07:00")
    # 04..06 decided at 05..07: H4 04:00 not closed until 08:00. 07:00 decided at 08:00 -> usable.
    assert last_closed_index(H4_TIMES, h1).tolist() == [-1, -1, -1, 0]


def test_dst_shifted_h4_boundaries() -> None:
    """Summer: broker H4 bars open at 03:00/07:00/11:00 UTC. Flooring to 4h (04:00/08:00) would be wrong.
    H1 06:00 (D=07:00): H4 03:00 closes 07:00 -> j*=03:00. H1 09:00 (D=10:00): still 03:00 (07:00 closes 11:00).
    H1 10:00 (D=11:00): H4 07:00 -> j*=1.
    """
    h4 = utc("2024-06-04 03:00", "2024-06-04 07:00", "2024-06-04 11:00")
    h1 = hourly("2024-06-04 03:00", 12)  # 03:00 .. 14:00
    j = last_closed_index(h4, h1)
    expected = {"03": -1, "04": -1, "05": -1, "06": 0, "07": 0, "08": 0, "09": 0,
                "10": 1, "11": 1, "12": 1, "13": 1, "14": 2}
    assert dict(zip(h1.strftime("%H"), j.tolist())) == expected
    # the "floor to 4h" shortcut would disagree (proves we are not using it)
    floored = h1.floor("4h") - H4
    assert not np.array_equal(np.searchsorted(h4.asi8, floored.asi8, side="right") - 1, j)


def test_weekend_gap_alignment() -> None:
    """Fri 2024-01-05 16:00/20:00 H4, then Mon 00:00/04:00. H1 Mon 00:00 (D=01:00) uses Fri 20:00
    (closed Sat 00:00); H1 Mon 03:00 (D=04:00) uses Mon 00:00."""
    h4 = utc("2024-01-05 16:00", "2024-01-05 20:00", "2024-01-08 00:00", "2024-01-08 04:00")
    h1 = hourly("2024-01-05 16:00", 8).append(hourly("2024-01-08 00:00", 8))
    j = last_closed_index(h4, h1)
    assert j.tolist() == [-1, -1, -1, 0, 0, 0, 0, 1,   1, 1, 1, 2, 2, 2, 2, 3]


def test_non_utc_tz_is_converted_and_naive_rejected() -> None:
    h4_tehran = H4_TIMES.tz_convert("Asia/Tehran")
    h1 = utc("2024-03-05 08:00", "2024-03-05 11:00")
    assert last_closed_index(h4_tehran, h1.tz_convert("Europe/Athens")).tolist() == [0, 1]
    with pytest.raises(ValueError, match="tz-aware"):
        last_closed_index(H4_TIMES.tz_localize(None), h1)
    with pytest.raises(ValueError, match="strictly increasing"):
        last_closed_index(H4_TIMES[::-1], h1)
    with pytest.raises(ValueError, match="strictly increasing"):
        last_closed_index(H4_TIMES, utc("2024-03-05 08:00", "2024-03-05 08:00"))
    with pytest.raises(ValueError, match="positive"):
        last_closed_index(H4_TIMES, h1, h4_duration=pd.Timedelta(0))


def _random_bar_times(rng: np.random.Generator, count: int, step_h: int, start: str) -> pd.DatetimeIndex:
    """Bar opens every `step_h` hours with random multi-step gaps (weekends/holidays/missing bars)."""
    steps = rng.choice([1, 1, 1, 1, 1, 2, 3, 13], size=count)
    offsets = np.cumsum(steps) * step_h
    return pd.Timestamp(start, tz="UTC") + pd.to_timedelta(offsets, unit="h")


@pytest.mark.parametrize("seed", range(5))
def test_property_never_uses_unclosed_h4_and_is_latest(seed: int) -> None:
    rng = np.random.default_rng(seed)
    h4 = _random_bar_times(rng, 400, 4, "2024-01-01 00:00") + pd.Timedelta(hours=int(rng.integers(0, 4)))
    h1 = _random_bar_times(rng, 1600, 1, "2023-12-31 20:00")
    j = last_closed_index(h4, h1)
    decision = (h1 + H1).asi8                  # int64 ns, UTC
    h4_close = (h4 + H4).asi8
    has = j >= 0
    assert np.all(h4_close[j[has]] <= decision[has])            # closed at decision time
    nxt = j + 1
    later = nxt < len(h4)
    assert np.all(h4_close[nxt[later]] > decision[later])        # and the latest such bar
    # brute force equivalence on every H1 bar: all (H1, H4) pairs as a boolean matrix
    closed = h4_close[None, :] <= decision[:, None]
    brute = np.where(closed.any(axis=1), len(h4) - 1 - np.argmax(closed[:, ::-1], axis=1), -1)
    assert j.tolist() == brute.tolist()
    assert has.any() and (~has).any() and later.any()           # both edges exercised


# --- h1_bars_since -------------------------------------------------------------------------------

def test_bars_since_worked_example_and_coverage() -> None:
    h1 = hourly("2024-03-05 04:00", 8)  # 04:00 .. 11:00
    j = last_closed_index(H4_TIMES, h1)
    m = h1_bars_since(H4_TIMES, j, h1)
    # 04..06 no H4 yet; 07 -> m=3 (04,05,06); 08 -> 4; 09 -> 5; 10 -> 6; 11 on H4 08:00 -> 3 (08,09,10)
    assert m.tolist() == [-1, -1, -1, 3, 4, 5, 6, 3]
    # H1 history starting after the H4 bar opened: the count would be truncated -> unknown (-1)
    late = hourly("2024-03-05 08:00", 4)
    m_late = h1_bars_since(H4_TIMES, last_closed_index(H4_TIMES, late), late)
    assert m_late.tolist() == [-1, -1, -1, 3]


def test_bars_since_counts_bars_not_hours_across_weekend() -> None:
    h4 = utc("2024-01-05 16:00", "2024-01-05 20:00", "2024-01-08 00:00")
    h1 = hourly("2024-01-05 16:00", 8).append(hourly("2024-01-08 00:00", 4))
    m = h1_bars_since(h4, last_closed_index(h4, h1), h1)
    # Mon 00:00 on Fri 20:00: H1 bars Fri 20,21,22,23 -> 4 (not 52 hours); Mon 03:00 on Mon 00:00 -> 3
    assert m[8:].tolist() == [4, 5, 6, 3]


def test_bars_since_validation() -> None:
    h1 = hourly("2024-03-05 08:00", 2)
    with pytest.raises(ValueError):
        h1_bars_since(H4_TIMES, np.array([0]), h1)
    with pytest.raises(ValueError):
        h1_bars_since(H4_TIMES, np.array([0, 5]), h1)


@pytest.mark.parametrize("seed", range(3))
def test_bars_since_matches_brute_force(seed: int) -> None:
    rng = np.random.default_rng(100 + seed)
    h4 = _random_bar_times(rng, 200, 4, "2024-01-01 00:00")
    h1 = pd.Timestamp("2024-01-01 00:00", tz="UTC") + pd.to_timedelta(
        np.sort(rng.choice(np.arange(1, 800 * 4), size=900, replace=False)), unit="h")
    j = last_closed_index(h4, h1)
    m = h1_bars_since(h4, j, h1)
    for i in range(len(h1)):
        if j[i] < 0 or h1[0] > h4[j[i]]:
            assert m[i] == -1
        else:
            assert m[i] == int(np.sum((h1 >= h4[j[i]]) & (h1 < h1[i])))


# --- projection ----------------------------------------------------------------------------------

def test_projection_worked_example_bar_count() -> None:
    h1 = h1_frame(hourly("2024-03-05 04:00", 8))  # 04:00 .. 11:00
    out = project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1, n=100)
    assert list(out.columns) == ["mid", "upper", "lower", "slope", "sigma", "h4_index", "bars_ahead"]
    assert out.index.equals(h1.index)
    rows = out.iloc[4:]  # H1 08:00 .. 11:00
    assert rows["h4_index"].tolist() == [0, 0, 0, 1]
    assert rows["bars_ahead"].tolist() == [1.0, 1.25, 1.5, 0.75]
    # mid = mid_j + slope_j*bars_ahead: 100+2*1=102, 100+2*1.25=102.5, 100+2*1.5=103, 110+3*0.75=112.25
    assert rows["mid"].tolist() == pytest.approx([102.0, 102.5, 103.0, 112.25])
    assert rows["upper"].tolist() == pytest.approx([106.0, 106.5, 107.0, 116.25])
    assert rows["lower"].tolist() == pytest.approx([98.0, 98.5, 99.0, 108.25])
    assert rows["slope"].tolist() == [2.0, 2.0, 2.0, 3.0]
    assert rows["sigma"].tolist() == [2.0, 2.0, 2.0, 2.0]
    # 04..06: no closed H4 bar -> NaN and h4_index -1 ; 07:00 -> m=3 -> 0.75
    assert out.iloc[:3][["mid", "upper", "lower", "slope", "sigma", "bars_ahead"]].isna().all().all()
    assert out["h4_index"].iloc[:3].tolist() == [-1, -1, -1]
    assert out["bars_ahead"].iloc[3] == 0.75 and out["mid"].iloc[3] == pytest.approx(101.5)


def test_projection_calendar_mode() -> None:
    h1 = h1_frame(hourly("2024-03-05 04:00", 8))
    out = project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1, n=100, mode="calendar")
    # without gaps calendar == bar_count
    assert out["bars_ahead"].iloc[3:].tolist() == [0.75, 1.0, 1.25, 1.5, 0.75]


def test_projection_weekend_bar_count_vs_calendar() -> None:
    h4 = utc("2024-01-05 16:00", "2024-01-05 20:00", "2024-01-08 00:00")
    channel = pd.DataFrame({"mid": [10.0, 20.0, 30.0], "upper": [11.0, 21.0, 31.0],
                            "lower": [9.0, 19.0, 29.0], "slope": [1.0, 2.0, 3.0], "sigma": [0.5] * 3})
    h1 = h1_frame(hourly("2024-01-05 16:00", 8).append(hourly("2024-01-08 00:00", 4)))
    bc = project_channel_to_h1(channel, h4, h1, n=50)
    cal = project_channel_to_h1(channel, h4, h1, n=50, mode="calendar")
    # Mon 00:00 on Fri 20:00: bar_count m=4 -> 1.0 ; calendar 52h/4h = 13.0
    assert bc["bars_ahead"].iloc[8] == 1.0 and cal["bars_ahead"].iloc[8] == 13.0
    assert bc["mid"].iloc[8] == pytest.approx(20.0 + 2.0 * 1.0)
    assert cal["mid"].iloc[8] == pytest.approx(20.0 + 2.0 * 13.0)
    # Mon 03:00 on Mon 00:00: both 0.75
    assert bc["bars_ahead"].iloc[11] == 0.75 and cal["bars_ahead"].iloc[11] == 0.75
    # max_bars_ahead blanks the stale calendar extrapolation instead of using it
    capped = project_channel_to_h1(channel, h4, h1, n=50, mode="calendar", max_bars_ahead=2.0)
    assert np.isnan(capped["mid"].iloc[8]) and capped["h4_index"].iloc[8] == 1
    assert capped["mid"].iloc[11] == pytest.approx(30.0 + 3.0 * 0.75)


def test_projection_dst_shifted() -> None:
    h4 = utc("2024-06-04 03:00", "2024-06-04 07:00")
    h1 = h1_frame(hourly("2024-06-04 03:00", 8))  # 03:00 .. 10:00
    out = project_channel_to_h1(H4_CHANNEL, h4, h1, n=100)
    # 06:00 on H4 03:00 -> m=3 -> 0.75 ; 07 -> 1.0 ; 08 -> 1.25 ; 09 -> 1.5 ; 10:00 on H4 07:00 -> m=3 -> 0.75
    assert out["h4_index"].tolist() == [-1, -1, -1, 0, 0, 0, 0, 1]
    assert out["bars_ahead"].iloc[3:].tolist() == [0.75, 1.0, 1.25, 1.5, 0.75]


def test_projection_atr_and_is_flat() -> None:
    h1 = h1_frame(hourly("2024-03-05 04:00", 8))
    atr = pd.Series([250.0, 250.0])
    out = project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1, n=100, h4_atr=atr)
    # |slope|*n: bar 0 -> 2*100=200 < 250 flat ; bar 1 -> 3*100=300 >= 250 not flat
    assert out["is_flat"].tolist() == [False, False, False, True, True, True, True, False]
    assert out["atr_h4"].iloc[3:].tolist() == [250.0] * 5 and out["atr_h4"].iloc[:3].isna().all()
    out2 = project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1, n=100, h4_atr=atr, flat_mult=0.5)
    assert not out2["is_flat"].any()  # 200 < 125 False, 300 < 125 False


def test_projection_validation() -> None:
    h1 = h1_frame(hourly("2024-03-05 04:00", 4))
    with pytest.raises(ValueError, match="mode"):
        project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1, n=100, mode="linear")
    with pytest.raises(ValueError, match="rows"):
        project_channel_to_h1(H4_CHANNEL.iloc[:1], H4_TIMES, h1, n=100)
    with pytest.raises(ValueError, match="missing"):
        project_channel_to_h1(H4_CHANNEL.drop(columns="sigma"), H4_TIMES, h1, n=100)
    with pytest.raises(ValueError):
        project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1, n=2)
    with pytest.raises(ValueError, match="h4_atr"):
        project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1, n=100, h4_atr=[1.0])
    with pytest.raises(ValueError):
        project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1.drop(columns="time"), n=100)
    with pytest.raises(ValueError):
        project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1, n=100, max_bars_ahead=-1)


def test_projection_empty_inputs() -> None:
    empty_h4 = pd.DatetimeIndex([], tz="UTC")
    empty_channel = H4_CHANNEL.iloc[:0]
    h1 = h1_frame(hourly("2024-03-05 04:00", 3))
    out = project_channel_to_h1(empty_channel, empty_h4, h1, n=100, h4_atr=[])
    assert out["h4_index"].tolist() == [-1, -1, -1] and out["mid"].isna().all()
    out_cal = project_channel_to_h1(empty_channel, empty_h4, h1, n=100, mode="calendar")
    assert out_cal["mid"].isna().all()
    none = project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1_frame(hourly("2024-03-05 04:00", 0)), n=100)
    assert len(none) == 0


def _synthetic_h1_with_weekends(weeks: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    times = pd.date_range("2024-01-01", periods=weeks * 7 * 24, freq="h", tz="UTC")
    times = times[times.dayofweek < 5]  # drop Sat/Sun
    close = 2000.0 + np.cumsum(rng.normal(0, 1.0, len(times)))
    frame = h1_frame(times)
    frame["close"] = close
    frame["high"] = close + rng.exponential(0.5, len(times))
    frame["low"] = close - rng.exponential(0.5, len(times))
    return frame


def _resample_h4(h1: pd.DataFrame) -> pd.DataFrame:
    g = h1.set_index("time").resample("4h", label="left", closed="left")
    h4 = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()}).dropna()
    return h4.reset_index()


def test_end_to_end_no_look_ahead_when_future_h4_bars_are_appended() -> None:
    h1 = _synthetic_h1_with_weekends(weeks=12, seed=21)
    h4 = _resample_h4(h1)
    n = 20

    def run(h4_part: pd.DataFrame, h1_part: pd.DataFrame) -> pd.DataFrame:
        ch = rolling_regression_channel(h4_part["close"], n=n)
        atr = wilder_atr(h4_part["high"], h4_part["low"], h4_part["close"])
        return project_channel_to_h1(ch, h4_part["time"], h1_part, n=n, h4_atr=atr)

    full = run(h4, h1)
    # every used H4 bar was closed at the H1 decision time
    used = full["h4_index"].to_numpy()
    ok = used >= 0
    decision = pd.DatetimeIndex(h1["time"] + H1)
    h4_close = pd.DatetimeIndex(h4["time"] + H4)
    assert bool(np.all(h4_close[used[ok]].asi8 <= decision[ok].asi8))
    # the projected mid equals the H4 regression line extended by bars_ahead
    ch = rolling_regression_channel(h4["close"], n=n)
    valid = full["mid"].notna().to_numpy()
    expect = ch["mid"].to_numpy()[used[valid]] + ch["slope"].to_numpy()[used[valid]] * full["bars_ahead"][valid]
    np.testing.assert_allclose(full["mid"][valid], expect, rtol=1e-13)
    # truncate at an H1 bar: only H4 bars closed by its decision time are available -> identical rows
    cut = 1000
    t_dec = h1["time"].iloc[cut] + H1
    h4_known = h4[h4["time"] + H4 <= t_dec].reset_index(drop=True)
    part = run(h4_known, h1.iloc[:cut + 1])
    pd.testing.assert_frame_equal(part, full.iloc[:cut + 1], check_exact=False, rtol=1e-12)
    # weekend rows: bar_count bars_ahead stays in the normal [0.75, 1.5] range
    assert full["bars_ahead"].dropna().between(0.75, 1.5).all()


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.parametrize("dev_mode", [True, False])
def test_guarded_summary_log(make_settings, dev_mode: bool) -> None:
    configure_logging(make_settings(f"DEV_MODE={'true' if dev_mode else 'false'}\n"),
                      stream=io.StringIO(), force=True)
    logger = get_logger("alpha_engine.indicators.mtf")
    handler = _ListHandler()
    logger.addHandler(handler)
    try:
        project_channel_to_h1(H4_CHANNEL, H4_TIMES, h1_frame(hourly("2024-03-05 04:00", 8)), n=100)
    finally:
        logger.removeHandler(handler)
    if dev_mode:
        assert len(handler.messages) == 1
        assert "h1_rows=8" in handler.messages[0] and "no_closed_h4=3" in handler.messages[0]
    else:
        assert handler.messages == []
