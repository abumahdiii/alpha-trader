"""Entry point: ``python -m alpha_engine`` (run from the ``engine/`` directory).

Starts uvicorn programmatically, bound to 127.0.0.1 only (never 0.0.0.0), on ``ENGINE_PORT``
(validated 1024-65535 by the settings). Single process: no reload, no extra workers.
``POST /shutdown`` stops it gracefully via ``server.should_exit``.
"""

from __future__ import annotations

import sys

WORKER_FLAG = "--plugin-worker"
PLUGIN_CHECK_FLAG = "--plugin-check"

# The sandboxed plugin worker (alpha_engine/plugins/worker.py) must start with nothing but the standard library
# loaded: dispatch BEFORE uvicorn / the app / the settings are imported (``python -m alpha_engine --plugin-worker``).
if __name__ == "__main__" and sys.argv[1:2] == [WORKER_FLAG]:
    from .plugins.worker import run_worker

    sys.exit(run_worker())

import uvicorn  # noqa: E402

from .app import create_app  # noqa: E402
from .config import Settings, get_settings  # noqa: E402
from .logging_setup import get_logger, is_dev_mode  # noqa: E402

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


def plugin_check(path: str) -> int:
    """``--plugin-check FILE``: the upload checks (static + sandboxed dynamic) on a local file; JSON report on
    stdout, exit 0 if the plugin would be accepted. Reads no settings / ``.env``; never touches ``data/``."""
    import json
    from pathlib import Path

    from .plugins.checks import validate_plugin

    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        print(json.dumps({"ok": False, "errors_fa": [f"فایل خوانده نشد: {exc.strerror or exc}"]}, ensure_ascii=False))
        return 2
    result = validate_plugin(data)
    print(json.dumps(result.report(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == [WORKER_FLAG]:  # frozen builds call main() from their own entry script
        from .plugins.worker import run_worker

        return run_worker()
    if args[:1] == [PLUGIN_CHECK_FLAG]:
        if len(args) != 2:
            print(f"usage: {PLUGIN_CHECK_FLAG} path/to/plugin.py", file=sys.stderr)
            return 2
        return plugin_check(args[1])
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
