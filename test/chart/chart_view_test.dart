import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/chart/chart_data.dart';
import 'package:alpha_trader/services/engine_api.dart';
import 'package:alpha_trader/chart/chart_format.dart';
import 'package:alpha_trader/models/chart_models.dart';
import 'package:alpha_trader/chart/chart_painter.dart';
import 'package:alpha_trader/chart/chart_view.dart';
import 'package:alpha_trader/chart/chart_viewport.dart';
import 'package:alpha_trader/chart/widgets/chart_canvas.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:flutter/gestures.dart';
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

Future<ChartController> _pump(WidgetTester tester, FakeChartDataSource src, {bool fullRange = false}) async {
  tester.view.physicalSize = const Size(1600, 900);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final ChartController c = ChartController(source: src);
  addTearDown(c.dispose);
  await tester.pumpWidget(_app(c));
  await tester.runAsync(c.init);
  if (fullRange) {
    final List<Candle> all = src.bars('XAUUSD.x', ChartTimeframe.h1);
    c.setRange(from: all.first.time, to: all.last.time);
    await tester.runAsync(c.load);
  }
  await tester.pumpAndSettle();
  return c;
}

ChartCanvasState _canvas(WidgetTester tester) => tester.state<ChartCanvasState>(find.byType(ChartCanvas));

/// Global position of a bar centre at price [price].
Offset _at(WidgetTester tester, int index, double price) {
  final ChartCanvasState s = _canvas(tester);
  final Offset origin = tester.getTopLeft(find.byType(ChartCanvas));
  return origin + Offset(s.viewport!.xOf(index), s.priceScale!.yOf(price));
}

void main() {
  testWidgets('31k H1 bars: builds, paints only the visible range, pans and zooms', (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 31000);
    final ChartController c = await _pump(tester, src, fullRange: true);
    expect(c.data!.length, 31000);
    expect(find.byType(ChartCanvas), findsOneWidget);
    final ChartViewport v0 = _canvas(tester).viewport!;
    expect(v0.visibleRange!.last, 30999);
    expect(ChartPainter.debugLastPaintedBars, v0.visibleRange!.length);
    expect(ChartPainter.debugLastPaintedBars, lessThan(200), reason: 'culling: ~150 of 31000 bars painted');

    // Drag right by 400 px = 50 bars older.
    await tester.drag(find.byType(ChartCanvas), const Offset(400, 0));
    await tester.pumpAndSettle();
    final ChartViewport v1 = _canvas(tester).viewport!;
    expect(v1.startIndex, lessThan(v0.startIndex - 40));

    // Mouse wheel up = zoom in around the cursor.
    final Offset centre = tester.getCenter(find.byType(ChartCanvas));
    final TestPointer mouse = TestPointer(1, PointerDeviceKind.mouse);
    await tester.sendEventToBinding(mouse.hover(centre));
    await tester.sendEventToBinding(mouse.scroll(const Offset(0, -100)));
    await tester.pump();
    final ChartViewport v2 = _canvas(tester).viewport!;
    expect(v2.barWidth, greaterThan(v1.barWidth));
    final double localX = centre.dx - tester.getTopLeft(find.byType(ChartCanvas)).dx;
    expect(v2.slotAt(localX), closeTo(v1.slotAt(localX), 1e-6));
    expect(ChartPainter.debugLastPaintedBars, lessThan(200));

    // Reset zoom.
    await tester.tap(find.byKey(const ValueKey<String>('chart-reset-zoom')));
    await tester.pumpAndSettle();
    expect(_canvas(tester).viewport!.startIndex, v0.startIndex);
  });

  testWidgets('hover shows server time, UTC and channel values of that bar', (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 1200);
    final ChartController c = await _pump(tester, src);
    final ChartData d = c.data!;
    final int i = d.length - 30;
    final TestGesture g = await tester.createGesture(kind: PointerDeviceKind.mouse);
    final Offset pos = _at(tester, i, d.candles[i].close);
    await g.addPointer(location: pos + const Offset(0, 30));
    await g.moveTo(pos);
    await tester.pump();
    final Candle bar = d.candles[i];
    expect(find.text('کندل زیر نشانگر'), findsOneWidget);
    expect(find.text(bar.serverTime!), findsOneWidget);
    expect(find.text(formatMt5Time(bar.time)), findsWidgets); // panel + crosshair label is painted, not text
    expect(bar.serverTime, isNot(formatMt5Time(bar.time)));
    expect(find.text(formatChartPrice(bar.open, 2)), findsWidgets);
    final ChannelPoint p = d.channel[i]!;
    expect(find.text(formatChartPrice(p.upper, 2)), findsOneWidget);
    expect(find.text(formatChartPrice(p.mid, 2)), findsOneWidget);
    expect(find.text(formatChartPrice(p.lower, 2)), findsOneWidget);
    expect(find.text('ATR H1'), findsOneWidget);
    expect(find.text(formatChartPrice(p.atrH4, 2)), findsOneWidget);
    await g.removePointer();
  });

  testWidgets('clicking a setup marker opens its details', (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 1200);
    final ChartController c = await _pump(tester, src);
    final ChartCanvasState s = _canvas(tester);
    final SetupMark m = c.data!.setups.firstWhere((SetupMark m) => m.item.status == SetupStatus.accepted);
    final Offset tip = SetupMarkerGeometry.tip(m, c.data!, s.viewport!, s.priceScale!);
    final Rect r = SetupMarkerGeometry.bounds(tip, m.item.direction);
    await tester.tapAt(tester.getTopLeft(find.byType(ChartCanvas)) + r.center);
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey<String>('setup-details')), findsOneWidget);
    expect(c.selectedSetupId, m.item.id);
    expect(find.text(m.item.reasonFa), findsOneWidget);
    expect(find.text('این فقط پیشنهاد است؛ برنامه هیچ سفارشی ثبت نمی‌کند.'), findsOneWidget);
  });

  testWidgets('setup table row tap jumps the chart to the setup and highlights it', (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 1200);
    final ChartController c = await _pump(tester, src, fullRange: true);
    final SetupMark mid = c.data!.setups.first; // bar 600, far from the visible newest bars
    expect(_canvas(tester).viewport!.visibleRange!.contains(mid.barIndex), isFalse);
    await tester.tap(find.byKey(ValueKey<String>('setup-row-${mid.item.id}')));
    await tester.pumpAndSettle();
    final ChartViewport v = _canvas(tester).viewport!;
    expect(c.selectedSetupId, mid.item.id);
    expect(c.highlightIndex, mid.barIndex);
    expect(v.xOf(mid.barIndex), closeTo(v.width / 2, v.barWidth));
  });

  testWidgets('data panel: gap tap jumps (reloading around an old gap)', (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 1200);
    final ChartController c = await _pump(tester, src);
    await tester.tap(find.text('اطلاعات داده'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey<String>('data-info-panel')), findsOneWidget);
    expect(find.text('us_dst(2)'), findsOneWidget);
    // gap-row-0 is the old "missing" gap (weekends are filtered out by default).
    final Gap gap = c.dataInfo!.gaps.gaps.first;
    expect(gap.kind, GapKind.missing);
    expect(c.data!.covers(gap.after), isFalse);
    await tester.tap(find.byKey(const ValueKey<String>('gap-row-0')));
    await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
    await tester.pumpAndSettle();
    expect(c.data!.covers(gap.after), isTrue);
    expect(c.data!.candles[c.highlightIndex!].time, gap.after);
    final ChartViewport v = _canvas(tester).viewport!;
    expect(v.visibleRange!.contains(c.highlightIndex!), isTrue);
  });

  testWidgets('update button: 409 shows the engine message and the explanation', (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 600)
      ..updateError = const EngineApiException(EngineApiErrorKind.badStatus, 'کش داده آزمایشی است.',
          statusCode: 409, code: 'not_incremental');
    await _pump(tester, src);
    await tester.tap(find.text('اطلاعات داده'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey<String>('update-from-mt5')));
    await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
    await tester.pumpAndSettle();
    expect(find.text('کش داده آزمایشی است.'), findsOneWidget);
    expect(find.text(updateErrorExplanationFa('not_incremental')!), findsOneWidget);
  });

  testWidgets('empty and error states', (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 300)
      ..error = const EngineApiException(EngineApiErrorKind.connection, 'اتصال به موتور برقرار نشد.');
    final ChartController c = await _pump(tester, src);
    expect(find.text(kEngineUnavailableFa), findsOneWidget);
    expect(find.text('تلاش دوباره'), findsOneWidget);

    src.error = null;
    await tester.runAsync(c.init);
    c.setRange(from: DateTime.utc(2000), to: DateTime.utc(2000, 1, 2));
    await tester.runAsync(c.load);
    await tester.pumpAndSettle();
    expect(c.state, ChartLoadState.empty);
    expect(find.byKey(const ValueKey<String>('chart-state-message')), findsOneWidget);
    expect(find.byType(ChartCanvas), findsNothing);
  });

  testWidgets('H4: setups table explains setups are H1-only', (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 1200);
    final ChartController c = await _pump(tester, src);
    await tester.tap(find.text('H4'));
    await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
    await tester.pumpAndSettle();
    expect(c.timeframe, ChartTimeframe.h4);
    expect(find.text('ستاپ‌ها روی چارت H1 نمایش داده می‌شوند.'), findsOneWidget);
    expect(find.byType(ChartCanvas), findsOneWidget);
  });
}
