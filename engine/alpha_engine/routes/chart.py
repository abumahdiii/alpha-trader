"""``/chart`` -- read-only chart data for the StdDev-channel system (phase 3): channel lines and setups.

Both routes read ONLY the local OHLCV cache (never MT5) and reuse the strategy's own code, so what the
chart shows is exactly what a backtest / live scan over the same cache decides:

* ``GET /chart/channel?symbol&timeframe=H1|H4&from&to``

  - ``H1``: one point per cached H1 bar: the H4 channel projected to that bar, exactly as the strategy
    uses it (``setups.compute_channel_bars`` -- the arrays behind ``StdDevChannelStrategy.indicator_frame``).
  - ``H4``: one point per cached (closed) H4 bar: the rolling channel ENDING at that bar, i.e. the
    regression line's value at the last bar of its window (what MT5's *Standard Deviation Channel*
    object shows at its right end), from ``indicators.regression_channel.rolling_regression_channel``,
    ``indicators.atr.wilder_atr`` and ``indicators.regression_channel.is_flat`` -- the same functions
    ``compute_channel_bars`` calls.

* ``GET /chart/setups?symbol&from&to`` -- the backtest's full-history scan (``backtest.jobs.scan_for``,
  the same cached candidate objects a backtest run uses), filtered by decision time. Status, entry, levels
  and volume follow the BACKTEST SIMULATOR's rules (``backtest.simulator`` helpers via
  ``backtest.setup_outcomes.simulate_setup``): buy fills at the ask open (bid open + spread * point), sell
  at the bid open; a ``missing`` gap, a gap through the stop (buy: bid open <= SL, sell: ask open >= SL),
  a volume below the minimum or a margin above the balance -> ``rejected``; volume sized on the fill with
  the CURRENT account settings and the cached symbol spec. Each setup's independent ``outcome`` (TP / SL /
  end of data) is simulated with the same helpers, ``backtest`` says whether the manual backtest of the
  chart range traded it, ``summary`` sums the outcomes (see ``backtest/setup_outcomes.py``). Without a
  cached symbol spec the phase-3 path is kept (entry = bid open, no volume) and ``evaluation.available``
  is false.

Warm-up consistency: Wilder ATR is a recursion over the whole series and the channel needs ``n`` H4
bars, so everything is computed on the FULL cached history (the same input a backtest or scan over the
full cache gets) and only then filtered to ``[from, to]``. A sub-range therefore returns bit-identical
numbers to the full range for the overlapping bars (tests/test_chart_routes.py).

A small in-process LRU (:class:`ChartCache`, ``app.state.chart_cache``, 16 entries, shared with the
backtest jobs) keeps the computed series, keyed by symbol, cache-file identity (file id/``mtime_ns``/size
of the Parquet files and the spec JSON), params hash and -- for the scan -- the account R:R (the only
account field a candidate carries). Any cache update or params change gives a new key; the least recently
used entries are dropped beyond ``maxsize``. Outcomes, backtest flags and the summary depend on every
account field and on the cost model, so they are computed per request and never cached or stored in a
scan entry.

Stored params come from the active params version (``strategy.context.load_active_params``); params
that no longer fit the schema -> 409 ``stored_params_invalid``. Errors use
``{"detail": {"code", "message_fa", "errors_fa"}}``. All times are ISO-8601 UTC with ``Z``.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..backtest.costs import observed_spread, resolve_fallback_points
from ..backtest.history import HistoryUnavailable
from ..backtest.jobs import bars_for_cost, first_valid_index, prepare_history, scan_for
from ..backtest.models import (
    HISTORICAL_SPREAD_LABEL_FA,
    PROVISIONAL_LABEL_FA,
    CostModel,
    SpreadFallback,
    spread_label_fa,
)
from ..backtest.setup_outcomes import (
    END_OF_DATA_FA,
    BacktestFlag,
    SetupOutcome,
    backtest_flags,
    backtest_window_for_range,
    counts_only_summary,
    run_range_backtest,
    simulate_setups,
    span_indices,
    summarize_setups,
)
from ..data.cache import CacheError, OhlcvCache
from ..data.schema import Timeframe, empty_frame, iso_z
from ..data.symbols import InvalidSymbolName, SymbolSpec
from ..indicators.atr import wilder_atr
from ..indicators.regression_channel import is_flat, rolling_regression_channel
from ..logging_setup import get_logger, is_dev_mode
from ..market_data import MarketDataService, SymbolNotConfigured
from ..risk.sizing import size_from_spec
from ..storage.account_settings import AccountSettings, AccountSettingsRepo
from ..storage.db import EngineConnection
from ..storage.strategies_repo import StoredParamsInvalidError
from ..strategies.stddev_channel import StdDevChannelStrategy
from ..strategies.stddev_channel.levels import entry_price_for, resolve_trade_levels
from ..strategies.stddev_channel.params import InvalidParamsError, StdDevParams, resolve_params
from ..strategies.stddev_channel.setups import SETUP_TITLE_FA, ChannelBars, compute_channel_bars
from ..strategy.base import bar_open_times
from ..strategy.context import DEFAULT_STRATEGY, load_active_params
from ..strategy.params import ParamValue
from ..strategy.registry import StrategyRegistry, UnknownStrategyError
from ..strategy.signal import SignalCandidate
from . import api_error, get_db, get_market_data
from .strategies import get_registry

logger = get_logger(__name__)
router = APIRouter(prefix="/chart", tags=["chart"])

DEFAULT_WINDOW = timedelta(days=30)
CHART_CACHE_SIZE = 16
_H1 = timedelta(hours=1)

Direction = Literal["up", "down", "flat", "none"]
SetupStatus = Literal["accepted", "rejected", "pending_entry"]
SpreadSourceName = Literal["historical", "filled", "fallback", "zero"]

NOTE_REJECTIONS_FA = (
    "وضعیت «رد شده» با همان قواعد بک‌تست است: داده گمشده بین کندل تایید و کندل ورود، باز شدن کندل ورود آن طرف "
    "حد ضرر (خرید با bid و فروش با ask)، و رد حجم (کمتر از حداقل حجم یا مارجین بیشتر از موجودی). ستاپ‌هایی که "
    "خود سیستم کنار می‌گذارد (مثل تعارض دو جهت) در این فهرست نیستند."
)
PENDING_NOTE_FA = "کندل بعد از کندل تایید هنوز در کش نیست؛ قیمت ورود، حد سود و حجم پس از باز شدن آن کندل مشخص می‌شوند."
SPEC_MISSING_FA = "مشخصات نماد در کش نیست؛ حجم قابل محاسبه نیست (یک بار با اتصال به MT5 فهرست نمادها را بگیرید)."
EVALUATION_LABEL_FA = "ارزیابی مستقل هر ستاپ، بدون قید یک معامله باز؛ با نتیجه بک‌تست فرق دارد"
EVAL_SPEC_MISSING_FA = ("مشخصات نماد در کش نیست؛ نتیجه ستاپ‌ها و مقایسه با بک‌تست محاسبه نمی‌شود و قیمت ورود "
                        "بدون اسپرد (open کندل بعد) نشان داده می‌شود.")
EVAL_FAILED_FA = "محاسبه نتیجه ستاپ‌ها با خطا روبه‌رو شد؛ جزئیات در لاگ engine ثبت شد."
BACKTEST_FAILED_FA = "اجرای بک‌تست این بازه با خطا روبه‌رو شد؛ جزئیات در لاگ engine ثبت شد."


# ---------------------------------------------------------------------------------------------- models
class ChannelPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: str  # bar open time (UTC)
    valid: bool
    mid: float | None = None
    upper: float | None = None
    lower: float | None = None
    slope: float | None = None  # price per H4 bar
    sigma: float | None = None
    is_flat: bool | None = None
    direction: Direction | None = None
    atr_h1: float | None = None  # H1 points only
    atr_h4: float | None = None
    h4_open: str | None = None  # open time of the H4 bar whose channel this is
    bars_ahead: float | None = None  # H1 points only: projection distance in H4 bars (x = n-1 + bars_ahead)


class _StrategyInfo(BaseModel):
    symbol: str
    strategy: str
    strategy_version: int  # code version
    params_version: int
    params_hash: str
    params: dict[str, ParamValue]


class ChannelResponse(_StrategyInfo):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    timeframe: Literal["H1", "H4"]
    from_: str | None = Field(serialization_alias="from")
    to: str | None
    count: int
    valid_count: int
    points: list[ChannelPoint]
    message_fa: str | None = None


class SetupOutcomeOut(BaseModel):
    """Independent result of an accepted setup (``backtest.setup_outcomes.simulate_setup``)."""

    model_config = ConfigDict(extra="forbid")

    result: Literal["tp", "sl", "end_of_data"]  # tp: tp/tp_gap, sl: sl/sl_gap, end_of_data: open at the last bar
    exit_reason: str  # sl | tp | sl_gap | tp_gap | end_of_period
    exit_reason_fa: str
    exit_bar_time: str  # OPEN of the exit bar (chart marker)
    exit_time: str  # gap exits: bar open; intrabar hits and end of data: bar close
    exit_price: float
    pnl_price: float  # price move in the trade's favour (sell: entry - exit)
    gross_pnl: float
    commission: float
    net_pnl: float
    r_multiple: float | None
    bars_held: int
    held_over_weekend: bool
    flags: list[str]


class SetupBacktestFlag(BaseModel):
    """Was this setup traded by the manual backtest of the chart range (``backtest_window``)?"""

    model_config = ConfigDict(extra="forbid")

    traded: bool
    reason: str | None  # not traded: position_open | entry_outside_window | missing_gap | gap_through_stop |
    #                     invalid_levels | sizing_rejected | outside_window | not_reached
    reason_fa: str | None
    trade_index: int | None
    net_pnl: float | None  # the backtest trade's net P&L (momentary balance of the backtest)


class SetupItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    symbol: str
    status: SetupStatus
    rejection_reason_fa: str | None
    setup_type: str
    setup_title_fa: str
    direction: Literal["buy", "sell"]
    pattern: str
    line: Literal["lower", "mid", "upper"]
    line_value: float | None  # value of that channel line at the confirmation bar
    channel_direction: str | None
    confirmation_bar_time: str  # OPEN time of the confirmation H1 bar t
    decision_time: str  # CLOSE of bar t = confirmation_bar_time + 1h (the decision is taken here)
    entry_time: str | None  # OPEN time of the next cached H1 bar t+1 (after a weekend: Sunday's open)
    entry: float | None  # simulator fill: buy = ask open (bid open + spread * point), sell = bid open
    entry_bid_open: float | None = None  # raw (bid) open of bar t+1
    spread_at_entry_points: int | None = None  # spread of bar t+1 actually used (after the zero fill / fallback)
    entry_spread_source: SpreadSourceName | None = None
    stop_loss: float
    take_profit: float | None  # entry +/- rr * |entry - stop_loss|
    rr: float
    risk_distance: float | None  # |entry - stop_loss|
    reference_price: float  # close of bar t (indicative only)
    indicative_take_profit: float  # from reference_price (indicative only)
    volume: float | None
    actual_risk: float | None
    margin: float | None
    risk_amount: float | None
    volume_note_fa: str | None  # why volume is null while the setup is not rejected
    sizing_warnings_fa: list[str]
    reason_fa: str
    indicators: dict[str, float | int | bool | str | None]  # SignalCandidate.extra (inputs of the decision)
    outcome: SetupOutcomeOut | None = None  # accepted + evaluation available
    backtest: SetupBacktestFlag | None = None  # evaluation + backtest window available


class SetupsEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    message_fa: str | None = None
    basis: Literal["independent_setups"] = "independent_setups"
    label_fa: str = EVALUATION_LABEL_FA
    end_of_data_label_fa: str = END_OF_DATA_FA
    cost_model: CostModel | None = None
    spread_fallback: SpreadFallback | None = None  # bars counted over the evaluated setups' span
    labels_fa: list[str] = Field(default_factory=list)


class SetupsSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    accepted: int
    rejected: int
    pending_entry: int
    closed: int  # accepted, exited at TP or SL (gaps included)
    wins: int
    losses: int
    breakeven: int
    open_end_of_data: int  # accepted, still open at the last cached bar (closed at its close, provisional)
    win_rate: float | None  # wins / closed, 0..1
    net_pnl: float  # closed only
    gross_profit: float
    gross_loss: float
    profit_factor: float | None
    profit_factor_infinite: bool
    total_r: float
    avg_r: float | None
    r_count: int
    open_net_pnl: float  # sum of the end-of-data P&L (not in wins/losses/PF/net_pnl)


class BacktestWindowOut(BaseModel):
    """The manual backtest window of this range (what «بک‌تست همین بازه» submits) and its result."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str | None = Field(serialization_alias="from")
    to: str | None
    clipped: bool = False
    note_fa: str | None = None
    available: bool
    code: str | None = None
    message_fa: str | None = None
    trades: int | None = None
    net_profit: float | None = None


class SetupsResponse(_StrategyInfo):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str | None = Field(serialization_alias="from")
    to: str | None
    account: AccountSettings
    symbol_spec: SymbolSpec | None
    count: int
    status_counts: dict[str, int]
    setups: list[SetupItem]
    note_fa: str = NOTE_REJECTIONS_FA
    message_fa: str | None = None
    evaluation: SetupsEvaluation
    summary: SetupsSummary
    backtest_window: BacktestWindowOut


# ---------------------------------------------------------------------------------------------- cache
class ChartCache:
    """Thread-safe bounded LRU of computed chart series (see module docstring for the key)."""

    def __init__(self, maxsize: int = CHART_CACHE_SIZE) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self._data: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._data)

    def get_or_compute(self, key: Hashable, compute: Callable[[], Any]) -> tuple[Any, bool]:
        """``(value, hit)``. ``compute`` runs outside the lock (two concurrent misses may both compute)."""
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key], True
        value = compute()
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
            self.misses += 1
        return value, False

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def get_chart_cache(request: Request) -> ChartCache:
    cache = getattr(request.app.state, "chart_cache", None)
    if cache is None:  # apps assembled without create_app
        cache = ChartCache()
        request.app.state.chart_cache = cache
    return cache


# ---------------------------------------------------------------------------------------------- helpers
def _utc(value: datetime | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _check_symbol(service: MarketDataService, symbol: str) -> str:
    try:
        return service.check_symbol(symbol)
    except InvalidSymbolName:
        raise api_error(422, "invalid_symbol", f"نام نماد «{symbol}» معتبر نیست.") from None
    except SymbolNotConfigured:
        raise api_error(404, "symbol_not_configured",
                        f"نماد «{symbol}» در فهرست نمادهای engine نیست ({', '.join(service.symbols)}).") from None


def _check_range(start: pd.Timestamp | None, end: pd.Timestamp | None) -> None:
    if start is not None and end is not None and start > end:
        raise api_error(422, "invalid_range", "ابتدای بازه («from») نباید بعد از انتهای آن («to») باشد.")


def _file_sig(cache: OhlcvCache, symbol: str, tf: Timeframe) -> tuple[int, int, int] | None:
    """Identity of a cache file. Every cache write is an atomic replace by a NEW file, so the file id
    (``st_ino``; the NTFS file index on Windows) changes even when mtime/size happen to be equal."""
    try:
        st = cache.series_path(symbol, tf).stat()
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _read_frame(cache: OhlcvCache, symbol: str, tf: Timeframe) -> pd.DataFrame | None:
    try:
        cached = cache.read(symbol, tf)
    except (CacheError, OSError, ValueError) as exc:
        logger.warning("chart: cache for %s %s is unreadable: %s", symbol, tf.value, exc)
        if is_dev_mode():
            logger.debug("chart: cache read failed", exc_info=True)
        raise api_error(503, "cache_unreadable", f"فایل کش {symbol} {tf.value} قابل خواندن نیست.") from None
    return None if cached is None else cached[0]


@dataclass(frozen=True)
class _Active:
    record_version: int
    code_version: int
    params_hash: str
    params: dict[str, ParamValue]
    p: StdDevParams
    strategy: StdDevChannelStrategy


def _active_params(db: EngineConnection, registry: StrategyRegistry) -> _Active:
    try:
        cls, record = load_active_params(db, DEFAULT_STRATEGY, registry=registry)
    except UnknownStrategyError:
        raise api_error(404, "strategy_not_found", f"استراتژی «{DEFAULT_STRATEGY}» پیدا نشد.") from None
    except StoredParamsInvalidError as exc:
        raise api_error(409, "stored_params_invalid",
                        "پارامترهای ذخیره‌شده سیستم با نسخه فعلی سازگار نیستند؛ پارامترها را دوباره ذخیره کنید.",
                        exc.errors_fa) from None
    if not (isinstance(cls, type) and issubclass(cls, StdDevChannelStrategy)):
        raise api_error(404, "strategy_not_found", f"استراتژی «{DEFAULT_STRATEGY}» از نوع کانال انحراف معیار نیست.")
    try:
        p, clean = resolve_params(record.params)
    except InvalidParamsError as exc:
        raise api_error(409, "stored_params_invalid", "پارامترهای ذخیره‌شده سیستم نامعتبر هستند.",
                        list(exc.errors_fa)) from None
    return _Active(record.version, cls.version, record.params_hash, dict(clean), p, cls())


def _info(symbol: str, active: _Active) -> dict[str, Any]:
    return {"symbol": symbol, "strategy": active.strategy.name, "strategy_version": active.code_version,
            "params_version": active.record_version, "params_hash": active.params_hash, "params": active.params}


def _window(times_ns: np.ndarray, start: pd.Timestamp | None, end: pd.Timestamp | None,
            last_default: pd.Timestamp | None) -> tuple[pd.Timestamp | None, pd.Timestamp | None, np.ndarray]:
    """Effective ``[from, to]`` (defaults: to = ``last_default``, from = to - 30 days) and the index mask."""
    eff_end = end if end is not None else last_default
    eff_start = start if start is not None else (None if eff_end is None else eff_end - DEFAULT_WINDOW)
    if eff_start is None or eff_end is None:
        return eff_start, eff_end, np.zeros(len(times_ns), dtype=bool)
    lo, hi = eff_start.as_unit("ns").value, eff_end.as_unit("ns").value
    return eff_start, eff_end, (times_ns >= lo) & (times_ns <= hi)


def _ns_iso(ns: int) -> str | None:
    return None if ns < 0 else iso_z(pd.Timestamp(int(ns), tz="UTC"))


def _direction(valid: bool, flat: bool, slope: float) -> Direction | None:
    """Channel direction for an H4 point; same rule as ``ChannelBars.channel_direction`` (tested)."""
    if not valid:
        return None
    if flat:
        return "flat"
    if slope > 0:
        return "up"
    if slope < 0:
        return "down"
    return "none"


# ---------------------------------------------------------------------------------------------- compute
@dataclass(frozen=True)
class _H4Channel:
    times_ns: np.ndarray
    mid: np.ndarray
    upper: np.ndarray
    lower: np.ndarray
    slope: np.ndarray
    sigma: np.ndarray
    atr_h4: np.ndarray
    is_flat: np.ndarray
    valid: np.ndarray


def compute_h4_channel(h4: pd.DataFrame, p: StdDevParams) -> _H4Channel:
    """Rolling channel ending at every H4 bar + Wilder ATR_H4 + flatness, over the full H4 history."""
    size = len(h4)
    times_ns = pd.DatetimeIndex(h4["time"]).as_unit("ns").asi8
    chan = rolling_regression_channel(h4["close"].to_numpy(dtype=np.float64), p.n, p.k, p.sigma_ddof)
    atr = wilder_atr(h4["high"], h4["low"], h4["close"], p.atr_period).to_numpy()
    mid, upper, lower, slope, sigma = (chan[c].to_numpy() for c in ("mid", "upper", "lower", "slope", "sigma"))
    flat = np.asarray(is_flat(slope, p.n, atr, p.flat_mult), dtype=bool).reshape(size)
    valid = np.isfinite(mid) & np.isfinite(upper) & np.isfinite(lower) & np.isfinite(atr)
    if is_dev_mode():
        logger.debug("chart compute_h4_channel: h4=%d n=%d k=%g ddof=%d valid=%d flat=%d", size, p.n, p.k,
                     p.sigma_ddof, int(valid.sum()), int(flat.sum()))
    return _H4Channel(times_ns=times_ns, mid=mid, upper=upper, lower=lower, slope=slope, sigma=sigma, atr_h4=atr,
                      is_flat=flat, valid=valid)


def _iso_list(ns: np.ndarray) -> list[str]:
    """Vectorised ``iso_z`` for int64 UTC nanoseconds (same ``2025-01-06T00:00:00Z`` format; numpy's
    formatter is ~50x faster than pandas ``strftime``)."""
    seconds = (np.asarray(ns, dtype=np.int64) // 1_000_000_000).astype("datetime64[s]")
    return [text + "Z" for text in np.datetime_as_string(seconds, unit="s").tolist()]


def _columns(source: Any, names: tuple[str, ...], idx: np.ndarray) -> dict[str, list[float | None]]:
    """Selected rows of float arrays as Python floats; non-finite -> None (JSON has no NaN)."""
    out: dict[str, list[float | None]] = {}
    for name in names:
        values = np.asarray(getattr(source, name), dtype=np.float64)[idx]
        out[name] = [v if math.isfinite(v) else None for v in values.tolist()]
    return out


def _h1_points(cb: ChannelBars, mask: np.ndarray) -> list[ChannelPoint]:
    idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    times = _iso_list(cb.times.as_unit("ns").asi8[idx])
    h4_ns = cb.h4_open[idx]
    h4_open = _iso_list(np.where(h4_ns >= 0, h4_ns, 0))
    cols = _columns(cb, ("mid", "upper", "lower", "slope", "sigma", "atr_h1", "atr_h4", "bars_ahead"), idx)
    valid, flat = cb.valid[idx].tolist(), cb.is_flat[idx].tolist()
    points: list[ChannelPoint] = []
    for k, i in enumerate(idx.tolist()):
        if not valid[k]:
            points.append(ChannelPoint(time=times[k], valid=False))
            continue
        points.append(ChannelPoint(
            time=times[k], valid=True, mid=cols["mid"][k], upper=cols["upper"][k], lower=cols["lower"][k],
            slope=cols["slope"][k], sigma=cols["sigma"][k], is_flat=bool(flat[k]), direction=cb.channel_direction(i),
            atr_h1=cols["atr_h1"][k], atr_h4=cols["atr_h4"][k], h4_open=h4_open[k] if h4_ns[k] >= 0 else None,
            bars_ahead=cols["bars_ahead"][k],
        ))
    return points


def _h4_points(ch: _H4Channel, mask: np.ndarray) -> list[ChannelPoint]:
    idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    times = _iso_list(ch.times_ns[idx])
    cols = _columns(ch, ("mid", "upper", "lower", "slope", "sigma", "atr_h4"), idx)
    valid, flat = ch.valid[idx].tolist(), ch.is_flat[idx].tolist()
    points: list[ChannelPoint] = []
    for k in range(len(idx)):
        if not valid[k]:
            points.append(ChannelPoint(time=times[k], valid=False))
            continue
        slope = cols["slope"][k]
        points.append(ChannelPoint(
            time=times[k], valid=True, mid=cols["mid"][k], upper=cols["upper"][k], lower=cols["lower"][k],
            slope=slope, sigma=cols["sigma"][k], is_flat=bool(flat[k]),
            direction=_direction(True, bool(flat[k]), slope if slope is not None else float("nan")), atr_h1=None,
            atr_h4=cols["atr_h4"][k], h4_open=times[k], bars_ahead=None,
        ))
    return points


# ---------------------------------------------------------------------------------------------- /chart/channel
@router.get("/channel", response_model=ChannelResponse, response_model_by_alias=True,
            responses={404: {"description": "Symbol/strategy not found"}, 409: {"description": "Stored params invalid"},
                       422: {"description": "Invalid parameters"}, 503: {"description": "DB or cache unavailable"}})
def get_channel(
    symbol: Annotated[str, Query(min_length=1, max_length=32)],
    timeframe: Annotated[str, Query()] = "H1",
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    service: MarketDataService = Depends(get_market_data),
    db: EngineConnection = Depends(get_db),
    registry: StrategyRegistry = Depends(get_registry),
    chart_cache: ChartCache = Depends(get_chart_cache),
) -> ChannelResponse:
    try:
        tf = Timeframe.parse(timeframe)
    except ValueError:
        raise api_error(422, "invalid_timeframe", f"تایم‌فریم «{timeframe}» پشتیبانی نمی‌شود (H1 یا H4).") from None
    name = _check_symbol(service, symbol)
    start, end = _utc(from_), _utc(to)
    _check_range(start, end)
    active = _active_params(db, registry)
    if is_dev_mode():
        logger.debug("GET /chart/channel %s %s from=%s to=%s params v%d hash=%s", name, tf.value, iso_z(start),
                     iso_z(end), active.record_version, active.params_hash[:12])
    cache = service.cache
    h1_sig, h4_sig = _file_sig(cache, name, Timeframe.H1), _file_sig(cache, name, Timeframe.H4)
    message: str | None = None

    if tf is Timeframe.H1:
        if h1_sig is None:
            return _empty_channel(name, tf, active, start, end, f"داده H1 نماد {name} در کش نیست.")
        if h4_sig is None:
            message = f"داده H4 نماد {name} در کش نیست؛ کانال ساخته نمی‌شود."
        key = ("h1_channel", str(cache.data_dir), name, h1_sig, h4_sig, active.params_hash)

        def compute_h1() -> ChannelBars:
            h1 = _read_frame(cache, name, Timeframe.H1)
            h4 = _read_frame(cache, name, Timeframe.H4)
            return compute_channel_bars(h1 if h1 is not None else empty_frame(),
                                        h4 if h4 is not None else empty_frame(), active.p, pattern_from=None)

        cb, hit = chart_cache.get_or_compute(key, compute_h1)
        times_ns = cb.times.as_unit("ns").asi8
        last = cb.times[-1] if len(cb) else None
        eff_start, eff_end, mask = _window(times_ns, start, end, last)
        points = _h1_points(cb, mask)
        total = len(cb)
    else:
        if h4_sig is None:
            return _empty_channel(name, tf, active, start, end, f"داده H4 نماد {name} در کش نیست.")
        key = ("h4_channel", str(cache.data_dir), name, h4_sig, active.params_hash)

        def compute_h4() -> _H4Channel:
            h4 = _read_frame(cache, name, Timeframe.H4)
            return compute_h4_channel(h4 if h4 is not None else empty_frame(), active.p)

        ch, hit = chart_cache.get_or_compute(key, compute_h4)
        last = pd.Timestamp(int(ch.times_ns[-1]), tz="UTC") if len(ch.times_ns) else None
        eff_start, eff_end, mask = _window(ch.times_ns, start, end, last)
        points = _h4_points(ch, mask)
        total = len(ch.times_ns)

    valid_count = sum(1 for pt in points if pt.valid)
    if message is None and total and not valid_count and points:
        message = f"داده کافی برای ساخت کانال (n = {active.p.n} کندل H4 و ATR) در این بازه وجود ندارد."
    if is_dev_mode():
        logger.debug("chart channel %s %s: cache %s, series=%d points=%d valid=%d window=%s..%s", name, tf.value,
                     "hit" if hit else "miss", total, len(points), valid_count, iso_z(eff_start), iso_z(eff_end))
    return ChannelResponse(**_info(name, active), timeframe=tf.value, from_=iso_z(eff_start), to=iso_z(eff_end),
                           count=len(points), valid_count=valid_count, points=points, message_fa=message)


def _empty_channel(name: str, tf: Timeframe, active: _Active, start: pd.Timestamp | None, end: pd.Timestamp | None,
                   message: str) -> ChannelResponse:
    if is_dev_mode():
        logger.debug("chart channel %s %s: no cached data (%s)", name, tf.value, message)
    return ChannelResponse(**_info(name, active), timeframe=tf.value, from_=iso_z(start), to=iso_z(end), count=0,
                           valid_count=0, points=[], message_fa=message)


# ---------------------------------------------------------------------------------------------- /chart/setups
@dataclass(frozen=True)
class _Scan:
    h1: pd.DataFrame
    times_ns: np.ndarray
    candidates: list[SignalCandidate]
    decision_ns: np.ndarray  # decision time of each candidate (int64 ns UTC)


def setup_id(candidate: SignalCandidate) -> str:
    """Stable id: symbol, confirmation bar open (UTC) and the params hash prefix."""
    stamp = candidate.confirmation_bar_open_utc.strftime("%Y%m%dT%H%MZ")
    return f"{candidate.symbol}:{stamp}:{candidate.params_hash[:12]}"


def _base_item(candidate: SignalCandidate) -> dict[str, Any]:
    """Fields known at decision time (never depend on bars after the confirmation bar)."""
    extra = dict(candidate.extra)  # a copy: nothing is ever written back into the candidate
    return dict(
        id=setup_id(candidate), symbol=candidate.symbol, setup_type=candidate.setup.value,
        setup_title_fa=SETUP_TITLE_FA[candidate.setup], direction=candidate.direction, pattern=candidate.pattern,
        line=candidate.line, line_value=extra.get("line_value"), channel_direction=extra.get("channel_direction"),
        confirmation_bar_time=iso_z(pd.Timestamp(candidate.confirmation_bar_open_utc)),
        decision_time=iso_z(pd.Timestamp(candidate.decision_time_utc)), stop_loss=candidate.stop_loss,
        rr=candidate.rr, reference_price=candidate.reference_price,
        indicative_take_profit=candidate.indicative_take_profit, reason_fa=candidate.reason_fa, indicators=extra,
        entry_time=None, entry=None, entry_bid_open=None, spread_at_entry_points=None, entry_spread_source=None,
        take_profit=None, risk_distance=None, volume=None, actual_risk=None, margin=None, risk_amount=None,
        volume_note_fa=None, sizing_warnings_fa=[], rejection_reason_fa=None, outcome=None, backtest=None,
    )


def build_setup_item(
    candidate: SignalCandidate,
    h1: pd.DataFrame,
    times_ns: np.ndarray,
    account: AccountSettings,
    spec: SymbolSpec | None,
) -> SetupItem:
    """Phase-3 path, used only when the setups cannot be evaluated with the simulator rules (no cached symbol
    spec, so no point / spread price): entry = bid open of bar t+1 (spread unknown), SL/TP at that entry."""
    conf_ns = pd.Timestamp(candidate.confirmation_bar_open_utc).as_unit("ns").value
    t = int(np.searchsorted(times_ns, conf_ns))
    if t >= len(times_ns) or times_ns[t] != conf_ns:  # pragma: no cover - scan only returns bars of h1
        raise RuntimeError(f"confirmation bar {candidate.confirmation_bar_open_utc} not in the H1 frame")
    base = _base_item(candidate)
    entry = entry_price_for(h1, t)
    if entry is None:
        base.update(status="pending_entry", volume_note_fa=PENDING_NOTE_FA)
        return SetupItem(**base)
    base["entry"] = entry
    base["entry_bid_open"] = entry
    base["entry_time"] = _ns_iso(int(times_ns[t + 1]))
    try:
        levels = resolve_trade_levels(candidate, entry)
    except ValueError as exc:
        gapped = entry <= candidate.stop_loss if candidate.direction == "buy" else entry >= candidate.stop_loss
        if gapped:
            side_fa = "زیر" if candidate.direction == "buy" else "بالای"
            reason = (f"کندل ورود با قیمت {entry:g} {side_fa} حد ضرر {candidate.stop_loss:g} باز شد "
                      "(گپ از حد ضرر عبور کرد)؛ ورود انجام نمی‌شود.")
        else:
            reason = f"سطوح معامله با قیمت ورود {entry:g} معتبر نیستند (حد سود محاسبه‌شده مثبت نیست): {exc}"
        base.update(status="rejected", rejection_reason_fa=reason)
        return SetupItem(**base)
    base["take_profit"] = levels.take_profit
    base["risk_distance"] = levels.risk_distance
    if spec is None:
        # Levels are valid, only the size is unknown: kept "accepted" so the chart still shows the trade.
        base.update(status="accepted", volume_note_fa=SPEC_MISSING_FA)
        return SetupItem(**base)
    sizing = size_from_spec(candidate, account, spec, entry=entry)
    base["sizing_warnings_fa"] = list(sizing.warnings)
    if not sizing.accepted:
        base.update(status="rejected", rejection_reason_fa=sizing.reason_fa)
        return SetupItem(**base)
    base.update(status="accepted", volume=sizing.volume, actual_risk=sizing.actual_risk, margin=sizing.margin,
                risk_amount=sizing.risk_amount)
    return SetupItem(**base)


def build_evaluated_item(outcome: SetupOutcome, times_ns: np.ndarray, flag: BacktestFlag | None) -> SetupItem:
    """Item from the simulator-rule evaluation (``setup_outcomes.simulate_setup``) + the backtest flag."""
    base = _base_item(outcome.candidate)
    if flag is not None:
        base["backtest"] = SetupBacktestFlag(traded=flag.traded, reason=flag.reason, reason_fa=flag.reason_fa,
                                             trade_index=flag.trade_index, net_pnl=flag.net_pnl)
    if outcome.status == "pending_entry":
        base.update(status="pending_entry", volume_note_fa=PENDING_NOTE_FA)
        return SetupItem(**base)
    if outcome.status == "rejected":
        rej = outcome.rejection
        assert rej is not None
        if rej.entry_index is not None:
            base["entry_time"] = _ns_iso(int(times_ns[rej.entry_index]))
        base.update(status="rejected", rejection_reason_fa=rej.reason_fa, entry=rej.fill, entry_bid_open=rej.bid_open,
                    spread_at_entry_points=rej.spread_points, entry_spread_source=rej.spread_source)
        if rej.take_profit is not None and rej.fill is not None:  # sizing rejection: the levels were valid
            base.update(take_profit=rej.take_profit, risk_distance=abs(rej.fill - outcome.candidate.stop_loss))
        if rej.sizing is not None:
            base["sizing_warnings_fa"] = list(rej.sizing.warnings)
        return SetupItem(**base)
    pos, tr = outcome.position, outcome.trade
    assert pos is not None and tr is not None and pos.sizing is not None and outcome.result is not None
    base.update(
        status="accepted", entry_time=_ns_iso(int(times_ns[pos.entry_index])), entry=pos.entry,
        entry_bid_open=pos.entry_bid_open, spread_at_entry_points=pos.spread_entry_pts,
        entry_spread_source=pos.spread_source, take_profit=pos.tp, risk_distance=abs(pos.entry - pos.sl),
        volume=pos.volume, actual_risk=pos.sizing.actual_risk, margin=pos.sizing.margin,
        risk_amount=pos.sizing.risk_amount, sizing_warnings_fa=list(pos.warnings),
        outcome=SetupOutcomeOut(
            result=outcome.result, exit_reason=tr.exit_reason.value,
            exit_reason_fa=END_OF_DATA_FA if outcome.result == "end_of_data" else tr.exit_reason_fa,
            exit_bar_time=iso_z(pd.Timestamp(tr.exit_bar_time)), exit_time=iso_z(pd.Timestamp(tr.exit_time)),
            exit_price=tr.exit_price, pnl_price=(tr.exit_price - tr.entry) * pos.sign, gross_pnl=tr.gross_pnl,
            commission=tr.commission, net_pnl=tr.net_pnl, r_multiple=tr.r_multiple, bars_held=tr.bars_held,
            held_over_weekend=tr.held_over_weekend, flags=list(tr.flags),
        ),
    )
    return SetupItem(**base)


@dataclass
class _Evaluated:
    eff_start: pd.Timestamp | None
    eff_end: pd.Timestamp | None
    items: list[SetupItem]
    total_candidates: int
    has_bars: bool
    evaluation: SetupsEvaluation
    summary: SetupsSummary
    backtest_window: BacktestWindowOut


def _provisional(request: Request) -> bool:
    settings = getattr(request.app.state, "settings", None)
    return not bool(getattr(settings, "alpha_data_check_confirmed", False))


def _evaluate_setups(name: str, active: _Active, account: AccountSettings, cache: OhlcvCache, lru: ChartCache,
                     start: pd.Timestamp | None, end: pd.Timestamp | None, provisional: bool) -> _Evaluated:
    """Simulator-rule evaluation of the setups in ``[start, end]`` (raises ``HistoryUnavailable``)."""
    cost = CostModel()  # what «بک‌تست همین بازه» submits by default (auto fallback spread, no commission)
    prepared = prepare_history(cache, name, lru)
    history = prepared.history
    bars = bars_for_cost(prepared, cost)
    scan = scan_for(prepared, symbol=name, params=active.params, params_hash=active.params_hash, account=account,
                    lru=lru)
    times_ns = bars.times_ns
    decision_ns = np.array([pd.Timestamp(c.decision_time_utc).as_unit("ns").value for c in scan.candidates],
                           dtype=np.int64)
    # Default window ends at the close of the last cached H1 bar, so a setup on the last bar is included.
    last_decision = pd.Timestamp(int(times_ns[-1]), tz="UTC") + _H1 if len(times_ns) else None
    eff_start, eff_end, mask = _window(decision_ns, start, end, last_decision)
    selected = [scan.candidates[i] for i in np.flatnonzero(mask).tolist()]
    outcomes = simulate_setups(bars, selected, spec=history.spec, account=account, cost_model=cost)

    # backtest of the same range (exactly what POST /backtests would run)
    fv = first_valid_index(prepared, active.params, active.params_hash, lru)
    h4_ns = bar_open_times(history.h4).as_unit("ns").asi8
    rw = backtest_window_for_range(eff_start, eff_end, times_ns, h4_ns, fv, active.p.atr_period)
    flags: list[BacktestFlag] | None = None
    bt = BacktestWindowOut(from_=iso_z(pd.Timestamp(rw.start)) if rw.start else None,
                           to=iso_z(pd.Timestamp(rw.end)) if rw.end else None, clipped=rw.clipped, note_fa=rw.note_fa,
                           available=rw.available, code=rw.code, message_fa=rw.message_fa)
    if rw.available:
        try:
            result = run_range_backtest(
                rw, history, scan, bars, account=account, params=active.params, params_hash=active.params_hash,
                params_version=active.record_version, strategy_name=active.strategy.name,
                strategy_version=active.code_version, cost_model=cost, provisional=provisional)
            flags = backtest_flags(result, selected)
            bt = bt.model_copy(update={"trades": len(result.trades), "net_profit": result.net_profit})
        except Exception as exc:  # never fail the chart for the comparison column
            logger.warning("chart setups %s: range backtest failed: %s: %s", name, type(exc).__name__, exc)
            if is_dev_mode():
                logger.debug("chart setups: range backtest traceback", exc_info=True)
            bt = bt.model_copy(update={"available": False, "code": "backtest_failed", "message_fa": BACKTEST_FAILED_FA})

    # honest cost labels (as a backtest run): spread fallback counted over the bars the outcomes used
    raw = history.h1["spread"].to_numpy() if "spread" in history.h1.columns else None
    fb_points, fb_source = resolve_fallback_points(raw, cost.fallback_spread_points)
    obs = observed_spread(raw)
    h1_times = bar_open_times(history.h1)
    span = span_indices(outcomes, bars)
    lo, hi = (span[0], span[1] + 1) if span else (0, 0)
    fallback = SpreadFallback(
        points=fb_points, source=fb_source,
        observed_from=None if obs.first_index is None else h1_times[obs.first_index].to_pydatetime(),
        observed_to=None if obs.last_index is None else h1_times[obs.last_index].to_pydatetime(),
        observed_bars=obs.count, observed_median=obs.median,
        fallback_bars=int(bars.spread.fallback[lo:hi].sum()), zero_bars=int(bars.spread.unfilled[lo:hi].sum()),
        total_bars=hi - lo,
    )
    labels = [spread_label_fa(fallback) if x == HISTORICAL_SPREAD_LABEL_FA else x for x in cost.labels_fa()]
    if provisional:
        labels.insert(0, PROVISIONAL_LABEL_FA)
    items = [build_evaluated_item(o, times_ns, None if flags is None else flags[k]) for k, o in enumerate(outcomes)]
    return _Evaluated(
        eff_start=eff_start, eff_end=eff_end, items=items, total_candidates=len(scan.candidates),
        has_bars=bool(len(times_ns)),
        evaluation=SetupsEvaluation(available=True, cost_model=cost, spread_fallback=fallback, labels_fa=labels),
        summary=SetupsSummary(**summarize_setups(outcomes, account.balance)), backtest_window=bt,
    )


def _no_evaluation(message: str) -> SetupsEvaluation:
    return SetupsEvaluation(available=False, message_fa=message)


def _no_window(code: str, message: str) -> BacktestWindowOut:
    return BacktestWindowOut(from_=None, to=None, available=False, code=code, message_fa=message)


@router.get("/setups", response_model=SetupsResponse, response_model_by_alias=True,
            responses={404: {"description": "Symbol/strategy not found"}, 409: {"description": "Stored params invalid"},
                       422: {"description": "Invalid parameters"}, 503: {"description": "DB or cache unavailable"}})
def get_setups(
    request: Request,
    symbol: Annotated[str, Query(min_length=1, max_length=32)],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    service: MarketDataService = Depends(get_market_data),
    db: EngineConnection = Depends(get_db),
    registry: StrategyRegistry = Depends(get_registry),
    chart_cache: ChartCache = Depends(get_chart_cache),
) -> SetupsResponse:
    name = _check_symbol(service, symbol)
    start, end = _utc(from_), _utc(to)
    _check_range(start, end)
    active = _active_params(db, registry)
    account = AccountSettingsRepo(db).get()
    cache = service.cache
    try:
        spec_entry = cache.read_spec(name)
    except (OSError, ValueError) as exc:
        logger.warning("chart setups: cached spec of %s is unreadable: %s", name, exc)
        spec_entry = None
    spec = spec_entry[0] if spec_entry else None
    if is_dev_mode():
        logger.debug("GET /chart/setups %s from=%s to=%s params v%d hash=%s account=%s spec=%s", name, iso_z(start),
                     iso_z(end), active.record_version, active.params_hash[:12], account.model_dump(),
                     "cached" if spec else "missing")
    h1_sig, h4_sig = _file_sig(cache, name, Timeframe.H1), _file_sig(cache, name, Timeframe.H4)
    info = _info(name, active)
    if h1_sig is None or h4_sig is None:
        missing = "H1" if h1_sig is None else "H4"
        message = f"داده {missing} نماد {name} در کش نیست."
        return SetupsResponse(**info, from_=iso_z(start), to=iso_z(end), account=account, symbol_spec=spec, count=0,
                              status_counts=_status_counts([]), setups=[], message_fa=message,
                              evaluation=_no_evaluation(message), summary=SetupsSummary(**counts_only_summary([])),
                              backtest_window=_no_window("no_data", message))

    evaluated: _Evaluated | None = None
    eval_message = EVAL_SPEC_MISSING_FA
    eval_code = "spec_missing"
    if spec is not None:
        started = time.perf_counter()
        try:
            evaluated = _evaluate_setups(name, active, account, cache, chart_cache, start, end, _provisional(request))
        except HistoryUnavailable as exc:
            eval_message, eval_code = exc.message_fa, exc.code
            if is_dev_mode():
                logger.debug("chart setups %s: evaluation unavailable: %s", name, exc.code)
        except Exception as exc:  # evaluation failures never fail the chart request
            logger.warning("chart setups %s: evaluation failed: %s: %s", name, type(exc).__name__, exc)
            if is_dev_mode():
                logger.debug("chart setups: evaluation traceback", exc_info=True)
            eval_message, eval_code = EVAL_FAILED_FA, "evaluation_failed"
        if is_dev_mode() and evaluated is not None:
            logger.debug("chart setups %s: evaluated %d setup(s) in %.3f s, summary=%s, backtest window=%s", name,
                         len(evaluated.items), time.perf_counter() - started, evaluated.summary.model_dump(),
                         evaluated.backtest_window.model_dump())

    if evaluated is not None:
        items = evaluated.items
        eff_start, eff_end = evaluated.eff_start, evaluated.eff_end
        total, has_bars = evaluated.total_candidates, evaluated.has_bars
        evaluation, summary, window = evaluated.evaluation, evaluated.summary, evaluated.backtest_window
        hit = None
    else:
        key = ("setups", str(cache.data_dir), name, h1_sig, h4_sig, active.params_hash, account.rr)

        def compute() -> _Scan:
            h1 = _read_frame(cache, name, Timeframe.H1)
            h4 = _read_frame(cache, name, Timeframe.H4)
            h1 = h1 if h1 is not None else empty_frame()
            h4 = h4 if h4 is not None else empty_frame()
            candidates = active.strategy.scan(h1, h4, active.params, account, symbol=name)
            decision_ns = np.array([pd.Timestamp(c.decision_time_utc).as_unit("ns").value for c in candidates],
                                   dtype=np.int64)
            return _Scan(h1=h1, times_ns=pd.DatetimeIndex(h1["time"]).as_unit("ns").asi8, candidates=candidates,
                         decision_ns=decision_ns)

        scan, hit = chart_cache.get_or_compute(key, compute)
        last_decision = pd.Timestamp(int(scan.times_ns[-1]), tz="UTC") + _H1 if len(scan.times_ns) else None
        eff_start, eff_end, mask = _window(scan.decision_ns, start, end, last_decision)
        items = [build_setup_item(scan.candidates[i], scan.h1, scan.times_ns, account, spec)
                 for i in np.flatnonzero(mask).tolist()]
        total, has_bars = len(scan.candidates), bool(len(scan.times_ns))
        evaluation = _no_evaluation(eval_message)
        summary = SetupsSummary(**counts_only_summary([s.status for s in items]))
        window = _no_window(eval_code, eval_message)
    counts = _status_counts(items)
    if is_dev_mode():
        logger.debug("chart setups %s: %s, scanned=%d in window=%d status=%s", name,
                     "evaluated (simulator rules)" if hit is None else f"legacy path, cache {'hit' if hit else 'miss'}",
                     total, len(items), counts)
        for item in items:
            out = item.outcome
            logger.debug("setup %s %s %s: status=%s entry=%s (bid open %s, spread %s %s) sl=%s tp=%s volume=%s "
                         "outcome=%s exit=%s net=%s R=%s backtest=%s reason=%s", item.id, item.setup_type,
                         item.direction, item.status, item.entry, item.entry_bid_open, item.spread_at_entry_points,
                         item.entry_spread_source, item.stop_loss, item.take_profit, item.volume,
                         out.result if out else None, out.exit_price if out else None, out.net_pnl if out else None,
                         out.r_multiple if out else None,
                         None if item.backtest is None else (item.backtest.traded, item.backtest.reason),
                         item.rejection_reason_fa or item.volume_note_fa or "-")
    message = None
    if not total and has_bars:
        message = "در کل تاریخچه کش هیچ ستاپی پیدا نشد (ممکن است داده برای ساخت کانال کافی نباشد)."
    return SetupsResponse(**info, from_=iso_z(eff_start), to=iso_z(eff_end), account=account, symbol_spec=spec,
                          count=len(items), status_counts=counts, setups=items, message_fa=message,
                          evaluation=evaluation, summary=summary, backtest_window=window)


def _status_counts(items: list[SetupItem]) -> dict[str, int]:
    counts = {"accepted": 0, "rejected": 0, "pending_entry": 0}
    for item in items:
        counts[item.status] += 1
    return counts


__all__ = ["ChartCache", "build_evaluated_item", "build_setup_item", "compute_h4_channel", "router", "setup_id"]
