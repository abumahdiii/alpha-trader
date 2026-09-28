"""Static checks for ``engine/packaging`` (nothing is built; PyInstaller is not needed).

* ``entry.py``: std-stream repair + UTF-8 reconfigure used by the frozen, console-less exe.
* ``alpha_engine.spec``: valid Python, onedir, console-less, no UPX, required hidden imports, tests excluded.
* ``requirements-build.txt``: PyInstaller only there, never in the runtime requirements.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import py_compile
import re
import sys
from pathlib import Path

import pytest

ENGINE_DIR = Path(__file__).resolve().parents[1]
PACKAGING_DIR = ENGINE_DIR / "packaging"
SPEC = PACKAGING_DIR / "alpha_engine.spec"
ENTRY = PACKAGING_DIR / "entry.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("alpha_engine_packaging_entry", ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- entry.py --------------------------------------------------------------------------------------

def test_entry_has_no_side_effects_on_import() -> None:
    module = _load_entry()
    assert callable(module.run)
    assert callable(module.ensure_std_streams)


def test_ensure_std_streams_replaces_only_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_entry()
    keep = io.StringIO()
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", keep)
    monkeypatch.setattr(sys, "stdin", None)
    replaced = module.ensure_std_streams()
    try:
        assert replaced == ["stdout", "stdin"]
        assert sys.stderr is keep
        # uvicorn's formatter calls isatty() -- must not raise on the replacement (Windows "nul" is a
        # character device, so the value itself is platform dependent and irrelevant).
        assert isinstance(sys.stdout.isatty(), bool)
        sys.stdout.write("discarded")
    finally:
        sys.stdout.close()
        sys.stdin.close()


def test_ensure_std_streams_noop_when_present() -> None:
    module = _load_entry()
    # pytest's capture objects are real streams, never None.
    assert module.ensure_std_streams() == []


def test_configure_utf8_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_entry()
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())  # no reconfigure() -> skipped, no error
    assert module.configure_utf8_streams() == ["stdout"]
    assert out.encoding == "utf-8"
    assert out.line_buffering is True
    out.write("بک‌تست\n")  # Persian text must not raise
    out.flush()
    assert "بک‌تست".encode() in out.buffer.getvalue()


def test_entry_delegates_to_package_main() -> None:
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert any(n.module == "alpha_engine.__main__" and n.names[0].name == "main" for n in imports)
    assert "freeze_support" in ENTRY.read_text(encoding="utf-8")


def test_entry_dispatches_the_plugin_worker_before_importing_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """``alpha_engine.exe --plugin-worker`` must reach ``run_worker`` without importing ``alpha_engine.__main__``
    (uvicorn / app / settings) -- the same guarantee as ``python -m alpha_engine --plugin-worker``."""
    import sys
    import types

    module = _load_entry()
    calls: list[str] = []
    fake_worker = types.ModuleType("alpha_engine.plugins.worker")
    fake_worker.run_worker = lambda: calls.append("worker") or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alpha_engine.plugins.worker", fake_worker)
    monkeypatch.setitem(sys.modules, "alpha_engine.__main__", None)  # importing it would raise ImportError
    monkeypatch.setattr(module.multiprocessing, "freeze_support", lambda: calls.append("freeze_support"))
    monkeypatch.setattr(sys, "argv", ["alpha_engine.exe", "--plugin-worker"])
    assert module.run() == 0
    assert calls == ["worker"]
    source = ENTRY.read_text(encoding="utf-8")
    assert source.index("run_worker") < source.index("from alpha_engine.__main__ import main")


# --- alpha_engine.spec -----------------------------------------------------------------------------

def test_spec_is_valid_python(tmp_path: Path) -> None:
    py_compile.compile(str(SPEC), cfile=str(tmp_path / "spec.pyc"), doraise=True)


def _spec_call_kwargs(name: str) -> dict[str, ast.expr]:
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
            return {kw.arg: kw.value for kw in node.keywords if kw.arg}
    raise AssertionError(f"{name}(...) not found in spec")


def test_spec_is_onedir_console_less_without_upx() -> None:
    exe = _spec_call_kwargs("EXE")
    assert ast.literal_eval(exe["exclude_binaries"]) is True  # onedir
    assert ast.literal_eval(exe["name"]) == "alpha_engine"
    assert ast.literal_eval(exe["console"]) is False
    assert ast.literal_eval(exe["upx"]) is False
    coll = _spec_call_kwargs("COLLECT")
    assert ast.literal_eval(coll["name"]) == "alpha_engine"
    assert ast.literal_eval(coll["upx"]) is False


@pytest.mark.parametrize("needle", [
    '"alpha_engine"', '"uvicorn"', "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", '"websockets"', '"fastapi"',
    '"pydantic"', "pydantic_core", '"pandas"', '"pyarrow"', '"numpy"', '"MetaTrader5"',
    "MetaTrader5._core", '"dotenv"', "alpha_engine.plugins.worker", "alpha_engine.plugins.validator",
])
def test_spec_hidden_imports(needle: str) -> None:
    text = SPEC.read_text(encoding="utf-8")
    hidden_block = text[text.index("hiddenimports = []"): text.index("datas = []")]
    assert needle in hidden_block


def test_spec_excludes_tests_and_uses_entry_wrapper() -> None:
    text = SPEC.read_text(encoding="utf-8")
    excludes = text[text.index("excludes = ["): text.index("a = Analysis(")]
    for name in ('"pytest"', '"_pytest"', '"tests"', '"fixtures"', '"PyInstaller"'):
        assert name in excludes
    assert 'PACKAGING_DIR / "entry.py"' in text
    assert "pathex=[str(ENGINE_DIR)]" in text  # engine/tests is never on the path
    assert "SLOT (strategy-plugin upload" in text


def test_spec_never_references_secrets_or_data() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert ".env" not in text
    assert "config.local" not in text
    assert not re.search(r'["\']data["\']\s*\)', text)


# --- requirements ----------------------------------------------------------------------------------

def test_pyinstaller_only_in_build_requirements() -> None:
    build = (ENGINE_DIR / "requirements-build.txt").read_text(encoding="utf-8")
    runtime = (ENGINE_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^pyinstaller[>=<~]=?\s*6\.", build, re.MULTILINE | re.IGNORECASE)
    assert "-r requirements.txt" in build
    assert "pyinstaller" not in runtime.lower()
