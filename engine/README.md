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

## Package (release)

Frozen with PyInstaller (onedir) for machines without Python: see `packaging/README.md` (build steps
need the user's permission; `requirements-build.txt` is build-only).

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
| `ALPHA_TRADER_DATA_DIR` | `<repo>/data` (frozen: `<app>/user_data`) | Cache, database and results |
| `ALPHA_TRADER_ENV_FILE` | `<repo>/.env` (frozen: `<app>/.env`) | Alternative `.env` path |
| `ALPHA_DATA_CHECK_CONFIRMED` | `false` | `true` once the user confirmed the raw-data check: backtests are no longer labelled «موقت تا تایید چک داده» (`provisional`) |

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
  version 1). Strategies are Python code; only their parameters are stored. Built-in names
  (`strategy.registry.RESERVED_NAMES` and every `source = "builtin"` class) can never be replaced, removed or
  taken by a plugin; `StrategyRegistry.replace(cls)` / `unregister(name)` exist for plugin versions.

### Strategy contract (S1: the engine is strategy-neutral)

The backtester, the chart and the params store only use these members of `strategy.base.Strategy`, so
`backtest/*` and `routes/backtests.py` never import a concrete strategy (`/chart/channel` is the only
StdDev-specific route):

| Member | Default | `stddev_channel` |
|---|---|---|
| `evaluate(ctx)` (abstract) | -- | one closed bar |
| `scan(h1, h4, params, account, *, symbol)` | `evaluate_checked` on every closed prefix (H1 `h1[:t+1]`, H4 closed at `open_t + 1h`); correct, O(n^2) | vectorised, proven equal to per-bar `evaluate` |
| `first_valid_index(h1, h4, params)` | `history_bars - 1` (class attribute, default 1) | first bar with channel + both Wilder ATRs defined |
| `warmup_margin_h4_bars(params)` | 0 | `10 * atr_period` (140) |
| `warmup_texts_fa` | neutral Persian wording | the channel/ATR wording of phase 4 |
| `identity` | `StrategyIdentity(name, version, code_sha256, source)` from the class attributes (`source` = `builtin` / `plugin`, `code_sha256` = plugin source file hash) | `("stddev_channel", 1, None, "builtin")` |

`SignalCandidate.setup` is any slug `^[a-z][a-z0-9_]{0,47}$`; the six `Setup` values keep the StdDev
line/direction rules (and `stddev_channel` may only emit them), `line` may be `null`, and `setup_title_fa`
(omitted while null) carries a non-StdDev title. `strategy.resolve.resolve_active(db, name=None, *, registry,
version)` returns an `ActiveStrategy` (instance, params record, clean params, identity, params hash);
`routes.resolve_strategy_or_error` maps unknown name / code version -> 404 `strategy_not_found`, stored params
that no longer validate -> 409 `stored_params_invalid`. `history.scan_full_history` rejects candidates of another
strategy / symbol / params hash or two on one bar (`StrategyContractError`). Every scan / first-valid / chart
LRU key contains `identity.cache_key = (name, version, sha256)`, so two strategies whose params hash alike never
share cached candidates.
- **Params versions:** the first read creates params version 1 from the schema defaults; saving a different
  (validated) set creates version n+1 and makes it active; saving the same set keeps the version. Provenance
  = strategy name + code version + params version + `params_hash` (sha256 of the canonical validated JSON).

| Route | Response |
|---|---|
| `GET /strategies` | `[{name, title_fa, version, params_version, params, params_hash, params_saved_utc, param_schema: [{name, type, default, min, max, choices, step, label_fa, description_fa}], params_errors_fa}]` |
| `GET /strategies/{name}` | one item as above; 404 `strategy_not_found` |
| `PUT /strategies/{name}` | body `{"params": {...}}`, FULL replacement (missing keys take defaults) -> item + `created_new_version`; 422 `invalid_body` / `invalid_params` (Persian `errors_fa`, includes cross-field rules such as "at least one confirmation pattern") |
| `GET /settings` | `{balance, risk_pct, leverage, rr}` (defaults `2500, 1.0, 100, 2.0` until the first update -- balance 2500 since 2026-09-27; 1 % of 2500 = 25.00 USD risked, rr 2 -> 50.00 USD target) |
| `PUT /settings` | partial update, e.g. `{"risk_pct": 0.5}` -> new settings; 422 `invalid_settings` with Persian `errors_fa`. Bounds: `0 < balance <= 1e9`, `0 < risk_pct <= 10`, `1 <= leverage <= 1000` (integer), `0.1 <= rr <= 20` |

Errors: `{"detail": {"code", "message_fa", "errors_fa": [...]}}`.

For callers inside the engine: `alpha_engine.strategy.context.build_context(symbol, h1, h4, *, db, ...)` builds
the `StrategyContext` from the active params and the stored account settings, keeping only H4 bars closed at
the decision time (and, with `now_utc`, only closed H1 bars); `alpha_engine.risk.size_from_spec(candidate or
(entry, sl), account, spec)` sizes a suggestion with a cached/live `SymbolSpec` (gold SL 5.00 -> 0.02 lots,
margin 40; brent SL 0.50 -> 0.02 lots, margin 16 with an explicit balance 1000, risk 1 %, leverage 100.
With the default balance 2500: risk 25.00 -> gold 25 / 500 = 0.05 lots, margin 0.05 * 100 * 2000 / 100 = 100;
brent 25 / (0.50 * 1000) = 0.05 lots, margin 0.05 * 1000 * 80 / 100 = 40).

### Strategy plugins (uploaded Python files, sandboxed)

A user (or an LLM agent) uploads ONE Python file defining one `Strategy` subclass. The engine never imports it:
it is checked statically, then run **only in an isolated worker process**, and usable in the chart and the
backtests (not live yet). Package `alpha_engine/plugins/`; routes `alpha_engine/routes/plugins.py`.

| Layer | Where | What |
|---|---|---|
| Static (parent) | `plugins/validator.py` | <= 256 KiB, UTF-8, `ast.parse`; import whitelist (numpy, pandas, math, statistics, dataclasses, typing; `from alpha_engine.{strategy,indicators,patterns,risk.sizing} import ...` only); no `exec/eval/compile/__import__/open/getattr/setattr/type/...`, no dunder names/attributes/strings, no `_private` attribute except on `self`/`cls`, no OS/process/clock/I-O names (`os`, `sys`, `read_*`, `to_pickle`, `now`, ...), no MT5 order-API names or MT5 package name; exactly one top-level `Strategy` subclass with literal `name` (not reserved) / `version` / `title_fa`, `param_schema`, `evaluate`; may not override `validate_params`/`clean_params` or set `source`/`code_sha256`. Owns the banned-name scanner shared with `tests/test_no_order_calls.py`. |
| Worker (child) | `plugins/worker.py` | `python -E -s -S -B -m alpha_engine --plugin-worker` (frozen: `<exe> --plugin-worker`), dispatched at the top of `__main__` before uvicorn/app/settings import; never calls `get_settings()`. Boot: parent's `sys.path` from the `go` line, MT5/pyarrow/numexpr/... set to `None` in `sys.modules`, numpy/pandas/helpers pre-imported and warmed up; then an irreversible lock: `sys.meta_path` finder + `sys.addaudithook` (denies sockets, subprocess/`os.system`/`os.exec*`/`os.spawn*`/`os.startfile`/`_winapi`, `ctypes`, `winreg`, `gc`/`sys.*` introspection, `__code__`/`__defaults__` rewrites, every file write, reads and listings outside the Python installation / site-packages / the engine package, and every non-whitelisted `import` event). The plugin runs in a fresh module with restricted builtins (no `open/exec/eval/getattr/...`, `print` is a no-op) and a whitelist `__import__`. JSON lines only (`describe`, `scan`, `evaluate`, `meta`, `shutdown`); frames as columns; candidates as `SignalCandidate.model_dump(mode="json")`. Never pickle. |
| Host (parent) | `plugins/host.py` | Minimal env (`SYSTEMROOT`, `PYTHONIOENCODING`, `DEV_MODE`), `CREATE_NO_WINDOW`, **Windows Job Object before `go`**: 1 GiB process + job memory, 1 active process, UI limits, kill-on-close (fail closed if it cannot be set). Per-request deadline -> job terminated; semaphore (2 workers); strict JSON (no NaN, 64 MiB line cap, request id). Every answer re-validated (`ParamSchema` rebuilt, `SignalCandidate.model_validate`, this name/version/symbol/params hash, one per bar, chronological, not before `first_valid_index`). `PluginStrategyProxy`: `source = "plugin"`, `code_sha256` = file SHA-256 -> provenance and cache keys. |
| Dynamic (parent) | `plugins/checks.py` | On `plugins/fixture.random_walk(3000, seed=20260928)`: describe matches the literal identity; >= 1 candidate; determinism (3 scans: same worker + a fresh worker); prefix equivalence (`evaluate` on the closed prefix of <= 40 candidate bars + 20 other bars + the last bar == scan); future mutation and truncation at 2 seeded cut-offs (candidates before the cut-off unchanged); 30 s plugin-time budget; peak memory reported. Floats compared at rel 1e-9. |

**Storage:** `<data_dir>/strategies/plugins/<name>/<version>-<sha12>.py` (+ `.json` manifest), table
`strategy_plugins` (DB migration **v3**, `UNIQUE(name, version)`, status `active|disabled|archived`, no FK). The
SHA-256 (of the UTF-8 text, BOM removed) is re-checked on every load; a modified file is never run. Strict
versions: same name+version+sha -> idempotent; different sha -> 409; old versions are kept; delete = archive
(files moved to `archive/`). The lifespan registers each name's latest **active** version (`registry.replace`) and
logs/skips broken files; shutdown unregisters them.

| Route | Response |
|---|---|
| `GET /plugins/template` | `{filename, content}` (two-MA crossover, contract + prohibitions + self-test in the docstring) |
| `POST /plugins` body `{filename, source}` | 201 new version / 200 same file (archived copy restored) -> `{name, version, sha256, title_fa, status, param_schema, validation: {static: [...], dynamic: {bars, candidates, prefix_checks, determinism, future_mutation, elapsed_s, budget_s, worker_boot_s, peak_memory_mib}}, filename, created_utc, registered}`; 409 `version_conflict`; 422 `invalid_plugin` (Persian `errors_fa`, `خط N:` prefixes) / `invalid_body` |
| `GET /plugins` | every stored version, newest first per name |
| `POST /plugins/{name}/{version}/disable` \| `/enable` | item; 404 `plugin_not_found`; 409 `plugin_archived` / `plugin_file_invalid` |
| `DELETE /plugins/{name}/{version}` | archive -> item; 409 `plugin_in_use` while a queued/running backtest uses it |

A registered plugin appears in `/strategies` (params store as usual) and is selected with `strategy=<name>` in
`/chart/setups`, `/backtests/limits` and `POST /backtests`; results record `strategy_source = "plugin"` and
`strategy_sha256` = the file hash. Self-test without the app: `python -m alpha_engine --plugin-check file.py`
(same checks, JSON report, exit 0 = accepted; reads no settings). Timings (template): validation ~5.5 s
(two worker boots ~1.4 s each); a 5-year (31 200 H1 bars) scan ~0.3 s of plugin time + boot.

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
version of the selected strategy (optional `strategy` query parameter, default `stddev_channel`;
`strategy.resolve.resolve_active`) and reuse the strategy code -- no channel/ATR/setup math is re-implemented.
`/chart/channel` exists for `stddev_channel` only: another strategy -> 409 `channel_not_available` (Persian).
Everything is computed on the FULL cached history and then
filtered to `[from, to]` (Wilder ATR and the n-bar channel are path dependent), so a sub-range returns
exactly the same numbers as the full range. Results are kept in a bounded in-process LRU
(`app.state.chart_cache`, 16 entries, shared with the backtest jobs) keyed by symbol, cache-file identity,
strategy identity (name, code version, source hash), params hash (and R:R for setups); a cache write or a
params change is a new key.

Strategy info in both responses: `strategy`, `strategy_version`, `strategy_source` (`builtin` | `plugin`) and,
for plugins only, `strategy_sha256`. Setup items of a non-StdDev strategy: `setup_type` = its slug, `line` and
`line_value` null, `setup_title_fa` from the candidate (else the slug), `id` =
`SYMBOL:YYYYMMDDTHHMMZ:<hash12>:<strategy>` (the `stddev_channel` ids keep the form without the suffix).

`from`/`to`: ISO UTC, optional. Defaults: `to` = last cached bar (setups: its close), `from` = `to - 30
days`; `from > to` -> 422 `invalid_range`. Unknown symbol -> 404 `symbol_not_configured`, bad symbol ->
422 `invalid_symbol`, bad timeframe -> 422 `invalid_timeframe`, stored params no longer valid -> 409
`stored_params_invalid`, DB not open -> 503 `db_unavailable`; all as
`{"detail": {"code", "message_fa", "errors_fa"}}`. Missing cache files or too little data -> 200 with
empty/invalid points and a Persian `message_fa`.

| Route | Response |
|---|---|
| `GET /chart/channel?symbol&timeframe=H1\|H4&from&to[&strategy]` (default `H1`) | `{symbol, strategy, strategy_version, strategy_source, params_version, params_hash, params: {n, k, sigma_ddof, projection_mode, ...all 15}, timeframe, from, to, count, valid_count, points: [{time, valid, mid, upper, lower, slope, sigma, is_flat, direction: up\|down\|flat\|none, atr_h1, atr_h4, h4_open, bars_ahead}], message_fa}` |
| `GET /chart/setups?symbol&from&to[&strategy]` | `{symbol, strategy, strategy_version, strategy_source, [strategy_sha256,] params_version, params_hash, params, from, to, account: {balance, risk_pct, leverage, rr}, symbol_spec, count, status_counts: {accepted, rejected, pending_entry}, setups: [...], note_fa, message_fa, evaluation: {...}, summary: {...}, backtest_window: {...}}` (phase 5: simulator rules, per-setup outcome and backtest flag) |

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

**Setup items** (the backtest's full-history scan -- `backtest.jobs.scan_for`, the SAME cached candidate objects
a backtest run uses -- filtered by `decision_time`). Status, entry, levels and volume follow the **backtest
simulator's rules** (phase 5; `backtest/simulator.py` helpers through `backtest/setup_outcomes.py`), so the table
and a backtest agree:

```
{id: "XAUUSD.x:20250428T1200Z:8e223612f12a",   # symbol : confirmation bar open : params_hash[:12]
 symbol, status: accepted|rejected|pending_entry, rejection_reason_fa,
 setup_type: "bounce_lower", setup_title_fa, direction: buy|sell, pattern: "pin_bar", line: lower|mid|upper,
 line_value, channel_direction,
 confirmation_bar_time: "2025-04-28T12:00:00Z",  # OPEN of the confirmation H1 bar t
 decision_time:         "2025-04-28T13:00:00Z",  # CLOSE of bar t (= confirmation_bar_time + 1h)
 entry_time:            "2025-04-28T13:00:00Z",  # OPEN of the next cached H1 bar t+1 (after a break or
                                                 #   weekend it is later than decision_time)
 entry: 2064.68,                    # CHANGED in phase 5: the simulator fill (buy = ask open, sell = bid open)
 entry_bid_open: 2064.37,           # raw (bid) open of bar t+1 = the phase-3 meaning of "entry"
 spread_at_entry_points: 31, entry_spread_source: historical|filled|fallback|zero,
 stop_loss: 2062.8835288730947, take_profit: 2068.27294225381, rr: 2.0, risk_distance: 1.7964711269050895,
 reference_price, indicative_take_profit,
 volume: 0.05, actual_risk: 8.982355634525447, margin: 103.234, risk_amount: 10.0,
 volume_note_fa, sizing_warnings_fa: [], reason_fa, indicators: {...SignalCandidate.extra},
 outcome: {result: tp|sl|end_of_data, exit_reason: sl|tp|sl_gap|tp_gap|end_of_period, exit_reason_fa,
           exit_bar_time: "2025-04-28T13:00:00Z", exit_time: "2025-04-28T14:00:00Z",
           exit_price: 2062.8835288730947, pnl_price: -1.7964711269050895, gross_pnl: -8.982355634525447,
           commission: 0.0, net_pnl: -8.982355634525447, r_multiple: -1.0, bars_held: 1,
           held_over_weekend: false, flags: []} | null,
 backtest: {traded: false, reason: "position_open", reason_fa: "در بک‌تست پوزیشن دیگری باز بود ...",
            trade_index: null, net_pnl: null} | null}
```

- **Entry / levels / volume (simulator rules):** bars are bid; `ask = bid + spread * point` (a zero spread is
  filled from the last previous non-zero spread, bars with no earlier broker spread use the auto fallback
  spread, see Backtests). A buy fills at the ask open of bar t+1, a sell at its bid open; `take_profit = entry
  +/- rr * |entry - SL|`; volume from `risk.sizing.size_position` on the FILL price with the CURRENT
  `/settings` (balance, risk %, leverage) and the cached symbol spec. Prices are not rounded (format with
  `symbol_spec.digits`).
- `pending_entry`: bar t+1 is not cached yet -> entry/levels/sizing fields and `outcome` null.
- `rejected` (Persian `rejection_reason_fa` = the backtest's skip reason): a `missing` gap between t and t+1
  (`entry` null, `entry_bid_open` shown), a gap through the stop (buy: BID open <= SL; sell: ASK open >= SL;
  `entry` = the would-be fill, TP/volume null), levels invalid at the fill, or the sizing rejected the trade
  (below `volume_min`, or margin > balance; TP shown, volume null). Setups the strategy drops itself never
  become candidates (see `note_fa`).
- **`outcome`** (accepted only): the setup simulated ON ITS OWN (no "one open trade" constraint, sized with the
  settings balance) from the entry bar on: SL before TP inside a bar, gap exits at the open, longs exit on the
  bid, shorts on the ask. `result`: `tp` (tp, tp_gap), `sl` (sl, sl_gap), or `end_of_data` -- still open on the
  last cached bar, closed at its close (long: bid close, short: ask close), `exit_reason: end_of_period`,
  `exit_reason_fa` «پایان داده ...»; provisional (changes when bars are appended). `pnl_price` = price move in
  the trade's favour, `net_pnl = gross_pnl - commission`, `r_multiple = net_pnl / actual_risk`.
- **`backtest`**: was this setup traded by the manual backtest of this range (`backtest_window`)? `traded` with
  the backtest trade's `trade_index` and `net_pnl` (the backtest's momentary balance), or not traded with
  `reason`/`reason_fa`: the backtest's skip reason (`position_open` -> «در بک‌تست پوزیشن دیگری باز بود ...»,
  `entry_outside_window`, `missing_gap`, `gap_through_stop`, `sizing_rejected`, ...), `outside_window`
  («خارج از بازه بک‌تست»: before a clipped window start), or `not_reached` (the backtest stopped, balance
  depleted). `null` when no window is available.

Top level (in addition to the fields above):

```
evaluation: {available: true, message_fa: null, basis: "independent_setups",
             label_fa: "ارزیابی مستقل هر ستاپ، بدون قید یک معامله باز؛ با نتیجه بک‌تست فرق دارد",
             end_of_data_label_fa, cost_model: {spread: "historical", fallback_spread_points: null,
             commission_per_lot_per_side: 0.0, swap: "none"},
             spread_fallback: {points: 30, source: "auto_median_observed", observed_from, observed_to, observed_bars,
                               observed_median, fallback_bars, zero_bars, total_bars},   # bars used by the outcomes
             labels_fa: ["موقت تا تایید چک داده", <spread label>, "کمیسیون صفر", "بدون سوآپ"]},
summary: {total: 22, accepted: 21, rejected: 1, pending_entry: 0, closed: 21, wins: 8, losses: 13, breakeven: 0,
          open_end_of_data: 0, win_rate: 0.38095238095238093, net_pnl: 23.700120156467165,
          gross_profit: 125.66829809407227, gross_loss: 101.9681779376051, profit_factor: 1.2324266318749895,
          profit_factor_infinite: false, total_r: 3.0, avg_r: 0.14285714285714285, r_count: 21, open_net_pnl: 0.0},
backtest_window: {from: "2025-04-22T09:00:00Z", to: "2025-05-05T00:00:00Z", clipped: true, note_fa,
                  available: true, code: null, message_fa: null, trades: 5, net_profit: -12.69148116917313}
```

- `summary`: counts over the listed setups; among the accepted, `closed` = TP/SL outcomes and
  `open_end_of_data`. `wins/losses/breakeven` (by `net_pnl` sign, eps 1e-9), `win_rate` (fraction of closed),
  `net_pnl`, `gross_profit`, `gross_loss`, `profit_factor` (null without losses; `profit_factor_infinite` when
  only wins), `avg_r` come from `backtest.metrics.compute_metrics` over the CLOSED outcomes; `total_r` = sum of
  their R; `open_net_pnl` = sum of the end-of-data P&L (never in wins/losses/PF/net_pnl). The outcomes overlap,
  so there is no drawdown / Sharpe / balance. Empty range -> zeros and nulls.
- `backtest_window`: what «بک‌تست همین بازه» submits to `POST /backtests` (mode manual, default costs): the bars
  whose decisions fall in `[from, to]` = `[ceil_hour(from) - 1h, floor_hour(to))`, clipped to the earliest
  allowed start (channel + ATR warm-up) and the end of the cache (`clipped`, Persian `note_fa`) and validated
  with the POST's own functions. `trades`/`net_profit` = that run's result (computed with `runner.run_backtest`,
  exactly as the job worker). A range entirely before the earliest start -> `available: false`, `code:
  "window_too_early"` and the Persian message of the POST error; every `backtest` flag is then null.
- **No cached symbol spec:** the phase-3 path is kept: `entry = entry_bid_open` (the spread price needs the
  point), levels from it, no volume (`volume_note_fa`), `outcome`/`backtest` null, `evaluation.available:
  false` with a Persian `message_fa`, `backtest_window.code: "spec_missing"`, `summary` = counts only. An
  unexpected evaluation error never fails the request (same degraded answer, `evaluation.message_fa` says the
  details are in the engine log).
- Outcomes, flags and the summary depend on every account field and the cost model and are computed per request
  (never cached; the shared history / scan / first-valid-bar LRU entries are reused). Latency on a 5-year
  synthetic gold cache (29,751 H1 bars, 1,072 setups): default 30 days ~55 ms warm / ~1.4 s cold; the full
  5 years ~1.1 s warm / ~2.4 s cold.
- Known limit (inherited from `data/gaps.py`, identical in the backtest): a daily session break in the first
  ~2 days of a new DST regime is recognised only with later data, so on a cache that ends right there the step
  is `missing` and such an entry is rejected; with more data it becomes a `session_break`.

Worked example (the item above; balance 1000, risk 1 % = 10.00, leverage 100, rr 2; gold spec contract 100,
tick 1.0/0.01 -> 100 USD per 1.00 per lot): SL = low 2063.81 - ATR_H1 4.632355634525692 * 0.2 =
2062.8835288730947; bar 13:00 bid open 2064.37, spread 31 points -> buy fills at the ask 2064.37 + 0.31 =
2064.68; distance 1.7964711269; TP = 2064.68 + 2 * 1.7964711 = 2068.27294225381; risk 10.00 / (1.7964711 * 100)
= 0.0556647 lots -> floor to the 0.01 step = 0.05; actual risk 0.05 * 179.64711 = 8.98236; margin 0.05 * 100 *
2064.68 / 100 = 103.234. Outcome: the entry bar's low reaches the SL -> `sl` at 2062.8835288730947, pnl_price
-1.79647, net -8.98236, R -1.0. The manual backtest of the clipped range had another trade open at that time
(`position_open`). (Phase 3 showed `entry` 2064.37 = the bid open; that value is now `entry_bid_open`.)

## Backtests (phase 4, cache only, never MT5)

Modules: `backtest/` (`simulator`, `runner`, `periods`, `costs`, `metrics`, `results`, `jobs`),
`storage/backtests_repo.py`, `routes/backtests.py`. Rules (see the module docstrings): decisions on closed
bars only, fill at the NEXT bar's open, bars are bid (buy fills at ask = bid + spread, sells exit on the ask),
SL before TP inside one bar, open trades closed at the end of the period, no swap (labelled), one open trade
at a time, every window starts flat with the account balance of `/settings`.

**Execution.** `POST /backtests` validates and stores the run `queued` and returns 202 at once; ONE background
worker thread (`app.state.backtest_jobs`) runs the runs in submit order. The strategy (body `strategy`, default
`stddev_channel`), its identity, its ACTIVE params version and `/settings` are snapshotted at POST time; the
worker checks the config against the strategy (`runner.check_config`: name, code version, source hash, params
hash) before scanning. Runs left `queued`/`running` by a previous engine process become
`interrupted` at startup; on shutdown the running run is cancelled (-> `interrupted`) before the DB closes.
Status: `queued -> running -> done | error | cancelled`, `queued -> cancelled`, `* -> interrupted`.

**Spread / costs.** Historical broker spread per bar; a zero spread is filled from the last previous non-zero
spread; bars with NO earlier broker spread (the broker's history only carries spread for its recent part) use
`fallback_spread_points`: omitted = auto = ceil(median of the non-zero spreads of the full cached H1 history),
`0` = explicit zero cost, `> 0` = user value. The resolved value is stored (`spread_fallback`) and the spread
label says honestly how much of the run used it. Commission per lot per side (default 0) on both sides.

**Metrics.** Always `backtest.metrics.compute_metrics` on the FULL per-bar equity of a window (Sharpe on UTC
daily closes, drawdown on the mark-to-market equity). Manual = one window: `metrics_kind: "single_window"`,
`metrics` = the full metrics dict. Random = N independent (possibly overlapping) windows:
`metrics_kind: "random_aggregate"`, `metrics = {window_count, windows_with_trades, initial_balance,
total_trades, pooled: {trade_count, win_count, loss_count, breakeven_count, win_rate, gross_profit, gross_loss,
profit_factor, profit_factor_infinite, expectancy, avg_win, avg_loss, largest_win, largest_loss, avg_r, r_count}
(all trades of all windows together), mean: {net_profit, net_profit_pct, max_drawdown_abs, max_drawdown_pct,
trade_count, win_rate, win_rate_windows, sharpe, sharpe_windows} (mean over windows), worst: {max_drawdown_abs,
max_drawdown_pct, net_profit_pct}, note_fa}` plus `distribution` (`summarize_windows`: `count,
windows_without_trades, mean/median_net_profit(_pct), pct_profitable, worst_window, best_window`). Windows are
never chained into one curve. `win_rate` is a fraction 0..1; `avg_loss`/`largest_loss` are negative; metric
times look like `2024-09-18T04:00:00+00:00`.

**Stored equity is downsampled** (display only; metrics are computed before): per window the first and last
point, the last point of every UTC day, the close of every trade's exit bar and the max-drawdown peak and
trough. So the stored curve reproduces the stored Sharpe and `max_drawdown_abs` exactly (5-year run: ~2k of
~30k points).

**Provisional.** Every run is `provisional: true` with the label «موقت تا تایید چک داده» until
`ALPHA_DATA_CHECK_CONFIRMED=true`.

### Routes

| Route | Result |
|---|---|
| `POST /backtests` | 202 `{id, status: "queued", provisional, labels_fa, seed, seed_generated}` (random: the seed the run uses; manual: both null) |
| `GET /backtests?offset=0&limit=50&symbol&mode&status` | `{count, total, offset, limit, runs: [summary]}`, newest first (below) |
| `GET /backtests/limits?symbol=XAUUSD.x[&strategy=]` | allowed manual period + request bounds for the ACTIVE params of the strategy (below) |
| `GET /backtests/{id}` | detail (below) |
| `GET /backtests/{id}/trades?window&offset=0&limit=500` (limit 1..5000) | `{run_id, window, total, offset, limit, trades: [trade]}` ordered by window, trade index |
| `GET /backtests/{id}/equity?window` | `{run_id, window, downsampled: true, rule, count, points: [{window_index, time, balance, equity}]}` |
| `GET /backtests/{id}/skipped?window` | `{run_id, window, count, skipped: [{window_index, time, confirmation_bar_time, direction, setup_type, reason, reason_fa, detail}]}` |
| `POST /backtests/{id}/cancel` | `{id, status: "cancelled" (was queued) \| "running" (stops at the next check), cancel_requested: true}`; 409 `not_cancellable` when finished |
| `DELETE /backtests/{id}` | `{id, deleted: true}`; 409 `run_active` while queued/running (cancel first) |
| `GET /backtests/{id}/export?format=xlsx\|csv&table&header=en\|fa` | the stored results as a file attachment (see "Export" below) |
| `WS /ws/backtests/{id}` | progress messages (plus keepalives while idle), then one final message, then close |

**List paging and filters.** `offset` 0..10^9 (default 0), `limit` 1..500 (default 50); optional exact filters
`symbol` (as stored, 1..32 characters), `mode` (`manual` | `random`), `status` (`queued` | `running` | `done` |
`error` | `cancelled` | `interrupted`); filters combine with AND. `count` = items in this page, `total` = runs
matching the filters (all pages), `offset`/`limit` = the effective values. An offset past the end gives
`runs: []` with the correct `total`. Invalid values -> 422 `invalid_query` (Persian `message_fa`). Filter values
are bound SQL parameters. Example: `GET /backtests?symbol=XAUUSD.x&mode=random&offset=20&limit=10` ->
`{"count": 10, "total": 37, "offset": 20, "limit": 10, "runs": [...]}` (runs 21..30 of 37, newest first).

**Period limits** (`GET /backtests/limits?symbol=`) are computed by the SAME code `POST /backtests` validates
with (`routes.backtests.period_limits`: `periods.earliest_start` on the cached history with the strategy's
`first_valid_index` and `warmup_margin_h4_bars` for its ACTIVE params, `periods.data_end`). A strategy without
warm-up (margin 0) may start at the close of the last H4 bar closed at its first valid decision. A manual run is accepted iff
`earliest_start <= from < to <= data_end` (and at least one H1 bar inside); `from = earliest_start - 1h` gets
422 `window_too_early`. The bounds change when the active params or the cache change (`params_hash` is
echoed so the UI can tell).

Worked example (synthetic fixture cache XAUUSD.x 2024-06-03 .. 2025-03-03, default params, n = 100,
atr_period = 14 -> warm-up 10 x 14 = 140 H4 bars):

```
{"symbol": "XAUUSD.x", "earliest_start": "2024-07-29T09:00:00Z", "data_start": "2024-06-03T00:00:00Z",
 "data_end": "2025-03-03T00:00:00Z", "warmup_h4_bars": 140, "window_months_max": 60, "windows_count_max": 500,
 "seed_max": 9223372036854775807,
 "default_fallback_spread_points": {"points": 30, "source": "auto_median_observed"},
 "params_version": 1, "params_hash": "8e223612...a32ec5", "note_fa": "بازه دستی نیم‌باز ..."}
```

`POST` manual `from=2024-07-29T09:00:00Z, to=2025-03-03T00:00:00Z` -> 202; `from=2024-07-29T08:00:00Z` ->
422 `window_too_early`; `to=2025-03-03T01:00:00Z` -> 422 `window_beyond_data`. `source` is
`auto_median_observed` or `none`.

`default_fallback_spread_points` = what an omitted `fallback_spread_points` resolves to
(`costs.resolve_fallback_points`: ceil(median non-zero spread of the cached H1 history); `none` = no spread
data, 0 points). Errors (standard format): 422 `invalid_query` (no symbol), `invalid_symbol`, `no_data`,
`spec_missing`, `data_too_short`; 404 `symbol_not_configured`, `strategy_not_found`; 409 `stored_params_invalid`;
503 `db_unavailable`, `cache_unreadable` (the same codes and statuses `POST /backtests` returns).

`POST /backtests` body (`null` = not given; fields of the other mode -> 422 `field_not_for_mode`). Optional in
both modes: `strategy` (registered name, default `stddev_channel`) and `strategy_version` (must equal the
registered code version); unknown -> 404 `strategy_not_found`. The stored config then carries
`strategy_source` and, for plugins, `strategy_sha256` (omitted while null, so older configs are unchanged):

```
{"symbol": "XAUUSD.x", "mode": "manual", "from": "2023-01-02T00:00:00Z", "to": "2024-01-01T00:00:00Z",
 "commission_per_lot_per_side": 0, "fallback_spread_points": null}
{"symbol": "XAUUSD.x", "mode": "random", "windows_count": 20, "window_months": 3, "seed": 42,
 "commission_per_lot_per_side": 3.5, "fallback_spread_points": 30}
```

Manual: `[from, to)` UTC (no offset = UTC), required. Random: `windows_count` 1..500 (default 20),
`window_months` 1..60 (default 3), `seed` 0..2^63-1 (omitted -> drawn by the engine AT SUBMIT TIME with
`secrets.randbits(63)`, stored in the run row and the config snapshot before the run is queued, returned in the
202 as `seed` with `seed_generated: true`; the stored `request` stays as sent, without a seed). Resubmitting
with that seed reproduces the run exactly (same windows, metrics and trades).
`commission_per_lot_per_side` 0..1000, `fallback_spread_points` 0..100000. The period is validated against the
cache at POST time with the runner's own functions.

Summary item: `{id, created_at, started_at, finished_at, status, progress (0..100), symbol, mode, from, to
(manual), windows_count, window_months, seed (random: the seed used, known from submit on; manual: null),
seed_generated (random: true when the engine drew it; manual: null), strategy, strategy_version, strategy_source
("builtin" | "plugin"; runs stored before S1: "builtin"), strategy_sha256 (plugins; else null), params_version, params_hash, provisional, trade_count, net_profit,
net_profit_pct, summary_basis: "single_window" | "window_mean" (random: mean window result), elapsed_s,
error: {code, message_fa} | null}`.

Detail = summary + `{labels_fa, provisional_label_fa, request (as validated), config (RunConfig: symbol, mode,
start, end, windows_count, window_months, seed (random: the seed used, incl. one drawn at submit), cost_model {spread, fallback_spread_points,
commission_per_lot_per_side, swap}, account {balance, risk_pct, leverage, rr}, strategy_name,
strategy_version, params, params_version, params_hash, provisional), plan (WindowPlan: mode, windows [{index,
start, end}], seed, seed_generated, algorithm "numpy.PCG64", numpy_version, windows_count, window_months,
earliest_start, data_end, eligible_starts, warmup_h4_bars), fingerprint (symbol, rows, first/last bars,
cache source mt5|seed, fetched_at, sha256 of H1/H4 content, spec), result_meta {cost_model, entry_rule
"next_bar_open", price_basis "bid_bars", spread_source, total_trades, total_skipped, metrics_kind,
spread_fallback}, spread_fallback {points, source auto_median_observed|user|none, observed_from, observed_to,
observed_bars, observed_median, fallback_bars, zero_bars, total_bars}, metrics_kind, metrics, distribution,
windows: [{index, start, end, initial_balance, final_balance, net_profit, net_profit_pct, trade_count,
skipped_count, candidates, bars, first_bar_time, last_bar_time, zero_spread_bars_filled,
zero_spread_bars_unfilled, weekend_holds, spread_fallback_bars, stopped_reason, equity_points_full,
equity_points_stored, metrics}], equity_storage_rule, timings {load_s, scan_s, simulate_s, metrics_s,
persist_s, total_s}, live (queued/running only: the WS progress snapshot)}`. `plan`, `fingerprint`,
`metrics`, `windows` are null/empty until `done`; the spread label is added when the run is done.

Trade (`trades[]`, all prices unrounded, times UTC): `{window_index, trade_index, direction, setup_type, line,
pattern, confirmation_bar_time, decision_time, entry_time (open of the entry bar), entry, entry_bid_open,
stop_loss, take_profit, rr, volume, risk_amount, balance_before, exit_bar_time (chart marker), exit_time,
exit_price, exit_reason sl|tp|sl_gap|tp_gap|end_of_period, exit_reason_fa, gross_pnl, commission, net_pnl,
r_multiple, balance_after, bars_held, spread_at_entry_points, spread_at_exit_points, held_over_weekend, flags,
sizing_warnings_fa, reason_fa, indicators}`.

WebSocket messages (`/ws/backtests/{id}`, polled every 200 ms, sent only on change):

```
{"type": "progress", "id": 3, "status": "queued|running", "phase": "queued|loading|scanning|simulating|saving",
 "percent": 42.5, "window": 3, "window_index": 2, "windows": 20, "cancel_requested": false}
{"type": "keepalive", "id": 3, "time_utc": "2026-09-28T10:15:00Z"}
{"type": "done", "id": 3, "status": "done", "percent": 100.0, "trade_count": 745, "net_profit": 12.3, "net_profit_pct": 1.23}
{"type": "error"|"cancelled"|"interrupted", "id": 3, "status": ..., "percent": 57.0, "code": "...", "message_fa": "..."}
{"type": "error", "id": 999, "status": null, "code": "backtest_not_found"|"db_unavailable", "message_fa": "..."}
```

`percent` is overall: loading 0-3, scanning 3-15, simulating 15-95 (the simulator's own progress), saving
95-100. A finished run sends its final message immediately. `window` is 1-based (i of `windows`).
**Keepalive:** while the run is `queued`/`running` and nothing was sent for `WS_KEEPALIVE_S` = 15 s
(`routes/backtests.py`), the socket sends `{"type": "keepalive", "id", "time_utc"}` (UTC, seconds); it carries
no progress, clients ignore it (or use it as a liveness signal). Never sent after the final message. Existing
message types are unchanged.

Errors (`{"detail": {"code", "message_fa", "errors_fa"}}`): 422 `invalid_request` (Persian `errors_fa`),
`field_not_for_mode`, `missing_period`, `invalid_range`, `window_too_early` (before channel + ATR warm-up),
`window_beyond_data`, `window_empty`, `data_too_short`, `no_data`, `spec_missing`, `invalid_symbol`,
`invalid_window`, `invalid_query`; 404 `symbol_not_configured`, `strategy_not_found`, `backtest_not_found`;
409 `stored_params_invalid`, `not_cancellable`, `run_active`, `run_not_finished` (export); 503 `db_unavailable`, `cache_unreadable`,
`engine_closing`. A failed run ends `error` with the same codes (`config_mismatch`, `internal_error`).

Storage (schema v2): `backtest_runs`, `backtest_windows`, `backtest_trades`, `backtest_equity` (children
`ON DELETE CASCADE`, no foreign key to the strategy tables); the result is written in one transaction, so a
cancelled or failed run stores no trades. Performance (31k-bar synthetic H1 cache, DEV_MODE off, through the
API): 5-year manual run ~2.5 s cold (scan 1.4 s, simulate 0.6 s, metrics 0.2 s, persist 0.2 s), 20 x 3-month
windows ~2.5-3 s cold, ~1.2 s with the history/scan already in the shared LRU.

### Export (phase 7: CSV and Excel)

`GET /backtests/{id}/export?format=xlsx|csv&table=...&header=en|fa` returns the STORED results of a run as a
file (`backtest/export.py`). Nothing is recomputed: every value is what `/backtests/{id}` (incl. its
`windows`), `/trades`, `/skipped`, `/equity` return. Presentation only: times ISO-8601 UTC with `Z` (metric times stored
as `+00:00` become `Z`), nested dicts flattened to dotted keys (`params.n`, `pooled.win_rate`), list cells
(`flags`, `sizing_warnings_fa`) as JSON text, empty cell = null.

| Query | Values | Default |
|---|---|---|
| `format` | `xlsx` (all tables, one sheet each) \| `csv` (one table) | `xlsx` |
| `table` | csv: `trades` \| `windows` \| `skipped` \| `equity` \| `run` \| `metrics`; xlsx: ignored (`all` or any of these) | `trades` |
| `header` | `en` = stable English snake_case keys \| `fa` = Persian labels | `en` |

Response: `200` with `Content-Type: text/csv; charset=utf-8` or
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
`Content-Disposition: attachment; filename="AlphaTrader_bt<id>_<symbol>_<mode>_<table|all>.<csv|xlsx>"`
(ASCII only: other characters become `_`), `Cache-Control: no-store`, `X-Alpha-Run-Status: <status>`.
Errors (standard Persian format): 404 `backtest_not_found`; 422 `invalid_query` (unknown `format`/`table`/
`header`, or `format=csv&table=all`); 409 `run_not_finished` while `queued`/`running`. Runs that ended
`error`/`cancelled`/`interrupted` store no results: they export the run table (first row `export_note_fa`
explains it, plus `status` / `error_code` / `error_message_fa`) and header-only tables.

Tables (column order is fixed; unknown extra stored keys are appended sorted, so nothing stored is dropped):

* `run` (sheet «مشخصات اجرا») -- rows `key,label_fa,value`: `id, status, status_fa, progress, error_code,
  error_message_fa, created_at, started_at, finished_at, elapsed_s, symbol, mode, from, to, windows_count,
  window_months, seed, seed_generated, strategy, strategy_version, strategy_source, strategy_sha256,
  params_version, params_hash, provisional, provisional_label_fa, trade_count, net_profit, net_profit_pct,
  summary_basis`, then `params.*`, `account.*`, `cost_model.*`, `spread_fallback.*`, `result_meta.*`,
  `labels_fa.<i>`, `plan.*` (without the window list), manual only `window.*` (bars, first/last bar, spread
  counts, stopped_reason, ...), `fingerprint.*` (incl. `fingerprint.spec.*`), `request.*`, `timings.*`,
  `equity_storage_rule`, `exported_at`, `export_format_version` (1).
* `metrics` («معیارها») -- rows `key,label_fa,value`: `metrics_kind`, the overall metrics (manual: the
  `compute_metrics` fields; random: `window_count, windows_with_trades, initial_balance, total_trades,
  pooled.*, mean.*, worst.*, note_fa`) and, random only, `distribution.*`.
* `trades` («معاملات») -- every `Trade` field in model order (`window_index ... reason_fa`, incl.
  `exit_reason`, `exit_reason_fa`, `reason_fa`), then `indicator.<name>` (sorted union over the run's trades).
* `windows` («پنجره‌ها», workbook: random runs only; csv: both modes) -- `window_index, start, end,
  initial_balance, final_balance, trade_count, skipped_count, candidates, bars, first_bar_time, last_bar_time,
  zero_spread_bars_filled, zero_spread_bars_unfilled, weekend_holds, spread_fallback_bars, stopped_reason,
  equity_points_full, equity_points_stored, metrics.*`.
* `skipped` («ردشده‌ها») -- `window_index, time, confirmation_bar_time, direction, setup_type, reason,
  reason_fa, detail`.
* `equity` («منحنی سرمایه») -- `window_index, time, balance, equity` (the stored, downsampled curve).

CSV: UTF-8 **with BOM** (Excel shows Persian), CRLF line endings, RFC 4180 quoting, floats as `repr()`
(`float(text) == stored` exactly), booleans `true`/`false`. A TEXT cell that starts with `= + - @`, TAB or CR
gets a leading `'` (spreadsheet formula injection; strategy texts can come from uploaded plugins); numbers are
never changed. xlsx: standard library only (`zipfile` + minimal SpreadsheetML, inline strings, no shared
strings), sheets right-to-left, bold frozen header row; numbers are numeric cells with the same `repr()`
text, booleans are boolean cells, integers beyond 2^53 (a 63-bit seed) are TEXT so Excel cannot round them;
XML-illegal control characters are written as `_xHHHH_` (Excel decodes them), literal `_xHHHH_` as
`_x005F_xHHHH_`. Performance (DEV_MODE off): 1000 trades -> xlsx ~180 ms / 185 KB, csv ~110 ms; 5000 trades ->
xlsx ~0.9 s. DEV_MODE logs `backtest export run <id> (...) format=... table=... rows={...} bytes=... load=...
total=... ms`.

Worked example (the fixture run of `tests/test_backtest_export.py`, manual run 1, `format=csv&table=trades`,
first data row; file
`AlphaTrader_bt1_XAUUSD.x_manual_trades.csv`, bytes `EF BB BF` first):

```
window_index,trade_index,direction,setup_type,line,pattern,confirmation_bar_time,decision_time,entry_time,entry,entry_bid_open,stop_loss,take_profit,rr,volume,risk_amount,balance_before,exit_bar_time,exit_time,exit_price,exit_reason,exit_reason_fa,gross_pnl,commission,net_pnl,r_multiple,balance_after,bars_held,spread_at_entry_points,spread_at_exit_points,held_over_weekend,flags,sizing_warnings_fa,reason_fa,indicator.atr,indicator.ok,indicator.slope,indicator.src
0,0,buy,touch_upper,upper,pin_bar,2024-09-02T00:00:00Z,2024-09-02T01:00:00Z,2024-09-02T01:00:00Z,2400.0,2399.66,2397.0,2406.0,2.0,0.01,8.333333333333334,2500.0,2024-09-02T03:00:00Z,2024-09-02T04:00:00Z,2397.0,sl,حد ضرر,-8.1,0.07,-8.17,0.3333333333333333,2500.000000001,3,34,,true,"[""weekend_gap""]",[],لمس خط بالای کانال,1.2345678901234567,true,-0.1,h4
```

`net_pnl` = `-8.17` is the stored value (= `gross_pnl - commission` = -8.1 - 0.07 as stored), not recomputed;
`spread_at_exit_points` is empty (null: buy exits are on the bid). `format=csv&table=run` starts
`key,label_fa,value` / `id,شناسه اجرا,1` / `status,وضعیت,done` / ... / `params_hash,هش پارامترها,8e22...`;
a random run with seed 9223372036854775807 shows it exactly in both formats.
