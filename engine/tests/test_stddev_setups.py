"""StdDev-channel setups on deterministic fixture scenarios (tests/fixtures/channel_scenarios.py).

Channels (n = 100 H4 bars): up (slope ~ +1.0 / H4 bar), down (~ -1.0), flat (~ +0.02); sigma ~ 5,
bands ~ mid +/- 10, ATR_H4 ~ 12. Flat test: |slope| * 100 ~ 100 vs 12 (up/down), ~ 2.3 vs 12 (flat).
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from alpha_engine.logging_setup import configure_logging
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import (
    SCHEMA,
    InvalidParamsError,
    StdDevChannelStrategy,
    resolve_params,
)
from alpha_engine.strategies.stddev_channel.setups import compute_channel_bars, touch_offset
from alpha_engine.strategies.stddev_channel.state_machine import PRIORITY, decide_at
from alpha_engine.strategy.base import evaluate_checked
from alpha_engine.strategy.params import params_hash
from alpha_engine.strategy.registry import default_registry
from alpha_engine.strategy.signal import Setup
from fixtures.channel_scenarios import (
    BG,
    BREAKOUT,
    DEFAULT_T,
    EXPECTED_SETUP,
    HAMMER,
    KINDS,
    SYMBOL,
    allowed,
    base_scenario,
    build,
    conflict_scenario,
)

STRATEGY = StdDevChannelStrategy()
T = DEFAULT_T
DEFAULT_HASH = params_hash(SCHEMA.defaults())


def run(sc, **ctx_kw):
    return evaluate_checked(STRATEGY, sc.ctx(**ctx_kw), params_hash(resolve_params(sc.params)[1]))


# ------------------------------------------------------------------------------ channel direction / flat
def test_fixture_channels_have_the_designed_direction() -> None:
    for trend, direction in (("up", "up"), ("down", "down"), ("flat", "flat")):
        cb = base_scenario(trend, "buy").lines
        assert cb.channel_direction(T) == direction
        change = abs(cb.slope[T]) * 100
        threshold = cb.atr_h4[T] * 1.0
        assert bool(cb.is_flat[T]) == (change < threshold)
        assert (cb.upper[T] - cb.mid[T]) == pytest.approx(2.0 * cb.sigma[T], rel=1e-12)
    up = base_scenario("up", "buy").lines
    assert 95 < abs(up.slope[T]) * 100 < 105 and 10 < up.atr_h4[T] < 14
    flat = base_scenario("flat", "buy").lines
    assert abs(flat.slope[T]) * 100 < 3


def test_direction_filter_arrays() -> None:
    up, down, flat = (base_scenario(tr, "buy").lines for tr in ("up", "down", "flat"))
    assert up.buy_ok[T] and not up.sell_ok[T]
    assert down.sell_ok[T] and not down.buy_ok[T]
    assert flat.buy_ok[T] and flat.sell_ok[T]
    # Before 100 closed H4 bars there is no channel: nothing is tradable.
    assert not up.valid[398] and up.valid[399]
    assert not (up.buy_ok[:399].any() or up.sell_ok[:399].any())


@pytest.mark.parametrize("trend", ["up", "down", "flat"])
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind", KINDS)
def test_setup_grid_found_or_filtered(trend: str, side: str, kind: str) -> None:
    """Every setup fires in the channels whose direction allows it and is filtered otherwise."""
    sc = build(trend, side, kind)
    cand = run(sc)
    cb = sc.bars()
    has_pattern = cb.bull[T] if side == "buy" else cb.bear[T]
    assert has_pattern  # the confirmation candle is there in every case
    if allowed(trend, side):
        assert cand is not None
        assert cand.setup.value == EXPECTED_SETUP[(kind, side)]
        assert cand.direction == side
        assert cand.extra["channel_direction"] == trend
    else:
        assert cand is None
        assert not (cb.buy_ok[T] if side == "buy" else cb.sell_ok[T])  # filtered by direction only


def test_flat_mult_param_changes_the_filter() -> None:
    # Up channel: change ~100.3 vs ATR_H4 ~12.04 -> with flat_mult 9 the threshold is ~108 -> flat.
    sc = build("up", "sell", "bounce_outer", params={"flat_mult": 9.0})
    cand = run(sc)
    assert cand is not None and cand.setup is Setup.BOUNCE_UPPER and cand.extra["channel_direction"] == "flat"
    assert cand.extra["channel_change_abs"] < cand.extra["flat_threshold"]
    assert cand.extra["flat_threshold"] == pytest.approx(cand.extra["atr_h4"] * 9.0, rel=1e-15)
    # Flat channel with flat_mult 0: never flat -> slope +0.023 makes it an up channel (buy only).
    assert run(build("flat", "sell", "bounce_outer", params={"flat_mult": 0.0})) is None
    buy = run(build("flat", "buy", "bounce_outer", params={"flat_mult": 0.0}))
    assert buy is not None and buy.extra["channel_direction"] == "up"


# ------------------------------------------------------------------------------ bounces
def test_bounce_with_touch_one_bar_before_confirmation() -> None:
    cand = run(build("up", "buy", "bounce_outer", touch_at=1))
    assert cand is not None and cand.setup is Setup.BOUNCE_LOWER and cand.extra["touch_offset"] == 1


def test_bounce_touch_too_early_is_ignored_unless_lookback_allows() -> None:
    assert run(build("up", "buy", "bounce_outer", touch_at=2)) is None
    cand = run(build("up", "buy", "bounce_outer", touch_at=2, params={"touch_lookback": 2}))
    assert cand is not None and cand.extra["touch_offset"] == 2
    # touch_lookback 0: only the confirmation bar itself may touch.
    assert run(build("up", "buy", "bounce_outer", touch_at=1, params={"touch_lookback": 0})) is None


@pytest.mark.parametrize(("trend", "side"), [("up", "buy"), ("down", "sell"), ("flat", "buy"), ("flat", "sell")])
@pytest.mark.parametrize("kind", KINDS)
def test_no_confirmation_no_setup(trend: str, side: str, kind: str) -> None:
    sc = build(trend, side, kind, confirm=False)
    cb = sc.bars()
    assert not cb.bull[T] and not cb.bear[T]
    assert run(sc) is None


@pytest.mark.parametrize(("trend", "side"), [("up", "buy"), ("down", "sell"), ("flat", "buy"), ("flat", "sell")])
@pytest.mark.parametrize("kind", ["bounce_outer", "bounce_mid"])
def test_confirmation_that_closed_through_the_line_is_rejected(trend: str, side: str, kind: str) -> None:
    sc = build(trend, side, kind, closed_through=True)
    cb = sc.bars()
    assert (cb.bull[T] if side == "buy" else cb.bear[T])  # a valid reversal candle ...
    line = cb.line(sc.support() if kind == "bounce_outer" else "mid")
    beyond = (line[T] - cb.close[T]) if side == "buy" else (cb.close[T] - line[T])
    assert beyond > cb.tol[T]  # ... that closed more than tol through the line
    assert run(sc) is None


def test_mid_bounce_requires_the_approach_side() -> None:
    # Up channel, price below the mid line, bullish hammer at mid: "from above" is not satisfied.
    sc = base_scenario("up", "buy")
    sc.background("mid", -5.0)
    sc.put_rel(T, "mid", 0.0, HAMMER)
    cb = sc.bars()
    assert cb.bull[T] and cb.buy_ok[T]
    assert cb.close[T - 2] < cb.mid[T - 2] + cb.tol[T - 2]
    assert run(sc) is None
    # Same candle, price came from above -> bounce_mid.
    cand = run(build("up", "buy", "bounce_mid"))
    assert cand is not None and cand.setup is Setup.BOUNCE_MID
    assert cand.extra["approach_close"] > cand.extra["approach_mid"]


def test_approach_lookback_param_picks_the_reference_bar() -> None:
    sc = build("up", "buy", "bounce_mid")
    # t-2 and t-1 below mid (no upward mid breakout before t); t-3 still above.
    sc.put_rel(T - 2, "mid", -5.0, BG)
    sc.put_rel(T - 1, "mid", -5.0, BG)
    assert run(sc) is None
    sc.params = {**sc.params, "approach_lookback": 3}
    cand = run(sc)
    assert cand is not None and cand.setup is Setup.BOUNCE_MID
    assert cand.extra["approach_bar_open_utc"] == sc.h1["time"].iloc[T - 3].strftime("%Y-%m-%d %H:%M UTC")


def test_bounce_upper_in_up_channel_and_lower_breakout_are_filtered() -> None:
    assert run(build("up", "sell", "bounce_outer")) is None      # upper-line bounce in an up channel
    assert run(build("up", "sell", "breakout_outer")) is None    # lower-line breakout in an up channel
    assert run(build("down", "buy", "bounce_outer")) is None
    assert run(build("down", "buy", "breakout_outer")) is None


# ------------------------------------------------------------------------------ breakout -> pullback
@pytest.mark.parametrize(("trend", "side"), [("up", "buy"), ("down", "sell")])
@pytest.mark.parametrize("kind", ["breakout_outer", "breakout_mid"])
def test_pullback_window_boundary(trend: str, side: str, kind: str) -> None:
    ok = run(build(trend, side, kind, bars_after_breakout=12))
    assert ok is not None and ok.setup.value == EXPECTED_SETUP[(kind, side)]
    assert ok.extra["bars_since_breakout"] == 12 and ok.extra["pullback_window"] == 12
    late = run(build(trend, side, kind, bars_after_breakout=13))  # M bars passed -> breakout cancelled
    if kind == "breakout_outer":
        assert late is None
    else:  # price still came to the mid line from the breakout side: only the plain mid bounce remains
        assert late is not None and late.setup is Setup.BOUNCE_MID and "breakout_bar_open_utc" not in late.extra


def test_pullback_window_param() -> None:
    assert run(build("up", "buy", "breakout_outer", bars_after_breakout=5, params={"pullback_window": 5})) is not None
    assert run(build("up", "buy", "breakout_outer", bars_after_breakout=6, params={"pullback_window": 5})) is None


@pytest.mark.parametrize(("trend", "side"), [("up", "buy"), ("down", "sell"), ("flat", "buy"), ("flat", "sell")])
@pytest.mark.parametrize("kind", ["breakout_outer", "breakout_mid"])
def test_failed_breakout_cancels(trend: str, side: str, kind: str) -> None:
    assert run(build(trend, side, kind)) is not None
    sc = build(trend, side, kind, bars_after_breakout=6, fail_at=2)
    cb = sc.bars()
    line = cb.line(sc.far() if kind == "breakout_outer" else "mid")
    f = T - 6 + 2
    back = (line[f] - cb.close[f]) if side == "buy" else (cb.close[f] - line[f])
    assert back == pytest.approx(1.0, abs=1e-9) and back > cb.tol[f]
    assert run(sc) is None


@pytest.mark.parametrize(("trend", "side"), [("up", "buy"), ("down", "sell")])
def test_close_back_inside_tolerance_does_not_cancel(trend: str, side: str) -> None:
    sc = build(trend, side, "breakout_outer", dip_within_tol=True)
    cb = sc.bars()
    line = cb.line(sc.far())
    back = (line[T - 1] - cb.close[T - 1]) if side == "buy" else (cb.close[T - 1] - line[T - 1])
    assert 0 < back < cb.tol[T - 1]  # closed back through the line, but within tol
    cand = run(sc)
    assert cand is not None and cand.extra["bars_since_breakout"] == 4


def test_latest_breakout_wins_after_a_failed_one() -> None:
    sc = base_scenario("up", "buy")
    sc.background("mid", 5.0)
    b1, b2 = T - 10, T - 4
    sc.put_rel(b1, "upper", 0.0, BREAKOUT)
    sc.put_rel(b1 + 1, "upper", -1.25, BG)  # failed (close 1.0 below the line)
    for i in range(b1 + 2, b2):
        sc.put_rel(i, "upper", -2.0, BG)
    sc.put_rel(b2, "upper", 0.0, BREAKOUT)
    for i in range(b2 + 1, T):
        sc.put_rel(i, "upper", 2.0, BG)
    sc.put_rel(T, "upper", 0.0, HAMMER)
    cand = run(sc)
    assert cand is not None and cand.setup is Setup.BREAKOUT_UPPER_PULLBACK
    assert cand.extra["bars_since_breakout"] == 4


def test_breakout_bar_itself_is_not_the_pullback() -> None:
    sc = base_scenario("up", "buy")
    sc.background("mid", 5.0)
    sc.put_rel(T - 1, "upper", 0.0, BREAKOUT)  # spans the line
    sc.put_rel(T, "upper", 1.0, HAMMER)        # bullish, but does not touch
    cb = sc.bars()
    assert touch_offset(cb, "upper", T, 1) == 1  # only the breakout bar touches
    assert run(sc) is None


@pytest.mark.parametrize(("trend", "side"), [("up", "buy"), ("down", "sell")])
@pytest.mark.parametrize("kind", ["breakout_outer", "breakout_mid"])
def test_breakout_without_confirmation(trend: str, side: str, kind: str) -> None:
    assert run(build(trend, side, kind, confirm=False)) is None


# ------------------------------------------------------------------------------ priority / conflict / state
def test_priority_order_constant() -> None:
    assert PRIORITY == (
        Setup.BREAKOUT_UPPER_PULLBACK, Setup.BREAKOUT_LOWER_PULLBACK, Setup.BREAKOUT_MID_PULLBACK,
        Setup.BOUNCE_LOWER, Setup.BOUNCE_UPPER, Setup.BOUNCE_MID,
    )


def test_one_candidate_by_priority_with_reason() -> None:
    sc = build("up", "buy", "breakout_mid")
    p, _ = resolve_params(sc.params)
    decision = decide_at(compute_channel_bars(*sc.prefix(), p), T, p)
    assert [(h.setup, h.side) for h in decision.hits] == [
        (Setup.BREAKOUT_MID_PULLBACK, "buy"), (Setup.BOUNCE_MID, "buy"),
    ]
    cand = run(sc)
    assert cand.setup is Setup.BREAKOUT_MID_PULLBACK
    assert cand.extra["other_setups"] == "bounce_mid:buy" and cand.extra["priority_rank"] == 3
    assert "کنار گذاشته شدند" in cand.reason_fa and "«برگشت از خط میانی»" in cand.reason_fa


def test_direction_conflict_gives_no_candidate() -> None:
    sc = conflict_scenario()
    p, _ = resolve_params(sc.params)
    cb = compute_channel_bars(*sc.prefix(), p)
    assert cb.bull[T] and cb.bear[T] and cb.is_flat[T]
    decision = decide_at(cb, T, p)
    assert decision.note == "direction_conflict" and decision.chosen is None
    assert {h.side for h in decision.hits} == {"buy", "sell"}
    assert run(sc) is None


def test_has_open_trade_returns_none() -> None:
    sc = build("up", "buy", "bounce_outer")
    assert run(sc) is not None
    assert STRATEGY.evaluate(sc.ctx(has_open_trade=True)) is None
    assert evaluate_checked(STRATEGY, sc.ctx(has_open_trade=True)) is None


def test_evaluate_is_stateless_and_deterministic() -> None:
    a = build("up", "buy", "breakout_outer")
    b = build("down", "sell", "bounce_mid")
    first = STRATEGY.evaluate(a.ctx())
    STRATEGY.evaluate(b.ctx())
    assert STRATEGY.evaluate(a.ctx()) == first
    assert StdDevChannelStrategy().evaluate(a.ctx()) == first
    assert vars(STRATEGY) == {}  # no instance state at all


# ------------------------------------------------------------------------------ candidate content
def test_candidate_fields_reason_and_extra() -> None:
    sc = build("up", "buy", "bounce_outer")
    account = AccountSettings(rr=2.5)
    cand = evaluate_checked(STRATEGY, sc.ctx(account=account), DEFAULT_HASH)
    open_t = sc.h1["time"].iloc[T]
    assert cand.strategy_name == "stddev_channel" and cand.strategy_version == 1 and cand.symbol == SYMBOL
    assert cand.confirmation_bar_open_utc == open_t and cand.decision_time_utc == open_t + timedelta(hours=1)
    assert cand.reference_price == sc.h1["close"].iloc[T] and cand.rr == 2.5
    assert cand.line == "lower" and cand.pattern == "pin_bar"
    for key in ("line_value", "upper", "mid", "lower", "slope_per_h4_bar", "sigma", "atr_h4", "is_flat",
                "atr_h1", "touch_tol", "sl_buffer", "h4_bar_open_utc", "bars_ahead", "bar_low", "bar_close"):
        assert key in cand.extra
    assert cand.extra["line_value"] == cand.extra["lower"]
    for word in ("خرید", "«برگشت از خط پایین»", "کانال صعودی", "پین‌بار صعودی", "حد ضرر", "ورود با open کندل H1 بعدی"):
        assert word in cand.reason_fa
    assert f"{cand.stop_loss:.5f}".rstrip("0").rstrip(".") in cand.reason_fa


def test_engulfing_confirmation_and_pattern_params() -> None:
    sc = base_scenario("up", "buy")
    sc.background("mid", -5.0)
    L = sc.line("lower", T)
    sc.set_bar(T - 1, L + 1.5, L + 1.6, L - 0.5, L + 0.3)  # bearish, touches the lower line
    sc.set_bar(T, L + 0.25, L + 1.9, L + 0.2, L + 1.8)     # bullish engulfing (open <= prev close, close >= prev open)
    cand = run(sc)
    assert cand is not None and cand.pattern == "engulfing" and "اینگالف صعودی" in cand.reason_fa
    sc.params = {**sc.params, "use_engulfing": False}
    assert run(sc) is None
    pin = build("up", "buy", "bounce_outer", params={"use_pin_bar": False})
    assert run(pin) is None


# ------------------------------------------------------------------------------ registry and schema
def test_registered_in_default_registry() -> None:
    cls = default_registry.get("stddev_channel")
    assert cls is StdDevChannelStrategy
    assert cls.version == 1 and cls.title_fa == "کانال انحراف معیار"


def test_param_schema_defaults() -> None:
    assert SCHEMA.defaults() == {
        "n": 100, "k": 2.0, "sigma_ddof": 0, "flat_mult": 1.0, "atr_period": 14, "touch_atr_mult": 0.1,
        "sl_atr_mult": 0.2, "pullback_window": 12, "touch_lookback": 1, "approach_lookback": 2,
        "pin_wick_body_ratio": 2.0, "pin_opposite_wick_max": 0.5, "use_pin_bar": True, "use_engulfing": True,
        "projection_mode": "bar_count",
    }
    assert all(spec.label_fa for spec in SCHEMA)
    p, clean = resolve_params(None)
    assert clean == SCHEMA.defaults() and p.history_bars == 14


@pytest.mark.parametrize(
    "bad",
    [
        {"n": 9}, {"n": 501}, {"n": 100.5}, {"k": 0.4}, {"k": 5.1}, {"sigma_ddof": 2}, {"flat_mult": -0.1},
        {"atr_period": 1}, {"touch_atr_mult": 2.1}, {"sl_atr_mult": 5.5}, {"pullback_window": 0},
        {"touch_lookback": 6}, {"approach_lookback": 0}, {"pin_wick_body_ratio": 0.4},
        {"pin_opposite_wick_max": 2.5}, {"use_pin_bar": 1}, {"projection_mode": "h4_axis"}, {"unknown": 1},
    ],
)
def test_invalid_params_rejected_in_persian(bad: dict) -> None:
    with pytest.raises(InvalidParamsError) as err:
        resolve_params(bad)
    assert err.value.errors_fa and any("پارامتر" in e for e in err.value.errors_fa)
    sc = build("up", "buy", "bounce_outer")
    sc.params = {**sc.params, **bad}
    with pytest.raises(InvalidParamsError):
        STRATEGY.evaluate(sc.ctx())


def test_at_least_one_pattern_required() -> None:
    with pytest.raises(InvalidParamsError, match="الگوهای تایید"):
        resolve_params({"use_pin_bar": False, "use_engulfing": False})


def test_params_are_coerced_and_hashed_after_validation() -> None:
    p, clean = resolve_params({"n": "120", "k": 2, "sigma_ddof": 1.0})
    assert (p.n, p.k, p.sigma_ddof) == (120, 2.0, 1)
    sc = build("up", "buy", "bounce_outer", params={"k": 2})  # int 2 -> 2.0: same hash as the default
    assert run(sc).params_hash == DEFAULT_HASH


# ------------------------------------------------------------------------------ DEV_MODE logging
def test_per_decision_debug_log_only_in_dev_mode(make_settings) -> None:
    sc = build("up", "buy", "bounce_outer")
    stream = io.StringIO()
    configure_logging(make_settings("DEV_MODE=false\n"), stream=stream, force=True)
    STRATEGY.evaluate(sc.ctx())
    assert "decision=" not in stream.getvalue()

    stream = io.StringIO()
    configure_logging(make_settings("DEV_MODE=true\n"), stream=stream, force=True)
    STRATEGY.evaluate(sc.ctx())
    text = stream.getvalue()
    open_t = sc.h1["time"].iloc[T]
    assert f"bar={open_t.strftime('%Y-%m-%d %H:%M')} UTC" in text
    assert "action=buy:bounce_lower" in text and "atr_h1=" in text and "mid=" in text
