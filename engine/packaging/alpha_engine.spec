# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Alpha Trader engine: onedir, console-less, no UPX.

Build (Rule 8: only after the user approved it; see engine/packaging/README.md), from the repo root:

    engine\\.venv\\Scripts\\python.exe -m PyInstaller engine\\packaging\\alpha_engine.spec ^
        --noconfirm --clean --distpath build\\engine_dist --workpath build\\engine_work

Output: build\\engine_dist\\alpha_engine\\alpha_engine.exe (+ _internal\\). The package puts that folder at
<app>\\engine\\alpha_engine\\ next to the Flutter release build.

Design notes
- onedir, not onefile: onefile unpacks to %TEMP% on every start (slow, and the classic antivirus
  false-positive pattern) and runs the app in a *second* process, which would break the /health pid
  that the Flutter launcher uses to kill an attached orphan. onedir runs in one process.
- console=False: no console window next to the app. engine/packaging/entry.py repairs missing std
  streams (uvicorn needs them) and forces UTF-8 on the pipes Flutter provides.
- upx=False: UPX-packed binaries are a frequent antivirus false positive and save little here.
- Tests are never bundled: engine/tests is not on pathex and the test-only packages are excluded.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

# SPECPATH is injected by PyInstaller: the folder containing this spec (engine/packaging).
PACKAGING_DIR = Path(SPECPATH).resolve()  # noqa: F821 - defined by PyInstaller
ENGINE_DIR = PACKAGING_DIR.parent
REPO_ROOT = ENGINE_DIR.parent
ICON = REPO_ROOT / "windows" / "runner" / "resources" / "app_icon.ico"


def _submodules(package):
    """collect_submodules that tolerates a package missing from the build venv (warns, returns [])."""
    try:
        return collect_submodules(package)
    except Exception as exc:  # pragma: no cover - build-time only
        print(f"alpha_engine.spec: WARNING could not collect {package}: {exc}")
        return []


def _metadata(*packages):
    out = []
    for package in packages:
        try:
            out += copy_metadata(package)
        except Exception as exc:  # pragma: no cover - build-time only
            print(f"alpha_engine.spec: WARNING no metadata for {package}: {exc}")
    return out


hiddenimports = []
# The engine itself: route/strategy registries may be resolved by name at runtime.
hiddenimports += _submodules("alpha_engine")
# uvicorn resolves loop/http/ws/lifespan implementations from strings ("auto" -> httptools/h11,
# websockets/wsproto, asyncio loop, lifespan.on), which static analysis cannot see.
hiddenimports += _submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]
hiddenimports += _submodules("websockets")
hiddenimports += _submodules("httptools")
hiddenimports += _submodules("h11")
hiddenimports += _submodules("anyio")  # starlette's backend, loaded via importlib
hiddenimports += _submodules("fastapi")
hiddenimports += _submodules("starlette")
hiddenimports += _submodules("pydantic")
hiddenimports += ["pydantic_core", "pydantic_core._pydantic_core", "annotated_types"]
hiddenimports += ["dotenv", "dotenv.main"]
# Data stack: PyInstaller ships hooks for numpy/pandas/pyarrow; these are belt and braces.
hiddenimports += ["numpy", "pandas", "pyarrow", "pyarrow.parquet", "pyarrow.lib", "tzdata"]
# MetaTrader5 is imported lazily inside mt5_adapter (and its _core*.pyd from __init__).
hiddenimports += _submodules("MetaTrader5")
hiddenimports += ["MetaTrader5", "MetaTrader5._core"]
# Strategy-plugin worker (``<exe> --plugin-worker``, dispatched by packaging/entry.py): the worker imports its
# modules lazily, and plugin code may import these whitelisted modules at runtime (they are pre-imported in the
# worker before its lockdown, so they must be in the bundle). ``_submodules("alpha_engine")`` above already
# covers alpha_engine.plugins.*; listed explicitly so a refactor of that helper cannot drop the worker.
hiddenimports += [
    "alpha_engine.plugins", "alpha_engine.plugins.worker", "alpha_engine.plugins.validator",
    "alpha_engine.plugins.host", "alpha_engine.plugins.checks", "alpha_engine.plugins.fixture",
    "alpha_engine.plugins.store", "alpha_engine.plugins.template", "alpha_engine.routes.plugins",
    "numpy.random", "numpy.typing", "numpy.lib.stride_tricks", "pandas.api.types", "statistics", "dataclasses",
]

# Package data. None needed today.
# SLOT (strategy-plugin upload): filled -- the downloadable template is a STRING constant
# (alpha_engine/plugins/template.py, TEMPLATE_SOURCE), so it ships inside the bytecode and needs no datas entry;
# the worker entry points are in the hiddenimports above.
datas = []
datas += _metadata("fastapi", "starlette", "pydantic", "pydantic_core", "uvicorn", "websockets")

excludes = [
    # test-only / dev-only
    "pytest", "_pytest", "pluggy", "iniconfig", "tests", "fixtures", "httpx", "httpcore",
    "PyInstaller",
    # never used by the engine; keeps the bundle small
    "tkinter", "matplotlib", "IPython", "jupyter", "notebook", "scipy", "PyQt5", "PySide6",
    "watchfiles",  # uvicorn --reload only; the engine runs with reload=False
]

a = Analysis(  # noqa: F821 - PyInstaller spec globals
    [str(PACKAGING_DIR / "entry.py")],
    pathex=[str(ENGINE_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,  # keep asserts/docstrings: pydantic and FastAPI rely on docstrings/annotations
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir
    name="alpha_engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON) if ICON.is_file() else None,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="alpha_engine",
)
