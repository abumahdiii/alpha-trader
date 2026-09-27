"""Safety net for the signal-only rule (``.claude/rules/03_trading_safety.md`` section 1).

Scans every ``.py`` file under ``engine/`` (excluding ``.venv``, ``__pycache__`` and this file) and fails
if a banned MetaTrader 5 order/position/history API name appears:

1. AST pass -- as an identifier (``Name``), attribute (``mt5.x``), import alias, function/class name,
   keyword argument, or inside any string literal (so ``getattr(mt5, "x")`` is caught too);
2. plain-text regex pass over the raw source -- catches comments and anything the AST pass misses,
   and still works on files that fail to parse.

Known limit: names built dynamically at runtime (``getattr(mt5, "order" + "_send")``,
``"".join([...])``, ``codecs``/base64 decoding, etc.) cannot be detected by static scanning. This test
is a tripwire against accidents, not a sandbox; code review remains mandatory for these files.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()
EXCLUDED_DIR_NAMES = frozenset({".venv", "venv", "__pycache__", ".pytest_cache"})

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


def iter_python_files(root: Path, exclude_files: Iterable[Path] = ()) -> Iterator[Path]:
    excluded = {p.resolve() for p in exclude_files}
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIR_NAMES for part in rel_parts[:-1]):
            continue
        if path.resolve() in excluded:
            continue
        yield path


def find_violations(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(line, kind, name)`` for each banned-name occurrence in ``path``."""
    source = path.read_text(encoding="utf-8", errors="replace")
    found: list[tuple[int, str, str]] = []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        tree = None
        found.append((0, "unparsable", path.name))

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


def scan_tree(root: Path, exclude_files: Iterable[Path] = ()) -> dict[Path, list[tuple[int, str, str]]]:
    results: dict[Path, list[tuple[int, str, str]]] = {}
    for path in iter_python_files(root, exclude_files):
        violations = find_violations(path)
        if violations:
            results[path] = violations
    return results


# --- the real check --------------------------------------------------------------------------------

def test_engine_contains_no_order_api_names() -> None:
    scanned = list(iter_python_files(ENGINE_DIR, exclude_files=[THIS_FILE]))
    # Guard against a broken glob producing a false green.
    assert ENGINE_DIR / "alpha_engine" / "app.py" in scanned
    assert THIS_FILE not in scanned
    assert not any(".venv" in p.relative_to(ENGINE_DIR).parts for p in scanned)

    violations = scan_tree(ENGINE_DIR, exclude_files=[THIS_FILE])
    report = "\n".join(
        f"{path.relative_to(ENGINE_DIR)}:{line}: {kind} {name}"
        for path, items in violations.items()
        for line, kind, name in items
    )
    assert not violations, "Banned MT5 order/position API names found (signal-only rule):\n" + report


# --- self-tests on planted violations (tmp files only) ---------------------------------------------

PLANTED = '''
import MetaTrader5 as mt5
from MetaTrader5 import order_check as oc

def run(req):
    mt5.order_send(req)
    fn = getattr(mt5, "positions_get")
    label = f"calling history_deals_get now"
    return orders_total
# a comment mentioning order_calc_margin
'''


def _kinds(violations: list[tuple[int, str, str]]) -> set[tuple[str, str]]:
    return {(kind, name) for _, kind, name in violations}


def test_scanner_detects_planted_violations(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(PLANTED, encoding="utf-8")
    kinds = _kinds(find_violations(bad))
    assert ("attribute", "order_send") in kinds
    assert ("string", "positions_get") in kinds
    assert ("string", "history_deals_get") in kinds
    assert ("identifier", "orders_total") in kinds
    assert ("import", "order_check") in kinds
    assert ("text", "order_calc_margin") in kinds  # only in a comment


def test_scanner_passes_clean_file_and_respects_word_boundaries(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text(
        "import MetaTrader5 as mt5\nrates = mt5.copy_rates_range('EURUSD', 1, 0, 1)\n"
        "my_orders_getter = 1\nreorder_send_queue = 2\n",
        encoding="utf-8",
    )
    assert find_violations(clean) == []


def test_scanner_word_boundary_reports_longest_name_only(tmp_path: Path) -> None:
    f = tmp_path / "hist.py"
    f.write_text("x = mt5.history_orders_get()\n", encoding="utf-8")
    names = {name for _, _, name in find_violations(f)}
    assert names == {"history_orders_get"}


def test_scanner_flags_unparsable_file_and_still_text_scans(tmp_path: Path) -> None:
    f = tmp_path / "broken.py"
    f.write_text("def (:\n  mt5.order_send(x)\n", encoding="utf-8")
    kinds = _kinds(find_violations(f))
    assert ("unparsable", "broken.py") in kinds
    assert ("text", "order_send") in kinds


def test_tree_scan_excludes_venv_pycache_and_given_file(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "mt5_stub.py").write_text("order_send = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "__pycache__" / "x.py").write_text("order_send = 1\n", encoding="utf-8")
    me = tmp_path / "pkg" / "self_test.py"
    me.write_text("BANNED = ['order_send']\n", encoding="utf-8")
    offender = tmp_path / "pkg" / "adapter.py"
    offender.write_text("mt5.positions_total()\n", encoding="utf-8")

    results = scan_tree(tmp_path, exclude_files=[me])
    assert set(results) == {offender}
