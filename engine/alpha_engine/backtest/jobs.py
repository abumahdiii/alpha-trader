"""Background execution of backtest runs (``app.state.backtest_jobs``; phase 4, wave 2).

* ONE worker thread (``ThreadPoolExecutor(max_workers=1)``): runs execute one at a time in submit order;
  later runs wait ``queued``. The HTTP threads and the event loop stay free (``/health`` answers during a
  run; tests/test_backtests_api.py).
* Per-run in-memory state (:class:`JobState`: status, phase, percent, window i/n, error) under one lock;
  the WebSocket route polls :meth:`BacktestJobs.snapshot`. The database row mirrors it: status changes
  immediately, progress THROTTLED (a write when >= ``PROGRESS_DB_INTERVAL_S`` seconds or >=
  ``PROGRESS_DB_STEP`` percentage points passed since the last write).
* Cancel: a ``threading.Event`` per run. A queued run is cancelled at once; a running run stops at the next
  progress check of the simulator (``BacktestCancelled``) and stores NOTHING but its status (no partial
  trades). Cancel is best effort: a run whose results are already being saved ends ``done``.
* Shutdown (lifespan): new submits are refused, queued runs become ``interrupted`` immediately, the running
  one is cancelled and joined for a few seconds and then ends ``interrupted``; the database is closed by the
  caller afterwards. Runs still ``queued``/``running`` in the database at the next startup are marked
  ``interrupted`` (``BacktestsRepo.mark_stale_interrupted``).

Worker steps and the overall percent reported to the UI (the simulator's own 0..100 is mapped into the
simulating band)::

    loading    0 ->  3 %   cached H1/H4 + spec (never MT5), bar arrays, gaps
    scanning   3 -> 15 %   StdDevChannelStrategy.scan over the FULL history (runner.run_backtest input)
    simulating 15 -> 95 %  runner.run_backtest progress_cb (every PROGRESS_EVERY bars + window ends)
    saving     95 -> 100 % metrics (full equity) + ONE result transaction

Prepared data is shared through a bounded LRU (the app's ``ChartCache`` instance): the history + bar arrays
keyed by symbol and the identity (file id, mtime, size) of the H1/H4 Parquet files and the spec JSON; the
first valid channel bar keyed additionally by the params hash; the full-history scan by params hash and
R:R. A cache write is a new file identity, i.e. a new key. ``/chart/setups`` uses the same history, bar
arrays and scan entries (``prepare_history`` / ``scan_for`` / ``bars_for_cost``), so the chart's setups
and a backtest are the very same candidate objects; the chart keeps its own scan only when the symbol spec
is missing.

Errors end the run ``error`` with a code and a Persian message: ``PeriodError`` / ``HistoryUnavailable``
keep their codes (``window_too_early``, ``data_too_short``, ``no_data``, ``spec_missing``, ...),
``RunConfigError`` -> ``config_mismatch``, anything else -> ``internal_error`` (logged with traceback).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ..data.cache import OhlcvCache
from ..data.schema import Timeframe
from ..logging_setup import get_logger, is_dev_mode
from ..storage.backtests_repo import ACTIVE_STATUSES, INTERRUPTED_FA, TERMINAL_STATUSES, BacktestsRepo
from ..storage.db import EngineConnection
from ..strategies.stddev_channel import StdDevChannelStrategy, resolve_params
from ..strategies.stddev_channel.setups import compute_channel_bars
from .history import (
    HistoryData,
    HistoryUnavailable,
    ScanResult,
    build_bar_arrays,
    load_history,
    scan_full_history,
    with_fallback_spread,
)
from ..storage.account_settings import AccountSettings
from .models import CostModel, RunConfig
from .periods import PeriodError
from .results import build_run_output
from .runner import BacktestCancelled, RunConfigError, run_backtest
from .simulator import BarArrays

logger = get_logger(__name__)

PROGRESS_DB_INTERVAL_S = 1.0
PROGRESS_DB_STEP = 5.0
SHUTDOWN_JOIN_S = 5.0
KEEP_FINISHED = 50  # finished job states kept in memory (older ones are served from the database)

PHASE_BANDS: dict[str, tuple[float, float]] = {
    "loading": (0.0, 3.0),
    "scanning": (3.0, 15.0),
    "simulating": (15.0, 95.0),
    "saving": (95.0, 100.0),
}

CANCELLED_FA = "اجرا به درخواست کاربر لغو شد؛ نتیجه‌ای ذخیره نشد."
CONFIG_MISMATCH_FA = "پیکربندی اجرا با استراتژی یا داده کش سازگار نیست؛ دوباره اجرا کنید."
INTERNAL_ERROR_FA = "خطای داخلی در اجرای بک‌تست؛ جزئیات در لاگ engine ثبت شد."
CLOSING_FA = "engine در حال بسته شدن است؛ اجرای جدید پذیرفته نمی‌شود."


class LruLike(Protocol):
    def get_or_compute(self, key: Hashable, compute: Callable[[], Any]) -> tuple[Any, bool]: ...


class _NoCache:
    def get_or_compute(self, key: Hashable, compute: Callable[[], Any]) -> tuple[Any, bool]:
        return compute(), False


def _lru(lru: LruLike | None) -> LruLike:
    # NOT ``lru or ...``: an empty ChartCache has __len__ == 0 and is falsy
    return lru if lru is not None else _NoCache()


class JobsClosed(RuntimeError):
    """The engine is shutting down; no new runs are accepted."""


# ---------------------------------------------------------------------------------------------- prepared data
@dataclass(frozen=True, eq=False)
class PreparedHistory:
    key: tuple[Hashable, ...]
    history: HistoryData
    bars: BarArrays


def _file_sig(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def prepare_history(cache: OhlcvCache, symbol: str, lru: LruLike | None = None) -> PreparedHistory:
    """Cached H1/H4/spec + bar arrays (raises ``HistoryUnavailable``)."""
    key = ("bt_history", str(cache.data_dir), symbol, _file_sig(cache.series_path(symbol, Timeframe.H1)),
           _file_sig(cache.series_path(symbol, Timeframe.H4)), _file_sig(cache.spec_path(symbol)))

    def compute() -> PreparedHistory:
        history = load_history(cache, symbol)
        return PreparedHistory(key=key, history=history, bars=build_bar_arrays(history.h1, history.spec))

    value, hit = _lru(lru).get_or_compute(key, compute)
    if is_dev_mode():
        logger.debug("backtest history %s: %s (%d H1 bars)", symbol, "cache hit" if hit else "loaded",
                     len(value.bars))
    return value


def first_valid_index(prepared: PreparedHistory, params: dict[str, Any], params_hash: str,
                      lru: LruLike | None = None) -> int | None:
    """First H1 bar whose channel and ATRs are defined (same code as ``history.scan_full_history``)."""

    def compute() -> int | None:
        p, _ = resolve_params(params)
        cb = compute_channel_bars(prepared.history.h1, prepared.history.h4, p, pattern_from=None)
        valid = np.flatnonzero(cb.valid)
        return int(valid[0]) if len(valid) else None

    value, _ = _lru(lru).get_or_compute(("bt_first_valid", prepared.key, params_hash), compute)
    return value


def bars_for_cost(prepared: PreparedHistory, cost_model: CostModel) -> BarArrays:
    """The prepared (auto-fallback) bar arrays, or a copy with the user's fallback spread (see costs.py)."""
    requested = cost_model.fallback_spread_points
    if requested is None:
        return prepared.bars
    return with_fallback_spread(prepared.bars, prepared.history.h1, prepared.history.spec, requested)


def bars_for(prepared: PreparedHistory, config: RunConfig) -> BarArrays:
    return bars_for_cost(prepared, config.cost_model)


def scan_for(prepared: PreparedHistory, *, symbol: str, params: dict[str, Any], params_hash: str,
             account: AccountSettings, lru: LruLike | None = None) -> ScanResult:
    """Full-history scan shared by the backtest runs and ``/chart/setups`` (same LRU entry, same candidate
    objects): keyed by the prepared data, the params hash and the R:R (the only account field a candidate
    carries)."""

    def compute() -> ScanResult:
        return scan_full_history(StdDevChannelStrategy(), prepared.history.h1, prepared.history.h4, params,
                                 account, symbol=symbol)

    value, hit = _lru(lru).get_or_compute(("bt_scan", prepared.key, params_hash, account.rr), compute)
    if is_dev_mode():
        logger.debug("backtest scan %s: %s (%d candidates)", symbol, "cache hit" if hit else "computed",
                     len(value.candidates))
    return value


def full_scan(prepared: PreparedHistory, config: RunConfig, lru: LruLike | None = None) -> ScanResult:
    return scan_for(prepared, symbol=config.symbol, params=config.params, params_hash=config.params_hash,
                    account=config.account, lru=lru)


# ---------------------------------------------------------------------------------------------- job state
@dataclass
class JobState:
    run_id: int
    config: RunConfig
    windows_total: int
    status: str = "queued"
    phase: str = "queued"
    percent: float = 0.0
    window_index: int | None = None
    error_code: str | None = None
    message_fa: str | None = None
    trade_count: int | None = None
    net_profit: float | None = None
    net_profit_pct: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    shutdown: bool = False
    future: Future | None = None
    version: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.run_id, "status": self.status, "phase": self.phase, "percent": round(self.percent, 2),
            "window_index": self.window_index,
            "window": None if self.window_index is None else self.window_index + 1,
            "windows": self.windows_total, "error_code": self.error_code, "message_fa": self.message_fa,
            "trade_count": self.trade_count, "net_profit": self.net_profit, "net_profit_pct": self.net_profit_pct,
            "cancel_requested": self.cancel_event.is_set(), "version": self.version,
        }


def _windows_total(config: RunConfig) -> int:
    return 1 if config.mode == "manual" else config.windows_count


class BacktestJobs:
    """Queue + single worker for backtest runs. See the module docstring."""

    def __init__(
        self,
        db: EngineConnection,
        cache: OhlcvCache,
        lru: LruLike | None = None,
        *,
        progress_db_interval_s: float = PROGRESS_DB_INTERVAL_S,
        progress_db_step: float = PROGRESS_DB_STEP,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repo = BacktestsRepo(db)
        self.cache = cache
        self.lru: LruLike = lru if lru is not None else _NoCache()
        self._interval = progress_db_interval_s
        self._step = progress_db_step
        self._clock = clock
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")
        self._jobs: dict[int, JobState] = {}
        self._lock = threading.Lock()
        self._closing = False

    # ------------------------------------------------------------------------------------------ public
    @property
    def closing(self) -> bool:
        return self._closing

    def submit(self, run_id: int, config: RunConfig) -> JobState:
        with self._lock:
            if self._closing:
                raise JobsClosed(CLOSING_FA)
            job = JobState(run_id=run_id, config=config, windows_total=_windows_total(config))
            self._jobs[run_id] = job
            queued_before = sum(1 for j in self._jobs.values() if j.status in ACTIVE_STATUSES) - 1
            job.future = self._executor.submit(self._run, job)
            self._prune_locked()
        if is_dev_mode():
            logger.debug("backtest job %d submitted: %s %s, %d run(s) ahead in the queue", run_id, config.symbol,
                         config.mode, queued_before)
        return job

    def snapshot(self, run_id: int) -> dict[str, Any] | None:
        """Live state from memory, else from the database (``None`` = unknown run)."""
        with self._lock:
            job = self._jobs.get(run_id)
            if job is not None:
                return job.snapshot()
        row = self.repo.status(run_id)
        if row is None:
            return None
        return {
            "id": run_id, "status": row["status"], "phase": "finished" if row["status"] in TERMINAL_STATUSES
            else row["status"], "percent": round(float(row["progress"]), 2), "window_index": None, "window": None,
            "windows": row["windows_count"] if row["mode"] == "random" else 1, "error_code": row["error_code"],
            "message_fa": row["error_message_fa"], "trade_count": row["trade_count"], "net_profit": row["net_profit"],
            "net_profit_pct": row["net_profit_pct"], "cancel_requested": False, "version": -1,
        }

    def is_active(self, run_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(run_id)
            return job is not None and job.status in ACTIVE_STATUSES

    def cancel(self, run_id: int) -> str:
        """``"cancelled"`` (was queued), ``"cancel_requested"`` (running), ``"not_active"``."""
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None or job.status not in ACTIVE_STATUSES:
                return "not_active"
            job.cancel_event.set()
            job.version += 1
            if job.status == "running":
                if is_dev_mode():
                    logger.debug("backtest job %d: cancel requested while running", run_id)
                return "cancel_requested"
            # queued: final right away (DB first, then memory; lock order jobs -> db, never the reverse)
            self._safe_finish_db(job, "cancelled", "cancelled", CANCELLED_FA, None, None)
            self._set_final_locked(job, "cancelled", "cancelled", CANCELLED_FA)
        if is_dev_mode():
            logger.debug("backtest job %d: cancelled while queued", run_id)
        return "cancelled"

    def shutdown(self, timeout: float = SHUTDOWN_JOIN_S) -> None:
        """Refuse new runs, interrupt queued ones, cancel + briefly join the running one."""
        with self._lock:
            self._closing = True
            active = [j for j in self._jobs.values() if j.status in ACTIVE_STATUSES]
            running = [j for j in active if j.status == "running"]
            for job in active:
                job.shutdown = True
                job.cancel_event.set()
                if job.status == "queued":
                    self._safe_finish_db(job, "interrupted", "interrupted", INTERRUPTED_FA, None, None)
                    self._set_final_locked(job, "interrupted", "interrupted", INTERRUPTED_FA)
        self._executor.shutdown(wait=False, cancel_futures=True)
        futures = [j.future for j in running if j.future is not None]
        started = time.perf_counter()
        done, not_done = wait_futures(futures, timeout=timeout) if futures else (set(), set())
        if not_done:
            logger.warning("backtest worker did not stop within %.1f s; its run is marked interrupted at the next "
                           "startup", timeout)
        if is_dev_mode():
            logger.debug("backtest jobs shut down: %d active (%d running) interrupted, joined in %.2f s", len(active),
                         len(running), time.perf_counter() - started)

    # ------------------------------------------------------------------------------------------ internals
    def _prune_locked(self) -> None:
        finished = [rid for rid, j in self._jobs.items() if j.status in TERMINAL_STATUSES]
        for rid in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
            del self._jobs[rid]

    def _set_final_locked(self, job: JobState, status: str, code: str | None, message_fa: str | None) -> None:
        job.status = status
        job.phase = "finished"
        job.error_code = code
        job.message_fa = message_fa
        job.version += 1

    def _safe_finish_db(self, job: JobState, status: str, code: str | None, message_fa: str | None,
                        elapsed: float | None, timings: dict[str, float] | None) -> None:
        try:
            self.repo.finish(job.run_id, status, error_code=code, error_message_fa=message_fa, elapsed_s=elapsed,
                             timings=timings)  # type: ignore[arg-type]
        except Exception as exc:  # the DB may already be closed at shutdown; startup marks it interrupted
            logger.warning("backtest run %d: could not store status %s (%s)", job.run_id, status, type(exc).__name__)
            if is_dev_mode():
                logger.debug("backtest run %d: status write failed", job.run_id, exc_info=True)

    def _finish(self, job: JobState, status: str, code: str | None, message_fa: str | None,
                elapsed: float | None, timings: dict[str, float] | None) -> None:
        self._safe_finish_db(job, status, code, message_fa, elapsed, timings)
        with self._lock:
            self._set_final_locked(job, status, code, message_fa)

    def _set_phase(self, job: JobState, phase: str) -> None:
        with self._lock:
            job.phase = phase
            job.percent = max(job.percent, PHASE_BANDS[phase][0])
            job.version += 1
        if is_dev_mode():
            logger.debug("backtest job %d: phase %s (%.1f %%)", job.run_id, phase, job.percent)

    def _check_cancel(self, job: JobState) -> None:
        if job.cancel_event.is_set():
            raise BacktestCancelled("cancel requested")

    def _run(self, job: JobState) -> None:
        with self._lock:
            if job.status != "queued":  # cancelled or interrupted while waiting
                return
            job.status = "running"
            job.version += 1
        started = time.perf_counter()
        timings: dict[str, float] = {}
        try:
            if not self.repo.mark_running(job.run_id):
                raise RuntimeError("run row is not queued any more")
        except Exception as exc:
            logger.warning("backtest run %d could not be started (%s)", job.run_id, type(exc).__name__)
            self._finish(job, "error", "internal_error", INTERNAL_ERROR_FA, None, None)
            return
        if is_dev_mode():
            logger.debug("backtest job %d started: %s %s windows=%d", job.run_id, job.config.symbol, job.config.mode,
                         job.windows_total)
        last_db = {"t": self._clock(), "pct": 0.0}
        lo, hi = PHASE_BANDS["simulating"]

        def on_progress(pct: float, index: int) -> None:
            overall = lo + (hi - lo) * pct / 100.0
            with self._lock:
                job.percent = max(job.percent, overall)
                job.window_index = index
                job.version += 1
                current = job.percent
            now = self._clock()
            if now - last_db["t"] >= self._interval or current - last_db["pct"] >= self._step:
                last_db["t"], last_db["pct"] = now, current
                try:
                    self.repo.update_progress(job.run_id, current)
                except Exception as exc:  # progress is cosmetic; never fail the run for it
                    logger.warning("backtest run %d: progress write failed (%s)", job.run_id, type(exc).__name__)
                if is_dev_mode():
                    logger.debug("backtest job %d: %.1f %% (window %d/%d)", job.run_id, current, index + 1,
                                 job.windows_total)

        try:
            self._set_phase(job, "loading")
            t = time.perf_counter()
            prepared = prepare_history(self.cache, job.config.symbol, self.lru)
            timings["load_s"] = time.perf_counter() - t
            self._check_cancel(job)
            self._set_phase(job, "scanning")
            t = time.perf_counter()
            scan = full_scan(prepared, job.config, self.lru)
            timings["scan_s"] = time.perf_counter() - t
            self._check_cancel(job)
            self._set_phase(job, "simulating")
            t = time.perf_counter()
            result = run_backtest(job.config, prepared.history, scan, bars=bars_for(prepared, job.config),
                                  progress_cb=on_progress, cancel_event=job.cancel_event)
            timings["simulate_s"] = time.perf_counter() - t
            self._set_phase(job, "saving")
            t = time.perf_counter()
            output = build_run_output(result)
            timings["metrics_s"] = time.perf_counter() - t
            self._check_cancel(job)
            timings = self.repo.save_result(job.run_id, output, started=started, timings=timings)
        except BacktestCancelled:
            status = "interrupted" if job.shutdown else "cancelled"
            self._finish(job, status, status, INTERRUPTED_FA if job.shutdown else CANCELLED_FA,
                         time.perf_counter() - started, timings)
            if is_dev_mode():
                logger.debug("backtest job %d %s after %.2f s", job.run_id, status, time.perf_counter() - started)
            return
        except (PeriodError, HistoryUnavailable) as exc:
            self._finish(job, "error", exc.code, exc.message_fa, time.perf_counter() - started, timings)
            if is_dev_mode():
                logger.debug("backtest job %d error %s: %s", job.run_id, exc.code, exc.message_fa)
            return
        except RunConfigError as exc:
            logger.warning("backtest run %d: config mismatch: %s", job.run_id, exc)
            self._finish(job, "error", "config_mismatch", CONFIG_MISMATCH_FA, time.perf_counter() - started, timings)
            return
        except Exception as exc:
            logger.error("backtest run %d failed: %s: %s", job.run_id, type(exc).__name__, exc)
            if is_dev_mode():
                logger.debug("backtest run %d traceback", job.run_id, exc_info=True)
            self._finish(job, "error", "internal_error", INTERNAL_ERROR_FA, time.perf_counter() - started, timings)
            return
        with self._lock:
            job.trade_count = output.trade_count
            job.net_profit = output.net_profit
            job.net_profit_pct = output.net_profit_pct
            job.percent = 100.0
            self._set_final_locked(job, "done", None, None)
        if is_dev_mode():
            logger.debug("backtest job %d done in %.2f s: trades=%d net=%.2f timings=%s", job.run_id,
                         timings["total_s"], output.trade_count, output.net_profit,
                         {k: round(v, 3) for k, v in timings.items()})


__all__ = [
    "PHASE_BANDS",
    "BacktestJobs",
    "JobState",
    "JobsClosed",
    "PreparedHistory",
    "bars_for",
    "bars_for_cost",
    "first_valid_index",
    "full_scan",
    "prepare_history",
    "scan_for",
]
