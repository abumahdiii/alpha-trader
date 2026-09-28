"""Load the cached history for a backtest and scan it once (no HTTP, no MT5).

* :func:`load_history` reads the FULL cached H1 and H4 frames and the cached symbol spec through
  :class:`~alpha_engine.data.cache.OhlcvCache` (never MT5; the cache is only read).
* :func:`data_fingerprint` identifies the data a run used: first/last bar and rows per timeframe, the cache
  ``source`` (``mt5`` / ``seed``), fetch time, a SHA-256 of the bar content and the symbol spec.
* :func:`scan_full_history` runs ``strategy.scan`` of ANY registered strategy ONCE over the FULL history
  (warm-up consistency: path-dependent indicators -- e.g. the StdDev channel's Wilder ATR, a recursion over the
  whole series -- would change if the history were sliced first, and thus the decisions). Candidates are
  checked against the strategy's identity / symbol / params hash (one per confirmation bar) and indexed by
  their confirmation H1 bar for the simulator; the strategy's ``first_valid_index`` and
  ``warmup_margin_h4_bars`` travel with the scan (``periods.earliest_start``).
* :func:`build_bar_arrays` prepares the arrays the simulator walks: OHLC, the causal spread series and the
  ``missing`` gaps (``data.gaps.find_gaps``).

Note for a later refactor: ``routes/chart.py`` has private helpers with overlapping duties (cache reads,
full-history scan); they are deliberately not imported here (private API) and not changed in this task.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pandas as pd

from ..data.cache import CacheError, CacheMeta, OhlcvCache
from ..data.gaps import find_gaps
from ..data.schema import Timeframe
from ..data.symbols import SymbolSpec
from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettings
from ..strategy.base import Strategy, StrategyContractError, StrategyIdentity, WarmupTextsFa, bar_open_times
from ..strategy.params import ParamValue, params_hash
from ..strategy.signal import SignalCandidate
from .costs import spread_from_frame
from .models import DataFingerprint
from .simulator import BarArrays, ns_to_dt

logger = get_logger(__name__)


class HistoryUnavailable(RuntimeError):
    """The cache cannot provide what a backtest needs (Persian message for the API)."""

    def __init__(self, code: str, message_fa: str) -> None:
        super().__init__(message_fa)
        self.code = code
        self.message_fa = message_fa


@dataclass(frozen=True, eq=False)
class HistoryData:
    symbol: str
    h1: pd.DataFrame
    h4: pd.DataFrame
    spec: SymbolSpec
    h1_meta: CacheMeta | None = None
    h4_meta: CacheMeta | None = None
    spec_source: str | None = None


def load_history(cache: OhlcvCache, symbol: str) -> HistoryData:
    """Full cached H1 + H4 frames and the cached spec of ``symbol``. Raises :class:`HistoryUnavailable`."""
    frames: dict[Timeframe, tuple[pd.DataFrame, CacheMeta]] = {}
    for tf in (Timeframe.H1, Timeframe.H4):
        try:
            cached = cache.read(symbol, tf)
        except (CacheError, OSError, ValueError) as exc:
            logger.warning("backtest: cache for %s %s is unreadable: %s", symbol, tf.value, exc)
            raise HistoryUnavailable("cache_unreadable", f"فایل کش {symbol} {tf.value} قابل خواندن نیست.") from None
        if cached is None or len(cached[0]) == 0:
            raise HistoryUnavailable("no_data", f"داده {tf.value} نماد {symbol} در کش نیست.")
        frames[tf] = cached
    try:
        spec_entry = cache.read_spec(symbol)
    except (OSError, ValueError) as exc:
        logger.warning("backtest: cached spec of %s is unreadable: %s", symbol, exc)
        spec_entry = None
    if spec_entry is None:
        raise HistoryUnavailable(
            "spec_missing", "مشخصات نماد در کش نیست؛ حجم قابل محاسبه نیست (یک بار با اتصال به MT5 فهرست نمادها را بگیرید).")
    spec, info = spec_entry
    (h1, h1_meta), (h4, h4_meta) = frames[Timeframe.H1], frames[Timeframe.H4]
    if is_dev_mode():
        logger.debug("backtest history %s: H1 %d rows %s..%s (%s), H4 %d rows (%s), spec source=%s point=%g",
                     symbol, len(h1), h1_meta.first_bar_utc, h1_meta.last_bar_utc, h1_meta.source, len(h4),
                     h4_meta.source, info.get("source"), spec.point)
    return HistoryData(symbol=symbol, h1=h1, h4=h4, spec=spec, h1_meta=h1_meta, h4_meta=h4_meta,
                       spec_source=info.get("source"))


def _frame_sha256(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(bar_open_times(frame).as_unit("ns").asi8).tobytes())
    for col in ("open", "high", "low", "close"):
        digest.update(np.ascontiguousarray(frame[col].to_numpy(dtype=np.float64)).tobytes())
    if "spread" in frame.columns:
        digest.update(np.ascontiguousarray(frame["spread"].to_numpy(dtype=np.int64)).tobytes())
    return digest.hexdigest()


def _bounds(frame: pd.DataFrame) -> tuple[Any, Any]:
    if len(frame) == 0:
        return None, None
    times = bar_open_times(frame)
    return times[0].to_pydatetime(), times[-1].to_pydatetime()


def data_fingerprint(history: HistoryData) -> DataFingerprint:
    h1_first, h1_last = _bounds(history.h1)
    h4_first, h4_last = _bounds(history.h4)
    return DataFingerprint(
        symbol=history.symbol, h1_rows=len(history.h1), h4_rows=len(history.h4), h1_first_bar=h1_first,
        h1_last_bar=h1_last, h4_first_bar=h4_first, h4_last_bar=h4_last,
        h1_source=history.h1_meta.source if history.h1_meta else None,
        h4_source=history.h4_meta.source if history.h4_meta else None,
        h1_fetched_at_utc=history.h1_meta.fetched_at_utc if history.h1_meta else None,
        h4_fetched_at_utc=history.h4_meta.fetched_at_utc if history.h4_meta else None,
        h1_sha256=_frame_sha256(history.h1), h4_sha256=_frame_sha256(history.h4),
        spec=history.spec.model_dump(), spec_source=history.spec_source,
    )


@dataclass(frozen=True, eq=False)
class ScanResult:
    """Full-history scan: candidates by confirmation bar + what the window planner needs."""

    symbol: str
    strategy_name: str
    strategy_version: int
    params_hash: str
    rr: float
    candidates: list[SignalCandidate]
    by_bar: dict[int, SignalCandidate]  # confirmation H1 bar index -> candidate
    first_valid_index: int | None  # Strategy.first_valid_index (StdDev: first bar with channel + ATRs defined)
    h1_count: int
    # strategy contract S1: who scanned, and the warm-up the period planner needs
    strategy: Strategy | None = None
    identity: StrategyIdentity | None = None
    params: dict[str, ParamValue] | None = None  # the validated params the scan used
    warmup_margin_h4_bars: int = 0
    warmup_texts_fa: WarmupTextsFa | None = None


def scan_full_history(
    strategy: Strategy,
    h1: pd.DataFrame,
    h4: pd.DataFrame,
    params: Mapping[str, Any] | None,
    account: AccountSettings,
    *,
    symbol: str,
) -> ScanResult:
    """``strategy.scan`` over the FULL history (never sliced first), indexed by confirmation bar.

    Raises ``StrategyParamsError`` for params the strategy rejects and ``StrategyContractError`` when a
    candidate does not belong to this strategy / symbol / params, is not on a bar of ``h1``, or shares its
    confirmation bar with another candidate."""
    clean = strategy.clean_params(params)
    phash = params_hash(clean)
    identity = strategy.identity
    candidates = strategy.scan(h1, h4, clean, account, symbol=symbol)
    times_ns = bar_open_times(h1).as_unit("ns").asi8
    by_bar: dict[int, SignalCandidate] = {}
    for cand in candidates:
        if not isinstance(cand, SignalCandidate):
            raise StrategyContractError(f"{identity.label()} scan returned {type(cand).__name__}, not SignalCandidate")
        if (cand.strategy_name, cand.strategy_version) != (identity.name, identity.version) \
                or cand.symbol != symbol or cand.params_hash != phash:
            raise StrategyContractError(
                f"{identity.label()} scan: candidate at {cand.confirmation_bar_open_utc} is for "
                f"{cand.strategy_name} v{cand.strategy_version} / {cand.symbol} / params {cand.params_hash[:12]}")
        conf_ns = pd.Timestamp(cand.confirmation_bar_open_utc).as_unit("ns").value
        t = int(np.searchsorted(times_ns, conf_ns))
        if t >= len(times_ns) or times_ns[t] != conf_ns:
            raise StrategyContractError(f"confirmation bar {cand.confirmation_bar_open_utc} not in the H1 frame")
        if t in by_bar:
            raise StrategyContractError(f"{identity.label()} scan: two candidates on bar "
                                        f"{cand.confirmation_bar_open_utc}")
        by_bar[t] = cand
    first_valid = strategy.first_valid_index(h1, h4, clean)
    margin = int(strategy.warmup_margin_h4_bars(clean))
    if is_dev_mode():
        logger.debug("backtest scan %s: %s hash=%s rr=%g h1=%d candidates=%d first_valid=%s warm-up=%d H4 bars",
                     symbol, identity.label(), phash[:12], account.rr, len(h1), len(candidates),
                     None if first_valid is None else ns_to_dt(int(times_ns[first_valid])).isoformat(), margin)
    return ScanResult(symbol=symbol, strategy_name=identity.name, strategy_version=identity.version,
                      params_hash=phash, rr=account.rr, candidates=candidates, by_bar=by_bar,
                      first_valid_index=first_valid, h1_count=len(h1), strategy=strategy, identity=identity,
                      params=dict(clean), warmup_margin_h4_bars=margin, warmup_texts_fa=strategy.warmup_texts_fa)


def build_bar_arrays(h1: pd.DataFrame, spec: SymbolSpec, fallback_spread_points: int | None = None) -> BarArrays:
    """H1 arrays + causal spread (fallback for bars without an earlier broker spread; ``None`` = auto, see
    ``costs.py``) + ``missing`` gaps for the simulator."""
    times_ns = np.ascontiguousarray(bar_open_times(h1).as_unit("ns").asi8)
    if len(times_ns) > 1 and not (np.diff(times_ns) > 0).all():
        raise ValueError("H1 bars must be sorted by time without duplicates")
    missing = np.zeros(len(times_ns), dtype=bool)
    if len(times_ns) > 1:
        frame = pd.DataFrame({"time": pd.to_datetime(times_ns, utc=True)})
        report = find_gaps(frame, Timeframe.H1)
        after_ns = [pd.Timestamp(g.after).as_unit("ns").value for g in report.gaps if g.kind == "missing"]
        if after_ns:
            idx = np.searchsorted(times_ns, np.asarray(after_ns, dtype=np.int64))
            missing[idx] = True
        if is_dev_mode():
            logger.debug("backtest bars: %d H1 bars, gaps %s, missing-gap entries blocked after %d bar(s)",
                         len(times_ns), report.counts, int(missing.sum()))
    spread = spread_from_frame(h1, spec.point, fallback_spread_points)
    return BarArrays(
        times_ns=times_ns, open=h1["open"].to_numpy(dtype=np.float64), high=h1["high"].to_numpy(dtype=np.float64),
        low=h1["low"].to_numpy(dtype=np.float64), close=h1["close"].to_numpy(dtype=np.float64), spread=spread,
        missing_gap_after=missing,
    )


def with_fallback_spread(bars: BarArrays, h1: pd.DataFrame, spec: SymbolSpec, fallback_points: int) -> BarArrays:
    """``bars`` with the spread series rebuilt for another fallback (prices/gaps shared, list views reset)."""
    if bars.spread.fallback_points == fallback_points:
        return bars
    return replace(bars, spread=spread_from_frame(h1, spec.point, fallback_points), _lists={})


__all__ = [
    "HistoryData",
    "HistoryUnavailable",
    "ScanResult",
    "build_bar_arrays",
    "data_fingerprint",
    "load_history",
    "scan_full_history",
    "with_fallback_spread",
]
