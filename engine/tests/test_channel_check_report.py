"""channel_check_report: cache-only Persian report for the MT5 StdDev-channel comparison.

Numbers must be exactly the engine's (``rolling_regression_channel`` / ``project_channel_to_h1``); the
synthetic cache (``fixtures.seed_cache``, offset model ``us_dst(+2)``) lives in ``tmp_path``.
"""

from __future__ import annotations

import ast
import io
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha_engine import logging_setup
from alpha_engine.data.cache import CacheMeta, OhlcvCache
from alpha_engine.data.timezone import OffsetModel
from alpha_engine.indicators.atr import sma_atr, wilder_atr
from alpha_engine.indicators.mtf import project_channel_to_h1
from alpha_engine.indicators.regression_channel import rolling_regression_channel
from alpha_engine.tools import channel_check_report as ccr
from fixtures.seed_cache import seed_cache

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
SYMBOL = "XAUUSD.x"
MODULE_FILE = Path(ccr.__file__)


def _frame(closes: list[float], start: str = "2025-01-06 00:00") -> pd.DataFrame:
    times = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=len(closes), freq="4h")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"time": times, "open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
                         "tick_volume": 1, "spread": 0, "real_volume": 0})


@pytest.fixture
def cache_env(make_settings, tmp_path: Path):
    data = tmp_path / "data"
    seed_cache(data, repo_data_dir=tmp_path / "elsewhere")
    settings = make_settings(environ={"ALPHA_TRADER_DATA_DIR": str(data)})
    cache = OhlcvCache(data)
    h4, meta = cache.read(SYMBOL, "H4")
    h1, _ = cache.read(SYMBOL, "H1")
    return settings, cache, h4, h1, meta


def _run(settings, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    return ccr.run(list(args), settings=settings, out=out, now=NOW), out.getvalue()


# --- worked example (module docstring) -------------------------------------------------------------------

def test_worked_example_three_points_both_ddof() -> None:
    check = ccr.compute_check(_frame([1, 3, 2, 5, 4]), None, symbol=SYMBOL, n=5, k=2.0, atr_period=3)
    assert check.slope == pytest.approx(0.8, rel=1e-12)
    assert check.mid_last == pytest.approx(4.6, rel=1e-12)
    assert check.sigma[0] == pytest.approx(math.sqrt(0.72), rel=1e-12)  # 0.848528
    assert check.sigma[1] == pytest.approx(math.sqrt(0.9), rel=1e-12)   # 0.948683
    first, middle, last = check.points
    assert (first.index, middle.index, last.index) == (0, 2, 4)
    assert first.mid == pytest.approx(1.4, rel=1e-12)    # 4.6 + 0.8 * (0 - 4)
    assert middle.mid == pytest.approx(3.0, rel=1e-12)   # 4.6 + 0.8 * (2 - 4)
    assert last.upper[0] == pytest.approx(4.6 + 2 * math.sqrt(0.72), rel=1e-12)  # 6.297056
    assert last.upper[1] == pytest.approx(4.6 + 2 * math.sqrt(0.9), rel=1e-12)   # 6.497367
    assert first.lower[0] == pytest.approx(1.4 - 2 * math.sqrt(0.72), rel=1e-12)
    assert check.change_abs == pytest.approx(4.0)  # |0.8| * 5
    assert check.h1_available is False and check.h1_rows == []


# --- numbers equal to the engine on the synthetic cache -----------------------------------------------

def test_numbers_equal_rolling_regression_channel(cache_env) -> None:
    _, _, h4, h1, _ = cache_env
    assert len(h4) > 200
    end_idx = 180
    end = h4["time"].iloc[end_idx] + pd.Timedelta(hours=1)  # not aligned -> latest open <= end
    check = ccr.compute_check(h4, h1, symbol=SYMBOL, n=100, k=2.0, end=end)
    window = h4.iloc[end_idx - 99:end_idx + 1].reset_index(drop=True)
    pd.testing.assert_frame_equal(check.window, window)
    for d in (0, 1):
        ref = rolling_regression_channel(h4["close"], 100, 2.0, d).iloc[end_idx]
        last = check.points[2]
        # exact (bit-for-bit) equality with the engine at the last bar
        assert (last.mid, last.upper[d], last.lower[d]) == (ref["mid"], ref["upper"], ref["lower"])
        assert (check.slope, check.sigma[d]) == (ref["slope"], ref["sigma"])
    # first/middle points = the regression line of the 100 closes (independent check with polyfit)
    slope, intercept = np.polyfit(np.arange(100.0), window["close"].to_numpy(), 1)
    for p in check.points:
        assert p.mid == pytest.approx(intercept + slope * p.index, abs=1e-8)
        assert p.time_utc == window["time"].iloc[p.index]
    assert [p.index for p in check.points] == [0, 49, 99]
    upto = h4.iloc[: end_idx + 1]
    assert check.atr_wilder == wilder_atr(upto["high"], upto["low"], upto["close"], 14).iloc[-1]
    assert check.atr_sma == sma_atr(upto["high"], upto["low"], upto["close"], 14).iloc[-1]


def test_h1_projection_equals_engine_projection(cache_env) -> None:
    _, _, h4, h1, _ = cache_env
    end_idx = 150
    open_end = h4["time"].iloc[end_idx]
    assert ((h1["time"] >= open_end) & (h1["time"] < open_end + pd.Timedelta(hours=4))).sum() == 4  # complete
    check = ccr.compute_check(h4, h1, symbol=SYMBOL, n=100, k=2.0, end=open_end)
    assert check.h1_in_last_h4 == 4
    # The 4 H1 bars decided with THIS channel: opening 1h before the H4 bar closes .. 2h after it closed.
    assert [r.time_utc for r in check.h1_rows] == [open_end + pd.Timedelta(hours=h) for h in (3, 4, 5, 6)]
    assert [r.bars_ahead for r in check.h1_rows] == [0.75, 1.0, 1.25, 1.5]
    assert all(r.uses_window_bar and r.h4_open_utc == open_end for r in check.h1_rows)
    atr4 = wilder_atr(h4["high"], h4["low"], h4["close"], 14).to_numpy()
    for d in (0, 1):
        ref = project_channel_to_h1(rolling_regression_channel(h4["close"], 100, 2.0, d), h4["time"], h1, 100,
                                    "bar_count", h4_atr=atr4).set_index(h1["time"])
        for r in check.h1_rows:
            row = ref.loc[r.time_utc]
            assert (r.mid, r.upper[d], r.lower[d], r.bars_ahead) == (row["mid"], row["upper"], row["lower"],
                                                                      row["bars_ahead"])
    for r in check.h1_rows:  # projection of THIS channel: mid_last + slope * bars_ahead, exactly
        assert r.mid == check.mid_last + check.slope * r.bars_ahead
        assert r.upper[0] == check.points[2].upper[0] + check.slope * r.bars_ahead


def test_truncated_friday_h4_bar_caveat(tmp_path: Path) -> None:
    """Window ends on a Friday 20:00 H4 bar holding ONE H1 bar: bar_count gives 0.25 on Monday 00:00."""
    hours = pd.date_range(pd.Timestamp("2025-01-06 00:00", tz="UTC"), pd.Timestamp("2025-01-13 03:00", tz="UTC"),
                          freq="h")
    friday_close = pd.Timestamp("2025-01-10 21:00", tz="UTC")
    hours = hours[(hours < friday_close) | (hours >= pd.Timestamp("2025-01-13 00:00", tz="UTC"))]
    rng = np.random.default_rng(3)
    h1 = _frame(list(2000 + np.cumsum(rng.normal(0, 1, len(hours)))))
    h1["time"] = hours
    h4 = (h1.groupby(h1["time"].dt.floor("4h"))
          .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
          .reset_index())
    friday_20 = pd.Timestamp("2025-01-10 20:00", tz="UTC")
    check = ccr.compute_check(h4, h1, symbol=SYMBOL, n=10, k=2.0, end=friday_20, atr_period=3)
    assert check.points[2].time_utc == friday_20 and check.h1_in_last_h4 == 1
    got = [(r.time_utc.strftime("%a %H:%M"), r.bars_ahead, r.bars_ahead_calendar) for r in check.h1_rows]
    # Mon 00:00: m = 1 H1 bar in [Fri 20:00, Mon 00:00) -> 0.25; calendar: 52h / 4h = 13.
    assert got == [("Mon 00:00", 0.25, 13.0), ("Mon 01:00", 0.5, 13.25), ("Mon 02:00", 0.75, 13.5)]
    text = ccr.render_report(check, meta=CacheMeta(symbol=SYMBOL, timeframe="H4", source="seed",
                                                   offset_model="fixed(0)", fetched_at_utc="2025-01-13T04:00:00Z"),
                             digits=2, cache_dir=tmp_path, now=NOW)
    assert "هشدار کندل H4 ناقص" in text and "فقط ۱ کندل H1" in text and "فقط ۳ کندل H1 با این کانال" in text
    assert "| 2025-01-13 00:00Z | 2025.01.13 00:00 | 2025-01-10 20:00Z | ۱ از ۴ | 0.25 | 13 |" in text


# --- the report file ------------------------------------------------------------------------------------

def test_report_persian_with_utc_server_times_and_instructions(cache_env, tmp_path: Path) -> None:
    settings, cache, h4, h1, meta = cache_env
    end_idx = 150
    out_path = tmp_path / "reports" / "channel_check.md"
    code, printed = _run(settings, "--symbol", SYMBOL, "--out", str(out_path),
                         "--end", h4["time"].iloc[end_idx].strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert code == 0 and out_path.is_file() and "report written" in printed
    text = out_path.read_text(encoding="utf-8")
    assert text.startswith(f"# گزارش مقایسه کانال انحراف معیار با MT5 — {SYMBOL}")
    assert meta.offset_model == "us_dst(+2)" and "`us_dst(+2)`" in text
    model = OffsetModel.parse(meta.offset_model)
    check = ccr.compute_check(h4, h1, symbol=SYMBOL, n=100, k=2.0, end=h4["time"].iloc[end_idx])
    for p in check.points:
        server = p.time_utc.tz_convert(None) + pd.Timedelta(hours=model.offset_hours_at(p.time_utc))
        assert f"| {p.time_utc.strftime('%Y-%m-%d %H:%M')}Z | {server.strftime('%Y.%m.%d %H:%M')} |" in text
        # 5 decimals for gold (digits 2 + 3), both ddof on one row
        assert (f"| {p.mid:.5f} | {p.upper[0]:.5f} | {p.lower[0]:.5f} | {p.upper[1]:.5f} | {p.lower[1]:.5f} |"
                in text)
    for needle in ("Insert ← Objects ← Channels ← Standard Deviation Channel", "**Deviation** = `2`",
                   "**Ray right**", "Price 1 و Price 2", "ddof=0", "ddof=1", "SMA (اندیکاتور ATR داخلی MT5)",
                   "Wilder (روش engine)", "## ۵. امتداد کانال روی H1", "نکته کندل H4 ناقص", "bars_ahead = 0.25",
                   "۱۰۰ کندل H4 بسته‌شده", "۵۰ از ۱۰۰", "۱۰۰ از ۱۰۰"):
        assert needle in text, needle
    first_server = (check.points[0].time_utc.tz_convert(None)
                    + pd.Timedelta(hours=model.offset_hours_at(check.points[0].time_utc)))
    assert f"(Time 1) = `{first_server.strftime('%Y.%m.%d %H:%M')}`" in text
    assert text.count("| ") > 40
    r0 = check.h1_rows[0]
    assert f"| {r0.mid:.5f} | {r0.upper[0]:.5f} | {r0.lower[0]:.5f} |" in text


def test_default_end_is_last_cached_h4_bar(cache_env, tmp_path: Path) -> None:
    settings, _, h4, h1, _ = cache_env
    path = tmp_path / "default.md"
    assert _run(settings, "--symbol", SYMBOL, "--out", str(path))[0] == 0
    check = ccr.compute_check(h4, h1, symbol=SYMBOL)
    assert check.points[2].time_utc == h4["time"].iloc[-1]
    text = path.read_text(encoding="utf-8")
    assert f"| آخرین کندل | ۱۰۰ از ۱۰۰ | {h4['time'].iloc[-1].strftime('%Y-%m-%d %H:%M')}Z |" in text


def test_data_dir_argument_and_exit_codes(cache_env, make_settings, tmp_path: Path) -> None:
    settings, _, _, _, _ = cache_env
    other = make_settings(environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "empty")})
    ok_path = tmp_path / "via_arg.md"
    assert _run(other, "--symbol", SYMBOL, "--out", str(ok_path), "--data-dir", str(tmp_path / "data"))[0] == 0
    code, printed = _run(other, "--symbol", SYMBOL, "--out", str(tmp_path / "x.md"))
    assert code == 1 and "no cached H4 data" in printed
    code, printed = _run(settings, "--symbol", SYMBOL, "--out", str(tmp_path / "x.md"), "--n", "500")
    assert code == 1 and "not enough data" in printed
    code, _ = _run(settings, "--symbol", SYMBOL, "--out", str(tmp_path / "x.md"), "--end", "2000-01-01")
    assert code == 1
    for bad in (["--end", "yesterday-ish"], ["--n", "2"], ["--k", "0"], ["--symbol", "../evil"]):
        args = ["--symbol", SYMBOL, "--out", str(tmp_path / "x.md"), *bad]
        assert _run(settings, *args)[0] == 2, bad
    assert not (tmp_path / "x.md").exists()


def test_report_never_writes_the_cache(cache_env, tmp_path: Path) -> None:
    settings, cache, _, _, _ = cache_env
    before = {p: p.stat().st_mtime_ns for p in cache.root.rglob("*") if p.is_file()}
    assert _run(settings, "--symbol", SYMBOL, "--out", str(tmp_path / "r.md"))[0] == 0
    after = {p: p.stat().st_mtime_ns for p in cache.root.rglob("*") if p.is_file()}
    assert before == after


def test_module_is_cache_only_no_mt5_imports() -> None:
    tree = ast.parse(MODULE_FILE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    assert not any("MetaTrader5" in name or "mt5_adapter" in name or "market_data" in name for name in imported)


def test_guarded_logs(cache_env, make_settings, tmp_path: Path) -> None:
    _, _, h4, h1, _ = cache_env
    for dev in (True, False):
        stream = io.StringIO()
        logging_setup.configure_logging(make_settings(f"DEV_MODE={'true' if dev else 'false'}\n"),
                                        stream=stream, force=True)
        ccr.compute_check(h4, h1, symbol=SYMBOL)
        assert ("channel check XAUUSD.x: n=100 k=2" in stream.getvalue()) is dev
