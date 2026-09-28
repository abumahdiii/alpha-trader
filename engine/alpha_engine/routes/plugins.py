"""``/plugins`` -- uploaded strategy plugins (prefix ``/plugins`` so nothing shadows ``/strategies/{name}``).

* ``GET /plugins/template`` -> ``{filename, content}``: the downloadable template (:mod:`..plugins.template`).
* ``POST /plugins`` body ``{"filename": "x.py", "source": "<the file text>"}`` (JSON; no multipart):
  static checks -> strict version check -> sandboxed dynamic checks -> stored + registered (latest active version
  of the name). ``201`` new version; ``200`` the same file again (same name, version and sha256; an archived copy
  is restored); ``409 version_conflict`` same name + version with a different file (bump ``version``);
  ``422 invalid_plugin`` with Persian ``errors_fa`` (line numbers); ``422 invalid_body`` for a malformed body.
* ``GET /plugins`` -> every stored version (newest first per name), including disabled and archived ones.
* ``POST /plugins/{name}/{version}/disable`` | ``/enable`` -> the record (enable of an archived version: 409
  ``plugin_archived``; a file whose hash no longer matches: 409 ``plugin_file_invalid``).
* ``DELETE /plugins/{name}/{version}`` -> archive (files kept under ``archive/``); 409 ``plugin_in_use`` while a
  queued/running backtest uses that version.

Item: ``{name, version, sha256, title_fa, status, param_schema, validation, filename, created_utc, registered}``;
``validation = {"static": [passed check groups], "dynamic": {bars, candidates, prefix_checks, determinism,
future_mutation, elapsed_s, budget_s, worker_boot_s, peak_memory_mib}}``. Errors: ``{"detail": {"code",
"message_fa", "errors_fa"}}``. The uploaded text is never logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePath
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from ..logging_setup import get_logger, is_dev_mode
from ..plugins.checks import validate_plugin
from ..plugins.store import (
    STORE_LOCK,
    PluginFileError,
    PluginNotFoundError,
    PluginRecord,
    PluginStore,
    sync_registration,
)
from ..plugins.template import TEMPLATE_FILENAME, TEMPLATE_SOURCE
from ..plugins.validator import MAX_SOURCE_BYTES
from ..storage.backtests_repo import ACTIVE_STATUSES
from ..storage.db import EngineConnection, parse_utc
from ..strategy.params import ParamSpec
from ..strategy.registry import StrategyRegistry
from . import api_error, get_db
from .strategies import ErrorResponse, get_registry

logger = get_logger(__name__)

router = APIRouter(prefix="/plugins", tags=["plugins"])

INVALID_PLUGIN_FA = "فایل سیستم پذیرفته نشد؛ خطاها را برطرف کنید و دوباره بارگذاری کنید."
VERSION_CONFLICT_FA = ("نسخه {version} سیستم «{name}» قبلا با محتوای دیگری بارگذاری شده است؛ برای هر تغییر، "
                       "عدد version را در فایل بالا ببرید.")
NOT_FOUND_FA = "نسخه {version} سیستم «{name}» پیدا نشد."
ARCHIVED_FA = "نسخه {version} سیستم «{name}» بایگانی شده است؛ برای استفاده دوباره همان فایل را بارگذاری کنید."
IN_USE_FA = "یک بک‌تست در صف یا در حال اجرا از نسخه {version} سیستم «{name}» استفاده می‌کند؛ بعد از پایان آن دوباره تلاش کنید."
FILE_INVALID_FA = "فایل ذخیره‌شده سیستم «{name}» (نسخه {version}) تغییر کرده یا پیدا نشد و اجرا نمی‌شود."

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Plugin version not found"},
    409: {"model": ErrorResponse, "description": "version_conflict / plugin_archived / plugin_in_use / plugin_file_invalid"},
    422: {"model": ErrorResponse, "description": "invalid_plugin / invalid_body (Persian errors_fa)"},
    503: {"model": ErrorResponse, "description": "Database not available"},
}


class TemplateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    content: str


class PluginOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int
    sha256: str
    title_fa: str
    status: Literal["active", "disabled", "archived"]
    param_schema: list[ParamSpec]
    validation: dict[str, Any]
    filename: str | None
    created_utc: str  # ISO-8601 UTC, "Z", second precision
    registered: bool  # this version is the one the strategy registry currently runs for ``name``


def get_store(request: Request, db: EngineConnection = Depends(get_db)) -> PluginStore:
    return PluginStore(db, request.app.state.settings.data_dir)


def _track(request: Request, name: str) -> None:
    names = getattr(request.app.state, "plugin_names", None)
    if isinstance(names, set):
        names.add(name)


def _registered(registry: StrategyRegistry, record: PluginRecord) -> bool:
    if record.name not in registry:
        return False
    cls = registry.get(record.name)
    return getattr(cls, "code_sha256", None) == record.sha256 and cls.version == record.version


def _out(record: PluginRecord, registry: StrategyRegistry) -> dict[str, Any]:
    return {"name": record.name, "version": record.version, "sha256": record.sha256, "title_fa": record.title_fa,
            "status": record.status, "param_schema": record.param_schema, "validation": record.validation,
            "filename": record.filename, "created_utc": parse_utc(record.created_utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "registered": _registered(registry, record)}


def _sync(request: Request, store: PluginStore, registry: StrategyRegistry, name: str, version: int) -> None:
    try:
        sync_registration(store, registry, name)
    except PluginFileError as exc:
        logger.warning("plugin %s could not be registered: %s", name, exc)
        raise api_error(409, "plugin_file_invalid", FILE_INVALID_FA.format(name=name, version=version)) from None
    _track(request, name)


def _require(store: PluginStore, name: str, version: int) -> PluginRecord:
    try:
        return store.require(name, version)
    except PluginNotFoundError:
        raise api_error(404, "plugin_not_found", NOT_FOUND_FA.format(name=name, version=version)) from None


def _reserved_names(registry: StrategyRegistry) -> set[str]:
    return {name for name in registry.names() if registry.is_reserved(name)}


def _body(payload: Any) -> tuple[str, str]:
    errors: list[str] = []
    if not isinstance(payload, Mapping):
        raise api_error(422, "invalid_body", "بدنه درخواست نامعتبر است.",
                        ["بدنه درخواست باید به شکل {\"filename\": \"...py\", \"source\": \"...\"} باشد."])
    errors.extend(f"فیلد ناشناخته در بدنه درخواست: {k!r}." for k in payload if k not in ("filename", "source"))
    filename, source = payload.get("filename"), payload.get("source")
    if not isinstance(filename, str) or not filename.strip():
        errors.append("فیلد «filename» (نام فایل .py) لازم است.")
    elif len(filename) > 255 or not filename.lower().endswith(".py"):
        errors.append("نام فایل باید با «.py» تمام شود و حداکثر ۲۵۵ کاراکتر باشد.")
    if not isinstance(source, str) or not source:
        errors.append("فیلد «source» (متن فایل پایتون) لازم است.")
    elif len(source) > MAX_SOURCE_BYTES:
        errors.append(f"حجم فایل از سقف مجاز {MAX_SOURCE_BYTES} بایت (۲۵۶ کیلوبایت) بیشتر است.")
    if errors:
        raise api_error(422, "invalid_body", "بدنه درخواست نامعتبر است.", errors)
    assert isinstance(filename, str) and isinstance(source, str)
    return PurePath(filename.replace("\\", "/")).name, source


@router.get("/template", response_model=TemplateOut)
def get_template() -> dict[str, str]:
    return {"filename": TEMPLATE_FILENAME, "content": TEMPLATE_SOURCE}


@router.get("", response_model=list[PluginOut], responses=ERROR_RESPONSES)
def list_plugins(store: PluginStore = Depends(get_store),
                 registry: StrategyRegistry = Depends(get_registry)) -> list[dict[str, Any]]:
    return [_out(r, registry) for r in store.list()]


@router.post("", response_model=PluginOut, status_code=201, responses=ERROR_RESPONSES)
def upload_plugin(
    request: Request,
    response: Response,
    payload: Any = Body(..., examples=[{"filename": TEMPLATE_FILENAME, "source": "<python source>"}]),
    store: PluginStore = Depends(get_store),
    registry: StrategyRegistry = Depends(get_registry),
) -> dict[str, Any]:
    filename, source = _body(payload)
    reserved = _reserved_names(registry)
    with STORE_LOCK:
        static_only = validate_plugin(source, reserved_names=reserved, dynamic=False)
        static = static_only.static
        if is_dev_mode():
            logger.debug("POST /plugins: file=%r %d bytes sha=%s name=%s v%s static_ok=%s", filename,
                         static.size_bytes, (static.sha256 or "")[:12], static.name, static.version, static.ok)
        if not static.ok:
            raise api_error(422, "invalid_plugin", INVALID_PLUGIN_FA, static.errors_fa)
        assert static.name is not None and static.version is not None and static.sha256 is not None
        name, version = static.name, static.version
        existing = store.get(name, version)
        if existing is not None:
            if existing.sha256 != static.sha256:
                if is_dev_mode():
                    logger.debug("POST /plugins: %s v%d exists with sha=%s -> 409", name, version, existing.sha256[:12])
                raise api_error(409, "version_conflict", VERSION_CONFLICT_FA.format(name=name, version=version))
            if existing.status == "archived":
                try:
                    existing = store.restore(name, version)
                except PluginFileError:
                    raise api_error(409, "plugin_file_invalid", FILE_INVALID_FA.format(name=name, version=version)) from None
            _sync(request, store, registry, name, version)
            response.status_code = 200
            return _out(existing, registry)

        full = validate_plugin(source, reserved_names=reserved)
        if not full.ok or full.dynamic is None or full.dynamic.description is None or full.source is None:
            raise api_error(422, "invalid_plugin", INVALID_PLUGIN_FA, full.errors_fa)
        desc = full.dynamic.description
        manifest = {"filename": filename, "class_name": desc.class_name, "param_schema": desc.schema_list(),
                    "history_bars": desc.history_bars, "overrides": list(desc.overrides),
                    "size_bytes": full.static.size_bytes}
        record = store.add(source=full.source, name=name, version=version, title_fa=desc.title_fa,
                           manifest=manifest, validation=full.validation())
        _sync(request, store, registry, name, version)
    if is_dev_mode():
        logger.debug("POST /plugins: %s v%d stored and registered (validation %.2f s)", name, version,
                     full.dynamic.elapsed_s)
    return _out(record, registry)


def _set_status(request: Request, store: PluginStore, registry: StrategyRegistry, name: str, version: int,
                enable: bool) -> dict[str, Any]:
    with STORE_LOCK:
        record = _require(store, name, version)
        if record.status == "archived":
            raise api_error(409, "plugin_archived", ARCHIVED_FA.format(name=name, version=version))
        if enable:
            try:
                store.read_source(record)
            except PluginFileError:
                raise api_error(409, "plugin_file_invalid", FILE_INVALID_FA.format(name=name, version=version)) from None
        record = store.set_status(name, version, "active" if enable else "disabled")
        _sync(request, store, registry, name, version)
    if is_dev_mode():
        logger.debug("plugin %s v%d %s", name, version, "enabled" if enable else "disabled")
    return _out(record, registry)


@router.post("/{name}/{version}/disable", response_model=PluginOut, responses=ERROR_RESPONSES)
def disable_plugin(name: str, version: int, request: Request, store: PluginStore = Depends(get_store),
                   registry: StrategyRegistry = Depends(get_registry)) -> dict[str, Any]:
    return _set_status(request, store, registry, name, version, enable=False)


@router.post("/{name}/{version}/enable", response_model=PluginOut, responses=ERROR_RESPONSES)
def enable_plugin(name: str, version: int, request: Request, store: PluginStore = Depends(get_store),
                  registry: StrategyRegistry = Depends(get_registry)) -> dict[str, Any]:
    return _set_status(request, store, registry, name, version, enable=True)


@router.delete("/{name}/{version}", response_model=PluginOut, responses=ERROR_RESPONSES)
def archive_plugin(name: str, version: int, request: Request, store: PluginStore = Depends(get_store),
                   registry: StrategyRegistry = Depends(get_registry)) -> dict[str, Any]:
    with STORE_LOCK:
        record = _require(store, name, version)
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with store.db.lock:
            busy = store.db.execute(
                f"SELECT COUNT(*) FROM backtest_runs WHERE status IN ({placeholders}) AND strategy_name = ? "
                "AND strategy_version = ?", (*sorted(ACTIVE_STATUSES), name, int(version))).fetchone()[0]
        if busy:
            if is_dev_mode():
                logger.debug("DELETE /plugins/%s/%d refused: %d active backtest run(s)", name, version, busy)
            raise api_error(409, "plugin_in_use", IN_USE_FA.format(name=name, version=version))
        try:
            record = store.archive(name, version)
        except PluginFileError:
            raise api_error(409, "plugin_file_invalid", FILE_INVALID_FA.format(name=name, version=version)) from None
        try:
            sync_registration(store, registry, name)
        except PluginFileError as exc:  # another (older) active version is broken: just leave it unregistered
            logger.warning("plugin %s: previous version could not be registered after archiving: %s", name, exc)
    return _out(record, registry)
