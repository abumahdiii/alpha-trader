import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/engine_health.dart';
import '../services/engine_client.dart';
import '../services/engine_process.dart';

enum EngineState { stopped, starting, running, error }

/// Owns the engine lifecycle as seen by the UI:
///
///   stopped --start()--> starting --first healthy /health--> running
///   starting --launch failure / child exit / no answer in 25 s--> error
///   running --N consecutive /health failures / child exit--> error
///   any --stop()--> stopped;  error --start()--> starting (after cleanup)
///
/// The launcher and client factory are injected so tests never spawn a
/// process or open a socket.
class EngineStatusProvider extends ChangeNotifier {
  EngineStatusProvider({
    required EngineLauncher launcher,
    EngineClientFactory? clientFactory,
    this.startupPollInterval = const Duration(seconds: 1),
    this.startupTimeout = const Duration(seconds: 25),
    this.runningPollInterval = const Duration(seconds: 5),
    this.failureThreshold = 3,
  })  : _launcher = launcher,
        _clientFactory = clientFactory ?? ((int port) => EngineClient(port: port));

  final EngineLauncher _launcher;
  final EngineClientFactory _clientFactory;

  final Duration startupPollInterval;
  final Duration startupTimeout;
  final Duration runningPollInterval;
  final int failureThreshold;

  EngineState _state = EngineState.stopped;
  EngineHealth? _health;
  String? _errorMessage;
  int? _port;
  bool _attached = false;

  EngineClient? _client;
  Timer? _startupDeadline;
  Timer? _pollTimer;
  bool _pollInFlight = false;
  int _consecutiveFailures = 0;
  bool _disposed = false;

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

  Mt5Status get mt5 => _state == EngineState.running ? (_health?.mt5 ?? Mt5Status.unknown) : Mt5Status.unknown;

  Future<void> start() async {
    if (_state == EngineState.starting || _state == EngineState.running) {
      _log('[EngineStatus] start() ignored: already ${_state.name}');
      return;
    }
    final bool recovering = _state == EngineState.error;
    final int session = ++_session;
    _cancelTimers();
    _consecutiveFailures = 0;
    _health = null;
    _errorMessage = null;
    _setState(EngineState.starting, recovering ? 'restart after error' : 'start()');

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
      if (session == _session) _fail(e.message, e);
      return;
    } catch (e) {
      if (session == _session) _fail('راه‌اندازی موتور با خطای غیرمنتظره متوقف شد: $e', e);
      return;
    }
    if (session != _session) return;

    _port = result.port;
    _attached = result.attached;
    _client?.close();
    final EngineClient client = _clientFactory(result.port);
    _client = client;
    _log('[EngineStatus] ${result.attached ? 'attached to' : 'launched'} engine on port ${result.port}');
    _notify();

    unawaited(result.exitCode?.then((int code) => _onChildExit(session, code)));

    _startupDeadline = Timer(startupTimeout, () {
      if (session == _session && _state == EngineState.starting) {
        _fail('موتور در ${startupTimeout.inSeconds} ثانیه پاسخ نداد.');
      }
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
      _setState(EngineState.stopped, 'stop()');
    }
  }

  Future<void> _startupLoop(int session, EngineClient client) async {
    while (_isCurrent(session, EngineState.starting)) {
      try {
        final EngineHealth h = await client.health();
        if (!_isCurrent(session, EngineState.starting)) return;
        if (h.isAlphaEngine) {
          _health = h;
          _startupDeadline?.cancel();
          _consecutiveFailures = 0;
          _setState(EngineState.running,
              'healthy: v${h.version} pid=${h.pid} mt5=${h.mt5.state.name}');
          _pollTimer = Timer.periodic(runningPollInterval, (_) => _pollRunning(session, client));
          return;
        }
        _log('[EngineStatus] startup poll: unexpected service "${h.service}"');
      } catch (e) {
        _log('[EngineStatus] startup poll: not ready yet ($e)');
      }
      if (!_isCurrent(session, EngineState.starting)) return;
      await Future<void>.delayed(startupPollInterval);
    }
  }

  Future<void> _pollRunning(int session, EngineClient client) async {
    if (_pollInFlight) return;
    _pollInFlight = true;
    try {
      final EngineHealth h = await client.health();
      if (!_isCurrent(session, EngineState.running)) return;
      if (_consecutiveFailures > 0) {
        _log('[EngineStatus] health recovered after $_consecutiveFailures failure(s)');
      }
      if (_health?.mt5.state != h.mt5.state) {
        _log('[EngineStatus] MT5 ${_health?.mt5.state.name} -> ${h.mt5.state.name}'
            '${h.mt5.message != null ? ' (${h.mt5.message})' : ''}');
      }
      _consecutiveFailures = 0;
      _health = h;
      _notify();
    } catch (e) {
      if (!_isCurrent(session, EngineState.running)) return;
      _consecutiveFailures++;
      _log('[EngineStatus] health poll failed ($_consecutiveFailures/$failureThreshold): $e');
      if (_consecutiveFailures >= failureThreshold) {
        _fail('ارتباط با موتور قطع شد: $failureThreshold بار پیاپی به بررسی سلامت پاسخ نداد.', e);
      }
    } finally {
      _pollInFlight = false;
    }
  }

  void _onChildExit(int session, int code) {
    if (session != _session) return; // expected exit after stop()
    _fail('پروسس موتور به‌طور غیرمنتظره بسته شد (کد خروج $code).');
  }

  void _fail(String message, [Object? cause]) {
    _cancelTimers();
    _errorMessage = message;
    _health = null;
    _setState(EngineState.error, '$message${cause != null ? ' | cause: $cause' : ''}');
  }

  bool _isCurrent(int session, EngineState expected) =>
      !_disposed && session == _session && _state == expected;

  void _cancelTimers() {
    _startupDeadline?.cancel();
    _startupDeadline = null;
    _pollTimer?.cancel();
    _pollTimer = null;
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
