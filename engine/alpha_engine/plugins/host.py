"""Parent side of the plugin sandbox: worker processes, limits, strict re-validation, the strategy proxy.

Worker process (:class:`WorkerSession`)
---------------------------------------
* command: ``[sys.executable, "-E", "-s", "-S", "-B", "-m", "alpha_engine", "--plugin-worker"]`` from the
  ``engine/`` directory (frozen build: ``[sys.executable, "--plugin-worker"]``). ``-E``: no ``PYTHON*``
  variables; ``-s``/``-S``: no user site, no ``site`` / ``.pth`` code -- the parent sends its own ``sys.path``
  in the ``go`` line instead; ``-B``: no ``.pyc`` writes;
* environment: only ``SYSTEMROOT``, ``PYTHONIOENCODING`` and ``DEV_MODE`` (no MT5 variables, no secrets);
  ``CREATE_NO_WINDOW``; stdin/stdout pipes, stderr discarded;
* Windows Job Object assigned BEFORE ``go`` is sent (the worker runs no plugin code before ``go``): process
  memory cap (:data:`DEFAULT_MEMORY_LIMIT`, 1 GiB), job memory cap, ONE active process (no children), UI
  restrictions, kill-on-close. If the job cannot be set up the worker is killed and nothing runs (fail closed);
* time limits: every request has a deadline; on expiry the job is terminated (:class:`PluginTimeout`);
* at most :data:`MAX_CONCURRENT_WORKERS` workers at a time (a semaphore);
* protocol: JSON lines. Every reply is parsed strictly (no NaN/Infinity, size cap :data:`MAX_LINE_BYTES`,
  matching request id); anything else kills the worker (:class:`PluginProtocolError`). Nothing is unpickled.

Re-validation (the worker is untrusted once the plugin ran)
-----------------------------------------------------------
:func:`parse_description` (name / version / title / ``ParamSchema`` rebuilt from plain JSON) and
:func:`parse_candidates` (every candidate through ``SignalCandidate.model_validate`` plus the S1
``scan_full_history`` checks: this strategy / version / symbol / params hash, on a bar of ``h1``, one per bar,
chronological, not before ``first_valid_index``).

:class:`PluginStrategyProxy`
----------------------------
A ``Strategy`` whose ``scan`` / ``evaluate`` / ``first_valid_index`` / ``warmup_margin_h4_bars`` run in a fresh
worker per call, built from a stored record (:func:`make_proxy_class`): ``source = "plugin"`` and
``code_sha256`` = SHA-256 of the source file, so chart / backtest provenance and cache keys carry the file hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettings
from ..strategy.base import H1_DURATION, H4_DURATION, Strategy, StrategyContext, bar_open_times
from ..strategy.params import ParamSchema, ParamSpec, params_hash
from ..strategy.signal import SignalCandidate

logger = get_logger(__name__)

WORKER_FLAG = "--plugin-worker"
DEFAULT_MEMORY_LIMIT = 1 << 30  # 1 GiB per worker (job object)
BOOT_TIMEOUT_S = 60.0  # imports + sandbox set-up + plugin module execution
CALL_TIMEOUT_S = 60.0  # evaluate / meta at runtime
SCAN_TIMEOUT_S = 300.0  # full-history scan at runtime (about 5 years of H1 bars)
MAX_LINE_BYTES = 64 << 20
MAX_CONCURRENT_WORKERS = 2
ACQUIRE_TIMEOUT_S = 120.0
MAX_PARAMS = 64
MAX_HISTORY_BARS = 1_000_000
MAX_WARMUP_H4 = 100_000
FRAME_COLUMNS = ("open", "high", "low", "close", "tick_volume", "spread")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_WORKERS = threading.BoundedSemaphore(MAX_CONCURRENT_WORKERS)


# -------------------------------------------------------------------------------------------------- errors
class PluginWorkerError(RuntimeError):
    """A plugin worker failed. ``kind``: boot | plugin_error | contract | sandbox | memory | timeout |
    protocol | crashed | busy | job. ``message_fa``: Persian text for the user; ``line``: plugin source line."""

    def __init__(self, kind: str, message_fa: str, detail: str = "", line: int | None = None) -> None:
        super().__init__(f"{kind}: {detail or message_fa}")
        self.kind = kind
        self.message_fa = message_fa
        self.detail = detail
        self.line = line


class PluginTimeout(PluginWorkerError):
    pass


class PluginProtocolError(PluginWorkerError):
    pass


_KIND_FA = {
    "boot": "فرآیند اجرای پلاگین راه‌اندازی نشد",
    "plugin_error": "کد پلاگین خطا داد",
    "contract": "خروجی پلاگین با قرارداد سیستم سازگار نیست",
    "sandbox": "پلاگین کاری انجام داد که در محیط ایزوله ممنوع است",
    "memory": "پلاگین از سقف حافظه مجاز بیشتر مصرف کرد",
    "protocol": "پاسخ فرآیند پلاگین نامعتبر بود",
}


def _clean_text(value: object, limit: int = 300) -> str:
    text = str(value) if value is not None else ""
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:limit]


def _worker_error(error: object) -> PluginWorkerError:
    """Map the worker's error object (untrusted JSON) to a :class:`PluginWorkerError`."""
    if not isinstance(error, dict):
        return PluginProtocolError("protocol", _KIND_FA["protocol"], "error payload is not an object")
    kind = error.get("kind") if error.get("kind") in _KIND_FA else "plugin_error"
    etype = _clean_text(error.get("type"), 60)
    message = _clean_text(error.get("message"))
    line = error.get("line")
    line = line if isinstance(line, int) and not isinstance(line, bool) and 0 < line < 10_000_000 else None
    where = f" (خط {line})" if line else ""
    message_fa = f"{_KIND_FA[kind]}{where}: {etype}: {message}" if etype else f"{_KIND_FA[kind]}{where}: {message}"
    return PluginWorkerError(kind, message_fa, f"{etype}: {message}", line)


# --------------------------------------------------------------------------------------------- job object
class _JobObject:
    """Windows Job Object: memory cap, one active process, UI restrictions, kill on close (ctypes, no pywin32)."""

    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectBasicUIRestrictions = 4
    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_UILIMIT_ALL = 0x000000FF

    def __init__(self, memory_limit: int) -> None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class UiRestrictions(ctypes.Structure):
            _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.QueryInformationJobObject.restype = wintypes.BOOL
        k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                  ctypes.c_void_p]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._ctypes = ctypes
        self._k32 = k32
        self._extended = ExtendedLimit
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self.handle = handle
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_ACTIVE_PROCESS | self.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | self.JOB_OBJECT_LIMIT_JOB_MEMORY | self.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = int(memory_limit)
        info.JobMemoryLimit = int(memory_limit)
        if not k32.SetInformationJobObject(handle, self.JobObjectExtendedLimitInformation, ctypes.byref(info),
                                           ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            self.close()
            raise OSError(err, "SetInformationJobObject (limits) failed")
        ui = UiRestrictions(self.JOB_OBJECT_UILIMIT_ALL)
        if not k32.SetInformationJobObject(handle, self.JobObjectBasicUIRestrictions, ctypes.byref(ui),
                                           ctypes.sizeof(ui)):
            err = ctypes.get_last_error()
            self.close()
            raise OSError(err, "SetInformationJobObject (UI) failed")

    def assign(self, proc: subprocess.Popen[bytes]) -> None:
        handle = int(proc._handle)  # type: ignore[attr-defined]  # the Popen's own process handle (no pid race)
        if not self._k32.AssignProcessToJobObject(self.handle, handle):
            raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def peak_memory(self) -> int | None:
        info = self._extended()
        ok = self._k32.QueryInformationJobObject(self.handle, self.JobObjectExtendedLimitInformation,
                                                 self._ctypes.byref(info), self._ctypes.sizeof(info), None)
        return int(info.PeakProcessMemoryUsed) if ok else None

    def terminate(self) -> None:
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def _apply_posix_limits(memory_limit: int) -> None:  # pragma: no cover - the engine ships for Windows
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))


# ------------------------------------------------------------------------------------------------ process
def engine_dir() -> Path:
    """Directory that contains the ``alpha_engine`` package (the worker's working directory)."""
    return Path(__file__).resolve().parents[2]


def _is_venv_launcher(path: str) -> bool:
    """``<venv>/Scripts/python.exe`` (or ``<venv>/bin/python``): its grandparent holds ``pyvenv.cfg``."""
    return os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(path))), "pyvenv.cfg"))


def worker_interpreter() -> str:
    """The REAL interpreter binary for the worker (never a venv redirector).

    On Windows ``<venv>\\Scripts\\python.exe`` is a launcher that starts the base interpreter as a CHILD process.
    The worker's Job Object allows exactly ONE active process, so the launcher's child is refused and the launcher
    exits with code 101 -- the worker never starts. Therefore the worker is spawned with the base interpreter
    (``sys._base_executable``, else ``<sys.base_prefix>/python.exe``). It does not need the venv's ``site``: the
    parent sends its own ``sys.path`` (including the venv's site-packages) in the ``go`` line, and the flags
    ``-E -s -S`` ignore the environment, user site and ``.pth`` files anyway. The one-process cap stays.

    Worked example (this project): parent ``engine\\.venv\\Scripts\\python.exe`` -> worker
    ``E:\\Programs\\minicoda\\python.exe``. Frozen builds are handled by :func:`worker_command` (``sys.executable``
    is the real exe). If no base binary can be found, ``sys.executable`` is returned and the worker fails closed
    under the job's process cap (reported as ``crashed``), never runs without it."""
    candidates = [getattr(sys, "_base_executable", None)]
    base_prefix = getattr(sys, "base_prefix", None)
    if base_prefix:
        names = ("python.exe",) if os.name == "nt" else (f"python{sys.version_info.major}.{sys.version_info.minor}",
                                                         f"python{sys.version_info.major}", "python")
        for name in names:
            candidates.append(os.path.join(base_prefix, name) if os.name == "nt"
                              else os.path.join(base_prefix, "bin", name))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and not _is_venv_launcher(candidate):
            return os.path.abspath(candidate)
    return sys.executable


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG]
    return [worker_interpreter(), "-E", "-s", "-S", "-B", "-m", "alpha_engine", WORKER_FLAG]


def worker_env() -> dict[str, str]:
    """Minimal environment: no MT5 variables, no ``ALPHA_TRADER_*`` paths, no secrets."""
    env = {"PYTHONIOENCODING": "utf-8", "DEV_MODE": "true" if is_dev_mode() else "false"}
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("SystemRoot")
    if system_root:
        env["SYSTEMROOT"] = system_root
    return env


def boot_sys_path() -> list[str]:
    """The parent's import path (absolute, existing entries) for the worker's ``go`` line."""
    out: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        full = os.path.abspath(entry)
        if os.path.exists(full) and full not in out:
            out.append(full)
    return out


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite number {token!r} in a worker reply")


class WorkerSession:
    """One sandboxed worker running one plugin source. Use as a context manager; :meth:`start` returns the raw
    ``describe`` answer (validate it with :func:`parse_description`)."""

    def __init__(self, source: str, *, memory_limit: int = DEFAULT_MEMORY_LIMIT,
                 boot_timeout: float = BOOT_TIMEOUT_S, label: str = "plugin") -> None:
        self._source = source
        self._memory_limit = int(memory_limit)
        self._boot_timeout = float(boot_timeout)
        self.label = label
        self._proc: subprocess.Popen[bytes] | None = None
        self._job: _JobObject | None = None
        self._lines: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 0
        self._acquired = False
        self._killed = False
        self.boot_s: float | None = None
        self.description: dict[str, Any] | None = None

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> WorkerSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> dict[str, Any]:
        if not _WORKERS.acquire(timeout=ACQUIRE_TIMEOUT_S):
            raise PluginWorkerError("busy", "همه فرآیندهای اجرای پلاگین مشغول هستند؛ کمی بعد دوباره تلاش کنید.")
        self._acquired = True
        started = time.perf_counter()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                worker_command(), cwd=str(engine_dir()) if not getattr(sys, "frozen", False) else None,
                env=worker_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=creationflags, close_fds=True,
                preexec_fn=None if os.name == "nt" else (lambda: _apply_posix_limits(self._memory_limit)),
            )
        except OSError as exc:
            self.close()
            raise PluginWorkerError("boot", _KIND_FA["boot"] + ".", f"spawn failed: {exc}") from None
        if os.name == "nt":
            try:
                self._job = _JobObject(self._memory_limit)
                self._job.assign(self._proc)
            except OSError as exc:
                self.close()
                raise PluginWorkerError("job", "محدودیت حافظه/فرآیند برای اجرای پلاگین برقرار نشد؛ پلاگین اجرا نشد.",
                                        f"job object: {exc}") from None
        self._reader = threading.Thread(target=self._read_loop, name=f"plugin-reader-{self._proc.pid}", daemon=True)
        self._reader.start()
        if is_dev_mode():
            logger.debug("plugin worker %s spawned: pid=%d memory_limit=%d MiB", self.label, self._proc.pid,
                         self._memory_limit >> 20)
        go = {"cmd": "go", "sys_path": boot_sys_path(), "dev_mode": is_dev_mode(), "source": self._source}
        result = self._exchange(0, go, self._boot_timeout)
        self.boot_s = time.perf_counter() - started
        self.description = result
        if is_dev_mode():
            logger.debug("plugin worker %s ready in %.2f s (imports %.2f s, plugin module %.3f s)", self.label,
                         self.boot_s, _num(result.get("boot_s")), _num(result.get("exec_s")))
        return result

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.poll() is None and not self._killed:
                try:
                    self._send({"id": -1, "cmd": "shutdown"}, proc)
                    proc.wait(timeout=2.0)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            if proc.poll() is None:
                self._kill(proc)
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - killed above
                pass
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._acquired:
            self._acquired = False
            _WORKERS.release()

    def _kill(self, proc: subprocess.Popen[bytes] | None = None) -> None:
        proc = proc if proc is not None else self._proc
        self._killed = True
        if self._job is not None:
            self._job.terminate()
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        if is_dev_mode() and proc is not None:
            logger.debug("plugin worker %s killed (pid=%d)", self.label, proc.pid)

    def peak_memory(self) -> int | None:
        return self._job.peak_memory() if self._job is not None else None

    # ------------------------------------------------------------------ I/O
    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stdout = proc.stdout
        try:
            while True:
                line = stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    self._lines.put(("eof", None))
                    return
                if not line.endswith(b"\n"):
                    self._lines.put(("overflow" if len(line) > MAX_LINE_BYTES else "eof", None))
                    return
                self._lines.put(("line", line))
        except (OSError, ValueError):
            self._lines.put(("eof", None))

    def _send(self, message: dict[str, Any], proc: subprocess.Popen[bytes] | None = None) -> None:
        proc = proc if proc is not None else self._proc
        if proc is None or proc.stdin is None:
            raise PluginProtocolError("crashed", "فرآیند پلاگین متوقف شده است.", "no process")
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        proc.stdin.write(data)
        proc.stdin.flush()

    def _exchange(self, rid: int, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout)
        watchdog = threading.Timer(max(0.0, timeout), self._kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                self._send(message)
            except (OSError, ValueError):
                if self._killed:
                    raise self._timeout(timeout) from None
                raise self._crashed() from None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill()
                    raise self._timeout(timeout)
                try:
                    kind, line = self._lines.get(timeout=remaining)
                except queue.Empty:
                    self._kill()
                    raise self._timeout(timeout) from None
                if kind == "overflow":
                    self._kill()
                    raise PluginProtocolError("protocol", "خروجی فرآیند پلاگین از سقف مجاز بزرگ‌تر بود.",
                                              f"reply line larger than {MAX_LINE_BYTES} bytes")
                if kind == "eof":
                    if self._killed:
                        raise self._timeout(timeout)
                    raise self._crashed()
                reply = self._parse(line or b"")
                if reply.get("id") != rid:
                    self._kill()
                    raise PluginProtocolError("protocol", _KIND_FA["protocol"] + ".", "reply id mismatch")
                if reply.get("ok") is True and isinstance(reply.get("result"), dict):
                    return reply["result"]
                if reply.get("ok") is False:
                    raise _worker_error(reply.get("error"))
                self._kill()
                raise PluginProtocolError("protocol", _KIND_FA["protocol"] + ".", "malformed reply")
        finally:
            watchdog.cancel()

    def _parse(self, line: bytes) -> dict[str, Any]:
        try:
            reply = json.loads(line.decode("utf-8", errors="strict"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._kill()
            raise PluginProtocolError("protocol", _KIND_FA["protocol"] + " (متن غیر JSON).", "non-JSON output") from None
        if not isinstance(reply, dict):
            self._kill()
            raise PluginProtocolError("protocol", _KIND_FA["protocol"] + ".", "reply is not an object")
        return reply

    def _timeout(self, timeout: float) -> PluginTimeout:
        if is_dev_mode():
            logger.debug("plugin worker %s: timeout after %.1f s", self.label, timeout)
        return PluginTimeout("timeout", f"اجرای پلاگین از سقف زمان مجاز ({timeout:.0f} ثانیه) طول کشید و متوقف شد.",
                             f"timeout {timeout:.1f}s")

    def _crashed(self) -> PluginWorkerError:
        code = None
        if self._proc is not None:
            try:
                code = self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                code = None
        return PluginWorkerError("crashed", "فرآیند اجرای پلاگین بدون پاسخ بسته شد (احتمالا به‌خاطر سقف حافظه یا خطای داخلی).",
                                 f"worker exited with code {code}")

    def request(self, cmd: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send one command and return its (unvalidated) result object."""
        if self._proc is None:
            raise PluginWorkerError("crashed", "فرآیند پلاگین شروع نشده است.", "not started")
        self._next_id += 1
        started = time.perf_counter()
        result = self._exchange(self._next_id, {"id": self._next_id, "cmd": cmd, "payload": payload}, timeout)
        if is_dev_mode():
            logger.debug("plugin worker %s: %s answered in %.3f s", self.label, cmd, time.perf_counter() - started)
        return result

    # ------------------------------------------------------------------ typed helpers
    def scan(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any], account: AccountSettings,
             symbol: str, timeout: float) -> dict[str, Any]:
        return self.request("scan", {"h1": encode_frame(h1), "h4": encode_frame(h4), "params": dict(params),
                                     "account": account.model_dump(), "symbol": symbol}, timeout)

    def evaluate(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any], account: AccountSettings,
                 symbol: str, timeout: float, has_open_trade: bool = False) -> dict[str, Any]:
        return self.request("evaluate", {"h1": encode_frame(h1), "h4": encode_frame(h4), "params": dict(params),
                                         "account": account.model_dump(), "symbol": symbol,
                                         "has_open_trade": bool(has_open_trade)}, timeout)

    def meta(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        return self.request("meta", {"h1": encode_frame(h1), "h4": encode_frame(h4), "params": dict(params)}, timeout)


def _num(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else float("nan")


# ------------------------------------------------------------------------------------------------- frames
def encode_frame(frame: pd.DataFrame) -> dict[str, list[Any]]:
    """Columns of ``frame`` for the worker: ``time`` as int64 ns (UTC bar open) + the known numeric columns."""
    out: dict[str, list[Any]] = {"time": bar_open_times(frame).as_unit("ns").asi8.tolist() if len(frame) else []}
    for column in FRAME_COLUMNS:
        if column in frame.columns:
            out[column] = frame[column].to_numpy(dtype=np.float64).tolist()
    return out


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({"time": pd.DatetimeIndex([], tz="UTC"), "open": [], "high": [], "low": [], "close": []})


def closed_h4_for(h1_times_ns: np.ndarray, h4: pd.DataFrame, t: int) -> pd.DataFrame:
    """H4 bars closed when H1 bar ``t`` closes (``open + 4h <= open_t + 1h``); ``h4`` sorted by time."""
    h4_close_ns = bar_open_times(h4).as_unit("ns").asi8 + pd.Timedelta(H4_DURATION).value
    decision_ns = int(h1_times_ns[t]) + pd.Timedelta(H1_DURATION).value
    return h4.iloc[: int(np.searchsorted(h4_close_ns, decision_ns, side="right"))]


# --------------------------------------------------------------------------------------------- validation
@dataclass(frozen=True)
class PluginDescription:
    """What a plugin declares (from ``describe``; re-validated here)."""

    name: str
    version: int
    title_fa: str
    class_name: str
    param_schema: ParamSchema
    history_bars: int
    overrides: tuple[str, ...]

    def schema_list(self) -> list[dict[str, Any]]:
        return self.param_schema.to_list()


def parse_description(raw: object) -> PluginDescription:
    """Validate the worker's ``describe`` answer (raises :class:`PluginWorkerError` kind ``contract``)."""
    def bad(msg_fa: str, detail: str) -> PluginWorkerError:
        return PluginWorkerError("contract", msg_fa, detail)

    if not isinstance(raw, dict):
        raise bad("مشخصات پلاگین نامعتبر است.", "describe is not an object")
    name, version, title = raw.get("name"), raw.get("version"), raw.get("title_fa")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise bad("نام سیستم (name) نامعتبر است.", "bad name")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise bad("نسخه سیستم (version) باید عدد صحیح مثبت باشد.", "bad version")
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise bad("عنوان سیستم (title_fa) نامعتبر است.", "bad title")
    class_name = _clean_text(raw.get("class_name"), 100)
    specs_raw = raw.get("param_schema")
    if not isinstance(specs_raw, list) or len(specs_raw) > MAX_PARAMS:
        raise bad(f"param_schema باید فهرستی با حداکثر {MAX_PARAMS} پارامتر باشد.", "bad param_schema")
    try:
        schema = ParamSchema([ParamSpec.model_validate(item) for item in specs_raw])
    except (ValueError, TypeError) as exc:
        raise bad("param_schema نامعتبر است: " + _clean_text(exc, 400), "invalid param spec") from None
    clean, errors = schema.validate({})
    if errors:
        raise bad("مقادیر پیش‌فرض param_schema معتبر نیستند: " + "؛ ".join(errors), "defaults invalid")
    history = raw.get("history_bars", 1)
    if isinstance(history, bool) or not isinstance(history, int) or not 1 <= history <= MAX_HISTORY_BARS:
        raise bad("history_bars باید عدد صحیح مثبت باشد.", "bad history_bars")
    overrides_raw = raw.get("overrides") or []
    overrides = tuple(sorted(str(o) for o in overrides_raw if isinstance(o, str)))[:8] if isinstance(overrides_raw, list) else ()
    return PluginDescription(name=name, version=version, title_fa=title.strip(), class_name=class_name,
                             param_schema=schema, history_bars=history, overrides=overrides)


@dataclass(frozen=True)
class ScanOutcome:
    candidates: list[SignalCandidate]
    first_valid_index: int | None
    warmup_margin_h4_bars: int
    plugin_s: float | None


def parse_meta(raw: dict[str, Any], n_h1: int) -> tuple[int | None, int]:
    first = raw.get("first_valid_index")
    if first is not None and (isinstance(first, bool) or not isinstance(first, int) or first < 0):
        raise PluginWorkerError("contract", "first_valid_index پلاگین نامعتبر است.", "bad first_valid_index")
    if first is not None and first >= n_h1:
        first = None  # "never in this history"
    margin = raw.get("warmup_margin_h4_bars", 0)
    if isinstance(margin, bool) or not isinstance(margin, int) or not 0 <= margin <= MAX_WARMUP_H4:
        raise PluginWorkerError("contract", "warmup_margin_h4_bars پلاگین نامعتبر است.", "bad warm-up margin")
    return first, margin


def parse_candidates(raw: object, *, name: str, version: int, symbol: str, phash: str,
                     h1_times_ns: np.ndarray) -> list[SignalCandidate]:
    """Re-validate the candidates a worker returned (untrusted JSON) -- see the module docstring."""
    def bad(msg_fa: str, detail: str) -> PluginWorkerError:
        return PluginWorkerError("contract", msg_fa, detail)

    if not isinstance(raw, list):
        raise bad("خروجی scan باید فهرست کاندیدها باشد.", "candidates is not a list")
    if len(raw) > len(h1_times_ns):
        raise bad("تعداد کاندیدها از تعداد کندل‌ها بیشتر است.", "too many candidates")
    out: list[SignalCandidate] = []
    last_bar = -1
    for item in raw:
        if not isinstance(item, dict):
            raise bad("کاندید نامعتبر است.", "candidate is not an object")
        try:
            cand = SignalCandidate.model_validate(item)
        except (ValueError, TypeError) as exc:
            raise bad("کاندید پلاگین با مدل SignalCandidate سازگار نیست: " + _clean_text(exc, 400),
                      "SignalCandidate validation failed") from None
        if (cand.strategy_name, cand.strategy_version) != (name, version):
            raise bad("کاندید برای نام یا نسخه دیگری از سیستم ساخته شده است.", "strategy name/version mismatch")
        if cand.symbol != symbol:
            raise bad("نماد کاندید با نماد درخواست یکی نیست.", "symbol mismatch")
        if cand.params_hash != phash:
            raise bad("params_hash کاندید با پارامترهای معتبرشده یکی نیست (از params_hash(پارامترها) استفاده کنید).",
                      "params_hash mismatch")
        conf_ns = pd.Timestamp(cand.confirmation_bar_open_utc).as_unit("ns").value
        t = int(np.searchsorted(h1_times_ns, conf_ns))
        if t >= len(h1_times_ns) or int(h1_times_ns[t]) != conf_ns:
            raise bad("کندل تایید کاندید در داده H1 وجود ندارد.", "confirmation bar not in h1")
        if t <= last_bar:
            raise bad("کاندیدها باید به ترتیب زمان و حداکثر یکی برای هر کندل باشند.", "unsorted or duplicate bars")
        last_bar = t
        out.append(cand)
    return out


def check_frame_sorted(h1: pd.DataFrame) -> np.ndarray:
    times = bar_open_times(h1).as_unit("ns").asi8 if len(h1) else np.array([], dtype=np.int64)
    if len(times) > 1 and not (np.diff(times) > 0).all():
        raise ValueError("H1 bars must be sorted by open time without duplicates")
    return times


def run_scan(session: WorkerSession, desc: PluginDescription, h1: pd.DataFrame, h4: pd.DataFrame,
             params: Mapping[str, Any], account: AccountSettings, symbol: str, timeout: float) -> ScanOutcome:
    """``scan`` in ``session`` + full re-validation of the answer."""
    times = check_frame_sorted(h1)
    phash = params_hash(dict(params))
    raw = session.scan(h1, h4, params, account, symbol, timeout)
    candidates = parse_candidates(raw.get("candidates"), name=desc.name, version=desc.version, symbol=symbol,
                                  phash=phash, h1_times_ns=times)
    first, margin = parse_meta(raw, len(times))
    plugin_s = raw.get("plugin_s")
    return ScanOutcome(candidates=candidates, first_valid_index=first, warmup_margin_h4_bars=margin,
                       plugin_s=float(plugin_s) if isinstance(plugin_s, (int, float)) and not isinstance(plugin_s, bool)
                       and math.isfinite(plugin_s) else None)


def run_evaluate(session: WorkerSession, desc: PluginDescription, h1: pd.DataFrame, h4: pd.DataFrame,
                 params: Mapping[str, Any], account: AccountSettings, symbol: str, timeout: float,
                 has_open_trade: bool = False) -> SignalCandidate | None:
    """``evaluate`` on the closed prefix ``h1`` (+ closed ``h4``) in ``session``, re-validated."""
    times = check_frame_sorted(h1)
    raw = session.evaluate(h1, h4, params, account, symbol, timeout, has_open_trade)
    item = raw.get("candidate")
    if item is None:
        return None
    cands = parse_candidates([item], name=desc.name, version=desc.version, symbol=symbol,
                             phash=params_hash(dict(params)), h1_times_ns=times)
    cand = cands[0]
    if pd.Timestamp(cand.confirmation_bar_open_utc).as_unit("ns").value != int(times[-1]):
        raise PluginWorkerError("contract", "evaluate باید فقط برای آخرین کندل بسته‌شده کاندید برگرداند.",
                                "evaluate candidate not on the last bar")
    return cand


# ------------------------------------------------------------------------------------------------- proxy
def _fingerprint(frame: pd.DataFrame) -> str:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(len(frame)).encode())
    if len(frame):
        digest.update(np.ascontiguousarray(bar_open_times(frame).as_unit("ns").asi8).tobytes())
        for column in ("open", "high", "low", "close"):
            if column in frame.columns:
                digest.update(np.ascontiguousarray(frame[column].to_numpy(dtype=np.float64)).tobytes())
    return digest.hexdigest()


class PluginStrategyProxy(Strategy):
    """Base of the per-plugin proxy classes (:func:`make_proxy_class`); every hook runs in a worker."""

    source: ClassVar[str] = "plugin"  # type: ignore[assignment]
    plugin_source: ClassVar[str] = ""
    plugin_class_name: ClassVar[str] = ""
    scan_timeout_s: ClassVar[float] = SCAN_TIMEOUT_S
    call_timeout_s: ClassVar[float] = CALL_TIMEOUT_S
    memory_limit: ClassVar[int] = DEFAULT_MEMORY_LIMIT
    _memo_lock: ClassVar[threading.Lock]
    _meta_memo: ClassVar[OrderedDict[tuple[str, str, str], tuple[int | None, int]]]
    _margin_memo: ClassVar[dict[str, int]]

    def _description(self) -> PluginDescription:
        return PluginDescription(name=self.name, version=self.version, title_fa=self.title_fa,
                                 class_name=self.plugin_class_name, param_schema=self.param_schema,
                                 history_bars=int(self.history_bars), overrides=())

    def _session(self) -> WorkerSession:
        session = WorkerSession(self.plugin_source, memory_limit=self.memory_limit,
                                label=f"{self.name} v{self.version}")
        try:
            desc = parse_description(session.start())
        except BaseException:
            session.close()
            raise
        if (desc.name, desc.version) != (self.name, self.version):
            session.close()
            raise PluginWorkerError("contract", "کد پلاگین با نام/نسخه ثبت‌شده یکی نیست.", "identity mismatch")
        return session

    def _remember(self, h1: pd.DataFrame, h4: pd.DataFrame, phash: str, first: int | None, margin: int) -> None:
        cls = type(self)
        with cls._memo_lock:
            cls._meta_memo[(_fingerprint(h1), _fingerprint(h4), phash)] = (first, margin)
            cls._meta_memo.move_to_end((_fingerprint(h1), _fingerprint(h4), phash))
            while len(cls._meta_memo) > 16:
                cls._meta_memo.popitem(last=False)
            cls._margin_memo[phash] = margin

    def scan(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None, account: AccountSettings,
             *, symbol: str) -> list[SignalCandidate]:
        clean = self.clean_params(params)
        phash = params_hash(clean)
        started = time.perf_counter()
        with self._session() as session:
            outcome = run_scan(session, self._description(), h1, h4, clean, account, symbol, self.scan_timeout_s)
        first = outcome.first_valid_index
        if first is not None and outcome.candidates:
            times = check_frame_sorted(h1)
            first_ns = int(times[first])
            if pd.Timestamp(outcome.candidates[0].confirmation_bar_open_utc).as_unit("ns").value < first_ns:
                raise PluginWorkerError("contract", "پلاگین قبل از اولین کندل معتبر خود کاندید داد.",
                                        "candidate before first_valid_index")
        self._remember(h1, h4, phash, first, outcome.warmup_margin_h4_bars)
        if is_dev_mode():
            logger.debug("plugin scan %s %s: bars=%d candidates=%d first_valid=%s warm-up=%d plugin %.3f s total %.2f s",
                         self.identity.label(), symbol, len(h1), len(outcome.candidates), first,
                         outcome.warmup_margin_h4_bars, outcome.plugin_s or float("nan"), time.perf_counter() - started)
        return outcome.candidates

    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        if ctx.has_open_trade:
            return None
        clean = self.clean_params(ctx.params)
        with self._session() as session:
            return run_evaluate(session, self._description(), ctx.h1, ctx.h4, clean, ctx.account, ctx.symbol,
                                self.call_timeout_s)

    def _meta(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None) -> tuple[int | None, int]:
        clean = self.clean_params(params)
        phash = params_hash(clean)
        key = (_fingerprint(h1), _fingerprint(h4), phash)
        cls = type(self)
        with cls._memo_lock:
            if key in cls._meta_memo:
                return cls._meta_memo[key]
        with self._session() as session:
            raw = session.meta(h1, h4, clean, self.call_timeout_s)
        first, margin = parse_meta(raw, len(h1))
        self._remember(h1, h4, phash, first, margin)
        return first, margin

    def first_valid_index(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None) -> int | None:
        return self._meta(h1, h4, params)[0]

    def warmup_margin_h4_bars(self, params: Mapping[str, Any] | None) -> int:
        phash = params_hash(self.clean_params(params))
        cls = type(self)
        with cls._memo_lock:
            if phash in cls._margin_memo:
                return cls._margin_memo[phash]
        return self._meta(empty_frame(), empty_frame(), params)[1]


def make_proxy_class(*, name: str, version: int, title_fa: str, param_schema: list[dict[str, Any]] | ParamSchema,
                     history_bars: int, sha256: str, source: str, class_name: str = "") -> type[PluginStrategyProxy]:
    """A registrable ``Strategy`` class for one stored plugin version. ``source`` must hash to ``sha256``."""
    if not _SHA_RE.fullmatch(sha256):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    actual = hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).hexdigest()
    if actual != sha256:
        raise ValueError(f"plugin source hash {actual[:12]} does not match the recorded {sha256[:12]}")
    schema = param_schema if isinstance(param_schema, ParamSchema) else ParamSchema(
        [ParamSpec.model_validate(item) for item in param_schema])
    attrs: dict[str, Any] = {
        "name": name, "version": int(version), "title_fa": title_fa, "param_schema": schema,
        "history_bars": int(history_bars), "source": "plugin", "code_sha256": sha256, "plugin_source": source,
        "plugin_class_name": class_name, "_memo_lock": threading.Lock(), "_meta_memo": OrderedDict(),
        "_margin_memo": {},
        "__module__": __name__, "__qualname__": f"Plugin_{name}_v{version}",
    }
    return type(f"Plugin_{name}_v{version}", (PluginStrategyProxy,), attrs)


__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "PluginDescription",
    "PluginProtocolError",
    "PluginStrategyProxy",
    "PluginTimeout",
    "PluginWorkerError",
    "ScanOutcome",
    "WorkerSession",
    "closed_h4_for",
    "encode_frame",
    "make_proxy_class",
    "parse_candidates",
    "parse_description",
    "run_evaluate",
    "run_scan",
]
