"""Runner, periods and history: window validation, seeded random windows, reproducibility, progress,
cancellation, provenance and the cached-data path (seeded synthetic cache, never the real data/)."""

from __future__ import annotations

import io
import logging
import threading
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from alpha_engine.backtest.history import (
    HistoryData,
    HistoryUnavailable,
    build_bar_arrays,
    data_fingerprint,
    load_history,
    scan_full_history,
)
from alpha_engine.backtest.metrics import compute_metrics, summarize_windows
from alpha_engine.backtest.models import (
    HISTORICAL_SPREAD_LABEL_FA,
    PROVISIONAL_LABEL_FA,
    ZERO_SPREAD_LABEL_FA,
    CostModel,
    RunConfig,
    RunResult,
)
from alpha_engine.backtest.periods import PeriodError, earliest_start, manual_window, random_windows
from alpha_engine.backtest.runner import BacktestCancelled, RunConfigError, run_backtest
from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.logging_setup import ROOT_LOGGER_NAME, configure_logging
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy, resolve_params
from alpha_engine.strategy.params import params_hash
from fixtures.channel_scenarios import SYMBOL, T0, make_h4, random_walk
from fixtures.seed_cache import seed_cache

STRATEGY = StdDevChannelStrategy()
SPEC = SymbolSpec(name=SYMBOL, digits=2, point=0.01, trade_contract_size=100.0, trade_tick_value=1.0,
                  trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0, currency_profit="USD",
                  currency_base="XAU")
ACCOUNT = AccountSettings(balance=10_000.0, risk_pct=1.0, leverage=100, rr=2.0)
_, CLEAN = resolve_params({})
HASH = params_hash(CLEAN)


@pytest.fixture(scope="module")
def data():
    h1, h4 = random_walk(3000, seed=21)
    h1 = h1.copy()
    h1["spread"] = np.random.default_rng(5).integers(15, 40, len(h1))
    history = HistoryData(symbol=SYMBOL, h1=h1, h4=h4, spec=SPEC)
    scan = scan_full_history(STRATEGY, h1, h4, CLEAN, ACCOUNT, symbol=SYMBOL)
    return history, scan, build_bar_arrays(h1, SPEC)


def cfg(**kw) -> RunConfig:
    base = dict(symbol=SYMBOL, mode="random", windows_count=6, window_months=1, seed=42, account=ACCOUNT,
                strategy_name="stddev_channel", strategy_version=1, params=CLEAN, params_hash=HASH)
    base.update(kw)
    return RunConfig(**base)


def h1ns(h1: pd.DataFrame) -> np.ndarray:
    return pd.DatetimeIndex(h1["time"]).as_unit("ns").asi8


# ---------------------------------------------------------------------------------------------- periods
def test_earliest_start_worked_example() -> None:
    """Continuous data from 2026-01-05: first valid H1 bar 399 sees 100 closed H4 bars; + 140 H4 bars ->
    H4 bar 239 closes at 2026-01-05 + 240 * 4h = 2026-02-14 00:00 UTC."""
    h4 = make_h4("up", n_h4=300)
    h1_ns = (T0 + pd.to_timedelta(np.arange(1200), unit="h")).as_unit("ns").asi8
    h4_ns = pd.DatetimeIndex(h4["time"]).as_unit("ns").asi8
    assert earliest_start(h1_ns, h4_ns, 399, 140) == pd.Timestamp("2026-02-14 00:00", tz="UTC")
    with pytest.raises(PeriodError) as exc:
        earliest_start(h1_ns, h4_ns[:200], 399, 140)
    assert exc.value.code == "data_too_short"
    with pytest.raises(PeriodError):
        earliest_start(h1_ns, h4_ns, None, 140)


def test_manual_window_validation(data) -> None:
    history, scan, bars = data
    times = bars.times_ns
    earliest = earliest_start(times, pd.DatetimeIndex(history.h4["time"]).as_unit("ns").asi8,
                              scan.first_valid_index, 140)
    end = pd.Timestamp(int(times[-1]), tz="UTC") + pd.Timedelta(hours=1)
    ok = manual_window(earliest, end, times, earliest, warmup_bars=140)
    assert ok.windows[0].start == earliest.to_pydatetime() and ok.data_end == end.to_pydatetime()
    cases = {
        "window_too_early": (earliest - pd.Timedelta(hours=1), end),
        "window_beyond_data": (earliest, end + pd.Timedelta(hours=1)),
        "invalid_range": (end - pd.Timedelta(days=1), end - pd.Timedelta(days=2)),
    }
    for code, (s, e) in cases.items():
        with pytest.raises(PeriodError) as exc:
            manual_window(s, e, times, earliest, warmup_bars=140)
        assert exc.value.code == code and exc.value.message_fa
    # a weekend-only window has no bars
    sat = pd.Timestamp("2025-03-08 00:00", tz="UTC")
    with pytest.raises(PeriodError) as exc:
        manual_window(sat, sat + pd.Timedelta(days=1), times, earliest, warmup_bars=140)
    assert exc.value.code == "window_empty"


def test_run_config_rejects_bad_manual_range() -> None:
    t = pd.Timestamp("2025-06-01", tz="UTC").to_pydatetime()
    with pytest.raises(ValidationError):
        cfg(mode="manual", start=t, end=t)
    with pytest.raises(ValidationError):
        cfg(mode="manual", start=t, end=None)
    with pytest.raises(ValidationError):
        cfg(mode="manual", start=t.replace(tzinfo=None), end=t + timedelta(days=5))


def test_manual_run_too_early_raises_period_error(data) -> None:
    history, scan, bars = data
    start = history.h1["time"].iloc[100].to_pydatetime()
    with pytest.raises(PeriodError) as exc:
        run_backtest(cfg(mode="manual", start=start, end=start + timedelta(days=20)), history, scan, bars=bars)
    assert exc.value.code == "window_too_early"


def test_random_windows_are_seeded_and_bounded(data) -> None:
    history, scan, bars = data
    times = bars.times_ns
    earliest = earliest_start(times, pd.DatetimeIndex(history.h4["time"]).as_unit("ns").asi8,
                              scan.first_valid_index, 140)
    a = random_windows(times, earliest, count=20, months=1, seed=123, warmup_bars=140)
    b = random_windows(times, earliest, count=20, months=1, seed=123, warmup_bars=140)
    c = random_windows(times, earliest, count=20, months=1, seed=124, warmup_bars=140)
    assert a == b and a.windows != c.windows
    assert a.algorithm == "numpy.PCG64" and a.numpy_version == np.__version__ and a.seed == 123
    assert [w.index for w in a.windows] == list(range(20))
    assert [w.start for w in a.windows] == sorted(w.start for w in a.windows)
    starts = set(pd.DatetimeIndex(pd.to_datetime(times, utc=True)).to_pydatetime())
    for w in a.windows:
        assert w.start >= earliest.to_pydatetime() and w.end <= a.data_end
        assert w.start in starts  # an H1 bar open
        assert pd.Timestamp(w.end) == pd.Timestamp(w.start) + pd.DateOffset(months=1)
    gen = random_windows(times, earliest, count=5, months=1, seed=None, warmup_bars=140)
    assert gen.seed_generated and 0 <= gen.seed < 2**63
    again = random_windows(times, earliest, count=5, months=1, seed=gen.seed, warmup_bars=140)
    assert again.windows == gen.windows
    with pytest.raises(PeriodError) as exc:
        random_windows(times, earliest, count=5, months=12, seed=1, warmup_bars=140)
    assert exc.value.code == "data_too_short"


# ---------------------------------------------------------------------------------------------- runner
def test_same_seed_gives_an_identical_result(data) -> None:
    history, scan, bars = data
    r1 = run_backtest(cfg(), history, scan, bars=bars)
    r2 = run_backtest(cfg(), history)  # fresh scan + bars
    assert r1 == r2
    assert r1.model_dump(mode="json") == r2.model_dump(mode="json")
    assert RunResult.model_validate(r1.model_dump(mode="json")) == r1
    assert r1.total_trades > 0 and len(r1.windows) == 6
    r3 = run_backtest(cfg(seed=43), history, scan, bars=bars)
    assert [w.window for w in r3.windows] != [w.window for w in r1.windows]


def test_generated_seed_reproduces(data) -> None:
    history, scan, bars = data
    r1 = run_backtest(cfg(seed=None), history, scan, bars=bars)
    assert r1.config.seed is None and r1.plan.seed_generated and r1.plan.seed is not None
    r2 = run_backtest(cfg(seed=r1.plan.seed), history, scan, bars=bars)
    assert r2.windows == r1.windows and r2.plan.windows == r1.plan.windows


def test_windows_start_independently_with_the_initial_balance(data) -> None:
    history, scan, bars = data
    res = run_backtest(cfg(), history, scan, bars=bars)
    for w in res.windows:
        assert w.initial_balance == 10_000.0 and w.equity[0].equity == 10_000.0
        assert w.final_balance == pytest.approx(10_000.0 + sum(t.net_pnl for t in w.trades))
        # metrics module reads these models directly
        m = compute_metrics(w.trades, w.equity, w.initial_balance)
        assert m.trade_count == len(w.trades)
    dist = summarize_windows(res.windows)
    assert dist.count == 6


def test_provenance_and_labels(data) -> None:
    history, scan, bars = data
    res = run_backtest(cfg(cost_model=CostModel(commission_per_lot_per_side=2.0)), history, scan, bars=bars)
    assert res.provisional and res.labels_fa[0] == PROVISIONAL_LABEL_FA
    assert "بدون سوآپ" in res.labels_fa and "کمیسیون صفر" not in res.labels_fa
    assert res.spread_source == "historical" and res.price_basis == "bid_bars"
    assert res.fingerprint.h1_rows == len(history.h1) and res.fingerprint.h1_source is None
    assert res.plan.seed == 42 and res.config.params_hash == HASH
    final = run_backtest(cfg(provisional=False), history, scan, bars=bars)
    assert not final.provisional and PROVISIONAL_LABEL_FA not in final.labels_fa and "کمیسیون صفر" in final.labels_fa
    no_spread = HistoryData(symbol=SYMBOL, h1=history.h1.drop(columns=["spread"]), h4=history.h4, spec=SPEC)
    res = run_backtest(cfg(), no_spread, scan)
    assert res.spread_source == "none" and any(x.startswith(ZERO_SPREAD_LABEL_FA) for x in res.labels_fa)
    assert res.spread_fallback.source == "none" and res.spread_fallback.points == 0
    assert HISTORICAL_SPREAD_LABEL_FA not in res.labels_fa
    # spreads on every bar: the auto fallback is resolved and recorded but never used
    fb = final.spread_fallback
    assert fb.source == "auto_median_observed" and fb.fallback_bars == 0 and fb.zero_bars == 0
    assert fb.points == int(np.ceil(np.median(history.h1["spread"].to_numpy())))
    assert HISTORICAL_SPREAD_LABEL_FA in final.labels_fa


def _leading_zero_history(history: HistoryData, zero_until: int) -> HistoryData:
    h1 = history.h1.copy()
    h1.loc[: zero_until - 1, "spread"] = 0
    return HistoryData(symbol=SYMBOL, h1=h1, h4=history.h4, spec=SPEC)


def _full_manual(history: HistoryData, scan, **kw) -> RunConfig:
    times = pd.DatetimeIndex(history.h1["time"]).as_unit("ns").asi8
    earliest = earliest_start(times, pd.DatetimeIndex(history.h4["time"]).as_unit("ns").asi8,
                              scan.first_valid_index, 140)
    end = pd.Timestamp(int(times[-1]), tz="UTC") + pd.Timedelta(hours=1)
    return cfg(mode="manual", start=earliest.to_pydatetime(), end=end.to_pydatetime(), **kw)


def test_fallback_spread_auto_user_and_zero_are_recorded_and_labelled(data) -> None:
    """Broker spread only from bar 2000 on (like the real cache): the leading bars use the fallback."""
    history, scan, _ = data
    lead = _leading_zero_history(history, 2000)
    good = lead.h1["spread"].to_numpy()[2000:]
    auto_points = int(np.ceil(np.median(good)))
    since = lead.h1["time"].iloc[2000].strftime("%Y-%m-%d %H:%M UTC")

    auto = run_backtest(_full_manual(lead, scan), lead, scan)
    fb, w = auto.spread_fallback, auto.windows[0]
    assert (fb.points, fb.source, fb.observed_bars) == (auto_points, "auto_median_observed", len(good))
    assert fb.observed_from == lead.h1["time"].iloc[2000].to_pydatetime() and fb.observed_median == np.median(good)
    i0 = int(np.searchsorted(lead.h1["time"], pd.Timestamp(w.window.start)))
    assert fb.fallback_bars == w.spread_fallback_bars == 2000 - i0 > 0 and fb.zero_bars == 0
    assert fb.total_bars == w.bars
    pct = f"{(2000 - i0) / w.bars * 100:.1f}"
    label = (f"اسپرد تاریخی بروکر فقط از {since}؛ برای {pct}٪ کندل‌ها اسپرد ثابت {auto_points} پوینت "
             f"(میانه اسپرد مشاهده‌شده) فرض شد")
    assert label in auto.labels_fa and HISTORICAL_SPREAD_LABEL_FA not in auto.labels_fa
    early = [t for t in w.trades if t.entry_time < fb.observed_from]
    assert early and all("entry_spread_fallback" in t.flags and t.spread_at_entry_points == auto_points
                         for t in early)
    for t in early:  # buy fill = bid open + fallback * point; sell fill = bid open
        expected = t.entry_bid_open + auto_points * SPEC.point if t.direction == "buy" else t.entry_bid_open
        assert t.entry == pytest.approx(expected, abs=1e-9)

    user = run_backtest(_full_manual(lead, scan, cost_model=CostModel(fallback_spread_points=50)), lead, scan)
    assert (user.spread_fallback.points, user.spread_fallback.source) == (50, "user")
    assert any("اسپرد ثابت 50 پوینت (تعیین کاربر)" in x for x in user.labels_fa)
    assert user.config.cost_model.fallback_spread_points == 50

    zero = run_backtest(_full_manual(lead, scan, cost_model=CostModel(fallback_spread_points=0)), lead, scan)
    zfb = zero.spread_fallback
    assert (zfb.points, zfb.source, zfb.fallback_bars, zfb.zero_bars) == (0, "user", 0, 2000 - i0)
    assert any(x.endswith(ZERO_SPREAD_LABEL_FA) and since in x for x in zero.labels_fa)
    assert zero.windows[0].zero_spread_bars_unfilled == 2000 - i0

    # prebuilt bar arrays must match the resolved fallback
    with pytest.raises(RunConfigError):
        run_backtest(_full_manual(lead, scan, cost_model=CostModel(fallback_spread_points=50)), lead, scan,
                     bars=build_bar_arrays(lead.h1, SPEC))
    # deterministic: same inputs -> equal results
    assert run_backtest(_full_manual(lead, scan), lead, scan) == auto


def test_config_and_scan_must_match(data) -> None:
    history, scan, bars = data
    with pytest.raises(RunConfigError):
        run_backtest(cfg(params_hash="0" * 64), history, scan, bars=bars)
    with pytest.raises(RunConfigError):
        run_backtest(cfg(account=AccountSettings(rr=3.0)), history, scan, bars=bars)  # scan made with rr 2
    with pytest.raises(RunConfigError):
        run_backtest(cfg(strategy_version=2), history, scan, bars=bars)


def test_progress_is_monotone_from_0_to_100(data) -> None:
    history, scan, bars = data
    calls: list[tuple[float, int]] = []
    run_backtest(cfg(), history, scan, bars=bars, progress_cb=lambda p, i: calls.append((p, i)))
    pcts = [p for p, _ in calls]
    assert pcts[0] == 0.0 and pcts[-1] == 100.0 and len(calls) >= 12
    assert all(b >= a for a, b in zip(pcts, pcts[1:]))
    assert {i for _, i in calls} == set(range(6))


def test_cancel_event_stops_the_run(data) -> None:
    history, scan, bars = data
    event = threading.Event()
    event.set()
    with pytest.raises(BacktestCancelled):
        run_backtest(cfg(), history, scan, bars=bars, cancel_event=event)
    event = threading.Event()
    seen: list[float] = []

    def progress(p: float, i: int) -> None:
        seen.append(p)
        if p > 30:
            event.set()

    with pytest.raises(BacktestCancelled):
        run_backtest(cfg(), history, scan, bars=bars, progress_cb=progress, cancel_event=event)
    assert max(seen) < 100.0


# ---------------------------------------------------------------------------------------------- cached data
def test_seeded_cache_end_to_end(tmp_path) -> None:
    """Synthetic seed cache (tgc_testdata layout, source=seed) in a temp dir: load, fingerprint, manual run.
    The seeded data has DST-shifted H4, session breaks, holidays and planted missing bars."""
    seed_cache(tmp_path, repo_data_dir=tmp_path / "not_data")
    cache = OhlcvCache(tmp_path)
    history = load_history(cache, SYMBOL)
    fp = data_fingerprint(history)
    assert fp.h1_source == "seed" and fp.h4_source == "seed" and fp.spec_source == "seed"
    assert fp.h1_rows == len(history.h1) and len(fp.h1_sha256) == 64
    assert fp == data_fingerprint(load_history(cache, SYMBOL))
    bars = build_bar_arrays(history.h1, history.spec)
    assert bars.missing_gap_after.sum() >= 3  # planted single missing bars / missing H4 buckets
    scan = scan_full_history(STRATEGY, history.h1, history.h4, CLEAN, ACCOUNT, symbol=SYMBOL)
    earliest = earliest_start(bars.times_ns, pd.DatetimeIndex(history.h4["time"]).as_unit("ns").asi8,
                              scan.first_valid_index, 140)
    end = pd.Timestamp(int(bars.times_ns[-1]), tz="UTC") + pd.Timedelta(hours=1)
    res = run_backtest(cfg(mode="manual", start=earliest.to_pydatetime(), end=end.to_pydatetime()), history, scan,
                       bars=bars)
    assert res.fingerprint == fp and res.spread_source == "historical"
    assert res.windows[0].bars > 0
    with pytest.raises(HistoryUnavailable):
        load_history(cache, "BRNUSD.y")


# ---------------------------------------------------------------------------------------------- DEV_MODE logs
class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())  # raises on a bad format string -> the test fails


@pytest.mark.parametrize("dev", [True, False])
def test_decision_logs_only_under_dev_mode(make_settings, data, dev: bool) -> None:
    history, scan, bars = data
    configure_logging(make_settings(f"DEV_MODE={'true' if dev else 'false'}\n"), stream=io.StringIO(), force=True)
    handler = _ListHandler()
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.addHandler(handler)
    try:
        res = run_backtest(cfg(windows_count=2), history, scan, bars=bars)
    finally:
        root.removeHandler(handler)
    msgs = [m for m in handler.messages if m.startswith("bt ")]
    if dev:
        fills = [m for m in msgs if " fill " in m]
        exits = [m for m in msgs if " exit " in m]
        decisions = [m for m in msgs if " decision @ " in m]
        assert len(fills) == len(exits) == res.total_trades > 0
        assert len(decisions) == sum(w.candidates for w in res.windows)
        assert "SL=" in fills[0] and "vol=" in fills[0] and "balance" in exits[0]
        assert all("password" not in m.lower() for m in handler.messages)
    else:
        assert msgs == []
