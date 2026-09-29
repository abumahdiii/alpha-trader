"""``/signals`` REST routes and the ``/ws/signals`` WebSocket (phase 6: live signals). SUGGESTIONS ONLY.

The engine suggests positions; it never places, modifies or checks an order. There is deliberately no route that
could send anything to the broker.

* ``GET /signals?status&symbol&limit&offset`` -- stored signals, newest confirmation bar first, with ``total``.
* ``GET /signals/status`` -- the scheduler state (enabled, state, MT5 state, server offset / clock skew, last tick
  per symbol, last / next check, live strategy, last error in Persian).
* ``GET /signals/settings`` / ``POST /signals/settings`` -- ``{enabled, live_strategy, grace_s}`` (persisted in
  ``live_settings``; a partial body keeps the other fields). ``live_strategy`` must be a registered BUILT-IN
  strategy whose stored params validate: unknown -> 404 ``strategy_not_found``, an uploaded plugin -> 409
  ``plugin_not_live`` (plugins are chart + backtest only for now), invalid stored params -> 409
  ``stored_params_invalid``.
* ``GET /signals/{id}`` -- one signal; 404 ``signal_not_found``.
* ``WS /ws/signals`` -- ``snapshot`` first (status + active signals), then ``signal`` / ``expired`` /
  ``superseded`` / ``status`` as they happen (polled every ``WS_POLL_S``), and ``keepalive`` after
  ``WS_KEEPALIVE_S`` seconds without any message (same pattern as ``/ws/backtests/{id}``).

Errors: ``{"detail": {"code", "message_fa", "errors_fa"}}``; 503 ``db_unavailable`` without a database.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..logging_setup import get_logger, is_dev_mode
from ..signals.service import PLUGIN_NOT_LIVE_FA, LiveSignals
from ..storage.db import EngineConnection
from ..storage.signals_repo import GRACE_MAX_S, GRACE_MIN_S, SIGNAL_STATUSES, signal_to_api
from ..strategy.registry import StrategyRegistry
from . import DB_UNAVAILABLE_FA, api_error, get_db, resolve_strategy_or_error
from .strategies import get_registry

logger = get_logger(__name__)
router = APIRouter(tags=["signals"])

WS_POLL_S = 0.5
WS_KEEPALIVE_S = 15.0
LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX = 50, 500
LIST_OFFSET_MAX = 10**9
NOT_FOUND_FA = "سیگنال پیدا نشد."


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool | None = None
    live_strategy: str | None = Field(default=None, min_length=1, max_length=64)
    grace_s: float | int | None = Field(default=None, ge=GRACE_MIN_S, le=GRACE_MAX_S)


_FIELD_FA = {"enabled": "«فعال بودن»", "live_strategy": "«سیستم لایو»", "grace_s": "«مهلت پس از بسته شدن کندل (ثانیه)»"}


def _errors_fa(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors(include_input=False):
        field = str(err["loc"][0]) if err.get("loc") else ""
        label = _FIELD_FA.get(field, f"«{field}»")
        kind = err.get("type", "")
        if kind == "extra_forbidden":
            out.append(f"فیلد ناشناخته: {field!r}.")
        elif kind.startswith("bool"):
            out.append(f"{label} باید true یا false باشد.")
        elif kind in ("greater_than_equal", "less_than_equal"):
            out.append(f"{label} باید بین {GRACE_MIN_S:g} و {GRACE_MAX_S:g} باشد.")
        elif kind.startswith("string") or kind == "str_type":
            out.append(f"{label} باید نام یک سیستم باشد.")
        else:
            out.append(f"مقدار {label} نامعتبر است.")
    return out


# ---------------------------------------------------------------------------------------------- deps
def get_live(request: Request) -> LiveSignals:
    live = getattr(request.app.state, "live_signals", None)
    if live is None:
        raise api_error(503, "db_unavailable", DB_UNAVAILABLE_FA)
    return live


# ---------------------------------------------------------------------------------------------- REST
@router.get("/signals/status")
def signals_status(live: LiveSignals = Depends(get_live)) -> dict[str, Any]:
    return live.status()


@router.get("/signals/settings")
def get_signal_settings(live: LiveSignals = Depends(get_live)) -> dict[str, Any]:
    return live.settings.to_api()


@router.post("/signals/settings", responses={404: {"description": "Unknown strategy"},
                                            409: {"description": "Plugin / invalid stored params"},
                                            422: {"description": "Invalid body"}})
def post_signal_settings(
    body: Any = Body(default=None),
    live: LiveSignals = Depends(get_live),
    db: EngineConnection = Depends(get_db),
    registry: StrategyRegistry = Depends(get_registry),
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise api_error(422, "invalid_body", "بدنه درخواست باید یک شیء JSON باشد.")
    try:
        parsed = SettingsBody.model_validate(body)
    except ValidationError as exc:
        raise api_error(422, "invalid_body", "تنظیمات سیگنال لایو نامعتبر است.", _errors_fa(exc)) from None
    if parsed.live_strategy is not None:
        active = resolve_strategy_or_error(db, registry, parsed.live_strategy)  # 404 / 409 stored_params_invalid
        if active.identity.source == "plugin":
            raise api_error(409, "plugin_not_live", PLUGIN_NOT_LIVE_FA)
    new = live.update_settings(enabled=parsed.enabled, live_strategy=parsed.live_strategy,
                               grace_s=None if parsed.grace_s is None else float(parsed.grace_s))
    if is_dev_mode():
        logger.debug("POST /signals/settings -> %s", new.to_api())
    return {**new.to_api(), "status": live.status()}


@router.get("/signals")
def list_signals(
    status: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    limit: int = Query(default=LIST_LIMIT_DEFAULT),
    offset: int = Query(default=0),
    live: LiveSignals = Depends(get_live),
) -> dict[str, Any]:
    if status is not None and status not in SIGNAL_STATUSES:
        raise api_error(422, "invalid_query", f"وضعیت «{status}» معتبر نیست ({'، '.join(SIGNAL_STATUSES)}).")
    if symbol is not None and not 1 <= len(symbol) <= 32:
        raise api_error(422, "invalid_query", "نام نماد باید ۱ تا ۳۲ نویسه باشد.")
    if not 1 <= limit <= LIST_LIMIT_MAX:
        raise api_error(422, "invalid_query", f"«limit» باید بین ۱ و {LIST_LIMIT_MAX} باشد.")
    if not 0 <= offset <= LIST_OFFSET_MAX:
        raise api_error(422, "invalid_query", "«offset» نامعتبر است.")
    rows, total = live.repo.list_signals(status=status, symbol=symbol, limit=limit, offset=offset)
    return {"count": len(rows), "total": total, "offset": offset, "limit": limit,
            "signals": [signal_to_api(r) for r in rows]}


@router.get("/signals/{signal_id}", responses={404: {"description": "Unknown signal"}})
def get_signal(signal_id: int, live: LiveSignals = Depends(get_live)) -> dict[str, Any]:
    row = live.repo.get(signal_id)
    if row is None:
        raise api_error(404, "signal_not_found", NOT_FOUND_FA)
    return signal_to_api(row)


# ---------------------------------------------------------------------------------------------- WebSocket
def keepalive_message() -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {"type": "keepalive", "time_utc": now.isoformat().replace("+00:00", "Z")}


@router.websocket("/ws/signals")
async def signals_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    live: LiveSignals | None = getattr(websocket.app.state, "live_signals", None)
    if is_dev_mode():
        logger.debug("WS /ws/signals connected")
    if live is None:
        await websocket.send_json({"type": "error", "code": "db_unavailable", "message_fa": DB_UNAVAILABLE_FA})
        await websocket.close(code=1011)
        return
    disconnected = asyncio.Event()

    async def reader() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except Exception:
            pass
        finally:
            disconnected.set()

    reader_task = asyncio.create_task(reader())
    loop = asyncio.get_running_loop()
    sent = keepalives = 0
    try:
        snapshot = await asyncio.to_thread(live.snapshot)
        seq = int(snapshot["seq"])
        await websocket.send_json(snapshot)
        last_sent_at = loop.time()
        while not disconnected.is_set():
            messages, seq_now = live.messages_after(seq)
            seq = seq_now
            for message in messages:
                await websocket.send_json(message)
                sent += 1
                last_sent_at = loop.time()
            if not messages and loop.time() - last_sent_at >= WS_KEEPALIVE_S:
                await websocket.send_json(keepalive_message())
                keepalives += 1
                last_sent_at = loop.time()
                if is_dev_mode():
                    logger.debug("WS /ws/signals: keepalive #%d", keepalives)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(disconnected.wait(), timeout=min(WS_POLL_S, WS_KEEPALIVE_S))
        if is_dev_mode():
            logger.debug("WS /ws/signals: client disconnected after %d message(s), %d keepalive(s)", sent, keepalives)
    except (WebSocketDisconnect, RuntimeError) as exc:
        if is_dev_mode():
            logger.debug("WS /ws/signals closed: %s", type(exc).__name__)
    finally:
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader_task
        if websocket.client_state == WebSocketState.CONNECTED and not disconnected.is_set():
            with contextlib.suppress(Exception):
                await websocket.close()


__all__ = ["WS_KEEPALIVE_S", "get_live", "keepalive_message", "router"]
