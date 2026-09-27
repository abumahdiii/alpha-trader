from __future__ import annotations

import ast
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpha_engine import __version__, logging_setup
from alpha_engine.app import Mt5Status, create_app
from conftest import FAKE_LOGIN, FAKE_PASSWORD

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "alpha_engine"
TOP_KEYS = {"status", "service", "version", "pid", "dev_mode", "time_utc", "mt5"}
MT5_KEYS = {"state", "server", "login_masked", "trade_mode", "message"}
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _client(app: FastAPI, host: str = "127.0.0.1") -> TestClient:
    return TestClient(app, client=(host, 50000))


# --- /health contract ------------------------------------------------------------------------------

def test_health_contract_exact(make_settings) -> None:
    app = create_app(make_settings("DEV_MODE=false\n"))
    response = _client(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == TOP_KEYS
    assert body["status"] == "ok"
    assert body["service"] == "alpha_engine"
    assert body["version"] == __version__ == "0.1.0"
    assert isinstance(body["pid"], int) and body["pid"] == os.getpid()
    assert body["dev_mode"] is False
    assert TIME_RE.match(body["time_utc"])
    parsed = datetime.strptime(body["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60
    assert body["mt5"] == {
        "state": "not_initialized", "server": None, "login_masked": None,
        "trade_mode": None, "message": None,
    }


def test_health_reports_dev_mode_true(make_settings) -> None:
    app = create_app(make_settings("DEV_MODE=true\n"))
    body = _client(app).get("/health").json()
    assert body["dev_mode"] is True


def test_health_mt5_seam_override_via_app_state(make_settings) -> None:
    app = create_app(make_settings())
    app.state.mt5_status_provider = lambda: Mt5Status(
        state="connected", server="Fake-Server", login_masked="****5678", trade_mode="demo", message=None
    )
    body = _client(app).get("/health").json()
    assert set(body["mt5"]) == MT5_KEYS
    assert body["mt5"] == {
        "state": "connected", "server": "Fake-Server", "login_masked": "****5678",
        "trade_mode": "demo", "message": None,
    }


def test_health_mt5_seam_via_factory_argument(make_settings) -> None:
    app = create_app(make_settings(), mt5_status_provider=lambda: Mt5Status(state="account_mismatch"))
    assert _client(app).get("/health").json()["mt5"]["state"] == "account_mismatch"


def test_health_stays_200_when_provider_raises(make_settings) -> None:
    app = create_app(make_settings())

    def broken() -> Mt5Status:
        raise RuntimeError("terminal exploded")

    app.state.mt5_status_provider = broken
    response = _client(app).get("/health")
    assert response.status_code == 200
    assert response.json()["mt5"]["state"] == "error"
    assert "exploded" not in response.text


def test_health_remasks_unmasked_login_from_provider(make_settings) -> None:
    app = create_app(make_settings())
    app.state.mt5_status_provider = lambda: Mt5Status(state="connected", login_masked=FAKE_LOGIN)
    response = _client(app).get("/health")
    assert response.json()["mt5"]["login_masked"] == "****5678"
    assert FAKE_LOGIN not in response.text


def test_health_never_exposes_configured_secrets(make_settings) -> None:
    app = create_app(make_settings(f"MT5_LOGIN={FAKE_LOGIN}\nMT5_PASSWORD={FAKE_PASSWORD}\n"))
    text = _client(app).get("/health").text
    assert FAKE_LOGIN not in text and FAKE_PASSWORD not in text


def test_module_level_app_is_lazy_fastapi_instance() -> None:
    import alpha_engine.app as app_module

    assert isinstance(app_module.app, FastAPI)
    assert app_module.app is app_module.app
    with pytest.raises(AttributeError):
        app_module.no_such_attribute  # noqa: B018


@pytest.mark.parametrize("module", ["app.py", "__main__.py", "config.py", "logging_setup.py", "__init__.py"])
def test_skeleton_modules_do_not_import_metatrader5(module: str) -> None:
    tree = ast.parse((PACKAGE_DIR / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "MetaTrader5" not in imported


# --- DEV_MODE request logging ----------------------------------------------------------------------

def test_request_logging_in_dev_mode_without_headers(make_settings) -> None:
    app = create_app(make_settings("DEV_MODE=true\n"))
    stream = io.StringIO()
    logging_setup.configure_logging(app.state.settings, stream=stream, force=True)
    _client(app).get("/health?token=abc", headers={"Authorization": "Bearer S3cr3tHeader"})
    output = stream.getvalue()
    assert re.search(r"GET /health -> 200 \(\d+\.\d ms\)", output)
    assert "S3cr3tHeader" not in output
    assert "token=abc" not in output


def test_no_request_logging_when_dev_mode_off(make_settings) -> None:
    app = create_app(make_settings("DEV_MODE=false\n"))
    stream = io.StringIO()
    logging_setup.configure_logging(app.state.settings, stream=stream, force=True)
    _client(app).get("/health")
    assert stream.getvalue() == ""


# --- /shutdown -------------------------------------------------------------------------------------

class FakeServer:
    should_exit = False


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_shutdown_local_without_server_handle(make_settings, host: str) -> None:
    app = create_app(make_settings())
    response = _client(app, host).post("/shutdown")
    assert response.status_code == 200
    assert response.json() == {"status": "shutting_down"}


def test_shutdown_local_signals_server_after_response(make_settings) -> None:
    app = create_app(make_settings())
    server = FakeServer()
    app.state.server = server
    response = _client(app).post("/shutdown")
    assert response.status_code == 200
    assert server.should_exit is True


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "testclient"])
def test_shutdown_rejected_from_non_local_client(make_settings, host: str) -> None:
    app = create_app(make_settings())
    server = FakeServer()
    app.state.server = server
    response = _client(app, host).post("/shutdown")
    assert response.status_code == 403
    assert server.should_exit is False


def test_shutdown_requires_post(make_settings) -> None:
    app = create_app(make_settings())
    assert _client(app).get("/shutdown").status_code == 405
