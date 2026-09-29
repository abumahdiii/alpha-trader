"""Storage of the live signals (phase 6; DB migration v4): ``signals``, ``signal_events`` and ``live_settings``.

SUGGESTIONS ONLY: a row here is a position the engine *suggests*; nothing in this module (or anywhere in the
engine) places, modifies or checks an order.

* ``signals`` -- one row per live decision. ``status``: ``active`` (valid until ``expires_utc``), ``expired``,
  ``superseded`` (a fresh scan after a broker bar revision no longer finds exactly this candidate) or
  ``rejected`` (no suggestion: backtest-vs-live mismatch, sizing / levels rejected at the indicative entry).
  Deduplicated by ``UNIQUE (strategy_name, strategy_version, params_hash, symbol, confirmation_bar_open_utc)``:
  :meth:`SignalsRepo.insert` returns ``None`` for a duplicate and never overwrites the first row.
* ``signal_events`` -- the scheduler's journal (``check`` / ``mt5_status`` / ``mismatch`` / ``error`` / ``tick``),
  pruned by age (:meth:`SignalsRepo.prune_events`).
* ``live_settings`` -- the single row of the user's live settings (on/off, selected strategy, grace seconds);
  defaults (:class:`LiveSettings`) until the first save. Not cleared by the ``tgc_startup`` reset (its name does
  not start with ``signal``).

Times are stored as ``format_utc`` text; the API shape (:func:`signal_to_api`) uses ISO-8601 UTC with ``Z``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from ..logging_setup import get_logger, is_dev_mode
from .db import EngineConnection, format_utc, parse_utc, utc_now_text

logger = get_logger(__name__)

SIGNAL_STATUSES: tuple[str, ...] = ("active", "expired", "superseded", "rejected")
EVENT_KINDS: tuple[str, ...] = ("tick", "mt5_status", "check", "mismatch", "error")
DEFAULT_LIVE_STRATEGY = "stddev_channel"
DEFAULT_GRACE_S = 20.0
GRACE_MIN_S, GRACE_MAX_S = 0.0, 600.0

TP_NOTE_FA = ("حد سود نمایش‌داده‌شده تقریبی است و با قیمت ورود تقریبی حساب شده؛ حد سود واقعی در لحظه ورود با "
              "قیمت واقعی (open کندل ورود) و همان نسبت R:R تعیین می‌شود.")
SUGGESTION_NOTE_FA = "این فقط یک پیشنهاد است؛ برنامه هیچ سفارشی ارسال نمی‌کند."

_JSON_COLUMNS = ("sizing_json", "extra_json", "account_json", "mismatch_json")
_COLUMNS = (
    "symbol", "strategy_name", "strategy_version", "strategy_sha256", "strategy_source", "params_version",
    "params_hash", "confirmation_bar_open_utc", "decision_time_utc", "expires_utc", "status", "direction", "setup",
    "setup_title_fa", "pattern", "line", "reference_price", "indicative_entry", "entry_source", "stop_loss",
    "take_profit_indicative", "rr", "volume", "risk_amount", "sizing_json", "reason_fa", "rejection_reason_fa",
    "backtest_would_skip", "backtest_skip_reason_fa", "extra_json", "account_json", "mismatch_json",
    "gap_class_provisional",
)
_UPDATABLE = frozenset(_COLUMNS) - {"symbol", "strategy_name", "strategy_version", "params_hash",
                                     "confirmation_bar_open_utc"}


@dataclass(frozen=True)
class LiveSettings:
    """The user's live-signal settings. ``enabled`` is OFF until the user turns it on (no background MT5 work
    without an explicit opt-in)."""

    enabled: bool = False
    live_strategy: str = DEFAULT_LIVE_STRATEGY
    grace_s: float = DEFAULT_GRACE_S

    def to_api(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "live_strategy": self.live_strategy, "grace_s": self.grace_s}


def _dt_text(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return format_utc(value)


def api_time(text: str | None) -> str | None:
    """Stored UTC text -> ``2026-09-28T10:00:00Z`` (second precision)."""
    if not text:
        return None
    return parse_utc(text).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class SignalsRepo:
    """Read/write ``signals``, ``signal_events`` and ``live_settings`` (serialised by the connection's lock)."""

    def __init__(self, conn: EngineConnection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------------------------------ settings
    def get_settings(self) -> LiveSettings:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT enabled, live_strategy, grace_s FROM live_settings WHERE id = 1").fetchone()
        if row is None:
            return LiveSettings()
        return LiveSettings(enabled=bool(row["enabled"]), live_strategy=row["live_strategy"],
                            grace_s=float(row["grace_s"]))

    def save_settings(self, settings: LiveSettings) -> LiveSettings:
        with self._conn.transaction():
            self._conn.execute(
                "INSERT INTO live_settings (id, enabled, live_strategy, grace_s, updated_utc) VALUES (1, ?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET enabled = excluded.enabled, live_strategy = excluded.live_strategy,"
                " grace_s = excluded.grace_s, updated_utc = excluded.updated_utc",
                (int(settings.enabled), settings.live_strategy, float(settings.grace_s), utc_now_text()),
            )
        if is_dev_mode():
            logger.debug("live settings saved: %s", settings.to_api())
        return settings

    def update_settings(self, **changes: Any) -> LiveSettings:
        with self._conn.transaction():
            new = replace(self.get_settings(), **{k: v for k, v in changes.items() if v is not None})
            return self.save_settings(new)

    # ------------------------------------------------------------------------------------------ signals
    def insert(self, row: dict[str, Any]) -> int | None:
        """Insert a signal row; ``None`` when the dedupe key already exists (the first row is kept)."""
        values = {c: row.get(c) for c in _COLUMNS}
        for key in ("confirmation_bar_open_utc", "decision_time_utc", "expires_utc"):
            values[key] = _dt_text(values[key])
        for key in _JSON_COLUMNS:
            value = values[key]
            values[key] = value if isinstance(value, str) or value is None else _dumps(value)
        if values["extra_json"] is None:
            values["extra_json"] = "{}"
        for key in ("backtest_would_skip", "gap_class_provisional"):
            values[key] = int(bool(values[key]))
        now = utc_now_text()
        cols = list(_COLUMNS) + ["created_utc", "updated_utc"]
        params = [values[c] for c in _COLUMNS] + [now, now]
        with self._conn.transaction():
            cur = self._conn.execute(
                f"INSERT INTO signals ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
                " ON CONFLICT (strategy_name, strategy_version, params_hash, symbol, confirmation_bar_open_utc)"
                " DO NOTHING", params)
            if cur.rowcount != 1:
                return None
            return int(cur.lastrowid)

    def update(self, signal_id: int, **fields: Any) -> dict[str, Any] | None:
        """Update some columns of one row; returns the new row (``None`` = unknown id)."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key in ("decision_time_utc", "expires_utc"):
                value = _dt_text(value)
            elif key in _JSON_COLUMNS and not (isinstance(value, str) or value is None):
                value = _dumps(value)
            elif key in ("backtest_would_skip", "gap_class_provisional"):
                value = int(bool(value))
            sets.append(f"{key} = ?")
            params.append(value)
        sets.append("updated_utc = ?")
        params.append(utc_now_text())
        with self._conn.transaction():
            self._conn.execute(f"UPDATE signals SET {', '.join(sets)} WHERE id = ?", [*params, signal_id])
        return self.get(signal_id)

    def get(self, signal_id: int) -> dict[str, Any] | None:
        with self._conn.lock:
            row = self._conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return dict(row) if row is not None else None

    def find(self, strategy_name: str, strategy_version: int, params_hash: str, symbol: str,
             confirmation_bar_open_utc: datetime) -> dict[str, Any] | None:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT * FROM signals WHERE strategy_name = ? AND strategy_version = ? AND params_hash = ? AND"
                " symbol = ? AND confirmation_bar_open_utc = ?",
                (strategy_name, strategy_version, params_hash, symbol, _dt_text(confirmation_bar_open_utc)),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_signals(self, *, status: str | None = None, symbol: str | None = None, limit: int = 50,
             offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Newest confirmation bar first (then newest id); ``total`` = rows matching the filters."""
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if symbol is not None:
            where.append("symbol = ?")
            params.append(symbol)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._conn.lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) FROM signals{clause}", params).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM signals{clause} ORDER BY confirmation_bar_open_utc DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    def with_status(self, status: str, symbol: str | None = None) -> list[dict[str, Any]]:
        rows, _ = self.list_signals(status=status, symbol=symbol, limit=1_000_000, offset=0)
        return rows

    def for_identity(self, symbol: str, strategy_name: str, strategy_version: int, params_hash: str, *,
                     since: datetime, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        """Rows of one strategy identity + symbol with ``confirmation_bar_open_utc >= since``, oldest first."""
        marks = ", ".join("?" for _ in statuses)
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE symbol = ? AND strategy_name = ? AND strategy_version = ? AND"
                f" params_hash = ? AND confirmation_bar_open_utc >= ? AND status IN ({marks})"
                " ORDER BY confirmation_bar_open_utc, id",
                (symbol, strategy_name, strategy_version, params_hash, _dt_text(since), *statuses),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------------------------------ events
    def add_event(self, kind: str, symbol: str | None, payload: dict[str, Any] | None = None,
                  when: datetime | None = None) -> None:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown signal event kind {kind!r}")
        with self._conn.transaction():
            self._conn.execute(
                "INSERT INTO signal_events (time_utc, kind, symbol, payload_json) VALUES (?, ?, ?, ?)",
                (format_utc(when or datetime.now(timezone.utc)), kind, symbol, _dumps(payload or {})),
            )

    def events(self, *, kind: str | None = None, symbol: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if symbol is not None:
            where.append("symbol = ?")
            params.append(symbol)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._conn.lock:
            rows = self._conn.execute(f"SELECT * FROM signal_events{clause} ORDER BY id LIMIT ?",
                                      [*params, limit]).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = _loads(item.pop("payload_json"))
            out.append(item)
        return out

    def prune_events(self, before: datetime) -> int:
        with self._conn.transaction():
            return int(self._conn.execute("DELETE FROM signal_events WHERE time_utc < ?",
                                          (format_utc(before),)).rowcount)


def signal_to_api(row: dict[str, Any]) -> dict[str, Any]:
    """The API item of one stored row (``GET /signals``, ``/signals/{id}``, WS ``signal``)."""
    extra = _loads(row.get("extra_json")) or {}
    candidate = extra.get("candidate") or {}
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "status": row["status"],
        "strategy": row["strategy_name"],
        "strategy_version": row["strategy_version"],
        "strategy_source": row["strategy_source"],
        "strategy_sha256": row["strategy_sha256"],
        "params_version": row["params_version"],
        "params_hash": row["params_hash"],
        "confirmation_bar_time": api_time(row["confirmation_bar_open_utc"]),
        "decision_time": api_time(row["decision_time_utc"]),
        "entry_bar_time": extra.get("entry_bar_time"),
        "expires_time": api_time(row["expires_utc"]),
        "expiry_pending": row["status"] == "active" and row["expires_utc"] is None,
        "direction": row["direction"],
        "setup_type": row["setup"],
        "setup_title_fa": row["setup_title_fa"],
        "pattern": row["pattern"],
        "line": row["line"],
        "reference_price": row["reference_price"],
        "indicative_entry": row["indicative_entry"],
        "entry_source": row["entry_source"],
        "stop_loss": row["stop_loss"],
        "take_profit_indicative": row["take_profit_indicative"],
        "take_profit_note_fa": TP_NOTE_FA,
        "rr": row["rr"],
        "volume": row["volume"],
        "risk_amount": row["risk_amount"],
        "sizing": _loads(row.get("sizing_json")),
        "account": _loads(row.get("account_json")),
        "reason_fa": row["reason_fa"],
        "rejection_reason_fa": row["rejection_reason_fa"],
        "backtest_would_skip": bool(row["backtest_would_skip"]),
        "backtest_skip_reason_fa": row["backtest_skip_reason_fa"],
        "gap_class_provisional": bool(row["gap_class_provisional"]),
        "entry_gap": extra.get("entry_gap"),
        "indicators": candidate.get("extra") or {},
        "mismatch": _loads(row.get("mismatch_json")),
        "superseded": extra.get("superseded"),
        "note_fa": SUGGESTION_NOTE_FA,
        "created_time": api_time(row["created_utc"]),
        "updated_time": api_time(row["updated_utc"]),
    }


__all__ = [
    "DEFAULT_GRACE_S",
    "DEFAULT_LIVE_STRATEGY",
    "EVENT_KINDS",
    "GRACE_MAX_S",
    "GRACE_MIN_S",
    "LiveSettings",
    "SIGNAL_STATUSES",
    "SUGGESTION_NOTE_FA",
    "SignalsRepo",
    "TP_NOTE_FA",
    "api_time",
    "signal_to_api",
]
