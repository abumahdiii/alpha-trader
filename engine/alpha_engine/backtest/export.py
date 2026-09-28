"""CSV and Excel (xlsx) export of a stored backtest run (phase 7, section 3).

What is exported is exactly what ``storage/backtests_repo.py`` stored -- no trading math is recomputed here.
The only transformations are presentation:

* times: stored UTC strings become ISO-8601 with ``Z`` (``+00:00`` -> ``Z``; the stored instant is unchanged);
* nested values: dicts are flattened to dotted keys (``params.n``, ``pooled.win_rate``); lists of strings
  inside a table cell (``flags``, ``sizing_warnings_fa``) are JSON text;
* numbers: CSV writes ``repr(float)`` / ``str(int)`` (full precision, ``float(text) == stored``); xlsx writes
  numeric cells with the same text, EXCEPT integers beyond 2**53 (e.g. a 63-bit seed), which are written as
  text so Excel cannot round them;
* CSV only: a TEXT cell starting with ``=``, ``+``, ``-``, ``@``, TAB or CR gets a leading ``'`` so a
  spreadsheet does not evaluate it as a formula (CSV injection; strategy texts can come from uploaded
  plugins). Numbers are never touched. xlsx cells are inline strings and are never formulas.

Tables (English snake_case header keys are stable; ``header="fa"`` gives Persian labels instead):

* ``run``      -- key / label_fa / value rows: status (+ a note for runs that did not finish), times, symbol,
  mode, period, seed, strategy name/version/source/sha256, params (flattened) + params_version/hash,
  account settings, cost model, spread fallback, result meta, labels, provisional label, window plan
  (without the window list), data fingerprint, the request as sent, timings, equity storage rule;
* ``metrics``  -- key / label_fa / value rows: ``metrics_kind``, the overall metrics (manual: the window's
  metrics; random: pooled / mean / worst ...) and, for random runs, ``distribution.*``;
* ``trades``   -- one row per trade (all windows), every :class:`~alpha_engine.backtest.models.Trade` field in
  model order, ``indicators`` expanded to ``indicator.<name>`` columns (sorted union) at the end;
* ``windows``  -- one row per window: the stored window columns + ``metrics.<name>``;
* ``skipped``  -- one row per skipped candidate;
* ``equity``   -- the stored (downsampled) equity points.

The workbook has the sheets «مشخصات اجرا», «معیارها», «معاملات», «پنجره‌ها» (random runs only; a manual run's
single window is in the run sheet as ``window.*``), «ردشده‌ها», «منحنی سرمایه». The xlsx is written with the
standard library only (``zipfile`` + minimal SpreadsheetML: inline strings, no shared-strings table), sheets
right-to-left, header row bold and frozen.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import time
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from xml.sax.saxutils import escape as _xml_escape

from ..logging_setup import get_logger, is_dev_mode
from ..storage.backtests_repo import BacktestsRepo, api_time
from .models import PROVISIONAL_LABEL_FA, SkippedCandidate, Trade
from .results import EQUITY_STORAGE_RULE

logger = get_logger(__name__)

ExportFormat = Literal["csv", "xlsx"]
EXPORT_FORMATS: tuple[str, ...] = ("csv", "xlsx")
CSV_TABLES: tuple[str, ...] = ("trades", "windows", "skipped", "equity", "run", "metrics")
HEADER_LANGS: tuple[str, ...] = ("en", "fa")
EXPORT_FORMAT_VERSION = 1

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SHEET_RUN_FA = "مشخصات اجرا"
SHEET_METRICS_FA = "معیارها"
SHEET_TRADES_FA = "معاملات"
SHEET_WINDOWS_FA = "پنجره‌ها"
SHEET_SKIPPED_FA = "ردشده‌ها"
SHEET_EQUITY_FA = "منحنی سرمایه"

STATUS_FA = {"done": "پایان‌یافته", "error": "خطا", "cancelled": "لغوشده", "interrupted": "نیمه‌کاره (قطع engine)",
             "queued": "در صف", "running": "در حال اجرا"}
NOT_DONE_NOTE_FA = ("این اجرا با وضعیت «{status}» پایان یافته و نتیجه‌ای (معامله، معیار یا منحنی سرمایه) ذخیره "
                    "نکرده است؛ فقط مشخصات اجرا خروجی گرفته شد.")

_ISO_UTC_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_EXCEL_EXACT_INT = 2**53


# ------------------------------------------------------------------------------------------ labels
KEY_LABELS_FA: dict[str, str] = {
    # run sheet
    "export_note_fa": "توضیح خروجی", "id": "شناسه اجرا", "status": "وضعیت", "status_fa": "وضعیت (فارسی)",
    "progress": "پیشرفت (٪)", "error_code": "کد خطا", "error_message_fa": "پیام خطا",
    "created_at": "زمان ثبت (UTC)", "started_at": "زمان شروع (UTC)", "finished_at": "زمان پایان (UTC)",
    "elapsed_s": "مدت اجرا (ثانیه)", "symbol": "نماد", "mode": "حالت اجرا", "from": "ابتدای بازه (UTC)",
    "to": "انتهای بازه (UTC)", "windows_count": "تعداد پنجره‌ها", "window_months": "طول هر پنجره (ماه)",
    "seed": "seed", "seed_generated": "seed توسط engine تولید شد", "strategy": "سیستم معاملاتی",
    "strategy_version": "نسخه کد سیستم", "strategy_source": "منبع سیستم", "strategy_sha256": "SHA-256 فایل سیستم",
    "params_version": "نسخه پارامترها", "params_hash": "هش پارامترها", "provisional": "موقت",
    "provisional_label_fa": "برچسب موقت", "summary_basis": "مبنای خلاصه", "trade_count": "تعداد معاملات",
    "net_profit": "سود خالص", "net_profit_pct": "سود خالص (٪)", "equity_storage_rule": "قاعده ذخیره منحنی سرمایه",
    "exported_at": "زمان خروجی (UTC)", "export_format_version": "نسخه قالب خروجی",
    # metrics
    "metrics_kind": "نوع معیارها", "initial_balance": "موجودی اولیه", "final_balance": "موجودی نهایی",
    "win_count": "تعداد برد", "loss_count": "تعداد باخت", "breakeven_count": "تعداد سر به سر",
    "win_rate": "نرخ برد (۰ تا ۱)", "gross_profit": "سود ناخالص", "gross_loss": "زیان ناخالص",
    "profit_factor": "فاکتور سود", "profit_factor_infinite": "فاکتور سود بی‌نهایت", "expectancy": "امید ریاضی",
    "avg_win": "میانگین برد", "avg_loss": "میانگین باخت", "largest_win": "بزرگ‌ترین برد",
    "largest_loss": "بزرگ‌ترین باخت", "max_consecutive_wins": "بیشترین برد پیاپی",
    "max_consecutive_losses": "بیشترین باخت پیاپی", "avg_r": "میانگین R", "r_count": "تعداد معاملات با R",
    "max_drawdown_abs": "بیشترین افت سرمایه", "max_drawdown_pct": "بیشترین افت سرمایه (٪)",
    "max_drawdown_peak_time": "قله افت سرمایه (UTC)", "max_drawdown_trough_time": "کف افت سرمایه (UTC)",
    "sharpe": "نسبت شارپ", "sharpe_return_count": "تعداد بازده روزانه شارپ", "window_count": "تعداد پنجره‌ها",
    "windows_with_trades": "پنجره‌های دارای معامله", "total_trades": "کل معاملات", "win_rate_windows":
    "پنجره‌های مبنای نرخ برد", "sharpe_windows": "پنجره‌های مبنای شارپ", "note_fa": "توضیح",
    "count": "تعداد", "windows_without_trades": "پنجره‌های بدون معامله", "mean_net_profit": "میانگین سود خالص",
    "median_net_profit": "میانه سود خالص", "mean_net_profit_pct": "میانگین سود خالص (٪)",
    "median_net_profit_pct": "میانه سود خالص (٪)", "pct_profitable": "درصد پنجره‌های سودده",
    "index": "شماره پنجره",
    # trades
    "window_index": "شماره پنجره", "trade_index": "شماره معامله", "direction": "جهت", "setup_type": "نوع ستاپ",
    "line": "خط کانال", "pattern": "الگو", "confirmation_bar_time": "کندل تایید (UTC)",
    "decision_time": "زمان تصمیم (UTC)", "entry_time": "زمان ورود (UTC)", "entry": "قیمت ورود",
    "entry_bid_open": "باز شدن کندل ورود (bid)", "stop_loss": "حد ضرر", "take_profit": "حد سود",
    "rr": "نسبت ریوارد به ریسک", "volume": "حجم (لات)", "risk_amount": "مبلغ ریسک",
    "balance_before": "موجودی قبل", "exit_bar_time": "کندل خروج (UTC)", "exit_time": "زمان خروج (UTC)",
    "exit_price": "قیمت خروج", "exit_reason": "دلیل خروج (کد)", "exit_reason_fa": "دلیل خروج",
    "gross_pnl": "سود/زیان ناخالص", "commission": "کمیسیون", "net_pnl": "سود/زیان خالص", "r_multiple": "ضریب R",
    "balance_after": "موجودی بعد", "bars_held": "تعداد کندل نگهداری",
    "spread_at_entry_points": "اسپرد ورود (پوینت)", "spread_at_exit_points": "اسپرد خروج (پوینت)",
    "held_over_weekend": "نگهداری در آخر هفته", "flags": "پرچم‌ها", "sizing_warnings_fa": "هشدارهای حجم",
    "reason_fa": "دلیل", "indicators": "اندیکاتورها",
    # windows
    "start": "ابتدای پنجره (UTC)", "end": "انتهای پنجره (UTC)", "skipped_count": "تعداد ردشده",
    "candidates": "تعداد کاندیدها", "bars": "تعداد کندل", "first_bar_time": "اولین کندل (UTC)",
    "last_bar_time": "آخرین کندل (UTC)", "zero_spread_bars_filled": "کندل‌های اسپرد صفر پرشده",
    "zero_spread_bars_unfilled": "کندل‌های اسپرد صفر پرنشده", "weekend_holds": "نگهداری‌های آخر هفته",
    "spread_fallback_bars": "کندل‌های اسپرد جایگزین", "stopped_reason": "دلیل توقف",
    "equity_points_full": "نقاط کامل منحنی سرمایه", "equity_points_stored": "نقاط ذخیره‌شده منحنی سرمایه",
    # skipped / equity
    "time": "زمان (UTC)", "reason": "دلیل (کد)", "detail": "جزئیات", "balance": "موجودی", "equity": "ارزش حساب",
    # key/value tables
    "key": "کلید", "label_fa": "عنوان", "value": "مقدار",
}
PREFIX_LABELS_FA: dict[str, str] = {
    "params": "پارامتر", "account": "تنظیمات حساب", "cost_model": "مدل هزینه", "spread_fallback": "اسپرد جایگزین",
    "result_meta": "مشخصات نتیجه", "labels_fa": "برچسب", "plan": "برنامه پنجره‌ها", "fingerprint": "اثر انگشت داده",
    "request": "درخواست", "timings": "زمان‌بندی", "window": "پنجره", "pooled": "تجمیعی (همه معاملات)",
    "mean": "میانگین پنجره‌ها", "worst": "بدترین پنجره", "distribution": "توزیع پنجره‌ها",
    "worst_window": "بدترین پنجره", "best_window": "بهترین پنجره", "metrics": "معیار", "indicator": "اندیکاتور",
    "spec": "مشخصات نماد",
}


def label_fa(key: str) -> str:
    """Persian label of a (dotted) key: known leaf label, prefixed with the Persian name of known sections."""
    parts = key.split(".")
    leaf = KEY_LABELS_FA.get(parts[-1], parts[-1])
    prefixes = [PREFIX_LABELS_FA.get(p, p) for p in parts[:-1]]
    return " / ".join([*prefixes, leaf]) if prefixes else leaf


# ------------------------------------------------------------------------------------------ data
@dataclass
class Table:
    """One exported table: English header keys + rows of scalar values (``None`` = empty cell)."""

    name: str
    sheet_fa: str
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)

    def header(self, lang: str) -> list[str]:
        return list(self.columns) if lang == "en" else [label_fa(c) for c in self.columns]


@dataclass
class ExportData:
    """What the repo stored for one run (read once per request)."""

    run: dict[str, Any]
    windows: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    equity: list[dict[str, Any]] = field(default_factory=list)


def load_export_data(repo: BacktestsRepo, run: dict[str, Any], tables: Iterable[str]) -> ExportData:
    """Read only the stored parts the requested tables need (``run`` = ``repo.get_run(id)``)."""
    wanted = set(tables)
    run_id = int(run["id"])
    data = ExportData(run=run)
    if wanted & {"windows", "run"}:
        data.windows = repo.get_windows(run_id)
    if "trades" in wanted:
        data.trades = repo.get_trades(run_id)
    if "skipped" in wanted:
        data.skipped = repo.get_skipped(run_id)
    if "equity" in wanted:
        data.equity = repo.get_equity(run_id)
    return data


# ------------------------------------------------------------------------------------------ value helpers
def _norm(value: Any) -> Any:
    """Presentation normalisation of one scalar: ISO UTC ``+00:00`` -> ``Z``; lists/dicts -> JSON text."""
    if isinstance(value, str):
        if value.endswith("+00:00") and _ISO_UTC_OFFSET.match(value):
            return value[:-6] + "Z"
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def _flatten(prefix: str, value: Any, out: list[tuple[str, Any]], skip: frozenset[str] = frozenset()) -> None:
    """Dotted-key rows of a nested JSON value (dicts by key, lists by index; empty containers as JSON)."""
    if isinstance(value, dict) and value:
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if key in skip:
                continue
            _flatten(key, v, out, skip)
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            _flatten(f"{prefix}.{i}", v, out, skip)
    else:
        out.append((prefix, _norm(value)))


def _strategy_identity(run: dict[str, Any]) -> tuple[str | None, str]:
    config = run.get("config") if isinstance(run.get("config"), dict) else {}
    return config.get("strategy_sha256"), config.get("strategy_source") or "builtin"


# ------------------------------------------------------------------------------------------ tables
def run_table(data: ExportData, exported_at: datetime | None = None) -> Table:
    run = data.run
    config = run.get("config") if isinstance(run.get("config"), dict) else {}
    meta = run.get("result") if isinstance(run.get("result"), dict) else None
    sha, source = _strategy_identity(run)
    status = run["status"]
    rows: list[tuple[str, Any]] = []
    if status != "done":
        rows.append(("export_note_fa", NOT_DONE_NOTE_FA.format(status=STATUS_FA.get(status, status))))
    rows += [
        ("id", run["id"]), ("status", status), ("status_fa", STATUS_FA.get(status, status)),
        ("progress", run["progress"]), ("error_code", run.get("error_code")),
        ("error_message_fa", run.get("error_message_fa")),
        ("created_at", api_time(run.get("created_utc"))), ("started_at", api_time(run.get("started_utc"))),
        ("finished_at", api_time(run.get("finished_utc"))), ("elapsed_s", run.get("elapsed_s")),
        ("symbol", run["symbol"]), ("mode", run["mode"]),
        ("from", api_time(run.get("period_start_utc"))), ("to", api_time(run.get("period_end_utc"))),
        ("windows_count", run.get("windows_count")), ("window_months", run.get("window_months")),
        ("seed", run.get("seed")),
        ("seed_generated", None if run.get("seed_generated") is None else bool(run["seed_generated"])),
        ("strategy", run["strategy_name"]), ("strategy_version", run["strategy_version"]),
        ("strategy_source", source), ("strategy_sha256", sha),
        ("params_version", run.get("params_version")), ("params_hash", run["params_hash"]),
        ("provisional", bool(run["provisional"])),
        ("provisional_label_fa", PROVISIONAL_LABEL_FA if run["provisional"] else None),
        ("trade_count", run.get("trade_count")), ("net_profit", run.get("net_profit")),
        ("net_profit_pct", run.get("net_profit_pct")),
        ("summary_basis", "single_window" if run["mode"] == "manual" else "window_mean"),
    ]
    _flatten("params", config.get("params"), rows)
    _flatten("account", config.get("account"), rows)
    _flatten("cost_model", (meta or {}).get("cost_model", config.get("cost_model")), rows)
    if meta is not None:
        _flatten("spread_fallback", meta.get("spread_fallback"), rows)
        _flatten("result_meta", {k: v for k, v in meta.items() if k not in ("cost_model", "spread_fallback")}, rows)
    _flatten("labels_fa", run.get("labels"), rows)
    if run.get("plan") is not None:  # the window list is the windows sheet (or window.* below)
        _flatten("plan", run["plan"], rows, skip=frozenset({"plan.windows"}))
    if run["mode"] == "manual" and data.windows:
        w = {k: v for k, v in data.windows[0].items() if k not in ("id", "run_id", "metrics")}
        _flatten("window", {_WINDOW_RENAME.get(k, k): _window_value(k, v) for k, v in w.items()}, rows)
    if run.get("fingerprint") is not None:
        _flatten("fingerprint", run["fingerprint"], rows)
    _flatten("request", run.get("request"), rows)
    if run.get("timings") is not None:
        _flatten("timings", run["timings"], rows)
    stamp = (exported_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    rows += [("equity_storage_rule", EQUITY_STORAGE_RULE), ("exported_at", _norm(stamp)),
             ("export_format_version", EXPORT_FORMAT_VERSION)]
    return Table("run", SHEET_RUN_FA, ["key", "label_fa", "value"],
                 [[k, label_fa(k), _norm(v)] for k, v in rows])


def metrics_table(data: ExportData) -> Table:
    run = data.run
    meta = run.get("result") if isinstance(run.get("result"), dict) else None
    rows: list[tuple[str, Any]] = [("metrics_kind", meta.get("metrics_kind") if meta else None)]
    if run.get("metrics") is not None:
        _flatten("", run["metrics"], rows)
    if run.get("distribution") is not None:
        _flatten("distribution", run["distribution"], rows)
    return Table("metrics", SHEET_METRICS_FA, ["key", "label_fa", "value"],
                 [[k, label_fa(k), _norm(v)] for k, v in rows])


def _columns(model_fields: Sequence[str], items: Sequence[dict[str, Any]], exclude: tuple[str, ...] = ()) -> list[str]:
    """Model field order, then any extra stored keys (sorted) so nothing stored is dropped."""
    fixed = [c for c in model_fields if c not in exclude]
    extra = sorted({k for it in items for k in it} - set(fixed) - set(exclude))
    return fixed + extra


def trades_table(data: ExportData) -> Table:
    columns = _columns(list(Trade.model_fields), data.trades, exclude=("indicators",))
    ind_keys = sorted({k for t in data.trades for k in (t.get("indicators") or {})})
    rows = []
    for t in data.trades:
        ind = t.get("indicators") or {}
        rows.append([_norm(t.get(c)) for c in columns] + [_norm(ind.get(k)) for k in ind_keys])
    return Table("trades", SHEET_TRADES_FA, columns + [f"indicator.{k}" for k in ind_keys], rows)


WINDOW_COLUMNS = ["window_index", "start_utc", "end_utc", "initial_balance", "final_balance", "trade_count",
                  "skipped_count", "candidates", "bars", "first_bar_utc", "last_bar_utc", "zero_spread_bars_filled",
                  "zero_spread_bars_unfilled", "weekend_holds", "spread_fallback_bars", "stopped_reason",
                  "equity_points_full", "equity_points_stored"]
_WINDOW_RENAME = {"start_utc": "start", "end_utc": "end", "first_bar_utc": "first_bar_time",
                  "last_bar_utc": "last_bar_time"}


def _window_value(key: str, value: Any) -> Any:
    return api_time(value) if key in _WINDOW_RENAME and value is not None else value


def windows_table(data: ExportData) -> Table:
    metric_keys: list[str] = []
    for w in data.windows:
        for k in (w.get("metrics") or {}):
            if k not in metric_keys:
                metric_keys.append(k)
    rows = []
    for w in data.windows:
        m = w.get("metrics") or {}
        rows.append([_norm(_window_value(c, w.get(c))) for c in WINDOW_COLUMNS] + [_norm(m.get(k)) for k in metric_keys])
    columns = [_WINDOW_RENAME.get(c, c) for c in WINDOW_COLUMNS] + [f"metrics.{k}" for k in metric_keys]
    return Table("windows", SHEET_WINDOWS_FA, columns, rows)


def skipped_table(data: ExportData) -> Table:
    columns = _columns(list(SkippedCandidate.model_fields), data.skipped)
    return Table("skipped", SHEET_SKIPPED_FA, columns, [[_norm(s.get(c)) for c in columns] for s in data.skipped])


def equity_table(data: ExportData) -> Table:
    columns = ["window_index", "time", "balance", "equity"]
    return Table("equity", SHEET_EQUITY_FA, columns, [[_norm(p.get(c)) for c in columns] for p in data.equity])


def build_table(data: ExportData, name: str, exported_at: datetime | None = None) -> Table:
    if name == "run":
        return run_table(data, exported_at)
    if name == "metrics":
        return metrics_table(data)
    return {"trades": trades_table, "windows": windows_table, "skipped": skipped_table,
            "equity": equity_table}[name](data)


def workbook_tables(mode: str) -> list[str]:
    """Sheets of the workbook, in order (the windows sheet only for random runs)."""
    names = ["run", "metrics", "trades", "windows", "skipped", "equity"]
    return names if mode == "random" else [n for n in names if n != "windows"]


# ------------------------------------------------------------------------------------------ CSV
def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else ""
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def to_csv(table: Table, header: str = "en") -> bytes:
    """UTF-8 with BOM (Excel shows Persian), CRLF line endings, RFC 4180 quoting."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(table.header(header))
    for row in table.rows:
        writer.writerow([_csv_cell(v) for v in row])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


# ------------------------------------------------------------------------------------------ XLSX
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_ST_XSTRING_ESCAPE = re.compile(r"_(x[0-9A-Fa-f]{4}_)")
# chars not allowed in XML 1.0 (C0 controls except TAB/LF/CR, surrogates, U+FFFE/U+FFFF)
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
SHEET_NAME_MAX = 31


def xlsx_text(text: str) -> str:
    """Text for a SpreadsheetML ``<t>`` element: ``_xHHHH_`` literals protected (``_x005F_``), XML-illegal
    characters as ``_xHHHH_`` (Excel decodes both), then ``& < > "`` escaped."""
    text = _ST_XSTRING_ESCAPE.sub(r"_x005F_\1", text)
    text = _XML_ILLEGAL.sub(lambda m: f"_x{ord(m.group()):04X}_", text)
    return _xml_escape(text, {'"': "&quot;"})


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _xlsx_cell(ref: str, value: Any, style: int = 0) -> str:
    s = f' s="{style}"' if style else ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}"{s} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, int) and abs(value) <= _EXCEL_EXACT_INT:
        return f'<c r="{ref}"{s}><v>{value}</v></c>'
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f'<c r="{ref}"{s}><v>{value!r}</v></c>'
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{xlsx_text(str(value))}</t></is></c>'


def _width(values: Iterable[Any]) -> float:
    longest = max((len(_csv_cell(v)) for v in values), default=8)
    return float(min(max(longest + 2, 8), 60))


def _sheet_xml(header: list[str], rows: list[list[Any]], rtl: bool = True) -> str:
    ncols = max(len(header), max((len(r) for r in rows), default=0), 1)
    last = f"{_col_letter(ncols - 1)}{len(rows) + 1}"
    rtl_attr = ' rightToLeft="1"' if rtl else ""
    parts = [_XML_DECL, f'<worksheet xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">',
             f'<dimension ref="A1:{last}"/>',
             f'<sheetViews><sheetView workbookViewId="0"{rtl_attr}>'
             '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
             '<selection pane="bottomLeft"/></sheetView></sheetViews>',
             '<sheetFormatPr defaultRowHeight="15"/><cols>']
    sample = rows[:200]
    for c in range(ncols):
        col_values = [header[c] if c < len(header) else ""] + [r[c] for r in sample if c < len(r)]
        parts.append(f'<col min="{c + 1}" max="{c + 1}" width="{_width(col_values)}" customWidth="1"/>')
    parts.append("</cols><sheetData>")
    parts.append('<row r="1">' + "".join(_xlsx_cell(f"{_col_letter(c)}1", h, 1) for c, h in enumerate(header))
                 + "</row>")
    letters = [_col_letter(c) for c in range(ncols)]
    for i, row in enumerate(rows, start=2):
        parts.append(f'<row r="{i}">' + "".join(_xlsx_cell(f"{letters[c]}{i}", v) for c, v in enumerate(row))
                     + "</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


_STYLES_XML = (
    _XML_DECL + f'<styleSheet xmlns="{_NS_MAIN}">'
    '<fonts count="2"><font><sz val="11"/><name val="Tahoma"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Tahoma"/><family val="2"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '</fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def to_xlsx(tables: Sequence[Table], header: str = "en") -> bytes:
    """A minimal, valid Office Open XML workbook (one sheet per table), standard library only."""
    names = [t.sheet_fa[:SHEET_NAME_MAX] for t in tables]
    content_types = [_XML_DECL, '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                     '<Default Extension="xml" ContentType="application/xml"/>',
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
                     'officedocument.spreadsheetml.sheet.main+xml"/>',
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
                     'officedocument.spreadsheetml.styles+xml"/>']
    content_types += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.'
                      f'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(tables) + 1)]
    content_types.append("</Types>")
    root_rels = (_XML_DECL + f'<Relationships xmlns="{_NS_PKG_REL}"><Relationship Id="rId1" Type="{_NS_REL}/'
                 'officeDocument" Target="xl/workbook.xml"/></Relationships>')
    workbook = [_XML_DECL, f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">',
                '<bookViews><workbookView activeTab="0"/></bookViews><sheets>']
    workbook += [f'<sheet name="{xlsx_text(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, start=1)]
    workbook.append("</sheets></workbook>")
    wb_rels = [_XML_DECL, f'<Relationships xmlns="{_NS_PKG_REL}">']
    wb_rels += [f'<Relationship Id="rId{i}" Type="{_NS_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(tables) + 1)]
    wb_rels.append(f'<Relationship Id="rId{len(tables) + 1}" Type="{_NS_REL}/styles" Target="styles.xml"/>')
    wb_rels.append("</Relationships>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("[Content_Types].xml", "".join(content_types))
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", "".join(workbook))
        zf.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
        zf.writestr("xl/styles.xml", _STYLES_XML)
        for i, t in enumerate(tables, start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(t.header(header), t.rows).encode("utf-8"))
    return buf.getvalue()


# ------------------------------------------------------------------------------------------ entry point
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def export_filename(run: dict[str, Any], table: str, fmt: str) -> str:
    """``AlphaTrader_bt<id>_<symbol>_<mode>_<table>.<csv|xlsx>`` (ASCII only; other characters -> ``_``)."""
    symbol = _SAFE_NAME.sub("_", str(run.get("symbol") or "")).strip("._") or "symbol"
    mode = _SAFE_NAME.sub("_", str(run.get("mode") or "")) or "mode"
    return f"AlphaTrader_bt{int(run['id'])}_{symbol}_{mode}_{table}.{fmt}"


@dataclass(frozen=True)
class ExportFile:
    content: bytes
    media_type: str
    filename: str
    rows: dict[str, int]  # data rows per table


def export_run(repo: BacktestsRepo, run: dict[str, Any], fmt: str, table: str = "trades", header: str = "en",
               exported_at: datetime | None = None) -> ExportFile:
    """Build the CSV (one ``table``) or the workbook (all tables) of a stored run. The caller validates
    ``fmt`` / ``table`` / ``header`` and the run status."""
    t0 = time.perf_counter()
    names = [table] if fmt == "csv" else workbook_tables(run["mode"])
    data = load_export_data(repo, run, names)
    t_load = time.perf_counter()
    tables = [build_table(data, n, exported_at) for n in names]
    if fmt == "csv":
        content = to_csv(tables[0], header)
        out = ExportFile(content, CSV_MEDIA_TYPE, export_filename(run, table, "csv"), {table: len(tables[0].rows)})
    else:
        content = to_xlsx(tables, header)
        out = ExportFile(content, XLSX_MEDIA_TYPE, export_filename(run, "all", "xlsx"),
                         {t.name: len(t.rows) for t in tables})
    if is_dev_mode():
        logger.debug("backtest export run %d (%s, %s) format=%s table=%s header=%s: rows=%s bytes=%d "
                     "load=%.1f ms total=%.1f ms", run["id"], run["mode"], run["status"], fmt,
                     table if fmt == "csv" else "all", header, out.rows, len(content), (t_load - t0) * 1000.0,
                     (time.perf_counter() - t0) * 1000.0)
    return out


__all__ = [
    "CSV_MEDIA_TYPE",
    "CSV_TABLES",
    "EXPORT_FORMATS",
    "HEADER_LANGS",
    "XLSX_MEDIA_TYPE",
    "ExportData",
    "ExportFile",
    "Table",
    "build_table",
    "export_filename",
    "export_run",
    "label_fa",
    "load_export_data",
    "to_csv",
    "to_xlsx",
    "workbook_tables",
    "xlsx_text",
]
