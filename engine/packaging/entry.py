"""PyInstaller entry point for the frozen engine (``alpha_engine.exe``).

``alpha_engine/__main__.py`` uses package-relative imports, so it cannot be the frozen script itself;
this wrapper imports it as ``alpha_engine.__main__`` and delegates. In dev nothing changes:
``python -m alpha_engine`` never runs this file.

Frozen-startup concerns handled here (and only here):

1. The bundle is built console-less (``console=False`` in ``alpha_engine.spec``) so no console window
   ever pops up next to the Flutter app. A GUI-subsystem process that is started *without* inherited
   std handles gets ``sys.stdout``/``sys.stderr`` = ``None``; uvicorn's default log formatter calls
   ``sys.stdout.isatty()`` at startup and would crash. :func:`ensure_std_streams` points missing
   streams at ``os.devnull``. When Flutter passes pipes (it always does), the real pipes are kept, so
   DEV_MODE output still reaches ``[engine:stderr]`` in the app's diagnostic log.
2. A frozen interpreter ignores ``PYTHON*`` environment variables, so the ``PYTHONIOENCODING=utf-8`` /
   ``PYTHONUNBUFFERED=1`` that Flutter passes have no effect. :func:`configure_utf8_streams` makes the
   existing stdout/stderr UTF-8 (``errors="replace"``) and line-buffered instead, matching what the
   Flutter side decodes (``Utf8Decoder``).
3. ``multiprocessing.freeze_support()`` -- a no-op today, but required before any future
   ``multiprocessing`` child (e.g. the strategy-plugin worker) can start from a frozen exe.

HOOK (strategy-plugin worker, not implemented): a future ``--plugin-worker`` style flag belongs in
``alpha_engine.__main__.main()`` (argv is passed through untouched), so dev and frozen share one code
path. Do not parse it here; this file must stay a thin, logic-free wrapper.
"""

from __future__ import annotations

import multiprocessing
import os
import sys


def ensure_std_streams() -> list[str]:
    """Replace missing ``sys.stdout``/``sys.stderr``/``sys.stdin`` with ``os.devnull``.

    Returns the names of the replaced streams (for tests). Existing streams are never touched.
    """
    replaced: list[str] = []
    for name, mode in (("stdout", "w"), ("stderr", "w"), ("stdin", "r")):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))  # noqa: SIM115 - process lifetime
            replaced.append(name)
    return replaced


def configure_utf8_streams() -> list[str]:
    """Reconfigure stdout/stderr to UTF-8, ``errors="replace"``, line-buffered where possible.

    Returns the names of the reconfigured streams. Streams without ``reconfigure`` (e.g. test
    doubles) are left alone.
    """
    done: list[str] = []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (OSError, ValueError):  # detached / closed stream: keep going, never block startup
            continue
        done.append(name)
    return done


def run() -> int:
    multiprocessing.freeze_support()
    ensure_std_streams()
    configure_utf8_streams()
    from alpha_engine.__main__ import main

    return main()


if __name__ == "__main__":
    sys.exit(run())
