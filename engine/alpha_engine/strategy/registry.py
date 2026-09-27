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


class DuplicateStrategyError(ValueError):
    pass


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


class StrategyRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type[Strategy]] = {}
        self._lock = threading.Lock()

    def register(self, cls: type[Strategy]) -> type[Strategy]:
        """Register ``cls`` (usable as a decorator). Returns ``cls``."""
        _check_class(cls)
        with self._lock:
            existing = self._classes.get(cls.name)
            if existing is cls:
                return cls
            if existing is not None:
                raise DuplicateStrategyError(
                    f"strategy name {cls.name!r} already registered by {existing.__module__}.{existing.__qualname__}"
                )
            self._classes[cls.name] = cls
        if is_dev_mode():
            logger.debug("strategy registered: %s v%d (%s)", cls.name, cls.version, cls.__qualname__)
        return cls

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


def get(name: str) -> type[Strategy]:
    return default_registry.get(name)


def all() -> list[type[Strategy]]:  # noqa: A001 - name required by the spec
    return default_registry.all()
