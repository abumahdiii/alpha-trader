"""``/plugins`` API, plugin store (DB v3 + files) and lifespan registration, end to end on a seeded tmp cache.

upload (static + sandboxed dynamic validation) -> listed in ``/plugins`` and ``/strategies`` -> usable in
``/chart/setups?strategy=`` and ``POST /backtests`` with plugin provenance (source file SHA-256); strict versions,
disable / enable, archive (blocked while a run is active), restart registration and tampered files.
Never MT5, never the real ``data/``.
"""

from __future__ import annotations

import functools
import logging
import shutil
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alpha_engine.app import create_app
from alpha_engine.logging_setup import ROOT_LOGGER_NAME
from alpha_engine.plugins import checks
from alpha_engine.plugins.template import TEMPLATE_FILENAME, TEMPLATE_SOURCE
from alpha_engine.plugins.validator import source_sha256
from alpha_engine.routes import plugins as plugin_routes
from alpha_engine.storage import db as db_module
from alpha_engine.storage.db import current_schema_version, open_db
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy
from alpha_engine.strategy.registry import StrategyRegistry
from fixtures.channel_scenarios import SYMBOL
from fixtures.seed_cache import seed_cache

GOLD = SYMBOL
START, END = "2024-06-03", "2025-03-03"
MANUAL = {"symbol": GOLD, "mode": "manual", "from": "2024-09-02T00:00:00Z", "to": "2025-02-24T00:00:00Z"}
TERMINAL = {"done", "error", "cancelled", "interrupted"}
NAME = "ma_cross_demo"
SHA_V1 = source_sha256(TEMPLATE_SOURCE)
V2_SOURCE = TEMPLATE_SOURCE.replace("    version = 1\n", "    version = 2\n", 1).replace(
    'ParamSpec(name="fast", type="int", default=10,', 'ParamSpec(name="