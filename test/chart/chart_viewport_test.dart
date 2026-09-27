import 'package:alpha_trader/chart/chart_data.dart';
import 'package:alpha_trader/chart/chart_models.dart';
import 'package:alpha_trader/chart/chart_viewport.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fake_chart_data_source.dart';

void main() {
  group('ChartViewport', () {
    test('initial viewport shows the newest bars with the right pad', () {
      final ChartViewport v = ChartViewport(barCount: 31000, width: 1000);
      expect(v.barWidth, 8);
      expect(v.visibleBars, 125);
      expect(v.maxStart, 31000 + 3 - 125);
      expect(v.startIndex, 30878);
      expect(v.visibleRange, const IndexRange(30878, 30999));
    });

    test('culling range only covers bars overlapping the plot', () {
      final ChartViewport v = ChartViewport(barCount: 31000, width: 1000, startIndex: 100.5);
      // Slot 100 is half visible at the left edge; 225 half visible at the right.
      expect(v.visibleRange, const IndexRange(100, 225));
      expect(v.visibleRange!.length, lessThanOrEqualTo(127));
    });

    test('zoom keeps the bar under the cursor fixed (numeric example)', () {
      final ChartViewport v = ChartViewport(barCount: 31000, width: 1000, startIndex: 14938);
      const double anchorX = 250;
      expect(v.slotAt(anchorX), 14969.25);
      final ChartViewport z = v.zoom(2, anchorX);
      expect(z.barWidth, 16);
      expect(z.startIndex, 14953.625);
      expect(z.slotAt(anchorX), closeTo(14969.25, 1e-9));
      final ChartViewport out = z.zoom(0.25, 700);
      expect(out.slotAt(700), closeTo(z.slotAt(700), 1e-9));
    });

    test('zoom is limited to min/max bar width', () {
      final ChartViewport v = ChartViewport(barCount: 31000, width: 1000, startIndex: 5000);
      expect(v.zoom(1000, 500).barWidth, ChartViewport.maxBarWidth);
      expect(v.zoom(0.0001, 500).barWidth, ChartViewport.minBarWidth);
    });

    test('pan clamps at both ends', () {
      final ChartViewport v = ChartViewport(barCount: 31000, width: 1000, startIndex: 5000);
      // Drag right by 80 px = 10 bars older.
      expect(v.pan(80).startIndex, 4990);
      expect(v.pan(1e9).startIndex, 0);
      expect(v.pan(-1e9).startIndex, v.maxStart);
    });

    test('few bars are right-aligned and cannot be panned', () {
      final ChartViewport v = ChartViewport(barCount: 50, width: 1000);
      expect(v.startIndex, 53 - 125);
      expect(v.pan(300).startIndex, v.startIndex);
      expect(v.pan(-300).startIndex, v.startIndex);
      expect(v.visibleRange, const IndexRange(0, 49));
    });

    test('centerOn puts the bar at the plot centre (numeric example)', () {
      final ChartViewport v = ChartViewport(barCount: 31000, width: 1000);
      final ChartViewport c = v.centerOn(15000);
      expect(c.startIndex, 15000.5 - 62.5);
      expect(c.xOf(15000), 500);
      // Near the ends it is clamped instead.
      expect(v.centerOn(3).startIndex, 0);
      expect(v.centerOn(30999).startIndex, v.maxStart);
    });

    test('barAt / xOf round-trip and out-of-range', () {
      final ChartViewport v = ChartViewport(barCount: 100, width: 400, startIndex: 10);
      expect(v.barAt(v.xOf(42)), 42);
      expect(v.barAt(-100), isNull);
      expect(v.barAt(1e6), isNull);
    });

    test('withWidth keeps the right edge', () {
      final ChartViewport v = ChartViewport(barCount: 31000, width: 1000, startIndex: 1000);
      final ChartViewport w = v.withWidth(800);
      expect(w.startIndex + w.visibleBars, v.startIndex + v.visibleBars);
    });
  });

  group('PriceScale / autoscale', () {
    test('price <-> y is linear and inverse', () {
      const PriceScale s = PriceScale(min: 100, max: 200, top: 0, bottom: 500);
      expect(s.yOf(200), 0);
      expect(s.yOf(100), 500);
      expect(s.yOf(150), 250);
      expect(s.priceAt(125), 175);
    });

    test('autoscale covers candles, channel and setup levels of the visible range', () async {
      final FakeChartDataSource src = FakeChartDataSource(h1Count: 300);
      final ChartData d = ChartData.build(
        rates: await src.rates('XAUUSD.x', ChartTimeframe.h1),
        timeframe: ChartTimeframe.h1,
        channelResult: await src.channel('XAUUSD.x', ChartTimeframe.h1),
        setupsResult: await src.setups('XAUUSD.x'),
      );
      const IndexRange r = IndexRange(250, 299);
      final PriceScale s = autoscalePrice(d, r, top: 0, bottom: 100, padFraction: 0);
      for (int i = r.first; i <= r.last; i++) {
        expect(d.candles[i].low, greaterThanOrEqualTo(s.min));
        expect(d.candles[i].high, lessThanOrEqualTo(s.max));
        expect(d.channel[i]!.upper, lessThanOrEqualTo(s.max));
        expect(d.channel[i]!.lower, greaterThanOrEqualTo(s.min));
      }
      for (final SetupMark m in d.setups.where((SetupMark m) => r.contains(m.barIndex))) {
        expect(m.item.stopLoss, inInclusiveRange(s.min, s.max));
        expect(m.item.indicativeTakeProfit, inInclusiveRange(s.min, s.max));
      }
    });
  });

  test('niceTicks / timeLabelStep', () {
    expect(niceTicks(0, 10, maxCount: 6), <double>[0, 2, 4, 6, 8, 10]);
    expect(timeLabelStep(8, ChartTimeframe.h1), 24);
    expect(timeLabelStep(64, ChartTimeframe.h1), 2);
  });

  test('ChartData aligns channel/setups/gaps and finds times', () async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: 300);
    final ChartData d = ChartData.build(
      rates: await src.rates('XAUUSD.x', ChartTimeframe.h1),
      timeframe: ChartTimeframe.h1,
      channelResult: await src.channel('XAUUSD.x', ChartTimeframe.h1),
      setupsResult: await src.setups('XAUUSD.x'),
      digits: 2,
    );
    expect(d.length, 300);
    expect(d.channel[0]!.valid, isFalse);
    expect(d.channel[100]!.time, d.candles[100].time);
    // Pending setup on the newest bar has no entry bar.
    final SetupMark pending = d.setups.last;
    expect(pending.item.status, SetupStatus.pendingEntry);
    expect(pending.entryIndex, isNull);
    expect(pending.levelsStartIndex, 300);
    expect(d.setups.first.entryIndex, d.setups.first.barIndex + 1);
    expect(d.gaps, isNotEmpty);
    expect(d.nearestIndex(d.candles[42].time.add(const Duration(minutes: 20))), 42);
    expect(d.nearestIndex(DateTime.utc(1990)), 0);
    expect(d.nearestIndex(DateTime.utc(2100)), 299);
  });
}
