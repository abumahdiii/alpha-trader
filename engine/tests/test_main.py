"""Entry-point wiring, verified without starting a server (no socket is opened)."""

from __future__ import annotations

import pytest

from alpha_engine import __main__ as entry


def test_build_server_binds_localhost_only_single_process(make_settings) -> None:
    server = entry.build_server(make_settings("ENGINE_PORT=9123\n"))
    config = server.config
    assert entry.HOST == "127.0.0.1"
    assert config.host == "127.0.0.1"
    assert config.port == 9123
    assert config.reload is False
    assert config.workers == 1
    # The app gets the server handle so POST /shutdown can stop it.
    assert config.app.state.server is server


def test_host_is_not_configurable_from_env(make_settings) -> None:
    server = entry.build_server(make_settings("HOST=0.0.0.0\nENGINE_HOST=0.0.0.0\n"))
    assert server.config.host == "127.0.0.1"


def test_main_returns_2_on_invalid_port(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("ENGINE_PORT", "80")
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "invalid configuration" in err


def test_main_runs_server(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(entry.uvicorn.Server, "run", lambda self, *a, **k: calls.append(self.config.host))
    assert entry.main() == 0
    assert calls == ["127.0.0.1"]
