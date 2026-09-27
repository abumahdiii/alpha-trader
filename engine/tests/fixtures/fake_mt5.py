"""A fake ``MetaTrader5`` module for tests (no terminal, no network).

It implements only the read-only functions the adapter may call and records every call. Rates come
from :mod:`fixtures.synthetic_ohlcv` raw (server-time) arrays; ``copy_rates_range`` treats both range
ends as inclusive, like the real package, so chunk-boundary handling is exercised.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import numpy as np

from alpha_engine.data.schema import Timeframe

from .synthetic_ohlcv import MT5_RATES_DTYPE, SyntheticSeries, symbol_info_dict

TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
_TF_BY_CONST = {TIMEFRAME_H1: Timeframe.H1, TIMEFRAME_H4: Timeframe.H4}


class FakeMt5:
    TIMEFRAME_H1 = TIMEFRAME_H1
    TIMEFRAME_H4 = TIMEFRAME_H4

    def __init__(
        self,
        series: Iterable[SyntheticSeries] = (),
        *,
        account_login: int = 12345678,
        account_server: str = "Fake-Server",
        trade_mode: int = 0,
        initialize_results: Iterable[bool] = (True,),
        account_available: bool = True,
        tick_time: int | None = None,
        invisible: Iterable[str] = (),
        call_delay: float = 0.0,
        fail_rates_for: Iterable[str] = (),
    ) -> None:
        self.rates: dict[tuple[str, Timeframe], np.ndarray] = {(s.symbol, s.timeframe): s.raw for s in series}
        self.symbols: dict[str, dict[str, Any]] = {}
        for symbol in {s.symbol for s in series}:
            self.symbols[symbol] = symbol_info_dict(symbol)
        for name in invisible:
            if name in self.symbols:
                self.symbols[name]["visible"] = False
        self.account_login = account_login
        self.account_server = account_server
        self.trade_mode = trade_mode
        self._init_results = list(initialize_results)
        self.account_available = account_available
        self.tick_time = tick_time
        self.call_delay = call_delay
        self.fail_rates_for = set(fail_rates_for)
        self.calls: list[tuple[str, tuple, dict]] = []
        self.initialized = False
        self._active = 0
        self.max_concurrency = 0
        self._guard = threading.Lock()

    # --- instrumentation --------------------------------------------------------------------------

    def _enter(self, name: str, args: tuple, kwargs: dict) -> None:
        with self._guard:
            self.calls.append((name, args, kwargs))
            self._active += 1
            self.max_concurrency = max(self.max_concurrency, self._active)
        if self.call_delay:
            time.sleep(self.call_delay)

    def _exit(self) -> None:
        with self._guard:
            self._active -= 1

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def rate_calls(self) -> list[tuple]:
        return [c[1] for c in self.calls if c[0] == "copy_rates_range"]

    # --- the read-only API ------------------------------------------------------------------------

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        self._enter("initialize", args, kwargs)
        try:
            result = self._init_results.pop(0) if len(self._init_results) > 1 else self._init_results[0]
            self.initialized = bool(result)
            return bool(result)
        finally:
            self._exit()

    def shutdown(self) -> None:
        self._enter("shutdown", (), {})
        self.initialized = False
        self._exit()

    def last_error(self) -> tuple[int, str]:
        self._enter("last_error", (), {})
        self._exit()
        return (-10003, "IPC initialize failed, MetaTrader 5 x64 not found")

    def version(self) -> tuple[int, int, str]:
        self._enter("version", (), {})
        self._exit()
        return (500, 5000, "01 Sep 2026")

    def account_info(self) -> Any:
        self._enter("account_info", (), {})
        self._exit()
        if not self.account_available:
            return None
        return SimpleNamespace(login=self.account_login, server=self.account_server, trade_mode=self.trade_mode,
                               currency="USD", company="Fake Broker")

    def symbol_info(self, name: str) -> Any:
        self._enter("symbol_info", (name,), {})
        self._exit()
        info = self.symbols.get(name)
        return SimpleNamespace(**info) if info is not None else None

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        self._enter("symbol_select", (name, enable), {})
        self._exit()
        if name not in self.symbols:
            return False
        self.symbols[name]["visible"] = bool(enable)
        return True

    def symbol_info_tick(self, name: str) -> Any:
        self._enter("symbol_info_tick", (name,), {})
        self._exit()
        if name not in self.symbols or self.tick_time is None:
            return None
        return SimpleNamespace(time=self.tick_time, bid=1.0, ask=1.1)

    def copy_rates_range(self, symbol: str, timeframe: int, date_from: Any, date_to: Any) -> np.ndarray | None:
        self._enter("copy_rates_range", (symbol, timeframe, date_from, date_to), {})
        try:
            if symbol in self.fail_rates_for:
                return None
            tf = _TF_BY_CONST.get(timeframe)
            raw = self.rates.get((symbol, tf)) if tf is not None else None
            if raw is None:
                return None if symbol not in self.symbols else np.zeros(0, dtype=MT5_RATES_DTYPE)
            lo, hi = int(date_from), int(date_to)
            mask = (raw["time"] >= lo) & (raw["time"] <= hi)
            return raw[mask].copy()
        finally:
            self._exit()

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> np.ndarray | None:
        self._enter("copy_rates_from_pos", (symbol, timeframe, start_pos, count), {})
        self._exit()
        tf = _TF_BY_CONST.get(timeframe)
        raw = self.rates.get((symbol, tf))
        if raw is None:
            return None
        end = len(raw) - start_pos
        return raw[max(0, end - count):end].copy()
