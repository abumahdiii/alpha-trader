Fixture data and fakes for engine tests (no live MT5 terminal is ever needed):

- `synthetic_ohlcv.py` -- deterministic (seeded) synthetic OHLCV for XAUUSD.x / BRNUSD.x, H1 and H4, in UTC
  and as MT5-style raw server-time arrays, with planted weekends, holidays, daily session breaks, missing
  bars, a high-spread period and a price gap; `meta` lists every anomaly and the expected gap counts.
- `fake_mt5.py` -- a fake `MetaTrader5` module (read-only functions only) serving those raw arrays.
- `seed_cache.py` -- writes the synthetic data as a real-layout cache (`source=seed`); used by the local
  `tgc_testdata` skill wrapper.
