"""Dynamic validation of a plugin (parent-side comparisons; the worker is untrusted) and the upload pipeline.

:func:`validate_plugin` = :func:`~.validator.validate_source` (static) and, only if that passes,
:func:`validate_dynamic` on deterministic synthetic data (:func:`~.fixture.random_walk`, :data:`FIXTURE_BARS`
H1 bars, seed :data:`FIXTURE_SEED`, symbol ``XAUUSD.x``, default account settings, the schema's default params):

1. **describe** -- ``param_schema`` rebuilt and its defaults valid; declared name / version / title equal the
   literal values the static check read from the file;
2. **scan** on the full fixture -- every candidate re-validated (``SignalCandidate`` + this strategy / symbol /
   params hash / one per bar / chronological / not before ``first_valid_index``); at least one candidate
   (without candidates the checks below would prove nothing);
3. **determinism** -- the same scan again in the SAME worker and once more in a FRESH worker process: all three
   identical (catches unseeded randomness, global state, hash-order effects);
4. **prefix equivalence** -- ``evaluate`` on the CLOSED prefix (H1 ``[:t+1]``, H4 closed at ``open_t + 1h``) of
   every candidate bar (seeded sample of at most :data:`MAX_PREFIX_CANDIDATES`) and of
   :data:`PREFIX_OTHER_BARS` seeded non-candidate bars equals the scan's answer on that bar (candidate or none):
   ``scan`` sees the whole history, ``evaluate`` only the past, so any look-ahead in ``scan`` shows up here;
5. **future mutation / truncation** -- for :data:`MUTATION_CUTOFFS` seeded cut-offs ``c``: the bars ``>= c``
   replaced by a different random continuation (:func:`~.fixture.mutate_after`), and the history cut at ``c``:
   the candidates on bars ``< c`` must not change;
6. **budget** -- all plugin work (every request after the first worker is up) within ``budget_s``
   (:data:`VALIDATION_BUDGET_S` = 30 s); each worker has the memory cap of :mod:`.host` (1 GiB).

Floats are compared with a relative tolerance of 1e-9 (a vectorised scan and a per-bar evaluate may differ in the
last bits); everything else exactly.

Worked example (the template, default params, 3000 bars): 82 candidates, 40 of them + 20 other bars checked on
their prefixes, 2 cut-offs x (mutation + truncation), 3 identical scans: about 2-4 s of plugin work plus two
worker start-ups of about 1.5 s each.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettings
from ..strategy.signal import SignalCandidate
from .fixture import mutate_after, random_walk
from .host import (
    DEFAULT_MEMORY_LIMIT,
    PluginDescription,
    PluginTimeout,
    PluginWorkerError,
    WorkerSession,
    closed_h4_for,
    parse_description,
    run_evaluate,
    run_scan,
)
from .validator import StaticReport, validate_source

logger = get_logger(__name__)

VALIDATION_BUDGET_S = 30.0
FIXTURE_BARS = 3000
FIXTURE_SEED = 20260928
VALIDATION_SYMBOL = "XAUUSD.x"
MAX_PREFIX_CANDIDATES = 40
PREFIX_OTHER_BARS = 20
MUTATION_CUTOFFS = 2
FLOAT_REL_TOL = 1e-9
MAX_REPORTED_MISMATCHES = 3
NOT_DETERMINISTIC_FA = ("رفتار سیستم قطعی نیست: اجرای دوباره scan روی همان داده نتیجه متفاوتی داد "
                        "(عدد تصادفی بدون seed، وضعیت سراسری یا وابستگی به ترتیب).")


def _iso(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%dT%H:%MZ")


def same_value(a: Any, b: Any) -> bool:
    """Deep equality of JSON-like values with a relative float tolerance (:data:`FLOAT_REL_TOL`)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b if isinstance(a, bool) and isinstance(b, bool) else False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            return a == b
        return math.isclose(float(a), float(b), rel_tol=FLOAT_REL_TOL, abs_tol=1e-12)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_value(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _by_bar(candidates: Iterable[SignalCandidate], times_ns: np.ndarray) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for cand in candidates:
        conf_ns = pd.Timestamp(cand.confirmation_bar_open_utc).as_unit("ns").value
        out[int(np.searchsorted(times_ns, conf_ns))] = cand.model_dump(mode="json")
    return out


def _first_difference(a: Mapping[int, dict[str, Any]], b: Mapping[int, dict[str, Any]],
                      bars: Iterable[int] | None = None) -> int | None:
    keys = sorted(set(a) | set(b)) if bars is None else sorted(bars)
    for t in keys:
        x, y = a.get(t), b.get(t)
        if x is None and y is None:
            continue
        if x is None or y is None or not same_value(x, y):
            return t
    return None


def _describe(cand: Mapping[str, Any] | None) -> str:
    if cand is None:
        return "بدون کاندید"
    return (f"{cand.get('direction')} {cand.get('setup')} ref={_short(cand.get('reference_price'))} "
            f"SL={_short(cand.get('stop_loss'))}")


def _short(value: Any) -> str:
    return f"{value:.6g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


@dataclass
class DynamicReport:
    """Result of :func:`validate_dynamic`; :meth:`summary` is stored with the plugin and returned by the API."""

    budget_s: float
    errors_fa: list[str] = field(default_factory=list)
    bars: int = 0
    candidates: int = 0
    prefix_checks: int = 0
    determinism: bool | None = None
    future_mutation: bool | None = None
    elapsed_s: float = 0.0
    boot_s: float | None = None
    peak_memory_mib: float | None = None
    description: PluginDescription | None = None

    @property
    def ok(self) -> bool:
        return not self.errors_fa and self.description is not None

    def summary(self) -> dict[str, Any]:
        return {"bars": self.bars, "candidates": self.candidates, "prefix_checks": self.prefix_checks,
                "determinism": self.determinism, "future_mutation": self.future_mutation,
                "elapsed_s": round(self.elapsed_s, 3), "budget_s": self.budget_s,
                "worker_boot_s": None if self.boot_s is None else round(self.boot_s, 3),
                "peak_memory_mib": self.peak_memory_mib}


class _Budget:
    def __init__(self, seconds: float) -> None:
        self.seconds = float(seconds)
        self.used = 0.0
        self._started: float | None = None

    def start(self) -> None:
        self._started = time.monotonic()

    def pause(self) -> None:
        if self._started is not None:
            self.used += time.monotonic() - self._started
            self._started = None

    def remaining(self) -> float:
        spent = self.used + (time.monotonic() - self._started if self._started is not None else 0.0)
        left = self.seconds - spent
        if left <= 0:
            raise PluginTimeout("timeout", f"اعتبارسنجی پویا از سقف زمان {self.seconds:.0f} ثانیه گذشت.", "budget")
        return left


def _sample(rng: np.random.Generator, items: list[int], k: int) -> list[int]:
    if len(items) <= k:
        return list(items)
    return sorted(int(x) for x in rng.choice(np.asarray(items, dtype=np.int64), size=k, replace=False))


def validate_dynamic(source: str, static: StaticReport, *, budget_s: float = VALIDATION_BUDGET_S,
                     memory_limit: int = DEFAULT_MEMORY_LIMIT, n_bars: int = FIXTURE_BARS,
                     seed: int = FIXTURE_SEED) -> DynamicReport:
    """Run the dynamic checks (module docstring) on a statically valid ``source``."""
    report = DynamicReport(budget_s=float(budget_s))
    budget = _Budget(budget_s)
    started = time.perf_counter()
    h1, h4 = random_walk(n_bars, seed=seed)
    times = pd.DatetimeIndex(h1["time"]).as_unit("ns").asi8
    report.bars = len(h1)
    account = AccountSettings()
    symbol = VALIDATION_SYMBOL
    rng = np.random.default_rng(seed)
    label = f"{static.name} v{static.version} (validation)"
    try:
        with WorkerSession(source, memory_limit=memory_limit, label=label) as session:
            desc = parse_description(session.start())
            report.boot_s = session.boot_s
            if (desc.name, desc.version, desc.title_fa) != (static.name, static.version, static.title_fa):
                report.errors_fa.append("نام، نسخه یا عنوان اعلام‌شده در زمان اجرا با مقادیر ثابت فایل یکی نیست.")
                return report
            params, _ = desc.param_schema.validate({})
            budget.start()
            if is_dev_mode():
                logger.debug("plugin validation %s: fixture %d H1 / %d H4 bars, params %s", label, len(h1), len(h4),
                             params)

            # 2. full scan
            full = run_scan(session, desc, h1, h4, params, account, symbol, budget.remaining())
            report.candidates = len(full.candidates)
            if not full.candidates:
                report.errors_fa.append(f"سیستم روی داده آزمایشی ({len(h1)} کندل H1) هیچ کاندیدی نداد؛ بدون کاندید، "
                                        "برابری scan و evaluate و نبود نگاه به آینده قابل بررسی نیست.")
                return report
            base = _by_bar(full.candidates, times)
            first_valid = full.first_valid_index
            if first_valid is not None and min(base) < first_valid:
                report.errors_fa.append(f"کاندید روی کندل {_iso(times[min(base)])} قبل از اولین کندل معتبر اعلام‌شده "
                                        f"(first_valid_index={first_valid}) است.")
            if is_dev_mode():
                logger.debug("plugin validation %s: scan %d candidates first_valid=%s plugin %.3f s", label,
                             len(full.candidates), first_valid, full.plugin_s or float("nan"))

            # 3a. determinism in the same process
            again = run_scan(session, desc, h1, h4, params, account, symbol, budget.remaining())
            same_process = _first_difference(base, _by_bar(again.candidates, times)) is None
            if not same_process:
                report.determinism = False
                report.errors_fa.append(NOT_DETERMINISTIC_FA)
                return report

            # 4. prefix equivalence
            cand_bars = _sample(rng, sorted(base), MAX_PREFIX_CANDIDATES)
            lo = first_valid if first_valid is not None else 0
            others = [t for t in range(lo, len(h1)) if t not in base]
            other_bars = _sample(rng, others, PREFIX_OTHER_BARS)
            mismatches = 0
            for t in sorted(set(cand_bars) | set(other_bars) | {len(h1) - 1}):
                got = run_evaluate(session, desc, h1.iloc[: t + 1], closed_h4_for(times, h4, t), params, account,
                                   symbol, budget.remaining())
                report.prefix_checks += 1
                got_dump = None if got is None else got.model_dump(mode="json")
                want = base.get(t)
                if (got_dump is None) != (want is None) or (got_dump is not None and not same_value(got_dump, want)):
                    mismatches += 1
                    if mismatches <= MAX_REPORTED_MISMATCHES:
                        report.errors_fa.append(
                            f"scan با evaluate روی کندل {_iso(times[t])} یکی نیست (scan: {_describe(want)}؛ "
                            f"evaluate روی کندل‌های بسته‌شده: {_describe(got_dump)}). scan باید دقیقا همان کاندیدهای "
                            "evaluate را بدهد؛ این معمولا یعنی نگاه به آینده (shift منفی یا ایندکس بعد از کندل جاری).")
            if is_dev_mode():
                logger.debug("plugin validation %s: prefix checks %d mismatches %d", label, report.prefix_checks,
                             mismatches)

            # 5. future mutation + truncation
            span = np.arange(max(lo + 1, int(len(h1) * 0.3)), max(lo + 2, int(len(h1) * 0.9)))
            cutoffs = sorted(int(c) for c in rng.choice(span, size=min(MUTATION_CUTOFFS, len(span)), replace=False))
            future_ok = True
            for i, cut in enumerate(cutoffs):
                h1m, h4m = mutate_after(h1, cut, seed + 1 + i)
                mutated = run_scan(session, desc, h1m, h4m, params, account, symbol, budget.remaining())
                diff = _first_difference(base, _by_bar(mutated.candidates, times), [t for t in range(cut)])
                if diff is not None:
                    future_ok = False
                    report.errors_fa.append(f"نگاه به آینده: با تغییر کندل‌های از {_iso(times[cut])} به بعد، کاندید "
                                            f"کندل {_iso(times[diff])} (قبل از آن) تغییر کرد.")
                h1t = h1.iloc[:cut]
                truncated = run_scan(session, desc, h1t, closed_h4_for(times, h4, cut - 1), params, account, symbol,
                                     budget.remaining())
                diff = _first_difference(base, _by_bar(truncated.candidates, times[:cut]), [t for t in range(cut)])
                if diff is not None:
                    future_ok = False
                    report.errors_fa.append(f"نگاه به آینده: با حذف کندل‌های از {_iso(times[cut])} به بعد، کاندید "
                                            f"کندل {_iso(times[diff])} تغییر کرد.")
            report.future_mutation = future_ok
            report.peak_memory_mib = _mib(session.peak_memory())
            budget.pause()

        # 3b. determinism across processes (fresh worker)
        with WorkerSession(source, memory_limit=memory_limit, label=label + " #2") as fresh:
            desc2 = parse_description(fresh.start())
            budget.start()
            other = run_scan(fresh, desc2, h1, h4, params, account, symbol, budget.remaining())
            budget.pause()
            peak2 = _mib(fresh.peak_memory())
            if peak2 is not None:
                report.peak_memory_mib = max(report.peak_memory_mib or 0.0, peak2)
        cross_process = _first_difference(base, _by_bar(other.candidates, times)) is None
        report.determinism = same_process and cross_process
        if not report.determinism:
            report.errors_fa.append(NOT_DETERMINISTIC_FA)
        report.description = desc
    except PluginWorkerError as exc:
        report.errors_fa.append(exc.message_fa)
        if is_dev_mode():
            logger.debug("plugin validation %s failed: %s (%s)", label, exc.kind, exc.detail)
    finally:
        budget.pause()
        report.elapsed_s = time.perf_counter() - started
    if is_dev_mode():
        logger.debug("plugin validation %s: ok=%s candidates=%d prefix=%d determinism=%s future=%s plugin %.2f s "
                     "total %.2f s peak %.0f MiB", label, report.ok, report.candidates, report.prefix_checks,
                     report.determinism, report.future_mutation, budget.used, report.elapsed_s,
                     report.peak_memory_mib or float("nan"))
    return report


def _mib(value: int | None) -> float | None:
    return None if value is None else round(value / (1 << 20), 1)


@dataclass
class PluginValidation:
    """Static + dynamic result of one uploaded file (:func:`validate_plugin`)."""

    static: StaticReport
    dynamic: DynamicReport | None = None
    source: str | None = None  # decoded text (only when the static check could decode it)

    @property
    def ok(self) -> bool:
        return self.static.ok and self.dynamic is not None and self.dynamic.ok

    @property
    def errors_fa(self) -> list[str]:
        return list(self.static.errors_fa) + (list(self.dynamic.errors_fa) if self.dynamic is not None else [])

    def validation(self) -> dict[str, Any]:
        """``{"static": [passed check groups], "dynamic": {...} | null}`` (stored and returned by the API)."""
        return {"static": list(self.static.checks) if self.static.ok else [],
                "dynamic": None if self.dynamic is None else self.dynamic.summary()}

    def report(self) -> dict[str, Any]:
        return {"ok": self.ok, "name": self.static.name, "version": self.static.version,
                "title_fa": self.static.title_fa, "sha256": self.static.sha256, "errors_fa": self.errors_fa,
                "validation": self.validation()}


def validate_plugin(data: str | bytes, *, reserved_names: Iterable[str] = (), budget_s: float = VALIDATION_BUDGET_S,
                    memory_limit: int = DEFAULT_MEMORY_LIMIT, n_bars: int = FIXTURE_BARS,
                    dynamic: bool = True) -> PluginValidation:
    """Static checks, then (if they pass and ``dynamic``) the sandboxed dynamic checks."""
    from .validator import decode_source

    static = validate_source(data, reserved_names=reserved_names)
    result = PluginValidation(static=static)
    if is_dev_mode():
        logger.debug("plugin static validation: %d bytes sha=%s name=%s v%s ok=%s errors=%d", static.size_bytes,
                     (static.sha256 or "")[:12], static.name, static.version, static.ok, len(static.errors_fa))
    if not static.ok:
        return result
    result.source = decode_source(data, StaticReport())
    if dynamic and result.source is not None:
        result.dynamic = validate_dynamic(result.source, static, budget_s=budget_s, memory_limit=memory_limit,
                                          n_bars=n_bars)
    return result


__all__ = [
    "FIXTURE_BARS",
    "FIXTURE_SEED",
    "VALIDATION_BUDGET_S",
    "DynamicReport",
    "PluginValidation",
    "same_value",
    "validate_dynamic",
    "validate_plugin",
]
