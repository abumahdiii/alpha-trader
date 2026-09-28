"""The downloadable strategy-plugin template (``GET /plugins/template``).

Kept as a string constant (:data:`TEMPLATE_SOURCE`) because the packaged engine (PyInstaller) ships no ``.py``
sources. The text is written for humans AND for LLM agents that complete it: contract, prohibitions, allowed
imports, a complete runnable two-moving-average crossover with a single-pass ``scan``, a ``param_schema``
example and the self-test command. The tests prove it passes the static validator, the sandboxed dynamic
validation and ``tests/test_no_order_calls.py`` (it never spells an order-API name or the MT5 package name).
"""

from __future__ import annotations

TEMPLATE_FILENAME = "alpha_strategy_template.py"

TEMPLATE_SOURCE = '''"""
Alpha Trader -- قالب سیستم معاملاتی (پلاگین پایتون) / strategy plugin template
==============================================================================

فارسی (خلاصه)
-------------
این فایل یک «سیستم معاملاتی» کامل و قابل اجراست: کراس دو میانگین متحرک. آن را کپی کنید، نام و منطق را عوض کنید
و در صفحه «سیستم‌ها» بارگذاری کنید. engine فایل را فقط در یک فرآیند جدا و محدود اجرا می‌کند و قبل از پذیرش،
آن را روی داده مصنوعی آزمایش می‌کند (قطعی بودن، نبود نگاه به آینده، برابری scan با evaluate، سقف زمان و حافظه).
برنامه فقط «پیشنهاد» می‌دهد و هیچ سفارشی ارسال نمی‌کند؛ پلاگین هم هیچ راهی به متاتریدر ندارد.

قواعد اصلی:
1. دقیقا یک کلاس مشتق از Strategy با name (حروف کوچک انگلیسی، رقم و _)، version (عدد صحیح) و title_fa (فارسی).
2. هر تغییر در منطق = version جدید. بارگذاری همان name و version با محتوای متفاوت رد می‌شود.
3. تصمیم فقط روی کندل بسته‌شده H1 و فقط با داده ctx. ورود در open کندل بعدی است و حد سود را engine حساب می‌کند.
4. ممنوع: متاتریدر، هر API سفارش یا پوزیشن، شبکه، فایل، سیستم‌عامل، ساعت سیستم، عدد تصادفی بدون seed و نگاه
   به آینده (مثل shift منفی یا ایندکس بعد از کندل جاری).
5. فقط این importها مجازند: numpy، pandas، math، statistics، dataclasses، typing و helperهای engine (پایین).

English (full contract -- written for humans and LLM agents)
------------------------------------------------------------
WHAT THIS FILE IS
    A complete, runnable Alpha Trader trading system: a two-moving-average crossover on H1 with an optional
    H4 candle-direction filter. Copy it, rename it, change the logic, then upload it on the "Systems" page.
    The engine runs uploaded code ONLY inside an isolated worker process (no network, no files, no MetaTrader 5,
    capped time and memory). The app only SUGGESTS positions; it never places orders.

THE CLASS (exactly one Strategy subclass per file)
    name        -- literal string, slug: lowercase letters, digits, "_", starts with a letter, <= 64 chars.
                   Must not be a built-in system name (e.g. the built-in channel system).
    version     -- literal int >= 1. Bump it whenever the decision logic changes. Uploading the same
                   name + version with different content is rejected; the old versions stay stored so old
                   backtest results remain reproducible.
    title_fa    -- literal Persian title shown in the UI.
    param_schema -- ParamSchema([ParamSpec(...), ...]); the UI builds the parameter form from it.
                   ParamSpec fields: name (slug), type ("int" | "float" | "bool" | "choice"), default (already
                   of that type), min, max (numbers only), choices (for "choice"), step (UI hint), label_fa,
                   description_fa. Validation is schema-only: do not override validate_params / clean_params.
    evaluate(self, ctx) -> SignalCandidate | None     (required)
    scan(self, h1, h4, params, account, *, symbol) -> list[SignalCandidate]   (strongly recommended)
    first_valid_index(self, h1, h4, params) -> int | None   (optional: first H1 index that can decide)
    history_bars / warmup_margin_h4_bars(params)             (optional; see the engine README)

THE INPUT: ctx (StrategyContext), a decision on the CLOSED H1 bar t = the last row of ctx.h1
    ctx.symbol          "XAUUSD.x" or "BRNUSD.x" (fixed timeframes H1 + H4)
    ctx.h1, ctx.h4      pandas DataFrames, one row per CLOSED bar, oldest first. Columns: time (bar OPEN
                        time, tz-aware UTC), open, high, low, close, and when available tick_volume, spread.
                        ctx.h4 only holds the H4 bars already closed when bar t closes.
    ctx.params          validated parameter dict (keys = param_schema names, defaults filled in)
    ctx.account         balance, risk_pct, leverage, rr (the user's R:R; use it as the candidate's rr)
    ctx.has_open_trade  True -> return None (max one open trade per symbol)

THE OUTPUT: None (no setup) or one SignalCandidate
    strategy_name=self.name, strategy_version=self.version, params_hash=params_hash(clean params),
    symbol=ctx.symbol, direction "buy" | "sell",
    setup  = your slug (lowercase, e.g. "ma_cross"), setup_title_fa = its Persian title, line = None,
    confirmation_bar_open_utc = open time of bar t, decision_time_utc = that + 1 hour (close of bar t),
    reference_price = close of bar t (indicative only), stop_loss (below reference for a buy, above for a
    sell; known at decision time), rr = ctx.account.rr, pattern (lowercase slug), reason_fa (Persian
    explanation), extra = flat dict of numbers / bools / short strings (the indicator values you used).
    You never set an entry price: the entry is the OPEN of the next H1 bar, and the engine computes the real
    take-profit at fill time: tp = entry +/- rr * |entry - stop_loss| (SignalCandidate.resolve_levels).

scan() -- WHY YOU SHOULD WRITE IT
    The chart and the backtester call scan() once on the FULL history (about 5 years of H1 bars) and use one
    candidate per bar. The inherited default calls evaluate() on every prefix: correct but O(n^2) and slow.
    Write a single vectorised pass that returns EXACTLY the candidates evaluate() would return bar by bar
    (same fields, same numbers). The upload check verifies this ("prefix equivalence") and also that the
    candidates before a cut-off do not change when the bars after it change ("future mutation").
    Only use causal operations: rolling windows, cumulative sums, positive shift(k). Never shift(-k), centred
    windows, full-sample normalisation, or any index after the current bar.

PROHIBITED (the upload is rejected, and the sandbox blocks it anyway)
    * MetaTrader 5 in any form, and any order / position / deal / history API;
    * network, sockets, files (read or write), the operating system, processes, threads, environment variables;
    * the system clock (no "now"/"today"), unseeded randomness (numpy.random.default_rng() needs a fixed seed);
    * exec / eval / compile / open / input / getattr / setattr / delattr / hasattr / globals / locals / vars /
      dir / type, any name or attribute that starts with two underscores, and private attributes of other
      objects (a leading "_" is fine only on self / cls);
    * pandas / numpy file or network helpers (read_* / to_csv / to_pickle / load / save ...), async code;
    * looking at future bars. Keep the code deterministic: same input -> same output.

ALLOWED IMPORTS
    numpy (also numpy.random, numpy.typing, numpy.lib.stride_tricks), pandas (also pandas.api.types), math,
    statistics, dataclasses, typing, the "annotations" future import, and -- with "from ... import ..."
    only -- the engine helpers:
        alpha_engine.strategy / .base / .signal / .params / .context  (Strategy, StrategyContext,
            SignalCandidate, ParamSchema, ParamSpec, params_hash, bar_open_times, ...)
        alpha_engine.indicators  (wilder_atr, sma_atr, true_range, rolling_regression_channel, ...)
        alpha_engine.patterns    (bullish_pin_bar, bearish_engulfing, detect_reversals, ...)
        alpha_engine.risk.sizing (size_position, ...)

LIMITS
    file <= 256 KiB, UTF-8. Validation budget about 30 seconds on ~3000 synthetic H1 bars; worker memory 1 GiB.

SELF-TEST BEFORE UPLOADING
    From the engine folder run:   python -m alpha_engine --plugin-check path/to/your_file.py
    (with the packaged engine:     <engine executable> --plugin-check path/to/your_file.py)
    It runs exactly the upload checks (static + sandboxed dynamic) and prints a JSON report with Persian
    errors and line numbers. Uploading the file in the app shows the same report.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha_engine.strategy.base import Strategy, StrategyContext, bar_open_times
from alpha_engine.strategy.params import ParamSchema, ParamSpec, params_hash
from alpha_engine.strategy.signal import SignalCandidate

SETUP = "ma_cross"
SETUP_TITLE_FA = "کراس میانگین‌های متحرک"
ONE_HOUR = pd.Timedelta(hours=1)
FOUR_HOURS = pd.Timedelta(hours=4)


class MovingAverageCross(Strategy):
    """Buy when the fast SMA of H1 closes crosses above the slow SMA, sell on the mirror cross.

    Optional H4 filter: a buy needs the last CLOSED H4 bar to be bullish (close >= open), a sell bearish.
    Stop loss: lowest low (buy) / highest high (sell) of the last sl_lookback H1 bars, pushed away by
    sl_atr_mult * ATR, where ATR = plain mean of the true range over atr_period bars.
    """

    name = "ma_cross_demo"
    version = 1
    title_fa = "کراس دو میانگین متحرک (نمونه)"
    param_schema = ParamSchema([
        ParamSpec(name="fast", type="int", default=10, min=2, max=100, step=1,
                  label_fa="دوره میانگین سریع", description_fa="تعداد کندل H1 برای میانگین سریع"),
        ParamSpec(name="slow", type="int", default=30, min=3, max=400, step=1,
                  label_fa="دوره میانگین کند", description_fa="باید بزرگ‌تر از دوره میانگین سریع باشد"),
        ParamSpec(name="sl_lookback", type="int", default=10, min=1, max=100, step=1,
                  label_fa="کندل‌های حد ضرر", description_fa="کمینه/بیشینه چند کندل آخر برای حد ضرر"),
        ParamSpec(name="atr_period", type="int", default=14, min=2, max=200, step=1,
                  label_fa="دوره ATR", description_fa="میانگین ساده دامنه واقعی"),
        ParamSpec(name="sl_atr_mult", type="float", default=0.5, min=0.0, max=10.0, step=0.1,
                  label_fa="ضریب ATR حد ضرر", description_fa="فاصله اضافه حد ضرر بر حسب ATR"),
        ParamSpec(name="use_h4_filter", type="bool", default=True, label_fa="فیلتر جهت کندل H4"),
    ])

    # ------------------------------------------------------------------ engine hooks
    def first_valid_index(self, h1, h4, params):
        # First bar with the previous slow SMA, the ATR and the stop window all defined.
        p = self.clean_params(params)
        need = max(int(p["slow"]), int(p["atr_period"]) - 1, int(p["sl_lookback"]) - 1)
        return need if len(h1) > need else None

    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        if ctx.has_open_trade or len(ctx.h1) == 0:
            return None
        p = self.clean_params(ctx.params)
        found = self._candidates(ctx.h1, ctx.h4, p, ctx.account, ctx.symbol, last_only=True)
        return found[0] if found else None

    def scan(self, h1, h4, params, account, *, symbol):
        p = self.clean_params(params)
        return self._candidates(h1, h4, p, account, symbol, last_only=False)

    # ------------------------------------------------------------------ one vectorised, causal pass
    def _candidates(self, h1, h4, p, account, symbol, last_only):
        n = len(h1)
        fast, slow = int(p["fast"]), int(p["slow"])
        if n == 0 or fast >= slow:
            return []
        close = h1["close"].astype("float64")
        high = h1["high"].astype("float64")
        low = h1["low"].astype("float64")
        prev_close = close.shift(1)  # positive shift only: the previous, already closed bar
        true_range = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr = true_range.rolling(int(p["atr_period"])).mean().to_numpy()
        fast_ma = close.rolling(fast).mean().to_numpy()
        slow_ma = close.rolling(slow).mean().to_numpy()
        lowest = low.rolling(int(p["sl_lookback"])).min().to_numpy()
        highest = high.rolling(int(p["sl_lookback"])).max().to_numpy()
        fast_prev = np.r_[np.nan, fast_ma[:-1]]
        slow_prev = np.r_[np.nan, slow_ma[:-1]]
        cross_up = (fast_prev <= slow_prev) & (fast_ma > slow_ma) & np.isfinite(atr)
        cross_down = (fast_prev >= slow_prev) & (fast_ma < slow_ma) & np.isfinite(atr)

        times = bar_open_times(h1)
        # Last H4 bar CLOSED when H1 bar t closes (h4 open + 4h <= h1 open + 1h); -1 = none yet.
        h4_bull = np.zeros(n, dtype=bool)
        h4_known = np.zeros(n, dtype=bool)
        if len(h4):
            h4_close_ns = (bar_open_times(h4) + FOUR_HOURS).as_unit("ns").asi8
            decision_ns = (times + ONE_HOUR).as_unit("ns").asi8
            k = np.searchsorted(h4_close_ns, decision_ns, side="right") - 1
            bullish = (h4["close"].to_numpy(dtype="float64") >= h4["open"].to_numpy(dtype="float64"))
            h4_known = k >= 0
            h4_bull = np.where(h4_known, bullish[np.maximum(k, 0)], False)

        closes = close.to_numpy()
        phash = params_hash(p)
        first = n - 1 if last_only else 0
        out = []
        for t in range(first, n):
            if cross_up[t]:
                direction = "buy"
            elif cross_down[t]:
                direction = "sell"
            else:
                continue
            if p["use_h4_filter"] and (not h4_known[t] or bool(h4_bull[t]) != (direction == "buy")):
                continue
            ref = float(closes[t])
            buffer = float(p["sl_atr_mult"]) * float(atr[t])
            sl = float(lowest[t]) - buffer if direction == "buy" else float(highest[t]) + buffer
            if not (sl > 0 and ((direction == "buy" and sl < ref) or (direction == "sell" and sl > ref))):
                continue
            open_t = times[t]
            out.append(SignalCandidate(
                strategy_name=self.name, strategy_version=self.version, params_hash=phash, symbol=symbol,
                direction=direction, setup=SETUP, setup_title_fa=SETUP_TITLE_FA, line=None,
                confirmation_bar_open_utc=open_t.to_pydatetime(),
                decision_time_utc=(open_t + ONE_HOUR).to_pydatetime(),
                reference_price=ref, stop_loss=sl, rr=float(account.rr),
                pattern="ma_cross_up" if direction == "buy" else "ma_cross_down",
                reason_fa=(f"میانگین {fast} کندلی ({fast_ma[t]:.5f}) میانگین {slow} کندلی ({slow_ma[t]:.5f}) را "
                           f"{'به بالا' if direction == 'buy' else 'به پایین'} قطع کرد."),
                extra={"fast_ma": float(fast_ma[t]), "slow_ma": float(slow_ma[t]), "atr": float(atr[t]),
                       "h4_bullish": bool(h4_bull[t]) if h4_known[t] else None},
            ))
        return out
'''

__all__ = ["TEMPLATE_FILENAME", "TEMPLATE_SOURCE"]
