"""Run a whole backtest: plan the windows, simulate each one, attach provenance (phase 4).

``run_backtest(config, history, scan=None, *, bars=None, progress_cb=None, cancel_event=None)``

1. checks that the config is internally consistent (strategy name/code version, ``params_hash`` of the
   validated params) and that a given ``scan`` belongs to the same symbol / params / R:R;
2. scans the FULL history once when no ``scan`` is given (``history.scan_full_history``);
3. plans the windows (``periods``): manual = one validated window, random = ``windows_count`` seeded
   windows of ``window_months`` months (seed generated and stored when missing);
4. simulates every window independently, each starting flat with the initial balance
   (``simulator.simulate_window``);
5. returns a :class:`~alpha_engine.backtest.models.RunResult` with the plan (seed, algorithm, numpy
   version, windows), the data fingerprint, the cost model, labels (no swap, zero commission, provisional
   "موقت تا تایید چک داده" unless ``config.provisional`` is False) and per-window results.

Metrics are NOT computed here (``backtest.metrics.compute_metrics`` / ``summarize_windows`` read the
trades and equity points of each ``WindowResult``). Wall-clock times are not part of the result, so the
same config + data always gives an equal ``RunResult`` (deep equality; tests/test_backtest_runner.py).

Progress: ``progress_cb(percent, window_index)`` with ``percent`` in ``[0, 100]``, non-decreasing: once at
0, then every ``PROGRESS_EVERY`` bars and at the end of each window, finally 100. ``cancel_event`` is
checked at the same cadence; when set, :class:`~alpha_engine.backtest.simulator.BacktestCancelled` is raised
(no partial result).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from ..logging_setup import get_logger, is_dev_mode
from ..strategies.stddev_channel import StdDevChannelStrategy, resolve_params
from ..strategy.base import bar_open_times
from ..strategy.params import params_hash
from .history import HistoryData, ScanResult, build_bar_arrays, data_fingerprint, scan_full_history
from .models import NO_SPREAD_DATA_LABEL_FA, HISTORICAL_SPREAD_LABEL_FA, PROVISIONAL_LABEL_FA, RunConfig, RunResult
from .periods import earliest_start, manual_window, random_windows, warmup_h4_bars
from .simulator import BacktestCancelled, BarArrays, scan_provider, simulate_window, window_bar_range

logger = get_logger(__name__)

ProgressCallback = Callable[[float, int], None]


class RunConfigError(ValueError):
    """The run config does not match the strategy / params / scan it is run with."""


def check_config(config: RunConfig) -> None:
    if config.strategy_name != StdDevChannelStrategy.name:
        raise RunConfigError(f"unsupported strategy {config.strategy_name!r} (only {StdDevChannelStrategy.name!r})")
    if config.strategy_version != StdDevChannelStrategy.version:
        raise RunConfigError(f"strategy code version {config.strategy_version} != {StdDevChannelStrategy.version}")
    _, clean = resolve_params(config.params)
    if params_hash(clean) != config.params_hash:
        raise RunConfigError("params_hash does not match the params")


def _check_scan(config: RunConfig, history: HistoryData, scan: ScanResult) -> None:
    problems = []
    if scan.symbol != config.symbol or history.symbol != config.symbol:
        problems.append("symbol")
    if scan.params_hash != config.params_hash:
        problems.append("params_hash")
    if scan.rr != config.account.rr:
        problems.append("rr")
    if scan.h1_count != len(history.h1):
        problems.append("h1 rows")
    if problems:
        raise RunConfigError(f"scan does not belong to this run ({', '.join(problems)} differ)")


def run_backtest(
    config: RunConfig,
    history: HistoryData,
    scan: ScanResult | None = None,
    *,
    bars: BarArrays | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    check_config(config)
    dev = is_dev_mode()
    if cancel_event is not None and cancel_event.is_set():
        raise BacktestCancelled("cancelled before start")
    if scan is None:
        scan = scan_full_history(StdDevChannelStrategy(), history.h1, history.h4, config.params, config.account,
                                 symbol=config.symbol)
    _check_scan(config, history, scan)
    if bars is None:
        bars = build_bar_arrays(history.h1, history.spec)
    elif len(bars) != len(history.h1):
        raise RunConfigError("bar arrays do not match the H1 history")

    p, _ = resolve_params(config.params)
    h4_ns = bar_open_times(history.h4).as_unit("ns").asi8
    earliest = earliest_start(bars.times_ns, h4_ns, scan.first_valid_index, p.atr_period)
    warm = warmup_h4_bars(p.atr_period)
    if config.mode == "manual":
        assert config.start is not None and config.end is not None
        plan = manual_window(config.start, config.end, bars.times_ns, earliest, warmup_bars=warm)
    else:
        plan = random_windows(bars.times_ns, earliest, count=config.windows_count, months=config.window_months,
                              seed=config.seed, warmup_bars=warm)

    sizes = [window_bar_range(bars.times_ns, w) for w in plan.windows]
    total = sum(max(1, hi - lo) for lo, hi in sizes)
    if dev:
        logger.debug("backtest run %s %s: %d window(s), %d bars, seed=%s, params %s, account %s, costs %s, "
                     "candidates=%d provisional=%s", config.symbol, config.mode, len(plan.windows), total, plan.seed,
                     config.params_hash[:12], config.account.model_dump(), config.cost_model.model_dump(),
                     len(scan.candidates), config.provisional)
    last_pct = 0.0

    def report(pct: float, index: int) -> None:
        nonlocal last_pct
        pct = min(100.0, max(last_pct, pct))
        last_pct = pct
        if progress_cb is not None:
            progress_cb(pct, index)

    report(0.0, plan.windows[0].index)
    provider = scan_provider(scan.by_bar)
    done = 0
    results = []
    for window, (lo, hi) in zip(plan.windows, sizes, strict=True):
        base = done

        def on_bars(k: int, _base: int = base, _index: int = window.index) -> None:
            report((_base + k) / total * 100.0, _index)

        results.append(simulate_window(bars, window, provider, spec=history.spec, account=config.account,
                                       cost_model=config.cost_model, progress=on_bars, cancel_event=cancel_event))
        done += max(1, hi - lo)
        report(done / total * 100.0, window.index)
    report(100.0, plan.windows[-1].index)

    labels = list(config.cost_model.labels_fa())
    if not bars.spread.has_data:
        labels = [NO_SPREAD_DATA_LABEL_FA if x == HISTORICAL_SPREAD_LABEL_FA else x for x in labels]
    if config.provisional:
        labels.insert(0, PROVISIONAL_LABEL_FA)
    result = RunResult(
        config=config, plan=plan, windows=results, fingerprint=data_fingerprint(history),
        provisional=config.provisional, labels_fa=labels, cost_model=config.cost_model,
        spread_source="historical" if bars.spread.has_data else "none",
        total_trades=sum(len(w.trades) for w in results), total_skipped=sum(len(w.skipped) for w in results),
    )
    if dev:
        logger.debug("backtest run %s done: trades=%d skipped=%d final balances=%s", config.symbol,
                     result.total_trades, result.total_skipped, [round(w.final_balance, 2) for w in results])
    return result


__all__ = ["BacktestCancelled", "ProgressCallback", "RunConfigError", "check_config", "run_backtest"]
