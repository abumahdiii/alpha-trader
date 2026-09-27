"""2A-wire: /strategies and /settings mounted on the real app (create_app), DB opened by the lifespan.

Every app here gets its settings from ``make_settings``, whose data dir is ``tmp_path / "data"``: the
database file must appear there and nowhere else (never in ``<repo>/data``).
"""

from __future__ import annotations

import io
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpha_engine import logging_setup
from alpha_engine.app import create_app
from alpha_engine.config import DEFAULT_DATA_DIR, Settings
from alpha_engine.storage.db import DB_FILENAME, EngineConnection
from alpha_engine.strategies.stddev_channel import SCHEMA, StdDevChannelStrategy
from alpha_engine.strategy.params import params_hash
from alpha_engine.strategy.registry import default_registry

TOP_KEYS = {"status", "service", "version", "pid", "dev_mode", "time_utc", "mt5"}
DB_UNAVAILABLE_FA = "پایگاه داده engine در دسترس نیست."
SETTINGS_DEFAULTS = {"balance": 1000.0, "risk_pct": 1.0, "leverage": 100, "rr": 2.0}


def _is_persian(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text)


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def started(settings: Settings) -> Iterator[tuple[FastAPI, TestClient]]:
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield app, client


# --- lifespan: DB open/close under data_dir ----------------------------------------------------------

def test_db_opened_under_sandboxed_data_dir_and_closed_on_shutdown(settings: Settings, tmp_path: Path) -> None:
    real_db = DEFAULT_DATA_DIR / DB_FILENAME
    real_before = real_db.stat().st_mtime_ns if real_db.exists() else None
    app = create_app(settings)
    assert app.state.db is None  # nothing opened by the factory itself
    assert not (tmp_path / "data" / DB_FILENAME).exists()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        db = app.state.db
        assert isinstance(db, EngineConnection)
        assert settings.data_dir == tmp_path / "data"
        assert (tmp_path / "data" / DB_FILENAME).is_file()
        assert client.get("/strategies").status_code == 200
    assert app.state.db is None
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")  # closed
    # The user's real data directory was not touched.
    real_after = real_db.stat().st_mtime_ns if real_db.exists() else None
    assert real_after == real_before
    assert settings.data_dir != DEFAULT_DATA_DIR


def test_without_lifespan_no_db_file_and_routes_answer_503(settings: Settings, tmp_path: Path) -> None:
    client = TestClient(create_app(settings), client=("127.0.0.1", 50000))  # no `with`: no lifespan
    for method, path in (("get", "/strategies"), ("get", "/settings"), ("put", "/settings")):
        response = getattr(client, method)(path, **({"json": {"rr": 3}} if method == "put" else {}))
        assert response.status_code == 503
        assert response.json()["detail"]["message_fa"] == DB_UNAVAILABLE_FA
    assert client.get("/health").status_code == 200
    assert not (tmp_path / "data" / DB_FILENAME).exists()


def test_db_open_failure_keeps_health_and_returns_503(make_settings, tmp_path: Path) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")  # data_dir is a FILE -> mkdir fails -> DB cannot open
    app = create_app(make_settings(environ={"ALPHA_TRADER_DATA_DIR": str(blocker)}))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert app.state.db is None
        health = client.get("/health")
        assert health.status_code == 200 and set(health.json()) == TOP_KEYS
        response = client.get("/strategies")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "db_unavailable"


def test_health_contract_unchanged_inside_lifespan(started) -> None:
    _, client = started
    body = client.get("/health").json()
    assert set(body) == TOP_KEYS
    assert body["status"] == "ok" and body["service"] == "alpha_engine"
    assert body["mt5"]["state"] == "not_initialized"


def test_registry_import_registers_stddev_channel(started) -> None:
    app, _ = started
    assert app.state.strategy_registry is default_registry
    assert default_registry.get("stddev_channel") is StdDevChannelStrategy


def test_routers_are_mounted(settings: Settings) -> None:
    paths = create_app(settings).openapi()["paths"]
    assert {"/health", "/shutdown", "/symbols", "/rates", "/rates/meta", "/rates/gaps", "/rates/update",
            "/strategies", "/strategies/{name}", "/settings", "/chart/channel", "/chart/setups"} <= set(paths)
    assert {"get", "put"} <= set(paths["/strategies/{name}"]) and {"get", "put"} <= set(paths["/settings"])
    assert set(paths["/rates/update"]) == {"post"} and set(paths["/rates/gaps"]) == {"get"}
    assert set(paths["/chart/channel"]) == {"get"} and set(paths["/chart/setups"]) == {"get"}


# --- /strategies ---------------------------------------------------------------------------------------

def _stddev(items: list[dict]) -> dict:
    matches = [item for item in items if item["name"] == "stddev_channel"]
    assert len(matches) == 1
    return matches[0]


def test_get_strategies_lists_stddev_channel_v1_with_full_persian_schema(started) -> None:
    _, client = started
    response = client.get("/strategies")
    assert response.status_code == 200
    item = _stddev(response.json())
    assert item["title_fa"] == "کانال انحراف معیار"
    assert item["version"] == 1 and item["params_version"] == 1
    assert item["params"] == SCHEMA.defaults()
    assert item["params"]["n"] == 100 and item["params"]["k"] == 2.0 and item["params"]["sigma_ddof"] == 0
    assert item["params_hash"] == params_hash(SCHEMA.defaults())
    assert item["params_errors_fa"] == []
    schema = item["param_schema"]
    assert [spec["name"] for spec in schema] == list(SCHEMA.names) and len(schema) == 15
    assert schema == SCHEMA.to_list()  # full spec: type, default, min, max, choices, step, labels
    assert all(_is_persian(spec["label_fa"]) for spec in schema)
    n_spec = schema[0]
    assert (n_spec["type"], n_spec["default"], n_spec["min"], n_spec["max"]) == ("int", 100, 10, 500)
    assert n_spec["label_fa"] == "طول کانال (تعداد کندل H4)"
    assert client.get("/strategies/stddev_channel").json() == item


def test_put_new_params_creates_version_2_same_params_keep_it(started, settings: Settings) -> None:
    _, client = started
    client.get("/strategies")  # v1 = defaults
    changed = client.put("/strategies/stddev_channel", json={"params": {"n": 120}})
    assert changed.status_code == 200
    body = changed.json()
    assert (body["params_version"], body["created_new_version"]) == (2, True)
    assert body["params"] == {**SCHEMA.defaults(), "n": 120}
    assert body["params_hash"] == params_hash({**SCHEMA.defaults(), "n": 120})

    same = client.put("/strategies/stddev_channel", json={"params": {"n": 120}}).json()
    assert (same["params_version"], same["created_new_version"]) == (2, False)
    # Equivalent spelling after validation (int given as float/string, defaults written out): same version.
    spelled = client.put("/strategies/stddev_channel",
                         json={"params": {**SCHEMA.defaults(), "n": "120", "k": 2}}).json()
    assert (spelled["params_version"], spelled["created_new_version"]) == (2, False)
    assert _stddev(client.get("/strategies").json())["params_version"] == 2


def test_params_version_survives_restart(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/strategies/stddev_channel").json()["params_version"] == 1  # v1 = defaults
        client.put("/strategies/stddev_channel", json={"params": {"n": 150, "k": 2.5}})
    with TestClient(create_app(settings)) as client:
        item = client.get("/strategies/stddev_channel").json()
    assert item["params_version"] == 2 and item["params"]["n"] == 150 and item["params"]["k"] == 2.5


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"n": 5}, "نباید کمتر از 10 باشد"),
        ({"n": 501}, "نباید بیشتر از 500 باشد"),
        ({"k": "wide"}, "باید عدد باشد"),
        ({"sigma_ddof": 2}, "باید یکی از این مقادیر باشد"),
        ({"bogus": 1}, "پارامتر ناشناخته"),
        ({"use_pin_bar": False, "use_engulfing": False}, "حداقل یکی از الگوهای تایید"),
    ],
)
def test_put_invalid_params_is_422_persian_and_saves_nothing(started, params: dict, needle: str) -> None:
    _, client = started
    response = client.put("/strategies/stddev_channel", json={"params": params})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_params" and _is_persian(detail["message_fa"])
    assert any(needle in message for message in detail["errors_fa"]), detail["errors_fa"]
    assert _stddev(client.get("/strategies").json())["params_version"] == 1  # nothing saved


def test_put_bad_body_and_unknown_strategy(started) -> None:
    _, client = started
    bad = client.put("/strategies/stddev_channel", json={"n": 120})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_body"
    missing = client.put("/strategies/nope", json={"params": {}})
    assert missing.status_code == 404 and "nope" in missing.json()["detail"]["message_fa"]


# --- /settings -------------------------------------------------------------------------------------------

def test_settings_round_trip(started, settings: Settings) -> None:
    _, client = started
    assert client.get("/settings").json() == SETTINGS_DEFAULTS
    updated = client.put("/settings", json={"balance": 5000, "risk_pct": 0.5, "leverage": 200, "rr": 3})
    assert updated.status_code == 200
    expected = {"balance": 5000.0, "risk_pct": 0.5, "leverage": 200, "rr": 3.0}
    assert updated.json() == expected
    assert client.get("/settings").json() == expected
    partial = client.put("/settings", json={"rr": 1.5}).json()
    assert partial == {**expected, "rr": 1.5}
    with TestClient(create_app(settings)) as again:  # persisted in <data_dir>/alpha.db
        assert again.get("/settings").json() == {**expected, "rr": 1.5}


@pytest.mark.parametrize(
    "payload",
    [{"risk_pct": 10}, {"risk_pct": 0.001}, {"leverage": 1}, {"leverage": 1000}, {"rr": 0.1}, {"rr": 20},
     {"balance": 1e9}],
)
def test_settings_edge_values_accepted(started, payload: dict) -> None:
    _, client = started
    response = client.put("/settings", json=payload)
    assert response.status_code == 200 and response.json() == {**SETTINGS_DEFAULTS, **payload}


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"risk_pct": 10.0001}, "«درصد ریسک» نباید بیشتر از 10 باشد."),
        ({"risk_pct": 0}, "«درصد ریسک» باید بیشتر از 0 باشد."),
        ({"leverage": 1001}, "«اهرم» نباید بیشتر از 1000 باشد."),
        ({"leverage": 100.5}, "«اهرم» باید عدد صحیح باشد."),
        ({"rr": 0.09}, "نباید کمتر از 0.1 باشد."),
        ({"balance": -1}, "«موجودی حساب» باید بیشتر از 0 باشد."),
        ({"balance": True}, "باید عدد باشد، نه true/false."),
        ({"margin": 5}, "فیلد ناشناخته"),
    ],
)
def test_settings_out_of_bounds_is_422_persian_and_not_saved(started, payload: dict, needle: str) -> None:
    _, client = started
    response = client.put("/settings", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_settings" and _is_persian(detail["message_fa"])
    assert any(needle in message for message in detail["errors_fa"]), detail["errors_fa"]
    assert client.get("/settings").json() == SETTINGS_DEFAULTS


# --- guarded logs (rule 01) -------------------------------------------------------------------------------

def _run_with_log(make_settings: Callable[..., Settings], dev: bool) -> str:
    settings = make_settings(f"DEV_MODE={'true' if dev else 'false'}\n")
    app = create_app(settings)
    stream = io.StringIO()
    logging_setup.configure_logging(settings, stream=stream, force=True)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.get("/strategies")  # creates v1 = defaults
        client.put("/strategies/stddev_channel", json={"params": {"n": 90}})
        client.put("/settings", json={"risk_pct": 2})
        client.put("/settings", json={"risk_pct": 99})
    return stream.getvalue()


def test_dev_mode_logs_db_lifecycle_and_changes(make_settings) -> None:
    text = _run_with_log(make_settings, dev=True)
    assert "startup: engine database opened at" in text
    assert "shutdown: engine database closed" in text
    assert "strategy params version saved: stddev_channel v2" in text
    assert "account settings updated:" in text
    assert "PUT /settings rejected:" in text


def test_logs_silent_without_dev_mode(make_settings) -> None:
    assert _run_with_log(make_settings, dev=False) == ""
