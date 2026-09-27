"""``GET /rates`` and ``GET /rates/meta`` -- cached OHLCV in UTC, refreshed from MT5 when needed.

All times are ISO-8601 UTC with a ``Z`` suffix. ``from``/``to`` without an offset are taken as UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..data.gaps import Gap
from ..data.schema import Timeframe, iso_z
from ..data.symbols import InvalidSymbolName
from ..logging_setup import get_logger, is_dev_mode
from ..market_data import MarketDataService, SymbolNotConfigured
from . import get_market_data

logger = get_logger(__name__)
router = APIRouter(tags=["market data"])


class Bar(BaseModel):
    time: str  # bar open time, UTC, "2025-01-05T23:00:00Z"
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
    bars = [
        Bar(time=t, open=o, high=h, low=lo, close=c, tick_volume=v, spread=s, real_volume=rv)
        for t, o, h, lo, c, v, s, rv in zip(
            times, frame["open"].tolist(), frame["high"].tolist(), frame["low"].tolist(), frame["close"].tolist(),
            frame["tick_volume"].tolist(), frame["spread"].tolist(), frame["real_volume"].tolist(), strict=True,
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
