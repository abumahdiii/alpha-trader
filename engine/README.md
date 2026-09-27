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
| `GET /rates?symbol&timeframe&from&to` | `{symbol, timeframe, from, to, source: cache\|mt5, stale, offset_model, count, bars: [{time, open, high, low, close, tick_volume, spread, real_volume}], gaps: [{kind, after, before, start, missing_bars, duration_hours}], gap_counts, message}` |
| `GET /rates/meta?symbol&timeframe` | `{symbol, timeframe, cached, rows, first_bar_utc, last_bar_utc, first_available_utc, requested_start_utc, history_short, offset_model, source, fetched_at_utc}` |

Times are ISO-8601 UTC with `Z`; `from`/`to` without offset are taken as UTC; `to` defaults to now and
`from` to `to - 30 days`. `timeframe` is `H1` or `H4`. Unknown (not configured) symbol -> 404; bad
timeframe/date/symbol format or `from >= to` -> 422. Served from the cache; when MT5 is connected and the
cache does not reach `to`, an incremental update runs first (`source: "mt5"`). `stale: true` means MT5 was
not reachable and the cache may be missing recent bars.

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
