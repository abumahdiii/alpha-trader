"""HTTP routes beyond the skeleton's ``/health`` and ``/shutdown`` (registered in ``create_app``)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from ..market_data import MarketDataService


def get_market_data(request: Request) -> MarketDataService:
    service = getattr(request.app.state, "market_data", None)
    if service is None:  # pragma: no cover - create_app always sets it
        raise HTTPException(status_code=503, detail="market data service not available")
    return service
