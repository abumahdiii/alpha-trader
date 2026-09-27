// BacktestScreen end to end over the real EngineApi, a fake HTTP transport
// and a fake progress socket: form, progress, results, runs list, prefill.

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/screens/backtest/backtest_form.dart';
import 'package:alpha_trader/screens/backtest/backtest_screen.dart';
import 'package:alpha_trader/screens/backtest/metric_cards.dart';
import 'package:alpha_trader/widgets/engine_gate.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';

Finder _key(String k) => find.byKey(ValueKey<String>(k));

Future<void> _tab(WidgetTester tester, String key) async {
  await tester.tap(_key(key));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 500));
}

class _Page {
  _Page(this.h, this.engine, this.sockets);

  final EngineHarness h;
  final FakeBacktestEngine engine;
  final FakeSocketConnector sockets;
}

Future<_Page> _pump(WidgetTester tester, {FakeBacktestEngine? engine, bool start = true}) async {
  tester.view.physicalSize = const Size(2200, 1100);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final FakeBacktestEngine e = engine ?? FakeBacktestEngine();
  final FakeSocketConnector sockets = FakeSocketConnector();
  final EngineHarness h = EngineHarness(http: e.http());
  await tester.pumpWidget(h.wrap(BacktestScreen(watcherFactory: sockets.watcher)));
  if (start) {
    await h.start(tester);
    await settle(tester, 20);
  }
  return _Page(h, e, sockets);
}

Future<void> _openRun(WidgetTester tester, int id) async {
  await _tab(tester, 'bt-side-runs');
  await tester.tap(_key('bt-run-$id'));
  await settle(tester, 20);
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('engine off: the page explains instead of failing', (tester) async {
    final _Page p = await _pump(tester, start: false);
    expect(find.text(EngineUnavailableView.title), findsOneWidget);
    expect(_key('bt-form'), findsNothing);
    await p.h.dispose(tester);
  });

  testWidgets('empty state and form: manual <-> random, new seed, zero-spread label', (tester) async {
    final _Page p = await _pump(tester);
    expect(find.text('هنوز بک‌تستی وجود ندارد'), findsOneWidget);
    expect(_key('bt-from'), findsOneWidget);
    expect(_key('bt-seed'), findsNothing);
    expect(find.text(BacktestFormPanel.zeroSpreadLabel), findsOneWidget);

    await tester.tap(_key('bt-mode-random'));
    await settle(tester);
    expect(_key('bt-from'), findsNothing);
    expect(_key('bt-windows-count'), findsOneWidget);
    expect(_key('bt-window-months'), findsOneWidget);
    final String seed = tester
        .widget<EditableText>(find.descendant(of: _key('bt-seed'), matching: find.byType(EditableText)))
        .controller
        .text;
    expect(seed, matches(RegExp(r'^\d+$')));

    await tester.tap(_key('bt-new-seed'));
    await settle(tester);
    final String seed2 = tester
        .widget<EditableText>(find.descendant(of: _key('bt-seed'), matching: find.byType(EditableText)))
        .controller
        .text;
    expect(seed2, matches(RegExp(r'^\d+$')));
    expect(seed2, isNot(seed));

    await tester.tap(_key('bt-spread-custom'));
    await settle(tester);
    expect(_key('bt-custom-spread'), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('form validation: a manual run without a period sends nothing', (tester) async {
    final _Page p = await _pump(tester);
    await tester.tap(find.text(BacktestFormPanel.runLabel));
    await settle(tester);
    expect(find.text('ابتدای بازه را انتخاب کنید.'), findsOneWidget);
    expect(find.text('انتهای بازه را انتخاب کنید.'), findsOneWidget);
    expect(p.h.http.sent('POST /backtests'), isEmpty);
    await p.h.dispose(tester);
  });

  testWidgets('the engine 422 period message is shown under the period', (tester) async {
    final FakeBacktestEngine engine = FakeBacktestEngine()
      ..onSubmit = (_) => engineError(422, 'window_beyond_data', 'انتهای بازه بعد از آخرین کندل ذخیره‌شده است.');
    final _Page p = await _pump(tester, engine: engine);
    p.h.navigation.openBacktest(symbol: 'XAUUSD.x', from: DateTime.utc(2026, 1, 1), to: DateTime.utc(2027, 1, 1));
    await settle(tester);
    expect(find.text('از 2026-01-01'), findsOneWidget);
    expect(find.text('تا 2026-12-31'), findsOneWidget);

    await tester.tap(find.text(BacktestFormPanel.runLabel));
    await settle(tester, 12);
    expect(
      find.descendant(
          of: _key('bt-period-errors'), matching: find.text('انتهای بازه بعد از آخرین کندل ذخیره‌شده است.')),
      findsOneWidget,
    );
    expect(_key('bt-progress'), findsNothing);
    await p.h.dispose(tester);
  });

  testWidgets('run: live progress with «لغو», then done shows the result', (tester) async {
    final _Page p = await _pump(tester);
    p.h.navigation.openBacktest(
      symbol: 'XAUUSD.x',
      from: DateTime.utc(2023, 1, 2),
      to: DateTime.utc(2023, 12, 31, 23, 59, 59),
      autoRun: true,
    );
    await settle(tester, 12);
    expect(p.h.http.sent('POST /backtests'), hasLength(1));
    expect(_key('bt-progress'), findsOneWidget);

    p.sockets.last.send(progressMsg(20, 'simulating', 42.5));
    await settle(tester);
    expect(find.textContaining('شبیه‌سازی معاملات'), findsOneWidget);
    expect(find.text('42.5%'), findsOneWidget);

    await tester.tap(_key('bt-cancel'));
    await settle(tester);
    expect(p.h.http.sent('POST /backtests/20/cancel'), hasLength(1));
    expect(find.textContaining('در حال لغو'), findsOneWidget);

    p.engine.details[20] = manualDetailJson(id: 20);
    p.engine.trades[20] = [tradeJson(0), tradeJson(1, netPnl: -30), tradeJson(2, netPnl: 130.25)];
    p.sockets.last.send(doneMsg(20));
    await settle(tester, 20);
    expect(_key('bt-progress'), findsNothing);
    expect(find.text('بک‌تست #20 — XAUUSD.x — دستی'), findsOneWidget);
    expect(find.descendant(of: _key('bt-metric-net-profit'), matching: find.text('+150.25 \$')), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('manual result: honest labels, metric cards, sortable trades, equity hover, skipped', (tester) async {
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [runSummaryJson(id: 7)],
      details: {7: manualDetailJson(id: 7)},
    )..trades[7] = [tradeJson(0), tradeJson(1, netPnl: -30), tradeJson(2, netPnl: 120)];
    final _Page p = await _pump(tester, engine: engine);
    await _openRun(tester, 7);

    // Header: provisional, labels, strategy + params hash, spread fallback.
    expect(_key('bt-provisional'), findsOneWidget);
    expect(find.descendant(of: _key('bt-labels'), matching: find.text('بدون سوآپ')), findsOneWidget);
    expect(find.descendant(of: _key('bt-labels'), matching: find.text('کمیسیون صفر')), findsOneWidget);
    expect(
        find.descendant(of: _key('bt-header-strategy'), matching: find.textContaining('8e223612f12a')), findsOneWidget);
    expect(find.descendant(of: _key('bt-header-spread'), matching: find.textContaining('34 پوینت')), findsOneWidget);

    // Cards: exactly the engine values.
    expect(find.descendant(of: _key('bt-metric-net-profit'), matching: find.text('+150.25 \$')), findsOneWidget);
    expect(find.descendant(of: _key('bt-metric-win-rate'), matching: find.text('66.7%')), findsOneWidget);
    expect(find.descendant(of: _key('bt-metric-pf'), matching: find.text('2.50')), findsOneWidget);
    expect(find.descendant(of: _key('bt-metric-max-dd'), matching: find.text('4.60%')), findsOneWidget);
    expect(find.descendant(of: _key('bt-metric-sharpe'), matching: find.text('1.23')), findsOneWidget);
    expect(_key('bt-zero-trades'), findsNothing);

    // Trades: sort by net P&L (column 11) ascending, then descending.
    await _tab(tester, 'bt-tab-trades');
    expect(find.descendant(of: _key('bt-trades-row-0'), matching: find.text('+50.00 \$')), findsOneWidget);
    await tester.tap(_key('bt-trades-h-11'));
    await tester.pump();
    expect(find.descendant(of: _key('bt-trades-row-0'), matching: find.text('-30.00 \$')), findsOneWidget);
    await tester.tap(_key('bt-trades-h-11'));
    await tester.pump();
    expect(find.descendant(of: _key('bt-trades-row-0'), matching: find.text('+120.00 \$')), findsOneWidget);
    expect(find.descendant(of: _key('bt-trades-row-0'), matching: find.text('حد سود')), findsOneWidget);
    expect(find.descendant(of: _key('bt-trades-row-0'), matching: find.text('1852.12')), findsOneWidget);

    // Equity: CustomPainter + hover readout (local + UTC time).
    await _tab(tester, 'bt-tab-equity');
    expect(_key('bt-equity-chart'), findsOneWidget);
    final TestGesture mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await mouse.addPointer(location: Offset.zero);
    addTearDown(mouse.removePointer);
    await mouse.moveTo(tester.getCenter(_key('bt-equity-chart')));
    await tester.pump();
    expect(_key('bt-equity-readout'), findsOneWidget);
    expect(find.textContaining('UTC: 2023.01.0'), findsOneWidget);

    // Skipped candidates with the Persian reason.
    await _tab(tester, 'bt-tab-skipped');
    expect(find.textContaining('معامله باز وجود داشت'), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('a run without trades says so', (tester) async {
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [runSummaryJson(id: 9, tradeCount: 0, netProfit: 0)],
      details: {9: manualDetailJson(id: 9, trades: 0, net: 0)},
    );
    final _Page p = await _pump(tester, engine: engine);
    await _openRun(tester, 9);
    expect(_key('bt-zero-trades'), findsOneWidget);
    expect(find.text(kZeroTradesFa), findsOneWidget);
    await _tab(tester, 'bt-tab-trades');
    expect(_key('bt-trades-empty'), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('a failed run shows the engine message', (tester) async {
    final Map<String, Object?> failed = runSummaryJson(
      id: 10,
      status: 'error',
      error: {'code': 'internal_error', 'message_fa': 'خطای داخلی موتور در شبیه‌سازی.'},
    );
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [failed],
      details: {
        10: {...failed, 'windows': <Object?>[]},
      },
    );
    final _Page p = await _pump(tester, engine: engine);
    await _openRun(tester, 10);
    expect(_key('bt-result-failed'), findsOneWidget);
    expect(find.textContaining('خطای داخلی موتور در شبیه‌سازی.'), findsWidgets);
    await p.h.dispose(tester);
  });

  testWidgets('random result: seed, windows table, distribution chart and summary', (tester) async {
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [runSummaryJson(id: 8, mode: 'random')],
      details: {8: randomDetailJson(id: 8)},
    )..trades[8] = [tradeJson(0), tradeJson(0, window: 2, netPnl: -40)];
    final _Page p = await _pump(tester, engine: engine);
    await _openRun(tester, 8);

    expect(find.descendant(of: _key('bt-header-seed'), matching: find.textContaining('seed: 42')), findsOneWidget);
    expect(_key('bt-distribution'), findsOneWidget);
    expect(find.descendant(of: _key('bt-dist-median'), matching: find.text('+20.00 \$')), findsOneWidget);
    expect(find.descendant(of: _key('bt-dist-profitable'), matching: find.text('66.7%')), findsOneWidget);
    expect(find.descendant(of: _key('bt-dist-worst'), matching: find.text('-100.00 \$')), findsOneWidget);

    await _tab(tester, 'bt-tab-windows');
    for (int i = 0; i < 3; i++) {
      expect(_key('bt-windows-row-$i'), findsOneWidget);
    }
    expect(find.descendant(of: _key('bt-windows-row-2'), matching: find.text('-100.00 \$')), findsOneWidget);
    await tester.tap(_key('bt-windows-row-2'));
    await tester.pump();

    await _tab(tester, 'bt-tab-trades');
    expect(_key('bt-window-filter'), findsOneWidget);
    expect(_key('bt-trades-row-0'), findsOneWidget);
    expect(_key('bt-trades-row-1'), findsNothing, reason: 'only window 3 trades');
    await p.h.dispose(tester);
  });

  testWidgets('previous runs: delete asks first, then removes the run', (tester) async {
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [runSummaryJson(id: 8), runSummaryJson(id: 7)],
      details: {7: manualDetailJson(id: 7)},
    );
    final _Page p = await _pump(tester, engine: engine);
    await _tab(tester, 'bt-side-runs');
    expect(_key('bt-run-8'), findsOneWidget);

    await tester.tap(_key('bt-run-delete-8'));
    await settle(tester);
    await tester.tap(_key('bt-delete-cancel'));
    await settle(tester);
    expect(p.h.http.sent('DELETE /backtests/8'), isEmpty);

    await tester.tap(_key('bt-run-delete-8'));
    await settle(tester);
    await tester.tap(_key('bt-delete-confirm'));
    await settle(tester, 12);
    expect(p.h.http.sent('DELETE /backtests/8'), hasLength(1));
    expect(_key('bt-run-8'), findsNothing);
    expect(_key('bt-run-7'), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('a job still running when the page opens is followed (progress shown)', (tester) async {
    final Map<String, Object?> running = runSummaryJson(id: 11, status: 'running', progress: 20);
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [running],
      details: {
        11: {
          ...running,
          'windows': <Object?>[],
          'live': {'id': 11, 'status': 'running', 'phase': 'scanning', 'percent': 20.0},
        },
      },
    );
    final _Page p = await _pump(tester, engine: engine);
    expect(_key('bt-progress'), findsOneWidget);
    expect(p.sockets.last.uri.path, '/ws/backtests/11');
    expect(_key('bt-result-running'), findsOneWidget);
    final ElevatedButton run = tester.widget<ElevatedButton>(
      find.descendant(of: _key('bt-run'), matching: find.byWidgetPredicate((Widget w) => w is ElevatedButton)),
    );
    expect(run.onPressed, isNull);
    await p.h.dispose(tester);
  });
}
