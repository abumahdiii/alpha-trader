"""Parameters of the StdDev-channel system (stddev-channel-system.md sections 2-5).

Every rule constant of the system is a parameter. Defaults follow the knowledge file and the phase-2
decision log (sigma ddof=0; ATR H1 for touch x0.1 and SL buffer x0.2; ATR H4 for flatness):

=====================  ==========  ============  ================================================
name                   type        default       meaning
=====================  ==========  ============  ================================================
n                      int 10..500 100           H4 bars per rolling regression channel
k                      float 0.5..5 2.0          band width in sigmas
sigma_ddof             choice 0/1  0             0 = population sigma, 1 = sample sigma
flat_mult              float 0..10 1.0           flat if |slope|*n < ATR_H4 * flat_mult
atr_period             int 2..100  14            Wilder ATR period (H1 and H4)
touch_atr_mult         float 0..2  0.1           touch tolerance = ATR_H1 * this
sl_atr_mult            float 0..5  0.2           SL buffer = ATR_H1 * this
pullback_window        int 1..100  12            M: H1 bars after a breakout for the pullback
touch_lookback         int 0..5    1             touch may be on bar t or up to this many bars before
approach_lookback      int 1..10   2             mid bounce: bar t-a must close on the approach side
pin_wick_body_ratio    float 0.5..10 2.0         pin bar: dominant wick >= ratio * body
pin_opposite_wick_max  float 0..2  0.5           pin bar: opposite wick <= this * dominant wick
use_pin_bar            bool        true          pin bar counts as confirmation
use_engulfing          bool        true          engulfing counts as confirmation
projection_mode        choice      bar_count     H4 -> H1 line projection (bar_count / calendar)
=====================  ==========  ============  ================================================

Cross-field rule (not expressible in the schema): at least one confirmation pattern must be enabled.
:func:`resolve_params` enforces it and raises :class:`InvalidParamsError` with Persian messages, so a
bad parameter set fails loudly instead of silently producing a backtest with zero trades.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...strategy.params import ParamSchema, ParamSpec, ParamValue

PROJECTION_MODES = ("bar_count", "calendar")

SCHEMA = ParamSchema([
    ParamSpec(name="n", type="int", default=100, min=10, max=500, step=1,
              label_fa="طول کانال (تعداد کندل H4)",
              description_fa="تعداد کندل‌های بسته‌شده H4 که رگرسیون خطی روی close آن‌ها ساخته می‌شود."),
    ParamSpec(name="k", type="float", default=2.0, min=0.5, max=5.0, step=0.1,
              label_fa="ضریب انحراف معیار (k)",
              description_fa="فاصله خط بالا و پایین از خط میانی بر حسب انحراف معیار."),
    ParamSpec(name="sigma_ddof", type="choice", default=0, choices=(0, 1),
              label_fa="روش محاسبه انحراف معیار",
              description_fa="۰ = جمعیت (پیش‌فرض)، ۱ = نمونه."),
    ParamSpec(name="flat_mult", type="float", default=1.0, min=0.0, max=10.0, step=0.1,
              label_fa="ضریب آستانه کانال افقی",
              description_fa="کانال افقی است اگر |شیب| × N کمتر از ATR روی H4 × این ضریب باشد."),
    ParamSpec(name="atr_period", type="int", default=14, min=2, max=100, step=1,
              label_fa="دوره ATR",
              description_fa="دوره ATR به روش Wilder، برای H1 (لمس و حد ضرر) و H4 (کانال افقی)."),
    ParamSpec(name="touch_atr_mult", type="float", default=0.1, min=0.0, max=2.0, step=0.05,
              label_fa="ضریب ATR برای لمس خط",
              description_fa="کندل خط را لمس کرده اگر محدوده آن تا فاصله ATR(H1) × این ضریب به خط برسد."),
    ParamSpec(name="sl_atr_mult", type="float", default=0.2, min=0.0, max=5.0, step=0.05,
              label_fa="ضریب ATR برای فاصله احتیاطی حد ضرر",
              description_fa="حد ضرر = کف (خرید) یا سقف (فروش) کندل تایید ∓ ATR(H1) × این ضریب."),
    ParamSpec(name="pullback_window", type="int", default=12, min=1, max=100, step=1,
              label_fa="پنجره پولبک (تعداد کندل H1)",
              description_fa="بعد از شکست، پولبک و تایید باید حداکثر در این تعداد کندل H1 رخ دهد."),
    ParamSpec(name="touch_lookback", type="int", default=1, min=0, max=5, step=1,
              label_fa="تعداد کندل مجاز بین لمس و تایید",
              description_fa="لمس خط می‌تواند روی کندل تایید یا حداکثر این تعداد کندل قبل از آن باشد."),
    ParamSpec(name="approach_lookback", type="int", default=2, min=1, max=10, step=1,
              label_fa="کندل مرجع جهت رسیدن به خط میانی",
              description_fa="برای برگشت از خط میانی، close این تعداد کندل قبل باید در سمت رسیدن (بالا برای خرید) باشد."),
    ParamSpec(name="pin_wick_body_ratio", type="float", default=2.0, min=0.5, max=10.0, step=0.1,
              label_fa="نسبت سایه به بدنه پین‌بار"),
    ParamSpec(name="pin_opposite_wick_max", type="float", default=0.5, min=0.0, max=2.0, step=0.05,
              label_fa="حداکثر نسبت سایه مخالف پین‌بار"),
    ParamSpec(name="use_pin_bar", type="bool", default=True, label_fa="تایید با پین‌بار"),
    ParamSpec(name="use_engulfing", type="bool", default=True, label_fa="تایید با اینگالف"),
    ParamSpec(name="projection_mode", type="choice", default="bar_count", choices=PROJECTION_MODES,
              label_fa="روش امتداد خط کانال H4 روی H1",
              description_fa="bar_count = شمارش کندل مثل محور چارت MT5 (پیش‌فرض)، calendar = زمان تقویمی."),
])


class InvalidParamsError(ValueError):
    """Parameter values rejected by the schema or by the cross-field rules (Persian messages)."""

    def __init__(self, errors_fa: list[str]) -> None:
        super().__init__("; ".join(errors_fa))
        self.errors_fa = errors_fa


@dataclass(frozen=True)
class StdDevParams:
    """Validated, typed parameter set."""

    n: int
    k: float
    sigma_ddof: int
    flat_mult: float
    atr_period: int
    touch_atr_mult: float
    sl_atr_mult: float
    pullback_window: int
    touch_lookback: int
    approach_lookback: int
    pin_wick_body_ratio: float
    pin_opposite_wick_max: float
    use_pin_bar: bool
    use_engulfing: bool
    projection_mode: str

    @property
    def patterns(self) -> tuple[str, ...]:
        """Enabled confirmation patterns in the order of ``patterns.candles.PATTERNS``."""
        enabled = []
        if self.use_pin_bar:
            enabled.append("pin_bar")
        if self.use_engulfing:
            enabled.append("engulfing")
        return tuple(enabled)

    @property
    def history_bars(self) -> int:
        """H1 bars (ending at t) that the setup logic reads, besides the causal indicator values.

        Breakout at ``b >= t - M`` needs ``close[b-1]`` -> ``M + 2`` bars; a touch needs
        ``touch_lookback + 1``; the mid-bounce approach bar needs ``approach_lookback + 1``.
        """
        return max(self.pullback_window + 2, self.touch_lookback + 1, self.approach_lookback + 1)


def resolve_params(values: Mapping[str, Any] | None) -> tuple[StdDevParams, dict[str, ParamValue]]:
    """Validate ``values`` (missing keys get defaults). Returns ``(typed, clean_dict)``.

    ``clean_dict`` is what :func:`alpha_engine.strategy.params.params_hash` must be computed from.
    Raises :class:`InvalidParamsError` on any error.
    """
    clean, errors = SCHEMA.validate(values)
    if errors:
        raise InvalidParamsError(errors)
    if not clean["use_pin_bar"] and not clean["use_engulfing"]:
        raise InvalidParamsError(["حداقل یکی از الگوهای تایید (پین‌بار یا اینگالف) باید فعال باشد."])
    typed = StdDevParams(
        n=int(clean["n"]),
        k=float(clean["k"]),
        sigma_ddof=int(clean["sigma_ddof"]),
        flat_mult=float(clean["flat_mult"]),
        atr_period=int(clean["atr_period"]),
        touch_atr_mult=float(clean["touch_atr_mult"]),
        sl_atr_mult=float(clean["sl_atr_mult"]),
        pullback_window=int(clean["pullback_window"]),
        touch_lookback=int(clean["touch_lookback"]),
        approach_lookback=int(clean["approach_lookback"]),
        pin_wick_body_ratio=float(clean["pin_wick_body_ratio"]),
        pin_opposite_wick_max=float(clean["pin_opposite_wick_max"]),
        use_pin_bar=bool(clean["use_pin_bar"]),
        use_engulfing=bool(clean["use_engulfing"]),
        projection_mode=str(clean["projection_mode"]),
    )
    return typed, clean
