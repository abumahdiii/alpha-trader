import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/backtest_models.dart';
import 'package:alpha_trader/screens/backtest/distribution_chart.dart';
import 'package:alpha_trader/screens/backtest/equity_chart.dart';
import 'package:alpha_trader/theme/theme.dart';

Widget _host(Widget child, {bool dark = false}) => MaterialApp(
      theme: dark ? MyThemes.getDarkTheme() : MyThemes.getLightTheme(),
      home: Directionality(
        textDirection: TextDirection.rtl,
        child: Scaffold(body: Center(child: SizedBox(width: 600, height: 300, child: child))),
      ),
    );

Future<TestGesture> _hover(WidgetTester tester, Offset at) async {
  final TestGesture mouse = await tester.createGesture(kind: PointerDeviceKind.mouse);
  await mouse.addPointer(location: Offset.zero);
  addTearDown(mouse.removePointer);
  await mouse.moveTo(at);
  await tester.pump();
  return mouse;
}

final List<BacktestEquityPoint> _points = [
  for (int i = 0; i < 10; i++)
    BacktestEquityPoint(
      windowIndex: 0,
      time: DateTime.utc(2024, 1, 1 + i),
      balance: 2500.0 + i * 10,
      equity: 2500.0 + i * 10 - (i.isOdd ? 15 : 0),
    ),
];

void main() {
  group('histogramBins (display grouping)', () {
    test('every value lands in exactly one bin', () {
      final List<HistogramBin> bins = histogramBins([-100, -20, 0, 5, 20, 40, 200, 150, 90]);
      expect(bins.fold<int>(0, (int s, HistogramBin b) => s + b.count), 9);
      expect(bins.first.from, -100);
      expect(bins.last.to, 200);
      expect(bins.length, 3);
    });

    test('identical values -> one bin; empty -> none', () {
      expect(histogramBins([5, 5, 5]).single.count, 3);
      expect(histogramBins(const []), isEmpty);
    });
  });

  testWidgets('equity chart: light and dark themes paint; hover shows local + UTC time and both values',
      (tester) async {
    for (final bool dark in [false, true]) {
      await tester.pumpWidget(_host(EquityChart(points: _points), dark: dark));
      expect(find.byType(CustomPaint), findsWidgets);
      expect(tester.takeException(), isNull);
    }
    final Rect r = tester.getRect(find.byType(EquityChart));
    await _hover(tester, Offset(r.left + 8, r.center.dy)); // the left edge = the first (oldest) point
    expect(find.byKey(const ValueKey<String>('bt-equity-readout')), findsOneWidget);
    expect(find.text('UTC: 2024.01.01 00:00 UTC'), findsOneWidget);
    expect(find.text('موجودی: 2,500.00 \$'), findsOneWidget);
  });

  testWidgets('distribution chart: hover over the leftmost bar reads its range and count', (tester) async {
    await tester.pumpWidget(_host(const DistributionChart(values: [-100, -80, 10, 50, 200], mean: 16, median: 10)));
    expect(tester.takeException(), isNull);
    final Rect r = tester.getRect(find.byType(DistributionChart));
    await _hover(tester, Offset(r.left + 80, r.center.dy));
    expect(find.byKey(const ValueKey<String>('bt-distribution-readout')), findsOneWidget);
    expect(find.textContaining('2 پنجره'), findsOneWidget);
  });
}
