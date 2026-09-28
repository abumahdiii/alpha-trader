"""Resolve "which strategy, with which params" for the chart, the backtests and (later) live signals.

* :func:`resolve_active` -- the registered strategy ``name`` (default :data:`DEFAULT_STRATEGY`) with its ACTIVE
  params version from the params store (``strategies_repo.get_active``: the first access creates version 1
  from the defaults), re-validated by the strategy itself (``Strategy.validate_params``). Returns an
  :class:`ActiveStrategy`: strategy instance, params record, clean params, identity and params hash -- what
  ``RunConfig`` / the chart responses record as provenance. An optional ``version`` must equal the registered
  code version (:class:`StrategyVersionMismatchError` otherwise; old code versions are not kept in-process).
* :func:`strategy_for` -- an instance of a registered strategy by name (no params store), used by the backtest
  runner/jobs when a stored ``RunConfig`` is re-run without a scan.

Errors: ``UnknownStrategyError`` (not registered), :class:`StrategyVersionMismatchError`,
``StoredParamsInvalidError`` (the stored params no longer validate; never a silent fallback). The HTTP layer
maps them to Persian 404 / 404 / 409 (``routes.resolve_strategy_or_error``).

Worked example: ``resolve_active(db)`` on a fresh database -> ``stddev_channel`` code v1, params v1 = the
schema defaults, ``params_hash = sha256(canonical JSON of the defaults)``, identity
``StrategyIdentity("stddev_channel", 1, None, "builtin")``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..logging_setup import get_logger, is_dev_mode
from .base import Strategy, StrategyIdentity
from .context import DEFAULT_STRATEGY
from .params import ParamValue, params_hash
from .registry import StrategyRegistry, default_registry

if TYPE_CHECKING:
    from ..storage.db import EngineConnection
    from ..storage.strategies_repo import StrategyVersionRecord

logger = get_logger(__name__)


class StrategyVersionMismatchError(LookupError):
    """The requested code version is not the registered one."""

    def __init__(self, name: str, requested: int, available: int) -> None:
        super().__init__(f"strategy {name!r}: code version {requested} requested, v{available} registered")
        self.name = name
        self.requested = requested
        self.available = available


@dataclass(frozen=True)
class ActiveStrategy:
    """A strategy instance + its active params version (see the module docstring)."""

    strategy: Strategy
    record: StrategyVersionRecord
    params: dict[str, ParamValue]  # clean (validated) params; params_hash(params) == params_hash
    identity: StrategyIdentity
    params_hash: str

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def record_version(self) -> int:
        """Params-store version (``strategy_versions.version``)."""
        return self.record.version

    @property
    def code_version(self) -> int:
        return self.identity.version


def _registry(registry: StrategyRegistry | None) -> StrategyRegistry:
    if registry is not None:
        return registry
    from .. import strategies  # noqa: F401  (import registers the code-defined strategies)

    return default_registry


def strategy_for(name: str, version: int | None = None, *, registry: StrategyRegistry | None = None) -> Strategy:
    """Instance of the registered strategy ``name`` (``UnknownStrategyError`` /
    :class:`StrategyVersionMismatchError`)."""
    cls = _registry(registry).get(name)
    if version is not None and version != cls.version:
        raise StrategyVersionMismatchError(name, version, cls.version)
    return cls()


def resolve_active(
    db: EngineConnection,
    name: str | None = None,
    *,
    registry: StrategyRegistry | None = None,
    version: int | None = None,
) -> ActiveStrategy:
    """The strategy ``name`` (default :data:`DEFAULT_STRATEGY`) with its active params version."""
    from ..storage.strategies_repo import StoredParamsInvalidError, StrategiesRepo  # storage -> strategy order

    reg = _registry(registry)
    strategy_name = DEFAULT_STRATEGY if name is None else name
    strategy = strategy_for(strategy_name, version, registry=reg)
    record = StrategiesRepo(db, reg).get_active(strategy_name)
    clean, errors = strategy.validate_params(record.params)
    if errors:
        raise StoredParamsInvalidError(record, errors)
    phash = params_hash(clean)
    identity = strategy.identity
    if is_dev_mode():
        logger.debug("resolve_active %s: params v%d (saved under code v%d) hash=%s", identity.label(),
                     record.version, record.code_version, phash[:12])
    return ActiveStrategy(strategy=strategy, record=record, params=dict(clean), identity=identity, params_hash=phash)


__all__ = [
    "DEFAULT_STRATEGY",
    "ActiveStrategy",
    "StrategyVersionMismatchError",
    "resolve_active",
    "strategy_for",
]
