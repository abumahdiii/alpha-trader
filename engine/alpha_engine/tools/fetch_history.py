"""Fetch / update the local OHLCV cache from MT5 (read-only).

Usage (from ``engine/``)::

    .venv\\Scripts\\python.exe -m alpha_engine.tools.fetch_history [--years 5] [--symbols XAUUSD.x,BRNUSD.x]
                                                              [--timeframes H1,H4]

Steps: attach to the running terminal (attach mode; login only if MT5_PASSWORD is set), fit the broker
offset model on XAUUSD.x H1, cache the symbol specs, then fetch/update every symbol x timeframe
(chunked, up to ``--years`` back) and print a summary. Exit code 0 = all good, 1 = some series failed,
2 = could not connect.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from ..config import Settings, get_settings
from ..data.gaps import find_gaps
from ..data.schema import Timeframe
from ..data.symbols import validate_symbol_name
from ..data.timezone import OffsetError
from ..logging_setup import configure_logging, get_logger
from ..market_data import MarketDataService
from ..mt5_adapter import DEFAULT_YEARS, Mt5Adapter, Mt5Error

logger = get_logger(__name__)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m alpha_engine.tools.fetch_history",
                                     description="Fetch/update the OHLCV cache from MT5 (read-only).")
    parser.add_argument("--years", type=float, default=DEFAULT_YEARS, help="history depth (default 5)")
    parser.add_argument("--symbols", type=_csv, default=None, help="comma-separated (default ENGINE_SYMBOLS)")
    parser.add_argument("--timeframes", type=_csv, default=["H1", "H4"], help="comma-separated (default H1,H4)")
    parser.add_argument("--full", action="store_true", help="ignore the cache and refetch everything")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    adapter: Mt5Adapter | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    configure_logging(settings)
    if not 0 < args.years <= 20:
        print("--years must be in (0, 20]", file=out)
        return 2
    try:
        symbols = [validate_symbol_name(s) for s in (args.symbols or settings.engine_symbols)]
        timeframes = [Timeframe.parse(t) for t in args.timeframes]
    except ValueError as exc:
        print(f"invalid argument: {exc}", file=out)
        return 2

    adapter = adapter or Mt5Adapter(settings)
    status = adapter.connect()
    print(f"MT5: state={status.state} server={status.server} login={status.login_masked} "
          f"trade_mode={status.trade_mode} ({status.message})", file=out)
    if status.state != "connected":
        return 2

    failures = 0
    try:
        # Series outside ENGINE_SYMBOLS are allowed here (explicit --symbols), so widen the service view.
        wide = settings.model_copy(update={"engine_symbols": tuple(dict.fromkeys([*settings.engine_symbols, *symbols]))})
        service = MarketDataService.create(wide, adapter, clock=adapter.clock)
        try:
            fit = service.refit_offset(years=args.years)
            print(f"offset: effective={fit.effective_label} source={fit.source} inferred={fit.inferred_label} "
                  f"match={fit.matched}/{fit.checks} ({fit.match_ratio:.0%}) weeks={fit.weeks} "
                  f"live={fit.live_offset_hours} trusted={fit.live_trusted} consistent={fit.consistent_with_live}",
                  file=out)
            if fit.low_confidence:
                print("WARNING: low-confidence offset fit; check the report / set MT5_SERVER_UTC_OFFSET", file=out)
        except (OffsetError, Mt5Error) as exc:
            print(f"offset fit failed: {exc}", file=out)
            return 1

        for symbol in symbols:
            try:
                spec = adapter.symbol_spec(symbol)
                service.cache.write_spec(spec, "mt5")
                print(f"spec {symbol}: digits={spec.digits} point={spec.point:g} contract={spec.trade_contract_size:g} "
                      f"tick_value={spec.trade_tick_value:g} tick_size={spec.trade_tick_size:g} "
                      f"volume min/step/max={spec.volume_min:g}/{spec.volume_step:g}/{spec.volume_max:g}", file=out)
            except Mt5Error as exc:
                failures += 1
                print(f"spec {symbol}: FAILED {exc}", file=out)
                continue
            for tf in timeframes:
                try:
                    result = service.update(symbol, tf, years=args.years, force_full=args.full)
                except (Mt5Error, OffsetError) as exc:
                    failures += 1
                    print(f"{symbol} {tf.value}: FAILED {exc}", file=out)
                    continue
                cached = service.cache.read(symbol, tf)
                frame = cached[0] if cached else None
                gaps = find_gaps(frame, tf) if frame is not None else None
                meta = result.meta
                print(
                    f"{symbol} {tf.value}: rows={meta.rows} (+{result.rows_added}, {'full' if result.full else 'incremental'}, "
                    f"chunks={result.chunks} empty={result.empty_chunks} failed={result.failed_chunks}, "
                    f"forming dropped={result.dropped_forming}) first={meta.first_bar_utc} last={meta.last_bar_utc} "
                    f"first_available={meta.first_available_utc} requested_start={meta.requested_start_utc} "
                    f"history_short={result.history_short} offset={meta.offset_model} "
                    f"gaps={gaps.counts if gaps else {}} missing_bars={gaps.missing_bars_total if gaps else 0}",
                    file=out,
                )
    finally:
        adapter.shutdown()
    print("done" if not failures else f"done with {failures} failure(s)", file=out)
    return 0 if not failures else 1


def main() -> int:  # pragma: no cover - thin wrapper
    return run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
