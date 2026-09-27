"""Broker server time <-> UTC (``.claude/rules/03_trading_safety.md`` section 3).

MT5 returns bar/tick times as epoch seconds of the broker's *server wall clock* (i.e. "UTC-looking"
numbers that are really server local time). Everything after the adapter boundary is UTC, so the
conversion happens exactly once, here, using an :class:`OffsetModel`.

Offset models
-------------
* ``fixed(h)``  -- server = UTC + h hours, all year.
* ``us_dst(b)`` -- server = UTC + b in US standard time, UTC + b + 1 while America/New_York observes
  DST (the common "New York close" broker convention: ``us_dst(+2)`` keeps 17:00 New York == 00:00
  server all year).
* ``eu_dst(b)`` -- like ``us_dst`` but switching on the Europe/London (EU) transition dates.

Transitions come from the IANA database via ``zoneinfo`` (pandas ``tz_convert``), so historic rule
changes (e.g. the 2007 US change) are handled.

Choosing the model (user decision Q2: automatic + optional ``MT5_SERVER_UTC_OFFSET`` override)
------------------------------------------------------------------------------------------------
1. **Live check** -- ``symbol_info_tick(sym).time - now_utc`` rounded to the nearest 0.5 h, trusted only
   when the market is open and the residual is small (tick recent). Gives the *current* offset.
2. **Historical fit** -- on XAUUSD.x H1 server-time bars, every trading week should close with a bar
   ending at Friday 17:00 America/New_York and open with a bar starting Sunday 18:00 New York (FX/gold
   convention). Every candidate model is scored by how many weekly closes/opens it puts exactly on
   those anchors; the best one wins (ties: consistency with the live offset, then us_dst > eu_dst >
   fixed).
3. **Override** -- if ``MT5_SERVER_UTC_OFFSET`` is set, ``fixed(override)`` is used for conversion and the
   inferred model is still computed and reported for comparison.

Worked examples (``us_dst(+2)``; tested in ``tests/test_timezone.py``):

* server ``2025-01-06 01:00`` (US standard time, offset +2) -> ``2025-01-05 23:00Z``
* server ``2025-07-07 01:00`` (US DST, offset +3)           -> ``2025-07-06 22:00Z``
* server ``2025-03-17 01:00`` -- a week between the US (03-09) and EU (03-30) DST starts: ``us_dst(+2)``
  gives ``2025-03-16 22:00Z`` but ``eu_dst(+2)`` gives ``2025-03-16 23:00Z``; this is exactly the
  window that lets the fit tell the two apart.

Conversion around a DST switch: server clock times that do not exist (spring forward) map with the
standard offset; ambiguous ones (fall back) map to the DST (earlier) instant. Both happen on Sunday
mornings, while FX/metal markets are closed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..logging_setup import get_logger, is_dev_mode

logger = get_logger(__name__)

OffsetKind = Literal["fixed", "us_dst", "eu_dst"]

# zone, standard (non-DST) UTC offset in seconds
_DST_ZONES: dict[str, tuple[str, int]] = {
    "us_dst": ("America/New_York", -5 * 3600),
    "eu_dst": ("Europe/London", 0),
}
_KIND_PRIORITY = {"us_dst": 2, "eu_dst": 1, "fixed": 0}

ANCHOR_ZONE = "America/New_York"
WEEKLY_CLOSE_ANCHOR = (4, 17)  # Friday 17:00 New York (weekday Mon=0)
WEEKLY_OPEN_ANCHOR = (6, 18)  # Sunday 18:00 New York
WEEK_SPLIT_SECONDS = 36 * 3600  # a gap this long separates trading weeks (weekday holidays are ~25 h)

CANDIDATE_BASES: tuple[float, ...] = tuple(h / 2 for h in range(-24, 29))  # -12.0 .. +14.0 step 0.5


class OffsetError(RuntimeError):
    """The broker offset could not be determined."""


class OffsetModel(BaseModel):
    """A server-time offset model. Immutable and hashable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OffsetKind
    base_hours: float = Field(ge=-12, le=14)

    @field_validator("base_hours")
    @classmethod
    def _half_hours(cls, value: float) -> float:
        if not math.isclose(value * 2, round(value * 2)):
            raise ValueError("base_hours must be a multiple of 0.5")
        return float(value)

    @classmethod
    def fixed(cls, hours: float) -> OffsetModel:
        return cls(kind="fixed", base_hours=hours)

    @classmethod
    def us_dst(cls, base: float) -> OffsetModel:
        return cls(kind="us_dst", base_hours=base)

    @classmethod
    def eu_dst(cls, base: float) -> OffsetModel:
        return cls(kind="eu_dst", base_hours=base)

    @property
    def label(self) -> str:
        """``us_dst(+2)``, ``fixed(-4.5)``, ``fixed(0)``."""
        h = self.base_hours
        text = f"{h:+g}" if h != 0 else "0"
        return f"{self.kind}({text})"

    @classmethod
    def parse(cls, label: str) -> OffsetModel:
        text = label.strip()
        if not text.endswith(")") or "(" not in text:
            raise ValueError(f"not an offset model label: {label!r}")
        kind, _, rest = text.partition("(")
        return cls(kind=kind, base_hours=float(rest[:-1]))  # type: ignore[arg-type]

    def __str__(self) -> str:
        return self.label

    # --- vectorized conversion --------------------------------------------------------------------

    def offset_seconds_at_utc(self, utc_epochs: Sequence[int] | np.ndarray) -> np.ndarray:
        """Server-minus-UTC offset (seconds) in effect at each UTC instant."""
        arr = np.asarray(utc_epochs, dtype="int64")
        base = np.full(arr.shape, int(round(self.base_hours * 3600)), dtype="int64")
        if self.kind == "fixed" or arr.size == 0:
            return base
        return base + np.where(_dst_flags(arr, self.kind), 3600, 0).astype("int64")

    def utc_to_server(self, utc_epochs: Sequence[int] | np.ndarray) -> np.ndarray:
        arr = np.asarray(utc_epochs, dtype="int64")
        return arr + self.offset_seconds_at_utc(arr)

    def server_to_utc(self, server_epochs: Sequence[int] | np.ndarray) -> np.ndarray:
        """UTC epochs for server-clock epochs (see module docstring for the DST-switch rule)."""
        s = np.asarray(server_epochs, dtype="int64")
        base = int(round(self.base_hours * 3600))
        if self.kind == "fixed" or s.size == 0:
            return s - base
        u_dst = s - base - 3600  # interpretation "DST was in effect"
        u_std = s - base
        return np.where(_dst_flags(u_dst, self.kind), u_dst, u_std).astype("int64")

    def offset_hours_at(self, when_utc: datetime | pd.Timestamp) -> float:
        ts = pd.Timestamp(when_utc)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        epoch = int(ts.timestamp())
        return float(self.offset_seconds_at_utc(np.array([epoch]))[0]) / 3600.0


def _dst_flags(utc_epochs: np.ndarray, kind: str) -> np.ndarray:
    zone, std = _DST_ZONES[kind]
    idx = pd.DatetimeIndex(pd.to_datetime(utc_epochs, unit="s", utc=True))
    local_naive = idx.tz_convert(zone).tz_localize(None)
    offsets = (local_naive - idx.tz_localize(None)) // pd.Timedelta(seconds=1)
    return np.asarray(offsets, dtype="int64") > std


def all_candidate_models(bases: Sequence[float] = CANDIDATE_BASES) -> list[OffsetModel]:
    return [OffsetModel(kind=k, base_hours=b) for k in ("us_dst", "eu_dst", "fixed") for b in bases]


# --- live offset ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveOffset:
    """Current offset estimated from the last tick. ``hours`` is None if no tick was available."""

    hours: float | None
    trusted: bool
    raw_seconds: int | None
    residual_seconds: int | None
    reason: str


def market_probably_closed(now_utc: datetime) -> bool:
    """Conservative FX/metals weekend window: Friday 21:00 UTC .. Sunday 23:00 UTC."""
    now = now_utc.astimezone(timezone.utc)
    wd, hour = now.weekday(), now.hour
    return wd == 5 or (wd == 4 and hour >= 21) or (wd == 6 and hour < 23)


def estimate_live_offset(
    tick_server_epoch: int | None,
    now_utc: datetime,
    max_residual_seconds: int = 300,
) -> LiveOffset:
    """``tick.time - now`` rounded to 0.5 h.

    Rounding to 0.5 h always leaves a residual of at most 15 min, so that alone proves nothing; the
    estimate is trusted only if the residual is <= ``max_residual_seconds`` (the tick is fresh) and the
    market is not in its weekend closure (a Friday tick read on Saturday would look like a wrong
    offset).
    """
    if tick_server_epoch is None or tick_server_epoch <= 0:
        return LiveOffset(None, False, None, None, "no tick available")
    now_epoch = int(now_utc.astimezone(timezone.utc).timestamp())
    raw = int(tick_server_epoch) - now_epoch
    hours = round(raw / 1800) / 2
    residual = abs(raw - int(hours * 3600))
    if market_probably_closed(now_utc):
        result = LiveOffset(hours, False, raw, residual, "market closed (weekend): tick is stale")
    elif residual > max_residual_seconds:
        result = LiveOffset(hours, False, raw, residual, f"tick not fresh (residual {residual}s)")
    elif not -12 <= hours <= 14:
        result = LiveOffset(hours, False, raw, residual, "offset out of range")
    else:
        result = LiveOffset(hours, True, raw, residual, "ok")
    if is_dev_mode():
        logger.debug("live offset: raw=%ss -> %+gh residual=%ss trusted=%s (%s)",
                     raw, hours, residual, result.trusted, result.reason)
    return result


# --- historical fit ------------------------------------------------------------------------------

class CandidateScore(BaseModel):
    label: str
    matched: int


class OffsetFit(BaseModel):
    """Result of :func:`fit_offset_model` (stored with the cache and shown in the data-check report)."""

    model_config = ConfigDict(extra="forbid")

    inferred: OffsetModel | None
    inferred_label: str | None
    effective: OffsetModel
    effective_label: str
    source: Literal["fit", "override", "live"]
    match_ratio: float
    matched: int
    checks: int
    weeks: int
    override_hours: float | None = None
    live_offset_hours: float | None = None
    live_trusted: bool = False
    live_reason: str | None = None
    model_offset_now_hours: float | None = None
    consistent_with_live: bool | None = None
    low_confidence: bool = False
    top_candidates: list[CandidateScore] = []
    anchor: str = "weekly close Fri 17:00 / open Sun 18:00 America/New_York"
    fitted_at_utc: str | None = None


def _week_segments(server_epochs: np.ndarray) -> list[tuple[int, int]]:
    """Index ranges ``[start, end]`` of contiguous trading weeks (split at gaps >= 36 h)."""
    if server_epochs.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(server_epochs) >= WEEK_SPLIT_SECONDS)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [server_epochs.size - 1]))
    return list(zip(starts.tolist(), ends.tolist(), strict=True))


def _anchor_hits(utc_epochs: np.ndarray, anchor: tuple[int, int]) -> np.ndarray:
    if utc_epochs.size == 0:
        return np.zeros(0, dtype=bool)
    local = pd.DatetimeIndex(pd.to_datetime(utc_epochs, unit="s", utc=True)).tz_convert(ANCHOR_ZONE)
    wd, hour = anchor
    return np.asarray((local.weekday == wd) & (local.hour == hour) & (local.minute == 0) & (local.second == 0))


def fit_offset_model(
    server_epochs: Sequence[int] | np.ndarray,
    timeframe_seconds: int = 3600,
    *,
    live: LiveOffset | None = None,
    override_hours: float | None = None,
    now_utc: datetime | None = None,
    candidates: Sequence[OffsetModel] | None = None,
) -> OffsetFit:
    """Infer the broker offset model from H1 bar open times in *server* epochs.

    Raises :class:`OffsetError` only if there is nothing to fit *and* no override/trusted live offset.
    """
    s = np.sort(np.unique(np.asarray(server_epochs, dtype="int64")))
    segments = _week_segments(s)
    # Weekly close of every week but the last (it may be cut by the data end / still running) and
    # weekly open of every week but the first (it may start mid-week at the data start).
    close_points = np.array([s[e] + timeframe_seconds for _, e in segments[:-1]], dtype="int64")
    open_points = np.array([s[b] for b, _ in segments[1:]], dtype="int64")
    checks = int(close_points.size + open_points.size)
    now = now_utc or datetime.now(timezone.utc)

    scores: list[tuple[tuple, OffsetModel, int]] = []
    if checks:
        for model in candidates or all_candidate_models():
            matched = int(_anchor_hits(model.server_to_utc(close_points), WEEKLY_CLOSE_ANCHOR).sum()
                          + _anchor_hits(model.server_to_utc(open_points), WEEKLY_OPEN_ANCHOR).sum())
            consistent = _consistent(model, live, now)
            rank = (matched, {True: 2, None: 1, False: 0}[consistent], _KIND_PRIORITY[model.kind])
            scores.append((rank, model, matched))
        scores.sort(key=lambda item: item[0], reverse=True)

    inferred: OffsetModel | None = None
    matched = 0
    if scores and scores[0][2] > 0:
        _, inferred, matched = scores[0]
    ratio = matched / checks if checks else 0.0

    if override_hours is not None:
        effective, source = OffsetModel.fixed(override_hours), "override"
    elif inferred is not None:
        effective, source = inferred, "fit"
    elif live is not None and live.trusted and live.hours is not None:
        effective, source = OffsetModel.fixed(live.hours), "live"
    else:
        raise OffsetError(
            "cannot determine the broker UTC offset: no weekly anchors matched, no trusted live tick "
            "and MT5_SERVER_UTC_OFFSET is not set"
        )

    fit = OffsetFit(
        inferred=inferred,
        inferred_label=inferred.label if inferred else None,
        effective=effective,
        effective_label=effective.label,
        source=source,
        match_ratio=round(ratio, 4),
        matched=matched,
        checks=checks,
        weeks=len(segments),
        override_hours=override_hours,
        live_offset_hours=live.hours if live else None,
        live_trusted=bool(live and live.trusted),
        live_reason=live.reason if live else None,
        model_offset_now_hours=effective.offset_hours_at(now),
        consistent_with_live=_consistent(effective, live, now),
        low_confidence=source != "override" and ratio < 0.5,
        top_candidates=[CandidateScore(label=m.label, matched=n) for _, m, n in scores[:5]],
        fitted_at_utc=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if is_dev_mode():
        logger.debug(
            "offset fit: effective=%s (source=%s) inferred=%s matched=%d/%d weeks=%d live=%s trusted=%s "
            "consistent=%s top=%s",
            fit.effective_label, fit.source, fit.inferred_label, matched, checks, fit.weeks,
            fit.live_offset_hours, fit.live_trusted, fit.consistent_with_live,
            [(c.label, c.matched) for c in fit.top_candidates],
        )
    return fit


def _consistent(model: OffsetModel, live: LiveOffset | None, now: datetime) -> bool | None:
    if live is None or not live.trusted or live.hours is None:
        return None
    return math.isclose(model.offset_hours_at(now), live.hours)


def server_frame_to_utc(raw: pd.DataFrame, model: OffsetModel) -> pd.DataFrame:
    """Canonical-column frame whose ``time`` is int server epochs -> same frame with UTC datetimes."""
    from .schema import TIME_DTYPE  # local import keeps the module graph flat

    out = raw.copy()
    utc = model.server_to_utc(out["time"].to_numpy(dtype="int64"))
    out["time"] = pd.Series(pd.to_datetime(utc, unit="s", utc=True), index=out.index).astype(TIME_DTYPE)
    if is_dev_mode() and len(out):
        logger.debug("converted %d bars server->UTC with %s (first offset %+gh, last offset %+gh)",
                     len(out), model.label,
                     (int(raw["time"].iloc[0]) - int(utc[0])) / 3600,
                     (int(raw["time"].iloc[-1]) - int(utc[-1])) / 3600)
    return out
