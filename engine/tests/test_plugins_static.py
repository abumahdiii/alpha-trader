"""Static plugin validation (``alpha_engine.plugins.validator``): the first layer, parent-side, never executes code.

Every malicious sample of the plugin task is rejected here with a Persian message (line number where it has one);
``test_plugins_sandbox.py`` proves the worker blocks the same things when this layer is skipped. The order-API
names are never spelled in this file (they are built as ``"order" + "_send"`` where needed).
"""

from __future__ import annotations

import pytest

from alpha_engine.plugins.template import TEMPLATE_FILENAME, TEMPLATE_SOURCE
from alpha_engine.plugins.validator import (
    BANNED_NAMES,
    ENGINE_MODULES,
    MAX_SOURCE_BYTES,
    import_allowed,
    source_sha256,
    validate_source,
)

ORDER_NAME = "order" + "_send"
MT5_PACKAGE = "Meta" + "Trader5"

# A minimal valid plugin; ``{body}`` is inserted at module level, ``{method}`` inside evaluate().
SKELETON = '''
from alpha_engine.strategy.base import Strategy
from alpha_engine.strategy.params import ParamSchema

{body}

class Demo(Strategy):
    name = "demo_plugin"
    version = 1
    title_fa = "نمونه"
    param_schema = ParamSchema([])

    def evaluate(self, ctx):
        {method}
        return None
'''


def _src(body: str = "", method: str = "pass") -> str:
    return SKELETON.format(body=body, method=method)


def _errors(source: str | bytes) -> list[str]:
    report = validate_source(source)
    assert not report.ok
    return report.errors_fa


def test_template_passes_and_exposes_its_identity() -> None:
    report = validate_source(TEMPLATE_SOURCE)
    assert report.ok, report.errors_fa
    assert (report.name, report.version, report.class_name) == ("ma_cross_demo", 1, "MovingAverageCross")
    assert report.title_fa and report.sha256 == source_sha256(TEMPLATE_SOURCE)
    assert report.checks == ["size_encoding", "syntax", "order_api_names", "imports_names_attributes",
                             "strategy_class"]
    assert TEMPLATE_FILENAME.endswith(".py")
    # self-explanatory for humans and agents: contract, prohibitions, imports, self-test
    for needle in ("THE INPUT", "THE OUTPUT", "PROHIBITED", "ALLOWED IMPORTS", "SELF-TEST", "--plugin-check",
                   "resolve_levels", "prefix equivalence", "future mutation", "قواعد اصلی"):
        assert needle in TEMPLATE_SOURCE, needle


def test_minimal_skeleton_is_valid() -> None:
    assert validate_source(_src()).ok


MALICIOUS = {
    "import_mt5": (_src(f"import {MT5_PACKAGE}"), "متاتریدر"),
    "from_mt5": (_src(f"from {MT5_PACKAGE} import initialize"), "متاتریدر"),
    "dunder_import_built": (_src('m = __import__("Meta" + "Trader5")'), "__import__"),
    "importlib": (_src("import importlib"), "importlib"),
    "os": (_src("import os"), "«os»"),
    "subprocess": (_src("import subprocess"), "subprocess"),
    "socket": (_src("import socket"), "socket"),
    "ctypes": (_src("import ctypes"), "ctypes"),
    "sys": (_src("import sys"), "«sys»"),
    "open": (_src(method='open("x.txt", "w").write("1")'), "«open»"),
    "eval": (_src(method='eval("1 + 1")'), "«eval»"),
    "exec": (_src(method='exec("x = 1")'), "«exec»"),
    "compile": (_src(method='compile("1", "f", "eval")'), "«compile»"),
    "subclasses_escape": (_src(method="().__class__.__bases__[0].__subclasses__()"), "__class__"),
    "getattr_globals": (_src(method='getattr(self.evaluate, "__glo" + "bals__")'), "«getattr»"),
    "order_name_built": (_src(method='getattr(ctx, "order" + "_send")(None)'), "«getattr»"),
    "order_name_literal": (_src(method=f"ctx.{ORDER_NAME}(None)"), ORDER_NAME),
    "order_name_comment": (_src(f"# call {ORDER_NAME} later"), ORDER_NAME),
    "order_name_string": (_src(f'X = "{ORDER_NAME}"'), ORDER_NAME),
    "builtins_via_name": (_src(method="__builtins__"), "__builtins__"),
    "type_escape": (_src(method="type(ctx).mro()"), "«type»"),
    "frame_walk": (_src(method="ctx.f_back.f_globals"), "f_back"),
    "relative_import": (_src("from . import x"), "نسبی"),
    "star_import": (_src("from numpy import *"), "import *"),
    "engine_package_import": (_src("import alpha_engine.strategy"), "from alpha_engine.strategy import"),
    "engine_config": (_src("from alpha_engine import config"), "زیرماژول"),
    "engine_storage": (_src("from alpha_engine.storage.db import open_db"), "alpha_engine.storage.db"),
    "engine_mt5_adapter": (_src("from alpha_engine.mt5_adapter import Mt5Adapter"), "alpha_engine.mt5_adapter"),
    "pandas_io": (_src("import pandas as pd", 'pd.read_csv("C:/x.csv")'), "read_csv"),
    "pandas_to_pickle": (_src("import pandas as pd", 'ctx.h1.to_pickle("x")'), "to_pickle"),
    "numpy_save": (_src("import numpy as np", 'np.save("x", 1)'), "«save»"),
    "clock": (_src("import pandas as pd", "pd.Timestamp.now()"), "«now»"),
    "clock_string": (_src("import pandas as pd", 'pd.Timestamp("now")'), "now"),
    "private_attr": (_src("import numpy as np", "np._core"), "_core"),
    "dunder_string": (_src(method='ctx.h1.agg("__class__")'), "__نام__"),
    "async": (_src("async def f():\n    return 1"), "async"),
    "environ": (_src("import pandas as pd", "pd.io.common.os.environ"), "«os»"),
    "pyarrow": (_src("import pyarrow"), "pyarrow"),
    "random_module": (_src("import random"), "random"),
    "time_module": (_src("import time"), "time"),
}


@pytest.mark.parametrize("case", sorted(MALICIOUS))
def test_malicious_sources_are_rejected_statically(case: str) -> None:
    source, needle = MALICIOUS[case]
    errors = _errors(source)
    assert any(needle in e for e in errors), errors


def test_errors_carry_line_numbers() -> None:
    source = _src("import os")
    line = source.splitlines().index("import os") + 1
    assert f"خط {line}:" in " ".join(_errors(source))
    syntax = validate_source("def broken(:\n    pass\n")
    assert not syntax.ok and syntax.errors_fa[0].startswith("خط 1:") and "نحوی" in syntax.errors_fa[0]


def test_strategy_class_rules() -> None:
    two = _src("class Other(Strategy):\n    name = 'x'\n    version = 1\n    title_fa = 'x'\n    param_schema = None\n"
               "    def evaluate(self, ctx):\n        return None")
    assert any("دقیقا یک کلاس" in e for e in _errors(two))
    derived = _src() + "\nclass Child(Demo):\n    pass\n"
    assert any("ارث" in e for e in _errors(derived))
    none = "import numpy as np\nX = 1\n"
    assert any("دقیقا یک کلاس" in e for e in _errors(none))
    computed = _src().replace('name = "demo_plugin"', 'name = "demo" + "_plugin"')
    assert any("literal" in e for e in _errors(computed))
    bad_slug = _src().replace('name = "demo_plugin"', 'name = "Demo-Plugin"')
    assert any("نامعتبر" in e for e in _errors(bad_slug))
    reserved = _src().replace('name = "demo_plugin"', 'name = "stddev_channel"')
    assert any("رزرو" in e for e in _errors(reserved))
    assert any("رزرو" in e for e in validate_source(_src(), reserved_names={"demo_plugin"}).errors_fa)
    for version in ("0", "True", "1.5", '"1"'):
        assert any("version" in e or "نسخه" in e for e in _errors(_src().replace("version = 1", f"version = {version}")))
    no_title = _src().replace('title_fa = "نمونه"', 'title_fa = ""')
    assert any("title_fa" in e for e in _errors(no_title))
    no_eval = _src().replace("def evaluate(self, ctx):", "def other(self, ctx):")
    assert any("evaluate" in e for e in _errors(no_eval))
    no_schema = _src().replace("param_schema = ParamSchema([])", "")
    assert any("param_schema" in e for e in _errors(no_schema))
    override = _src().replace("    def evaluate(self, ctx):",
                              "    @classmethod\n    def validate_params(cls, values):\n        return {}, []\n\n"
                              "    def evaluate(self, ctx):")
    assert any("validate_params" in e for e in _errors(override))
    host_attr = _src().replace("version = 1", 'version = 1\n    code_sha256 = "' + "a" * 64 + '"')
    assert any("code_sha256" in e for e in _errors(host_attr))
    dunder_method = _src().replace("    def evaluate(self, ctx):", "    def __init__(self):\n        pass\n\n"
                                                                    "    def evaluate(self, ctx):")
    assert any("__init__" in e for e in _errors(dunder_method))
    nested = "from alpha_engine.strategy.base import Strategy\ndef f():\n    class X(Strategy):\n        pass\n"
    assert any("سطح بالای فایل" in e for e in _errors(nested))


def test_size_encoding_and_nul_limits() -> None:
    big = _src("X = 1\n" + "# padding\n" * (MAX_SOURCE_BYTES // 10 + 10))
    assert any("سقف" in e for e in _errors(big))
    assert any("UTF-8" in e for e in _errors(b"x = '\xff\xfe'\n"))
    assert any("NUL" in e for e in _errors(_src("X = 1\x00")))
    assert any("خالی" in e for e in _errors(b""))
    # a UTF-8 BOM is accepted and not part of the identity
    bom = validate_source(b"\xef\xbb\xbf" + _src().encode("utf-8"))
    assert bom.ok and bom.sha256 == source_sha256(_src())


def test_own_private_members_and_helpers_are_allowed() -> None:
    source = _src("import numpy as np\nimport math\nfrom dataclasses import dataclass\nfrom typing import Any\n"
                  "from alpha_engine.indicators import wilder_atr\nfrom alpha_engine.patterns import bullish_pin_bar\n"
                  "from alpha_engine.risk.sizing import size_position\nfrom alpha_engine.risk import sizing\n"
                  "from alpha_engine import strategy\nfrom numpy.lib.stride_tricks import sliding_window_view\n"
                  "import pandas.api.types as ptypes\n"
                  "def to_candidate(x):\n    return x\n",
                  "self._helper(ctx.h1.to_numpy(), math.floor(1.5))")
    report = validate_source(source)
    assert report.ok, report.errors_fa


@pytest.mark.parametrize(("module", "names", "level", "allowed"), [
    ("numpy", None, 0, True),
    ("numpy.random", None, 0, True),
    ("pandas", ("DataFrame",), 0, True),
    ("math", None, 0, True),
    ("statistics", None, 0, True),
    ("dataclasses", ("dataclass",), 0, True),
    ("typing", ("Any",), 0, True),
    ("__future__", ("annotations",), 0, True),
    ("alpha_engine.strategy", ("Strategy",), 0, True),
    ("alpha_engine.strategy.signal", ("SignalCandidate",), 0, True),
    ("alpha_engine.risk", ("sizing",), 0, True),
    ("alpha_engine", ("strategy", "indicators"), 0, True),
    ("alpha_engine.strategy", None, 0, False),  # plain import binds the whole engine package
    ("alpha_engine", ("config",), 0, False),
    ("alpha_engine", ("strategy", "config"), 0, False),
    ("alpha_engine.risk", ("size_position",), 0, False),
    ("alpha_engine.strategy", ("_private",), 0, False),
    ("alpha_engine.strategy", ("*",), 0, False),
    ("numpy", None, 1, False),
    ("os", None, 0, False),
    ("os.path", None, 0, False),
    ("pandas.io.common", None, 0, False),
    ("MetaTrader5", None, 0, False),
    ("metatrader5", ("initialize",), 0, False),
    ("builtins", None, 0, False),
    (None, ("x",), 0, False),
])
def test_import_whitelist(module: str | None, names: tuple[str, ...] | None, level: int, allowed: bool) -> None:
    assert (import_allowed(module, names, level) is None) is allowed


def test_engine_module_list_matches_the_user_decision() -> None:
    tops = {m.split(".")[1] for m in ENGINE_MODULES}
    assert tops == {"indicators", "patterns", "risk", "strategy"}
    assert "alpha_engine.risk.sizing" in ENGINE_MODULES and len(BANNED_NAMES) == 12
