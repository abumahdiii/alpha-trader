"""``/settings`` REST routes for account/risk settings (not yet mounted).

* ``GET /settings`` -- current :class:`AccountSettings` (stored, or the defaults).
* ``PUT /settings`` -- partial update, e.g. ``{"risk_pct": 0.5}``; returns the new settings.
  Unknown fields, nulls, booleans, non-numbers and out-of-range values -> 422 with Persian messages.

Error shape and the ``request.app.state.db`` dependency match ``routes/strategies.py``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettings, AccountSettingsRepo, AccountSettingsValidationError
from ..storage.db import EngineConnection

logger = get_logger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


def _error(status: int, code: str, message_fa: str, errors_fa: list[str] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"code": code, "message_fa": message_fa, "errors_fa": errors_fa or []}
    )


def get_db(request: Request) -> EngineConnection:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise _error(503, "db_unavailable", "پایگاه داده engine در دسترس نیست.")
    return db


def get_settings_repo(db: EngineConnection = Depends(get_db)) -> AccountSettingsRepo:
    return AccountSettingsRepo(db)


@router.get("", response_model=AccountSettings)
def get_account_settings(repo: AccountSettingsRepo = Depends(get_settings_repo)) -> AccountSettings:
    return repo.get()


@router.put("", response_model=AccountSettings)
def put_account_settings(
    payload: Any = Body(..., examples=[{"risk_pct": 0.5}]),
    repo: AccountSettingsRepo = Depends(get_settings_repo),
) -> AccountSettings:
    try:
        return repo.update(payload)
    except AccountSettingsValidationError as exc:
        if is_dev_mode():
            logger.debug("PUT /settings rejected: %s", exc.errors_fa)
        raise _error(422, "invalid_settings", "تنظیمات حساب نامعتبر است.", exc.errors_fa) from None
