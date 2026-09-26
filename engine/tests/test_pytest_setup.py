from __future__ import annotations

import pytest


def test_mt5_marker_is_registered(request: pytest.FixtureRequest) -> None:
    markers = request.config.getini("markers")
    assert any(m.startswith("mt5:") for m in markers)


@pytest.mark.mt5
def test_mt5_marked_tests_only_run_with_opt_in(request: pytest.FixtureRequest) -> None:
    # Skipped by default; when it does run, the opt-in flag must have been given.
    assert request.config.getoption("--run-mt5") is True
