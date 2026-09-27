"""The deterministic synthetic dataset (tests + tgc_testdata) itself, and the seed-cache writer."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpha_engine.data.cache import OhlcvCache
from alpha_engine.data.schema import Timeframe, to_epoch_seconds, validate_frame
from alpha_engine.data.timezone import OffsetModel
from fixtures.seed_cache import SeedRefused, seed_cache
from fixtures.synthetic_ohlcv import SYMBOLS, generate_dataset, generate_symbol, good_friday


def test_same_seed_identical_frames() -> None:
    a, b = generate_dataset(), generate_dataset()
    for key in a:
        pd.testing.assert_frame_equal(a[key].utc, b[key].utc)
        assert np.array_equal(a[key].raw, b[key].raw)
        assert a[key].meta == b[key].meta


def test_different_seed_differs() -> None:
    a = generate_symbol("XAUUSD.x", seed=1)[Timeframe.H1].utc
    b = generate_symbol("XAUUSD.x", seed=2)[Timeframe.H1].utc
    assert not a["close"].equals(b["close"])


def test_frames_are_canonical_and_in_range(synthetic) -> None:
    ranges = {"XAUUSD.x": (1900, 2100), "BRNUSD.x": (70, 90)}
    for (symbol, tf), series in synthetic.items():
        validate_frame(series.utc, tf)
        lo, hi = ranges[symbol]
        assert series.utc["low"].min() >= lo and series.utc["high"].max() <= hi
        assert (series.utc["close"].round(2) == series.utc["close"]).all()  # digits 2
        assert series.utc["time"].iloc[0] >= pd.Timestamp("2025-02-24", tz="UTC")
        assert series.utc["time"].iloc[-1] < pd.Timestamp("2025-05-05", tz="UTC")


def test_raw_is_server_time_under_the_model(synthetic) -> None:
    s = synthetic[("XAUUSD.x", Timeframe.H1)]
    assert s.raw.dtype.names == ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
    utc = to_epoch_seconds(s.utc["time"])
    offsets = (s.raw["time"] - utc) // 3600
    # us_dst(+2): +2 before the US DST start (2025-03-09 07:00Z), +3 after
    switch = int(pd.Timestamp("2025-03-09T07:00Z").timestamp())
    assert set(offsets[utc < switch]) == {2} and set(offsets[utc >= switch]) == {3}
    # gold session: server hour 00 (17:00 New York) never has an H1 bar
    assert not ((s.raw["time"] % 86400) // 3600 == 0).any()


def test_h4_aggregated_from_h1_in_server_buckets(synthetic) -> None:
    h1, h4 = synthetic[("XAUUSD.x", Timeframe.H1)], synthetic[("XAUUSD.x", Timeframe.H4)]
    assert (h4.raw["time"] % 14400 == 0).all()
    bucket = (h1.raw["time"] // 14400) * 14400
    first = h4.raw[10]
    members = h1.raw[bucket == first["time"]]
    assert first["open"] == members["open"][0] and first["close"] == members["close"][-1]
    assert first["high"] == members["high"].max() and first["low"] == members["low"].min()
    assert first["tick_volume"] == members["tick_volume"].sum()


def test_planted_anomalies_are_listed(synthetic) -> None:
    for symbol in SYMBOLS:
        meta = synthetic[(symbol, Timeframe.H1)].meta
        h1 = synthetic[(symbol, Timeframe.H1)].utc
        h4meta = synthetic[(symbol, Timeframe.H4)].meta
        assert meta["holidays"] == ["2025-04-18"]  # Good Friday
        kinds = [e["kind"] for e in meta["missing_events"]]
        assert kinds.count("single_h1") == 3 and kinds.count("h4_bucket") == 2
        assert len(meta["missing_bars"]) == 3 + 2 * 4 and len(h4meta["missing_bars"]) == 2
        times = set(h1["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        assert not times & set(meta["missing_bars"])
        hs = meta["high_spread"]
        start, end = pd.Timestamp(hs["start"]), pd.Timestamp(hs["end"])
        window = h1[(h1["time"] >= start) & (h1["time"] < end)]
        assert len(window) == 6
        assert h1["spread"].max() <= window["spread"].max()
        assert window["spread"].min() > h1.loc[~h1.index.isin(window.index), "spread"].max()
        gap = meta["price_gap"]
        assert abs(gap["pct"]) > 1.0
        t = pd.Timestamp(gap["time"])
        assert h1[h1["time"] == t].iloc[0]["open"] == gap["open"]
        assert h1[h1["time"] < t].iloc[-1]["close"] == gap["prev_close"]
        assert meta["expected_gaps"] == {"holiday": 1, "weekend": 9, "missing": 5, "session_break": 39}
        # the last weekend has no complete H4 bucket after it inside the span -> 8
        assert h4meta["expected_gaps"] == {"holiday": 1, "weekend": 8, "missing": 2, "session_break": 0}


def test_christmas_new_year_span() -> None:
    out = generate_symbol("XAUUSD.x", start="2024-12-02", end="2025-01-20")
    assert out[Timeframe.H1].meta["holidays"] == ["2024-12-25", "2025-01-01"]
    ny = out[Timeframe.H1].utc["time"].dt.tz_convert("America/New_York")
    # the whole session that ends 17:00 New York on Dec 25 is absent
    assert not ((ny.dt.date == pd.Timestamp("2024-12-25").date()) & (ny.dt.hour < 17)).any()


def test_good_friday_dates() -> None:
    assert str(good_friday(2025)) == "2025-04-18" and str(good_friday(2024)) == "2024-03-29"


def test_rejects_unknown_symbol_and_bad_span() -> None:
    with pytest.raises(ValueError):
        generate_symbol("EURUSD")
    with pytest.raises(ValueError):
        generate_symbol("XAUUSD.x", start="2025-03-01", end="2025-02-01")


def test_fixed_offset_variant_is_consistent() -> None:
    s = generate_symbol("XAUUSD.x", offset_model=OffsetModel.fixed(3))[Timeframe.H1]
    assert set((s.raw["time"] - to_epoch_seconds(s.utc["time"])) // 3600) == {3}
    assert s.meta["offset_model"] == "fixed(+3)"


# --- seed cache (tgc_testdata wrapper logic) ------------------------------------------------------

def test_seed_cache_writes_real_layout(tmp_path) -> None:
    fake_repo_data = tmp_path / "repo" / "data"
    out = tmp_path / "seeded"
    report = seed_cache(out, repo_data_dir=fake_repo_data)
    cache = OhlcvCache(out)
    for symbol in SYMBOLS:
        for tf in Timeframe:
            assert (out / "cache" / "ohlcv" / f"{symbol}_{tf.value}.parquet").is_file()
            frame, meta = cache.read(symbol, tf)
            assert meta.source == "seed" and meta.offset_model == "us_dst(+2)" and meta.extra["synthetic"]
            assert meta.rows == len(frame) == report.rows[f"{symbol}_{tf.value}"]
        spec, info = cache.read_spec(symbol)
        assert info["source"] == "seed" and spec.digits == 2
    assert not fake_repo_data.exists()


def test_seed_cache_refuses_repo_data_without_force(tmp_path) -> None:
    data = tmp_path / "data"
    with pytest.raises(SeedRefused):
        seed_cache(data, repo_data_dir=data)
    with pytest.raises(SeedRefused):
        seed_cache(data / "sub", repo_data_dir=data)
    assert not (data / "cache").exists()


def test_seed_cache_force_backs_up_first(tmp_path) -> None:
    data = tmp_path / "data"
    user_file = data / "cache" / "ohlcv" / "XAUUSD.x_H1.parquet"
    user_file.parent.mkdir(parents=True)
    user_file.write_bytes(b"user data")
    report = seed_cache(data, repo_data_dir=data, force=True)
    assert report.backup is not None
    assert (report.backup / "ohlcv" / "XAUUSD.x_H1.parquet").read_bytes() == b"user data"
