# Alpha Trader engine (`alpha_engine`)

Local Python service (FastAPI + uvicorn) that the Flutter app launches and talks to over REST/WebSocket.
It reads market data from MetaTrader 5, runs backtests and emits position **suggestions**.

## Safety

- **Signal-only.** The engine never places, modifies or checks orders. Only read-only MT5 functions are
  allowed; `tests/test_no_order_calls.py` fails the build if an order/position API name appears anywhere
  under `engine/`.
- Listens on **127.0.0.1 only** (not configurable). `POST /shutdown` is accepted only from the local machine.
- Credentials live only in the gitignored `.env` (or the OS environment). They are never logged, and
  `Settings.safe_summary()` is the only view meant for display (password hidden, login masked `****1234`).

## Run

From this directory, with the project venv (after the user has approved the environment setup):

```
.venv\Scripts\python.exe -m alpha_engine
```

Then `GET http://127.0.0.1:8765/health`.

## Test

```
.venv\Scripts\python.exe -m pytest                      # fixture data only, no MT5 needed
.venv\Scripts\python.exe -m pytest --run-mt5 -m mt5     # only the live tests (attach, read-only)
```

Tests never read the real `.env`: `tests/conftest.py` points `ALPHA_TRADER_ENV_FILE` at a temporary fake file,
`ALPHA_TRADER_DATA_DIR` at a temp dir and sets `ENGINE_MT5_AUTOCONNECT=false`. Without `--run-mt5` the real
`MetaTrader5` package is replaced by a blocker that raises on any use; tests use `tests/fixtures/fake_mt5.py`
backed by the deterministic generator `tests/fixtures/synthetic_ohlcv.py`. The live test
(`tests/test_mt5_live.py`) attaches without credentials; `MT5_LIVE_TERMINAL_PATH` optionally names the terminal.

## Environment variables

Read from the OS environment first, then from `<repo>/.env` (OS environment wins). See `../.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `DEV_MODE` | `false` | `true` (case-insensitive) enables guarded debug logs |
| `ENGINE_PORT` | `8765` | API port, 1024-65535 |
| `MT5_TERMINAL_PATH` | - | `terminal64.exe` of the MT5 terminal to attach to |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | - | Only if the engine must log in itself (prefer the investor password) |
| `MT5_SERVER_UTC_OFFSET` | - | Fixed broker offset override in hours (-14..14, no DST). Unset = automatic (see below) |
| `ENGINE_MT5_AUTOCONNECT` | `true` | Connect to MT5 in the background on startup (`false`/`0`/`no`/`off` disables) |
| `ENGINE_SYMBOLS` | `XAUUSD.x,BRNUSD.x` | Symbols served by `/symbols` and `/rates` |
| `ALPHA_TRADER_DATA_DIR` | `<repo>/data` | Cache, database and results |
| `ALPHA_TRADER_ENV_FILE` | `<repo>/.env` | Alternative `.env` path |

## Market data (phase 1)

- **MT5 adapter** (`alpha_engine/mt5_adapter.py`): the only module that touches the terminal. Read-only
  allow-list enforced at runtime, every call serialized by one process-wide lock, `MetaTrader5` imported
  lazily. Attach mode by default (`initialize(path)`, no credentials); `login/password/server` only when
  `MT5_PASSWORD` is set. After connecting, `account_info()` must match `MT5_LOGIN`/`MT5_SERVER` when set,
  else state `account_mismatch`, disconnect, cache-only (decision D3).
- **Broker time -> UTC** (`alpha_engine/data/timezone.py`): models `fixed(h)`, `us_dst(b)`, `eu_dst(b)`. The
  model is fitted on XAUUSD.x H1 weekly closes/opens (Fri 17:00 / Sun 18:00 New York) and cross-checked with
  the live tick offset; `MT5_SERVER_UTC_OFFSET` overrides. Worked example, `us_dst(+2)`: server
  `2025-01-06 01:00` -> `2025-01-05T23:00Z`; server `2025-07-07 01:00` -> `2025-07-06T22:00Z`.
- **Cache** (`alpha_engine/data/cache.py`): `data/cache/ohlcv/<SYMBOL>_<TF>.parquet` (canonical UTC frame +
  metadata: offset model, first available bar, source `mt5`/`seed`, ...), `data/cache/symbols/<SYMBOL>.json`,
  `data/cache/offset_fit.json`. Atomic writes, one writer per series (lockfile with stale detection).
  Only closed bars are stored; nothing is ever forward-filled.
- **Gaps** (`alpha_engine/data/gaps.py`): `weekend`, `holiday`, `session_break` (learned recurring daily
  break), `missing`.

### Routes

| Route | Response |
|---|---|
| `GET /symbols` | `{mt5_state, symbols: [{symbol, source: mt5\|cache\|none, spec, fetched_at_utc, error}]}` |
| `GET /rates?symbol&timeframe&from&to` | `{symbol, timeframe, from, to, source: cache\|mt5, stale, offset_model, count, bars: [{time, server_time, open, high, low, close, tick_volume, spread, real_volume}], gaps: [{kind, after, before, start, missing_bars, duration_hours}], gap_counts, message}` |
| `GET /rates/meta?symbol&timeframe` | `{symbol, timeframe, cached, rows, first_bar_utc, last_bar_utc, first_available_utc, requested_start_utc, history_short, offset_model, source, fetched_at_utc}` |
| `GET /rates/gaps?symbol&timeframe` | `{symbol, timeframe, cached, rows, first_bar_utc, last_bar_utc, offset_model, count, gaps: [{kind, after, before, start, missing_bars, duration_hours}], gap_counts: {weekend, holiday, session_break, missing}, missing_bars_total, session_break_slots}` |
| `POST /rates/update` | body `{"symbol": "XAUUSD.x", "timeframe": "H1"}` (`timeframe` omitted -> H1 then H4) -> `{symbol, updated: [{timeframe, bars_added, rows, first_bar_utc, last_bar_utc, fetched}], message_fa}` |

Times are ISO-8601 UTC with `Z`; `from`/`to` without offset are taken as UTC; `to` defaults to now and
`from` to `to - 30 days`. `timeframe` is `H1` or `H4`. Unknown (not configured) symbol -> 404; bad
timeframe/date/symbol format or `from >= to` -> 422. Served from the cache; when MT5 is connected and the
cache does not reach `to`, an incremental update runs first (`source: "mt5"`). `stale: true` means MT5 was
not reachable and the cache may be missing recent bars.

- **`server_time`** (phase 3): the bar open on the broker's server clock in MT5 Data Window format
  (`"2025.03.10 03:00"`), converted from `time` with the offset model stored in the cache metadata
  (`OffsetModel.parse(meta.offset_model).utc_to_server`; never a new fit, never MT5). `null` when the
  cache has no parsable model. Example `us_dst(+2)`: `2025-01-05T23:00:00Z` -> `2025.01.06 01:00`,
  `2025-03-10T00:00:00Z` (US DST) -> `2025.03.10 03:00`. Current live broker: `fixed(0)` -> equal to UTC.
- **`GET /rates/gaps`** reads the whole cached series and runs `data.gaps.find_gaps` (cache only, never
  MT5; nothing is filled). No cache -> `cached: false` and empty lists. Example item:
  `{"kind": "session_break", "after": "2025-02-24T21:00:00Z", "before": "2025-02-24T23:00:00Z",
  "start": "2025-02-24T22:00:00Z", "missing_bars": 1, "duration_hours": 1.0}`.
- **`POST /rates/update`** runs the existing incremental `MarketDataService.update` (read-only
  `copy_rates_range` from the last cached bar minus 2 bars; bars are only added, the newer fetch wins
  on the overlap) with `start` = the series' stored `requested_start_utc`, so it never backfills.
  Refused with 409 (Persian `message_fa`, cache untouched) when an incremental update is impossible:
  `no_cache` (run `fetch_history` first), `not_incremental` (seed/test data), `offset_model_changed`
  (would force a full rewrite). MT5 not reachable -> 503 `mt5_unavailable`; MT5/offset/lock error during
  the update -> 503 `update_failed` (`errors_fa` lists what already finished). Errors of this route use
  `{"detail": {"code", "message_fa", "errors_fa"}}`. Example response:
  `{"symbol": "XAUUSD.x", "updated": [{"timeframe": "H1", "bars_added": 3, "rows": 31012,
  "first_bar_utc": "2021-09-27T00:00:00Z", "last_bar_utc": "2026-09-27T09:00:00Z", "fetched": 5}],
  "message_fa": "کش XAUUSD.x به‌روز شد — H1: 3 کندل جدید (...)."}`.

### Tools (read-only towards MT5)

```
.venv\Scripts\python.exe -m alpha_engine.tools.fetch_history [--years 5] [--symbols XAUUSD.x,BRNUSD.x] [--timeframes H1,H4] [--full]
.venv\Scripts\python.exe -m alpha_engine.tools.data_check_report --out <report.md> [--offline]
```

`fetch_history` attaches, fits the offset model, caches symbol specs and fetches/updates every series
(chunked `copy_rates_range`: 180 days for H1, 365 for H4). `data_check_report` writes a Persian report from
the cache: offset model/match ratio/live offset, symbol specs, and per series the 10 newest and 10 oldest
bars in both UTC and broker server time (compare with the MT5 Data Window), history depth vs the 5-year
target and gap counts.

## Strategies and account settings (phase 2)

- **Database:** SQLite `<data_dir>/alpha.db` (`ALPHA_TRADER_DATA_DIR`, default `<repo>/data/alpha.db`), stdlib
  `sqlite3`, WAL, migrations in `alpha_engine/storage/db.py`. Opened by the app lifespan into `app.state.db`
  and closed on shutdown; only the engine writes it. If it cannot be opened the error is logged, `/health`
  keeps working and the routes below answer 503 `db_unavailable`. Tests always use a temp data dir.
- **Registry:** `create_app` imports `alpha_engine.strategies`, which registers `stddev_channel` (code
  version 1). Strategies are Python code; only their parameters are stored.
- **Params versions:** the first read creates params version 1 from the schema defaults; saving a different
  (validated) set creates version n+1 and makes it active; saving the same set keeps the version. Provenance
  = strategy name + code version + params version + `params_hash` (sha256 of the canonical validated JSON).

| Route | Response |
|---|---|
| `GET /strategies` | `[{name, title_fa, version, params_version, params, params_hash, params_saved_utc, param_schema: [{name, type, default, min, max, choices, step, label_fa, description_fa}], params_errors_fa}]` |
| `GET /strategies/{name}` | one item as above; 404 `strategy_not_found` |
| `PUT /strategies/{name}` | body `{"params": {...}}`, FULL replacement (missing keys take defaults) -> item + `created_new_version`; 422 `invalid_body` / `invalid_params` (Persian `errors_fa`, includes cross-field rules such as "at least one confirmation pattern") |
| `GET /settings` | `{balance, risk_pct, leverage, rr}` (defaults `1000, 1.0, 100, 2.0` until the first update) |
| `PUT /settings` | partial update, e.g. `{"risk_pct": 0.5}` -> new settings; 422 `invalid_settings` with Persian `errors_fa`. Bounds: `0 < balance <= 1e9`, `0 < risk_pct <= 10`, `1 <= leverage <= 1000` (integer), `0.1 <= rr <= 20` |

Errors: `{"detail": {"code", "message_fa", "errors_fa": [...]}}`.

For callers inside the engine: `alpha_engine.strategy.context.build_context(symbol, h1, h4, *, db, ...)` builds
the `StrategyContext` from the active params and the stored account settings, keeping only H4 bars closed at
the decision time (and, with `now_utc`, only closed H1 bars); `alpha_engine.risk.size_from_spec(candidate or
(entry, sl), account, spec)` sizes a suggestion with a cached/live `SymbolSpec` (gold SL 5.00 -> 0.02 lots,
margin 40; brent SL 0.50 -> 0.02 lots, margin 16 with balance 1000, risk 1 %, leverage 100).

### Channel check tool (cache only, no MT5)

```
.venv\Scripts\python.exe -m alpha_engine.tools.channel_check_report --symbol XAUUSD.x --out <report.md> [--end <UTC ISO>] [--n 100] [--k 2] [--h1-bars 4] [--data-dir <dir>]
```

Persian report for comparing the engine's channel with MT5's *Standard Deviation Channel* object: the last
`n` closed H4 bars ending at `--end` (default: last cached H4 bar), first/middle/last bar times in UTC and
broker server time, the three lines at those bars for sigma `ddof=0` and `ddof=1`, slope, sigma, flatness
vs ATR_H4(14) (Wilder = engine, SMA = MT5's built-in ATR), drawing/reading instructions for MT5, and the H1
projection (`bar_count`, `calendar` for comparison) for the H1 bars decided with this channel. Exit codes:
0 written, 1 not enough cached data, 2 invalid arguments. It never writes the cache.

## Chart data (phase 3, read-only, cache only)

`alpha_engine/routes/chart.py`. Both routes read only the local cache (never MT5), use the ACTIVE params
version of `stddev_channel` (`strategy.context.load_active_params`) and reuse the strategy code -- no
channel/ATR/setup math is re-implemented. Everything is computed on the FULL cached history and then
filtered to `[from, to]` (Wilder ATR and the n-bar channel are path dependent), so a sub-range returns
exactly the same numbers as the full range. Results are kept in a bounded in-process LRU
(`app.state.chart_cache`, 8 entries) keyed by symbol, cache-file identity, params hash (and R:R for
setups); a cache write or a params change is a new key.

`from`/`to`: ISO UTC, optional. Defaults: `to` = last cached bar (setups: its close), `from` = `to - 30
days`; `from > to` -> 422 `invalid_range`. Unknown symbol -> 404 `symbol_not_configured`, bad symbol ->
422 `invalid_symbol`, bad timeframe -> 422 `invalid_timeframe`, stored params no longer valid -> 409
`stored_params_invalid`, DB not open -> 503 `db_unavailable`; all as
`{"detail": {"code", "message_fa", "errors_fa"}}`. Missing cache files or too little data -> 200 with
empty/invalid points and a Persian `message_fa`.

| Route | Response |
|---|---|
| `GET /chart/channel?symbol&timeframe=H1\|H4&from&to` (default `H1`) | `{symbol, strategy, strategy_version, params_version, params_hash, params: {n, k, sigma_ddof, projection_mode, ...all 15}, timeframe, from, to, count, valid_count, points: [{time, valid, mid, upper, lower, slope, sigma, is_flat, direction: up\|down\|flat\|none, atr_h1, atr_h4, h4_open, bars_ahead}], message_fa}` |
| `GET /chart/setups?symbol&from&to` | `{symbol, strategy, strategy_version, params_version, params_hash, params, from, to, account: {balance, risk_pct, leverage, rr}, symbol_spec, count, status_counts: {accepted, rejected, pending_entry}, setups: [...], note_fa, message_fa}` |

**Channel points.** `H1`: one point per cached H1 bar = the channel of the last H4 bar closed at that
bar's decision time (`h4_open`), projected `bars_ahead` H4 bars forward -- exactly the values the strategy
decides with (`compute_channel_bars`, the arrays behind `indicator_frame`). `H4`: one point per cached
(closed) H4 bar = the rolling channel ENDING at that bar, i.e. the regression line at the last bar of its
window -- what MT5's *Standard Deviation Channel* shows at its right end (`atr_h1`/`bars_ahead` null,
`h4_open` = `time`). Warm-up rows: `{"time": ..., "valid": false}` with every other field null. Relation:
`H1.mid = H4[h4_open].mid + H4[h4_open].slope * bars_ahead` (bit for bit; same for upper/lower). `slope`
is price per H4 bar; `is_flat = |slope| * n < atr_h4 * flat_mult`.

Worked example (synthetic fixture XAUUSD.x, defaults n = 100, k = 2, ddof = 0), H4 point
`2025-05-02T17:00:00Z`: `mid = 2076.7364376237624`, `sigma = 11.653738584711668`, `slope =
0.19758661866186655` -> `upper = 2076.73644 + 2 * 11.65374 = 2100.0439147931856`, `lower =
2053.428960454339`; change `|slope| * n = 19.7587 >= atr_h4 * 1.0 = 10.8578` -> not flat, `direction:
"up"`. The next H1 bars decided with it: `2025-05-02T20:00Z` (bars_ahead 0.75) `mid = 2076.73644 +
0.19759 * 0.75 = 2076.8846275877586`; `2025-05-04T22:00Z` (1.0) `2076.934024242424`.

**Setup items** (`scan()` over the full history, filtered by `decision_time`):

```
{id: "XAUUSD.x:20250428T1200Z:8e223612f12a",   # symbol : confirmation bar open : params_hash[:12]
 symbol, status: accepted|rejected|pending_entry, rejection_reason_fa,
 setup_type: "bounce_lower", setup_title_fa, direction: buy|sell, pattern: "pin_bar", line: lower|mid|upper,
 line_value, channel_direction,
 confirmation_bar_time: "2025-04-28T12:00:00Z",  # OPEN of the confirmation H1 bar t
 decision_time:         "2025-04-28T13:00:00Z",  # CLOSE of bar t (= confirmation_bar_time + 1h)
 entry_time:            "2025-04-28T13:00:00Z",  # OPEN of the next cached H1 bar t+1 (after a break or
                                                 #   weekend it is later than decision_time)
 entry: 2064.37, stop_loss: 2062.8835288730947, take_profit: 2067.34294225381, rr: 2.0,
 risk_distance: 1.486471126905144, reference_price, indicative_take_profit,
 volume: 0.06, actual_risk: 8.918826761430864, margin: 123.8622, risk_amount: 10.0,
 volume_note_fa, sizing_warnings_fa: [], reason_fa, indicators: {...SignalCandidate.extra}}
```

- `entry` = open of bar t+1 (`levels.entry_price_for`), `take_profit` = `entry +/- rr * |entry - SL|`
  (`levels.resolve_trade_levels`), volume/margin from `risk.size_from_spec` with the CURRENT `/settings`
  and the cached symbol spec. Prices are not rounded (format with `symbol_spec.digits`).
- `pending_entry`: bar t+1 is not cached yet -> `entry`, `entry_time`, `take_profit` and all sizing
  fields are null (the indicative `reference_price`/`indicative_take_profit` are labelled as such).
- `rejected`: bar t+1 opened beyond the stop (gap through the SL; `entry` shown, TP/volume null) or the
  sizing rejected the trade (below `volume_min`, or margin > balance) with `SizingResult.reason_fa`.
  Rejections are limited to these fill/sizing checks: setups the strategy drops itself (e.g. both
  directions at once) never become candidates (see `note_fa`).
- No cached symbol spec: `status` stays `accepted` (levels are valid), `volume`/`margin` null and
  `volume_note_fa` explains why.

Worked example (the item above; balance 1000, risk 1 %, leverage 100, rr 2; gold spec contract 100,
tick 1.0/0.01): SL = low 2063.81 - ATR_H1 4.632355634525692 * 0.2 = 2062.8835288730947; entry = open of
13:00 = 2064.37; distance 1.486471126905144; TP = 2064.37 + 2 * 1.486471 = 2067.34294225381; risk 10.00 /
(1.486471 * 100) = 0.0672734 lots -> floor to 0.01 step = 0.06; actual risk 0.06 * 148.6471 = 8.9188;
margin 0.06 * 100 * 2064.37 / 100 = 123.8622.
