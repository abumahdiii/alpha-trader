"""Pure helpers of the live signals (phase 6): no MT5, no database, no clock. SUGGESTIONS ONLY.

Everything that decides or prices a live signal is either the backtest's own code or a thin wrapper of it:

* **Decision** -- the caller takes the candidate of the just-closed bar from the backtest's full-history scan
  (``backtest.jobs.scan_for``) and cross-checks it with ``evaluate_checked`` on the closed prefix;
  :func:`candidate_diff` compares the two field by field (EXACT: ``stddev_channel``'s scan is bit-identical to
  ``evaluate``, tests/test_no_lookahead.py). Any difference = a backtest-vs-live mismatch.
* **Indicative entry, levels, volume** (:func:`indicative_levels`, orchestrator decision 3): the entry is not
  known before the fill (``SignalCandidate`` has no entry). For the suggestion the engine uses the CURRENT tick
  -- buy at the ask, sell at the bid, exactly the sides the simulator fills on -- when the tick belongs to the
  entry bar (tick time >= decision time); otherwise (market closed after the confirmation bar, stale tick) the
  last close with the simulator's spread: buy = ``close_t + spread_t`` (ask), sell = ``close_t`` (bid). Then the
  simulator's rules: ``cand.resolve_levels(entry)`` (TP = entry +/- rr * |entry - SL|; a ValueError = the price
  is already beyond the stop -> rejected, like ``gap_through_stop``) and ``risk.sizing.size_position`` with the
  CURRENT account settings and the symbol spec (the call ``simulator.open_position`` makes). The TP shown is
  indicative: the real TP is resolved at the actual fill (open of the entry bar).
* **"The backtest would skip it"** (:func:`backtest_skip_flag`, decision 1): every setup becomes a suggestion,
  but each one says whether the backtest -- one open trade at a time -- would have dropped it because a trade
  opened by an EARLIER setup of the same strategy/symbol was still open (``position_open``). Computed with the
  backtest's own ``simulator.simulate_window`` (which uses the shared helpers ``entry_block`` / ``open_position``
  / ``check_exit`` / ``settle``) over the scan candidates from the chain start to the new bar: identical to a
  manual backtest started at the chain start (tests/test_live_signals.py compares with ``run_backtest``).
* **Entry step / gap class** (:func:`entry_step_info`): the backtest skips an entry when the step from the
  confirmation bar t to the entry bar t+1 is a ``missing`` gap. Live, bar t+1 does not exist at decision time;
  and ``data.gaps`` classifies short gaps (``session_break`` vs ``missing``) with a +/-28-day window and pooled
  regime counts, i.e. partly from LATER bars (known look-ahead of the classifier, decided with the user in
  phase 4). The live path therefore never depends on future data: a step that is not yet known, or a short gap
  whose class can still change, is reported as ``provisional`` (``gap_class_provisional``) instead of being
  rejected; ``weekend`` / ``holiday`` / a plain 1-hour step are final.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd

from ..backtest.models import SKIP_REASON_FA, CostModel, SkipReason, Window
from ..backtest.simulator import BarArrays, ns_to_dt, scan_provider, simulate_window
from ..data.gaps import find_gaps
from ..data.schema import Timeframe
from ..data.symbols import SymbolSpec
from ..logging_setup import get_logger, is_dev_mode
from ..risk.sizing import SizingResult, size_position
from ..storage.account_settings import AccountSettings
from ..strategy.signal import Setup, SignalCandidate

logger = get_logger(__name__)

H1 = timedelta(hours=1)
H1_NS = 3_600_000_000_000
EntrySource = Literal["tick", "last_close"]

LEVELS_INVALID_FA = ("قیمت فعلی بازار آن طرف حد ضرر است (یا سطوح با این قیمت معتبر نیستند)؛ پیشنهادی داده نمی‌شود. "
                     "بک‌تست هم چنین ورودی را رد می‌کرد.")
SIZING_REJECTED_FA = "حجم پیشنهادی قابل محاسبه نبود:"
ENTRY_UNKNOWN_FA = ("کندل ورود هنوز شروع نشده (بازار پس از کندل تایید بسته است)؛ نوع فاصله تا کندل ورود پس از "
                    "بازگشایی مشخص می‌شود. انقضا: پایان اولین کندل پس از بازگشایی.")
GAP_PROVISIONAL_FA = ("نوع این فاصله (وقفه روزانه یا داده گمشده) با داده‌های بعدی ممکن است تغییر کند "
                      "(طبقه‌بندی گپ از ۲۸ روز بعد هم استفاده می‌کند)؛ برچسب موقت است.")


# ---------------------------------------------------------------------------------------------- comparison
def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}{key}."))
        return out
    return {prefix[:-1]: value}


def candidate_dump(cand: SignalCandidate | None) -> dict[str, Any] | None:
    return None if cand is None else cand.model_dump(mode="json")


def candidate_diff(a: SignalCandidate | dict[str, Any] | None, b: SignalCandidate | dict[str, Any] | None) -> list[str]:
    """Fields (dotted, ``extra.sigma``) whose values differ; ``["<presence>"]`` when only one exists. EXACT
    comparison (JSON values: floats compared by value, never with a tolerance)."""
    da = candidate_dump(a) if isinstance(a, SignalCandidate) or a is None else a
    db = candidate_dump(b) if isinstance(b, SignalCandidate) or b is None else b
    if da is None and db is None:
        return []
    if da is None or db is None:
        return ["<presence>"]
    fa, fb = _flatten(da), _flatten(db)
    return sorted(k for k in set(fa) | set(fb) if fa.get(k, _MISSING) != fb.get(k, _MISSING))


_MISSING = object()


def setup_title_fa(cand: SignalCandidate) -> str:
    """Same rule as ``/chart/setups``: the StdDev title table for the channel setups, else the candidate's own
    ``setup_title_fa``, else its slug."""
    if isinstance(cand.setup, Setup):
        from ..strategies.stddev_channel.setups import SETUP_TITLE_FA

        return SETUP_TITLE_FA[cand.setup]
    return cand.setup_title_fa or cand.setup_slug


# ---------------------------------------------------------------------------------------------- indicative levels
@dataclass(frozen=True)
class IndicativeLevels:
    entry: float
    entry_source: EntrySource
    spread_points: int | None  # spread used by the last_close fallback (buy side), else None
    stop_loss: float
    take_profit: float | None  # None when the levels are invalid at this entry
    sizing: SizingResult | None
    rejection_reason_fa: str | None  # None = a complete suggestion

    @property
    def accepted(self) -> bool:
        return self.rejection_reason_fa is None


def choose_entry(direction: str, *, tick_bid: float | None, tick_ask: float | None, tick_fresh: bool,
                 last_close: float, last_spread_price: float) -> tuple[float, EntrySource]:
    """Indicative entry (module docstring): fresh tick -> ask (buy) / bid (sell); else last close + spread (buy,
    the ask) / last close (sell, the bid)."""
    buy = direction == "buy"
    price = tick_ask if buy else tick_bid
    if tick_fresh and price is not None and math.isfinite(price) and price > 0:
        return float(price), "tick"
    return (float(last_close + last_spread_price) if buy else float(last_close)), "last_close"


def indicative_levels(cand: SignalCandidate, *, tick_bid: float | None, tick_ask: float | None, tick_fresh: bool,
                      last_close: float, last_spread_price: float, last_spread_points: int | None,
                      spec: SymbolSpec, account: AccountSettings) -> IndicativeLevels:
    """Entry, SL, indicative TP and volume of a live suggestion with the simulator's rules (module docstring)."""
    entry, source = choose_entry(cand.direction, tick_bid=tick_bid, tick_ask=tick_ask, tick_fresh=tick_fresh,
                                 last_close=last_close, last_spread_price=last_spread_price)
    spread_pts = last_spread_points if source == "last_close" and cand.direction == "buy" else None
    try:
        sl, tp = cand.resolve_levels(entry)
    except ValueError as exc:
        if is_dev_mode():
            logger.debug("live levels %s %s @ %s: entry %s (%s) invalid: %s", cand.symbol, cand.direction,
                         cand.decision_time_utc.isoformat(), entry, source, exc)
        return IndicativeLevels(entry, source, spread_pts, cand.stop_loss, None, None, LEVELS_INVALID_FA)
    sizing = size_position(  # exactly the call simulator.open_position makes (balance = the account settings)
        balance=account.balance, risk_pct=account.risk_pct, entry=entry, stop_loss=sl,
        tick_value=spec.trade_tick_value, tick_size=spec.trade_tick_size, volume_min=spec.volume_min,
        volume_step=spec.volume_step, volume_max=spec.volume_max, contract_size=spec.trade_contract_size,
        leverage=account.leverage,
    )
    reason = None if sizing.accepted else f"{SIZING_REJECTED_FA} {sizing.reason_fa}"
    if is_dev_mode():
        logger.debug("live levels %s %s @ %s: entry=%.5f (%s%s) SL=%.5f TP(indicative)=%.5f balance=%.2f risk=%.2f%% "
                     "-> vol=%s (raw %.6f) actual_risk=%.4f margin=%.2f accepted=%s", cand.symbol, cand.direction,
                     cand.decision_time_utc.isoformat(), entry, source,
                     "" if spread_pts is None else f", spread {spread_pts} pts", sl, tp, account.balance,
                     account.risk_pct, sizing.volume, sizing.raw_volume, sizing.actual_risk, sizing.margin,
                     sizing.accepted)
    return IndicativeLevels(entry, source, spread_pts, sl, tp, sizing, reason)


# ---------------------------------------------------------------------------------------------- entry step
@dataclass(frozen=True)
class EntryStep:
    """What is known about the step confirmation bar t -> entry bar t+1."""

    kind: str  # "none" (1-hour step) | "unknown" | weekend | holiday | session_break | missing
    provisional: bool
    entry_bar_open: datetime | None  # None while unknown
    note_fa: str | None = None

    @property
    def expires(self) -> datetime | None:
        """Close of the entry bar (orchestrator decision 2), ``None`` while it is not known."""
        return None if self.entry_bar_open is None else self.entry_bar_open + H1

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "provisional": self.provisional,
                "entry_bar_time": None if self.entry_bar_open is None else _iso(self.entry_bar_open),
                "note_fa": self.note_fa}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_step_info(times_ns: np.ndarray, t: int, *, tick_utc: datetime | None) -> EntryStep:
    """The step from bar ``t`` to its entry bar, using only data that exists now (no future bars).

    * bar t+1 cached: 1-hour step -> ``none``; else classify with ``data.gaps.find_gaps`` on the cached bars up to
      t+1: ``weekend`` / ``holiday`` are final, ``session_break`` / ``missing`` are provisional (module docstring);
    * not cached, but the latest tick is at/after the decision time: the entry bar is the tick's hour (forming);
      a 1-hour step -> ``none``, otherwise a gap whose class is not known yet -> ``unknown`` (provisional);
    * no tick after the decision time (market closed now): ``unknown``, provisional, entry bar not known yet.
    """
    decision_ns = int(times_ns[t]) + H1_NS
    if t + 1 < len(times_ns):
        nxt = int(times_ns[t + 1])
        entry_open = ns_to_dt(nxt)
        if nxt == decision_ns:
            return EntryStep("none", False, entry_open)
        # every cached bar up to t+1 (what the backtest's classifier would see if the cache ended here)
        frame = pd.DataFrame({"time": pd.to_datetime(times_ns[: t + 2], utc=True)})
        report = find_gaps(frame, Timeframe.H1)
        after = _iso(ns_to_dt(int(times_ns[t])))
        kind = next((g.kind for g in report.gaps if g.after == after), "missing")
        provisional = kind in ("session_break", "missing")
        return EntryStep(kind, provisional, entry_open, GAP_PROVISIONAL_FA if provisional else None)
    if tick_utc is not None and int(pd.Timestamp(tick_utc).value) >= decision_ns:
        entry_open = pd.Timestamp(tick_utc).floor("h").to_pydatetime()
        if int(pd.Timestamp(entry_open).value) == decision_ns:
            return EntryStep("none", False, entry_open)
        return EntryStep("unknown", True, entry_open, GAP_PROVISIONAL_FA)
    return EntryStep("unknown", True, None, ENTRY_UNKNOWN_FA)


# ---------------------------------------------------------------------------------------------- backtest skip flag
@dataclass(frozen=True)
class SkipFlag:
    would_skip: bool
    reason: str | None  # SkipReason value
    reason_fa: str | None
    detail: str | None
    chain_start: datetime
    chain_candidates: int


def backtest_skip_flag(bars: BarArrays, by_bar: dict[int, SignalCandidate], *, start_index: int, t: int,
                       spec: SymbolSpec, account: AccountSettings,
                       cost_model: CostModel | None = None) -> SkipFlag:
    """Would a manual backtest started at bar ``start_index`` drop the candidate of bar ``t`` because a trade of
    an earlier candidate was still open? ``simulate_window`` over ``[start_index, t]`` with the scan candidates
    of those bars (module docstring). The candidate at ``t`` itself is either ``position_open`` or (entry bar
    outside this window) ``entry_outside_window``; only the former is a skip."""
    start_index = max(0, min(start_index, t))
    cand = by_bar[t]
    chain = {i: c for i, c in by_bar.items() if start_index <= i <= t}
    window = Window(index=0, start=ns_to_dt(int(bars.times_ns[start_index])), end=ns_to_dt(int(bars.times_ns[t])) + H1)
    result = simulate_window(bars, window, scan_provider(chain), spec=spec, account=account,
                             cost_model=cost_model or CostModel())
    conf = cand.confirmation_bar_open_utc
    hit = next((s for s in result.skipped if s.confirmation_bar_time == conf), None)
    skip = hit is not None and hit.reason is SkipReason.POSITION_OPEN
    flag = SkipFlag(
        would_skip=skip, reason=hit.reason.value if skip and hit is not None else None,
        reason_fa=SKIP_REASON_FA[SkipReason.POSITION_OPEN] if skip else None,
        detail=hit.detail if skip and hit is not None else None, chain_start=window.start,
        chain_candidates=len(chain),
    )
    if is_dev_mode():
        logger.debug("live backtest-skip flag %s @ %s: chain %s..%s (%d candidates, %d trades) -> %s%s",
                     cand.symbol, cand.decision_time_utc.isoformat(), window.start.isoformat(),
                     window.end.isoformat(), len(chain), len(result.trades), "SKIP position_open" if skip else "no skip",
                     f" ({flag.detail})" if flag.detail else "")
    return flag


__all__ = [
    "ENTRY_UNKNOWN_FA",
    "EntryStep",
    "GAP_PROVISIONAL_FA",
    "IndicativeLevels",
    "LEVELS_INVALID_FA",
    "SkipFlag",
    "backtest_skip_flag",
    "candidate_diff",
    "candidate_dump",
    "choose_entry",
    "entry_step_info",
    "indicative_levels",
    "setup_title_fa",
]
