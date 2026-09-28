// The backtest result's «نمودار» tab: the run's trades on the candlestick
// chart (fake bars), a row click zooms onto the trade (reloading around it
// when needed), a marker click opens the trade's details.

import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/chart/chart_data.dart';
import 'package:alpha_trader/chart/chart_painter.dart';
import 'package:alpha_trader/chart/chart_viewport.dart';
import 'package:alpha_trader/chart/widgets/chart_canvas.dart';
import 'package:alpha_trader/models/backtest_models.dart';
import 'package:alpha_trader/models/chart_models.dart';
import 'package:alpha_trader/screens/backtest/backtest_trades_chart.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';

import '../chart/fake_chart_data_source.dart';

const String _sym = 'XAUUSD.x';

Finder _key(String k) => find.byKey(ValueKey<String>(k));

class _Fixture {
  _Fixture() {
    bars = src.bars(_sym, ChartTimeframe.h1);
    trades = <BacktestTrade>[
      fakeTrade(bars, 0, 30, 40, direction: 'buy', exitReason: 'tp'),
      fakeTrade(bars, 1, 1800, 1830, direction: 'sell', exitReason: 'sl_gap'),
    ];
  }

  final FakeChartDataSource src = FakeChartDataSource(h1Count: 2000);
  late final List<Candle> bars;
  late final List<BacktestTrade> trades;
}

Future<BacktestTradesChartState> _pump(WidgetTester tester, _Fixture f, {ThemeData? theme}) async {
  tester.view.physicalSize = const Size(1600, 1000);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(MaterialApp(
    theme: theme ?? MyThemes.getLightTheme(),
    locale: const Locale('fa', 'IR'),
    supportedLocales: const <Locale>[Locale('fa', 'IR'), Locale('en', 'US')],
    localizationsDelegates: const <LocalizationsDelegate<Object>>[
      GlobalMaterialLocalizations.delegate,
      GlobalWidgetsLocalizations.delegate,
      GlobalCupertinoLocalizations.delegate,
    ],
    home: Scaffold(
      body: BacktestTradesChart(
        source: f.src,
        runId: 7,
        symbol: _sym,
        trades: f.trades,
        periodStart: f.bars.first.time,
        periodEnd: f.bars.last.time.add(const Duration(hours: 1)),
        digits: 2,
      ),
    ),
  ));
  await tester.pumpAndSettle();
  return tester.state<BacktestTradesChartState>(find.byType(BacktestTradesChart));
}

ChartCanvasState _canvas(WidgetTester tester) => tester.state<ChartCanvasState>(find.byType(ChartCanvas));

/// Asserts the chart shows [t] whole, centred on its entry .. exit bars.
void _expectCentredOn(WidgetTester tester, ChartController c, BacktestTrade t) {
  final ChartData d = c.data!;
  final TradeMark m = d.tradeByKey(tradeKeyOf(t))!;
  final ChartViewport v = _canvas(tester).viewport!;
  final IndexRange r = v.visibleRange!;
  expect(r.contains(m.entryIndex) && r.contains(m.exitIndex), isTrue, reason: '$r vs ${m.entryIndex}..${m.exitIndex}');
  final double centre = v.startIndex + v.visibleBars / 2;
  expect(centre, closeTo((m.entryIndex + m.exitIndex) / 2 + 0.5, 1.0));
  expect(r.length, lessThan(200), reason: 'zoomed onto the trade, not the whole range');
}

void main() {
  testWidgets('opens on the period start with the trades overlay; no setups are requested', (tester) async {
    final _Fixture f = _Fixture();
    final BacktestTradesChartState s = await _pump(tester, f);
    final ChartController c = s.chart;
    expect(c.state, ChartLoadState.ready);
    expect(c.symbol, _sym);
    expect(c.from, f.bars.first.time);
    expect(c.to, f.bars.first.time.add(kBacktestChartInitialWindow));
    expect(f.src.calls.where((String x) => x.startsWith('setups')), isEmpty);
    expect(c.data!.trades.single.key, (window: 0, index: 0), reason: 'trade B is outside the first 60 days');
    expect(_key('bt-chart-trades-pane'), findsOneWidget);
    expect(find.textContaining('2 معامله؛ 1 در بازه نمودار'), findsOneWidget);
    expect(_key('setups-pane'), findsNothing, reason: 'the trades table replaces the setups pane');
    // The symbol is fixed to the run's.
    final DropdownButton<String> dd = tester.widget<DropdownButton<String>>(_key('chart-symbol'));
    expect(dd.onChanged, isNull);
    expect(tester.takeException(), isNull);
  });

  testWidgets('row click: reloads around the trade, zooms and centres it, selects it', (tester) async {
    final _Fixture f = _Fixture();
    final BacktestTradesChartState s = await _pump(tester, f);
    final ChartController c = s.chart;

    await tester.tap(_key('bt-trades-row-1'));
    await tester.pumpAndSettle();
    expect(c.selectedTradeKey, (window: 0, index: 1));
    expect(c.data!.covers(f.trades[1].entryTime!) && c.data!.covers(f.trades[1].exitBarTime!), isTrue);
    _expectCentredOn(tester, c, f.trades[1]);
    expect(ChartPainter.debugLastPaintedTrades, greaterThanOrEqualTo(1));

    // Back to the first trade: outside the new range again -> another reload.
    await tester.tap(_key('bt-trades-row-0'));
    await tester.pumpAndSettle();
    expect(c.selectedTradeKey, (window: 0, index: 0));
    _expectCentredOn(tester, c, f.trades[0]);

    // A new overlay (e.g. another window) keeps the view.
    final ChartViewport before = _canvas(tester).viewport!;
    c.setTradeOverlay(TradeOverlay(runId: 7, trades: <BacktestTrade>[f.trades[0]]));
    await tester.pumpAndSettle();
    expect(_canvas(tester).viewport, before);
    expect(c.selectedTradeKey, (window: 0, index: 0));
    expect(tester.takeException(), isNull);
  });

  for (final (String name, ThemeData theme) in <(String, ThemeData)>[
    ('light', MyThemes.getLightTheme()),
    ('dark', MyThemes.getDarkTheme()),
  ]) {
    testWidgets('marker click opens the trade details ($name)', (tester) async {
      final _Fixture f = _Fixture();
      final BacktestTradesChartState s = await _pump(tester, f, theme: theme);
      await tester.tap(_key('bt-trades-row-1'));
      await tester.pumpAndSettle();

      final ChartCanvasState cs = _canvas(tester);
      final TradeMark m = s.chart.data!.tradeByKey((window: 0, index: 1))!;
      final Offset exit = TradeMarkerGeometry.exitCenter(m, cs.viewport!, cs.priceScale!)!;
      s.chart.selectTrade(null);
      await tester.tapAt(tester.getTopLeft(find.byType(ChartCanvas)) + exit);
      await tester.pumpAndSettle();
      Finder inDialog(String text) => find.descendant(of: find.byType(Dialog), matching: find.text(text));
      expect(inDialog('معامله 2'), findsOneWidget, reason: 'the details dialog of trade #2');
      expect(inDialog('دلیل آزمایشی معامله 1'), findsOneWidget);
      expect(inDialog('دلیل خروج sl_gap'), findsOneWidget);
      expect(s.chart.selectedTradeKey, (window: 0, index: 1));
      expect(tester.takeException(), isNull);
    });
  }

  test('initial range: period start, capped; around the first trade without a period', () {
    final DateTime a = DateTime.utc(2023, 1, 2);
    expect(backtestChartInitialRange(a, DateTime.utc(2023, 1, 20), const <BacktestTrade>[]),
        (a, DateTime.utc(2023, 1, 20)));
    expect(backtestChartInitialRange(a, DateTime.utc(2024, 1, 1), const <BacktestTrade>[]),
        (a, a.add(kBacktestChartInitialWindow)));
    final BacktestTrade t = BacktestTrade(windowIndex: 0, tradeIndex: 0, entryTime: DateTime.utc(2023, 5, 10));
    expect(backtestChartInitialRange(null, null, <BacktestTrade>[t]).$1, DateTime.utc(2023, 5, 7));
    expect(backtestChartInitialRange(null, null, const <BacktestTrade>[]), (null, null));
  });
}
