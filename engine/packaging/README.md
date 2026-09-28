# Packaging the engine (PyInstaller, onedir)

The release app must run on a machine without Python. The engine is frozen with PyInstaller into a
one-folder bundle that the Flutter release build launches instead of `engine\.venv\Scripts\python.exe`.

| File | Role |
|---|---|
| `alpha_engine.spec` | PyInstaller spec: onedir, `console=False`, `upx=False`, hidden imports, excludes |
| `entry.py` | Frozen entry: repairs missing std streams, forces UTF-8 pipes, `freeze_support()`, calls `alpha_engine.__main__.main()` |
| `../requirements-build.txt` | `pyinstaller` + hooks (build-only, never a runtime dependency) |

## Build (Rule 8: every command below needs the user's explicit permission)

From the repo root:

```bat
:: 1. one-time: install the build tooling into the engine venv
engine\.venv\Scripts\python.exe -m pip install -r engine\requirements-build.txt

:: 2. freeze the engine (a few minutes)
engine\.venv\Scripts\python.exe -m PyInstaller engine\packaging\alpha_engine.spec --noconfirm --clean --distpath build\engine_dist --workpath build\engine_work

:: 3. smoke test of the frozen exe (starts the engine; also needs permission)
set ENGINE_PORT=8799& set ALPHA_TRADER_DATA_DIR=%TEMP%\alpha_smoke& set ENGINE_MT5_AUTOCONNECT=false& build\engine_dist\alpha_engine\alpha_engine.exe
:: in a second shell: curl http://127.0.0.1:8799/health  then  curl -X POST http://127.0.0.1:8799/shutdown
```

`build/` is gitignored. The QA / release packages are built by the local skill scripts
(`tgc_testbuild`, `tgc_production_build`), which run step 2 into a temp work dir and assemble the zip.
They have `--dry-run`.

## Package layout

```
AlphaTrader\
  alpha_trader.exe, flutter_windows.dll, *.dll    Flutter release build
  data\                                           Flutter's own assets (flutter_assets, icudtl.dat, app.so)
  engine\alpha_engine\alpha_engine.exe            frozen engine
  engine\alpha_engine\_internal\                  Python runtime + packages
  user_data\                                      engine data dir (cache, alpha.db, results)
  BUILD_INFO.json                                 provenance (kind, version, commit, dev_mode)
  QA_Test_Plan\                                   QA packages only
```

The engine's data folder is `user_data\`, not `data\`, because Flutter's Windows bundle already owns
`<app>\data`. Frozen defaults (`alpha_engine/config.py`): install root = two levels above the exe folder,
data dir = `<app>\user_data`, env file = `<app>\.env` (optional, never shipped). `ALPHA_TRADER_DATA_DIR`
and `ALPHA_TRADER_ENV_FILE` override both. Dev (`python -m alpha_engine`) is unchanged: `<repo>\data`,
`<repo>\.env`.

## Runtime contract for the Flutter release launcher

- Executable: `<app>\engine\alpha_engine\alpha_engine.exe`, where `<app>` is the folder of
  `Platform.resolvedExecutable`. No arguments. Working directory: `<app>\engine\alpha_engine`.
- Environment (always explicit, as today): `ENGINE_PORT`, `DEV_MODE` (`true`/`false` from `kDevMode`),
  `ALPHA_TRADER_DATA_DIR=<app>\user_data`, `ALPHA_TRADER_ENV_FILE=<app>\.env`.
  `PYTHONUNBUFFERED` / `PYTHONIOENCODING` are harmless but ignored by a frozen interpreter;
  `entry.py` makes stdout/stderr UTF-8 and line-buffered itself.
- Pipes: keep draining stdout/stderr (unchanged). With `console=False` no console window appears; the
  pipes Flutter passes are still used, so DEV_MODE output keeps flowing into `[engine:stderr]`.
- Process tree: onedir runs the app in the bootloader process itself, so `Process.pid` == the pid in
  `/health` (no venv-launcher indirection). Job object, `/health` attach, `POST /shutdown` and the kill
  fallback are unchanged.
- First start is slower than dev (antivirus scans ~1-2 k files once); keep the existing 90 s startup cap.

## Size (estimate, to be confirmed on the first real build)

Python 3.12 runtime ~15 MB, numpy ~30 MB, pandas ~45 MB, pyarrow ~90-110 MB (largest; parquet),
pydantic-core ~5 MB, uvicorn/fastapi/websockets/httptools < 5 MB, MetaTrader5 < 2 MB:
roughly **200-250 MB unpacked**, ~80-100 MB zipped. Flutter release adds ~25-30 MB. Record the real
numbers in the phase 7 log after the first build.

## Antivirus notes

- **onedir, not onefile.** onefile self-extracts to `%TEMP%` on each start (the classic false-positive
  pattern, and slower) and runs a second process, which would break the `/health` pid contract.
- **No UPX** (`upx=False`): packed PE files are a frequent heuristic trigger.
- The bootloader is PyInstaller's prebuilt one; if Windows Defender still flags it, the options are:
  submit the file to Microsoft as a false positive, rebuild the bootloader from source, or code-sign
  `alpha_engine.exe` and `alpha_trader.exe` (future distribution step). Never disable antivirus.
- SmartScreen may warn on first run of an unsigned exe from a downloaded zip ("More info" -> "Run anyway");
  unblocking the zip (Properties -> Unblock) before extracting avoids per-file marks.

## Signal-only

Packaging does not change the safety model: the bundle contains the same code that
`tests/test_no_order_calls.py` scans, and both build scripts re-run that scan in their preflight.
No credentials are ever bundled; both scripts reject `.env`, `config.local.json` and credential patterns.

## Future: strategy-plugin template

The spec has a documented `datas` slot for the downloadable plugin template. A plugin worker flag, if
added, is parsed in `alpha_engine.__main__.main()` (argv passes through `entry.py` untouched);
`freeze_support()` is already called for a `multiprocessing`-based worker.
