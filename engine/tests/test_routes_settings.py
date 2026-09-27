"""/settings router (mounted on a local FastAPI app) and AccountSettings bounds."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from alpha_engine.routes.settings import router
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.storage.db import open_db

DEFAULTS = {"balance": 2500.0, "risk_pct": 1.0, "leverage": 100, "rr": 2.0}  # default balance 2500 since 2026-09-27


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.state.db = open_db(tmp_path / "alpha.db")
    with TestClient(app) as c:
        yield c
    app.state.db.close()


# --- model bounds (edge values exactly as specified) ------------------------------------------------

@pytest.mark.parametrize(
    "values",
    [
        {"balance": 1e9}, {"balance": 0.01},
        {"risk_pct": 10}, {"risk_pct": 0.001},
        {"leverage": 1}, {"leverage": 1000},
        {"rr": 0.1}, {"rr": 20},
    ],
)
def test_bounds_accepted(values: dict) -> None:
    AccountSettings(**values)


@pytest.mark.parametrize(
    "values",
    [
        {"balance": 0}, {"balance": -1}, {"balance": 1e9 + 1},
        {"risk_pct": 0}, {"risk_pct": 10.0001},
        {"leverage": 0}, {"leverage": 1001}, {"leverage": 100.5}, {"leverage": True},
        {"rr": 0.0999}, {"rr": 20.001},
        {"balance": float("nan")}, {"balance": float("inf")}, {"risk_pct": True},
        {"extra": 1},
    ],
)
def test_bounds_rejected(values: dict) -> None:
    with pytest.raises(ValidationError):
        AccountSettings(**values)


def test_defaults_and_risk_amount_worked_example() -> None:
    s = AccountSettings()
    assert s.model_dump() == DEFAULTS
    assert s.risk_amount == 25.0  # 2500 * 1% = 25.00 USD risked per trade
    assert s.risk_amount * s.rr == 50.0  # rr 2 -> a winning trade targets 25.00 * 2 = 50.00 USD


# --- routes ----------------------------------------------------------------------------------------

def test_get_defaults(client: TestClient) -> None:
    response = client.get("/settings")
    assert response.status_code == 200
    assert response.json() == DEFAULTS


def test_put_partial_update_persists(client: TestClient) -> None:
    response = client.put("/settings", json={"risk_pct": 0.5})
    assert response.status_code == 200
    assert response.json() == {**DEFAULTS, "risk_pct": 0.5}
    response = client.put("/settings", json={"leverage": 500, "rr": 3})
    # balance was never PUT -> stays at the default 2500.0
    assert response.json() == {"balance": 2500.0, "risk_pct": 0.5, "leverage": 500, "rr": 3.0}
    assert client.get("/settings").json() == {"balance": 2500.0, "risk_pct": 0.5, "leverage": 500, "rr": 3.0}


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"risk_pct": 11}, "درصد ریسک"),
        ({"risk_pct": 0}, "باید بیشتر از 0"),
        ({"leverage": 1001}, "نباید بیشتر از 1000"),
        ({"leverage": 2.5}, "عدد صحیح"),
        ({"rr": 0.05}, "نباید کمتر از 0.1"),
        ({"balance": "abc"}, "موجودی حساب"),
        ({"balance": None}, "نمی‌تواند خالی باشد"),
        ({"leverage": True}, "true/false"),
        ({"unknown": 1}, "فیلد ناشناخته"),
        ([1, 2], "شیء کلید-مقدار"),
    ],
)
def test_put_invalid_returns_422_persian(client: TestClient, body: object, fragment: str) -> None:
    response = client.put("/settings", json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_settings"
    assert detail["message_fa"] == "تنظیمات حساب نامعتبر است."
    assert any(fragment in e for e in detail["errors_fa"]), detail["errors_fa"]
    assert client.get("/settings").json() == DEFAULTS  # nothing changed


def test_db_missing_returns_503() -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        response = c.get("/settings")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "db_unavailable"


def test_openapi_schema_builds_with_both_routers() -> None:
    from alpha_engine.routes.strategies import router as strategies_router

    app = FastAPI()
    app.include_router(strategies_router)
    app.include_router(router)
    paths = app.openapi()["paths"]
    assert {"/settings", "/strategies", "/strategies/{name}"} <= set(paths)
    assert {"get", "put"} <= set(paths["/strategies/{name}"]) and {"get", "put"} <= set(paths["/settings"])
