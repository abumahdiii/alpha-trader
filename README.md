# Alpha Trader

Windows desktop app for defining trading systems, backtesting them on MetaTrader 5 data (manual or randomly sampled periods), and suggesting positions for live trading. **Signal-only — it never places orders.**

- `lib/` — Flutter UI (Persian, RTL, Gregorian dates)
- `engine/` — Python analysis engine (local FastAPI service; added in a later phase)
- `data/` — local market-data cache, strategies and results (not in git)

## Run

```bash
flutter pub get --offline   # dependencies come from the local pub cache
flutter run -d windows
flutter run -d windows --dart-define=DEV_MODE=true   # with diagnostic logging
```

## Test

```bash
flutter analyze
flutter test
```
