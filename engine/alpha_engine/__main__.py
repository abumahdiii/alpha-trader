"""Entry point: ``python -m alpha_engine`` (run from the ``engine/`` directory).

Starts uvicorn programmatically, bound to 127.0.0.1 only (never 0.0.0.0), on ``ENGINE_PORT``
(validated 1024-65535 by the settings). Single process: no reload, no extra workers.
``POST /shutdown`` stops it gracefully via ``server.should_exit``.
"""

from __future__ import annotations

import sys

import uvicorn

from .app import create_app
from .config import Settings, get_settings
from .logging_setup import get_logger, is_dev_mode

HOST = "127.0.0.1"  # deliberately not configurable

logger = get_logger(__name__)


def build_server(settings: Settings | None = None) -> uvicorn.Server:
    settings = settings if settings is not None else get_settings()
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=HOST,
        port=settings.engine_port,
        reload=False,
        workers=1,
        access_log=False,  # the DEV_MODE middleware logs requests (without headers)
        log_level="debug" if settings.dev_mode else "warning",
    )
    server = uvicorn.Server(config)
    app.state.server = server
    return server


def main() -> int:
    try:
        settings = get_settings()
    except ValueError as exc:  # pydantic ValidationError; inputs are hidden from the message
        print(f"alpha_engine: invalid configuration: {exc}", file=sys.stderr)
        return 2
    server = build_server(settings)
    if is_dev_mode():
        logger.debug("starting uvicorn on http://%s:%d", HOST, settings.engine_port)
    server.run()
    if is_dev_mode():
        logger.debug("uvicorn stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
