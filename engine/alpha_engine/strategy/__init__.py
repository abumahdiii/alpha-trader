"""Strategy framework: parameter schema, signal model, strategy interface and registry."""

from .base import (
    LookAheadError,
    Strategy,
    StrategyContext,
    StrategyContractError,
    StrategyIdentity,
    StrategyParamsError,
    WarmupTextsFa,
    assert_closed_bars,
    evaluate_checked,
    slice_closed_bars,
)
from .params import ParamSchema, ParamSpec, params_hash
from .registry import (
    DuplicateStrategyError,
    ReservedStrategyNameError,
    StrategyRegistry,
    UnknownStrategyError,
    default_registry,
)
from .signal import SignalCandidate, Setup

__all__ = [
    "DuplicateStrategyError",
    "LookAheadError",
    "ParamSchema",
    "ParamSpec",
    "ReservedStrategyNameError",
    "Setup",
    "SignalCandidate",
    "Strategy",
    "StrategyContext",
    "StrategyContractError",
    "StrategyIdentity",
    "StrategyParamsError",
    "StrategyRegistry",
    "UnknownStrategyError",
    "WarmupTextsFa",
    "assert_closed_bars",
    "default_registry",
    "evaluate_checked",
    "params_hash",
    "slice_closed_bars",
]
