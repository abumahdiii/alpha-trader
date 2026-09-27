"""``/strategies`` REST routes (mounted by ``create_app``).

* ``GET /strategies`` -- every registered strategy with its active params version.
* ``GET /strategies/{name}`` -- one strategy; 404 if not registered.
* ``PUT /strategies/{name}`` -- body ``{"params": {...}}``; FULL replacement (keys left out take their
  schema defaults). Same params as the active version -> that version, ``created_new_version=false``;
  different -> params version n+1 becomes active. Invalid -> 422 with Persian messages.

Errors use ``{"detail": {"code": ..., "message_fa": ..., "errors_fa": [...]}}``.
Dependencies read ``request.app.state.db`` (an ``EngineConnection``, via the shared ``routes.get_db``) and, optionally,
``request.app.state.strategy_registry`` (defaults to the process-wide registry).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ..logging_setup import get_logger, is_dev_mode
from ..storage.db import EngineConnection
from ..storage.strategies_repo import (
    ParamsValidationError,
    StoredParamsInvalidError,
    StrategiesRepo,
    StrategyVersionRecord,
)
from ..strategy.base import Strategy
from ..strategy.params import ParamSpec, ParamValue
from ..strategy.registry import StrategyRegistry, UnknownStrategyError, default_registry
from . import get_db

logger = get_logger(__name__)

router = APIRouter(prefix="/strategies", tags=["strategies"])


class ErrorDetail(BaseModel):
    code: str
    message_fa: str
    errors_fa: list[str] = []


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class StrategyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title_fa: str
    version: int  # code version of the strategy
    params_version: int  # active params version (strategy_versions.version)
    params: dict[str, ParamValue]
    params_hash: str
    params_saved_utc: str  # ISO-8601 UTC, "Z", second precision
    param_schema: list[ParamSpec]
    # Non-empty only when the stored params no longer fit the current schema; save new params to fix.
    params_errors_fa: list[str] = []


class StrategyUpdateOut(StrategyOut):
    created_new_version: bool


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Strategy not registered"},
    409: {"model": ErrorResponse, "description": "Stored params no longer valid for the current schema"},
    422: {"model": ErrorResponse, "description": "Invalid params (Persian messages)"},
    503: {"model": ErrorResponse, "description": "Database not available"},
}


def _error(status: int, code: str, message_fa: str, errors_fa: list[str] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorDetail(code=code, message_fa=message_fa, errors_fa=errors_fa or []).model_dump(),
    )


def get_registry(request: Request) -> StrategyRegistry:
    registry = getattr(request.app.state, "strategy_registry", None)
    return registry if registry is not None else default_registry


def get_repo(
    db: EngineConnection = Depends(get_db), registry: StrategyRegistry = Depends(get_registry)
) -> StrategiesRepo:
    return StrategiesRepo(db, registry)


def _iso(record: StrategyVersionRecord) -> str:
    return record.created_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _out(cls: type[Strategy], record: StrategyVersionRecord, errors_fa: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": cls.name,
        "title_fa": cls.title_fa,
        "version": cls.version,
        "params_version": record.version,
        "params": record.params,
        "params_hash": record.params_hash,
        "params_saved_utc": _iso(record),
        "param_schema": list(cls.param_schema.specs),
        "params_errors_fa": errors_fa or [],
    }


def _strategy_out(repo: StrategiesRepo, cls: type[Strategy]) -> dict[str, Any]:
    try:
        return _out(cls, repo.get_active(cls.name))
    except StoredParamsInvalidError as exc:
        # Show the stored values with the reasons instead of hiding the strategy.
        return _out(cls, exc.record, exc.errors_fa)


def _lookup(registry: StrategyRegistry, name: str) -> type[Strategy]:
    try:
        return registry.get(name)
    except UnknownStrategyError:
        if is_dev_mode():
            logger.debug("strategy not found: %r", name)
        raise _error(404, "strategy_not_found", f"استراتژی «{name}» پیدا نشد.") from None


@router.get("", response_model=list[StrategyOut], responses=ERROR_RESPONSES)
def list_strategies(
    repo: StrategiesRepo = Depends(get_repo), registry: StrategyRegistry = Depends(get_registry)
) -> list[dict[str, Any]]:
    return [_strategy_out(repo, cls) for cls in registry.all()]


@router.get("/{name}", response_model=StrategyOut, responses=ERROR_RESPONSES)
def get_strategy(
    name: str, repo: StrategiesRepo = Depends(get_repo), registry: StrategyRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return _strategy_out(repo, _lookup(registry, name))


@router.put("/{name}", response_model=StrategyUpdateOut, responses=ERROR_RESPONSES)
def put_strategy(
    name: str,
    payload: Any = Body(..., examples=[{"params": {"channel_period": 100}}]),
    repo: StrategiesRepo = Depends(get_repo),
    registry: StrategyRegistry = Depends(get_registry),
) -> dict[str, Any]:
    cls = _lookup(registry, name)
    shape_errors: list[str] = []
    if not isinstance(payload, Mapping) or "params" not in payload:
        shape_errors.append("بدنه درخواست باید به شکل {\"params\": {...}} باشد.")
    else:
        shape_errors.extend(f"فیلد ناشناخته در بدنه درخواست: {k!r}." for k in payload if k != "params")
        if not isinstance(payload["params"], Mapping):
            shape_errors.append("مقدار «params» باید یک شیء کلید-مقدار باشد.")
    if shape_errors:
        if is_dev_mode():
            logger.debug("PUT /strategies/%s rejected (body shape): %s", name, shape_errors)
        raise _error(422, "invalid_body", "بدنه درخواست نامعتبر است.", shape_errors)
    try:
        record, created = repo.save_params(name, payload["params"])
    except ParamsValidationError as exc:
        raise _error(422, "invalid_params", "پارامترهای استراتژی نامعتبر هستند.", exc.errors_fa) from None
    if is_dev_mode():
        logger.debug("PUT /strategies/%s -> params v%d (new=%s)", name, record.version, created)
    return {**_out(cls, record), "created_new_version": created}
