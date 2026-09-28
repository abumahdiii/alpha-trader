"""The sandboxed plugin worker: ``python -m alpha_engine --plugin-worker`` (a child of the engine).

The engine never runs uploaded code in its own process (which is attached to MetaTrader 5). This process
starts with ONLY the standard library loaded, waits for the parent's single ``go`` line and then, in order:

1. extends ``sys.path`` with the parent's entries (the parent is trusted; the environment is not used:
   ``-E -s -S -B``), marks optional native libraries unavailable (``sys.modules[name] = None``: the MT5 package,
   pyarrow, numexpr, bottleneck, ...), so pandas runs on plain numpy and no native I/O library is loaded;
2. imports numpy, pandas, pydantic and the whitelisted engine helpers and warms them up (lazy imports done
   before the lock). It NEVER calls ``get_settings()`` / ``configure_logging()`` (the ``.env`` may hold secrets);
3. **locks down**: a ``sys.meta_path`` finder refuses every NEW import outside the standard library (minus
   network / process / FFI modules) and the numpy/pandas/pydantic stack, and a ``sys.addaudithook`` hook (which
   cannot be removed) denies sockets, processes (``subprocess.Popen``, ``os.system``, ``os.exec*``,
   ``os.spawn*``, ``os.startfile``, ``_winapi.*``), FFI (``ctypes.*``), the registry, ``gc`` / tracing
   introspection, new interpreters, every file write, and every file read or directory listing outside the
   Python installation / site-packages / the engine package (so ``.env`` and ``data/`` are unreadable). The
   same import rule is enforced on the ``import`` audit event, so removing the finder changes nothing;
4. executes the plugin source in a fresh module whose builtins are a small safe set (no ``open``, ``exec``,
   ``eval``, ``getattr``, ...; ``print`` is a no-op) and whose ``__import__`` only allows the static whitelist
   (:func:`.validator.import_allowed`);
5. answers JSON lines on stdin/stdout -- ``describe``, ``scan``, ``evaluate``, ``meta``, ``shutdown`` -- with bars
   as columns (int64 ns open times + float lists) and candidates as ``SignalCandidate.model_dump(mode="json")``.
   Nothing is ever pickled. ``sys.stdout``/``sys.stderr`` are replaced by a sink before the plugin runs, so a
   ``print`` cannot corrupt the protocol (the parent treats any malformed line as fatal anyway).

The parent (:mod:`.host`) puts this process into a Windows Job Object (memory cap, one process, killed with the
job) BEFORE sending ``go``, enforces the time limits by killing it, and re-validates every answer: the worker is
treated as untrusted from the moment the plugin code runs.
"""

from __future__ import annotations

import json
import sys
import time

PROTOCOL_VERSION = 1
PLUGIN_MODULE = "alpha_plugin"
PLUGIN_FILENAME = "<plugin>"

# Optional libraries pandas/numpy would load if present: native code with its own file/network I/O outside
# Python's audit events (pyarrow file systems, ...) or expression evaluators. Marked unavailable up front.
BLOCKED_MODULES = (
    "MetaTrader5", "pyarrow", "numexpr", "bottleneck", "numba", "fsspec", "s3fs", "gcsfs", "tables", "scipy",
    "matplotlib", "sqlalchemy", "openpyxl", "xlsxwriter", "xlrd", "odf", "pyxlsb", "python_calamine", "lxml",
    "bs4", "html5lib", "jinja2", "IPython", "psutil", "win32api", "win32con", "pywintypes", "pythoncom",
)
# Third-party top-level packages a lazy import may still load after the lock (numpy / pandas / pydantic stack).
ALLOWED_THIRD_PARTY = frozenset({
    "numpy", "pandas", "dateutil", "pytz", "tzdata", "pydantic", "pydantic_core", "typing_extensions",
    "annotated_types", "typing_inspection", "six",
})
# Standard-library modules that may never be loaded after the lock (network, processes, FFI, OS, debuggers,
# sub-interpreters). Some are already loaded by numpy/pandas: those stay usable only through the audit hook.
DENIED_NEW_MODULES = frozenset({
    "socket", "_socket", "ssl", "_ssl", "select", "selectors", "asyncio", "_asyncio", "subprocess",
    "_posixsubprocess", "multiprocessing", "_multiprocessing", "concurrent", "ctypes", "_ctypes", "winreg",
    "_winreg", "_winapi", "msvcrt", "_overlapped", "_wmi", "mmap", "http", "urllib", "ftplib", "smtplib",
    "poplib", "imaplib", "nntplib", "telnetlib", "xmlrpc", "socketserver", "webbrowser", "ensurepip", "venv",
    "shutil", "tempfile", "sqlite3", "_sqlite3", "dbm", "shelve", "pty", "tty", "code", "codeop", "pdb", "bdb",
    "trace", "runpy", "idlelib", "tkinter", "_tkinter", "turtle", "curses", "_xxsubinterpreters",
    "_interpreters", "_xxinterpchannels", "_interpchannels", "_interpqueues", "_testcapi", "_testinternalcapi",
    "faulthandler", "tracemalloc", "cProfile", "profile", "_lsprof", "wsgiref", "mailbox", "smtpd", "cgi",
    "antigravity", "zipapp", "compileall", "py_compile", "pickletools", "uuid", "getpass", "pydoc",
    "alpha_engine",  # every engine module the plugin may use is loaded before the lock
})
# Audit events (prefix match) denied after the lock. "os.listdir" / "os.scandir" are checked separately.
DENIED_EVENT_PREFIXES = (
    "socket.", "subprocess.", "ctypes.", "winreg.", "_winapi.", "msvcrt.", "_wmi.", "mmap.", "sqlite3.",
    "shutil.", "tempfile.", "webbrowser.", "urllib.", "http.", "ftplib.", "smtplib.", "poplib.", "imaplib.",
    "nntplib.", "telnetlib.", "glob.", "gc.", "_thread.", "os.", "sys.", "code.", "function.", "pickle.",
    "cpython.run_", "cpython.PyInterpreterState_New", "cpython.remote_debugger", "builtins.input",
    "builtins.breakpoint", "setopencode", "shelve.", "dbm.", "resource.", "signal.", "syslog.", "fcntl.",
    "pty.", "_posixsubprocess.", "_interpreters.", "_xxsubinterpreters.",
)
ALLOWED_SYS_EVENTS = frozenset({"sys._getframe", "sys._getframemodulename", "sys.excepthook", "sys.unraisablehook"})
PATH_CHECKED_EVENTS = frozenset({"os.listdir", "os.scandir"})
MAX_ERROR_CHARS = 600


class SandboxViolation(PermissionError):
    """Raised by the worker's audit hook / import guard. A ``PermissionError`` so library code that tolerates
    unwritable caches (e.g. importlib's bytecode writer) degrades gracefully instead of crashing."""


class _Sink:
    """Replacement for ``sys.stdout``/``sys.stderr``: swallows everything (the real stdout is the protocol)."""

    encoding = "utf-8"
    errors = "replace"

    def write(self, text: str) -> int:
        return len(text)

    def writelines(self, lines: object) -> None:
        return None

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


# ---------------------------------------------------------------------------------------------- lock down
def _top(name: str) -> str:
    return name.partition(".")[0]


def new_import_allowed(fullname: str) -> bool:
    """May ``fullname`` be LOADED (it is not in ``sys.modules`` yet) after the lock?"""
    lowered = fullname.lower()
    if "metatrader" in lowered or lowered.startswith("mt5"):
        return False
    top = _top(fullname)
    if top in DENIED_NEW_MODULES:
        return False
    if top in ALLOWED_THIRD_PARTY:
        return True
    return top in getattr(sys, "stdlib_module_names", frozenset())


class _ImportBlocker:
    """``sys.meta_path[0]``: refuses to find any module :func:`new_import_allowed` rejects."""

    @staticmethod
    def find_spec(fullname: str, path: object = None, target: object = None) -> None:
        if not new_import_allowed(fullname):
            raise SandboxViolation(f"import of {fullname!r} is blocked in the plugin sandbox")
        return None

    @staticmethod
    def invalidate_caches() -> None:
        return None


def _read_roots(boot_path: list[str]) -> tuple[str, ...]:
    """Directories the worker may read from after the lock (lower-cased absolute paths): the Python
    installation, site-packages directories, the frozen bundle and the ``alpha_engine`` package itself."""
    import os

    import alpha_engine

    candidates = [sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(alpha_engine.__file__)]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(bundle)
    for entry in boot_path:
        base = os.path.basename(entry.rstrip("\\/")).lower()
        if base in ("site-packages", "dist-packages") or entry.lower().endswith(".zip"):
            candidates.append(entry)
    roots: list[str] = []
    for item in candidates:
        if not item:
            continue
        full = os.path.abspath(item).rstrip("\\/").lower()
        if full and full not in roots:
            roots.append(full)
    return tuple(roots)


def install_lockdown(read_roots: tuple[str, ...]) -> None:
    """Install the import finder and the audit hook (irreversible for this process)."""
    import os

    try:
        from nt import _getfullpathname as fullpath  # C function: cannot be monkey-patched by the plugin
        sep = "\\"
    except ImportError:  # pragma: no cover - the engine targets Windows
        fullpath = os.path.abspath
        sep = "/"
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | getattr(os, "O_TEMPORARY", 0)
    sys.meta_path.insert(0, _ImportBlocker())

    # Positional defaults (an immutable tuple; replacing it is an audited attribute set) -- never kw-only
    # defaults, whose dict could be mutated in place.
    def hook(event: str, args: tuple, _roots=read_roots, _fullpath=fullpath, _sep=sep,
             _write_flags=write_flags, _denied=DENIED_EVENT_PREFIXES, _sys_ok=ALLOWED_SYS_EVENTS,
             _path_events=PATH_CHECKED_EVENTS, _allowed_import=new_import_allowed, _violation=SandboxViolation,
             _isinstance=isinstance, _str=str, _bytes=bytes, _int=int, _len=len) -> None:
        def path_ok(path: object) -> bool:
            if _isinstance(path, _bytes):
                path = path.decode("utf-8", "replace")
            if not _isinstance(path, _str) or not path:
                return False
            try:
                full = _fullpath(path).lower()
            except Exception:
                return False
            if "metatrader" in full:
                return False
            for root in _roots:
                if full == root or full.startswith(root + _sep):
                    return True
            return False

        if event == "open":
            path = args[0] if _len(args) > 0 else None
            mode = args[1] if _len(args) > 1 else None
            flags = args[2] if _len(args) > 2 else 0
            if _isinstance(mode, _str) and ("w" in mode or "a" in mode or "x" in mode or "+" in mode):
                raise _violation("file write is blocked in the plugin sandbox")
            if _isinstance(flags, _int) and flags & _write_flags:
                raise _violation("file write is blocked in the plugin sandbox")
            if not path_ok(path):
                raise _violation("file access outside the Python installation is blocked in the plugin sandbox")
            return
        if event == "import":
            name = args[0] if _len(args) > 0 else ""
            filename = args[1] if _len(args) > 1 else None
            if not _isinstance(name, _str) or not _allowed_import(name):
                raise _violation(f"import of {name!r} is blocked in the plugin sandbox")
            if filename is not None and not path_ok(filename):
                raise _violation(f"import of {name!r} from outside the Python installation is blocked")
            return
        if event in _path_events:
            if not path_ok(args[0] if _len(args) > 0 else None):
                raise _violation("directory listing outside the Python installation is blocked in the plugin sandbox")
            return
        if event in _sys_ok:
            return
        if event == "object.__setattr__" or event == "object.__delattr__":
            name = args[1] if _len(args) > 1 else ""
            if _isinstance(name, _str) and name.startswith("__"):
                raise _violation(f"setting {name!r} is blocked in the plugin sandbox")
            return
        for prefix in _denied:
            if event.startswith(prefix):
                raise _violation(f"{event} is blocked in the plugin sandbox")

    sys.addaudithook(hook)


# ------------------------------------------------------------------------------------------ plugin module
SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "callable", "chr", "classmethod", "complex", "dict",
    "divmod", "enumerate", "filter", "float", "format", "frozenset", "hash", "hex", "id", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next", "object", "oct", "ord", "pow", "property",
    "range", "repr", "reversed", "round", "set", "slice", "sorted", "staticmethod", "str", "sum", "super",
    "tuple", "zip", "NotImplemented", "Ellipsis", "ArithmeticError", "AssertionError", "AttributeError",
    "Exception", "FloatingPointError", "IndexError", "KeyError", "LookupError", "NameError",
    "NotImplementedError", "OverflowError", "RuntimeError", "StopIteration", "TypeError", "ValueError",
    "ZeroDivisionError", "Warning", "UserWarning", "RuntimeWarning", "FutureWarning", "DeprecationWarning",
)


def _no_print(*args: object, **kwargs: object) -> None:
    return None


def safe_builtins() -> dict[str, object]:
    import builtins

    from .validator import import_allowed

    real_import = builtins.__import__

    def guarded_import(name: str, globals: object = None, locals: object = None, fromlist: object = (),
                       level: int = 0) -> object:
        names = tuple(fromlist) if fromlist else None
        problem = import_allowed(name, names, level)
        if problem is not None:
            raise SandboxViolation(f"import of {name!r} is not allowed in a plugin")
        return real_import(name, globals, locals, fromlist, level)

    table: dict[str, object] = {name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES}
    table["print"] = _no_print
    table["__build_class__"] = builtins.__build_class__
    table["__import__"] = guarded_import
    return table


def load_plugin(source: str, builtins_table: dict[str, object] | None = None) -> type:
    """Execute ``source`` in a fresh sandboxed module and return its single ``Strategy`` subclass.

    ``builtins_table``: from :func:`safe_builtins`, built BEFORE the lock (it imports the validator)."""
    import inspect
    import types

    from ..strategy.base import Strategy

    module = types.ModuleType(PLUGIN_MODULE)
    module.__dict__["__builtins__"] = builtins_table if builtins_table is not None else safe_builtins()
    sys.modules[PLUGIN_MODULE] = module  # dataclasses/typing resolve string annotations through sys.modules
    code = compile(source, PLUGIN_FILENAME, "exec", dont_inherit=True)
    exec(code, module.__dict__)  # noqa: S102 - the whole point of this sandboxed process
    found = [value for value in module.__dict__.values()
             if isinstance(value, type) and issubclass(value, Strategy) and value is not Strategy
             and value.__module__ == PLUGIN_MODULE]
    if len(found) != 1:
        raise _ProtocolFailure("contract", f"the plugin must define exactly one Strategy subclass (found {len(found)})")
    cls = found[0]
    if inspect.isabstract(cls):
        raise _ProtocolFailure("contract", "the Strategy subclass does not implement evaluate()")
    return cls


# ---------------------------------------------------------------------------------------------- protocol
class _ProtocolFailure(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _plugin_line(exc: BaseException) -> int | None:
    line = None
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == PLUGIN_FILENAME:
            line = tb.tb_lineno
        tb = tb.tb_next
    if line is None and isinstance(exc, SyntaxError) and exc.filename == PLUGIN_FILENAME:
        line = exc.lineno
    return line


def _error_payload(exc: BaseException) -> dict[str, object]:
    from ..strategy.base import LookAheadError, StrategyContractError, StrategyParamsError

    if isinstance(exc, _ProtocolFailure):
        kind = exc.kind
    elif isinstance(exc, SandboxViolation):
        kind = "sandbox"
    elif isinstance(exc, MemoryError):
        kind = "memory"
    elif isinstance(exc, (StrategyContractError, LookAheadError, StrategyParamsError)):
        kind = "contract"
    else:
        kind = "plugin_error"
    message = str(exc).replace("\r", " ").replace("\n", " ")
    return {"kind": kind, "type": type(exc).__name__, "message": message[:MAX_ERROR_CHARS], "line": _plugin_line(exc)}


def _frame(payload: object, name: str):  # -> pd.DataFrame
    import numpy as np
    import pandas as pd

    if not isinstance(payload, dict) or "time" not in payload:
        raise _ProtocolFailure("protocol", f"frame {name!r} is malformed")
    times = np.asarray(payload["time"], dtype=np.int64)
    data = {"time": pd.to_datetime(times, utc=True)}
    for column, values in payload.items():
        if column == "time":
            continue
        arr = np.asarray(values, dtype=np.float64)
        if arr.shape != times.shape:
            raise _ProtocolFailure("protocol", f"frame {name!r} column {column!r} has the wrong length")
        data[str(column)] = arr
    return pd.DataFrame(data)


class _Session:
    def __init__(self, cls: type) -> None:
        self.cls = cls
        self.strategy = cls()

    def describe(self) -> dict[str, object]:
        from ..strategy.base import Strategy

        cls = self.cls
        schema = getattr(cls, "param_schema", None)
        to_list = getattr(schema, "to_list", None)
        if to_list is None:
            raise _ProtocolFailure("contract", "param_schema must be a ParamSchema")
        overrides = sorted(n for n in ("scan", "first_valid_index", "warmup_margin_h4_bars", "prepare")
                           if getattr(cls, n) is not getattr(Strategy, n))
        return {"name": getattr(cls, "name", None), "version": getattr(cls, "version", None),
                "title_fa": getattr(cls, "title_fa", None), "class_name": cls.__name__,
                "param_schema": to_list(), "history_bars": getattr(cls, "history_bars", 1), "overrides": overrides}

    def _common(self, payload: dict):  # -> tuple
        from ..storage.account_settings import AccountSettings

        clean = self.strategy.clean_params(payload.get("params") or {})
        account = AccountSettings(**(payload.get("account") or {}))
        symbol = payload.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise _ProtocolFailure("protocol", "symbol missing")
        return clean, account, symbol

    def _meta(self, h1, h4, clean) -> dict[str, object]:
        first = self.strategy.first_valid_index(h1, h4, clean)
        margin = self.strategy.warmup_margin_h4_bars(clean)
        return {"first_valid_index": None if first is None else int(first), "warmup_margin_h4_bars": int(margin)}

    def scan(self, payload: dict) -> dict[str, object]:
        from ..strategy.signal import SignalCandidate

        clean, account, symbol = self._common(payload)
        h1, h4 = _frame(payload.get("h1"), "h1"), _frame(payload.get("h4"), "h4")
        started = time.perf_counter()
        candidates = self.strategy.scan(h1, h4, clean, account, symbol=symbol)
        elapsed = time.perf_counter() - started
        if not isinstance(candidates, (list, tuple)):
            raise _ProtocolFailure("contract", f"scan() returned {type(candidates).__name__}, not a list")
        dumped = []
        for cand in candidates:
            if not isinstance(cand, SignalCandidate):
                raise _ProtocolFailure("contract", f"scan() returned {type(cand).__name__}, not SignalCandidate")
            dumped.append(cand.model_dump(mode="json"))
        return {"candidates": dumped, **self._meta(h1, h4, clean), "plugin_s": round(elapsed, 6)}

    def meta(self, payload: dict) -> dict[str, object]:
        clean = self.strategy.clean_params(payload.get("params") or {})
        h1, h4 = _frame(payload.get("h1"), "h1"), _frame(payload.get("h4"), "h4")
        return self._meta(h1, h4, clean)

    def evaluate(self, payload: dict) -> dict[str, object]:
        from ..strategy.base import StrategyContext, evaluate_checked
        from ..strategy.params import params_hash

        clean, account, symbol = self._common(payload)
        h1, h4 = _frame(payload.get("h1"), "h1"), _frame(payload.get("h4"), "h4")
        if len(h1) == 0:
            raise _ProtocolFailure("protocol", "evaluate needs at least one H1 bar")
        ctx = StrategyContext(symbol=symbol, h1=h1, h4=h4, account=account, params=clean,
                              has_open_trade=bool(payload.get("has_open_trade", False)))
        cand = evaluate_checked(self.strategy, ctx, params_hash(clean))
        return {"candidate": None if cand is None else cand.model_dump(mode="json")}


def _warm_up() -> None:
    """Exercise the libraries once so their lazy imports happen before the lock."""
    import dataclasses  # noqa: F401
    import math  # noqa: F401
    import statistics  # noqa: F401
    import typing  # noqa: F401

    import numpy as np
    import numpy.lib.stride_tricks  # noqa: F401
    import numpy.random  # noqa: F401
    import numpy.typing  # noqa: F401
    import pandas as pd
    import pandas.api.types  # noqa: F401

    from .. import indicators, patterns  # noqa: F401
    from ..risk import sizing  # noqa: F401
    from ..storage.account_settings import AccountSettings
    from ..strategy import base, context, params, signal  # noqa: F401
    from ..strategy.params import ParamSchema, ParamSpec, params_hash
    from ..strategy.signal import SignalCandidate

    rng = np.random.default_rng(1)
    times = pd.date_range("2025-01-06", periods=64, freq="h", tz="UTC")
    close = 100.0 + np.cumsum(rng.normal(size=64))
    frame = pd.DataFrame({"time": times, "open": close, "high": close + 1, "low": close - 1, "close": close})
    s = frame["close"]
    _ = (s.rolling(5).mean(), s.rolling(5).std(), s.rolling(5).min(), s.rolling(5).max(), s.ewm(span=5).mean(),
         s.shift(1), s.diff(), s.pct_change(), s.cumsum(), s.expanding().mean(), s.rank(), s.clip(0, 1e9),
         s.where(s > 0), frame.groupby(frame["time"].dt.floor("4h")).agg(open=("open", "first")),
         pd.concat([s, s], axis=1).max(axis=1), frame.iloc[-5:], frame.tail(2), repr(frame.tail(2)),
         str(s.describe()), s.astype("float64"), s.to_numpy().tolist(), frame["time"].dt.hour, np.convolve(close, [1.0]),
         np.lib.stride_tricks.sliding_window_view(close, 3).mean(axis=1), pd.Timedelta(hours=1),
         pd.Timestamp(times[0]).to_pydatetime(), pd.isna(s).any(), s.fillna(0.0), s.round(2), s.abs(),
         np.nanmean(close), np.percentile(close, 50), statistics.mean([1.0, 2.0]), statistics.pstdev([1.0, 2.0]))
    indicators.wilder_atr(frame["high"], frame["low"], frame["close"], 14)
    indicators.sma_atr(frame["high"], frame["low"], frame["close"], 14)
    patterns.detect_reversals(frame)
    schema = ParamSchema([ParamSpec(name="n", type="int", default=3, min=1, max=10, label_fa="n")])
    clean = schema.validate({})[0]
    open_t = times[10].to_pydatetime()
    cand = SignalCandidate(strategy_name="warm_up", strategy_version=1, params_hash=params_hash(clean),
                           symbol="XAUUSD.x", direction="buy", setup="warm_up", line=None, setup_title_fa="گرم",
                           decision_time_utc=open_t + pd.Timedelta(hours=1).to_pytimedelta(),
                           confirmation_bar_open_utc=open_t, reference_price=100.0, stop_loss=99.0, rr=2.0,
                           pattern="warm", reason_fa="گرم کردن", extra={"x": 1.0})
    SignalCandidate.model_validate(json.loads(json.dumps(cand.model_dump(mode="json"))))
    schema.to_list()
    AccountSettings().model_dump()
    try:
        raise ValueError("warm-up")
    except ValueError as exc:
        _plugin_line(exc)


def _write(out, message: dict) -> None:  # type: ignore[no-untyped-def]
    data = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    out.write(data + b"\n")
    out.flush()


def run_worker() -> int:
    """Entry point of the worker process. Returns the exit code."""
    inp = sys.stdin.buffer
    out = sys.stdout.buffer
    sys.stdout = _Sink()  # type: ignore[assignment]
    sys.stderr = _Sink()  # type: ignore[assignment]
    sys.dont_write_bytecode = True

    line = inp.readline()
    try:
        go = json.loads(line)
    except ValueError:
        return 3
    if not isinstance(go, dict) or go.get("cmd") != "go" or not isinstance(go.get("source"), str):
        return 3
    boot_path = [p for p in go.get("sys_path") or [] if isinstance(p, str) and p]
    started = time.perf_counter()
    for entry in reversed(boot_path):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    for name in BLOCKED_MODULES:
        sys.modules[name] = None  # type: ignore[assignment]
    try:
        _warm_up()
        roots = _read_roots(boot_path)
        builtins_table = safe_builtins()
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        _write(out, {"id": 0, "ok": False, "error": {"kind": "boot", "type": type(exc).__name__,
                                                     "message": str(exc)[:MAX_ERROR_CHARS], "line": None}})
        return 4
    boot_s = time.perf_counter() - started
    install_lockdown(roots)

    source = go["source"]
    del go
    try:
        exec_started = time.perf_counter()
        session = _Session(load_plugin(source, builtins_table))
        description = session.describe()
        description.update({"protocol": PROTOCOL_VERSION, "boot_s": round(boot_s, 4),
                            "exec_s": round(time.perf_counter() - exec_started, 4)})
        _write(out, {"id": 0, "ok": True, "result": description})
    except BaseException as exc:  # noqa: BLE001 - a broken plugin is reported, never raised
        _write(out, {"id": 0, "ok": False, "error": _error_payload(exc)})
        return 5
    del source

    handlers = {"scan": session.scan, "evaluate": session.evaluate, "meta": session.meta}
    while True:
        line = inp.readline()
        if not line:
            return 0
        try:
            request = json.loads(line)
            rid = request["id"]
            cmd = request["cmd"]
        except (ValueError, KeyError, TypeError):
            return 6
        if cmd == "shutdown":
            _write(out, {"id": rid, "ok": True, "result": {}})
            return 0
        handler = handlers.get(cmd)
        try:
            if handler is None:
                raise _ProtocolFailure("protocol", f"unknown command {cmd!r}")
            result = handler(request.get("payload") or {})
            reply = {"id": rid, "ok": True, "result": result}
        except BaseException as exc:  # noqa: BLE001 - plugin failures go back to the parent
            reply = {"id": rid, "ok": False, "error": _error_payload(exc)}
        try:
            _write(out, reply)
        except (ValueError, TypeError) as exc:  # e.g. NaN in a result: never send invalid JSON
            _write(out, {"id": rid, "ok": False, "error": {"kind": "protocol", "type": type(exc).__name__,
                                                          "message": str(exc)[:MAX_ERROR_CHARS], "line": None}})


__all__ = ["BLOCKED_MODULES", "SandboxViolation", "install_lockdown", "new_import_allowed", "run_worker"]
