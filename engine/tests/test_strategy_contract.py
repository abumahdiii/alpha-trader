"""Strategy contract S1: the engine is no longer hard-coded to the StdDev channel.

A tiny non-channel test strategy (``fixtures/ma_cross.py``: two moving averages crossing, setup slug
``ma_cross``, ``line = None``, its own ``setup_title_fa``) goes through every generic path:

* ``SignalCandidate``: free slugs, ``line = None``, StdDev rules still enforced for the six ``Setup`` values;
* ``Strategy`` hooks: default ``scan`` == per-bar ``evaluate`` on closed prefixes (and truncation invariant),
  a single-pass override == the default, ``first_valid_index`` / ``warmup_margin_h4_bars`` defaults, identity;
  the StdDev overrides return exactly the phase-4 values (first valid channel bar, 10 * atr_period, texts);
* registry: ``replace`` / ``unregister`` for plugins, built-in names reserved;
* backtest: ``run_backtest`` with the test strategy (provenance: name, version, sha256, source), the config and
  scan identity checks; strategy-aware LRU keys (two strategies with the SAME params hash never share cached
  candidates / first valid bars);
* API: ``/chart/setups?strategy=``, ``/chart/channel`` (StdDev only -> 409 ``channel_not_available``),
  ``/backtests/limits?strategy=``, ``POST /backtests`` with ``strategy`` / ``strategy_version``, run summaries
  with ``strategy_source`` / ``strategy_sha256``; unknown strategy -> 404 ``strategy_not_found``; DEV_MODE logs
  carry the strategy identity.

Never MT5, never the real ``data/`` (synthetic walk + seeded tmp cache).
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from alpha_engine.app import create_app
from alpha_engine.backtest.history import HistoryData, build_bar_arrays, load_history, scan_full_history
from alpha_engine.backtest.jobs import PreparedHistory, first_valid_index, scan_for
from alpha_engine.backtest.metrics import compute_metrics
from alpha_engine.backtest.models import RunConfig
from alpha_engine.backtest.periods import PeriodError, earliest_start
from alpha_engine.backtest.runner import RunConfigError, run_backtest
from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.logging_setup import ROOT_LOGGER_NAME
from alpha_engine.routes.chart import ChartCache
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy, resolve_params
from alpha_engine.strategies.stddev_channel.setups import compute_channel_bars
from alpha_engine.strategy.base import (
    H4_DURATION,
    Strategy,
    StrategyContext,
    StrategyContractError,
    StrategyIdentity,
    StrategyParamsError,
    WarmupTextsFa,
    slice_closed_bars,
)
from alpha_engine.strategy.params import ParamSchema, params_hash
from alpha_engine.strategy.registry import (
    RESERVED_NAMES,
    InvalidStrategyError,
    ReservedStrategyNameError,
    StrategyRegistry,
    UnknownStrategyError,
)
from alpha_engine.strategy.signal import Setup, SignalCandidate
from fixtures.channel_scenarios import random_walk
from fixtures.ma_cross import TITLE_FA, MaCross, MaCrossFade, MaCrossFast, plugin_class
from fixtures.seed_cache import seed_cache

UTC = timezone.utc
SYMBOL = "XAUUSD.x"
ACCOUNT = AccountSettings(balance=2500.0, risk_pct=1.0, leverage=100, rr=2.0)
SPEC = SymbolSpec(name=SYMBOL, digits=2, point=0.01, trade_contract_size=100.0, trade_tick_value=1.0,
                  trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0,
                  currency_profit="USD", currency_base="XAU")
MA_CLEAN, _ = MaCross.validate_params({})
MA_HASH = params_hash(MA_CLEAN)
SHA_A, SHA_B = "a" * 64, "b" * 64
MaPlugin = plugin_class(MaCross, SHA_A)


def _is_persian(text: str | None) -> bool:
    return bool(text) and any("؀" <= ch <= "ۿ" for ch in text)


@pytest.fixture(scope="module")
def walk() -> tuple[pd.DataFrame, pd.DataFrame]:
    return random_walk(1500, seed=11)


# ================================================================================ SignalCandidate
CONF = datetime(2026, 9, 21, 13, 0, tzinfo=UTC)


def _cand(**overrides: Any) -> SignalCandidate:
    base: dict[str, Any] = dict(
        strategy_name="ma_cross", strategy_version=1, params_hash=MA_HASH, symbol=SYMBOL, direction="buy",
        setup="ma_cross", line=None, decision_time_utc=CONF + timedelta(hours=1), confirmation_bar_open_utc=CONF,
        reference_price=2000.0, stop_loss=1995.0, rr=2.0, pattern="ma_cross_up", reason_fa="کراس",
        setup_title_fa="کراس میانگین‌ها",
    )
    base.update(overrides)
    return SignalCandidate(**base)


def test_candidate_accepts_a_strategy_slug_without_a_line() -> None:
    c = _cand()
    assert c.setup == "ma_cross" and type(c.setup) is str and c.setup_slug == "ma_cross" and c.line is None
    dumped = c.model_dump(mode="json")
    assert dumped["setup"] == "ma_cross" and dumped["line"] is None and dumped["setup_title_fa"] == "کراس میانگین‌ها"
    assert SignalCandidate.model_validate_json(c.model_dump_json()) == c
    # no title -> the key is left out of the dump (as for every StdDev candidate)
    assert "setup_title_fa" not in _cand(setup_title_fa=None).model_dump(mode="json")
    # the channel line is allowed too (e.g. a later anchored-channel system), with its own slug
    assert _cand(setup="anchor_bounce", line="lower").line == "lower"


def test_stddev_candidates_serialize_exactly_as_before() -> None:
    c = _cand(strategy_name="stddev_channel", setup="bounce_lower", line="lower", setup_title_fa=None,
              pattern="pin_bar", reason_fa="برگشت")
    assert c.setup is Setup.BOUNCE_LOWER and c.setup_slug == "bounce_lower"
    assert list(c.model_dump(mode="json")) == [
        "strategy_name", "strategy_version", "params_hash", "symbol", "direction", "setup", "line",
        "decision_time_utc", "confirmation_bar_open_utc", "entry_rule", "reference_price", "stop_loss", "rr",
        "indicative_take_profit", "pattern", "reason_fa", "extra"]
    assert c.model_dump(mode="json")["setup"] == "bounce_lower"


@pytest.mark.parametrize("overrides", [
    dict(setup="Ma_cross"), dict(setup="1abc"), dict(setup="ma-cross"), dict(setup=""), dict(setup="a" * 49),
    dict(setup=5), dict(setup_title_fa=""),
    # the six Setup values keep the StdDev line/direction rules whatever the strategy
    dict(setup="bounce_lower", line=None), dict(setup="bounce_lower", line="mid"),
    dict(setup="bounce_upper", line="upper"),  # sell-only setup given as a buy
    # the built-in channel strategy may only emit Setup values
    dict(strategy_name="stddev_channel", setup="ma_cross"),
    dict(line="side"),
])
def test_candidate_validation(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        _cand(**overrides)


def test_slug_length_limit_is_48() -> None:
    assert _cand(setup="a" * 48).setup_slug == "a" * 48


# ================================================================================ Strategy hooks
def _per_bar_evaluate(strategy: Strategy, h1: pd.DataFrame, h4: pd.DataFrame, params: dict) -> list[SignalCandidate]:
    """Independent reference: evaluate on every closed prefix (H4 sliced with slice_closed_bars)."""
    out = []
    for t in range(len(h1)):
        decision = h1["time"].iloc[t] + timedelta(hours=1)
        ctx = StrategyContext(symbol=SYMBOL, h1=h1.iloc[: t + 1], h4=slice_closed_bars(h4, H4_DURATION, decision),
                              account=ACCOUNT, params=params)
        cand = strategy.evaluate(ctx)
        if cand is not None:
            out.append(cand)
    return out


def test_default_scan_equals_per_bar_evaluate(walk) -> None:
    h1, h4 = walk
    strategy = MaCross()
    scanned = strategy.scan(h1, h4, {}, ACCOUNT, symbol=SYMBOL)
    assert scanned == _per_bar_evaluate(strategy, h1, h4, MA_CLEAN)
    assert len(scanned) >= 20 and {c.direction for c in scanned} == {"buy", "sell"}
    assert all(c.setup == "ma_cross" and c.line is None and c.setup_title_fa == TITLE_FA["ma_cross"] for c in scanned)
    # the H4 filter really reads the H4 bars closed at each decision (a filter-off scan finds more)
    unfiltered = strategy.scan(h1, h4, {"use_h4_filter": False}, ACCOUNT, symbol=SYMBOL)
    assert len(unfiltered) > len(scanned)


def test_default_scan_is_truncation_invariant(walk) -> None:
    """No look-ahead: scanning a prefix (with the FULL H4 frame) gives the same candidates for its bars."""
    h1, h4 = walk
    full = MaCross().scan(h1, h4, {}, ACCOUNT, symbol=SYMBOL)
    for cut in (300, 777, 1200):
        cutoff = h1["time"].iloc[cut].to_pydatetime()
        prefix = MaCross().scan(h1.iloc[:cut], h4, {}, ACCOUNT, symbol=SYMBOL)
        assert prefix == [c for c in full if c.confirmation_bar_open_utc < cutoff]


def test_single_pass_override_equals_default_scan(walk) -> None:
    h1, h4 = walk
    fast = MaCrossFast()
    for params in ({}, {"fast": 5, "slow": 13, "use_h4_filter": False}):
        assert fast.scan(h1, h4, params, ACCOUNT, symbol=SYMBOL) == Strategy.scan(fast, h1, h4, params, ACCOUNT,
                                                                                  symbol=SYMBOL)


def test_default_scan_rejects_invalid_params(walk) -> None:
    h1, h4 = walk
    with pytest.raises(StrategyParamsError) as err:
        MaCross().scan(h1, h4, {"fast": 9, "slow": 8}, ACCOUNT, symbol=SYMBOL)
    assert _is_persian(err.value.errors_fa[0])


class _Minimal(Strategy):
    name: ClassVar[str] = "minimal"
    version: ClassVar[int] = 2
    title_fa: ClassVar[str] = "حداقلی"
    param_schema: ClassVar[ParamSchema] = ParamSchema([])
    history_bars: ClassVar[int] = 5

    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        return None


def test_default_hooks_and_identity(walk) -> None:
    h1, h4 = walk
    s = _Minimal()
    assert s.first_valid_index(h1, h4, {}) == 4 and s.first_valid_index(h1.iloc[:4], h4, {}) is None
    assert s.warmup_margin_h4_bars({}) == 0 and s.warmup_texts_fa == WarmupTextsFa()
    assert s.identity == StrategyIdentity("minimal", 2, None, "builtin")
    assert s.scan(h1.iloc[:0], h4, {}, ACCOUNT, symbol=SYMBOL) == []
    assert MaPlugin().identity == StrategyIdentity("ma_cross", 1, SHA_A, "plugin")
    assert MaPlugin().identity.cache_key == ("ma_cross", 1, SHA_A)


def test_stddev_hooks_keep_the_phase4_values(walk) -> None:
    h1, h4 = walk
    s = StdDevChannelStrategy()
    assert s.identity == StrategyIdentity("stddev_channel", 1, None, "builtin")
    assert s.warmup_margin_h4_bars({}) == 140 and s.warmup_margin_h4_bars({"atr_period": 20}) == 200
    p, _ = resolve_params({})
    valid = np.flatnonzero(compute_channel_bars(h1, h4, p, pattern_from=None).valid)
    assert s.first_valid_index(h1, h4, {}) == int(valid[0])
    # the Persian period texts are the phase-4 strings
    times = pd.DatetimeIndex(h1["time"]).as_unit("ns").asi8
    h4_ns = pd.DatetimeIndex(h4["time"]).as_unit("ns").asi8
    with pytest.raises(PeriodError) as err:
        earliest_start(times, h4_ns, None, 140, texts=s.warmup_texts_fa)
    assert err.value.message_fa == "داده کش برای ساختن کانال کافی نیست (هیچ کندلی با کانال و ATR معتبر وجود ندارد)."
    with pytest.raises(PeriodError) as err:
        earliest_start(times, h4_ns[:200], int(valid[0]), 140, texts=s.warmup_texts_fa)
    assert err.value.message_fa == ("داده کش برای گرم شدن اندیکاتورها کافی نیست: بعد از اولین کندل معتبر کانال به "
                                    "140 کندل H4 دیگر (۱۰ برابر دوره ATR) نیاز است.")


# ================================================================================ registry
def _plugin(name: str, version: int = 1, sha: str = SHA_A, base: type[MaCross] = MaCross) -> type[MaCross]:
    cls = plugin_class(base, sha, version)
    cls.name = name  # type: ignore[misc]
    return cls


def test_registry_replace_unregister_and_reserved_names() -> None:
    reg = StrategyRegistry()
    assert "stddev_channel" in RESERVED_NAMES
    # a plugin can never take a built-in name, even before the built-in is registered
    with pytest.raises(ReservedStrategyNameError):
        reg.register(_plugin("stddev_channel"))
    reg.register(StdDevChannelStrategy)
    for action in (lambda: reg.register(_plugin("stddev_channel")), lambda: reg.replace(_plugin("stddev_channel")),
                   lambda: reg.unregister("stddev_channel")):
        with pytest.raises(ReservedStrategyNameError):
            action()
    assert reg.get("stddev_channel") is StdDevChannelStrategy and reg.is_reserved("stddev_channel")
    # built-in test classes (source builtin) are protected as well once registered
    reg.register(_Minimal)
    with pytest.raises(ReservedStrategyNameError):
        reg.unregister("minimal")
    # plugin versions: replace returns the previous class, unregister removes
    v1, v2 = _plugin("ma_cross", 1, SHA_A), _plugin("ma_cross", 2, SHA_B)
    assert reg.replace(v1) is None and reg.get("ma_cross") is v1 and not reg.is_reserved("ma_cross")
    assert reg.replace(v2) is v1 and reg.get("ma_cross") is v2
    with pytest.raises(ReservedStrategyNameError):  # replace() is for plugins only
        reg.replace(MaCross)
    assert reg.unregister("ma_cross") is v2 and "ma_cross" not in reg
    with pytest.raises(UnknownStrategyError):
        reg.unregister("ma_cross")
    with pytest.raises(InvalidStrategyError):
        reg.register(_plugin("bad_sha", sha="XYZ"))


# ================================================================================ backtest
@pytest.fixture(scope="module")
def walk_history() -> HistoryData:
    h1, h4 = random_walk(3000, seed=5)
    h1 = h1.assign(spread=np.full(len(h1), 25, dtype=np.int64))
    return HistoryData(symbol=SYMBOL, h1=h1, h4=h4, spec=SPEC)


def _ma_config(history: HistoryData, strategy: Strategy, **kw: Any) -> RunConfig:
    ident = strategy.identity
    base: dict[str, Any] = dict(
        symbol=SYMBOL, mode="random", windows_count=5, window_months=1, seed=3, account=ACCOUNT,
        strategy_name=ident.name, strategy_version=ident.version, strategy_sha256=ident.sha256,
        strategy_source=ident.source, params=MA_CLEAN, params_hash=MA_HASH, provisional=False)
    base.update(kw)
    return RunConfig(**base)


def test_run_backtest_with_a_non_channel_strategy(walk_history: HistoryData) -> None:
    strategy = MaPlugin()
    config = _ma_config(walk_history, strategy)
    result = run_backtest(config, walk_history, strategy=strategy)
    trades = [t for w in result.windows for t in w.trades]
    assert len(trades) >= 10
    assert {t.setup_type for t in trades} == {"ma_cross"} and all(t.line is None for t in trades)
    # provenance: name, code version, sha256, source in the stored config
    dumped = result.config.model_dump(mode="json")
    assert (dumped["strategy_name"], dumped["strategy_version"], dumped["strategy_sha256"],
            dumped["strategy_source"]) == ("ma_cross", 1, SHA_A, "plugin")
    # no warm-up margin: earliest start = close of the last H4 bar closed at the first decision (slow = 8)
    assert result.plan.warmup_h4_bars == 0
    times = pd.DatetimeIndex(walk_history.h1["time"]).as_unit("ns").asi8
    h4_ns = pd.DatetimeIndex(walk_history.h4["time"]).as_unit("ns").asi8
    assert pd.Timestamp(result.plan.earliest_start) == earliest_start(times, h4_ns, 8, 0)
    # the same run through a precomputed scan is identical (deep equality)
    scan = scan_full_history(strategy, walk_history.h1, walk_history.h4, MA_CLEAN, ACCOUNT, symbol=SYMBOL)
    assert scan.identity == strategy.identity and scan.warmup_margin_h4_bars == 0 and scan.first_valid_index == 8
    assert run_backtest(config, walk_history, scan) == result


def test_run_backtest_checks_the_strategy_identity(walk_history: HistoryData) -> None:
    strategy = MaPlugin()
    config = _ma_config(walk_history, strategy)
    with pytest.raises(RunConfigError):  # not registered in the process-wide registry
        run_backtest(config, walk_history)
    with pytest.raises(RunConfigError):  # source file changed (other sha256)
        run_backtest(_ma_config(walk_history, strategy, strategy_sha256=SHA_B), walk_history, strategy=strategy)
    with pytest.raises(RunConfigError):  # a scan of another strategy with the same params hash
        fade = plugin_class(MaCrossFade, SHA_A)()
        fade_scan = scan_full_history(fade, walk_history.h1, walk_history.h4, MA_CLEAN, ACCOUNT, symbol=SYMBOL)
        run_backtest(config, walk_history, fade_scan, strategy=strategy)
    with pytest.raises(RunConfigError):  # StdDev config run with the test strategy
        run_backtest(_ma_config(walk_history, strategy, strategy_name="stddev_channel", strategy_sha256=None,
                                strategy_source=None), walk_history, strategy=strategy)
    with pytest.raises(RunConfigError):  # params the strategy rejects
        run_backtest(_ma_config(walk_history, strategy, params={"fast": 9, "slow": 8}), walk_history,
                     strategy=strategy)


class _Broken(MaCross):
    name: ClassVar[str] = "broken"
    mode: ClassVar[str] = "hash"

    def scan(self, h1, h4, params, account, *, symbol):  # type: ignore[override]
        good = MaCross.scan(self, h1, h4, params, account, symbol=symbol)
        if self.mode == "hash":
            return [c.model_copy(update={"params_hash": "0" * 64}) for c in good]
        return good + good[:1]  # duplicate confirmation bar


@pytest.mark.parametrize("mode", ["hash", "duplicate"])
def test_scan_full_history_enforces_the_candidate_contract(walk, mode: str) -> None:
    h1, h4 = walk
    broken = type("B", (_Broken,), {"mode": mode})()
    with pytest.raises(StrategyContractError):
        scan_full_history(broken, h1, h4, {}, ACCOUNT, symbol=SYMBOL)


def test_lru_keys_include_the_strategy_identity(walk_history: HistoryData) -> None:
    """MaCross and MaCrossFade have the same params hash: they must never share a cached scan / first valid."""
    assert params_hash(MaCrossFade.validate_params({})[0]) == MA_HASH
    prepared = PreparedHistory(key=("test",), history=walk_history,
                               bars=build_bar_arrays(walk_history.h1, walk_history.spec))
    lru = ChartCache()
    kw = dict(symbol=SYMBOL, params=MA_CLEAN, params_hash=MA_HASH, account=ACCOUNT, lru=lru)
    a = scan_for(prepared, strategy=MaCross(), **kw)
    b = scan_for(prepared, strategy=MaCrossFade(), **kw)
    assert {c.strategy_name for c in a.candidates} == {"ma_cross"}
    assert {c.strategy_name for c in b.candidates} == {"ma_fade"} and {c.setup for c in b.candidates} == {"ma_fade"}
    assert lru.misses == 2 and lru.hits == 0
    assert scan_for(prepared, strategy=MaCross(), **kw) is a and lru.hits == 1  # same identity -> same entry
    # same name + version, different source hash (a re-uploaded plugin file) -> its own entry
    c = scan_for(prepared, strategy=plugin_class(MaCross, SHA_B)(), **kw)
    assert c is not a and lru.misses == 3 and c.identity.sha256 == SHA_B
    # first valid bar: keyed by identity too
    assert first_valid_index(prepared, MaCross(), MA_CLEAN, MA_HASH, lru) == 8
    assert first_valid_index(prepared, _Minimal(), MA_CLEAN, MA_HASH, lru) == 4
    assert lru.misses == 5


# ================================================================================ API
GOLD = SYMBOL
START, END = "2024-06-03", "2025-03-03"
MANUAL = {"symbol": GOLD, "mode": "manual", "from": "2024-09-02T00:00:00Z", "to": "2025-02-24T00:00:00Z"}
TERMINAL = {"done", "error", "cancelled", "interrupted"}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("s1_seed")
    seed_cache(out, repo_data_dir=out / "not_data", symbols=(GOLD,), start=START, end=END)
    return out


def _registry() -> StrategyRegistry:
    reg = StrategyRegistry()
    reg.register(StdDevChannelStrategy)
    reg.register(MaPlugin)
    return reg


@pytest.fixture
def client(make_settings, seeded: Path, tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(make_settings, seeded, tmp_path)


def _client(make_settings, seeded: Path, tmp_path: Path, env_text: str = "") -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    if not data_dir.exists():
        shutil.copytree(seeded, data_dir)
    settings = make_settings(env_text, environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    app = create_app(settings, strategy_registry=_registry())
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        c.app_data_dir = data_dir  # type: ignore[attr-defined]
        yield c


def _error(response: Any, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code and _is_persian(detail["message_fa"])
    return detail


def _wait(client: TestClient, run_id: int, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/backtests/{run_id}").json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def test_chart_setups_for_a_selected_strategy(client: TestClient) -> None:
    span = {"symbol": GOLD, "from": "2024-09-01T00:00:00Z", "to": "2025-03-01T00:00:00Z"}
    body = client.get("/chart/setups", params={**span, "strategy": "ma_cross"}).json()
    assert (body["strategy"], body["strategy_version"], body["strategy_source"], body["strategy_sha256"]) == \
        ("ma_cross", 1, "plugin", SHA_A)
    assert body["params_hash"] == MA_HASH and body["count"] > 5 and body["evaluation"]["available"]
    for item in body["setups"]:
        assert item["setup_type"] == "ma_cross" and item["line"] is None and item["line_value"] is None
        assert item["setup_title_fa"] == TITLE_FA["ma_cross"] and item["id"].endswith(":ma_cross")
    assert body["backtest_window"]["available"] and body["summary"]["total"] == body["count"]
    assert any(i["backtest"]["traded"] for i in body["setups"])
    # the default strategy is unchanged: StdDev, built-in, no sha key, phase-3 ids
    default = client.get("/chart/setups", params=span).json()
    assert default["strategy"] == "stddev_channel" and default["strategy_source"] == "builtin"
    assert "strategy_sha256" not in default
    assert default["setups"] and all(len(i["id"].split(":")) == 3 for i in default["setups"])
    assert {i["setup_type"] for i in default["setups"]} <= {s.value for s in Setup}


def test_chart_channel_is_stddev_only(client: TestClient) -> None:
    detail = _error(client.get("/chart/channel", params={"symbol": GOLD, "strategy": "ma_cross"}), 409,
                    "channel_not_available")
    assert "stddev_channel" in detail["message_fa"]
    ok = client.get("/chart/channel", params={"symbol": GOLD, "strategy": "stddev_channel"})
    assert ok.status_code == 200 and ok.json()["valid_count"] > 0 and ok.json()["strategy_source"] == "builtin"


def test_unknown_strategy_is_a_persian_404(client: TestClient) -> None:
    for response in (client.get("/chart/setups", params={"symbol": GOLD, "strategy": "nope"}),
                     client.get("/chart/channel", params={"symbol": GOLD, "strategy": "nope"}),
                     client.get("/backtests/limits", params={"symbol": GOLD, "strategy": "nope"}),
                     client.post("/backtests", json={**MANUAL, "strategy": "nope"}),
                     client.post("/backtests", json={**MANUAL, "strategy": "ma_cross", "strategy_version": 2})):
        _error(response, 404, "strategy_not_found")
    bad = client.post("/backtests", json={**MANUAL, "strategy_version": True})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_request"


def test_limits_follow_the_selected_strategy(client: TestClient) -> None:
    std = client.get("/backtests/limits", params={"symbol": GOLD}).json()
    ma = client.get("/backtests/limits", params={"symbol": GOLD, "strategy": "ma_cross"}).json()
    assert std["warmup_h4_bars"] == 140 and ma["warmup_h4_bars"] == 0
    assert ma["earliest_start"] < std["earliest_start"] and ma["params_hash"] == MA_HASH
    assert "ATR" in std["note_fa"] and "ATR" not in ma["note_fa"] and _is_persian(ma["note_fa"])
    # POST accepts a window the StdDev limits would refuse
    early = {**MANUAL, "from": ma["earliest_start"], "to": std["earliest_start"]}
    _error(client.post("/backtests", json=early), 422, "window_too_early")
    accepted = client.post("/backtests", json={**early, "strategy": "ma_cross"})
    assert accepted.status_code == 202, accepted.text
    assert _wait(client, accepted.json()["id"])["status"] == "done"


def test_post_backtest_with_a_selected_strategy(client: TestClient) -> None:
    response = client.post("/backtests", json={**MANUAL, "strategy": "ma_cross", "strategy_version": 1})
    assert response.status_code == 202, response.text
    detail = _wait(client, response.json()["id"])
    assert detail["status"] == "done", detail["error"]
    assert (detail["strategy"], detail["strategy_version"], detail["strategy_source"], detail["strategy_sha256"]) == \
        ("ma_cross", 1, "plugin", SHA_A)
    cfg = detail["config"]
    assert (cfg["strategy_name"], cfg["strategy_sha256"], cfg["strategy_source"], cfg["params_hash"]) == \
        ("ma_cross", SHA_A, "plugin", MA_HASH)
    assert detail["request"] == {**MANUAL, "strategy": "ma_cross", "strategy_version": 1}
    trades = client.get(f"/backtests/{detail['id']}/trades").json()["trades"]
    assert trades and {t["setup_type"] for t in trades} == {"ma_cross"} and all(t["line"] is None for t in trades)
    # exactly what a direct run of the stored config gives
    history = load_history(OhlcvCache(client.app_data_dir), GOLD)  # type: ignore[attr-defined]
    direct = run_backtest(RunConfig.model_validate(cfg), history, strategy=MaPlugin())
    window = direct.windows[0]
    assert detail["metrics"] == compute_metrics(window.trades, window.equity, window.initial_balance).to_dict()
    # a default run next to it: built-in, no sha, config without the sha key
    std = _wait(client, client.post("/backtests", json=MANUAL).json()["id"])
    assert std["status"] == "done" and std["strategy_source"] == "builtin" and std["strategy_sha256"] is None
    assert "strategy_sha256" not in std["config"] and std["config"]["strategy_source"] == "builtin"
    runs = {r["id"]: r for r in client.get("/backtests").json()["runs"]}
    assert (runs[detail["id"]]["strategy_source"], runs[detail["id"]]["strategy_sha256"]) == ("plugin", SHA_A)
    assert (runs[std["id"]]["strategy_source"], runs[std["id"]]["strategy_sha256"]) == ("builtin", None)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.parametrize("dev", [True, False])
def test_dev_mode_logs_carry_the_strategy_identity(make_settings, seeded: Path, tmp_path: Path, dev: bool) -> None:
    handler = _ListHandler()
    for c in _client(make_settings, seeded, tmp_path, f"DEV_MODE={'true' if dev else 'false'}\n"
                     "MT5_PASSWORD=S3cr3t!\nMT5_LOGIN=12345678\n"):
        logging.getLogger(ROOT_LOGGER_NAME).addHandler(handler)
        try:
            assert c.get("/chart/setups", params={"symbol": GOLD, "strategy": "ma_cross"}).status_code == 200
            _wait(c, c.post("/backtests", json={**MANUAL, "strategy": "ma_cross"}).json()["id"])
        finally:
            logging.getLogger(ROOT_LOGGER_NAME).removeHandler(handler)
    text = "\n".join(handler.messages)
    assert "S3cr3t!" not in text and "12345678" not in text
    label = f"ma_cross v1 [plugin sha={SHA_A[:12]}]"
    if dev:
        for needle in (f"strategy selected: {label}", f"backtest scan {GOLD}: {label}", "cache hit",
                       f"strategy {label}", f"backtest first valid bar {GOLD} {label}"):
            assert needle in text, needle
    else:
        assert label not in text
