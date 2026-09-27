"""Persian report for the manual MT5 comparison of the StdDev channel (phase 2, section 2, the paused check).

Usage (from ``engine/``)::

    .venv\\Scripts\\python.exe -m alpha_engine.tools.channel_check_report --symbol XAUUSD.x --out <path.md>
        [--end <UTC ISO>] [--n 100] [--k 2] [--h1-bars 4] [--data-dir <dir>]

Reads the OHLCV CACHE only (never MetaTrader 5, never writes to the cache). Steps:

1. H4 window: the last ``n`` cached (= closed) H4 bars whose last bar is the latest bar with open time
   ``<= --end`` (default: the last cached H4 bar).
2. Channel exactly as the engine computes it: :func:`rolling_regression_channel` on the H4 closes up to
   that bar, value at that bar, for ``ddof=0`` (population sigma, engine default) AND ``ddof=1``. The three
   lines at bar ``i`` of the window (``i = 0 .. n-1``)::

       mid_i   = mid_last + slope * (i - (n - 1))
       upper_i = mid_i + k * sigma,   lower_i = mid_i - k * sigma

   shown at the first, middle (``i = (n-1)//2``) and last bar, with the bar times in UTC and in broker
   server time (MT5 chart time; offset model from the cache metadata).
3. Flatness at the last bar: ``|slope| * n`` against ATR_H4(14) * flat_mult, Wilder (engine) and SMA (what
   MT5's built-in ATR shows).
4. H1 projection (``bar_count`` mode, engine default; ``calendar`` shown for comparison): the first
   ``--h1-bars`` cached H1 bars that the engine decides with THIS channel, i.e. whose ``j*`` (last H4 bar
   closed at the H1 bar's close) is the window's last H4 bar -- normally 4 bars, opening from 1h before
   that H4 bar closes, with ``bars_ahead`` 0.75 / 1.0 / 1.25 / 1.5. Values come from
   ``project_channel_to_h1`` on the full cached history.

Worked example (``tests/test_channel_check_report.py``): closes ``[1, 3, 2, 5, 4]``, ``n = 5``, ``k = 2``
-> slope 0.8, mid at the last bar 4.6, first bar 4.6 + 0.8 * (0 - 4) = 1.4, middle (i = 2) 3.0;
sigma 0.848528 (ddof 0) / 0.948683 (ddof 1) -> upper at the last bar 6.297056 / 6.497367.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import numpy as np
import pandas as pd

from ..config import Settings, get_settings
from ..data.cache import CacheMeta, OhlcvCache, atomic_write_text
from ..data.gaps import find_gaps
from ..data.schema import Timeframe, to_epoch_seconds
from ..data.symbols import validate_symbol_name
from ..data.timezone import OffsetModel
from ..indicators.atr import sma_atr, wilder_atr
from ..indicators.mtf import project_channel_to_h1
from ..indicators.regression_channel import is_flat, rolling_regression_channel
from ..logging_setup import configure_logging, get_logger, is_dev_mode

logger = get_logger(__name__)

H4 = pd.Timedelta(hours=4)
H1 = pd.Timedelta(hours=1)
DDOFS = (0, 1)
POINT_LABELS = {"first": "اولین کندل", "middle": "کندل میانی", "last": "آخرین کندل"}


class ChannelCheckError(RuntimeError):
    """Not enough cached data for the requested window (message is Persian, shown to the user)."""


@dataclass(frozen=True)
class LinePoint:
    label: str  # first / middle / last
    index: int  # 0 .. n-1 inside the window
    time_utc: pd.Timestamp
    close: float
    mid: float
    upper: dict[int, float]  # ddof -> value
    lower: dict[int, float]


@dataclass(frozen=True)
class H1Row:
    time_utc: pd.Timestamp
    h4_open_utc: pd.Timestamp | None
    uses_window_bar: bool  # j* is the window's last H4 bar
    h1_in_h4_bar: int  # H1 bars cached inside [open_j*, open_j* + 4h) (4 = complete H4 bar)
    bars_ahead: float
    bars_ahead_calendar: float
    mid: float
    upper: dict[int, float]
    lower: dict[int, float]


@dataclass
class ChannelCheck:
    symbol: str
    n: int
    k: float
    flat_mult: float
    atr_period: int
    window: pd.DataFrame  # the n H4 bars (time, open, high, low, close, ...)
    slope: float
    mid_last: float
    sigma: dict[int, float]
    points: list[LinePoint]
    atr_wilder: float
    atr_sma: float
    missing_gaps_in_window: int
    h1_rows: list[H1Row] = field(default_factory=list)
    h1_available: bool = True
    h1_in_last_h4: int = 0  # H1 bars cached inside the window's last H4 bar (4 = complete)

    @property
    def change_abs(self) -> float:
        return abs(self.slope) * self.n

    def flat(self, atr: float) -> bool:
        return bool(is_flat(self.slope, self.n, atr, self.flat_mult))


def _utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def compute_check(
    h4: pd.DataFrame,
    h1: pd.DataFrame | None,
    *,
    symbol: str,
    n: int = 100,
    k: float = 2.0,
    end: pd.Timestamp | None = None,
    atr_period: int = 14,
    flat_mult: float = 1.0,
    h1_bars: int = 4,
) -> ChannelCheck:
    """All numbers of the report (pure; ``h4``/``h1`` are canonical cache frames, UTC ``time`` column)."""
    h4 = h4.reset_index(drop=True)
    times = pd.DatetimeIndex(h4["time"])
    if end is None:
        end_idx = len(h4) - 1
    else:
        end_idx = int(np.searchsorted(times, end, side="right")) - 1
    if end_idx < 0:
        raise ChannelCheckError("هیچ کندل H4 در کش تا زمان پایان داده‌شده وجود ندارد.")
    if end_idx + 1 < n:
        raise ChannelCheckError(
            f"برای کانال {n} کندلی فقط {end_idx + 1} کندل H4 تا زمان پایان در کش هست؛ ابتدا تاریخچه را دریافت کنید."
        )
    upto = h4.iloc[: end_idx + 1]
    window = upto.iloc[end_idx + 1 - n:].reset_index(drop=True)

    channels = {d: rolling_regression_channel(upto["close"], n, k, d) for d in DDOFS}
    last = {d: channels[d].iloc[end_idx] for d in DDOFS}
    slope = float(last[0]["slope"])
    mid_last = float(last[0]["mid"])
    assert slope == float(last[1]["slope"]) and mid_last == float(last[1]["mid"])  # the line ignores ddof
    sigma = {d: float(last[d]["sigma"]) for d in DDOFS}

    points: list[LinePoint] = []
    for label, i in (("first", 0), ("middle", (n - 1) // 2), ("last", n - 1)):
        mid = mid_last + slope * (i - (n - 1))
        points.append(LinePoint(
            label=label, index=i, time_utc=window["time"].iloc[i], close=float(window["close"].iloc[i]), mid=mid,
            upper={d: mid + k * sigma[d] for d in DDOFS}, lower={d: mid - k * sigma[d] for d in DDOFS},
        ))

    atr_w = float(wilder_atr(upto["high"], upto["low"], upto["close"], atr_period).iloc[-1])
    atr_s = float(sma_atr(upto["high"], upto["low"], upto["close"], atr_period).iloc[-1])
    gaps = find_gaps(window, Timeframe.H4)
    missing = sum(1 for g in gaps.gaps if g.kind == "missing")

    check = ChannelCheck(symbol=symbol, n=n, k=k, flat_mult=flat_mult, atr_period=atr_period, window=window,
                         slope=slope, mid_last=mid_last, sigma=sigma, points=points, atr_wilder=atr_w,
                         atr_sma=atr_s, missing_gaps_in_window=missing)
    if h1 is None or not len(h1):
        check.h1_available = False
    else:
        check.h1_rows = _h1_projection(h4, h1, end_idx, n, k, atr_period, h1_bars)
        check.h1_in_last_h4 = _h1_inside(h1, h4["time"].iloc[end_idx])
    if is_dev_mode():
        logger.debug(
            "channel check %s: n=%d k=%g window %s..%s slope=%.8f mid_last=%.6f sigma0=%.6f sigma1=%.6f "
            "atr_wilder=%.6f atr_sma=%.6f missing_gaps=%d h1_rows=%d",
            symbol, n, k, window["time"].iloc[0], window["time"].iloc[-1], slope, mid_last, sigma[0], sigma[1],
            atr_w, atr_s, missing, len(check.h1_rows),
        )
    return check


def _h1_inside(h1: pd.DataFrame, h4_open: pd.Timestamp) -> int:
    """Number of cached H1 bars inside the H4 bar opening at ``h4_open`` (4 = complete bar)."""
    times = h1["time"]
    return int(((times >= h4_open) & (times < h4_open + H4)).sum())


def _h1_projection(h4: pd.DataFrame, h1: pd.DataFrame, end_idx: int, n: int, k: float, atr_period: int,
                   count: int) -> list[H1Row]:
    """Engine projection (``project_channel_to_h1``) at the first ``count`` H1 bars whose ``j*`` is ``end_idx``."""
    h1 = h1.reset_index(drop=True)
    if count <= 0:
        return []
    atr4 = wilder_atr(h4["high"], h4["low"], h4["close"], atr_period).to_numpy()
    projected = {}
    for d in DDOFS:
        channel = rolling_regression_channel(h4["close"], n, k, d)
        projected[d] = project_channel_to_h1(channel, h4["time"], h1, n, "bar_count", h4_atr=atr4)
    calendar = project_channel_to_h1(rolling_regression_channel(h4["close"], n, k, 0), h4["time"], h1, n,
                                     "calendar")
    picked = np.flatnonzero(projected[0]["h4_index"].to_numpy() == end_idx)[:count]
    rows: list[H1Row] = []
    for pos in picked:
        j = int(projected[0]["h4_index"].iloc[pos])
        j_open = h4["time"].iloc[j]
        inside = _h1_inside(h1, j_open)
        rows.append(H1Row(
            time_utc=h1["time"].iloc[pos], h4_open_utc=j_open, uses_window_bar=(j == end_idx), h1_in_h4_bar=inside,
            bars_ahead=float(projected[0]["bars_ahead"].iloc[pos]),
            bars_ahead_calendar=float(calendar["bars_ahead"].iloc[pos]),
            mid=float(projected[0]["mid"].iloc[pos]),
            upper={d: float(projected[d]["upper"].iloc[pos]) for d in DDOFS},
            lower={d: float(projected[d]["lower"].iloc[pos]) for d in DDOFS},
        ))
    return rows


# ---------------------------------------------------------------------------------------------- render

def _fa_digits(value: int | str) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _server(ts: pd.Timestamp | None, model: OffsetModel | None) -> str:
    if ts is None or model is None:
        return "?"
    epoch = int(model.utc_to_server(to_epoch_seconds(pd.DatetimeIndex([ts])))[0])
    return pd.Timestamp(epoch, unit="s").strftime("%Y.%m.%d %H:%M")


def _utc_text(ts: pd.Timestamp | None) -> str:
    return "-" if ts is None else pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M") + "Z"


def render_report(
    check: ChannelCheck,
    *,
    meta: CacheMeta,
    digits: int | None,
    cache_dir: Path,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    decimals = (digits if digits is not None else 2) + 3
    point = 10.0 ** -(digits if digits is not None else 2)

    def f(value: float) -> str:
        return "-" if value is None or not math.isfinite(value) else f"{value:.{decimals}f}"

    def price(value: float) -> str:  # as MT5 shows it (symbol digits)
        return f"{value:.{decimals - 3}f}"

    model = OffsetModel.parse(meta.offset_model) if meta.offset_model else None
    first, middle, last = check.points
    n_fa = _fa_digits(check.n)
    k_text = f"{check.k:g}"
    out: list[str] = [
        f"# گزارش مقایسه کانال انحراف معیار با MT5 — {check.symbol} (Alpha Trader، فاز ۲)",
        "",
        f"- زمان تولید گزارش: `{now.strftime('%Y-%m-%d %H:%M')}Z`",
        f"- منبع: فقط کش محلی (`{cache_dir}`)، بدون اتصال به MT5. منبع داده کش: `{meta.source}`، "
        f"دریافت: `{meta.fetched_at_utc}`",
        f"- مدل اختلاف زمان سرور (از متادیتای کش): `{meta.offset_model or 'نامشخص'}`"
        + ("" if model is not None else " — **زمان سرور قابل محاسبه نیست؛ فقط ستون UTC معتبر است.**"),
        f"- پارامترها: N = {n_fa} کندل H4، k = {k_text}، ATR({_fa_digits(check.atr_period)})، "
        f"ضریب آستانه افقی = {check.flat_mult:g}",
        "- چارت MT5 زمان **سرور بروکر** را نشان می‌دهد؛ برای رسم ابزار از ستون «زمان سرور» استفاده کنید.",
        "",
        f"## ۱. بازه کانال: {n_fa} کندل H4 بسته‌شده",
        "",
        "| نقطه | شماره کندل در بازه | زمان UTC | زمان سرور (چارت MT5) | Close |",
        "|---|---|---|---|---|",
    ]
    for p in check.points:
        out.append(f"| {POINT_LABELS[p.label]} | {_fa_digits(p.index + 1)} از {n_fa} | {_utc_text(p.time_utc)} | "
                   f"{_server(p.time_utc, model)} | {price(p.close)} |")
    if check.missing_gaps_in_window:
        out += ["", f"> **هشدار:** داخل این بازه {_fa_digits(check.missing_gaps_in_window)} گپ از نوع «کندل گم‌شده» "
                "در کش هست. اگر MT5 آن کندل‌ها را داشته باشد، ابزار MT5 روی تعداد متفاوتی کندل رسم می‌شود و "
                "مقادیر کمی فرق می‌کنند."]

    out += [
        "",
        "## ۲. مقادیر سه خط در ۳ نقطه (σ جمعیت ddof=0 و σ نمونه ddof=1، کنار هم)",
        "",
        "خط میانی (رگرسیون) به ddof بستگی ندارد؛ فقط فاصله خط بالا و پایین از آن فرق می‌کند.",
        "",
        "| نقطه | زمان سرور | خط میانی | خط بالا (ddof=0) | خط پایین (ddof=0) | خط بالا (ddof=1) | خط پایین (ddof=1) |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in check.points:
        out.append(f"| {POINT_LABELS[p.label]} | {_server(p.time_utc, model)} | {f(p.mid)} | {f(p.upper[0])} | "
                   f"{f(p.lower[0])} | {f(p.upper[1])} | {f(p.lower[1])} |")
    width_diff = check.k * (check.sigma[1] - check.sigma[0])
    out += [
        "",
        "| مورد | مقدار |",
        "|---|---|",
        f"| شیب (قیمت در هر کندل H4) | {check.slope:.{decimals + 2}f} |",
        f"| جهت کانال | {'صعودی' if check.slope > 0 else 'نزولی' if check.slope < 0 else 'بدون شیب'} |",
        f"| تغییر خط میانی در کل کانال (قدر مطلق شیب × N) | {f(check.change_abs)} |",
        f"| σ جمعیت (ddof=0، پیش‌فرض engine) | {f(check.sigma[0])} |",
        f"| σ نمونه (ddof=1) | {f(check.sigma[1])} |",
        f"| فاصله خط بالا از میانی: k×σ (ddof=0 / ddof=1) | {f(check.k * check.sigma[0])} / {f(check.k * check.sigma[1])} |",
        f"| اختلاف دو روش در فاصله هر خط بیرونی | {f(width_diff)} (≈ {width_diff / point:.1f} پوینت) |",
    ]

    flat_w, flat_s = check.flat(check.atr_wilder), check.flat(check.atr_sma)
    out += [
        "",
        "## ۳. افقی بودن کانال در آخرین کندل",
        "",
        f"قاعده: کانال افقی است اگر |شیب| × N کمتر از ATR(H4) × {check.flat_mult:g} باشد. "
        f"|شیب| × N = **{f(check.change_abs)}**.",
        "",
        "| روش ATR | مقدار ATR | آستانه | نتیجه |",
        "|---|---|---|---|",
        f"| Wilder (روش engine) | {f(check.atr_wilder)} | {f(check.atr_wilder * check.flat_mult)} | "
        f"{'افقی (هر دو جهت مجاز)' if flat_w else 'غیر افقی (فقط هم‌جهت با شیب)'} |",
        f"| SMA (اندیکاتور ATR داخلی MT5) | {f(check.atr_sma)} | {f(check.atr_sma * check.flat_mult)} | "
        f"{'افقی' if flat_s else 'غیر افقی'} |",
        "",
        "> اندیکاتور **Average True Range** داخلی MT5 میانگین ساده (SMA) است، نه Wilder؛ پس عدد آن با ستون "
        "Wilder برابر نیست و با ستون SMA مقایسه می‌شود. engine طبق تعریف سیستم از Wilder استفاده می‌کند.",
    ]

    out += [
        "",
        "## ۴. رسم ابزار Standard Deviation Channel در MT5",
        "",
        f"1. چارت **{check.symbol}** را باز کنید و تایم‌فریم را روی **H4** بگذارید.",
        "2. از منو: **Insert ← Objects ← Channels ← Standard Deviation Channel**.",
        "3. روی کندل اول بازه کلیک کنید و تا کندل آخر بکشید (فعلاً تقریبی؛ در گام بعد دقیق می‌شود).",
        "4. روی خط کانال دوبار کلیک کنید تا انتخاب شود، سپس کلیک راست ← **Properties** (یا Ctrl+B و انتخاب شیء).",
        "5. در زبانه **Parameters**:",
        f"   - زمان نقطه اول (Time 1) = `{_server(first.time_utc, model)}` (زمان سرور اولین کندل)",
        f"   - زمان نقطه دوم (Time 2) = `{_server(last.time_utc, model)}` (زمان سرور آخرین کندل)",
        f"   - **Deviation** = `{k_text}`",
        "6. گزینه **Ray right** (امتداد به راست) را **خاموش** کنید (بسته به نسخه MT5 در زبانه Parameters یا "
        "Common است). Ray left هم خاموش باشد.",
        f"7. بررسی کنید که ابزار دقیقاً {n_fa} کندل را پوشش می‌دهد (از کندل `{_server(first.time_utc, model)}` "
        f"تا `{_server(last.time_utc, model)}`، هر دو شامل).",
        "",
        "### خواندن مقادیر",
        "",
        "- **خط میانی در اولین و آخرین کندل:** در همان زبانه Parameters، قیمت دو نقطه لنگر (Price 1 و Price 2) "
        "همان مقدار خط میانی در این دو کندل است. آن‌ها را با ستون «خط میانی» جدول بخش ۲ مقایسه کنید.",
        "- **خط بالا و پایین و نقطه میانی:** crosshair را فعال کنید (کلید وسط ماوس یا Ctrl+F)، روی کندل مورد نظر "
        "روی هر خط بایستید و قیمت را از محور قیمت بخوانید. یا یک خط افقی (Horizontal Line) را روی خط کانال بگذارید "
        "و قیمت آن را در Properties بخوانید.",
        f"- MT5 قیمت‌ها را با {_fa_digits(digits if digits is not None else 2)} رقم اعشار نشان می‌دهد؛ اختلاف تا "
        "حدود ۱ پوینت ناشی از گرد کردن نمایش است.",
        "",
        "### تصمیم روش σ",
        "",
        f"- اگر فاصله خط بالا تا خط میانی در MT5 برابر **{f(check.k * check.sigma[0])}** بود ← MT5 از σ جمعیت "
        "(ddof=0) استفاده می‌کند و پیش‌فرض engine درست است.",
        f"- اگر برابر **{f(check.k * check.sigma[1])}** بود ← MT5 از σ نمونه (ddof=1) استفاده می‌کند؛ پارامتر "
        "«روش محاسبه انحراف معیار» باید ۱ شود.",
        f"- اختلاف این دو حدود {width_diff / point:.1f} پوینت است، پس با crosshair قابل تشخیص است.",
        "- اگر خط میانی هم متفاوت بود، اول زمان‌های لنگر و تعداد کندل‌ها را بررسی کنید (بخش ۱).",
    ]

    out += ["", "## ۵. امتداد کانال روی H1 (حالت bar_count، پیش‌فرض engine)", ""]
    if not check.h1_available:
        out.append("> داده H1 این نماد در کش نیست.")
    elif not check.h1_rows:
        out.append(f"> در کش هیچ کندل H1 نیست که تصمیمش بعد از بسته شدن آخرین کندل H4 بازه "
                   f"(`{_utc_text(last.time_utc + H4)}`) و با همین کانال گرفته شود. برای این بخش `--end` را روی "
                   "کندل H4 قدیمی‌تری بگذارید.")
    else:
        out += [
            f"کندل‌های H1 که تصمیمشان (در close کندل) بعد از بسته شدن آخرین کندل H4 بازه (`{_utc_text(last.time_utc + H4)}`) "
            "گرفته می‌شود و engine برایشان از **همین کانال** استفاده می‌کند (j* = آخرین کندل بازه). مقدار خط در کندل H1 "
            "با زمان open برابر T:",
            "`خط = مقدار خط در آخرین کندل بازه + شیب × bars_ahead`، و در حالت bar_count: `bars_ahead = m / 4` که m "
            "تعداد کندل‌های واقعی H1 از open آخرین کندل H4 بازه تا قبل از T است (در داده عادی 0.75، 1، 1.25 و 1.5).",
            "",
            "| زمان UTC | زمان سرور | کندل H4 مرجع (j*) UTC | H1های داخل j* | bars_ahead (bar_count) | bars_ahead (calendar) "
            "| خط میانی | بالا (ddof=0) | پایین (ddof=0) | بالا (ddof=1) | پایین (ddof=1) |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in check.h1_rows:
            mark = "" if r.uses_window_bar else " (کندل بعدی)"
            out.append(
                f"| {_utc_text(r.time_utc)} | {_server(r.time_utc, model)} | {_utc_text(r.h4_open_utc)}{mark} | "
                f"{_fa_digits(r.h1_in_h4_bar)} از ۴ | {r.bars_ahead:g} | {r.bars_ahead_calendar:g} | {f(r.mid)} | "
                f"{f(r.upper[0])} | {f(r.lower[0])} | {f(r.upper[1])} | {f(r.lower[1])} |"
            )
        out += [
            "",
            "مقایسه در MT5: ابزار بخش ۴ را کپی کنید، گزینه **Ray right** را روشن کنید، به چارت **H1** بروید و مقدار هر خط را "
            "روی این کندل‌ها با crosshair بخوانید.",
        ]
        if len(check.h1_rows) < 4:
            out += ["", f"> فقط {_fa_digits(len(check.h1_rows))} کندل H1 با این کانال در کش هست (پایان داده یا "
                    "آخر هفته)."]
    if check.h1_available and check.h1_in_last_h4 < 4:
        out += ["", f"> **هشدار کندل H4 ناقص:** آخرین کندل H4 بازه فقط {_fa_digits(check.h1_in_last_h4)} کندل H1 "
                "دارد. روی چارت H4 هم یک کندل کامل حساب می‌شود، پس مقایسه بخش ۲ تغییری نمی‌کند؛ ولی در امتداد روی H1 "
                "حالت bar_count با محور H4 چارت فرق می‌کند (پایین را ببینید)."]
    out += [
        "",
        "> **نکته کندل H4 ناقص (از لاگ فاز ۲):** اگر کندل H4 مرجع ناقص باشد (مثلاً H4 ساعت ۲۰ جمعه که فقط یک کندل H1 "
        "دارد)، حالت bar_count برای کندل H1 ساعت ۰۰ دوشنبه مقدار bars_ahead = 0.25 می‌دهد، در حالی که روی محور H4 "
        "چارت این فاصله ۱٫۰ کندل است. اگر MT5 روی چنین کندلی با ستون bar_count نخواند، شاید حالت «محور H4» لازم "
        "شود؛ ستون calendar هم برای مقایسه آمده است. تصمیم با همین مقایسه گرفته می‌شود.",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m alpha_engine.tools.channel_check_report",
                                     description="Persian StdDev-channel check report (cache only, no MT5).")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--out", required=True, type=Path, help="output markdown file")
    parser.add_argument("--end", default=None, help="UTC ISO time; last H4 bar = latest open <= end "
                                                     "(default: last cached H4 bar). Naive = UTC.")
    parser.add_argument("--n", type=int, default=100, help="channel length in H4 bars (default 100)")
    parser.add_argument("--k", type=float, default=2.0, help="deviation in sigmas (default 2)")
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--flat-mult", type=float, default=1.0)
    parser.add_argument("--h1-bars", type=int, default=4, help="H1 bars in the projection section (default 4)")
    parser.add_argument("--data-dir", type=Path, default=None, help="data dir (default: ALPHA_TRADER_DATA_DIR)")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    out: TextIO | None = None,
    now: datetime | None = None,
) -> int:
    """0 = report written, 1 = not enough cached data, 2 = invalid arguments."""
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    configure_logging(settings)
    try:
        symbol = validate_symbol_name(args.symbol)
        end = _utc(args.end) if args.end else None
        if args.n < 3 or not math.isfinite(args.k) or args.k <= 0 or args.h1_bars < 0 or args.atr_period < 1:
            raise ValueError("n >= 3, k > 0, h1-bars >= 0 and atr-period >= 1 are required")
    except ValueError as exc:
        print(f"invalid argument: {exc}", file=out)
        return 2
    data_dir = args.data_dir or settings.data_dir
    cache = OhlcvCache(data_dir)
    cached_h4 = cache.read(symbol, Timeframe.H4)
    if cached_h4 is None:
        print(f"no cached H4 data for {symbol} under {cache.ohlcv_dir}", file=out)
        return 1
    h4, meta = cached_h4
    cached_h1 = cache.read(symbol, Timeframe.H1)
    spec = cache.read_spec(symbol)
    try:
        check = compute_check(h4, cached_h1[0] if cached_h1 else None, symbol=symbol, n=args.n, k=args.k, end=end,
                              atr_period=args.atr_period, flat_mult=args.flat_mult, h1_bars=args.h1_bars)
    except ChannelCheckError as exc:
        print(f"not enough data: {exc}", file=out)
        return 1
    text = render_report(check, meta=meta, digits=spec[0].digits if spec else None, cache_dir=cache.ohlcv_dir,
                         now=now)
    atomic_write_text(args.out, text)
    if is_dev_mode():
        logger.debug("channel check report for %s written to %s (%d chars)", symbol, args.out, len(text))
    print(f"report written: {args.out}", file=out)
    return 0


def main() -> int:  # pragma: no cover
    return run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
