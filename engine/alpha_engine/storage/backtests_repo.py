"""Backtest runs and results in SQLite (schema v2, ``storage/db.py``).

Tables (children reference ``backtest_runs(id) ON DELETE CASCADE``):

* ``backtest_runs``    -- one row per submitted run: status/progress, the request as sent, the full
  :class:`~alpha_engine.backtest.models.RunConfig` snapshot (params, params_hash, account settings, cost
  model, provisional), then -- once done -- the window plan (incl. the seed actually used, algorithm and
  numpy version), the data fingerprint, labels, result meta, overall metrics, the random-mode distribution,
  elapsed time and step timings;
* ``backtest_windows`` -- per window: bounds, balances, counts, metrics (full-equity ``compute_metrics``)
  and the skipped candidates (JSON);
* ``backtest_trades``  -- one row per trade: key columns (indexed by run/window/trade and entry time) plus
  the complete :class:`~alpha_engine.backtest.models.Trade` as JSON (``payload_json``);
* ``backtest_equity``  -- the DOWNSAMPLED equity per window (``backtest.results.downsample_equity``).

Writes are short ``BEGIN IMMEDIATE`` transactions on the shared connection (``conn.transaction()``);
progress updates touch one row; the whole result is written in ONE transaction with ``executemany`` at the
end of a run, so a run is either fully stored or not at all (cancelled / failed runs store no trades).
Reads take ``conn.lock`` (the connection is shared across threads).

Status machine: ``queued -> running -> done | error | cancelled``; ``queued -> cancelled``;
``queued | running -> interrupted`` (engine stopped: at shutdown, or found stale at the next startup).

Listing (``list_runs_page``): newest first, ``LIMIT ? OFFSET ?`` plus optional exact ``symbol`` / ``mode`` /
``status`` filters; the total matching count is read under the same lock. Filter values are bound
parameters, never interpolated into the SQL text.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, Literal

from ..logging_setup import get_logger, is_dev_mode
from .db import EngineConnection, format_utc, parse_utc, utc_now_text

logger = get_logger(__name__)

RunStatus = Literal["queued", "running", "done", "error", "cancelled", "interrupted"]
ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "running"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "error", "cancelled", "interrupted"})
INTERRUPTED_FA = "اجرا با بسته شدن engine نیمه‌کاره ماند و ادامه داده نمی‌شود؛ دوباره اجرا کنید."

# strategy_sha256 / strategy_source live in the stored RunConfig snapshot (strategy contract S1; no schema
# change): NULL for configs stored before these fields existed or for built-in strategies without a hash.
_SUMMARY_COLUMNS = (
    "id, created_utc, started_utc, finished_utc, status, progress, error_code, error_message_fa, symbol, mode, "
    "period_start_utc, period_end_utc, windows_count, window_months, seed, seed_generated, strategy_name, "
    "strategy_version, params_version, params_hash, provisional, trade_count, net_profit, net_profit_pct, "
    "elapsed_s, json_extract(config_json, '$.strategy_sha256') AS strategy_sha256, "
    "json_extract(config_json, '$.strategy_source') AS strategy_source"
)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _run_filters(symbol: str | None, mode: str | None, status: str | None) -> tuple[str, list[Any]]:
    """``(" WHERE ...", args)`` for the list filters: fixed column names, values as ``?`` parameters."""
    clauses: list[str] = []
    args: list[Any] = []
    for column, value in (("symbol", symbol), ("mode", mode), ("status", status)):
        if value is not None:
            clauses.append(f"{column} = ?")
            args.append(value)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def api_time(text: str | None) -> str | None:
    """Stored ``2025-01-06T00:00:00.000000Z`` -> ``2025-01-06T00:00:00Z`` (microseconds kept if non-zero)."""
    if text is None:
        return None
    return parse_utc(text).isoformat().replace("+00:00", "Z")


class BacktestsRepo:
    def __init__(self, conn: EngineConnection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------------------------------ writes
    def create_run(
        self,
        *,
        request: dict[str, Any],
        config: dict[str, Any],
        symbol: str,
        mode: str,
        period_start: Any,
        period_end: Any,
        windows_count: int | None,
        window_months: int | None,
        seed: int | None,
        strategy_name: str,
        strategy_version: int,
        params_version: int | None,
        params_hash: str,
        provisional: bool,
        labels_fa: Sequence[str],
        seed_generated: bool | None = None,
    ) -> int:
        """Store a ``queued`` run. Random runs get their seed at submit time (``seed_generated`` = the engine
        drew it because the request had none), so the row shows the seed from the start."""
        with self._conn.transaction():
            cur = self._conn.execute(
                "INSERT INTO backtest_runs (created_utc, status, progress, symbol, mode, period_start_utc,"
                " period_end_utc, windows_count, window_months, seed, seed_generated, strategy_name,"
                " strategy_version, params_version, params_hash, provisional, request_json, config_json,"
                " labels_json) VALUES (?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (utc_now_text(), symbol, mode, None if period_start is None else format_utc(period_start),
                 None if period_end is None else format_utc(period_end), windows_count, window_months, seed,
                 None if seed_generated is None else int(seed_generated), strategy_name, strategy_version,
                 params_version, params_hash, int(provisional), _dumps(request), _dumps(config),
                 _dumps(list(labels_fa))),
            )
            run_id = int(cur.lastrowid)
        if is_dev_mode():
            logger.debug("backtest run %d created: %s %s seed=%s (generated=%s) params v%s hash=%s provisional=%s",
                         run_id, symbol, mode, seed, seed_generated, params_version, params_hash[:12], provisional)
        return run_id

    def mark_running(self, run_id: int) -> bool:
        with self._conn.transaction():
            cur = self._conn.execute(
                "UPDATE backtest_runs SET status = 'running', started_utc = ? WHERE id = ? AND status = 'queued'",
                (utc_now_text(), run_id))
        return cur.rowcount == 1

    def update_progress(self, run_id: int, percent: float) -> None:
        pct = min(100.0, max(0.0, float(percent)))
        with self._conn.transaction():
            self._conn.execute("UPDATE backtest_runs SET progress = ? WHERE id = ? AND status = 'running'",
                               (pct, run_id))

    def finish(
        self,
        run_id: int,
        status: RunStatus,
        *,
        error_code: str | None = None,
        error_message_fa: str | None = None,
        elapsed_s: float | None = None,
        timings: dict[str, float] | None = None,
    ) -> bool:
        """End an active run WITHOUT results (error / cancelled / interrupted)."""
        if status not in TERMINAL_STATUSES or status == "done":
            raise ValueError(f"finish() is for error/cancelled/interrupted, not {status!r}")
        with self._conn.transaction():
            cur = self._conn.execute(
                "UPDATE backtest_runs SET status = ?, finished_utc = ?, error_code = ?, error_message_fa = ?,"
                " elapsed_s = ?, timings_json = ? WHERE id = ? AND status IN ('queued', 'running')",
                (status, utc_now_text(), error_code, error_message_fa, elapsed_s,
                 None if timings is None else _dumps(timings), run_id))
        return cur.rowcount == 1

    def save_result(self, run_id: int, output: Any, *, started: float, timings: dict[str, float]) -> dict[str, float]:
        """Store a finished run (``backtest.results.RunOutput``) in ONE transaction and mark it ``done``.

        ``started`` = ``time.perf_counter()`` at the start of the run; the stored timings get ``persist_s``
        (row building + inserts, up to but excluding the COMMIT) and ``total_s`` (= ``elapsed_s``), so a
        ``done`` row always has complete timings. Returns the stored timings."""
        t0 = time.perf_counter()
        result = output.result
        plan = result.plan
        window_rows = []
        trade_rows = []
        equity_rows = []
        for wo in output.windows:
            w = wo.result
            wi = w.window.index
            window_rows.append((
                run_id, wi, format_utc(w.window.start), format_utc(w.window.end), w.initial_balance, w.final_balance,
                len(w.trades), len(w.skipped), w.candidates, w.bars,
                None if w.first_bar_time is None else format_utc(w.first_bar_time),
                None if w.last_bar_time is None else format_utc(w.last_bar_time),
                w.zero_spread_bars_filled, w.zero_spread_bars_unfilled, w.weekend_holds, w.spread_fallback_bars,
                w.stopped_reason,
                len(w.equity), len(wo.equity_stored), _dumps(wo.metrics.to_dict()),
                _dumps([s.model_dump(mode="json") for s in w.skipped]),
            ))
            for t in w.trades:
                trade_rows.append((
                    run_id, wi, t.trade_index, t.direction, t.setup_type, format_utc(t.entry_time),
                    format_utc(t.exit_time), t.entry, t.stop_loss, t.take_profit, t.volume, t.exit_price,
                    t.exit_reason.value, t.net_pnl, t.r_multiple, _dumps(t.model_dump(mode="json")),
                ))
            for seq, p in enumerate(wo.equity_stored):
                equity_rows.append((run_id, wi, seq, format_utc(p.time), p.balance, p.equity))
        meta = {
            "cost_model": result.cost_model.model_dump(mode="json"),
            "entry_rule": result.entry_rule,
            "price_basis": result.price_basis,
            "spread_source": result.spread_source,
            "total_trades": result.total_trades,
            "total_skipped": result.total_skipped,
            "metrics_kind": output.metrics_kind,
            "spread_fallback": result.spread_fallback.model_dump(mode="json"),
        }
        with self._conn.transaction():
            self._conn.executemany(
                "INSERT INTO backtest_windows (run_id, window_index, start_utc, end_utc, initial_balance,"
                " final_balance, trade_count, skipped_count, candidates, bars, first_bar_utc, last_bar_utc,"
                " zero_spread_bars_filled, zero_spread_bars_unfilled, weekend_holds, spread_fallback_bars,"
                " stopped_reason,"
                " equity_points_full, equity_points_stored, metrics_json, skipped_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", window_rows)
            self._conn.executemany(
                "INSERT INTO backtest_trades (run_id, window_index, trade_index, direction, setup_type, entry_time,"
                " exit_time, entry, stop_loss, take_profit, volume, exit_price, exit_reason, net_pnl, r_multiple,"
                " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", trade_rows)
            self._conn.executemany(
                "INSERT INTO backtest_equity (run_id, window_index, seq, time_utc, balance, equity)"
                " VALUES (?, ?, ?, ?, ?, ?)", equity_rows)
            now = time.perf_counter()
            timings = {**timings, "persist_s": now - t0, "total_s": now - started}
            cur = self._conn.execute(
                "UPDATE backtest_runs SET status = 'done', progress = 100, finished_utc = ?, seed = ?,"
                " seed_generated = ?, plan_json = ?, fingerprint_json = ?, labels_json = ?, result_json = ?,"
                " metrics_json = ?, distribution_json = ?, trade_count = ?, net_profit = ?, net_profit_pct = ?,"
                " elapsed_s = ?, timings_json = ? WHERE id = ? AND status = 'running'",
                (utc_now_text(), plan.seed, int(plan.seed_generated), _dumps(plan.model_dump(mode="json")),
                 _dumps(result.fingerprint.model_dump(mode="json")), _dumps(list(result.labels_fa)), _dumps(meta),
                 _dumps(output.metrics), None if output.distribution is None else _dumps(output.distribution),
                 output.trade_count, output.net_profit, output.net_profit_pct, timings["total_s"], _dumps(timings),
                 run_id))
            if cur.rowcount != 1:
                raise RuntimeError(f"backtest run {run_id} is not running any more; result not stored")
        if is_dev_mode():
            logger.debug("backtest run %d stored in %.3f s: windows=%d trades=%d equity points=%d", run_id,
                         time.perf_counter() - t0, len(window_rows), len(trade_rows), len(equity_rows))
        return timings

    def mark_stale_interrupted(self) -> int:
        """Runs left ``queued``/``running`` by a previous engine process -> ``interrupted`` (startup)."""
        with self._conn.transaction():
            cur = self._conn.execute(
                "UPDATE backtest_runs SET status = 'interrupted', finished_utc = ?, error_code = 'interrupted',"
                " error_message_fa = ? WHERE status IN ('queued', 'running')", (utc_now_text(), INTERRUPTED_FA))
        count = cur.rowcount
        if count:
            logger.warning("%d unfinished backtest run(s) from a previous engine process marked interrupted", count)
        return count

    def delete_run(self, run_id: int) -> Literal["deleted", "not_found", "active"]:
        with self._conn.transaction():
            row = self._conn.execute("SELECT status FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return "not_found"
            if row["status"] in ACTIVE_STATUSES:
                return "active"
            self._conn.execute("DELETE FROM backtest_runs WHERE id = ?", (run_id,))  # children cascade
        if is_dev_mode():
            logger.debug("backtest run %d deleted", run_id)
        return "deleted"

    # ------------------------------------------------------------------------------------------ reads
    def status(self, run_id: int) -> dict[str, Any] | None:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT id, status, progress, error_code, error_message_fa, trade_count, net_profit, net_profit_pct,"
                " mode, windows_count FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else dict(row)

    def list_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        symbol: str | None = None,
        mode: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run summaries, newest first (``id DESC``), optionally filtered (exact matches)."""
        return self.list_runs_page(limit, offset, symbol=symbol, mode=mode, status=status)[0]

    def count_runs(self, *, symbol: str | None = None, mode: str | None = None, status: str | None = None) -> int:
        where, args = _run_filters(symbol, mode, status)
        with self._conn.lock:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM backtest_runs{where}", args).fetchone()[0])

    def list_runs_page(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        symbol: str | None = None,
        mode: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """``(page, total)``: one page of summaries (newest first) and the number of runs matching the filters,
        read under ONE lock so both agree. Filter VALUES are always bound parameters; only the fixed column
        names of ``_run_filters`` are part of the SQL text."""
        where, args = _run_filters(symbol, mode, status)
        with self._conn.lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) FROM backtest_runs{where}", args).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT {_SUMMARY_COLUMNS} FROM backtest_runs{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, int(limit), int(offset)]).fetchall()
        if is_dev_mode():
            logger.debug("backtest runs listed: symbol=%s mode=%s status=%s offset=%d limit=%d -> %d of %d", symbol,
                         mode, status, offset, limit, len(rows), total)
        return [dict(r) for r in rows], total

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._conn.lock:
            row = self._conn.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        for key in ("request_json", "config_json", "labels_json", "plan_json", "fingerprint_json", "result_json",
                    "metrics_json", "distribution_json", "timings_json"):
            out[key[:-5]] = _loads(out.pop(key))
        return out

    def get_windows(self, run_id: int) -> list[dict[str, Any]]:
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT * FROM backtest_windows WHERE run_id = ? ORDER BY window_index", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["metrics"] = _loads(d.pop("metrics_json"))
            d.pop("skipped_json")
            out.append(d)
        return out

    def window_count(self, run_id: int) -> int:
        with self._conn.lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM backtest_windows WHERE run_id = ?",
                                          (run_id,)).fetchone()[0])

    def count_trades(self, run_id: int, window: int | None = None) -> int:
        sql, args = "SELECT COUNT(*) FROM backtest_trades WHERE run_id = ?", [run_id]
        if window is not None:
            sql += " AND window_index = ?"
            args.append(window)
        with self._conn.lock:
            return int(self._conn.execute(sql, args).fetchone()[0])

    def get_trades(self, run_id: int, window: int | None = None, *, offset: int = 0,
                   limit: int | None = None) -> list[dict[str, Any]]:
        """Full trade payloads (``Trade.model_dump(mode="json")``), ordered by window then trade index."""
        sql, args = "SELECT payload_json FROM backtest_trades WHERE run_id = ?", [run_id]
        if window is not None:
            sql += " AND window_index = ?"
            args.append(window)
        sql += " ORDER BY window_index, trade_index LIMIT ? OFFSET ?"
        args += [-1 if limit is None else int(limit), int(offset)]
        with self._conn.lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_equity(self, run_id: int, window: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT window_index, time_utc, balance, equity FROM backtest_equity WHERE run_id = ?", [run_id]
        if window is not None:
            sql += " AND window_index = ?"
            args.append(window)
        sql += " ORDER BY window_index, seq"
        with self._conn.lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [{"window_index": r[0], "time": api_time(r[1]), "balance": r[2], "equity": r[3]} for r in rows]

    def get_skipped(self, run_id: int, window: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT skipped_json FROM backtest_windows WHERE run_id = ?", [run_id]
        if window is not None:
            sql += " AND window_index = ?"
            args.append(window)
        sql += " ORDER BY window_index"
        with self._conn.lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [item for r in rows for item in json.loads(r[0])]


__all__ = [
    "ACTIVE_STATUSES",
    "INTERRUPTED_FA",
    "TERMINAL_STATUSES",
    "BacktestsRepo",
    "RunStatus",
    "api_time",
]
