"""Local OHLCV cache (decision D6).

Layout under ``<data_dir>/cache/``::

    ohlcv/<SYMBOL>_<TF>.parquet      canonical UTC frame + key-value metadata (b"alpha_trader" -> JSON)
    ohlcv/<SYMBOL>_<TF>.parquet.lock single-writer lockfile (exists only while a writer holds it)
    symbols/<SYMBOL>.json            last known SymbolSpec (+ source, fetched_at_utc)
    offset_fit.json                  last broker-offset fit (see alpha_engine.data.timezone)

* Writes are atomic: temp file in the same directory, then ``os.replace``. A crash can never leave a
  half-written Parquet file behind.
* One writer at a time per series: ``os.open(O_CREAT | O_EXCL)`` lockfile holding ``{"pid", "created"}``.
  A lock whose owner process is gone, or that is older than ``stale_after`` seconds, is broken.
  Waiting longer than ``timeout`` raises :class:`CacheLockTimeout`. Readers never take the lock
  (they always see either the old or the new complete file).
* Incremental merge: ``merge_frames(old, new)`` -- union by bar time, the NEWER fetch wins on overlap.
  Nothing is ever forward-filled.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

from ..logging_setup import get_logger, is_dev_mode
from .schema import SCHEMA_VERSION, Timeframe, empty_frame, iso_z, normalize_frame, sort_dedupe, validate_frame
from .symbols import SymbolSpec, validate_symbol_name
from .timezone import OffsetFit

logger = get_logger(__name__)

META_KEY = b"alpha_trader"
CacheSource = Literal["mt5", "seed"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


class CacheError(RuntimeError):
    pass


class CacheLockTimeout(CacheError, TimeoutError):
    pass


class CacheMeta(BaseModel):
    """Key-value metadata stored inside each Parquet file."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    symbol: str
    timeframe: str
    source: CacheSource
    offset_model: str | None = None
    first_available_utc: str | None = None  # earliest bar the broker returned (history start)
    requested_start_utc: str | None = None  # earliest start ever requested for this series
    first_bar_utc: str | None = None
    last_bar_utc: str | None = None
    rows: int = 0
    fetched_at_utc: str
    extra: dict[str, Any] = {}


# --- lockfile ------------------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists. Never sends a signal (``os.kill`` on Windows would
    *terminate* the target), so Windows uses OpenProcess/GetExitCodeProcess."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information, still_active, error_access_denied = 0x1000, 259, 5
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied  # exists, but not ours
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FileLock:
    """Cross-process single-writer lock (standard library only). Not re-entrant."""

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = 30.0,
        stale_after: float = 600.0,
        poll: float = 0.05,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        is_alive: Callable[[int], bool] = pid_alive,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.stale_after = stale_after
        self.poll = poll
        self._clock = clock
        self._sleep = sleep
        self._is_alive = is_alive
        self._token: str | None = None

    @property
    def held(self) -> bool:
        return self._token is not None

    def acquire(self) -> None:
        if self._token is not None:
            raise CacheError(f"lock already held: {self.path.name}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        started = self._clock()
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._break_if_stale():
                    continue
                if self._clock() - started >= self.timeout:
                    raise CacheLockTimeout(
                        f"cache is locked by another writer: {self.path.name} (waited {self.timeout:.0f}s)"
                    ) from None
                self._sleep(self.poll)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "created": self._clock(), "token": token}, handle)
            self._token = token
            if is_dev_mode():
                logger.debug("lock acquired: %s", self.path.name)
            return

    def _read_owner(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {}

    def _break_if_stale(self) -> bool:
        owner = self._read_owner()
        if owner is None:
            return True  # vanished meanwhile: retry immediately
        pid = int(owner.get("pid", 0) or 0)
        created = float(owner.get("created", 0) or 0)
        age = self._clock() - created if created else None
        if not owner:
            # Unreadable/empty: the writer may be between os.open and json.dump. Judge by file age.
            try:
                age = self._clock() - self.path.stat().st_mtime
            except FileNotFoundError:
                return True
            stale = age > self.stale_after
        else:
            stale = (not self._is_alive(pid)) or (age is not None and age > self.stale_after)
        if not stale:
            return False
        # Move it aside first (atomic), so two breakers cannot both delete a fresh lock.
        aside = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex}")
        try:
            os.replace(self.path, aside)
        except FileNotFoundError:
            return True
        except PermissionError:
            return False
        try:
            moved = json.loads(aside.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            moved = {}
        if owner and moved.get("token") != owner.get("token"):
            # A fresh lock replaced the stale one between our read and the move: put it back.
            with contextlib.suppress(OSError):
                os.link(aside, self.path)
            with contextlib.suppress(OSError):
                os.remove(aside)
            return False
        with contextlib.suppress(OSError):
            os.remove(aside)
        logger.warning("broke stale cache lock %s (pid=%s, age=%s s)", self.path.name, pid,
                       None if age is None else round(age))
        return True

    def release(self) -> None:
        if self._token is None:
            return
        owner = self._read_owner()
        if owner and owner.get("token") == self._token:
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.path)
        self._token = None
        if is_dev_mode():
            logger.debug("lock released: %s", self.path.name)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# --- atomic file helpers -------------------------------------------------------------------------

def _replace_with_retry(src: Path, dst: Path, attempts: int = 20, delay: float = 0.05) -> None:
    # On Windows os.replace fails with PermissionError while a reader has dst open; retry briefly.
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _tmp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)


def merge_frames(old: pd.DataFrame | None, new: pd.DataFrame | None) -> pd.DataFrame:
    """Union of two canonical frames by bar time; rows from ``new`` win on overlap."""
    parts = [normalize_frame(f) for f in (old, new) if f is not None and len(f)]
    if not parts:
        return empty_frame()
    return sort_dedupe(pd.concat(parts, ignore_index=True), keep="last")


# --- the cache -----------------------------------------------------------------------------------

class OhlcvCache:
    def __init__(self, data_dir: Path | str, *, lock_timeout: float = 30.0, stale_after: float = 600.0) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "cache"
        self.ohlcv_dir = self.root / "ohlcv"
        self.symbols_dir = self.root / "symbols"
        self.lock_timeout = lock_timeout
        self.stale_after = stale_after

    # paths
    def series_path(self, symbol: str, timeframe: Timeframe | str) -> Path:
        tf = Timeframe.parse(timeframe)
        return self.ohlcv_dir / f"{validate_symbol_name(symbol)}_{tf.value}.parquet"

    def spec_path(self, symbol: str) -> Path:
        return self.symbols_dir / f"{validate_symbol_name(symbol)}.json"

    @property
    def offset_fit_path(self) -> Path:
        return self.root / "offset_fit.json"

    def lock(self, symbol: str, timeframe: Timeframe | str) -> FileLock:
        path = self.series_path(symbol, timeframe)
        return FileLock(path.with_name(path.name + ".lock"), timeout=self.lock_timeout,
                        stale_after=self.stale_after)

    # OHLCV
    def read(self, symbol: str, timeframe: Timeframe | str) -> tuple[pd.DataFrame, CacheMeta] | None:
        path = self.series_path(symbol, timeframe)
        if not path.is_file():
            return None
        table = pq.read_table(path)
        meta = self._meta_from_schema(table.schema, symbol, timeframe)
        frame = normalize_frame(table.to_pandas())
        if is_dev_mode():
            logger.debug("cache read %s: %d rows %s..%s (%s)", path.name, len(frame), meta.first_bar_utc,
                         meta.last_bar_utc, meta.source)
        return frame, meta

    def read_meta(self, symbol: str, timeframe: Timeframe | str) -> CacheMeta | None:
        path = self.series_path(symbol, timeframe)
        if not path.is_file():
            return None
        return self._meta_from_schema(pq.read_schema(path), symbol, timeframe)

    @staticmethod
    def _meta_from_schema(schema: pa.Schema, symbol: str, timeframe: Timeframe | str) -> CacheMeta:
        raw = (schema.metadata or {}).get(META_KEY)
        if raw is None:
            raise CacheError(f"cache file for {symbol} {timeframe} has no alpha_trader metadata")
        return CacheMeta.model_validate_json(raw)

    def write(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        frame: pd.DataFrame,
        meta: CacheMeta,
        *,
        lock: bool = True,
    ) -> CacheMeta:
        """Validate and atomically write ``frame``. ``lock=False`` only if the caller holds :meth:`lock`."""
        tf = Timeframe.parse(timeframe)
        frame = sort_dedupe(normalize_frame(frame))
        validate_frame(frame, tf)
        meta = meta.model_copy(update={
            "symbol": symbol, "timeframe": tf.value, "rows": len(frame),
            "first_bar_utc": iso_z(frame["time"].iloc[0]) if len(frame) else None,
            "last_bar_utc": iso_z(frame["time"].iloc[-1]) if len(frame) else None,
        })
        path = self.series_path(symbol, tf)
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx = self.lock(symbol, tf) if lock else contextlib.nullcontext()
        with ctx:
            table = pa.Table.from_pandas(frame, preserve_index=False)
            metadata = dict(table.schema.metadata or {})
            metadata[META_KEY] = meta.model_dump_json().encode("utf-8")
            table = table.replace_schema_metadata(metadata)
            tmp = _tmp_path(path)
            try:
                pq.write_table(table, tmp)
                _replace_with_retry(tmp, path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(tmp)
        if is_dev_mode():
            logger.debug("cache write %s: %d rows %s..%s source=%s offset=%s", path.name, meta.rows,
                         meta.first_bar_utc, meta.last_bar_utc, meta.source, meta.offset_model)
        return meta

    def list_series(self) -> list[tuple[str, Timeframe]]:
        out: list[tuple[str, Timeframe]] = []
        if not self.ohlcv_dir.is_dir():
            return out
        for path in sorted(self.ohlcv_dir.glob("*.parquet")):
            symbol, _, tf = path.stem.rpartition("_")
            with contextlib.suppress(ValueError):
                out.append((symbol, Timeframe.parse(tf)))
        return out

    # symbol specs
    def write_spec(self, spec: SymbolSpec, source: CacheSource = "mt5") -> None:
        payload = {"spec": spec.model_dump(), "source": source, "fetched_at_utc": utc_now_iso()}
        atomic_write_text(self.spec_path(spec.name), json.dumps(payload, indent=2, ensure_ascii=False))

    def read_spec(self, symbol: str) -> tuple[SymbolSpec, dict[str, Any]] | None:
        path = self.spec_path(symbol)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return SymbolSpec.model_validate(payload["spec"]), {k: v for k, v in payload.items() if k != "spec"}

    # offset fit
    def write_offset_fit(self, fit: OffsetFit) -> None:
        atomic_write_text(self.offset_fit_path, fit.model_dump_json(indent=2))

    def read_offset_fit(self) -> OffsetFit | None:
        if not self.offset_fit_path.is_file():
            return None
        return OffsetFit.model_validate_json(self.offset_fit_path.read_text(encoding="utf-8"))

