"""Write the synthetic fixture as a real-layout OHLCV cache (used by the ``tgc_testdata`` skill).

The skill script (``.claude/skills/tgc_testdata/scripts/seed_test_data.py``, local-only) is a thin CLI
around :func:`seed_cache`; all logic lives here so it is committed and unit-tested.

Safety:

* never connects to MT5, never reads ``.env`` or settings;
* refuses to write into the repository's real ``data/`` directory (or anything inside it) unless
  ``force=True`` -- and then first copies the existing ``cache/`` tree to
  ``<out>/backup/tgc_testdata-<UTC timestamp>/``;
* every file is tagged ``source=seed`` (Parquet metadata / spec JSON), so the engine replaces seed data
  with a full MT5 fetch as soon as it can (``MarketDataService.update``).
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from alpha_engine.config import DEFAULT_DATA_DIR
from alpha_engine.data.cache import CacheMeta, OhlcvCache
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.data.timezone import OffsetModel

from .synthetic_ohlcv import (
    DEFAULT_END,
    DEFAULT_OFFSET_MODEL,
    DEFAULT_SEED,
    DEFAULT_START,
    SYMBOLS,
    generate_dataset,
    symbol_info_dict,
)


class SeedRefused(RuntimeError):
    pass


@dataclass
class SeedReport:
    out: Path
    files: list[Path] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)
    expected_gaps: dict[str, dict[str, int]] = field(default_factory=dict)
    backup: Path | None = None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def seed_cache(
    out: Path | str,
    *,
    force: bool = False,
    repo_data_dir: Path | str = DEFAULT_DATA_DIR,
    symbols: Sequence[str] = SYMBOLS,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    seed: int = DEFAULT_SEED,
    offset_model: OffsetModel = DEFAULT_OFFSET_MODEL,
) -> SeedReport:
    out_dir = Path(out).resolve()
    data_dir = Path(repo_data_dir).resolve()
    report = SeedReport(out=out_dir)
    if _inside(out_dir, data_dir):
        if not force:
            raise SeedRefused(
                f"{out_dir} is inside the real data directory {data_dir}; pass --force to overwrite "
                "(a backup is taken first)"
            )
        existing = out_dir / "cache"
        if existing.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = out_dir / "backup" / f"tgc_testdata-{stamp}" / "cache"
            shutil.copytree(existing, backup)
            report.backup = backup

    cache = OhlcvCache(out_dir)
    dataset = generate_dataset(symbols, start, end, seed, offset_model)
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for (symbol, tf), series in dataset.items():
        meta = CacheMeta(
            symbol=symbol, timeframe=tf.value, source="seed", offset_model=offset_model.label,
            first_available_utc=series.meta["start_utc"], requested_start_utc=series.meta["start_utc"],
            fetched_at_utc=fetched,
            extra={"synthetic": True, "seed": seed, "expected_gaps": series.meta["expected_gaps"],
                   "missing_events": series.meta["missing_events"], "high_spread": series.meta["high_spread"],
                   "price_gap": series.meta["price_gap"], "holidays": series.meta["holidays"]},
        )
        cache.write(symbol, tf, series.utc, meta)
        key = f"{symbol}_{tf.value}"
        report.files.append(cache.series_path(symbol, tf))
        report.rows[key] = len(series.utc)
        report.expected_gaps[key] = series.meta["expected_gaps"]
    for symbol in symbols:
        cache.write_spec(SymbolSpec.from_mt5(symbol_info_dict(symbol)), "seed")
        report.files.append(cache.spec_path(symbol))
    return report
