"""Account / risk settings used for position sizing (stddev-channel-system.md section 5).

``AccountSettings`` (not to be confused with the process config ``alpha_engine.config.Settings``):

============  =========  ======================================
field         default    bounds
============  =========  ======================================
balance       1000.0     0 < balance <= 1e9 (account currency, USD)
risk_pct      1.0        0 < risk_pct <= 10 (percent of balance risked per trade)
leverage      100        1 <= leverage <= 1000, integer (1:100)
rr            2.0        0.1 <= rr <= 20 (take-profit distance / stop distance)
============  =========  ======================================

Worked example with the defaults: risking 1.0 % of 1000.0 USD = 10.00 USD per trade; with rr = 2.0
a winning trade targets 20.00 USD.

Booleans are rejected for every field (``True`` is not a leverage of 1), as are NaN/inf. The same
bounds are also CHECK constraints in the database (defense in depth).

Persistence: a single row (``id = 1``) in ``account_settings``. :meth:`AccountSettingsRepo.get`
returns the stored row or the defaults (nothing is written until the first update).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..logging_setup import get_logger, is_dev_mode
from .db import EngineConnection, utc_now_text

logger = get_logger(__name__)

BALANCE_MAX = 1e9
RISK_PCT_MAX = 10.0
LEVERAGE_MIN, LEVERAGE_MAX = 1, 1000
RR_MIN, RR_MAX = 0.1, 20.0

FIELD_LABELS_FA: dict[str, str] = {
    "balance": "موجودی حساب",
    "risk_pct": "درصد ریسک",
    "leverage": "اهرم",
    "rr": "نسبت ریسک به ریوارد (R:R)",
}


class AccountSettings(BaseModel):
    """Risk settings for sizing suggestions. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    balance: float = Field(default=1000.0, gt=0, le=BALANCE_MAX)
    risk_pct: float = Field(default=1.0, gt=0, le=RISK_PCT_MAX)
    leverage: int = Field(default=100, ge=LEVERAGE_MIN, le=LEVERAGE_MAX)
    rr: float = Field(default=2.0, ge=RR_MIN, le=RR_MAX)

    @field_validator("balance", "risk_pct", "leverage", "rr", mode="before")
    @classmethod
    def _no_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean is not a number")
        return value

    @property
    def risk_amount(self) -> float:
        """Money risked per trade: ``balance * risk_pct / 100``."""
        return self.balance * self.risk_pct / 100.0


class AccountSettingsValidationError(ValueError):
    def __init__(self, errors_fa: list[str]) -> None:
        super().__init__("; ".join(errors_fa))
        self.errors_fa = errors_fa


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def errors_fa_from_validation(exc: ValidationError, labels: Mapping[str, str] = FIELD_LABELS_FA) -> list[str]:
    """Translate a pydantic ``ValidationError`` on :class:`AccountSettings` into Persian messages."""
    messages: list[str] = []
    for err in exc.errors(include_input=False):
        field = str(err["loc"][0]) if err.get("loc") else ""
        label = f"«{labels.get(field, field)}»"
        kind = err.get("type", "")
        ctx = err.get("ctx") or {}
        if kind == "extra_forbidden":
            messages.append(f"فیلد ناشناخته: {field!r}.")
        elif kind == "greater_than":
            messages.append(f"{label} باید بیشتر از {_num(ctx.get('gt'))} باشد.")
        elif kind == "greater_than_equal":
            messages.append(f"{label} نباید کمتر از {_num(ctx.get('ge'))} باشد.")
        elif kind == "less_than":
            messages.append(f"{label} باید کمتر از {_num(ctx.get('lt'))} باشد.")
        elif kind == "less_than_equal":
            messages.append(f"{label} نباید بیشتر از {_num(ctx.get('le'))} باشد.")
        elif kind in ("int_type", "int_parsing", "int_from_float"):
            messages.append(f"{label} باید عدد صحیح باشد.")
        elif kind in ("float_type", "float_parsing", "finite_number"):
            messages.append(f"{label} باید یک عدد معتبر باشد.")
        else:
            messages.append(f"مقدار {label} نامعتبر است.")
    return messages


def merge_update(current: AccountSettings, partial: Any) -> AccountSettings:
    """Apply a partial update (``{"risk_pct": 2}``) to ``current``; raise with Persian errors."""
    if not isinstance(partial, Mapping):
        raise AccountSettingsValidationError(["تنظیمات باید به‌صورت یک شیء کلید-مقدار ارسال شوند."])
    errors: list[str] = []
    for key, value in partial.items():
        if key not in FIELD_LABELS_FA:
            errors.append(f"فیلد ناشناخته: {key!r}.")
        elif value is None:
            errors.append(f"«{FIELD_LABELS_FA[key]}» نمی‌تواند خالی باشد.")
        elif isinstance(value, bool):
            errors.append(f"«{FIELD_LABELS_FA[key]}» باید عدد باشد، نه true/false.")
    if errors:
        raise AccountSettingsValidationError(errors)
    try:
        return AccountSettings.model_validate({**current.model_dump(), **dict(partial)})
    except ValidationError as exc:
        raise AccountSettingsValidationError(errors_fa_from_validation(exc)) from None


class AccountSettingsRepo:
    """Read/write the single ``account_settings`` row."""

    def __init__(self, conn: EngineConnection) -> None:
        self._conn = conn

    def get(self) -> AccountSettings:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT balance, risk_pct, leverage, rr FROM account_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            return AccountSettings()
        return AccountSettings(balance=row["balance"], risk_pct=row["risk_pct"], leverage=row["leverage"], rr=row["rr"])

    def update(self, partial: Any) -> AccountSettings:
        """Validate ``partial`` against the current settings, persist, and return the new settings."""
        with self._conn.transaction():
            current = self.get()
            try:
                new = merge_update(current, partial)
            except AccountSettingsValidationError as exc:
                if is_dev_mode():
                    logger.debug("account settings update rejected: %s", exc.errors_fa)
                raise
            self._conn.execute(
                "INSERT INTO account_settings (id, balance, risk_pct, leverage, rr, updated_utc)"
                " VALUES (1, ?, ?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET balance = excluded.balance, risk_pct = excluded.risk_pct,"
                " leverage = excluded.leverage, rr = excluded.rr, updated_utc = excluded.updated_utc",
                (new.balance, new.risk_pct, new.leverage, new.rr, utc_now_text()),
            )
        if is_dev_mode():
            logger.debug("account settings updated: %s -> %s", current.model_dump(), new.model_dump())
        return new
