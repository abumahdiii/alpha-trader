import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/market_data.dart';
import '../models/signal_models.dart';
import '../services/backtest_progress.dart';
import '../services/engine_api.dart';
import 'engine_api_provider.dart';

/// Connection of [SignalsProvider] to `WS /ws/signals`.
enum SignalsConnection {
  /// The engine is not running (no API): nothing to connect to.
  offline,
  connecting,

  /// Snapshot received; live messages flow.
  connected,

  /// The socket dropped or never came up; a retry is scheduled.
  reconnecting,
}

/// App-level live signals state (registered above the shell in `main.dart`,
/// so it lives as long as the app, whichever page is shown).
///
/// Follows [EngineApiProvider]: whenever the engine is running it keeps
/// `WS /ws/signals` open, reconnecting with [backoff] after a drop, and when
/// no message (not even the 15 s keepalive) came for [staleAfter] it treats
/// the socket as dead. Messages:
/// * `snapshot` replaces the active signals and the status. Its signals are
///   marked seen WITHOUT being announced (a reconnect never re-alerts);
/// * `signal` adds/updates one; announced on [newSignals] only the first
///   time its id is seen;
/// * `expired` / `superseded` remove it from the active list;
/// * `status` replaces the status and re-reads the active list over REST
///   (the engine updates an expiry without a WS message: market reopen,
///   entry bar known);
/// * `keepalive` and unknown types are ignored (tolerant).
///
/// SUGGESTIONS ONLY: this class only reads.
class SignalsProvider extends ChangeNotifier {
  SignalsProvider({
    required EngineApiProvider apis,
    ProgressSocketConnector? connect,
    this.backoff = const [
      Duration(seconds: 1),
      Duration(seconds: 2),
      Duration(seconds: 5),
      Duration(seconds: 10),
      Duration(seconds: 30),
    ],
    this.connectTimeout = const Duration(seconds: 5),
    this.staleAfter = const Duration(seconds: 45),
    this.pendingExpiryPoll = const Duration(seconds: 60),
  })  : _apis = apis,
        _connect = connect ?? WebSocketProgressSocket.new {
    _apis.addListener(_syncApi);
    _syncApi();
  }

  final EngineApiProvider _apis;
  final ProgressSocketConnector _connect;

  /// Delay before retry n (the last one repeats).
  final List<Duration> backoff;
  final Duration connectTimeout;

  /// No message for this long = dead socket (the engine sends a keepalive
  /// after 15 s idle).
  final Duration staleAfter;

  /// Re-read of the active signals while one waits for the market to reopen.
  final Duration pendingExpiryPoll;

  final StreamController<LiveSignal> _newSignals = StreamController<LiveSignal>.broadcast();

  EngineApi? _api;
  ProgressSocket? _socket;
  StreamSubscription<Object?>? _socketSub;
  Timer? _retryTimer;
  Timer? _watchdog;
  Timer? _expiryPoll;
  int _attempt = 0;

  /// Bumped on every (re)connect; late callbacks of an older socket are ignored.
  int _generation = 0;
  bool _disposed = false;

  SignalsConnection _connection = SignalsConnection.offline;
  String? _connectionErrorFa;
  LiveSignalsStatus? _status;
  final Map<int, LiveSignal> _active = <int, LiveSignal>{};
  final Set<int> _seen = <int>{};
  final Map<String, int> _digits = <String, int>{};
  bool _refreshing = false;

  // ------------------------------------------------------------------ public state

  SignalsConnection get connection => _connection;

  /// Persian reason of the last connection failure (null once connected).
  String? get connectionErrorFa => _connectionErrorFa;

  /// Null until the first snapshot / status arrived.
  LiveSignalsStatus? get status => _status;

  /// The API of the running engine (null while it is not running).
  EngineApi? get api => _api;

  /// Active signals, newest decision first.
  List<LiveSignal> get active {
    final List<LiveSignal> list = _active.values.toList()
      ..sort((LiveSignal a, LiveSignal b) {
        final int t = (b.decisionTime ?? b.confirmationBarTime ?? _epoch)
            .compareTo(a.decisionTime ?? a.confirmationBarTime ?? _epoch);
        return t != 0 ? t : b.id.compareTo(a.id);
      });
    return List<LiveSignal>.unmodifiable(list);
  }

  int get activeCount => _active.length;

  /// Signals announced for the first time (WS `signal` only, deduplicated by
  /// id across reconnects and engine restarts). Never fed by a snapshot.
  Stream<LiveSignal> get newSignals => _newSignals.stream;

  /// Price digits of [symbol] from `GET /symbols` (null until known).
  int? digitsOf(String symbol) => _digits[symbol];

  /// The engine's symbols (status ticks / checks and `GET /symbols`), sorted.
  List<String> get knownSymbols =>
      List<String>.unmodifiable({...?_status?.symbols, ..._digits.keys, ..._active.values.map((s) => s.symbol)}.toList()
        ..sort());

  static final DateTime _epoch = DateTime.utc(1970);

  /// A status just returned by another call (e.g. `POST /signals/settings`):
  /// shown at once instead of waiting for the socket.
  void applyStatus(LiveSignalsStatus status) {
    if (_disposed) return;
    _log('status applied from REST: $status');
    _status = status;
    notifyListeners();
  }

  // ------------------------------------------------------------------ engine follow

  void _syncApi() {
    if (_disposed) return;
    final EngineApi? api = _apis.api;
    if (identical(api, _api)) return;
    _api = api;
    _attempt = 0;
    _closeSocket('engine API changed');
    _retryTimer?.cancel();
    if (api == null) {
      _log('engine not running: live signals offline');
      _expiryPoll?.cancel();
      _setConnection(SignalsConnection.offline);
      return;
    }
    unawaited(_loadDigits(api));
    _open();
  }

  Future<void> _loadDigits(EngineApi api) async {
    try {
      final SymbolsResponse r = await api.getSymbols();
      if (_disposed || !identical(api, _api)) return;
      for (final SymbolItem s in r.symbols) {
        final int? d = s.spec?.digits;
        if (d != null) _digits[s.symbol] = d;
      }
      _log('symbol digits: $_digits');
      notifyListeners();
    } on EngineApiException catch (e) {
      _log('symbol digits unavailable (prices shown unrounded): $e');
    }
  }

  // ------------------------------------------------------------------ socket

  void _open() {
    final EngineApi? api = _api;
    if (_disposed || api == null) return;
    final int gen = ++_generation;
    final Uri uri = api.signalsSocketUri();
    _log('connecting $uri (attempt ${_attempt + 1})');
    _setConnection(_attempt == 0 ? SignalsConnection.connecting : SignalsConnection.reconnecting);
    final ProgressSocket socket;
    try {
      socket = _connect(uri);
    } catch (e) {
      _dropped(gen, 'connect threw: $e');
      return;
    }
    _socket = socket;
    _socketSub = socket.messages.listen(
      (Object? frame) => _onFrame(gen, frame),
      onError: (Object e) => _dropped(gen, 'socket error: $e'),
      onDone: () => _dropped(gen, 'socket closed by the engine'),
      cancelOnError: true,
    );
    _armWatchdog(gen);
    unawaited(socket.ready.timeout(connectTimeout).then(
          (_) => _log('socket open (waiting for the snapshot)'),
          onError: (Object e) => _dropped(gen, 'socket not ready: $e'),
        ));
  }

  void _armWatchdog(int gen) {
    _watchdog?.cancel();
    _watchdog = Timer(staleAfter, () => _dropped(gen, 'no message for ${staleAfter.inSeconds} s'));
  }

  void _dropped(int gen, String why) {
    if (_disposed || gen != _generation || _api == null) return;
    _generation++; // late callbacks of this socket are ignored from now on
    final Duration delay = backoff.isEmpty ? const Duration(seconds: 5) : backoff[math.min(_attempt, backoff.length - 1)];
    _attempt++;
    _log('socket lost: $why -> retry #$_attempt in ${delay.inMilliseconds} ms');
    _closeSocket(why);
    _connectionErrorFa ??= 'ارتباط زنده با موتور قطع شد؛ دوباره وصل می‌شود…';
    _setConnection(SignalsConnection.reconnecting);
    _retryTimer?.cancel();
    _retryTimer = Timer(delay, _open);
  }

  void _closeSocket(String why) {
    _watchdog?.cancel();
    _watchdog = null;
    final StreamSubscription<Object?>? sub = _socketSub;
    final ProgressSocket? socket = _socket;
    _socketSub = null;
    _socket = null;
    if (socket == null) return;
    _log('closing socket ($why)');
    unawaited(sub?.cancel());
    unawaited(socket.close().catchError((Object e) => _log('socket close failed: $e')));
  }

  void _onFrame(int gen, Object? frame) {
    if (_disposed || gen != _generation) return;
    _armWatchdog(gen);
    final SignalsWsMessage? m;
    try {
      m = SignalsWsMessage.fromJson(frame is String ? jsonDecode(frame) : frame);
    } on FormatException catch (e) {
      _log('ignored unparsable WS frame: $e');
      return;
    }
    if (m == null) {
      _log('ignored WS frame that is not an object');
      return;
    }
    switch (m.type) {
      case SignalsWsType.snapshot:
        _onSnapshot(m);
      case SignalsWsType.signal:
        _onSignal(m);
      case SignalsWsType.expired || SignalsWsType.superseded:
        _onEnded(m);
      case SignalsWsType.status:
        final LiveSignalsStatus? s = m.status;
        if (s == null) {
          _log('WS <- status without a readable status: ignored');
          return;
        }
        _log('WS <- status $s');
        _status = s;
        notifyListeners();
        unawaited(refreshActive(reason: 'status message'));
      case SignalsWsType.error:
        _log('WS <- error: ${m.messageFa ?? '-'}');
        _connectionErrorFa = m.messageFa ?? 'موتور ارتباط زنده سیگنال را نپذیرفت.';
        notifyListeners();
      case SignalsWsType.keepalive:
        _log('WS <- keepalive');
      case SignalsWsType.unknown:
        _log('WS <- unknown type "${m.rawType}": ignored');
    }
  }

  void _onSnapshot(SignalsWsMessage m) {
    _attempt = 0;
    _connectionErrorFa = null;
    if (m.status != null) _status = m.status;
    _active
      ..clear()
      ..addEntries(m.active.where((LiveSignal s) => s.isActive).map((LiveSignal s) => MapEntry(s.id, s)));
    final int fresh = m.active.where((LiveSignal s) => !_seen.contains(s.id)).length;
    _seen.addAll(m.active.map((LiveSignal s) => s.id));
    _log('WS <- snapshot seq=${m.seq} active=${_active.length} ($fresh not seen before, not announced) '
        'status=${_status ?? '-'}');
    _setConnection(SignalsConnection.connected, force: true);
    _syncExpiryPoll();
  }

  void _onSignal(SignalsWsMessage m) {
    final LiveSignal? s = m.signal;
    if (s == null) {
      _log('WS <- signal without a readable item: ignored');
      return;
    }
    if (s.isActive) {
      _active[s.id] = s;
    } else {
      _active.remove(s.id);
    }
    final bool isNew = _seen.add(s.id);
    _log('WS <- signal $s${isNew ? ' (NEW -> announced)' : ' (already seen: updated only)'}');
    notifyListeners();
    _syncExpiryPoll();
    if (isNew && s.isActive) _newSignals.add(s);
  }

  void _onEnded(SignalsWsMessage m) {
    final LiveSignal? s = m.signal;
    if (s == null) {
      _log('WS <- ${m.rawType} without a readable item: ignored');
      return;
    }
    _seen.add(s.id);
    final bool removed = _active.remove(s.id) != null;
    _log('WS <- ${m.rawType} #${s.id} ${s.symbol}${removed ? '' : ' (was not in the active list)'}');
    notifyListeners();
    _syncExpiryPoll();
  }

  // ------------------------------------------------------------------ REST refresh

  /// Re-reads the active signals (`GET /signals?status=active`) and merges
  /// them: newer copies replace the shown ones; a shown signal missing from
  /// the answer is dropped only if it was already shown when the request
  /// left (a `signal` message arriving meanwhile is kept). Never announces.
  Future<void> refreshActive({String reason = 'manual'}) async {
    final EngineApi? api = _api;
    if (_disposed || api == null || _refreshing) return;
    _refreshing = true;
    final Set<int> before = _active.keys.toSet();
    try {
      final SignalsPage page =
          await api.listSignals(status: LiveSignalStatus.active, limit: EngineApi.maxSignalsPerPage);
      if (_disposed || !identical(api, _api)) return;
      final Set<int> got = {for (final LiveSignal s in page.signals) s.id};
      for (final LiveSignal s in page.signals) {
        if (s.isActive) _active[s.id] = s;
      }
      _seen.addAll(got);
      final bool complete = page.total <= page.signals.length;
      if (complete) _active.removeWhere((int id, _) => before.contains(id) && !got.contains(id));
      _log('active re-read ($reason): ${page.signals.length}/${page.total} -> ${_active.length} shown');
      notifyListeners();
      _syncExpiryPoll();
    } on EngineApiException catch (e) {
      _log('active re-read ($reason) failed: $e');
    } finally {
      _refreshing = false;
    }
  }

  /// Polls while an active signal waits for the market to reopen (its
  /// expiry is set by the engine without a WS message).
  void _syncExpiryPoll() {
    final bool needed = _api != null && _active.values.any((LiveSignal s) => s.expiryUnknown);
    if (needed && _expiryPoll == null) {
      _log('expiry poll on (${pendingExpiryPoll.inSeconds} s): a signal waits for the market to reopen');
      _expiryPoll = Timer.periodic(pendingExpiryPoll, (_) => unawaited(refreshActive(reason: 'pending expiry')));
    } else if (!needed && _expiryPoll != null) {
      _log('expiry poll off');
      _expiryPoll?.cancel();
      _expiryPoll = null;
    }
  }

  // ------------------------------------------------------------------ plumbing

  void _setConnection(SignalsConnection c, {bool force = false}) {
    if (_connection == c && !force) return;
    if (_connection != c) _log('connection ${_connection.name} -> ${c.name}');
    _connection = c;
    if (c == SignalsConnection.offline) _connectionErrorFa = null;
    notifyListeners();
  }

  @override
  void notifyListeners() {
    if (!_disposed) super.notifyListeners();
  }

  @override
  void dispose() {
    if (_disposed) return;
    _log('dispose');
    _apis.removeListener(_syncApi);
    _retryTimer?.cancel();
    _expiryPoll?.cancel();
    _closeSocket('dispose');
    _disposed = true;
    unawaited(_newSignals.close());
    super.dispose();
  }

  // Signals carry no secret (no login, no password), so ids, symbols and
  // prices may be logged under DEV_MODE.
  static void _log(String message) {
    if (!kDevMode) return;
    final String line = '[Signals] $message';
    devLog(line);
    AppLogger.log(line);
  }
}
