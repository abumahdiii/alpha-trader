"""/strategies router mounted on a local FastAPI app with a test registry and a tmp database."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpha_engine import logging_setup
from alpha_engine.routes.strategies import router
from alpha_engine.storage.db import open_db
from alpha_engine.strategy.base import Strategy, StrategyContext
from alpha_engine.strategy.params import ParamSchema, ParamSpec, params_hash
from alpha_engine.strategy.registry import StrategyRegistry
from alpha_engine.strategy.signal import SignalCandidate

TIME_Z = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class Channel(Strategy):
    name: ClassVar[str] = "channel_double"
    version: ClassVar[int] = 2
    title_fa: ClassVar[str] = "کانال آزمایشی"
    param_schema: ClassVar[ParamSchema] = ParamSchema([
        ParamSpec(name="channel_period", type="int", default=100, min=20, max=500, label_fa="دوره کانال"),
        ParamSpec(name="k", type="float", default=2.0, min=0.5, max=4.0, step=0.1, label_fa="ضریب"),
    ])

    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        return None


DEFAULTS = {"channel_period": 100, "k": 2.0}


@pytest.fixture
def app(tmp_path: Path) -> Iterator[FastAPI]:
    registry = StrategyRegistry()
    registry.register(Channel)
    application = FastAPI()
    application.include_router(router)
    application.state.db = open_db(tmp_path / "alpha.db")
    application.state.strategy_registry = registry
    yield application
    application.state.db.close()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_list_strategies(client: TestClient) -> None:
    response = client.get("/strategies")
    assert response.status_code == 200
    (item,) = response.json()
    assert item["name"] == "channel_double"
    assert item["title_fa"] == "کانال آزمایشی"
    assert item["version"] == 2
    assert item["params_version"] == 1
    assert item["params"] == DEFAULTS
    assert item["params_hash"] == params_hash(DEFAULTS)
    assert item["params_errors_fa"] == []
    assert [p["name"] for p in item["param_schema"]] == ["channel_period", "k"]
    assert item["param_schema"][0]["min"] == 20 and item["param_schema"][1]["step"] == 0.1
    import re

    assert re.match(TIME_Z, item["params_saved_utc"])


def test_get_one_and_404(client: TestClient) -> None:
    assert client.get("/strategies/channel_double").json()["params"] == DEFAULTS
    response = client.get("/strategies/unknown")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "strategy_not_found"
    assert detail["message_fa"] == "استراتژی «unknown» پیدا نشد."
    assert client.put("/strategies/unknown", json={"params": {}}).status_code == 404


def test_put_versioning(client: TestClient) -> None:
    same = client.put("/strategies/channel_double", json={"params": DEFAULTS})
    assert same.status_code == 200
    assert (same.json()["params_version"], same.json()["created_new_version"]) == (1, True)  # first save

    again = client.put("/strategies/channel_double", json={"params": {"channel_period": 100, "k": 2}})
    assert (again.json()["params_version"], again.json()["created_new_version"]) == (1, False)

    changed = client.put("/strategies/channel_double", json={"params": {"channel_period": 150}})
    body = changed.json()
    assert changed.status_code == 200
    assert (body["params_version"], body["created_new_version"]) == (2, True)
    assert body["params"] == {"channel_period": 150, "k": 2.0}  # full replacement: k takes its default
    assert client.get("/strategies/channel_double").json()["params_version"] == 2


@pytest.mark.parametrize(
    ("body", "code", "fragment"),
    [
        ({"params": {"channel_period": 10}}, "invalid_params", "نباید کمتر از 20"),
        ({"params": {"k": True}}, "invalid_params", "باید عدد باشد"),
        ({"params": {"bogus": 1}}, "invalid_params", "پارامتر ناشناخته"),
        ({"params": [1]}, "invalid_body", "params"),
        ({"channel_period": 150}, "invalid_body", "بدنه درخواست"),
        ({"params": {}, "extra": 1}, "invalid_body", "فیلد ناشناخته"),
        ([1, 2], "invalid_body", "بدنه درخواست"),
    ],
)
def test_put_invalid_returns_422_persian(client: TestClient, body: object, code: str, fragment: str) -> None:
    response = client.put("/strategies/channel_double", json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["message_fa"]
    assert any(fragment in e for e in detail["errors_fa"]), detail["errors_fa"]
    assert client.get("/strategies/channel_double").json()["params_version"] == 1


def test_stored_params_invalid_shown_with_errors(app: FastAPI, client: TestClient) -> None:
    client.put("/strategies/channel_double", json={"params": {"channel_period": 400}})

    class Tightened(Channel):
        version = 3
        param_schema = ParamSchema([
            ParamSpec(name="channel_period", type="int", default=100, min=20, max=300, label_fa="دوره کانال"),
            ParamSpec(name="k", type="float", default=2.0, min=0.5, max=4.0, label_fa="ضریب"),
        ])

    registry = StrategyRegistry()
    registry.register(Tightened)
    app.state.strategy_registry = registry
    item = client.get("/strategies/channel_double").json()
    assert item["params"] == {"channel_period": 400, "k": 2.0}
    assert len(item["params_errors_fa"]) == 1 and "300" in item["params_errors_fa"][0]
    fixed = client.put("/strategies/channel_double", json={"params": {"channel_period": 250}}).json()
    assert (fixed["params_version"], fixed["params_errors_fa"]) == (2, [])  # v1 = 400, v2 = 250


def test_db_missing_returns_503() -> None:
    application = FastAPI()
    application.include_router(router)
    with TestClient(application) as c:
        response = c.get("/strategies")
    assert response.status_code == 503
    assert response.json()["detail"]["message_fa"] == "پایگاه داده engine در دسترس نیست."


def _capture(make_settings, dev: bool) -> io.StringIO:
    stream = io.StringIO()
    logging_setup.configure_logging(make_settings(f"DEV_MODE={'true' if dev else 'false'}\n"),
                                    stream=stream, force=True)
    return stream


def test_dev_mode_logs_state_changes_and_rejections(client: TestClient, make_settings) -> None:
    stream = _capture(make_settings, dev=True)
    client.put("/strategies/channel_double", json={"params": {"channel_period": 150}})
    client.put("/strategies/channel_double", json={"params": {"channel_period": 5}})
    text = stream.getvalue()
    assert "strategy params version saved: channel_double v" in text
    assert "params rejected for channel_double" in text


def test_logs_silent_without_dev_mode(client: TestClient, make_settings) -> None:
    stream = _capture(make_settings, dev=False)
    client.put("/strategies/channel_double", json={"params": {"channel_period": 150}})
    client.put("/strategies/channel_double", json={"params": {"channel_period": 5}})
    assert stream.getvalue() == ""
    assert logging.getLogger("alpha_engine").level == logging.WARNING
