import 'package:flutter/foundation.dart';

/// Global runtime debug-logging switch for the whole app.
///
/// OFF by default so release/customer builds never leak internal state
/// (engine payloads, strategy decisions, market data) into logs.
/// Enable it per `.claude/rules/01_logging_standard.md` with:
///   flutter run --dart-define=DEV_MODE=true
/// (or use the "Alpha Trader (DEV_MODE on)" launch config in .vscode/launch.json).
const bool kDevMode = bool.fromEnvironment('DEV_MODE', defaultValue: false);

/// Guarded debug log. Use this instead of a raw `if (kDevMode) print(...)`
/// at every call site so every DEV_MODE log line is consistently tagged
/// and easy to filter out of the console.
void devLog(String message) {
  if (kDevMode) {
    debugPrint('[DEV_MODE] $message');
  }
}
