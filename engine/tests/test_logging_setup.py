from __future__ import annotations

import io
import logging

import pytest

from alpha_engine import logging_setup
from alpha_engine.logging_setup import ROOT_LOGGER_NAME, configure_logging, get_logger, is_dev_mode
from conftest import FAKE_LOGIN, FAKE_PASSWORD

SECRETS_ENV = f"MT5_LOGIN={FAKE_LOGIN}\nMT5_PASSWORD={FAKE_PASSWORD}\n"


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


@pytest.fixture
def capture():
    handlers: list[tuple[logging.Logger, ListHandler]] = []

    def _attach(logger: logging.Logger) -> ListHandler:
        handler = ListHandler()
        logger.addHandler(handler)
        handlers.append((logger, handler))
        return handler

    yield _attach
    for logger, handler in handlers:
        logger.removeHandler(handler)


def test_get_logger_uses_single_hierarchy() -> None:
    assert get_logger("backtest").name == "alpha_engine.backtest"
    assert get_logger("alpha_engine.app").name == "alpha_engine.app"
    assert get_logger().name == ROOT_LOGGER_NAME


def test_debug_suppressed_when_dev_mode_false(make_settings, capture) -> None:
    configure_logging(make_settings("DEV_MODE=false\n"), stream=io.StringIO(), force=True)
    assert is_dev_mode() is False
    root = logging.getLogger(ROOT_LOGGER_NAME)
    assert root.level == logging.WARNING
    logger = get_logger("t_off")
    handler = capture(logger)
    logger.debug("hidden debug")
    logger.info("hidden info")
    logger.warning("visible warning")
    assert handler.messages == ["visible warning"]


def test_debug_enabled_with_console_handler_when_dev_mode_true(make_settings) -> None:
    stream = io.StringIO()
    configure_logging(make_settings("DEV_MODE=true\n"), stream=stream, force=True)
    assert is_dev_mode() is True
    root = logging.getLogger(ROOT_LOGGER_NAME)
    assert root.level == logging.DEBUG
    console = [h for h in root.handlers if getattr(h, "alpha_engine_handler", False)]
    assert len(console) == 1 and console[0].level == logging.DEBUG
    get_logger("t_on").debug("bar %s close=%.5f", "2026-09-26T18:00:00Z", 1.08765)
    assert "bar 2026-09-26T18:00:00Z close=1.08765" in stream.getvalue()


def test_reconfigure_does_not_duplicate_handlers(make_settings) -> None:
    settings = make_settings("DEV_MODE=true\n")
    for _ in range(3):
        configure_logging(settings, stream=io.StringIO(), force=True)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    assert len([h for h in root.handlers if getattr(h, "alpha_engine_handler", False)]) == 1


def test_dev_mode_read_once_not_from_os_environ(make_settings, monkeypatch) -> None:
    configure_logging(make_settings("DEV_MODE=false\n"), stream=io.StringIO(), force=True)
    monkeypatch.setenv("DEV_MODE", "true")
    assert is_dev_mode() is False
    configure_logging(stream=io.StringIO())  # already configured, no force -> no re-read
    assert is_dev_mode() is False


def test_redaction_on_logger_records(make_settings, capture) -> None:
    configure_logging(make_settings(SECRETS_ENV + "DEV_MODE=true\n"), stream=io.StringIO(), force=True)
    logger = get_logger("t_redact")
    handler = capture(logger)
    logger.warning("pw=%s login=%s", FAKE_PASSWORD, FAKE_LOGIN)
    logger.error(f"oops password {FAKE_PASSWORD} for {FAKE_LOGIN}")
    assert len(handler.records) == 2
    for message in handler.messages:
        assert FAKE_PASSWORD not in message
        assert FAKE_LOGIN not in message
        assert "****5678" in message


def test_redaction_on_console_output_including_traceback(make_settings) -> None:
    stream = io.StringIO()
    configure_logging(make_settings(SECRETS_ENV + "DEV_MODE=true\n"), stream=stream, force=True)
    # A logger obtained WITHOUT get_logger still hits the console handler's filter/formatter.
    raw = logging.getLogger("alpha_engine.raw_child")
    raw.warning("login=%s", FAKE_LOGIN)
    try:
        raise RuntimeError(f"bad credentials {FAKE_PASSWORD}")
    except RuntimeError:
        raw.exception("failure")
    output = stream.getvalue()
    assert "login=****5678" in output
    assert "RuntimeError" in output
    assert FAKE_PASSWORD not in output
    assert FAKE_LOGIN not in output


def test_configure_logging_debug_line_is_safe(make_settings) -> None:
    stream = io.StringIO()
    configure_logging(make_settings(SECRETS_ENV + "DEV_MODE=true\n"), stream=stream, force=True)
    output = stream.getvalue()
    assert "logging configured" in output
    assert FAKE_PASSWORD not in output and FAKE_LOGIN not in output


def test_no_redaction_configured_passes_messages_through(capture) -> None:
    logging_setup.reset_logging()
    logger = get_logger("t_plain")
    logger.setLevel(logging.DEBUG)
    handler = capture(logger)
    try:
        logger.warning("value=%d", 42)
    finally:
        logger.setLevel(logging.NOTSET)
    assert handler.messages == ["value=42"]
