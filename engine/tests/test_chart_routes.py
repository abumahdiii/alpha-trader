"""``/chart/channel`` and ``/chart/setups`` (phase 3) on the synthetic fixture cache -- no MT5.

The synthetic dataset (tests/fixtures/synthetic_ohlcv.py, seeded through fixtures/seed_cache.py) has
10 weeks of XAUUSD.x / BRNUSD.x H1+H4 with weekends, daily breaks, holidays and planted holes; with the
default params (n = 100 H4 bars) the channel becomes valid after ~4 weeks and ``scan`` finds 22 gold
candidates. Every expectation below is computed independently from the SAME cached frames with the
strategy code (indicator_frame / compute_channel_bars / scan / evaluate / levels / sizing) and compared
EXACTLY (JSON floats round-trip bit for bit).
"""

from __future__ import annotations

import copy
import io
import math
import time
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
from alpha_engine.backtest.costs import spread_from_frame
from alpha_engine.backtest.history import build_bar_arrays, load_history
from alpha_engine.backtest.models import (
    NO_SWAP_LABEL_FA,
    PROVISIONAL_LABEL_FA,
    ZERO_COMMISSION_LABEL_FA,
    CostModel,
)
from alpha_engine.backtest.setup_outcomes import simulate_setups
from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.gaps import find_gaps
from alpha_engine.data.schema import Timeframe, iso_z
from alpha_engine.indicators.atr import wilder_atr
from alpha_engine.indicators.regression_channel import rolling_regression_channel
from alpha_engine.market_data import MarketDataService
from alpha_engine.mt5_adapter import Mt5Adapter
from alpha_engine.risk.sizing import size_from_spec
from alpha_engine.routes.chart import CHART_CACHE_SIZE, SPEC_MISSING_FA, ChartCache, _direction
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import SCHEMA, StdDevChannelStrategy, resolve_params
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
    """Direct full-history scan + the SIMULATOR's entry rules, written out independently of the route:
    bars are bid, ask = bid + spread * point (causal zero fill / auto fallback: costs.spread_from_frame);
    a ``missing`` gap between t and t+1 rejects; buy fills at the ask open and is rejected when the BID open
    is at/below the SL, sell fills at the bid open and is rejected when the ASK open is at/above the SL;
    TP from the fill; volume sized on the fill (size_from_spec) with the stored account."""
    h1, h4 = env.frames(symbol)
    account = env.account()
    spec_entry = env.cache.read_spec(symbol)
    assert spec_entry is not None
    spec = spec_entry[0]
    spread = spread_from_frame(h1, spec.point)
    missing = find_missing_after(h1)
    out = []
    for cand in STRATEGY.scan(h1, h4, env.params(), account, symbol=symbol):
        t = int(np.flatnonzero(h1["time"] == pd.Timestamp(cand.confirmation_bar_open_utc))[0])
        item: dict[str, Any] = {"candidate": cand, "t": t, "entry": None, "bid_open": None, "status": None,
                                "tp": None, "volume": None, "reason": None, "spread_points": None}
        out.append(item)
        if t + 1 >= len(h1):
            item["status"] = "pending_entry"
            continue
        bid_open = float(h1["open"].iloc[t + 1])
        item["bid_open"] = bid_open
        if missing[t]:
            item["status"] = "rejected"
            continue
        ask_open = bid_open + float(spread.price[t + 1])
        buy = cand.direction == "buy"
        fill = ask_open if buy else bid_open
        item.update(entry=fill, spread_points=int(spread.points[t + 1]))
        if (bid_open <= cand.stop_loss) if buy else (ask_open >= cand.stop_loss):
            item["status"] = "rejected"
            continue
        _, item["tp"] = cand.resolve_levels(fill)
        sizing = size_from_spec(cand, account, spec, entry=fill)
        item["status"] = "accepted" if sizing.accepted else "rejected"
        item["reason"] = sizing.reason_fa
        item["volume"] = sizing.volume if sizing.accepted else None
        item["margin"] = sizing.margin if sizing.accepted else None
    return out


def find_missing_after(h1: pd.DataFrame) -> np.ndarray:
    """``missing[t]``: the step from bar t to bar t+1 is a ``missing`` gap (data.gaps)."""
    report = find_gaps(h1[["time"]], Timeframe.H1)
    after = {pd.Timestamp(g.after) for g in report.gaps if g.kind == "missing"}
    return np.array([ts in after for ts in h1["time"]], dtype=bool)


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
    assert got["entry"] == exp["entry"]  # simulator fill (buy: ask open, sell: bid open)
    assert got["entry_bid_open"] == exp["bid_open"]  # the phase-3 meaning of "entry"
    if exp["bid_open"] is not None:
        assert got["entry_time"] == iso_z(h1["time"].iloc[exp["t"] + 1])
        assert got["entry_bid_open"] == h1["open"].iloc[exp["t"] + 1]
    assert got["spread_at_entry_points"] == exp["spread_points"]
    assert got["take_profit"] == exp["tp"]
    assert got["volume"] == exp["volume"]
    if exp["status"] == "rejected" and exp["reason"]:
        assert exp["reason"] in got["rejection_reason_fa"]
    assert (got["outcome"] is not None) == (exp["status"] == "accepted")


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


def _range_independent(item: dict) -> dict:
    """An item without the backtest flag (the only field that depends on the requested range)."""
    return {k: v for k, v in item.items() if k != "backtest"}


def test_setups_filtered_by_decision_time(env: Env) -> None:
    full = env.setups(**FAR)["setups"]
    assert len(full) >= 6
    lo, hi = full[2]["decision_time"], full[-3]["decision_time"]
    sub = env.setups(**{"from": lo, "to": hi})
    assert [_range_independent(s) for s in sub["setups"]] == [
        _range_independent(s) for s in full if lo <= s["decision_time"] <= hi]
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
    full_missing = find_missing_after(h1)
    for s in sample:
        cut = pd.Timestamp(s["entry_time"])
        _truncate(env, h1, h4, cut)  # cache now ends with the entry bar t+1
        again = env.setups(**FAR)["setups"]
        got = {x["id"]: x for x in again}[s["id"]]
        t = int(np.flatnonzero(h1["time"] == pd.Timestamp(s["confirmation_bar_time"]))[0])
        if find_missing_after(h1[h1["time"] <= cut])[t] != full_missing[t]:
            # Known limit of data/gaps.py (not of this route): a session break in the first days of a new DST
            # regime is only recognised with up to 28 days of LATER data, so on a cache ending right after it
            # the step t -> t+1 is "missing" and the entry is rejected (the backtest applies the same rule).
            assert got["status"] == "rejected" and "missing" in got["rejection_reason_fa"]
            continue
        # decision-time fields, entry, levels, volume: unchanged (outcome / flag are checked below)
        strip = ("outcome", "backtest")
        assert {k: v for k, v in got.items() if k not in strip} == {k: v for k, v in s.items() if k not in strip}
        # every earlier setup is unchanged as well
        earlier = [x for x in body if x["decision_time"] <= s["decision_time"]]
        assert [{k: v for k, v in x.items() if k not in strip} for x in again[: len(earlier)]] == [
            {k: v for k, v in x.items() if k not in strip} for x in earlier]
        # outcomes: an exit up to the new last bar is unchanged; a trade still open there is "end of data"
        for old, new in zip(earlier, again[: len(earlier)], strict=True):
            if old["outcome"] is None:
                assert new["outcome"] is None
            elif pd.Timestamp(old["outcome"]["exit_bar_time"]) <= cut:
                assert new["outcome"] == old["outcome"]
            else:
                assert new["outcome"]["result"] == "end_of_data"
                assert new["outcome"]["exit_bar_time"] == iso_z(cut)
                assert "پایان داده" in new["outcome"]["exit_reason_fa"]


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
    spread_price = got["spread_at_entry_points"] * 0.01  # gold point
    assert got["status"] == "rejected" and got["entry_bid_open"] == gap_open
    assert got["entry"] == (gap_open + spread_price if s["direction"] == "buy" else gap_open)
    assert got["take_profit"] is None and got["volume"] is None and got["outcome"] is None
    assert _is_persian(got["rejection_reason_fa"]) and "حد ضرر" in got["rejection_reason_fa"]
    assert got["stop_loss"] == s["stop_loss"]  # SL was known at decision time; only the fill failed


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_gap_through_stop_uses_the_exit_side_price(env: Env, direction: str) -> None:
    """F1: a buy is rejected when the BID open is at/below the SL; a sell when the ASK open (bid + spread) is
    at/above the SL -- even if its bid open is still below the SL (phase 3 checked the bid for both)."""
    full = [s for s in env.setups(**FAR)["setups"] if s["status"] == "accepted" and s["direction"] == direction]
    assert full
    s = full[0]
    h1, _ = env.frames()
    t1 = int(np.flatnonzero(h1["time"] == pd.Timestamp(s["entry_time"]))[0])
    spread_price = s["spread_at_entry_points"] * 0.01
    assert spread_price > 0
    # buy: bid open exactly at the SL (ask above it); sell: bid open half a spread below the SL (ask above it)
    gap_open = s["stop_loss"] if direction == "buy" else s["stop_loss"] - spread_price / 2
    edited = h1.copy()
    edited.loc[t1, "open"] = gap_open
    edited.loc[t1, "low"] = min(edited.loc[t1, "low"], gap_open)
    edited.loc[t1, "high"] = max(edited.loc[t1, "high"], gap_open)
    env.rewrite(GOLD, Timeframe.H1, edited)
    got = {x["id"]: x for x in env.setups(**FAR)["setups"]}[s["id"]]
    assert got["status"] == "rejected" and "حد ضرر" in got["rejection_reason_fa"], got


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


def test_outcomes_equal_the_setup_outcomes_module(env: Env) -> None:
    """Every item's outcome is ``simulate_setup`` on the cached data with the stored account (settings balance)."""
    env.use_account(WORKED_ACCOUNT)
    body = env.setups(**FAR)
    history = load_history(env.cache, GOLD)
    bars = build_bar_arrays(history.h1, history.spec)
    cands = STRATEGY.scan(history.h1, history.h4, env.params(), env.account(), symbol=GOLD)
    outs = simulate_setups(bars, cands, spec=history.spec, account=env.account(), cost_model=CostModel())
    assert len(outs) == len(body["setups"]) >= 15
    results = set()
    for got, o in zip(body["setups"], outs, strict=True):
        assert got["status"] == o.status
        if o.trade is None:
            assert got["outcome"] is None
            continue
        tr, out = o.trade, got["outcome"]
        assert (got["entry"], got["take_profit"], got["volume"]) == (tr.entry, tr.take_profit, tr.volume)
        assert got["risk_amount"] == 1000.0 * 1.0 / 100  # sized with the settings balance, never another one
        assert (out["result"], out["exit_reason"], out["exit_price"], out["net_pnl"], out["r_multiple"]) == (
            o.result, tr.exit_reason.value, tr.exit_price, tr.net_pnl, tr.r_multiple)
        assert out["exit_time"] == iso_z(pd.Timestamp(tr.exit_time)) and out["bars_held"] == tr.bars_held
        assert out["pnl_price"] == (tr.exit_price - tr.entry) * (1 if got["direction"] == "buy" else -1)
        assert out["net_pnl"] == out["gross_pnl"] - out["commission"] and out["commission"] == 0
        assert _is_persian(out["exit_reason_fa"])
        results.add(out["result"])
    assert {"tp", "sl"} <= results


def test_summary_is_the_sum_of_the_rows(env: Env) -> None:
    body = env.setups(**FAR)
    rows, s = body["setups"], body["summary"]
    closed = [r["outcome"] for r in rows if r["outcome"] and r["outcome"]["result"] in ("tp", "sl")]
    open_end = [r["outcome"] for r in rows if r["outcome"] and r["outcome"]["result"] == "end_of_data"]
    wins = [o["net_pnl"] for o in closed if o["net_pnl"] > 1e-9]
    losses = [o["net_pnl"] for o in closed if o["net_pnl"] < -1e-9]
    assert (s["total"], s["accepted"], s["rejected"], s["pending_entry"]) == (
        len(rows), body["status_counts"]["accepted"], body["status_counts"]["rejected"],
        body["status_counts"]["pending_entry"])
    assert (s["closed"], s["wins"], s["losses"], s["open_end_of_data"]) == (
        len(closed), len(wins), len(losses), len(open_end))
    assert s["closed"] + s["open_end_of_data"] == s["accepted"] and s["closed"] >= 10
    assert s["win_rate"] == pytest.approx(len(wins) / len(closed))
    assert s["net_pnl"] == pytest.approx(math.fsum(o["net_pnl"] for o in closed))
    assert s["gross_profit"] == pytest.approx(math.fsum(wins)) and s["gross_loss"] == pytest.approx(-math.fsum(losses))
    assert s["profit_factor"] == pytest.approx(math.fsum(wins) / -math.fsum(losses))
    assert s["total_r"] == pytest.approx(math.fsum(o["r_multiple"] for o in closed))
    assert s["avg_r"] == pytest.approx(s["total_r"] / len(closed))
    assert s["open_net_pnl"] == pytest.approx(math.fsum(o["net_pnl"] for o in open_end))
    ev = body["evaluation"]
    assert ev["available"] and ev["basis"] == "independent_setups"
    assert ev["label_fa"] == "ارزیابی مستقل هر ستاپ، بدون قید یک معامله باز؛ با نتیجه بک‌تست فرق دارد"
    assert ev["cost_model"] == CostModel().model_dump(mode="json")
    assert ev["labels_fa"][0] == PROVISIONAL_LABEL_FA and NO_SWAP_LABEL_FA in ev["labels_fa"]
    assert ZERO_COMMISSION_LABEL_FA in ev["labels_fa"] and ev["spread_fallback"]["source"] == "auto_median_observed"
    empty = env.setups(**{"from": "2020-01-01T00:00:00Z", "to": "2020-02-01T00:00:00Z"})
    assert empty["summary"]["total"] == 0 and empty["summary"]["win_rate"] is None
    assert empty["summary"]["profit_factor"] is None and empty["summary"]["net_pnl"] == 0


def test_backtest_flag_matches_a_real_backtest_run(env: Env) -> None:
    """The window in ``backtest_window`` is what «بک‌تست همین بازه» submits: POST it to /backtests and the
    traded setups are exactly the run's trades (same confirmation bars, same net P&L)."""
    body = env.setups(**FAR)
    win = body["backtest_window"]
    assert win["available"] and win["clipped"] and _is_persian(win["note_fa"])  # FAR is clipped on both sides
    response = env.client.post("/backtests", json={"symbol": GOLD, "mode": "manual", "from": win["from"],
                                                   "to": win["to"]})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    deadline = time.monotonic() + 60
    while env.client.get(f"/backtests/{run_id}").json()["status"] not in ("done", "error"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    detail = env.client.get(f"/backtests/{run_id}").json()
    assert detail["status"] == "done", detail
    trades = env.client.get(f"/backtests/{run_id}/trades").json()["trades"]
    assert trades and win["trades"] == len(trades) and win["net_profit"] == pytest.approx(detail["net_profit"])
    traded = [s for s in body["setups"] if s["backtest"] and s["backtest"]["traded"]]
    assert [s["confirmation_bar_time"] for s in traded] == [
        iso_z(pd.Timestamp(t["confirmation_bar_time"])) for t in trades]
    assert [s["backtest"]["net_pnl"] for s in traded] == [t["net_pnl"] for t in trades]
    reasons = {s["backtest"]["reason"] for s in body["setups"] if s["backtest"] and not s["backtest"]["traded"]}
    assert "outside_window" in reasons
    for s in body["setups"]:
        flag = s["backtest"]
        assert flag is not None
        assert flag["traded"] or _is_persian(flag["reason_fa"])
        if flag["reason"] == "position_open":
            assert "پوزیشن دیگری باز بود" in flag["reason_fa"]
        if flag["reason"] == "outside_window":
            assert flag["reason_fa"] == "خارج از بازه بک‌تست"


def test_range_before_the_earliest_start_has_no_backtest(env: Env) -> None:
    full = env.setups(**FAR)["setups"]
    early = {"from": full[0]["decision_time"], "to": full[1]["decision_time"]}
    body = env.setups(**early)
    win = body["backtest_window"]
    assert not win["available"] and win["code"] == "window_too_early" and _is_persian(win["message_fa"])
    assert win["from"] is None and win["trades"] is None
    assert body["setups"] and all(s["backtest"] is None for s in body["setups"])
    assert body["evaluation"]["available"]  # outcomes are still there
    assert any(s["outcome"] for s in body["setups"])


def test_missing_spec_degrades_the_evaluation(env: Env) -> None:
    env.cache.spec_path(GOLD).unlink()
    body = env.setups(**FAR)
    ev = body["evaluation"]
    assert ev["available"] is False and _is_persian(ev["message_fa"]) and ev["labels_fa"] == []
    assert body["backtest_window"]["available"] is False and body["backtest_window"]["code"] == "spec_missing"
    assert body["summary"]["total"] == body["count"] and body["summary"]["closed"] == 0
    for s in body["setups"]:
        assert s["outcome"] is None and s["backtest"] is None and s["entry_spread_source"] is None
        assert s["entry"] == s["entry_bid_open"]  # spread unknown without the spec's point


def test_evaluation_failure_never_fails_the_request(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import alpha_engine.routes.chart as chart_module

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated evaluation failure")

    monkeypatch.setattr(chart_module, "simulate_setups", boom)
    body = env.setups(**FAR)
    assert body["count"] >= 15 and body["evaluation"]["available"] is False
    assert body["evaluation"]["message_fa"] == chart_module.EVAL_FAILED_FA
    assert all(s["outcome"] is None for s in body["setups"])


def test_account_change_is_never_served_from_a_cache(env: Env) -> None:
    """Outcomes depend on every account field: a new balance / risk / leverage changes volumes and P&L at once
    (the shared scan entry is reused, nothing evaluated is cached)."""
    env.use_account(WORKED_ACCOUNT)
    first = env.setups(**FAR)
    cache: ChartCache = env.app.state.chart_cache
    assert cache.maxsize == CHART_CACHE_SIZE == 16
    misses = cache.misses
    env.use_account({**WORKED_ACCOUNT, "balance": 4000.0})
    second = env.setups(**FAR)
    assert cache.misses == misses  # same data, params and R:R -> history, scan, first valid bar all cache hits
    a = [s for s in first["setups"] if s["status"] == "accepted"]
    b = {s["id"]: s for s in second["setups"]}
    changed = [s for s in a if b[s["id"]]["volume"] != s["volume"]]
    assert changed and all(b[s["id"]]["risk_amount"] == 40.0 for s in a if b[s["id"]]["status"] == "accepted")
    assert second["summary"]["net_pnl"] != first["summary"]["net_pnl"]


def test_scan_candidates_are_not_mutated_by_the_route(env: Env) -> None:
    env.setups(**FAR)
    cache: ChartCache = env.app.state.chart_cache
    scans = [v for k, v in cache._data.items() if isinstance(k, tuple) and k and k[0] == "bt_scan"]
    assert len(scans) == 1
    before = copy.deepcopy([c.model_dump() for c in scans[0].candidates])
    env.setups(**FAR)
    env.setups(**{"from": "2025-04-01T00:00:00Z", "to": "2025-05-01T00:00:00Z"})
    assert env.client.put("/settings", json={"balance": 700.0}).status_code == 200
    env.setups(**FAR)
    assert [c.model_dump() for c in scans[0].candidates] == before
    assert not any(k[0] in ("setups_eval", "outcomes") for k in cache._data if isinstance(k, tuple))


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
    # phase 5: per-setup outcome, window clipping, backtest-flag matching, summary, scan cache
    assert "setup outcome XAUUSD.x" in text and "entry=" in text and " exit " in text
    assert "setups backtest window for range" in text and "setups backtest flags" in text
    assert "setups summary:" in text and "evaluated (simulator rules)" in text and "backtest scan XAUUSD.x" in text
    assert "S3cr3t!" not in text and "12345678" not in text


def test_chart_logs_silent_without_dev_mode(make_settings, tmp_path: Path) -> None:
    assert _log_run(make_settings, tmp_path, dev=False) == ""
