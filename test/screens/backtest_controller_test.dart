import 'dart:async';
import 'dart:math' as math;

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/backtest_models.dart';
import 'package:alpha_trader/providers/shell_navigation.dart';
import 'package:alpha_trader/screens/backtest/backtest_controller.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';

/// Polls [condition] (the controller works on real futures) for up to 2 s.
Future<void> until(bool Function() condition, {String reason = ''}) async {
  final Stopwatch w = Stopwatch()..start();
  while (!condition()) {
    if (w.elapsed > const Duration(seconds: 2)) fail('timed out waiting: $reason');
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}

void main() {
  late FakeBacktestEngine engine;
  late FakeSocketConnector sockets;
  late FakeEngineHttp http;
  late BacktestController c;

  BacktestController make({FakeEngineHttp? withHttp}) {
    http = withHttp ?? engine.http();
    return BacktestController(
      api: EngineApi(port: 8765, httpClientAdapter: http),
      watcherFactory: sockets.watcher,
      random: math.Random(1),
    );
  }

  setUp(() {
    engine = FakeBacktestEngine();
    sockets = FakeSocketConnector();
  });

  tearDown(() => c.dispose());

  test('init loads symbols and runs; the first symbol is preselected; a random seed is filled', () async {
    engine.runs.add(runSummaryJson(id: 7));
    c = make();
    await c.init();
    expect(c.symbols.map((s) => s.symbol), ['XAUUSD.x', 'BRENT.x']);
    expect(c.symbol, 'XAUUSD.x');
    expect(c.runs.single.id, 7);
    expect(c.seedText, matches(RegExp(r'^\d+$')));
    expect(c.activeRunId, isNull);
  });

  test('form validation blocks the request (manual without a period, bad seed)', () async {
    c = make();
    await c.init();
    expect(await c.submit(), isFalse);
    expect(c.errorsOf(BacktestField.period), hasLength(2));

    c.setFromDay(DateTime(2024, 5, 1));
    c.setToDay(DateTime(2024, 1, 1));
    expect(await c.submit(), isFalse);
    expect(c.errorsOf(BacktestField.period).single, contains('قبل از'));

    c.setMode(BacktestMode.random);
    c.setSeedText('99999999999999999999');
    expect(await c.submit(), isFalse);
    expect(c.errorsOf(BacktestField.seed), isNotEmpty);
    expect(http.sent('POST /backtests'), isEmpty);
  });

  test('submit -> WS progress -> done -> the result is loaded', () async {
    c = make();
    await c.init();
    c.setFromDay(DateTime(2023, 1, 2));
    c.setToDay(DateTime(2023, 12, 31));
    c.setCommission(3.5);
    c.setSpreadChoice(SpreadChoice.zero);

    expect(await c.submit(), isTrue);
    expect(FakeEngineHttp.bodyOf(http.sent('POST /backtests').single), {
      'symbol': 'XAUUSD.x',
      'mode': 'manual',
      'strategy': 'stddev_channel', // the default system (no /strategies route in this fake)
      'from': '2023-01-02T00:00:00.000Z',
      'to': '2024-01-01T00:00:00.000Z', // the last day is included
      'commission_per_lot_per_side': 3.5,
      'fallback_spread_points': 0,
    });
    expect(c.activeRunId, 20);
    expect(c.canSubmit, isFalse);
    expect(sockets.last.uri.path, '/ws/backtests/20');

    sockets.last.send(progressMsg(20, 'scanning', 10));
    await until(() => c.progress?.phase == 'scanning', reason: 'progress');
    expect(c.progress!.percent, 10);

    engine.details[20] = manualDetailJson(id: 20);
    engine.trades[20] = [tradeJson(0), tradeJson(1, netPnl: -30)];
    sockets.last.send(doneMsg(20));
    await until(() => c.trades != null && c.equity != null && c.skipped != null, reason: 'results');
    expect(c.activeRunId, isNull);
    expect(c.progress!.isDone, isTrue);
    expect(c.detail!.id, 20);
    expect(c.detail!.metrics!.tradeCount, 3);
    expect(c.trades!.trades, hasLength(2));
    expect(c.openRunDigits, 2);
    expect(sockets.last.closed, isTrue);
  });

  test('random submit sends count / months / seed and never the period', () async {
    c = make();
    await c.init();
    c.setMode(BacktestMode.random);
    c.setWindowsCount(5);
    c.setWindowMonths(2);
    c.setSeedText('123');
    c.setSpreadChoice(SpreadChoice.custom);
    c.setCustomSpreadPoints(30);
    expect(await c.submit(), isTrue);
    expect(FakeEngineHttp.bodyOf(http.sent('POST /backtests').single), {
      'symbol': 'XAUUSD.x',
      'mode': 'random',
      'strategy': 'stddev_channel',
      'windows_count': 5,
      'window_months': 2,
      'seed': 123,
      'commission_per_lot_per_side': 0.0,
      'fallback_spread_points': 30,
    });
  });

  test('a period 422 lands under the period fields; errors_fa under their fields', () async {
    c = make();
    await c.init();
    c.setFromDay(DateTime(2019, 1, 1));
    c.setToDay(DateTime(2019, 6, 1));
    engine.onSubmit =
        (_) => engineError(422, 'window_too_early', 'ابتدای بازه قبل از آماده شدن کانال و ATR است (2021-11-18).');
    expect(await c.submit(), isFalse);
    expect(c.errorsOf(BacktestField.period).single, contains('2021-11-18'));
    expect(c.submitError, isNull);
    expect(c.activeRunId, isNull);

    c.setMode(BacktestMode.random);
    engine.onSubmit = (_) => engineError(422, 'invalid_request', 'درخواست بک‌تست نامعتبر است.', [
          '«تعداد پنجره‌ها» نباید بیشتر از 500 باشد.',
          'فیلد ناشناخته: \'x\'.',
        ]);
    expect(await c.submit(), isFalse);
    expect(c.errorsOf(BacktestField.windowsCount).single, contains('500'));
    expect(c.submitError!.messageFa, 'درخواست بک‌تست نامعتبر است.');
    expect(c.generalErrors.single, contains('ناشناخته'));
  });

  test('a failed run: the final error message is kept and the list reloaded', () async {
    c = make();
    await c.init();
    c.setFromDay(DateTime(2023, 1, 2));
    c.setToDay(DateTime(2023, 3, 1));
    await c.submit();
    final int before = http.sent('GET /backtests').length;
    sockets.last.send({
      'type': 'error',
      'id': 20,
      'status': 'error',
      'percent': 40.0,
      'code': 'internal_error',
      'message_fa': 'خطای داخلی موتور در شبیه‌سازی.',
    });
    await until(() => c.activeRunId == null, reason: 'final');
    expect(c.progress!.type, 'error');
    expect(c.progress!.messageFa, contains('خطای داخلی'));
    await until(() => http.sent('GET /backtests').length > before, reason: 'reload');
  });

  test('cancel posts /cancel and the final "cancelled" ends the run', () async {
    c = make();
    await c.init();
    c.setFromDay(DateTime(2023, 1, 2));
    c.setToDay(DateTime(2023, 3, 1));
    await c.submit();
    await c.cancel();
    expect(http.sent('POST /backtests/20/cancel'), hasLength(1));
    expect(c.isCancelling, isTrue);
    sockets.last.send({
      'type': 'cancelled',
      'id': 20,
      'status': 'cancelled',
      'percent': 30.0,
      'code': 'cancelled',
      'message_fa': 'اجرا لغو شد.',
    });
    await until(() => c.activeRunId == null, reason: 'cancelled');
    expect(c.progress!.type, 'cancelled');
    expect(c.isCancelling, isFalse);
  });

  test('polling fallback when the socket cannot connect', () async {
    sockets = FakeSocketConnector(failReady: true);
    int polls = 0;
    c = make(
      withHttp: engine.http(extra: {
        'GET /backtests/20': (_) {
          polls++;
          return jsonBody(polls < 2
              ? {
                  ...runSummaryJson(id: 20, status: 'running', progress: 50),
                  'windows': <Object?>[],
                  'live': {'id': 20, 'status': 'running', 'phase': 'simulating', 'percent': 50.0},
                }
              : manualDetailJson(id: 20));
        },
      }),
    );
    engine.details[20] = manualDetailJson(id: 20);
    await c.init();
    c.setFromDay(DateTime(2023, 1, 2));
    c.setToDay(DateTime(2023, 3, 1));
    await c.submit();
    await until(() => c.progress?.isDone ?? false, reason: 'done via polling');
    expect(c.activeRunId, isNull);
    await until(() => c.detail?.status == BacktestStatus.done, reason: 'detail');
  });

  test('a run still active on init is followed and opened again (engine restart / page rebuilt)', () async {
    engine.runs.addAll([runSummaryJson(id: 11, status: 'running', progress: 20), runSummaryJson(id: 7)]);
    engine.details[11] = {
      ...runSummaryJson(id: 11, status: 'running', progress: 20),
      'windows': <Object?>[],
      'live': {'id': 11, 'status': 'running', 'phase': 'simulating', 'percent': 20.0},
    };
    c = make();
    await c.init();
    expect(c.activeRunId, 11);
    expect(sockets.last.uri.path, '/ws/backtests/11');
    await until(() => c.detail?.id == 11, reason: 'detail');
    expect(c.canSubmit, isFalse);
  });

  test('delete removes the run and closes it when open; 409 is returned in Persian', () async {
    engine.runs.addAll([runSummaryJson(id: 8), runSummaryJson(id: 7)]);
    engine.details[7] = manualDetailJson(id: 7);
    c = make();
    await c.init();
    await c.openRun(7);
    await until(() => c.trades != null, reason: 'results');
    expect(await c.deleteRun(7), isNull);
    expect(c.runs.map((r) => r.id), [8]);
    expect(c.openRunId, isNull);

    c.dispose();
    c = make(
      withHttp: engine.http(extra: {
        'DELETE /backtests/8': (_) => engineError(409, 'run_active', 'اجرای در صف یا در حال اجرا حذف نمی‌شود.'),
      }),
    );
    await c.init();
    final EngineApiException? e = await c.deleteRun(8);
    expect(e!.code, 'run_active');
    expect(c.runs.map((r) => r.id), [8]);
  });

  test('prefill fills symbol + manual period and auto-runs', () async {
    c = make();
    await c.init();
    c.setMode(BacktestMode.random);
    await c.applyPrefill(BacktestPrefill(
      symbol: 'BRENT.x',
      from: DateTime.utc(2024, 2, 1),
      to: DateTime.utc(2024, 3, 1, 23, 59, 59),
      autoRun: true,
    ));
    expect(c.mode, BacktestMode.manual);
    expect(c.symbol, 'BRENT.x');
    expect(c.fromDay, DateTime.utc(2024, 2, 1));
    expect(c.toDay, DateTime.utc(2024, 3, 1));
    // A full prefill runs its exact instants (not the whole days shown in the fields).
    final Map<String, Object?> body =
        FakeEngineHttp.bodyOf(http.sent('POST /backtests').single)! as Map<String, Object?>;
    expect(body, containsPair('from', '2024-02-01T00:00:00.000Z'));
    expect(body, containsPair('to', '2024-03-01T23:59:59.000Z'));
  });

  test('chart prefill (23:00 .. 21:00): the request carries exactly the engine window', () async {
    c = make();
    await c.init();
    await c.applyPrefill(BacktestPrefill(
      symbol: 'XAUUSD.x',
      from: DateTime.utc(2026, 8, 9, 23),
      to: DateTime.utc(2026, 9, 25, 21),
      autoRun: true,
    ));
    expect(c.hasExactPeriod, isTrue);
    expect(c.fromDay, DateTime.utc(2026, 8, 9), reason: 'the day fields only show it');
    expect(c.toDay, DateTime.utc(2026, 9, 25));
    final Map<String, Object?> body =
        FakeEngineHttp.bodyOf(http.sent('POST /backtests').single)! as Map<String, Object?>;
    expect(body['mode'], 'manual');
    expect(body['from'], '2026-08-09T23:00:00.000Z');
    expect(body['to'], '2026-09-25T21:00:00.000Z');
  });

  test('editing a period day, the mode, the symbol or «حذف» drops the exact period (day semantics)', () async {
    c = make();
    await c.init();
    Future<void> prefill() => c.applyPrefill(
        BacktestPrefill(symbol: 'XAUUSD.x', from: DateTime.utc(2026, 8, 9, 23), to: DateTime.utc(2026, 9, 25, 21)));

    await prefill();
    c.setFromDay(DateTime(2026, 8, 10));
    expect(c.hasExactPeriod, isFalse);
    expect(c.buildRequest().toJson()['from'], '2026-08-10T00:00:00.000Z');
    expect(c.buildRequest().toJson()['to'], '2026-09-26T00:00:00.000Z', reason: 'end day included');

    await prefill();
    c.setToDay(DateTime(2026, 9, 20));
    expect(c.hasExactPeriod, isFalse);
    expect(c.buildRequest().toJson()['to'], '2026-09-21T00:00:00.000Z');

    await prefill();
    c.setMode(BacktestMode.random);
    expect(c.hasExactPeriod, isFalse);
    c.setMode(BacktestMode.manual);
    expect(c.buildRequest().toJson()['from'], '2026-08-09T00:00:00.000Z');

    await prefill();
    c.setSymbol('BRENT.x');
    expect(c.hasExactPeriod, isFalse);

    await prefill();
    c.clearExactPeriod();
    expect(c.hasExactPeriod, isFalse);
    expect(c.buildRequest().toJson()['to'], '2026-09-26T00:00:00.000Z');
    expect(http.sent('POST /backtests'), isEmpty);
  });

  test('prefill with an exclusive midnight end keeps the day before as the last day', () async {
    c = make();
    await c.init();
    await c.applyPrefill(
        BacktestPrefill(symbol: 'XAUUSD.x', from: DateTime.utc(2024, 2, 1), to: DateTime.utc(2024, 3, 1)));
    expect(c.toDay, DateTime.utc(2024, 2, 29));
    expect(http.sent('POST /backtests'), isEmpty);
  });

  test('symbols failing: the error is kept and nothing can be submitted', () async {
    c = make(
      withHttp: engine.http(extra: {
        'GET /symbols': (_) => throw DioException.connectionError(
              requestOptions: RequestOptions(path: '/symbols'),
              reason: 'refused',
            ),
      }),
    );
    await c.init();
    expect(c.symbolsError, isNotNull);
    expect(c.canSubmit, isFalse);
  });

  test('dispose closes the watcher (socket closed)', () async {
    c = make();
    await c.init();
    c.setFromDay(DateTime(2023, 1, 2));
    c.setToDay(DateTime(2023, 3, 1));
    await c.submit();
    final FakeProgressSocket s = sockets.last;
    c.dispose();
    await until(() => s.closed, reason: 'socket closed');
    c = make(); // for tearDown
  });

  test('limits: first symbol, refetch on symbol change / prefill, stale answers dropped, failure -> null', () async {
    final Map<String, Completer<void>> gates = {};
    int failures = 0;
    c = make(
      withHttp: engine.http(extra: {
        'GET /backtests/limits': (RequestOptions r) async {
          final String symbol = r.uri.queryParameters['symbol']!;
          await gates[symbol]?.future;
          if (symbol == 'BAD.x') {
            failures++;
            return engineError(503, 'cache_unreadable', 'کش خوانده نشد.');
          }
          return jsonBody(limitsJson(symbol: symbol));
        },
      }),
    );
    await c.init();
    await until(() => c.limits != null, reason: 'limits of the first symbol');
    expect(c.limits!.symbol, 'XAUUSD.x');

    // A slow answer for BRENT.x arrives after the user moved on to XAUUSD.x again: dropped.
    gates['BRENT.x'] = Completer<void>();
    c.setSymbol('BRENT.x');
    expect(c.limits, isNull, reason: 'the XAUUSD.x bounds are not shown for BRENT.x');
    c.setSymbol('XAUUSD.x');
    await until(() => c.limits?.symbol == 'XAUUSD.x');
    gates['BRENT.x']!.complete();
    await Future<void>.delayed(const Duration(milliseconds: 20));
    expect(c.limits!.symbol, 'XAUUSD.x');

    // A failure leaves no bounds; the form is not blocked.
    c.setSymbol('BAD.x');
    await until(() => failures == 1);
    await Future<void>.delayed(const Duration(milliseconds: 10));
    expect(c.limits, isNull);
    expect(c.submitError, isNull);

    // A chart prefill of another symbol refetches; its exact period stays exact.
    await c.applyPrefill(
        BacktestPrefill(symbol: 'BRENT.x', from: DateTime.utc(2026, 8, 9, 23), to: DateTime.utc(2026, 9, 25, 21)));
    await until(() => c.limits?.symbol == 'BRENT.x');
    expect(c.hasExactPeriod, isTrue);
    final BacktestRequest req = c.buildRequest();
    expect(req.toJson()['from'], '2026-08-09T23:00:00.000Z');
    expect(req.toJson()['to'], '2026-09-25T21:00:00.000Z');
    expect(http.sent('GET /backtests/limits').map((r) => r.uri.queryParameters['symbol']),
        ['XAUUSD.x', 'BRENT.x', 'XAUUSD.x', 'BAD.x', 'BRENT.x']);
  });
}
