import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/chart/chart_data.dart';
import 'package:alpha_trader/chart/chart_painter.dart';
import 'package:alpha_trader/chart/chart_viewport.dart';
import 'package:alpha_trader/models/chart_models.dart';
import 'package:alpha_trader/models/signal_models.dart';
import 'package:alpha_trader/screens/signals/signal_card.dart';
import 'package:alpha_trader/screens/signals/signal_mini_chart.dart';
import 'package:alpha_trader/theme/theme.dart';

import '../helpers/signal_fakes.dart';
import 'fake_chart_data_source.dart';

void main() {
  final DateTime conf = DateTime.utc(2021, 10, 26, 13);

  test('range: 5 days before the confirmation; active -> up to the latest; ended -> 2 days after', () {
    final DateTime now = DateTime.utc(2021, 11, 20);
    expect(signalChartRange(conf, active: true, now: now), (DateTime.utc(2021, 10, 21, 13), null));
    expect(signalChartRange(conf, active: false, now: now),
        (DateTime.utc(2021, 10, 21, 13), DateTime.utc(2021, 10, 28, 13)));
    expect(signalChartRange(conf, active: false, now: DateTime.utc(2021, 10, 27)).$2, isNull,
        reason: 'never a `to` in the future');
  });

  test('levels: indicative entry (dashed), SL, indicative TP (dashed) from the bar after the confirmation', () {
    final LiveSignal s = LiveSignal.fromJson(signalJson());
    final List<ChartPriceLevel> l = signalLevels(s, 40);
    expect([
      for (final ChartPriceLevel x in l) (x.kind, x.price, x.tag, x.fromIndex, x.dashed)
    ], [
      (ChartLevelKind.entry, 2064.68, 'Entry≈', 41, true),
      (ChartLevelKind.stopLoss, 2062.8835288730947, 'SL', 41, false),
      (ChartLevelKind.takeProfit, 2068.27294, 'TP≈', 41, true),
    ]);
    final LiveSignal noTp = LiveSignal.fromJson(signalJson()..['take_profit_indicative'] = null);
    expect(
        signalLevels(noTp, null).map((ChartPriceLevel x) => x.kind), [ChartLevelKind.entry, ChartLevelKind.stopLoss]);
    expect(signalLevels(noTp, null).first.fromIndex, isNull);
  });

  test('viewport: ~60 bars before the confirmation to the newest bar; scale covers every level', () async {
    final FakeChartDataSource src = FakeChartDataSource();
    final RatesResult rates = await src.rates('XAUUSD.x', ChartTimeframe.h1, from: conf.subtract(kMiniChartLookback));
    final ChartData data = ChartData.build(rates: rates, timeframe: ChartTimeframe.h1, digits: 2);
    final int ci = data.containingIndex(conf)!;
    final ChartViewport v = SignalChartCanvas.viewportFor(data, ci, 900);
    final IndexRange range = v.visibleRange!;
    expect(range.first, ci - kMiniChartBarsBefore);
    expect(range.last, data.length - 1);

    final ChartLayout layout = ChartLayout(const Size(976, 300));
    const List<ChartPriceLevel> far = [
      ChartPriceLevel(kind: ChartLevelKind.takeProfit, price: 2500, tag: 'TP≈'),
      ChartPriceLevel(kind: ChartLevelKind.stopLoss, price: 1500, tag: 'SL'),
    ];
    final PriceScale s = SignalChartCanvas.scaleFor(data, range, layout, far);
    expect(s.max, greaterThan(2500));
    expect(s.min, lessThan(1500));
    final PriceScale plain = SignalChartCanvas.scaleFor(data, range, layout, const []);
    expect(plain, autoscalePrice(data, range, top: s.top, bottom: s.bottom));
  });

  testWidgets('a card with its chart open renders in the dark theme too (semantic colours)', (tester) async {
    tester.view.physicalSize = const Size(1400, 1200);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final FakeChartDataSource src = FakeChartDataSource();
    await tester.pumpWidget(MaterialApp(
      theme: MyThemes.getDarkTheme(),
      locale: const Locale('fa', 'IR'),
      supportedLocales: const [Locale('fa', 'IR'), Locale('en', 'US')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      home: Scaffold(
        body: SingleChildScrollView(
          child: SignalCard(
            signal: LiveSignal.fromJson(signalJson(direction: 'sell', expires: null)),
            source: src,
            digits: 2,
            initiallyExpanded: true,
            clock: () => DateTime.utc(2021, 10, 26, 14),
          ),
        ),
      ),
    ));
    for (int i = 0; i < 6; i++) {
      await tester.pump(const Duration(milliseconds: 10));
    }
    expect(tester.takeException(), isNull);
    expect(find.byType(SignalMiniChart), findsOneWidget);
    expect(find.text(kExpiryPendingFa), findsOneWidget);
    expect(Directionality.of(tester.element(find.byType(SignalCard))), TextDirection.rtl);
    expect(Directionality.of(tester.element(find.byType(SignalChartCanvas).first)), TextDirection.rtl,
        reason: 'the canvas sets LTR for its own subtree only');
  });
}
