"""Run a whole backtest: plan the windows, simulate each one, attach provenance (phase 4).

``run_backtest(config, history, scan=None, *, strategy=None, bars=None, progress_cb=None, cancel_event=None)``

0. the strategy is ``strategy`` if given, else the one that made ``scan`` (``ScanResult.strategy``), else the
   registered strategy named ``config.strategy_name`` (``strategy.resolve.strategy_for``; an unknown name ->
   :class:`RunConfigError`). Nothing here is specific to one strategy;
1. checks that the config is internally consistent with that strategy (:func:`check_config`: name, code
   version, source SHA-256, ``params_hash`` of the params the strategy validates) and that a given ``scan``
   belongs to the same strategy / symbol / params / R:R;
2. scans the FULL history once when no ``scan`` is given (``history.scan_full_history``);
3. plans the windows (``periods``) with the strategy's first valid bar and warm-up margin: manual = one
   validated window, random = ``windows_count`` seeded windows of ``window_months`` months (seed generated
   and stored when missing);
4. simulates every window independently, each starting flat with the initial balance
   (``simulator.simulate_window``);
5. returns a :class:`~alpha_engine.backtest.models.RunResult` with the plan (seed, algorithm, numpy
   version, windows), the data fingerprint, the cost model, the resolved fallback spread
   (``spread_fallback``: points, source auto/user/none, observed spread period, bars that used it), labels
   (spread: historical / fallback / zero cost -- ``models.spread_label_fa``; no swap, zero commission,
   provisional "موقت تا تایید چک داده" unless ``config.provisional`` is False) and per-window results.

``bars`` (optional, prebuilt) must have been built with the fallback spread the cost model resolves to
(``costs.resolve_fallback_points``: user value, or auto = ceil of the median observed spread), else
:class:`RunConfigError`.

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
from ..strategy.base import Strategy, bar_open_times
from ..strategy.params import params_hash
from ..strategy.registry import StrategyRegistry, UnknownStrategyError
from ..strategy.resolve import StrategyVersionMismatchError, strategy_for
from .history import HistoryData, ScanResult, build_bar_arrays, data_fingerprint, scan_full_history
from .costs import observed_spread, resolve_fallback_points
from .models import (
    HISTORICAL_SPREAD_LABEL_FA,
    PROVISIONAL_LABEL_FA,
    RunConfig,
    RunResult,
    SpreadFallback,
    spread_label_fa,
)
from .periods import earliest_start, manual_window, random_windows
from .simulator import BacktestCancelled, BarArrays, scan_provider, simulate_window, window_bar_range

logger = get_logger(__name__)

ProgressCallback = Callable[[float, int], None]


class RunConfigError(ValueError):
    """The run config does not match the strategy / params / scan it is run with."""


def strategy_for_config(config: RunConfig, registry: StrategyRegistry | None = None) -> Strategy:
    """The registered strategy named ``config.strategy_name`` (:class:`RunConfigError` if unknown)."""
    try:
        return strategy_for(config.strategy_name, registry=registry)
    except (UnknownStrategyError, StrategyVersionMismatchError) as exc:
        raise RunConfigError(f"strategy {config.strategy_name!r} is not available: {exc}") from None


def check_config(config: RunConfig, strategy: Strategy) -> None:
    """``config`` was made for ``strategy`` (name, code version, source hash) with params it accepts."""
    ident = strategy.identity
    if config.strategy_name != ident.name:
        raise RunConfigError(f"config is for strategy {config.strategy_name!r}, not {ident.name!r}")
    if config.strategy_version != ident.version:
        raise RunConfigError(f"strategy code version {config.strategy_version} != {ident.version}")
    if config.strategy_sha256 != ident.sha256:
        raise RunConfigError(f"strategy source hash {config.strategy_sha256} != {ident.sha256}")
    if config.strategy_source is not None and config.strategy_source != ident.source:
        raise RunConfigError(f"strategy source {config.strategy_source!r} != {ident.source!r}")
    clean, errors = strategy.validate_params(config.params)
    if errors:
        raise RunConfigError("params rejected by the strategy: " + "; ".join(errors))
    if params_hash(clean) != config.params_hash:
        raise RunConfigError("params_hash does not match the params")


def _check_scan(config: RunConfig, history: HistoryData, scan: ScanResult, strategy: Strategy) -> None:
    problems = []
    if (scan.strategy_name, scan.strategy_version) != (config.strategy_name, config.strategy_version) or \
            (scan.identity is not None and scan.identity != strategy.identity):
        problems.append("strategy")
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
    strategy: Strategy | None = None,
    bars: BarArrays | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    if strategy is None:
        strategy = scan.strategy if scan is not None and scan.strategy is not None else strategy_for_config(config)
    check_config(config, strategy)
    dev = is_dev_mode()
    if cancel_event is not None and cancel_event.is_set():
        raise BacktestCancelled("cancelled before start")
    if scan is None:
        scan = scan_full_history(strategy, history.h1, history.h4, config.params, config.account,
                                 symbol=config.symbol)
    _check_scan(config, history, scan, strategy)
    raw_spread = history.h1["spread"].to_numpy() if "spread" in history.h1.columns else None
    fb_points, fb_source = resolve_fallback_points(raw_spread, config.cost_model.fallback_spread_points)
    if bars is None:
        bars = build_bar_arrays(history.h1, history.spec, fb_points)
    elif len(bars) != len(history.h1):
        raise RunConfigError("bar arrays do not match the H1 history")
    elif bars.spread.fallback_points != fb_points:
        raise RunConfigError(f"bar arrays were built with fallback spread {bars.spread.fallback_points} points, "
                             f"the cost model resolves to {fb_points}")

    h4_ns = bar_open_times(history.h4).as_unit("ns").asi8
    warm = int(strategy.warmup_margin_h4_bars(config.params))
    texts = strategy.warmup_texts_fa
    earliest = earliest_start(bars.times_ns, h4_ns, scan.first_valid_index, warm, texts=texts)
    if config.mode == "manual":
        assert config.start is not None and config.end is not None
        plan = manual_window(config.start, config.end, bars.times_ns, earliest, warmup_bars=warm, texts=texts)
    else:
        plan = random_windows(bars.times_ns, earliest, count=config.windows_count, months=config.window_months,
                              seed=config.seed, warmup_bars=warm)

    sizes = [window_bar_range(bars.times_ns, w) for w in plan.windows]
    total = sum(max(1, hi - lo) for lo, hi in sizes)
    if dev:
        logger.debug("backtest run %s %s: strategy %s, %d window(s), %d bars, seed=%s, params %s, account %s, "
                     "costs %s, fallback spread %d pts (%s), candidates=%d warm-up=%d H4 bars provisional=%s",
                     config.symbol, config.mode, strategy.identity.label(), len(plan.windows), total, plan.seed,
                     config.params_hash[:12], config.account.model_dump(), config.cost_model.model_dump(), fb_points,
                     fb_source, len(scan.candidates), warm, config.provisional)
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

    obs = observed_spread(raw_spread)
    h1_times = bar_open_times(history.h1)
    fallback = SpreadFallback(
        points=fb_points, source=fb_source,
        observed_from=None if obs.first_index is None else h1_times[obs.first_index].to_pydatetime(),
        observed_to=None if obs.last_index is None else h1_times[obs.last_index].to_pydatetime(),
        observed_bars=obs.count, observed_median=obs.median,
        fallback_bars=sum(w.spread_fallback_bars for w in results),
        zero_bars=sum(w.zero_spread_bars_unfilled for w in results), total_bars=sum(w.bars for w in results),
    )
    spread_label = spread_label_fa(fallback)
    labels = [spread_label if x == HISTORICAL_SPREAD_LABEL_FA else x for x in config.cost_model.labels_fa()]
    if config.provisional:
        labels.insert(0, PROVISIONAL_LABEL_FA)
    result = RunResult(
        config=config, plan=plan, windows=results, fingerprint=data_fingerprint(history),
        provisional=config.provisional, labels_fa=labels, cost_model=config.cost_model,
        spread_source="historical" if bars.spread.has_data else "none", spread_fallback=fallback,
        total_trades=sum(len(w.trades) for w in results), total_skipped=sum(len(w.skipped) for w in results),
    )
    if dev:
        logger.debug("backtest run %s done: trades=%d skipped=%d final balances=%s", config.symbol,
                     result.total_trades, result.total_skipped, [round(w.final_balance, 2) for w in results])
    return result


__all__ = ["BacktestCancelled", "ProgressCallback", "RunConfigError", "check_config", "run_backtest",
           "strategy_for_config"]
