// BacktestScreen over the real EngineApi and a fake HTTP transport:
// `GET /backtests/limits` bounds the manual date pickers (and never blocks
// the form when it fails), and the result's «نمودار» tab draws the run's
// trades on the chart (fake bars).

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/screens/backtest/backtest_format.dart';
import 'package:alpha_trader/screens/backtest/backtest_screen.dart';
import 'package:alpha_trader/screens/backtest/backtest_trades_chart.dart';

import '../chart/fake_chart_data_source.dart';
import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';

Finder _key(String k) => find.byKey(ValueKey<String>(k));

class _Page {
  _Page(this.h, this.engine);

  final EngineHarness h;
  final FakeBacktestEngine engine;
}

Future<_Page> _pump(
  WidgetTester tester, {
  FakeBacktestEngine? engine,
  RouteHandler? limits,
  FakeChartDataSource? chart,
}) async {
  tester.view.physicalSize = const Size(2200, 1100);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final FakeBacktestEngine e = engine ?? FakeBacktestEngine();
  final FakeSocketConnector sockets = FakeSocketConnector();
  final EngineHarness h = EngineHarness(
    http: e.http(extra: {if (limits != null) 'GET /backtests/limits': limits}),
  );
  await tester.pumpWidget(h.wrap(BacktestScreen(watcherFactory: sockets.watcher, chartSource: chart)));
  await h.start(tester);
  await settle(tester, 20);
  return _Page(h, e);
}

/// The engine's worked example for XAUUSD.x, other bounds for BRENT.x.
ResponseBody _limits(RequestOptions r) {
  final String symbol = r.uri.queryParameters['symbol']!;
  return jsonBody(symbol == 'XAUUSD.x'
      ? limitsJson()
      : limitsJson(symbol: symbol, earliestStart: '2022-01-10T00:00:00Z', dataEnd: '2026-09-25T21:00:00Z'));
}

List<String?> _limitsRequests(_Page p) => [
      for (final RequestOptions r in p.h.http.sent('GET /backtests/limits')) r.uri.queryParameters['symbol'],
    ];

Future<DatePickerDialog> _openPicker(WidgetTester tester, String key) async {
  await tester.tap(_key(key));
  await tester.pumpAndSettle();
  final DatePickerDialog d = tester.widget<DatePickerDialog>(find.byType(DatePickerDialog));
  Navigator.of(tester.element(find.byType(DatePickerDialog))).pop();
  await tester.pumpAndSettle();
  return d;
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('limits: pickers bounded to whole allowed UTC days, note shown; symbol change refetches', (tester) async {
    final _Page p = await _pump(tester, limits: _limits);
    expect(_limitsRequests(p), ['XAUUSD.x']);
    // earliest_start 2024-07-29 09:00 -> first whole day 07-30; data_end 2025-03-03 00:00 -> last day 03-02.
    final String note = 'بازه مجاز: ${fmtDay(DateTime.utc(2024, 7, 30))} تا ${fmtDay(DateTime.utc(2025, 3, 2))} (UTC)';
    expect(find.text(note), findsOneWidget);
    for (final String k in ['bt-from', 'bt-to']) {
      final DatePickerDialog d = await _openPicker(tester, k);
      expect(d.firstDate, DateTime(2024, 7, 30));
      expect(d.lastDate, DateTime(2025, 3, 2));
      expect(d.initialDate, DateTime(2025, 3, 2), reason: 'today is clamped into the allowed days');
    }

    await tester.tap(_key('bt-symbol'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('BRENT.x').last);
    await settle(tester, 20);
    expect(_limitsRequests(p), ['XAUUSD.x', 'BRENT.x']);
    expect(find.text(note), findsNothing);
    expect(
      find.text('بازه مجاز: ${fmtDay(DateTime.utc(2022, 1, 10))} تا ${fmtDay(DateTime.utc(2026, 9, 24))} (UTC)'),
      findsOneWidget,
      reason: 'earliest_start at midnight is its own day; data_end 21:00 -> the day before is the last whole day',
    );
    final DatePickerDialog d = await _openPicker(tester, 'bt-from');
    expect(d.firstDate, DateTime(2022, 1, 10));
    expect(d.lastDate, DateTime(2026, 9, 24));
    await p.h.dispose(tester);
  });

  testWidgets('limits failure: no note, pickers unbounded, the form still submits', (tester) async {
    final _Page p = await _pump(
      tester,
      limits: (_) => engineError(503, 'cache_unreadable', 'کش خوانده نشد.'),
    );
    expect(_limitsRequests(p), ['XAUUSD.x']);
    expect(_key('bt-limits-note'), findsNothing);
    expect(find.text('کش خوانده نشد.'), findsNothing, reason: 'a limits failure is silent');
    final DatePickerDialog d = await _openPicker(tester, 'bt-from');
    expect(d.firstDate, DateTime(2000));
    expect(_key('bt-run'), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('limits route missing (older engine, 404): no note, nothing breaks', (tester) async {
    final _Page p = await _pump(tester);
    expect(_key('bt-limits-note'), findsNothing);
    expect(tester.takeException(), isNull);
    await p.h.dispose(tester);
  });

  testWidgets('result «نمودار» tab: the run\'s trades on the chart; a row zooms onto its trade', (tester) async {
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [runSummaryJson(id: 7)],
      details: {7: manualDetailJson(id: 7)},
    )..trades[7] = [tradeJson(0), tradeJson(1, netPnl: -30), tradeJson(2, netPnl: 120)];
    // Fake bars from 2021-10-04 cover the fixture trades (2023-02-01 .. 2023-02-05).
    final FakeChartDataSource bars = FakeChartDataSource(h1Count: 12000);
    final _Page p = await _pump(tester, engine: engine, chart: bars, limits: _limits);
    await tester.tap(_key('bt-side-runs'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));
    await tester.tap(_key('bt-run-7'));
    await settle(tester, 20);
    await tester.tap(_key('bt-tab-chart'));
    await tester.pumpAndSettle();

    final BacktestTradesChartState s = tester.state<BacktestTradesChartState>(find.byType(BacktestTradesChart));
    final ChartController c = s.chart;
    expect(c.symbol, 'XAUUSD.x');
    expect(c.from, DateTime.utc(2023, 1, 2), reason: 'the run period start');
    // tradeJson(2) exits on Saturday 2023-02-04 12:00, a time without a bar: it cannot be placed
    // truthfully and is left out (counted in the header).
    expect(c.data!.trades.map((m) => m.key.index), [0, 1]);
    expect(find.textContaining('3 معامله؛ 2 در بازه نمودار'), findsOneWidget);

    await tester.tap(_key('bt-trades-row-1'));
    await tester.pumpAndSettle();
    expect(c.selectedTradeKey, (window: 0, index: 1));
    expect(c.highlightIndex, c.data!.tradeByKey((window: 0, index: 1))!.entryIndex);
    expect(tester.takeException(), isNull);
    await p.h.dispose(tester);
  });
}
