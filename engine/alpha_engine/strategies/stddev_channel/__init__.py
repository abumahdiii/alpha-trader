"""Trading system 1: StdDev (linear regression) channel. Importing this package registers the strategy."""

from .params import SCHEMA, InvalidParamsError, StdDevParams, resolve_params
from .strategy import StdDevChannelStrategy, scan

__all__ = ["SCHEMA", "InvalidParamsError", "StdDevChannelStrategy", "StdDevParams", "resolve_params", "scan"]
