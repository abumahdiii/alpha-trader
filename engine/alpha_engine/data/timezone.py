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
2. **Historical fit** -- on XAUUSD.x H1 server-time bars, every candidate model is scored separately on
   three anchors of the FX/gold session (America/New_York local time):

   * ``weekly_close`` -- every trading week (but the last) ends with a bar ending 17:00 NY on Friday
     (or on Thursday when Friday is an exchange holiday, e.g. Good Friday);
   * ``daily_break``  -- every *recurring* short intra-week gap (<= 6 h; its server-clock slot occurs
     in at least a quarter of the weeks, so holiday early closes and one-off data holes are not
     evidence) starts at 17:00 NY, Monday to Thursday (gold's daily break);
   * ``weekly_open``  -- every trading week (but the first) opens with a bar starting Sunday 18:00 NY.

   The model is chosen by weekly close + daily break (equal weight, by match *ratio*); the weekly
   open only breaks ties, because brokers often reopen on a fixed UTC time (the live broker opens gold
   on Sunday 23:00 UTC all year, i.e. 19:00 NY in US summer, so the open anchor fails every summer
   week). The daily-break anchor is used only if there are at least ``DAILY_BREAK_MIN_CHECKS``
   recurring short gaps and the candidate that best explains them explains at least half of them and
   at least half of the weekly closes (a break at another local time must not vote for a model the
   closes reject). Remaining ties: consistency with the live offset, then
   us_dst > eu_dst > fixed. Per-anchor matched/checks/ratio are reported in :class:`OffsetFit`.

   Worked example (real 5-year XAUUSD.x H1 cache, broker server clock == UTC): ``fixed(0)`` puts
   235/261 weekly closes and 923/997 recurring daily breaks on the anchors; the runner-up
   ``us_dst(-1)`` only 184/261 and 725/997. The misses are the winter 2021/22 weeks, when this
   broker closed gold at 16:00 NY (21:00 UTC), plus US-holiday early closes; the weekly open matches
   only 105/261 (Sunday 23:00 UTC is 18:00 NY in winter only) and is therefore just a tie-breaker.
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
WEEKLY_CLOSE_ANCHOR = (4, 17)  # Friday 17:00 New York (weekday Mon=0) ...
WEEKLY_CLOSE_HOLIDAY_WEEKDAY = 3  # ... or Thursday 17:00 when Friday is a holiday (Good Friday)
WEEKLY_OPEN_ANCHOR = (6, 18)  # Sunday 18:00 New York
DAILY_BREAK_HOUR = 17  # the daily break starts 17:00 New York ...
DAILY_BREAK_WEEKDAYS = (0, 1, 2, 3)  # ... Monday to Thursday
DAILY_BREAK_MAX_SECONDS = 6 * 3600  # intra-week gaps at most this long (bar end -> next open) are candidates
DAILY_BREAK_MIN_CHECKS = 10
DAILY_BREAK_MIN_WEEK_SHARE = 0.25  # a short-gap slot must occur in >= 25% of the weeks to be a break check
WEEK_SPLIT_SECONDS = 36 * 3600  # a gap this long separates trading weeks (weekday holidays are ~25 h)
AnchorName = Literal["weekly_close", "daily_break", "weekly_open"]
ANCHOR_DESCRIPTIONS: dict[str, str] = {
    "weekly_close": "weekly close Fri 17:00 (Thu 17:00 before a Friday holiday) America/New_York",
    "daily_break": "recurring daily break starts 17:00 America/New_York, Mon-Thu",
    "weekly_open": "weekly open Sun 18:00 America/New_York",
}

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


def dst_in_effect(utc_epochs: Sequence[int] | np.ndarray, kind: Literal["us_dst", "eu_dst"]) -> np.ndarray:
    """Per UTC instant: is America/New_York (``us_dst``) / Europe/London (``eu_dst``) on summer time?"""
    arr = np.asarray(utc_epochs, dtype="int64")
    if arr.size == 0:
        return np.zeros(0, dtype=bool)
    return _dst_flags(arr, kind)


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
    matched: int  # matches on the primary anchors (weekly close + daily break when used)
    weekly_close: int = 0
    daily_break: int = 0
    weekly_open: int = 0


class AnchorScore(BaseModel):
    """How well the inferred model fits one anchor."""

    name: AnchorName
    description: str
    role: Literal["primary", "tie_break", "unused"]
    matched: int
    checks: int
    ratio: float


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
    anchors: list[AnchorScore] = []  # per-anchor scores of the inferred model (empty: nothing inferred)
    anchor: str = ("primary: weekly close Fri 17:00 + daily break 17:00 Mon-Thu; "
                   "tie-break: weekly open Sun 18:00 (America/New_York)")
    fitted_at_utc: str | None = None


def _week_segments(server_epochs: np.ndarray) -> list[tuple[int, int]]:
    """Index ranges ``[start, end]`` of contiguous trading weeks (split at gaps >= 36 h)."""
    if server_epochs.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(server_epochs) >= WEEK_SPLIT_SECONDS)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [server_epochs.size - 1]))
    return list(zip(starts.tolist(), ends.tolist(), strict=True))


def _anchor_hits(utc_epochs: np.ndarray, weekdays: Sequence[int], hour: int) -> np.ndarray:
    if utc_epochs.size == 0:
        return np.zeros(0, dtype=bool)
    local = pd.DatetimeIndex(pd.to_datetime(utc_epochs, unit="s", utc=True)).tz_convert(ANCHOR_ZONE)
    return np.asarray(np.isin(local.weekday, list(weekdays)) & (local.hour == hour)
                      & (local.minute == 0) & (local.second == 0))


def _daily_break_points(server_epochs: np.ndarray, timeframe_seconds: int, weeks: int) -> np.ndarray:
    """Server-clock start (= end of the bar before it) of every *recurring* short intra-week gap.

    A short gap recurs if its server-clock slot (time of day, length) occurs at least
    ``max(3, DAILY_BREAK_MIN_WEEK_SHARE * weeks)`` times. The slot is model-independent, so every
    candidate model is checked against the same points.
    """
    if server_epochs.size < 2:
        return np.zeros(0, dtype="int64")
    gap = np.diff(server_epochs) - timeframe_seconds  # bar end -> next open
    pos = np.flatnonzero((gap > 0) & (gap <= DAILY_BREAK_MAX_SECONDS))
    starts = (server_epochs[pos] + timeframe_seconds).astype("int64")
    if starts.size == 0:
        return starts
    slots = (starts % 86400) * 100_000 + gap[pos]
    values, inverse, counts = np.unique(slots, return_inverse=True, return_counts=True)
    keep = counts[inverse] >= max(3, math.ceil(DAILY_BREAK_MIN_WEEK_SHARE * weeks))
    if is_dev_mode():
        logger.debug("offset fit: %d short intra-week gaps, %d on recurring slots (%d slots kept of %d)",
                     starts.size, int(keep.sum()), int((counts >= max(3, math.ceil(
                         DAILY_BREAK_MIN_WEEK_SHARE * weeks))).sum()), values.size)
    return starts[keep]


def _ratio(matched: int, checks: int) -> float:
    return round(matched / checks, 4) if checks else 0.0


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

    ``matched``/``checks``/``match_ratio`` of the result cover the primary anchors (weekly close, plus the
    daily break when it is used); ``anchors`` has the per-anchor breakdown. ``low_confidence`` is set
    when any primary anchor matches less than half of its checks.

    Raises :class:`OffsetError` only if there is nothing to fit *and* no override/trusted live offset.
    """
    s = np.sort(np.unique(np.asarray(server_epochs, dtype="int64")))
    segments = _week_segments(s)
    # Weekly close of every week but the last (it may be cut by the data end / still running) and
    # weekly open of every week but the first (it may start mid-week at the data start).
    points: dict[str, np.ndarray] = {
        "weekly_close": np.array([s[e] + timeframe_seconds for _, e in segments[:-1]], dtype="int64"),
        "daily_break": _daily_break_points(s, timeframe_seconds, len(segments)),
        "weekly_open": np.array([s[b] for b, _ in segments[1:]], dtype="int64"),
    }
    rules: dict[str, tuple[Sequence[int], int]] = {
        "weekly_close": ((WEEKLY_CLOSE_ANCHOR[0], WEEKLY_CLOSE_HOLIDAY_WEEKDAY), WEEKLY_CLOSE_ANCHOR[1]),
        "daily_break": (DAILY_BREAK_WEEKDAYS, DAILY_BREAK_HOUR),
        "weekly_open": ((WEEKLY_OPEN_ANCHOR[0],), WEEKLY_OPEN_ANCHOR[1]),
    }
    n_checks = {name: int(p.size) for name, p in points.items()}
    now = now_utc or datetime.now(timezone.utc)

    per_model: list[tuple[OffsetModel, dict[str, int]]] = []
    if any(n_checks.values()):
        for model in candidates or all_candidate_models():
            hits = {name: int(_anchor_hits(model.server_to_utc(p), *rules[name]).sum()) if p.size else 0
                    for name, p in points.items()}
            per_model.append((model, hits))

    # The daily-break anchor votes only if the data really has a daily break at 17:00 New York: the
    # model that best explains the breaks must explain at least half of them AND at least half of the
    # weekly closes (a break at another local time must not pull the fit to a model the closes reject).
    breaker = max(per_model, key=lambda mh: (mh[1]["daily_break"], mh[1]["weekly_close"]), default=None)
    best_break = breaker[1]["daily_break"] if breaker else 0
    use_break = (n_checks["daily_break"] >= DAILY_BREAK_MIN_CHECKS and best_break * 2 >= n_checks["daily_break"]
                 and breaker is not None and breaker[1]["weekly_close"] * 2 >= n_checks["weekly_close"])
    primary = [a for a in ("weekly_close", "daily_break") if n_checks[a] and (a != "daily_break" or use_break)]
    denominator = math.prod(n_checks[a] for a in primary)

    def _primary_score(hits: dict[str, int]) -> int:
        # sum of the primary anchors' match ratios, scaled by their common denominator (exact integers)
        return sum(hits[a] * (denominator // n_checks[a]) for a in primary)

    scores: list[tuple[tuple, OffsetModel, dict[str, int]]] = []
    for model, hits in per_model:
        consistent = _consistent(model, live, now)
        rank = (_primary_score(hits), hits["weekly_open"], {True: 2, None: 1, False: 0}[consistent],
                _KIND_PRIORITY[model.kind])
        scores.append((rank, model, hits))
    scores.sort(key=lambda item: item[0], reverse=True)

    inferred: OffsetModel | None = None
    best_hits: dict[str, int] = {name: 0 for name in points}
    if scores and sum(scores[0][2].values()) > 0:
        _, inferred, best_hits = scores[0]
    matched = sum(best_hits[a] for a in primary)
    checks = sum(n_checks[a] for a in primary)
    ratio = matched / checks if checks else 0.0
    anchors = [
        AnchorScore(name=name, description=ANCHOR_DESCRIPTIONS[name],  # type: ignore[arg-type]
                    role="primary" if name in primary else ("tie_break" if name == "weekly_open" else "unused"),
                    matched=best_hits[name], checks=n_checks[name], ratio=_ratio(best_hits[name], n_checks[name]))
        for name in ("weekly_close", "daily_break", "weekly_open")
    ] if inferred is not None else []
    weakest = min((a.ratio for a in anchors if a.role == "primary"), default=0.0)

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
        low_confidence=source != "override" and weakest < 0.5,
        top_candidates=[
            CandidateScore(label=m.label, matched=sum(h[a] for a in primary), weekly_close=h["weekly_close"],
                           daily_break=h["daily_break"], weekly_open=h["weekly_open"])
            for _, m, h in scores[:5]
        ],
        anchors=anchors,
        fitted_at_utc=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if is_dev_mode():
        logger.debug(
            "offset fit: effective=%s (source=%s) inferred=%s matched=%d/%d weeks=%d live=%s trusted=%s "
            "consistent=%s top=%s",
            fit.effective_label, fit.source, fit.inferred_label, matched, checks, fit.weeks,
            fit.live_offset_hours, fit.live_trusted, fit.consistent_with_live,
            [(c.label, c.weekly_close, c.daily_break, c.weekly_open) for c in fit.top_candidates],
        )
        logger.debug("offset fit anchors: %s (daily break %s: best candidate explains %d/%d short gaps)",
                     [(a.name, a.role, a.matched, a.checks, a.ratio) for a in fit.anchors],
                     "used" if use_break else "not used", best_break, n_checks["daily_break"])
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
