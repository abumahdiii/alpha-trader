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
.venv\Scripts\python.exe -m pytest            # fixture data only, no MT5 needed
.venv\Scripts\python.exe -m pytest --run-mt5  # also run tests marked `mt5` (live terminal)
```

Tests never read the real `.env`: `tests/conftest.py` points `ALPHA_TRADER_ENV_FILE` at a temporary fake file.

## Environment variables

Read from the OS environment first, then from `<repo>/.env` (OS environment wins). See `../.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `DEV_MODE` | `false` | `true` (case-insensitive) enables guarded debug logs |
| `ENGINE_PORT` | `8765` | API port, 1024-65535 |
| `MT5_TERMINAL_PATH` | - | `terminal64.exe` of the MT5 terminal to attach to |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | - | Only if the engine must log in itself (prefer the investor password) |
| `MT5_SERVER_UTC_OFFSET` | - | Broker server offset from UTC in hours (-14..14) |
| `ALPHA_TRADER_DATA_DIR` | `<repo>/data` | Cache, database and results |
| `ALPHA_TRADER_ENV_FILE` | `<repo>/.env` | Alternative `.env` path |
