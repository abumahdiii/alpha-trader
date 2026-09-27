"""Central logging for the engine (see ``.claude/rules/01_logging_standard.md``).

* ``DEV_MODE`` is read once, from the settings object, in :func:`configure_logging`; call sites use
  :func:`is_dev_mode` -- they never re-read ``os.environ``.
* All engine loggers live under the single ``"alpha_engine"`` hierarchy (:func:`get_logger`).
* DEV_MODE on  -> level DEBUG, console handler on stderr.
  DEV_MODE off -> level WARNING (warnings/errors still reach the console).
* Defense in depth: a redaction filter masks the configured MT5 password / full login if either
  ever reaches a log record. It is attached to every logger returned by :func:`get_logger` and to the
  console handler, and the console formatter redacts the final text (covers tracebacks too).
  This is a safety net, not permission: credentials must never be logged in the first place.

Guarded debug log pattern::

    from alpha_engine.logging_setup import get_logger, is_dev_mode

    logger = get_logger(__name__)
    if is_dev_mode():
        logger.debug("bar %s close=%.5f signal=%s", bar_time, close, signal)
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, TextIO

from .config import get_settings, mask_login

if TYPE_CHECKING:
    from .config import Settings

ROOT_LOGGER_NAME = "alpha_engine"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_REDACTED_PASSWORD = "****"

DEV_MODE: bool = False
_configured = False


class RedactionFilter(logging.Filter):
    """Replaces the configured password and full login inside log messages."""

    def __init__(self) -> None:
        super().__init__()
        self._replacements: list[tuple[str, str]] = []

    def set_secrets(self, password: str | None, login: str | None) -> None:
        pairs: list[tuple[str, str]] = []
        if password:
            pairs.append((password, _REDACTED_PASSWORD))
        if login and login.strip():
            pairs.append((login.strip(), mask_login(login) or _REDACTED_PASSWORD))
        # Longest first so a secret that contains the other is fully replaced.
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        self._replacements = pairs

    def redact(self, text: str) -> str:
        for secret, replacement in self._replacements:
            if secret in text:
                text = text.replace(secret, replacement)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._replacements:
            return True
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args: leave it to the handler's error path
            return True
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Redacts the fully formatted output, including exception tracebacks."""

    def __init__(self, fmt: str, redaction: RedactionFilter) -> None:
        super().__init__(fmt)
        self._redaction = redaction

    def format(self, record: logging.LogRecord) -> str:
        return self._redaction.redact(super().format(record))


class _ConsoleHandler(logging.StreamHandler):
    """StreamHandler that follows the *current* ``sys.stderr`` unless given an explicit stream."""

    alpha_engine_handler = True

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__(stream if stream is not None else sys.stderr)
        self._follow_stderr = stream is None

    def emit(self, record: logging.LogRecord) -> None:
        if self._follow_stderr:
            self.stream = sys.stderr
        super().emit(record)


_redaction_filter = RedactionFilter()


def configure_logging(
    settings: Settings | None = None,
    *,
    stream: TextIO | None = None,
    force: bool = False,
) -> None:
    """Configure the ``alpha_engine`` logger hierarchy. Idempotent unless ``force=True``."""
    global DEV_MODE, _configured
    if _configured and not force:
        return
    settings = settings if settings is not None else get_settings()

    DEV_MODE = bool(settings.dev_mode)  # the single DEV_MODE read
    password = settings.mt5_password.get_secret_value() if settings.mt5_password else None
    _redaction_filter.set_secrets(password, settings.mt5_login)

    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        if getattr(handler, "alpha_engine_handler", False):
            root.removeHandler(handler)

    level = logging.DEBUG if DEV_MODE else logging.WARNING
    handler = _ConsoleHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, _redaction_filter))
    handler.addFilter(_redaction_filter)
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    if _redaction_filter not in root.filters:
        root.addFilter(_redaction_filter)
    _configured = True

    if DEV_MODE:
        root.debug("logging configured (DEV_MODE on): %s", settings.safe_summary())


def get_logger(name: str | None = None) -> logging.Logger:
    """Logger inside the ``alpha_engine`` hierarchy, with the redaction filter attached.

    Does not configure logging by itself (keeps imports side-effect free); until
    :func:`configure_logging` runs, ``is_dev_mode()`` is False so guarded debug logs are skipped.
    """
    if not name or name == ROOT_LOGGER_NAME:
        full = ROOT_LOGGER_NAME
    elif name.startswith(ROOT_LOGGER_NAME + "."):
        full = name
    else:
        full = f"{ROOT_LOGGER_NAME}.{name}"
    logger = logging.getLogger(full)
    if _redaction_filter not in logger.filters:
        logger.addFilter(_redaction_filter)
    return logger


def is_dev_mode() -> bool:
    """The DEV_MODE value captured by the last :func:`configure_logging` call."""
    return DEV_MODE


def reset_logging() -> None:
    """Undo :func:`configure_logging` (tests only)."""
    global DEV_MODE, _configured
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        if getattr(handler, "alpha_engine_handler", False):
            root.removeHandler(handler)
    root.setLevel(logging.NOTSET)
    root.propagate = True
    _redaction_filter.set_secrets(None, None)
    DEV_MODE = False
    _configured = False
