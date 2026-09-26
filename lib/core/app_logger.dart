import 'dart:async';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'dev_mode.dart';

/// Diagnostic logger.
///
/// [debugPrint] is invisible in a packaged Windows release build (a GUI app
/// has no attached console), so under `--dart-define=DEV_MODE=true` this
/// also appends to `Log/alpha-trader-debug.log` next to the executable.
///
/// Writes are BUFFERED and flushed on a timer. Engine messages (backtest
/// progress, live signals) can arrive many times a second; an
/// open/write/flush/close disk round trip per line on the UI isolate
/// progressively freezes the app. Never make this path synchronous per line.
///
/// Fully gated behind [kDevMode] per .claude/rules/01_logging_standard.md
/// — a normal release writes nothing.
class AppLogger {
  static File? _logFile;
  static bool _initTried = false;

  /// Safety cap: truncate rather than let an unattended machine fill its disk.
  static const int _maxLogBytes = 10 * 1024 * 1024;

  static final StringBuffer _pending = StringBuffer();
  static Timer? _flushTimer;

  /// Flush early if a burst fills the buffer before the timer fires.
  static const int _maxPendingChars = 64 * 1024;

  /// Absolute path of the diagnostic log, or null if it could not be opened.
  static String? get logFilePath => _logFile?.path;

  static void _ensureFile() {
    if (_initTried) return;
    _initTried = true;
    try {
      final String exeDir = File(Platform.resolvedExecutable).parent.path;
      final Directory logDir =
          Directory('$exeDir${Platform.pathSeparator}Log');
      if (!logDir.existsSync()) {
        logDir.createSync(recursive: true);
      }
      final File f =
          File('${logDir.path}${Platform.pathSeparator}alpha-trader-debug.log');
      if (f.existsSync() && f.lengthSync() > _maxLogBytes) {
        f.writeAsStringSync('');
      }
      _logFile = f;
      f.writeAsStringSync(
        '\n===== Alpha Trader diagnostic session started ${DateTime.now()} =====\n',
        mode: FileMode.append,
      );
      debugPrint('[AppLogger] Diagnostic log: ${f.path}');
    } catch (e) {
      _logFile = null;
      debugPrint('[AppLogger] Could not open diagnostic log file: $e');
    }
  }

  static String _stamp() {
    final DateTime n = DateTime.now();
    String two(int v) => v.toString().padLeft(2, '0');
    final String ms = n.millisecond.toString().padLeft(3, '0');
    return '${two(n.hour)}:${two(n.minute)}:${two(n.second)}.$ms';
  }

  static void log(String message) {
    if (!kDevMode) return;
    _ensureFile();

    if (_logFile == null) {
      // No file sink available; console is the only fallback.
      debugPrint(message);
      return;
    }

    // Deliberately NOT calling debugPrint here: it routes through
    // debugPrintThrottled (queue + timer + line wrapping) and is pure
    // overhead at high message rates in a build with no console.
    _pending.writeln('[${_stamp()}] $message');
    _flushTimer ??= Timer.periodic(const Duration(seconds: 1), (_) => flushNow());
    if (_pending.length >= _maxPendingChars) {
      flushNow();
    }
  }

  /// Writes everything buffered so far in a single append.
  static void flushNow() {
    if (_logFile == null || _pending.isEmpty) return;
    final String chunk = _pending.toString();
    _pending.clear();
    try {
      _logFile!.writeAsStringSync(chunk, mode: FileMode.append);
    } catch (_) {
      // Diagnostics must never break the app.
    }
  }

  /// Called once during clean app shutdown: flushes
  /// whatever is still buffered and stops the periodic timer so it can't
  /// fire again after the process has decided to exit.
  static void shutdown() {
    flushNow();
    _flushTimer?.cancel();
    _flushTimer = null;
  }
}
