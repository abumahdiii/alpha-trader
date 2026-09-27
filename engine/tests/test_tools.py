"""fetch_history and data_check_report against a fake mt5 + tmp cache (no terminal)."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.schema import Timeframe
from alpha_engine.mt5_adapter import Mt5Adapter
from alpha_engine.tools import data_check_report, fetch_history
from conftest import FAKE_LOGIN, FAKE_PASSWORD
from fixtures.fake_mt5 import FakeMt5

NOW = datetime(2025, 5, 5, 0, 30, tzinfo=timezone.utc)


@pytest.fixture
def setup(make_settings, synthetic, tmp_path):
    def _build(connected: bool = True, env: str = ""):
        settings = make_settings(env, environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "data")})
        fake = FakeMt5(synthetic.values(), initialize_results=[connected], account_login=int(FAKE_LOGIN))
        adapter = Mt5Adapter(settings, fake, sleep=lambda s: None, clock=lambda: NOW)
        return settings, fake, adapter

    return _build


def test_fetch_history_fills_cache_and_prints_summary(setup, synthetic, tmp_path) -> None:
    settings, fake, adapter = setup(env=f"MT5_LOGIN={FAKE_LOGIN}\nMT5_PASSWORD={FAKE_PASSWORD}\n")
    out = io.StringIO()
    assert fetch_history.run(["--years", "5"], settings=settings, adapter=adapter, out=out) == 0
    text = out.getvalue()
    assert "state=connected" in text and "offset: effective=us_dst(+2) source=fit" in text
    assert "match=19/20" in text
    assert "XAUUSD.x H1: rows=1117" in text and "history_short=True" in text
    assert "'weekend': 9, 'holiday': 1, 'session_break': 39, 'missing': 5" in text
    assert FAKE_PASSWORD not in text and FAKE_LOGIN not in text
    cache = OhlcvCache(tmp_path / "data")
    for key, series in synthetic.items():
        frame, meta = cache.read(*key)
        assert len(frame) == len(series.utc) and meta.source == "mt5"
    assert cache.read_offset_fit().effective_label == "us_dst(+2)"
    assert cache.read_spec("BRNUSD.x")[0].trade_contract_size == 1000.0
    assert fake.names()[-1] == "shutdown"
    # read-only: nothing but allow-listed calls reached the terminal
    assert set(fake.names()) <= {"initialize", "account_info", "version", "symbol_info", "symbol_info_tick",
                                 "copy_rates_range", "last_error", "shutdown", "symbol_select"}
    # second run is incremental
    out2 = io.StringIO()
    assert fetch_history.run([], settings=settings, adapter=Mt5Adapter(settings, fake, clock=lambda: NOW),
                             out=out2) == 0
    assert "incremental" in out2.getvalue() and "(+0," in out2.getvalue()


def test_fetch_history_exit_codes(setup) -> None:
    settings, _, adapter = setup(connected=False)
    out = io.StringIO()
    assert fetch_history.run([], settings=settings, adapter=adapter, out=out) == 2
    assert "state=error" in out.getvalue()
    settings, _, adapter = setup()
    assert fetch_history.run(["--timeframes", "M1"], settings=settings, adapter=adapter, out=io.StringIO()) == 2
    out = io.StringIO()
    assert fetch_history.run(["--symbols", "XAUUSD.x,NOPE.x"], settings=settings, adapter=adapter, out=out) == 1
    assert "spec NOPE.x: FAILED" in out.getvalue()


def test_data_check_report_persian_with_utc_and_server_time(setup, tmp_path) -> None:
    settings, fake, adapter = setup(env=f"MT5_LOGIN={FAKE_LOGIN}\n")
    fetch_history.run([], settings=settings, adapter=adapter, out=io.StringIO())
    report_path = tmp_path / "report" / "data_check.md"
    out = io.StringIO()
    code = data_check_report.run(["--out", str(report_path)], settings=settings,
                                 adapter=Mt5Adapter(settings, fake, clock=lambda: NOW), out=out, now=NOW)
    assert code == 0 and report_path.is_file()
    text = report_path.read_text(encoding="utf-8")
    assert text.startswith("# گزارش چک دستی داده خام")
    assert "زمان سرور (Data Window)" in text and "`us_dst(+2)`" in text and "19/20 (95%)" in text
    # newest XAUUSD.x H1 bar: 2025-05-04 23:00Z == server 2025.05.05 02:00 (US DST, +3)
    assert "| 2025-05-04 23:00Z | 2025.05.05 02:00 |" in text
    # oldest: 2025-02-24 00:00Z == server 02:00 (US standard time, +2)
    assert "| 2025-02-24 00:00Z | 2025.02.24 02:00 |" in text
    for symbol in ("XAUUSD.x", "BRNUSD.x"):
        for tf in ("H1", "H4"):
            assert f"{symbol} — {tf}" in text
    assert "| XAUUSD.x | MT5 زنده | 2 | 0.01 | 100 |" in text
    assert "کوتاه‌تر از هدف" in text and "کندل گم‌شده" in text
    assert "****5678" in text and FAKE_LOGIN not in text
    assert text.count("**۱۰ کندل اخیر:**") == 4


def test_data_check_report_offline_uses_cache(setup, tmp_path) -> None:
    settings, fake, adapter = setup()
    fetch_history.run([], settings=settings, adapter=adapter, out=io.StringIO())
    fake.calls.clear()
    path = tmp_path / "offline.md"
    assert data_check_report.run(["--out", str(path), "--offline", "--timeframes", "H4"],
                                 settings=settings, out=io.StringIO(), now=NOW) == 0
    text = path.read_text(encoding="utf-8")
    assert "آفلاین" in text and "| XAUUSD.x | کش |" in text and "XAUUSD.x — H1" not in text
    assert fake.calls == []


def test_report_without_cache(make_settings, tmp_path: Path) -> None:
    settings = make_settings("", environ={"ALPHA_TRADER_DATA_DIR": str(tmp_path / "empty")})
    path = tmp_path / "r.md"
    assert data_check_report.run(["--out", str(path), "--offline"], settings=settings, out=io.StringIO()) == 0
    text = path.read_text(encoding="utf-8")
    assert "هنوز برازش offset انجام نشده" in text and "داده‌ای در کش نیست" in text


def test_tools_are_importable_as_modules() -> None:
    assert callable(fetch_history.main) and callable(data_check_report.main)
    assert Timeframe.H1.value == "H1"
