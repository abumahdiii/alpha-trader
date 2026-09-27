"""Golden snapshots of backtest results (regression net for refactors of backtest/simulator.py).

Every case runs the backtest on deterministic fixture data (never MT5, never the real ``data/``) and
compares the FULL result (``model_dump(mode="json")``, floats bit for bit) with a snapshot stored in
``tests/fixtures/golden/<case>.json``. The snapshots were written BEFORE the phase-5 extraction of the
simulator helpers, so a behaviour change anywhere in the fill / exit / pnl / skip logic fails here.

Cases (together they cover every exit and skip reason of ``backtest/models.py`` except ``invalid_levels``,
which needs an absurd price scale and is checked in tests/test_setup_outcomes.py):

* ``seed_*``: the synthetic seed cache (weekends, holidays, session breaks, planted missing bars, a
  high-spread day, a price gap) through ``run_backtest``: manual full period with commission, seeded
  random windows, a tiny balance (volume below the minimum) and leverage 5 (margin rejections), each
  mixing traded and rejected candidates;
* ``walk_*``: a 3,000-bar random walk with no broker spread before bar 1600 (auto fallback spread) and a
  user fallback, manual + random;
* ``micro``: hand-built frames through ``simulate_window``: TP, SL, SL-before-TP on one bar, sl_gap /
  tp_gap (long and short, short on the ask), gap through the stop at entry (bid for buys, ask for sells),
  a candidate while a trade is open, entry outside the window, a missing gap, a weekend hold,
  end_of_period (long at the bid close, short at the ask close), sizing rejection, balance depleted.

Regenerate (only when a behaviour change is INTENDED and reviewed): ``ALPHA_REGEN_GOLDEN=1 pytest
tests/test_backtest_golden.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from alpha_engine.backtest.history import HistoryData, build_bar_arrays, load_history, scan_full_history
from alpha_engine.backtest.models import CostModel, RunConfig, Window
from alpha_engine.backtest.periods import earliest_start
from alpha_engine.backtest.runner import run_backtest
from alpha_engine.backtest.simulator import scan_provider, simulate_window
from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy, resolve_params
from alpha_engine.strategy.params import params_hash
from alpha_engine.strategy.signal import Setup, SignalCandidate
from fixtures.channel_scenarios import random_walk
from fixtures.seed_cache import seed_cache

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"
REGEN = os.environ.get("ALPHA_REGEN_GOLDEN", "").lower() in ("1", "true", "yes")
STRATEGY = StdDevChannelStrategy()
_, CLEAN = resolve_params({})
HASH = params_hash(CLEAN)
GOLD_SPEC = SymbolSpec(name="XAUUSD.x", digits=2, point=0.01, trade_contract_size=100.0, trade_tick_value=1.0,
                       trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0,
                       currency_profit="USD", currency_base="XAU")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _normalise(data: Any) -> Any:
    """JSON round trip with sorted keys; wall-clock fields of the seed cache removed; every per-bar equity
    list replaced by its count, first/last point and the SHA-256 of its exact JSON (keeps the snapshots
    small while any change of any equity float still fails)."""
    data = json.loads(json.dumps(data, sort_keys=True))

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "equity" and isinstance(value, list):
                    out[key] = {"count": len(value), "first": value[0] if value else None,
                                "last": value[-1] if value else None, "sha256": _digest(value)}
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    data = walk(data)
    if isinstance(data, dict) and "fingerprint" in data:
        for key in ("h1_fetched_at_utc", "h4_fetched_at_utc"):
            data["fingerprint"].pop(key, None)
    return data


def check_golden(name: str, data: Any) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    data = _normalise(data)
    if REGEN:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, sort_keys=True, indent=0, separators=(",", ":"), ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return
    assert path.exists(), f"golden snapshot {path.name} missing (ALPHA_REGEN_GOLDEN=1 writes it)"
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert data == expected


def _config(symbol: str, account: AccountSettings, **kw: Any) -> RunConfig:
    base = dict(symbol=symbol, account=account, strategy_name=STRATEGY.name, strategy_version=STRATEGY.version,
                params=CLEAN, params_hash=HASH)
    base.update(kw)
    return RunConfig(**base)


def _manual_full(history: HistoryData, scan, account: AccountSettings, **kw: Any) -> RunConfig:
    times = pd.DatetimeIndex(history.h1["time"]).as_unit("ns").asi8
    earliest = earliest_start(times, pd.DatetimeIndex(history.h4["time"]).as_unit("ns").asi8,
                              scan.first_valid_index, 14)
    end = pd.Timestamp(int(times[-1]), tz="UTC") + pd.Timedelta(hours=1)
    return _config(history.symbol, account, mode="manual", start=earliest.to_pydatetime(), end=end.to_pydatetime(),
                   **kw)


def _reasons(result) -> Counter:
    counts: Counter = Counter()
    for w in result.windows:
        counts.update(t.exit_reason.value for t in w.trades)
        counts.update(s.reason.value for s in w.skipped)
    return counts


# ---------------------------------------------------------------------------------------------- seed cache
@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> OhlcvCache:
    root = tmp_path_factory.mktemp("golden_seed")
    # 36 weeks (the default seed has 10): enough bars after the channel + ATR warm-up for several windows
    seed_cache(root, repo_data_dir=root / "not_data", start="2024-09-02", end="2025-05-12")
    return OhlcvCache(root)


SEED_CASES = {
    "manual_commission": (AccountSettings(balance=10_000.0, risk_pct=1.0, leverage=100, rr=2.0),
                          dict(cost_model=CostModel(commission_per_lot_per_side=3.5)), "manual"),
    "random": (AccountSettings(balance=2500.0, risk_pct=1.0, leverage=100, rr=2.0),
               dict(mode="random", windows_count=4, window_months=1, seed=7), "random"),
    "tiny_balance": (AccountSettings(balance=400.0, risk_pct=0.5, leverage=100, rr=1.5), {}, "manual"),
    "low_leverage": (AccountSettings(balance=2000.0, risk_pct=1.0, leverage=5, rr=3.0), {}, "manual"),
}


@pytest.mark.parametrize("symbol", ["XAUUSD.x", "BRNUSD.x"])
@pytest.mark.parametrize("case", sorted(SEED_CASES))
def test_seed_cache_golden(seeded: OhlcvCache, symbol: str, case: str) -> None:
    account, extra, mode = SEED_CASES[case]
    history = load_history(seeded, symbol)
    scan = scan_full_history(STRATEGY, history.h1, history.h4, CLEAN, account, symbol=symbol)
    config = _manual_full(history, scan, account, **extra) if mode == "manual" else _config(symbol, account, **extra)
    result = run_backtest(config, history, scan)
    check_golden(f"seed_{symbol.split('.')[0].lower()}_{case}", result.model_dump(mode="json"))


def test_seed_cache_golden_covers_rejections(seeded: OhlcvCache) -> None:
    """The seed cases really exercise sizing / margin rejections and gaps (else the net has holes)."""
    counts: Counter = Counter()
    for symbol in ("XAUUSD.x", "BRNUSD.x"):
        history = load_history(seeded, symbol)
        for case in ("tiny_balance", "low_leverage", "manual_commission"):
            account, extra, _ = SEED_CASES[case]
            scan = scan_full_history(STRATEGY, history.h1, history.h4, CLEAN, account, symbol=symbol)
            counts += _reasons(run_backtest(_manual_full(history, scan, account, **extra), history, scan))
    assert counts["sizing_rejected"] > 0 and counts["position_open"] > 0
    assert counts["tp"] > 0 and counts["sl"] > 0


# ---------------------------------------------------------------------------------------------- random walk
@pytest.fixture(scope="module")
def walk() -> HistoryData:
    h1, h4 = random_walk(3000, seed=21)
    h1 = h1.copy()
    spread = np.random.default_rng(5).integers(15, 40, len(h1))
    spread[:1600] = 0  # broker spread only from bar 1600 on (like the real cache) -> fallback spread
    spread[np.random.default_rng(6).random(len(h1)) < 0.03] = 0  # causal zero fill
    h1["spread"] = spread
    return HistoryData(symbol="XAUUSD.x", h1=h1, h4=h4, spec=GOLD_SPEC)


WALK_ACCOUNT = AccountSettings(balance=10_000.0, risk_pct=1.0, leverage=100, rr=2.0)


@pytest.mark.parametrize("case", ["auto", "user40", "zero", "random"])
def test_random_walk_golden(walk: HistoryData, case: str) -> None:
    scan = scan_full_history(STRATEGY, walk.h1, walk.h4, CLEAN, WALK_ACCOUNT, symbol=walk.symbol)
    if case == "random":
        config = _config(walk.symbol, WALK_ACCOUNT, mode="random", windows_count=6, window_months=1, seed=42,
                         cost_model=CostModel(commission_per_lot_per_side=2.0))
    else:
        fb = {"auto": None, "user40": 40, "zero": 0}[case]
        config = _manual_full(walk, scan, WALK_ACCOUNT,
                              cost_model=CostModel(commission_per_lot_per_side=3.5, fallback_spread_points=fb))
    result = run_backtest(config, walk, scan)
    assert result.total_trades > 10
    check_golden(f"walk_{case}", result.model_dump(mode="json"))


# ---------------------------------------------------------------------------------------------- micro scenarios
T0 = pd.Timestamp("2026-01-06 00:00", tz="UTC")  # Tuesday


def _frame(rows: list[tuple], times: list[pd.Timestamp] | None = None) -> pd.DataFrame:
    times = times or [T0 + pd.Timedelta(hours=i) for i in range(len(rows))]
    return pd.DataFrame({
        "time": pd.DatetimeIndex(times), "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows], "tick_volume": 1,
        "spread": [r[4] for r in rows], "real_volume": 0,
    })


def _cand(h1: pd.DataFrame, i: int, direction: str, sl: float, rr: float = 2.0) -> SignalCandidate:
    conf = h1["time"].iloc[i].to_pydatetime()
    return SignalCandidate(
        strategy_name="stddev_channel", strategy_version=1, params_hash="a" * 64, symbol=GOLD_SPEC.name,
        direction=direction, setup=Setup.BOUNCE_LOWER if direction == "buy" else Setup.BOUNCE_UPPER,
        line="lower" if direction == "buy" else "upper", decision_time_utc=conf + timedelta(hours=1),
        confirmation_bar_open_utc=conf, reference_price=float(h1["close"].iloc[i]), stop_loss=sl, rr=rr,
        pattern="pin_bar", reason_fa="آزمایشی",
    )


def _run(h1: pd.DataFrame, cands: dict[int, tuple], *, balance: float = 10_000.0, risk: float = 1.0,
         leverage: int = 100, commission: float = 0.0, window: tuple[int, int] | None = None,
         fallback: int | None = None) -> dict:
    bars = build_bar_arrays(h1, GOLD_SPEC, fallback)
    lo, hi = window or (0, len(h1))
    start = h1["time"].iloc[lo].to_pydatetime()
    end = (h1["time"].iloc[hi - 1] + pd.Timedelta(hours=1)).to_pydatetime() if hi == len(h1) else \
        h1["time"].iloc[hi].to_pydatetime()
    by_bar = {i: _cand(h1, i, *args) for i, args in cands.items()}
    result = simulate_window(
        bars, Window(index=0, start=start, end=end), scan_provider(by_bar), spec=GOLD_SPEC,
        account=AccountSettings(balance=balance, risk_pct=risk, leverage=leverage, rr=2.0),
        cost_model=CostModel(commission_per_lot_per_side=commission),
    )
    return result.model_dump(mode="json")


def _weekend() -> pd.DataFrame:
    fri = pd.Timestamp("2026-01-09 19:00", tz="UTC")
    sun = pd.Timestamp("2026-01-11 23:00", tz="UTC")
    times = [fri + pd.Timedelta(hours=h) for h in range(3)] + [sun + pd.Timedelta(hours=h) for h in range(3)]
    return _frame([(2001, 2002, 1999, 2000.5, 25), (2000.0, 2001.0, 1999.0, 2000.0, 25),
                   (2000.0, 2001.0, 1999.0, 2000.0, 25), (2001.0, 2002.0, 2000.0, 2001.0, 25),
                   (2001.0, 2011.0, 2000.5, 2010.0, 25), (2010.0, 2011.0, 2009.0, 2010.0, 25)], times=times)


def micro_results() -> dict[str, dict]:
    conf_buy = (2001, 2002, 1999, 2000.5, 25)
    conf_sell = (1999.00, 2001.00, 1998.50, 2000.50, 30)
    flat_b = (2000.00, 2001.00, 1999.00, 2000.00, 25)
    flat_s = (2000.00, 2001.00, 1999.00, 2000.20, 30)
    out: dict[str, dict] = {}
    out["long_tp"] = _run(_frame([conf_buy, flat_b, (2002.00, 2011.00, 2001.50, 2010.90, 25),
                                  (2010.00, 2011.00, 2009.00, 2010.50, 25)]), {0: ("buy", 1995.00)}, commission=3.5)
    out["long_sl"] = _run(_frame([conf_buy, (2000.00, 2001.00, 1998.00, 1999.00, 25),
                                  (1999.00, 1999.50, 1994.00, 1996.00, 25)]), {0: ("buy", 1995.00)})
    out["long_sl_first"] = _run(_frame([conf_buy, flat_b, (2000.00, 2012.00, 1994.00, 2005.00, 25)]),
                                {0: ("buy", 1995.00)})
    out["long_tp_entry_bar"] = _run(_frame([conf_buy, (2000.00, 2011.00, 1999.00, 2010.00, 25)]), {0: ("buy", 1995.00)})
    out["long_gap_entry"] = _run(_frame([conf_buy, (1994.00, 1996.00, 1993.00, 1995.50, 25),
                                         (1995, 1996, 1994, 1995, 25)]), {0: ("buy", 1995.00)})
    out["long_sl_gap"] = _run(_frame([conf_buy, flat_b, (1990.00, 1991.00, 1989.00, 1990.50, 25)]),
                              {0: ("buy", 1995.00)})
    out["long_tp_gap"] = _run(_frame([conf_buy, flat_b, (2015.00, 2016.00, 2014.00, 2015.50, 25)]),
                              {0: ("buy", 1995.00)})
    out["short_sl_ask"] = _run(_frame([conf_sell, flat_s, (2000.20, 2004.80, 1999.50, 2003.00, 30)]),
                               {0: ("sell", 2005.00)}, commission=3.5)
    out["short_tp_ask"] = _run(_frame([conf_sell, flat_s, (1995.00, 1996.00, 1989.80, 1992.00, 30),
                                       (1992.00, 1993.00, 1989.60, 1991.00, 30)]), {0: ("sell", 2005.00)})
    out["short_gap_entry_ask"] = _run(_frame([conf_sell, (2004.90, 2006.0, 2004.0, 2005.5, 30)]),
                                      {0: ("sell", 2005.00)})
    out["short_sl_gap"] = _run(_frame([conf_sell, flat_s, (2006.00, 2007.00, 2005.50, 2006.50, 30)]),
                               {0: ("sell", 2005.00)})
    out["short_tp_gap"] = _run(_frame([conf_sell, flat_s, (1985.00, 1986.00, 1984.00, 1985.50, 30)]),
                               {0: ("sell", 2005.00)})
    out["long_end_of_period"] = _run(_frame([conf_buy, flat_b, (2000.00, 2003.00, 1999.00, 2002.00, 40)]),
                                     {0: ("buy", 1995.00)})
    out["short_end_of_period"] = _run(_frame([conf_sell, flat_s, (2000.00, 2001.00, 1998.00, 1999.00, 40)]),
                                      {0: ("sell", 2005.00)})
    out["short_zero_spread"] = _run(_frame([(1999.00, 2001.00, 1998.50, 2000.50, 0), (2000.00, 2001.00, 1999.00,
                                                                                         2000.20, 0),
                                            (2000.20, 2004.80, 1999.50, 2003.00, 0)]), {0: ("sell", 2005.00)},
                                    fallback=0)
    out["long_fallback"] = _run(_frame([(2001, 2002, 1999, 2000.5, 0), (2000.00, 2001.00, 1999.00, 2000.00, 0),
                                        (2000.00, 2011.50, 1999.00, 2010.00, 50)]), {0: ("buy", 1995.00)},
                                commission=3.5, fallback=34)
    out["short_fallback_exit"] = _run(_frame([(1999.00, 2001.00, 1998.50, 2000.50, 0),
                                              (2000.00, 2001.00, 1999.00, 2000.20, 0),
                                              (2000.20, 2004.80, 1999.50, 2003.00, 0)]), {0: ("sell", 2005.00)},
                                      fallback=30)
    out["filled_spread"] = _run(_frame([conf_buy, (2000.00, 2001.00, 1999.00, 2000.00, 0),
                                        (2000.00, 2001.00, 1999.00, 2000.00, 0)]), {0: ("buy", 1995.00)})
    out["position_open_and_resize"] = _run(_frame([
        conf_buy, flat_b, (2000.00, 2011.00, 1999.50, 2010.00, 25), (2010.00, 2011.00, 2009.00, 2010.50, 25),
        (2010.00, 2011.00, 2009.00, 2010.00, 25), (2010.00, 2011.00, 2009.00, 2010.00, 25)]),
        {0: ("buy", 1995.00), 1: ("buy", 1990.00), 3: ("buy", 2005.00)}, risk=5.0)
    out["entry_outside_window"] = _run(_frame([conf_buy] * 2 + [flat_b] * 2), {1: ("buy", 1995.00)}, window=(0, 2))
    out["entry_after_data"] = _run(_frame([conf_buy] * 2 + [flat_b] * 2), {3: ("buy", 1995.00)})
    times = [T0 + pd.Timedelta(hours=h) for h in (0, 1, 2, 4, 5)]
    out["missing_gap"] = _run(_frame([conf_buy] * 3 + [flat_b] * 2, times=times), {2: ("buy", 1995.00)})
    times = [T0 + pd.Timedelta(hours=h) for h in (0, 1, 2, 4, 5, 6)]
    out["missing_gap_during_trade"] = _run(_frame([conf_buy, flat_b, flat_b, flat_b,
                                                   (2000.00, 2011.00, 1999.00, 2010.00, 25), flat_b], times=times),
                                           {0: ("buy", 1995.00)})
    out["weekend_hold"] = _run(_weekend(), {0: ("buy", 1995.00)})
    out["weekend_entry"] = _run(_weekend(), {2: ("buy", 1995.00)})
    out["sizing_rejected"] = _run(_frame([conf_buy, flat_b]), {0: ("buy", 1995.00)}, balance=100.0)
    out["margin_rejected"] = _run(_frame([conf_buy, flat_b, flat_b]), {0: ("buy", 1999.50)}, balance=100.0,
                                  risk=10.0, leverage=1)
    out["balance_depleted"] = _run(_frame([conf_buy, (2000.00, 2001.00, 1999.90, 2000.00, 25),
                                           (1000.00, 1001.00, 999.00, 1000.00, 25),
                                           (1000.00, 1001.00, 999.00, 1000.00, 25)]), {0: ("buy", 1999.75)},
                                   balance=100.0, risk=10.0, leverage=1000)
    return out


def test_micro_scenarios_golden() -> None:
    results = micro_results()
    seen: Counter = Counter()
    for data in results.values():
        seen.update(t["exit_reason"] for t in data["trades"])
        seen.update(s["reason"] for s in data["skipped"])
        seen.update(f for t in data["trades"] for f in t["flags"])
        seen.update(["stopped"] if data["stopped_reason"] else [])
    for key in ("tp", "sl", "sl_gap", "tp_gap", "end_of_period", "gap_through_stop", "position_open",
                "entry_outside_window", "missing_gap", "sizing_rejected", "held_over_weekend",
                "missing_gap_during_trade", "entry_spread_filled", "entry_spread_fallback", "entry_spread_zero",
                "exit_spread_fallback", "exit_spread_zero", "stopped"):
        assert seen[key] > 0, key
    check_golden("micro", results)
