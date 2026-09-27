"""Live, read-only MetaTrader 5 checks. Skipped unless ``pytest --run-mt5``.

Attach mode only: ``initialize(path)`` with no credentials, reusing the terminal's logged-in session.
The terminal path comes from ``MT5_LIVE_TERMINAL_PATH`` (optional; without it the package attaches to
the running terminal it finds). The real ``.env`` is never read (conftest isolates it), nothing is
written outside pytest's tmp dir, and only allow-listed read functions are called.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from alpha_engine.data.schema import Timeframe, to_epoch_seconds, validate_frame
from alpha_engine.market_data import MarketDataService
from alpha_engine.mt5_adapter import Mt5Adapter

pytestmark = pytest.mark.mt5


@pytest.fixture
def live(make_settings, tmp_path):
    path = os.environ.get("MT5_LIVE_TERMINAL_PATH", "")
    env = f"MT5_TERMINAL_PATH={path}\n" if path else ""
    settings = make_settings(env, environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "data")})
    assert settings.mt5_password is None  # attach only
    adapter = Mt5Adapter(settings)
    status = adapter.connect()
    try:
        yield settings, adapter, status
    finally:
        adapter.shutdown()


def test_live_attach_spec_and_recent_h1_bars(live) -> None:
    settings, adapter, status = live
    assert status.state == "connected", status.message
    assert status.login_masked is None or status.login_masked.startswith("****")
    print(f"\nMT5 server={status.server} login={status.login_masked} trade_mode={status.trade_mode}")

    spec = adapter.symbol_spec("XAUUSD.x")
    assert spec.name == "XAUUSD.x" and spec.digits >= 1 and spec.point > 0
    assert spec.trade_contract_size > 0 and spec.volume_min > 0 and spec.volume_step > 0
    print(f"XAUUSD.x spec: {spec.model_dump()}")

    service = MarketDataService.create(settings, adapter, clock=adapter.clock)
    fit = service.refit_offset(years=2)
    print(f"offset fit: {fit.effective_label} match={fit.matched}/{fit.checks} live={fit.live_offset_hours} "
          f"trusted={fit.live_trusted} consistent={fit.consistent_with_live}")
    assert fit.checks > 0

    now = datetime.now(timezone.utc)
    result = adapter.fetch_rates("XAUUSD.x", Timeframe.H1, now - timedelta(days=10), None, fit.effective)
    frame = result.frame
    assert len(frame) > 0, "no H1 bars in the last 10 days"
    validate_frame(frame, Timeframe.H1)
    epochs = to_epoch_seconds(frame["time"])
    assert (epochs + 3600 <= int(now.timestamp()) + 5).all()  # no forming bar
    assert frame["time"].iloc[-1] > pd.Timestamp(now) - pd.Timedelta(days=5)
    print(frame.tail(5).to_string())
