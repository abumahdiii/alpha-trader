"""Anti-look-ahead tests for the StdDev-channel strategy (rule 03 section 3).

(1) evaluate at t is unchanged when bars after t (H1) or H4 bars not closed at t's decision time are
    appended / modified -- even when the future H4 bars are passed straight into the context;
(2) scan() over the whole history returns, at every bar t, what evaluate() returns on the prefix <= t
    (3,000-bar FX-like random walk with weekend gaps);
(3) an H4 bar still forming at the decision time is never used (planted extreme bar), while the same
    extreme values in the last CLOSED H4 bar do change the decision (control).

Float note: ``rolling_regression_channel`` computes the slope with a BLAS matrix-vector product whose
rounding depends on the matrix shape, so channel values computed on a prefix and on the full history
may differ by ~1 ulp. Decisions, setups, prices, SL/TP and reason text are compared exactly; the
channel-derived floats in ``extra`` are compared to 1e-12 relative.
"""

from __future__ import annotations

import math
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from alpha_engine.indicators.atr import wilder_atr
from alpha_engine.indicators.mtf import project_channel_to_h1
from alpha_engine.indicators.regression_channel import rolling_regression_channel
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy, resolve_params
from alpha_engine.strategies.stddev_channel.setups import compute_channel_bars
from alpha_engine.strategy.base import (
    H4_DURATION,
    LookAheadError,
    StrategyContext,
    evaluate_checked,
    slice_closed_bars,
)
from alpha_engine.strategy.signal import Setup, SignalCandidate
from fixtures.channel_scenarios import DEFAULT_T, SYMBOL, build, random_walk

STRATEGY = StdDevChannelStrategy()
ACCOUNT = AccountSettings()
N_BARS = 3000


@pytest.fixture(scope="module")
def walk() -> tuple[pd.DataFrame, pd.DataFrame]:
    return random_walk(N_BARS, seed=7)


@pytest.fixture(scope="module")
def walk_scan(walk) -> dict:
    h1, h4 = walk
    return {c.confirmation_bar_open_utc: c for c in STRATEGY.scan(h1, h4, {}, ACCOUNT, symbol=SYMBOL)}


def closed_counts(h1: pd.DataFrame, h4: pd.DataFrame) -> np.ndarray:
    """Number of H4 bars closed at each H1 decision time (open + 4h <= open_t + 1h)."""
    return np.searchsorted((h4["time"] + H4_DURATION).to_numpy(),
                           (h1["time"] + timedelta(hours=1)).to_numpy(), side="right")


def ctx_at(h1: pd.DataFrame, h4: pd.DataFrame, t: int, **kw) -> StrategyContext:
    return StrategyContext(symbol=SYMBOL, h1=h1.iloc[: t + 1], h4=h4, account=ACCOUNT, params={}, **kw)


def assert_same(a: SignalCandidate | None, b: SignalCandidate | None) -> None:
    if a is None or b is None:
        assert a is b, (a, b)
        return
    da, db = a.model_dump(), b.model_dump()
    ea, eb = da.pop("extra"), db.pop("extra")
    assert da == db
    assert ea.keys() == eb.keys()
    for key, va in ea.items():
        vb = eb[key]
        if isinstance(va, float):
            assert math.isclose(va, vb, rel_tol=1e-12, abs_tol=1e-9), (key, va, vb)
        else:
            assert va == vb, (key, va, vb)


def _randomize(frame: pd.DataFrame, rows: np.ndarray, seed: int) -> pd.DataFrame:
    out = frame.copy()
    rng = np.random.default_rng(seed)
    base = rng.uniform(1500.0, 2500.0, len(rows))
    body = rng.normal(0.0, 5.0, len(rows))
    o, c = base, base + body
    out.loc[rows, "open"] = o
    out.loc[rows, "close"] = c
    out.loc[rows, "high"] = np.maximum(o, c) + rng.exponential(3.0, len(rows))
    out.loc[rows, "low"] = np.minimum(o, c) - rng.exponential(3.0, len(rows))
    return out


# ------------------------------------------------------------------------------ building blocks
def test_closed_counts_match_slice_closed_bars(walk) -> None:
    h1, h4 = walk
    k = closed_counts(h1, h4)
    for t in (0, 3, 4, 500, 1234, N_BARS - 1):
        decision = h1["time"].iloc[t] + timedelta(hours=1)
        assert len(slice_closed_bars(h4, H4_DURATION, decision)) == k[t]


@pytest.mark.parametrize("mode", ["bar_count", "calendar"])
def test_projection_is_bitwise_equal_to_project_channel_to_h1(walk, mode: str) -> None:
    """compute_channel_bars assembles the projection from mtf's building blocks: prove it is identical."""
    h1, h4 = walk
    h4c = h4.iloc[: closed_counts(h1, h4)[-1]].reset_index(drop=True)  # H4 closed by the last decision
    p, _ = resolve_params({"projection_mode": mode})
    cb = compute_channel_bars(h1, h4c, p, pattern_from=None)
    channel = rolling_regression_channel(h4c["close"], p.n, p.k, p.sigma_ddof)
    atr4 = wilder_atr(h4c["high"], h4c["low"], h4c["close"], p.atr_period)
    ref = project_channel_to_h1(channel, h4c["time"], h1, p.n, mode, h4_atr=atr4.to_numpy(), flat_mult=p.flat_mult)
    for name in ("mid", "upper", "lower", "slope", "sigma", "bars_ahead", "atr_h4"):
        np.testing.assert_array_equal(getattr(cb, name), ref[name].to_numpy(), err_msg=name)
    np.testing.assert_array_equal(cb.is_flat, ref["is_flat"].to_numpy(dtype=bool))
    assert np.isfinite(cb.mid).sum() > 2000


def test_tail_computation_matches_full_history(walk) -> None:
    """evaluate's tail-only computation (start > 0) gives the same values for the bars it reads."""
    h1, h4 = walk
    p, _ = resolve_params({})
    full = compute_channel_bars(h1, h4, p, pattern_from=None)
    for t in (650, 1111, 2222, N_BARS - 1):
        h4t = h4.iloc[: closed_counts(h1, h4)[t]]
        start = t - p.history_bars + 1
        tail = compute_channel_bars(h1.iloc[: t + 1], h4t, p, start=start, pattern_from=None)
        sl = slice(start, t + 1)
        np.testing.assert_array_equal(tail.bars_ahead[sl], full.bars_ahead[sl])
        np.testing.assert_array_equal(tail.h4_open[sl], full.h4_open[sl])
        np.testing.assert_array_equal(tail.atr_h1[sl], full.atr_h1[sl])
        np.testing.assert_array_equal(tail.atr_h4[sl], full.atr_h4[sl])
        for name in ("mid", "upper", "lower", "slope", "sigma"):
            np.testing.assert_allclose(getattr(tail, name)[sl], getattr(full, name)[sl], rtol=1e-12, atol=0)
        assert np.isnan(tail.mid[:start]).all()


# ------------------------------------------------------------------------------ (2) scan == evaluate
def test_scan_equals_evaluate_at_every_bar(walk, walk_scan) -> None:
    h1, h4 = walk
    k = closed_counts(h1, h4)
    found = 0
    for t in range(N_BARS):
        got = STRATEGY.evaluate(ctx_at(h1, h4.iloc[: k[t]], t))
        expected = walk_scan.get(h1["time"].iloc[t].to_pydatetime())
        assert_same(got, expected)
        found += got is not None
    assert found == len(walk_scan)
    # The dataset exercises every setup and both directions.
    setups = {c.setup for c in walk_scan.values()}
    assert setups == set(Setup)
    assert {c.direction for c in walk_scan.values()} == {"buy", "sell"}
    assert found >= 100


def test_scan_ignores_open_trade_state(walk_scan) -> None:
    """scan reports candidates on consecutive bars; the backtester enforces 'max one open trade'."""
    times = sorted(walk_scan)
    assert any(b - a == timedelta(hours=1) for a, b in zip(times, times[1:]))


# ------------------------------------------------------------------------------ (1) future bars
def test_future_bars_do_not_change_past_decisions(walk, walk_scan) -> None:
    h1, h4 = walk
    cutoff = 2000
    decision = h1["time"].iloc[cutoff] + timedelta(hours=1)
    h1_future = np.arange(cutoff + 1, N_BARS)
    h4_future = np.flatnonzero((h4["time"] + H4_DURATION > decision).to_numpy())
    assert h4_future[0] == closed_counts(h1, h4)[cutoff]  # includes the H4 bar forming at `cutoff`
    h1m = _randomize(h1, h1_future, seed=11)
    h4m = _randomize(h4, h4_future, seed=12)

    # scan over the modified history: identical for every decision up to the cutoff.
    modified = {c.confirmation_bar_open_utc: c for c in STRATEGY.scan(h1m, h4m, {}, ACCOUNT, symbol=SYMBOL)}
    cut_time = h1["time"].iloc[cutoff].to_pydatetime()
    before = sorted(t for t in walk_scan if t <= cut_time)
    assert before == sorted(t for t in modified if t <= cut_time) and len(before) > 50
    for t in before:
        assert_same(modified[t], walk_scan[t])
    assert any(t > cut_time and t not in walk_scan for t in modified)  # the future really changed

    # evaluate with the FULL modified H4 history in the context (future H4 bars present).
    k = closed_counts(h1, h4)
    candidates = [i for i in range(cutoff - 300, cutoff + 1) if h1["time"].iloc[i].to_pydatetime() in walk_scan]
    for t in candidates[:25] + list(range(cutoff - 5, cutoff + 1)):
        expected = STRATEGY.evaluate(ctx_at(h1, h4.iloc[: k[t]], t))
        assert_same(STRATEGY.evaluate(ctx_at(h1m, h4m, t)), expected)


def test_scenario_decision_unchanged_by_appended_bars() -> None:
    sc = build("up", "buy", "breakout_outer")
    before = evaluate_checked(STRATEGY, sc.ctx())
    assert before is not None
    for i in range(DEFAULT_T + 1, len(sc.h1)):
        sc.set_bar(i, 9000.0, 9001.0, 8999.0, 9000.5)  # absurd future H1 bars
    sc.h4.loc[sc.h4.index[-3:], ["open", "high", "low", "close"]] = [9000.0, 9001.0, 8999.0, 9000.5]
    assert evaluate_checked(STRATEGY, sc.ctx()) == before
    assert STRATEGY.evaluate(sc.ctx(h4=sc.h4)) == before  # future H4 rows handed in directly


# ------------------------------------------------------------------------------ (3) forming H4 bar
def _plant(h4: pd.DataFrame, row: int) -> pd.DataFrame:
    out = h4.copy()
    out.loc[row, ["open", "high", "low", "close"]] = [float(h4["open"].iloc[row]), 6000.0, 100.0, 5000.0]
    return out


def test_forming_h4_bar_is_never_used(walk, walk_scan) -> None:
    h1, h4 = walk
    k = closed_counts(h1, h4)
    checked = 0
    for when, cand in sorted(walk_scan.items()):
        t = int(np.flatnonzero(h1["time"] == pd.Timestamp(when))[0])
        forming = int(k[t])
        if forming >= len(h4) or not (h4["time"].iloc[forming] <= h1["time"].iloc[t]):
            continue  # t is the last hour of its H4 bar: nothing is forming at D
        planted = _plant(h4, forming)
        ctx = ctx_at(h1, planted.iloc[: forming + 1], t)
        with pytest.raises(LookAheadError):
            evaluate_checked(STRATEGY, ctx)
        assert_same(STRATEGY.evaluate(ctx), cand)  # dropped defensively by the strategy
        scanned = {c.confirmation_bar_open_utc: c for c in STRATEGY.scan(h1.iloc[: t + 1], planted, {}, ACCOUNT,
                                                                          symbol=SYMBOL)}
        assert_same(scanned.get(when), cand)
        # Control: the same extreme values in the last CLOSED H4 bar do change the decision.
        control = _plant(h4, forming - 1)
        assert STRATEGY.evaluate(ctx_at(h1, control.iloc[:forming], t)) != cand
        checked += 1
        if checked == 8:
            break
    assert checked == 8


def test_forming_h4_bar_in_scenario() -> None:
    sc = build("down", "sell", "bounce_outer")
    before = evaluate_checked(STRATEGY, sc.ctx())
    assert before is not None
    decision = sc.h1["time"].iloc[DEFAULT_T] + timedelta(hours=1)
    forming = int((sc.h4["time"] + H4_DURATION <= decision).sum())
    assert sc.h4["time"].iloc[forming] <= sc.h1["time"].iloc[DEFAULT_T]  # bar t lies inside a forming H4 bar
    planted = _plant(sc.h4, forming)
    assert STRATEGY.evaluate(sc.ctx(h4=planted.iloc[: forming + 1])) == before
    with pytest.raises(LookAheadError):
        evaluate_checked(STRATEGY, sc.ctx(h4=planted.iloc[: forming + 1]))
    control = _plant(sc.h4, forming - 1)
    assert STRATEGY.evaluate(sc.ctx(h4=control.iloc[:forming])) != before
