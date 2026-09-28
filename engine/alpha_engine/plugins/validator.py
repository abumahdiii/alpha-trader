"""Static (parent-side) validation of an uploaded strategy plugin -- the FIRST layer only.

The source is never imported or executed here: it is decoded, parsed with :func:`ast.parse` and walked.
Everything that passes still runs only inside the sandboxed worker (:mod:`.worker`), which re-applies the
import whitelist at runtime (:func:`import_allowed`) and adds the process-level guards.

Rules (Persian messages with line numbers in :attr:`StaticReport.errors_fa`):

* size <= :data:`MAX_SOURCE_BYTES` (256 KiB), strict UTF-8, no NUL bytes, parses as Python 3;
* imports: only the whitelist of user decision 6-الف -- ``numpy``, ``pandas``, ``math``, ``statistics``,
  ``dataclasses``, ``typing`` (+ ``from __future__ import ...``) and, with ``from ... import`` only, the engine
  helpers ``alpha_engine.indicators``, ``alpha_engine.patterns``, ``alpha_engine.risk.sizing`` and
  ``alpha_engine.strategy`` (``base``/``signal``/``params``/``context``). No relative imports, no ``import *``;
* no ``exec``/``eval``/``compile``/``__import__``/``open``/``input``/``breakpoint``/``globals``/``locals``/
  ``vars``/``getattr``/``setattr``/``delattr``/``hasattr``/``dir``/``type``/... (not even as plain names);
* no dunder names or attributes (``__class__``, ``__subclasses__``, ``__globals__``, ``__builtins__``, ...),
  no ``_private`` attribute except on ``self``/``cls``, no dunder text inside string literals;
* no module / introspection / I/O / clock names as identifiers or attributes (``os``, ``sys``, ``socket``,
  ``f_globals``, ``read_csv``, ``to_pickle``, ``now``, ...), no ``async`` code;
* none of the MT5 order/position/history API names (:data:`BANNED_NAMES`), anywhere -- identifiers,
  attributes, strings or comments -- and no mention of the MT5 Python package name;
* exactly ONE top-level class deriving from ``Strategy``, with literal ``name`` (slug, not reserved for a
  built-in system), ``version`` (int >= 1), ``title_fa`` (non-empty), a ``param_schema`` and an ``evaluate``
  method; it may not override ``validate_params``/``clean_params``/``identity``/``evaluate_checked`` or set
  ``source``/``code_sha256`` (the host sets those).

This module also owns the banned-name scanner shared with ``tests/test_no_order_calls.py``
(:data:`BANNED_NAMES`, :func:`find_violations`, :func:`find_source_violations`). It is therefore the ONE module
of the engine that legitimately contains those names, and that test excludes exactly this file.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

MAX_SOURCE_BYTES = 256 * 1024
MAX_TITLE_CHARS = 120
MAX_VERSION = 1_000_000
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# --------------------------------------------------------------------------------- banned MT5 order API names
BANNED_NAMES = frozenset({
    "order_send",
    "order_check",
    "order_calc_margin",
    "order_calc_profit",
    "positions_get",
    "positions_total",
    "orders_get",
    "orders_total",
    "history_orders_get",
    "history_orders_total",
    "history_deals_get",
    "history_deals_total",
})
# Word boundaries: "_" is a word char, so "history_orders_get" does not also report "orders_get".
BANNED_RE = re.compile(r"\b(" + "|".join(sorted(BANNED_NAMES)) + r")\b")
# The MT5 Python package name (any case, written without a space; prose "MetaTrader 5" is fine). Plugins never
# need to mention it.
MT5_PACKAGE_RE = re.compile(r"metatrader_?5", re.IGNORECASE)
DUNDER_TEXT_RE = re.compile(r"__\w+__")


def find_source_violations(source: str, label: str = "<source>") -> list[tuple[int, str, str]]:
    """``(line, kind, name)`` for each banned order-API name in ``source`` (AST pass + raw-text pass).

    1. AST: identifier, attribute, import alias, function/class name, keyword/argument name, or inside any
       string literal (so ``getattr(mt5, "x")`` is caught);
    2. raw text: comments and anything the AST pass misses; also works when the source does not parse.

    Names built at runtime (``"order" + "_send"``) cannot be seen statically -- see :mod:`.worker` for the
    runtime guards that make them useless in a plugin."""
    found: list[tuple[int, str, str]] = []
    try:
        tree = ast.parse(source, filename=label)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        tree = None
        found.append((0, "unparsable", label))
    if tree is not None:
        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
                found.append((line, "identifier", node.id))
            elif isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
                found.append((line, "attribute", node.attr))
            elif isinstance(node, ast.alias):
                for name in (node.name, node.asname):
                    if name and set(name.split(".")) & BANNED_NAMES:
                        found.append((line, "import", name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in BANNED_NAMES:
                found.append((line, "definition", node.name))
            elif isinstance(node, ast.keyword) and node.arg in BANNED_NAMES:
                found.append((line, "keyword", node.arg))
            elif isinstance(node, ast.arg) and node.arg in BANNED_NAMES:
                found.append((line, "argument", node.arg))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in BANNED_RE.finditer(node.value):
                    found.append((line, "string", match.group(1)))
    for lineno, text in enumerate(source.splitlines(), start=1):
        for match in BANNED_RE.finditer(text):
            found.append((lineno, "text", match.group(1)))
    return found


def find_violations(path: Path) -> list[tuple[int, str, str]]:
    """:func:`find_source_violations` of a file (read as UTF-8, undecodable bytes replaced)."""
    return find_source_violations(path.read_text(encoding="utf-8", errors="replace"), str(path))


# ------------------------------------------------------------------------------------------- import whitelist
# ``import X`` / ``import X as y`` / ``from X import ...``
PLAIN_IMPORT_MODULES = frozenset({
    "numpy", "numpy.random", "numpy.typing", "numpy.lib.stride_tricks",
    "pandas", "pandas.api.types",
    "math", "statistics", "dataclasses", "typing",
})
# Engine helpers: ``from X import ...`` only (a plain ``import alpha_engine.x`` binds the whole package).
ENGINE_MODULES = frozenset({
    "alpha_engine.indicators", "alpha_engine.indicators.atr", "alpha_engine.indicators.mtf",
    "alpha_engine.indicators.regression_channel",
    "alpha_engine.patterns", "alpha_engine.patterns.candles",
    "alpha_engine.risk.sizing",
    "alpha_engine.strategy", "alpha_engine.strategy.base", "alpha_engine.strategy.signal",
    "alpha_engine.strategy.params", "alpha_engine.strategy.context",
})
# Packages whose imported names must themselves be whitelisted engine modules (``from alpha_engine import strategy``).
ENGINE_PARENT_PACKAGES = frozenset({"alpha_engine", "alpha_engine.risk"})
FUTURE_MODULE = "__future__"
ALLOWED_TOP_LEVEL = frozenset({"numpy", "pandas", "math", "statistics", "dataclasses", "typing", "alpha_engine"})

# ------------------------------------------------------------------------------------------- name deny-lists
FORBIDDEN_BUILTINS = frozenset({
    "exec", "eval", "compile", "__import__", "open", "input", "breakpoint", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "hasattr", "dir", "help", "exit", "quit", "memoryview", "type",
    "__build_class__", "copyright", "credits", "license",
})
# Modules, process/OS/introspection handles and secret/config accessors: never an identifier or attribute.
FORBIDDEN_IDENTIFIERS = frozenset({
    "os", "sys", "nt", "posix", "subprocess", "socket", "ssl", "select", "selectors", "ctypes", "importlib",
    "builtins", "shutil", "pathlib", "io", "pickle", "marshal", "gc", "inspect", "types", "code", "codeop",
    "threading", "multiprocessing", "concurrent", "asyncio", "signal", "tempfile", "glob", "urllib", "http",
    "requests", "winreg", "msvcrt", "platform", "sqlite3", "mmap", "pty", "runpy", "zipimport", "zipfile",
    "tarfile", "codecs", "operator", "functools", "weakref", "copyreg", "linecache", "traceback", "warnings",
    "webbrowser", "ftplib", "smtplib", "pyarrow", "numexpr", "bottleneck", "mt5", "dotenv",
    "get_settings", "load_settings", "logging_setup", "environ", "getenv", "putenv", "load_dotenv",
    "dotenv_values", "mt5_adapter", "market_data",
    # frame / code / traceback introspection
    "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "f_trace", "tb_frame", "tb_next", "gi_frame",
    "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code", "co_code", "co_consts", "mro", "with_traceback",
    "func_globals", "func_code", "modules", "meta_path", "path_hooks", "addaudithook", "audit",
    # process / file system helpers (numpy/pandas/os spellings)
    "system", "popen", "spawn", "startfile", "remove", "unlink", "rmdir", "rename", "makedirs", "mkdir",
    "chdir", "listdir", "scandir", "walk", "getcwd", "tofile", "fromfile", "load", "loads", "save", "savez",
    "savez_compressed", "savetxt", "loadtxt", "genfromtxt", "memmap", "open_memmap", "DataSource",
    "ctypeslib", "f2py", "distutils", "testing", "show_config", "show_versions", "query",
    # wall clock
    "now", "utcnow", "today", "time_ns", "perf_counter", "monotonic",
})
# ``to_*`` attributes that are pure conversions (every other ``to_*`` / ``read_*`` is file or network I/O).
SAFE_TO_ATTRIBUTES = frozenset({
    "to_numpy", "to_list", "to_dict", "to_frame", "to_series", "to_datetime", "to_timedelta", "to_numeric",
    "to_period", "to_timestamp", "to_pydatetime", "to_datetime64", "to_offset", "to_flat_index",
    "to_records", "to_string",
})
CLOCK_STRINGS = frozenset({"now", "today", "utcnow"})
# Strategy members the host owns: a plugin may not override / set them.
FORBIDDEN_OVERRIDES = frozenset({"validate_params", "clean_params", "identity", "evaluate_checked"})
HOST_ATTRIBUTES = frozenset({"source", "code_sha256"})
STRATEGY_BASE = "Strategy"


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _name_problem(name: str, *, attribute: bool = False, own: bool = False) -> str | None:
    """Persian reason why ``name`` may not appear as an identifier / attribute / imported name, else None.

    ``attribute``: an attribute access (``x.name``); ``own``: on ``self``/``cls`` (the plugin's own members:
    only the order-API, MT5 and dunder rules apply)."""
    if name in BANNED_NAMES:
        return f"نام «{name}» از API سفارش/پوزیشن متاتریدر است و در پلاگین ممنوع است."
    if MT5_PACKAGE_RE.search(name):
        return "هر نوع ارجاع به پکیج متاتریدر در پلاگین ممنوع است."
    if name.startswith("__"):
        return f"نام‌های دو-زیرخطی مثل «{name}» مجاز نیستند."
    if own:
        return None
    if name in FORBIDDEN_BUILTINS:
        return f"استفاده از «{name}» مجاز نیست."
    if name in FORBIDDEN_IDENTIFIERS:
        return f"نام «{name}» (ماژول سیستمی، فایل، شبکه، ساعت یا درون‌نگری) در پلاگین مجاز نیست."
    if attribute and (name.startswith("read_") or (name.startswith("to_") and name not in SAFE_TO_ATTRIBUTES)):
        return f"«{name}» عملیات فایل/شبکه است و در پلاگین مجاز نیست."
    return None


def import_allowed(module: str | None, names: Sequence[str] | None = None, level: int = 0) -> str | None:
    """``None`` if the import is on the whitelist, else a Persian reason.

    ``names`` = the imported names of ``from module import names`` (``None`` for a plain ``import module``).
    Used by the static check AND by the worker's runtime ``__import__`` guard."""
    if level:
        return "import نسبی (با نقطه) مجاز نیست."
    if not module or not isinstance(module, str):
        return "import نامعتبر است."
    if MT5_PACKAGE_RE.search(module):
        return "import پکیج متاتریدر در پلاگین ممنوع است."
    if names is None:
        if module in PLAIN_IMPORT_MODULES:
            return None
        if module in ENGINE_MODULES or module.split(".")[0] == "alpha_engine":
            return (f"ماژول «{module}» را فقط به شکل «from {module} import ...» وارد کنید "
                    "(import مستقیم کل پکیج engine را در دسترس می‌گذارد).")
        return f"import ماژول «{module}» مجاز نیست (فهرست مجاز: numpy، pandas، math، statistics، dataclasses، typing و helperهای engine)."
    names = tuple(names)
    if not names:
        return "import نامعتبر است."
    for name in names:
        if not isinstance(name, str) or name == "*":
            return "«import *» مجاز نیست."
        if module != FUTURE_MODULE:
            if name.startswith("_"):
                return f"وارد کردن نام خصوصی «{name}» مجاز نیست."
            problem = _name_problem(name, attribute=True)
            if problem is not None:
                return problem
    if module == FUTURE_MODULE or module in PLAIN_IMPORT_MODULES or module in ENGINE_MODULES:
        return None
    if module in ENGINE_PARENT_PACKAGES:
        bad = [n for n in names if f"{module}.{n}" not in ENGINE_MODULES]
        if not bad:
            return None
        return f"از «{module}» فقط زیرماژول‌های مجاز قابل import هستند (نه «{bad[0]}»)."
    return f"import از ماژول «{module}» مجاز نیست (فهرست مجاز: numpy، pandas، math، statistics، dataclasses، typing و helperهای engine)."


# ------------------------------------------------------------------------------------------------- the report
@dataclass
class StaticReport:
    """Result of :func:`validate_source`. ``ok`` iff ``errors_fa`` is empty."""

    sha256: str | None = None
    size_bytes: int = 0
    name: str | None = None
    version: int | None = None
    title_fa: str | None = None
    class_name: str | None = None
    errors_fa: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)  # names of the passed check groups (for the report)

    @property
    def ok(self) -> bool:
        return not self.errors_fa

    def add(self, line: int | None, message: str) -> None:
        text = message if not line else f"خط {line}: {message}"
        if text not in self.errors_fa and len(self.errors_fa) < 200:
            self.errors_fa.append(text)

    def summary(self) -> dict[str, object]:
        return {"ok": self.ok, "size_bytes": self.size_bytes, "sha256": self.sha256, "name": self.name,
                "version": self.version, "class_name": self.class_name, "checks": list(self.checks),
                "errors_fa": list(self.errors_fa)}


def decode_source(data: str | bytes, report: StaticReport) -> str | None:
    """UTF-8 text of ``data`` (size, encoding and NUL checks recorded in ``report``); ``None`` on failure."""
    raw = data.encode("utf-8", errors="surrogatepass") if isinstance(data, str) else bytes(data)
    report.size_bytes = len(raw)
    report.sha256 = hashlib.sha256(raw).hexdigest()  # replaced by the hash of the decoded text below
    if len(raw) == 0:
        report.add(None, "فایل خالی است.")
        return None
    if len(raw) > MAX_SOURCE_BYTES:
        report.add(None, f"حجم فایل ({len(raw)} بایت) بیشتر از سقف مجاز {MAX_SOURCE_BYTES} بایت (۲۵۶ کیلوبایت) است.")
        return None
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        report.add(None, f"فایل باید با کدگذاری UTF-8 ذخیره شده باشد (بایت نامعتبر در موقعیت {exc.start}).")
        return None
    if "\x00" in text:
        report.add(None, "فایل شامل کاراکتر NUL است و متن پایتون معتبر نیست.")
        return None
    if text.startswith("﻿"):
        text = text[1:]
    report.sha256 = source_sha256(text)
    return text


def source_sha256(text: str) -> str:
    """The plugin's identity: SHA-256 of the UTF-8 bytes of its decoded text (BOM removed) -- exactly the bytes
    stored under ``data/strategies/plugins/``."""
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _literal(node: ast.expr | None) -> tuple[bool, object]:
    if isinstance(node, ast.Constant):
        return True, node.value
    return False, None


def _base_is_strategy(base: ast.expr) -> bool:
    return (isinstance(base, ast.Name) and base.id == STRATEGY_BASE) or (
        isinstance(base, ast.Attribute) and base.attr == STRATEGY_BASE)


def _check_nodes(tree: ast.Module, report: StaticReport) -> None:
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if isinstance(node, ast.Import):
            for alias in node.names:
                problem = import_allowed(alias.name, None, 0)
                if problem is not None:
                    report.add(line, problem)
                if alias.asname:
                    problem = _name_problem(alias.asname)
                    if problem is not None:
                        report.add(line, problem)
        elif isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names]
            problem = import_allowed(node.module, names, node.level or 0)
            if problem is not None:
                report.add(line, problem)
            for alias in node.names:
                if alias.asname:
                    problem = _name_problem(alias.asname)
                    if problem is not None:
                        report.add(line, problem)
        elif isinstance(node, ast.Name):
            problem = _name_problem(node.id)
            if problem is not None:
                report.add(line, problem)
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            own = isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
            if attr.startswith("__"):
                report.add(line, f"دسترسی به ویژگی دو-زیرخطی «{attr}» مجاز نیست.")
            elif attr.startswith("_") and not own:
                report.add(line, f"دسترسی به ویژگی خصوصی «{attr}» فقط روی self/cls مجاز است.")
            else:
                problem = _name_problem(attr, attribute=True, own=own)
                if problem is not None:
                    report.add(line, problem)
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            report.add(line, "کد async در پلاگین مجاز نیست.")
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if node.name.startswith("__"):
                report.add(line, f"تعریف «{node.name}» (نام دو-زیرخطی) مجاز نیست.")
            else:
                problem = _name_problem(node.name, own=True)
                if problem is not None:
                    report.add(line, problem)
        elif isinstance(node, ast.arg):
            if node.arg.startswith("__"):
                report.add(line, f"آرگومان «{node.arg}» (نام دو-زیرخطی) مجاز نیست.")
            else:
                problem = _name_problem(node.arg, own=True)
                if problem is not None:
                    report.add(line, problem)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            if node.arg.startswith("__") or node.arg in BANNED_NAMES:
                report.add(line, f"آرگومان «{node.arg}» مجاز نیست.")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if MT5_PACKAGE_RE.search(text):
                report.add(line, "هر نوع ارجاع به پکیج متاتریدر در پلاگین ممنوع است.")
            if DUNDER_TEXT_RE.search(text):
                report.add(line, "متن‌هایی به شکل «__نام__» در رشته‌ها مجاز نیستند.")
            if text.strip().lower() in CLOCK_STRINGS:
                report.add(line, "استفاده از ساعت سیستم («now»/«today») در پلاگین مجاز نیست.")
        elif isinstance(node, ast.Global):
            for name in node.names:
                problem = _name_problem(name)
                if problem is not None:
                    report.add(line, problem)


def _check_strategy_class(tree: ast.Module, report: StaticReport, reserved: frozenset[str]) -> None:
    top_classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    strategy_classes = [c for c in top_classes if any(_base_is_strategy(b) for b in c.bases)]
    nested = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n not in top_classes
              and any(_base_is_strategy(b) for b in n.bases)]
    for cls in nested:
        report.add(cls.lineno, "کلاس Strategy باید در سطح بالای فایل تعریف شود.")
    if len(strategy_classes) != 1:
        report.add(None, f"فایل باید دقیقا یک کلاس مشتق از Strategy داشته باشد (تعداد یافته‌شده: {len(strategy_classes)}).")
        return
    cls = strategy_classes[0]
    report.class_name = cls.name
    derived = [c for c in top_classes if c is not cls and any(isinstance(b, ast.Name) and b.id == cls.name
                                                              for b in c.bases)]
    for other in derived:
        report.add(other.lineno, f"کلاس «{other.name}» از کلاس سیستم ارث می‌برد؛ فقط یک کلاس سیستم مجاز است.")

    assigned: dict[str, ast.expr | None] = {}
    methods: dict[str, int] = {}
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            assigned[stmt.target.id] = stmt.value
        elif isinstance(stmt, ast.FunctionDef):
            methods[stmt.name] = stmt.lineno

    for attr in sorted(HOST_ATTRIBUTES & set(assigned)):
        report.add(cls.lineno, f"ویژگی «{attr}» را engine تنظیم می‌کند و در پلاگین نباید مقدار بگیرد.")
    for method in sorted(FORBIDDEN_OVERRIDES & (set(methods) | set(assigned))):
        report.add(methods.get(method, cls.lineno), f"بازنویسی «{method}» در پلاگین مجاز نیست "
                                                    "(اعتبارسنجی پارامترها فقط با param_schema انجام می‌شود).")
    if "evaluate" not in methods:
        report.add(cls.lineno, "کلاس سیستم باید متد «evaluate(self, ctx)» را تعریف کند.")
    if "param_schema" not in assigned:
        report.add(cls.lineno, "کلاس سیستم باید «param_schema = ParamSchema([...])» را تعریف کند.")

    ok, name = _literal(assigned.get("name"))
    if not ok or not isinstance(name, str):
        report.add(cls.lineno, "ویژگی «name» باید یک رشته ثابت (literal) باشد، مثلا name = \"ma_cross_demo\".")
    elif not NAME_RE.fullmatch(name):
        report.add(cls.lineno, f"نام سیستم «{name}» نامعتبر است: فقط حروف کوچک انگلیسی، رقم و «_»، شروع با حرف، حداکثر ۶۴ کاراکتر.")
    elif name in reserved:
        report.add(cls.lineno, f"نام «{name}» متعلق به یک سیستم داخلی است و برای پلاگین رزرو شده است.")
    else:
        report.name = name
    ok, version = _literal(assigned.get("version"))
    if not ok or isinstance(version, bool) or not isinstance(version, int):
        report.add(cls.lineno, "ویژگی «version» باید یک عدد صحیح ثابت باشد، مثلا version = 1.")
    elif not 1 <= version <= MAX_VERSION:
        report.add(cls.lineno, f"نسخه سیستم باید بین ۱ و {MAX_VERSION} باشد.")
    else:
        report.version = version
    ok, title = _literal(assigned.get("title_fa"))
    if not ok or not isinstance(title, str) or not title.strip():
        report.add(cls.lineno, "ویژگی «title_fa» باید یک رشته فارسی ثابت و غیرخالی باشد.")
    elif len(title) > MAX_TITLE_CHARS:
        report.add(cls.lineno, f"عنوان سیستم حداکثر {MAX_TITLE_CHARS} کاراکتر است.")
    else:
        report.title_fa = title.strip()


def validate_source(data: str | bytes, *, reserved_names: Iterable[str] = ()) -> StaticReport:
    """Run every static check on ``data`` (``bytes`` as uploaded, or ``str``). Never executes it."""
    from ..strategy.registry import RESERVED_NAMES  # late: keep this module importable on its own

    report = StaticReport()
    text = decode_source(data, report)
    if text is None:
        return report
    report.checks.append("size_encoding")
    try:
        tree = ast.parse(text, filename="<plugin>", mode="exec")
    except SyntaxError as exc:
        report.add(exc.lineno, f"خطای نحوی پایتون: {exc.msg}.")
        return report
    except (ValueError, RecursionError, MemoryError, OverflowError):
        report.add(None, "فایل قابل تجزیه نیست (بیش از حد تودرتو یا نامعتبر).")
        return report
    report.checks.append("syntax")
    for line, _kind, name in find_source_violations(text, "<plugin>"):
        report.add(line or None, f"نام «{name}» از API سفارش/پوزیشن متاتریدر است و در پلاگین ممنوع است.")
    for lineno, row in enumerate(text.splitlines(), start=1):
        if MT5_PACKAGE_RE.search(row):
            report.add(lineno, "هر نوع ارجاع به پکیج متاتریدر در پلاگین ممنوع است.")
    report.checks.append("order_api_names")
    _check_nodes(tree, report)
    report.checks.append("imports_names_attributes")
    _check_strategy_class(tree, report, frozenset(RESERVED_NAMES) | frozenset(reserved_names))
    report.checks.append("strategy_class")
    return report


__all__ = [
    "BANNED_NAMES",
    "BANNED_RE",
    "ENGINE_MODULES",
    "MAX_SOURCE_BYTES",
    "PLAIN_IMPORT_MODULES",
    "StaticReport",
    "find_source_violations",
    "find_violations",
    "import_allowed",
    "source_sha256",
    "validate_source",
]
