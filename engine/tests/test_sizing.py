"""Position sizing worked examples (risk/sizing.py docstring) -- exact numbers.

balance 1000, risk 1 % -> 10.00 USD; leverage 100; volume_step 0.01, volume_min 0.01, volume_max 100.
"""

from __future__ import annotations

import math

import pytest

from alpha_engine.risk import SizingResult, floor_to_step, size_for_account, size_position, step_decimals
from alpha_engine.storage.account_settings import AccountSettings

COMMON = dict(balance=1000.0, risk_pct=1.0, volume_min=0.01, volume_step=0.01, volume_max=100.0, leverage=100)
GOLD = dict(COMMON, tick_value=1.0, tick_size=0.01, contract_size=100.0)
BRENT = dict(COMMON, tick_value=10.0, tick_size=0.01, contract_size=1000.0)


def test_gold_worked_example() -> None:
    r = size_position(entry=2000.00, stop_loss=1995.00, **GOLD)
    assert r.accepted and r.reason_fa is None and r.warnings == []
    assert r.risk_amount == 10.0
    assert r.value_per_unit == 100.0
    assert r.loss_per_lot == 500.0
    assert r.raw_volume == 0.02
    assert r.volume == 0.02
    assert r.actual_risk == 10.0
    assert r.margin == 40.0  # 0.02 * 100 * 2000 / 100


def test_brent_worked_example() -> None:
    r = size_position(entry=80.00, stop_loss=79.50, **BRENT)
    assert r.accepted and r.warnings == []
    assert r.value_per_unit == 1000.0
    assert r.loss_per_lot == 500.0
    assert r.volume == 0.02
    assert r.actual_risk == 10.0
    assert r.margin == 16.0  # 0.02 * 1000 * 80 / 100


def test_gold_too_wide_stop_is_rejected() -> None:
    r = size_position(entry=2000.00, stop_loss=1940.00, **GOLD)  # 60.00 away
    assert not r.accepted
    assert r.loss_per_lot == 6000.0
    assert r.raw_volume == pytest.approx(0.0016666667, abs=1e-9)
    assert r.volume == 0.0
    assert r.actual_risk == 0.0 and r.margin == 0.0
    assert "کمتر از حداقل حجم" in r.reason_fa and "0.01" in r.reason_fa


def test_sell_side_uses_absolute_distance() -> None:
    r = size_position(entry=1995.00, stop_loss=2000.00, **GOLD)
    assert r.accepted and r.volume == 0.02 and r.loss_per_lot == 500.0


@pytest.mark.parametrize(
    ("raw", "step", "expected"),
    [
        (0.0299999, 0.01, 0.02),   # floor, not round
        (0.03, 0.01, 0.03),        # float 0.0299999999999999989 -> 0.03 (epsilon absorbs representation error)
        (0.29, 0.01, 0.29),        # 0.29 / 0.01 = 28.999999999999996
        (0.3, 0.1, 0.3),           # 0.3 / 0.1 = 2.9999999999999996
        (0.0149, 0.01, 0.01),
        (0.0099, 0.01, 0.0),
        (1.37, 1.0, 1.0),
        (0.1234, 0.001, 0.123),
    ],
)
def test_floor_to_step(raw: float, step: float, expected: float) -> None:
    assert floor_to_step(raw, step) == expected


def test_float_representation_cases_are_real() -> None:
    assert 0.29 / 0.01 < 29 and 0.3 / 0.1 < 3  # the reason for the 1e-9 step epsilon
    assert repr(floor_to_step(0.07, 0.01)) == "0.07"  # rounded to the step's decimals, no 0.07000000000000001


def test_raw_volume_just_below_a_step_through_the_formula() -> None:
    # risk 10, loss/lot 10 / 0.0299999 -> raw 0.0299999 -> 0.02
    r = size_position(entry=2000.0, stop_loss=2000.0 - 10.0 / 0.0299999 / 100.0, **GOLD)
    assert r.raw_volume == pytest.approx(0.0299999, rel=1e-12)
    assert r.volume == 0.02 and r.actual_risk == pytest.approx(0.02 * r.loss_per_lot, rel=1e-15)
    assert r.actual_risk < r.risk_amount


@pytest.mark.parametrize(("step", "decimals"), [(0.01, 2), (0.1, 1), (1.0, 0), (0.001, 3), (10.0, 0), (0.05, 2)])
def test_step_decimals(step: float, decimals: int) -> None:
    assert step_decimals(step) == decimals


def test_volume_max_caps_with_warning() -> None:
    r = size_position(entry=2000.0, stop_loss=1999.99, **{**GOLD, "volume_max": 5.0, "balance": 1e6})
    # risk 10000, loss/lot 0.01 * 100 = 1 -> raw 10000 lots -> capped at 5.00
    assert r.raw_volume == pytest.approx(10000.0, rel=1e-9)
    assert r.accepted and r.volume == 5.0
    assert any("حداکثر حجم" in w for w in r.warnings)
    assert r.actual_risk == pytest.approx(5.0, rel=1e-9)


def test_margin_above_balance_is_rejected() -> None:
    # Tight stop: 0.10 away -> loss/lot 10 -> raw 1.00 lot -> margin 1 * 100 * 2000 / 100 = 2000 > 1000.
    r = size_position(entry=2000.0, stop_loss=1999.90, **GOLD)
    assert r.volume == 1.0
    assert r.margin == pytest.approx(2000.0, rel=1e-12)
    assert not r.accepted and "مارجین" in r.reason_fa


def test_contract_size_cross_check_warning() -> None:
    # Within 1 %: no warning (value per unit 100.5 vs contract 100).
    ok = size_position(entry=2000.0, stop_loss=1995.0, **{**GOLD, "tick_value": 1.005})
    assert ok.warnings == [] and ok.accepted
    # Quote currency != account currency: tick 0.00001 worth 1.2 -> 120000 per unit vs contract 100000.
    # loss/lot = 0.005 * 120000 = 600 -> raw 0.0166667 -> 0.01; margin 0.01 * 100000 * 1.1 / 100 = 11.
    fx = size_position(entry=1.10000, stop_loss=1.09500, **{**COMMON, "tick_value": 1.2, "tick_size": 0.00001,
                                                             "contract_size": 100000.0})
    assert fx.value_per_unit == pytest.approx(120000.0, rel=1e-12)
    assert fx.loss_per_lot == pytest.approx(600.0, rel=1e-9)
    assert fx.volume == 0.01 and fx.margin == pytest.approx(11.0, rel=1e-9)
    assert fx.accepted  # a warning, not a rejection
    assert len(fx.warnings) == 1 and "اندازه قرارداد" in fx.warnings[0]


def test_jpy_like_symbol_margin_rejection_keeps_the_warning() -> None:
    # tick 0.001 worth 0.64 USD -> 640 per unit; SL 0.5 away -> loss/lot 320 -> raw 0.03125 -> 0.03.
    # margin 0.03 * 100000 * 160 / 100 = 4800 > balance 1000 -> rejected.
    jpy = size_position(entry=160.000, stop_loss=159.500, **{**COMMON, "tick_value": 0.64, "tick_size": 0.001,
                                                             "contract_size": 100000.0})
    assert jpy.value_per_unit == pytest.approx(640.0, rel=1e-12)
    assert jpy.loss_per_lot == pytest.approx(320.0, rel=1e-12)
    assert jpy.volume == 0.03
    assert jpy.margin == pytest.approx(4800.0, rel=1e-12)
    assert not jpy.accepted and "مارجین" in jpy.reason_fa
    assert any("اندازه قرارداد" in w for w in jpy.warnings)


@pytest.mark.parametrize(
    "override",
    [
        {"tick_value": 0.0}, {"tick_size": 0.0}, {"tick_value": math.nan}, {"volume_step": -0.01},
        {"volume_min": 0.0}, {"contract_size": math.inf}, {"leverage": 0}, {"balance": True}, {"risk_pct": "1"},
    ],
)
def test_invalid_inputs_are_rejected_not_raised(override: dict) -> None:
    r = size_position(entry=2000.0, stop_loss=1995.0, **{**GOLD, **override})
    assert not r.accepted and r.volume == 0.0 and "نامعتبر" in r.reason_fa


def test_zero_distance_and_min_above_max_rejected() -> None:
    assert not size_position(entry=2000.0, stop_loss=2000.0, **GOLD).accepted
    r = size_position(entry=2000.0, stop_loss=1995.0, **{**GOLD, "volume_min": 2.0, "volume_max": 1.0})
    assert not r.accepted and "حداقل حجم" in r.reason_fa


def test_size_for_account_uses_account_settings() -> None:
    r = size_for_account(AccountSettings(), entry=2000.0, stop_loss=1995.0, tick_value=1.0, tick_size=0.01,
                         volume_min=0.01, volume_step=0.01, volume_max=100.0, contract_size=100.0)
    assert r == size_position(entry=2000.0, stop_loss=1995.0, **GOLD)
    r2 = size_for_account(AccountSettings(balance=5000, risk_pct=2.0, leverage=50), entry=2000.0,
                          stop_loss=1995.0, tick_value=1.0, tick_size=0.01, volume_min=0.01, volume_step=0.01,
                          volume_max=100.0, contract_size=100.0)
    assert r2.risk_amount == 100.0 and r2.volume == 0.2 and r2.margin == pytest.approx(800.0, rel=1e-12)


def test_result_is_immutable_model() -> None:
    r = size_position(entry=2000.0, stop_loss=1995.0, **GOLD)
    assert isinstance(r, SizingResult)
    with pytest.raises(Exception):
        r.volume = 1.0  # type: ignore[misc]
