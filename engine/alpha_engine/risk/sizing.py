"""Position sizing for suggestions (stddev-channel-system.md section 5). Pure numbers, no MT5, no I/O.

Formula::

    risk_amount     = balance * risk_pct / 100                      (account currency)
    value_per_unit  = tick_value / tick_size                        (money per 1.0 price move per lot)
    loss_per_lot    = |entry - stop_loss| * value_per_unit
    raw_volume      = risk_amount / loss_per_lot
    volume          = floor(raw_volume / volume_step + 1e-9) * volume_step   (DOWN to the step,
                      rounded to the step's decimals to remove float noise)
    volume          = min(volume, volume_max)                        (warning when capped)
    rejected        if volume < volume_min                          (no suggestion, Persian reason)
    actual_risk     = volume * loss_per_lot
    margin          = volume * contract_size * price / leverage     (price defaults to entry)
    rejected        if margin > balance                             (conservative: free margin unknown)
    warning         if |value_per_unit - contract_size| / contract_size > 1 %

``value_per_unit`` comes from ``trade_tick_value / trade_tick_size`` (phase-2 decision): it is correct
for any quote currency because the broker converts ``tick_value`` into the account currency. For
USD-quoted symbols on a USD account it must equal ``trade_contract_size``; a mismatch is reported as a
warning (not a rejection) so odd symbol data is visible.

The ``+ 1e-9`` (in units of steps) only absorbs float representation error, e.g. ``0.03 / 0.01 =
2.9999999999999996``; it can round up a raw volume that is less than 1e-9 steps below a step, i.e.
an over-risk of at most ~1e-9 relative, which is negligible.

Worked examples (balance 1000, risk 1 % -> 10.00 USD, leverage 100, step 0.01, min 0.01):

* Gold (contract 100, tick_size 0.01, tick_value 1.0 -> 100 per unit): entry 2000.00, SL 1995.00
  -> loss/lot 5.00 * 100 = 500.00 -> raw 0.02 -> volume 0.02, actual risk 10.00,
  margin 0.02 * 100 * 2000 / 100 = 40.00.
* Brent (contract 1000, tick_size 0.01, tick_value 10 -> 1000 per unit): entry 80.00, SL 79.50
  -> loss/lot 0.50 * 1000 = 500.00 -> volume 0.02, margin 0.02 * 1000 * 80 / 100 = 16.00.
* Gold with SL 60.00 away -> loss/lot 6000 -> raw 0.0016667 -> 0.00 < 0.01 -> rejected.
* Floor, not round: raw 0.0299999 -> 0.02; raw 0.03 (float 0.0299999999999999989) -> 0.03.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettings

logger = get_logger(__name__)

STEP_EPSILON = 1e-9
CONTRACT_CHECK_REL_TOL = 0.01


class SizingResult(BaseModel):
    """Outcome of :func:`size_position`. ``accepted=False`` means: no suggestion, see ``reason_fa``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    volume: float = Field(ge=0)  # 0.0 when rejected before a volume could be computed
    raw_volume: float = Field(ge=0)
    risk_amount: float = Field(ge=0)
    value_per_unit: float = Field(ge=0)
    loss_per_lot: float = Field(ge=0)
    actual_risk: float = Field(ge=0)
    margin: float = Field(ge=0)
    reason_fa: str | None = None
    warnings: list[str] = Field(default_factory=list)


def _is_real(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def step_decimals(step: float) -> int:
    """Decimals of a volume step: 0.01 -> 2, 0.1 -> 1, 1 -> 0, 0.001 -> 3."""
    exponent = Decimal(repr(float(step))).normalize().as_tuple().exponent
    return max(0, -int(exponent))


def floor_to_step(raw: float, step: float) -> float:
    """``floor(raw / step + 1e-9) * step`` rounded to the step's decimals (never rounds half up)."""
    steps = math.floor(raw / step + STEP_EPSILON)
    return round(steps * step, step_decimals(step))


def _rejected(reason: str, **values: float) -> SizingResult:
    base = dict(volume=0.0, raw_volume=0.0, risk_amount=0.0, value_per_unit=0.0, loss_per_lot=0.0,
                actual_risk=0.0, margin=0.0)
    base.update(values)
    return SizingResult(accepted=False, reason_fa=reason, **base)


def size_position(
    *,
    balance: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    tick_value: float,
    tick_size: float,
    volume_min: float,
    volume_step: float,
    volume_max: float,
    contract_size: float,
    leverage: float,
    price: float | None = None,
) -> SizingResult:
    """Suggested volume for risking ``risk_pct`` % of ``balance`` between ``entry`` and ``stop_loss``.

    Never raises for bad numbers: invalid inputs (e.g. ``tick_value = 0`` from a closed market) give a
    rejected result with a Persian reason, so the caller simply shows "no suggestion".
    """
    price = entry if price is None else price
    named = {
        "balance": balance, "risk_pct": risk_pct, "entry": entry, "stop_loss": stop_loss,
        "tick_value": tick_value, "tick_size": tick_size, "volume_min": volume_min,
        "volume_step": volume_step, "volume_max": volume_max, "contract_size": contract_size,
        "leverage": leverage, "price": price,
    }
    bad = [name for name, value in named.items() if not _is_real(value) or float(value) <= 0]
    if bad:
        return _rejected(f"اطلاعات لازم برای محاسبه حجم نامعتبر است ({', '.join(bad)} باید عدد مثبت باشد).")
    if volume_min > volume_max:
        return _rejected("اطلاعات نماد نامعتبر است: حداقل حجم از حداکثر حجم بیشتر است.")

    risk_amount = balance * risk_pct / 100.0
    value_per_unit = tick_value / tick_size
    distance = abs(entry - stop_loss)
    if distance == 0:
        return _rejected("فاصله ورود تا حد ضرر صفر است؛ حجم قابل محاسبه نیست.",
                         risk_amount=risk_amount, value_per_unit=value_per_unit)
    loss_per_lot = distance * value_per_unit
    raw = risk_amount / loss_per_lot
    volume = floor_to_step(raw, volume_step)

    warnings: list[str] = []
    if abs(value_per_unit - contract_size) / contract_size > CONTRACT_CHECK_REL_TOL:
        warnings.append(
            f"ارزش هر واحد قیمت برای یک لات ({value_per_unit:g} = tick_value/tick_size) با اندازه قرارداد "
            f"({contract_size:g}) بیش از ۱٪ اختلاف دارد؛ برای نمادهای غیر دلاری طبیعی است، وگرنه اطلاعات نماد را بررسی کنید."
        )
    if volume > volume_max:
        warnings.append(f"حجم محاسبه‌شده ({volume:g}) به حداکثر حجم مجاز نماد ({volume_max:g}) محدود شد.")
        volume = floor_to_step(volume_max, volume_step)  # on the step grid and never above the max

    common = dict(raw_volume=raw, risk_amount=risk_amount, value_per_unit=value_per_unit, loss_per_lot=loss_per_lot)
    if volume < volume_min:
        result = SizingResult(
            accepted=False, volume=volume, actual_risk=0.0, margin=0.0, warnings=warnings,
            reason_fa=(
                f"حجم محاسبه‌شده ({raw:.6f} لات، گرد‌شده به پایین {volume:g}) کمتر از حداقل حجم نماد "
                f"({volume_min:g}) است. با ریسک {risk_amount:.2f} و فاصله حد ضرر {distance:g} پیشنهادی داده نمی‌شود."
            ),
            **common,
        )
    else:
        actual_risk = volume * loss_per_lot
        margin = volume * contract_size * price / leverage
        if margin > balance:
            result = SizingResult(
                accepted=False, volume=volume, actual_risk=actual_risk, margin=margin, warnings=warnings,
                reason_fa=f"مارجین تخمینی ({margin:.2f}) از موجودی حساب ({balance:.2f}) بیشتر است؛ پیشنهادی داده نمی‌شود.",
                **common,
            )
        else:
            result = SizingResult(accepted=True, volume=volume, actual_risk=actual_risk, margin=margin,
                                  warnings=warnings, **common)
    if is_dev_mode():
        logger.debug(
            "size_position: risk=%.2f vpu=%.6f dist=%.6f loss/lot=%.4f raw=%.8f vol=%s accepted=%s margin=%.2f warn=%d",
            risk_amount, value_per_unit, distance, loss_per_lot, raw, result.volume, result.accepted,
            result.margin, len(warnings),
        )
    return result


def size_for_account(
    account: AccountSettings,
    *,
    entry: float,
    stop_loss: float,
    tick_value: float,
    tick_size: float,
    volume_min: float,
    volume_step: float,
    volume_max: float,
    contract_size: float,
    price: float | None = None,
) -> SizingResult:
    """:func:`size_position` with balance / risk_pct / leverage from the account settings."""
    return size_position(
        balance=account.balance, risk_pct=account.risk_pct, leverage=account.leverage, entry=entry,
        stop_loss=stop_loss, tick_value=tick_value, tick_size=tick_size, volume_min=volume_min,
        volume_step=volume_step, volume_max=volume_max, contract_size=contract_size, price=price,
    )
