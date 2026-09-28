"""The live-signal scheduler (``app.state.live_signals``; phase 6). SUGGESTIONS ONLY -- it never places, modifies or
checks an order; MT5 is used exclusively through the read-only adapter (``symbol_info_tick`` and the incremental
``copy_rates`` cache update of ``MarketDataService.update``).

One background thread (like ``BacktestJobs``), started by the app lifespan after the backtest jobs and stopped
before the database closes. Nothing runs until the user enables it (``POST /signals/settings``; stored in
``live_settings``). :meth:`LiveSignals.step` is one iteration of the loop and is what the tests drive with an
injected clock.

Per iteration (every ``poll_s`` seconds, or sooner when a check is due)
-----------------------------------------------------------------------
1. **MT5**: not connected -> ``mt5_down`` (event), reconnect through ``Mt5Adapter.ensure_connected`` with an
   exponential backoff (``RECONNECT_BACKOFF_S``); no data -> no signal. Errors never escape the loop.
2. **Server time** (orchestrator decision 4): the last tick of every symbol (``symbol_info_tick``, server clock,
   converted to UTC with the broker offset model). ``clock skew = min(pc_now - tick_utc)`` over the last hour of
   polls (the freshest tick approximates the server clock). ``|skew| > SKEW_TOLERANCE_S`` -> the server time is
   ``pc_now - skew`` (warning + ``tick`` event); a tick newer than that wins. Every "which bar is closed" decision
   below uses this server time -- including the cache update (``update(now_utc=server_now)``), so a PC clock that
   runs ahead can never make a forming bar look closed.
3. **Expiry** (decision 2): an ``active`` signal expires at ``expires_utc`` = the close of its entry bar (the next
   TRADING H1 bar after the confirmation bar; over a weekend/holiday/break: the first bar after the reopen, which
   is filled in once known -- ``expires_utc`` stays null meanwhile).
4. **Boundary check**, once per H1 boundary ``B`` (server time) per symbol, ``grace_s`` after ``B``, symbols ONE AT A
   TIME:

   a. incremental cache update H1 (+ H4 when an H4 bar closed at ``B`` or the H4 cache is behind) -- only an
      existing MT5 cache is updated (never a full download: ``no_cache`` / seed data / offset-model change ->
      error, no signal);
   b. the expected bar (open ``B - 1h``) must be cached: no tick during that hour -> **market closed** (logged, no
      signal); ticks but no bar yet -> retried every poll up to ``retry_tolerance_s``, then **incomplete data**
      (logged, no signal). Same for the H4 bar that closed at ``B``;
   c. **decision = the backtest's code path**: ``scan_for(prepare_history(...))`` (the very function and LRU entry
      the backtest and ``/chart/setups`` use) -> the candidate of bar ``t = B - 1h``; cross-checked with
      ``evaluate_checked(build_context(..., now_utc=B))`` on the closed prefix. Both ``None`` -> no setup. Equal
      (every field, exactly) -> a signal. Different -> **mismatch**: stored as a ``rejected`` row with
      ``mismatch_json`` + a ``mismatch`` event, never an active signal;
   d. signal = indicative entry/levels/volume (``live.indicative_levels``), the entry step / expiry
      (``live.entry_step_info``), the backtest-skip flag (``live.backtest_skip_flag``), stored with the dedupe key
      ``(strategy, code version, params hash, symbol, confirmation bar)``; a duplicate is logged and dropped;
   e. **revision**: the signals of the last ``REVISION_BARS`` bars are compared with the fresh scan (the update
      re-reads the last 2 cached bars, so broker revisions land here); a signal the scan no longer produces
      identically -> ``superseded``. Signals waiting for their entry bar get it (expiry, entry-step class).

Live settings: ``live_strategy`` (default ``stddev_channel``) must be a registered BUILT-IN strategy -- uploaded
plugins are chart + backtest only for now (user decision; ``POST /signals/settings`` refuses them).

Messages for ``WS /ws/signals`` go through an in-memory, sequence-numbered buffer (:meth:`messages_after`): ``signal``,
``expired``, ``superseded`` and ``status`` (on a change of the significant status fields).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from ..backtest.history import HistoryUnavailable
from ..backtest.jobs import LruLike, PreparedHistory, prepare_history, scan_for
from ..backtest.models import SKIP_REASON_FA, SkipReason
from ..data.cache import CacheError, CacheLockTimeout
from ..data.schema import Timeframe
from ..data.timezone import OffsetError, OffsetModel
from ..logging_setup import get_logger, is_dev_mode
from ..market_data import MarketDataService
from ..mt5_adapter import Mt5Error
from ..storage.account_settings import AccountSettings, AccountSettingsRepo
from ..storage.db import EngineConnection, parse_utc
from ..storage.signals_repo import LiveSettings, SignalsRepo, api_time, signal_to_api
from ..storage.strategies_repo import StoredParamsInvalidError
from ..strategy.base import StrategyContractError, bar_open_times, evaluate_checked
from ..strategy.context import build_context
from ..strategy.registry import StrategyRegistry, UnknownStrategyError
from ..strategy.resolve import ActiveStrategy, StrategyVersionMismatchError, resolve_active
from ..strategy.signal import SignalCandidate
from .live import (
    backtest_skip_flag,
    candidate_diff,
    candidate_dump,
    entry_step_info,
    indicative_levels,
    setup_title_fa,
)

logger = get_logger(__name__)

H1 = timedelta(hours=1)
H4 = timedelta(hours=4)
H1_NS = 3_600_000_000_000
POLL_S = 15.0
RETRY_TOLERANCE_S = 180.0
SKEW_TOLERANCE_S = 10.0
SKEW_WINDOW_S = 3600.0
MIN_SKEW_SAMPLES = 1
WARMUP_POLLS = 2  # tick polls before the first boundary check (so a skewed PC clock is known first)
RECONNECT_BACKOFF_S = (10.0, 20.0, 40.0, 60.0, 120.0, 300.0)
REVISION_BARS = 3
REVISION_IGNORED = frozenset({"rr", "indicative_take_profit"})
CHAIN_LOOKBACK = timedelta(days=90)
ENTRY_REVISION_WINDOW = timedelta(days=7)
EVENT_RETENTION = timedelta(days=30)
MESSAGE_BUFFER = 500
SHUTDOWN_JOIN_S = 5.0

MT5_DOWN_FA = "اتصال به MetaTrader 5 برقرار نیست؛ سیگنال لایو تا برقراری دوباره اتصال صادر نمی‌شود."
PLUGIN_NOT_LIVE_FA = ("سیستم‌های بارگذاری‌شده (پلاگین) فعلا فقط در چارت و بک‌تست قابل استفاده‌اند و برای سیگنال لایو "
                      "انتخاب نمی‌شوند.")
MARKET_CLOSED_FA = "بازار در ساعت گذشته بسته بود (تیکی دریافت نشد)؛ کندل جدیدی برای تصمیم نیست."
INCOMPLETE_FA = "کندل بسته‌شده مورد انتظار (یا کندل H4 آن) پس از مهلت تحمل در کش نیامد؛ با داده ناقص سیگنالی صادر نمی‌شود."
MISMATCH_FA = ("تصمیم مسیر بک‌تست (اسکن کل تاریخچه) و مسیر لایو (ارزیابی همان کندل) یکسان نبود؛ سیگنالی صادر نشد و "
               "مغایرت ثبت شد.")
MISSING_GAP_SKIP_FA = f"{SKIP_REASON_FA[SkipReason.MISSING_GAP]} (برچسب موقت: طبقه‌بندی گپ ممکن است با داده بعدی تغییر کند)"
SUPERSEDED_FA = "داده این کندل‌ها در بروکر اصلاح شد و اسکن تازه دیگر همین ستاپ را نمی‌دهد؛ سیگنال باطل شد."
UPDATE_REFUSED_FA = {
    "no_cache": "برای این نماد کش MT5 وجود ندارد؛ ابتدا دریافت تاریخچه (fetch_history) را اجرا کنید.",
    "not_incremental": "کش این نماد داده آزمایشی است؛ به‌روزرسانی افزایشی ممکن نیست (دریافت کامل تاریخچه لازم است).",
    "offset_model_changed": "مدل اختلاف ساعت سرور با کش فرق دارد؛ دریافت کامل تاریخچه لازم است.",
}
UPDATE_FAILED_FA = "به‌روزرسانی کش از MT5 ناموفق بود؛ با داده ناقص سیگنالی صادر نمی‌شود."
ERROR_FA = "خطای داخلی در بررسی سیگنال لایو؛ جزئیات در لاگ engine ثبت شد."


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | pd.Timestamp | None) -> str | None:
    if value is None:
        return None
    return pd.Timestamp(value).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _floor_hour(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _ns(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).tz_convert("UTC").as_unit("ns").value)


class PluginNotLive(RuntimeError):
    """The selected live strategy is an uploaded plugin (chart + backtest only for now)."""


class _UpdateRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LiveSignals:
    """Live-signal scheduler + message buffer (module docstring)."""

    def __init__(
        self,
        db: EngineConnection,
        market_data: MarketDataService,
        *,
        registry: StrategyRegistry | None = None,
        lru: LruLike | None = None,
        clock: Callable[[], datetime] = utc_now,
        poll_s: float = POLL_S,
        retry_tolerance_s: float = RETRY_TOLERANCE_S,
        skew_tolerance_s: float = SKEW_TOLERANCE_S,
        reconnect_backoff_s: tuple[float, ...] = RECONNECT_BACKOFF_S,
    ) -> None:
        self.db = db
        self.repo = SignalsRepo(db)
        self.market = market_data
        self.registry = registry
        self.lru = lru
        self._clock = clock
        self.poll_s = poll_s
        self.retry_tolerance_s = retry_tolerance_s
        self.skew_tolerance_s = skew_tolerance_s
        self._backoff = reconnect_backoff_s or (60.0,)
        self._settings = self.repo.get_settings()
        self._lock = threading.RLock()  # status fields + message buffer
        self._stop = threading.Event()
        self._step_lock = threading.Lock()
        self._polls = 0
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._messages: deque[tuple[int, dict[str, Any]]] = deque(maxlen=MESSAGE_BUFFER)
        self._seq = 0
        # scheduler state
        self._state = "disabled" if not self._settings.enabled else "starting"
        self._mt5_state: str | None = None
        self._model_label: str | None = None
        self._ticks: dict[str, dict[str, Any]] = {}
        self._tick_hours: dict[str, deque[datetime]] = {}  # UTC hours in which a tick was observed (recent)
        self._skew_samples: deque[tuple[float, float]] = deque()  # (pc epoch, pc_now - tick_utc) of moving ticks
        self._last_poll: dict[str, tuple[float, int]] = {}  # symbol -> (pc epoch, tick server time) of the last poll
        self._skew_s: float | None = None
        self._skew_warned = False
        self._server_now: datetime | None = None
        self._done: dict[str, datetime] = {}
        self._checks: dict[str, dict[str, Any]] = {}
        self._last_check_utc: datetime | None = None
        self._next_check_utc: datetime | None = None
        self._last_error_fa: str | None = None
        self._last_error_utc: datetime | None = None
        self._reconnect_attempts = 0
        self._next_reconnect_pc: datetime | None = None
        self._last_prune: datetime | None = None
        self._status_key: tuple[Any, ...] | None = None

    # ------------------------------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-signals", daemon=True)
        self._thread.start()
        if is_dev_mode():
            logger.debug("live signals: scheduler thread started (enabled=%s, strategy=%s, grace=%gs)",
                         self._settings.enabled, self._settings.live_strategy, self._settings.grace_s)

    def shutdown(self, timeout: float = SHUTDOWN_JOIN_S) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("live signals thread did not stop within %.1f s", timeout)
        with self._lock:
            self._state = "stopped"
        if is_dev_mode():
            logger.debug("live signals: scheduler stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # the engine must never crash because of the scheduler
                self._error(ERROR_FA, None, exc)
            self._wake.wait(self._wait_s())
            self._wake.clear()

    def _wait_s(self) -> float:
        with self._lock:
            if not self._settings.enabled:
                return 3600.0  # woken by update_settings / shutdown
            nxt = self._next_check_utc
        if nxt is None:
            return self.poll_s
        delta = (nxt - self._clock()).total_seconds()
        return float(min(self.poll_s, max(0.5, delta)))

    # ------------------------------------------------------------------------------------------ settings
    @property
    def settings(self) -> LiveSettings:
        with self._lock:
            return self._settings

    def update_settings(self, *, enabled: bool | None = None, live_strategy: str | None = None,
                        grace_s: float | None = None) -> LiveSettings:
        """Persist new settings (validated by the route) and wake the scheduler."""
        new = self.repo.update_settings(enabled=enabled, live_strategy=live_strategy, grace_s=grace_s)
        with self._lock:
            old = self._settings
            self._settings = new
            if new.live_strategy != old.live_strategy or (new.enabled and not old.enabled):
                self._done.clear()  # decide the current boundary again with the new settings
            if not new.enabled:
                self._state = "disabled"
                self._next_check_utc = None
            elif not old.enabled:
                self._state = "starting"
        if is_dev_mode():
            logger.debug("live signals settings: %s -> %s", old.to_api(), new.to_api())
        self._publish_status(force=True)
        self._wake.set()
        return new

    # ------------------------------------------------------------------------------------------ status / messages
    def status(self) -> dict[str, Any]:
        adapter = self.market.adapter
        try:
            mt5_state = adapter.status().state if adapter is not None else "not_initialized"
        except Exception:
            mt5_state = "error"
        with self._lock:
            return {
                "enabled": self._settings.enabled,
                "state": self._state,
                "mt5_state": mt5_state,
                "live_strategy": self._settings.live_strategy,
                "grace_s": self._settings.grace_s,
                "server_offset": self._model_label,
                "server_time_utc": _iso(self._server_now),
                "clock_skew_s": None if self._skew_s is None else round(self._skew_s, 3),
                "clock_skew_warning": self._skew_warned,
                "last_tick_utc": {s: (self._ticks.get(s) or {}).get("time_utc") for s in self.market.symbols},
                "last_check_utc": _iso(self._last_check_utc),
                "next_check_utc": _iso(self._next_check_utc),
                "checks": {s: dict(v) for s, v in self._checks.items()},
                "last_error_fa": self._last_error_fa,
                "last_error_utc": _iso(self._last_error_utc),
            }

    def _publish(self, message: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self._messages.append((self._seq, message))
        if is_dev_mode():
            logger.debug("live signals WS message #%d: %s%s", self._seq, message.get("type"),
                         f" id={message['signal']['id']}" if "signal" in message else "")

    def _publish_status(self, force: bool = False) -> None:
        status = self.status()
        key = (status["enabled"], status["state"], status["mt5_state"], status["live_strategy"], status["grace_s"],
               status["last_check_utc"], status["next_check_utc"], status["last_error_fa"],
               status["clock_skew_warning"])
        with self._lock:
            if not force and key == self._status_key:
                return
            self._status_key = key
        self._publish({"type": "status", "status": status})

    def messages_after(self, seq: int) -> tuple[list[dict[str, Any]], int]:
        """Buffered messages with a sequence number > ``seq`` and the latest sequence number."""
        with self._lock:
            return [m for s, m in self._messages if s > seq], self._seq

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            seq = self._seq
        active = [signal_to_api(r) for r in self.repo.with_status("active")]
        return {"type": "snapshot", "seq": seq, "status": self.status(), "active": active}

    # ------------------------------------------------------------------------------------------ helpers
    def _event(self, kind: str, symbol: str | None, payload: dict[str, Any]) -> None:
        try:
            self.repo.add_event(kind, symbol, payload, when=self._clock())
        except Exception as exc:  # the journal is best effort (the DB may be closing)
            logger.warning("live signals: event %s could not be stored (%s)", kind, type(exc).__name__)

    def _error(self, message_fa: str, symbol: str | None, exc: BaseException | None = None,
               **payload: Any) -> None:
        with self._lock:
            self._last_error_fa = message_fa
            self._last_error_utc = self._clock()
        detail = None if exc is None else f"{type(exc).__name__}: {exc}"
        logger.warning("live signals%s: %s%s", f" {symbol}" if symbol else "", message_fa,
                       f" ({detail})" if detail else "")
        if is_dev_mode() and exc is not None:
            logger.debug("live signals error traceback", exc_info=exc)
        self._event("error", symbol, {"message_fa": message_fa, "detail": detail, **payload})

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    # ------------------------------------------------------------------------------------------ the loop body
    def step(self) -> None:
        """One scheduler iteration (module docstring). Never raises for MT5/data problems. Serialised: the
        thread and a direct caller (tests) never run two iterations at once."""
        with self._step_lock:
            self._step()

    def _step(self) -> None:
        pc_now = self._clock()
        with self._lock:
            settings = self._settings
        if not settings.enabled:
            self._set_state("disabled")
            self._publish_status()
            return
        if not self._ensure_mt5(pc_now):
            self._publish_status()
            return
        try:
            model = self.market.effective_model()
        except (OffsetError, Mt5Error) as exc:
            self._error("مدل اختلاف ساعت سرور بروکر مشخص نیست؛ سیگنال لایو صادر نمی‌شود.", None, exc)
            self._publish_status()
            return
        with self._lock:
            self._model_label = model.label
        self._poll_ticks(pc_now, model)
        self._polls += 1
        server_now = self._estimate_server_now(pc_now)
        if self._polls < WARMUP_POLLS:
            # the first poll cannot tell a moving market from a stale tick (clock skew unknown): never decide yet
            with self._lock:
                self._state = "running"
                self._next_check_utc = pc_now + timedelta(seconds=self.poll_s)
            self._publish_status()
            return
        self._expire_due(server_now)
        self._tick_expiries()
        boundary = _floor_hour(server_now)
        grace = timedelta(seconds=settings.grace_s)
        pending = False
        if server_now >= boundary + grace:
            for symbol in self.market.symbols:  # one symbol at a time
                if self._stop.is_set():
                    return
                if self._done.get(symbol) == boundary:
                    continue
                with self._lock:
                    self._last_check_utc = server_now
                if self._check_symbol(symbol, boundary, server_now, pc_now, model, settings) == "done":
                    self._done[symbol] = boundary
                else:
                    pending = True
        with self._lock:
            self._state = "running"
            if pending:
                self._next_check_utc = server_now + timedelta(seconds=self.poll_s)
            elif server_now >= boundary + grace:
                self._next_check_utc = boundary + H1 + grace
            else:
                self._next_check_utc = boundary + grace
        self._prune(server_now)
        self._publish_status()

    # ------------------------------------------------------------------------------------------ MT5
    def _ensure_mt5(self, pc_now: datetime) -> bool:
        adapter = self.market.adapter
        if adapter is None:
            self._set_state("mt5_down")
            return False
        if adapter.connected:
            if self._mt5_state not in (None, "connected"):
                self._event("mt5_status", None, {"state": "connected", "after_attempts": self._reconnect_attempts})
                if is_dev_mode():
                    logger.debug("live signals: MT5 connected again after %d attempt(s)", self._reconnect_attempts)
                self._polls = 0  # fresh ticks after a reconnect: warm up again
            self._mt5_state = "connected"
            self._reconnect_attempts = 0
            self._next_reconnect_pc = None
            return True
        state = adapter.status().state
        if self._mt5_state != "down":
            self._mt5_state = "down"
            self._event("mt5_status", None, {"state": state})
            with self._lock:
                self._last_error_fa = MT5_DOWN_FA
                self._last_error_utc = pc_now
            logger.warning("live signals: MT5 not connected (%s); no signals until it is back", state)
        self._set_state("mt5_down")
        if self._next_reconnect_pc is None or pc_now >= self._next_reconnect_pc:
            delay = self._backoff[min(self._reconnect_attempts, len(self._backoff) - 1)]
            self._reconnect_attempts += 1
            self._next_reconnect_pc = pc_now + timedelta(seconds=delay)
            ok = False
            try:
                ok = bool(adapter.ensure_connected(min_interval=0.0))
            except Exception as exc:  # never let a reconnect attempt kill the loop
                if is_dev_mode():
                    logger.debug("live signals: reconnect raised %s", type(exc).__name__, exc_info=True)
            if is_dev_mode():
                logger.debug("live signals: MT5 reconnect attempt %d -> %s (next in %.0f s)",
                             self._reconnect_attempts, "connected" if ok else "failed", delay)
            if ok:
                return self._ensure_mt5(pc_now)
        with self._lock:
            self._next_check_utc = self._next_reconnect_pc
        return False

    def _poll_ticks(self, pc_now: datetime, model: OffsetModel) -> None:
        adapter = self.market.adapter
        assert adapter is not None
        for symbol in self.market.symbols:
            try:
                tick = adapter.tick(symbol)
            except Mt5Error as exc:
                if is_dev_mode():
                    logger.debug("live signals: tick %s failed: %s", symbol, exc)
                continue
            if tick is None:
                continue
            utc_epoch = int(model.server_to_utc([tick.server_time])[0])
            tick_utc = datetime.fromtimestamp(utc_epoch, tz=timezone.utc)
            with self._lock:
                self._ticks[symbol] = {"time_utc": _iso(tick_utc), "utc": tick_utc, "bid": tick.bid, "ask": tick.ask,
                                       "server_time": tick.server_time}
                hours = self._tick_hours.setdefault(symbol, deque(maxlen=48))
                hour = _floor_hour(tick_utc)
                if not hours or hours[-1] != hour:
                    hours.append(hour)
            age = (pc_now - tick_utc).total_seconds()
            prev = self._last_poll.get(symbol)
            self._last_poll[symbol] = (pc_now.timestamp(), tick.server_time)
            # A skew sample needs a MOVING market: a new tick since the previous poll, and that poll recent (so the
            # tick is at most ~one poll interval old). A stale tick (market closed) says nothing about the clocks.
            # A tick "from the future" (age < 0) is evidence of a PC clock running behind in any case.
            moving = prev is not None and tick.server_time != prev[1] and                 pc_now.timestamp() - prev[0] <= 2 * self.poll_s + 1
            if moving or age < 0:
                self._skew_samples.append((pc_now.timestamp(), age))
            if is_dev_mode():
                logger.debug("live signals: tick %s server=%d utc=%s bid=%s ask=%s (pc %s, age %.1f s)", symbol,
                             tick.server_time, _iso(tick_utc), tick.bid, tick.ask, _iso(pc_now),
                             (pc_now - tick_utc).total_seconds())
        cutoff = pc_now.timestamp() - SKEW_WINDOW_S
        while self._skew_samples and self._skew_samples[0][0] < cutoff:
            self._skew_samples.popleft()

    def _estimate_server_now(self, pc_now: datetime) -> datetime:
        skew = min(d for _, d in self._skew_samples) if self._skew_samples else None
        server_now = pc_now
        warned = False
        if skew is not None and skew < -self.skew_tolerance_s:  # PC behind: a tick is never in the future
            server_now = pc_now - timedelta(seconds=skew)
        elif skew is not None and skew > self.skew_tolerance_s and len(self._skew_samples) >= MIN_SKEW_SAMPLES:
            server_now = pc_now - timedelta(seconds=skew)  # PC ahead (consistently, over moving ticks)
            warned = True
        with self._lock:
            latest = max((t["utc"] for t in self._ticks.values()), default=None)
        if latest is not None and latest > server_now:
            server_now = latest  # a tick is never in the future
        if warned and not self._skew_warned:
            logger.warning("live signals: the PC clock is %.1f s AHEAD of the broker server; using server time "
                           "(%s) for closed bars", skew, _iso(server_now))
            self._event("tick", None, {"clock_skew_s": skew, "server_now": _iso(server_now), "pc_now": _iso(pc_now)})
        with self._lock:
            self._skew_s = skew
            self._skew_warned = warned
            self._server_now = server_now
        if is_dev_mode():
            logger.debug("live signals: pc=%s server=%s skew=%s s (tolerance %.0f s, %d samples)", _iso(pc_now),
                         _iso(server_now), None if skew is None else round(skew, 3), self.skew_tolerance_s,
                         len(self._skew_samples))
        return server_now

    # ------------------------------------------------------------------------------------------ expiry
    def _mark(self, row: dict[str, Any], status: str, **fields: Any) -> dict[str, Any] | None:
        new = self.repo.update(row["id"], status=status, **fields)
        if new is not None:
            self._publish({"type": status, "signal": signal_to_api(new)})
        return new

    def _expire_due(self, server_now: datetime) -> None:
        for row in self.repo.with_status("active"):
            if row["expires_utc"] is None:
                continue
            expires = parse_utc(row["expires_utc"])
            if expires <= server_now:
                self._mark(row, "expired")
                if is_dev_mode():
                    logger.debug("live signal %d %s %s expired at %s (server now %s)", row["id"], row["symbol"],
                                 row["direction"], _iso(expires), _iso(server_now))

    def _tick_expiries(self) -> None:
        """Provisional expiry of signals waiting for the reopen: the first tick at/after the decision time gives
        the entry bar (its hour); the boundary check corrects it from the cache."""
        for row in self.repo.with_status("active"):
            if row["expires_utc"] is not None:
                continue
            with self._lock:
                tick = self._ticks.get(row["symbol"])
            if tick is None:
                continue
            decision = parse_utc(row["decision_time_utc"])
            if tick["utc"] >= decision:
                entry_open = _floor_hour(tick["utc"])
                self.repo.update(row["id"], expires_utc=entry_open + H1)
                if is_dev_mode():
                    logger.debug("live signal %d %s: market reopened (tick %s) -> provisional expiry %s", row["id"],
                                 row["symbol"], tick["time_utc"], _iso(entry_open + H1))

    # ------------------------------------------------------------------------------------------ boundary check
    def _check_symbol(self, symbol: str, boundary: datetime, server_now: datetime, pc_now: datetime,
                      model: OffsetModel, settings: LiveSettings) -> str:
        """``"done"`` or ``"retry"`` (expected bar not cached yet, still within the tolerance)."""
        started = time.perf_counter()
        expected_open = boundary - H1
        within_tolerance = (server_now - boundary).total_seconds() < self.retry_tolerance_s
        info: dict[str, Any] = {"boundary": _iso(boundary), "bar": _iso(expected_open), "server_now": _iso(server_now),
                                "pc_now": _iso(pc_now), "clock_skew_s": self._skew_s}
        if is_dev_mode():
            logger.debug("live check %s: boundary %s (bar %s) server=%s pc=%s skew=%s", symbol, _iso(boundary),
                         _iso(expected_open), _iso(server_now), _iso(pc_now), self._skew_s)
        # a. incremental cache update
        try:
            updated = self._update_cache(symbol, boundary, server_now, model)
        except _UpdateRefused as exc:
            self._record_check(symbol, boundary, "error", UPDATE_REFUSED_FA[exc.code], info)
            self._error(UPDATE_REFUSED_FA[exc.code], symbol, None, code=exc.code)
            return "done"
        except (Mt5Error, OffsetError, CacheError, CacheLockTimeout, OSError, ValueError) as exc:
            self._error(UPDATE_FAILED_FA, symbol, exc)
            if within_tolerance and self.market.mt5_connected:
                return "retry"
            self._record_check(symbol, boundary, "update_failed", UPDATE_FAILED_FA, info)
            return "done"
        info["update"] = updated
        # b. completeness
        try:
            prepared = prepare_history(self.market.cache, symbol, self.lru)
        except HistoryUnavailable as exc:
            self._record_check(symbol, boundary, "error", exc.message_fa, info)
            self._error(exc.message_fa, symbol, None, code=exc.code)
            return "done"
        h1 = prepared.history.h1
        times_ns = prepared.bars.times_ns
        exp_ns = _ns(expected_open)
        t = int(np.searchsorted(times_ns, exp_ns))
        with self._lock:
            tick = self._ticks.get(symbol)
        tick_utc: datetime | None = None if tick is None else tick["utc"]
        if t >= len(times_ns) or int(times_ns[t]) != exp_ns:
            # Was the market trading during the expected bar's hour? (1) a tick seen there by the polls -> the bar
            # must come (retry); (2) else MT5 already has the NEXT bar forming -> that hour had no ticks (daily
            # break); (3) else no tick since before that hour -> closed (weekend, holiday).
            with self._lock:
                seen_in_hour = expected_open in self._tick_hours.get(symbol, ())
            forming_now = int((updated.get("H1") or {}).get("dropped_forming") or 0) > 0
            closed = not seen_in_hour and (forming_now or tick_utc is None or tick_utc < expected_open)
            if closed:
                if is_dev_mode():
                    logger.debug("live check %s: market closed during %s (last tick %s, next bar forming=%s, tick seen "
                                 "in that hour=%s), last cached bar %s", symbol, _iso(expected_open), _iso(tick_utc),
                                 forming_now, seen_in_hour, _iso(h1["time"].iloc[-1]) if len(h1) else None)
                self._record_check(symbol, boundary, "market_closed", MARKET_CLOSED_FA, info)
                return "done"
            if within_tolerance:
                if is_dev_mode():
                    logger.debug("live check %s: bar %s not cached yet (last %s); retry", symbol, _iso(expected_open),
                                 _iso(h1["time"].iloc[-1]) if len(h1) else None)
                return "retry"
            self._record_check(symbol, boundary, "incomplete", INCOMPLETE_FA, info)
            return "done"
        if not self._h4_complete(prepared, model, boundary, t):
            if within_tolerance:
                if is_dev_mode():
                    logger.debug("live check %s: H4 bar closed at %s not cached yet; retry", symbol, _iso(boundary))
                return "retry"
            self._record_check(symbol, boundary, "incomplete", INCOMPLETE_FA, info)
            return "done"
        # c. decision (the backtest's code path + the per-bar cross-check)
        try:
            outcome = self._decide(symbol, prepared, t, boundary, server_now, settings, tick, info)
        except PluginNotLive:
            self._record_check(symbol, boundary, "error", PLUGIN_NOT_LIVE_FA, info)
            self._error(PLUGIN_NOT_LIVE_FA, symbol)
            return "done"
        except (UnknownStrategyError, StrategyVersionMismatchError):
            message = f"سیستم انتخاب‌شده برای لایو («{settings.live_strategy}») پیدا نشد."
            self._record_check(symbol, boundary, "error", message, info)
            self._error(message, symbol)
            return "done"
        except StoredParamsInvalidError:
            message = "پارامترهای ذخیره‌شده سیستم لایو معتبر نیستند؛ پارامترها را دوباره ذخیره کنید."
            self._record_check(symbol, boundary, "error", message, info)
            self._error(message, symbol)
            return "done"
        except (HistoryUnavailable, StrategyContractError, ValueError) as exc:
            self._record_check(symbol, boundary, "error", ERROR_FA, info)
            self._error(ERROR_FA, symbol, exc)
            return "done"
        if is_dev_mode():
            logger.debug("live check %s %s done in %.3f s: %s", symbol, _iso(boundary), time.perf_counter() - started,
                         outcome)
        return "done"

    def _update_cache(self, symbol: str, boundary: datetime, server_now: datetime,
                      model: OffsetModel) -> dict[str, Any]:
        cache = self.market.cache
        out: dict[str, Any] = {}
        server_b = int(model.utc_to_server([int(boundary.timestamp())])[0])
        h4_closed_now = server_b % (4 * 3600) == 0
        for tf in (Timeframe.H1, Timeframe.H4):
            meta = cache.read_meta(symbol, tf)
            if meta is None or not meta.last_bar_utc:
                raise _UpdateRefused("no_cache")
            if meta.source != "mt5":
                raise _UpdateRefused("not_incremental")
            if meta.offset_model != model.label:
                raise _UpdateRefused("offset_model_changed")
            if tf is Timeframe.H4:
                last = pd.Timestamp(meta.last_bar_utc)
                behind = last + 2 * H4 <= pd.Timestamp(boundary)
                if not (h4_closed_now or behind):
                    out[tf.value] = {"skipped": True}
                    continue
            start = pd.Timestamp(meta.requested_start_utc or meta.first_bar_utc).tz_convert("UTC").to_pydatetime()
            result = self.market.update(symbol, tf, start=start, now_utc=server_now)
            if result.full:  # excluded by the checks above
                logger.warning("live signals: %s %s ran a FULL refetch unexpectedly", symbol, tf.value)
            out[tf.value] = {"bars_added": result.rows_added, "fetched": result.fetched,
                             "last_bar_utc": result.meta.last_bar_utc, "dropped_forming": result.dropped_forming}
            if is_dev_mode():
                logger.debug("live update %s %s: +%d bar(s) (fetched %d, forming dropped %d), last %s", symbol,
                             tf.value, result.rows_added, result.fetched, result.dropped_forming,
                             result.meta.last_bar_utc)
        return out

    @staticmethod
    def _h4_complete(prepared: PreparedHistory, model: OffsetModel, boundary: datetime, t: int) -> bool:
        """If an H4 bucket (server clock) ended at ``boundary`` and the market traded in it (H1 bars cached), its
        H4 bar must be cached too, else the strategy would decide with a stale channel."""
        server_b = int(model.utc_to_server([int(boundary.timestamp())])[0])
        if server_b % (4 * 3600) != 0:
            return True
        bucket_start = datetime.fromtimestamp(int(model.server_to_utc([server_b - 4 * 3600])[0]), tz=timezone.utc)
        h4_times = bar_open_times(prepared.history.h4)
        if len(h4_times) and h4_times[-1] >= pd.Timestamp(bucket_start):
            return True
        times_ns = prepared.bars.times_ns
        traded = bool(((times_ns >= _ns(bucket_start)) & (times_ns <= int(times_ns[t]))).any())
        return not traded

    def _record_check(self, symbol: str, boundary: datetime, result: str, reason_fa: str | None,
                      info: dict[str, Any], **extra: Any) -> None:
        with self._lock:
            self._checks[symbol] = {"boundary_utc": _iso(boundary), "result": result, "reason_fa": reason_fa,
                                    **{k: v for k, v in extra.items() if k in ("signal_id",)}}
        self._event("check", symbol, {**info, "result": result, "reason_fa": reason_fa, **extra})
        if is_dev_mode():
            logger.debug("live check %s %s -> %s%s", symbol, _iso(boundary), result,
                         f" ({reason_fa})" if reason_fa else "")

    def _resolve(self, settings: LiveSettings) -> ActiveStrategy:
        active = resolve_active(self.db, settings.live_strategy, registry=self.registry)
        if active.identity.source == "plugin":
            raise PluginNotLive(active.identity.label())
        return active

    def _decide(self, symbol: str, prepared: PreparedHistory, t: int, boundary: datetime, server_now: datetime,
                settings: LiveSettings, tick: dict[str, Any] | None, info: dict[str, Any]) -> str:
        active = self._resolve(settings)
        account = AccountSettingsRepo(self.db).get()
        history = prepared.history
        scan = scan_for(prepared, strategy=active.strategy, symbol=symbol, params=active.params,
                        params_hash=active.params_hash, account=account, lru=self.lru)
        scan_cand = scan.by_bar.get(t)
        ctx = build_context(symbol, history.h1, history.h4, db=self.db, registry_name=active.name,
                            now_utc=boundary, symbol_spec=history.spec, registry=self.registry)
        if pd.Timestamp(ctx.decision_time_utc) != pd.Timestamp(boundary):
            raise ValueError(f"context decision time {ctx.decision_time_utc} != boundary {boundary}")
        live_cand = evaluate_checked(active.strategy, ctx, active.params_hash)
        diff = candidate_diff(scan_cand, live_cand)
        info.update(strategy=active.identity.label(), params_version=active.record_version,
                    params_hash=active.params_hash[:12], scan_candidate=scan_cand is not None,
                    live_candidate=live_cand is not None)
        if is_dev_mode():
            logger.debug("live decide %s @ %s: %s params v%d; scan=%s evaluate=%s diff=%s; inputs=%s", symbol,
                         _iso(boundary), active.identity.label(), active.record_version,
                         None if scan_cand is None else f"{scan_cand.direction} {scan_cand.setup_slug}",
                         None if live_cand is None else f"{live_cand.direction} {live_cand.setup_slug}", diff,
                         (scan_cand or live_cand).extra if (scan_cand or live_cand) is not None else None)
        self._revise(symbol, prepared, scan.by_bar, active, t, server_now)
        if scan_cand is None and live_cand is None:
            self._record_check(symbol, boundary, "no_setup", None, info)
            return "no_setup"
        if diff:
            self._store_mismatch(symbol, active, account, scan_cand, live_cand, diff, boundary, info)
            return "mismatch"
        assert scan_cand is not None
        return self._emit(symbol, prepared, scan.by_bar, t, scan_cand, active, account, tick, server_now, info)

    def _base_row(self, symbol: str, active: ActiveStrategy, account: AccountSettings,
                  cand: SignalCandidate) -> dict[str, Any]:
        ident = active.identity
        return {
            "symbol": symbol, "strategy_name": ident.name, "strategy_version": ident.version,
            "strategy_sha256": ident.sha256, "strategy_source": ident.source, "params_version": active.record_version,
            "params_hash": active.params_hash, "confirmation_bar_open_utc": cand.confirmation_bar_open_utc,
            "decision_time_utc": cand.decision_time_utc, "direction": cand.direction, "setup": cand.setup_slug,
            "setup_title_fa": setup_title_fa(cand), "pattern": cand.pattern, "line": cand.line,
            "reference_price": cand.reference_price, "stop_loss": cand.stop_loss, "rr": cand.rr,
            "reason_fa": cand.reason_fa, "account_json": account.model_dump(),
        }

    def _store_mismatch(self, symbol: str, active: ActiveStrategy, account: AccountSettings,
                        scan_cand: SignalCandidate | None, live_cand: SignalCandidate | None, diff: list[str],
                        boundary: datetime, info: dict[str, Any]) -> None:
        cand = scan_cand or live_cand
        assert cand is not None
        mismatch = {"scan": candidate_dump(scan_cand), "evaluate": candidate_dump(live_cand), "fields": diff,
                    "boundary_utc": _iso(boundary)}
        row = {**self._base_row(symbol, active, account, cand), "status": "rejected",
               "rejection_reason_fa": MISMATCH_FA, "mismatch_json": mismatch,
               "extra_json": {"candidate": candidate_dump(cand)}}
        signal_id = self.repo.insert(row)
        logger.warning("live signals %s @ %s: BACKTEST-VS-LIVE MISMATCH (fields %s); no signal emitted", symbol,
                       _iso(boundary), diff)
        self._event("mismatch", symbol, {**info, "fields": diff, "signal_id": signal_id})
        self._record_check(symbol, boundary, "mismatch", MISMATCH_FA, info, signal_id=signal_id)

    def _chain_start(self, symbol: str, active: ActiveStrategy, times_ns: np.ndarray, t: int) -> int:
        """First bar of the backtest-skip chain: the earliest agreed signal of this identity/symbol within
        ``CHAIN_LOOKBACK`` (the backtest from the first live signal), else ``t`` itself (flat)."""
        since = pd.Timestamp(int(times_ns[t]), tz="UTC").to_pydatetime() - CHAIN_LOOKBACK
        rows = self.repo.for_identity(symbol, active.identity.name, active.identity.version, active.params_hash,
                                      since=since, statuses=("active", "expired", "rejected"))
        agreed = [r for r in rows if r["mismatch_json"] is None]
        if not agreed:
            return t
        first = _ns(parse_utc(agreed[0]["confirmation_bar_open_utc"]))
        return min(t, int(np.searchsorted(times_ns, first)))

    def _emit(self, symbol: str, prepared: PreparedHistory, by_bar: dict[int, SignalCandidate], t: int,
              cand: SignalCandidate, active: ActiveStrategy, account: AccountSettings, tick: dict[str, Any] | None,
              server_now: datetime, info: dict[str, Any]) -> str:
        existing = self.repo.find(active.identity.name, active.identity.version, active.params_hash, symbol,
                                  cand.confirmation_bar_open_utc)
        if existing is not None:
            if is_dev_mode():
                logger.debug("live signals %s @ %s: dedupe -> signal %d already stored (%s)", symbol,
                             _iso(cand.decision_time_utc), existing["id"], existing["status"])
            self._record_check(symbol, cand.decision_time_utc, "duplicate", None, info, signal_id=existing["id"])
            return "duplicate"
        history = prepared.history
        bars = prepared.bars
        spec = history.spec
        tick_utc = None if tick is None else tick["utc"]
        fresh = tick_utc is not None and tick_utc >= cand.decision_time_utc
        spr_price = float(bars.spread.price[t])
        spr_pts = int(bars.spread.points[t])
        levels = indicative_levels(cand, tick_bid=None if tick is None else tick["bid"],
                                   tick_ask=None if tick is None else tick["ask"], tick_fresh=fresh,
                                   last_close=float(bars.close[t]), last_spread_price=spr_price,
                                   last_spread_points=spr_pts, spec=spec, account=account)
        step = entry_step_info(bars.times_ns, t, tick_utc=tick_utc)
        flag = backtest_skip_flag(bars, by_bar, start_index=self._chain_start(symbol, active, bars.times_ns, t), t=t,
                                  spec=spec, account=account)
        sizing = levels.sizing
        extra = {
            "candidate": candidate_dump(cand),
            "entry_gap": step.to_json(),
            "entry_bar_time": step.to_json()["entry_bar_time"],
            "indicative": {"entry": levels.entry, "source": levels.entry_source, "tick_time_utc": _iso(tick_utc),
                           "tick_bid": None if tick is None else tick["bid"],
                           "tick_ask": None if tick is None else tick["ask"], "last_close": float(bars.close[t]),
                           "last_spread_points": spr_pts, "server_now": _iso(server_now)},
            "virtual": {"chain_start": _iso(flag.chain_start), "chain_candidates": flag.chain_candidates,
                        "detail": flag.detail},
            "symbol_spec": {"digits": spec.digits, "point": spec.point},
        }
        status = "active" if levels.accepted else "rejected"
        row = {
            **self._base_row(symbol, active, account, cand), "status": status, "expires_utc": step.expires,
            "indicative_entry": levels.entry, "entry_source": levels.entry_source,
            "stop_loss": levels.stop_loss, "take_profit_indicative": levels.take_profit,
            "volume": sizing.volume if sizing is not None and sizing.accepted else None,
            "risk_amount": sizing.actual_risk if sizing is not None and sizing.accepted else None,
            "sizing_json": None if sizing is None else sizing.model_dump(),
            "rejection_reason_fa": levels.rejection_reason_fa, "backtest_would_skip": flag.would_skip,
            "backtest_skip_reason_fa": flag.reason_fa, "extra_json": extra,
            "gap_class_provisional": step.provisional,
        }
        signal_id = self.repo.insert(row)
        if signal_id is None:  # lost a race with another writer of the same key
            self._record_check(symbol, cand.decision_time_utc, "duplicate", None, info)
            return "duplicate"
        stored = self.repo.get(signal_id)
        assert stored is not None
        if status == "active":
            self._publish({"type": "signal", "signal": signal_to_api(stored)})
        self._record_check(symbol, cand.decision_time_utc, "signal" if status == "active" else "rejected",
                           levels.rejection_reason_fa, info, signal_id=signal_id)
        if is_dev_mode():
            logger.debug("live SIGNAL %d %s %s %s @ %s: entry %.5f (%s) SL %.5f TP~%s vol=%s risk=%s expires=%s "
                         "backtest_would_skip=%s gap=%s/%s status=%s", signal_id, symbol, cand.direction,
                         cand.setup_slug, _iso(cand.decision_time_utc), levels.entry, levels.entry_source,
                         levels.stop_loss, levels.take_profit, row["volume"], row["risk_amount"], _iso(step.expires),
                         flag.would_skip, step.kind, "provisional" if step.provisional else "final", status)
        return "signal" if status == "active" else "rejected"

    # ------------------------------------------------------------------------------------------ revision
    def _revise(self, symbol: str, prepared: PreparedHistory, by_bar: dict[int, SignalCandidate],
                active: ActiveStrategy, t: int, server_now: datetime) -> None:
        """Superseded detection (broker revisions) + entry bar / expiry / entry-step class of earlier signals."""
        times_ns = prepared.bars.times_ns
        since = pd.Timestamp(int(times_ns[max(0, t - REVISION_BARS)]), tz="UTC").to_pydatetime()
        rows = self.repo.for_identity(symbol, active.identity.name, active.identity.version, active.params_hash,
                                      since=since, statuses=("active", "expired"))
        for row in rows:
            conf = parse_utc(row["confirmation_bar_open_utc"])
            if conf >= pd.Timestamp(int(times_ns[t]), tz="UTC"):
                continue  # the bar being decided now
            idx = int(np.searchsorted(times_ns, _ns(conf)))
            located = idx < len(times_ns) and int(times_ns[idx]) == _ns(conf)
            fresh = by_bar.get(idx) if located else None
            stored = (_json(row["extra_json"]) or {}).get("candidate")
            # rr / indicative TP follow the account settings (a settings change is not a broker revision)
            diff = [f for f in candidate_diff(stored, fresh) if f not in REVISION_IGNORED]
            if diff:
                extra = _json(row["extra_json"]) or {}
                extra["superseded"] = {"reason_fa": SUPERSEDED_FA, "fields": diff, "fresh": candidate_dump(fresh),
                                       "bar_cached": located, "detected_utc": _iso(server_now)}
                self._mark(row, "superseded", extra_json=extra)
                self._event("check", symbol, {"result": "superseded", "signal_id": row["id"], "fields": diff})
                logger.warning("live signal %d %s @ %s superseded after a broker revision (fields %s)", row["id"],
                               symbol, row["confirmation_bar_open_utc"], diff)
        # entry bar known now? (signals whose entry step was unknown / provisional at decision time; active or
        # already expired -- the expiry may have come from the tick estimate first)
        recent = pd.Timestamp(int(times_ns[t]), tz="UTC").to_pydatetime() - ENTRY_REVISION_WINDOW
        for row in self.repo.for_identity(symbol, active.identity.name, active.identity.version, active.params_hash,
                                          since=recent, statuses=("active", "expired")):
            extra = _json(row["extra_json"]) or {}
            gap = extra.get("entry_gap") or {}
            if not gap.get("provisional") and row["expires_utc"] is not None:
                continue
            conf_ns = _ns(parse_utc(row["confirmation_bar_open_utc"]))
            idx = int(np.searchsorted(times_ns, conf_ns))
            if idx >= len(times_ns) or int(times_ns[idx]) != conf_ns or idx + 1 >= len(times_ns):
                continue
            step = entry_step_info(times_ns, idx, tick_utc=None)
            if gap == step.to_json() and row["expires_utc"] is not None:
                continue  # nothing new
            extra["entry_gap"] = step.to_json()
            extra["entry_bar_time"] = step.to_json()["entry_bar_time"]
            fields: dict[str, Any] = {"expires_utc": step.expires, "gap_class_provisional": step.provisional,
                                      "extra_json": extra}
            skip_reason = row["backtest_skip_reason_fa"] or ""
            if step.kind == "missing" and not row["backtest_would_skip"]:
                fields.update(backtest_would_skip=True, backtest_skip_reason_fa=MISSING_GAP_SKIP_FA)
            elif step.kind != "missing" and skip_reason == MISSING_GAP_SKIP_FA:
                fields.update(backtest_would_skip=False, backtest_skip_reason_fa=None)
            self.repo.update(row["id"], **fields)
            if is_dev_mode():
                logger.debug("live signal %d %s: entry bar %s known (step %s, provisional=%s) -> expires %s", row["id"],
                             symbol, step.to_json()["entry_bar_time"], step.kind, step.provisional, _iso(step.expires))
        self._expire_due(server_now)

    # ------------------------------------------------------------------------------------------ housekeeping
    def _prune(self, server_now: datetime) -> None:
        if self._last_prune is not None and server_now - self._last_prune < timedelta(hours=6):
            return
        self._last_prune = server_now
        try:
            removed = self.repo.prune_events(server_now - EVENT_RETENTION)
        except Exception as exc:
            logger.warning("live signals: event pruning failed (%s)", type(exc).__name__)
            return
        if is_dev_mode() and removed:
            logger.debug("live signals: pruned %d event(s) older than %s", removed, EVENT_RETENTION)


def _json(text: str | None) -> Any:
    import json

    return None if text is None else json.loads(text)


__all__ = ["LiveSignals", "PLUGIN_NOT_LIVE_FA", "PluginNotLive", "api_time"]
