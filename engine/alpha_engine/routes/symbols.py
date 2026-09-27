"""``GET /symbols`` -- configured symbols with their contract spec (live from MT5, else cached)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from ..data.symbols import SymbolSpec
from ..logging_setup import get_logger, is_dev_mode
from ..market_data import MarketDataService
from . import get_market_data

logger = get_logger(__name__)
router = APIRouter(tags=["market data"])


class SymbolItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    source: Literal["mt5", "cache", "none"]
    spec: SymbolSpec | None
    fetched_at_utc: str | None
    error: str | None


class SymbolsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mt5_state: str
    symbols: list[SymbolItem]


@router.get("/symbols", response_model=SymbolsResponse)
def list_symbols(service: MarketDataService = Depends(get_market_data)) -> SymbolsResponse:
    items = [
        SymbolItem(symbol=r.symbol, source=r.source, spec=r.spec, fetched_at_utc=r.fetched_at_utc, error=r.error)
        for r in service.symbol_infos()
    ]
    state = service.adapter.status().state if service.adapter is not None else "not_initialized"
    if is_dev_mode():
        logger.debug("GET /symbols: mt5=%s %s", state, [(i.symbol, i.source) for i in items])
    return SymbolsResponse(mt5_state=state, symbols=items)
