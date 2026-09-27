"""``/chart/channel`` and ``/chart/setups`` (phase 3) on the synthetic fixture cache -- no MT5.

The synthetic dataset (tests/fixtures/synthetic_ohlcv.py, seeded through fixtures/seed_cache.py) has
10 weeks of XAUUSD.x / BRNUSD.x H1+H4 with weekends, daily breaks, holidays and planted holes; with the
default params (n = 100 H4 bars) the channel becomes valid after ~4 weeks and ``scan`` finds 22 gold
candidates. Every expectation below is computed independently from the SAME cached frames with the
strategy code (indicator_frame / compute_channel_bars / scan / evaluate / levels / sizing) and compared
EXACTLY (JSON floats round-trip bit for bit).
"""

from __future__ import annotations

import io
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpha_engine import logging_setup
from alpha_engine.app import create_app
from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.schema import Timeframe, iso_z
from alpha_engine.indicators.atr import wilder_atr
from alpha_engine.indicators.regression_channel import rolling_regression_channel
from alpha_engine.market_data import MarketDataService
from alpha_engine.mt5_adapter import Mt5Adapter
from alpha_engine.risk.sizing import size_from_spec
from alpha_engine.routes.chart import SPEC_MISSING_FA, ChartCache, _direction
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import SCHEMA, StdDevChannelStrategy, resolve_params
from alpha_engine.strategies.stddev_channel.levels import entry_price_for, resolve_trade_levels
from alpha_engine.strategies.stddev_channel.setups import compute_channel_bars
from alpha_engine.strategy.base import H4_DURATION, StrategyContext, slice_closed_bars
from alpha_engine.strategy.params import params_hash
from fixtures.fake_mt5 import FakeMt5
from fixtures.seed_cache import seed_cache

GOLD, BRENT = "XAUUSD.x", "BRNUSD.x"
STRATEGY = StdDevChannelStrategy()
H1 = timedelta(hours=1)
FAR = {"from": "2000-01-01T00:00:00Z", "to": "2100-01-01T00:00:00Z"}  # the whole cache
CHANNEL_FIELDS = ("mid", "upper", "lower", "slope", "sigma", "atr_h4")
# Explicit account for the hand-verified setup arithmetic (independent of the AccountSettings defaults).
WORKED_ACCOUNT = {"balance": 1000.0, "risk_pct": 1.0, "leverage": 100, "rr": 2.0}


def _is_persian(text: str | None) -> bool:
    return bool(text) and any("؀" <= ch <= "ۿ" for ch in text)


@dataclass
class Env:
    app: FastAPI
    client: TestClient
    fake: FakeMt5
    cache: OhlcvCache
    data_dir: Path

    def frames(self, symbol: str = GOLD) -> tuple[pd.DataFrame, pd.DataFrame]:
        h1 = self.cache.read(symbol, Timeframe.H1)
        h4 = self.cache.read(symbol, Timeframe.H4)
        assert h1 is not None and h4 is not None
        return h1[0], h4[0]

    def rewrite(self, symbol: str, tf: Timeframe, frame: pd.DataFrame) -> None:
        cached = self.cache.read(symbol, tf)
        assert cached is not None
        self.cache.write(symbol, tf, frame.reset_index(drop=True), cached[1])

    def params(self) -> dict[str, Any]:
        return self.client.get("/strategies/stddev_channel").json()["params"]

    def account(self) -> AccountSettings:
        return AccountSettings.model_validate(self.client.get("/settings").json())

    def use_account(self, values: dict[str, Any]) -> None:
        response = self.client.put("/settings", json=values)
        assert response.status_code == 200 and response.json() == values, response.text

    def channel(self, symbol: str = GOLD, **query: Any) -> dict:
        response = self.client.get("/chart/channel", params={"symbol": symbol, **query})
        assert response.status_code == 200, response.text
        return response.json()

    def setups(self, symbol: str = GOLD, **query: Any) -> dict:
        response = self.client.get("/chart/setups", params={"symbol": symbol, **query})
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def env(make_settings, synthetic, tmp_path: Path) -> Iterator[Env]:
    data_dir = tmp_path / "data"
    settings = make_settings(environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    seed_cache(data_dir, repo_data_dir=tmp_path / "elsewhere")
    fake = FakeMt5(synthetic.values(), initialize_results=[False])  # never connected
    adapter = Mt5Adapter(settings, fake, sleep=lambda s: None)
    service = MarketDataService.create(settings, adapter)
    app = create_app(settings, mt5_adapter=adapter, market_data=service)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield Env(app=app, client=client, fake=fake, cache=service.cache, data_dir=data_dir)


# ------------------------------------------------------------------------------------------ expectations
def expected_items(env: Env, symbol: str = GOLD) -> list[dict]:
    """Direct full-history scan + entry/level resolution + sizing, independent of the route."""
    h1, h4 = env.frames(symbol)
    account = env.account()
    spec = env.cache.read_spec(symbol)
    out = []
    for cand in STRATEGY.scan(h1, h4, env.params(), account, symbol=symbol):
        t = int(np.flatnonzero(h1["time"] == pd.Timestamp(cand.confirmation_bar_open_utc))[0])
        entry = entry_price_for(h1, t)
        item: dict[str, Any] = {"candidate": cand, "t": t, "entry": entry, "status": None, "tp": None,
                                "volume": None, "reason": None}
        if entry is None:
            item["status"] = "pending_entry"
        else:
            try:
                levels = resolve_trade_levels(cand, entry)
            except ValueError:
                item["status"] = "rejected"
            else:
                item["tp"] = levels.take_profit
                if spec is None:
                    item["status"] = "accepted"
                else:
                    sizing = size_from_spec(cand, account, spec[0], entry=entry)
                    item["status"] = "accepted" if sizing.accepted else "rejected"
                    item["reason"] = sizing.reason_fa
                    item["volume"] = sizing.volume if sizing.accepted else None
                    item["margin"] = sizing.margin if sizing.accepted else None
        out.append(item)
    return out


def assert_item_matches(got: dict, exp: dict, h1: pd.DataFrame) -> None:
    cand = exp["candidate"]
    assert got["confirmation_bar_time"] == iso_z(pd.Timestamp(cand.confirmation_bar_open_utc))
    assert got["decision_time"] == iso_z(pd.Timestamp(cand.decision_time_utc))
    assert pd.Timestamp(got["decision_time"]) - pd.Timestamp(got["confirmation_bar_time"]) == H1
    assert (got["setup_type"], got["direction"], got["pattern"], got["line"]) == (
        cand.setup.value, cand.direction, cand.pattern, cand.line)
    assert got["stop_loss"] == cand.stop_loss and got["reference_price"] == cand.reference_price
    assert got["indicative_take_profit"] == cand.indicative_take_profit and got["rr"] == cand.rr
    assert got["reason_fa"] == cand.reason_fa and got["indicators"] == cand.extra
    assert got["line_value"] == cand.extra["line_value"]
    assert got["status"] == exp["status"]
    assert got["entry"] == exp["entry"]
    if exp["entry"] is not None:
        assert got["entry_time"] == iso_z(h1["time"].iloc[exp["t"] + 1])
        assert got["entry"] == h1["open"].iloc[exp["t"] + 1]
    assert got["take_profit"] == exp["tp"]
    assert got["volume"] == exp["volume"]
    if exp["status"] == "rejected" and exp["reason"]:
        assert got["rejection_reason_fa"] == exp["reason"]


def assert_same_candidate(a, b) -> None:
    da, db = a.model_dump(), b.model_dump()
    assert da == db, (da, db)


# ------------------------------------------------------------------------------------------ /chart/channel H1
def test_h1_channel_equals_indicator_frame_on_full_history(env: Env) -> None:
    body = env.channel(timeframe="H1", **FAR)
    h1, h4 = env.frames()
    params = env.params()
    frame = STRATEGY.indicator_frame(h1, h4, params)
    cb = compute_channel_bars(h1, h4, resolve_params(params)[0])
    assert body["count"] == len(h1) == len(body["points"])
    assert body["symbol"] == GOLD and body["timeframe"] == "H1" and body["strategy"] == "stddev_channel"
    assert body["params_version"] == 1 and body["params"] == SCHEMA.defaults()
    assert body["params_hash"] == params_hash(SCHEMA.defaults())
    assert {"n", "k", "sigma_ddof", "projection_mode"} <= set(body["params"])
    assert body["valid_count"] == int(cb.valid.sum()) > 500
    for i, pt in enumerate(body["points"]):
        assert pt["time"] == iso_z(h1["time"].iloc[i])
        assert pt["valid"] is bool(cb.valid[i])
        if not pt["valid"]:
            assert all(pt[k] is None for k in (*CHANNEL_FIELDS, "atr_h1", "is_flat", "direction", "h4_open"))
            continue
        for name in (*CHANNEL_FIELDS, "atr_h1", "bars_ahead"):
            assert pt[name] == frame[name].iloc[i], (i, name)
        assert pt["is_flat"] is bool(frame["is_flat"].iloc[i])
        assert pt["direction"] == cb.channel_direction(i) in ("up", "down", "flat")
        assert pt["h4_open"] == iso_z(pd.Timestamp(int(cb.h4_open[i]), tz="UTC"))
        assert pt["upper"] > pt["mid"] > pt["lower"]


def test_first_points_are_warm_up_and_invalid(env: Env) -> None:
    points = env.channel(**FAR)["points"]
    assert points[0]["valid"] is False and points[0]["mid"] is None
    first_valid = next(i for i, p in enumerate(points) if p["valid"])
    assert first_valid > 300  # ~100 H4 bars of warm-up
    assert all(not p["valid"] for p in points[:first_valid])


# ------------------------------------------------------------------------------------------ /chart/channel H4
def test_h4_channel_is_rolling_channel_ending_at_each_bar(env: Env) -> None:
    body = env.channel(timeframe="H4", **FAR)
    _, h4 = env.frames()
    p, _ = resolve_params(env.params())
    chan = rolling_regression_channel(h4["close"], p.n, p.k, p.sigma_ddof)
    atr = wilder_atr(h4["high"], h4["low"], h4["close"], p.atr_period)
    assert body["timeframe"] == "H4" and body["count"] == len(h4)
    assert body["valid_count"] == len(h4) - (p.n - 1)  # ATR(14) is ready before the channel (n = 100)
    for j, pt in enumerate(body["points"]):
        assert pt["time"] == iso_z(h4["time"].iloc[j])
        if j < p.n - 1:
            assert pt["valid"] is False and pt["mid"] is None
            continue
        assert pt["valid"] and pt["h4_open"] == pt["time"]
        for name in ("mid", "upper", "lower", "slope", "sigma"):
            assert pt[name] == chan[name].iloc[j], (j, name)
        assert pt["atr_h4"] == atr.iloc[j]
        assert pt["is_flat"] is (abs(pt["slope"]) * p.n < pt["atr_h4"] * p.flat_mult)
        assert pt["atr_h1"] is None and pt["bars_ahead"] is None
        assert math.isclose(pt["upper"] - pt["mid"], p.k * pt["sigma"], rel_tol=1e-9)


def test_h4_points_are_the_source_of_the_h1_projection(env: Env) -> None:
    """Each valid H1 point = the H4 point of its h4_open, extended by slope * bars_ahead (bit for bit)."""
    h1_points = [p for p in env.channel(timeframe="H1", **FAR)["points"] if p["valid"]]
    h4_by_time = {p["time"]: p for p in env.channel(timeframe="H4", **FAR)["points"]}
    for pt in h1_points:
        src = h4_by_time[pt["h4_open"]]
        assert (pt["slope"], pt["sigma"], pt["atr_h4"], pt["is_flat"], pt["direction"]) == (
            src["slope"], src["sigma"], src["atr_h4"], src["is_flat"], src["direction"])
        for line in ("mid", "upper", "lower"):
            assert pt[line] == src[line] + src["slope"] * pt["bars_ahead"]
        # the H4 bar was closed when the H1 bar was decided
        assert pd.Timestamp(pt["h4_open"]) + H4_DURATION <= pd.Timestamp(pt["time"]) + H1


def test_direction_helper_matches_channel_bars(env: Env) -> None:
    h1, h4 = env.frames()
    cb = compute_channel_bars(h1, h4, resolve_params({})[0], pattern_from=None)
    seen = set()
    for i in range(len(cb)):
        expected = cb.channel_direction(i) if cb.valid[i] else None
        assert _direction(bool(cb.valid[i]), bool(cb.is_flat[i]), float(cb.slope[i])) == expected
        seen.add(expected)
    assert {None, "up", "down"} <= seen
    assert _direction(True, False, 0.0) == "none" and _direction(True, True, 5.0) == "flat"


# ------------------------------------------------------------------------------------------ warm-up consistency
@pytest.mark.parametrize("timeframe", ["H1", "H4"])
def test_sub_range_equals_full_range(env: Env, timeframe: str) -> None:
    full = {p["time"]: p for p in env.channel(timeframe=timeframe, **FAR)["points"]}
    windows = [("2025-03-24T00:00:00Z", "2025-03-28T23:00:00Z"), ("2025-04-15T05:00:00Z", "2025-04-16T05:00:00Z"),
               ("2025-04-28T00:00:00Z", "2025-05-10T00:00:00Z")]
    for start, end in windows:
        env.app.state.chart_cache.clear()  # recompute from scratch for every window
        sub = env.channel(timeframe=timeframe, **{"from": start, "to": end})
        assert sub["from"] == start and sub["to"] == end
        assert sub["points"] and all(start <= p["time"] <= end for p in sub["points"])
        expected = [full[t] for t in sorted(full) if start <= t <= end]
        assert sub["points"] == expected
        assert any(p["valid"] for p in sub["points"])


def test_sub_range_test_is_discriminating(env: Env) -> None:
    """Sanity: a naive from-minus-warm-up slice would NOT match (Wilder ATR is path dependent)."""
    h1, h4 = env.frames()
    p, _ = resolve_params({})
    full = compute_channel_bars(h1, h4, p, pattern_from=None)
    cut = pd.Timestamp("2025-03-17T00:00:00Z")
    sliced = compute_channel_bars(h1[h1["time"] >= cut].reset_index(drop=True),
                                  h4[h4["time"] >= cut - pd.Timedelta(days=30)].reset_index(drop=True), p,
                                  pattern_from=None)
    i0 = int(np.flatnonzero(h1["time"] == cut)[0])
    tail = slice(i0 + 200, len(h1))
    assert not np.array_equal(full.atr_h1[tail], sliced.atr_h1[200:], equal_nan=True)


def test_default_window_is_30_days_before_last_bar(env: Env) -> None:
    h1, _ = env.frames()
    body = env.channel()
    last = h1["time"].iloc[-1]
    assert body["to"] == iso_z(last) and body["from"] == iso_z(last - pd.Timedelta(days=30))
    assert body["points"][-1]["time"] == iso_z(last)


# ------------------------------------------------------------------------------------------ cache
def test_chart_cache_hit_and_invalidation_on_cache_write(env: Env) -> None:
    cache: ChartCache = env.app.state.chart_cache
    first = env.channel(**FAR)
    assert (cache.misses, cache.hits) == (1, 0)
    assert env.channel(**{"from": "2025-04-01T00:00:00Z", "to": "2025-04-02T00:00:00Z"})["count"] > 0
    assert (cache.misses, cache.hits) == (1, 1)
    h1, _ = env.frames()
    env.rewrite(GOLD, Timeframe.H1, h1.iloc[:-5])  # the cache file changed -> new key
    second = env.channel(**FAR)
    assert cache.misses == 2 and second["count"] == first["count"] - 5
    env.client.put("/strategies/stddev_channel", json={"params": {"k": 2.5}})  # params changed -> new key
    third = env.channel(**FAR)
    assert cache.misses == 3 and third["params_version"] == 2 and third["params"]["k"] == 2.5


def test_chart_cache_is_bounded_lru() -> None:
    cache = ChartCache(maxsize=2)
    calls: list[str] = []

    def make(v: str):
        return lambda: calls.append(v) or v

    assert cache.get_or_compute("a", make("a")) == ("a", False)
    assert cache.get_or_compute("b", make("b")) == ("b", False)
    assert cache.get_or_compute("a", make("x")) == ("a", True)  # a is now most recent
    assert cache.get_or_compute("c", make("c")) == ("c", False)  # evicts b
    assert len(cache) == 2 and cache.get_or_compute("b", make("b2")) == ("b2", False)
    assert calls == ["a", "b", "c", "b2"]
    with pytest.raises(ValueError):
        ChartCache(maxsize=0)


# ------------------------------------------------------------------------------------------ /chart/setups
@pytest.mark.parametrize("symbol", [GOLD, BRENT])
def test_setups_equal_direct_full_scan(env: Env, symbol: str) -> None:
    env.use_account(WORKED_ACCOUNT)  # route and direct scan (env.account()) both read this stored account
    body = env.setups(symbol, **FAR)
    h1, _ = env.frames(symbol)
    expected = expected_items(env, symbol)
    assert len(expected) >= 15 and body["count"] == len(expected) == len(body["setups"])
    for got, exp in zip(body["setups"], expected, strict=True):
        assert_item_matches(got, exp, h1)
    assert body["status_counts"] == {s: sum(e["status"] == s for e in expected)
                                     for s in ("accepted", "rejected", "pending_entry")}
    assert body["account"] == WORKED_ACCOUNT == env.account().model_dump()
    assert body["params_version"] == 1 and body["params_hash"] == params_hash(SCHEMA.defaults())
    assert body["symbol_spec"]["name"] == symbol and _is_persian(body["note_fa"])
    ids = [s["id"] for s in body["setups"]]
    assert len(set(ids)) == len(ids) and env.setups(symbol, **FAR)["setups"] == body["setups"]  # stable


def test_setups_accepted_levels_and_volume_arithmetic(env: Env) -> None:
    env.use_account(WORKED_ACCOUNT)  # balance 1000, risk 1 % -> 10.00 USD per trade, leverage 100
    body = env.setups(**FAR)
    assert body["account"] == WORKED_ACCOUNT
    accepted = [s for s in body["setups"] if s["status"] == "accepted"]
    assert accepted
    for s in accepted:
        dist = abs(s["entry"] - s["stop_loss"])
        sign = 1 if s["direction"] == "buy" else -1
        assert s["risk_distance"] == dist
        assert s["take_profit"] == s["entry"] + sign * s["rr"] * dist
        raw = (1000.0 * 1.0 / 100) / (dist * 100.0)  # gold: tick_value/tick_size = 1.0/0.01 = 100 per lot
        assert s["volume"] == round(math.floor(raw / 0.01 + 1e-9) * 0.01, 2) >= 0.01
        assert s["margin"] == s["volume"] * 100 * s["entry"] / 100  # contract 100, leverage 100
        assert s["actual_risk"] == s["volume"] * (dist * 100.0)  # volume * loss_per_lot
        assert s["actual_risk"] <= 10.0 + 1e-9 and s["rejection_reason_fa"] is None


def test_setups_filtered_by_decision_time(env: Env) -> None:
    full = env.setups(**FAR)["setups"]
    assert len(full) >= 6
    lo, hi = full[2]["decision_time"], full[-3]["decision_time"]
    sub = env.setups(**{"from": lo, "to": hi})
    assert sub["setups"] == [s for s in full if lo <= s["decision_time"] <= hi]
    assert sub["setups"][0]["decision_time"] == lo and sub["setups"][-1]["decision_time"] == hi


def test_setups_equal_evaluate_on_prefix(env: Env) -> None:
    """Anti look-ahead: each setup is what evaluate() returns on the data known at its decision time."""
    h1, h4 = env.frames()
    params, account = env.params(), env.account()
    scanned = {c.confirmation_bar_open_utc: c for c in STRATEGY.scan(h1, h4, params, account, symbol=GOLD)}
    body = env.setups(**FAR)
    assert len(body["setups"]) == len(scanned) >= 15
    for s in body["setups"]:
        t = int(np.flatnonzero(h1["time"] == pd.Timestamp(s["confirmation_bar_time"]))[0])
        decision = pd.Timestamp(s["decision_time"])
        ctx = StrategyContext(symbol=GOLD, h1=h1.iloc[: t + 1], h4=slice_closed_bars(h4, H4_DURATION, decision),
                              account=account, params=params)
        cand = STRATEGY.evaluate(ctx)
        assert cand is not None
        assert_same_candidate(cand, scanned[cand.confirmation_bar_open_utc])
        assert (s["setup_type"], s["stop_loss"], s["reason_fa"], s["indicators"]) == (
            cand.setup.value, cand.stop_loss, cand.reason_fa, cand.extra)


def _truncate(env: Env, h1: pd.DataFrame, h4: pd.DataFrame, last_h1_open: pd.Timestamp) -> None:
    """Rewrite the gold cache as it looked when bar ``last_h1_open`` had just closed."""
    now = last_h1_open + H1
    env.rewrite(GOLD, Timeframe.H1, h1[h1["time"] <= last_h1_open])
    env.rewrite(GOLD, Timeframe.H4, slice_closed_bars(h4, H4_DURATION, now))


def test_truncating_future_bars_does_not_change_a_setup(env: Env) -> None:
    h1, h4 = env.frames()
    body = env.setups(**FAR)["setups"]
    accepted = [s for s in body if s["status"] == "accepted"]
    sample = accepted[:: max(1, len(accepted) // 4)][:4]
    assert len(sample) >= 3
    for s in sample:
        _truncate(env, h1, h4, pd.Timestamp(s["entry_time"]))  # cache now ends with the entry bar t+1
        again = env.setups(**FAR)["setups"]
        assert {x["id"]: x for x in again}[s["id"]] == s
        # every earlier setup is unchanged as well
        earlier = [x for x in body if x["decision_time"] <= s["decision_time"]]
        assert again[: len(earlier)] == earlier


def test_last_bar_setup_is_pending_entry(env: Env) -> None:
    h1, h4 = env.frames()
    full = [s for s in env.setups(**FAR)["setups"] if s["status"] == "accepted"]
    s = full[len(full) // 2]
    _truncate(env, h1, h4, pd.Timestamp(s["confirmation_bar_time"]))  # bar t+1 does not exist yet
    body = env.setups(**FAR)
    last = body["setups"][-1]
    assert last["id"] == s["id"] and last["status"] == "pending_entry"
    assert last["entry"] is None and last["entry_time"] is None and last["take_profit"] is None
    assert last["volume"] is None and last["margin"] is None and last["risk_distance"] is None
    assert last["stop_loss"] == s["stop_loss"] and last["reference_price"] == s["reference_price"]
    assert _is_persian(last["volume_note_fa"]) and last["rejection_reason_fa"] is None
    assert body["status_counts"]["pending_entry"] == 1
    # the default window ends at the close of the last bar, so the pending setup is included
    assert env.setups()["setups"][-1]["id"] == s["id"]


def test_gap_through_stop_is_rejected(env: Env) -> None:
    full = [s for s in env.setups(**FAR)["setups"] if s["status"] == "accepted"]
    s = full[0]
    h1, _ = env.frames()
    t1 = int(np.flatnonzero(h1["time"] == pd.Timestamp(s["entry_time"]))[0])
    gap_open = s["stop_loss"] - 1.0 if s["direction"] == "buy" else s["stop_loss"] + 1.0
    edited = h1.copy()
    edited.loc[t1, "open"] = gap_open
    edited.loc[t1, "low"] = min(edited.loc[t1, "low"], gap_open)
    edited.loc[t1, "high"] = max(edited.loc[t1, "high"], gap_open)
    env.rewrite(GOLD, Timeframe.H1, edited)
    got = {x["id"]: x for x in env.setups(**FAR)["setups"]}[s["id"]]
    assert got["status"] == "rejected" and got["entry"] == gap_open
    assert got["take_profit"] is None and got["volume"] is None
    assert _is_persian(got["rejection_reason_fa"]) and "حد ضرر" in got["rejection_reason_fa"]
    assert got["stop_loss"] == s["stop_loss"]  # SL was known at decision time; only the fill failed


def test_sizing_rejection_below_volume_min(env: Env) -> None:
    assert env.client.put("/settings", json={"balance": 100, "risk_pct": 0.01}).status_code == 200
    body = env.setups(**FAR)
    expected = expected_items(env)
    rejected = [s for s in body["setups"] if s["status"] == "rejected"]
    assert rejected and body["account"]["risk_pct"] == 0.01
    for got, exp in zip(body["setups"], expected, strict=True):
        assert_item_matches(got, exp, env.frames()[0])
    for s in rejected:
        assert s["volume"] is None and "کمتر از حداقل حجم" in s["rejection_reason_fa"]
        assert s["take_profit"] is not None  # the levels were fine; only the size failed


def test_sizing_rejection_margin_above_balance(env: Env) -> None:
    assert env.client.put("/settings", json={"balance": 50, "risk_pct": 10, "leverage": 1}).status_code == 200
    body = env.setups(BRENT, **FAR)
    for got, exp in zip(body["setups"], expected_items(env, BRENT), strict=True):
        assert_item_matches(got, exp, env.frames(BRENT)[0])
    margin = [s for s in body["setups"] if s["status"] == "rejected" and "مارجین" in (s["rejection_reason_fa"] or "")]
    assert margin and all(s["volume"] is None for s in margin)


def test_missing_spec_keeps_levels_but_no_volume(env: Env) -> None:
    env.cache.spec_path(GOLD).unlink()
    body = env.setups(**FAR)
    assert body["symbol_spec"] is None
    accepted = [s for s in body["setups"] if s["status"] == "accepted"]
    assert accepted
    for s in accepted:
        assert s["volume"] is None and s["volume_note_fa"] == SPEC_MISSING_FA
        assert s["entry"] is not None and s["take_profit"] is not None
    assert env.setups(BRENT, **FAR)["symbol_spec"]["name"] == BRENT


def test_rr_change_rescans_with_new_rr(env: Env) -> None:
    before = env.setups(**FAR)["setups"]
    env.client.put("/settings", json={"rr": 3})
    after = env.setups(**FAR)["setups"]
    assert [s["id"] for s in after] == [s["id"] for s in before]
    for s in after:
        assert s["rr"] == 3.0
        if s["take_profit"] is not None:
            sign = 1 if s["direction"] == "buy" else -1
            assert s["take_profit"] == s["entry"] + sign * 3.0 * abs(s["entry"] - s["stop_loss"])


# ------------------------------------------------------------------------------------------ boundaries
def test_empty_range_returns_empty_lists(env: Env) -> None:
    window = {"from": "2020-01-01T00:00:00Z", "to": "2020-02-01T00:00:00Z"}
    for tf in ("H1", "H4"):
        body = env.channel(timeframe=tf, **window)
        assert body["points"] == [] and body["count"] == 0 and body["from"] == window["from"]
    body = env.setups(**window)
    assert body["setups"] == [] and body["count"] == 0
    weekend = {"from": "2025-03-08T00:00:00Z", "to": "2025-03-09T12:00:00Z"}  # Saturday -> Sunday noon
    assert env.channel(**weekend)["points"] == []


@pytest.mark.parametrize(("path", "extra"), [("/chart/channel", {}), ("/chart/setups", {})])
@pytest.mark.parametrize(("symbol", "status", "code"), [
    ("EURUSD", 404, "symbol_not_configured"), ("../../etc", 422, "invalid_symbol"),
])
def test_invalid_symbol_is_clean_persian_4xx(env: Env, path: str, extra: dict, symbol: str, status: int,
                                             code: str) -> None:
    response = env.client.get(path, params={"symbol": symbol, **extra})
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code and _is_persian(detail["message_fa"])


@pytest.mark.parametrize("path", ["/chart/channel", "/chart/setups"])
def test_from_after_to_is_422(env: Env, path: str) -> None:
    response = env.client.get(path, params={"symbol": GOLD, "from": "2025-04-02T00:00:00Z",
                                            "to": "2025-04-01T00:00:00Z"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_range"
    assert env.client.get(path, params={"symbol": GOLD, "from": "not-a-date"}).status_code == 422


def test_invalid_timeframe_is_422(env: Env) -> None:
    response = env.client.get("/chart/channel", params={"symbol": GOLD, "timeframe": "M5"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_timeframe"


def test_too_little_data_gives_invalid_points_and_no_setups(env: Env) -> None:
    assert env.client.put("/strategies/stddev_channel", json={"params": {"n": 500}}).status_code == 200
    for tf in ("H1", "H4"):
        body = env.channel(timeframe=tf, **FAR)
        assert body["count"] > 0 and body["valid_count"] == 0
        assert all(p["valid"] is False for p in body["points"]) and _is_persian(body["message_fa"])
        assert body["params"]["n"] == 500
    setups = env.setups(**FAR)
    assert setups["setups"] == [] and _is_persian(setups["message_fa"])


def test_missing_cache_is_200_with_message(env: Env) -> None:
    env.cache.series_path(GOLD, Timeframe.H4).unlink()
    h1_only = env.channel(**FAR)
    assert h1_only["count"] > 0 and h1_only["valid_count"] == 0 and _is_persian(h1_only["message_fa"])
    assert env.channel(timeframe="H4", **FAR)["points"] == []
    assert env.setups(**FAR)["setups"] == []
    env.cache.series_path(GOLD, Timeframe.H1).unlink()
    body = env.channel(**FAR)
    assert body["points"] == [] and _is_persian(body["message_fa"])


def test_stored_params_invalid_is_409(env: Env) -> None:
    env.client.get("/strategies")  # v1
    db = env.app.state.db
    with db.transaction():
        db.execute("UPDATE strategy_versions SET params_json = ? WHERE is_active = 1", ('{"n": 3}',))
    for path in ("/chart/channel", "/chart/setups"):
        response = env.client.get(path, params={"symbol": GOLD})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "stored_params_invalid"


def test_chart_routes_never_touch_mt5(env: Env) -> None:
    env.channel(**FAR)
    env.channel(timeframe="H4", **FAR)
    env.setups(**FAR)
    env.client.get("/rates/gaps", params={"symbol": GOLD, "timeframe": "H1"})
    assert env.fake.calls == []


# ------------------------------------------------------------------------------------------ logging
def _log_run(make_settings, tmp_path: Path, dev: bool) -> str:
    data_dir = tmp_path / ("dev" if dev else "prod")
    settings = make_settings(f"DEV_MODE={'true' if dev else 'false'}\nMT5_LOGIN=12345678\nMT5_PASSWORD=S3cr3t!\n",
                             environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    seed_cache(data_dir, repo_data_dir=tmp_path / "elsewhere")
    app = create_app(settings)
    stream = io.StringIO()
    logging_setup.configure_logging(settings, stream=stream, force=True)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        client.get("/chart/channel", params={"symbol": GOLD, **FAR})
        client.get("/chart/channel", params={"symbol": GOLD, **FAR})
        client.get("/chart/setups", params={"symbol": GOLD, **FAR})
        client.get("/chart/setups", params={"symbol": GOLD, "from": "2025-04-02T00:00:00Z",
                                            "to": "2025-04-01T00:00:00Z"})
    return stream.getvalue()


def test_dev_mode_logs_chart_flow_without_secrets(make_settings, tmp_path: Path) -> None:
    text = _log_run(make_settings, tmp_path, dev=True)
    assert "GET /chart/channel XAUUSD.x H1" in text
    assert "cache miss" in text and "cache hit" in text
    assert "chart setups XAUUSD.x" in text and "status=accepted" in text
    assert "S3cr3t!" not in text and "12345678" not in text


def test_chart_logs_silent_without_dev_mode(make_settings, tmp_path: Path) -> None:
    assert _log_run(make_settings, tmp_path, dev=False) == ""
