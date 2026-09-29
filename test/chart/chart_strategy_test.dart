// Strategy selection on the chart: the selector's entries, the `strategy`
// passed to /chart/setups, no /chart/channel for a strategy without one, and
// the «این سیستم کانال ندارد» note instead of channel lines / values.

import 'package:alpha_trader/chart/backtest_range_request.dart';
import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/chart/chart_view.dart';
import 'package:alpha_trader/chart/widgets/chart_canvas.dart';
import 'package:alpha_trader/models/chart_models.dart';
import 'package:alpha_trader/models/strategy.dart';
import 'package:alpha_trader/services/engine_api.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:alpha_trader/widgets/strategy_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fake_chart_data_source.dart';

Widget _app(ChartController c) => MaterialApp(
      theme: MyThemes.getLightTheme(),
      locale: const Locale('fa', 'IR'),
      supportedLocales: const <Locale>[Locale('fa', 'IR'), Locale('en', 'US')],
      localizationsDelegates: const <LocalizationsDelegate<Object>>[
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      home: Scaffold(body: ChartView(controller: c)),
    );

void main() {
  group('controller', () {
    late FakeChartDataSource src;
    late ChartController c;

    setUp(() {
      src = FakeChartDataSource(h1Count: 1200);
      c = ChartController(source: src);
    });

    tearDown(() => c.dispose());

    test('init loads the strategy list; default stddev_channel with its channel and strategy query', () async {
      await c.init();
      expect(c.strategies.map((o) => o.name), ['stddev_channel', kFakePluginName]);
      expect(c.strategy, 'stddev_channel');
      expect(c.hasChannel, isTrue);
      expect(src.channelStrategies, ['stddev_channel']);
      expect(src.setupsStrategies, ['stddev_channel']);
      expect(c.data!.channelResult, isNotNull);
    });

    test('a plugin: no /chart/channel request, setups carry strategy, lines absent, window carries it', () async {
      await c.init();
      src.calls.clear();
      await c.setStrategy(kFakePluginName);

      expect(c.strategy, kFakePluginName);
      expect(c.hasChannel, isFalse);
      expect(src.calls.where((String s) => s.startsWith('channel')), isEmpty, reason: 'channel suppressed');
      expect(src.setupsStrategies.last, kFakePluginName);
      expect(c.state, ChartLoadState.ready);
      expect(c.data!.channelResult, isNull);
      expect(c.data!.channel.every((ChannelPoint? p) => p == null), isTrue);
      final SetupItem first = c.data!.setups.first.item;
      expect(first.line, isNull);
      expect(first.typeTitleFa, kFakePluginSetupTitleFa);
      expect(c.data!.setupsResult!.provenance.strategySource, StrategySource.plugin);
      expect(BacktestRangeRequest.of(c.data!.setupsResult)!.strategy, kFakePluginName);

      await c.setStrategy('stddev_channel');
      expect(src.channelStrategies.last, 'stddev_channel');
      expect(c.data!.channelResult, isNotNull);
    });

    test('a 409 channel_not_available is handled silently (no error state)', () async {
      // A strategy the UI believes has a channel, but the engine says it has none.
      final ChartController other = ChartController(source: _NoChannelSource());
      addTearDown(other.dispose);
      await other.init();
      expect(other.state, ChartLoadState.ready);
      expect(other.data!.channelResult, isNull);
    });

    test('strategy list failure: the chart still loads (default strategy, selector hidden)', () async {
      src.strategyOptions = null;
      await c.init();
      expect(c.state, ChartLoadState.ready);
      expect(c.strategies, isEmpty);
      expect(c.strategy, 'stddev_channel');
    });

    test('backtest chart of a plugin run: constructed with the run strategy, no channel request', () async {
      final ChartController bt = ChartController(source: src, showSetups: false, strategy: kFakePluginName);
      addTearDown(bt.dispose);
      await bt.openSeries(symbol: 'XAUUSD.x');
      expect(src.calls.where((String s) => s.startsWith('channel')), isEmpty);
      expect(bt.state, ChartLoadState.ready);
    });
  });

  group('view', () {
    Future<(ChartController, FakeChartDataSource)> pump(WidgetTester tester) async {
      tester.view.physicalSize = const Size(1600, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 1200);
      final ChartController c = ChartController(source: src);
      addTearDown(c.dispose);
      await tester.pumpWidget(_app(c));
      await tester.runAsync(c.init);
      await tester.pumpAndSettle();
      return (c, src);
    }

    testWidgets('selector in the toolbar (plugin badge); picking a plugin shows the no-channel note', (tester) async {
      final (ChartController c, FakeChartDataSource src) = await pump(tester);
      expect(find.byKey(const ValueKey<String>('chart-strategy')), findsOneWidget);
      expect(find.byKey(const ValueKey<String>('chart-no-channel')), findsNothing);

      await tester.tap(find.byKey(const ValueKey<String>('chart-strategy')));
      await tester.pumpAndSettle();
      expect(find.byType(PluginBadge), findsWidgets);
      await tester.tap(find.text(kFakePluginTitleFa).last);
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 30)));
      await tester.pumpAndSettle();

      expect(c.strategy, kFakePluginName);
      expect(src.setupsStrategies.last, kFakePluginName);
      expect(find.byKey(const ValueKey<String>('chart-no-channel')), findsOneWidget);
      expect(find.text(kNoChannelFa), findsOneWidget);
      expect(find.textContaining('$kFakePluginName v2 (پلاگین، 2d070ea08a45)'), findsOneWidget);

      // The candle panel shows the note instead of channel rows.
      final ChartCanvasState s = tester.state<ChartCanvasState>(find.byType(ChartCanvas));
      final int i = c.data!.length - 5;
      final Offset origin = tester.getTopLeft(find.byType(ChartCanvas));
      await tester.tapAt(origin + Offset(s.viewport!.xOf(i), s.priceScale!.yOf(c.data!.candles[i].close)));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey<String>('candle-info-panel')), findsOneWidget);
      expect(find.byKey(const ValueKey<String>('candle-info-no-channel')), findsOneWidget);
      expect(find.text('خط بالا'), findsNothing);
    });
  });
}

/// The fake, but /chart/channel answers like an engine for which the
/// strategy has no channel (409).
class _NoChannelSource extends FakeChartDataSource {
  _NoChannelSource() : super(h1Count: 1200);

  @override
  Future<ChannelResult> channel(String symbol, ChartTimeframe timeframe,
      {DateTime? from, DateTime? to, String? strategy}) async {
    throw const EngineApiException(EngineApiErrorKind.badStatus, 'این سیستم کانال ندارد.',
        statusCode: 409, code: 'channel_not_available');
  }
}
