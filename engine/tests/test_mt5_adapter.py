"""Mt5Adapter with a fake MetaTrader5 module: connection modes, secrets, retries, D3, specs, rates."""

from __future__ import annotations

import ast
import io
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha_engine import logging_setup
from alpha_engine.data.schema import Timeframe, to_epoch_seconds, validate_frame
from alpha_engine.data.timezone import OffsetModel
from alpha_engine.mt5_adapter import (
    ALLOWED_MT5_FUNCTIONS,
    MT5_LOCK,
    Mt5Adapter,
    Mt5DataError,
    Mt5Error,
    Mt5NotConnectedError,
    UnknownSymbolError,
)
from conftest import FAKE_LOGIN, FAKE_PASSWORD
from fixtures.fake_mt5 import FakeMt5

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "alpha_engine"
AFTER_FIXTURE = datetime(2025, 5, 5, 0, 30, tzinfo=timezone.utc)
US2 = OffsetModel.us_dst(2)


def _adapter(settings, fake, clock=AFTER_FIXTURE, **kw) -> Mt5Adapter:
    sleeps: list[float] = []
    adapter = Mt5Adapter(settings, fake, sleep=sleeps.append, clock=lambda: clock, **kw)
    adapter.sleeps = sleeps  # type: ignore[attr-defined]
    return adapter


def _dev_stream(make_settings) -> io.StringIO:
    """DEV_MODE logging WITHOUT configured secrets, so the redaction filter cannot hide a leak."""
    stream = io.StringIO()
    logging_setup.configure_logging(make_settings("DEV_MODE=true\n"), stream=stream, force=True)
    return stream


# --- connection ------------------------------------------------------------------------------------

def test_attach_mode_passes_no_credentials(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values())
    settings = make_settings(f"MT5_TERMINAL_PATH=C:\\T\\terminal64.exe\nMT5_LOGIN=12345678\nMT5_SERVER=Fake-Server\n")
    status = _adapter(settings, fake).connect()
    assert status.state == "connected" and status.trade_mode == "demo"
    assert status.login_masked == "****5678" and status.server == "Fake-Server"
    init = [c for c in fake.calls if c[0] == "initialize"]
    assert init == [("initialize", (), {"path": "C:\\T\\terminal64.exe"})]


def test_login_mode_only_when_password_set_and_never_leaks(make_settings, synthetic) -> None:
    stream = _dev_stream(make_settings)
    fake = FakeMt5(synthetic.values(), account_login=int(FAKE_LOGIN), trade_mode=2)
    settings = make_settings(f"MT5_LOGIN={FAKE_LOGIN}\nMT5_PASSWORD={FAKE_PASSWORD}\nMT5_SERVER=Fake-Server\n")
    adapter = _adapter(settings, fake)
    status = adapter.connect()
    assert status.state == "connected" and status.trade_mode == "real"
    kwargs = [c[2] for c in fake.calls if c[0] == "initialize"][0]
    assert kwargs == {"login": int(FAKE_LOGIN), "password": FAKE_PASSWORD, "server": "Fake-Server"}
    for text in (stream.getvalue(), repr(adapter), str(adapter), status.model_dump_json(), repr(status)):
        assert FAKE_PASSWORD not in text
        assert FAKE_LOGIN not in text
    assert "mt5 connect attempt 1/3 mode=login" in stream.getvalue()


def test_password_without_numeric_login_is_refused(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values())
    status = _adapter(make_settings(f"MT5_PASSWORD={FAKE_PASSWORD}\n"), fake).connect()
    assert status.state == "error" and "MT5_LOGIN" in status.message
    assert fake.calls == []
    assert FAKE_PASSWORD not in status.model_dump_json()


def test_retries_with_backoff_then_success(make_settings, synthetic) -> None:
    stream = _dev_stream(make_settings)
    fake = FakeMt5(synthetic.values(), initialize_results=[False, False, True])
    adapter = _adapter(make_settings(), fake)
    assert adapter.connect().state == "connected"
    assert adapter.sleeps == [1.0, 2.0]
    assert fake.names().count("initialize") == 3
    assert "initialize failed (attempt 1)" in stream.getvalue()


def test_retries_exhausted_reports_error(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values(), initialize_results=[False])
    adapter = _adapter(make_settings(), fake)
    status = adapter.connect()
    assert status.state == "error" and "after 3 attempts" in status.message and "-10003" in status.message
    assert adapter.sleeps == [1.0, 2.0]
    with pytest.raises(Mt5NotConnectedError):
        adapter.symbol_spec("XAUUSD.x")


@pytest.mark.parametrize(("env", "problem"), [
    ("MT5_LOGIN=87654321\n", "MT5_LOGIN"),
    ("MT5_LOGIN=12345678\nMT5_SERVER=Other-Server\n", "MT5_SERVER"),
])
def test_account_mismatch_disconnects_and_refuses_data(make_settings, synthetic, env, problem) -> None:
    fake = FakeMt5(synthetic.values(), account_login=12345678, account_server="Fake-Server")
    adapter = _adapter(make_settings(env), fake)
    status = adapter.connect()
    assert status.state == "account_mismatch" and problem in status.message
    assert fake.names()[-1] == "shutdown"
    with pytest.raises(Mt5NotConnectedError):
        adapter.fetch_rates("XAUUSD.x", "H1", AFTER_FIXTURE, None, US2)
    assert adapter.ensure_connected(min_interval=0) is False  # never re-attaches silently
    assert "copy_rates_range" not in fake.names()


def test_empty_login_skips_check_with_warning(make_settings, synthetic, caplog) -> None:
    fake = FakeMt5(synthetic.values(), account_server="Anything")
    with caplog.at_level("WARNING", logger="alpha_engine"):
        status = _adapter(make_settings(), fake).connect()
    assert status.state == "connected"
    assert "MT5_LOGIN is not set" in caplog.text


def test_account_info_missing(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values(), account_available=False)
    status = _adapter(make_settings(), fake).connect()
    assert status.state == "error" and "account_info" in status.message


def test_shutdown_and_background_connect(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values(), call_delay=0.01)
    adapter = _adapter(make_settings(), fake)
    thread = adapter.connect_in_background()
    thread.join(5)
    assert adapter.connected
    adapter.shutdown()
    assert adapter.status().state == "disconnected" and fake.names()[-1] == "shutdown"


def test_allow_list_blocks_other_functions(make_settings, synthetic) -> None:
    adapter = _adapter(make_settings(), FakeMt5(synthetic.values()))
    with pytest.raises(Mt5Error, match="allow-list"):
        adapter._call("terminal_" + "shutdown_everything")
    assert "copy_rates_range" in ALLOWED_MT5_FUNCTIONS


def test_mt5_calls_are_serialized_by_the_lock(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values(), call_delay=0.002)
    adapter = _adapter(make_settings(), fake)
    adapter.connect()
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(5):
                adapter.symbol_spec("XAUUSD.x")
                adapter.last_tick_time("XAUUSD.x")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and fake.max_concurrency == 1
    assert isinstance(MT5_LOCK, type(threading.Lock()))


def test_metatrader5_imported_lazily_only_in_adapter() -> None:
    for path in PACKAGE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module level only
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.split(".")[0] == "MetaTrader5" for n in names), path


def test_real_package_is_blocked_in_default_test_runs(make_settings) -> None:
    import MetaTrader5  # conftest installed a blocker (no --run-mt5)

    with pytest.raises(RuntimeError, match="without --run-mt5"):
        MetaTrader5.initialize()
    status = Mt5Adapter(make_settings(), sleep=lambda s: None).connect()
    assert status.state == "error"


# --- symbols ---------------------------------------------------------------------------------------

def test_symbol_spec_typed_and_complete(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values(), invisible=["BRNUSD.x"])
    adapter = _adapter(make_settings(), fake)
    adapter.connect()
    spec = adapter.symbol_spec("XAUUSD.x")
    assert (spec.name, spec.digits, spec.point, spec.trade_contract_size) == ("XAUUSD.x", 2, 0.01, 100.0)
    assert (spec.volume_min, spec.volume_step, spec.volume_max) == (0.01, 0.01, 100.0)
    # D9 worked example: tick_value 1.00 / tick_size 0.01 = 100 USD per 1.00 move per lot = contract size
    assert spec.trade_tick_value == pytest.approx(1.0) and spec.value_per_price_unit == pytest.approx(100.0)
    assert "symbol_select" not in fake.names()
    brent = adapter.symbol_spec("BRNUSD.x")
    assert ("symbol_select", ("BRNUSD.x", True), {}) in fake.calls
    assert brent.trade_contract_size == 1000.0 and brent.currency_profit == "USD"


def test_unknown_symbol(make_settings, synthetic) -> None:
    adapter = _adapter(make_settings(), FakeMt5(synthetic.values()))
    adapter.connect()
    with pytest.raises(UnknownSymbolError):
        adapter.symbol_spec("NOPE.x")
    with pytest.raises(ValueError):
        adapter.symbol_spec("../x")


# --- rates -----------------------------------------------------------------------------------------

def test_fetch_rates_matches_fixture_exactly(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values())
    adapter = _adapter(make_settings(), fake)
    adapter.connect()
    for tf in Timeframe:
        expected = synthetic[("XAUUSD.x", tf)].utc
        result = adapter.fetch_rates("XAUUSD.x", tf, datetime(2025, 2, 24, tzinfo=timezone.utc), None, US2)
        validate_frame(result.frame, tf)
        pd.testing.assert_frame_equal(result.frame, expected)
        assert result.first_available_utc == expected["time"].iloc[0]


@pytest.mark.parametrize("chunk_days", [1, 3, 7, 30])
def test_chunk_boundaries_no_dup_no_loss(make_settings, synthetic, chunk_days) -> None:
    fake = FakeMt5(synthetic.values())
    adapter = _adapter(make_settings(), fake)
    adapter.connect()
    expected = synthetic[("BRNUSD.x", Timeframe.H1)]
    lo, hi = int(expected.raw["time"][0]), int(expected.raw["time"][-1])
    result = adapter.fetch_raw("BRNUSD.x", "H1", lo, hi, chunk_days=chunk_days)
    assert np.array_equal(result.frame["time"].to_numpy(), expected.raw["time"])
    assert result.frame["time"].is_unique
    calls = fake.rate_calls()
    assert len(calls) == result.chunks and result.chunks >= (hi - lo) // (chunk_days * 86400)
    # consecutive requests touch but never overlap: [a, b-1], [b, ...]
    for (_, _, _, prev_to), (_, _, next_from, _) in zip(calls[:-1], calls[1:]):
        assert next_from == prev_to + 1
    assert result.empty_chunks >= (1 if chunk_days == 1 else 0)  # daily chunks hit weekends


def test_five_year_window_uses_default_chunks(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values())
    adapter = _adapter(make_settings(), fake)
    adapter.connect()
    now = AFTER_FIXTURE
    result = adapter.fetch_rates("XAUUSD.x", "H1", datetime(2020, 5, 5, tzinfo=timezone.utc), None, US2)
    # 5 years (+1 day padding each side) of H1 in <= 180-day chunks: ceil(1828/180) = 11 requests;
    # the 10-week history (from 2025-02-24) straddles the last two chunks, the other 9 are empty.
    assert result.chunks == 11 and result.empty_chunks == 9 and result.failed_chunks == 0
    assert result.first_available_utc == pd.Timestamp("2025-02-24T00:00Z")
    h4 = adapter.fetch_rates("XAUUSD.x", "H4", datetime(2020, 5, 5, tzinfo=timezone.utc), now, US2)
    assert h4.chunks == 6


def test_forming_bar_is_dropped(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values())
    now = datetime(2025, 3, 12, 10, 30, tzinfo=timezone.utc)  # inside the 10:00Z H1 bar
    adapter = _adapter(make_settings(), fake, clock=now)
    adapter.connect()
    result = adapter.fetch_rates("XAUUSD.x", "H1", datetime(2025, 3, 10, tzinfo=timezone.utc), None, US2)
    assert result.frame["time"].iloc[-1] == pd.Timestamp("2025-03-12T09:00Z")
    assert result.dropped_forming > 0
    epochs = to_epoch_seconds(result.frame["time"])
    assert (epochs + 3600 <= int(now.timestamp())).all()
    h4 = adapter.fetch_rates("XAUUSD.x", "H4", datetime(2025, 3, 10, tzinfo=timezone.utc), None, US2)
    # H4 buckets (US DST, server = UTC+3): ... 05:00Z (ends 09:00Z) complete, 09:00Z (ends 13:00Z) forming
    assert h4.frame["time"].iloc[-1] == pd.Timestamp("2025-03-12T05:00Z")


def test_fetch_failure_modes(make_settings, synthetic) -> None:
    fake = FakeMt5(synthetic.values(), fail_rates_for=["XAUUSD.x"])
    adapter = _adapter(make_settings(), fake)
    adapter.connect()
    with pytest.raises(Mt5DataError, match="-10003"):
        adapter.fetch_rates("XAUUSD.x", "H1", datetime(2025, 3, 1, tzinfo=timezone.utc), None, US2)
    empty = adapter.fetch_rates("BRNUSD.x", "H1", datetime(2026, 1, 1, tzinfo=timezone.utc), None, US2)
    assert empty.frame.empty  # start after "now": nothing requested


def test_live_offset_from_tick(make_settings, synthetic) -> None:
    now = datetime(2025, 3, 12, 10, 30, 10, tzinfo=timezone.utc)
    fake = FakeMt5(synthetic.values(), tick_time=int(now.timestamp()) + 3 * 3600 - 4)
    adapter = _adapter(make_settings(), fake, clock=now)
    adapter.connect()
    live = adapter.live_offset("XAUUSD.x")
    assert live.trusted and live.hours == 3.0


def test_fetch_logs_under_dev_mode_without_secrets(make_settings, synthetic) -> None:
    stream = _dev_stream(make_settings)
    fake = FakeMt5(synthetic.values(), account_login=int(FAKE_LOGIN))
    adapter = _adapter(make_settings(f"MT5_LOGIN={FAKE_LOGIN}\nMT5_PASSWORD={FAKE_PASSWORD}\n"), fake)
    adapter.connect()
    adapter.fetch_rates("XAUUSD.x", "H1", datetime(2025, 4, 1, tzinfo=timezone.utc), None, US2)
    out = stream.getvalue()
    assert "copy_rates_range XAUUSD.x H1 chunk 1" in out and "fetched XAUUSD.x H1" in out
    assert FAKE_PASSWORD not in out and FAKE_LOGIN not in out
