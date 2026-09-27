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

* ``GET /chart/setups?symbol&from&to`` -- ``StdDevChannelStrategy.scan`` over the full history, filtered by
  decision time; entry = open of the next cached H1 bar (``levels.entry_price_for``), SL/TP resolved at
  that entry (``levels.resolve_trade_levels``), volume from ``risk.sizing.size_from_spec`` with the
  CURRENT account settings and the cached symbol spec.

Warm-up consistency: Wilder ATR is a recursion over the whole series and the channel needs ``n`` H4
bars, so everything is computed on the FULL cached history (the same input a backtest or scan over the
full cache gets) and only then filtered to ``[from, to]``. A sub-range therefore returns bit-identical
numbers to the full range for the overlapping bars (tests/test_chart_routes.py).

A small in-process LRU (:class:`ChartCache`, ``app.state.chart_cache``) keeps the computed series,
keyed by symbol, cache-file identity (file id/``mtime_ns``/size of the Parquet files), params hash and -- for
setups -- the account R:R (the only account field a candidate carries). Any cache update or params
change gives a new key; the least recently used entries are dropped beyond ``maxsize``.

Stored params come from the active params version (``strategy.context.load_active_params``); params
that no longer fit the schema -> 409 ``stored_params_invalid``. Errors use
``{"detail": {"code", "message_fa", "errors_fa"}}``. All times are ISO-8601 UTC with ``Z``.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

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
from ..strategy.context import DEFAULT_STRATEGY, load_active_params
from ..strategy.params import ParamValue
from ..strategy.registry import StrategyRegistry, UnknownStrategyError
from ..strategy.signal import SignalCandidate
from . import api_error, get_db, get_market_data
from .strategies import get_registry

logger = get_logger(__name__)
router = APIRouter(prefix="/chart", tags=["chart"])

DEFAULT_WINDOW = timedelta(days=30)
CHART_CACHE_SIZE = 8
_H1 = timedelta(hours=1)

Direction = Literal["up", "down", "flat", "none"]
SetupStatus = Literal["accepted", "rejected", "pending_entry"]

NOTE_REJECTIONS_FA = (
    "وضعیت «رد شده» فقط برای رد هنگام ورود (باز شدن کندل بعد آن طرف حد ضرر) و رد حجم (کمتر از حداقل حجم "
    "یا مارجین بیشتر از موجودی) است. ستاپ‌هایی که خود سیستم کنار می‌گذارد (مثل تعارض دو جهت) در این فهرست نیستند."
)
PENDING_NOTE_FA = "کندل بعد از کندل تایید هنوز در کش نیست؛ قیمت ورود، حد سود و حجم پس از باز شدن آن کندل مشخص می‌شوند."
SPEC_MISSING_FA = "مشخصات نماد در کش نیست؛ حجم قابل محاسبه نیست (یک بار با اتصال به MT5 فهرست نمادها را بگیرید)."


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
    entry: float | None  # open of bar t+1
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


def build_setup_item(
    candidate: SignalCandidate,
    h1: pd.DataFrame,
    times_ns: np.ndarray,
    account: AccountSettings,
    spec: SymbolSpec | None,
) -> SetupItem:
    """Entry / levels / sizing / status of one scanned candidate (see module docstring)."""
    conf_ns = pd.Timestamp(candidate.confirmation_bar_open_utc).as_unit("ns").value
    t = int(np.searchsorted(times_ns, conf_ns))
    if t >= len(times_ns) or times_ns[t] != conf_ns:  # pragma: no cover - scan only returns bars of h1
        raise RuntimeError(f"confirmation bar {candidate.confirmation_bar_open_utc} not in the H1 frame")
    extra = dict(candidate.extra)
    base: dict[str, Any] = dict(
        id=setup_id(candidate), symbol=candidate.symbol, setup_type=candidate.setup.value,
        setup_title_fa=SETUP_TITLE_FA[candidate.setup], direction=candidate.direction, pattern=candidate.pattern,
        line=candidate.line, line_value=extra.get("line_value"), channel_direction=extra.get("channel_direction"),
        confirmation_bar_time=iso_z(pd.Timestamp(candidate.confirmation_bar_open_utc)),
        decision_time=iso_z(pd.Timestamp(candidate.decision_time_utc)), stop_loss=candidate.stop_loss,
        rr=candidate.rr, reference_price=candidate.reference_price,
        indicative_take_profit=candidate.indicative_take_profit, reason_fa=candidate.reason_fa, indicators=extra,
        entry_time=None, entry=None, take_profit=None, risk_distance=None, volume=None, actual_risk=None,
        margin=None, risk_amount=None, volume_note_fa=None, sizing_warnings_fa=[], rejection_reason_fa=None,
    )
    entry = entry_price_for(h1, t)
    if entry is None:
        base.update(status="pending_entry", volume_note_fa=PENDING_NOTE_FA)
        return SetupItem(**base)
    base["entry"] = entry
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


@router.get("/setups", response_model=SetupsResponse, response_model_by_alias=True,
            responses={404: {"description": "Symbol/strategy not found"}, 409: {"description": "Stored params invalid"},
                       422: {"description": "Invalid parameters"}, 503: {"description": "DB or cache unavailable"}})
def get_setups(
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
        return SetupsResponse(**info, from_=iso_z(start), to=iso_z(end), account=account, symbol_spec=spec, count=0,
                              status_counts=_status_counts([]), setups=[],
                              message_fa=f"داده {missing} نماد {name} در کش نیست.")
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
    # Default window ends at the close of the last cached H1 bar, so a setup on the last bar is included.
    last_decision = pd.Timestamp(int(scan.times_ns[-1]), tz="UTC") + _H1 if len(scan.times_ns) else None
    eff_start, eff_end, mask = _window(scan.decision_ns, start, end, last_decision)
    items = [build_setup_item(scan.candidates[i], scan.h1, scan.times_ns, account, spec)
             for i in np.flatnonzero(mask).tolist()]
    counts = _status_counts(items)
    if is_dev_mode():
        logger.debug("chart setups %s: cache %s, scanned=%d in window=%d status=%s", name, "hit" if hit else "miss",
                     len(scan.candidates), len(items), counts)
        for item in items:
            logger.debug("setup %s %s %s: status=%s entry=%s sl=%s tp=%s volume=%s reason=%s", item.id,
                         item.setup_type, item.direction, item.status, item.entry, item.stop_loss, item.take_profit,
                         item.volume, item.rejection_reason_fa or item.volume_note_fa or "-")
    message = None
    if not scan.candidates and len(scan.times_ns):
        message = "در کل تاریخچه کش هیچ ستاپی پیدا نشد (ممکن است داده برای ساخت کانال کافی نباشد)."
    return SetupsResponse(**info, from_=iso_z(eff_start), to=iso_z(eff_end), account=account, symbol_spec=spec,
                          count=len(items), status_counts=counts, setups=items, message_fa=message)


def _status_counts(items: list[SetupItem]) -> dict[str, int]:
    counts = {"accepted": 0, "rejected": 0, "pending_entry": 0}
    for item in items:
        counts[item.status] += 1
    return counts


__all__ = ["ChartCache", "build_setup_item", "compute_h4_channel", "router", "setup_id"]
