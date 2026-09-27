"""``/backtests`` REST routes and the ``/ws/backtests/{id}`` progress WebSocket (phase 4, wave 2).

* ``POST /backtests`` -- validate, snapshot the ACTIVE params version and the account settings at request
  time, validate the period against the cached data (cheap: bars + first valid channel bar, shared LRU),
  store the run ``queued`` and hand it to ``app.state.backtest_jobs`` -> 202 ``{id, status}``.
* ``GET /backtests`` (newest first, ``limit``), ``GET /backtests/{id}`` (config, plan incl. seed, windows
  with metrics, overall metrics, distribution, labels, provisional, fingerprint, status/progress/error),
  ``GET /backtests/{id}/trades?window&offset&limit``, ``GET /backtests/{id}/equity?window``,
  ``GET /backtests/{id}/skipped?window``, ``POST /backtests/{id}/cancel``, ``DELETE /backtests/{id}``.
* ``WS /ws/backtests/{id}`` -- ``{"type": "progress", ...}`` on every change (polled every
  ``WS_POLL_S``), then ONE final ``{"type": "done"|"error"|"cancelled"|"interrupted", ...}`` and close.

The engine only reads the local cache here (never MT5) and never places orders. Errors use
``{"detail": {"code", "message_fa", "errors_fa"}}``. All times are ISO-8601 UTC with ``Z``; a ``from`` /
``to`` without an offset is taken as UTC (like ``/chart``). Full shapes: ``engine/README.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, Body, Depends, Query, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..backtest.history import HistoryUnavailable
from ..backtest.jobs import BacktestJobs, JobsClosed, first_valid_index, prepare_history
from ..backtest.models import PROVISIONAL_LABEL_FA, SEED_MAX, CostModel, RunConfig
from ..backtest.periods import PeriodError, earliest_start, manual_window, random_windows, warmup_h4_bars
from ..backtest.results import EQUITY_STORAGE_RULE
from ..logging_setup import get_logger, is_dev_mode
from ..market_data import MarketDataService
from ..storage.account_settings import AccountSettingsRepo
from ..storage.backtests_repo import ACTIVE_STATUSES, TERMINAL_STATUSES, BacktestsRepo, api_time
from ..storage.db import EngineConnection
from ..strategy.base import bar_open_times
from ..strategy.registry import StrategyRegistry
from . import DB_UNAVAILABLE_FA, api_error, get_db, get_market_data
from .chart import ChartCache, _active_params, _check_symbol, get_chart_cache
from .strategies import get_registry

logger = get_logger(__name__)
router = APIRouter(tags=["backtests"])

WS_POLL_S = 0.2
LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX = 50, 500
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
    windows_count: int | None = Field(default=None, ge=1, le=500)
    window_months: int | None = Field(default=None, ge=1, le=60)
    seed: int | None = Field(default=None, ge=0, le=SEED_MAX)
    commission_per_lot_per_side: float | None = Field(default=None, ge=0.0, le=1000.0)

    @field_validator("windows_count", "window_months", "seed", "commission_per_lot_per_side", mode="before")
    @classmethod
    def _no_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean is not a number")
        return value

    @field_validator("windows_count", "window_months", "seed", mode="before")
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
    count: int
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
        elif kind.startswith("string"):
            messages.append(f"{label} باید یک متن معتبر (۱ تا ۳۲ نویسه) باشد.")
        elif kind == "model_type" or kind == "dict_type":
            messages.append("بدنه درخواست باید یک شیء JSON باشد.")
        elif kind.startswith("int") or field in ("windows_count", "window_months", "seed"):
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
    labels = list(cost.labels_fa())
    if provisional:
        labels.insert(0, PROVISIONAL_LABEL_FA)
    return labels


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
    active = _active_params(db, registry)  # 404 strategy_not_found / 409 stored_params_invalid
    account = AccountSettingsRepo(db).get()
    settings = request.app.state.settings
    provisional = not bool(getattr(settings, "alpha_data_check_confirmed", False))
    cost = CostModel(commission_per_lot_per_side=body.commission_per_lot_per_side or 0.0)
    config = RunConfig(
        symbol=name, mode=body.mode, start=start, end=end,
        windows_count=body.windows_count if body.windows_count is not None else 20,
        window_months=body.window_months if body.window_months is not None else 3,
        seed=body.seed, cost_model=cost, account=account, strategy_name=active.strategy.name,
        strategy_version=active.code_version, params=active.params, params_version=active.record_version,
        params_hash=active.params_hash, provisional=provisional,
    )
    # Period validation now (same functions the runner uses), so the UI gets a 422 instead of a failed run.
    try:
        prepared = prepare_history(service.cache, name, lru)
    except HistoryUnavailable as exc:
        raise _history_error(exc) from None
    fv = first_valid_index(prepared, active.params, active.params_hash, lru)
    h4_ns = bar_open_times(prepared.history.h4).as_unit("ns").asi8
    try:
        earliest = earliest_start(prepared.bars.times_ns, h4_ns, fv, active.p.atr_period)
        warm = warmup_h4_bars(active.p.atr_period)
        if body.mode == "manual":
            assert start is not None and end is not None
            manual_window(start, end, prepared.bars.times_ns, earliest, warmup_bars=warm)
        else:  # seed-independent check that at least one window fits (seed 0: nothing is generated)
            random_windows(prepared.bars.times_ns, earliest, count=1, months=config.window_months, seed=0,
                           warmup_bars=warm)
    except PeriodError as exc:
        if is_dev_mode():
            logger.debug("POST /backtests %s %s: period rejected %s", name, body.mode, exc.code)
        raise _period_error(exc) from None
    labels = _run_labels(cost, provisional)
    request_json = body.model_dump(mode="json", by_alias=True, exclude_none=True)
    repo = BacktestsRepo(db)
    run_id = repo.create_run(
        request=request_json, config=config.model_dump(mode="json"), symbol=name, mode=body.mode,
        period_start=start, period_end=end,
        windows_count=config.windows_count if body.mode == "random" else None,
        window_months=config.window_months if body.mode == "random" else None,
        seed=body.seed if body.mode == "random" else None, strategy_name=config.strategy_name,
        strategy_version=config.strategy_version, params_version=config.params_version,
        params_hash=config.params_hash, provisional=provisional, labels_fa=labels,
    )
    try:
        jobs.submit(run_id, config)
    except JobsClosed:
        repo.finish(run_id, "interrupted", error_code="engine_closing", error_message_fa="engine در حال بسته شدن بود.")
        raise api_error(503, "engine_closing", "engine در حال بسته شدن است؛ اجرای جدید پذیرفته نمی‌شود.") from None
    if is_dev_mode():
        logger.debug("POST /backtests -> run %d: %s %s from=%s to=%s windows=%s months=%s seed=%s commission=%g "
                     "params v%d hash=%s account=%s earliest=%s provisional=%s", run_id, name, body.mode, start, end,
                     config.windows_count, config.window_months, body.seed, cost.commission_per_lot_per_side,
                     active.record_version, active.params_hash[:12], account.model_dump(), earliest.isoformat(),
                     provisional)
    return SubmitResponse(id=run_id, status="queued", provisional=provisional, labels_fa=labels)


# ---------------------------------------------------------------------------------------------- GET
def _summary(row: dict[str, Any]) -> dict[str, Any]:
    error = None
    if row.get("error_code") or row.get("error_message_fa"):
        error = {"code": row.get("error_code"), "message_fa": row.get("error_message_fa")}
    return {
        "id": row["id"], "created_at": api_time(row["created_utc"]), "started_at": api_time(row["started_utc"]),
        "finished_at": api_time(row["finished_utc"]), "status": row["status"], "progress": row["progress"],
        "symbol": row["symbol"], "mode": row["mode"], "from_": api_time(row["period_start_utc"]),
        "to": api_time(row["period_end_utc"]), "windows_count": row["windows_count"],
        "window_months": row["window_months"], "seed": row["seed"],
        "seed_generated": None if row["seed_generated"] is None else bool(row["seed_generated"]),
        "strategy": row["strategy_name"], "strategy_version": row["strategy_version"],
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


@router.get("/backtests", response_model=RunList, response_model_by_alias=True)
def list_backtests(
    limit: Annotated[int | None, Query()] = None,
    db: EngineConnection = Depends(get_db),
) -> dict[str, Any]:
    _check_int(limit, "limit", 1, LIST_LIMIT_MAX)
    rows = BacktestsRepo(db).list_runs(limit or LIST_LIMIT_DEFAULT)
    return {"count": len(rows), "runs": [_summary(r) for r in rows]}


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
        "result_meta": meta, "metrics_kind": meta.get("metrics_kind") if meta else None, "metrics": row["metrics"],
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
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(disconnected.wait(), timeout=WS_POLL_S)
        if is_dev_mode():
            logger.debug("WS /ws/backtests/%d: client disconnected after %d progress message(s)", run_id, sent)
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


__all__ = ["BacktestRequest", "final_message", "get_jobs", "progress_message", "router"]
