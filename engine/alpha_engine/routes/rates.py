"""``/rates`` routes -- cached OHLCV in UTC.

* ``GET /rates`` -- bars of a window (refreshed from MT5 first when the cache does not reach ``to``).
  Each bar also carries ``server_time``: the bar open on the broker's server clock (``YYYY.MM.DD HH:MM``,
  as in MT5's Data Window), converted from UTC with the offset model stored in the cache metadata
  (``OffsetModel.parse(meta.offset_model).utc_to_server`` -- never a new fit, never MT5).
* ``GET /rates/meta`` -- cache metadata.
* ``GET /rates/gaps`` -- the full-history gap list of the cached series (cache only, no MT5).
* ``POST /rates/update`` -- incremental, append-only cache update from MT5 (read-only ``copy_rates``).

All times are ISO-8601 UTC with a ``Z`` suffix. ``from``/``to`` without an offset are taken as UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..data.cache import CacheError, CacheMeta
from ..data.gaps import GAP_KINDS, Gap, find_gaps
from ..data.schema import Timeframe, iso_z, to_epoch_seconds
from ..data.symbols import InvalidSymbolName
from ..data.timezone import OffsetError, OffsetModel
from ..logging_setup import get_logger, is_dev_mode
from ..market_data import MarketDataService, SymbolNotConfigured
from ..mt5_adapter import Mt5Error
from . import api_error, get_market_data

logger = get_logger(__name__)
router = APIRouter(tags=["market data"])

SERVER_TIME_FORMAT = "%Y.%m.%d %H:%M"  # MT5 Data Window style


class Bar(BaseModel):
    time: str  # bar open time, UTC, "2025-01-05T23:00:00Z"
    server_time: str | None  # bar open on the broker server clock, "2025.01.06 01:00"; None if no offset model
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int
    real_volume: int


class RatesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    timeframe: str
    from_: str = Field(serialization_alias="from")
    to: str
    source: Literal["cache", "mt5"]
    stale: bool
    offset_model: str | None
    count: int
    bars: list[Bar]
    gaps: list[Gap]
    gap_counts: dict[str, int]
    message: str | None = None


class RatesMetaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: str
    cached: bool
    rows: int
    first_bar_utc: str | None
    last_bar_utc: str | None
    first_available_utc: str | None
    requested_start_utc: str | None
    history_short: bool | None
    offset_model: str | None
    source: str | None
    fetched_at_utc: str | None


def server_times(times: pd.Series, offset_label: str | None) -> list[str | None]:
    """Broker server-clock open times (``YYYY.MM.DD HH:MM``) from the cached offset model label.

    Worked example (``us_dst(+2)``): ``2025-01-05T23:00Z`` -> ``2025.01.06 01:00``;
    ``2025-07-06T22:00Z`` -> ``2025.07.07 01:00`` (US DST: +3). Unknown/unparsable model -> ``None``s.
    """
    if not len(times):
        return []
    model: OffsetModel | None = None
    if offset_label:
        try:
            model = OffsetModel.parse(offset_label)
        except ValueError:
            logger.warning("cached offset model %r is not parsable; server_time omitted", offset_label)
    if model is None:
        return [None] * len(times)
    server = model.utc_to_server(to_epoch_seconds(times))
    text = pd.to_datetime(np.asarray(server, dtype="int64"), unit="s").strftime(SERVER_TIME_FORMAT)
    if is_dev_mode():
        logger.debug("server_time via %s: first %s -> %s", model.label, iso_z(times.iloc[0]), text[0])
    return list(text)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _parse_tf(timeframe: str) -> Timeframe:
    try:
        return Timeframe.parse(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _check_symbol(service: MarketDataService, symbol: str) -> str:
    try:
        return service.check_symbol(symbol)
    except InvalidSymbolName as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except SymbolNotConfigured as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get(
    "/rates",
    response_model=RatesResponse,
    response_model_by_alias=True,
    responses={404: {"description": "Symbol is not configured"}, 422: {"description": "Invalid parameters"}},
)
def get_rates(
    symbol: Annotated[str, Query(min_length=1, max_length=32)],
    timeframe: Annotated[str, Query()],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    service: MarketDataService = Depends(get_market_data),
) -> RatesResponse:
    tf = _parse_tf(timeframe)
    name = _check_symbol(service, symbol)
    try:
        result = service.get_rates(name, tf, _as_utc(from_), _as_utc(to))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    frame = result.frame
    times = frame["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist() if len(frame) else []
    server = server_times(frame["time"], result.meta.offset_model if result.meta else None)
    bars = [
        Bar(time=t, server_time=st, open=o, high=h, low=lo, close=c, tick_volume=v, spread=s, real_volume=rv)
        for t, st, o, h, lo, c, v, s, rv in zip(
            times, server, frame["open"].tolist(), frame["high"].tolist(), frame["low"].tolist(),
            frame["close"].tolist(), frame["tick_volume"].tolist(), frame["spread"].tolist(),
            frame["real_volume"].tolist(), strict=True,
        )
    ]
    gaps = result.gaps.gaps if result.gaps else []
    counts = result.gaps.counts if result.gaps else {k: 0 for k in ("weekend", "holiday", "session_break", "missing")}
    if is_dev_mode():
        logger.debug("GET /rates %s %s -> %d bars source=%s stale=%s", name, tf.value, len(bars), result.source,
                     result.stale)
    return RatesResponse(
        symbol=name, timeframe=tf.value, from_=iso_z(result.start) or "", to=iso_z(result.end) or "",
        source=result.source, stale=result.stale,
        offset_model=result.meta.offset_model if result.meta else None,
        count=len(bars), bars=bars, gaps=gaps, gap_counts=counts, message=result.message,
    )


@router.get(
    "/rates/meta",
    response_model=RatesMetaResponse,
    responses={404: {"description": "Symbol is not configured"}, 422: {"description": "Invalid parameters"}},
)
def get_rates_meta(
    symbol: Annotated[str, Query(min_length=1, max_length=32)],
    timeframe: Annotated[str, Query()],
    service: MarketDataService = Depends(get_market_data),
) -> RatesMetaResponse:
    tf = _parse_tf(timeframe)
    name = _check_symbol(service, symbol)
    meta = service.cache.read_meta(name, tf)
    if meta is None:
        return RatesMetaResponse(symbol=name, timeframe=tf.value, cached=False, rows=0, first_bar_utc=None,
                                 last_bar_utc=None, first_available_utc=None, requested_start_utc=None,
                                 history_short=None, offset_model=None, source=None, fetched_at_utc=None)
    return RatesMetaResponse(
        symbol=name, timeframe=tf.value, cached=True, rows=meta.rows, first_bar_utc=meta.first_bar_utc,
        last_bar_utc=meta.last_bar_utc, first_available_utc=meta.first_available_utc,
        requested_start_utc=meta.requested_start_utc, history_short=meta.extra.get("history_short"),
        offset_model=meta.offset_model, source=meta.source, fetched_at_utc=meta.fetched_at_utc,
    )


# --- GET /rates/gaps --------------------------------------------------------------------------------

class RatesGapsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: str
    cached: bool
    rows: int
    first_bar_utc: str | None
    last_bar_utc: str | None
    offset_model: str | None
    count: int
    gaps: list[Gap]
    gap_counts: dict[str, int]
    missing_bars_total: int
    session_break_slots: list[str]


@router.get(
    "/rates/gaps",
    response_model=RatesGapsResponse,
    responses={404: {"description": "Symbol is not configured"}, 422: {"description": "Invalid parameters"},
               503: {"description": "Cache file unreadable"}},
)
def get_rates_gaps(
    symbol: Annotated[str, Query(min_length=1, max_length=32)],
    timeframe: Annotated[str, Query()],
    service: MarketDataService = Depends(get_market_data),
) -> RatesGapsResponse:
    """Every gap of the whole cached series (``data.gaps.find_gaps``). Cache only: never calls MT5."""
    tf = _parse_tf(timeframe)
    name = _check_symbol(service, symbol)
    try:
        cached = service.cache.read(name, tf)
    except (CacheError, OSError, ValueError) as exc:
        logger.warning("GET /rates/gaps %s %s: cache unreadable: %s", name, tf.value, exc)
        raise api_error(503, "cache_unreadable", f"فایل کش {name} {tf.value} قابل خواندن نیست.") from None
    if cached is None:
        if is_dev_mode():
            logger.debug("GET /rates/gaps %s %s: no cache", name, tf.value)
        return RatesGapsResponse(symbol=name, timeframe=tf.value, cached=False, rows=0, first_bar_utc=None,
                                 last_bar_utc=None, offset_model=None, count=0, gaps=[],
                                 gap_counts={k: 0 for k in GAP_KINDS}, missing_bars_total=0, session_break_slots=[])
    frame, meta = cached
    report = find_gaps(frame, tf)
    if is_dev_mode():
        logger.debug("GET /rates/gaps %s %s: rows=%d gaps=%d counts=%s", name, tf.value, len(frame), len(report.gaps),
                     report.counts)
    return RatesGapsResponse(
        symbol=name, timeframe=tf.value, cached=True, rows=len(frame), first_bar_utc=meta.first_bar_utc,
        last_bar_utc=meta.last_bar_utc, offset_model=meta.offset_model, count=len(report.gaps), gaps=report.gaps,
        gap_counts=report.counts, missing_bars_total=report.missing_bars_total,
        session_break_slots=report.session_break_slots,
    )


# --- POST /rates/update -----------------------------------------------------------------------------

class RatesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str | None = None  # omitted -> H1 and H4


class TimeframeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeframe: str
    bars_added: int
    rows: int
    first_bar_utc: str | None
    last_bar_utc: str | None
    fetched: int  # bars returned by MT5 (includes the re-fetched last 2 cached bars)


class RatesUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    updated: list[TimeframeUpdate]
    message_fa: str


def _mt5_available(service: MarketDataService) -> bool:
    if service.mt5_connected:
        return True
    return service.adapter is not None and service.adapter.ensure_connected()


def _update_precheck(service: MarketDataService, name: str, tf: Timeframe) -> CacheMeta:
    """The update must be incremental: an MT5-sourced cache must exist. Seed data or no cache would make
    ``MarketDataService.update`` download and REWRITE the whole series -- that is ``fetch_history``'s job."""
    meta = service.cache.read_meta(name, tf)
    if meta is None or not meta.last_bar_utc:
        raise api_error(409, "no_cache", f"برای {name} {tf.value} هنوز کشی وجود ندارد؛ "
                        "ابتدا دریافت تاریخچه (fetch_history) را اجرا کنید.")
    if meta.source != "mt5":
        raise api_error(409, "not_incremental",
                        f"کش {name} {tf.value} داده آزمایشی ({meta.source}) است؛ به‌روزرسانی افزایشی ممکن نیست. "
                        "برای جایگزینی آن دریافت کامل تاریخچه (fetch_history) را اجرا کنید.")
    return meta


@router.post(
    "/rates/update",
    response_model=RatesUpdateResponse,
    responses={404: {"description": "Symbol is not configured"}, 409: {"description": "No incremental update possible"},
               422: {"description": "Invalid parameters"}, 503: {"description": "MT5 not available / update failed"}},
)
def post_rates_update(
    body: RatesUpdateRequest,
    service: MarketDataService = Depends(get_market_data),
) -> RatesUpdateResponse:
    """Append the bars closed since the last cached bar (read-only ``copy_rates``; existing bars are kept).

    Calls ``MarketDataService.update`` with ``start`` = the series' stored ``requested_start_utc``, so it
    never backfills; an offset-model change would force a full rewrite, so it is refused (409) instead.
    """
    try:
        tf_list = [Timeframe.parse(body.timeframe)] if body.timeframe else [Timeframe.H1, Timeframe.H4]
    except ValueError:
        raise api_error(422, "invalid_timeframe",
                        f"تایم‌فریم «{body.timeframe}» پشتیبانی نمی‌شود (H1 یا H4).") from None
    try:
        name = service.check_symbol(body.symbol)
    except InvalidSymbolName:
        raise api_error(422, "invalid_symbol", f"نام نماد «{body.symbol}» معتبر نیست.") from None
    except SymbolNotConfigured:
        raise api_error(404, "symbol_not_configured", f"نماد «{body.symbol}» در فهرست نمادهای engine نیست.") from None
    if is_dev_mode():
        logger.debug("POST /rates/update %s %s", name, [t.value for t in tf_list])
    metas = {tf: _update_precheck(service, name, tf) for tf in tf_list}
    if not _mt5_available(service):
        if is_dev_mode():
            logger.debug("POST /rates/update %s: MT5 not available", name)
        raise api_error(503, "mt5_unavailable", "اتصال به MT5 برقرار نیست؛ به‌روزرسانی انجام نشد. داده کش دست نخورد.")
    done: list[TimeframeUpdate] = []
    for tf in tf_list:
        meta = metas[tf]
        try:
            model = service.effective_model()
            if meta.offset_model != model.label:
                raise api_error(409, "offset_model_changed",
                                f"مدل اختلاف ساعت سرور ({model.label}) با مدل کش {name} {tf.value} "
                                f"({meta.offset_model}) فرق دارد؛ به‌روزرسانی افزایشی ممکن نیست. "
                                "دریافت کامل تاریخچه را اجرا کنید.")
            start = pd.Timestamp(meta.requested_start_utc or meta.first_bar_utc).tz_convert("UTC").to_pydatetime()
            result = service.update(name, tf, start=start)
        except (Mt5Error, OffsetError, CacheError) as exc:
            logger.warning("POST /rates/update %s %s failed: %s", name, tf.value, exc)
            if is_dev_mode():
                logger.debug("rates update failure", exc_info=True)
            done_fa = "، ".join(f"{d.timeframe}: {d.bars_added}" for d in done)
            raise api_error(503, "update_failed", f"به‌روزرسانی {name} {tf.value} از MT5 ناموفق بود.",
                            [str(exc)] + ([f"انجام‌شده پیش از خطا: {done_fa}"] if done else [])) from None
        if result.full:  # pragma: no cover - excluded by the pre-checks above
            logger.warning("POST /rates/update %s %s ran a FULL refetch unexpectedly", name, tf.value)
        done.append(TimeframeUpdate(timeframe=tf.value, bars_added=result.rows_added, rows=result.rows_after,
                                    first_bar_utc=result.meta.first_bar_utc, last_bar_utc=result.meta.last_bar_utc,
                                    fetched=result.fetched))
        if is_dev_mode():
            logger.debug("POST /rates/update %s %s: +%d bars (fetched %d), rows %d, last %s", name, tf.value,
                         result.rows_added, result.fetched, result.rows_after, result.meta.last_bar_utc)
    parts = "، ".join(f"{d.timeframe}: {d.bars_added} کندل جدید (آخرین کندل {d.last_bar_utc})" for d in done)
    return RatesUpdateResponse(symbol=name, updated=done, message_fa=f"کش {name} به‌روز شد — {parts}.")
