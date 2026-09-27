"""Versioned strategy parameters.

Two different "versions" exist and both go into provenance:

* **code version** -- ``Strategy.version``, bumped by a developer when the decision logic changes;
* **params version** -- a row in ``strategy_versions``; a new one is created whenever the user saves a
  parameter set that differs from the active one. Exactly one params version per strategy is active
  (also enforced by a partial unique index). ``code_version`` on the row records which code version
  the params were saved/validated under.

Rules:

* ``get_active(name)``: first access creates params version 1 from the schema defaults. The stored
  params are re-validated against the *current* schema; if the schema gained a defaulted param, the
  normalised set is saved as version n+1 (so ``params_hash`` always matches what the strategy
  receives); if they are no longer valid (removed param / tightened bound) it raises
  :class:`StoredParamsInvalidError` -- never a silent change of the user's parameters.
* ``save_params(name, values)``: full replacement (missing keys take their defaults), validated by the
  strategy's schema and cross-field rules (``Strategy.validate_params``). Same canonical params as the active version -> that version is returned, no new
  row. Different -> version n+1 becomes active. Re-saving an older parameter set also creates n+1:
  history stays linear.
* ``list_versions(name)``: all params versions, oldest first (empty until first access).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..logging_setup import get_logger, is_dev_mode
from ..strategy.params import ParamValue, canonical_params_json, params_hash
from ..strategy.registry import StrategyRegistry, default_registry
from .db import EngineConnection, parse_utc, utc_now_text

logger = get_logger(__name__)


@dataclass(frozen=True)
class StrategyVersionRecord:
    strategy_name: str
    version: int
    code_version: int
    params: dict[str, ParamValue]
    params_hash: str
    created_utc: datetime
    is_active: bool


class ParamsValidationError(ValueError):
    def __init__(self, strategy_name: str, errors_fa: list[str]) -> None:
        super().__init__(f"{strategy_name}: " + "; ".join(errors_fa))
        self.strategy_name = strategy_name
        self.errors_fa = errors_fa


class StoredParamsInvalidError(RuntimeError):
    """The active stored params no longer validate against the strategy's current schema."""

    def __init__(self, record: StrategyVersionRecord, errors_fa: list[str]) -> None:
        super().__init__(f"{record.strategy_name} params v{record.version}: " + "; ".join(errors_fa))
        self.record = record
        self.errors_fa = errors_fa


def _record(row: sqlite3.Row) -> StrategyVersionRecord:
    return StrategyVersionRecord(
        strategy_name=row["strategy_name"],
        version=int(row["version"]),
        code_version=int(row["code_version"]),
        params=json.loads(row["params_json"]),
        params_hash=row["params_hash"],
        created_utc=parse_utc(row["created_utc"]),
        is_active=bool(row["is_active"]),
    )


_SELECT = "SELECT strategy_name, version, code_version, params_json, params_hash, created_utc, is_active FROM strategy_versions"


class StrategiesRepo:
    def __init__(self, conn: EngineConnection, registry: StrategyRegistry | None = None) -> None:
        self._conn = conn
        self._registry = registry if registry is not None else default_registry

    # -- helpers ---------------------------------------------------------------------------------

    def ensure_strategy(self, name: str) -> None:
        """Create the ``strategies`` row for a registered strategy (raises ``UnknownStrategyError``)."""
        self._registry.get(name)
        with self._conn.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO strategies (name, created_utc) VALUES (?, ?)", (name, utc_now_text())
            )

    def _active_row(self, name: str) -> StrategyVersionRecord | None:
        row = self._conn.execute(_SELECT + " WHERE strategy_name = ? AND is_active = 1", (name,)).fetchone()
        return _record(row) if row is not None else None

    def _insert_active(self, name: str, code_version: int, clean: Mapping[str, ParamValue]) -> StrategyVersionRecord:
        """Insert params version n+1 and make it the only active one. Caller holds a transaction."""
        (next_version,) = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM strategy_versions WHERE strategy_name = ?", (name,)
        ).fetchone()
        self._conn.execute(
            "UPDATE strategy_versions SET is_active = 0 WHERE strategy_name = ? AND is_active = 1", (name,)
        )
        self._conn.execute(
            "INSERT INTO strategy_versions"
            " (strategy_name, version, code_version, params_json, params_hash, created_utc, is_active)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (name, next_version, code_version, canonical_params_json(clean), params_hash(clean), utc_now_text()),
        )
        record = self._active_row(name)
        assert record is not None and record.version == next_version
        if is_dev_mode():
            logger.debug(
                "strategy params version saved: %s v%d (code v%d) hash=%s params=%s",
                name, record.version, code_version, record.params_hash[:12], record.params,
            )
        return record

    # -- public API ------------------------------------------------------------------------------

    def get_active(self, name: str) -> StrategyVersionRecord:
        cls = self._registry.get(name)
        schema = cls.param_schema
        with self._conn.transaction():
            self.ensure_strategy(name)
            active = self._active_row(name)
            if active is None:
                return self._insert_active(name, cls.version, schema.defaults())
            clean, errors = cls.validate_params(active.params)
            if errors:
                if is_dev_mode():
                    logger.debug("stored params of %s v%d are invalid now: %s", name, active.version, errors)
                raise StoredParamsInvalidError(active, errors)
            if params_hash(clean) != active.params_hash:
                if is_dev_mode():
                    logger.debug("stored params of %s v%d normalised by current schema", name, active.version)
                return self._insert_active(name, cls.version, clean)
            return active

    def save_params(self, name: str, values: Mapping[str, Any] | None) -> tuple[StrategyVersionRecord, bool]:
        """Validate and save; returns ``(active_record, created_new_version)``."""
        cls = self._registry.get(name)
        clean, errors = cls.validate_params(values)
        if errors:
            if is_dev_mode():
                logger.debug("params rejected for %s: %s", name, errors)
            raise ParamsValidationError(name, errors)
        with self._conn.transaction():
            self.ensure_strategy(name)
            active = self._active_row(name)
            if active is not None and active.params_hash == params_hash(clean):
                if is_dev_mode():
                    logger.debug("params for %s unchanged; keeping v%d", name, active.version)
                return active, False
            return self._insert_active(name, cls.version, clean), True

    def list_versions(self, name: str) -> list[StrategyVersionRecord]:
        self._registry.get(name)
        with self._conn.lock:
            rows = self._conn.execute(_SELECT + " WHERE strategy_name = ? ORDER BY version", (name,)).fetchall()
        return [_record(r) for r in rows]
