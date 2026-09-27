"""FastAPI application factory.

Routes in this skeleton:

* ``GET /health``   -- liveness + MT5 connection status (always HTTP 200 while the process is up).
* ``POST /shutdown`` -- graceful stop, accepted only from the local machine.

MT5 status comes from a seam, ``app.state.mt5_status_provider: Callable[[], Mt5Status]``. The factory
creates an :class:`~alpha_engine.mt5_adapter.Mt5Adapter` (which imports ``MetaTrader5`` lazily, never
here) and plugs ``adapter.status`` in there. On startup (lifespan) it starts a background, non-blocking
connect when ``ENGINE_MT5_AUTOCONNECT`` is true; on shutdown it calls ``adapter.shutdown()``.

Market-data routes (``GET /symbols``, ``GET /rates``, ``GET /rates/meta``) live in
:mod:`alpha_engine.routes` and use ``app.state.market_data``.

``app`` is provided lazily (PEP 562 module ``__getattr__``) so that importing this module does not load
settings / the ``.env`` as a side effect; ``from alpha_engine.app import app`` and
``uvicorn alpha_engine.app:app`` both work.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from . import __version__
from .config import Settings, get_settings, mask_login
from .logging_setup import configure_logging, get_logger, is_dev_mode

logger = get_logger(__name__)

SERVICE_NAME = "alpha_engine"
LOCAL_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

Mt5State = Literal["not_initialized", "connected", "disconnected", "error", "account_mismatch"]


class Mt5Status(BaseModel):
    """MT5 connection status as reported by ``/health``. ``login_masked`` is always masked."""

    model_config = ConfigDict(extra="forbid")

    state: Mt5State = "not_initialized"
    server: str | None = None
    login_masked: str | None = None
    trade_mode: str | None = None
    message: str | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: Literal["alpha_engine"] = SERVICE_NAME
    version: str
    pid: int
    dev_mode: bool
    time_utc: str  # ISO-8601 UTC with "Z", second precision, e.g. "2026-09-26T18:00:00Z"
    mt5: Mt5Status


class ShutdownResponse(BaseModel):
    status: Literal["shutting_down"] = "shutting_down"


Mt5StatusProvider = Callable[[], Mt5Status]


def default_mt5_status_provider() -> Mt5Status:
    """Used until the MT5 adapter is plugged in."""
    return Mt5Status(state="not_initialized")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_mt5_status(app: FastAPI) -> Mt5Status:
    """Call the provider; never let it break ``/health``; never let a full login through."""
    provider: Mt5StatusProvider = getattr(app.state, "mt5_status_provider", default_mt5_status_provider)
    try:
        status = provider()
        if not isinstance(status, Mt5Status):
            status = Mt5Status.model_validate(status)
    except Exception:
        if is_dev_mode():
            logger.debug("mt5_status_provider failed", exc_info=True)
        return Mt5Status(state="error", message="MT5 status provider failed")

    if status.login_masked and not status.login_masked.startswith("****"):
        status = status.model_copy(update={"login_masked": mask_login(status.login_masked)})
    return status


def _request_exit(server: Any) -> None:
    if is_dev_mode():
        logger.debug("shutdown: signalling uvicorn server to exit")
    server.should_exit = True


def create_app(
    settings: Settings | None = None,
    mt5_status_provider: Mt5StatusProvider | None = None,
    mt5_adapter: Any | None = None,
    market_data: Any | None = None,
) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    configure_logging(settings, force=True)

    # Local imports: these modules import Mt5Status from here.
    from .market_data import MarketDataService
    from .mt5_adapter import Mt5Adapter
    from .routes import rates as rates_routes
    from .routes import symbols as symbols_routes

    adapter = mt5_adapter if mt5_adapter is not None else Mt5Adapter(settings)
    service = market_data if market_data is not None else MarketDataService.create(settings, adapter)

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        if settings.engine_mt5_autoconnect:
            if is_dev_mode():
                logger.debug("startup: connecting to MT5 in the background")
            adapter.connect_in_background()
        elif is_dev_mode():
            logger.debug("startup: MT5 autoconnect disabled")
        try:
            yield
        finally:
            if is_dev_mode():
                logger.debug("shutdown: closing MT5 connection")
            adapter.shutdown()

    app = FastAPI(title="Alpha Trader Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.mt5_adapter = adapter
    app.state.market_data = service
    app.state.mt5_status_provider = mt5_status_provider or adapter.status
    app.state.server = None  # set by __main__ to the uvicorn.Server handle
    app.include_router(symbols_routes.router)
    app.include_router(rates_routes.router)

    @app.middleware("http")
    async def dev_request_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Method, path, status and duration only: never headers, query strings or bodies.
        if not is_dev_mode():
            return await call_next(request)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.debug(
                "%s %s raised after %.1f ms",
                request.method,
                request.url.path,
                (time.perf_counter() - started) * 1000,
                exc_info=True,
            )
            raise
        logger.debug(
            "%s %s -> %d (%.1f ms)",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response

    @app.get("/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        current: Settings = request.app.state.settings
        return HealthResponse(
            version=__version__,
            pid=os.getpid(),
            dev_mode=current.dev_mode,
            time_utc=utc_now_iso(),
            mt5=_read_mt5_status(request.app),
        )

    @app.post(
        "/shutdown",
        response_model=ShutdownResponse,
        responses={403: {"description": "Request did not come from the local machine"}},
    )
    def shutdown(request: Request, background: BackgroundTasks) -> ShutdownResponse:
        host = request.client.host if request.client else None
        if host not in LOCAL_CLIENT_HOSTS:
            logger.warning("shutdown refused: request did not come from the local machine")
            raise HTTPException(status_code=403, detail="shutdown is only accepted from the local machine")
        server = getattr(request.app.state, "server", None)
        if server is not None:
            background.add_task(_request_exit, server)  # runs after the 200 has been sent
        elif is_dev_mode():
            logger.debug("shutdown: no server handle (test mode); not exiting")
        return ShutdownResponse()

    if is_dev_mode():
        logger.debug("app created: %s", settings.safe_summary())
    return app


def __getattr__(name: str) -> Any:
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
