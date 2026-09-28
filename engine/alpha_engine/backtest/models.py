"""Backtest contract: configuration, windows, trades, skipped candidates and results (phase 4).

All models are immutable pydantic models (like ``SignalCandidate`` / ``AccountSettings``), so every result
is JSON-serialisable with ``model_dump(mode="json")`` for the persistence wave. All datetimes are
tz-aware UTC.

Time conventions (see ``simulator.py`` for the full rules)
---------------------------------------------------------
* ``Window`` is half-open: an H1 bar belongs to the window when ``start <= bar open < end``.
* ``Trade.entry_time`` = OPEN time of the entry bar ``t+1`` (the fill instant).
* ``Trade.exit_bar_time`` = OPEN time of the H1 bar in which the exit happened (chart marker).
* ``Trade.exit_time`` = the instant the exit is known on H1 data: the bar OPEN for gap exits
  (``sl_gap``/``tp_gap``), the bar CLOSE (open + 1h) for intrabar ``sl``/``tp`` hits and for
  ``end_of_period``. Metrics use ``exit_time``.
* ``EquityPoint.time`` = the instant of the valuation: the first point of a window is the open of its first
  bar (equity = balance = initial balance), every further point is the CLOSE of one in-window H1 bar.

Money is in the account currency (USD) and never rounded; prices (SL/TP) are not rounded to the symbol's
digits either (documented decision, phase 4).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..storage.account_settings import AccountSettings

# ------------------------------------------------------------------------------------------ labels
PROVISIONAL_LABEL_FA = "موقت تا تایید چک داده"
NO_SWAP_LABEL_FA = "بدون سوآپ"
ZERO_COMMISSION_LABEL_FA = "کمیسیون صفر"
HISTORICAL_SPREAD_LABEL_FA = "اسپرد تاریخی بروکر (خرید با ask، فروش با bid)"
NO_SPREAD_DATA_LABEL_FA = "داده اسپرد در دسترس نیست؛ اسپرد صفر فرض شد (بدون هزینه اسپرد)"
ZERO_SPREAD_LABEL_FA = "بدون اسپرد (هزینه صفر)"

SEED_MAX = 2**63 - 1


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be in UTC (offset 0), got offset {value.utcoffset()}")
    return value.astimezone(timezone.utc)


class ExitReason(StrEnum):
    SL = "sl"  # stop loss touched inside the bar, filled exactly at the SL
    TP = "tp"  # take profit touched inside the bar, filled exactly at the TP
    SL_GAP = "sl_gap"  # bar OPENED beyond the SL, filled at that open
    TP_GAP = "tp_gap"  # bar OPENED beyond the TP, filled at that open
    END_OF_PERIOD = "end_of_period"  # still open at the end of the window, closed at the last close


EXIT_REASON_FA: dict[ExitReason, str] = {
    ExitReason.SL: "حد ضرر",
    ExitReason.TP: "حد سود",
    ExitReason.SL_GAP: "حد ضرر (گپ؛ پر شدن با قیمت باز شدن کندل)",
    ExitReason.TP_GAP: "حد سود (گپ؛ پر شدن با قیمت باز شدن کندل)",
    ExitReason.END_OF_PERIOD: "بسته‌شده در پایان بازه",
}


class SkipReason(StrEnum):
    POSITION_OPEN = "position_open"  # a trade was open at decision time (max one open trade)
    ENTRY_OUTSIDE_WINDOW = "entry_outside_window"  # bar t+1 is after the window (or after the data)
    MISSING_GAP = "missing_gap"  # a data hole ("missing" gap) between bar t and bar t+1
    GAP_THROUGH_STOP = "gap_through_stop"  # bar t+1 opened at/through the SL on the exit side
    INVALID_LEVELS = "invalid_levels"  # levels not valid at the fill price (e.g. TP <= 0)
    SIZING_REJECTED = "sizing_rejected"  # size_position rejected (below volume_min, margin, bad spec)


SKIP_REASON_FA: dict[SkipReason, str] = {
    SkipReason.POSITION_OPEN: "در زمان تصمیم یک معامله باز وجود داشت (حداکثر یک معامله باز)؛ کاندید کنار گذاشته شد.",
    SkipReason.ENTRY_OUTSIDE_WINDOW: "کندل ورود (کندل بعد از کندل تایید) بیرون از بازه بک‌تست است؛ ورود انجام نشد.",
    SkipReason.MISSING_GAP: "بین کندل تایید و کندل بعد داده گمشده (missing) وجود دارد؛ ورود انجام نشد.",
    SkipReason.GAP_THROUGH_STOP: "کندل ورود آن طرف حد ضرر باز شد (گپ از حد ضرر عبور کرد)؛ ورود انجام نشد.",
    SkipReason.INVALID_LEVELS: "سطوح معامله با قیمت ورود واقعی معتبر نیستند؛ ورود انجام نشد.",
    SkipReason.SIZING_REJECTED: "حجم معامله قابل قبول نبود؛ ورود انجام نشد.",
}


# ------------------------------------------------------------------------------------------ cost model
class CostModel(BaseModel):
    """Trading costs. Spread always comes from the broker's history (bars are bid; ask = bid + spread)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    spread: Literal["historical"] = "historical"
    # Spread (points) for bars with no earlier non-zero broker spread (see costs.py): None = auto (ceil of the
    # median observed non-zero spread of the full cached history), 0 = explicit zero cost, > 0 = fixed value.
    fallback_spread_points: int | None = Field(default=None, ge=0, le=100_000)
    commission_per_lot_per_side: float = Field(default=0.0, ge=0.0, le=1000.0)
    swap: Literal["none"] = "none"

    @property
    def swap_label_fa(self) -> str:
        return NO_SWAP_LABEL_FA

    def labels_fa(self) -> list[str]:
        labels = [HISTORICAL_SPREAD_LABEL_FA]
        if self.commission_per_lot_per_side == 0:
            labels.append(ZERO_COMMISSION_LABEL_FA)
        labels.append(NO_SWAP_LABEL_FA)
        return labels


# ------------------------------------------------------------------------------------------ config
class RunConfig(BaseModel):
    """Everything that defines one backtest run (snapshotted at submit time; stored with the result)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    symbol: str = Field(min_length=1, max_length=32)
    mode: Literal["manual", "random"]
    # manual: [start, end) in UTC
    start: datetime | None = None
    end: datetime | None = None
    # random
    windows_count: int = Field(default=20, ge=1, le=500)
    window_months: int = Field(default=3, ge=1, le=60)
    seed: int | None = Field(default=None, ge=0, le=SEED_MAX)
    cost_model: CostModel = Field(default_factory=CostModel)
    account: AccountSettings  # balance = initial balance of EVERY window; risk_pct, leverage, rr
    strategy_name: str = Field(min_length=1)
    strategy_version: int = Field(ge=1)  # code version
    # Strategy identity beyond name/version (strategy contract S1): SHA-256 of an uploaded plugin's source file
    # and builtin/plugin. Omitted from dumps while None, so configs of the built-in strategy stored before these
    # fields existed (and the golden snapshots) are unchanged.
    strategy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda v: v is None)
    strategy_source: Literal["builtin", "plugin"] | None = Field(default=None, exclude_if=lambda v: v is None)
    params: dict[str, Any]
    params_version: int | None = Field(default=None, ge=1)  # params-store version (None = ad hoc)
    params_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provisional: bool = True  # "موقت تا تایید چک داده" until the raw-data check is confirmed

    @field_validator("start", "end")
    @classmethod
    def _utc_only(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _utc(value, info.field_name)

    @model_validator(mode="after")
    def _check(self) -> RunConfig:
        if self.mode == "manual":
            if self.start is None or self.end is None:
                raise ValueError("manual mode needs start and end")
            if not self.start < self.end:
                raise ValueError("start must be before end")
        return self

    @property
    def initial_balance(self) -> float:
        return self.account.balance


# ------------------------------------------------------------------------------------------ windows
class Window(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    start: datetime  # inclusive (bar open time)
    end: datetime  # exclusive

    @field_validator("start", "end")
    @classmethod
    def _utc_only(cls, value: datetime, info: Any) -> datetime:
        return _utc(value, info.field_name)


class WindowPlan(BaseModel):
    """How the windows of a run were chosen (stored for reproducibility)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["manual", "random"]
    windows: list[Window]
    seed: int | None = None  # random mode: the seed actually used (generated when none was given)
    seed_generated: bool = False
    algorithm: str | None = None  # "numpy.PCG64"
    numpy_version: str | None = None
    windows_count: int | None = None
    window_months: int | None = None
    earliest_start: datetime  # first allowed window start (channel valid + ATR warm-up margin)
    data_end: datetime  # close of the last cached H1 bar
    eligible_starts: int | None = None  # random mode: number of H1 bar opens a window could start at
    warmup_h4_bars: int  # the warm-up margin in H4 bars


# ------------------------------------------------------------------------------------------ trades
class Trade(BaseModel):
    """One simulated trade. ``net_pnl`` / ``r_multiple`` / ``exit_time`` are what the metrics read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_index: int
    trade_index: int  # 0-based within the window
    direction: Literal["buy", "sell"]
    setup_type: str  # SignalCandidate.setup_slug
    line: str | None  # channel line (StdDev setups); None for strategies without channel lines
    pattern: str
    confirmation_bar_time: datetime  # OPEN of bar t
    decision_time: datetime  # CLOSE of bar t
    entry_time: datetime  # OPEN of bar t+1
    entry: float  # fill price: buy = ask open (open + spread), sell = bid open
    entry_bid_open: float  # raw bar open (bid) of the entry bar
    stop_loss: float
    take_profit: float
    rr: float
    volume: float
    risk_amount: float  # volume * |entry - SL| * value_per_price_unit (actual currency risk at entry)
    balance_before: float  # realized balance used for sizing
    exit_bar_time: datetime
    exit_time: datetime
    exit_price: float
    exit_reason: ExitReason
    exit_reason_fa: str
    gross_pnl: float
    commission: float
    net_pnl: float
    r_multiple: float | None  # net_pnl / risk_amount
    balance_after: float
    bars_held: int  # H1 bars from the entry bar to the exit bar, both included
    spread_at_entry_points: int  # spread (points) of the entry bar actually used (after zero fill)
    spread_at_exit_points: int | None  # sell exits only (ask side); None for buys
    held_over_weekend: bool
    flags: list[str] = Field(default_factory=list)
    sizing_warnings_fa: list[str] = Field(default_factory=list)
    reason_fa: str  # the strategy's rationale (SignalCandidate.reason_fa)
    indicators: dict[str, float | int | bool | str | None] = Field(default_factory=dict)


class SkippedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_index: int
    time: datetime  # decision time (close of the confirmation bar t)
    confirmation_bar_time: datetime
    direction: Literal["buy", "sell"]
    setup_type: str
    reason: SkipReason
    reason_fa: str
    detail: str | None = None  # numbers behind the reason (fill price, SL, sizing message, ...)


class EquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    time: datetime
    balance: float  # realized
    equity: float  # mark-to-market liquidation value (see simulator.py)


class WindowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: Window
    initial_balance: float
    final_balance: float
    bars: int  # in-window H1 bars actually simulated
    first_bar_time: datetime | None
    last_bar_time: datetime | None
    trades: list[Trade]
    skipped: list[SkippedCandidate]
    equity: list[EquityPoint]
    candidates: int  # scan candidates with a decision inside the window
    zero_spread_bars_filled: int  # in-window bars whose zero spread was replaced by the last non-zero one
    zero_spread_bars_unfilled: int  # zero spread, no earlier non-zero spread and no fallback: 0 really used
    weekend_holds: int  # trades held over a weekend (swap is NOT modeled)
    spread_fallback_bars: int = 0  # zero spread, no earlier non-zero spread: the fallback spread was used
    stopped_reason: Literal["balance_depleted"] | None = None

    # metrics.WindowSummaryLike (summarize_windows)
    @property
    def index(self) -> int:
        return self.window.index

    @property
    def net_profit(self) -> float:
        return self.final_balance - self.initial_balance

    @property
    def net_profit_pct(self) -> float:
        return self.net_profit / self.initial_balance * 100.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)


class DataFingerprint(BaseModel):
    """Identity of the cached data a run used."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    h1_rows: int
    h4_rows: int
    h1_first_bar: datetime | None
    h1_last_bar: datetime | None
    h4_first_bar: datetime | None
    h4_last_bar: datetime | None
    h1_source: str | None  # cache meta source: "mt5" | "seed" | None (not from the cache)
    h4_source: str | None
    h1_fetched_at_utc: str | None = None
    h4_fetched_at_utc: str | None = None
    h1_sha256: str  # time + OHLC + spread content hash
    h4_sha256: str
    spec: dict[str, Any] | None = None  # SymbolSpec snapshot used for sizing / point
    spec_source: str | None = None


class SpreadFallback(BaseModel):
    """The fallback spread a run used for bars without an earlier broker spread (see costs.py)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    points: int = Field(ge=0)
    source: Literal["auto_median_observed", "user", "none"]  # none = auto, but no non-zero spread in the data
    observed_from: datetime | None  # open of the first cached H1 bar with a non-zero spread
    observed_to: datetime | None  # open of the last one
    observed_bars: int  # cached H1 bars with a non-zero spread (full history)
    observed_median: float | None  # median of those spreads (points)
    fallback_bars: int  # simulated in-window bars (all windows) that used ``points`` (> 0)
    zero_bars: int  # simulated in-window bars that used 0 (no earlier spread and fallback 0)
    total_bars: int  # simulated in-window bars (all windows)


def spread_label_fa(fb: SpreadFallback) -> str:
    """Honest spread label of a run (replaces the plain historical-spread label when bars were not priced
    with the broker's own spread)."""
    since = fb.observed_from.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if fb.observed_from else None

    def pct(n: int) -> str:
        return f"{(n / fb.total_bars * 100.0) if fb.total_bars else 0.0:.1f}"

    why = "میانه اسپرد مشاهده‌شده" if fb.source == "auto_median_observed" else "تعیین کاربر"
    if fb.fallback_bars:
        if since:
            return (f"اسپرد تاریخی بروکر فقط از {since}؛ برای {pct(fb.fallback_bars)}٪ کندل‌ها اسپرد ثابت "
                    f"{fb.points} پوینت ({why}) فرض شد")
        return f"داده اسپرد بروکر در دسترس نیست؛ برای همه کندل‌ها اسپرد ثابت {fb.points} پوینت ({why}) فرض شد"
    if fb.zero_bars:
        if since:
            return f"اسپرد تاریخی بروکر فقط از {since}؛ برای {pct(fb.zero_bars)}٪ کندل‌ها {ZERO_SPREAD_LABEL_FA}"
        return f"{ZERO_SPREAD_LABEL_FA}؛ داده اسپرد بروکر در دسترس نیست"
    return HISTORICAL_SPREAD_LABEL_FA


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    config: RunConfig  # as submitted (seed may be None)
    plan: WindowPlan  # the seed actually used is here
    windows: list[WindowResult]
    fingerprint: DataFingerprint
    provisional: bool
    labels_fa: list[str]  # cost / data labels to show with every result
    cost_model: CostModel
    entry_rule: Literal["next_bar_open"] = "next_bar_open"
    price_basis: Literal["bid_bars"] = "bid_bars"  # bars are bid; ask = bid + spread * point
    spread_source: Literal["historical", "none"]
    spread_fallback: SpreadFallback
    total_trades: int
    total_skipped: int

    @field_validator("labels_fa")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("labels_fa must not be empty")
        return value


__all__ = [
    "EXIT_REASON_FA",
    "NO_SWAP_LABEL_FA",
    "PROVISIONAL_LABEL_FA",
    "SKIP_REASON_FA",
    "CostModel",
    "DataFingerprint",
    "EquityPoint",
    "ExitReason",
    "RunConfig",
    "RunResult",
    "SkipReason",
    "SkippedCandidate",
    "SpreadFallback",
    "ZERO_SPREAD_LABEL_FA",
    "spread_label_fa",
    "Trade",
    "Window",
    "WindowPlan",
    "WindowResult",
]
