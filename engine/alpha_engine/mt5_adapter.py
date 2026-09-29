"""The only module that talks to the MetaTrader 5 terminal -- strictly READ-ONLY.

Safety (``.claude/rules/03_trading_safety.md`` section 1):

* Only these package functions are ever called: ``initialize``, ``login`` (never used directly: the
  password path goes through ``initialize(login=..., password=..., server=...)``), ``shutdown``,
  ``last_error``, ``version``, ``terminal_info``, ``account_info``, ``symbol_info``, ``symbol_select``
  (makes a symbol visible in Market Watch so its data can be read), ``symbol_info_tick``,
  ``copy_rates_range``, ``copy_rates_from_pos``. :data:`ALLOWED_MT5_FUNCTIONS` enforces this at runtime:
  :meth:`Mt5Adapter._call` refuses anything else.
* ``MetaTrader5`` is imported lazily (first :meth:`connect`), and can be injected (tests use a fake).
* The package is process-global and not thread-safe: every call is serialized by :data:`MT5_LOCK`.
* Default = **attach** mode: ``initialize(path=MT5_TERMINAL_PATH)`` with no credentials, reusing the
  terminal's own logged-in session. Only if ``MT5_PASSWORD`` is set does it pass
  ``login/password/server`` (prefer the investor password). The password is never logged, never put in
  the status, and never in ``repr``.
* After connecting, ``account_info()`` must match ``MT5_LOGIN``/``MT5_SERVER`` when those are set
  (decision D3): otherwise state ``account_mismatch``, ``shutdown()``, and every data call is refused.

Time: MT5 bar/tick times are *server* clock epochs. :meth:`fetch_rates` converts them to UTC with the
given :class:`~alpha_engine.data.timezone.OffsetModel` before anything leaves this module, and drops
the bar that is still forming.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd

from .app import Mt5Status
from .config import Settings, mask_login
from .data.schema import COLUMNS, Timeframe, empty_frame, normalize_frame, sort_dedupe, to_epoch_seconds
from .data.symbols import SymbolSpec, validate_symbol_name
from .data.timezone import LiveOffset, OffsetModel, estimate_live_offset, server_frame_to_utc
from .logging_setup import get_logger, is_dev_mode

logger = get_logger(__name__)

MT5_LOCK = threading.Lock()  # one lock for the whole process: the MetaTrader5 package is global state

ALLOWED_MT5_FUNCTIONS = frozenset({
    "initialize", "login", "shutdown", "last_error", "version", "terminal_info", "account_info",
    "symbol_info", "symbol_select", "symbol_info_tick", "copy_rates_range", "copy_rates_from_pos",
})

TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}  # ACCOUNT_TRADE_MODE_DEMO/CONTEST/REAL
_TIMEFRAME_CONSTANTS = {Timeframe.H1: ("TIMEFRAME_H1", 16385), Timeframe.H4: ("TIMEFRAME_H4", 16388)}
INIT_TIMEOUT_MS = 15_000  # mt5.initialize waits up to 60 s by default when the terminal is unreachable
RECONNECT_TIMEOUT_MS = 5_000  # on-demand reconnects run inside an HTTP request: keep them short
CHUNK_DAYS = {Timeframe.H1: 180, Timeframe.H4: 365}
DEFAULT_YEARS = 5


class Mt5Error(RuntimeError):
    pass


class Mt5NotConnectedError(Mt5Error):
    pass


class UnknownSymbolError(Mt5Error, LookupError):
    pass


class Mt5DataError(Mt5Error):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Tick:
    """Last tick of a symbol as the terminal reports it: ``server_time`` is a SERVER-clock epoch (convert with the
    offset model), ``bid``/``ask`` are prices (0.0 when the broker sent none)."""

    symbol: str
    server_time: int
    bid: float
    ask: float
    server_time_msc: int = 0


@dataclass
class FetchResult:
    """Outcome of a (chunked) rates fetch. ``frame`` is canonical UTC (or raw server epochs for
    :meth:`Mt5Adapter.fetch_raw`)."""

    frame: pd.DataFrame
    chunks: int = 0
    empty_chunks: int = 0
    failed_chunks: int = 0
    errors: list[str] = field(default_factory=list)
    dropped_forming: int = 0
    first_available_utc: pd.Timestamp | None = None

    @property
    def rows(self) -> int:
        return len(self.frame)


def _last_error_text(err: Any) -> str:
    if isinstance(err, tuple) and len(err) >= 2:
        return f"{err[0]} {err[1]}"
    return str(err)


class Mt5Adapter:
    """Read-only facade over the ``MetaTrader5`` package (see module docstring)."""

    def __init__(
        self,
        settings: Settings,
        mt5_module: ModuleType | Any | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utc_now,
        attempts: int = 3,
        backoff: Sequence[float] = (1.0, 2.0, 4.0),
    ) -> None:
        self._settings = settings
        self._mt5 = mt5_module
        self._sleep = sleep
        self._clock = clock
        self._attempts = max(1, attempts)
        self._backoff = tuple(backoff)
        self._status = Mt5Status(state="not_initialized")
        self._status_lock = threading.Lock()
        self._connect_thread: threading.Thread | None = None
        self._last_attempt: float | None = None

    def __repr__(self) -> str:
        s = self.status()
        return f"Mt5Adapter(state={s.state!r}, server={s.server!r}, login={s.login_masked!r})"

    # --- plumbing ---------------------------------------------------------------------------------

    def _module(self) -> Any:
        if self._mt5 is None:
            import MetaTrader5  # lazy: only when a connection is actually wanted

            self._mt5 = MetaTrader5
        return self._mt5

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in ALLOWED_MT5_FUNCTIONS:
            raise Mt5Error(f"MT5 function {name!r} is not on the read-only allow-list")
        module = self._module()
        with MT5_LOCK:
            return getattr(module, name)(*args, **kwargs)

    def _set_status(self, **fields: Any) -> Mt5Status:
        with self._status_lock:
            self._status = Mt5Status(**fields)
            return self._status

    def status(self) -> Mt5Status:
        """Snapshot for ``/health`` (``app.state.mt5_status_provider``). Never contains secrets."""
        with self._status_lock:
            return self._status.model_copy()

    @property
    def clock(self) -> Callable[[], datetime]:
        """The adapter's notion of "now" (UTC); shared with the service so both agree."""
        return self._clock

    @property
    def connected(self) -> bool:
        return self.status().state == "connected"

    def require_connected(self) -> None:
        status = self.status()
        if status.state != "connected":
            raise Mt5NotConnectedError(f"MT5 is not connected (state={status.state})")

    def _timeframe_constant(self, tf: Timeframe) -> int:
        name, fallback = _TIMEFRAME_CONSTANTS[tf]
        return int(getattr(self._module(), name, fallback))

    # --- connection -------------------------------------------------------------------------------

    def connect(self, timeout_ms: int = INIT_TIMEOUT_MS) -> Mt5Status:
        """Attach (or log in, if a password is configured), with retries and account verification."""
        settings = self._settings
        self._last_attempt = time.monotonic()
        kwargs: dict[str, Any] = {}
        if settings.mt5_terminal_path:
            kwargs["path"] = settings.mt5_terminal_path
        use_login = settings.mt5_password is not None
        mode = "login" if use_login else "attach"
        if use_login:
            login_text = (settings.mt5_login or "").strip()
            if not login_text.isdigit():
                logger.warning("MT5_PASSWORD is set but MT5_LOGIN is missing or not numeric; not connecting")
                return self._set_status(state="error", message="MT5_PASSWORD is set but MT5_LOGIN is missing/invalid")
            kwargs["login"] = int(login_text)
            kwargs["password"] = settings.mt5_password.get_secret_value()
            if settings.mt5_server:
                kwargs["server"] = settings.mt5_server
        kwargs["timeout"] = int(timeout_ms)

        try:
            self._module()
        except ImportError:
            logger.warning("MetaTrader5 package is not installed")
            return self._set_status(state="error", message="MetaTrader5 package is not installed")

        last_error = ""
        for attempt in range(1, self._attempts + 1):
            if is_dev_mode():
                logger.debug("mt5 connect attempt %d/%d mode=%s path_set=%s login=%s server=%s",
                             attempt, self._attempts, mode, "path" in kwargs,
                             mask_login(settings.mt5_login) if use_login else None,
                             settings.mt5_server if use_login else None)
            try:
                ok = bool(self._call("initialize", **kwargs))
            except Exception as exc:  # the package can raise on odd inputs; never echo kwargs
                ok = False
                last_error = type(exc).__name__
                if is_dev_mode():
                    logger.debug("mt5 initialize raised %s", type(exc).__name__)
            if ok:
                break
            try:
                last_error = _last_error_text(self._call("last_error"))
            except Exception:
                pass
            if is_dev_mode():
                logger.debug("mt5 initialize failed (attempt %d): %s", attempt, last_error)
            try:
                self._call("shutdown")
            except Exception:
                pass
            if attempt < self._attempts:
                delay = self._backoff[min(attempt - 1, len(self._backoff) - 1)] if self._backoff else 0
                self._set_status(state="disconnected",
                                 message=f"connect attempt {attempt} failed; retrying in {delay:g}s")
                self._sleep(delay)
        else:
            logger.warning("MT5 connection failed after %d attempts: %s", self._attempts, last_error)
            return self._set_status(state="error",
                                    message=f"initialize failed after {self._attempts} attempts: {last_error}")

        return self._verify_account(mode)

    def _verify_account(self, mode: str) -> Mt5Status:
        settings = self._settings
        info = self._call("account_info")
        if info is None:
            err = _last_error_text(self._call("last_error"))
            self._safe_shutdown()
            logger.warning("MT5 account_info unavailable (terminal not logged in?): %s", err)
            return self._set_status(state="error", message=f"account_info unavailable: {err}")
        login = getattr(info, "login", None)
        server = getattr(info, "server", None)
        trade_mode = TRADE_MODES.get(getattr(info, "trade_mode", None), "unknown")
        expected_login = (settings.mt5_login or "").strip() or None
        expected_server = (settings.mt5_server or "").strip() or None

        problems: list[str] = []
        if expected_login is None:
            logger.warning("MT5_LOGIN is not set: skipping the connected-account check")
        elif str(login) != expected_login:
            problems.append("login")
        if expected_server is not None and server != expected_server:
            problems.append("server")
        if problems:
            self._safe_shutdown()
            logger.warning("MT5 account mismatch (%s): connected %s@%s, expected %s@%s; disconnected",
                           "/".join(problems), mask_login(login), server, mask_login(expected_login),
                           expected_server)
            return self._set_status(
                state="account_mismatch", server=server, login_masked=mask_login(login), trade_mode=trade_mode,
                message=f"connected account does not match MT5_{'/MT5_'.join(p.upper() for p in problems)}; "
                        "disconnected, serving cache only",
            )
        status = self._set_status(
            state="connected", server=server, login_masked=mask_login(login), trade_mode=trade_mode,
            message="attached to the running terminal" if mode == "attach" else "logged in with configured account",
        )
        if is_dev_mode():
            try:
                version = self._call("version")
            except Exception:
                version = None
            logger.debug("mt5 connected: server=%s login=%s trade_mode=%s mode=%s version=%s",
                         server, mask_login(login), trade_mode, mode, version)
        return status

    def connect_in_background(self) -> threading.Thread:
        """Non-blocking connect (used on app startup)."""
        if self._connect_thread is not None and self._connect_thread.is_alive():
            return self._connect_thread
        thread = threading.Thread(target=self._connect_quietly, name="mt5-connect", daemon=True)
        self._connect_thread = thread
        thread.start()
        return thread

    def _connect_quietly(self) -> None:
        try:
            self.connect()
        except Exception as exc:
            if is_dev_mode():
                logger.debug("background mt5 connect crashed", exc_info=True)
            self._set_status(state="error", message=f"connect crashed: {type(exc).__name__}")

    def ensure_connected(self, min_interval: float = 60.0) -> bool:
        """One quick reconnect attempt (no retries) if disconnected and the last try is old enough.
        Never reconnects after an account mismatch."""
        state = self.status().state
        if state == "connected":
            return True
        if state == "account_mismatch":
            return False  # D3: never silently re-attach to the wrong account
        if self._mt5 is None and not self._settings.engine_mt5_autoconnect:
            return False  # autoconnect disabled and never connected: do not import/attach on demand
        if self._connect_thread is not None and self._connect_thread.is_alive():
            return False
        if self._last_attempt is not None and time.monotonic() - self._last_attempt < min_interval:
            return False
        attempts, self._attempts = self._attempts, 1
        try:
            return self.connect(timeout_ms=RECONNECT_TIMEOUT_MS).state == "connected"
        finally:
            self._attempts = attempts

    def _safe_shutdown(self) -> None:
        try:
            self._call("shutdown")
        except Exception:
            if is_dev_mode():
                logger.debug("mt5 shutdown raised", exc_info=True)

    def shutdown(self, join_timeout: float = 2.0) -> None:
        thread = self._connect_thread
        if thread is not None and thread.is_alive():
            thread.join(join_timeout)
        if self._mt5 is not None and self.status().state in ("connected", "disconnected"):
            self._safe_shutdown()
            if is_dev_mode():
                logger.debug("mt5 shut down")
        state = self.status()
        if state.state == "connected":
            self._set_status(state="disconnected", server=state.server, login_masked=state.login_masked,
                             trade_mode=state.trade_mode, message="engine shut down")

    # --- symbols ----------------------------------------------------------------------------------

    def symbol_spec(self, name: str) -> SymbolSpec:
        symbol = validate_symbol_name(name)
        self.require_connected()
        info = self._call("symbol_info", symbol)
        if info is None:
            raise UnknownSymbolError(f"symbol {symbol!r} is not available on this broker")
        if not getattr(info, "visible", True):
            if not self._call("symbol_select", symbol, True):
                raise Mt5DataError(f"could not make {symbol!r} visible: "
                                   f"{_last_error_text(self._call('last_error'))}")
            info = self._call("symbol_info", symbol)
            if info is None:
                raise UnknownSymbolError(f"symbol {symbol!r} is not available on this broker")
        spec = SymbolSpec.from_mt5(info)
        if is_dev_mode():
            logger.debug("symbol spec %s: digits=%d point=%g contract=%g tick_value=%g tick_size=%g vol=%g/%g/%g",
                         spec.name, spec.digits, spec.point, spec.trade_contract_size, spec.trade_tick_value,
                         spec.trade_tick_size, spec.volume_min, spec.volume_step, spec.volume_max)
        return spec

    def last_tick_time(self, symbol: str) -> int | None:
        """Server-clock epoch of the last tick, or None."""
        self.require_connected()
        tick = self._call("symbol_info_tick", validate_symbol_name(symbol))
        if tick is None:
            return None
        value = int(getattr(tick, "time", 0) or 0)
        return value or None

    def live_offset(self, symbol: str = "XAUUSD.x") -> LiveOffset:
        return estimate_live_offset(self.last_tick_time(symbol), self._clock())

    def tick(self, symbol: str) -> Tick | None:
        """Last tick of ``symbol`` (read-only ``symbol_info_tick``): server-clock epoch, bid, ask; ``None`` when the
        terminal has no tick for it. Used by the live signals for the server-time boundary and the indicative
        entry (``signals/``); never for anything that trades."""
        self.require_connected()
        raw = self._call("symbol_info_tick", validate_symbol_name(symbol))
        if raw is None:
            return None
        server_time = int(getattr(raw, "time", 0) or 0)
        if server_time <= 0:
            return None
        msc = int(getattr(raw, "time_msc", 0) or 0)
        out = Tick(symbol=symbol, server_time=server_time, bid=float(getattr(raw, "bid", 0.0) or 0.0),
                   ask=float(getattr(raw, "ask", 0.0) or 0.0), server_time_msc=msc or server_time * 1000)
        if is_dev_mode():
            logger.debug("tick %s: server_time=%d bid=%s ask=%s", symbol, out.server_time, out.bid, out.ask)
        return out

    # --- rates ------------------------------------------------------------------------------------

    def copy_rates_raw(self, symbol: str, timeframe: Timeframe | str, server_from: int, server_to: int) -> pd.DataFrame | None:
        """One ``copy_rates_range`` call. Returns canonical columns with int server-epoch ``time``,
        an empty frame for "no bars", or None if the call failed."""
        tf = Timeframe.parse(timeframe)
        self.require_connected()
        rates = self._call("copy_rates_range", validate_symbol_name(symbol), self._timeframe_constant(tf),
                           int(server_from), int(server_to))
        if rates is None:
            return None
        if len(rates) == 0:
            return pd.DataFrame({c: pd.Series(dtype="int64" if c == "time" else "float64") for c in COLUMNS})
        frame = pd.DataFrame({c: np.asarray(rates[c]) for c in COLUMNS})
        frame["time"] = frame["time"].astype("int64")
        for col in ("tick_volume", "spread", "real_volume"):
            frame[col] = frame[col].astype("int64")
        return frame

    def fetch_raw(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        server_from: int,
        server_to: int,
        chunk_days: int | None = None,
    ) -> FetchResult:
        """Chunked ``copy_rates_range`` over ``[server_from, server_to]`` (server epochs).

        Chunks are ``[a, b - 1]`` except the last (``[a, server_to]``); results are concatenated and
        deduplicated by time, so an inclusive/exclusive boundary difference can neither duplicate nor
        lose a bar. Empty chunks (weekends, holidays, before the history start) are normal.
        """
        tf = Timeframe.parse(timeframe)
        span = int(timedelta(days=chunk_days or CHUNK_DAYS[tf]).total_seconds())
        parts: list[pd.DataFrame] = []
        result = FetchResult(frame=pd.DataFrame())
        a = int(server_from)
        while a <= server_to:
            b = min(a + span, int(server_to))
            upper = b - 1 if b < server_to else b
            chunk = self.copy_rates_raw(symbol, tf, a, upper)
            result.chunks += 1
            if chunk is None:
                result.failed_chunks += 1
                result.errors.append(_last_error_text(self._call("last_error")))
            elif chunk.empty:
                result.empty_chunks += 1
            else:
                parts.append(chunk)
            if is_dev_mode():
                logger.debug("copy_rates_range %s %s chunk %d [%d..%d]: %s", symbol, tf.value, result.chunks, a,
                             upper, "failed" if chunk is None else f"{len(chunk)} bars")
            if b >= server_to:
                break
            a = b
        if result.chunks and result.failed_chunks == result.chunks:
            raise Mt5DataError(f"copy_rates_range failed for {symbol} {tf.value}: {result.errors[-1]}")
        if parts:
            frame = pd.concat(parts, ignore_index=True)
            frame = frame.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)
        else:
            frame = pd.DataFrame({c: pd.Series(dtype="int64" if c == "time" else "float64") for c in COLUMNS})
        result.frame = frame
        return result

    def fetch_rates(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        start_utc: datetime,
        end_utc: datetime | None,
        model: OffsetModel,
        *,
        chunk_days: int | None = None,
        now_utc: datetime | None = None,
    ) -> FetchResult:
        """Closed bars with UTC open time in ``[start_utc, end_utc]`` (``end_utc`` None = now).

        The request is made in server time (``model.utc_to_server``) padded by one day on both sides,
        converted to UTC, trimmed to the range, and the still-forming bar (``open + timeframe > now``)
        is dropped. ``now_utc`` replaces the adapter clock (the PC clock) for that "now": the live signals pass
        the broker's server time (estimated from its ticks) so a PC clock that runs ahead of the server can
        never make a still-forming bar look closed (phase 6, decision 4).
        """
        tf = Timeframe.parse(timeframe)
        now = now_utc if now_utc is not None else self._clock()
        end = min(end_utc or now, now)
        start_epoch = int(pd.Timestamp(start_utc).timestamp())
        end_epoch = int(pd.Timestamp(end).timestamp())
        if end_epoch < start_epoch:
            return FetchResult(frame=empty_frame())
        server_from = int(model.utc_to_server([start_epoch])[0]) - 86400
        server_to = int(model.utc_to_server([end_epoch])[0]) + 86400
        result = self.fetch_raw(symbol, tf, server_from, server_to, chunk_days)
        raw = result.frame
        if raw.empty:
            result.frame = empty_frame()
            return result
        utc = server_frame_to_utc(raw, model)
        utc = sort_dedupe(normalize_frame(utc))
        epochs = to_epoch_seconds(utc["time"])
        result.first_available_utc = utc["time"].iloc[0]
        forming = epochs + tf.seconds > int(now.timestamp())
        result.dropped_forming = int(forming.sum())
        keep = (~forming) & (epochs >= start_epoch) & (epochs <= end_epoch)
        result.frame = utc.loc[keep].reset_index(drop=True)
        if is_dev_mode():
            first = result.frame["time"].iloc[0] if len(result.frame) else None
            last = result.frame["time"].iloc[-1] if len(result.frame) else None
            logger.debug("fetched %s %s: %d bars %s..%s (chunks=%d empty=%d failed=%d dropped_forming=%d, %s)",
                         symbol, tf.value, len(result.frame), first, last, result.chunks, result.empty_chunks,
                         result.failed_chunks, result.dropped_forming, model.label)
        return result


def history_start_for(years: float, now: datetime) -> datetime:
    """``now - years`` (365.25-day years), floored to the hour."""
    start = now - timedelta(days=365.25 * years)
    return start.replace(minute=0, second=0, microsecond=0)

