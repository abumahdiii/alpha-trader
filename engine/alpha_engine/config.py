"""Typed engine settings, loaded once from the OS environment and a local ``.env``.

Precedence (highest first):
    1. the real OS environment (e.g. ``DEV_MODE`` / ``ENGINE_PORT`` passed through by Flutter),
    2. the ``.env`` file,
    3. the defaults below.

This is exactly the semantics of ``python-dotenv``'s ``load_dotenv(override=False)``. We implement it
with ``dotenv_values`` + a merge instead of ``load_dotenv`` so that the ``.env`` contents (which may hold
account data) are never copied into ``os.environ`` -- and therefore never inherited by child processes
or visible to unrelated code. ``interpolate=False`` keeps values such as passwords containing ``$``
byte-for-byte intact.

The ``.env`` file is located from this file's path (``<repo>/.env``, the parent of ``engine/``), never
from the current working directory. ``ALPHA_TRADER_ENV_FILE`` overrides the path (tests and the smoke
run point it at a temporary fake file).

Secrets: ``mt5_password`` is a :class:`pydantic.SecretStr`; ``mt5_login`` is excluded from ``repr``.
Use :meth:`Settings.safe_summary` for anything displayed or logged. See
``.claude/rules/03_trading_safety.md`` section 2.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

ENGINE_DIR: Path = Path(__file__).resolve().parents[1]
REPO_ROOT: Path = ENGINE_DIR.parent
DEFAULT_ENV_FILE: Path = REPO_ROOT / ".env"
DEFAULT_DATA_DIR: Path = REPO_ROOT / "data"
DEFAULT_ENGINE_PORT = 8765
DEFAULT_SYMBOLS: tuple[str, ...] = ("XAUUSD.x", "BRNUSD.x")

ENV_FILE_OVERRIDE_VAR = "ALPHA_TRADER_ENV_FILE"
DATA_DIR_VAR = "ALPHA_TRADER_DATA_DIR"

_MASK = "****"


def mask_login(login: object | None) -> str | None:
    """Return a display-safe login: ``"****1234"`` (last 4 chars).

    ``None``/empty/blank -> ``None``; logins of 4 chars or fewer -> ``"****"`` (showing the "last 4"
    of a 4-char login would reveal all of it).
    """
    if login is None:
        return None
    text = str(login).strip()
    if not text:
        return None
    if len(text) <= 4:
        return _MASK
    return _MASK + text[-4:]


def _clean(value: str | None) -> str | None:
    """Strip whitespace; treat empty values (e.g. ``MT5_LOGIN=``) as unset."""
    if value is None:
        return None
    value = value.strip()
    return value or None


class Settings(BaseModel):
    """Engine settings. Immutable; construct via :func:`load_settings` / :func:`get_settings`."""

    # hide_input_in_errors: a ValidationError message never echoes the offending raw value.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    mt5_terminal_path: str | None = None
    mt5_login: str | None = Field(default=None, repr=False)
    mt5_password: SecretStr | None = Field(default=None, repr=False)
    mt5_server: str | None = None
    engine_port: int = Field(default=DEFAULT_ENGINE_PORT, ge=1024, le=65535)
    dev_mode: bool = False
    mt5_server_utc_offset: float | None = Field(default=None, ge=-14, le=14)
    data_dir: Path = DEFAULT_DATA_DIR
    # Connect to MT5 in the background when the app starts (tests set ENGINE_MT5_AUTOCONNECT=false).
    engine_mt5_autoconnect: bool = True
    # Symbols served by /symbols and /rates (ENGINE_SYMBOLS, comma-separated).
    engine_symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    # The user confirmed the raw-data check (.claude/test/phase_1_data_check.md): backtests are no longer
    # labelled provisional ("موقت تا تایید چک داده"). ALPHA_DATA_CHECK_CONFIRMED, only "true" enables it.
    alpha_data_check_confirmed: bool = False
    env_file: Path | None = None
    env_file_loaded: bool = False

    @field_validator("dev_mode", "alpha_data_check_confirmed", mode="before")
    @classmethod
    def _parse_dev_mode(cls, value: Any) -> Any:
        # Only the literal "true" (case-insensitive) enables DEV_MODE / the data-check confirmation.
        if isinstance(value, str):
            return value.strip().lower() == "true"
        return value

    @field_validator("engine_mt5_autoconnect", mode="before")
    @classmethod
    def _parse_autoconnect(cls, value: Any) -> Any:
        # Default on; only an explicit "false"/"0"/"no"/"off" disables it.
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return value

    @field_validator("engine_symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, value: Any) -> Any:
        from .data.symbols import validate_symbol_name

        items = value.split(",") if isinstance(value, str) else list(value)
        names: list[str] = []
        for item in items:
            if str(item).strip():
                name = validate_symbol_name(str(item))
                if name not in names:
                    names.append(name)
        if not names:
            raise ValueError("ENGINE_SYMBOLS must name at least one symbol")
        return tuple(names)

    @property
    def mt5_login_masked(self) -> str | None:
        return mask_login(self.mt5_login)

    def safe_summary(self) -> dict[str, Any]:
        """Display/log-safe view: no password, masked login only."""
        return {
            "mt5_terminal_path": self.mt5_terminal_path,
            "mt5_server": self.mt5_server,
            "mt5_login_masked": self.mt5_login_masked,
            "mt5_password_set": self.mt5_password is not None,
            "engine_port": self.engine_port,
            "dev_mode": self.dev_mode,
            "mt5_server_utc_offset": self.mt5_server_utc_offset,
            "data_dir": str(self.data_dir),
            "engine_mt5_autoconnect": self.engine_mt5_autoconnect,
            "engine_symbols": list(self.engine_symbols),
            "alpha_data_check_confirmed": self.alpha_data_check_confirmed,
            "env_file": str(self.env_file) if self.env_file is not None else None,
            "env_file_loaded": self.env_file_loaded,
        }

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v!r}" for k, v in self.safe_summary().items())
        return f"Settings({inner})"

    def __str__(self) -> str:
        return self.__repr__()


def resolve_env_file(environ: Mapping[str, str] | None = None) -> Path:
    """Path of the ``.env`` to load: ``$ALPHA_TRADER_ENV_FILE`` if set, else ``<repo>/.env``."""
    env = os.environ if environ is None else environ
    override = _clean(env.get(ENV_FILE_OVERRIDE_VAR))
    return Path(override) if override else DEFAULT_ENV_FILE


def load_settings(
    env_file: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build settings from ``environ`` (default: ``os.environ``) over the ``.env`` file.

    A missing ``.env`` is fine (defaults apply). Raises ``ValueError`` (pydantic ``ValidationError``)
    on invalid values such as an out-of-range ``ENGINE_PORT``.
    """
    os_env: dict[str, str] = dict(os.environ if environ is None else environ)
    path = Path(env_file) if env_file is not None else resolve_env_file(os_env)

    file_values: dict[str, str] = {}
    loaded = False
    if path.is_file():
        raw = dotenv_values(path, interpolate=False)
        file_values = {k: v for k, v in raw.items() if v is not None}
        loaded = True

    # OS environment wins over .env (== load_dotenv(override=False)).
    merged: dict[str, str] = {**file_values, **os_env}

    password = merged.get("MT5_PASSWORD")
    port = _clean(merged.get("ENGINE_PORT"))
    offset = _clean(merged.get("MT5_SERVER_UTC_OFFSET"))
    data_dir = _clean(merged.get(DATA_DIR_VAR))
    autoconnect = _clean(merged.get("ENGINE_MT5_AUTOCONNECT"))
    symbols = _clean(merged.get("ENGINE_SYMBOLS"))

    kwargs: dict[str, Any] = {
        "mt5_terminal_path": _clean(merged.get("MT5_TERMINAL_PATH")),
        "mt5_login": _clean(merged.get("MT5_LOGIN")),
        # Passwords are not stripped (only an empty value counts as unset).
        "mt5_password": SecretStr(password) if password else None,
        "mt5_server": _clean(merged.get("MT5_SERVER")),
        "dev_mode": merged.get("DEV_MODE", "false"),
        "alpha_data_check_confirmed": merged.get("ALPHA_DATA_CHECK_CONFIRMED", "false"),
        "data_dir": Path(data_dir) if data_dir else DEFAULT_DATA_DIR,
        "env_file": path,
        "env_file_loaded": loaded,
    }
    if port is not None:
        kwargs["engine_port"] = port
    if offset is not None:
        kwargs["mt5_server_utc_offset"] = offset
    if autoconnect is not None:
        kwargs["engine_mt5_autoconnect"] = autoconnect
    if symbols is not None:
        kwargs["engine_symbols"] = symbols
    return Settings(**kwargs)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded once."""
    return load_settings()


def reset_settings_cache() -> None:
    """Forget the cached settings (tests only)."""
    get_settings.cache_clear()
