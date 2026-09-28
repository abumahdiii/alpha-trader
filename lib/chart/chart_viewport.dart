// Pure chart geometry: which bars are visible, where a bar/price is on
// screen, zoom/pan/jump. No Flutter widgets, fully unit-tested.
//
// Time axis = bar index (like MT5): weekends and other gaps take no space.
// Slot i spans x in [(i - startIndex) * barWidth, (i - startIndex + 1) * barWidth)
// and the candle is drawn at the slot centre.

import 'dart:math' as math;
import 'dart:ui' show Offset, Rect, Size;

import 'package:flutter/foundation.dart';

import '../models/backtest_models.dart';
import 'chart_data.dart';
import '../models/chart_models.dart';

/// Inclusive range of bar indices.
@immutable
class IndexRange {
  const IndexRange(this.first, this.last);

  final int first;
  final int last;

  int get length => last - first + 1;
  bool contains(int i) => i >= first && i <= last;

  @override
  bool operator ==(Object other) => other is IndexRange && other.first == first && other.last == last;

  @override
  int get hashCode => Object.hash(first, last);

  @override
  String toString() => 'IndexRange($first..$last)';
}

/// Horizontal viewport over [barCount] bars inside a plot [width] px wide.
@immutable
class ChartViewport {
  const ChartViewport._({
    required this.barCount,
    required this.width,
    required this.barWidth,
    required this.startIndex,
  });

  /// A clamped viewport; with no explicit [startIndex] the newest bars are shown.
  factory ChartViewport({
    required int barCount,
    required double width,
    double barWidth = defaultBarWidth,
    double? startIndex,
  }) {
    final double w = barWidth.clamp(minBarWidth, maxBarWidth).toDouble();
    final double safeWidth = math.max(width, 1.0);
    final ChartViewport raw = ChartViewport._(
      barCount: math.max(barCount, 0),
      width: safeWidth,
      barWidth: w,
      startIndex: 0,
    );
    return raw._withStart(startIndex ?? raw.maxStart);
  }

  static const double minBarWidth = 1.0;
  static const double maxBarWidth = 64.0;
  static const double defaultBarWidth = 8.0;

  /// Empty slots kept right of the newest bar so it never touches the axis.
  static const double rightPadBars = 3.0;

  final int barCount;
  final double width;

  /// Pixels per bar slot.
  final double barWidth;

  /// Fractional bar coordinate at the left edge of the plot.
  final double startIndex;

  double get visibleBars => width / barWidth;

  /// Largest start: newest bar + [rightPadBars] at the right edge.
  double get maxStart => barCount + rightPadBars - visibleBars;

  /// Smallest start: oldest bar at the left edge — or, when everything fits,
  /// the same as [maxStart] (bars right-aligned, like MT5).
  double get minStart => math.min(0.0, maxStart);

  ChartViewport _withStart(double start) => ChartViewport._(
        barCount: barCount,
        width: width,
        barWidth: barWidth,
        startIndex: start.clamp(minStart, maxStart).toDouble(),
      );

  /// X of the centre of bar [index].
  double xOf(int index) => (index - startIndex) * barWidth + barWidth / 2;

  /// Fractional bar coordinate under [x].
  double slotAt(double x) => startIndex + x / barWidth;

  /// Bar under [x], or null outside the data.
  int? barAt(double x) {
    final int i = slotAt(x).floor();
    return (i < 0 || i >= barCount) ? null : i;
  }

  /// Bars that overlap the plot (culling range); null when none do.
  IndexRange? get visibleRange {
    if (barCount == 0) return null;
    final int first = math.max(0, startIndex.floor());
    final int last = math.min(barCount - 1, (startIndex + visibleBars).ceil() - 1);
    return first > last ? null : IndexRange(first, last);
  }

  /// Zoom by [factor] (> 1 = zoom in) keeping the bar coordinate under
  /// [anchorX] fixed (unless a clamp kicks in).
  ChartViewport zoom(double factor, double anchorX) {
    final double anchor = slotAt(anchorX);
    final double w = (barWidth * factor).clamp(minBarWidth, maxBarWidth).toDouble();
    final ChartViewport resized = ChartViewport._(
      barCount: barCount,
      width: width,
      barWidth: w,
      startIndex: startIndex,
    );
    return resized._withStart(anchor - anchorX / w);
  }

  /// Drag by [dx] px: dragging right (dx > 0) reveals older bars.
  ChartViewport pan(double dx) => _withStart(startIndex - dx / barWidth);

  /// Centre bar [index] in the plot.
  ChartViewport centerOn(int index) => _withStart(index + 0.5 - visibleBars / 2);

  /// Zoom so about [bars] bar slots fill the plot (bar width clamped to
  /// [minBarWidth]..[maxBarWidth]); the start is re-clamped.
  ChartViewport fitBars(double bars) {
    if (!(bars > 0)) return this;
    final double w = (width / bars).clamp(minBarWidth, maxBarWidth).toDouble();
    return ChartViewport._(barCount: barCount, width: width, barWidth: w, startIndex: startIndex)._withStart(startIndex);
  }

  /// Newest bars at the default zoom.
  ChartViewport reset() => ChartViewport(barCount: barCount, width: width);

  /// New plot width, keeping the right edge where it was.
  ChartViewport withWidth(double newWidth) {
    if (newWidth == width) return this;
    final double right = startIndex + visibleBars;
    final ChartViewport resized = ChartViewport._(
      barCount: barCount,
      width: math.max(newWidth, 1.0),
      barWidth: barWidth,
      startIndex: startIndex,
    );
    return resized._withStart(right - resized.visibleBars);
  }

  @override
  bool operator ==(Object other) =>
      other is ChartViewport &&
      other.barCount == barCount &&
      other.width == width &&
      other.barWidth == barWidth &&
      other.startIndex == startIndex;

  @override
  int get hashCode => Object.hash(barCount, width, barWidth, startIndex);

  @override
  String toString() => 'ChartViewport(n=$barCount, w=$width, bar=$barWidth, start=$startIndex)';
}

/// Linear price <-> y mapping over the plot's vertical extent.
@immutable
class PriceScale {
  const PriceScale({required this.min, required this.max, required this.top, required this.bottom});

  final double min;
  final double max;
  final double top;
  final double bottom;

  double get _span => max - min;

  double yOf(double price) => top + (max - price) / _span * (bottom - top);

  double priceAt(double y) => max - (y - top) / (bottom - top) * _span;

  @override
  bool operator ==(Object other) =>
      other is PriceScale && other.min == min && other.max == max && other.top == top && other.bottom == bottom;

  @override
  int get hashCode => Object.hash(min, max, top, bottom);
}

/// Number of bars the entry/SL/TP segments of a setup extend over.
const int kSetupLevelBars = 10;

/// Price range over the visible bars: highs/lows, the channel lines of those
/// bars, the levels of setups whose segments reach into the range and the
/// prices of backtest trades overlapping it, plus
/// [padFraction] headroom. Values are only compared, never computed.
PriceScale autoscalePrice(
  ChartData data,
  IndexRange range, {
  required double top,
  required double bottom,
  double padFraction = 0.06,
}) {
  double lo = double.infinity;
  double hi = double.negativeInfinity;
  void take(double? v) {
    if (v == null || !v.isFinite) return;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }

  for (int i = range.first; i <= range.last; i++) {
    final Candle c = data.candles[i];
    take(c.low);
    take(c.high);
    final ChannelPoint? p = data.channel[i];
    if (p != null && p.drawable) {
      take(p.upper);
      take(p.lower);
    }
  }
  for (int k = data.firstSetupAtOrAfter(range.first - kSetupLevelBars - 1); k < data.setups.length; k++) {
    final SetupMark m = data.setups[k];
    if (m.barIndex > range.last) break;
    if (m.levelsStartIndex + kSetupLevelBars < range.first) continue;
    final SetupItem s = m.item;
    take(s.stopLoss);
    take(s.entry ?? s.referencePrice);
    take(s.takeProfit ?? s.indicativeTakeProfit);
  }
  for (final TradeMark m in data.tradesOverlapping(range.first, range.last)) {
    final BacktestTrade t = m.trade;
    take(t.entry);
    take(t.stopLoss);
    take(t.takeProfit);
    take(t.exitPrice);
  }
  if (!lo.isFinite || !hi.isFinite) {
    lo = 0;
    hi = 1;
  }
  if (hi - lo < 1e-9) {
    final double half = math.max(hi.abs() * 1e-4, 1e-6);
    lo -= half;
    hi += half;
  }
  final double pad = (hi - lo) * padFraction;
  return PriceScale(min: lo - pad, max: hi + pad, top: top, bottom: bottom);
}

/// Plot / axis rectangles of a chart canvas of [size].
@immutable
class ChartLayout {
  factory ChartLayout(Size size) {
    final double w = math.max(size.width - priceAxisWidth, 1.0);
    final double h = math.max(size.height - timeAxisHeight, 1.0);
    return ChartLayout._(
      size: size,
      plot: Rect.fromLTWH(0, 0, w, h),
      priceAxis: Rect.fromLTWH(w, 0, priceAxisWidth, h),
      timeAxis: Rect.fromLTWH(0, h, w, timeAxisHeight),
    );
  }

  const ChartLayout._({required this.size, required this.plot, required this.priceAxis, required this.timeAxis});

  static const double priceAxisWidth = 76;
  static const double timeAxisHeight = 22;
  static const double plotPadTop = 12;
  static const double plotPadBottom = 12;

  final Size size;
  final Rect plot;
  final Rect priceAxis;
  final Rect timeAxis;

  bool inPlot(Offset p) => plot.contains(p);

  @override
  bool operator ==(Object other) => other is ChartLayout && other.size == size;

  @override
  int get hashCode => size.hashCode;
}

/// "Nice" price grid values (1/2/5 x 10^k steps) inside [min, max].
List<double> niceTicks(double min, double max, {int maxCount = 8}) {
  if (!(max > min) || maxCount < 2) return const <double>[];
  final double raw = (max - min) / (maxCount - 1);
  final double mag = math.pow(10, (math.log(raw) / math.ln10).floor()).toDouble();
  final double norm = raw / mag;
  final double step = (norm <= 1
          ? 1
          : norm <= 2
              ? 2
              : norm <= 5
                  ? 5
                  : 10) *
      mag;
  final List<double> out = <double>[];
  for (double v = (min / step).ceil() * step; v <= max; v += step) {
    out.add(v);
  }
  return out;
}

/// Decimals needed to label grid prices spaced by [step].
int decimalsForStep(double step) {
  if (step <= 0 || !step.isFinite) return 2;
  return math.max(0, -(math.log(step) / math.ln10).floor());
}

/// Bars between time-axis labels so labels are >= [minSpacingPx] apart.
/// Steps are "calendar-friendly" multiples so labels stay put while panning.
int timeLabelStep(double barWidth, ChartTimeframe timeframe, {double minSpacingPx = 110}) {
  final List<int> steps = timeframe == ChartTimeframe.h1
      ? const <int>[1, 2, 3, 4, 6, 12, 24, 48, 72, 120, 240, 480, 720, 1440, 2880, 5760]
      : const <int>[1, 2, 3, 6, 12, 18, 30, 60, 120, 180, 360, 720, 1440];
  for (final int s in steps) {
    if (s * barWidth >= minSpacingPx) return s;
  }
  return steps.last;
}
