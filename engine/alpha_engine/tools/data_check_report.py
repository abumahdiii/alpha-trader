"""Persian markdown report for the manual raw-data check (phase 1, section 4, the manual-check step).

Usage (from ``engine/``)::

    .venv\\Scripts\\python.exe -m alpha_engine.tools.data_check_report --out <path.md> [--offline]

Reads the CACHE (never fetches bars). If MT5 is connectable (attach mode, read-only) and ``--offline`` is
not given, live symbol specs and the live broker offset are shown too; otherwise the cached specs.
For every symbol x timeframe it lists the 10 newest and 10 oldest bars in BOTH UTC and broker server
time -- the MT5 Data Window shows server time, so the user can compare bar by bar.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import pandas as pd

from ..config import Settings, get_settings
from ..data.cache import OhlcvCache, atomic_write_text
from ..data.gaps import find_gaps
from ..data.schema import Timeframe, to_epoch_seconds
from ..data.symbols import SymbolSpec, validate_symbol_name
from ..data.timezone import LiveOffset, OffsetModel
from ..logging_setup import configure_logging, get_logger
from ..mt5_adapter import DEFAULT_YEARS, Mt5Adapter, Mt5Error

logger = get_logger(__name__)

GAP_LABELS = {"weekend": "آخر هفته", "holiday": "تعطیلی", "session_break": "وقفه روزانه", "missing": "کندل گم‌شده"}
SOURCE_LABELS = {"fit": "برازش تاریخی", "override": "override دستی (MT5_SERVER_UTC_OFFSET)", "live": "تیک زنده"}


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m alpha_engine.tools.data_check_report",
                                     description="Persian data-check report from the OHLCV cache.")
    parser.add_argument("--out", required=True, type=Path, help="output markdown file")
    parser.add_argument("--symbols", type=_csv, default=None)
    parser.add_argument("--timeframes", type=_csv, default=["H1", "H4"])
    parser.add_argument("--rows", type=int, default=10, help="bars shown at each end (default 10)")
    parser.add_argument("--offline", action="store_true", help="do not connect to MT5; cached specs only")
    return parser


def _fmt_price(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"


def _server_times(times: pd.Series, model: OffsetModel | None) -> list[str]:
    if model is None:
        return ["?"] * len(times)
    server = model.utc_to_server(to_epoch_seconds(times))
    return [pd.Timestamp(int(s), unit="s").strftime("%Y.%m.%d %H:%M") for s in server]


def _bars_table(frame: pd.DataFrame, model: OffsetModel | None, digits: int) -> list[str]:
    lines = [
        "| زمان UTC | زمان سرور (Data Window) | Open | High | Low | Close | Tick Vol | Spread |",
        "|---|---|---|---|---|---|---|---|",
    ]
    server = _server_times(frame["time"], model)
    for (_, row), srv in zip(frame.iterrows(), server, strict=True):
        lines.append(
            f"| {row['time'].strftime('%Y-%m-%d %H:%M')}Z | {srv} | {_fmt_price(row['open'], digits)} | "
            f"{_fmt_price(row['high'], digits)} | {_fmt_price(row['low'], digits)} | "
            f"{_fmt_price(row['close'], digits)} | {int(row['tick_volume'])} | {int(row['spread'])} |"
        )
    return lines


def build_report(
    settings: Settings,
    cache: OhlcvCache,
    symbols: Sequence[str],
    timeframes: Sequence[Timeframe],
    *,
    specs: dict[str, tuple[SymbolSpec | None, str]],
    mt5_line: str,
    live: LiveOffset | None,
    rows: int = 10,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    fit = cache.read_offset_fit()
    out: list[str] = [
        "# گزارش چک دستی داده خام (Alpha Trader، فاز ۱)",
        "",
        f"- زمان تولید گزارش: `{now.strftime('%Y-%m-%d %H:%M')}Z`",
        f"- مسیر کش: `{cache.ohlcv_dir}`",
        f"- وضعیت MT5: {mt5_line}",
        "- همه زمان‌های ذخیره‌شده UTC هستند. پنجره **Data Window** در MT5 زمان **سرور بروکر** را نشان می‌دهد؛"
        " برای مقایسه از ستون «زمان سرور» استفاده کنید.",
        "",
        "## ۱. اختلاف زمان سرور بروکر (offset)",
        "",
    ]
    if fit is None:
        out.append("> هنوز برازش offset انجام نشده است (ابتدا `fetch_history` را اجرا کنید).")
    else:
        out += [
            "| مورد | مقدار |",
            "|---|---|",
            f"| مدل مؤثر (برای تبدیل) | `{fit.effective_label}` |",
            f"| منبع | {SOURCE_LABELS.get(fit.source, fit.source)} |",
            f"| مدل استنتاج‌شده از داده | `{fit.inferred_label}` |",
            f"| نسبت تطابق | {fit.matched}/{fit.checks} ({fit.match_ratio:.0%}) روی {fit.weeks} هفته |",
            f"| لنگر برازش | بسته شدن جمعه ۱۷:۰۰ و باز شدن یکشنبه ۱۸:۰۰ نیویورک |",
            f"| offset فعلی مدل | {fit.model_offset_now_hours:+g} ساعت |" if fit.model_offset_now_hours is not None
            else "| offset فعلی مدل | - |",
            f"| offset زنده (زمان برازش) | {fit.live_offset_hours if fit.live_offset_hours is not None else '-'}"
            f" (معتبر: {'بله' if fit.live_trusted else 'خیر'}، {fit.live_reason or '-'}) |",
            f"| سازگاری مدل با offset زنده | {({True: 'بله', False: 'خیر'}).get(fit.consistent_with_live, 'نامشخص')} |",
            f"| override تنظیم‌شده | {fit.override_hours if fit.override_hours is not None else 'ندارد'} |",
            f"| زمان برازش | `{fit.fitted_at_utc}` |",
            "",
            "نامزدهای برتر: " + "، ".join(f"`{c.label}`={c.matched}" for c in fit.top_candidates),
        ]
        if fit.low_confidence:
            out.append("")
            out.append("> **هشدار:** اطمینان برازش پایین است؛ offset را با Data Window مقایسه کنید یا "
                       "`MT5_SERVER_UTC_OFFSET` را تنظیم کنید.")
    if live is not None:
        out.append("")
        out.append(f"- offset زنده هنگام تولید گزارش: {live.hours if live.hours is not None else '-'} ساعت "
                   f"(معتبر: {'بله' if live.trusted else 'خیر'}، {live.reason})")

    out += ["", "## ۲. مشخصات نمادها", "",
            "| نماد | منبع | digits | point | contract size | tick value | tick size | ارزش ۱.۰۰ حرکت برای ۱ لات"
            " | حداقل/گام/حداکثر حجم | ارز سود/پایه |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for symbol in symbols:
        spec, source = specs.get(symbol, (None, "none"))
        if spec is None:
            out.append(f"| {symbol} | {source} | - | - | - | - | - | - | - | - |")
            continue
        out.append(
            f"| {symbol} | {source} | {spec.digits} | {spec.point:g} | {spec.trade_contract_size:g} | "
            f"{spec.trade_tick_value:g} | {spec.trade_tick_size:g} | {spec.value_per_price_unit:g} {spec.currency_profit} | "
            f"{spec.volume_min:g}/{spec.volume_step:g}/{spec.volume_max:g} | {spec.currency_profit}/{spec.currency_base} |"
        )

    section = 3
    for symbol in symbols:
        spec, _ = specs.get(symbol, (None, "none"))
        for tf in timeframes:
            out += ["", f"## {_fa_digits(section)}. {symbol} — {tf.value}", ""]
            section += 1
            cached = cache.read(symbol, tf)
            if cached is None:
                out.append("> داده‌ای در کش نیست.")
                continue
            frame, meta = cached
            digits = spec.digits if spec is not None else 5
            model = OffsetModel.parse(meta.offset_model) if meta.offset_model else None
            target = meta.requested_start_utc or "-"
            short = meta.extra.get("history_short")
            out += [
                "| مورد | مقدار |",
                "|---|---|",
                f"| تعداد کندل | {meta.rows} |",
                f"| اولین / آخرین کندل (UTC) | `{meta.first_bar_utc}` / `{meta.last_bar_utc}` |",
                f"| اولین کندل موجود نزد بروکر | `{meta.first_available_utc}` |",
                f"| شروع هدف ({DEFAULT_YEARS:g} سال) | `{target}` |",
                f"| وضعیت عمق تاریخچه | {'کوتاه‌تر از هدف (طبق تصمیم کاربر پذیرفته و گزارش می‌شود)' if short else 'کامل'} |",
                f"| مدل offset استفاده‌شده | `{meta.offset_model}` |",
                f"| منبع داده | {meta.source} (دریافت: `{meta.fetched_at_utc}`) |",
            ]
            report = find_gaps(frame, tf)
            out += ["", "**خلاصه گپ‌ها (هیچ گپی پر نشده است):**", "", "| نوع | تعداد |", "|---|---|"]
            for kind, count in report.counts.items():
                out.append(f"| {GAP_LABELS[kind]} (`{kind}`) | {count} |")
            out.append(f"| مجموع کندل‌های گم‌شده | {report.missing_bars_total} |")
            if report.session_break_slots:
                out.append(f"| ساعت‌های وقفه روزانه (UTC) | {', '.join(report.session_break_slots)} |")
            missing = [g for g in report.gaps if g.kind == "missing"]
            if missing:
                out += ["", f"آخرین گپ‌های «کندل گم‌شده» (حداکثر ۱۰ مورد از {len(missing)}):", ""]
                out += [f"- `{g.start}` تا `{g.before}` ({g.missing_bars} کندل)" for g in missing[-10:]]
            out += ["", f"**{_fa_digits(rows)} کندل اخیر:**", ""]
            out += _bars_table(frame.tail(rows).iloc[::-1].reset_index(drop=True), model, digits)
            out += ["", f"**{_fa_digits(rows)} کندل قدیمی:**", ""]
            out += _bars_table(frame.head(rows).reset_index(drop=True), model, digits)
    out.append("")
    return "\n".join(out)


def _fa_digits(value: int) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def run(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    adapter: Mt5Adapter | None = None,
    out: TextIO | None = None,
    now: datetime | None = None,
) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    configure_logging(settings)
    try:
        symbols = [validate_symbol_name(s) for s in (args.symbols or settings.engine_symbols)]
        timeframes = [Timeframe.parse(t) for t in args.timeframes]
    except ValueError as exc:
        print(f"invalid argument: {exc}", file=out)
        return 2
    cache = OhlcvCache(settings.data_dir)

    specs: dict[str, tuple[SymbolSpec | None, str]] = {}
    live: LiveOffset | None = None
    mt5_line = "آفلاین (--offline)"
    connected = False
    if not args.offline:
        adapter = adapter or Mt5Adapter(settings)
        status = adapter.connect()
        connected = status.state == "connected"
        mt5_line = (f"`{status.state}` — سرور `{status.server}`، حساب `{status.login_masked}`، "
                    f"نوع حساب `{status.trade_mode}`")
    try:
        for symbol in symbols:
            if connected and adapter is not None:
                try:
                    spec = adapter.symbol_spec(symbol)
                    cache.write_spec(spec, "mt5")
                    specs[symbol] = (spec, "MT5 زنده")
                    continue
                except Mt5Error as exc:
                    logger.warning("live spec for %s failed: %s", symbol, exc)
            cached = cache.read_spec(symbol)
            specs[symbol] = (cached[0], "کش") if cached else (None, "ندارد")
        if connected and adapter is not None:
            try:
                live = adapter.live_offset(symbols[0] if "XAUUSD.x" not in symbols else "XAUUSD.x")
            except Mt5Error:
                live = None
        text = build_report(settings, cache, symbols, timeframes, specs=specs, mt5_line=mt5_line, live=live,
                            rows=args.rows, now=now)
    finally:
        if adapter is not None and not args.offline:
            adapter.shutdown()
    atomic_write_text(args.out, text)
    print(f"report written: {args.out}", file=out)
    return 0


def main() -> int:  # pragma: no cover
    return run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
