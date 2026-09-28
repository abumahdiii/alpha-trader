"""Plugin storage: files under ``<data_dir>/strategies/plugins/`` + the ``strategy_plugins`` table (DB v3).

Layout (``name`` is a validated slug, so it is safe in a path)::

    strategies/plugins/<name>/<version>-<sha12>.py      the source, byte-exact (UTF-8, what was hashed)
    strategies/plugins/<name>/<version>-<sha12>.json    manifest (name, version, sha256, title, param schema, ...)
    strategies/plugins/<name>/archive/...               the same two files after DELETE (archive)

Strict versions (user decision 6-الف.4): ``(name, version)`` is unique forever. Uploading the same name + version
with the SAME sha256 is idempotent (an archived one is restored as active); with a DIFFERENT sha256 it is refused
(:class:`VersionConflictError`) -- a changed file needs a new version. Old versions are never deleted: disable /
enable switch ``status``, delete = archive (files moved to ``archive/``, ``status = 'archived'``).

The file's SHA-256 is re-checked on every load (:meth:`PluginStore.read_source`, :class:`PluginFileError`), so an
edited file on disk is never run under the recorded identity.

Registration (:func:`sync_registration`): for each plugin name the LATEST ``active`` version is registered in the
strategy registry as a :class:`~.host.PluginStrategyProxy` (``registry.replace``); a name without an active
version is unregistered. :func:`register_active_plugins` does this for every name at startup; failures are logged
and skipped, never raised.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..logging_setup import get_logger, is_dev_mode
from ..storage.db import EngineConnection, utc_now_text
from ..strategy.registry import ReservedStrategyNameError, StrategyRegistry, UnknownStrategyError
from .validator import NAME_RE, source_sha256

logger = get_logger(__name__)

PluginStatus = Literal["active", "disabled", "archived"]
PLUGINS_SUBDIR = ("strategies", "plugins")
ARCHIVE_DIR = "archive"

# One lock for every mutation of the plugin files / rows (uploads are rare; validation runs under it).
STORE_LOCK = threading.RLock()


class PluginStoreError(RuntimeError):
    pass


class VersionConflictError(PluginStoreError):
    def __init__(self, name: str, version: int, existing_sha: str) -> None:
        super().__init__(f"plugin {name} v{version} already exists with sha {existing_sha[:12]}")
        self.name = name
        self.version = version
        self.existing_sha = existing_sha


class PluginNotFoundError(PluginStoreError):
    def __init__(self, name: str, version: int) -> None:
        super().__init__(f"plugin {name} v{version} not found")
        self.name = name
        self.version = version


class PluginFileError(PluginStoreError):
    """The stored file is missing or its SHA-256 no longer matches the record."""


@dataclass(frozen=True)
class PluginRecord:
    id: int
    name: str
    version: int
    sha256: str
    title_fa: str
    file_relpath: str
    manifest: dict[str, Any]
    validation: dict[str, Any]
    status: PluginStatus
    created_utc: str

    @property
    def param_schema(self) -> list[dict[str, Any]]:
        return list(self.manifest.get("param_schema") or [])

    @property
    def history_bars(self) -> int:
        return int(self.manifest.get("history_bars") or 1)

    @property
    def filename(self) -> str | None:
        return self.manifest.get("filename")

    @property
    def class_name(self) -> str:
        return str(self.manifest.get("class_name") or "")


def _row_to_record(row: Any) -> PluginRecord:
    return PluginRecord(id=int(row["id"]), name=row["name"], version=int(row["version"]), sha256=row["sha256"],
                        title_fa=row["title_fa"], file_relpath=row["file_relpath"],
                        manifest=json.loads(row["manifest_json"]), validation=json.loads(row["validation_json"]),
                        status=row["status"], created_utc=row["created_utc"])


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PluginNotFoundError(str(name), 0)


class PluginStore:
    """Plugin files + rows. ``data_dir`` = the engine data directory (``Settings.data_dir``)."""

    def __init__(self, db: EngineConnection, data_dir: str | Path) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.root = self.data_dir.joinpath(*PLUGINS_SUBDIR)

    # ------------------------------------------------------------------ queries
    def list(self) -> list[PluginRecord]:
        with self.db.lock:
            rows = self.db.execute("SELECT * FROM strategy_plugins ORDER BY name, version DESC").fetchall()
        return [_row_to_record(r) for r in rows]

    def get(self, name: str, version: int) -> PluginRecord | None:
        with self.db.lock:
            row = self.db.execute("SELECT * FROM strategy_plugins WHERE name = ? AND version = ?",
                                  (name, int(version))).fetchone()
        return None if row is None else _row_to_record(row)

    def require(self, name: str, version: int) -> PluginRecord:
        _check_name(name)
        record = self.get(name, version)
        if record is None:
            raise PluginNotFoundError(name, version)
        return record

    def latest_active(self, name: str) -> PluginRecord | None:
        with self.db.lock:
            row = self.db.execute("SELECT * FROM strategy_plugins WHERE name = ? AND status = 'active' "
                                  "ORDER BY version DESC LIMIT 1", (name,)).fetchone()
        return None if row is None else _row_to_record(row)

    def names(self) -> list[str]:
        with self.db.lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT name FROM strategy_plugins ORDER BY name")]

    # ------------------------------------------------------------------ files
    def _path(self, relpath: str) -> Path:
        path = (self.data_dir / relpath).resolve()
        root = self.root.resolve()
        if root not in path.parents:
            raise PluginFileError(f"plugin path outside the plugin directory: {relpath}")
        return path

    def _relpath(self, name: str, version: int, sha: str, *, archived: bool = False, suffix: str = ".py") -> str:
        parts = [*PLUGINS_SUBDIR, name] + ([ARCHIVE_DIR] if archived else []) + [f"{version}-{sha[:12]}{suffix}"]
        return "/".join(parts)

    def read_source(self, record: PluginRecord) -> str:
        """The stored source, verified against the recorded SHA-256 (:class:`PluginFileError` otherwise)."""
        path = self._path(record.file_relpath)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PluginFileError(f"plugin file missing: {record.file_relpath} ({exc.strerror})") from None
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise PluginFileError(f"plugin file is not UTF-8: {record.file_relpath}") from None
        actual = source_sha256(text)
        if actual != record.sha256:
            raise PluginFileError(f"plugin file {record.file_relpath} was modified: sha {actual[:12]} != "
                                  f"recorded {record.sha256[:12]}")
        return text

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    # ------------------------------------------------------------------ mutations
    def add(self, *, source: str, name: str, version: int, title_fa: str, manifest: dict[str, Any],
            validation: dict[str, Any]) -> PluginRecord:
        """Store a NEW validated version (caller checked :meth:`get` under :data:`STORE_LOCK`)."""
        sha = source_sha256(source)
        relpath = self._relpath(name, version, sha)
        manifest_rel = self._relpath(name, version, sha, suffix=".json")
        created = utc_now_text()
        full_manifest = {**manifest, "name": name, "version": version, "sha256": sha, "title_fa": title_fa,
                         "file": relpath, "created_utc": created}
        path, manifest_path = self._path(relpath), self._path(manifest_rel)
        with STORE_LOCK:
            self._write_atomic(path, source.encode("utf-8", errors="surrogatepass"))
            self._write_atomic(manifest_path, json.dumps(full_manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            try:
                with self.db.transaction():
                    self.db.execute(
                        "INSERT INTO strategy_plugins (name, version, sha256, title_fa, file_relpath, manifest_json,"
                        " validation_json, status, created_utc) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                        (name, int(version), sha, title_fa, relpath, json.dumps(full_manifest, ensure_ascii=False),
                         json.dumps(validation, ensure_ascii=False), created))
            except Exception:
                for leftover in (path, manifest_path):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
                raise
        if is_dev_mode():
            logger.debug("plugin stored: %s v%d sha=%s file=%s", name, version, sha[:12], relpath)
        record = self.get(name, version)
        assert record is not None
        return record

    def set_status(self, name: str, version: int, status: PluginStatus) -> PluginRecord:
        with STORE_LOCK:
            record = self.require(name, version)
            if record.status == status:
                return record
            with self.db.transaction():
                self.db.execute("UPDATE strategy_plugins SET status = ? WHERE id = ?", (status, record.id))
        if is_dev_mode():
            logger.debug("plugin %s v%d: status %s -> %s", name, version, record.status, status)
        return self.require(name, version)

    def _move(self, record: PluginRecord, *, to_archive: bool) -> str:
        new_rel = self._relpath(record.name, record.version, record.sha256, archived=to_archive)
        for suffix in (".py", ".json"):
            src_rel = record.file_relpath[: -len(".py")] + suffix
            dst_rel = new_rel[: -len(".py")] + suffix
            src, dst = self._path(src_rel), self._path(dst_rel)
            if src == dst or not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        return new_rel

    def archive(self, name: str, version: int) -> PluginRecord:
        """Delete = archive: files moved to ``archive/``, status ``archived`` (nothing is removed)."""
        with STORE_LOCK:
            record = self.require(name, version)
            if record.status == "archived":
                return record
            self.read_source(record)  # never archive (and later restore) a file that no longer matches
            new_rel = self._move(record, to_archive=True)
            with self.db.transaction():
                self.db.execute("UPDATE strategy_plugins SET status = 'archived', file_relpath = ? WHERE id = ?",
                                (new_rel, record.id))
        if is_dev_mode():
            logger.debug("plugin %s v%d archived (%s)", name, version, new_rel)
        return self.require(name, version)

    def restore(self, name: str, version: int) -> PluginRecord:
        """Archived -> active (same file back in place; sha re-checked)."""
        with STORE_LOCK:
            record = self.require(name, version)
            if record.status != "archived":
                return record
            self.read_source(record)
            new_rel = self._move(record, to_archive=False)
            with self.db.transaction():
                self.db.execute("UPDATE strategy_plugins SET status = 'active', file_relpath = ? WHERE id = ?",
                                (new_rel, record.id))
        if is_dev_mode():
            logger.debug("plugin %s v%d restored from the archive", name, version)
        return self.require(name, version)


# ------------------------------------------------------------------------------------------ registration
def proxy_for(store: PluginStore, record: PluginRecord) -> type:
    """A registrable proxy class for ``record`` (source read and sha-checked; no worker is started)."""
    from .host import make_proxy_class

    source = store.read_source(record)
    return make_proxy_class(name=record.name, version=record.version, title_fa=record.title_fa,
                            param_schema=record.param_schema, history_bars=record.history_bars, sha256=record.sha256,
                            source=source, class_name=record.class_name)


def sync_registration(store: PluginStore, registry: StrategyRegistry, name: str) -> PluginRecord | None:
    """Register the latest ACTIVE version of plugin ``name`` (or unregister the name if none is active).
    Returns the registered record. Raises :class:`PluginFileError` if the file no longer matches."""
    record = store.latest_active(name)
    if record is None:
        if name in registry and getattr(registry.get(name), "source", "builtin") == "plugin":
            try:
                registry.unregister(name)
            except (UnknownStrategyError, ReservedStrategyNameError):
                pass
            if is_dev_mode():
                logger.debug("plugin %s: no active version, unregistered", name)
        return None
    current = registry.get(name) if name in registry else None
    if current is not None and getattr(current, "code_sha256", None) == record.sha256 \
            and current.version == record.version:
        return record
    try:
        proxy = proxy_for(store, record)
    except Exception:
        # Never leave another (e.g. just archived) version registered under this name.
        if current is not None and getattr(current, "source", "builtin") == "plugin":
            try:
                registry.unregister(name)
            except (UnknownStrategyError, ReservedStrategyNameError):
                pass
        raise
    registry.replace(proxy)
    if is_dev_mode():
        logger.debug("plugin %s: v%d registered (sha=%s)", name, record.version, record.sha256[:12])
    return record


def register_active_plugins(store: PluginStore, registry: StrategyRegistry) -> list[str]:
    """Startup: register every plugin name's latest active version. Failures are logged, never raised."""
    registered: list[str] = []
    for name in store.names():
        try:
            if sync_registration(store, registry, name) is not None:
                registered.append(name)
        except Exception as exc:  # a broken/modified plugin must never stop the engine
            logger.warning("plugin %s could not be registered (%s: %s)", name, type(exc).__name__, exc)
            if is_dev_mode():
                logger.debug("plugin %s registration failed", name, exc_info=True)
    return registered


__all__ = [
    "STORE_LOCK",
    "PluginFileError",
    "PluginNotFoundError",
    "PluginRecord",
    "PluginStore",
    "PluginStoreError",
    "VersionConflictError",
    "proxy_for",
    "register_active_plugins",
    "sync_registration",
]
