"""MarketDataService (offset, incremental cache update) and the /symbols, /rates, /rates/meta routes."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from alpha_engine.app import create_app
from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.schema import Timeframe
from alpha_engine.data.timezone import OffsetError, OffsetModel
from alpha_engine.market_data import MarketDataService
from alpha_engine.mt5_adapter import Mt5Adapter
from fixtures.fake_mt5 import FakeMt5
from fixtures.seed_cache import seed_cache

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class Clock:
    def __init__(self, when: datetime) -> None:
        self.when = when

    def __call__(self) -> datetime:
        return self.when


@pytest.fixture
def env(make_settings, synthetic, tmp_path):
    def _build(connected: bool = True, when: datetime = datetime(2025, 5, 5, 0, 30, tzinfo=timezone.utc),
               extra_env: str = ""):
        settings = make_settings(extra_env, environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "data")})
        clock = Clock(when)
        # offline == the terminal is not reachable: initialize() fails (also for on-demand reconnects)
        fake = FakeMt5(synthetic.values(), initialize_results=[connected])
        adapter = Mt5Adapter(settings, fake, sleep=lambda s: None, clock=clock)
        if connected:
            adapter.connect()
        service = MarketDataService.create(settings, adapter, clock=clock, update_min_interval=0.0)
        app = create_app(settings, mt5_adapter=adapter, market_data=service)
        return {"settings": settings, "clock": clock, "fake": fake, "adapter": adapter, "service": service,
                "app": app, "client": TestClient(app, client=("127.0.0.1", 50000))}

    return _build


# --- service ---------------------------------------------------------------------------------------

def test_offset_fit_stored_and_override(env) -> None:
    e = env()
    fit = e["service"].refit_offset()
    assert fit.effective_label == "us_dst(+2)" and fit.source == "fit"
    assert e["service"].cache.read_offset_fit() == fit
    assert e["service"].effective_model() == OffsetModel.us_dst(2)
    o = env(extra_env="MT5_SERVER_UTC_OFFSET=3\n")
    assert o["service"].effective_model() == OffsetModel.fixed(3)  # no MT5 needed for an override


def test_effective_model_unknown_offline(env) -> None:
    with pytest.raises(OffsetError):
        env(connected=False)["service"].effective_model()


def test_incremental_update_equals_full_history(env, synthetic) -> None:
    e = env(when=datetime(2025, 3, 20, 12, 0, tzinfo=timezone.utc))
    svc = e["service"]
    start = datetime(2025, 2, 24, tzinfo=timezone.utc)
    first = svc.update("XAUUSD.x", "H1", start=start)
    assert first.full and first.meta.last_bar_utc == "2025-03-20T11:00:00Z"
    assert first.meta.first_available_utc == "2025-02-24T00:00:00Z" and first.meta.source == "mt5"
    e["clock"].when = datetime(2025, 5, 5, 0, 30, tzinfo=timezone.utc)
    calls_before = len(e["fake"].rate_calls())
    second = svc.update("XAUUSD.x", "H1", start=start)
    assert not second.full and second.rows_added > 0
    # incremental request starts 2 bars before the last cached bar (minus the 1-day server padding)
    from_server = e["fake"].rate_calls()[calls_before][2]
    last = pd.Timestamp("2025-03-20T11:00Z") - pd.Timedelta(hours=2)
    assert from_server == int(last.timestamp()) + 3 * 3600 - 86400
    frame, meta = svc.cache.read("XAUUSD.x", "H1")
    pd.testing.assert_frame_equal(frame, synthetic[("XAUUSD.x", Timeframe.H1)].utc)
    assert meta.first_available_utc == "2025-02-24T00:00:00Z"


def test_model_change_and_seed_source_force_full_refetch(env, tmp_path) -> None:
    e = env()
    seed_cache(tmp_path / "data", repo_data_dir=tmp_path / "elsewhere")  # seed data in the engine's data dir
    result = e["service"].update("BRNUSD.x", "H4", start=datetime(2025, 2, 24, tzinfo=timezone.utc))
    assert result.full and result.meta.source == "mt5"
    o = env(extra_env="MT5_SERVER_UTC_OFFSET=3\n")
    again = o["service"].update("BRNUSD.x", "H4", start=datetime(2025, 2, 24, tzinfo=timezone.utc))
    assert again.full and again.meta.offset_model == "fixed(+3)"


def test_history_shorter_than_target_is_recorded(env) -> None:
    e = env()
    e["service"].refit_offset()
    result = e["service"].update("XAUUSD.x", "H4", years=5)
    assert result.history_short and result.meta.extra["history_short"] is True
    assert result.meta.requested_start_utc.startswith("2020-05-")
    assert result.meta.first_available_utc == "2025-02-24T02:00:00Z"


# --- routes ----------------------------------------------------------------------------------------

def test_symbols_live_then_cached(env) -> None:
    e = env()
    body = e["client"].get("/symbols").json()
    assert body["mt5_state"] == "connected"
    assert [(s["symbol"], s["source"]) for s in body["symbols"]] == [("XAUUSD.x", "mt5"), ("BRNUSD.x", "mt5")]
    spec = body["symbols"][0]["spec"]
    assert set(spec) == {"name", "digits", "point", "trade_contract_size", "trade_tick_value", "trade_tick_size",
                         "volume_min", "volume_step", "volume_max", "currency_profit", "currency_base",
                         "description"}
    e["adapter"].shutdown()
    offline = e["client"].get("/symbols").json()
    assert offline["mt5_state"] == "disconnected"
    assert [s["source"] for s in offline["symbols"]] == ["cache", "cache"]


def test_symbols_offline_without_cache(env) -> None:
    body = env(connected=False)["client"].get("/symbols").json()
    assert [(s["source"], s["spec"]) for s in body["symbols"]] == [("none", None), ("none", None)]


def test_rates_fetches_then_serves_cache(env, synthetic) -> None:
    e = env()
    r = e["client"].get("/rates", params={"symbol": "XAUUSD.x", "timeframe": "H1",
                                          "from": "2025-03-10T00:00:00Z", "to": "2025-03-15T00:00:00Z"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"symbol", "timeframe", "from", "to", "source", "stale", "offset_model", "count", "bars",
                         "gaps", "gap_counts", "message"}
    assert body["source"] == "mt5" and body["stale"] is False and body["offset_model"] == "us_dst(+2)"
    assert body["from"] == "2025-03-10T00:00:00Z" and body["to"] == "2025-03-15T00:00:00Z"
    assert all(ISO_Z.match(b["time"]) for b in body["bars"])
    expected = synthetic[("XAUUSD.x", Timeframe.H1)].utc
    window = expected[(expected["time"] >= "2025-03-10") & (expected["time"] <= "2025-03-15")]
    assert body["count"] == len(window) == len(body["bars"])
    assert body["bars"][0] == {"time": "2025-03-10T00:00:00Z", **{k: window.iloc[0][k] for k in
                               ("open", "high", "low", "close", "tick_volume", "spread", "real_volume")}}
    assert body["gap_counts"]["session_break"] == 4 and body["gap_counts"]["weekend"] == 1
    assert {g["kind"] for g in body["gaps"]} <= {"weekend", "holiday", "session_break", "missing"}
    # second request inside the cached range: served from cache, no MT5 call
    n = len(e["fake"].rate_calls())
    again = e["client"].get("/rates", params={"symbol": "XAUUSD.x", "timeframe": "H1",
                                              "from": "2025-03-11T00:00:00Z", "to": "2025-03-12T00:00:00Z"}).json()
    assert again["source"] == "cache" and again["stale"] is False and len(e["fake"].rate_calls()) == n


def test_rates_offline_marks_stale(env, tmp_path) -> None:
    e = env(connected=False, when=datetime(2025, 6, 1, 12, tzinfo=timezone.utc))
    seed_cache(tmp_path / "data", repo_data_dir=tmp_path / "elsewhere")
    body = e["client"].get("/rates", params={"symbol": "BRNUSD.x", "timeframe": "h4"}).json()
    assert body["source"] == "cache" and body["stale"] is True and "not connected" in body["message"]
    assert "copy_rates_range" not in e["fake"].names() and e["fake"].names().count("initialize") == 1
    assert body["from"] == "2025-05-02T12:00:00Z" and body["to"] == "2025-06-01T12:00:00Z"  # default 30 days
    assert body["count"] == len(body["bars"]) > 0


def test_rates_offline_no_cache(env) -> None:
    body = env(connected=False)["client"].get("/rates", params={"symbol": "XAUUSD.x", "timeframe": "H1"}).json()
    assert body["count"] == 0 and body["stale"] is True


@pytest.mark.parametrize(("params", "status"), [
    ({"symbol": "EURUSD", "timeframe": "H1"}, 404),
    ({"symbol": "XAUUSD.x", "timeframe": "M5"}, 422),
    ({"symbol": "../../etc", "timeframe": "H1"}, 422),
    ({"symbol": "XAUUSD.x", "timeframe": "H1", "from": "not-a-date"}, 422),
    ({"symbol": "XAUUSD.x", "timeframe": "H1", "from": "2025-03-02T00:00:00Z", "to": "2025-03-01T00:00:00Z"}, 422),
    ({"timeframe": "H1"}, 422),
])
def test_rates_validation(env, params, status) -> None:
    r = env(connected=False)["client"].get("/rates", params=params)
    assert r.status_code == status
    assert r.json()["detail"]


def test_rates_meta(env) -> None:
    e = env()
    empty = e["client"].get("/rates/meta", params={"symbol": "XAUUSD.x", "timeframe": "H4"}).json()
    assert empty["cached"] is False and empty["rows"] == 0
    e["service"].update("XAUUSD.x", "H4", start=datetime(2025, 2, 24, tzinfo=timezone.utc))
    meta = e["client"].get("/rates/meta", params={"symbol": "XAUUSD.x", "timeframe": "H4"}).json()
    assert meta["cached"] and meta["rows"] > 0 and meta["offset_model"] == "us_dst(+2)" and meta["source"] == "mt5"
    assert ISO_Z.match(meta["first_bar_utc"]) and ISO_Z.match(meta["last_bar_utc"])
    assert e["client"].get("/rates/meta", params={"symbol": "EURUSD", "timeframe": "H4"}).status_code == 404


def test_health_uses_adapter_status_and_lifespan_shuts_down(env) -> None:
    e = env()
    with TestClient(e["app"], client=("127.0.0.1", 50000)) as client:
        mt5 = client.get("/health").json()["mt5"]
        assert mt5 == {"state": "connected", "server": "Fake-Server", "login_masked": "****5678",
                       "trade_mode": "demo", "message": "attached to the running terminal"}
    assert e["adapter"].status().state == "disconnected"
    assert e["fake"].names()[-1] == "shutdown"


def test_autoconnect_in_background_on_startup(make_settings, synthetic, tmp_path) -> None:
    settings = make_settings("ENGINE_MT5_AUTOCONNECT=true\n", environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path)})
    fake = FakeMt5(synthetic.values())
    adapter = Mt5Adapter(settings, fake, sleep=lambda s: None)
    app = create_app(settings, mt5_adapter=adapter)
    with TestClient(app, client=("127.0.0.1", 50000)):
        adapter._connect_thread.join(5)
        assert adapter.connected
    assert fake.names()[0] == "initialize" and fake.names()[-1] == "shutdown"


def test_default_app_never_connects_in_tests(make_settings) -> None:
    app = create_app(make_settings())
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/health").json()["mt5"]["state"] == "not_initialized"
    assert app.state.mt5_adapter._connect_thread is None


def test_cache_dir_is_isolated(env, tmp_path) -> None:
    e = env()
    e["service"].update("XAUUSD.x", "H1", start=datetime(2025, 4, 1, tzinfo=timezone.utc))
    assert OhlcvCache(tmp_path / "data").read_meta("XAUUSD.x", "H1") is not None
