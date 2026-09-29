"""Market-data service: adapter + broker offset + Parquet cache + gaps, shared by routes and tools.

Cache-first: reads always come from the cache; MT5 is only used to bring the cache up to date
(incremental: from the last cached bar minus 2 bars to now, newer data wins on overlap).

A full (re)fetch happens when there is no cache yet, when the cached series was converted with a
different offset model or comes from seed data, or when an earlier start than ever requested is asked
for (backfill).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from .config import Settings
from .data.cache import CacheMeta, OhlcvCache, merge_frames
from .data.gaps import GapReport, find_gaps
from .data.schema import Timeframe, empty_frame, iso_z, to_epoch_seconds
from .data.symbols import SymbolSpec, validate_symbol_name
from .data.timezone import LiveOffset, OffsetError, OffsetFit, OffsetModel, fit_offset_model
from .logging_setup import get_logger, is_dev_mode
from .mt5_adapter import DEFAULT_YEARS, Mt5Adapter, Mt5Error, UnknownSymbolError, history_start_for

logger = get_logger(__name__)

FIT_SYMBOL = "XAUUSD.x"
FIT_YEARS = 2.0  # enough to contain several DST transitions
HISTORY_SHORT_SLACK = timedelta(days=7)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> pd.Timestamp | None:
    return pd.Timestamp(value).tz_convert("UTC") if value else None


class SymbolNotConfigured(LookupError):
    pass


@dataclass
class UpdateResult:
    symbol: str
    timeframe: Timeframe
    full: bool
    rows_before: int
    rows_after: int
    fetched: int
    meta: CacheMeta
    history_short: bool
    chunks: int = 0
    empty_chunks: int = 0
    failed_chunks: int = 0
    dropped_forming: int = 0

    @property
    def rows_added(self) -> int:
        return self.rows_after - self.rows_before


@dataclass
class RatesResult:
    symbol: str
    timeframe: Timeframe
    start: pd.Timestamp
    end: pd.Timestamp
    frame: pd.DataFrame
    source: str  # "cache" | "mt5"
    stale: bool
    meta: CacheMeta | None
    gaps: GapReport | None
    message: str | None = None


@dataclass
class SymbolInfoResult:
    symbol: str
    source: str  # "mt5" | "cache" | "none"
    spec: SymbolSpec | None
    fetched_at_utc: str | None = None
    error: str | None = None


@dataclass
class MarketDataService:
    settings: Settings
    adapter: Mt5Adapter | None
    cache: OhlcvCache
    clock: Any = utc_now
    update_min_interval: float = 15.0
    _last_update: dict[tuple[str, Timeframe], float] = field(default_factory=dict)

    @classmethod
    def create(cls, settings: Settings, adapter: Mt5Adapter | None, **kwargs: Any) -> MarketDataService:
        return cls(settings=settings, adapter=adapter, cache=OhlcvCache(settings.data_dir), **kwargs)

    # --- helpers ----------------------------------------------------------------------------------

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.settings.engine_symbols

    def check_symbol(self, symbol: str) -> str:
        name = validate_symbol_name(symbol)
        if name not in self.symbols:
            raise SymbolNotConfigured(f"symbol {name!r} is not configured (ENGINE_SYMBOLS: {', '.join(self.symbols)})")
        return name

    @property
    def mt5_connected(self) -> bool:
        return self.adapter is not None and self.adapter.connected

    def _try_connect(self) -> bool:
        return self.adapter is not None and self.adapter.ensure_connected()

    @property
    def fit_symbol(self) -> str:
        return FIT_SYMBOL if FIT_SYMBOL in self.symbols else self.symbols[0]

    # --- broker offset ----------------------------------------------------------------------------

    def refit_offset(self, years: float = FIT_YEARS) -> OffsetFit:
        """Fit the offset model on the fit symbol's H1 server-time bars (needs MT5)."""
        if self.adapter is None:
            raise OffsetError("no MT5 adapter")
        self.adapter.require_connected()
        now = self.clock()
        now_epoch = int(now.timestamp())
        start = int(history_start_for(years, now).timestamp())
        raw = self.adapter.fetch_raw(self.fit_symbol, Timeframe.H1, start - 86400, now_epoch + 2 * 86400)
        live: LiveOffset | None
        try:
            live = self.adapter.live_offset(self.fit_symbol)
        except Mt5Error:
            live = None
        epochs = raw.frame["time"].to_numpy(dtype="int64")
        # the bar that may still be forming has no complete week close; the fit ignores the last week anyway
        fit = fit_offset_model(epochs, Timeframe.H1.seconds, live=live,
                               override_hours=self.settings.mt5_server_utc_offset, now_utc=now)
        self.cache.write_offset_fit(fit)
        if is_dev_mode():
            logger.debug("offset model stored: %s (source=%s, match=%.2f over %d weeks)",
                         fit.effective_label, fit.source, fit.match_ratio, fit.weeks)
        if fit.low_confidence:
            logger.warning("broker offset fit has low confidence (%.0f%%); consider MT5_SERVER_UTC_OFFSET",
                           fit.match_ratio * 100)
        return fit

    def offset_fit(self) -> OffsetFit | None:
        return self.cache.read_offset_fit()

    def effective_model(self) -> OffsetModel:
        override = self.settings.mt5_server_utc_offset
        if override is not None:
            return OffsetModel.fixed(override)
        fit = self.cache.read_offset_fit()
        if fit is not None and fit.source != "override":
            return fit.effective
        if self.mt5_connected:
            return self.refit_offset().effective
        raise OffsetError("broker offset unknown: run fetch_history with MT5 connected or set MT5_SERVER_UTC_OFFSET")

    # --- cache update -----------------------------------------------------------------------------

    def update(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        *,
        years: float = DEFAULT_YEARS,
        start: datetime | None = None,
        force_full: bool = False,
        now_utc: datetime | None = None,
    ) -> UpdateResult:
        """Bring the cache for ``symbol``/``timeframe`` up to date from MT5 (needs a connection).

        ``now_utc`` (live signals): the "now" that decides which bar is still forming -- the broker's server time
        instead of this service's clock (see ``Mt5Adapter.fetch_rates``)."""
        tf = Timeframe.parse(timeframe)
        name = validate_symbol_name(symbol)
        if self.adapter is None:
            raise Mt5Error("no MT5 adapter")
        self.adapter.require_connected()
        now = now_utc if now_utc is not None else self.clock()
        target_start = pd.Timestamp(start or history_start_for(years, now)).tz_convert("UTC")
        model = self.effective_model()
        with self.cache.lock(name, tf):
            existing = self.cache.read(name, tf)
            old_frame, old_meta = existing if existing else (None, None)
            requested_before = _parse_iso(old_meta.requested_start_utc) if old_meta else None
            reason = None
            if force_full:
                reason = "forced"
            elif old_meta is None or old_frame is None or old_frame.empty:
                reason = "no cache"
            elif old_meta.offset_model != model.label:
                reason = f"offset model changed {old_meta.offset_model} -> {model.label}"
            elif old_meta.source != "mt5":
                reason = f"cache source is {old_meta.source}"
            elif requested_before is not None and target_start < requested_before - timedelta(days=1):
                reason = "backfill"
            full = reason is not None
            if full:
                fetch_from = target_start
            else:
                last_open = old_frame["time"].iloc[-1]
                fetch_from = last_open - 2 * tf.delta
            if is_dev_mode():
                logger.debug("update %s %s: %s from %s (%s)", name, tf.value, "full" if full else "incremental",
                             iso_z(fetch_from), reason or "cache present")
            result = self.adapter.fetch_rates(name, tf, fetch_from.to_pydatetime(), None, model, now_utc=now_utc)
            if full:
                merged = merge_frames(None, result.frame)
                first_available = iso_z(merged["time"].iloc[0]) if len(merged) else None
                requested = target_start
            else:
                merged = merge_frames(old_frame, result.frame)
                first_available = old_meta.first_available_utc
                requested = min(target_start, requested_before) if requested_before is not None else target_start
            fa = _parse_iso(first_available)
            history_short = bool(fa is not None and fa > requested + HISTORY_SHORT_SLACK)
            meta = CacheMeta(
                symbol=name, timeframe=tf.value, source="mt5", offset_model=model.label,
                first_available_utc=first_available, requested_start_utc=iso_z(requested),
                fetched_at_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                extra={"history_short": history_short, "last_fetch_bars": result.rows,
                       "last_fetch_full": full},
            )
            meta = self.cache.write(name, tf, merged, meta, lock=False)
        self._last_update[(name, tf)] = time.monotonic()
        out = UpdateResult(
            symbol=name, timeframe=tf, full=full, rows_before=0 if old_frame is None or full else len(old_frame),
            rows_after=len(merged), fetched=result.rows, meta=meta, history_short=history_short,
            chunks=result.chunks, empty_chunks=result.empty_chunks, failed_chunks=result.failed_chunks,
            dropped_forming=result.dropped_forming,
        )
        if is_dev_mode():
            logger.debug("update %s %s done: %d -> %d rows (fetched %d, forming dropped %d), first_available=%s",
                         name, tf.value, out.rows_before, out.rows_after, out.fetched, out.dropped_forming,
                         first_available)
        return out

    # --- reads ------------------------------------------------------------------------------------

    @staticmethod
    def _covers(meta: CacheMeta | None, tf: Timeframe, until: pd.Timestamp) -> bool:
        """Does the cache contain the last bar that could be complete at ``until``?

        The newest complete bar opened in ``(until - 2*tf, until - tf]``; comparing against the lower
        bound (instead of a UTC-aligned floor) also works for H4, whose UTC grid shifts with DST.
        Example (H1): until 12:00Z -> covered iff last cached open > 10:00Z, i.e. the 11:00Z bar is there.
        """
        if meta is None or not meta.last_bar_utc:
            return False
        last = _parse_iso(meta.last_bar_utc)
        return last > until - 2 * tf.delta

    def get_rates(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> RatesResult:
        name = self.check_symbol(symbol)
        tf = Timeframe.parse(timeframe)
        now = pd.Timestamp(self.clock()).tz_convert("UTC")
        end_ts = pd.Timestamp(end).tz_convert("UTC") if end is not None else now
        start_ts = pd.Timestamp(start).tz_convert("UTC") if start is not None else end_ts - pd.Timedelta(days=30)
        if start_ts >= end_ts:
            raise ValueError("'from' must be earlier than 'to'")
        until = min(end_ts, now)

        meta = self.cache.read_meta(name, tf)
        source, message, updated = "cache", None, False
        requested_before = _parse_iso(meta.requested_start_utc) if meta else None
        needs_backfill = meta is not None and requested_before is not None and start_ts < requested_before
        if not self._covers(meta, tf, until) or needs_backfill:
            last_try = self._last_update.get((name, tf))
            throttled = last_try is not None and time.monotonic() - last_try < self.update_min_interval \
                and not needs_backfill
            if throttled:
                updated = self.mt5_connected  # we just asked MT5; its answer is still current
            elif self.mt5_connected or self._try_connect():
                try:
                    # First fetch from a request covers only the requested range; the 5-year download is
                    # fetch_history's job (it would block this request for too long).
                    target = min(start_ts, requested_before) if requested_before is not None else start_ts
                    self.update(name, tf, start=target.to_pydatetime())
                    source, updated = "mt5", True
                except (Mt5Error, OffsetError) as exc:
                    self._last_update[(name, tf)] = time.monotonic()
                    message = f"update from MT5 failed: {exc}"
                    logger.warning("rates update %s %s failed: %s", name, tf.value, exc)
            else:
                message = "MT5 is not connected; serving cached data"

        cached = self.cache.read(name, tf)
        if cached is None:
            return RatesResult(name, tf, start_ts, end_ts, empty_frame(), source, True, None, None,
                               message or "no cached data for this symbol/timeframe")
        frame, meta = cached
        stale = not updated and not self._covers(meta, tf, until)
        report = find_gaps(frame, tf)
        epochs = to_epoch_seconds(frame["time"])
        mask = (epochs >= int(start_ts.timestamp())) & (epochs <= int(end_ts.timestamp()))
        window = frame.loc[np.asarray(mask)].reset_index(drop=True)
        window_gaps = GapReport(
            timeframe=tf.value, gaps=report.within(start_ts, end_ts), counts={},
            missing_bars_total=0, session_break_slots=report.session_break_slots,
        )
        counts = {k: 0 for k in report.counts}
        for g in window_gaps.gaps:
            counts[g.kind] += 1
        window_gaps.counts = counts
        window_gaps.missing_bars_total = sum(g.missing_bars for g in window_gaps.gaps if g.kind == "missing")
        if is_dev_mode():
            logger.debug("rates %s %s %s..%s: %d bars source=%s stale=%s gaps=%s", name, tf.value, iso_z(start_ts),
                         iso_z(end_ts), len(window), source, stale, counts)
        return RatesResult(name, tf, start_ts, end_ts, window, source, stale, meta, window_gaps, message)

    def symbol_infos(self) -> list[SymbolInfoResult]:
        out: list[SymbolInfoResult] = []
        live = self.mt5_connected or self._try_connect()
        for name in self.symbols:
            if live and self.adapter is not None:
                try:
                    spec = self.adapter.symbol_spec(name)
                    self.cache.write_spec(spec, "mt5")
                    out.append(SymbolInfoResult(name, "mt5", spec, iso_z(pd.Timestamp(self.clock()))))
                    continue
                except UnknownSymbolError as exc:
                    out.append(SymbolInfoResult(name, "none", None, None, str(exc)))
                    continue
                except Mt5Error as exc:
                    error = str(exc)
                    logger.warning("symbol_info %s failed, falling back to cache: %s", name, exc)
                else:  # pragma: no cover
                    error = None
            else:
                error = None
            cached = self.cache.read_spec(name)
            if cached is not None:
                spec, info = cached
                out.append(SymbolInfoResult(name, "cache", spec, info.get("fetched_at_utc"), error))
            else:
                out.append(SymbolInfoResult(name, "none", None, None, error or "no cached symbol spec"))
        return out
