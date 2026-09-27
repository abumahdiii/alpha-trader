"""SQLite storage for the engine (``data/alpha.db``) -- stdlib ``sqlite3`` only.

* **Single writer:** only the engine process opens this file (the Flutter UI never touches it; see
  ``.claude/spec.md`` section 2.3). Inside the engine, one shared connection is used from FastAPI's
  worker threads, so every statement batch is serialised by ``conn.lock`` (a re-entrant
  :class:`threading.RLock`). Use :meth:`EngineConnection.transaction` for writes: it takes the lock and
  runs ``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on error). Nested calls join the outer transaction.
* **Pragmas:** ``journal_mode=WAL``, ``foreign_keys=ON``, ``busy_timeout=5000``, ``synchronous=NORMAL``.
* **Migrations:** ``schema_version`` records each applied version; :func:`migrate` applies missing
  ones in order, each in its own transaction, and is idempotent. A database written by a *newer*
  engine (version above :data:`SCHEMA_VERSION`) is refused rather than silently used.
* All timestamps are stored as ISO-8601 UTC text (``2026-09-26T18:00:00.000000Z``).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..logging_setup import get_logger, is_dev_mode

if TYPE_CHECKING:
    from ..config import Settings

logger = get_logger(__name__)

DB_FILENAME = "alpha.db"
BUSY_TIMEOUT_MS = 5000

# version -> DDL statements. Append new versions; never edit an applied one.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE strategies (
            name        TEXT PRIMARY KEY,
            created_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE strategy_versions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT    NOT NULL REFERENCES strategies(name) ON DELETE RESTRICT,
            version       INTEGER NOT NULL CHECK (version >= 1),
            code_version  INTEGER NOT NULL CHECK (code_version >= 1),
            params_json   TEXT    NOT NULL,
            params_hash   TEXT    NOT NULL CHECK (length(params_hash) = 64),
            created_utc   TEXT    NOT NULL,
            is_active     INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
            UNIQUE (strategy_name, version)
        )
        """,
        # At most one active params version per strategy, enforced by the database itself.
        """
        CREATE UNIQUE INDEX ux_strategy_versions_one_active
            ON strategy_versions (strategy_name) WHERE is_active = 1
        """,
        # Risk bounds duplicated as CHECKs: defense in depth behind the pydantic model.
        """
        CREATE TABLE account_settings (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            balance     REAL    NOT NULL CHECK (balance > 0 AND balance <= 1000000000),
            risk_pct    REAL    NOT NULL CHECK (risk_pct > 0 AND risk_pct <= 10),
            leverage    INTEGER NOT NULL CHECK (leverage >= 1 AND leverage <= 1000),
            rr          REAL    NOT NULL CHECK (rr >= 0.1 AND rr <= 20),
            updated_utc TEXT    NOT NULL
        )
        """,
    ),
    # v2 (phase 4): backtest runs and their results (storage/backtests_repo.py). Deliberately NO foreign
    # key from the runs to the strategy tables: the params are snapshotted into config_json, and the
    # tgc_startup reset can clear these four tables (children first) independently of the strategies.
    2: (
        """
        CREATE TABLE backtest_runs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            created_utc       TEXT    NOT NULL,
            started_utc       TEXT,
            finished_utc      TEXT,
            status            TEXT    NOT NULL CHECK (status IN
                                  ('queued', 'running', 'done', 'error', 'cancelled', 'interrupted')),
            progress          REAL    NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
            error_code        TEXT,
            error_message_fa  TEXT,
            symbol            TEXT    NOT NULL,
            mode              TEXT    NOT NULL CHECK (mode IN ('manual', 'random')),
            period_start_utc  TEXT,
            period_end_utc    TEXT,
            windows_count     INTEGER,
            window_months     INTEGER,
            seed              INTEGER,
            seed_generated    INTEGER CHECK (seed_generated IN (0, 1)),
            strategy_name     TEXT    NOT NULL,
            strategy_version  INTEGER NOT NULL,
            params_version    INTEGER,
            params_hash       TEXT    NOT NULL CHECK (length(params_hash) = 64),
            provisional       INTEGER NOT NULL CHECK (provisional IN (0, 1)),
            request_json      TEXT    NOT NULL,
            config_json       TEXT    NOT NULL,
            labels_json       TEXT    NOT NULL,
            plan_json         TEXT,
            fingerprint_json  TEXT,
            result_json       TEXT,
            metrics_json      TEXT,
            distribution_json TEXT,
            trade_count       INTEGER,
            net_profit        REAL,
            net_profit_pct    REAL,
            elapsed_s         REAL,
            timings_json      TEXT
        )
        """,
        "CREATE INDEX ix_backtest_runs_status ON backtest_runs (status)",
        """
        CREATE TABLE backtest_windows (
            run_id                    INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
            window_index              INTEGER NOT NULL CHECK (window_index >= 0),
            start_utc                 TEXT    NOT NULL,
            end_utc                   TEXT    NOT NULL,
            initial_balance           REAL    NOT NULL,
            final_balance             REAL    NOT NULL,
            trade_count               INTEGER NOT NULL,
            skipped_count             INTEGER NOT NULL,
            candidates                INTEGER NOT NULL,
            bars                      INTEGER NOT NULL,
            first_bar_utc             TEXT,
            last_bar_utc              TEXT,
            zero_spread_bars_filled   INTEGER NOT NULL,
            zero_spread_bars_unfilled INTEGER NOT NULL,
            weekend_holds             INTEGER NOT NULL,
            spread_fallback_bars      INTEGER NOT NULL,
            stopped_reason            TEXT,
            equity_points_full        INTEGER NOT NULL,
            equity_points_stored      INTEGER NOT NULL,
            metrics_json              TEXT    NOT NULL,
            skipped_json              TEXT    NOT NULL,
            PRIMARY KEY (run_id, window_index)
        )
        """,
        """
        CREATE TABLE backtest_trades (
            run_id        INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
            window_index  INTEGER NOT NULL,
            trade_index   INTEGER NOT NULL,
            direction     TEXT    NOT NULL CHECK (direction IN ('buy', 'sell')),
            setup_type    TEXT    NOT NULL,
            entry_time    TEXT    NOT NULL,
            exit_time     TEXT    NOT NULL,
            entry         REAL    NOT NULL,
            stop_loss     REAL    NOT NULL,
            take_profit   REAL    NOT NULL,
            volume        REAL    NOT NULL,
            exit_price    REAL    NOT NULL,
            exit_reason   TEXT    NOT NULL,
            net_pnl       REAL    NOT NULL,
            r_multiple    REAL,
            payload_json  TEXT    NOT NULL,
            PRIMARY KEY (run_id, window_index, trade_index)
        )
        """,
        "CREATE INDEX ix_backtest_trades_entry ON backtest_trades (run_id, entry_time)",
        """
        CREATE TABLE backtest_equity (
            run_id       INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
            window_index INTEGER NOT NULL,
            seq          INTEGER NOT NULL,
            time_utc     TEXT    NOT NULL,
            balance      REAL    NOT NULL,
            equity       REAL    NOT NULL,
            PRIMARY KEY (run_id, window_index, seq)
        )
        """,
    ),
}
SCHEMA_VERSION: int = max(MIGRATIONS)


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer engine version."""


class EngineConnection(sqlite3.Connection):
    """``sqlite3.Connection`` with the engine's lock and transaction helper."""

    lock: threading.RLock

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[EngineConnection]:
        """Serialised write transaction (``BEGIN IMMEDIATE``); joins an already-open one."""
        with self.lock:
            if self.in_transaction:
                yield self
                return
            self.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                if self.in_transaction:  # SQLite may already have rolled back on some errors
                    self.execute("ROLLBACK")
                raise
            self.execute("COMMIT")


def utc_now_text() -> str:
    return format_utc(datetime.now(timezone.utc))


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime; UTC required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def default_db_path(settings: Settings) -> Path:
    """``<data_dir>/alpha.db``; creates ``data_dir`` if needed."""
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / DB_FILENAME


def open_db(path: str | Path) -> EngineConnection:
    """Open (creating if needed) and migrate the engine database. ``":memory:"`` is allowed for tests."""
    in_memory = str(path) == ":memory:"
    if not in_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        factory=EngineConnection,
        isolation_level=None,  # we issue BEGIN/COMMIT ourselves
        check_same_thread=False,  # shared across FastAPI worker threads; serialised by conn.lock
        timeout=BUSY_TIMEOUT_MS / 1000,
    )
    assert isinstance(conn, EngineConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if not in_memory and str(mode).lower() != "wal":
            raise RuntimeError(f"could not enable WAL journal mode (got {mode!r})")
        conn.execute("PRAGMA synchronous = NORMAL")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("could not enable foreign keys")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    if is_dev_mode():
        logger.debug("db opened: %s (journal_mode=%s, schema_version=%d)", path, mode, current_schema_version(conn))
    return conn


def current_schema_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def migrate(conn: EngineConnection) -> list[int]:
    """Apply pending migrations in order; return the versions applied (``[]`` when up to date)."""
    applied: list[int] = []
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, applied_utc TEXT NOT NULL)"
        )
    current = current_schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"database schema version {current} is newer than this engine supports ({SCHEMA_VERSION})"
        )
    for version in sorted(v for v in MIGRATIONS if v > current):
        with conn.transaction():
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version (version, applied_utc) VALUES (?, ?)", (version, utc_now_text())
            )
        applied.append(version)
        if is_dev_mode():
            logger.debug("db migration applied: v%d", version)
    return applied
