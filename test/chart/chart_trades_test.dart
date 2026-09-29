// Backtest trades on the candlestick chart: placement on the bars, culling,
// painting of every exit kind (light + dark), hit-test priority and the
// autoscale / fit geometry. Trade values are fixtures drawn as given.

import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/chart/chart_data.dart';
import 'package:alpha_trader/chart/chart_painter.dart';
import 'package:alpha_trader/chart/chart_viewport.dart';
import 'package:alpha_trader/chart/widgets/chart_canvas.dart';
import 'package:alpha_trader/models/backtest_models.dart';
import 'package:alpha_trader/models/chart_models.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fake_chart_data_source.dart';

const String _sym = 'XAUUSD.x';

Future<ChartData> _data(
  FakeChartDataSource src,
  List<BacktestTrade> trades, {
  ChartTimeframe tf = ChartTimeframe.h1,
  bool withSetups = false,
  int runId = 7,
}) async {
  final RatesResult rates = await src.rates(_sym, tf);
  final SetupsResult? setups = withSetups ? await src.setups(_sym) : null;
  return ChartData.build(
    rates: rates,
    timeframe: tf,
    setupsResult: setups,
    tradeOverlay: TradeOverlay(runId: runId, trades: trades),
    digits: 2,
  );
}

class _Taps {
  final List<String> events = <String>[];
}

Future<ChartCanvasState> _pumpCanvas(
  WidgetTester tester,
  ChartData data, {
  ThemeData? theme,
  double width = 1200,
  _Taps? taps,
  TradeKey? selected,
}) async {
  tester.view.physicalSize = Size(width, 700);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(MaterialApp(
    theme: theme ?? MyThemes.getLightTheme(),
    home: Scaffold(
      body: ChartCanvas(
        data: data,
        viewRequest: ChartViewRequest.none,
        selectedTradeKey: selected,
        onSetupTap: (SetupMark m) => taps?.events.add('setup ${m.item.id}'),
        onTradeTap: (TradeMark m) => taps?.events.add('trade ${m.key.window}:${m.key.index}'),
        onCandleTap: (int i) => taps?.events.add('candle $i'),
        onEmptyTap: () => taps?.events.add('empty'),
      ),
    ),
  ));
  await tester.pump();
  return tester.state<ChartCanvasState>(find.byType(ChartCanvas));
}

/// Trades overlapping [r], counted by brute force (reference for culling).
int _overlapping(ChartData d, IndexRange r) =>
    d.trades.where((TradeMark m) => m.entryIndex <= r.last && m.exitIndex >= r.first).length;

void main() {
  group('ChartData trade placement', () {
    test('entry / exit bars by UTC time, sorted, span tracked; unplaced trades dropped', () async {
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 400);
      final List<Candle> b = src.bars(_sym, ChartTimeframe.h1);
      final BacktestTrade later = fakeTrade(b, 1, 300, 330);
      final BacktestTrade early = fakeTrade(b, 0, 100, 103, direction: 'sell', exitReason: 'sl');
      final BacktestTrade outside = BacktestTrade(
        windowIndex: 0,
        tradeIndex: 2,
        direction: 'buy',
        entryTime: b.last.time.add(const Duration(days: 30)),
        exitBarTime: b.last.time.add(const Duration(days: 31)),
        entry: 1,
      );
      final ChartData d = await _data(src, <BacktestTrade>[later, early, outside]);
      expect(d.trades.map((TradeMark m) => m.key), <TradeKey>[(window: 0, index: 0), (window: 0, index: 1)]);
      expect(d.trades.first.entryIndex, 100);
      expect(d.trades.first.exitIndex, 103);
      expect(d.trades.last.exitIndex, 330);
      expect(d.maxTradeSpan, 30);
      expect(d.tradeByKey((window: 0, index: 1))!.entryIndex, 300);
      expect(d.tradeOverlay!.trades, hasLength(3));
      // Culling helper: only the trades overlapping the bars.
      expect(d.tradesOverlapping(0, 99), isEmpty);
      expect(d.tradesOverlapping(103, 103).single.key, (window: 0, index: 0));
      expect(d.tradesOverlapping(320, 399).single.key, (window: 0, index: 1));
      expect(d.tradesOverlapping(0, 399), hasLength(2));
    });

    test('H4: the bars containing the H1 entry / exit times', () async {
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 400);
      final List<Candle> h1 = src.bars(_sym, ChartTimeframe.h1);
      final BacktestTrade t = fakeTrade(h1, 0, 101, 110); // 101 = 05:00 of day 5 -> the 04:00 H4 bar
      final ChartData d = await _data(src, <BacktestTrade>[t], tf: ChartTimeframe.h4);
      final TradeMark m = d.trades.single;
      final DateTime entryBar = d.candles[m.entryIndex].time;
      expect(entryBar.isAfter(t.entryTime!), isFalse);
      expect(t.entryTime!.difference(entryBar), lessThan(const Duration(hours: 4)));
      final DateTime exitBar = d.candles[m.exitIndex].time;
      expect(t.exitBarTime!.difference(exitBar), lessThan(const Duration(hours: 4)));
      expect(d.containingIndex(entryBar.add(const Duration(hours: 3, minutes: 59))), m.entryIndex);
    });

    test('exit kinds from the engine reason; gap exits flagged', () async {
      expect(TradeExitKind.parse('tp'), TradeExitKind.takeProfit);
      expect(TradeExitKind.parse('tp_gap'), TradeExitKind.takeProfit);
      expect(TradeExitKind.parse('sl'), TradeExitKind.stopLoss);
      expect(TradeExitKind.parse('sl_gap'), TradeExitKind.stopLoss);
      expect(TradeExitKind.parse('end_of_period'), TradeExitKind.endOfPeriod);
      expect(TradeExitKind.parse(null), TradeExitKind.other);
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 50);
      final List<Candle> b = src.bars(_sym, ChartTimeframe.h1);
      final ChartData d = await _data(src, <BacktestTrade>[
        fakeTrade(b, 0, 10, 12, exitReason: 'sl_gap'),
        fakeTrade(b, 1, 20, 22, exitReason: 'tp'),
      ]);
      expect(d.trades[0].gapExit, isTrue);
      expect(d.trades[1].gapExit, isFalse);
    });

    test('withTrades keeps the bars and replaces only the overlay', () async {
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 100);
      final List<Candle> b = src.bars(_sym, ChartTimeframe.h1);
      final ChartData d = await _data(src, <BacktestTrade>[fakeTrade(b, 0, 10, 12)]);
      final ChartData none = d.withTrades(null);
      expect(identical(none.rates, d.rates), isTrue);
      expect(none.trades, isEmpty);
      expect(none.length, d.length);
    });
  });

  group('geometry', () {
    test('fitBars zooms so about n bars fill the plot (clamped)', () {
      final ChartViewport v = ChartViewport(barCount: 1000, width: 800);
      expect(v.fitBars(40).barWidth, 20);
      expect(v.fitBars(40).visibleBars, closeTo(40, 1e-9));
      expect(v.fitBars(4).barWidth, ChartViewport.maxBarWidth);
      expect(v.fitBars(100000).barWidth, ChartViewport.minBarWidth);
      expect(v.fitBars(40).centerOn(500).visibleRange!.contains(500), isTrue);
    });

    test('autoscale includes the levels of trades overlapping the range', () async {
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 200);
      final List<Candle> b = src.bars(_sym, ChartTimeframe.h1);
      final BacktestTrade t = BacktestTrade(
        windowIndex: 0,
        tradeIndex: 0,
        direction: 'buy',
        entryTime: b[150].time,
        exitBarTime: b[160].time,
        entry: b[150].open,
        stopLoss: 1900,
        takeProfit: 2300,
        exitPrice: 2300,
        exitReason: 'tp',
      );
      final ChartData d = await _data(src, <BacktestTrade>[t]);
      final PriceScale withTrade = autoscalePrice(d, const IndexRange(155, 199), top: 0, bottom: 500);
      expect(withTrade.max, greaterThan(2300));
      expect(withTrade.min, lessThan(1900));
      final PriceScale away = autoscalePrice(d, const IndexRange(170, 199), top: 0, bottom: 500);
      expect(away.max, lessThan(2100), reason: 'a trade that ended before the range does not stretch it');
    });
  });

  group('painting', () {
    for (final (String name, ThemeData theme) in <(String, ThemeData)>[
      ('light', MyThemes.getLightTheme()),
      ('dark', MyThemes.getDarkTheme()),
    ]) {
      testWidgets('every direction / exit kind paints without errors ($name)', (WidgetTester tester) async {
        final FakeChartDataSource src = FakeChartDataSource(h1Count: 400);
        final List<Candle> b = src.bars(_sym, ChartTimeframe.h1);
        final List<BacktestTrade> trades = <BacktestTrade>[
          fakeTrade(b, 0, 290, 296, direction: 'buy', exitReason: 'tp'),
          fakeTrade(b, 1, 300, 305, direction: 'sell', exitReason: 'sl'),
          fakeTrade(b, 2, 310, 312, direction: 'buy', exitReason: 'sl_gap'),
          fakeTrade(b, 3, 320, 330, direction: 'sell', exitReason: 'tp_gap'),
          fakeTrade(b, 4, 340, 399, direction: 'buy', exitReason: 'end_of_period'),
          // Engine fields missing: drawn as far as they go.
          BacktestTrade(
            windowIndex: 0,
            tradeIndex: 5,
            direction: 'sell',
            entryTime: b[360].time,
            exitBarTime: b[362].time,
            entry: b[360].open,
          ),
        ];
        final ChartData d = await _data(src, trades);
        expect(d.trades, hasLength(6));
        final ChartCanvasState s = await _pumpCanvas(tester, d, theme: theme, selected: (window: 0, index: 3));
        expect(tester.takeException(), isNull);
        final IndexRange r = s.viewport!.visibleRange!;
        expect(r.contains(290), isTrue, reason: 'the newest ~140 bars are visible');
        expect(ChartPainter.debugLastPaintedTrades, 6);
        expect(ChartPainter.debugLastPaintedTrades, _overlapping(d, r));
      });
    }

    testWidgets('~700 trades over 31k bars: only the visible ones are painted', (WidgetTester tester) async {
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 31000);
      final List<Candle> b = src.bars(_sym, ChartTimeframe.h1);
      final List<BacktestTrade> trades = <BacktestTrade>[
        for (int i = 0; i < 700; i++)
          fakeTrade(b, i, 200 + i * 44, 200 + i * 44 + 1 + i % 20,
              direction: i.isEven ? 'buy' : 'sell',
              exitReason: const <String>['tp', 'sl', 'sl_gap', 'tp_gap', 'end_of_period'][i % 5]),
      ];
      final ChartData d = await _data(src, trades);
      expect(d.trades, hasLength(700));
      final ChartCanvasState s = await _pumpCanvas(tester, d);
      final IndexRange r = s.viewport!.visibleRange!;
      expect(ChartPainter.debugLastPaintedTrades, _overlapping(d, r));
      expect(ChartPainter.debugLastPaintedTrades, lessThan(10), reason: '~140 bars on screen of 31000');
      expect(ChartPainter.debugLastPaintedTrades, greaterThan(0));

      // Zoomed far out: still only the trades of the ~1000+ visible bars.
      final TestPointer mouse = TestPointer(1, PointerDeviceKind.mouse);
      await tester.sendEventToBinding(mouse.hover(tester.getCenter(find.byType(ChartCanvas))));
      for (int k = 0; k < 20; k++) {
        await tester.sendEventToBinding(mouse.scroll(const Offset(0, 100)));
        await tester.pump();
      }
      final IndexRange wide = s.viewport!.visibleRange!;
      expect(wide.length, greaterThan(1000));
      expect(ChartPainter.debugLastPaintedTrades, _overlapping(d, wide));
      expect(ChartPainter.debugLastPaintedTrades, lessThan(40));
    });
  });

  group('hit-test', () {
    testWidgets('setup marker > trade marker > candle', (WidgetTester tester) async {
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 400);
      final List<Candle> b = src.bars(_sym, ChartTimeframe.h1);
      final int setupBar = src.setupIndices()[3]; // rejected buy: arrow under the low
      final BacktestTrade overlapping = BacktestTrade(
        windowIndex: 0,
        tradeIndex: 0,
        direction: 'buy',
        entryTime: b[setupBar].time,
        exitBarTime: b[setupBar + 4].time,
        entry: b[setupBar].low, // entry arrow right on top of the setup arrow
        exitPrice: b[setupBar + 4].close,
        stopLoss: b[setupBar].low - 3,
        takeProfit: b[setupBar].low + 6,
        exitReason: 'tp',
      );
      final ChartData d = await _data(src, <BacktestTrade>[overlapping], withSetups: true);
      final SetupMark setup = d.setups.firstWhere((SetupMark m) => m.barIndex == setupBar);
      final _Taps taps = _Taps();
      final ChartCanvasState s = await _pumpCanvas(tester, d, taps: taps);
      final Offset origin = tester.getTopLeft(find.byType(ChartCanvas));
      final ChartViewport v = s.viewport!;
      final PriceScale sc = s.priceScale!;
      final TradeMark t = d.trades.single;

      // Both markers are under this point; the setup wins.
      final Offset setupTip = SetupMarkerGeometry.tip(setup, d, v, sc);
      expect(hitTestTrade(d, v, sc, setupTip + const Offset(0, 2)), isNotNull, reason: 'the trade is hit too');
      await tester.tapAt(origin + setupTip + const Offset(0, 2));
      await tester.pump();
      expect(taps.events.last, 'setup ${setup.item.id}');

      // The exit marker alone: the trade.
      final Offset exit = TradeMarkerGeometry.exitCenter(t, v, sc)!;
      await tester.tapAt(origin + exit);
      await tester.pump();
      expect(taps.events.last, 'trade 0:0');

      // A plain candle.
      final int plain = setupBar - 30;
      await tester.tapAt(origin + Offset(v.xOf(plain), sc.yOf(b[plain].close)));
      await tester.pump();
      expect(taps.events.last, 'candle $plain');
    });
  });
}
