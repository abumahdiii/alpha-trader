import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/engine_health.dart';
import '../services/engine_client.dart';
import '../services/engine_process.dart';

enum EngineState { stopped, starting, running, error }

/// Startup cap (user decision 2026-09-27). A cold first run after install or
/// a Windows restart took 16 s in the repro (antivirus scanning pandas /
/// pyarrow) and can take much longer.
const Duration kEngineStartupTimeout = Duration(seconds: 90);

/// Owns the engine lifecycle as seen by the UI:
///
///   stopped --start()--> starting --first healthy /health--> running
///   starting --launch failure / child exit--> error (final)
///   starting --no answer within [startupTimeout]--> error (watching)
///   running --N consecutive /health failures--> error (watching)
///   running --child exit--> error (final)
///   error (watching) --first healthy /health--> running  (auto-recovery)
///   error (watching) --child exit--> error (final: polling stops)
///   any --stop()--> stopped;  error --start()--> starting (after cleanup)
///   any --restart()--> stopped --> starting
///
/// "Watching" means the process may still come up (a slow cold start, a
/// busy engine), so /health keeps being polled every
/// [recoveryPollInterval]. It stops for good once the process has exited,
/// or when there was never a process/client (launch failure).
///
/// The launcher and client factory are injected so tests never spawn a
/// process or open a socket.
class EngineStatusProvider extends ChangeNotifier {
  EngineStatusProvider({
    required EngineLauncher launcher,
    EngineClientFactory? clientFactory,
    this.startupPollInterval = const Duration(seconds: 1),
    this.startupTimeout = kEngineStartupTimeout,
    this.runningPollInterval = const Duration(seconds: 5),
    this.recoveryPollInterval = const Duration(seconds: 3),
    this.failureThreshold = 3,
  })  : _launcher = launcher,
        _clientFactory =
            clientFactory ?? ((int port) => EngineClient(port: port));

  final EngineLauncher _launcher;
  final EngineClientFactory _clientFactory;

  final Duration startupPollInterval;
  final Duration startupTimeout;
  final Duration runningPollInterval;

  /// /health cadence while in [EngineState.error] with a live process.
  final Duration recoveryPollInterval;
  final int failureThreshold;

  EngineState _state = EngineState.stopped;
  EngineHealth? _health;
  String? _errorMessage;
  int? _port;
  bool _attached = false;

  EngineClient? _client;
  Timer? _startupDeadline;

  /// Periodic /health poll: the running poll in [EngineState.running], the
  /// recovery poll in [EngineState.error]. Never both.
  Timer? _pollTimer;
  bool _watchingForRecovery = false;
  bool _pollInFlight = false;
  int _consecutiveFailures = 0;
  int _recoveryPolls = 0;
  bool _processExited = false;
  bool _restartStopping = false;
  bool _disposed = false;

  /// Cancellable pause between startup polls (see [_sleep]).
  Timer? _sleepTimer;
  Completer<void>? _sleepDone;

  /// Bumped by every start()/stop()/dispose(); async work from an older
  /// session checks it and drops its result instead of clobbering state.
  int _session = 0;

  EngineState get state => _state;
  EngineHealth? get health => _health;

  /// Persian, user-facing; set only in [EngineState.error].
  String? get errorMessage => _errorMessage;
  int? get port => _port;

  /// True when we reused an engine that was already running on the port.
  bool get attached => _attached;

  /// True in [EngineState.error] while /health is still being polled, i.e.
  /// the state flips to [EngineState.running] by itself once the engine
  /// answers.
  bool get awaitingRecovery =>
      _state == EngineState.error && _watchingForRecovery;

  Mt5Status get mt5 => _state == EngineState.running
      ? (_health?.mt5 ?? Mt5Status.unknown)
      : Mt5Status.unknown;

  /// Completes once startup has settled (running / error / stopped).
  Future<void> start() async {
    if (_state == EngineState.starting || _state == EngineState.running) {
      _log('[EngineStatus] start() ignored: already ${_state.name}');
      return;
    }
    final bool recovering = _state == EngineState.error;
    final int session = ++_session;
    _cancelTimers();
    _consecutiveFailures = 0;
    _processExited = false;
    _health = null;
    _errorMessage = null;
    _setState(
      EngineState.starting,
      recovering ? 'restart after error' : 'start()',
    );

    if (recovering) {
      // A half-alive process from the failed attempt would hold the port.
      try {
        await _launcher.stop();
      } catch (e) {
        _log('[EngineStatus] cleanup before restart failed: $e');
      }
      if (session != _session) return;
    }

    final EngineLaunchResult result;
    try {
      result = await _launcher.launch();
    } on EngineLaunchException catch (e) {
      if (session == _session) _fail(e.message, cause: e);
      return;
    } catch (e) {
      if (session == _session) {
        _fail('راه‌اندازی موتور با خطای غیرمنتظره متوقف شد: $e', cause: e);
      }
      return;
    }
    if (session != _session) return;

    _port = result.port;
    _attached = result.attached;
    _client?.close();
    final EngineClient client = _clientFactory(result.port);
    _client = client;
    _log(
      '[EngineStatus] ${result.attached ? 'attached to' : 'launched'} engine '
      'on port ${result.port}; startup cap ${startupTimeout.inSeconds} s',
    );
    _notify();

    unawaited(result.exitCode?.then((int code) => _onChildExit(session, code)));

    _startupDeadline = Timer(startupTimeout, () {
      if (!_isCurrent(session, EngineState.starting)) return;
      _fail(
        'موتور در ${startupTimeout.inSeconds} ثانیه پاسخ نداد.',
        watch: (session: session, client: client),
      );
    });

    await _startupLoop(session, client);
  }

  Future<void> stop() async {
    final EngineState from = _state;
    ++_session;
    _cancelTimers();
    _log('[EngineStatus] stop() requested in state ${from.name}');
    try {
      await _launcher.stop();
    } catch (e, s) {
      _log('[EngineStatus] launcher.stop failed: $e\n$s');
    } finally {
      _client?.close();
      _client = null;
      _health = null;
      _errorMessage = null;
      _port = null;
      _attached = false;
      _consecutiveFailures = 0;
      _processExited = false;
      _setState(EngineState.stopped, 'stop()');
    }
  }

  /// "راه‌اندازی مجدد موتور": stop() then start(), through the same paths
  /// (so the launcher's port/attach policy is unchanged). Works from any
  /// state, including a start that is still in progress. A second call
  /// while the first one is still stopping is ignored.
  Future<void> restart() async {
    if (_restartStopping) {
      _log('[EngineStatus] restart() ignored: a restart is already stopping');
      return;
    }
    _log('[EngineStatus] restart() requested in state ${_state.name}');
    _restartStopping = true;
    try {
      await stop();
    } finally {
      _restartStopping = false;
    }
    if (_disposed) return;
    await start();
  }

  Future<void> _startupLoop(int session, EngineClient client) async {
    int attempt = 0;
    while (_isCurrent(session, EngineState.starting)) {
      attempt++;
      try {
        final EngineHealth h = await client.health();
        if (!_isCurrent(session, EngineState.starting)) return;
        if (h.isAlphaEngine) {
          _enterRunning(
            session,
            client,
            h,
            'healthy after $attempt poll(s): v${h.version} pid=${h.pid} '
            'mt5=${h.mt5.state.name}',
          );
          return;
        }
        _log(
          '[EngineStatus] startup poll #$attempt: unexpected service '
          '"${h.service}"',
        );
      } catch (e) {
        _log('[EngineStatus] startup poll #$attempt: not ready yet ($e)');
      }
      if (!_isCurrent(session, EngineState.starting)) return;
      await _sleep(startupPollInterval);
    }
  }

  void _enterRunning(
    int session,
    EngineClient client,
    EngineHealth h,
    String reason,
  ) {
    _cancelTimers();
    _health = h;
    _errorMessage = null;
    _consecutiveFailures = 0;
    _setState(EngineState.running, reason);
    _pollTimer = Timer.periodic(
      runningPollInterval,
      (_) => _pollRunning(session, client),
    );
  }

  Future<void> _pollRunning(int session, EngineClient client) async {
    if (_pollInFlight) return;
    _pollInFlight = true;
    try {
      final EngineHealth h = await client.health();
      if (!_isCurrent(session, EngineState.running)) return;
      if (_consecutiveFailures > 0) {
        _log(
          '[EngineStatus] health recovered after $_consecutiveFailures '
          'failure(s)',
        );
      }
      if (_health?.mt5.state != h.mt5.state) {
        _log(
          '[EngineStatus] MT5 ${_health?.mt5.state.name} -> '
          '${h.mt5.state.name}'
          '${h.mt5.message != null ? ' (${h.mt5.message})' : ''}',
        );
      }
      _consecutiveFailures = 0;
      _health = h;
      _notify();
    } catch (e) {
      if (!_isCurrent(session, EngineState.running)) return;
      _consecutiveFailures++;
      _log(
        '[EngineStatus] health poll failed '
        '($_consecutiveFailures/$failureThreshold): $e',
      );
      if (_consecutiveFailures >= failureThreshold) {
        _fail(
          'ارتباط با موتور قطع شد: $failureThreshold بار پیاپی به بررسی '
          'سلامت پاسخ نداد.',
          cause: e,
          watch: (session: session, client: client),
        );
      }
    } finally {
      _pollInFlight = false;
    }
  }

  /// Error-state poll: the first healthy answer brings the UI back to
  /// running without user action.
  Future<void> _pollRecovery(int session, EngineClient client) async {
    if (_pollInFlight) return;
    _pollInFlight = true;
    final int n = ++_recoveryPolls;
    try {
      final EngineHealth h = await client.health();
      if (!_isWatching(session)) return;
      if (!h.isAlphaEngine) {
        _log('[EngineStatus] recovery poll #$n: unexpected service '
            '"${h.service}"');
        return;
      }
      _log('[EngineStatus] recovery poll #$n: engine answered -> recovering');
      _enterRunning(
        session,
        client,
        h,
        'auto-recovered after $n recovery poll(s): v${h.version} '
        'pid=${h.pid} mt5=${h.mt5.state.name}',
      );
    } catch (e) {
      if (_isWatching(session)) {
        _log('[EngineStatus] recovery poll #$n: still no answer ($e)');
      }
    } finally {
      _pollInFlight = false;
    }
  }

  void _onChildExit(int session, int code) {
    if (session != _session) return; // expected exit after stop()
    _processExited = true;
    _log('[EngineStatus] engine process exited (code $code) -> polling stops');
    _fail('پروسس موتور به‌طور غیرمنتظره بسته شد (کد خروج $code).');
  }

  /// Enters [EngineState.error]. With [watch], and only while the process
  /// has not exited, /health keeps being polled for auto-recovery.
  void _fail(
    String message, {
    Object? cause,
    ({int session, EngineClient client})? watch,
  }) {
    _cancelTimers();
    _errorMessage = message;
    _health = null;
    _consecutiveFailures = 0;
    final bool keepWatching =
        watch != null && watch.session == _session && !_processExited;
    if (keepWatching) {
      _watchingForRecovery = true;
      _recoveryPolls = 0;
      _pollTimer = Timer.periodic(
        recoveryPollInterval,
        (_) => _pollRecovery(watch.session, watch.client),
      );
    }
    final String polling = keepWatching
        ? 'still polling /health every ${recoveryPollInterval.inMilliseconds} '
            'ms for auto-recovery'
        : 'polling stopped';
    _setState(
      EngineState.error,
      '$message${cause != null ? ' | cause: $cause' : ''} | $polling',
    );
  }

  bool _isCurrent(int session, EngineState expected) =>
      !_disposed && session == _session && _state == expected;

  /// Still in the watching error state of [session] (not after a child
  /// exit, which stops the recovery poll even if an answer is in flight).
  bool _isWatching(int session) =>
      _isCurrent(session, EngineState.error) && _watchingForRecovery;

  /// A pause that [_cancelTimers] can cut short, so stop()/dispose() never
  /// leave a pending timer or a startup loop hanging behind them.
  Future<void> _sleep(Duration duration) {
    final Completer<void> done = Completer<void>();
    _sleepDone = done;
    _sleepTimer = Timer(duration, () {
      if (!done.isCompleted) done.complete();
    });
    return done.future;
  }

  void _cancelTimers() {
    _startupDeadline?.cancel();
    _startupDeadline = null;
    _pollTimer?.cancel();
    _pollTimer = null;
    _watchingForRecovery = false;
    _sleepTimer?.cancel();
    _sleepTimer = null;
    final Completer<void>? sleeping = _sleepDone;
    _sleepDone = null;
    if (sleeping != null && !sleeping.isCompleted) sleeping.complete();
  }

  void _setState(EngineState next, String reason) {
    final EngineState prev = _state;
    _state = next;
    _log('[EngineStatus] ${prev.name} -> ${next.name}: $reason');
    _notify();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    ++_session;
    _cancelTimers();
    _client?.close();
    _client = null;
    super.dispose();
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog(message);
    AppLogger.log(message);
  }
}
