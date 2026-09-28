"""Frozen (PyInstaller) path defaults in ``alpha_engine.config``.

Package layout (engine/packaging/README.md)::

    <app>/engine/alpha_engine/alpha_engine.exe  -> install root <app>
    <app>/user_data/                            -> default data dir
    <app>/.env                                  -> default env file

``sys.frozen`` / ``sys.executable`` are monkeypatched; nothing is frozen for real and no file outside
``tmp_path`` is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from alpha_engine import config
from alpha_engine.config import (
    DATA_DIR_VAR,
    ENV_FILE_OVERRIDE_VAR,
    REPO_ROOT,
    Settings,
    default_data_dir,
    default_env_file,
    install_root,
    is_frozen,
    load_settings,
    resolve_env_file,
)


@pytest.fixture
def frozen_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend to run as ``<tmp>/AlphaTrader/engine/alpha_engine/alpha_engine.exe``."""
    app = tmp_path / "AlphaTrader"
    exe = app / "engine" / "alpha_engine" / "alpha_engine.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return app.resolve()


# --- dev behaviour is unchanged ----------------------------------------------------------------------

def test_dev_defaults_unchanged() -> None:
    assert not is_frozen()
    assert install_root() == REPO_ROOT
    assert default_env_file() == REPO_ROOT / ".env"
    assert default_data_dir() == REPO_ROOT / "data"
    assert config.DEFAULT_DATA_DIR == REPO_ROOT / "data"
    assert config.DEFAULT_ENV_FILE == REPO_ROOT / ".env"
    assert resolve_env_file({}) == REPO_ROOT / ".env"


def test_dev_settings_default_data_dir(tmp_path: Path) -> None:
    settings = load_settings(env_file=tmp_path / "none.env", environ={})
    assert settings.data_dir == REPO_ROOT / "data"
    assert Settings().data_dir == REPO_ROOT / "data"


# --- frozen defaults ---------------------------------------------------------------------------------

def test_frozen_install_root_is_two_levels_above_exe_dir(frozen_app: Path) -> None:
    assert is_frozen()
    assert install_root() == frozen_app


def test_frozen_defaults_resolve_next_to_the_package(frozen_app: Path) -> None:
    assert default_env_file() == frozen_app / ".env"
    assert default_data_dir() == frozen_app / "user_data"
    assert resolve_env_file({}) == frozen_app / ".env"
    # Never inside the bundle (_MEIPASS / _internal) nor the source repo.
    assert REPO_ROOT not in default_data_dir().parents
    assert "_internal" not in default_data_dir().parts


def test_frozen_load_settings_defaults(frozen_app: Path) -> None:
    settings = load_settings(environ={})
    assert settings.data_dir == frozen_app / "user_data"
    assert settings.env_file == frozen_app / ".env"
    assert settings.env_file_loaded is False  # no .env is shipped
    assert Settings().data_dir == frozen_app / "user_data"


def test_frozen_env_file_next_to_package_is_loaded(frozen_app: Path) -> None:
    (frozen_app / ".env").write_text("ENGINE_PORT=9123\n", encoding="utf-8")
    settings = load_settings(environ={})
    assert settings.env_file_loaded is True
    assert settings.engine_port == 9123


def test_frozen_overrides_still_win(frozen_app: Path, tmp_path: Path) -> None:
    data = tmp_path / "elsewhere" / "data"
    env_file = tmp_path / "elsewhere" / "custom.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("ENGINE_PORT=9444\n", encoding="utf-8")
    environ = {DATA_DIR_VAR: str(data), ENV_FILE_OVERRIDE_VAR: str(env_file)}
    assert resolve_env_file(environ) == env_file
    settings = load_settings(environ=environ)
    assert settings.data_dir == data
    assert settings.env_file == env_file
    assert settings.engine_port == 9444


def test_frozen_data_dir_can_come_from_the_env_file(frozen_app: Path, tmp_path: Path) -> None:
    target = tmp_path / "from_env_file"
    (frozen_app / ".env").write_text(f"{DATA_DIR_VAR}={target}\n", encoding="utf-8")
    assert load_settings(environ={}).data_dir == target


@pytest.mark.parametrize("depth", [0, 1])
def test_frozen_exe_near_drive_root_falls_back_to_exe_dir(
    depth: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = Path(tmp_path.anchor)  # e.g. "C:\\" -- only used as a path, nothing is written there
    exe_dir = anchor if depth == 0 else anchor / "AlphaEngineOnly"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "alpha_engine.exe"))
    assert install_root() == exe_dir.resolve()
    assert default_data_dir() == exe_dir.resolve() / "user_data"


def test_frozen_flag_false_is_dev(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert install_root() == REPO_ROOT
    assert default_data_dir() == REPO_ROOT / "data"
