import 'dart:async';
import 'dart:convert';
import 'dart:ffi' as ffi;
import 'dart:io';

import 'package:ffi/ffi.dart';
import 'package:flutter/foundation.dart' show kReleaseMode;
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
// Packaged (release) engine discovery and the launch spec
// ---------------------------------------------------------------------------

/// Where the frozen engine sits below the app folder (engine/packaging/README.md).
const List<String> _packagedEngineParts = ['engine', 'alpha_engine', 'alpha_engine.exe'];

/// The PyInstaller onedir engine of a packaged install:
/// `<app>\engine\alpha_engine\alpha_engine.exe`.
class PackagedEngine {
  const PackagedEngine({required this.appDir});

  /// Folder of `alpha_trader.exe` (the install root).
  final String appDir;

  String get executable => p.joinAll([appDir, ..._packagedEngineParts]);

  /// The exe's own folder (its `_internal\` runtime sits next to it).
  String get workingDirectory => p.dirname(executable);

  /// Engine data folder: `<app>\user_data` (`<app>\data` is Flutter's own assets).
  String get dataDir => p.join(appDir, 'user_data');

  /// Optional local env file, never shipped.
  String get envFile => p.join(appDir, '.env');
}

/// Returns the packaged engine next to the running app, or null.
///
/// [appDir] defaults to the folder of [Platform.resolvedExecutable].
PackagedEngine? findPackagedEngine({String? appDir, bool Function(String path)? fileExists}) {
  final bool Function(String) exists = fileExists ?? (String path) => File(path).existsSync();
  final String dir = p.normalize(p.absolute(appDir ?? File(Platform.resolvedExecutable).parent.path));
  final PackagedEngine engine = PackagedEngine(appDir: dir);
  return exists(engine.executable) ? engine : null;
}

/// Which engine [EngineProcess] starts.
sealed class EngineLocation {
  const EngineLocation();
}

/// The frozen `alpha_engine.exe` of a packaged install (release).
class PackagedEngineLocation extends EngineLocation {
  const PackagedEngineLocation(this.engine);

  final PackagedEngine engine;

  @override
  String toString() => 'packaged(${engine.executable})';
}

/// `engine\.venv\Scripts\python.exe -m alpha_engine` of a repository checkout (dev).
class DevEngineLocation extends EngineLocation {
  const DevEngineLocation(this.repoRoot);

  final String repoRoot;

  @override
  String toString() => 'dev(venv under $repoRoot)';
}

/// Chooses the engine to start.
///
/// * [preferPackaged] (release builds): the packaged exe next to the app
///   first; without one (a release build run from the repository) the venv
///   discovery below.
/// * otherwise (debug/profile): the venv discovery first ([findEngineRoot]:
///   `ENGINE_ROOT` / `ALPHA_ENGINE_ROOT` overrides, then a walk up), the
///   packaged exe only as the last resort.
///
/// An override that is set but invalid still throws (never a silent switch
/// to some other engine). Null = nothing found.
EngineLocation? resolveEngineLocation({
  required bool preferPackaged,
  String? appDir,
  String? dartDefineRoot,
  String? envRoot,
  List<Directory>? searchFrom,
  bool Function(String path)? fileExists,
}) {
  PackagedEngineLocation? packaged() {
    final PackagedEngine? e = findPackagedEngine(appDir: appDir, fileExists: fileExists);
    return e == null ? null : PackagedEngineLocation(e);
  }

  DevEngineLocation? dev() {
    final String? root = findEngineRoot(
      dartDefineRoot: dartDefineRoot,
      envRoot: envRoot,
      searchFrom: searchFrom,
      fileExists: fileExists,
    );
    return root == null ? null : DevEngineLocation(root);
  }

  return preferPackaged ? (packaged() ?? dev()) : (dev() ?? packaged());
}

/// Persian message when no engine was found in either place.
const String kEngineNotFoundFa = 'موتور برنامه پیدا نشد. نسخه نصبی باید فایل '
    'engine\\alpha_engine\\alpha_engine.exe را کنار alpha_trader.exe داشته باشد؛ '
    'در حالت توسعه محیط پایتون engine\\.venv\\Scripts\\python.exe لازم است '
    '(مسیر پروژه را با متغیر محیطی ALPHA_ENGINE_ROOT مشخص کنید).';

/// Everything [Process.start] needs to start the engine.
class EngineLaunchSpec {
  const EngineLaunchSpec({
    required this.location,
    required this.executable,
    required this.arguments,
    required this.workingDirectory,
    required this.environment,
  });

  final EngineLocation location;
  final String executable;
  final List<String> arguments;
  final String workingDirectory;

  /// Added to the inherited environment (explicit values win).
  final Map<String, String> environment;

  bool get isPackaged => location is PackagedEngineLocation;
}

/// Pure: the command line, folder and environment for [location] on [port].
///
/// Both modes always pass `DEV_MODE` explicitly (an inherited OS-level
/// value can't flip the engine) and `ENGINE_PORT`. Packaged: no arguments,
/// cwd = the exe folder, plus `ALPHA_TRADER_DATA_DIR=<app>\user_data` and
/// `ALPHA_TRADER_ENV_FILE=<app>\.env`. Dev: `python.exe -m alpha_engine`
/// in `<repo>\engine` (the engine's own defaults: `<repo>\data`, `<repo>\.env`).
EngineLaunchSpec buildEngineLaunchSpec({
  required EngineLocation location,
  required int port,
  required bool devMode,
}) {
  final Map<String, String> env = {
    'DEV_MODE': devMode ? 'true' : 'false',
    'ENGINE_PORT': '$port',
    // Child output is decoded as UTF-8; without these a venv Python would
    // write the console code page into the pipe (a frozen exe ignores them:
    // its entry.py forces UTF-8 itself).
    'PYTHONUNBUFFERED': '1',
    'PYTHONIOENCODING': 'utf-8',
  };
  return switch (location) {
    PackagedEngineLocation(:final PackagedEngine engine) => EngineLaunchSpec(
        location: location,
        executable: engine.executable,
        arguments: const [],
        workingDirectory: engine.workingDirectory,
        environment: {
          ...env,
          'ALPHA_TRADER_DATA_DIR': engine.dataDir,
          'ALPHA_TRADER_ENV_FILE': engine.envFile,
        },
      ),
    DevEngineLocation(:final String repoRoot) => EngineLaunchSpec(
        location: location,
        executable: p.joinAll([repoRoot, ..._venvPythonParts]),
        arguments: const ['-m', 'alpha_engine'],
        workingDirectory: p.join(repoRoot, 'engine'),
        environment: env,
      ),
  };
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
/// Which engine: [resolveEngineLocation] -- a release build prefers the
/// packaged `<app>\engine\alpha_engine\alpha_engine.exe`, a debug build the
/// repository venv; the command line comes from [buildEngineLaunchSpec].
///
/// Process tree note (dev): `engine\.venv\Scripts\python.exe` is the venv
/// *launcher*; it starts the base interpreter as its own child inside a
/// kill-on-close job, so terminating the launcher also ends the interpreter.
/// The pid in `/health` is therefore the interpreter's, not [Process.pid].
/// The packaged onedir exe has no such indirection: the bootloader process
/// runs the engine itself, so [Process.pid] == the `/health` pid.
class EngineProcess implements EngineLauncher {
  EngineProcess({
    EngineClientFactory? clientFactory,
    this.preferredPort = kDefaultEnginePort,
    this.fallbackPorts = kFallbackEnginePorts,
    this.preferPackaged = kReleaseMode,
  }) : _clientFactory = clientFactory ?? ((int port) => EngineClient(port: port));

  final EngineClientFactory _clientFactory;
  final int preferredPort;
  final List<int> fallbackPorts;

  /// Look for the packaged engine first (release builds).
  final bool preferPackaged;

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

    final EngineLocation? location = resolveEngineLocation(
      preferPackaged: preferPackaged,
      dartDefineRoot: const String.fromEnvironment('ENGINE_ROOT'),
      envRoot: Platform.environment['ALPHA_ENGINE_ROOT'],
    );
    if (location == null) {
      _log('[EngineProcess] no engine found (preferPackaged=$preferPackaged, '
          'app=${File(Platform.resolvedExecutable).parent.path})');
      throw const EngineLaunchException(kEngineNotFoundFa);
    }
    _log('[EngineProcess] launcher mode: ${location is PackagedEngineLocation ? 'packaged' : 'dev'} '
        '(preferPackaged=$preferPackaged) -> $location');

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

    final EngineLaunchSpec spec = buildEngineLaunchSpec(location: location, port: decision.port, devMode: kDevMode);
    // The environment holds no secrets (port, flags, folders), so it is logged whole.
    _log('[EngineProcess] spawning ${spec.executable} ${spec.arguments.join(' ')} '
        '(cwd=${spec.workingDirectory}, env=${spec.environment})');

    final Process process;
    try {
      process = await Process.start(
        spec.executable,
        spec.arguments,
        workingDirectory: spec.workingDirectory,
        environment: spec.environment,
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
