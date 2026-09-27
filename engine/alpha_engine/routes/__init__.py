"""HTTP routes beyond the skeleton's ``/health`` and ``/shutdown`` (registered in ``create_app``).

Shared FastAPI dependencies live here so every router uses the same ones:

* :func:`get_market_data` -- ``request.app.state.market_data`` (503 if missing);
* :func:`get_db` -- ``request.app.state.db`` (503 ``db_unavailable`` with a Persian message if the
  database could not be opened, see ``app.py``);
* :func:`api_error` -- the structured error shape ``{"detail": {"code", "message_fa", "errors_fa"}}``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from ..market_data import MarketDataService
from ..storage.db import EngineConnection

DB_UNAVAILABLE_FA = "پایگاه داده engine در دسترس نیست."


def api_error(status: int, code: str, message_fa: str, errors_fa: list[str] | None = None) -> HTTPException:
    """``HTTPException`` with ``detail = {"code", "message_fa", "errors_fa"}``."""
    return HTTPException(
        status_code=status, detail={"code": code, "message_fa": message_fa, "errors_fa": list(errors_fa or [])}
    )


def get_market_data(request: Request) -> MarketDataService:
    service = getattr(request.app.state, "market_data", None)
    if service is None:  # pragma: no cover - create_app always sets it
        raise HTTPException(status_code=503, detail="market data service not available")
    return service


def get_db(request: Request) -> EngineConnection:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise api_error(503, "db_unavailable", DB_UNAVAILABLE_FA)
    return db
