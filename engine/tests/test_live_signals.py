"""Live signals (phase 6): the H1-close scheduler on a fake live market (no terminal, never the real data/).

The market is the deterministic synthetic XAUUSD.x/BRNUSD.x data (server clock ``fixed(+2)``) served by
``fixtures.live_market.LiveFakeMt5``: bars appear as the test clock advances (the bar in progress is returned with
its full OHLC, exactly what the adapter must drop), ticks follow the clock. The scheduler is driven step by step
(``LiveSignals.step``) with an injected PC clock.

SUGGESTIONS ONLY: nothing here (or in the engine) places, modifies or checks an order.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
import pytest

from alpha_engine.backtest.history import load_history, scan_full_history
from alpha_engine.backtest.models import CostModel, RunConfig, SkipReason
from alpha_engine.backtest.runner import run_backtest
from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.schema import Timeframe
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.data.timezone import OffsetModel
from alpha_engine.logging_setup import ROOT_LOGGER_NAME
from alpha_engine.market_data import MarketDataService
from alpha_engine.mt5_adapter import Mt5Adapter
from alpha_engine.signals import service as service_module
from alpha_engine.signals.service import LiveSignals
from alpha_engine.storage.account_settings import AccountSettings, AccountSettingsRepo
from alpha_engine.storage.db import open_db
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy
from alpha_engine.strategy.base import Strategy, StrategyContext, bar_open_times
from alpha_engine.strategy.params import ParamSchema, ParamSpec, params_hash
from alpha_engine.strategy.registry import StrategyRegistry
from alpha_engine.strategy.resolve import resolve_active
from alpha_engine.strategy.signal import SignalCandidate
from fixtures.live_market import LiveFakeMt5, MarketClock, seed_mt5_cache
from fixtures.synthetic_ohlcv import generate_dataset

GOLD, BRENT = "XAUUSD.x", "BRNUSD.x"
MODEL = OffsetModel.fixed(2)
DATA_START = datetime(2025, 2, 24, tzinfo=timezone.utc)
H1 = timedelta(hours=1)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def server_epoch(when: datetime) -> int:
    return int(MODEL.utc_to_server([int(when.timestamp())])[0])


# ---------------------------------------------------------------------------------------------- test strategy
class ScheduledBuy(Strategy):
    """Deterministic test strategy: a BUY on every H1 bar whose open time is in ``bars`` (ISO ``...Z``);
    SL = low - ``sl_buffer``, reference = close. O(n) scan == per-bar evaluate."""

    name: ClassVar[str] = "scheduled_buy"
    version: ClassVar[int] = 1
    title_fa: ClassVar[str] = "خرید زمان‌بندی‌شده (آزمایشی)"
    param_schema: ClassVar[ParamSchema] = ParamSchema([
        ParamSpec(name="sl_buffer", type="float", default=1.0, min=0.0, max=100.0, label_fa="فاصله حد ضرر"),
    ])
    bars: ClassVar[frozenset[str]] = frozenset()

    def _cand(self, symbol: str, open_t: pd.Timestamp, low: float, close: float, p: Mapping[str, Any],
              account: AccountSettings) -> SignalCandidate | None:
        if open_t.strftime("%Y-%m-%dT%H:%M:%SZ") not in self.bars:
            return None
        sl = low - float(p["sl_buffer"])
        if not 0 < sl < close:
            return None
        open_dt = open_t.to_pydatetime()
        return SignalCandidate(
            strategy_name=self.name, strategy_version=self.version, params_hash=params_hash(dict(p)), symbol=symbol,
            direction="buy", setup="scheduled", line=None, setup_title_fa="ستاپ زمان‌بندی‌شده",
            decision_time_utc=open_dt + H1, confirmation_bar_open_utc=open_dt, reference_price=close, stop_loss=sl,
            rr=account.rr, pattern="scheduled", reason_fa="کندل زمان‌بندی‌شده آزمایشی", extra={"low": low})

    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        if ctx.has_open_trade or len(ctx.h1) == 0:
            return None
        p = self.clean_params(ctx.params)
        last = ctx.h1.iloc[-1]
        return self._cand(ctx.symbol, pd.Timestamp(bar_open_times(ctx.h1)[-1]), float(last["low"]),
                          float(last["close"]), p, ctx.account)

    def scan(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None, account: AccountSettings,
             *, symbol: str) -> list[SignalCandidate]:
        p = self.clean_params(params)
        times = bar_open_times(h1)
        lows, closes = h1["low"].tolist(), h1["close"].tolist()
        out = []
        for i, open_t in enumerate(times):
            cand = self._cand(symbol, pd.Timestamp(open_t), float(lows[i]), float(closes[i]), p, account)
            if cand is not None:
                out.append(cand)
        return out


# ---------------------------------------------------------------------------------------------- environment
@pytest.fixture(scope="module")
def dataset():
    return generate_dataset((GOLD, BRENT), "2025-02-24", "2025-05-05", 20250224, MODEL)


@dataclass
class LiveEnv:
    clock: MarketClock
    fake: LiveFakeMt5
    adapter: Mt5Adapter
    service: MarketDataService
    live: LiveSignals
    registry: StrategyRegistry
    data_dir: Path

    def at(self, when: datetime, *, polls: int = 1, spacing_s: float = 15.0) -> None:
        """Set the TRUE (server) time and run ``polls`` scheduler steps ``spacing_s`` seconds apart."""
        self.clock.set(when)
        for i in range(polls):
            if i:
                self.clock.advance(seconds=spacing_s)
            self.live.step()

    def hourly(self, first_boundary: datetime, last_boundary: datetime, second: int = 21) -> None:
        b = first_boundary
        while b <= last_boundary:
            self.at(b + timedelta(seconds=second))
            b += H1

    def signals(self, **filters: Any) -> list[dict]:
        rows, _ = self.live.repo.list_signals(limit=500, **filters)
        return list(reversed(rows))

    def checks(self) -> list[dict]:
        return [e["payload"] for e in self.live.repo.events(kind="check", limit=100000)]

    def messages(self) -> list[dict]:
        return self.live.messages_after(0)[0]


@pytest.fixture
def make_env(make_settings, dataset, tmp_path):
    envs: list[LiveEnv] = []

    def _make(start: datetime, *, symbols: tuple[str, ...] = (GOLD,), pc_ahead_s: float = 0.0,
              strategy: str = "stddev_channel", env_text: str = "", enable: bool = True) -> LiveEnv:
        data_dir = tmp_path / f"data{len(envs)}"
        settings = make_settings(f"MT5_SERVER_UTC_OFFSET=2\nENGINE_SYMBOLS={','.join(symbols)}\n{env_text}",
                                 environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
        clock = MarketClock(start)  # the initial cache is fetched with a correct clock (then the PC may drift)
        fake = LiveFakeMt5([s for (sym, _), s in dataset.items() if sym in symbols], clock, MODEL,
                           account_login=12345678)
        adapter = Mt5Adapter(settings, fake, sleep=lambda s: None, clock=clock.pc_clock)
        assert adapter.connect().state == "connected"
        service = MarketDataService.create(settings, adapter, clock=clock.pc_clock, update_min_interval=0.0)
        seed_mt5_cache(service, symbols, DATA_START)
        clock.pc_ahead_s = pc_ahead_s
        registry = StrategyRegistry()
        registry.register(StdDevChannelStrategy)
        registry.register(ScheduledBuy)
        db = open_db(data_dir / "alpha.db")
        live = LiveSignals(db, service, registry=registry, clock=clock.pc_clock)
        if enable:
            live.update_settings(enabled=True, live_strategy=strategy)
        env = LiveEnv(clock, fake, adapter, service, live, registry, data_dir)
        envs.append(env)
        return env

    yield _make
    for env in envs:
        env.live.db.close()


class ListHandler(logging.Handler):
    """Collects the engine's log messages (the ``alpha_engine`` logger does not propagate to pytest's caplog)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def __enter__(self) -> ListHandler:
        logging.getLogger(ROOT_LOGGER_NAME).addHandler(self)
        return self

    def __exit__(self, *exc: object) -> None:
        logging.getLogger(ROOT_LOGGER_NAME).removeHandler(self)


def _cand_dump(c: SignalCandidate) -> dict:
    return c.model_dump(mode="json")


# ---------------------------------------------------------------------------------------------- live == backtest
PROOF_START = utc(2025, 4, 27, 21, 0)  # Sunday, before the reopen (22:00 UTC, NY summer time)
PROOF_LAST_BOUNDARY = utc(2025, 5, 3, 0, 0)


@pytest.fixture(scope="module")
def proof_run(tmp_path_factory, dataset):
    """ONE live run over a week, bar by bar (module scope: ~20 s)."""
    from alpha_engine.config import load_settings

    tmp = tmp_path_factory.mktemp("live_proof")
    env_file = tmp / "fake.env"
    env_file.write_text(f"MT5_SERVER_UTC_OFFSET=2\nENGINE_SYMBOLS={GOLD}\n", encoding="utf-8")
    settings = load_settings(env_file=env_file, environ={"ALPHA_TRADER_DATA_DIR": str(tmp / "data"),
                                                         "ENGINE_MT5_AUTOCONNECT": "false"})
    clock = MarketClock(PROOF_START)
    fake = LiveFakeMt5([s for (sym, _), s in dataset.items() if sym == GOLD], clock, MODEL)
    adapter = Mt5Adapter(settings, fake, sleep=lambda s: None, clock=clock.pc_clock)
    adapter.connect()
    service = MarketDataService.create(settings, adapter, clock=clock.pc_clock, update_min_interval=0.0)
    seed_mt5_cache(service, (GOLD,), DATA_START)
    db = open_db(tmp / "data" / "alpha.db")
    live = LiveSignals(db, service, clock=clock.pc_clock)
    live.update_settings(enabled=True)
    env = LiveEnv(clock, fake, adapter, service, live, StrategyRegistry(), tmp / "data")
    env.at(PROOF_START, polls=2)  # warm-up polls (market closed: nothing to decide)
    env.hourly(PROOF_START + H1, PROOF_LAST_BOUNDARY)
    yield env
    db.close()


def test_live_decisions_equal_the_backtest_scan_bar_by_bar(proof_run: LiveEnv, dataset) -> None:
    env = proof_run
    rows = env.signals()
    agreed = [r for r in rows if r["mismatch_json"] is None]
    assert len(agreed) == len(rows) >= 5  # no mismatch at all
    first_bar, last_bar = PROOF_START, PROOF_LAST_BOUNDARY - H1
    # (a) the backtest's scan over the FINAL cache (what a backtest run uses)
    active = resolve_active(env.live.db)
    history = load_history(OhlcvCache(env.data_dir), GOLD)
    account = AccountSettingsRepo(env.live.db).get()
    scan = scan_full_history(active.strategy, history.h1, history.h4, active.params, account, symbol=GOLD)
    in_range = [c for c in scan.candidates if first_bar <= c.confirmation_bar_open_utc <= last_bar]
    # (b) the same scan straight on the synthetic source frames (independent of the cache writes)
    src = scan_full_history(active.strategy, dataset[(GOLD, Timeframe.H1)].utc, dataset[(GOLD, Timeframe.H4)].utc,
                            active.params, account, symbol=GOLD)
    src_in_range = [c for c in src.candidates if first_bar <= c.confirmation_bar_open_utc <= last_bar]
    stored = [json.loads(r["extra_json"])["candidate"] for r in agreed]
    assert stored == [_cand_dump(c) for c in in_range] == [_cand_dump(c) for c in src_in_range]
    # every signal: decided at the close of its bar, entry bar = the next bar, expired at the entry bar's close
    for row, cand in zip(agreed, in_range, strict=True):
        assert row["status"] == "expired"
        assert row["decision_time_utc"].startswith(cand.decision_time_utc.strftime("%Y-%m-%dT%H:%M:%S"))
        t = int(history.h1.index[history.h1["time"] == pd.Timestamp(cand.confirmation_bar_open_utc)][0])
        entry_open = history.h1["time"].iloc[t + 1]
        assert row["expires_utc"].startswith((entry_open + H1).strftime("%Y-%m-%dT%H:%M:%S"))
        assert row["entry_source"] == "tick" and row["direction"] == cand.direction
    results = Counter(c["result"] for c in env.checks())
    assert results["signal"] == len(agreed) and results["mismatch"] == 0 and "incomplete" not in results
    assert results["market_closed"] >= 4  # Sunday before the reopen, daily breaks, Friday night
    assert sum(results.values()) == int((PROOF_LAST_BOUNDARY - PROOF_START) / H1)  # one check per boundary
    assert not env.live.repo.events(kind="error")
    assert [m["type"] for m in env.messages() if m["type"] != "status"].count("signal") == len(agreed)


def test_backtest_skip_flag_matches_run_backtest(proof_run: LiveEnv) -> None:
    env = proof_run
    rows = env.signals()
    active = resolve_active(env.live.db)
    history = load_history(OhlcvCache(env.data_dir), GOLD)
    data_end = (history.h1["time"].iloc[-1] + H1).to_pydatetime()  # the whole stepped week (to the Friday close)
    config = RunConfig(symbol=GOLD, mode="manual", start=PROOF_START, end=data_end,
                       cost_model=CostModel(), account=AccountSettingsRepo(env.live.db).get(),
                       strategy_name=active.name, strategy_version=active.code_version, params=active.params,
                       params_hash=active.params_hash)
    result = run_backtest(config, history)
    skipped_open = {s.confirmation_bar_time for s in result.windows[0].skipped if s.reason is SkipReason.POSITION_OPEN}
    flagged = {datetime.strptime(r["confirmation_bar_open_utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
               for r in rows if r["backtest_would_skip"]}
    assert flagged == skipped_open and len(flagged) >= 1  # non-trivial: a trade was open for at least one setup
    traded = {t.confirmation_bar_time for t in result.windows[0].trades}
    for r in rows:
        conf = datetime.strptime(r["confirmation_bar_open_utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        assert (conf in traded) != bool(r["backtest_would_skip"])
        if r["backtest_would_skip"]:
            assert "معامله باز" in r["backtest_skip_reason_fa"]


# ---------------------------------------------------------------------------------------------- boundary timing
CAND_BAR = utc(2025, 4, 28, 10, 0)  # a stddev_channel candidate of the synthetic gold data (proof run)
CAND_BOUNDARY = CAND_BAR + H1


def test_signal_once_per_closed_bar_after_grace_and_deduped(make_env) -> None:
    env = make_env(utc(2025, 4, 28, 10, 30))
    env.at(utc(2025, 4, 28, 10, 30), polls=2)  # warm-up; the 10:00 boundary (bar 09:00) is checked
    assert env.signals() == []
    env.at(CAND_BOUNDARY + timedelta(seconds=5))  # inside the 20 s grace: no check yet
    assert env.signals() == [] and env.live.status()["next_check_utc"] == "2025-04-28T11:00:20Z"
    env.at(CAND_BOUNDARY + timedelta(seconds=21))
    rows = env.signals()
    assert len(rows) == 1 and rows[0]["status"] == "active"
    assert rows[0]["confirmation_bar_open_utc"].startswith("2025-04-28T10:00:00")
    assert rows[0]["expires_utc"].startswith("2025-04-28T12:00:00")  # close of the entry bar 11:00
    for seconds in (40, 95, 600):  # same boundary again: nothing new
        env.at(CAND_BOUNDARY + timedelta(seconds=seconds))
    assert len(env.signals()) == 1
    assert env.live.status()["next_check_utc"] == "2025-04-28T12:00:20Z"
    # a restarted engine (new scheduler, same database) decides the same bar again -> dedupe, no second row
    again = LiveSignals(env.live.db, env.service, registry=env.registry, clock=env.clock.pc_clock)
    env.live = again
    env.at(CAND_BOUNDARY + timedelta(seconds=700), polls=2)
    assert len(env.signals()) == 1
    assert env.checks()[-1]["result"] == "duplicate" and env.checks()[-1]["signal_id"] == rows[0]["id"]
    # the entry bar closes -> expired (WS message)
    env.at(CAND_BOUNDARY + H1 + timedelta(seconds=21))
    assert env.signals()[0]["status"] == "expired"
    assert any(m["type"] == "expired" and m["signal"]["id"] == rows[0]["id"] for m in env.messages())


def test_late_bar_is_retried_until_it_arrives(make_env) -> None:
    env = make_env(utc(2025, 4, 28, 10, 30))
    env.at(utc(2025, 4, 28, 10, 30), polls=2)  # ticks seen during the 10:00 hour
    env.fake.withhold.add((GOLD, Timeframe.H1, server_epoch(CAND_BAR)))
    env.at(CAND_BOUNDARY + timedelta(seconds=21))
    env.at(CAND_BOUNDARY + timedelta(seconds=90))
    assert env.signals() == [] and env.live.status()["next_check_utc"] == "2025-04-28T11:01:45Z"
    assert "incomplete" not in {c["result"] for c in env.checks()}
    env.fake.withhold.clear()  # the broker delivers the bar (within the 3-minute tolerance)
    env.at(CAND_BOUNDARY + timedelta(seconds=120))
    rows = env.signals()
    assert len(rows) == 1 and rows[0]["confirmation_bar_open_utc"].startswith("2025-04-28T10:00:00")


def test_bar_missing_beyond_tolerance_is_incomplete_no_signal(make_env) -> None:
    env = make_env(utc(2025, 4, 28, 10, 30))
    env.at(utc(2025, 4, 28, 10, 30), polls=2)
    env.fake.withhold.add((GOLD, Timeframe.H1, server_epoch(CAND_BAR)))
    for seconds in (21, 90, 181):
        env.at(CAND_BOUNDARY + timedelta(seconds=seconds))
    assert env.signals() == []
    last = env.checks()[-1]
    assert last["result"] == "incomplete" and "داده ناقص" in last["reason_fa"]
    env.fake.withhold.clear()
    env.at(CAND_BOUNDARY + H1 + timedelta(seconds=21))  # next hour: bar 11:00 decided (the 10:00 one never)
    assert all(not r["confirmation_bar_open_utc"].startswith("2025-04-28T10:00") for r in env.signals())


def test_market_closed_weekend_and_daily_break_give_no_signal(make_env) -> None:
    env = make_env(utc(2025, 4, 25, 19, 30))  # Friday
    env.at(utc(2025, 4, 25, 19, 30), polls=2)
    env.hourly(utc(2025, 4, 25, 20, 0), utc(2025, 4, 27, 23, 0))  # Fri 20:00 .. Sun 23:00 (reopen 22:00)
    by_boundary = {c["boundary"]: c["result"] for c in env.checks()}
    assert by_boundary["2025-04-25T21:00:00Z"] in ("no_setup", "signal")  # last bar of the week (20:00) decided
    closed = [b for b, r in by_boundary.items() if r == "market_closed"]
    assert "2025-04-25T22:00:00Z" in closed and "2025-04-26T12:00:00Z" in closed and "2025-04-27T22:00:00Z" in closed
    assert len(closed) == 48 + 1  # Fri 22:00 .. Sun 22:00 boundaries: no bar to decide
    assert by_boundary["2025-04-27T23:00:00Z"] in ("no_setup", "signal")  # first bar after the reopen
    # daily break (17:00-18:00 New York = 21:00-22:00 UTC in summer): Monday 22:00 boundary has no bar 21:00
    env.hourly(utc(2025, 4, 28, 0, 0), utc(2025, 4, 28, 23, 0))
    by_boundary = {c["boundary"]: c["result"] for c in env.checks()}
    assert by_boundary["2025-04-28T22:00:00Z"] == "market_closed"
    assert not env.live.repo.events(kind="error")


def test_pc_clock_ahead_uses_server_time(make_env) -> None:
    env = make_env(utc(2025, 4, 28, 10, 58), pc_ahead_s=300.0)  # PC clock 5 minutes AHEAD of the broker
    with ListHandler() as logs:
        env.at(utc(2025, 4, 28, 10, 58), polls=4)  # true 10:58:00..10:58:45, PC 11:03:00..11:03:45
    status = env.live.status()
    assert status["clock_skew_s"] == pytest.approx(300.0) and status["clock_skew_warning"] is True
    assert status["server_time_utc"] == "2025-04-28T10:58:45Z"
    # the PC clock says 11:03 but bar 10:00 is still forming on the server: never cached, never decided
    frame, _ = OhlcvCache(env.data_dir).read(GOLD, Timeframe.H1)
    assert frame["time"].iloc[-1] == pd.Timestamp("2025-04-28T09:00:00Z")
    assert env.signals() == [] and all(c["boundary"] != "2025-04-28T11:00:00Z" for c in env.checks())
    assert any("AHEAD" in m for m in logs.messages)
    assert env.live.repo.events(kind="tick")[0]["payload"]["clock_skew_s"] == pytest.approx(300.0)
    env.at(CAND_BOUNDARY + timedelta(seconds=21))  # true 11:00:21 (PC 11:05:21): now the bar is closed
    rows = env.signals()
    assert len(rows) == 1 and rows[0]["confirmation_bar_open_utc"].startswith("2025-04-28T10:00:00")


def test_pc_clock_ahead_without_the_fix_would_have_decided_a_forming_bar(make_env) -> None:
    """Regression guard for decision 4: the adapter's own (PC) clock keeps the forming bar."""
    env = make_env(utc(2025, 4, 28, 10, 58), pc_ahead_s=300.0, enable=False)
    before = OhlcvCache(env.data_dir).read(GOLD, Timeframe.H1)[0]["time"].iloc[-1]
    env.service.update(GOLD, Timeframe.H1, start=DATA_START)  # no now_utc: PC clock (11:03) -> 10:00 kept
    assert before == pd.Timestamp("2025-04-28T09:00:00Z")
    assert OhlcvCache(env.data_dir).read(GOLD, Timeframe.H1)[0]["time"].iloc[-1] == pd.Timestamp("2025-04-28T10:00:00Z")


# ---------------------------------------------------------------------------------------------- mismatch
@pytest.mark.parametrize("variant", ["missing", "different"])
def test_injected_disagreement_is_a_mismatch_not_a_signal(make_env, monkeypatch, variant: str) -> None:
    real = service_module.evaluate_checked

    def fake_evaluate(strategy, ctx, phash=None):
        cand = real(strategy, ctx, phash)
        if cand is None or cand.confirmation_bar_open_utc != CAND_BAR:
            return cand
        if variant == "missing":
            return None
        return cand.model_copy(update={"reason_fa": cand.reason_fa + " (تغییر آزمایشی)"})

    monkeypatch.setattr(service_module, "evaluate_checked", fake_evaluate)
    env = make_env(utc(2025, 4, 28, 10, 30))
    env.at(utc(2025, 4, 28, 10, 30), polls=2)
    env.at(CAND_BOUNDARY + timedelta(seconds=21))
    rows = env.signals()
    assert len(rows) == 1 and rows[0]["status"] == "rejected"
    mismatch = json.loads(rows[0]["mismatch_json"])
    assert mismatch["fields"] == (["<presence>"] if variant == "missing" else ["reason_fa"])
    assert mismatch["scan"] is not None and (mismatch["evaluate"] is None) == (variant == "missing")
    assert "مغایرت" in rows[0]["rejection_reason_fa"]
    assert env.signals(status="active") == []
    assert [e["payload"]["fields"] for e in env.live.repo.events(kind="mismatch")] == [mismatch["fields"]]
    assert not any(m["type"] == "signal" for m in env.messages())


# ---------------------------------------------------------------------------------------------- expiry / supersede
FRIDAY_LAST = utc(2025, 4, 25, 20, 0)  # last bar of the week (close 21:00 UTC = 17:00 New York)


def test_weekend_signal_expires_at_the_close_of_the_first_bar_after_the_reopen(make_env, monkeypatch) -> None:
    monkeypatch.setattr(ScheduledBuy, "bars", frozenset({"2025-04-25T20:00:00Z"}))
    env = make_env(utc(2025, 4, 25, 20, 30), strategy="scheduled_buy")
    env.at(utc(2025, 4, 25, 20, 30), polls=2)
    env.at(FRIDAY_LAST + H1 + timedelta(seconds=21))  # Friday 21:00:21: market closed after the bar
    [row] = env.signals()
    extra = json.loads(row["extra_json"])
    assert row["status"] == "active" and row["expires_utc"] is None and row["gap_class_provisional"] == 1
    assert extra["entry_gap"]["kind"] == "unknown" and extra["entry_bar_time"] is None
    assert row["entry_source"] == "last_close"  # the tick (20:59:59) is older than the decision time
    env.hourly(utc(2025, 4, 25, 22, 0), utc(2025, 4, 27, 21, 0))  # the weekend: still active
    assert env.signals()[0]["status"] == "active"
    env.at(utc(2025, 4, 27, 22, 0, 30))  # Sunday reopen: tick 22:00:30 -> provisional expiry 23:00
    assert env.signals()[0]["expires_utc"].startswith("2025-04-27T23:00:00")
    assert env.signals()[0]["status"] == "active"
    env.at(utc(2025, 4, 27, 23, 0, 21))  # the entry bar (22:00) closed and is cached
    [row] = env.signals()
    extra = json.loads(row["extra_json"])
    assert row["status"] == "expired" and row["expires_utc"].startswith("2025-04-27T23:00:00")
    assert extra["entry_gap"] == {"kind": "weekend", "provisional": False, "entry_bar_time": "2025-04-27T22:00:00Z",
                                  "note_fa": None}
    assert row["gap_class_provisional"] == 0


def test_signal_superseded_after_a_broker_revision(make_env, monkeypatch) -> None:
    monkeypatch.setattr(ScheduledBuy, "bars", frozenset({"2025-04-28T10:00:00Z"}))
    env = make_env(utc(2025, 4, 28, 10, 30), strategy="scheduled_buy")
    env.at(utc(2025, 4, 28, 10, 30), polls=2)
    env.at(CAND_BOUNDARY + timedelta(seconds=21))
    [row] = env.signals()
    assert row["status"] == "active"
    # the broker revises bar 10:00 (lower low): the fresh scan gives another SL -> the old signal is void
    raw = env.fake.rates[(GOLD, Timeframe.H1)]
    idx = int((raw["time"] == server_epoch(CAND_BAR)).nonzero()[0][0])
    env.fake.revise(GOLD, Timeframe.H1, server_epoch(CAND_BAR), low=float(raw["low"][idx]) - 2.0)
    env.at(CAND_BOUNDARY + H1 + timedelta(seconds=21))
    [row] = env.signals()
    assert row["status"] == "superseded"
    sup = json.loads(row["extra_json"])["superseded"]
    assert "stop_loss" in sup["fields"] and sup["fresh"]["stop_loss"] == pytest.approx(row["stop_loss"] - 2.0)
    assert any(m["type"] == "superseded" for m in env.messages())


def test_missing_gap_after_the_signal_bar_is_flagged_not_rejected(make_env, monkeypatch, dataset) -> None:
    """A planted one-off missing H1 bar right after the signal bar: at decision time the entry step is unknown
    (provisional, allowed); once the next bar arrives it is classified ``missing`` -> the backtest would skip it
    (``missing_gap``), flagged as provisional (the classifier also looks 28 days ahead)."""
    single = dataset[(GOLD, Timeframe.H1)].meta["missing_events"]
    hole = next(pd.Timestamp(e["start"]) for e in single if e["kind"] == "single_h1"
                and pd.Timestamp(e["start"]) > pd.Timestamp("2025-03-03T00:00Z"))
    bar = (hole - H1).to_pydatetime()
    monkeypatch.setattr(ScheduledBuy, "bars", frozenset({bar.strftime("%Y-%m-%dT%H:%M:%SZ")}))
    env = make_env(bar - timedelta(minutes=30), strategy="scheduled_buy")
    env.at(bar - timedelta(minutes=30), polls=2)
    env.at(bar + timedelta(minutes=30))  # a tick during the signal bar's hour
    env.at(bar + H1 + timedelta(seconds=21))
    [row] = env.signals()
    assert row["status"] == "active" and row["gap_class_provisional"] == 1 and row["expires_utc"] is None
    env.hourly(bar + 2 * H1, bar + 3 * H1)  # the hole, then the next bar
    [row] = env.signals()
    extra = json.loads(row["extra_json"])
    assert extra["entry_gap"]["kind"] == "missing" and extra["entry_gap"]["provisional"] is True
    assert row["backtest_would_skip"] == 1 and "گمشده" in row["backtest_skip_reason_fa"]
    assert row["status"] == "expired"


# ---------------------------------------------------------------------------------------------- MT5 down
def test_mt5_down_reconnects_with_backoff_and_never_crashes(make_env) -> None:
    env = make_env(utc(2025, 4, 28, 9, 30))
    env.at(utc(2025, 4, 28, 9, 30), polls=2)
    env.adapter.shutdown()  # terminal gone
    env.fake._init_results = [False]
    env.at(CAND_BOUNDARY + timedelta(seconds=21))
    status = env.live.status()
    assert status["state"] == "mt5_down" and status["mt5_state"] == "error" and "MetaTrader" in status["last_error_fa"]
    attempts = env.fake.names().count("initialize")
    env.at(CAND_BOUNDARY + timedelta(seconds=25))  # inside the 10 s backoff: no new attempt
    assert env.fake.names().count("initialize") == attempts
    env.at(CAND_BOUNDARY + timedelta(seconds=40))  # after it: one more attempt (fails)
    assert env.fake.names().count("initialize") == attempts + 1
    assert env.signals() == []
    env.fake._init_results = [True]  # terminal back
    env.at(CAND_BOUNDARY + timedelta(seconds=90), polls=3)  # reconnect (backoff 20 s passed), warm-up, check
    assert env.live.status()["state"] == "running"
    assert [e["payload"]["state"] for e in env.live.repo.events(kind="mt5_status")] == ["disconnected", "connected"]
    assert len(env.signals()) == 1  # the missed boundary is decided after the reconnect


def test_data_calls_failing_while_connected_record_errors_and_no_signal(make_env) -> None:
    env = make_env(utc(2025, 4, 28, 10, 30))
    env.at(utc(2025, 4, 28, 10, 30), polls=2)
    env.fake.offline = True  # copy_rates / ticks return None although the adapter still says "connected"
    for seconds in (21, 100, 200):
        env.at(CAND_BOUNDARY + timedelta(seconds=seconds))
    assert env.signals() == []
    assert env.checks()[-1]["result"] == "update_failed"
    assert env.live.repo.events(kind="error")  # and step() never raised


def test_scheduler_thread_survives_an_unexpected_error(make_env, monkeypatch) -> None:
    env = make_env(utc(2025, 4, 28, 10, 30))
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("unexpected")

    monkeypatch.setattr(env.live, "_step", boom)
    monkeypatch.setattr(env.live, "_wait_s", lambda: 0.01)
    env.live.start()
    deadline = time.monotonic() + 5
    while calls["n"] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    env.live.shutdown()
    assert calls["n"] >= 3 and env.live.status()["state"] == "stopped"
    assert env.live.repo.events(kind="error")[0]["payload"]["detail"] == "RuntimeError: unexpected"


def test_two_symbols_are_checked_one_after_the_other(make_env) -> None:
    env = make_env(utc(2025, 4, 28, 10, 30), symbols=(GOLD, BRENT))
    env.at(utc(2025, 4, 28, 10, 30), polls=2)
    env.at(CAND_BOUNDARY + timedelta(seconds=21))
    checks = [c for c in env.live.repo.events(kind="check") if c["payload"]["boundary"] == "2025-04-28T11:00:00Z"]
    assert [c["symbol"] for c in checks] == [GOLD, BRENT]
    assert set(env.live.status()["last_tick_utc"]) == {GOLD, BRENT}


def test_plugin_strategy_is_refused_for_live(make_env) -> None:
    class PluginLike(ScheduledBuy):
        name: ClassVar[str] = "plugin_like"
        source: ClassVar[str] = "plugin"  # type: ignore[assignment]
        code_sha256: ClassVar[str | None] = "a" * 64

    env = make_env(utc(2025, 4, 28, 10, 30))
    env.registry.register(PluginLike)
    env.live.update_settings(live_strategy="plugin_like")  # bypassing the route's validation on purpose
    env.at(utc(2025, 4, 28, 10, 30), polls=2)
    env.at(CAND_BOUNDARY + timedelta(seconds=21))
    assert env.signals() == []
    assert "پلاگین" in env.live.status()["last_error_fa"]


def test_dev_mode_logs_each_check_without_secrets(make_env) -> None:
    from conftest import FAKE_LOGIN, FAKE_PASSWORD

    env = make_env(utc(2025, 4, 28, 10, 30),
                   env_text=f"DEV_MODE=true\nMT5_LOGIN={FAKE_LOGIN}\nMT5_PASSWORD={FAKE_PASSWORD}\n")
    from alpha_engine.logging_setup import configure_logging

    configure_logging(env.service.settings, force=True)
    with ListHandler() as logs:
        env.at(utc(2025, 4, 28, 10, 30), polls=2)
        env.at(CAND_BOUNDARY + timedelta(seconds=21))
    text = "\n".join(logs.messages)
    for needle in ("live check XAUUSD.x: boundary 2025-04-28T11:00:00Z", "live update XAUUSD.x H1",
                   "live decide XAUUSD.x", "live levels XAUUSD.x buy", "live backtest-skip flag", "live SIGNAL",
                   "skew="):
        assert needle in text, needle
    assert FAKE_PASSWORD not in text and FAKE_LOGIN not in text
