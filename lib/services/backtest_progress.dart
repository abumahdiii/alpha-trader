import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/status.dart' as ws_status;
import 'package:web_socket_channel/web_socket_channel.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/backtest_models.dart';
import 'engine_api.dart';

/// The minimal socket the watcher needs, so tests can fake it without a
/// network (see [WebSocketProgressSocket] for the real one).
abstract interface class ProgressSocket {
  /// Raw text frames from the engine.
  Stream<Object?> get messages;

  /// Completes once connected; errors when the connection fails.
  Future<void> get ready;

  Future<void> close();
}

/// Opens the progress socket of a run (injected in tests).
typedef ProgressSocketConnector = ProgressSocket Function(Uri uri);

/// [ProgressSocket] over `package:web_socket_channel`.
class WebSocketProgressSocket implements ProgressSocket {
  WebSocketProgressSocket(Uri uri) : _channel = WebSocketChannel.connect(uri);

  final WebSocketChannel _channel;

  @override
  Stream<Object?> get messages => _channel.stream;

  @override
  Future<void> get ready => _channel.ready;

  @override
  Future<void> close() => _channel.sink.close(ws_status.normalClosure);
}

/// Follows one backtest run until it ends: `WS /ws/backtests/{id}` first;
/// when the socket cannot connect or drops before the final message, it
/// falls back to polling `GET /backtests/{id}` every [pollInterval].
///
/// [events] emits [BacktestProgress] messages and closes after exactly one
/// final message (`done`/`error`/`cancelled`/`interrupted`, or `lost` when
/// even polling fails [maxPollFailures] times in a row). [close] stops
/// everything (socket closed with 1000, timer cancelled); it is safe to
/// call more than once.
class BacktestProgressWatcher {
  BacktestProgressWatcher({
    required this.api,
    required this.runId,
    ProgressSocketConnector? connect,
    this.pollInterval = const Duration(seconds: 1),
    this.connectTimeout = const Duration(seconds: 5),
    this.maxPollFailures = 5,
  }) : _connect = connect ?? WebSocketProgressSocket.new;

  final EngineApi api;
  final int runId;
  final Duration pollInterval;
  final Duration connectTimeout;
  final int maxPollFailures;
  final ProgressSocketConnector _connect;

  final StreamController<BacktestProgress> _out = StreamController<BacktestProgress>();
  ProgressSocket? _socket;
  StreamSubscription<Object?>? _socketSub;
  Timer? _pollTimer;
  bool _started = false;
  bool _finished = false;
  bool _polling = false;
  bool _pollInFlight = false;
  int _pollFailures = 0;
  BacktestProgress? _last;

  Stream<BacktestProgress> get events => _out.stream;

  /// True once the watcher gave up on the socket and polls instead.
  bool get isPolling => _polling;

  bool get isFinished => _finished;

  static const String lostMessageFa =
      'پیگیری پیشرفت این اجرا ممکن نشد (ارتباط با موتور قطع شد). فهرست اجراها را دوباره بارگذاری کنید.';

  void start() {
    if (_started) return;
    _started = true;
    final Uri uri = api.backtestProgressUri(runId);
    _log('connecting $uri');
    final ProgressSocket socket;
    try {
      socket = _connect(uri);
    } catch (e) {
      _fallback('connect threw: $e');
      return;
    }
    _socket = socket;
    _socketSub = socket.messages.listen(
      _onFrame,
      onError: (Object e) => _fallback('socket error: $e'),
      onDone: () => _fallback('socket closed before the final message'),
      cancelOnError: true,
    );
    unawaited(socket.ready.timeout(connectTimeout).then(
          (_) => _log('socket connected'),
          onError: (Object e) => _fallback('socket not ready: $e'),
        ));
  }

  void _onFrame(Object? frame) {
    if (_finished || _polling) return;
    final BacktestProgress message;
    try {
      message = BacktestProgress.fromJson(frame is String ? jsonDecode(frame) : frame);
    } on FormatException catch (e) {
      _log('ignored unparsable WS frame: $e | $frame');
      return;
    }
    if (!message.isProgress && !message.isFinal) {
      // keepalive (or a future informational type): not a state change.
      _log('WS <- ${message.type} ignored');
      return;
    }
    _log('WS <- $message');
    _emit(message);
  }

  void _fallback(String why) {
    if (_finished || _polling) return;
    _polling = true;
    _log('falling back to polling GET /backtests/$runId: $why');
    unawaited(_closeSocket());
    unawaited(_poll());
    _pollTimer = Timer.periodic(pollInterval, (_) => unawaited(_poll()));
  }

  Future<void> _poll() async {
    if (_finished || _pollInFlight) return;
    _pollInFlight = true;
    try {
      final BacktestRunDetail detail = await api.getBacktest(runId);
      _pollFailures = 0;
      if (_finished) return;
      final BacktestProgress message = BacktestProgress.fromDetail(detail);
      _log('poll <- $message');
      _emit(message);
    } on EngineApiException catch (e) {
      if (_finished) return;
      if (e.statusCode == 404) {
        _emit(BacktestProgress(type: 'error', id: runId, code: e.code, messageFa: e.messageFa));
        return;
      }
      _pollFailures++;
      _log('poll failed ($_pollFailures/$maxPollFailures): $e');
      if (_pollFailures >= maxPollFailures) {
        _emit(BacktestProgress(type: BacktestProgress.lostType, id: runId, messageFa: lostMessageFa));
      }
    } finally {
      _pollInFlight = false;
    }
  }

  void _emit(BacktestProgress message) {
    if (_finished) return;
    if (!message.isFinal && message == _last) return; // polling: unchanged
    _last = message;
    _out.add(message);
    if (message.isFinal) {
      _log('final ${message.type} -> closing');
      unawaited(close());
    }
  }

  Future<void> _closeSocket() async {
    final StreamSubscription<Object?>? sub = _socketSub;
    final ProgressSocket? socket = _socket;
    _socketSub = null;
    _socket = null;
    await sub?.cancel();
    try {
      await socket?.close();
    } catch (e) {
      _log('socket close failed: $e');
    }
  }

  Future<void> close() async {
    if (_finished) return;
    _finished = true;
    _pollTimer?.cancel();
    _pollTimer = null;
    // Not awaited: the done event waits for a listener, and there may be none.
    unawaited(_out.close());
    await _closeSocket();
    _log('closed');
  }

  void _log(String message) {
    if (!kDevMode) return;
    final String line = '[BacktestProgress #$runId] $message';
    devLog(line);
    AppLogger.log(line);
  }
}
