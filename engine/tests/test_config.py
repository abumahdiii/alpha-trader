from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_engine import config
from alpha_engine.config import (
    DEFAULT_DATA_DIR,
    DEFAULT_ENGINE_PORT,
    REPO_ROOT,
    get_settings,
    load_settings,
    mask_login,
    resolve_env_file,
)
from conftest import FAKE_LOGIN, FAKE_PASSWORD

FAKE_ENV = f"""
DEV_MODE=false
ENGINE_PORT=9000
MT5_TERMINAL_PATH=C:\\Fake\\terminal64.exe
MT5_LOGIN={FAKE_LOGIN}
MT5_PASSWORD={FAKE_PASSWORD}
MT5_SERVER=Fake-Server
MT5_SERVER_UTC_OFFSET=3
"""


# --- location of .env ------------------------------------------------------------------------------

def test_repo_root_is_resolved_from_file_not_cwd() -> None:
    config_file = Path(config.__file__).resolve()
    assert config.ENGINE_DIR == config_file.parents[1]
    assert config.ENGINE_DIR.name == "engine"
    assert REPO_ROOT == config_file.parents[2]
    assert DEFAULT_DATA_DIR == REPO_ROOT / "data"


def test_default_env_path_is_repo_root_dotenv() -> None:
    # Only resolves the path; never loads the real file.
    assert resolve_env_file({}) == REPO_ROOT / ".env"


def test_env_file_override_variable(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("ENGINE_PORT=9555\nMT5_SERVER=From-Override\n", encoding="utf-8")
    environ = {"ALPHA_TRADER_ENV_FILE": str(env_file)}
    assert resolve_env_file(environ) == env_file
    settings = load_settings(environ=environ)
    assert settings.engine_port == 9555
    assert settings.mt5_server == "From-Override"
    assert settings.env_file == env_file
    assert settings.env_file_loaded is True


def test_get_settings_uses_override_from_os_env_and_is_cached() -> None:
    # conftest points ALPHA_TRADER_ENV_FILE at an empty fake file.
    first = get_settings()
    assert first.env_file is not None and first.env_file.name == "fake.env"
    assert get_settings() is first


# --- loading and defaults ----------------------------------------------------------------------------

def test_missing_env_file_uses_defaults(tmp_path: Path) -> None:
    settings = load_settings(env_file=tmp_path / "does-not-exist.env", environ={})
    assert settings.env_file_loaded is False
    assert settings.engine_port == DEFAULT_ENGINE_PORT == 8765
    assert settings.dev_mode is False
    assert settings.mt5_login is None
    assert settings.mt5_password is None
    assert settings.mt5_server is None
    assert settings.mt5_terminal_path is None
    assert settings.mt5_server_utc_offset is None
    assert settings.data_dir == DEFAULT_DATA_DIR


def test_values_loaded_from_env_file(make_settings) -> None:
    settings = make_settings(FAKE_ENV)
    assert settings.env_file_loaded is True
    assert settings.engine_port == 9000
    assert settings.mt5_terminal_path == "C:\\Fake\\terminal64.exe"
    assert settings.mt5_login == FAKE_LOGIN
    assert settings.mt5_password is not None
    assert settings.mt5_password.get_secret_value() == FAKE_PASSWORD
    assert settings.mt5_server == "Fake-Server"
    assert settings.mt5_server_utc_offset == 3.0


def test_empty_values_are_unset(make_settings) -> None:
    settings = make_settings("MT5_LOGIN=\nMT5_PASSWORD=\nMT5_SERVER=\nENGINE_PORT=\nMT5_SERVER_UTC_OFFSET=\n")
    assert settings.mt5_login is None
    assert settings.mt5_password is None
    assert settings.mt5_server is None
    assert settings.engine_port == 8765
    assert settings.mt5_server_utc_offset is None


def test_password_with_dollar_is_not_interpolated(make_settings) -> None:
    settings = make_settings("MT5_PASSWORD=ab$HOME${X}cd\n")
    assert settings.mt5_password.get_secret_value() == "ab$HOME${X}cd"


def test_data_dir_override(make_settings, tmp_path: Path) -> None:
    settings = make_settings("", environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "d")})
    assert settings.data_dir == tmp_path / "d"


# --- precedence: OS env > .env > defaults ------------------------------------------------------------

def test_os_env_wins_over_env_file(make_settings) -> None:
    settings = make_settings(
        "ENGINE_PORT=9000\nDEV_MODE=false\nMT5_SERVER=File-Server\n",
        environ={"ENGINE_PORT": "9100", "DEV_MODE": "true"},
    )
    assert settings.engine_port == 9100  # OS env
    assert settings.dev_mode is True  # OS env
    assert settings.mt5_server == "File-Server"  # only in .env


def test_real_os_environ_precedence_and_no_pollution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "fake.env"
    env_file.write_text("ENGINE_PORT=9000\nMT5_SERVER=Only-In-File\n", encoding="utf-8")
    monkeypatch.setenv("ENGINE_PORT", "9200")
    monkeypatch.delenv("MT5_SERVER", raising=False)
    import os

    settings = load_settings(env_file=env_file)  # environ=None -> real os.environ
    assert settings.engine_port == 9200
    assert settings.mt5_server == "Only-In-File"
    # .env values are not copied into the process environment.
    assert "MT5_SERVER" not in os.environ


# --- validation --------------------------------------------------------------------------------------

@pytest.mark.parametrize("port", ["80", "1023", "65536", "70000", "abc", "-1"])
def test_invalid_port_rejected(make_settings, port: str) -> None:
    with pytest.raises(ValueError):
        make_settings(f"ENGINE_PORT={port}\n")


@pytest.mark.parametrize("port", ["1024", "8765", "65535"])
def test_valid_port_bounds(make_settings, port: str) -> None:
    assert make_settings(f"ENGINE_PORT={port}\n").engine_port == int(port)


def test_invalid_port_error_does_not_echo_input(make_settings) -> None:
    with pytest.raises(ValueError) as info:
        make_settings("ENGINE_PORT=99999\n")
    assert "99999" not in str(info.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), ("True", True), (" true ", True),
     ("false", False), ("1", False), ("yes", False), ("", False)],
)
def test_dev_mode_parsing(make_settings, raw: str, expected: bool) -> None:
    assert make_settings("", environ={"DEV_MODE": raw}).dev_mode is expected


@pytest.mark.parametrize(("raw", "expected"), [("3", 3.0), ("-4.5", -4.5), ("0", 0.0)])
def test_utc_offset_parsing(make_settings, raw: str, expected: float) -> None:
    assert make_settings(f"MT5_SERVER_UTC_OFFSET={raw}\n").mt5_server_utc_offset == expected


@pytest.mark.parametrize("raw", ["abc", "20", "-15"])
def test_utc_offset_invalid(make_settings, raw: str) -> None:
    with pytest.raises(ValueError):
        make_settings(f"MT5_SERVER_UTC_OFFSET={raw}\n")


# --- masking and secret hygiene ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("login", "expected"),
    [(None, None), ("", None), ("   ", None), ("12", "****"), ("1234", "****"),
     ("12345", "****2345"), ("12345678", "****5678"), (12345678, "****5678")],
)
def test_mask_login(login, expected) -> None:
    assert mask_login(login) == expected


def test_repr_str_summary_never_leak(make_settings) -> None:
    settings = make_settings(FAKE_ENV)
    summary = settings.safe_summary()
    views = [repr(settings), str(settings), f"{settings}", f"{settings!r}", str(summary),
             json.dumps(summary), repr(settings.mt5_password)]
    for view in views:
        assert FAKE_PASSWORD not in view
        assert FAKE_LOGIN not in view
    assert summary["mt5_login_masked"] == "****5678"
    assert summary["mt5_password_set"] is True
    assert "****5678" in repr(settings)
    assert set(summary) == {
        "mt5_terminal_path", "mt5_server", "mt5_login_masked", "mt5_password_set", "engine_port",
        "dev_mode", "mt5_server_utc_offset", "data_dir", "env_file", "env_file_loaded",
    }


def test_settings_are_immutable(make_settings) -> None:
    settings = make_settings(FAKE_ENV)
    with pytest.raises(Exception):
        settings.engine_port = 9999  # type: ignore[misc]
