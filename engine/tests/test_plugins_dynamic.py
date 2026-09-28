"""Dynamic plugin validation (``alpha_engine.plugins.checks``): the template passes; look-ahead, non-deterministic,
silent, identity-changing, slow and memory-hungry plugins are rejected by the parent-side comparisons / limits.

Each case spawns sandboxed worker processes (about 1.5 s each); never MT5, never ``data/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_engine import __main__ as entry
from alpha_engine.plugins.checks import FIXTURE_BARS, validate_plugin
from alpha_engine.plugins.template import TEMPLATE_SOURCE

SMALL = 1500  # fixture bars for the negative cases (faster; the template case uses the full size)


def _variant(old: str, new: str) -> str:
    assert old in TEMPLATE_SOURCE, old
    return TEMPLATE_SOURCE.replace(old, new)


def _rejected(source: str, needle: str, **kw) -> list[str]:
    result = validate_plugin(source, n_bars=kw.pop("n_bars", SMALL), **kw)
    assert result.static.ok, result.static.errors_fa  # these samples get past the static layer on purpose
    assert not result.ok
    errors = result.errors_fa
    assert any(needle in e for e in errors), errors
    return errors


def test_template_passes_static_and_dynamic_validation() -> None:
    result = validate_plugin(TEMPLATE_SOURCE)
    assert result.ok, result.errors_fa
    dyn = result.validation()["dynamic"]
    assert dyn["bars"] == FIXTURE_BARS and dyn["candidates"] > 10
    assert dyn["prefix_checks"] >= 40 and dyn["determinism"] is True and dyn["future_mutation"] is True
    assert dyn["elapsed_s"] < 30 and dyn["peak_memory_mib"] is not None and dyn["peak_memory_mib"] < 1024
    desc = result.dynamic.description
    assert desc.name == "ma_cross_demo" and desc.history_bars == 1 and "scan" in desc.overrides
    assert [s["name"] for s in desc.schema_list()] == ["fast", "slow", "sl_lookback", "atr_period", "sl_atr_mult",
                                                       "use_h4_filter"]
    assert result.report()["validation"]["static"][-1] == "strategy_class"


def test_negative_shift_look_ahead_is_caught_by_prefix_equivalence() -> None:
    source = _variant('close = h1["close"].astype("float64")',
                      'close = h1["close"].astype("float64").shift(-1).ffill()')
    _rejected(source, "scan با evaluate")


def test_full_sample_normalisation_is_caught_by_future_mutation() -> None:
    # scan scales the stop by the mean close of the WHOLE history (bars after t leak into bar t)
    source = _variant('buffer = float(p["sl_atr_mult"]) * float(atr[t])',
                      'buffer = float(p["sl_atr_mult"]) * float(atr[t]) * float(closes.mean()) / 2000.0')
    errors = _rejected(source, "نگاه به آینده")
    assert any("تغییر کندل‌های" in e or "حذف کندل‌های" in e for e in errors)


def test_unseeded_randomness_is_caught_by_determinism() -> None:
    source = _variant('buffer = float(p["sl_atr_mult"]) * float(atr[t])',
                      'buffer = float(p["sl_atr_mult"]) * float(atr[t]) * float(np.random.default_rng().uniform(1, 2))')
    _rejected(source, "قطعی نیست")


def test_state_carried_between_scans_is_caught_by_determinism() -> None:
    source = _variant("        p = self.clean_params(params)\n        return self._candidates(",
                      "        p = self.clean_params(params)\n        COUNTER.append(1)\n"
                      "        if len(COUNTER) > 1:\n            return []\n        return self._candidates(")
    source = source.replace('SETUP = "ma_cross"', 'SETUP = "ma_cross"\nCOUNTER = []')
    _rejected(source, "قطعی نیست")


def test_a_plugin_without_candidates_is_rejected() -> None:
    source = _variant("        return out\n", "        return []\n")
    _rejected(source, "هیچ کاندیدی")


def test_runtime_identity_must_match_the_literal_values() -> None:
    source = TEMPLATE_SOURCE + '\nMovingAverageCross.version = 2\n'
    _rejected(source, "نام، نسخه یا عنوان")


def test_wrong_params_hash_is_a_contract_error() -> None:
    source = _variant("        phash = params_hash(p)", '        phash = "0" * 64')
    _rejected(source, "params_hash")


def test_endless_scan_is_stopped_by_the_time_budget() -> None:
    source = _variant("        p = self.clean_params(params)\n        return self._candidates(",
                      "        p = self.clean_params(params)\n        while True:\n            pass\n"
                      "        return self._candidates(")
    _rejected(source, "سقف زمان", budget_s=3.0)


def test_memory_hungry_scan_is_stopped_by_the_memory_cap() -> None:
    # about 1.6 GB of float64 against a 512 MiB job limit
    source = _variant("        p = self.clean_params(params)\n        return self._candidates(",
                      "        p = self.clean_params(params)\n        big = np.ones(200_000_000)\n"
                      "        return self._candidates(")
    errors = _rejected(source, "", memory_limit=512 << 20)
    assert any("حافظه" in e for e in errors), errors


def test_plugin_check_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.py"
    good.write_text(TEMPLATE_SOURCE, encoding="utf-8")
    assert entry.main(["--plugin-check", str(good)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["name"] == "ma_cross_demo" and report["errors_fa"] == []
    bad = tmp_path / "bad.py"
    bad.write_text("import os\n", encoding="utf-8")
    assert entry.main(["--plugin-check", str(bad)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["errors_fa"] and report["validation"]["dynamic"] is None
    assert entry.main(["--plugin-check", str(tmp_path / "missing.py")]) == 2
    assert entry.main(["--plugin-check"]) == 2
