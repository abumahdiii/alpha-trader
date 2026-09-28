"""``GET /backtests/{id}/export`` and ``backtest/export.py`` (phase 7, section 3): CSV + xlsx of stored results.

No MT5, never the real data/: real runs are simulated on the seeded synthetic cache (module fixture), fake
runs are written straight into a temp database. Every exported value is compared with what the JSON API
returns for the same run (values exactly as stored); the xlsx is validated as XML part by part with the
standard library (no openpyxl / pandas engine is installed).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from alpha_engine.app import create_app
from alpha_engine.backtest import export
from alpha_engine.backtest.models import EXIT_REASON_FA, SEED_MAX, SKIP_REASON_FA, ExitReason, SkippedCandidate, \
    SkipReason, Trade
from alpha_engine.config import load_settings
from alpha_engine.logging_setup import ROOT_LOGGER_NAME
from alpha_engine.storage.backtests_repo import BacktestsRepo
from alpha_engine.storage.db import EngineConnection, format_utc, open_db
from fixtures.seed_cache import seed_cache

GOLD = "XAUUSD.x"
MANUAL = {"symbol": GOLD, "mode": "manual", "from": "2024-09-02T00:00:00Z", "to": "2025-02-24T00:00:00Z"}
RANDOM = {"symbol": GOLD, "mode": "random", "windows_count": 4, "window_months": 1, "seed": SEED_MAX,
          "commission_per_lot_per_side": 3.5}
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN}
BOM = b"\xef\xbb\xbf"
NASTY = 'قیمت <بالا> & "پایین" \'x\' \x01\x0b ctrl _x0041_ literal =SUM(A1)'
TIME_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _is_persian(text: str | None) -> bool:
    return bool(text) and any("؀" <= ch <= "ۿ" for ch in text)


# ------------------------------------------------------------------------------------------ readers
def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group()  # type: ignore[union-attr]
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _decode_xstring(text: str) -> str:
    return re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), text)


@dataclass
class Book:
    names: list[str]
    sheets: dict[str, list[list[Any]]]
    raw: dict[str, bytes]


def read_xlsx(content: bytes) -> Book:
    """Parse the workbook with xml.etree only; every part must be well-formed XML."""
    zf = zipfile.ZipFile(io.BytesIO(content))
    assert zf.testzip() is None
    raw = {name: zf.read(name) for name in zf.namelist()}
    for name, data in raw.items():
        ET.fromstring(data)  # raises on malformed XML
    for part in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                 "xl/styles.xml"):
        assert part in raw, part
    overrides = {o.get("PartName") for o in ET.fromstring(raw["[Content_Types].xml"])
                 if o.tag.endswith("Override")}
    wb = ET.fromstring(raw["xl/workbook.xml"])
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(raw["xl/_rels/workbook.xml.rels"])}
    names, sheets = [], {}
    for sheet in wb.find("m:sheets", NS):
        target = "xl/" + rels[sheet.get(f"{{{REL}}}id")]
        assert "/" + target in overrides
        root = ET.fromstring(raw[target])
        view = root.find("m:sheetViews/m:sheetView", NS)
        assert view is not None and view.get("rightToLeft") == "1"
        rows: list[list[Any]] = []
        for r_index, row in enumerate(root.find("m:sheetData", NS), start=1):
            assert row.get("r") == str(r_index)
            cells: dict[int, Any] = {}
            for c in row:
                assert re.fullmatch(rf"[A-Z]+{r_index}", c.get("r"))
                kind = c.get("t")
                if kind == "inlineStr":
                    value: Any = _decode_xstring(c.find("m:is/m:t", NS).text or "")
                elif kind == "b":
                    value = c.find("m:v", NS).text == "1"
                else:
                    assert kind is None, kind
                    text = c.find("m:v", NS).text
                    value = int(text) if re.fullmatch(r"-?\d+", text) else float(text)
                cells[_col_index(c.get("r"))] = value
            rows.append([cells.get(i) for i in range(max(cells, default=-1) + 1)])
        names.append(sheet.get("name"))
        sheets[sheet.get("name")] = rows
    return Book(names, sheets, raw)


def read_csv(content: bytes) -> list[list[str]]:
    assert content.startswith(BOM)
    assert content[len(BOM):].split(b"\r\n", 1)[0].find(b"\n") == -1  # header line ends with CRLF
    return list(csv.reader(io.StringIO(content[len(BOM):].decode("utf-8"), newline="")))


def kv(rows: list[list[Any]]) -> dict[str, Any]:
    """key/label_fa/value table -> {key: value} (header row skipped)."""
    return {r[0]: (r[2] if len(r) > 2 else None) for r in rows[1:]}


def csv_matches(text: str, value: Any) -> bool:
    """The CSV text of one stored value: exact (repr for floats, JSON for lists, Z times, formula guard)."""
    if value is None:
        return text == ""
    if isinstance(value, bool):
        return text == ("true" if value else "false")
    if isinstance(value, int):
        return text == str(value)
    if isinstance(value, float):
        return text == repr(value) and float(text) == value
    if isinstance(value, (list, dict)):
        return json.loads(text) == value
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return text == "'" + value
    return text == value


def xlsx_matches(cell: Any, value: Any) -> bool:
    if isinstance(value, (list, dict)):
        return json.loads(cell) == value
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53:
        return cell == str(value)
    if isinstance(value, float):
        return isinstance(cell, (int, float)) and float(cell) == value
    return cell == value


# ------------------------------------------------------------------------------------------ fake stored runs
T0 = datetime(2024, 9, 2, tzinfo=timezone.utc)


def _trade(i: int, window: int = 0, reason_fa: str = "لمس خط بالای کانال و الگوی پین‌بار") -> dict[str, Any]:
    sl = i % 3 == 0
    t = T0 + timedelta(hours=5 * i)
    entry = 2400.0 + i * 0.123456789
    return Trade(
        window_index=window, trade_index=i, direction="buy" if i % 2 == 0 else "sell", setup_type="touch_upper",
        line="upper", pattern="pin_bar", confirmation_bar_time=t, decision_time=t + timedelta(hours=1),
        entry_time=t + timedelta(hours=1), entry=entry, entry_bid_open=entry - 0.34, stop_loss=entry - 3.0,
        take_profit=entry + 6.0, rr=2.0, volume=0.01 * (1 + i % 5), risk_amount=25.0 / 3,
        balance_before=2500.0 + i / 7, exit_bar_time=t + timedelta(hours=3), exit_time=t + timedelta(hours=4),
        exit_price=entry - 3.0 if sl else entry + 6.0, exit_reason=ExitReason.SL if sl else ExitReason.TP,
        exit_reason_fa=EXIT_REASON_FA[ExitReason.SL if sl else ExitReason.TP], gross_pnl=-8.1 if sl else 16.2,
        commission=0.07, net_pnl=(-8.1 if sl else 16.2) - 0.07, r_multiple=None if i == 1 else 1.0 / 3.0,
        balance_after=2500.0 + i / 7 + 1e-9, bars_held=3, spread_at_entry_points=34,
        spread_at_exit_points=None if i % 2 == 0 else 31, held_over_weekend=i % 4 == 0,
        flags=["weekend_gap"] if i % 4 == 0 else [], sizing_warnings_fa=["حجم به حداقل گرد شد"] if i == 2 else [],
        reason_fa=reason_fa, indicators={"atr": 1.2345678901234567 + i, "slope": -0.1, "ok": True, "src": "h4"},
    ).model_dump(mode="json")


def _skipped(window: int, i: int, detail: str | None = None) -> dict[str, Any]:
    t = T0 + timedelta(hours=7 * i + 2)
    return SkippedCandidate(window_index=window, time=t + timedelta(hours=1), confirmation_bar_time=t,
                            direction="sell", setup_type="touch_lower", reason=SkipReason.POSITION_OPEN,
                            reason_fa=SKIP_REASON_FA[SkipReason.POSITION_OPEN], detail=detail).model_dump(mode="json")


def store_fake_run(db: EngineConnection, *, mode: str = "manual", trades_per_window: int = 5, windows: int = 1,
                   status: str = "done", reason_fa: str = "لمس خط بالای کانال", detail: str | None = "fill=2400.5",
                   symbol: str = GOLD) -> int:
    """A run row (+ children for ``done``) written straight into the database (no simulation)."""
    repo = BacktestsRepo(db)
    random_mode = mode == "random"
    config = {"symbol": symbol, "mode": mode, "params": {"n": 100, "k": 2.0, "atr_period": 14},
              "account": {"balance": 2500.0, "risk_pct": 1.0, "leverage": 100, "rr": 2.0},
              "cost_model": {"spread": "historical", "fallback_spread_points": None,
                             "commission_per_lot_per_side": 0.0, "swap": "none"},
              "strategy_name": "stddev_channel", "strategy_version": 1, "params_version": 1,
              "params_hash": "a" * 64, "provisional": True}
    run_id = repo.create_run(
        request={"symbol": symbol, "mode": mode}, config=config, symbol=symbol, mode=mode,
        period_start=None if random_mode else T0, period_end=None if random_mode else T0 + timedelta(days=90),
        windows_count=windows if random_mode else None, window_months=1 if random_mode else None,
        seed=SEED_MAX if random_mode else None, seed_generated=False if random_mode else None,
        strategy_name="stddev_channel", strategy_version=1, params_version=1, params_hash="a" * 64,
        provisional=True, labels_fa=["موقت تا تایید چک داده", "بدون سوآپ"])
    if status in ("queued",):
        return run_id
    assert repo.mark_running(run_id)
    if status == "running":
        return run_id
    if status != "done":
        assert repo.finish(run_id, status, error_code="internal_error", error_message_fa="خطای آزمایشی")  # type: ignore[arg-type]
        return run_id
    metrics = {"initial_balance": 2500.0, "net_profit": 12.345678901234567, "win_rate": 2 / 3,
               "max_drawdown_peak_time": "2024-09-18T04:00:00+00:00", "profit_factor": None}
    with db.transaction():
        for w in range(windows):
            trades = [_trade(i, w, reason_fa) for i in range(trades_per_window)]
            skipped = [_skipped(w, i, detail) for i in range(2)]
            db.execute(
                "INSERT INTO backtest_windows (run_id, window_index, start_utc, end_utc, initial_balance,"
                " final_balance, trade_count, skipped_count, candidates, bars, first_bar_utc, last_bar_utc,"
                " zero_spread_bars_filled, zero_spread_bars_unfilled, weekend_holds, spread_fallback_bars,"
                " stopped_reason, equity_points_full, equity_points_stored, metrics_json, skipped_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, w, format_utc(T0), format_utc(T0 + timedelta(days=30)), 2500.0, 2500.0 + w / 3,
                 len(trades), len(skipped), 9, 480, format_utc(T0), format_utc(T0 + timedelta(days=29)), 3, 0, 1,
                 120, None, 480, 3, json.dumps({**metrics, "net_profit": w / 3}), json.dumps(skipped)))
            db.executemany(
                "INSERT INTO backtest_trades (run_id, window_index, trade_index, direction, setup_type, entry_time,"
                " exit_time, entry, stop_loss, take_profit, volume, exit_price, exit_reason, net_pnl, r_multiple,"
                " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, w, t["trade_index"], t["direction"], t["setup_type"], t["entry_time"].replace("Z", ".000000Z"),
                  t["exit_time"].replace("Z", ".000000Z"), t["entry"], t["stop_loss"], t["take_profit"], t["volume"],
                  t["exit_price"], t["exit_reason"], t["net_pnl"], t["r_multiple"], json.dumps(t, ensure_ascii=False))
                 for t in trades])
            db.executemany(
                "INSERT INTO backtest_equity (run_id, window_index, seq, time_utc, balance, equity)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(run_id, w, s, format_utc(T0 + timedelta(hours=s)), 2500.0 + s / 7, 2500.0 + s / 9) for s in range(3)])
        plan = {"mode": mode, "windows": [{"index": w, "start": "2024-09-02T00:00:00Z", "end": "2024-10-02T00:00:00Z"}
                                          for w in range(windows)],
                "seed": SEED_MAX if random_mode else None, "earliest_start": "2024-07-29T09:00:00Z",
                "data_end": "2025-03-03T00:00:00Z", "warmup_h4_bars": 140}
        result = {"cost_model": config["cost_model"], "entry_rule": "next_bar_open", "price_basis": "bid_bars",
                  "spread_source": "broker_history", "total_trades": trades_per_window * windows, "total_skipped": 2,
                  "metrics_kind": "random_aggregate" if random_mode else "single_window",
                  "spread_fallback": {"points": 34, "source": "auto_median_observed", "fallback_bars": 100,
                                      "zero_bars": 0, "total_bars": 480}}
        overall = ({"window_count": windows, "pooled": {"win_rate": 0.5}, "mean": {"sharpe": 1.25},
                    "worst": {"net_profit_pct": -0.5}, "note_fa": "پنجره‌ها زنجیر نمی‌شوند"}
                   if random_mode else metrics)
        db.execute(
            "UPDATE backtest_runs SET status = 'done', progress = 100, finished_utc = ?, plan_json = ?,"
            " fingerprint_json = ?, labels_json = ?, result_json = ?, metrics_json = ?, distribution_json = ?,"
            " trade_count = ?, net_profit = ?, net_profit_pct = ?, elapsed_s = ?, timings_json = ? WHERE id = ?",
            (format_utc(datetime.now(timezone.utc)), json.dumps(plan),
             json.dumps({"symbol": symbol, "h1_rows": 480, "h1_sha256": "b" * 64, "spec": {"digits": 2}}),
             json.dumps(["موقت تا تایید چک داده", "بدون سوآپ", "کمیسیون صفر"], ensure_ascii=False),
             json.dumps(result), json.dumps(overall, ensure_ascii=False),
             json.dumps({"count": windows, "worst_window": {"index": 0}}) if random_mode else None,
             trades_per_window * windows, 1.5, 0.06, 0.25, json.dumps({"total_s": 0.25}), run_id))
    return run_id


@pytest.fixture
def db(tmp_path: Path) -> Iterator[EngineConnection]:
    conn = open_db(tmp_path / "export.db")
    yield conn
    conn.close()


@pytest.fixture
def fake_client(make_settings) -> Iterator[TestClient]:
    """An app on an empty temp data dir (no cache needed: export reads the database only)."""
    with TestClient(create_app(make_settings()), client=("127.0.0.1", 50000)) as client:
        yield client


# ------------------------------------------------------------------------------------------ unit: text / cells
def test_xlsx_text_escapes_xml_specials_control_chars_and_xstring_literals() -> None:
    escaped = export.xlsx_text(NASTY)
    ET.fromstring(f"<t>{escaped}</t>")  # well-formed
    assert "&lt;" in escaped and "&amp;" in escaped and "&quot;" in escaped
    assert "_x0001_" in escaped and "_x000B_" in escaped and "_x005F_x0041_" in escaped
    assert "\x01" not in escaped and "\x0b" not in escaped
    parsed = ET.fromstring(f"<t>{escaped}</t>").text
    assert _decode_xstring(parsed) == NASTY  # Excel's decoding gives the original text back


def test_label_fa_and_filename() -> None:
    assert export.label_fa("net_pnl") == "سود/زیان خالص"
    assert export.label_fa("pooled.win_rate") == "تجمیعی (همه معاملات) / نرخ برد (۰ تا ۱)"
    assert export.label_fa("indicator.atr") == "اندیکاتور / atr"
    run = {"id": 7, "symbol": "XAUUSD.x", "mode": "random"}
    assert export.export_filename(run, "trades", "csv") == "AlphaTrader_bt7_XAUUSD.x_random_trades.csv"
    assert export.export_filename({**run, "symbol": "طلا/..x"}, "all", "xlsx") == "AlphaTrader_bt7_x_random_all.xlsx"


def test_csv_cells_full_precision_and_formula_guard() -> None:
    table = export.Table("t", "t", ["a", "b", "c", "d", "e", "f", "g"],
                         [[0.1 + 0.2, 2**63 - 1, True, None, "=HYPERLINK(1)", "-x", "متن\nدو خط"]])
    rows = read_csv(export.to_csv(table))
    assert rows[0] == ["a", "b", "c", "d", "e", "f", "g"]
    assert rows[1] == ["0.30000000000000004", "9223372036854775807", "true", "", "'=HYPERLINK(1)", "'-x",
                       "متن\nدو خط"]
    assert float(rows[1][0]) == 0.1 + 0.2


def test_xlsx_numbers_booleans_big_ints_and_empty_cells() -> None:
    table = export.Table("t", "جدول", ["a", "b", "c", "d", "e"], [[1e-05, -2, 2**63 - 1, False, None]])
    book = read_xlsx(export.to_xlsx([table]))
    assert book.names == ["جدول"]
    assert book.sheets["جدول"] == [["a", "b", "c", "d", "e"], [1e-05, -2, "9223372036854775807", False]]
    assert b'<c r="A2"><v>1e-05</v></c>' in book.raw["xl/worksheets/sheet1.xml"]


# ------------------------------------------------------------------------------------------ unit: repo -> file
def test_manual_fake_run_workbook_sheets_values_and_escaping(db: EngineConnection) -> None:
    run_id = store_fake_run(db, reason_fa=NASTY, detail=NASTY)
    repo = BacktestsRepo(db)
    run = repo.get_run(run_id)
    out = export.export_run(repo, run, "xlsx")
    assert out.media_type == export.XLSX_MEDIA_TYPE and out.filename == f"AlphaTrader_bt{run_id}_XAUUSD.x_manual_all.xlsx"
    book = read_xlsx(out.content)
    assert book.names == ["مشخصات اجرا", "معیارها", "معاملات", "ردشده‌ها", "منحنی سرمایه"]  # no windows sheet
    assert out.rows == {"run": len(book.sheets["مشخصات اجرا"]) - 1, "metrics": len(book.sheets["معیارها"]) - 1,
                        "trades": 5, "skipped": 2, "equity": 3}
    trades = book.sheets["معاملات"]
    header = trades[0]
    stored = repo.get_trades(run_id)
    for row, t in zip(trades[1:], stored, strict=True):
        for col, value in zip(header, row + [None] * (len(header) - len(row))):
            want = t["indicators"].get(col[len("indicator."):]) if col.startswith("indicator.") else t[col]
            assert xlsx_matches(value, want), (col, value, want)
    assert trades[1][header.index("reason_fa")] == NASTY
    assert book.sheets["ردشده‌ها"][1][book.sheets["ردشده‌ها"][0].index("detail")] == NASTY
    info = kv(book.sheets["مشخصات اجرا"])
    assert info["window.bars"] == 480 and info["window.start"] == "2024-09-02T00:00:00Z"
    metrics = kv(book.sheets["معیارها"])
    assert metrics["metrics_kind"] == "single_window" and metrics["net_profit"] == 12.345678901234567
    assert metrics["max_drawdown_peak_time"] == "2024-09-18T04:00:00Z" and metrics["profit_factor"] is None


def test_big_run_exports_under_a_second(db: EngineConnection) -> None:
    run_id = store_fake_run(db, mode="random", trades_per_window=250, windows=4)
    repo = BacktestsRepo(db)
    run = repo.get_run(run_id)
    t0 = time.perf_counter()
    out = export.export_run(repo, run, "xlsx")
    xlsx_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    csv_out = export.export_run(repo, run, "csv", "trades")
    csv_s = time.perf_counter() - t0
    assert xlsx_s < 1.0 and csv_s < 1.0, (xlsx_s, csv_s)
    book = read_xlsx(out.content)
    assert len(book.sheets["معاملات"]) == 1001 and len(read_csv(csv_out.content)) == 1001
    assert len(book.sheets["پنجره‌ها"]) == 5


# ------------------------------------------------------------------------------------------ API on real runs
@dataclass
class Real:
    client: TestClient
    manual: int
    random: int


@pytest.fixture(scope="module")
def real_dir(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, int, int]:
    """A seeded cache + one manual and one random run simulated through the API (once per module)."""
    root = tmp_path_factory.mktemp("export_real")
    data_dir = root / "data"
    seed_cache(data_dir, repo_data_dir=root / "not_data", symbols=(GOLD,), start="2024-06-03", end="2025-03-03")
    env_file = root / "fake.env"
    env_file.write_text("", encoding="utf-8")
    settings = load_settings(env_file=env_file, environ={"ENGINE_MT5_AUTOCONNECT": "false",
                                                        "ALPHA_TRADER_DATA_DIR": str(data_dir)})
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        ids = []
        for body in (MANUAL, RANDOM):
            response = client.post("/backtests", json=body)
            assert response.status_code == 202, response.text
            ids.append(response.json()["id"])
        for run_id in ids:
            deadline = time.monotonic() + 120
            while client.get(f"/backtests/{run_id}").json()["status"] not in ("done", "error"):
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert client.get(f"/backtests/{run_id}").json()["status"] == "done"
    return data_dir, ids[0], ids[1]


@pytest.fixture
def real(real_dir: tuple[Path, int, int], make_settings) -> Iterator[Real]:
    data_dir, manual, random_id = real_dir
    settings = make_settings(environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        yield Real(client, manual, random_id)


def _get(client: TestClient, run_id: int, **query: Any):
    response = client.get(f"/backtests/{run_id}/export", params=query)
    assert response.status_code == 200, response.text
    return response


def _json(client: TestClient, path: str, **query: Any) -> dict:
    response = client.get(path, params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_csv_trades_round_trip_exactly_against_the_json_api(real: Real) -> None:
    for run_id in (real.manual, real.random):
        response = _get(real.client, run_id, format="csv", table="trades")
        detail = _json(real.client, f"/backtests/{run_id}")
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        assert response.headers["content-disposition"] == (
            f'attachment; filename="AlphaTrader_bt{run_id}_XAUUSD.x_{detail["mode"]}_trades.csv"')
        assert response.headers["x-alpha-run-status"] == "done"
        rows = read_csv(response.content)
        stored = _json(real.client, f"/backtests/{run_id}/trades", limit=5000)
        assert stored["total"] > 0 and len(rows) - 1 == stored["total"] == detail["trade_count"]
        header = rows[0]
        fixed = [c for c in Trade.model_fields if c != "indicators"]
        assert header[:len(fixed)] == fixed and all(c.startswith("indicator.") for c in header[len(fixed):])
        for row, trade in zip(rows[1:], stored["trades"], strict=True):
            for col, text in zip(header, row, strict=True):
                want = trade["indicators"].get(col[len("indicator."):]) if col.startswith("indicator.") else trade[col]
                assert csv_matches(text, want), (col, text, want)
            for col in ("entry_time", "exit_time", "decision_time", "confirmation_bar_time", "exit_bar_time"):
                assert TIME_Z.match(row[header.index(col)])
            assert _is_persian(row[header.index("exit_reason_fa")]) and _is_persian(row[header.index("reason_fa")])


def test_csv_other_tables_counts_and_values(real: Real) -> None:
    rid = real.random
    detail = _json(real.client, f"/backtests/{rid}")
    windows = read_csv(_get(real.client, rid, format="csv", table="windows").content)
    assert len(windows) - 1 == len(detail["windows"]) == 4
    h = windows[0]
    for row, w in zip(windows[1:], detail["windows"], strict=True):
        assert csv_matches(row[h.index("window_index")], w["index"]) and row[h.index("start")] == w["start"]
        assert csv_matches(row[h.index("final_balance")], w["final_balance"])
        for key, value in w["metrics"].items():
            text = row[h.index(f"metrics.{key}")]
            if isinstance(value, str) and value.endswith("+00:00"):
                assert text == value[:-6] + "Z"
            else:
                assert csv_matches(text, value), (key, text, value)
    skipped = read_csv(_get(real.client, rid, format="csv", table="skipped").content)
    assert len(skipped) - 1 == _json(real.client, f"/backtests/{rid}/skipped")["count"]
    equity = read_csv(_get(real.client, rid, format="csv", table="equity").content)
    points = _json(real.client, f"/backtests/{rid}/equity")["points"]
    assert len(equity) - 1 == len(points) > 0
    assert [csv_matches(r[3], p["equity"]) and r[1] == p["time"] for r, p in zip(equity[1:], points)] == [True] * len(points)
    run_rows = read_csv(_get(real.client, rid, format="csv", table="run").content)
    assert run_rows[0] == ["key", "label_fa", "value"]
    info = {r[0]: r[2] for r in run_rows[1:]}
    assert info["seed"] == str(SEED_MAX) and info["cost_model.commission_per_lot_per_side"] == "3.5"
    metrics = {r[0]: r[2] for r in read_csv(_get(real.client, rid, format="csv", table="metrics").content)[1:]}
    assert csv_matches(metrics["pooled.win_rate"], detail["metrics"]["pooled"]["win_rate"])
    assert metrics["distribution.count"] == "4"
    fa = read_csv(_get(real.client, rid, format="csv", table="trades", header="fa").content)
    assert fa[0][0] == "شماره پنجره" and fa[0][fa[0].index("دلیل خروج")] and len(fa) == len(
        read_csv(_get(real.client, rid, format="csv").content))  # default table = trades


def test_xlsx_random_workbook_provenance_and_values(real: Real) -> None:
    rid = real.random
    response = _get(real.client, rid)  # default format = xlsx
    assert response.headers["content-type"] == export.XLSX_MEDIA_TYPE
    assert response.headers["content-disposition"] == f'attachment; filename="AlphaTrader_bt{rid}_XAUUSD.x_random_all.xlsx"'
    assert _get(real.client, rid, format="xlsx", table="all").content[:2] == b"PK"
    book = read_xlsx(response.content)
    assert book.names == ["مشخصات اجرا", "معیارها", "معاملات", "پنجره‌ها", "ردشده‌ها", "منحنی سرمایه"]
    detail = _json(real.client, f"/backtests/{rid}")
    info = kv(book.sheets["مشخصات اجرا"])
    assert book.sheets["مشخصات اجرا"][0] == ["key", "label_fa", "value"]
    config = detail["config"]
    expected = {
        "id": rid, "status": "done", "symbol": GOLD, "mode": "random", "seed": str(SEED_MAX),  # text: > 2**53
        "seed_generated": False, "windows_count": 4, "window_months": 1, "strategy": detail["strategy"],
        "strategy_version": detail["strategy_version"], "strategy_source": "builtin", "strategy_sha256": None,
        "params_version": detail["params_version"], "params_hash": detail["params_hash"], "provisional": True,
        "provisional_label_fa": detail["provisional_label_fa"], "created_at": detail["created_at"],
        "finished_at": detail["finished_at"], "cost_model.commission_per_lot_per_side": 3.5,
        "cost_model.spread": "historical", "cost_model.swap": "none",
        "spread_fallback.points": detail["spread_fallback"]["points"],
        "spread_fallback.source": detail["spread_fallback"]["source"],
        "fingerprint.h1_sha256": detail["fingerprint"]["h1_sha256"],
        "fingerprint.h4_sha256": detail["fingerprint"]["h4_sha256"],
        "plan.seed": str(SEED_MAX), "plan.algorithm": "numpy.PCG64", "result_meta.entry_rule": "next_bar_open",
        "account.balance": config["account"]["balance"], "account.risk_pct": config["account"]["risk_pct"],
        "request.seed": str(SEED_MAX), "equity_storage_rule": detail["equity_storage_rule"],
    }
    for key, value in expected.items():
        assert info.get(key) == value, (key, info.get(key), value)
    for key, value in config["params"].items():
        assert xlsx_matches(info[f"params.{key}"], value), key
    for i, label in enumerate(detail["labels_fa"]):
        assert info[f"labels_fa.{i}"] == label
    assert "plan.windows.0.start" not in info and "export_note_fa" not in info
    labels = {r[0]: r[1] for r in book.sheets["مشخصات اجرا"][1:]}
    assert labels["seed"] == "seed" and labels["params_hash"] == "هش پارامترها"
    metrics = kv(book.sheets["معیارها"])
    assert metrics["metrics_kind"] == "random_aggregate"
    for block in ("pooled", "mean", "worst"):
        for key, value in detail["metrics"][block].items():
            assert xlsx_matches(metrics[f"{block}.{key}"], value), (block, key)
    assert metrics["distribution.count"] == 4 and _is_persian(metrics["note_fa"])
    windows = book.sheets["پنجره‌ها"]
    assert len(windows) == 5 and windows[0][:3] == ["window_index", "start", "end"]
    trades = book.sheets["معاملات"]
    stored = _json(real.client, f"/backtests/{rid}/trades", limit=5000)["trades"]
    assert len(trades) - 1 == len(stored)
    header = trades[0]
    for row, t in zip(trades[1:], stored, strict=True):
        row = row + [None] * (len(header) - len(row))
        for col, value in zip(header, row):
            want = t["indicators"].get(col[len("indicator."):]) if col.startswith("indicator.") else t[col]
            assert xlsx_matches(value, want), (col, value, want)
    assert len(book.sheets["ردشده‌ها"]) - 1 == _json(real.client, f"/backtests/{rid}/skipped")["count"]
    assert len(book.sheets["منحنی سرمایه"]) - 1 == _json(real.client, f"/backtests/{rid}/equity")["count"]


def test_xlsx_manual_workbook_has_no_windows_sheet_and_persian_headers(real: Real) -> None:
    book = read_xlsx(_get(real.client, real.manual, format="xlsx", header="fa").content)
    assert "پنجره‌ها" not in book.names and len(book.names) == 5
    assert book.sheets["معاملات"][0][0] == "شماره پنجره" and book.sheets["مشخصات اجرا"][0] == ["کلید", "عنوان", "مقدار"]
    info = kv(book.sheets["مشخصات اجرا"])
    detail = _json(real.client, f"/backtests/{real.manual}")
    assert info["from"] == MANUAL["from"] and info["to"] == MANUAL["to"] and info["seed"] is None
    assert info["window.bars"] == detail["windows"][0]["bars"]
    metrics = kv(book.sheets["معیارها"])
    for key, value in detail["metrics"].items():
        if isinstance(value, str) and value.endswith("+00:00"):
            assert metrics[key] == value[:-6] + "Z"
        else:
            assert xlsx_matches(metrics.get(key), value), key


# ------------------------------------------------------------------------------------------ errors / statuses
def _error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code and _is_persian(detail["message_fa"]) and isinstance(detail["errors_fa"], list)
    return detail


def test_export_errors_are_standard_persian(fake_client: TestClient) -> None:
    db = fake_client.app.state.db
    _error(fake_client.get("/backtests/999/export"), 404, "backtest_not_found")
    done = store_fake_run(db)
    for query in ({"format": "pdf"}, {"format": "csv", "table": "all"}, {"format": "csv", "table": "orders"},
                  {"format": "xlsx", "table": "orders"}, {"format": "csv", "header": "de"}, {"format": ""}):
        _error(fake_client.get(f"/backtests/{done}/export", params=query), 422, "invalid_query")
    for status in ("queued", "running"):
        run_id = store_fake_run(db, status=status)
        for fmt in ("csv", "xlsx"):
            _error(fake_client.get(f"/backtests/{run_id}/export", params={"format": fmt}), 409, "run_not_finished")


@pytest.mark.parametrize("status", ["error", "cancelled", "interrupted"])
def test_unfinished_runs_export_what_exists_with_a_status_note(fake_client: TestClient, status: str) -> None:
    run_id = store_fake_run(fake_client.app.state.db, mode="random", windows=3, status=status)
    response = _get(fake_client, run_id)
    assert response.headers["x-alpha-run-status"] == status
    book = read_xlsx(response.content)
    assert "پنجره‌ها" in book.names
    info = kv(book.sheets["مشخصات اجرا"])
    assert book.sheets["مشخصات اجرا"][1][0] == "export_note_fa" and _is_persian(info["export_note_fa"])
    assert info["status"] == status and info["error_code"] == "internal_error" and info["seed"] == str(SEED_MAX)
    assert all(len(book.sheets[n]) == 1 for n in ("معاملات", "پنجره‌ها", "ردشده‌ها", "منحنی سرمایه"))  # header only
    assert kv(book.sheets["معیارها"]) == {"metrics_kind": None}
    trades = read_csv(_get(fake_client, run_id, format="csv", table="trades").content)
    assert len(trades) == 1 and trades[0][0] == "window_index"


# ------------------------------------------------------------------------------------------ DEV_MODE logs
class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.parametrize("dev", [True, False])
def test_export_logs_only_under_dev_mode_without_secrets(make_settings, dev: bool) -> None:
    handler = _ListHandler()
    settings = make_settings(f"DEV_MODE={'true' if dev else 'false'}\nMT5_PASSWORD=S3cr3t!\nMT5_LOGIN=12345678\n")
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        run_id = store_fake_run(client.app.state.db)
        busy = store_fake_run(client.app.state.db, status="running")
        logging.getLogger(ROOT_LOGGER_NAME).addHandler(handler)
        try:
            _get(client, run_id, format="csv", table="trades")
            _get(client, run_id, format="xlsx")
            assert client.get(f"/backtests/{busy}/export").status_code == 409
        finally:
            logging.getLogger(ROOT_LOGGER_NAME).removeHandler(handler)
    text = "\n".join(handler.messages)
    assert "S3cr3t!" not in text and "12345678" not in text
    exports = [m for m in handler.messages if "export" in m]
    if dev:
        assert any(f"backtest export run {run_id}" in m and "format=csv table=trades" in m and "rows={'trades': 5}"
                   in m and "bytes=" in m and "total=" in m for m in exports), exports
        assert any("format=xlsx table=all" in m for m in exports)
        assert any(f"GET /backtests/{busy}/export rejected: run is running" in m for m in exports)
    else:
        assert exports == []
