"""Strategy ABC contract, closed-bar guard, and registry (with a trivial test double)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import pandas as pd
import pytest

from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategy.base import (
    LookAheadError,
    Strategy,
    StrategyContext,
    StrategyContractError,
    assert_closed_bars,
    decision_time_for,
    evaluate_checked,
    slice_closed_bars,
)
from alpha_engine.strategy.params import ParamSchema, ParamSpec, params_hash
from alpha_engine.strategy.registry import (
    DuplicateStrategyError,
    InvalidStrategyError,
    StrategyRegistry,
    UnknownStrategyError,
)
from alpha_engine.strategy.signal import SignalCandidate

UTC = timezone.utc
H1_START = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)  # Monday 00:00 UTC


def _bars(start: datetime, count: int, hours: int, *, as_index: bool = False) -> pd.DataFrame:
    times = pd.date_range(start, periods=count, freq=f"{hours}h", tz="UTC")
    close = [2000.0 + i for i in range(count)]
    frame = pd.DataFrame({
        "time": times, "open": close, "high": [c + 2 for c in close], "low": [c - 2 for c in close], "close": close,
    })
    return frame.set_index("time") if as_index else frame


SCHEMA = ParamSchema([ParamSpec(name="threshold", type="float", default=2010.0, label_fa="آستانه")])
DEFAULT_HASH = params_hash(SCHEMA.defaults())


class BuyAboveThreshold(Strategy):
    """Test double: buy when the last closed H1 close exceeds ``threshold``."""

    name: ClassVar[str] = "test_double"
    version: ClassVar[int] = 3
    title_fa: ClassVar[str] = "استراتژی آزمایشی"
    param_schema: ClassVar[ParamSchema] = SCHEMA

    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        if ctx.has_open_trade:
            return None
        last = ctx.h1.iloc[-1]
        if last["close"] <= ctx.params["threshold"]:
            return None
        open_time = pd.Timestamp(last["time"]).to_pydatetime()
        return SignalCandidate(
            strategy_name=self.name, strategy_version=self.version, params_hash=params_hash(ctx.params),
            symbol=ctx.symbol, direction="buy", setup="bounce_lower", line="lower",
            decision_time_utc=open_time + timedelta(hours=1), confirmation_bar_open_utc=open_time,
            reference_price=float(last["close"]), stop_loss=float(last["low"]) - 1.0,
            rr=ctx.account.rr, pattern="pin_bar", reason_fa="آزمایشی", extra={"close": float(last["close"])},
        )


def _ctx(h1_count: int = 24, *, has_open_trade: bool = False, h4: pd.DataFrame | None = None) -> StrategyContext:
    h1 = _bars(H1_START, h1_count, 1)
    decision = decision_time_for(h1)
    h4_full = _bars(H1_START, 12, 4)
    return StrategyContext(
        symbol="XAUUSD.x",
        h1=h1,
        h4=h4 if h4 is not None else slice_closed_bars(h4_full, timedelta(hours=4), decision),
        account=AccountSettings(),
        params=SCHEMA.defaults(),
        has_open_trade=has_open_trade,
    )


# --- decision time and closed-bar slicing ---------------------------------------------------------

def test_decision_time_is_close_of_last_h1_bar() -> None:
    ctx = _ctx(24)  # last H1 opens 23:00 -> decision 2026-09-22 00:00
    assert ctx.decision_time_utc == pd.Timestamp("2026-09-22 00:00", tz="UTC")


def test_slice_closed_h4_worked_example() -> None:
    # Decision at 10:00: H4 bars 00:00 (closes 04:00) and 04:00 (closes 08:00) are closed;
    # 08:00 closes at 12:00 -> excluded even though it opened before 10:00.
    h4 = _bars(H1_START, 6, 4)
    decision = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    closed = slice_closed_bars(h4, timedelta(hours=4), decision)
    assert list(closed["time"].dt.hour) == [0, 4]
    # Exactly at a close boundary the bar is closed (08:00 open + 4h == 12:00).
    closed = slice_closed_bars(h4, timedelta(hours=4), datetime(2026, 9, 21, 12, 0, tzinfo=UTC))
    assert list(closed["time"].dt.hour) == [0, 4, 8]


def test_assert_closed_bars_passes_for_closed_frames() -> None:
    ctx = _ctx(24)
    assert_closed_bars(ctx, ctx.decision_time_utc)
    assert len(ctx.h4) == 6  # all six H4 bars of Monday are closed by Tuesday 00:00


def test_assert_closed_bars_raises_for_forming_h1_bar() -> None:
    ctx = _ctx(24)
    with pytest.raises(LookAheadError, match="H1"):
        assert_closed_bars(ctx, ctx.decision_time_utc - timedelta(minutes=1))


def test_assert_closed_bars_raises_for_forming_h4_bar() -> None:
    h1 = _bars(H1_START, 10, 1)  # decision at 10:00
    forming_h4 = _bars(H1_START, 3, 4)  # includes the 08:00 bar, closing at 12:00
    ctx = StrategyContext(symbol="XAUUSD.x", h1=h1, h4=forming_h4, account=AccountSettings(),
                          params=SCHEMA.defaults())
    with pytest.raises(LookAheadError, match="H4"):
        assert_closed_bars(ctx, ctx.decision_time_utc)
    with pytest.raises(LookAheadError):
        evaluate_checked(BuyAboveThreshold(), ctx)


def test_datetimeindex_frames_supported() -> None:
    h1 = _bars(H1_START, 5, 1, as_index=True)
    assert decision_time_for(h1) == pd.Timestamp("2026-09-21 05:00", tz="UTC")


def test_naive_times_rejected() -> None:
    h1 = _bars(H1_START, 5, 1)
    h1["time"] = h1["time"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        decision_time_for(h1)
    with pytest.raises(ValueError):
        assert_closed_bars(_ctx(), datetime(2026, 9, 22))


def test_non_utc_aware_times_converted_exactly() -> None:
    h1 = _bars(H1_START, 5, 1)
    h1["time"] = h1["time"].dt.tz_convert("Asia/Tehran")
    assert decision_time_for(h1) == pd.Timestamp("2026-09-21 05:00", tz="UTC")


# --- Strategy ABC and evaluate_checked ------------------------------------------------------------

def test_abc_cannot_instantiate_without_evaluate() -> None:
    class Incomplete(Strategy):
        name = "incomplete"
        version = 1
        title_fa = "ناقص"
        param_schema = SCHEMA

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_evaluate_checked_returns_signal() -> None:
    ctx = _ctx(24)  # last close = 2023 > 2010
    sig = evaluate_checked(BuyAboveThreshold(), ctx, params_hash=DEFAULT_HASH)
    assert isinstance(sig, SignalCandidate)
    assert sig.decision_time_utc == datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
    assert sig.reference_price == 2023.0 and sig.stop_loss == 2020.0
    assert sig.indicative_take_profit == 2023.0 + 2.0 * 3.0


def test_evaluate_checked_no_setup() -> None:
    assert evaluate_checked(BuyAboveThreshold(), _ctx(5)) is None  # last close 2004 <= 2010


def test_has_open_trade_short_circuits() -> None:
    class Exploding(BuyAboveThreshold):
        def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
            raise AssertionError("must not be called while a trade is open")

    assert evaluate_checked(Exploding(), _ctx(24, has_open_trade=True)) is None
    # and the double itself honours the contract when called directly
    assert BuyAboveThreshold().evaluate(_ctx(24, has_open_trade=True)) is None


def test_prepare_default_is_empty() -> None:
    assert BuyAboveThreshold().prepare(_bars(H1_START, 3, 1), _bars(H1_START, 1, 4), {}) == {}


def test_deterministic_and_no_change_when_future_bars_exist() -> None:
    full_h1 = _bars(H1_START, 48, 1)
    ctx_a = _ctx(24)
    first = evaluate_checked(BuyAboveThreshold(), ctx_a)
    assert first == evaluate_checked(BuyAboveThreshold(), ctx_a)
    # Build the same context from a longer history via the helper: identical result.
    decision = ctx_a.decision_time_utc
    ctx_b = StrategyContext(
        symbol="XAUUSD.x",
        h1=slice_closed_bars(full_h1, timedelta(hours=1), decision),
        h4=slice_closed_bars(_bars(H1_START, 12, 4), timedelta(hours=4), decision),
        account=AccountSettings(), params=SCHEMA.defaults(),
    )
    assert evaluate_checked(BuyAboveThreshold(), ctx_b) == first


@pytest.mark.parametrize("bad", ["not a signal", 42])
def test_contract_error_on_wrong_return_type(bad: Any) -> None:
    class Bad(BuyAboveThreshold):
        def evaluate(self, ctx: StrategyContext) -> Any:
            return bad

    with pytest.raises(StrategyContractError):
        evaluate_checked(Bad(), _ctx(24))


def test_contract_error_on_mismatched_signal() -> None:
    class WrongTime(BuyAboveThreshold):
        def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
            sig = super().evaluate(ctx)
            assert sig is not None
            earlier = sig.confirmation_bar_open_utc - timedelta(hours=1)
            return sig.model_copy(update={"confirmation_bar_open_utc": earlier,
                                          "decision_time_utc": earlier + timedelta(hours=1)})

    with pytest.raises(StrategyContractError, match="decision_time"):
        evaluate_checked(WrongTime(), _ctx(24))
    with pytest.raises(StrategyContractError, match="params_hash"):
        evaluate_checked(BuyAboveThreshold(), _ctx(24), params_hash="b" * 64)

    class WrongVersion(BuyAboveThreshold):
        version = 4

        def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
            sig = super().evaluate(ctx)
            assert sig is not None
            return sig.model_copy(update={"strategy_version": 3})

    with pytest.raises(StrategyContractError, match="version"):
        evaluate_checked(WrongVersion(), _ctx(24))


# --- registry -------------------------------------------------------------------------------------

def test_registry_register_get_all() -> None:
    reg = StrategyRegistry()
    assert reg.register(BuyAboveThreshold) is BuyAboveThreshold
    assert reg.register(BuyAboveThreshold) is BuyAboveThreshold  # same class again: no-op
    assert reg.get("test_double") is BuyAboveThreshold
    assert reg.all() == [BuyAboveThreshold]
    assert reg.names() == ["test_double"] and "test_double" in reg and len(reg) == 1


def test_registry_duplicate_name_rejected() -> None:
    reg = StrategyRegistry()
    reg.register(BuyAboveThreshold)

    class Clash(BuyAboveThreshold):
        pass

    with pytest.raises(DuplicateStrategyError):
        reg.register(Clash)


def test_registry_unknown_name() -> None:
    with pytest.raises(UnknownStrategyError) as info:
        StrategyRegistry().get("nope")
    assert isinstance(info.value, KeyError) and "nope" in str(info.value)


@pytest.mark.parametrize(
    "attrs",
    [
        {"name": "Bad Name"},
        {"version": 0},
        {"version": True},
        {"title_fa": " "},
        {"param_schema": [SCHEMA]},
    ],
)
def test_registry_rejects_invalid_classes(attrs: dict[str, Any]) -> None:
    cls = type("Broken", (BuyAboveThreshold,), attrs)
    with pytest.raises(InvalidStrategyError):
        StrategyRegistry().register(cls)


def test_registry_rejects_abstract_and_non_strategy() -> None:
    reg = StrategyRegistry()
    with pytest.raises(InvalidStrategyError):
        reg.register(Strategy)
    with pytest.raises(InvalidStrategyError):
        reg.register(int)  # type: ignore[arg-type]


def test_default_registry_has_no_concrete_strategies_yet() -> None:
    from alpha_engine.strategy import registry

    assert "test_double" not in registry.default_registry
