import 'dart:async';
import 'dart:convert';
import 'dart:ffi' as ffi;
import 'dart:io';

import 'package:ffi/ffi.dart';
import 'package:path/path.dart' as p;
import 'package:win32/win32.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/engine_health.dart';
import 'engine_client.dart';

/// Default engine port (user decision); `--dart-define=ENGINE_PORT=...` overrides.
const int kDefaultEnginePort = int.fromEnvironment('ENGINE_PORT', defaultValue: 8765);

/// Tried in order when the preferred port is taken by something that is not
/// our engine.
const List<int> kFallbackEnginePorts = [8766, 8767, 8768, 8769, 8770, 8771, 8772, 8773, 8774, 8775];

/// Relative path of the venv interpreter below the repository root.
const List<String> _venvPythonParts = ['engine', '.venv', 'Scripts', 'python.exe'];

/// Failure to start/attach, carrying a Persian message for the UI.
class EngineLaunchException implements Exception {
  const EngineLaunchException(this.message, {this.cause});

  final String message;
  final Object? cause;

  @override
  String toString() => 'EngineLaunchException: $message${cause != null ? ' ($cause)' : ''}';
}

/// Outcome of [EngineLauncher.launch].
class EngineLaunchResult {
  const EngineLaunchResult({required this.port, required this.attached, this.exitCode});

  final int port;

  /// True when an engine was already serving [port] and we reuse it.
  final bool attached;

  /// Completes when the spawned child exits; null for an attached engine.
  final Future<int>? exitCode;
}

/// Seam between the status provider and the OS: the real implementation is
/// [EngineProcess]; tests inject a fake that never spawns anything.
abstract class EngineLauncher {
  Future<EngineLaunchResult> launch();

  /// Must finish in well under 4 s (window close allows 6 s in total).
  Future<void> stop();
}

// ---------------------------------------------------------------------------
// Repository-root discovery
// ---------------------------------------------------------------------------

/// Returns the repository root that contains `engine/.venv/Scripts/python.exe`.
///
/// Order: `--dart-define=ENGINE_ROOT`, then the `ALPHA_ENGINE_ROOT` OS
/// environment variable, then a walk up the parents of each [searchFrom]
/// directory (by default the CWD and the executable's folder — a debug exe
/// lives under `build/windows/x64/runner/Debug/`). An override may point at
/// the repo root or at its `engine/` folder. An override that is set but
/// invalid throws instead of silently falling back to some other engine.
String? findEngineRoot({
  String? dartDefineRoot,
  String? envRoot,
  List<Directory>? searchFrom,
  bool Function(String path)? fileExists,
}) {
  final bool Function(String) exists = fileExists ?? (String path) => File(path).existsSync();
  bool hasPython(String root) => exists(p.joinAll([root, ..._venvPythonParts]));

  for (final (String label, String? value) in [
    ('ENGINE_ROOT', dartDefineRoot),
    ('ALPHA_ENGINE_ROOT', envRoot),
  ]) {
    if (value == null || value.trim().isEmpty) continue;
    final String dir = p.normalize(p.absolute(value.trim()));
    if (hasPython(dir)) return dir;
    if (p.basename(dir).toLowerCase() == 'engine' && hasPython(p.dirname(dir))) {
      return p.dirname(dir);
    }
    throw EngineLaunchException(
      'مسیر تعیین‌شده در $label معتبر نیست؛ فایل engine\\.venv\\Scripts\\python.exe در «$dir» پیدا نشد.',
    );
  }

  final List<Directory> starts = searchFrom ??
      [Directory.current, File(Platform.resolvedExecutable).parent];
  for (final Directory start in starts) {
    String dir = p.normalize(p.absolute(start.path));
    while (true) {
      if (hasPython(dir)) return dir;
      final String parent = p.dirname(dir);
      if (parent == dir) break;
      dir = parent;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Port policy
// ---------------------------------------------------------------------------

class EnginePortDecision {
  const EnginePortDecision({required this.port, required this.attach, this.attachedPid});

  final int port;
  final bool attach;

  /// PID reported by the engine we attach to (for the kill fallback on stop).
  final int? attachedPid;
}

/// Port policy (user decision):
/// 1. [preferred] free -> spawn there.
/// 2. [preferred] busy and `/health` says `service == "alpha_engine"` ->
///    attach (an orphan of a previous run; single_instance_guard makes sure
///    it cannot belong to a live second copy of the app).
/// 3. Otherwise the first free port in [fallbacks].
/// 4. None free -> [EngineLaunchException].
Future<EnginePortDecision> chooseEnginePort({
  required int preferred,
  required List<int> fallbacks,
  required Future<bool> Function(int port) isPortFree,
  required Future<EngineHealth?> Function(int port) probeHealth,
}) async {
  if (await isPortFree(preferred)) {
    _log('[EngineProcess] port $preferred is free -> spawn');
    return EnginePortDecision(port: preferred, attach: false);
  }
  final EngineHealth? h = await probeHealth(preferred);
  if (h != null && h.isAlphaEngine) {
    _log('[EngineProcess] port $preferred already serves alpha_engine (pid=${h.pid}) -> attach');
    return EnginePortDecision(port: preferred, attach: true, attachedPid: h.pid);
  }
  _log('[EngineProcess] port $preferred busy with a foreign service -> trying fallbacks');
  for (final int port in fallbacks) {
    if (port == preferred) continue;
    if (await isPortFree(port)) {
      _log('[EngineProcess] fallback port $port is free -> spawn');
      return EnginePortDecision(port: port, attach: false);
    }
  }
  throw EngineLaunchException(
    'هیچ پورت آزادی برای موتور پیدا نشد: پورت $preferred و پورت‌های '
    '${fallbacks.first} تا ${fallbacks.last} همه توسط برنامه‌های دیگر اشغال شده‌اند.',
  );
}

/// Bind-and-release probe on loopback.
Future<bool> isLoopbackPortFree(int port) async {
  try {
    final ServerSocket s = await ServerSocket.bind(InternetAddress.loopbackIPv4, port);
    await s.close();
    return true;
  } on SocketException {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Real launcher
// ---------------------------------------------------------------------------

/// Spawns (or attaches to) the Python engine and stops it again.
///
/// Process tree note: `engine\.venv\Scripts\python.exe` is the venv
/// *launcher*; it starts the base interpreter as its own child inside a
/// kill-on-close job, so terminating the launcher also ends the interpreter.
/// The pid in `/health` is therefore the interpreter's, not [Process.pid].
class EngineProcess implements EngineLauncher {
  EngineProcess({
    EngineClientFactory? clientFactory,
    this.preferredPort = kDefaultEnginePort,
    this.fallbackPorts = kFallbackEnginePorts,
  }) : _clientFactory = clientFactory ?? ((int port) => EngineClient(port: port));

  final EngineClientFactory _clientFactory;
  final int preferredPort;
  final List<int> fallbackPorts;

  static const Duration _probeTimeout = Duration(seconds: 1);
  static const Duration _shutdownRequestTimeout = Duration(seconds: 1);
  static const Duration _gracefulExitWait = Duration(seconds: 2);
  static const Duration _postKillWait = Duration(milliseconds: 500);

  Process? _process;
  int? _port;
  bool _attached = false;
  int? _attachedPid;
  _KillOnCloseJob? _job;
  bool _cancelled = false;
  Future<void>? _stopping;

  int? get port => _port;
  bool get isAttached => _attached;

  @override
  Future<EngineLaunchResult> launch() async {
    if (!Platform.isWindows) {
      throw const EngineLaunchException('اجرای موتور فقط روی ویندوز پشتیبانی می‌شود.');
    }
    if (_process != null || _attached) {
      return EngineLaunchResult(port: _port!, attached: _attached, exitCode: _process?.exitCode);
    }
    _cancelled = false;

    final String? root = findEngineRoot(
      dartDefineRoot: const String.fromEnvironment('ENGINE_ROOT'),
      envRoot: Platform.environment['ALPHA_ENGINE_ROOT'],
    );
    if (root == null) {
      throw const EngineLaunchException(
        'محیط پایتون موتور پیدا نشد (engine\\.venv\\Scripts\\python.exe). '
        'مسیر پروژه را با متغیر محیطی ALPHA_ENGINE_ROOT مشخص کنید.',
      );
    }
    final String python = p.joinAll([root, ..._venvPythonParts]);
    final String engineDir = p.join(root, 'engine');
    _log('[EngineProcess] engine root: $root');

    final EnginePortDecision decision = await chooseEnginePort(
      preferred: preferredPort,
      fallbacks: fallbackPorts,
      isPortFree: isLoopbackPortFree,
      probeHealth: _probe,
    );
    _throwIfCancelled();

    if (decision.attach) {
      _port = decision.port;
      _attached = true;
      _attachedPid = decision.attachedPid;
      return EngineLaunchResult(port: decision.port, attached: true);
    }

    final Map<String, String> env = {
      // Always explicit so an inherited OS-level DEV_MODE can't flip the engine.
      'DEV_MODE': kDevMode ? 'true' : 'false',
      'ENGINE_PORT': '${decision.port}',
      'PYTHONUNBUFFERED': '1',
      // Child output is decoded as UTF-8 below; without this Python would
      // write the console code page into the pipe.
      'PYTHONIOENCODING': 'utf-8',
    };
    _log('[EngineProcess] spawning $python -m alpha_engine (cwd=$engineDir, '
        'DEV_MODE=${env['DEV_MODE']}, ENGINE_PORT=${env['ENGINE_PORT']})');

    final Process process;
    try {
      process = await Process.start(
        python,
        const ['-m', 'alpha_engine'],
        workingDirectory: engineDir,
        environment: env,
        includeParentEnvironment: true,
        runInShell: false,
      );
    } on ProcessException catch (e) {
      throw EngineLaunchException('اجرای پروسس موتور ناموفق بود: ${e.message}', cause: e);
    }
    _process = process;
    _port = decision.port;
    _job = _KillOnCloseJob.tryAssign(process.pid);

    // Always drain both pipes (a full pipe would block the child); only
    // the logging itself is DEV_MODE-gated.
    _pipe(process.stdout, 'stdout');
    _pipe(process.stderr, 'stderr');
    unawaited(process.exitCode.then((int code) {
      _log('[EngineProcess] child pid=${process.pid} exited with code $code');
    }));

    _log('[EngineProcess] spawned pid=${process.pid} on port ${decision.port} '
        '(job object: ${_job != null ? 'yes' : 'no'})');

    if (_cancelled) {
      // stop() raced with Process.start (and already returned, seeing no
      // port): don't leave an orphan or stale state behind.
      process.kill();
      _releaseJob();
      _process = null;
      _port = null;
      _throwIfCancelled();
    }
    return EngineLaunchResult(port: decision.port, attached: false, exitCode: process.exitCode);
  }

  @override
  Future<void> stop() => _stopping ??= _stop().whenComplete(() => _stopping = null);

  Future<void> _stop() async {
    _cancelled = true;
    final int? port = _port;
    final Process? process = _process;
    final bool attached = _attached;
    if (port == null) {
      _log('[EngineProcess] stop: nothing to stop');
      _releaseJob();
      return;
    }
    final Stopwatch sw = Stopwatch()..start();
    _log('[EngineProcess] stop: ${attached ? 'attached engine' : 'child pid=${process?.pid}'} on port $port');
    final EngineClient client = _clientFactory(port);
    try {
      try {
        await client.shutdown(timeout: _shutdownRequestTimeout);
        _log('[EngineProcess] stop: /shutdown accepted');
      } catch (e) {
        _log('[EngineProcess] stop: /shutdown failed: $e');
      }

      if (process != null) {
        final bool exited = await _waitForExit(process, _gracefulExitWait);
        if (!exited) {
          _log('[EngineProcess] stop: no exit after ${_gracefulExitWait.inSeconds} s -> kill');
          process.kill();
          await _waitForExit(process, _postKillWait);
        }
      } else if (attached) {
        await _stopAttached(client);
      }
    } finally {
      client.close();
      _releaseJob();
      _process = null;
      _port = null;
      _attached = false;
      _attachedPid = null;
      _log('[EngineProcess] stop: done in ${sw.elapsedMilliseconds} ms');
    }
  }

  /// An attached engine is not our child, so there is no exit code to wait
  /// on: poll /health until it stops answering, then kill by its reported
  /// pid as the last resort (window-close policy: no engine may survive).
  Future<void> _stopAttached(EngineClient client) async {
    final Stopwatch sw = Stopwatch()..start();
    int? pid = _attachedPid;
    while (sw.elapsed < _gracefulExitWait) {
      try {
        final EngineHealth h = await client.health(timeout: const Duration(milliseconds: 300));
        if (!h.isAlphaEngine) return;
        pid = h.pid ?? pid;
      } catch (_) {
        _log('[EngineProcess] stop: attached engine no longer answers');
        return;
      }
      await Future<void>.delayed(const Duration(milliseconds: 200));
    }
    if (pid != null) {
      _log('[EngineProcess] stop: attached engine still up -> killPid($pid)');
      Process.killPid(pid);
    }
  }

  Future<EngineHealth?> _probe(int port) async {
    final EngineClient client = _clientFactory(port);
    try {
      return await client.health(timeout: _probeTimeout);
    } catch (e) {
      _log('[EngineProcess] probe of busy port $port failed: $e');
      return null;
    } finally {
      client.close();
    }
  }

  void _throwIfCancelled() {
    if (_cancelled) throw const EngineLaunchException('راه‌اندازی موتور لغو شد.');
  }

  void _releaseJob() {
    // Closing the last job handle terminates anything still inside it.
    _job?.close();
    _job = null;
  }

  static Future<bool> _waitForExit(Process process, Duration limit) async {
    try {
      await process.exitCode.timeout(limit);
      return true;
    } on TimeoutException {
      return false;
    }
  }

  static void _pipe(Stream<List<int>> stream, String name) {
    stream
        .transform(const Utf8Decoder(allowMalformed: true))
        .transform(const LineSplitter())
        .listen(
          (String line) => _log('[engine:$name] $line'),
          onError: (Object e) => _log('[EngineProcess] $name pipe error: $e'),
        );
  }
}

// ---------------------------------------------------------------------------
// Job Object (kill the engine even if the app crashes)
// ---------------------------------------------------------------------------

/// package:win32 5.15 binds CreateJobObject / SetInformationJobObject /
/// AssignProcessToJobObject but not the limit structs or the
/// KILL_ON_JOB_CLOSE flag, so those are declared here.
const int _jobObjectExtendedLimitInformation = 9;
const int _jobObjectLimitKillOnJobClose = 0x00002000;

final class _JobObjectBasicLimitInformation extends ffi.Struct {
  @ffi.Int64()
  external int perProcessUserTimeLimit;
  @ffi.Int64()
  external int perJobUserTimeLimit;
  @ffi.Uint32()
  external int limitFlags;
  @ffi.UintPtr()
  external int minimumWorkingSetSize;
  @ffi.UintPtr()
  external int maximumWorkingSetSize;
  @ffi.Uint32()
  external int activeProcessLimit;
  @ffi.UintPtr()
  external int affinity;
  @ffi.Uint32()
  external int priorityClass;
  @ffi.Uint32()
  external int schedulingClass;
}

final class _IoCounters extends ffi.Struct {
  @ffi.Uint64()
  external int readOperationCount;
  @ffi.Uint64()
  external int writeOperationCount;
  @ffi.Uint64()
  external int otherOperationCount;
  @ffi.Uint64()
  external int readTransferCount;
  @ffi.Uint64()
  external int writeTransferCount;
  @ffi.Uint64()
  external int otherTransferCount;
}

final class _JobObjectExtendedLimitInformation extends ffi.Struct {
  external _JobObjectBasicLimitInformation basicLimitInformation;
  external _IoCounters ioInfo;
  @ffi.UintPtr()
  external int processMemoryLimit;
  @ffi.UintPtr()
  external int jobMemoryLimit;
  @ffi.UintPtr()
  external int peakProcessMemoryUsed;
  @ffi.UintPtr()
  external int peakJobMemoryUsed;
}

/// Holds a job handle with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The handle
/// is only closed by [close] or by Windows when this process dies — clean
/// exit or crash — and either way the engine goes with it.
class _KillOnCloseJob {
  _KillOnCloseJob._(this._handle);

  int _handle;

  static _KillOnCloseJob? tryAssign(int pid) {
    if (!Platform.isWindows) return null;
    int job = 0;
    final ffi.Pointer<_JobObjectExtendedLimitInformation> info =
        calloc<_JobObjectExtendedLimitInformation>();
    try {
      job = CreateJobObject(ffi.nullptr, ffi.nullptr);
      if (job == 0) {
        _log('[EngineProcess] CreateJobObject failed');
        return null;
      }
      info.ref.basicLimitInformation.limitFlags = _jobObjectLimitKillOnJobClose;
      if (SetInformationJobObject(job, _jobObjectExtendedLimitInformation, info,
              ffi.sizeOf<_JobObjectExtendedLimitInformation>()) ==
          0) {
        _log('[EngineProcess] SetInformationJobObject failed');
        CloseHandle(job);
        return null;
      }
      final int hProcess = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
      if (hProcess == 0) {
        _log('[EngineProcess] OpenProcess($pid) failed; no job object');
        CloseHandle(job);
        return null;
      }
      final int ok = AssignProcessToJobObject(job, hProcess);
      CloseHandle(hProcess);
      if (ok == 0) {
        _log('[EngineProcess] AssignProcessToJobObject failed; no job object');
        CloseHandle(job);
        return null;
      }
      return _KillOnCloseJob._(job);
    } catch (e) {
      // FFI trouble must never stop the engine from starting.
      _log('[EngineProcess] job object setup threw: $e');
      if (job != 0) CloseHandle(job);
      return null;
    } finally {
      calloc.free(info);
    }
  }

  void close() {
    if (_handle == 0) return;
    CloseHandle(_handle);
    _handle = 0;
  }
}

void _log(String message) {
  if (!kDevMode) return;
  devLog(message);
  AppLogger.log(message);
}
