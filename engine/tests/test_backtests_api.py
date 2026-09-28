"""``/backtests`` + ``/ws/backtests/{id}`` end to end on a seeded synthetic cache (no MT5, never the real data/).

The synthetic XAUUSD.x cache spans 2024-06-03 .. 2025-03-03 (~9 months, ~4.4k H1 bars with weekends,
session breaks, holidays and planted holes). With the default params the first allowed window start
(channel + ATR warm-up) is in early August 2024, so manual windows from 2024-09-01 and random windows of
1 month fit. Every stored number is compared EXACTLY with a direct ``run_backtest`` + ``compute_metrics``
on the same cached frames.
"""

from __future__ import annotations

import logging
import math
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from alpha_engine.app import create_app
from alpha_engine.backtest import jobs as jobs_module
from alpha_engine.backtest.history import load_history
from alpha_engine.backtest.metrics import compute_metrics, summarize_windows
from alpha_engine.backtest.models import NO_SWAP_LABEL_FA, PROVISIONAL_LABEL_FA, SEED_MAX, RunConfig
from alpha_engine.backtest.runner import run_backtest
from alpha_engine.config import Settings
from alpha_engine.data.cache import OhlcvCache
from alpha_engine.logging_setup import ROOT_LOGGER_NAME
from alpha_engine.routes import backtests as routes_module
from alpha_engine.storage.backtests_repo import ACTIVE_STATUSES, TERMINAL_STATUSES, BacktestsRepo
from alpha_engine.storage.db import DB_FILENAME, open_db
from alpha_engine.strategy.base import bar_open_times
from fixtures.seed_cache import seed_cache

GOLD, BRENT = "XAUUSD.x", "BRNUSD.x"
START, END = "2024-06-03", "2025-03-03"
MANUAL = {"symbol": GOLD, "mode": "manual", "from": "2024-09-02T00:00:00Z", "to": "2025-02-24T00:00:00Z"}
RANDOM = {"symbol": GOLD, "mode": "random", "windows_count": 4, "window_months": 1, "seed": 7}
TERMINAL = {"done", "error", "cancelled", "interrupted"}


def _is_persian(text: str | None) -> bool:
    return bool(text) and any("؀" <= ch <= "ۿ" for ch in text)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("bt_seed")
    seed_cache(out, repo_data_dir=out / "not_data", symbols=(GOLD,), start=START, end=END)
    return out


@dataclass
class Env:
    app: FastAPI
    client: TestClient
    data_dir: Path
    settings: Settings

    def post(self, body: dict) -> Any:
        return self.client.post("/backtests", json=body)

    def submit(self, body: dict) -> int:
        response = self.post(body)
        assert response.status_code == 202, response.text
        return response.json()["id"]

    def detail(self, run_id: int) -> dict:
        response = self.client.get(f"/backtests/{run_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def wait(self, run_id: int, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.detail(run_id)
            if body["status"] in TERMINAL:
                return body
            time.sleep(0.05)
        raise AssertionError(f"run {run_id} did not finish: {self.detail(run_id)['status']}")

    def trades(self, run_id: int, **query: Any) -> dict:
        response = self.client.get(f"/backtests/{run_id}/trades", params=query)
        assert response.status_code == 200, response.text
        return response.json()

    def equity(self, run_id: int, **query: Any) -> dict:
        response = self.client.get(f"/backtests/{run_id}/equity", params=query)
        assert response.status_code == 200, response.text
        return response.json()

    def direct(self, detail: dict):
        """The same run computed directly (no API, no DB) from the stored config snapshot."""
        config = RunConfig.model_validate(detail["config"])
        if detail["mode"] == "random" and config.seed is None:
            config = config.model_copy(update={"seed": detail["seed"]})
        return run_backtest(config, load_history(OhlcvCache(self.data_dir), config.symbol))


def _make_env(make_settings, seeded: Path, tmp_path: Path, env_text: str = "") -> Iterator[Env]:
    data_dir = tmp_path / "data"
    if not data_dir.exists():
        shutil.copytree(seeded, data_dir)
    settings = make_settings(env_text, environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield Env(app=app, client=client, data_dir=data_dir, settings=settings)


@pytest.fixture
def env(make_settings, seeded: Path, tmp_path: Path) -> Iterator[Env]:
    yield from _make_env(make_settings, seeded, tmp_path)


class Gate:
    """Blocks the worker at a chosen point until released (released automatically after ``auto`` s)."""

    def __init__(self, auto: float = 20.0) -> None:
        self.entered = threading.Event()
        self.release_event = threading.Event()
        self.auto = auto

    def wait(self) -> None:
        self.entered.set()
        self.release_event.wait(self.auto)

    def release(self) -> None:
        self.release_event.set()


@pytest.fixture
def scan_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[Gate]:
    """The FIRST run's full-history scan waits for the gate (status running, phase scanning)."""
    gate = Gate()
    original = jobs_module.full_scan
    calls = {"n": 0}

    def gated(prepared, config, lru=None, strategy=None):
        calls["n"] += 1
        if calls["n"] == 1:
            gate.wait()
        return original(prepared, config, lru, strategy)

    monkeypatch.setattr(jobs_module, "full_scan", gated)
    yield gate
    gate.release()


def _ws_messages(env: Env, run_id: int) -> list[dict]:
    """Progress + final messages (keepalives, sent only after long idle periods, are skipped)."""
    messages = []
    with env.client.websocket_connect(f"/ws/backtests/{run_id}") as ws:
        while True:
            msg = ws.receive_json()
            if msg["type"] == "keepalive":
                continue
            messages.append(msg)
            if msg["type"] != "progress":
                break
    return messages


# ------------------------------------------------------------------------------------------ manual run
def test_manual_run_end_to_end_with_ws_and_exact_metrics(env: Env, scan_gate: Gate) -> None:
    response = env.post(MANUAL)
    assert response.status_code == 202
    body = response.json()
    run_id = body["id"]
    assert body["status"] == "queued" and body["provisional"] is True
    assert body["labels_fa"][0] == PROVISIONAL_LABEL_FA and NO_SWAP_LABEL_FA in body["labels_fa"]
    assert scan_gate.entered.wait(10)

    with env.client.websocket_connect(f"/ws/backtests/{run_id}") as ws:
        first = ws.receive_json()
        assert first["type"] == "progress" and first["status"] == "running" and first["phase"] == "scanning"
        assert first["windows"] == 1 and first["id"] == run_id
        assert env.client.get("/health").status_code == 200  # responsive while the run is active
        scan_gate.release()
        messages = [first]
        while messages[-1]["type"] == "progress":
            messages.append(ws.receive_json())
    final = messages[-1]
    assert final["type"] == "done" and final["status"] == "done" and final["percent"] == 100.0
    pcts = [m["percent"] for m in messages]
    assert all(b >= a for a, b in zip(pcts, pcts[1:])) and 0 <= pcts[0] < 100

    detail = env.wait(run_id)
    assert detail["status"] == "done" and detail["progress"] == 100.0 and detail["error"] is None
    direct = env.direct(detail)
    window = direct.windows[0]
    expected = compute_metrics(window.trades, window.equity, window.initial_balance).to_dict()
    assert detail["metrics_kind"] == "single_window" and detail["metrics"] == expected
    assert detail["windows"][0]["metrics"] == expected and detail["distribution"] is None
    assert final["trade_count"] == detail["trade_count"] == len(window.trades) > 0
    assert detail["net_profit"] == expected["net_profit"] and final["net_profit"] == expected["net_profit"]
    # provenance (rule 03 section 3)
    cfg = detail["config"]
    assert cfg["params_hash"] == detail["params_hash"] and cfg["params_version"] == detail["params_version"] == 1
    assert cfg["strategy_name"] == detail["strategy"] == "stddev_channel" and cfg["strategy_version"] == 1
    assert cfg["account"] == env.client.get("/settings").json()
    assert cfg["cost_model"] == {"spread": "historical", "fallback_spread_points": None,
                                 "commission_per_lot_per_side": 0.0, "swap": "none"}
    assert detail["plan"]["windows"][0]["start"] == "2024-09-02T00:00:00Z" and detail["plan"]["mode"] == "manual"
    assert detail["fingerprint"]["h1_source"] == "seed" and len(detail["fingerprint"]["h1_sha256"]) == 64
    assert detail["fingerprint"] == direct.fingerprint.model_dump(mode="json")
    assert detail["labels_fa"] == direct.labels_fa and PROVISIONAL_LABEL_FA in detail["labels_fa"]
    assert detail["provisional"] is True and detail["provisional_label_fa"] == PROVISIONAL_LABEL_FA
    assert detail["spread_fallback"] == direct.spread_fallback.model_dump(mode="json")
    assert detail["request"] == MANUAL and detail["from"] == MANUAL["from"] and detail["to"] == MANUAL["to"]
    assert detail["result_meta"]["entry_rule"] == "next_bar_open"
    assert {"load_s", "scan_s", "simulate_s", "metrics_s", "persist_s", "total_s"} <= set(detail["timings"])
    assert detail["elapsed_s"] == detail["timings"]["total_s"]
    # the worker reused the history the POST validation prepared (shared LRU with the chart routes)
    assert env.app.state.chart_cache.hits >= 1

    # trades: full payloads, identical to the direct run
    page = env.trades(run_id)
    assert page["total"] == len(window.trades) and page["trades"] == [t.model_dump(mode="json") for t in window.trades]
    paged = env.trades(run_id, offset=1, limit=2)
    assert paged["trades"] == page["trades"][1:3] and paged["total"] == page["total"] and paged["limit"] == 2
    assert env.trades(run_id, window=0)["total"] == page["total"]

    # equity: a subsequence of the full per-bar equity, with the documented points kept
    eq = env.equity(run_id)["points"]
    full = {p.time.strftime("%Y-%m-%dT%H:%M:%SZ"): p for p in window.equity}
    assert detail["windows"][0]["equity_points_full"] == len(window.equity) > len(eq) == \
        detail["windows"][0]["equity_points_stored"]
    for p in eq:
        ref = full[p["time"]]
        assert (p["balance"], p["equity"]) == (ref.balance, ref.equity)
    times = [p["time"] for p in eq]
    assert times[0] == window.equity[0].time.strftime("%Y-%m-%dT%H:%M:%SZ") and times == sorted(times)
    assert times[-1] == window.equity[-1].time.strftime("%Y-%m-%dT%H:%M:%SZ")
    stored_ts = {p.time.timestamp() for p in window.equity if p.time.strftime("%Y-%m-%dT%H:%M:%SZ") in set(times)}
    assert {t.exit_bar_time.timestamp() + 3600 for t in window.trades} <= stored_ts  # every exit bar close

    # worked-example checks: net profit, win rate and max drawdown recomputed by hand from stored rows
    trades = page["trades"]
    net = math.fsum(t["net_pnl"] for t in trades)
    assert net == pytest.approx(expected["net_profit"], abs=1e-9)
    assert sum(t["net_pnl"] > 1e-9 for t in trades) / len(trades) == expected["win_rate"]
    peak, max_dd = window.initial_balance, 0.0
    for p in eq:
        peak = max(peak, p["equity"])
        max_dd = max(max_dd, peak - p["equity"])
    assert max_dd == pytest.approx(expected["max_drawdown_abs"], abs=1e-9)
    assert trades[-1]["balance_after"] == pytest.approx(window.initial_balance + net, abs=1e-6)

    # listing
    runs = env.client.get("/backtests").json()
    assert runs["count"] == 1 and runs["runs"][0]["id"] == run_id and runs["runs"][0]["summary_basis"] == "single_window"
    assert runs["runs"][0]["trade_count"] == len(window.trades) and runs["runs"][0]["seed"] is None

    # a finished run: WS sends the final message immediately; cancel -> 409; delete works
    assert _ws_messages(env, run_id) == [final]
    response = env.client.post(f"/backtests/{run_id}/cancel")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "not_cancellable"
    assert env.client.delete(f"/backtests/{run_id}").json() == {"id": run_id, "deleted": True}
    assert env.client.get(f"/backtests/{run_id}").status_code == 404
    db = env.app.state.db
    for table in ("backtest_windows", "backtest_trades", "backtest_equity"):
        assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)).fetchone()[0] == 0


def test_user_fallback_spread_and_commission_are_snapshotted(env: Env) -> None:
    run_id = env.submit({**MANUAL, "commission_per_lot_per_side": 3.5, "fallback_spread_points": 40})
    detail = env.wait(run_id)
    assert detail["status"] == "done"
    assert detail["config"]["cost_model"]["commission_per_lot_per_side"] == 3.5
    assert detail["config"]["cost_model"]["fallback_spread_points"] == 40
    assert detail["spread_fallback"]["points"] == 40 and detail["spread_fallback"]["source"] == "user"
    direct = env.direct(detail)
    assert env.trades(run_id)["trades"] == [t.model_dump(mode="json") for t in direct.windows[0].trades]
    assert all(t["commission"] > 0 for t in env.trades(run_id)["trades"])


# ------------------------------------------------------------------------------------------ random runs
def test_random_run_same_seed_is_reproducible_through_the_api(env: Env) -> None:
    a = env.wait(env.submit(RANDOM))
    b = env.wait(env.submit(RANDOM))
    assert a["status"] == b["status"] == "done" and a["id"] != b["id"]
    assert a["seed"] == b["seed"] == 7 and a["seed_generated"] is False
    assert a["plan"] == b["plan"] and a["plan"]["algorithm"] == "numpy.PCG64" and len(a["plan"]["windows"]) == 4
    assert a["metrics"] == b["metrics"] and a["distribution"] == b["distribution"] and a["windows"] == b["windows"]
    assert env.trades(a["id"]) | {"run_id": 0} == env.trades(b["id"]) | {"run_id": 0}
    assert env.equity(a["id"]) | {"run_id": 0} == env.equity(b["id"]) | {"run_id": 0}
    # exact against a direct run: per-window metrics, distribution, aggregate
    direct = env.direct(a)
    for got, w in zip(a["windows"], direct.windows, strict=True):
        assert got["metrics"] == compute_metrics(w.trades, w.equity, w.initial_balance).to_dict()
        assert got["start"] == w.window.start.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert got["net_profit"] == pytest.approx(w.net_profit, abs=1e-9)
    assert a["distribution"] == summarize_windows(direct.windows).to_dict()
    assert a["metrics_kind"] == "random_aggregate" and a["metrics"]["window_count"] == 4
    assert a["metrics"]["total_trades"] == direct.total_trades == a["trade_count"]
    assert a["net_profit"] == a["distribution"]["mean_net_profit"]
    for i in range(4):
        per = env.trades(a["id"], window=i)
        assert per["trades"] == [t.model_dump(mode="json") for t in direct.windows[i].trades]
    # a different seed gives different windows
    c = env.wait(env.submit({**RANDOM, "seed": 8}))
    assert c["plan"]["windows"] != a["plan"]["windows"]


def test_random_run_without_seed_stores_the_generated_seed_and_reproduces(env: Env) -> None:
    body = {k: v for k, v in RANDOM.items() if k != "seed"}
    gen = env.wait(env.submit(body))
    assert gen["status"] == "done" and gen["seed_generated"] is True and gen["plan"]["seed_generated"] is True
    seed = gen["seed"]
    assert isinstance(seed, int) and 0 <= seed < 2**63 and gen["plan"]["seed"] == seed
    assert "seed" not in gen["request"]
    again = env.wait(env.submit({**body, "seed": seed}))
    assert again["plan"]["windows"] == gen["plan"]["windows"] and again["metrics"] == gen["metrics"]
    assert env.trades(again["id"])["trades"] == env.trades(gen["id"])["trades"]


# ------------------------------------------------------------------------------------------ queue / cancel
def test_cancel_running_and_queued_runs_and_the_queue_continues(env: Env, scan_gate: Gate) -> None:
    first = env.submit(MANUAL)
    assert scan_gate.entered.wait(10)
    second = env.submit(RANDOM)
    third = env.submit(RANDOM)
    assert env.detail(first)["status"] == "running"
    assert env.detail(second)["status"] == "queued" and env.detail(third)["status"] == "queued"
    # DELETE while active is refused
    response = env.client.delete(f"/backtests/{first}")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "run_active"
    assert env.client.delete(f"/backtests/{second}").status_code == 409
    # cancel the queued third run: final at once
    assert env.client.post(f"/backtests/{third}/cancel").json() == {"id": third, "status": "cancelled",
                                                                     "cancel_requested": True}
    assert env.detail(third)["status"] == "cancelled" and _ws_messages(env, third)[-1]["type"] == "cancelled"
    # cancel the running one; it stops at the next check and stores nothing
    assert env.client.post(f"/backtests/{first}/cancel").json()["status"] == "running"
    scan_gate.release()
    d1 = env.wait(first)
    assert d1["status"] == "cancelled" and d1["error"]["code"] == "cancelled" and _is_persian(d1["error"]["message_fa"])
    assert d1["windows"] == [] and d1["metrics"] is None and env.trades(first)["total"] == 0
    assert env.equity(first)["count"] == 0
    final = _ws_messages(env, first)
    assert final[-1]["type"] == "cancelled" and _is_persian(final[-1]["message_fa"])
    # the queued second run executes after the first
    d2 = env.wait(second)
    assert d2["status"] == "done" and d2["trade_count"] == env.trades(second)["total"]
    assert d2["started_at"] >= d1["finished_at"]
    assert env.client.delete(f"/backtests/{first}").status_code == 200


def test_cancel_mid_simulation_stores_no_partial_trades(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = Gate()
    original = jobs_module.run_backtest

    def slow_run(config, history, scan=None, *, bars=None, progress_cb=None, cancel_event=None):
        def cb(pct: float, index: int) -> None:
            progress_cb(pct, index)
            if pct > 20:
                gate.wait()  # the simulation is past 20 %: trades already exist in memory

        return original(config, history, scan, bars=bars, progress_cb=cb, cancel_event=cancel_event)

    monkeypatch.setattr(jobs_module, "run_backtest", slow_run)
    run_id = env.submit(MANUAL)
    assert gate.entered.wait(20)
    live = env.detail(run_id)
    assert live["status"] == "running" and live["live"]["phase"] == "simulating" and live["live"]["percent"] > 15
    assert env.client.get("/health").status_code == 200
    assert env.client.post(f"/backtests/{run_id}/cancel").json()["cancel_requested"] is True
    gate.release()
    detail = env.wait(run_id)
    assert detail["status"] == "cancelled" and detail["trade_count"] is None
    assert env.trades(run_id)["total"] == 0 and detail["windows"] == []


def test_health_stays_responsive_during_a_cpu_bound_run(env: Env) -> None:
    run_id = env.submit({**RANDOM, "windows_count": 30})
    latencies = []
    seen_running = False
    while True:
        started = time.perf_counter()
        assert env.client.get("/health").status_code == 200
        latencies.append(time.perf_counter() - started)
        status = env.client.get(f"/backtests/{run_id}").json()["status"]
        seen_running |= status == "running"
        if status in TERMINAL:
            break
    assert status == "done" and seen_running
    assert max(latencies) < 2.0, latencies


# ------------------------------------------------------------------------------------------ errors
def _error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code and _is_persian(detail["message_fa"]), detail
    return detail


def test_request_validation_errors_are_persian(env: Env) -> None:
    for body in ({"mode": "manual"}, {"symbol": GOLD, "mode": "x"}, {**RANDOM, "windows_count": 0},
                 {**RANDOM, "seed": -1}, {**RANDOM, "window_months": True}, {**RANDOM, "windows_count": 2.5},
                 {**MANUAL, "from": "yesterday"}, {**MANUAL, "extra": 1}, {**MANUAL, "fallback_spread_points": -3},
                 {**MANUAL, "commission_per_lot_per_side": 5000}, [1, 2]):
        detail = _error(env.post(body), 422, "invalid_request")
        assert detail["errors_fa"] and all(_is_persian(e) for e in detail["errors_fa"]), (body, detail)
    _error(env.post({**MANUAL, "seed": 3}), 422, "field_not_for_mode")
    _error(env.post({**RANDOM, "from": "2024-09-01T00:00:00Z"}), 422, "field_not_for_mode")
    _error(env.post({"symbol": GOLD, "mode": "manual", "from": "2024-09-01T00:00:00Z"}), 422, "missing_period")
    _error(env.post({**MANUAL, "to": MANUAL["from"]}), 422, "invalid_range")
    _error(env.post({**MANUAL, "from": "2025-02-01T00:00:00Z", "to": "2024-10-01T00:00:00Z"}), 422, "invalid_range")
    assert env.client.get("/backtests").json()["count"] == 0  # nothing was stored


def test_symbol_and_period_errors(env: Env) -> None:
    _error(env.post({**MANUAL, "symbol": "EURUSD"}), 404, "symbol_not_configured")
    _error(env.post({**MANUAL, "symbol": "../x"}), 422, "invalid_symbol")
    _error(env.post({**MANUAL, "symbol": BRENT}), 422, "no_data")  # configured, not cached
    detail = _error(env.post({**MANUAL, "from": "2024-06-10T00:00:00Z"}), 422, "window_too_early")
    assert "UTC" in detail["message_fa"]
    _error(env.post({**MANUAL, "to": "2025-06-01T00:00:00Z"}), 422, "window_beyond_data")
    _error(env.post({**MANUAL, "from": "2024-09-07T00:00:00Z", "to": "2024-09-08T00:00:00Z"}), 422, "window_empty")
    _error(env.post({**RANDOM, "window_months": 12}), 422, "data_too_short")
    naive = env.post({**MANUAL, "from": "2024-09-02T00:00:00", "to": "2025-02-24T00:00:00"})
    assert naive.status_code == 202  # no offset = UTC (like /chart)
    assert env.wait(naive.json()["id"])["from"] == MANUAL["from"]


def test_short_cache_is_data_too_short(make_settings, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    seed_cache(data_dir, repo_data_dir=tmp_path / "elsewhere", symbols=(GOLD,))  # 10 weeks only
    settings = make_settings(environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.post("/backtests", json={**RANDOM, "window_months": 3})
        assert response.status_code == 422 and response.json()["detail"]["code"] == "data_too_short"


def test_stored_params_invalid_is_409(env: Env) -> None:
    env.client.get("/strategies")
    db = env.app.state.db
    with db.transaction():
        db.execute("UPDATE strategy_versions SET params_json = ? WHERE is_active = 1", ('{"n": 3}',))
    detail = _error(env.post(MANUAL), 409, "stored_params_invalid")
    assert detail["errors_fa"]


def test_unknown_ids_are_404_and_ws_reports_them(env: Env) -> None:
    for method, path in (("get", "/backtests/999"), ("get", "/backtests/999/trades"),
                         ("get", "/backtests/999/equity"), ("get", "/backtests/999/skipped"),
                         ("post", "/backtests/999/cancel"), ("delete", "/backtests/999")):
        _error(getattr(env.client, method)(path), 404, "backtest_not_found")
    with env.client.websocket_connect("/ws/backtests/999") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "backtest_not_found" and _is_persian(msg["message_fa"])
    run_id = env.wait(env.submit(RANDOM))["id"]
    _error(env.client.get(f"/backtests/{run_id}/trades", params={"window": 4}), 422, "invalid_window")
    _error(env.client.get(f"/backtests/{run_id}/trades", params={"limit": 0}), 422, "invalid_query")
    _error(env.client.get("/backtests", params={"limit": 501}), 422, "invalid_query")
    skipped = env.client.get(f"/backtests/{run_id}/skipped").json()
    assert skipped["count"] == len(skipped["skipped"]) == sum(w["skipped_count"] for w in env.detail(run_id)["windows"])


def test_list_is_newest_first_with_limit(env: Env) -> None:
    ids = [env.submit(RANDOM) for _ in range(3)]
    for run_id in ids:
        env.wait(run_id)
    listed = env.client.get("/backtests", params={"limit": 2}).json()
    assert [r["id"] for r in listed["runs"]] == ids[::-1][:2] and listed["count"] == 2
    item = listed["runs"][0]
    assert item["mode"] == "random" and item["seed"] == 7 and item["windows_count"] == 4 and item["window_months"] == 1
    assert item["summary_basis"] == "window_mean" and item["from"] is None and item["provisional"] is True


# ------------------------------------------------------------------------------------------ provisional flag
def test_data_check_confirmed_flag_removes_the_provisional_label(make_settings, seeded: Path, tmp_path: Path) -> None:
    for env in _make_env(make_settings, seeded, tmp_path, "ALPHA_DATA_CHECK_CONFIRMED=true\n"):
        detail = env.wait(env.submit(MANUAL))
        assert detail["provisional"] is False and detail["provisional_label_fa"] is None
        assert PROVISIONAL_LABEL_FA not in detail["labels_fa"] and detail["config"]["provisional"] is False


# ------------------------------------------------------------------------------------------ lifecycle
def test_startup_marks_stale_runs_interrupted(make_settings, seeded: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(seeded, data_dir)
    conn = open_db(data_dir / DB_FILENAME)
    repo = BacktestsRepo(conn)
    common = dict(request={}, config={}, symbol=GOLD, mode="manual", period_start=None, period_end=None,
                  windows_count=None, window_months=None, seed=None, strategy_name="stddev_channel",
                  strategy_version=1, params_version=1, params_hash="a" * 64, provisional=True, labels_fa=[])
    running = repo.create_run(**common)
    assert repo.mark_running(running)
    queued = repo.create_run(**common)
    conn.close()
    for env in _make_env(make_settings, seeded, tmp_path):
        for run_id in (running, queued):
            detail = env.detail(run_id)
            assert detail["status"] == "interrupted" and detail["error"]["code"] == "interrupted"
            assert _is_persian(detail["error"]["message_fa"]) and detail["finished_at"]
            assert _ws_messages(env, run_id) == [{"type": "interrupted", "id": run_id, "status": "interrupted",
                                                  "percent": 0.0, "code": "interrupted",
                                                  "message_fa": detail["error"]["message_fa"]}]
        assert env.client.delete(f"/backtests/{running}").status_code == 200


def test_shutdown_interrupts_running_and_queued_runs(make_settings, seeded: Path, tmp_path: Path,
                                                     scan_gate: Gate) -> None:
    for env in _make_env(make_settings, seeded, tmp_path):
        first = env.submit(MANUAL)
        assert scan_gate.entered.wait(10)
        second = env.submit(RANDOM)
        threading.Timer(0.3, scan_gate.release).start()  # the worker sees the cancel after the gate
    conn = open_db(tmp_path / "data" / DB_FILENAME)
    try:
        repo = BacktestsRepo(conn)
        assert repo.status(first)["status"] == "interrupted" and repo.status(second)["status"] == "interrupted"
        assert conn.execute("SELECT COUNT(*) FROM backtest_trades").fetchone()[0] == 0
    finally:
        conn.close()


def test_without_database_backtest_routes_answer_503(make_settings, tmp_path: Path) -> None:
    client = TestClient(create_app(make_settings()), client=("127.0.0.1", 50000))  # no lifespan: no DB/jobs
    for method, path, kw in (("post", "/backtests", {"json": MANUAL}), ("get", "/backtests", {}),
                             ("get", "/backtests/1", {}), ("post", "/backtests/1/cancel", {})):
        response = getattr(client, method)(path, **kw)
        assert response.status_code == 503 and response.json()["detail"]["code"] == "db_unavailable"
    with client.websocket_connect("/ws/backtests/1") as ws:
        assert ws.receive_json()["code"] == "db_unavailable"


# ------------------------------------------------------------------------------------------ DEV_MODE logs
class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.parametrize("dev", [True, False])
def test_job_lifecycle_logs_only_under_dev_mode(make_settings, seeded: Path, tmp_path: Path, dev: bool) -> None:
    handler = _ListHandler()
    for env in _make_env(make_settings, seeded, tmp_path, f"DEV_MODE={'true' if dev else 'false'}\n"
                         "MT5_PASSWORD=S3cr3t!\nMT5_LOGIN=12345678\n"):
        logging.getLogger(ROOT_LOGGER_NAME).addHandler(handler)
        try:
            env.wait(env.submit(MANUAL))
            _ws_messages(env, 1)
        finally:
            logging.getLogger(ROOT_LOGGER_NAME).removeHandler(handler)
    text = "\n".join(handler.messages)
    assert "S3cr3t!" not in text and "12345678" not in text
    if dev:
        for needle in ("POST /backtests -> run 1", "backtest job 1 submitted", "backtest job 1 started",
                       "phase scanning", "phase simulating", "backtest run 1 stored", "backtest job 1 done in",
                       "WS /ws/backtests/1"):
            assert needle in text, needle
    else:
        assert not [m for m in handler.messages if "backtest" in m.lower()]


# ================================================================================ phase 5 wave 2 (W2-E)
# ------------------------------------------------------------------------------------------ list paging/filters
def _fake_run(repo: BacktestsRepo, symbol: str, mode: str, status: str) -> int:
    """A stored run row (no job): enough for the list endpoint, which reads the database only."""
    random_mode = mode == "random"
    run_id = repo.create_run(
        request={}, config={}, symbol=symbol, mode=mode, period_start=None, period_end=None,
        windows_count=4 if random_mode else None, window_months=1 if random_mode else None,
        seed=7 if random_mode else None, seed_generated=False if random_mode else None,
        strategy_name="stddev_channel", strategy_version=1, params_version=1, params_hash="a" * 64,
        provisional=True, labels_fa=[])
    if status == "running":
        assert repo.mark_running(run_id)
    elif status != "queued":
        assert repo.finish(run_id, status, error_code=status, error_message_fa="خطای آزمایشی")  # type: ignore[arg-type]
    return run_id


RUN_SPECS = [(GOLD, "manual", "error"), (GOLD, "random", "queued"), (BRENT, "manual", "cancelled"),
             (GOLD, "random", "error"), (BRENT, "random", "running"), (GOLD, "manual", "interrupted"),
             (BRENT, "random", "error")]


def _list(env: Env, **query: Any) -> dict:
    response = env.client.get("/backtests", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_list_offset_paging_filters_and_total(env: Env) -> None:
    repo = BacktestsRepo(env.app.state.db)
    ids = [_fake_run(repo, *spec) for spec in RUN_SPECS]
    newest = ids[::-1]

    full = _list(env)
    assert (full["count"], full["total"], full["offset"], full["limit"]) == (7, 7, 0, 50)
    assert [r["id"] for r in full["runs"]] == newest
    # pages of 3 cover everything exactly once, newest first
    pages = [_list(env, offset=o, limit=3) for o in (0, 3, 6)]
    assert [r["id"] for p in pages for r in p["runs"]] == newest
    assert [p["count"] for p in pages] == [3, 3, 1] and {(p["total"], p["limit"]) for p in pages} == {(7, 3)}
    assert [p["offset"] for p in pages] == [0, 3, 6]
    # offset beyond the total: empty page, correct total
    beyond = _list(env, offset=100, limit=5)
    assert beyond == {"count": 0, "total": 7, "offset": 100, "limit": 5, "runs": []}
    assert _list(env, offset=7)["runs"] == [] and _list(env, offset=7)["total"] == 7

    def expect(**filters: str) -> list[int]:
        keep = []
        for run_id, (symbol, mode, status) in zip(ids, RUN_SPECS, strict=True):
            row = {"symbol": symbol, "mode": mode, "status": status}
            if all(row[k] == v for k, v in filters.items()):
                keep.append(run_id)
        return keep[::-1]

    for filters in ({"symbol": GOLD}, {"symbol": BRENT}, {"mode": "random"}, {"mode": "manual"},
                    {"status": "error"}, {"status": "queued"}, {"symbol": GOLD, "mode": "random"},
                    {"symbol": BRENT, "mode": "random", "status": "error"}, {"symbol": GOLD, "status": "error"},
                    {"symbol": GOLD, "status": "cancelled"}, {"mode": "manual", "status": "running"}):
        got = _list(env, **filters)
        want = expect(**filters)
        assert [r["id"] for r in got["runs"]] == want and got["total"] == got["count"] == len(want), filters
        assert all(r[k] == v for r in got["runs"] for k, v in filters.items())
    assert expect(symbol=GOLD, status="cancelled") == [] and len(expect(symbol=GOLD)) == 4
    # filters + paging together
    paged = _list(env, symbol=GOLD, limit=2, offset=1)
    assert [r["id"] for r in paged["runs"]] == expect(symbol=GOLD)[1:3] and paged["total"] == 4
    assert _list(env, symbol=GOLD, mode="random", offset=5)["runs"] == []
    # filter values are bound parameters, never SQL
    assert _list(env, symbol="x' OR '1'='1")["total"] == 0
    assert _list(env, symbol=f"{GOLD}' --")["total"] == 0
    assert _list(env, status="done") == {"count": 0, "total": 0, "offset": 0, "limit": 50, "runs": []}
    # the item shape is unchanged and the repo stays backward compatible
    assert set(full["runs"][0]) == set(routes_module.RunSummary.model_json_schema(by_alias=True, mode="serialization")["properties"])
    assert [r["id"] for r in repo.list_runs(2)] == newest[:2]
    assert repo.count_runs() == 7 and repo.count_runs(symbol=BRENT) == 3 and repo.count_runs(status="error") == 3
    assert set(routes_module.LIST_STATUSES) == ACTIVE_STATUSES | TERMINAL_STATUSES


def test_list_query_errors_are_persian(env: Env) -> None:
    for params in ({"offset": -1}, {"offset": 10**9 + 1}, {"limit": 0}, {"limit": 501}, {"mode": "both"},
                   {"status": "finished"}, {"status": "DONE"}, {"symbol": "X" * 33}, {"symbol": ""}):
        _error(env.client.get("/backtests", params=params), 422, "invalid_query")


# ------------------------------------------------------------------------------------------ limits
@pytest.fixture(scope="module")
def seeded_both(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("bt_seed_both")
    seed_cache(out, repo_data_dir=out / "not_data", symbols=(GOLD, BRENT), start=START, end=END)
    return out


@pytest.fixture
def env_both(make_settings, seeded_both: Path, tmp_path: Path) -> Iterator[Env]:
    yield from _make_env(make_settings, seeded_both, tmp_path)


def _limits(env: Env, symbol: str) -> dict:
    response = env.client.get("/backtests/limits", params={"symbol": symbol})
    assert response.status_code == 200, response.text
    return response.json()


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


LIMIT_KEYS = {"symbol", "earliest_start", "data_start", "data_end", "warmup_h4_bars", "window_months_max",
              "windows_count_max", "seed_max", "default_fallback_spread_points", "params_version", "params_hash",
              "note_fa"}


@pytest.mark.parametrize("symbol", [GOLD, BRENT])
def test_limits_are_exactly_what_post_accepts_and_the_runner_plans(env_both: Env, symbol: str) -> None:
    env = env_both
    lim = _limits(env, symbol)
    assert set(lim) == LIMIT_KEYS and lim["symbol"] == symbol and _is_persian(lim["note_fa"])
    assert lim["warmup_h4_bars"] == 140  # 10 x atr_period 14 (default params)
    assert (lim["window_months_max"], lim["windows_count_max"], lim["seed_max"]) == (60, 500, SEED_MAX)
    history = load_history(OhlcvCache(env.data_dir), symbol)
    times = bar_open_times(history.h1)
    assert lim["data_start"] == _z(times[0].to_pydatetime())
    assert lim["data_end"] == _z((times[-1] + timedelta(hours=1)).to_pydatetime())
    earliest, end = _ts(lim["earliest_start"]), _ts(lim["data_end"])
    assert _ts(lim["data_start"]) < earliest < end
    # the boundary is exact: 1 h earlier is rejected, 1 h past the data is rejected, the limits are accepted
    manual = {"symbol": symbol, "mode": "manual"}
    detail = _error(env.post({**manual, "from": _z(earliest - timedelta(hours=1)), "to": lim["data_end"]}), 422,
                    "window_too_early")
    assert earliest.strftime("%Y-%m-%d %H:%M UTC") in detail["message_fa"]
    _error(env.post({**manual, "from": lim["earliest_start"], "to": _z(end + timedelta(hours=1))}), 422,
           "window_beyond_data")
    ok = env.post({**manual, "from": lim["earliest_start"], "to": lim["data_end"]})
    assert ok.status_code == 202, ok.text
    run = env.wait(ok.json()["id"])
    assert run["status"] == "done", run["error"]
    # the runner (scan_full_history's first valid bar) planned the very same bounds
    plan = run["plan"]
    assert (plan["earliest_start"], plan["data_end"], plan["warmup_h4_bars"]) == \
        (lim["earliest_start"], lim["data_end"], lim["warmup_h4_bars"])
    assert (lim["params_version"], lim["params_hash"]) == (run["params_version"], run["params_hash"])
    # the auto fallback spread the run resolved is the one announced
    fallback = lim["default_fallback_spread_points"]
    assert fallback == {"points": run["spread_fallback"]["points"], "source": run["spread_fallback"]["source"]}
    assert fallback["source"] in ("auto_median_observed", "none")


def test_limits_errors_use_the_standard_persian_format(env: Env) -> None:
    detail = _error(env.client.get("/backtests/limits"), 422, "invalid_query")
    assert detail["errors_fa"] == []
    _error(env.client.get("/backtests/limits", params={"symbol": "  "}), 422, "invalid_query")
    _error(env.client.get("/backtests/limits", params={"symbol": "../x"}), 422, "invalid_symbol")
    _error(env.client.get("/backtests/limits", params={"symbol": "EURUSD"}), 404, "symbol_not_configured")
    _error(env.client.get("/backtests/limits", params={"symbol": BRENT}), 422, "no_data")  # configured, not cached
    assert _limits(env, GOLD)["symbol"] == GOLD
    OhlcvCache(env.data_dir).spec_path(GOLD).unlink()
    _error(env.client.get("/backtests/limits", params={"symbol": GOLD}), 422, "spec_missing")
    _error(env.post(MANUAL), 422, "spec_missing")  # POST says the same


def test_limits_stored_params_invalid_is_409(env: Env) -> None:
    env.client.get("/strategies")
    db = env.app.state.db
    with db.transaction():
        db.execute("UPDATE strategy_versions SET params_json = ? WHERE is_active = 1", ('{"n": 3}',))
    detail = _error(env.client.get("/backtests/limits", params={"symbol": GOLD}), 409, "stored_params_invalid")
    assert detail["errors_fa"]


def test_limits_data_too_short_matches_post(make_settings, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    # 3 weeks: fewer than n = 100 closed H4 bars -> no valid channel bar at all
    seed_cache(data_dir, repo_data_dir=tmp_path / "elsewhere", symbols=(GOLD,), start="2024-06-03", end="2024-06-24")
    settings = make_settings(environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        _error(client.get("/backtests/limits", params={"symbol": GOLD}), 422, "data_too_short")
        _error(client.post("/backtests", json={**MANUAL, "from": "2024-06-10T00:00:00Z",
                                               "to": "2024-06-20T00:00:00Z"}), 422, "data_too_short")


def test_limits_without_database_is_503(make_settings) -> None:
    client = TestClient(create_app(make_settings()), client=("127.0.0.1", 50000))  # no lifespan: no DB
    response = client.get("/backtests/limits", params={"symbol": GOLD})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "db_unavailable"


# ------------------------------------------------------------------------------------------ seed at submit
def test_random_submit_without_seed_returns_the_seed_the_run_uses(env: Env, scan_gate: Gate) -> None:
    body = {k: v for k, v in RANDOM.items() if k != "seed"}
    blocker = env.submit(MANUAL)  # holds the worker so the random run is still queued when we look
    assert scan_gate.entered.wait(10)
    response = env.post(body)
    assert response.status_code == 202, response.text
    sub = response.json()
    seed = sub["seed"]
    assert sub["seed_generated"] is True and isinstance(seed, int) and 0 <= seed <= SEED_MAX
    queued = env.detail(sub["id"])
    assert queued["status"] == "queued" and queued["plan"] is None
    assert queued["seed"] == seed and queued["seed_generated"] is True and queued["config"]["seed"] == seed
    assert "seed" not in queued["request"]
    listed = _list(env, mode="random")["runs"][0]
    assert listed["id"] == sub["id"] and listed["seed"] == seed and listed["seed_generated"] is True
    scan_gate.release()
    assert env.wait(blocker)["status"] == "done"
    done = env.wait(sub["id"])
    assert done["status"] == "done"
    assert done["seed"] == done["config"]["seed"] == done["plan"]["seed"] == seed
    assert done["seed_generated"] is True and done["plan"]["seed_generated"] is True
    # rerun with that seed: identical windows, metrics and trades
    again = env.post({**body, "seed": seed})
    assert again.status_code == 202 and again.json()["seed"] == seed and again.json()["seed_generated"] is False
    rerun = env.wait(again.json()["id"])
    assert rerun["seed_generated"] is False and rerun["plan"]["seed_generated"] is False
    assert rerun["plan"]["windows"] == done["plan"]["windows"] and rerun["metrics"] == done["metrics"]
    assert rerun["distribution"] == done["distribution"] and rerun["windows"] == done["windows"]
    assert env.trades(rerun["id"])["trades"] == env.trades(done["id"])["trades"]
    # and the stored config alone regenerates it (no API, no DB)
    direct = env.direct(done)
    assert [w.window.start.strftime("%Y-%m-%dT%H:%M:%SZ") for w in direct.windows] == \
        [w["start"] for w in done["windows"]]


def test_submit_seed_is_the_one_drawn_at_submit_and_echoed_otherwise(env: Env,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    drawn = []

    def fake_seed() -> int:
        drawn.append(123456789)
        return 123456789

    monkeypatch.setattr(routes_module, "new_seed", fake_seed)
    body = {k: v for k, v in RANDOM.items() if k != "seed"}
    sub = env.post(body).json()
    assert (sub["seed"], sub["seed_generated"]) == (123456789, True) and drawn == [123456789]
    done = env.wait(sub["id"])
    assert done["plan"]["seed"] == 123456789 and done["plan"]["seed_generated"] is True  # the runner drew nothing
    given = env.post(RANDOM).json()
    assert (given["seed"], given["seed_generated"]) == (7, False) and drawn == [123456789]
    manual = env.post(MANUAL).json()
    assert manual["seed"] is None and manual["seed_generated"] is None
    for run_id in (given["id"], manual["id"]):
        env.wait(run_id)


def test_jobs_submit_rejects_seed_generated_without_a_seed(env: Env) -> None:
    detail = env.wait(env.submit(MANUAL))
    config = RunConfig.model_validate(detail["config"])
    with pytest.raises(ValueError):
        env.app.state.backtest_jobs.submit(10**6, config, seed_generated=True)


# ------------------------------------------------------------------------------------------ WS keepalive
def _assert_keepalive(msg: dict, run_id: int) -> None:
    assert set(msg) == {"type", "id", "time_utc"} and msg["type"] == "keepalive" and msg["id"] == run_id, msg
    assert msg["time_utc"].endswith("Z") and _ts(msg["time_utc"]).utcoffset() == timedelta(0)


def test_ws_keepalive_while_queued_or_running_never_after_the_end(env: Env, scan_gate: Gate,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    assert routes_module.WS_KEEPALIVE_S == 15.0  # documented default
    monkeypatch.setattr(routes_module, "WS_KEEPALIVE_S", 0.3)
    first = env.submit(MANUAL)
    assert scan_gate.entered.wait(10)
    second = env.submit(RANDOM)
    for run_id, status in ((second, "queued"), (first, "running")):
        with env.client.websocket_connect(f"/ws/backtests/{run_id}") as ws:
            progress = ws.receive_json()
            assert progress["type"] == "progress" and progress["status"] == status
            started = time.monotonic()
            for _ in range(2):
                _assert_keepalive(ws.receive_json(), run_id)
            assert time.monotonic() - started >= 0.5  # two idle periods of 0.3 s (minus scheduling jitter)
    # the run goes on: progress messages resume, then the final message, then the socket closes
    with env.client.websocket_connect(f"/ws/backtests/{first}") as ws:
        assert ws.receive_json()["type"] == "progress"
        scan_gate.release()
        messages: list[dict] = []
        while not messages or messages[-1]["type"] in ("progress", "keepalive"):
            messages.append(ws.receive_json())
        assert messages[-1]["type"] == "done"
        assert {m["type"] for m in messages} <= {"progress", "keepalive", "done"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert env.wait(second)["status"] == "done"
    # finished runs: the final message only, no keepalive, then close
    for run_id in (first, second):
        with env.client.websocket_connect(f"/ws/backtests/{run_id}") as ws:
            final = ws.receive_json()
            assert final["type"] == "done" and final["id"] == run_id
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


# ------------------------------------------------------------------------------------------ DEV_MODE logs (W2-E)
@pytest.mark.parametrize("dev", [True, False])
def test_w2_logs_only_under_dev_mode(make_settings, seeded: Path, tmp_path: Path, dev: bool, scan_gate: Gate,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_module, "WS_KEEPALIVE_S", 0.2)
    handler = _ListHandler()
    for env in _make_env(make_settings, seeded, tmp_path, f"DEV_MODE={'true' if dev else 'false'}\n"
                         "MT5_PASSWORD=S3cr3t!\nMT5_LOGIN=12345678\n"):
        logging.getLogger(ROOT_LOGGER_NAME).addHandler(handler)
        try:
            _list(env, symbol=GOLD, mode="random", offset=0, limit=10)
            _limits(env, GOLD)
            run_id = env.submit({k: v for k, v in RANDOM.items() if k != "seed"})
            assert scan_gate.entered.wait(10)
            with env.client.websocket_connect(f"/ws/backtests/{run_id}") as ws:
                assert ws.receive_json()["type"] == "progress"
                _assert_keepalive(ws.receive_json(), run_id)
            scan_gate.release()
            env.wait(run_id)
        finally:
            logging.getLogger(ROOT_LOGGER_NAME).removeHandler(handler)
    text = "\n".join(handler.messages)
    assert "S3cr3t!" not in text and "12345678" not in text
    if dev:
        for needle in (f"GET /backtests symbol={GOLD} mode=random status=None offset=0 limit=10 -> 0 item(s), total 0",
                       f"backtest period limits {GOLD}", "earliest=", "generated seed=", "at submit",
                       f"WS /ws/backtests/{run_id}: keepalive #1"):
            assert needle in text, needle
    else:
        assert not [m for m in handler.messages if "backtest" in m.lower() or "keepalive" in m.lower()]
