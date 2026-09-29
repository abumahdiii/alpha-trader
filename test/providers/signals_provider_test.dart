import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/signal_models.dart';
import 'package:alpha_trader/providers/engine_api_provider.dart';
import 'package:alpha_trader/providers/engine_status_provider.dart';
import 'package:alpha_trader/providers/signals_provider.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';
import '../helpers/signal_fakes.dart';

/// Engine + API + provider with a recording socket connector. testWidgets
/// for the fake clock (health polls, backoff, watchdog).
class _Rig {
  _Rig(this.http) {
    engine = EngineStatusProvider(launcher: NoProcessLauncher(), clientFactory: HealthyClient.new);
    apis = EngineApiProvider(engine: engine, apiFactory: (int port) => EngineApi(port: port, httpClientAdapter: http));
    provider = SignalsProvider(
      apis: apis,
      connect: sockets.call,
      backoff: const [Duration(milliseconds: 100), Duration(milliseconds: 400)],
      staleAfter: const Duration(seconds: 5),
      pendingExpiryPoll: const Duration(seconds: 2),
    );
    _sub = provider.newSignals.listen(announced.add);
  }

  final FakeEngineHttp http;
  final FakeSocketConnector sockets = FakeSocketConnector();
  late final EngineStatusProvider engine;
  late final EngineApiProvider apis;
  late final SignalsProvider provider;
  final List<LiveSignal> announced = [];
  late final StreamSubscription<LiveSignal> _sub;

  Future<void> start(WidgetTester tester) async {
    unawaited(engine.start());
    await tester.pump();
    await tester.pump();
    expect(engine.state, EngineState.running);
    // Lets the provider's GET /symbols (fake transport) finish.
    await tester.pump(const Duration(milliseconds: 20));
  }

  Future<void> send(WidgetTester tester, Map<String, Object?> message) async {
    sockets.last.send(message);
    await tester.pump();
    await tester.pump();
  }

  Future<void> dispose(WidgetTester tester) async {
    // Not awaited: a broadcast cancel completes in the root zone, and
    // awaiting it would leave testWidgets' fake async loop.
    unawaited(_sub.cancel());
    provider.dispose();
    await engine.stop();
    await tester.pump();
    apis.dispose();
    engine.dispose();
  }
}

List<int> _ids(SignalsProvider p) => p.active.map((LiveSignal s) => s.id).toList();

void main() {
  testWidgets('offline until the engine runs; then connects to /ws/signals and applies the snapshot silently',
      (tester) async {
    final _Rig rig = _Rig(FakeEngineHttp());
    expect(rig.provider.connection, SignalsConnection.offline);
    expect(rig.sockets.sockets, isEmpty);

    await rig.start(tester);
    expect(rig.sockets.last.uri.toString(), 'ws://127.0.0.1:8765/ws/signals');
    expect(rig.provider.connection, SignalsConnection.connecting);

    await rig.send(tester, snapshotMsg([signalJson(id: 1), signalJson(id: 2, symbol: 'BRNUSD.x')]));
    expect(rig.provider.connection, SignalsConnection.connected);
    expect(_ids(rig.provider)..sort(), [1, 2]);
    expect(rig.provider.activeCount, 2);
    expect(rig.provider.status!.state, LiveSignalsState.running);
    expect(rig.announced, isEmpty, reason: 'a snapshot never alerts');
    await rig.dispose(tester);
  });

  testWidgets('signal -> announced once; expired / superseded remove; keepalive, unknown and junk ignored',
      (tester) async {
    final _Rig rig = _Rig(FakeEngineHttp());
    await rig.start(tester);
    await rig.send(tester, snapshotMsg([signalJson(id: 1)]));

    await rig.send(tester, signalMsg(signalJson(id: 5, direction: 'sell')));
    expect(rig.announced.map((LiveSignal s) => s.id), [5]);
    expect(_ids(rig.provider)..sort(), [1, 5]);

    // The same id again (an update): replaced, not re-announced.
    await rig.send(tester, signalMsg(signalJson(id: 5, direction: 'sell', volume: 0.2)));
    expect(rig.announced, hasLength(1));
    expect(rig.provider.active.firstWhere((LiveSignal s) => s.id == 5).volume, 0.2);

    int notified = 0;
    rig.provider.addListener(() => notified++);
    await rig.send(tester, keepaliveMsg());
    await rig.send(tester, {'type': 'tick', 'symbol': 'XAUUSD.x'});
    rig.sockets.last.controller.add('{not json');
    await tester.pump();
    rig.sockets.last.controller.add('[1, 2]');
    await tester.pump();
    expect(notified, 0, reason: 'keepalive / unknown / unparsable frames change nothing');
    expect(rig.provider.connection, SignalsConnection.connected);

    await rig.send(tester, signalMsg(signalJson(id: 5, status: 'expired'), type: 'expired'));
    expect(_ids(rig.provider), [1]);
    await rig.send(tester, signalMsg(signalJson(id: 1, status: 'superseded'), type: 'superseded'));
    expect(rig.provider.active, isEmpty);
    expect(rig.announced, hasLength(1));
    await rig.dispose(tester);
  });

  testWidgets('reconnect with backoff; the new snapshot and later updates never re-announce', (tester) async {
    final _Rig rig = _Rig(FakeEngineHttp());
    await rig.start(tester);
    await rig.send(tester, snapshotMsg([signalJson(id: 1)]));
    await rig.send(tester, signalMsg(signalJson(id: 2)));
    expect(rig.announced.map((LiveSignal s) => s.id), [2]);

    // The engine drops the socket.
    await rig.sockets.last.controller.close();
    await tester.pump();
    expect(rig.provider.connection, SignalsConnection.reconnecting);
    expect(rig.provider.connectionErrorFa, isNotNull);
    expect(rig.sockets.sockets, hasLength(1));

    await tester.pump(const Duration(milliseconds: 150)); // backoff[0]
    expect(rig.sockets.sockets, hasLength(2));

    // Meanwhile signal 3 was created: it arrives in the snapshot -> shown, not announced.
    await rig.send(tester, snapshotMsg([signalJson(id: 1), signalJson(id: 2), signalJson(id: 3)]));
    expect(rig.provider.connection, SignalsConnection.connected);
    expect(rig.provider.connectionErrorFa, isNull);
    expect(_ids(rig.provider)..sort(), [1, 2, 3]);
    await rig.send(tester, signalMsg(signalJson(id: 3)));
    await rig.send(tester, signalMsg(signalJson(id: 2)));
    expect(rig.announced.map((LiveSignal s) => s.id), [2], reason: 'deduped by id across the reconnect');

    await rig.send(tester, signalMsg(signalJson(id: 4)));
    expect(rig.announced.map((LiveSignal s) => s.id), [2, 4]);
    await rig.dispose(tester);
  });

  testWidgets('silent socket (no keepalive) is treated as dead after staleAfter', (tester) async {
    final _Rig rig = _Rig(FakeEngineHttp());
    await rig.start(tester);
    await rig.send(tester, snapshotMsg(const []));
    await tester.pump(const Duration(seconds: 4));
    await rig.send(tester, keepaliveMsg()); // re-arms the watchdog
    await tester.pump(const Duration(seconds: 4));
    expect(rig.sockets.sockets, hasLength(1));
    await tester.pump(const Duration(seconds: 2));
    expect(rig.provider.connection, SignalsConnection.reconnecting);
    expect(rig.sockets.sockets.first.closed, isTrue);
    await tester.pump(const Duration(milliseconds: 150));
    expect(rig.sockets.sockets, hasLength(2));
    await rig.dispose(tester);
  });

  testWidgets('status message: status replaced and the active list re-read over REST (expiry updates)',
      (tester) async {
    final FakeEngineHttp http = FakeEngineHttp({
      'GET /signals': (_) => jsonBody(signalsPageJson([signalJson(id: 1, expires: '2021-10-26T16:00:00Z')])),
    });
    final _Rig rig = _Rig(http);
    await rig.start(tester);
    await rig.send(tester, snapshotMsg([signalJson(id: 1, expires: null), signalJson(id: 2)]));
    expect(rig.provider.active.firstWhere((LiveSignal s) => s.id == 1).expiryUnknown, isTrue);

    await rig.send(tester, statusMsg(signalStatusJson(state: 'mt5_down', mt5State: 'disconnected')));
    await tester.pump(const Duration(milliseconds: 20)); // the REST re-read (fake transport)
    expect(rig.provider.status!.state, LiveSignalsState.mt5Down);
    expect(http.sent('GET /signals').single.uri.queryParameters, {'status': 'active', 'limit': '500', 'offset': '0'});
    expect(_ids(rig.provider), [1], reason: 'signal 2 is no longer active on the engine');
    expect(rig.provider.active.single.expiresTime, DateTime.utc(2021, 10, 26, 16));
    expect(rig.announced, isEmpty, reason: 'a REST re-read never alerts');
    await rig.dispose(tester);
  });

  testWidgets('a pending expiry is polled until the engine sets it', (tester) async {
    int calls = 0;
    final FakeEngineHttp http = FakeEngineHttp({
      'GET /signals': (_) {
        calls++;
        return jsonBody(signalsPageJson([signalJson(id: 1, expires: calls >= 2 ? '2021-10-26T16:00:00Z' : null)]));
      },
    });
    final _Rig rig = _Rig(http);
    await rig.start(tester);
    await rig.send(tester, snapshotMsg([signalJson(id: 1, expires: null)]));
    await tester.pump(const Duration(seconds: 2));
    await tester.pump();
    expect(calls, 1);
    await rig.send(tester, keepaliveMsg());
    await tester.pump(const Duration(seconds: 2));
    await tester.pump();
    expect(calls, 2);
    expect(rig.provider.active.single.expiryUnknown, isFalse);
    await rig.send(tester, keepaliveMsg());
    await tester.pump(const Duration(seconds: 4));
    expect(calls, 2, reason: 'poll stops once no expiry is pending');
    await rig.dispose(tester);
  });

  testWidgets('engine stop -> offline and socket closed; restart -> new socket, nothing re-announced', (tester) async {
    final _Rig rig = _Rig(FakeEngineHttp());
    await rig.start(tester);
    await rig.send(tester, snapshotMsg(const []));
    await rig.send(tester, signalMsg(signalJson(id: 9)));
    expect(rig.announced, hasLength(1));

    await rig.engine.stop();
    await tester.pump();
    expect(rig.provider.connection, SignalsConnection.offline);
    expect(rig.provider.api, isNull);
    expect(rig.sockets.sockets.first.closed, isTrue);

    await rig.start(tester);
    expect(rig.sockets.sockets, hasLength(2));
    await rig.send(tester, snapshotMsg([signalJson(id: 9)]));
    await rig.send(tester, signalMsg(signalJson(id: 9)));
    expect(rig.announced, hasLength(1));
    await rig.dispose(tester);
  });

  testWidgets('engine error message (db_unavailable) is shown; applyStatus updates at once', (tester) async {
    final _Rig rig = _Rig(FakeEngineHttp());
    await rig.start(tester);
    await rig.send(tester, {'type': 'error', 'code': 'db_unavailable', 'message_fa': 'پایگاه داده در دسترس نیست.'});
    expect(rig.provider.connectionErrorFa, 'پایگاه داده در دسترس نیست.');

    rig.provider.applyStatus(LiveSignalsStatus.fromJson(signalStatusJson(enabled: false, state: 'disabled')));
    expect(rig.provider.status!.enabled, isFalse);
    await rig.dispose(tester);
  });

  testWidgets('symbol digits come from GET /symbols (for price formatting)', (tester) async {
    final _Rig rig = _Rig(FakeEngineHttp({'GET /symbols': (_) => jsonBody(symbolsJson())}));
    await rig.start(tester);
    await tester.pump();
    expect(rig.provider.digitsOf('XAUUSD.x'), isNotNull);
    expect(rig.provider.knownSymbols, contains('XAUUSD.x'));
    await rig.dispose(tester);
  });
}
