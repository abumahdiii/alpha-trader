"""Deterministic synthetic OHLCV for XAUUSD.x / BRNUSD.x, H1 and H4 (tests + tgc_testdata).

Everything is driven by a seeded ``numpy.random.Generator``: the same ``(symbol, start, end, seed,
offset_model)`` always gives byte-identical frames.

Session model (FX/metals convention, defined in America/New_York local time)
---------------------------------------------------------------------------
* The week opens Sunday 18:00 New York and closes Friday 17:00 New York (so in UTC the closure is
  Fri 22:00 -> Sun 23:00 in US standard time and Fri 21:00 -> Sun 22:00 during US DST).
* Daily session break 17:00-18:00 New York, Monday to Thursday (one missing H1 bar per day; in UTC
  it sits at 22:00 in winter and 21:00 in summer).
* Full holidays (Dec 25, Jan 1 and Good Friday of each year in the span, or ``holidays=``): the whole
  session that ends at 17:00 New York on that date is closed.

Planted anomalies (all listed in ``meta``)
------------------------------------------
* ``missing_events``: 3 single missing H1 bars and 2 whole missing H4 buckets (the four H1 bars inside
  the bucket are removed too, as a broker data hole would do), on distinct normal Tue-Thu days.
* ``high_spread``: a 6-hour window with spread x8.
* ``price_gap``: the 3rd weekly open of the span jumps ``gap_pct`` away from the previous close.

Server time and H4
------------------
``raw`` is what ``mt5.copy_rates_range`` would return: a structured array with MT5's dtype whose
``time`` is epoch seconds of the *server* clock under ``offset_model`` (default ``us_dst(+2)``). H4
bars are aggregated from the H1 bars in server-time-aligned 4-hour buckets (00, 04, ... server), like
a broker's native H4 (decision D4), so in UTC the H4 grid shifts with DST.

``meta["expected_gaps"]`` is computed from the *schedule* (which hours were closed and why), not from
the bars, so it is an independent oracle for :mod:`alpha_engine.data.gaps`.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from alpha_engine.data.schema import COLUMNS, TIME_DTYPE, Timeframe, normalize_frame
from alpha_engine.data.timezone import OffsetModel

DEFAULT_START = "2025-02-24"  # Monday; the span crosses the US (03-09) and EU (03-30) DST starts
DEFAULT_END = "2025-05-05"  # exclusive, Monday 00:00 UTC -> 10 trading weeks
DEFAULT_SEED = 20250224
DEFAULT_OFFSET_MODEL = OffsetModel.us_dst(2)
SYMBOLS: tuple[str, ...] = ("XAUUSD.x", "BRNUSD.x")
NY = "America/New_York"

MT5_RATES_DTYPE = np.dtype([
    ("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
    ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8"),
])

HIGH_SPREAD_FACTOR = 8
HIGH_SPREAD_HOURS = 6
N_SINGLE_MISSING = 3
N_BUCKET_MISSING = 2


@dataclass(frozen=True)
class SymbolProfile:
    symbol: str
    digits: int
    center: float
    low: float
    high: float
    sigma: float  # per-bar log-return volatility
    theta: float  # mean reversion per bar (log space)
    spread_base: int  # points
    spread_jitter: int
    gap_pct: float
    contract_size: float
    description: str
    currency_base: str
    currency_profit: str = "USD"


PROFILES: dict[str, SymbolProfile] = {
    "XAUUSD.x": SymbolProfile("XAUUSD.x", 2, 2000.0, 1900.0, 2100.0, 0.0015, 0.002, 25, 10, 0.012,
                              100.0, "Gold vs US Dollar (synthetic)", "XAU"),
    "BRNUSD.x": SymbolProfile("BRNUSD.x", 2, 80.0, 70.0, 90.0, 0.0025, 0.002, 4, 3, 0.02,
                              1000.0, "Brent Crude Oil (synthetic)", "BRN"),
}


@dataclass
class SyntheticSeries:
    symbol: str
    timeframe: Timeframe
    utc: pd.DataFrame  # canonical frame (time = UTC bar open)
    raw: np.ndarray  # MT5-style structured array, time = server epoch seconds
    meta: dict[str, Any] = field(default_factory=dict)

    def raw_frame(self) -> pd.DataFrame:
        """``raw`` as a DataFrame with canonical columns and int ``time`` (server epochs)."""
        frame = pd.DataFrame({name: self.raw[name] for name in MT5_RATES_DTYPE.names})
        for col in ("tick_volume", "spread", "real_volume"):
            frame[col] = frame[col].astype("int64")
        return frame.loc[:, list(COLUMNS)]


def symbol_info_dict(symbol: str) -> dict[str, Any]:
    """A fake ``mt5.symbol_info`` payload matching the synthetic profile (tests/fake mt5)."""
    p = PROFILES[symbol]
    point = 10.0 ** -p.digits
    return {
        "name": p.symbol, "digits": p.digits, "point": point,
        "trade_contract_size": p.contract_size,
        "trade_tick_size": point, "trade_tick_value": point * p.contract_size,
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0,
        "currency_profit": p.currency_profit, "currency_base": p.currency_base,
        "description": p.description, "visible": True,
    }


# --- calendar helpers ----------------------------------------------------------------------------

def good_friday(year: int) -> date:
    """Western Good Friday (anonymous Gregorian Easter algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def default_holidays(start: pd.Timestamp, end: pd.Timestamp) -> list[date]:
    days: set[date] = set()
    for year in range(start.year - 1, end.year + 2):
        days.update({date(year, 1, 1), good_friday(year), date(year, 12, 25)})
    lo, hi = start.date() - timedelta(days=1), end.date() + timedelta(days=1)
    return sorted(d for d in days if lo <= d <= hi and d.weekday() < 5)


def _ts(value: str | date | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _iso(epoch: int) -> str:
    return pd.Timestamp(int(epoch), unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _symbol_seed(seed: int, symbol: str) -> int:
    return (int(seed) * 1_000_003 + zlib.crc32(symbol.encode("utf-8"))) % (2**63)


# --- generation ----------------------------------------------------------------------------------

def generate_symbol(
    symbol: str = "XAUUSD.x",
    start: str | date | pd.Timestamp = DEFAULT_START,
    end: str | date | pd.Timestamp = DEFAULT_END,
    seed: int = DEFAULT_SEED,
    offset_model: OffsetModel = DEFAULT_OFFSET_MODEL,
    holidays: Iterable[date] | None = None,
) -> dict[Timeframe, SyntheticSeries]:
    """H1 and H4 series for one symbol over ``[start, end)`` (UTC)."""
    if symbol not in PROFILES:
        raise ValueError(f"no synthetic profile for {symbol!r}; known: {sorted(PROFILES)}")
    profile = PROFILES[symbol]
    start_ts, end_ts = _ts(start), _ts(end)
    if end_ts <= start_ts:
        raise ValueError("end must be after start")
    rng = np.random.default_rng(_symbol_seed(seed, symbol))
    hol = sorted(set(holidays)) if holidays is not None else default_holidays(start_ts, end_ts)

    grid = pd.date_range(start_ts, end_ts, freq="h", inclusive="left")
    utc_epoch = np.asarray((grid - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1), dtype="int64")
    ny = grid.tz_convert(NY)
    wd, hour = np.asarray(ny.weekday), np.asarray(ny.hour)
    ny_date = np.asarray(ny.date)

    weekend = (wd == 5) | ((wd == 4) & (hour >= 17)) | ((wd == 6) & (hour < 18))
    brk = (wd <= 3) & (hour == 17)
    holiday = np.zeros(grid.size, dtype=bool)
    for d in hol:
        holiday |= ((ny_date == d) & (hour < 17)) | ((ny_date == d - timedelta(days=1)) & (hour >= 18))
    closed = weekend | brk | holiday

    server = offset_model.utc_to_server(utc_epoch)
    server_day = server // 86400
    server_hour = (server % 86400) // 3600
    server_wd = (server_day + 3) % 7  # 1970-01-01 was a Thursday (Mon=0)

    # --- choose the days that receive anomalies (normal Tue-Thu with hours 02..22 all open)
    candidates: list[int] = []
    for day in np.unique(server_day):
        in_day = server_day == day
        if server_wd[in_day][0] not in (1, 2, 3):
            continue
        core = in_day & (server_hour >= 2) & (server_hour <= 22)
        if core.sum() == 21 and not closed[core].any():
            candidates.append(int(day))
    n_needed = N_BUCKET_MISSING + N_SINGLE_MISSING + 1
    chosen = sorted(rng.choice(candidates, size=min(n_needed, len(candidates)), replace=False).tolist()) \
        if candidates else []
    order = rng.permutation(len(chosen)).tolist()
    roles = [chosen[i] for i in order]
    bucket_days = roles[:N_BUCKET_MISSING]
    single_days = roles[N_BUCKET_MISSING:N_BUCKET_MISSING + N_SINGLE_MISSING]
    spread_days = roles[N_BUCKET_MISSING + N_SINGLE_MISSING:]

    missing = np.zeros(grid.size, dtype=bool)
    missing_events: list[dict[str, Any]] = []
    for day in sorted(bucket_days):
        bucket_hour = int(rng.choice([4, 8, 12, 16]))
        mask = (server_day == day) & (server_hour >= bucket_hour) & (server_hour < bucket_hour + 4)
        missing |= mask
        first = int(utc_epoch[mask][0])
        missing_events.append({"kind": "h4_bucket", "start": _iso(first), "bars_h1": int(mask.sum()),
                               "h4_bucket_server_hour": bucket_hour})
    for day in sorted(single_days):
        h = int(rng.integers(3, 22))
        mask = (server_day == day) & (server_hour == h)
        missing |= mask
        missing_events.append({"kind": "single_h1", "start": _iso(int(utc_epoch[mask][0])), "bars_h1": 1})
    missing_events.sort(key=lambda e: e["start"])

    trading = ~closed  # bars that the market "produced" (missing ones are produced then lost)
    idx = np.flatnonzero(trading)
    n = idx.size
    if n == 0:
        raise ValueError("span contains no trading hours")

    # --- price path (OU in log space around the profile center, clipped to the profile range)
    lo, hi = np.log(profile.low * 1.005), np.log(profile.high * 0.995)
    mu = np.log(profile.center)
    x = mu + rng.normal(0.0, 0.01)
    eps = rng.normal(0.0, 1.0, size=n)
    wick = np.abs(rng.normal(0.0, profile.sigma * 0.6, size=(n, 2)))
    week_open = np.zeros(n, dtype=bool)
    week_open[1:] = np.diff(utc_epoch[idx]) >= 36 * 3600
    gap_positions = np.flatnonzero(week_open)
    gap_at = int(gap_positions[2]) if gap_positions.size >= 3 else (int(gap_positions[0]) if gap_positions.size else -1)
    gap_sign = 1.0 if rng.random() < 0.5 else -1.0

    opens = np.empty(n)
    closes = np.empty(n)
    prev_close = float(np.exp(x))
    price_gap: dict[str, Any] | None = None
    for i in range(n):
        o = prev_close
        if i == gap_at:
            o = float(np.clip(prev_close * (1 + gap_sign * profile.gap_pct), np.exp(lo), np.exp(hi)))
            x = np.log(o)
        o = round(o, profile.digits)
        x = float(np.clip(x + profile.theta * (mu - x) + profile.sigma * eps[i], lo, hi))
        c = round(float(np.exp(x)), profile.digits)
        opens[i], closes[i] = o, c
        if i == gap_at:
            price_gap = {"time": _iso(int(utc_epoch[idx[i]])), "prev_close": round(prev_close, profile.digits),
                         "open": o, "pct": round((o / round(prev_close, profile.digits) - 1) * 100, 4)}
        prev_close = c
    highs = np.round(np.maximum(opens, closes) * (1 + wick[:, 0]), profile.digits)
    lows = np.round(np.minimum(opens, closes) * (1 - wick[:, 1]), profile.digits)
    highs = np.maximum(highs, np.maximum(opens, closes))
    lows = np.minimum(lows, np.minimum(opens, closes))

    spreads = profile.spread_base + rng.integers(0, profile.spread_jitter + 1, size=n)
    high_spread: dict[str, Any] | None = None
    if spread_days:
        day = spread_days[0]
        mask_grid = (server_day == day) & (server_hour >= 8) & (server_hour < 8 + HIGH_SPREAD_HOURS)
        mask = mask_grid[idx]
        spreads = np.where(mask, spreads * HIGH_SPREAD_FACTOR, spreads)
        first, last = int(utc_epoch[mask_grid][0]), int(utc_epoch[mask_grid][-1])
        high_spread = {"start": _iso(first), "end": _iso(last + 3600), "factor": HIGH_SPREAD_FACTOR,
                       "bars_h1": int(mask.sum())}
    tick_volume = rng.integers(150, 2500, size=n)

    keep = ~missing[idx]
    h1_utc = utc_epoch[idx][keep]
    h1 = pd.DataFrame({
        "time": server[idx][keep],
        "open": opens[keep], "high": highs[keep], "low": lows[keep], "close": closes[keep],
        "tick_volume": tick_volume[keep].astype("int64"), "spread": spreads[keep].astype("int64"),
        "real_volume": np.zeros(int(keep.sum()), dtype="int64"),
    })

    # --- H4 from H1, server-time buckets
    h1b = h1.assign(bucket=(h1["time"] // 14400) * 14400)
    agg = h1b.groupby("bucket", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        tick_volume=("tick_volume", "sum"), spread=("spread", "max"), real_volume=("real_volume", "sum"),
    ).reset_index().rename(columns={"bucket": "time"})
    # only buckets whose four server hours all lie inside the span (no partial edge bars)
    first_bucket, last_bucket_end = int(server[0]), int(server[-1]) + 3600

    def _complete(b: pd.Series | np.ndarray) -> np.ndarray:
        return np.asarray((b >= first_bucket) & (b + 14400 <= last_bucket_end))

    h4 = agg.loc[_complete(agg["time"]), list(COLUMNS)].reset_index(drop=True)

    point = 10.0 ** -profile.digits
    base_meta = {
        "symbol": symbol, "seed": int(seed), "start_utc": _iso(int(utc_epoch[0])),
        "end_utc": _iso(int(utc_epoch[-1]) + 3600), "offset_model": offset_model.label,
        "digits": profile.digits, "point": point,
        "holidays": [d.isoformat() for d in hol if start_ts.date() <= d < end_ts.date()],
        "session_break_ny": "17:00-18:00 Mon-Thu",
        "weekly_close_ny": "Fri 17:00", "weekly_open_ny": "Sun 18:00",
        "missing_events": missing_events, "high_spread": high_spread, "price_gap": price_gap,
    }

    # --- expected gaps, from the schedule (independent of the classifier)
    tags = {"holiday": holiday, "weekend": weekend, "missing": missing, "session_break": brk}
    present_h1 = trading & ~missing
    h1_expected = _expected_gaps(utc_epoch, present_h1, tags)
    bucket_of_grid = (server // 14400) * 14400
    grid_df = pd.DataFrame({"bucket": bucket_of_grid, "present": present_h1, **tags})
    per_bucket = grid_df.groupby("bucket", sort=True).any()
    per_bucket = per_bucket.loc[_complete(per_bucket.index.to_numpy(dtype="int64"))]
    missing_buckets = [
        int(b) for b in per_bucket.index
        if not per_bucket.at[b, "present"] and per_bucket.at[b, "missing"]
    ]
    h4_expected = _expected_gaps(offset_model.server_to_utc(per_bucket.index.to_numpy(dtype="int64")),
                                 per_bucket["present"].to_numpy(),
                                 {k: per_bucket[k].to_numpy() for k in tags})

    out: dict[Timeframe, SyntheticSeries] = {}
    for tf, frame, expected, miss in (
        (Timeframe.H1, h1, h1_expected, [_iso(int(e)) for e in utc_epoch[missing]]),
        (Timeframe.H4, h4, h4_expected,
         [_iso(int(e)) for e in offset_model.server_to_utc(np.array(missing_buckets, dtype="int64"))]),
    ):
        raw = np.zeros(len(frame), dtype=MT5_RATES_DTYPE)
        for name in MT5_RATES_DTYPE.names:
            raw[name] = frame[name].to_numpy()
        utc = frame.copy()
        utc["time"] = pd.Series(
            pd.to_datetime(offset_model.server_to_utc(frame["time"].to_numpy()), unit="s", utc=True),
            index=frame.index).astype(TIME_DTYPE)
        utc = normalize_frame(utc)
        meta = {**base_meta, "timeframe": tf.value, "rows": len(frame), "missing_bars": miss,
                "expected_gaps": expected["counts"], "expected_gap_list": expected["gaps"]}
        out[tf] = SyntheticSeries(symbol=symbol, timeframe=tf, utc=utc, raw=raw, meta=meta)
    # sanity: H1 conversion must invert the server clock exactly
    assert np.array_equal(offset_model.server_to_utc(out[Timeframe.H1].raw["time"]), h1_utc)
    return out


_KIND_PRIORITY: tuple[str, ...] = ("holiday", "weekend", "missing", "session_break")


def _expected_gaps(grid: np.ndarray, present: np.ndarray, tags: dict[str, np.ndarray]) -> dict[str, Any]:
    """Runs of absent grid slots bounded by present slots, tagged by the dominant closure reason.

    ``grid`` holds the UTC epoch of each slot's bar open time.
    """
    counts = {k: 0 for k in _KIND_PRIORITY}
    gaps: list[dict[str, Any]] = []
    pos = np.flatnonzero(present)
    for a, b in zip(pos[:-1], pos[1:], strict=True):
        if b - a <= 1:
            continue
        run = slice(a + 1, b)
        kind = next((k for k in _KIND_PRIORITY if tags[k][run].any()), "missing")
        counts[kind] += 1
        gaps.append({"after": _iso(int(grid[a])), "before": _iso(int(grid[b])), "kind": kind,
                     "slots": int(b - a - 1)})
    return {"counts": counts, "gaps": gaps}


def generate_dataset(
    symbols: Sequence[str] = SYMBOLS,
    start: str | date | pd.Timestamp = DEFAULT_START,
    end: str | date | pd.Timestamp = DEFAULT_END,
    seed: int = DEFAULT_SEED,
    offset_model: OffsetModel = DEFAULT_OFFSET_MODEL,
) -> dict[tuple[str, Timeframe], SyntheticSeries]:
    out: dict[tuple[str, Timeframe], SyntheticSeries] = {}
    for symbol in symbols:
        for tf, series in generate_symbol(symbol, start, end, seed, offset_model).items():
            out[(symbol, tf)] = series
    return out
