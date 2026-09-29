"""Code-defined strategy registry (stddev-channel-system.md section 7: systems are Python code).

Usage::

    from alpha_engine.strategy import registry

    @registry.register
    class StdDevChannel(Strategy):
        name = "stddev_channel"
        ...

Registering the same class object twice is a no-op (safe re-import); a *different* class with an
already-registered name raises :class:`DuplicateStrategyError`. Tests use their own
:class:`StrategyRegistry` instance instead of the process-wide default.

Plugin versions (strategy contract S1): :meth:`StrategyRegistry.replace` swaps the class registered under a
name (a new plugin version) and :meth:`StrategyRegistry.unregister` removes one. Built-in names can never be
replaced, removed or taken by a plugin: the names in :data:`RESERVED_NAMES` and every name registered by a
class with ``source = "builtin"`` (:class:`ReservedStrategyNameError`).
"""

from __future__ import annotations

import inspect
import re
import threading

from ..logging_setup import get_logger, is_dev_mode
from .base import Strategy
from .params import ParamSchema

logger = get_logger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Names of the code-defined (built-in) systems: never replaceable, removable or usable by a plugin, even
# before the built-in class is imported.
RESERVED_NAMES: frozenset[str] = frozenset({"stddev_channel"})


class DuplicateStrategyError(ValueError):
    pass


class ReservedStrategyNameError(DuplicateStrategyError):
    """A plugin tried to take, replace or remove a built-in strategy name."""


class UnknownStrategyError(KeyError):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"unknown strategy: {self.name!r}"


class InvalidStrategyError(TypeError):
    pass


def _check_class(cls: type) -> None:
    if not (inspect.isclass(cls) and issubclass(cls, Strategy)):
        raise InvalidStrategyError(f"{cls!r} is not a Strategy subclass")
    if inspect.isabstract(cls):
        raise InvalidStrategyError(f"{cls.__name__} is abstract (evaluate not implemented)")
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise InvalidStrategyError(f"{cls.__name__}.name must match {_NAME_RE.pattern}")
    version = getattr(cls, "version", None)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise InvalidStrategyError(f"{cls.__name__}.version must be an int >= 1")
    title = getattr(cls, "title_fa", None)
    if not isinstance(title, str) or not title.strip():
        raise InvalidStrategyError(f"{cls.__name__}.title_fa must be a non-empty string")
    if not isinstance(getattr(cls, "param_schema", None), ParamSchema):
        raise InvalidStrategyError(f"{cls.__name__}.param_schema must be a ParamSchema")
    source = getattr(cls, "source", "builtin")
    if source not in ("builtin", "plugin"):
        raise InvalidStrategyError(f"{cls.__name__}.source must be 'builtin' or 'plugin'")
    sha = getattr(cls, "code_sha256", None)
    if sha is not None and not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        raise InvalidStrategyError(f"{cls.__name__}.code_sha256 must be None or 64 lowercase hex characters")


def _is_builtin(cls: type[Strategy]) -> bool:
    return getattr(cls, "source", "builtin") == "builtin"


class StrategyRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type[Strategy]] = {}
        self._lock = threading.Lock()

    def _reserved_locked(self, name: str) -> bool:
        existing = self._classes.get(name)
        return name in RESERVED_NAMES or (existing is not None and _is_builtin(existing))

    def register(self, cls: type[Strategy]) -> type[Strategy]:
        """Register ``cls`` (usable as a decorator). Returns ``cls``."""
        _check_class(cls)
        with self._lock:
            if not _is_builtin(cls) and self._reserved_locked(cls.name):
                raise ReservedStrategyNameError(f"strategy name {cls.name!r} is reserved for a built-in system")
            existing = self._classes.get(cls.name)
            if existing is cls:
                return cls
            if existing is not None:
                raise DuplicateStrategyError(
                    f"strategy name {cls.name!r} already registered by {existing.__module__}.{existing.__qualname__}"
                )
            self._classes[cls.name] = cls
        if is_dev_mode():
            logger.debug("strategy registered: %s v%d (%s, %s)", cls.name, cls.version, cls.__qualname__,
                         getattr(cls, "source", "builtin"))
        return cls

    def replace(self, cls: type[Strategy]) -> type[Strategy] | None:
        """Register ``cls`` in place of the class registered under ``cls.name`` (e.g. a new plugin version);
        returns the previous class (``None`` if the name was free). Built-in names are refused."""
        _check_class(cls)
        with self._lock:
            if self._reserved_locked(cls.name) or _is_builtin(cls):
                raise ReservedStrategyNameError(
                    f"strategy {cls.name!r}: built-in systems cannot be replaced (and replace() is for plugins only)")
            previous = self._classes.get(cls.name)
            self._classes[cls.name] = cls
        if is_dev_mode():
            logger.debug("strategy replaced: %s v%s -> v%d (%s)", cls.name,
                         None if previous is None else previous.version, cls.version, cls.__qualname__)
        return previous

    def unregister(self, name: str) -> type[Strategy]:
        """Remove and return the class registered under ``name`` (plugins only)."""
        with self._lock:
            if self._reserved_locked(name):
                raise ReservedStrategyNameError(f"strategy {name!r} is a built-in system and cannot be removed")
            try:
                cls = self._classes.pop(name)
            except KeyError:
                raise UnknownStrategyError(name) from None
        if is_dev_mode():
            logger.debug("strategy unregistered: %s v%d", name, cls.version)
        return cls

    def is_reserved(self, name: str) -> bool:
        """``True`` for a built-in name (reserved, or registered by a ``source = "builtin"`` class)."""
        with self._lock:
            return self._reserved_locked(name)

    def get(self, name: str) -> type[Strategy]:
        try:
            return self._classes[name]
        except KeyError:
            raise UnknownStrategyError(name) from None

    def all(self) -> list[type[Strategy]]:
        """Registered classes in registration order."""
        return list(self._classes.values())

    def names(self) -> list[str]:
        return list(self._classes)

    def __contains__(self, name: object) -> bool:
        return name in self._classes

    def __len__(self) -> int:
        return len(self._classes)


default_registry = StrategyRegistry()


def register(cls: type[Strategy]) -> type[Strategy]:
    return default_registry.register(cls)


def replace(cls: type[Strategy]) -> type[Strategy] | None:
    return default_registry.replace(cls)


def unregister(name: str) -> type[Strategy]:
    return default_registry.unregister(name)


def get(name: str) -> type[Strategy]:
    return default_registry.get(name)


def all() -> list[type[Strategy]]:  # noqa: A001 - name required by the spec
    return default_registry.all()
