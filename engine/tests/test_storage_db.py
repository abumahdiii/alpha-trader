"""SQLite storage: pragmas, migrations, write lock, strategy params versioning, account settings."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from alpha_engine.storage import db as db_module
from alpha_engine.storage.account_settings import AccountSettings, AccountSettingsRepo, AccountSettingsValidationError
from alpha_engine.storage.db import (
    SCHEMA_VERSION,
    EngineConnection,
    SchemaTooNewError,
    current_schema_version,
    default_db_path,
    migrate,
    open_db,
)
from alpha_engine.storage.strategies_repo import ParamsValidationError, StoredParamsInvalidError, StrategiesRepo
from alpha_engine.strategy.base import Strategy, StrategyContext
from alpha_engine.strategy.params import ParamSchema, ParamSpec, params_hash
from alpha_engine.strategy.registry import StrategyRegistry, UnknownStrategyError
from alpha_engine.strategy.signal import SignalCandidate


class Dummy(Strategy):
    name: ClassVar[str] = "dummy"
    version: ClassVar[int] = 1
    title_fa: ClassVar[str] = "آزمایشی"
    param_schema: ClassVar[ParamSchema] = ParamSchema([
        ParamSpec(name="period", type="int", default=100, min=10, max=500, label_fa="دوره"),
        ParamSpec(name="k", type="float", default=2.0, min=0.5, max=4.0, label_fa="ضریب"),
    ])

    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        return None


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[EngineConnection]:
    connection = open_db(tmp_path / "data" / "alpha.db")
    yield connection
    connection.close()


@pytest.fixture
def registry() -> StrategyRegistry:
    reg = StrategyRegistry()
    reg.register(Dummy)
    return reg


# --- db basics ------------------------------------------------------------------------------------

def test_pragmas_wal_and_foreign_keys(conn: EngineConnection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert isinstance(conn, sqlite3.Connection)


def test_tables_and_schema_version(conn: EngineConnection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_version", "strategies", "strategy_versions", "account_settings"} <= tables
    assert {"backtest_runs", "backtest_windows", "backtest_trades", "backtest_equity"} <= tables
    assert "strategy_plugins" in tables  # v3
    assert current_schema_version(conn) == SCHEMA_VERSION == 3


def test_migrations_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "alpha.db"
    first = open_db(path)
    assert migrate(first) == []
    first.execute("INSERT INTO strategies (name, created_utc) VALUES ('x', '2026-09-26T00:00:00.000000Z')")
    first.close()
    second = open_db(path)  # reopen: no re-migration, data kept
    assert second.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert second.execute("SELECT name FROM strategies").fetchall()[0][0] == "x"
    second.close()


def test_newer_schema_refused(tmp_path: Path) -> None:
    path = tmp_path / "alpha.db"
    conn = open_db(path)
    conn.execute("INSERT INTO schema_version (version, applied_utc) VALUES (99, '2030-01-01T00:00:00.000000Z')")
    conn.close()
    with pytest.raises(SchemaTooNewError):
        open_db(path)


def test_foreign_key_enforced(conn: EngineConnection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO strategy_versions (strategy_name, version, code_version, params_json, params_hash,"
            " created_utc, is_active) VALUES ('ghost', 1, 1, '{}', ?, 'x', 1)", ("a" * 64,)
        )


def test_default_db_path_under_data_dir(tmp_path: Path, make_settings) -> None:
    data_dir = tmp_path / "nested" / "data"
    settings = make_settings(environ={"ALPHA_TRADER_DATA_DIR": str(data_dir)})
    path = default_db_path(settings)
    assert path == data_dir / "alpha.db"
    assert data_dir.is_dir()


def test_transaction_rolls_back_on_error(conn: EngineConnection) -> None:
    with pytest.raises(RuntimeError):
        with conn.transaction():
            conn.execute("INSERT INTO strategies (name, created_utc) VALUES ('a', 'x')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
    assert not conn.in_transaction


def test_nested_transaction_joins_outer(conn: EngineConnection) -> None:
    with conn.transaction():
        conn.execute("INSERT INTO strategies (name, created_utc) VALUES ('a', 'x')")
        with conn.transaction():
            conn.execute("INSERT INTO strategies (name, created_utc) VALUES ('b', 'x')")
    assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 2


def test_write_lock_serialises_threads(conn: EngineConnection) -> None:
    """Many threads doing read-modify-write on one row: no lost updates."""
    conn.execute("INSERT INTO strategies (name, created_utc) VALUES ('counter', '0')")

    def bump() -> None:
        for _ in range(25):
            with conn.transaction():
                value = int(conn.execute("SELECT created_utc FROM strategies WHERE name='counter'").fetchone()[0])
                conn.execute("UPDATE strategies SET created_utc = ? WHERE name='counter'", (str(value + 1),))

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert conn.execute("SELECT created_utc FROM strategies WHERE name='counter'").fetchone()[0] == "200"


def test_lock_is_reentrant(conn: EngineConnection) -> None:
    assert isinstance(conn.lock, type(threading.RLock()))


# --- strategy params versioning -------------------------------------------------------------------

def test_first_access_creates_version_1_from_defaults(conn: EngineConnection, registry: StrategyRegistry) -> None:
    repo = StrategiesRepo(conn, registry)
    assert repo.list_versions("dummy") == []
    active = repo.get_active("dummy")
    assert (active.version, active.code_version, active.is_active) == (1, 1, True)
    assert active.params == {"period": 100, "k": 2.0}
    assert active.params_hash == params_hash({"period": 100, "k": 2.0})
    assert repo.get_active("dummy") == active  # no new version on repeated access
    assert len(repo.list_versions("dummy")) == 1


def test_same_params_same_version_changed_params_new_version(conn: EngineConnection, registry: StrategyRegistry) -> None:
    repo = StrategiesRepo(conn, registry)
    record, created = repo.save_params("dummy", {"period": 100, "k": 2})  # == defaults after coercion
    assert (record.version, created) == (1, True)  # nothing stored yet -> v1
    record, created = repo.save_params("dummy", {"period": 100, "k": 2.0})
    assert (record.version, created) == (1, False)

    record, created = repo.save_params("dummy", {"period": 150})
    assert (record.version, created) == (2, True)
    assert record.params == {"period": 150, "k": 2.0}
    record, created = repo.save_params("dummy", {"k": 2.0, "period": 150})  # same set, other key order
    assert (record.version, created) == (2, False)

    record, created = repo.save_params("dummy", {})  # back to defaults: new version 3, history linear
    assert (record.version, created) == (3, True)

    versions = repo.list_versions("dummy")
    assert [v.version for v in versions] == [1, 2, 3]
    assert [v.is_active for v in versions] == [False, False, True]
    assert versions[0].params_hash == versions[2].params_hash
    assert repo.get_active("dummy").version == 3


def test_invalid_params_rejected_without_new_version(conn: EngineConnection, registry: StrategyRegistry) -> None:
    repo = StrategiesRepo(conn, registry)
    repo.get_active("dummy")
    with pytest.raises(ParamsValidationError) as info:
        repo.save_params("dummy", {"period": 5, "bogus": 1})
    assert len(info.value.errors_fa) == 2
    assert [v.version for v in repo.list_versions("dummy")] == [1]


def test_unknown_strategy(conn: EngineConnection, registry: StrategyRegistry) -> None:
    repo = StrategiesRepo(conn, registry)
    for call in (lambda: repo.get_active("nope"), lambda: repo.save_params("nope", {}),
                 lambda: repo.list_versions("nope")):
        with pytest.raises(UnknownStrategyError):
            call()


def test_one_active_version_enforced_by_db(conn: EngineConnection, registry: StrategyRegistry) -> None:
    repo = StrategiesRepo(conn, registry)
    repo.get_active("dummy")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO strategy_versions (strategy_name, version, code_version, params_json, params_hash,"
            " created_utc, is_active) VALUES ('dummy', 2, 1, '{}', ?, 'x', 1)", ("b" * 64,)
        )


def test_schema_gains_defaulted_param_normalises_to_new_version(conn: EngineConnection) -> None:
    old = StrategyRegistry()
    old.register(Dummy)
    StrategiesRepo(conn, old).save_params("dummy", {"period": 200})  # v1

    class DummyV2(Dummy):
        version = 2
        param_schema = ParamSchema([*Dummy.param_schema.specs,
                                    ParamSpec(name="flat", type="float", default=1.0, label_fa="افقی")])

    new = StrategyRegistry()
    new.register(DummyV2)
    active = StrategiesRepo(conn, new).get_active("dummy")
    assert (active.version, active.code_version) == (2, 2)
    assert active.params == {"period": 200, "k": 2.0, "flat": 1.0}


def test_stored_params_invalid_under_new_schema_raises(conn: EngineConnection) -> None:
    old = StrategyRegistry()
    old.register(Dummy)
    StrategiesRepo(conn, old).save_params("dummy", {"period": 400})

    class Tightened(Dummy):
        version = 2
        param_schema = ParamSchema([ParamSpec(name="period", type="int", default=100, min=10, max=300,
                                              label_fa="دوره")])

    new = StrategyRegistry()
    new.register(Tightened)
    repo = StrategiesRepo(conn, new)
    with pytest.raises(StoredParamsInvalidError) as info:
        repo.get_active("dummy")
    assert info.value.record.version == 1 and len(info.value.errors_fa) == 2  # bound + unknown 'k'
    # The user fixes it by saving valid params:
    record, created = repo.save_params("dummy", {"period": 250})
    assert (record.version, created, record.code_version) == (2, True, 2)
    assert repo.get_active("dummy").version == 2


# --- account settings -----------------------------------------------------------------------------

def test_account_settings_defaults_not_persisted_until_update(conn: EngineConnection) -> None:
    repo = AccountSettingsRepo(conn)
    # Defaults (balance 2500 since user decision 2026-09-27; 1 % risk of 2500 = 25.00 USD).
    assert repo.get() == AccountSettings(balance=2500.0, risk_pct=1.0, leverage=100, rr=2.0)
    assert conn.execute("SELECT COUNT(*) FROM account_settings").fetchone()[0] == 0


def test_account_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "alpha.db"
    conn = open_db(path)
    new = AccountSettingsRepo(conn).update({"risk_pct": 0.5, "leverage": 200})
    assert new == AccountSettings(balance=2500.0, risk_pct=0.5, leverage=200, rr=2.0)  # balance not given -> default
    new = AccountSettingsRepo(conn).update({"balance": 2500.25})
    assert new.risk_pct == 0.5 and new.balance == 2500.25
    conn.close()
    reopened = open_db(path)
    assert AccountSettingsRepo(reopened).get() == AccountSettings(balance=2500.25, risk_pct=0.5, leverage=200, rr=2.0)
    assert reopened.execute("SELECT COUNT(*) FROM account_settings").fetchone()[0] == 1
    reopened.close()


def test_account_settings_rejected_update_changes_nothing(conn: EngineConnection) -> None:
    repo = AccountSettingsRepo(conn)
    repo.update({"rr": 3.0})
    with pytest.raises(AccountSettingsValidationError) as info:
        repo.update({"rr": 1.5, "risk_pct": 11})  # one invalid field rejects the whole update
    assert any("درصد ریسک" in e for e in info.value.errors_fa)
    assert repo.get().rr == 3.0


def test_account_settings_db_checks_are_defense_in_depth(conn: EngineConnection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO account_settings (id, balance, risk_pct, leverage, rr, updated_utc)"
            " VALUES (1, 1000, 50, 100, 2, 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO account_settings (id, balance, risk_pct, leverage, rr, updated_utc)"
            " VALUES (2, 1000, 1, 100, 2, 'x')"
        )


def test_migrations_table_is_append_only_registry() -> None:
    assert sorted(db_module.MIGRATIONS) == list(range(1, SCHEMA_VERSION + 1))


# --- v2: backtest tables ------------------------------------------------------------------------

BACKTEST_TABLES = ("backtest_runs", "backtest_windows", "backtest_trades", "backtest_equity")


def _open_v1(path: Path) -> None:
    """A database exactly as a v1 engine left it (v1 DDL only), with user data in it."""
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_utc TEXT NOT NULL)")
    for statement in db_module.MIGRATIONS[1]:
        raw.execute(statement)
    raw.execute("INSERT INTO schema_version VALUES (1, '2026-09-26T00:00:00.000000Z')")
    raw.execute("INSERT INTO strategies VALUES ('stddev_channel', '2026-09-26T00:00:00.000000Z')")
    raw.execute("INSERT INTO strategy_versions (strategy_name, version, code_version, params_json, params_hash,"
                " created_utc, is_active) VALUES ('stddev_channel', 1, 1, '{\"n\": 120}', ?, "
                "'2026-09-26T00:00:00.000000Z', 1)", ("b" * 64,))
    raw.execute("INSERT INTO account_settings VALUES (1, 5000.0, 0.5, 200, 3.0, '2026-09-26T00:00:00.000000Z')")
    raw.close()


def test_v1_database_upgrades_to_v2_keeping_user_data(tmp_path: Path) -> None:
    path = tmp_path / "alpha.db"
    _open_v1(path)
    conn = open_db(path)
    try:
        assert current_schema_version(conn) == 3  # v2 (backtests) and v3 (strategy plugins) applied in order
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")] == [1, 2, 3]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(BACKTEST_TABLES) <= tables
        assert AccountSettingsRepo(conn).get() == AccountSettings(balance=5000.0, risk_pct=0.5, leverage=200, rr=3.0)
        row = conn.execute("SELECT version, params_json, params_hash, is_active FROM strategy_versions").fetchone()
        assert tuple(row) == (1, '{"n": 120}', "b" * 64, 1)
        assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0
    finally:
        conn.close()
    again = open_db(path)  # idempotent
    assert current_schema_version(again) == 3 and migrate(again) == []
    again.close()


def test_backtest_children_cascade_and_no_fk_to_strategy_tables(conn: EngineConnection) -> None:
    # no foreign key from the backtest tables to the strategy/account tables (tgc_startup clears them alone)
    for table in BACKTEST_TABLES:
        targets = {r["table"] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
        assert targets <= {"backtest_runs"}, (table, targets)
    for table in ("strategies", "strategy_versions", "account_settings"):
        targets = {r["table"] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
        assert not targets & set(BACKTEST_TABLES)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0] == 0
    now = "2026-09-27T00:00:00.000000Z"
    conn.execute("INSERT INTO backtest_runs (id, created_utc, status, symbol, mode, strategy_name, strategy_version,"
                 " params_hash, provisional, request_json, config_json, labels_json) VALUES"
                 " (7, ?, 'done', 'XAUUSD.x', 'manual', 'stddev_channel', 1, ?, 1, '{}', '{}', '[]')", (now, "c" * 64))
    conn.execute("INSERT INTO backtest_windows VALUES (7, 0, ?, ?, 1000, 1010, 1, 0, 1, 10, NULL, NULL, 0, 0, 0, 0,"
                 " NULL, 10, 3, '{}', '[]')", (now, now))
    conn.execute("INSERT INTO backtest_trades VALUES (7, 0, 0, 'buy', 'bounce_lower', ?, ?, 1, 0.5, 2, 0.1, 2, 'tp',"
                 " 10, 1.0, '{}')", (now, now))
    conn.execute("INSERT INTO backtest_equity VALUES (7, 0, 0, ?, 1000, 1000)", (now,))
    with pytest.raises(sqlite3.IntegrityError):  # child without a parent
        conn.execute("INSERT INTO backtest_equity VALUES (8, 0, 0, ?, 1000, 1000)", (now,))
    with pytest.raises(sqlite3.IntegrityError):  # status CHECK
        conn.execute("UPDATE backtest_runs SET status = 'weird' WHERE id = 7")
    conn.execute("DELETE FROM backtest_runs WHERE id = 7")
    for table in BACKTEST_TABLES[1:]:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
