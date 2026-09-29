"""``/signals`` REST + ``/ws/signals`` through the real app and lifespan (fake live market, temp data dir only).

The lifespan starts the scheduler thread with the market-data service's clock (the test clock); the test also calls
``step()`` directly (serialised with the thread by the scheduler's own lock). SUGGESTIONS ONLY: no route can send
anything to the broker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpha_engine.app import create_app
from alpha_engine.data.timezone import OffsetModel
from alpha_engine.market_data import MarketDataService
from alpha_engine.mt5_adapter import Mt5Adapter
from alpha_engine.routes import signals as signals_routes
from alpha_engine.signals.service import LiveSignals
from alpha_engine.storage import db as db_module
from alpha_engine.storage.db import current_schema_version, open_db
from alpha_engine.storage.signals_repo import LiveSettings, SignalsRepo
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy
from alpha_engine.strategy.registry import StrategyRegistry
from fixtures.live_market import LiveFakeMt5, MarketClock, ScheduledBuy, seed_mt5_cache
from fixtures.synthetic_ohlcv import generate_dataset

GOLD = "XAUUSD.x"
MODEL = OffsetModel.fixed(2)
BAR = datetime(2025, 4, 28, 10, 0, tzinfo=timezone.utc)  # a stddev_channel candidate of the synthetic data
BOUNDARY = BAR + timedelta(hours=1)
ITEM_KEYS = {
    "id", "symbol", "status", "strategy", "strategy_version", "strategy_source", "strategy_sha256", "params_version",
    "params_hash", "confirmation_bar_time", "decision_time", "entry_bar_time", "expires_time", "expiry_pending",
    "direction", "setup_type", "setup_title_fa", "pattern", "line", "reference_price", "indicative_entry",
    "entry_source", "stop_loss", "take_profit_indicative", "take_profit_note_fa", "rr", "volume", "risk_amount",
    "sizing", "account", "reason_fa", "rejection_reason_fa", "backtest_would_skip", "backtest_skip_reason_fa",
    "gap_class_provisional", "entry_gap", "indicators", "mismatch", "superseded", "note_fa", "created_time",
    "updated_time",
}


class PluginLike(ScheduledBuy):
    name: ClassVar[str] = "plugin_like"
    source: ClassVar[str] = "plugin"  # type: ignore[assignment]
    code_sha256: ClassVar[str | None] = "b" * 64


@dataclass
class ApiEnv:
    app: FastAPI
    client: TestClient
    clock: MarketClock

    @property
    def live(self) -> LiveSignals:
        return self.app.state.live_signals

    def at(self, when: datetime, polls: int = 1) -> None:
        self.clock.set(when)
        for i in range(polls):
            if i:
                self.clock.advance(seconds=15)
            self.live.step()


@pytest.fixture(scope="module")
def gold():
    return [s for (sym, _), s in generate_dataset((GOLD,), "2025-02-24", "2025-05-05", 20250224, MODEL).items()]


@pytest.fixture
def api(make_settings, gold, tmp_path) -> Iterator[ApiEnv]:
    data_dir = tmp_path / "data"
    settings = make_settings(f"MT5_SERVER_UTC_OFFSET=2\nENGINE_SYMBOLS={GOLD}\n",
                             environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    clock = MarketClock(datetime(2025, 4, 28, 10, 30, tzinfo=timezone.utc))
    fake = LiveFakeMt5(gold, clock, MODEL)
    adapter = Mt5Adapter(settings, fake, sleep=lambda s: None, clock=clock.pc_clock)
    adapter.connect()
    service = MarketDataService.create(settings, adapter, clock=clock.pc_clock, update_min_interval=0.0)
    seed_mt5_cache(service, (GOLD,), datetime(2025, 2, 24, tzinfo=timezone.utc))
    registry = StrategyRegistry()
    registry.register(StdDevChannelStrategy)
    registry.register(PluginLike)
    app = create_app(settings, mt5_adapter=adapter, market_data=service, strategy_registry=registry)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield ApiEnv(app, client, clock)


def _signal(api: ApiEnv) -> dict:
    assert api.client.post("/signals/settings", json={"enabled": True}).status_code == 200
    api.at(api.clock.true_utc, polls=2)  # warm-up polls at 10:30 (bar 09:00 decided)
    api.at(BOUNDARY + timedelta(seconds=21))
    body = api.client.get("/signals").json()
    assert body["total"] == 1
    return body["signals"][0]


# ---------------------------------------------------------------------------------------------- status / settings
def test_status_and_settings_defaults(api: ApiEnv) -> None:
    status = api.client.get("/signals/status").json()
    assert status["enabled"] is False and status["state"] == "disabled" and status["mt5_state"] == "connected"
    assert status["live_strategy"] == "stddev_channel" and status["grace_s"] == 20.0
    assert status["last_tick_utc"] == {GOLD: None} and status["last_error_fa"] is None
    assert api.client.get("/signals/settings").json() == {"enabled": False, "live_strategy": "stddev_channel",
                                                          "grace_s": 20.0}
    assert api.client.get("/signals").json() == {"count": 0, "total": 0, "offset": 0, "limit": 50, "signals": []}


def test_settings_update_is_persisted_and_validated(api: ApiEnv) -> None:
    ok = api.client.post("/signals/settings", json={"enabled": True, "grace_s": 30})
    assert ok.status_code == 200, ok.text
    assert ok.json()["enabled"] is True and ok.json()["grace_s"] == 30.0 and ok.json()["status"]["enabled"] is True
    assert SignalsRepo(api.app.state.db).get_settings() == LiveSettings(enabled=True, grace_s=30.0)
    partial = api.client.post("/signals/settings", json={"grace_s": 0}).json()
    assert partial["enabled"] is True and partial["grace_s"] == 0.0
    for body, code in (({"grace_s": 601}, "invalid_body"), ({"enabled": "yes"}, "invalid_body"),
                       ({"unknown": 1}, "invalid_body"), ([1, 2], "invalid_body"), ({"live_strategy": ""}, "invalid_body")):
        response = api.client.post("/signals/settings", json=body)
        assert response.status_code == 422 and response.json()["detail"]["code"] == code, body
        assert any("؀" <= ch <= "ۿ" for ch in response.json()["detail"]["message_fa"])
    assert api.client.post("/signals/settings", json={"grace_s": 601}).json()["detail"]["errors_fa"]
    missing = api.client.post("/signals/settings", json={"live_strategy": "nope"})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "strategy_not_found"
    plugin = api.client.post("/signals/settings", json={"live_strategy": "plugin_like"})
    assert plugin.status_code == 409 and plugin.json()["detail"]["code"] == "plugin_not_live"
    assert "پلاگین" in plugin.json()["detail"]["message_fa"]
    assert api.client.get("/signals/settings").json()["live_strategy"] == "stddev_channel"  # unchanged


# ---------------------------------------------------------------------------------------------- list / get
def test_signal_list_detail_and_filters(api: ApiEnv) -> None:
    item = _signal(api)
    assert set(item) == ITEM_KEYS
    assert item["status"] == "active" and item["symbol"] == GOLD and item["strategy"] == "stddev_channel"
    assert item["confirmation_bar_time"] == "2025-04-28T10:00:00Z" and item["decision_time"] == "2025-04-28T11:00:00Z"
    assert item["entry_bar_time"] == "2025-04-28T11:00:00Z" and item["expires_time"] == "2025-04-28T12:00:00Z"
    assert item["expiry_pending"] is False and item["entry_source"] == "tick"
    assert item["take_profit_indicative"] == pytest.approx(
        item["indicative_entry"] + 2 * abs(item["indicative_entry"] - item["stop_loss"]))
    assert item["volume"] > 0 and item["sizing"]["accepted"] is True and item["account"]["balance"] == 2500.0
    assert "سفارش" in item["note_fa"] and "حد سود واقعی" in item["take_profit_note_fa"]
    assert item["indicators"]["line_value"] > 0 and item["mismatch"] is None
    assert api.client.get(f"/signals/{item['id']}").json() == item
    missing = api.client.get("/signals/999")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "signal_not_found"
    assert api.client.get("/signals", params={"status": "active", "symbol": GOLD}).json()["total"] == 1
    assert api.client.get("/signals", params={"status": "expired"}).json()["total"] == 0
    assert api.client.get("/signals", params={"offset": 1}).json()["signals"] == []
    for params in ({"status": "open"}, {"limit": 0}, {"limit": 501}, {"offset": -1}, {"symbol": "X" * 33}):
        response = api.client.get("/signals", params=params)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_query", params


# ---------------------------------------------------------------------------------------------- WebSocket
def test_ws_snapshot_signal_expired_and_keepalive(api: ApiEnv, monkeypatch) -> None:
    monkeypatch.setattr(signals_routes, "WS_KEEPALIVE_S", 0.3)
    monkeypatch.setattr(signals_routes, "WS_POLL_S", 0.05)
    assert api.client.post("/signals/settings", json={"enabled": True}).status_code == 200
    api.at(api.clock.true_utc, polls=2)
    with api.client.websocket_connect("/ws/signals") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["active"] == [] and snap["status"]["enabled"] is True
        api.at(BOUNDARY + timedelta(seconds=21))
        seen: list[dict] = []
        while not any(m["type"] == "signal" for m in seen):
            seen.append(ws.receive_json())
        signal = next(m for m in seen if m["type"] == "signal")["signal"]
        assert signal["confirmation_bar_time"] == "2025-04-28T10:00:00Z" and signal["status"] == "active"
        assert set(signal) == ITEM_KEYS
        api.at(BOUNDARY + timedelta(hours=1, seconds=21))
        while True:
            message = ws.receive_json()
            if message["type"] == "expired":
                assert message["signal"]["id"] == signal["id"] and message["signal"]["status"] == "expired"
                break
        while True:  # idle -> keepalive
            message = ws.receive_json()
            if message["type"] == "keepalive":
                assert message["time_utc"].endswith("Z")
                break
    with api.client.websocket_connect("/ws/signals") as ws:  # a new client gets the current state first
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["active"] == [] and snap["seq"] > 0
        assert {m["type"] for m in [ws.receive_json()]} <= {"keepalive", "status"}


def test_ws_status_messages_on_settings_change(api: ApiEnv) -> None:
    with api.client.websocket_connect("/ws/signals") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        api.client.post("/signals/settings", json={"enabled": True})
        while True:
            message = ws.receive_json()
            if message["type"] == "status" and message["status"]["enabled"]:
                break


# ---------------------------------------------------------------------------------------------- lifecycle / 503
def test_lifespan_starts_and_stops_the_scheduler_before_the_database(make_settings, tmp_path) -> None:
    settings = make_settings("", environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "data")})
    app = create_app(settings)
    assert app.state.live_signals is None
    with TestClient(app) as client:
        live = app.state.live_signals
        assert isinstance(live, LiveSignals) and live._thread is not None and live._thread.is_alive()
        status = client.get("/signals/status").json()
        assert status["state"] == "disabled" and status["mt5_state"] == "not_initialized"
    assert app.state.live_signals is None and not live._thread.is_alive() and live.status()["state"] == "stopped"


def test_routes_answer_503_without_the_database(make_settings, tmp_path) -> None:
    app = create_app(make_settings("", environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "data")}))
    client = TestClient(app)  # no lifespan: no database, no scheduler
    for path in ("/signals", "/signals/status", "/signals/settings", "/signals/1"):
        response = client.get(path)
        assert response.status_code == 503 and response.json()["detail"]["code"] == "db_unavailable", path
    with client.websocket_connect("/ws/signals") as ws:
        assert ws.receive_json()["code"] == "db_unavailable"


def test_enabled_without_mt5_is_mt5_down_and_never_crashes(make_settings, tmp_path) -> None:
    settings = make_settings("", environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "data")})
    app = create_app(settings)  # autoconnect off, no terminal
    with TestClient(app) as client:
        assert client.post("/signals/settings", json={"enabled": True}).status_code == 200
        app.state.live_signals.step()
        status = client.get("/signals/status").json()
        assert status["state"] == "mt5_down" and "MetaTrader" in status["last_error_fa"]
        assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------------------------- migration v4
def _open_v3(path: Path) -> None:
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_utc TEXT NOT NULL)")
    for version in (1, 2, 3):
        for statement in db_module.MIGRATIONS[version]:
            raw.execute(statement)
        raw.execute("INSERT INTO schema_version VALUES (?, '2026-09-28T00:00:00.000000Z')", (version,))
    raw.execute("INSERT INTO account_settings VALUES (1, 5000.0, 0.5, 200, 3.0, '2026-09-28T00:00:00.000000Z')")
    raw.execute("INSERT INTO strategy_plugins (name, version, sha256, title_fa, file_relpath, manifest_json,"
                " validation_json, status, created_utc) VALUES ('p', 1, ?, 't', 'x.py', '{}', '{}', 'active', 'now')",
                ("c" * 64,))
    raw.close()


def test_v3_database_upgrades_to_v4_keeping_data(tmp_path: Path) -> None:
    path = tmp_path / "alpha.db"
    _open_v3(path)
    conn = open_db(path)
    try:
        assert current_schema_version(conn) == 4
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")] == [1, 2, 3, 4]
        assert conn.execute("SELECT balance FROM account_settings").fetchone()[0] == 5000.0
        assert conn.execute("SELECT COUNT(*) FROM strategy_plugins").fetchone()[0] == 1
        repo = SignalsRepo(conn)
        assert repo.get_settings() == LiveSettings()  # defaults until the first save
        row = {"symbol": GOLD, "strategy_name": "stddev_channel", "strategy_version": 1, "strategy_source": "builtin",
               "params_hash": "d" * 64, "confirmation_bar_open_utc": BAR, "decision_time_utc": BOUNDARY,
               "status": "active", "direction": "buy", "extra_json": {"candidate": None}}
        first = repo.insert(row)
        assert first == 1 and repo.insert({**row, "direction": "sell"}) is None  # dedupe key: the first row wins
        assert repo.get(1)["direction"] == "buy"
        assert (repo.insert({**row, "params_hash": "e" * 64}) or 0) > first  # other params -> another signal
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert({**row, "confirmation_bar_open_utc": BAR - timedelta(hours=1), "status": "open"})
        repo.add_event("check", GOLD, {"result": "no_setup"})
        with pytest.raises(ValueError):
            repo.add_event("order", GOLD, {})
        assert repo.events(kind="check")[0]["payload"] == {"result": "no_setup"}
        assert repo.save_settings(LiveSettings(enabled=True, grace_s=5.0)) and repo.get_settings().grace_s == 5.0
    finally:
        conn.close()
    again = open_db(path)  # idempotent
    assert current_schema_version(again) == 4
    again.close()
