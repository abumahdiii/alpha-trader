"""Shared pytest setup for the engine.

* Makes ``alpha_engine`` importable when pytest runs from ``engine/``.
* Hermetic environment: before any engine import, ``ALPHA_TRADER_ENV_FILE`` points at an empty fake
  file and ``ALPHA_TRADER_DATA_DIR`` at a temp dir, and MT5/engine variables are removed from this
  test process, so the real ``.env`` and ``data/`` are never read or touched.
* ``mt5``-marked tests (need a live MetaTrader 5 terminal) are skipped unless ``--run-mt5`` is given.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

_SANDBOX = Path(tempfile.mkdtemp(prefix="alpha_engine_tests_"))
_FAKE_ENV = _SANDBOX / "fake.env"
_FAKE_ENV.write_text("", encoding="utf-8")
for _var in ("MT5_TERMINAL_PATH", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_SERVER_UTC_OFFSET",
             "ENGINE_PORT", "DEV_MODE", "ENGINE_SYMBOLS"):
    os.environ.pop(_var, None)
os.environ["ALPHA_TRADER_ENV_FILE"] = str(_FAKE_ENV)
os.environ["ALPHA_TRADER_DATA_DIR"] = str(_SANDBOX / "data")
# Never let an app built by a test start a background MT5 connection.
os.environ["ENGINE_MT5_AUTOCONNECT"] = "false"

from alpha_engine.config import Settings, load_settings, reset_settings_cache  # noqa: E402
from alpha_engine.logging_setup import reset_logging  # noqa: E402

# Fake secrets used by the leak tests. Never real values.
FAKE_PASSWORD = "S3cr3t!"
FAKE_LOGIN = "12345678"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-mt5",
        action="store_true",
        default=False,
        help="run tests marked 'mt5' (they need a live, logged-in MetaTrader 5 terminal)",
    )


class _BlockedMetaTrader5(types.ModuleType):
    """Stand-in for the real package during default test runs: any use fails loudly.

    Tests inject a fake mt5 module into the adapter explicitly; nothing may reach the real terminal
    unless ``--run-mt5`` is given (then the real package is importable again).
    """

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        raise RuntimeError(f"MetaTrader5.{name} used in a test without --run-mt5")


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("--run-mt5"):
        sys.modules["MetaTrader5"] = _BlockedMetaTrader5("MetaTrader5")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-mt5"):
        return
    skip_mt5 = pytest.mark.skip(reason="needs a live MetaTrader 5 terminal; pass --run-mt5 to run")
    for item in items:
        if "mt5" in item.keywords:
            item.add_marker(skip_mt5)


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(_SANDBOX, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_global_state() -> Iterator[None]:
    reset_settings_cache()
    reset_logging()
    yield
    reset_settings_cache()
    reset_logging()


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    """Factory: ``make_settings("KEY=value\\n...", environ={...})`` -> Settings from a tmp fake .env.

    ``environ`` stands in for the OS environment (default: empty), so tests are hermetic. The data dir
    defaults to ``tmp_path / "data"`` (not ``<repo>/data``): an app started by a test (lifespan) opens
    its SQLite database there, never in the user's real data directory.
    """

    def _make(env_text: str = "", environ: dict[str, str] | None = None) -> Settings:
        env_file = tmp_path / "fake.env"
        env_file.write_text(env_text, encoding="utf-8")
        # Autoconnect off unless a test opts in explicitly (via env_text or environ).
        base = {} if "ENGINE_MT5_AUTOCONNECT" in env_text else {"ENGINE_MT5_AUTOCONNECT": "false"}
        if "ALPHA_TRADER_DATA_DIR" not in env_text:
            base["ALPHA_TRADER_DATA_DIR"] = str(tmp_path / "data")
        return load_settings(env_file=env_file, environ={**base, **(environ or {})})

    return _make


@pytest.fixture(scope="session")
def synthetic():
    """Default deterministic synthetic dataset: {(symbol, Timeframe): SyntheticSeries}. Do not mutate."""
    from fixtures.synthetic_ohlcv import generate_dataset

    return generate_dataset()
