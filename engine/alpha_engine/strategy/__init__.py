"""Strategy framework: parameter schema, signal model, strategy interface and registry."""

from .base import (
    LookAheadError,
    Strategy,
    StrategyContext,
    StrategyContractError,
    assert_closed_bars,
    evaluate_checked,
    slice_closed_bars,
)
from .params import ParamSchema, ParamSpec, params_hash
from .registry import DuplicateStrategyError, StrategyRegistry, UnknownStrategyError, default_registry
from .signal import SignalCandidate, Setup

__all__ = [
    "DuplicateStrategyError",
    "LookAheadError",
    "ParamSchema",
    "ParamSpec",
    "Setup",
    "SignalCandidate",
    "Strategy",
    "StrategyContext",
    "StrategyContractError",
    "StrategyRegistry",
    "UnknownStrategyError",
    "assert_closed_bars",
    "default_registry",
    "evaluate_checked",
    "params_hash",
    "slice_closed_bars",
]
