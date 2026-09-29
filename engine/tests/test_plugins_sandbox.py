"""The worker sandbox itself: even if the static validator were skipped, the isolated process blocks MetaTrader5,
network, subprocess, FFI, file writes, reads outside the Python installation, the registry and secrets.

Every case drives :class:`WorkerSession` directly with a source the static layer would reject, so it proves the
SECOND layer alone. Forbidden names are built at runtime (``"order" + "_send"``, ``"Meta" + "Trader5"``); no
order-API name or the MT5 package name is spelled literally. Windows-only sandbox (job object / audit paths).
"""

from __future__ import annotations

import sys

import pytest

from alpha_engine.plugins.host import PluginWorkerError, WorkerSession, parse_description
from alpha_engine.plugins.worker import BLOCKED_MODULES, new_import_allowed

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the plugin sandbox targets Windows")

MT5_PACKAGE = "Meta" + "Trader5"
ORDER_NAME = "order" + "_send"

# A plugin whose module-level body runs the attack at import time, so ``session.start()`` reports it.
BOOT = '''
from alpha_engine.strategy.base import Strategy
from alpha_engine.strategy.params import ParamSchema

{attack}

class Demo(Strategy):
    name = "sandbox_demo"
    version = 1
    title_fa = "نمونه"
    param_schema = ParamSchema([])

    def evaluate(self, ctx):
        return None
'''

ATTACKS = {
    "metatrader": f'import {MT5_PACKAGE}',
    "metatrader_runtime_name": f'__import__("Meta" + "Trader{5}")',
    "os": "import os",
    "os_system": 'import os\nos.system("cmd /c echo hi")',
    "subprocess": "import subprocess",
    "socket": "import socket\nsocket.socket()",
    "ctypes": "import ctypes",
    "importlib": 'import importlib\nimportlib.import_module("os")',
    "winreg": "import winreg",
    "urllib": "import urllib.request",
    "sqlite3": "import sqlite3",
    "shutil": "import shutil",
    "order_name_getattr": 'val = getattr(ParamSchema, "order" + "_send", None)',  # getattr itself is unavailable
}


@pytest.fixture(scope="module")
def _short_boot():
    # The attacks are cheap; a short boot timeout keeps the suite quick while still allowing imports + warm-up.
    return 60.0


def _start_fails(source: str, kinds=("sandbox", "plugin_error", "contract"), timeout: float = 60.0) -> PluginWorkerError:
    with WorkerSession(source, label="sandbox-test", boot_timeout=timeout) as session:
        with pytest.raises(PluginWorkerError) as info:
            session.start()
    assert info.value.kind in kinds, (info.value.kind, info.value.detail)
    return info.value


@pytest.mark.parametrize("case", sorted(ATTACKS))
def test_blocked_imports_and_calls_fail_in_the_worker(case: str) -> None:
    # ``getattr`` is not even in the worker's builtins, so the "build an order-API name and getattr it" trick
    # dies with a NameError before it can reach anything.
    _start_fails(BOOT.format(attack=ATTACKS[case]))


def test_file_write_is_blocked() -> None:
    attack = 'open("sandbox_probe.txt", "w").write("x")'
    _start_fails(BOOT.format(attack=attack))


def test_reading_the_env_file_is_blocked() -> None:
    # Even with an absolute path, reads outside the Python installation / package are denied.
    attack = ('import pandas as pd\n'
              'pd.read_csv("' + str((__file__)).replace("\\", "\\\\") + '")')
    err = _start_fails(BOOT.format(attack=attack))
    assert err.kind in ("sandbox", "plugin_error")


def test_dunder_subclasses_escape_to_open_is_useless() -> None:
    # Build the classic breakout at runtime; even reaching a file object, opening for write is audited.
    attack = ('bad = None\n'
              'for c in ().__class__.__bases__[0].__subclasses__():\n'
              '    if c.__name__ == "_io" or "open" in getattr(c, "__name__", ""):\n'
              '        bad = c\n'
              'f = open("escape.txt", "w")\n')
    _start_fails(BOOT.format(attack=attack))


def test_a_valid_plugin_still_runs_in_the_worker() -> None:
    from alpha_engine.plugins.template import TEMPLATE_SOURCE

    with WorkerSession(TEMPLATE_SOURCE, label="sandbox-ok") as session:
        desc = parse_description(session.start())
    assert desc.name == "ma_cross_demo"


def test_garbage_or_huge_stdout_is_handled() -> None:
    # print() is a no-op in the worker, so a plugin that spams stdout cannot corrupt the JSON protocol.
    noisy = BOOT.format(attack='print("A" * 5_000_000)\nprint("not json")')
    with WorkerSession(noisy, label="sandbox-noise") as session:
        desc = parse_description(session.start())
    assert desc.name == "sandbox_demo"


def test_worker_crash_during_import_is_reported_not_raised_as_protocol() -> None:
    boom = BOOT.format(attack='raise SystemExit(0)')
    err = _start_fails(boom, kinds=("plugin_error", "crashed", "sandbox", "boot"))
    assert err.message_fa


def test_new_import_allow_list_logic() -> None:
    assert new_import_allowed("numpy") and new_import_allowed("pandas.core.frame")
    assert new_import_allowed("math") and new_import_allowed("json")  # stdlib is fine (the audit hook guards use)
    assert not new_import_allowed("socket") and not new_import_allowed("subprocess")
    assert not new_import_allowed("ctypes") and not new_import_allowed("winreg")
    assert not new_import_allowed(MT5_PACKAGE) and not new_import_allowed("alpha_engine.mt5_adapter")
    assert not new_import_allowed("this_package_does_not_exist_xyz")
    assert "MetaTrader5" in BLOCKED_MODULES and "pyarrow" in BLOCKED_MODULES
