import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/backtest_models.dart';
import 'package:alpha_trader/services/backtest_progress.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';

Map<String, Object?> _running(int id, double percent) => {
      ...runSummaryJson(id: id, status: 'running', progress: percent),
      'windows': <Object?>[],
      'live': {
        'id': id,
        'status': 'running',
        'phase': 'simulating',
        'percent': percent,
        'window': 1,
        'windows': 1,
        'cancel_requested': false,
      },
    };

void main() {
  test('WS: progress messages then the final one; the socket is closed', () async {
    final FakeSocketConnector sockets = FakeSocketConnector();
    final EngineApi api = EngineApi(port: 8765, httpClientAdapter: FakeEngineHttp());
    final BacktestProgressWatcher w = sockets.watcher(api, 3)..start();
    final List<BacktestProgress> got = [];
    final Future<void> done = w.events.forEach(got.add);

    expect(sockets.last.uri.toString(), 'ws://127.0.0.1:8765/ws/backtests/3');
    sockets.last.send(progressMsg(3, 'loading', 1.0));
    sockets.last.send(progressMsg(3, 'simulating', 50.0));
    sockets.last.send(doneMsg(3));
    await done;

    expect(got.map((p) => p.type), ['progress', 'progress', 'done']);
    expect(got[1].percent, 50.0);
    expect(w.isPolling, isFalse);
    expect(w.isFinished, isTrue);
    expect(sockets.last.closed, isTrue);
  });

  test('unparsable frames are ignored', () async {
    final FakeSocketConnector sockets = FakeSocketConnector();
    final BacktestProgressWatcher w = sockets.watcher(EngineApi(port: 1, httpClientAdapter: FakeEngineHttp()), 3)
      ..start();
    final List<BacktestProgress> got = [];
    final Future<void> done = w.events.forEach(got.add);
    sockets.last.controller.add('not json');
    sockets.last.send(doneMsg(3));
    await done;
    expect(got.single.isDone, isTrue);
  });

  test('socket cannot connect -> polls GET /backtests/{id} until terminal', () async {
    int polls = 0;
    final http = FakeEngineHttp({
      'GET /backtests/4': (_) {
        polls++;
        return jsonBody(polls < 3 ? _running(4, polls * 30.0) : manualDetailJson(id: 4));
      },
    });
    final FakeSocketConnector sockets = FakeSocketConnector(failReady: true);
    final BacktestProgressWatcher w = sockets.watcher(EngineApi(port: 8765, httpClientAdapter: http), 4)..start();
    final List<BacktestProgress> got = [];
    await w.events.forEach(got.add);

    expect(w.isPolling, isTrue);
    expect(sockets.last.closed, isTrue);
    expect(got.map((p) => p.type), ['progress', 'progress', 'done']);
    expect(got.first.percent, 30.0);
    expect(got.last.tradeCount, 3);
  });

  test('socket drops before the final message -> polling takes over', () async {
    final http = FakeEngineHttp({
      'GET /backtests/5': (_) => jsonBody({
            ...runSummaryJson(
              id: 5,
              status: 'cancelled',
              error: {'code': 'cancelled', 'message_fa': 'اجرا لغو شد.'},
            ),
            'windows': <Object?>[],
          }),
    });
    final FakeSocketConnector sockets = FakeSocketConnector();
    final BacktestProgressWatcher w = sockets.watcher(EngineApi(port: 8765, httpClientAdapter: http), 5)..start();
    final List<BacktestProgress> got = [];
    final Future<void> done = w.events.forEach(got.add);
    sockets.last.send(progressMsg(5, 'scanning', 10));
    await Future<void>.delayed(Duration.zero);
    await sockets.last.controller.close(); // engine went away

    await done;
    expect(w.isPolling, isTrue);
    expect(got.last.type, 'cancelled');
    expect(got.last.messageFa, 'اجرا لغو شد.');
  });

  test('connect throws -> polling; repeated failures end with "lost"', () async {
    final http = FakeEngineHttp({
      'GET /backtests/6': (_) => throw DioException.connectionError(
            requestOptions: RequestOptions(path: '/backtests/6'),
            reason: 'refused',
          ),
    });
    final BacktestProgressWatcher w = BacktestProgressWatcher(
      api: EngineApi(port: 8765, httpClientAdapter: http),
      runId: 6,
      connect: FakeSocketConnector(throwOnConnect: true).call,
      pollInterval: const Duration(milliseconds: 5),
      maxPollFailures: 3,
    )..start();
    final List<BacktestProgress> got = [];
    await w.events.forEach(got.add);
    expect(got.single.type, BacktestProgress.lostType);
    expect(got.single.messageFa, BacktestProgressWatcher.lostMessageFa);
  });

  test('close() before the end stops everything', () async {
    final FakeSocketConnector sockets = FakeSocketConnector();
    final BacktestProgressWatcher w = sockets.watcher(EngineApi(port: 8765, httpClientAdapter: FakeEngineHttp()), 7)
      ..start();
    final Completer<void> ended = Completer<void>();
    w.events.listen((_) {}, onDone: ended.complete);
    await w.close();
    await ended.future.timeout(const Duration(seconds: 1));
    expect(sockets.last.closed, isTrue);
    await w.close(); // idempotent
  });
}
