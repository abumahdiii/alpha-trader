"""``/backtests`` REST routes and the ``/ws/backtests/{id}`` progress WebSocket (phase 4, wave 2).

* ``POST /backtests`` -- validate, resolve the strategy (body ``strategy``, default ``stddev_channel``; optional
  ``strategy_version`` must be the registered code version; unknown -> 404 ``strategy_not_found``), snapshot
  its ACTIVE params version, identity (``strategy_sha256`` / ``strategy_source``) and the account settings at
  request time, validate the period against the cached data (cheap: bars + the strategy's first valid bar,
  shared LRU; :func:`period_limits`), draw the seed of a seedless random run, store the run ``queued`` and hand
  it (with the resolved strategy instance) to ``app.state.backtest_jobs`` -> 202 ``{id, status, provisional,
  labels_fa, seed, seed_generated}``.
* ``GET /backtests/limits?symbol=[&strategy=]`` -- the allowed manual period for the ACTIVE params of the
  strategy, computed by the SAME :func:`period_limits` the POST validates with (earliest start, data start/end,
  warm-up), the request bounds and the auto fallback spread.
* ``GET /backtests?offset&limit&symbol&mode&status`` (newest first; ``total`` = matching runs),
  ``GET /backtests/{id}`` (config, plan incl. seed, windows
  with metrics, overall metrics, distribution, labels, provisional, fingerprint, status/progress/error),
  ``GET /backtests/{id}/trades?window&offset&limit``, ``GET /backtests/{id}/equity?window``,
  ``GET /backtests/{id}/skipped?window``, ``POST /backtests/{id}/cancel``, ``DELETE /backtests/{id}``.
* ``WS /ws/backtests/{id}`` -- ``{"type": "progress", ...}`` on every change (polled every
  ``WS_POLL_S``), ``{"type": "keepalive", "id", "time_utc"}`` after ``WS_KEEPALIVE_S`` seconds without any
  message while the run is queued/running, then ONE final ``{"type": "done"|"error"|"cancelled"|
  "interrupted", ...}`` and close.

The engine only reads the local cache here (never MT5) and never places orders. Errors use
``{"detail": {"code", "message_fa", "errors_fa"}}``. All times are ISO-8601 UTC with ``Z``; a ``from`` /
``to`` without an offset is taken as UTC (like ``/chart``). Full shapes: ``engine/README.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, Body, Depends, Query, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..backtest.costs import resolve_fallback_points
from ..backtest.history import HistoryUnavailable
from ..backtest.jobs import BacktestJobs, JobsClosed, PreparedHistory, first_valid_index, prepare_history
from ..backtest.models import HISTORICAL_SPREAD_LABEL_FA, PROVISIONAL_LABEL_FA, SEED_MAX, CostModel, RunConfig
from ..backtest.periods import (
    PeriodError,
    data_end,
    earliest_start,
    manual_window,
    new_seed,
    random_windows,
)
from ..backtest.results import EQUITY_STORAGE_RULE
from ..logging_setup import get_logger, is_dev_mode
from ..market_data import MarketDataService
from ..storage.account_settings import AccountSettingsRepo
from ..storage.backtests_repo import ACTIVE_STATUSES, TERMINAL_STATUSES, BacktestsRepo, api_time
from ..storage.db import EngineConnection
from ..strategy.base import WarmupTextsFa, bar_open_times
from ..strategy.registry import StrategyRegistry
from ..strategy.resolve import ActiveStrategy
from . import DB_UNAVAILABLE_FA, api_error, get_db, get_market_data, resolve_strategy_or_error
from .chart import ChartCache, _check_symbol, get_chart_cache
from .strategies import get_registry

logger = get_logger(__name__)
router = APIRouter(tags=["backtests"])

WS_POLL_S = 0.2
WS_KEEPALIVE_S = 15.0  # idle seconds before a {"type": "keepalive"} while the run is queued/running
LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX = 50, 500
LIST_OFFSET_MAX = 10**9
WINDOWS_COUNT_MAX, WINDOW_MONTHS_MAX = 500, 60
LIST_MODES = ("manual", "random")
LIST_STATUSES = ("queued", "running", "done", "error", "cancelled", "interrupted")
TRADES_LIMIT_DEFAULT, TRADES_LIMIT_MAX = 500, 5000
NOT_FOUND_FA = "بک‌تستی با این شناسه پیدا نشد."

FIELD_LABELS_FA: dict[str, str] = {
    "symbol": "نماد",
    "mode": "حالت اجرا",
    "from": "ابتدای بازه",
    "to": "انتهای بازه",
    "windows_count": "تعداد پنجره‌ها",
    "window_months": "طول هر پنجره (ماه)",
    "seed": "seed",
    "commission_per_lot_per_side": "کمیسیون هر لات در هر طرف",
    "fallback_spread_points": "اسپرد جایگزین (پوینت)",
    "strategy": "سیستم معاملاتی",
    "strategy_version": "نسخه سیستم",
}
MANUAL_ONLY = ("from_", "to")
RANDOM_ONLY = ("windows_count", "window_months", "seed")


# ---------------------------------------------------------------------------------------------- models
class BacktestRequest(BaseModel):
    """Body of ``POST /backtests``. ``null`` counts as "not given"."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, allow_inf_nan=False)

    symbol: str = Field(min_length=1, max_length=32)
    mode: Literal["manual", "random"]
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    windows_count: int | None = Field(default=None, ge=1, le=WINDOWS_COUNT_MAX)
    window_months: int | None = Field(default=None, ge=1, le=WINDOW_MONTHS_MAX)
    seed: int | None = Field(default=None, ge=0, le=SEED_MAX)
    commission_per_lot_per_side: float | None = Field(default=None, ge=0.0, le=1000.0)
    # spread (points) for bars without an earlier broker spread: omitted = auto (median observed), 0 = zero cost
    fallback_spread_points: int | None = Field(default=None, ge=0, le=100_000)
    # registered strategy name (omitted = stddev_channel) and, optionally, its expected code version
    strategy: str | None = Field(default=None, min_length=1, max_length=64)
    strategy_version: int | None = Field(default=None, ge=1)

    @field_validator("windows_count", "window_months", "seed", "commission_per_lot_per_side", "fallback_spread_points",
                     "strategy_version", mode="before")
    @classmethod
    def _no_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean is not a number")
        return value

    @field_validator("windows_count", "window_months", "seed", "fallback_spread_points", "strategy_version",
                     mode="before")
    @classmethod
    def _whole(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("integer required")
        return value


class SubmitResponse(BaseModel):
    id: int
    status: Literal["queued"]
    provisional: bool
    labels_fa: list[str]
    seed: int | None = None  # random: the seed the run uses (echoed, or drawn at submit time); manual: null
    seed_generated: bool | None = None  # random: true when the engine drew the seed; manual: null


class FallbackSpreadDefault(BaseModel):
    points: int
    source: Literal["auto_median_observed", "none"]


class LimitsResponse(BaseModel):
    symbol: str
    earliest_start: str
    data_start: str
    data_end: str
    warmup_h4_bars: int
    window_months_max: int
    windows_count_max: int
    seed_max: int
    default_fallback_spread_points: FallbackSpreadDefault
    params_version: int | None
    params_hash: str
    note_fa: str


class RunError(BaseModel):
    code: str | None
    message_fa: str | None


class RunSummary(BaseModel):
    id: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    status: str
    progress: float
    symbol: str
    mode: Literal["manual", "random"]
    from_: str | None = Field(serialization_alias="from")
    to: str | None
    windows_count: int | None
    window_months: int | None
    seed: int | None
    seed_generated: bool | None
    strategy: str
    strategy_version: int
    strategy_source: Literal["builtin", "plugin"]  # runs stored before the field existed: "builtin"
    strategy_sha256: str | None  # uploaded plugins: SHA-256 of the source file; built-in: null
    params_version: int | None
    params_hash: str
    provisional: bool
    trade_count: int | None
    net_profit: float | None
    net_profit_pct: float | None
    summary_basis: Literal["single_window", "window_mean"]
    elapsed_s: float | None
    error: RunError | None


class RunList(BaseModel):
    count: int  # items in this page
    total: int  # runs matching the filters (all pages)
    offset: int
    limit: int
    runs: list[RunSummary]


class WindowOut(BaseModel):
    index: int
    start: str
    end: str
    initial_balance: float
    final_balance: float
    net_profit: float
    net_profit_pct: float
    trade_count: int
    skipped_count: int
    candidates: int
    bars: int
    first_bar_time: str | None
    last_bar_time: str | None
    zero_spread_bars_filled: int
    zero_spread_bars_unfilled: int
    weekend_holds: int
    spread_fallback_bars: int
    stopped_reason: str | None
    equity_points_full: int
    equity_points_stored: int
    metrics: dict[str, Any]


class RunDetail(RunSummary):
    labels_fa: list[str]
    provisional_label_fa: str | None
    request: dict[str, Any]
    config: dict[str, Any]
    plan: dict[str, Any] | None
    fingerprint: dict[str, Any] | None
    result_meta: dict[str, Any] | None
    spread_fallback: dict[str, Any] | None
    metrics_kind: Literal["single_window", "random_aggregate"] | None
    metrics: dict[str, Any] | None
    distribution: dict[str, Any] | None
    windows: list[WindowOut]
    equity_storage_rule: str
    timings: dict[str, float] | None
    live: dict[str, Any] | None


class TradesPage(BaseModel):
    run_id: int
    window: int | None
    total: int
    offset: int
    limit: int
    trades: list[dict[str, Any]]


class EquityPointOut(BaseModel):
    window_index: int
    time: str
    balance: float
    equity: float


class EquityOut(BaseModel):
    run_id: int
    window: int | None
    downsampled: bool = True
    rule: str = EQUITY_STORAGE_RULE
    count: int
    points: list[EquityPointOut]


class SkippedOut(BaseModel):
    run_id: int
    window: int | None
    count: int
    skipped: list[dict[str, Any]]


class CancelResponse(BaseModel):
    id: int
    status: str
    cancel_requested: bool


class DeleteResponse(BaseModel):
    id: int
    deleted: bool


# ---------------------------------------------------------------------------------------------- deps
def get_jobs(request: Request) -> BacktestJobs:
    jobs = getattr(request.app.state, "backtest_jobs", None)
    if jobs is None:
        raise api_error(503, "db_unavailable", DB_UNAVAILABLE_FA)
    return jobs


def _errors_fa(exc: ValidationError) -> list[str]:
    messages: list[str] = []
    for err in exc.errors(include_input=False):
        loc = [str(x) for x in err.get("loc", ())]
        field = "from" if loc and loc[0] == "from_" else (loc[0] if loc else "")
        label = f"«{FIELD_LABELS_FA.get(field, field)}»"
        kind = err.get("type", "")
        ctx = err.get("ctx") or {}
        if kind == "extra_forbidden":
            messages.append(f"فیلد ناشناخته: {field!r}.")
        elif kind == "missing":
            messages.append(f"{label} الزامی است.")
        elif kind == "literal_error" and field == "mode":
            messages.append(f"{label} باید manual (بازه دستی) یا random (پنجره‌های تصادفی) باشد.")
        elif kind in ("greater_than_equal", "less_than_equal"):
            bound = ctx.get("ge", ctx.get("le"))
            word = "کمتر" if kind == "greater_than_equal" else "بیشتر"
            messages.append(f"{label} نباید {word} از {bound} باشد.")
        elif kind.startswith("datetime") or kind.startswith("date"):
            messages.append(f"{label} باید یک تاریخ-زمان ISO-8601 باشد (مثل 2024-01-01T00:00:00Z).")
        elif kind.startswith("string") and field == "strategy":
            messages.append(f"{label} باید نام یک سیستم ثبت‌شده (۱ تا ۶۴ نویسه) باشد.")
        elif kind.startswith("string"):
            messages.append(f"{label} باید یک متن معتبر (۱ تا ۳۲ نویسه) باشد.")
        elif kind == "model_type" or kind == "dict_type":
            messages.append("بدنه درخواست باید یک شیء JSON باشد.")
        elif kind.startswith("int") or field in ("windows_count", "window_months", "seed", "fallback_spread_points",
                                                 "strategy_version"):
            messages.append(f"{label} باید عدد صحیح باشد.")
        elif kind.startswith("float") or kind == "finite_number" or field == "commission_per_lot_per_side":
            messages.append(f"{label} باید یک عدد معتبر باشد.")
        else:
            messages.append(f"مقدار {label} نامعتبر است.")
    return messages or ["درخواست نامعتبر است."]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _period_error(exc: PeriodError) -> Exception:
    return api_error(422, exc.code, exc.message_fa)


def _history_error(exc: HistoryUnavailable) -> Exception:
    return api_error(503 if exc.code == "cache_unreadable" else 422, exc.code, exc.message_fa)


def _run_labels(cost: CostModel, provisional: bool) -> list[str]:
    """Labels known at submit time. The spread label depends on the data actually simulated (historical /
    fallback / zero cost, ``models.spread_label_fa``) and is added when the run is done."""
    labels = [x for x in cost.labels_fa() if x != HISTORICAL_SPREAD_LABEL_FA]
    if provisional:
        labels.insert(0, PROVISIONAL_LABEL_FA)
    return labels


@dataclass(frozen=True)
class PeriodLimits:
    """The allowed manual period of one symbol for the ACTIVE params of a strategy: what ``POST /backtests``
    validates against and what ``GET /backtests/limits`` reports (one computation, :func:`period_limits`)."""

    prepared: PreparedHistory
    earliest: pd.Timestamp  # periods.earliest_start (strategy's first valid bar + its warm-up margin)
    data_start: pd.Timestamp  # open of the first cached H1 bar
    data_end: pd.Timestamp  # periods.data_end (close of the last cached H1 bar)
    warmup_bars: int  # Strategy.warmup_margin_h4_bars(params)
    texts: WarmupTextsFa  # Strategy.warmup_texts_fa


def period_limits(service: MarketDataService, name: str, active: ActiveStrategy, lru: ChartCache) -> PeriodLimits:
    """Cached history (shared LRU) + ``periods.earliest_start`` / ``data_end`` with the strategy's first valid
    bar and warm-up margin, the same functions the runner uses. Raises the API errors: ``HistoryUnavailable`` ->
    422 ``no_data`` / ``spec_missing`` (503 ``cache_unreadable``), ``PeriodError`` -> 422 ``data_too_short``."""
    try:
        prepared = prepare_history(service.cache, name, lru)
    except HistoryUnavailable as exc:
        if is_dev_mode():
            logger.debug("backtest period limits %s: history unavailable %s", name, exc.code)
        raise _history_error(exc) from None
    strategy = active.strategy
    fv = first_valid_index(prepared, strategy, active.params, active.params_hash, lru)
    warm = int(strategy.warmup_margin_h4_bars(active.params))
    texts = strategy.warmup_texts_fa
    h4_ns = bar_open_times(prepared.history.h4).as_unit("ns").asi8
    times_ns = prepared.bars.times_ns
    try:
        earliest = earliest_start(times_ns, h4_ns, fv, warm, texts=texts)
    except PeriodError as exc:
        if is_dev_mode():
            logger.debug("backtest period limits %s: %s", name, exc.code)
        raise _period_error(exc) from None
    limits = PeriodLimits(prepared=prepared, earliest=earliest, data_start=pd.Timestamp(int(times_ns[0]), tz="UTC"),
                          data_end=data_end(times_ns), warmup_bars=warm, texts=texts)
    if is_dev_mode():
        logger.debug("backtest period limits %s (%s, params %s): first valid H1 bar #%s, earliest=%s data=%s..%s "
                     "warm-up=%d H4 bars", name, active.identity.label(), active.params_hash[:12], fv,
                     earliest.isoformat(), limits.data_start.isoformat(), limits.data_end.isoformat(),
                     limits.warmup_bars)
    return limits


def _iso(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------------- POST
@router.post("/backtests", status_code=202, response_model=SubmitResponse,
             responses={404: {"description": "Symbol/strategy not found"}, 409: {"description": "Stored params invalid"},
                        422: {"description": "Invalid request or period"}, 503: {"description": "DB/cache unavailable"}})
def submit_backtest(
    request: Request,
    payload: Any = Body(..., examples=[{"symbol": "XAUUSD.x", "mode": "manual", "from": "2023-01-01T00:00:00Z",
                                        "to": "2024-01-01T00:00:00Z"},
                                       {"symbol": "XAUUSD.x", "mode": "random", "windows_count": 20,
                                        "window_months": 3, "seed": 42}]),
    service: MarketDataService = Depends(get_market_data),
    db: EngineConnection = Depends(get_db),
    registry: StrategyRegistry = Depends(get_registry),
    jobs: BacktestJobs = Depends(get_jobs),
    lru: ChartCache = Depends(get_chart_cache),
) -> SubmitResponse:
    if jobs.closing:
        raise api_error(503, "engine_closing", "engine در حال بسته شدن است؛ اجرای جدید پذیرفته نمی‌شود.")
    try:
        body = BacktestRequest.model_validate(payload)
    except ValidationError as exc:
        errors = _errors_fa(exc)
        if is_dev_mode():
            logger.debug("POST /backtests rejected: %s", errors)
        raise api_error(422, "invalid_request", "درخواست بک‌تست نامعتبر است.", errors) from None
    wrong = [f for f in (RANDOM_ONLY if body.mode == "manual" else MANUAL_ONLY) if getattr(body, f) is not None]
    if wrong:
        names = "، ".join(f"«{FIELD_LABELS_FA['from' if f == 'from_' else f]}»" for f in wrong)
        mode_fa = "دستی" if body.mode == "manual" else "تصادفی"
        raise api_error(422, "field_not_for_mode", f"در حالت {mode_fa} این فیلدها کاربرد ندارند: {names}.")
    start, end = _utc(body.from_), _utc(body.to)
    if body.mode == "manual":
        if start is None or end is None:
            raise api_error(422, "missing_period", "در حالت دستی «ابتدای بازه» و «انتهای بازه» الزامی هستند.")
        if not start < end:
            raise api_error(422, "invalid_range", "ابتدای بازه باید قبل از انتهای آن باشد.")
    name = _check_symbol(service, body.symbol)
    # 404 strategy_not_found (unknown name / code version) / 409 stored_params_invalid
    active = resolve_strategy_or_error(db, registry, body.strategy, body.strategy_version)
    account = AccountSettingsRepo(db).get()
    settings = request.app.state.settings
    provisional = not bool(getattr(settings, "alpha_data_check_confirmed", False))
    cost = CostModel(commission_per_lot_per_side=body.commission_per_lot_per_side or 0.0,
                     fallback_spread_points=body.fallback_spread_points)
    # Period validation now (same functions the runner uses, shared with GET /backtests/limits), so the UI
    # gets a 422 instead of a failed run.
    limits = period_limits(service, name, active, lru)
    window_months = body.window_months if body.window_months is not None else 3
    try:
        if body.mode == "manual":
            assert start is not None and end is not None
            manual_window(start, end, limits.prepared.bars.times_ns, limits.earliest, warmup_bars=limits.warmup_bars,
                          texts=limits.texts)
        else:  # seed-independent check that at least one window fits (seed 0: nothing is generated)
            random_windows(limits.prepared.bars.times_ns, limits.earliest, count=1, months=window_months, seed=0,
                           warmup_bars=limits.warmup_bars)
    except PeriodError as exc:
        if is_dev_mode():
            logger.debug("POST /backtests %s %s: period rejected %s", name, body.mode, exc.code)
        raise _period_error(exc) from None
    # Random runs: the seed is explicit BEFORE the run is stored (03_trading_safety.md section 3): echoed, or
    # drawn here, stored in the config snapshot and the row, returned in the 202.
    seed: int | None = None
    seed_generated: bool | None = None
    if body.mode == "random":
        seed_generated = body.seed is None
        seed = new_seed() if body.seed is None else body.seed
        if seed_generated and is_dev_mode():
            logger.debug("POST /backtests %s random: no seed in the request, generated seed=%d at submit", name, seed)
    config = RunConfig(
        symbol=name, mode=body.mode, start=start, end=end,
        windows_count=body.windows_count if body.windows_count is not None else 20,
        window_months=window_months, seed=seed, cost_model=cost, account=account,
        strategy_name=active.identity.name, strategy_version=active.code_version,
        strategy_sha256=active.identity.sha256, strategy_source=active.identity.source, params=active.params,
        params_version=active.record_version, params_hash=active.params_hash, provisional=provisional,
    )
    labels = _run_labels(cost, provisional)
    request_json = body.model_dump(mode="json", by_alias=True, exclude_none=True)
    repo = BacktestsRepo(db)
    run_id = repo.create_run(
        request=request_json, config=config.model_dump(mode="json"), symbol=name, mode=body.mode,
        period_start=start, period_end=end,
        windows_count=config.windows_count if body.mode == "random" else None,
        window_months=config.window_months if body.mode == "random" else None,
        seed=seed, seed_generated=seed_generated, strategy_name=config.strategy_name,
        strategy_version=config.strategy_version, params_version=config.params_version,
        params_hash=config.params_hash, provisional=provisional, labels_fa=labels,
    )
    try:
        jobs.submit(run_id, config, strategy=active.strategy, seed_generated=bool(seed_generated))
    except JobsClosed:
        repo.finish(run_id, "interrupted", error_code="engine_closing", error_message_fa="engine در حال بسته شدن بود.")
        raise api_error(503, "engine_closing", "engine در حال بسته شدن است؛ اجرای جدید پذیرفته نمی‌شود.") from None
    if is_dev_mode():
        logger.debug("POST /backtests -> run %d: %s %s from=%s to=%s windows=%s months=%s seed=%s (generated=%s) "
                     "commission=%g strategy %s params v%d hash=%s account=%s earliest=%s provisional=%s", run_id,
                     name, body.mode, start, end, config.windows_count, config.window_months, seed, seed_generated,
                     cost.commission_per_lot_per_side, active.identity.label(), active.record_version,
                     active.params_hash[:12], account.model_dump(), limits.earliest.isoformat(), provisional)
    return SubmitResponse(id=run_id, status="queued", provisional=provisional, labels_fa=labels, seed=seed,
                          seed_generated=seed_generated)


# ---------------------------------------------------------------------------------------------- GET
def _strategy_identity(row: dict[str, Any]) -> tuple[str | None, str]:
    """``(sha256, source)`` of a run: the list query extracts them from the stored config
    (``BacktestsRepo`` summary columns), a full row carries the parsed ``config``. Older configs lack both:
    built-in, no hash."""
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    sha = row["strategy_sha256"] if "strategy_sha256" in row else config.get("strategy_sha256")
    source = row["strategy_source"] if "strategy_source" in row else config.get("strategy_source")
    return sha, source or "builtin"


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    error = None
    if row.get("error_code") or row.get("error_message_fa"):
        error = {"code": row.get("error_code"), "message_fa": row.get("error_message_fa")}
    sha, source = _strategy_identity(row)
    return {
        "id": row["id"], "created_at": api_time(row["created_utc"]), "started_at": api_time(row["started_utc"]),
        "finished_at": api_time(row["finished_utc"]), "status": row["status"], "progress": row["progress"],
        "symbol": row["symbol"], "mode": row["mode"], "from_": api_time(row["period_start_utc"]),
        "to": api_time(row["period_end_utc"]), "windows_count": row["windows_count"],
        "window_months": row["window_months"], "seed": row["seed"],
        "seed_generated": None if row["seed_generated"] is None else bool(row["seed_generated"]),
        "strategy": row["strategy_name"], "strategy_version": row["strategy_version"],
        "strategy_source": source, "strategy_sha256": sha,
        "params_version": row["params_version"], "params_hash": row["params_hash"],
        "provisional": bool(row["provisional"]), "trade_count": row["trade_count"], "net_profit": row["net_profit"],
        "net_profit_pct": row["net_profit_pct"],
        "summary_basis": "single_window" if row["mode"] == "manual" else "window_mean",
        "elapsed_s": row["elapsed_s"], "error": error,
    }


def _run_or_404(repo: BacktestsRepo, run_id: int) -> dict[str, Any]:
    row = repo.get_run(run_id)
    if row is None:
        raise api_error(404, "backtest_not_found", NOT_FOUND_FA)
    return row


def _check_window(row: dict[str, Any], window: int | None) -> None:
    if window is None:
        return
    total = 1 if row["mode"] == "manual" else int(row["windows_count"] or 0)
    if not 0 <= window < total:
        raise api_error(422, "invalid_window", f"شماره پنجره باید بین 0 و {total - 1} باشد.")


def _check_int(value: int | None, name_fa: str, lo: int, hi: int) -> None:
    if value is not None and not lo <= value <= hi:
        raise api_error(422, "invalid_query", f"«{name_fa}» باید بین {lo} و {hi} باشد.")


def _check_choice(value: str | None, name_fa: str, choices: tuple[str, ...]) -> None:
    if value is not None and value not in choices:
        raise api_error(422, "invalid_query", f"«{name_fa}» باید یکی از این مقدارها باشد: {'، '.join(choices)}.")


@router.get("/backtests", response_model=RunList, response_model_by_alias=True)
def list_backtests(
    limit: Annotated[int | None, Query()] = None,
    offset: Annotated[int | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    mode: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    db: EngineConnection = Depends(get_db),
) -> dict[str, Any]:
    """Newest first; ``offset``/``limit`` paging; optional exact filters (``symbol`` as stored, ``mode``,
    ``status``); ``total`` = runs matching the filters."""
    _check_int(limit, "limit", 1, LIST_LIMIT_MAX)
    _check_int(offset, "offset", 0, LIST_OFFSET_MAX)
    if symbol is not None and not 1 <= len(symbol) <= 32:
        raise api_error(422, "invalid_query", "«symbol» باید ۱ تا ۳۲ نویسه باشد.")
    _check_choice(mode, "mode", LIST_MODES)
    _check_choice(status, "status", LIST_STATUSES)
    eff_limit, eff_offset = limit or LIST_LIMIT_DEFAULT, offset or 0
    rows, total = BacktestsRepo(db).list_runs_page(eff_limit, eff_offset, symbol=symbol, mode=mode, status=status)
    if is_dev_mode():
        logger.debug("GET /backtests symbol=%s mode=%s status=%s offset=%d limit=%d -> %d item(s), total %d", symbol,
                     mode, status, eff_offset, eff_limit, len(rows), total)
    return {"count": len(rows), "total": total, "offset": eff_offset, "limit": eff_limit,
            "runs": [_summary(r) for r in rows]}


# {why} = the strategy's WarmupTextsFa.limits with the margin filled in (StdDev: "کانال به داده کافی H4 و حاشیه
# گرم شدن ATR (140 کندل H4 = ۱۰ برابر دوره ATR) نیاز دارد").
LIMITS_NOTE_FA = (
    "بازه دستی نیم‌باز [ابتدا، انتها) و به وقت UTC است. ابتدای بازه نباید زودتر از «اولین زمان مجاز» باشد: "
    "{why}. انتهای بازه نباید بعد از "
    "آخرین داده کش باشد. این مرزها به پارامترهای فعال سیستم بستگی دارند و با تغییر آن‌ها یا به‌روزرسانی کش عوض "
    "می‌شوند.")


@router.get("/backtests/limits", response_model=LimitsResponse,
            responses={404: {"description": "Symbol/strategy not found"}, 409: {"description": "Stored params invalid"},
                       422: {"description": "Invalid symbol / no data / data too short"},
                       503: {"description": "DB/cache unavailable"}})
def backtest_limits(
    symbol: Annotated[str | None, Query()] = None,
    strategy: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    service: MarketDataService = Depends(get_market_data),
    db: EngineConnection = Depends(get_db),
    registry: StrategyRegistry = Depends(get_registry),
    lru: ChartCache = Depends(get_chart_cache),
) -> dict[str, Any]:
    """Allowed manual period of ``symbol`` for the ACTIVE params of ``strategy`` (default ``stddev_channel``;
    exactly what ``POST /backtests`` accepts: ``from >= earliest_start``, ``to <= data_end``) plus the request
    bounds and the auto fallback spread."""
    if symbol is None or not symbol.strip():
        raise api_error(422, "invalid_query", "«symbol» الزامی است.")
    name = _check_symbol(service, symbol)
    active = resolve_strategy_or_error(db, registry, strategy)
    limits = period_limits(service, name, active, lru)
    h1 = limits.prepared.history.h1
    points, source = resolve_fallback_points(h1["spread"].to_numpy() if "spread" in h1.columns else None, None)
    return {
        "symbol": name, "earliest_start": _iso(limits.earliest), "data_start": _iso(limits.data_start),
        "data_end": _iso(limits.data_end), "warmup_h4_bars": limits.warmup_bars,
        "window_months_max": WINDOW_MONTHS_MAX, "windows_count_max": WINDOWS_COUNT_MAX, "seed_max": SEED_MAX,
        "default_fallback_spread_points": {"points": points, "source": source},
        "params_version": active.record_version, "params_hash": active.params_hash,
        "note_fa": LIMITS_NOTE_FA.format(why=limits.texts.limits.format(margin=limits.warmup_bars)),
    }


@router.get("/backtests/{run_id}", response_model=RunDetail, response_model_by_alias=True,
            responses={404: {"description": "Unknown run"}})
def get_backtest(run_id: int, request: Request, db: EngineConnection = Depends(get_db)) -> dict[str, Any]:
    repo = BacktestsRepo(db)
    row = _run_or_404(repo, run_id)
    windows = []
    for w in repo.get_windows(run_id):
        net = w["final_balance"] - w["initial_balance"]
        windows.append({
            "index": w["window_index"], "start": api_time(w["start_utc"]), "end": api_time(w["end_utc"]),
            "initial_balance": w["initial_balance"], "final_balance": w["final_balance"], "net_profit": net,
            "net_profit_pct": net / w["initial_balance"] * 100.0, "trade_count": w["trade_count"],
            "skipped_count": w["skipped_count"], "candidates": w["candidates"], "bars": w["bars"],
            "first_bar_time": api_time(w["first_bar_utc"]), "last_bar_time": api_time(w["last_bar_utc"]),
            "zero_spread_bars_filled": w["zero_spread_bars_filled"],
            "zero_spread_bars_unfilled": w["zero_spread_bars_unfilled"], "weekend_holds": w["weekend_holds"],
            "spread_fallback_bars": w["spread_fallback_bars"],
            "stopped_reason": w["stopped_reason"], "equity_points_full": w["equity_points_full"],
            "equity_points_stored": w["equity_points_stored"], "metrics": w["metrics"],
        })
    jobs: BacktestJobs | None = getattr(request.app.state, "backtest_jobs", None)
    live = jobs.snapshot(run_id) if jobs is not None and row["status"] in ACTIVE_STATUSES else None
    meta = row["result"]
    return {
        **_summary(row),
        "labels_fa": row["labels"], "provisional_label_fa": PROVISIONAL_LABEL_FA if row["provisional"] else None,
        "request": row["request"], "config": row["config"], "plan": row["plan"], "fingerprint": row["fingerprint"],
        "result_meta": meta, "spread_fallback": meta.get("spread_fallback") if meta else None,
        "metrics_kind": meta.get("metrics_kind") if meta else None, "metrics": row["metrics"],
        "distribution": row["distribution"], "windows": windows, "equity_storage_rule": EQUITY_STORAGE_RULE,
        "timings": row["timings"], "live": live,
    }


@router.get("/backtests/{run_id}/trades", response_model=TradesPage, responses={404: {"description": "Unknown run"}})
def get_backtest_trades(
    run_id: int,
    window: Annotated[int | None, Query()] = None,
    offset: Annotated[int, Query()] = 0,
    limit: Annotated[int | None, Query()] = None,
    db: EngineConnection = Depends(get_db),
) -> dict[str, Any]:
    repo = BacktestsRepo(db)
    row = _run_or_404(repo, run_id)
    _check_window(row, window)
    _check_int(offset, "offset", 0, 10**9)
    _check_int(limit, "limit", 1, TRADES_LIMIT_MAX)
    eff_limit = limit or TRADES_LIMIT_DEFAULT
    trades = repo.get_trades(run_id, window, offset=offset, limit=eff_limit)
    return {"run_id": run_id, "window": window, "total": repo.count_trades(run_id, window), "offset": offset,
            "limit": eff_limit, "trades": trades}


@router.get("/backtests/{run_id}/equity", response_model=EquityOut, responses={404: {"description": "Unknown run"}})
def get_backtest_equity(
    run_id: int,
    window: Annotated[int | None, Query()] = None,
    db: EngineConnection = Depends(get_db),
) -> dict[str, Any]:
    repo = BacktestsRepo(db)
    row = _run_or_404(repo, run_id)
    _check_window(row, window)
    points = repo.get_equity(run_id, window)
    return {"run_id": run_id, "window": window, "count": len(points), "points": points}


@router.get("/backtests/{run_id}/skipped", response_model=SkippedOut, responses={404: {"description": "Unknown run"}})
def get_backtest_skipped(
    run_id: int,
    window: Annotated[int | None, Query()] = None,
    db: EngineConnection = Depends(get_db),
) -> dict[str, Any]:
    repo = BacktestsRepo(db)
    row = _run_or_404(repo, run_id)
    _check_window(row, window)
    items = repo.get_skipped(run_id, window)
    return {"run_id": run_id, "window": window, "count": len(items), "skipped": items}


# ---------------------------------------------------------------------------------------------- cancel / delete
@router.post("/backtests/{run_id}/cancel", response_model=CancelResponse,
             responses={404: {"description": "Unknown run"}, 409: {"description": "Run already finished"}})
def cancel_backtest(run_id: int, db: EngineConnection = Depends(get_db),
                    jobs: BacktestJobs = Depends(get_jobs)) -> CancelResponse:
    repo = BacktestsRepo(db)
    _run_or_404(repo, run_id)
    outcome = jobs.cancel(run_id)
    if outcome == "not_active":
        status = (repo.status(run_id) or {}).get("status")
        raise api_error(409, "not_cancellable", f"این اجرا فعال نیست و قابل لغو نیست (وضعیت: {status}).")
    if is_dev_mode():
        logger.debug("POST /backtests/%d/cancel -> %s", run_id, outcome)
    return CancelResponse(id=run_id, status="cancelled" if outcome == "cancelled" else "running", cancel_requested=True)


@router.delete("/backtests/{run_id}", response_model=DeleteResponse,
               responses={404: {"description": "Unknown run"}, 409: {"description": "Run is queued/running"}})
def delete_backtest(run_id: int, db: EngineConnection = Depends(get_db),
                    jobs: BacktestJobs = Depends(get_jobs)) -> DeleteResponse:
    if jobs.is_active(run_id):
        raise api_error(409, "run_active", "اجرای در صف یا در حال اجرا حذف نمی‌شود؛ ابتدا آن را لغو کنید.")
    outcome = BacktestsRepo(db).delete_run(run_id)
    if outcome == "not_found":
        raise api_error(404, "backtest_not_found", NOT_FOUND_FA)
    if outcome == "active":
        raise api_error(409, "run_active", "اجرای در صف یا در حال اجرا حذف نمی‌شود؛ ابتدا آن را لغو کنید.")
    return DeleteResponse(id=run_id, deleted=True)


# ---------------------------------------------------------------------------------------------- WebSocket
def final_message(snap: dict[str, Any]) -> dict[str, Any]:
    status = snap["status"]
    msg: dict[str, Any] = {"type": status, "id": snap["id"], "status": status, "percent": snap["percent"]}
    if status == "done":
        msg.update(percent=100.0, trade_count=snap["trade_count"], net_profit=snap["net_profit"],
                   net_profit_pct=snap["net_profit_pct"])
    else:
        msg.update(code=snap["error_code"] or status, message_fa=snap["message_fa"])
    return msg


def progress_message(snap: dict[str, Any]) -> dict[str, Any]:
    return {"type": "progress", "id": snap["id"], "status": snap["status"], "phase": snap["phase"],
            "percent": snap["percent"], "window": snap["window"], "window_index": snap["window_index"],
            "windows": snap["windows"], "cancel_requested": snap["cancel_requested"]}


def keepalive_message(run_id: int) -> dict[str, Any]:
    """Sent while the run is queued/running and nothing was sent for ``WS_KEEPALIVE_S`` seconds."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {"type": "keepalive", "id": run_id, "time_utc": now.isoformat().replace("+00:00", "Z")}


@router.websocket("/ws/backtests/{run_id}")
async def backtest_progress_ws(websocket: WebSocket, run_id: int) -> None:
    await websocket.accept()
    jobs: BacktestJobs | None = getattr(websocket.app.state, "backtest_jobs", None)
    if is_dev_mode():
        logger.debug("WS /ws/backtests/%d connected", run_id)
    if jobs is None:
        await websocket.send_json({"type": "error", "id": run_id, "status": None, "code": "db_unavailable",
                                   "message_fa": DB_UNAVAILABLE_FA})
        await websocket.close(code=1011)
        return
    disconnected = asyncio.Event()

    async def reader() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except Exception:  # closed by us, or the client went away
            pass
        finally:
            disconnected.set()

    reader_task = asyncio.create_task(reader())
    last: tuple[Any, ...] | None = None
    sent = 0
    keepalives = 0
    loop = asyncio.get_running_loop()
    last_sent_at = loop.time()
    try:
        while not disconnected.is_set():
            snap = await asyncio.to_thread(jobs.snapshot, run_id)
            if snap is None:
                await websocket.send_json({"type": "error", "id": run_id, "status": None, "code": "backtest_not_found",
                                           "message_fa": NOT_FOUND_FA})
                await websocket.close(code=4404)
                return
            if snap["status"] in TERMINAL_STATUSES:
                final = final_message(snap)
                await websocket.send_json(final)
                await websocket.close(code=1000)
                if is_dev_mode():
                    logger.debug("WS /ws/backtests/%d: %d progress message(s), final %s", run_id, sent, final["type"])
                return
            key = (snap["status"], snap["phase"], snap["percent"], snap["window_index"], snap["cancel_requested"])
            if key != last:
                await websocket.send_json(progress_message(snap))
                last = key
                sent += 1
                last_sent_at = loop.time()
            elif loop.time() - last_sent_at >= WS_KEEPALIVE_S:  # queued/running, nothing sent for a while
                await websocket.send_json(keepalive_message(run_id))
                keepalives += 1
                last_sent_at = loop.time()
                if is_dev_mode():
                    logger.debug("WS /ws/backtests/%d: keepalive #%d (%s, idle >= %.1f s)", run_id, keepalives,
                                 snap["status"], WS_KEEPALIVE_S)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(disconnected.wait(), timeout=min(WS_POLL_S, WS_KEEPALIVE_S))
        if is_dev_mode():
            logger.debug("WS /ws/backtests/%d: client disconnected after %d progress message(s), %d keepalive(s)",
                         run_id, sent, keepalives)
    except (WebSocketDisconnect, RuntimeError) as exc:
        if is_dev_mode():
            logger.debug("WS /ws/backtests/%d closed: %s", run_id, type(exc).__name__)
    finally:
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader_task
        if websocket.client_state == WebSocketState.CONNECTED and disconnected.is_set() is False:
            with contextlib.suppress(Exception):
                await websocket.close()


__all__ = [
    "BacktestRequest",
    "PeriodLimits",
    "final_message",
    "get_jobs",
    "keepalive_message",
    "period_limits",
    "progress_message",
    "router",
]
