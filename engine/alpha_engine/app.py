"""FastAPI application factory.

Routes in this skeleton:

* ``GET /health``   -- liveness + MT5 connection status (always HTTP 200 while the process is up).
* ``POST /shutdown`` -- graceful stop, accepted only from the local machine.

MT5 status comes from a seam, ``app.state.mt5_status_provider: Callable[[], Mt5Status]``. The factory
creates an :class:`~alpha_engine.mt5_adapter.Mt5Adapter` (which imports ``MetaTrader5`` lazily, never
here) and plugs ``adapter.status`` in there. On startup (lifespan) it starts a background, non-blocking
connect when ``ENGINE_MT5_AUTOCONNECT`` is true; on shutdown it calls ``adapter.shutdown()``.

Market-data routes (``GET /symbols``, ``GET /rates``, ``GET /rates/meta``, ``GET /rates/gaps``,
``POST /rates/update``) live in :mod:`alpha_engine.routes` and use ``app.state.market_data``.

Chart routes (``GET /chart/channel``, ``GET /chart/setups``; cache-only, read-only) use
``app.state.market_data``, ``app.state.db`` and a bounded in-process cache ``app.state.chart_cache``.

Backtest routes (``POST|GET /backtests``, ``GET /backtests/{id}[/trades|/equity|/skipped]``,
``POST /backtests/{id}/cancel``, ``DELETE /backtests/{id}``, ``WS /ws/backtests/{id}``) use
``app.state.backtest_jobs`` (:class:`~alpha_engine.backtest.jobs.BacktestJobs`: one background worker thread),
created by the lifespan right after the database is opened; runs left ``queued``/``running`` by a previous
process are marked ``interrupted`` first. On shutdown the jobs are stopped (running run cancelled and joined
briefly) BEFORE the database is closed.

Live-signal routes (``GET /signals``, ``GET /signals/{id}``, ``GET /signals/status``, ``GET|POST /signals/settings``,
``WS /ws/signals``; :mod:`alpha_engine.routes.signals`) use ``app.state.live_signals``
(:class:`~alpha_engine.signals.service.LiveSignals`: one scheduler thread, SUGGESTIONS ONLY), started by the lifespan
after the backtest jobs and stopped before them (and before the database closes). It does nothing until the user
enables it.

Plugin routes (``GET /plugins/template``, ``GET|POST /plugins``, ``POST /plugins/{name}/{version}/enable|disable``,
``DELETE /plugins/{name}/{version}``; :mod:`alpha_engine.routes.plugins`): the lifespan registers every stored
plugin's latest active version right after the database opened (:func:`load_plugins`; a broken file is logged and
skipped) and removes them from the registry on shutdown. Uploaded code only ever runs in a sandboxed worker
process (:mod:`alpha_engine.plugins`).

Strategy and account-settings routes (``GET /strategies``, ``GET|PUT /strategies/{name}``,
``GET|PUT /settings``) use ``app.state.db`` and ``app.state.strategy_registry``:

* the factory imports :mod:`alpha_engine.strategies` (which registers ``stddev_channel`` in the default
  registry) and sets ``app.state.strategy_registry``;
* the lifespan opens the SQLite database ``<data_dir>/alpha.db`` (:func:`~alpha_engine.storage.db.open_db`,
  migrations applied) into ``app.state.db`` on startup and closes it on shutdown. If it cannot be opened
  (e.g. written by a newer engine), the error is logged, ``app.state.db`` stays ``None`` and those routes
  answer 503 ``db_unavailable`` -- ``/health`` keeps working either way. Outside the lifespan (a
  ``TestClient`` used without ``with``) no database file is created at all.

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
from .storage.db import EngineConnection, default_db_path, open_db

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


def open_engine_db(settings: Settings) -> EngineConnection | None:
    """Open ``<data_dir>/alpha.db`` (creating/migrating it); ``None`` (error logged) if that fails."""
    try:
        path = default_db_path(settings)
        db = open_db(path)
    except Exception as exc:
        # Not fatal for the process: /health must keep answering; DB routes answer 503 instead.
        logger.error("engine database could not be opened (%s: %s); /strategies and /settings will answer 503",
                     type(exc).__name__, exc)
        if is_dev_mode():
            logger.debug("startup: database open failed", exc_info=True)
        return None
    if is_dev_mode():
        logger.debug("startup: engine database opened at %s", path)
    return db


def close_engine_db(app: FastAPI) -> None:
    """Close ``app.state.db`` (if open) and set it to ``None``. Never raises."""
    db: EngineConnection | None = getattr(app.state, "db", None)
    app.state.db = None
    if db is None:
        return
    try:
        with db.lock:  # never close under a writer that is mid-transaction (e.g. the backtest worker)
            db.close()
    except Exception:
        logger.warning("engine database did not close cleanly")
        if is_dev_mode():
            logger.debug("shutdown: database close failed", exc_info=True)
        return
    if is_dev_mode():
        logger.debug("shutdown: engine database closed")


def start_backtest_jobs(app: FastAPI) -> Any:
    """Mark stale runs interrupted and create ``app.state.backtest_jobs`` (``None`` without a database)."""
    from .backtest.jobs import BacktestJobs
    from .storage.backtests_repo import BacktestsRepo

    db: EngineConnection | None = getattr(app.state, "db", None)
    if db is None:
        return None
    try:
        stale = BacktestsRepo(db).mark_stale_interrupted()
    except Exception as exc:
        logger.error("backtest runs could not be checked at startup (%s: %s)", type(exc).__name__, exc)
        return None
    jobs = BacktestJobs(db, app.state.market_data.cache, getattr(app.state, "chart_cache", None),
                        registry=getattr(app.state, "strategy_registry", None))
    if is_dev_mode():
        logger.debug("startup: backtest jobs ready (%d stale run(s) marked interrupted)", stale)
    return jobs


def stop_backtest_jobs(app: FastAPI) -> None:
    """Shut the backtest worker down (never raises)."""
    jobs = getattr(app.state, "backtest_jobs", None)
    app.state.backtest_jobs = None
    if jobs is None:
        return
    try:
        jobs.shutdown()
    except Exception:
        logger.warning("backtest jobs did not shut down cleanly")
        if is_dev_mode():
            logger.debug("shutdown: backtest jobs shutdown failed", exc_info=True)


def start_live_signals(app: FastAPI) -> Any:
    """Create and start ``app.state.live_signals`` (the H1-close scheduler; ``None`` without a database).

    The thread idles until the user enables live signals; with MT5 unreachable its state is ``mt5_down``."""
    from .signals.service import LiveSignals

    db: EngineConnection | None = getattr(app.state, "db", None)
    if db is None:
        return None
    try:
        market = app.state.market_data
        live = LiveSignals(db, market, registry=getattr(app.state, "strategy_registry", None),
                           lru=getattr(app.state, "chart_cache", None),
                           clock=getattr(market, "clock", None) or (lambda: datetime.now(timezone.utc)))
        live.start()
    except Exception as exc:
        logger.error("live signals could not be started (%s: %s)", type(exc).__name__, exc)
        if is_dev_mode():
            logger.debug("startup: live signals failed", exc_info=True)
        return None
    if is_dev_mode():
        logger.debug("startup: live signals scheduler ready (%s)", live.settings.to_api())
    return live


def stop_live_signals(app: FastAPI) -> None:
    """Stop the live-signal scheduler (never raises); before the database closes."""
    live = getattr(app.state, "live_signals", None)
    app.state.live_signals = None
    if live is None:
        return
    try:
        live.shutdown()
    except Exception:
        logger.warning("live signals did not shut down cleanly")
        if is_dev_mode():
            logger.debug("shutdown: live signals shutdown failed", exc_info=True)


def load_plugins(app: FastAPI) -> set[str]:
    """Register every stored plugin's latest ACTIVE version in the app's registry (after the database opened).

    A broken or modified plugin file is logged and skipped; nothing here can stop the engine from starting."""
    db: EngineConnection | None = getattr(app.state, "db", None)
    if db is None:
        return set()
    try:
        from .plugins.store import PluginStore, register_active_plugins

        store = PluginStore(db, app.state.settings.data_dir)
        names = set(register_active_plugins(store, app.state.strategy_registry))
    except Exception as exc:
        logger.error("strategy plugins could not be loaded (%s: %s)", type(exc).__name__, exc)
        if is_dev_mode():
            logger.debug("startup: plugin loading failed", exc_info=True)
        return set()
    if is_dev_mode():
        logger.debug("startup: %d strategy plugin(s) registered: %s", len(names), sorted(names))
    return names


def unload_plugins(app: FastAPI) -> None:
    """Remove the plugins this app registered from its registry (never raises; keeps a shared registry clean)."""
    names = getattr(app.state, "plugin_names", None) or set()
    app.state.plugin_names = set()
    registry = getattr(app.state, "strategy_registry", None)
    for name in sorted(names):
        try:
            if registry is not None and name in registry and getattr(registry.get(name), "source", "") == "plugin":
                registry.unregister(name)
        except Exception:
            logger.warning("plugin %s could not be unregistered at shutdown", name)


def create_app(
    settings: Settings | None = None,
    mt5_status_provider: Mt5StatusProvider | None = None,
    mt5_adapter: Any | None = None,
    market_data: Any | None = None,
    strategy_registry: Any | None = None,
) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    configure_logging(settings, force=True)

    # Local imports: these modules import Mt5Status from here.
    from . import strategies  # noqa: F401  (importing the package registers every code-defined strategy)
    from .market_data import MarketDataService
    from .mt5_adapter import Mt5Adapter
    from .routes import backtests as backtests_routes
    from .routes import chart as chart_routes
    from .routes import plugins as plugins_routes
    from .routes import rates as rates_routes
    from .routes import settings as settings_routes
    from .routes import signals as signals_routes
    from .routes import strategies as strategies_routes
    from .routes import symbols as symbols_routes
    from .strategy.registry import default_registry

    adapter = mt5_adapter if mt5_adapter is not None else Mt5Adapter(settings)
    service = market_data if market_data is not None else MarketDataService.create(settings, adapter)
    registry = strategy_registry if strategy_registry is not None else default_registry

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        if settings.engine_mt5_autoconnect:
            if is_dev_mode():
                logger.debug("startup: connecting to MT5 in the background")
            adapter.connect_in_background()
        elif is_dev_mode():
            logger.debug("startup: MT5 autoconnect disabled")
        try:
            app_.state.db = open_engine_db(settings)
            app_.state.backtest_jobs = start_backtest_jobs(app_)
            app_.state.plugin_names = load_plugins(app_)
            app_.state.live_signals = start_live_signals(app_)
            yield
        finally:
            try:
                stop_live_signals(app_)
                stop_backtest_jobs(app_)
                unload_plugins(app_)
                close_engine_db(app_)
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
    app.state.db = None  # opened by the lifespan
    app.state.backtest_jobs = None  # created by the lifespan (needs the database)
    app.state.live_signals = None  # created by the lifespan (needs the database), stopped before it closes
    app.state.plugin_names = set()  # plugin names registered by this app (lifespan + /plugins routes)
    app.state.strategy_registry = registry
    app.state.chart_cache = chart_routes.ChartCache()
    app.include_router(symbols_routes.router)
    app.include_router(rates_routes.router)
    app.include_router(strategies_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(chart_routes.router)
    app.include_router(backtests_routes.router)
    app.include_router(plugins_routes.router)
    app.include_router(signals_routes.router)

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
        logger.debug("app created: %s strategies=%s", settings.safe_summary(), registry.names())
    return app


def __getattr__(name: str) -> Any:
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
