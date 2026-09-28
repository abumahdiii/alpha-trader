"""HTTP routes beyond the skeleton's ``/health`` and ``/shutdown`` (registered in ``create_app``).

Shared FastAPI dependencies live here so every router uses the same ones:

* :func:`get_market_data` -- ``request.app.state.market_data`` (503 if missing);
* :func:`get_db` -- ``request.app.state.db`` (503 ``db_unavailable`` with a Persian message if the
  database could not be opened, see ``app.py``);
* :func:`api_error` -- the structured error shape ``{"detail": {"code", "message_fa", "errors_fa"}}``;
* :func:`resolve_strategy_or_error` -- the selected (default ``stddev_channel``) strategy with its ACTIVE params
  (``strategy.resolve.resolve_active``), mapped to Persian errors: unknown name or code version -> 404
  ``strategy_not_found``, stored params that no longer validate -> 409 ``stored_params_invalid``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from ..logging_setup import get_logger, is_dev_mode
from ..market_data import MarketDataService
from ..storage.db import EngineConnection
from ..storage.strategies_repo import StoredParamsInvalidError
from ..strategy.context import DEFAULT_STRATEGY
from ..strategy.registry import StrategyRegistry, UnknownStrategyError
from ..strategy.resolve import ActiveStrategy, StrategyVersionMismatchError, resolve_active

logger = get_logger(__name__)

DB_UNAVAILABLE_FA = "پایگاه داده engine در دسترس نیست."
STORED_PARAMS_INVALID_FA = "پارامترهای ذخیره‌شده سیستم با نسخه فعلی سازگار نیستند؛ پارامترها را دوباره ذخیره کنید."


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


def resolve_strategy_or_error(
    db: EngineConnection,
    registry: StrategyRegistry,
    name: str | None = None,
    version: int | None = None,
) -> ActiveStrategy:
    """The strategy ``name`` (default ``stddev_channel``) with its active params, or a Persian API error."""
    wanted = DEFAULT_STRATEGY if name is None else name
    try:
        active = resolve_active(db, wanted, registry=registry, version=version)
    except UnknownStrategyError:
        raise api_error(404, "strategy_not_found", f"استراتژی «{wanted}» پیدا نشد.") from None
    except StrategyVersionMismatchError as exc:
        raise api_error(404, "strategy_not_found",
                        f"نسخه {exc.requested} استراتژی «{wanted}» در دسترس نیست (نسخه فعلی: {exc.available}).") from None
    except StoredParamsInvalidError as exc:
        raise api_error(409, "stored_params_invalid", STORED_PARAMS_INVALID_FA, exc.errors_fa) from None
    if is_dev_mode():
        logger.debug("strategy selected: %s (requested %s%s) params v%d hash=%s", active.identity.label(), name,
                     "" if version is None else f" v{version}", active.record_version, active.params_hash[:12])
    return active
