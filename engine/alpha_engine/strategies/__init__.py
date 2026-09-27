"""Concrete, code-defined trading systems (stddev-channel-system.md section 7).

Importing this package registers every system in ``alpha_engine.strategy.registry.default_registry``.
"""

from .stddev_channel import StdDevChannelStrategy

__all__ = ["StdDevChannelStrategy"]
