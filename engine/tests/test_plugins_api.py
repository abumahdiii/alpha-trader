"""``/plugins`` API, plugin store (DB v3 + files) and lifespan registration, end to end on a seeded tmp cache.

upload (static + sandboxed dynamic validation) -> listed in ``/plugins`` and ``/strategies`` -> usable in
``/chart/setups?strategy=`` and ``POST /backtests`` with plugin provenance (source file SHA-256); strict versions,
disable / enable, archive (blocked while a run is active), restart registration and tampered files.
Never MT5, never the real ``data/``. Each upload runs the full dynamic validation (about 5 s).
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alpha_engine.app import create_app
from alpha_engine.logging_setup import ROOT_LOGGER_NAME
from alpha_engine.plugins.store import PluginStore
from alpha_engine.plugins.template import TEMPLATE_FILENAME, TEMPLATE_SOURCE
from alpha_engine.plugins.validator import source_sha256
from alpha_engine.storage.backtests_repo import BacktestsRepo
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy
from alpha_engine.strategy.registry import StrategyRegistry
from fixtures.channel_scenarios import SYMBOL
from fixtures.seed_cache import seed_cache

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the plugin sandbox targets Windows")

GOLD = SYMBOL
START, END = "2024-06-03", "2025-03-03"
MANUAL = {"symbol": GOLD, "mode": "manual", "from": "2024-09-02T00:00:00Z", "to": "2025-02-24T00:00:00Z"}
SPAN = {"symbol": GOLD, "from": "2024-09-01T00:00:00Z", "to": "2025-03-01T00:00:00Z"}
TERMINAL = {"done", "error", "cancelled", "interrupted"}
NAME = "ma_cross_demo"
SHA_V1 = source_sha256(TEMPLATE_SOURCE)
V2_SOURCE = TEMPLATE_SOURCE.replace("    version = 1\n", "    version = 2\n", 1).replace(
    'label_fa="دوره میانگین سریع"', 'label_fa="دوره میانگین سریع (نسخه ۲)"', 1)
SHA_V2 = source_sha256(V2_SOURCE)
# Same name + version 1 as the template, different content -> 409.
CONFLICT_SOURCE = TEMPLATE_SOURCE.replace('title_fa = "کراس دو میانگین متحرک (نمونه)"',
                                          'title_fa = "کراس دو میانگین متحرک (تغییر یافته)"', 1)
SECRET_MARKER = "# UNIQUE-SOURCE-MARKER-7f3a"


def _is_persian(text: str | None) -> bool:
    return bool(text) and any("؀" <= ch <= "ۿ" for ch in text)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("plugin_seed")
    seed_cache(out, repo_data_dir=out / "not_data", symbols=(GOLD,), start=START, end=END)
    return out


def _registry() -> StrategyRegistry:
    reg = StrategyRegistry()
    reg.register(StdDevChannelStrategy)
    return reg


def _open(make_settings, data_dir: Path, registry: StrategyRegistry | None = None, env_text: str = "") -> Iterator[TestClient]:
    settings = make_settings(env_text, environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    app = create_app(settings, strategy_registry=registry if registry is not None else _registry())
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        c.app_data_dir = data_dir  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def data_dir(seeded: Path, tmp_path: Path) -> Path:
    out = tmp_path / "data"
    shutil.copytree(seeded, out)
    return out


@pytest.fixture
def client(make_settings, data_dir: Path) -> Iterator[TestClient]:
    yield from _open(make_settings, data_dir)


def _error(response: Any, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code and _is_persian(detail["message_fa"]), detail
    return detail


def _wait(client: TestClient, run_id: int, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/backtests/{run_id}").json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish")


def _upload(client: TestClient, source: str = TEMPLATE_SOURCE, filename: str = TEMPLATE_FILENAME) -> Any:
    return client.post("/plugins", json={"filename": filename, "source": source})


def _registered(client: TestClient) -> Any:
    return client.app.state.strategy_registry.get(NAME)


# ------------------------------------------------------------------------------------------------ template
def test_template_endpoint(client: TestClient) -> None:
    body = client.get("/plugins/template").json()
    assert body == {"filename": TEMPLATE_FILENAME, "content": TEMPLATE_SOURCE}
    assert client.get("/plugins").json() == []


# ------------------------------------------------------------------------------------ upload -> chart -> backtest
def test_upload_then_chart_and_backtest_with_plugin_provenance(client: TestClient) -> None:
    response = _upload(client)
    assert response.status_code == 201, response.text
    item = response.json()
    assert (item["name"], item["version"], item["sha256"], item["status"], item["registered"]) == \
        (NAME, 1, SHA_V1, "active", True)
    assert item["filename"] == TEMPLATE_FILENAME and _is_persian(item["title_fa"])
    assert [p["name"] for p in item["param_schema"]][:2] == ["fast", "slow"]
    dyn = item["validation"]["dynamic"]
    assert dyn["determinism"] is True and dyn["future_mutation"] is True and dyn["candidates"] > 0
    assert item["validation"]["static"][-1] == "strategy_class"

    # stored on disk under data/strategies/plugins/<name>/<version>-<sha12>.py, byte-exact
    stored = client.app_data_dir / "strategies" / "plugins" / NAME / f"1-{SHA_V1[:12]}.py"  # type: ignore[attr-defined]
    assert stored.read_bytes() == TEMPLATE_SOURCE.encode("utf-8")
    assert stored.with_suffix(".json").exists()

    assert [p["name"] for p in client.get("/plugins").json()] == [NAME]
    strategies = {s["name"]: s for s in client.get("/strategies").json()}
    assert set(strategies) == {"stddev_channel", NAME} and strategies[NAME]["version"] == 1
    assert [s["name"] for s in strategies[NAME]["param_schema"]] == [p["name"] for p in item["param_schema"]]

    chart = client.get("/chart/setups", params={**SPAN, "strategy": NAME})
    assert chart.status_code == 200, chart.text
    body = chart.json()
    assert (body["strategy"], body["strategy_version"], body["strategy_source"], body["strategy_sha256"]) == \
        (NAME, 1, "plugin", SHA_V1)
    assert body["count"] > 0
    for setup in body["setups"]:
        assert setup["setup_type"] == "ma_cross" and setup["line"] is None and setup["id"].endswith(f":{NAME}")
    _error(client.get("/chart/channel", params={"symbol": GOLD, "strategy": NAME}), 409, "channel_not_available")

    limits = client.get("/backtests/limits", params={"symbol": GOLD, "strategy": NAME})
    assert limits.status_code == 200, limits.text
    assert limits.json()["warmup_h4_bars"] == 0 and len(limits.json()["params_hash"]) == 64

    run = client.post("/backtests", json={**MANUAL, "strategy": NAME, "strategy_version": 1})
    assert run.status_code == 202, run.text
    detail = _wait(client, run.json()["id"])
    assert detail["status"] == "done", detail.get("error")
    assert (detail["strategy"], detail["strategy_version"], detail["strategy_source"], detail["strategy_sha256"]) == \
        (NAME, 1, "plugin", SHA_V1)
    assert detail["config"]["strategy_sha256"] == SHA_V1 and detail["config"]["strategy_source"] == "plugin"
    trades = client.get(f"/backtests/{detail['id']}/trades").json()["trades"]
    assert {t["setup_type"] for t in trades} <= {"ma_cross"}

    # the built-in default is untouched
    default = client.get("/chart/setups", params=SPAN).json()
    assert default["strategy"] == "stddev_channel" and default["strategy_source"] == "builtin"


# -------------------------------------------------------------------------------------------- versioning
def test_strict_versioning_disable_enable_and_archive(client: TestClient) -> None:
    assert _upload(client).status_code == 201
    # same file again: idempotent 200, nothing new stored
    again = _upload(client, filename="renamed.py")
    assert again.status_code == 200 and again.json()["sha256"] == SHA_V1 and again.json()["filename"] == TEMPLATE_FILENAME
    # same name + version, different content: 409 (bump the version)
    detail = _error(_upload(client, CONFLICT_SOURCE), 409, "version_conflict")
    assert "version" in detail["message_fa"]
    # v2 -> both kept, the latest active version is registered
    v2 = _upload(client, V2_SOURCE)
    assert v2.status_code == 201, v2.text
    listed = [(p["version"], p["status"], p["registered"]) for p in client.get("/plugins").json()]
    assert listed == [(2, "active", True), (1, "active", False)]
    assert (_registered(client).version, _registered(client).code_sha256) == (2, SHA_V2)
    assert {s["name"]: s["version"] for s in client.get("/strategies").json()}[NAME] == 2

    # disable v2 -> v1 runs again; enable v2 -> back to v2
    off = client.post(f"/plugins/{NAME}/2/disable")
    assert off.status_code == 200 and off.json()["status"] == "disabled" and not off.json()["registered"]
    assert _registered(client).code_sha256 == SHA_V1
    on = client.post(f"/plugins/{NAME}/2/enable")
    assert on.status_code == 200 and on.json()["status"] == "active" and on.json()["registered"]
    assert _registered(client).code_sha256 == SHA_V2

    # archive is blocked while a queued/running backtest uses that version
    db = client.app.state.db
    run_id = BacktestsRepo(db).create_run(
        request={}, config={}, symbol=GOLD, mode="manual", period_start=None, period_end=None, windows_count=None,
        window_months=None, seed=None, strategy_name=NAME, strategy_version=2, params_version=1,
        params_hash="a" * 64, provisional=True, labels_fa=[])
    _error(client.delete(f"/plugins/{NAME}/2"), 409, "plugin_in_use")
    BacktestsRepo(db).finish(run_id, "cancelled")
    archived = client.delete(f"/plugins/{NAME}/2")
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    root = client.app_data_dir / "strategies" / "plugins" / NAME  # type: ignore[attr-defined]
    assert (root / "archive" / f"2-{SHA_V2[:12]}.py").read_bytes() == V2_SOURCE.encode("utf-8")
    assert not (root / f"2-{SHA_V2[:12]}.py").exists()
    assert _registered(client).code_sha256 == SHA_V1  # the older active version takes over
    _error(client.post(f"/plugins/{NAME}/2/enable"), 409, "plugin_archived")
    # re-uploading the archived file restores it (same sha -> 200)
    restored = _upload(client, V2_SOURCE)
    assert restored.status_code == 200 and restored.json()["status"] == "active" and restored.json()["registered"]
    assert (root / f"2-{SHA_V2[:12]}.py").exists()

    # archive everything -> the name disappears from the registry and /strategies
    for version in (2, 1):
        assert client.delete(f"/plugins/{NAME}/{version}").status_code == 200
    assert NAME not in client.app.state.strategy_registry
    assert NAME not in {s["name"] for s in client.get("/strategies").json()}
    _error(client.get("/chart/setups", params={**SPAN, "strategy": NAME}), 404, "strategy_not_found")
    _error(client.delete(f"/plugins/{NAME}/9"), 404, "plugin_not_found")
    _error(client.post(f"/plugins/{NAME}/9/disable"), 404, "plugin_not_found")


def test_invalid_uploads_are_rejected_with_persian_errors(client: TestClient) -> None:
    reserved = TEMPLATE_SOURCE.replace(f'name = "{NAME}"', 'name = "stddev_channel"', 1)
    detail = _error(_upload(client, reserved), 422, "invalid_plugin")
    assert any("رزرو" in e for e in detail["errors_fa"])
    mt5 = "import " + "Meta" + "Trader5\n" + TEMPLATE_SOURCE
    detail = _error(_upload(client, mt5), 422, "invalid_plugin")
    assert any(e.startswith("خط 1:") for e in detail["errors_fa"])
    look_ahead = TEMPLATE_SOURCE.replace('close = h1["close"].astype("float64")',
                                         'close = h1["close"].astype("float64").shift(-1).ffill()', 1)
    detail = _error(_upload(client, look_ahead), 422, "invalid_plugin")
    assert any("scan با evaluate" in e for e in detail["errors_fa"])
    for body in ({"source": TEMPLATE_SOURCE}, {"filename": "x.txt", "source": TEMPLATE_SOURCE},
                 {"filename": "x.py", "source": ""}, {"filename": "x.py", "source": TEMPLATE_SOURCE, "extra": 1},
                 ["not", "an", "object"]):
        _error(client.post("/plugins", json=body), 422, "invalid_body")
    assert client.get("/plugins").json() == []  # nothing was stored
    assert not (client.app_data_dir / "strategies" / "plugins" / NAME).exists()  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------------ restart + tampering
def test_restart_registers_active_plugins_and_skips_tampered_files(make_settings, data_dir: Path) -> None:
    for c in _open(make_settings, data_dir):
        assert _upload(c).status_code == 201
        assert _upload(c, V2_SOURCE).status_code == 201
        assert c.post(f"/plugins/{NAME}/2/disable").status_code == 200
    # a fresh app (and a fresh registry) registers the latest ACTIVE version (v1) at startup
    registry = _registry()
    for c in _open(make_settings, data_dir, registry):
        assert registry.get(NAME).version == 1 and registry.get(NAME).code_sha256 == SHA_V1
        assert c.app.state.plugin_names == {NAME}
    assert NAME not in registry  # unregistered on shutdown

    # tamper with v1 on disk: startup logs and skips it (never crashes), enabling reports the bad file
    path = data_dir / "strategies" / "plugins" / NAME / f"1-{SHA_V1[:12]}.py"
    path.write_bytes(path.read_bytes() + b"\n# edited outside the app\n")
    registry = _registry()
    for c in _open(make_settings, data_dir, registry):
        assert c.get("/health").status_code == 200
        assert NAME not in registry
        _error(c.post(f"/plugins/{NAME}/1/enable"), 409, "plugin_file_invalid")
        # v2 is still fine: enabling it registers v2
        assert c.post(f"/plugins/{NAME}/2/enable").json()["registered"] is True
        assert registry.get(NAME).code_sha256 == SHA_V2
        # the rows themselves are untouched by the tampering (both versions still listed)
        assert [(r.version, r.sha256) for r in PluginStore(c.app.state.db, data_dir).list()] ==             [(2, SHA_V2), (1, SHA_V1)]


# ------------------------------------------------------------------------------------------------ logging
class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.parametrize("dev", [True, False])
def test_dev_mode_logs_upload_steps_without_source_or_secrets(make_settings, data_dir: Path, dev: bool) -> None:
    handler = _ListHandler()
    source = TEMPLATE_SOURCE + "\n" + SECRET_MARKER + "\n"
    for c in _open(make_settings, data_dir, env_text=f"DEV_MODE={'true' if dev else 'false'}\n"
                   "MT5_PASSWORD=S3cr3t!\nMT5_LOGIN=12345678\n"):
        logging.getLogger(ROOT_LOGGER_NAME).addHandler(handler)
        try:
            assert _upload(c, source).status_code == 201
        finally:
            logging.getLogger(ROOT_LOGGER_NAME).removeHandler(handler)
    text = "\n".join(handler.messages)
    assert SECRET_MARKER not in text and "S3cr3t!" not in text and "12345678" not in text
    assert "class MovingAverageCross" not in text
    if dev:
        for needle in ("POST /plugins: file=", "plugin static validation", "plugin worker", "spawned", "ready in",
                       "plugin validation", "prefix checks", "plugin stored", "registered"):
            assert needle in text, needle
    else:
        assert "plugin worker" not in text
