"""Parquet cache: schema + metadata, incremental merge, atomic write, single-writer lock."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from alpha_engine.data import cache as cache_mod
from alpha_engine.data.cache import (
    META_KEY,
    CacheLockTimeout,
    CacheMeta,
    FileLock,
    OhlcvCache,
    merge_frames,
    pid_alive,
)
from alpha_engine.data.schema import TIME_DTYPE, OhlcvValidationError, Timeframe
from alpha_engine.data.symbols import InvalidSymbolName, SymbolSpec
from fixtures.synthetic_ohlcv import symbol_info_dict


def _meta(**kw) -> CacheMeta:
    base = {"symbol": "XAUUSD.x", "timeframe": "H1", "source": "mt5", "offset_model": "us_dst(+2)",
            "fetched_at_utc": "2025-05-05T00:00:00Z"}
    base.update(kw)
    return CacheMeta(**base)


def test_write_read_roundtrip_with_metadata(tmp_path: Path, synthetic) -> None:
    frame = synthetic[("XAUUSD.x", Timeframe.H1)].utc
    cache = OhlcvCache(tmp_path)
    written = cache.write("XAUUSD.x", "H1", frame, _meta(first_available_utc="2025-02-24T00:00:00Z"))
    path = tmp_path / "cache" / "ohlcv" / "XAUUSD.x_H1.parquet"
    assert path.is_file() and not path.with_name(path.name + ".lock").exists()
    raw_meta = json.loads(pq.read_schema(path).metadata[META_KEY])
    assert raw_meta["source"] == "mt5" and raw_meta["schema_version"] == 1
    assert raw_meta["offset_model"] == "us_dst(+2)" and raw_meta["rows"] == len(frame)
    back, meta = cache.read("XAUUSD.x", "H1")
    pd.testing.assert_frame_equal(back, frame)
    assert str(back["time"].dtype) == TIME_DTYPE
    assert meta == written and meta.first_bar_utc == "2025-02-24T00:00:00Z"
    assert cache.read_meta("XAUUSD.x", "H1") == written
    assert cache.list_series() == [("XAUUSD.x", Timeframe.H1)]
    assert cache.read("BRNUSD.x", "H1") is None and cache.read_meta("BRNUSD.x", "H4") is None


def test_write_rejects_invalid_frames_and_names(tmp_path: Path, synthetic) -> None:
    cache = OhlcvCache(tmp_path)
    bad = synthetic[("XAUUSD.x", Timeframe.H1)].utc.head(5).copy()
    bad.loc[2, "high"] = 1.0
    with pytest.raises(OhlcvValidationError):
        cache.write("XAUUSD.x", "H1", bad, _meta())
    assert not (tmp_path / "cache" / "ohlcv" / "XAUUSD.x_H1.parquet").exists()
    with pytest.raises(InvalidSymbolName):
        cache.series_path("../evil", "H1")


def test_merge_newer_wins_no_dup_no_loss(synthetic) -> None:
    frame = synthetic[("XAUUSD.x", Timeframe.H1)].utc
    old, new = frame.iloc[:100].copy(), frame.iloc[95:150].copy()
    new.loc[new.index[0], "close"] = new["close"].iloc[0] + 0.01  # revised overlapping bar
    new.loc[new.index[0], "high"] = max(new["high"].iloc[0], new["close"].iloc[0])
    merged = merge_frames(old, new)
    assert len(merged) == 150 and merged["time"].is_unique
    assert merged.loc[95, "close"] == new["close"].iloc[0]
    assert merge_frames(None, None).empty


def test_atomic_write_keeps_old_file_on_failure(tmp_path: Path, synthetic, monkeypatch) -> None:
    frame = synthetic[("XAUUSD.x", Timeframe.H1)].utc
    cache = OhlcvCache(tmp_path)
    cache.write("XAUUSD.x", "H1", frame.iloc[:10], _meta())

    def boom(table, where, *a, **k):
        Path(where).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(cache_mod.pq, "write_table", boom)
    with pytest.raises(OSError):
        cache.write("XAUUSD.x", "H1", frame, _meta())
    back, meta = cache.read("XAUUSD.x", "H1")
    assert len(back) == 10 and meta.rows == 10
    leftovers = [p.name for p in (tmp_path / "cache" / "ohlcv").iterdir()]
    assert leftovers == ["XAUUSD.x_H1.parquet"]  # no temp file, no lock


def test_spec_and_offset_fit_json(tmp_path: Path) -> None:
    cache = OhlcvCache(tmp_path)
    spec = SymbolSpec.from_mt5(symbol_info_dict("XAUUSD.x"))
    cache.write_spec(spec, "mt5")
    back, info = cache.read_spec("XAUUSD.x")
    assert back == spec and info["source"] == "mt5"
    assert cache.read_spec("BRNUSD.x") is None and cache.read_offset_fit() is None


# --- lock ------------------------------------------------------------------------------------------

def test_lock_exclusive_and_released(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with FileLock(path) as lock:
        assert lock.held and path.exists()
        owner = json.loads(path.read_text())
        assert owner["pid"] == os.getpid()
        with pytest.raises(CacheLockTimeout):
            FileLock(path, timeout=0.2, poll=0.01).acquire()
    assert not path.exists()


def test_lock_contention_serializes_writers(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    inside, max_inside, order = [0], [0], []
    guard = threading.Lock()

    def worker(i: int) -> None:
        with FileLock(path, timeout=10, poll=0.005):
            with guard:
                inside[0] += 1
                max_inside[0] = max(max_inside[0], inside[0])
            time.sleep(0.02)
            order.append(i)
            with guard:
                inside[0] -= 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max_inside[0] == 1 and sorted(order) == list(range(6))


def test_stale_lock_dead_pid_is_broken(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    path.write_text(json.dumps({"pid": 999999, "created": time.time(), "token": "old"}))
    lock = FileLock(path, timeout=1, is_alive=lambda pid: False)
    lock.acquire()
    assert json.loads(path.read_text())["pid"] == os.getpid()
    lock.release()


def test_stale_lock_too_old_is_broken_but_fresh_live_lock_is_not(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    path.write_text(json.dumps({"pid": os.getpid(), "created": time.time() - 3600, "token": "old"}))
    lock = FileLock(path, timeout=1, stale_after=600)
    lock.acquire()
    lock.release()
    path.write_text(json.dumps({"pid": os.getpid(), "created": time.time(), "token": "live"}))
    with pytest.raises(CacheLockTimeout):
        FileLock(path, timeout=0.1, poll=0.01, stale_after=600).acquire()
    assert json.loads(path.read_text())["token"] == "live"  # never removed a live lock


def test_release_does_not_remove_foreign_lock(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    lock = FileLock(path)
    lock.acquire()
    path.write_text(json.dumps({"pid": 1, "created": time.time(), "token": "someone-else"}))
    lock.release()
    assert path.exists()


def test_pid_alive_never_signals() -> None:
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False and pid_alive(-5) is False
    assert pid_alive(4_000_000) is False


def test_cache_write_waits_for_lock(tmp_path: Path, synthetic) -> None:
    cache = OhlcvCache(tmp_path, lock_timeout=0.2)
    frame = synthetic[("XAUUSD.x", Timeframe.H1)].utc.head(5)
    with cache.lock("XAUUSD.x", "H1"):
        with pytest.raises(CacheLockTimeout):
            cache.write("XAUUSD.x", "H1", frame, _meta())
        cache.write("XAUUSD.x", "H1", frame, _meta(), lock=False)  # holder writes without re-locking
    assert cache.read_meta("XAUUSD.x", "H1").rows == 5
