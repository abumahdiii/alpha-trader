"""No look-ahead in the backtest (rule 03 section 3).

(1) Equivalence: the scan-driven simulator gives EXACTLY the trades of an independent, deliberately naive
    reference simulator that, at every H1 bar, builds a context from ``h1[:t+1]`` + the H4 bars closed at
    the decision time and calls ``evaluate_checked`` with ``has_open_trade`` from its own state.
(2) Perturbation: randomising every bar after a cutoff leaves every trade that exited up to the cutoff,
    every skipped candidate decided before it and every equity point up to it unchanged.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from alpha_engine.backtest.history import HistoryData, build_bar_arrays, scan_full_history
from alpha_engine.backtest.models import CostModel, RunConfig, SkipReason, Window
from alpha_engine.backtest.runner import run_backtest
from alpha_engine.backtest.simulator import scan_provider, simulate_window
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.risk.sizing import size_position
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy, resolve_params
from alpha_engine.strategy.base import H4_DURATION, StrategyContext, evaluate_checked
from alpha_engine.strategy.params import params_hash
from fixtures.channel_scenarios import SYMBOL, random_walk

STRATEGY = StdDevChannelStrategy()
SPEC = SymbolSpec(name=SYMBOL, digits=2, point=0.01, trade_contract_size=100.0, trade_tick_value=1.0,
                  trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0, currency_profit="USD",
                  currency_base="XAU")
ACCOUNT = AccountSettings(balance=10_000.0, risk_pct=1.0, leverage=100, rr=2.0)
COSTS = CostModel(commission_per_lot_per_side=3.5)
N_BARS = 3000


def with_spread(h1: pd.DataFrame, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    spread = rng.integers(10, 60, len(h1))
    spread[rng.random(len(h1)) < 0.05] = 0  # zero spreads -> causal fill
    out = h1.copy()
    out["spread"] = spread
    return out


@pytest.fixture(scope="module")
def walk() -> tuple[pd.DataFrame, pd.DataFrame]:
    h1, h4 = random_walk(N_BARS, seed=7)
    return with_spread(h1, 3), h4


def config(h1: pd.DataFrame, start: int, end: int | None = None) -> RunConfig:
    _, clean = resolve_params({})
    end_ts = h1["time"].iloc[end] if end is not None else h1["time"].iloc[-1] + timedelta(hours=1)
    return RunConfig(symbol=SYMBOL, mode="manual", start=h1["time"].iloc[start].to_pydatetime(),
                     end=end_ts.to_pydatetime(), cost_model=COSTS, account=ACCOUNT, strategy_name=STRATEGY.name,
                     strategy_version=STRATEGY.version, params=clean, params_hash=params_hash(clean))


# ---------------------------------------------------------------------------------------------- reference
def reference_trades(h1: pd.DataFrame, h4: pd.DataFrame, i0: int, i1: int) -> tuple[list[dict], list[tuple]]:
    """Naive per-bar simulator: evaluate_checked on the closed prefix at every bar (no scan, no shared code
    with simulator.py except resolve_levels and size_position)."""
    o, h, lo, c = (h1[k].to_numpy(dtype=float).tolist() for k in ("open", "high", "low", "close"))
    raw = h1["spread"].to_numpy().tolist()
    spr = []
    last = 0
    for s in raw:  # causal zero fill
        last = s if s > 0 else last
        spr.append(last * SPEC.point)
    times = h1["time"].tolist()
    h4_close = pd.DatetimeIndex(h4["time"] + H4_DURATION).as_unit("ns").asi8
    vpu = SPEC.trade_tick_value / SPEC.trade_tick_size
    balance = ACCOUNT.balance
    trades: list[dict] = []
    skipped: list[tuple] = []
    trade = None
    pending = None
    for i in range(i0, i1):
        if pending is not None:
            cand, pending = pending, None
            buy = cand.direction == "buy"
            fill = o[i] + spr[i] if buy else o[i]
            if (buy and o[i] <= cand.stop_loss) or (not buy and o[i] + spr[i] >= cand.stop_loss):
                skipped.append((cand.confirmation_bar_open_utc, "gap_through_stop"))
            else:
                sl, tp = cand.resolve_levels(fill)
                size = size_position(balance=balance, risk_pct=ACCOUNT.risk_pct, entry=fill, stop_loss=sl,
                                     tick_value=SPEC.trade_tick_value, tick_size=SPEC.trade_tick_size,
                                     volume_min=SPEC.volume_min, volume_step=SPEC.volume_step,
                                     volume_max=SPEC.volume_max, contract_size=SPEC.trade_contract_size,
                                     leverage=ACCOUNT.leverage)
                if not size.accepted:
                    skipped.append((cand.confirmation_bar_open_utc, "sizing_rejected"))
                else:
                    trade = dict(cand=cand, entry_time=times[i], entry=fill, sl=sl, tp=tp, vol=size.volume,
                                 dir=1.0 if buy else -1.0)
        if trade is not None:
            sl, tp = trade["sl"], trade["tp"]
            exit_ = None
            if trade["dir"] > 0:
                if o[i] <= sl:
                    exit_ = ("sl_gap", o[i])
                elif o[i] >= tp:
                    exit_ = ("tp_gap", o[i])
                elif lo[i] <= sl:
                    exit_ = ("sl", sl)
                elif h[i] >= tp:
                    exit_ = ("tp", tp)
            else:
                a = o[i] + spr[i]
                if a >= sl:
                    exit_ = ("sl_gap", a)
                elif a <= tp:
                    exit_ = ("tp_gap", a)
                elif h[i] + spr[i] >= sl:
                    exit_ = ("sl", sl)
                elif lo[i] + spr[i] <= tp:
                    exit_ = ("tp", tp)
            if exit_ is None and i == i1 - 1:
                exit_ = ("end_of_period", c[i] if trade["dir"] > 0 else c[i] + spr[i])
            if exit_ is not None:
                net = (exit_[1] - trade["entry"]) * trade["dir"] * trade["vol"] * vpu - 3.5 * trade["vol"] * 2
                balance += net
                trades.append(dict(confirmation=trade["cand"].confirmation_bar_open_utc,
                                   entry_time=trade["entry_time"], exit_bar=times[i], reason=exit_[0],
                                   entry=trade["entry"], exit=exit_[1], sl=sl, tp=tp, volume=trade["vol"],
                                   net=net, balance=balance))
                trade = None
        decision = times[i] + timedelta(hours=1)
        k = int(np.searchsorted(h4_close, decision.as_unit("ns").value, side="right"))
        ctx = StrategyContext(symbol=SYMBOL, h1=h1.iloc[: i + 1], h4=h4.iloc[:k], account=ACCOUNT, params={},
                              has_open_trade=trade is not None)
        cand = evaluate_checked(STRATEGY, ctx)
        if cand is not None:
            if i + 1 >= i1:
                skipped.append((cand.confirmation_bar_open_utc, "entry_outside_window"))
            else:
                pending = cand  # random_walk has no missing gaps (weekends only)
    return trades, skipped


def test_scan_simulator_equals_per_bar_evaluate_reference(walk) -> None:
    h1, h4 = walk
    cfg = config(h1, 1000)
    history = HistoryData(symbol=SYMBOL, h1=h1, h4=h4, spec=SPEC)
    result = run_backtest(cfg, history)
    win = result.windows[0]
    i0 = int(np.searchsorted(h1["time"], pd.Timestamp(cfg.start)))
    ref, ref_skipped = reference_trades(h1, h4, i0, len(h1))

    got = [dict(confirmation=t.confirmation_bar_time, entry_time=t.entry_time, exit_bar=t.exit_bar_time,
                reason=t.exit_reason.value, entry=t.entry, exit=t.exit_price, sl=t.stop_loss, tp=t.take_profit,
                volume=t.volume, net=t.net_pnl, balance=t.balance_after) for t in win.trades]
    assert len(got) >= 20
    assert got == ref  # exact, float for float
    assert [(s.confirmation_bar_time, s.reason.value) for s in win.skipped
            if s.reason is not SkipReason.POSITION_OPEN] == ref_skipped
    # the open-trade gating really mattered: scan produced candidates while a trade was open
    assert sum(s.reason is SkipReason.POSITION_OPEN for s in win.skipped) >= 5
    assert {t.direction for t in win.trades} == {"buy", "sell"}
    assert {t.exit_reason.value for t in win.trades} >= {"sl", "tp"}  # gap exits: micro tests (continuous walk)


# ---------------------------------------------------------------------------------------------- perturbation
def _randomize(frame: pd.DataFrame, rows: np.ndarray, seed: int) -> pd.DataFrame:
    out = frame.copy()
    rng = np.random.default_rng(seed)
    base = rng.uniform(1500.0, 2500.0, len(rows))
    body = rng.normal(0.0, 5.0, len(rows))
    out.loc[rows, "open"] = base
    out.loc[rows, "close"] = base + body
    out.loc[rows, "high"] = np.maximum(base, base + body) + rng.exponential(3.0, len(rows))
    out.loc[rows, "low"] = np.minimum(base, base + body) - rng.exponential(3.0, len(rows))
    if "spread" in out.columns:
        out.loc[rows, "spread"] = rng.integers(0, 500, len(rows))
    return out


@pytest.mark.parametrize("cutoff", [1500, 2400])
def test_future_bars_do_not_change_the_past(walk, cutoff: int) -> None:
    h1, h4 = walk
    cfg = config(h1, 1000)
    base = run_backtest(cfg, HistoryData(symbol=SYMBOL, h1=h1, h4=h4, spec=SPEC)).windows[0]
    decision = h1["time"].iloc[cutoff] + timedelta(hours=1)
    h1m = _randomize(h1, np.arange(cutoff + 1, len(h1)), seed=11)
    h4m = _randomize(h4, np.flatnonzero((h4["time"] + H4_DURATION > decision).to_numpy()), seed=12)
    pert = run_backtest(cfg, HistoryData(symbol=SYMBOL, h1=h1m, h4=h4m, spec=SPEC)).windows[0]

    cut_open = h1["time"].iloc[cutoff].to_pydatetime()
    before = [t for t in base.trades if t.exit_bar_time <= cut_open]
    assert len(before) >= 5
    assert [t for t in pert.trades if t.exit_bar_time <= cut_open] == before
    skip_before = [s for s in base.skipped if s.confirmation_bar_time < cut_open]
    assert [s for s in pert.skipped if s.confirmation_bar_time < cut_open] == skip_before
    eq_before = [p for p in base.equity if p.time <= decision.to_pydatetime()]
    assert [p for p in pert.equity if p.time <= decision.to_pydatetime()] == eq_before
    assert pert.trades != base.trades  # the future really changed


def test_simulation_on_a_window_uses_the_full_history_scan(walk) -> None:
    """A later window of the same scan gives the same trades as the corresponding part of a long window
    started flat at a moment with no open trade (the scan is never re-run on a slice)."""
    h1, h4 = walk
    scan = scan_full_history(STRATEGY, h1, h4, {}, ACCOUNT, symbol=SYMBOL)
    bars = build_bar_arrays(h1, SPEC)
    cfg = config(h1, 1000)
    long_run = run_backtest(cfg, HistoryData(symbol=SYMBOL, h1=h1, h4=h4, spec=SPEC), scan, bars=bars).windows[0]
    # first moment after the 5th trade exits with no trade open and no pending entry
    t5 = long_run.trades[4]
    later = long_run.trades[5:]
    start = t5.exit_bar_time + timedelta(hours=1)
    assert later[0].confirmation_bar_time >= start
    part = simulate_window(bars, Window(index=0, start=start, end=cfg.end), scan_provider(scan.by_bar), spec=SPEC,
                           account=AccountSettings(balance=t5.balance_after, risk_pct=1.0, leverage=100, rr=2.0),
                           cost_model=COSTS)
    assert [(t.entry_time, t.exit_time, t.volume, t.net_pnl) for t in part.trades] == \
        [(t.entry_time, t.exit_time, t.volume, t.net_pnl) for t in later]
