// CustomPainters of the candlestick chart (no chart package).
//
// Two layers:
//  * [ChartPainter] — grid, gaps, candles, channel lines, setups, axes.
//    Repaints only when data / viewport / scale / selection / style change.
//  * [CrosshairPainter] — hover crosshair and its axis labels; driven by a
//    Listenable so mouse moves repaint only this layer.
//
// Culling: every loop runs over `viewport.visibleRange` only (plus the few
// setups whose level segments reach into it), so a 5-year H1 series (~31k
// bars) costs the same per frame as the ~150 bars on screen.
//
// Channel lines, entry/SL/TP and markers are drawn at the engine's values;
// nothing is derived here except screen positions.

import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../theme/app_semantic_colors.dart';
import 'chart_data.dart';
import 'chart_format.dart';
import '../models/chart_models.dart';
import 'chart_viewport.dart';

/// Colours and reusable paints of the chart, built once per theme.
class ChartStyle {
  ChartStyle._({
    required this.bull,
    required this.bear,
    required this.channelEdge,
    required this.channelMid,
    required this.entry,
    required this.pending,
    required this.muted,
    required this.highlight,
    required this.grid,
    required this.axisBackground,
    required this.axisText,
    required this.crosshair,
    required this.labelBackground,
    required this.onLabel,
    required this.gapWeekend,
    required this.gapHoliday,
    required this.gapSessionBreak,
    required this.gapMissing,
  });

  factory ChartStyle.fromTheme(ThemeData theme) {
    final ColorScheme cs = theme.colorScheme;
    final AppSemanticColors? sc = theme.extension<AppSemanticColors>();
    final Color success = sc?.success ?? Colors.green;
    final Color error = sc?.error ?? cs.error;
    final Color info = sc?.info ?? Colors.blue;
    final Color warning = sc?.warning ?? Colors.orange;
    final Color muted = sc?.mutedText ?? cs.onSurfaceVariant;
    return ChartStyle._(
      bull: success,
      bear: error,
      channelEdge: info,
      channelMid: info.withValues(alpha: 0.65),
      entry: cs.primary,
      pending: warning,
      muted: muted,
      highlight: cs.primary.withValues(alpha: 0.14),
      grid: (sc?.borderColor ?? cs.outline).withValues(alpha: 0.18),
      axisBackground: cs.surface,
      axisText: muted,
      crosshair: cs.onSurface.withValues(alpha: 0.55),
      labelBackground: cs.inverseSurface,
      onLabel: cs.onInverseSurface,
      gapWeekend: muted.withValues(alpha: 0.10),
      gapHoliday: info.withValues(alpha: 0.22),
      gapSessionBreak: muted.withValues(alpha: 0.16),
      gapMissing: warning.withValues(alpha: 0.45),
    );
  }

  final Color bull;
  final Color bear;
  final Color channelEdge;
  final Color channelMid;
  final Color entry;
  final Color pending;
  final Color muted;
  final Color highlight;
  final Color grid;
  final Color axisBackground;
  final Color axisText;
  final Color crosshair;
  final Color labelBackground;
  final Color onLabel;
  final Color gapWeekend;
  final Color gapHoliday;
  final Color gapSessionBreak;
  final Color gapMissing;

  late final Paint bullFill = Paint()..color = bull;
  late final Paint bearFill = Paint()..color = bear;
  late final Paint bullWick = Paint()
    ..color = bull
    ..style = PaintingStyle.stroke
    ..strokeWidth = 1;
  late final Paint bearWick = Paint()
    ..color = bear
    ..style = PaintingStyle.stroke
    ..strokeWidth = 1;
  late final Paint channelEdgePaint = Paint()
    ..color = channelEdge
    ..style = PaintingStyle.stroke
    ..strokeWidth = 1.4
    ..isAntiAlias = true;
  late final Paint channelMidPaint = Paint()
    ..color = channelMid
    ..style = PaintingStyle.stroke
    ..strokeWidth = 1.0
    ..isAntiAlias = true;
  late final Paint gridPaint = Paint()
    ..color = grid
    ..strokeWidth = 1;
  late final Paint fillPaint = Paint();
  late final Paint strokePaint = Paint()..style = PaintingStyle.stroke;

  Color gapColor(GapKind kind) => switch (kind) {
        GapKind.weekend => gapWeekend,
        GapKind.holiday => gapHoliday,
        GapKind.sessionBreak => gapSessionBreak,
        GapKind.missing => gapMissing,
      };

  TextStyle get axisTextStyle => TextStyle(color: axisText, fontSize: 10.5, fontFamily: 'Mikhak');
}

/// Geometry of a setup marker (arrow) — shared by painting and hit-testing.
abstract final class SetupMarkerGeometry {
  static const double size = 10;
  static const double gap = 5;

  /// The arrow's tip: under the low for a buy (pointing up), above the high
  /// for a sell (pointing down).
  static Offset tip(SetupMark m, ChartData data, ChartViewport v, PriceScale s) {
    final Candle c = data.candles[m.barIndex];
    final double x = v.xOf(m.barIndex);
    return m.item.direction == TradeSide.buy ? Offset(x, s.yOf(c.low) + gap) : Offset(x, s.yOf(c.high) - gap);
  }

  static Path arrow(Offset tip, TradeSide side) {
    final double dir = side == TradeSide.buy ? 1 : -1;
    return Path()
      ..moveTo(tip.dx, tip.dy)
      ..lineTo(tip.dx - size * 0.6, tip.dy + dir * size)
      ..lineTo(tip.dx + size * 0.6, tip.dy + dir * size)
      ..close();
  }

  static Rect bounds(Offset tip, TradeSide side) {
    final double dir = side == TradeSide.buy ? 1 : -1;
    return Rect.fromPoints(Offset(tip.dx - size * 0.6, tip.dy), Offset(tip.dx + size * 0.6, tip.dy + dir * size));
  }
}

/// The setup marker under [p] (plot coordinates), nearest first; null if none.
SetupMark? hitTestSetup(ChartData data, ChartViewport v, PriceScale s, Offset p, {double slop = 5}) {
  final IndexRange? range = v.visibleRange;
  if (range == null) return null;
  SetupMark? best;
  double bestDist = double.infinity;
  for (int k = data.firstSetupAtOrAfter(range.first); k < data.setups.length; k++) {
    final SetupMark m = data.setups[k];
    if (m.barIndex > range.last) break;
    final Offset tip = SetupMarkerGeometry.tip(m, data, v, s);
    final Rect r = SetupMarkerGeometry.bounds(tip, m.item.direction).inflate(slop);
    if (!r.contains(p)) continue;
    final double d = (r.center - p).distanceSquared;
    if (d < bestDist) {
      bestDist = d;
      best = m;
    }
  }
  return best;
}

void _dashedHLine(Canvas canvas, double x1, double x2, double y, Paint paint, {double dash = 5, double space = 4}) {
  for (double x = x1; x < x2; x += dash + space) {
    canvas.drawLine(Offset(x, y), Offset(math.min(x + dash, x2), y), paint);
  }
}

void _dashedVLine(Canvas canvas, double x, double y1, double y2, Paint paint, {double dash = 4, double space = 4}) {
  for (double y = y1; y < y2; y += dash + space) {
    canvas.drawLine(Offset(x, y), Offset(x, math.min(y + dash, y2)), paint);
  }
}

/// Draws [text] in a filled label box centred vertically at [anchor] (left
/// aligned) or horizontally (top aligned) — used for axis labels.
void _labelBox(Canvas canvas, String text, Offset anchor, ChartStyle style, {required bool vertical}) {
  final TextPainter tp = TextPainter(
    text: TextSpan(text: text, style: style.axisTextStyle.copyWith(color: style.onLabel)),
    textDirection: TextDirection.ltr,
  )..layout();
  final Rect box = vertical
      ? Rect.fromLTWH(anchor.dx, anchor.dy - tp.height / 2 - 2, tp.width + 8, tp.height + 4)
      : Rect.fromLTWH(anchor.dx - tp.width / 2 - 4, anchor.dy, tp.width + 8, tp.height + 4);
  canvas.drawRect(box, style.fillPaint..color = style.labelBackground);
  tp.paint(canvas, box.topLeft + const Offset(4, 2));
}

/// Static chart layer.
class ChartPainter extends CustomPainter {
  ChartPainter({
    required this.data,
    required this.viewport,
    required this.layout,
    required this.scale,
    required this.style,
    this.selectedSetupId,
    this.highlightIndex,
  });

  final ChartData data;
  final ChartViewport viewport;
  final ChartLayout layout;
  final PriceScale scale;
  final ChartStyle style;
  final String? selectedSetupId;
  final int? highlightIndex;

  /// Bars drawn by the last [paint] — evidence that culling works.
  @visibleForTesting
  static int debugLastPaintedBars = 0;

  double get _w => viewport.barWidth;

  @override
  void paint(Canvas canvas, Size size) {
    final Rect plot = layout.plot;
    final IndexRange? range = viewport.visibleRange;
    canvas.save();
    canvas.clipRect(plot);
    _paintPriceGrid(canvas, plot);
    if (range != null) {
      _paintTimeGrid(canvas, plot, range);
      _paintGaps(canvas, plot, range);
      _paintHighlights(canvas, plot, range);
      _paintCandles(canvas, range);
      _paintChannel(canvas, range);
      _paintSetups(canvas, range);
      debugLastPaintedBars = range.length;
    } else {
      debugLastPaintedBars = 0;
    }
    canvas.restore();
    _paintPriceAxis(canvas);
    if (range != null) _paintTimeAxis(canvas, range);
  }

  // ------------------------------------------------------------ grid & axes

  List<double> get _priceTicks =>
      niceTicks(scale.min, scale.max, maxCount: math.max(3, (layout.plot.height / 56).floor()));

  void _paintPriceGrid(Canvas canvas, Rect plot) {
    for (final double p in _priceTicks) {
      final double y = scale.yOf(p);
      canvas.drawLine(Offset(plot.left, y), Offset(plot.right, y), style.gridPaint);
    }
  }

  int get _timeStep => timeLabelStep(_w, data.timeframe);

  void _paintTimeGrid(Canvas canvas, Rect plot, IndexRange range) {
    final int step = _timeStep;
    for (int i = (range.first / step).ceil() * step; i <= range.last; i += step) {
      final double x = viewport.xOf(i);
      canvas.drawLine(Offset(x, plot.top), Offset(x, plot.bottom), style.gridPaint);
    }
  }

  void _paintPriceAxis(Canvas canvas) {
    final Rect axis = layout.priceAxis;
    canvas.drawRect(axis, style.fillPaint..color = style.axisBackground);
    canvas.drawLine(axis.topLeft, axis.bottomLeft, style.gridPaint);
    final List<double> ticks = _priceTicks;
    final int decimals = ticks.length >= 2 ? decimalsForStep(ticks[1] - ticks[0]) : 2;
    for (final double p in ticks) {
      final double y = scale.yOf(p);
      final TextPainter tp = TextPainter(
        text: TextSpan(text: p.toStringAsFixed(decimals), style: style.axisTextStyle),
        textDirection: TextDirection.ltr,
      )..layout();
      tp.paint(canvas, Offset(axis.left + 6, y - tp.height / 2));
    }
    // Last close, like MT5's bid line (the engine's last cached close).
    if (!data.isEmpty) {
      final double close = data.candles.last.close;
      final double y = scale.yOf(close);
      if (y >= layout.plot.top && y <= layout.plot.bottom) {
        _dashedHLine(
            canvas,
            layout.plot.left,
            layout.plot.right,
            y,
            style.strokePaint
              ..color = style.muted.withValues(alpha: 0.6)
              ..strokeWidth = 1);
        _labelBox(canvas, formatChartPrice(close, data.digits), Offset(axis.left + 1, y), style, vertical: true);
      }
    }
  }

  void _paintTimeAxis(Canvas canvas, IndexRange range) {
    final Rect axis = layout.timeAxis;
    canvas.drawRect(axis, style.fillPaint..color = style.axisBackground);
    canvas.drawLine(axis.topLeft, axis.topRight, style.gridPaint);
    final int step = _timeStep;
    final bool dateOnly = step * data.timeframe.duration.inHours >= 24;
    canvas.save();
    canvas.clipRect(axis);
    for (int i = (range.first / step).ceil() * step; i <= range.last; i += step) {
      final TextPainter tp = TextPainter(
        text: TextSpan(text: formatAxisTime(data.candles[i].time, dateOnly: dateOnly), style: style.axisTextStyle),
        textDirection: TextDirection.ltr,
      )..layout();
      tp.paint(canvas, Offset(viewport.xOf(i) - tp.width / 2, axis.top + 4));
    }
    canvas.restore();
  }

  // ------------------------------------------------------------ bands

  void _paintGaps(Canvas canvas, Rect plot, IndexRange range) {
    final double bandW = (_w * 0.35).clamp(2.0, 8.0);
    for (int k = data.firstGapAtOrAfter(range.first - 1); k < data.gaps.length; k++) {
      final GapMark g = data.gaps[k];
      if (g.afterIndex > range.last) break;
      final double x = viewport.xOf(g.afterIndex) + _w / 2;
      canvas.drawRect(
        Rect.fromLTRB(x - bandW / 2, plot.top, x + bandW / 2, plot.bottom),
        style.fillPaint..color = style.gapColor(g.gap.kind),
      );
    }
  }

  void _paintHighlights(Canvas canvas, Rect plot, IndexRange range) {
    void band(int i) {
      if (!range.contains(i)) return;
      final double x = viewport.xOf(i);
      canvas.drawRect(
        Rect.fromLTRB(x - _w / 2, plot.top, x + _w / 2, plot.bottom),
        style.fillPaint..color = style.highlight,
      );
    }

    final int? h = highlightIndex;
    if (h != null) band(h);
    final SetupMark? sel = data.setupById(selectedSetupId);
    if (sel != null && sel.barIndex != h) band(sel.barIndex);
  }

  // ------------------------------------------------------------ candles

  void _paintCandles(Canvas canvas, IndexRange range) {
    final Path bullWicks = Path();
    final Path bearWicks = Path();
    final Path bullBodies = Path();
    final Path bearBodies = Path();
    final bool bodies = _w >= 3;
    final double bodyW = math.max(1.0, _w * 0.7);
    for (int i = range.first; i <= range.last; i++) {
      final Candle c = data.candles[i];
      final double x = viewport.xOf(i);
      final bool bull = c.isBull;
      (bull ? bullWicks : bearWicks)
        ..moveTo(x, scale.yOf(c.high))
        ..lineTo(x, scale.yOf(c.low));
      if (bodies) {
        final double yO = scale.yOf(c.open);
        final double yC = scale.yOf(c.close);
        double top = math.min(yO, yC);
        double bottom = math.max(yO, yC);
        if (bottom - top < 1) {
          top -= 0.5;
          bottom += 0.5;
        }
        (bull ? bullBodies : bearBodies).addRect(Rect.fromLTRB(x - bodyW / 2, top, x + bodyW / 2, bottom));
      }
    }
    canvas.drawPath(bullWicks, style.bullWick);
    canvas.drawPath(bearWicks, style.bearWick);
    if (bodies) {
      canvas.drawPath(bullBodies, style.bullFill);
      canvas.drawPath(bearBodies, style.bearFill);
    }
  }

  // ------------------------------------------------------------ channel

  /// Three polylines through the engine's per-bar channel values.
  ///
  /// Pen-up rules: an invalid (warm-up) point breaks every line, and on H1 a
  /// change of `h4_open` breaks them too — the H1 values are the channel of
  /// one closed H4 bar projected forward, and the next H4 close starts a new
  /// channel, so joining the two would draw a slope the strategy never used.
  /// On H4 each point is a separate rolling channel's right end; joining them
  /// gives the familiar channel envelope. A one-point segment is drawn as a
  /// bar-wide tick so it stays visible.
  void _paintChannel(Canvas canvas, IndexRange range) {
    final Path upper = Path();
    final Path mid = Path();
    final Path lower = Path();
    final bool breakOnH4 = data.timeframe == ChartTimeframe.h1;
    ChannelPoint? prev;
    int segLen = 0;
    double segX = 0;
    double segU = 0;
    double segM = 0;
    double segL = 0;

    void closeSegment() {
      if (segLen == 1) {
        final double h = _w / 2;
        upper
          ..moveTo(segX - h, segU)
          ..lineTo(segX + h, segU);
        mid
          ..moveTo(segX - h, segM)
          ..lineTo(segX + h, segM);
        lower
          ..moveTo(segX - h, segL)
          ..lineTo(segX + h, segL);
      }
      segLen = 0;
    }

    for (int i = range.first; i <= range.last; i++) {
      final ChannelPoint? p = data.channel[i];
      if (p == null || !p.drawable) {
        closeSegment();
        prev = null;
        continue;
      }
      if (prev != null && breakOnH4 && p.h4Open != prev.h4Open) closeSegment();
      final double x = viewport.xOf(i);
      final double yU = scale.yOf(p.upper!);
      final double yM = scale.yOf(p.mid!);
      final double yL = scale.yOf(p.lower!);
      if (segLen == 0) {
        upper.moveTo(x, yU);
        mid.moveTo(x, yM);
        lower.moveTo(x, yL);
        segX = x;
        segU = yU;
        segM = yM;
        segL = yL;
      } else {
        upper.lineTo(x, yU);
        mid.lineTo(x, yM);
        lower.lineTo(x, yL);
      }
      segLen++;
      prev = p;
    }
    closeSegment();
    canvas.drawPath(upper, style.channelEdgePaint);
    canvas.drawPath(lower, style.channelEdgePaint);
    canvas.drawPath(mid, style.channelMidPaint);
  }

  // ------------------------------------------------------------ setups

  void _paintSetups(Canvas canvas, IndexRange range) {
    if (data.setups.isEmpty) return;
    for (int k = data.firstSetupAtOrAfter(range.first - kSetupLevelBars - 1); k < data.setups.length; k++) {
      final SetupMark m = data.setups[k];
      if (m.barIndex > range.last) break;
      final bool selected = m.item.id == selectedSetupId;
      _paintLevels(canvas, m, selected);
      if (range.contains(m.barIndex)) _paintMarker(canvas, m, selected);
    }
  }

  void _paintLevels(Canvas canvas, SetupMark m, bool selected) {
    final SetupItem s = m.item;
    final int start = m.levelsStartIndex;
    final double x1 = viewport.xOf(start) - _w / 2;
    final double x2 = viewport.xOf(start + kSetupLevelBars - 1) + _w / 2;
    if (x2 < layout.plot.left || x1 > layout.plot.right) return;
    final Paint p = style.strokePaint..strokeWidth = selected ? 2.0 : 1.2;

    void level(double? price, Color color, {required bool dashed, String? tag}) {
      if (price == null) return;
      final double y = scale.yOf(price);
      p.color = color;
      if (dashed) {
        _dashedHLine(canvas, x1, x2, y, p);
      } else {
        canvas.drawLine(Offset(x1, y), Offset(x2, y), p);
      }
      if (selected && tag != null) {
        final TextPainter tp = TextPainter(
          text: TextSpan(
            text: '$tag ${formatChartPrice(price, data.digits)}',
            style: style.axisTextStyle.copyWith(color: color, fontWeight: FontWeight.bold),
          ),
          textDirection: TextDirection.ltr,
        )..layout();
        tp.paint(canvas, Offset(x2 + 3, y - tp.height / 2));
      }
    }

    switch (s.status) {
      case SetupStatus.accepted:
        level(s.entry, style.entry, dashed: false, tag: 'Entry');
        level(s.stopLoss, style.bear, dashed: false, tag: 'SL');
        level(s.takeProfit, style.bull, dashed: false, tag: 'TP');
      case SetupStatus.pendingEntry:
        // Entry unknown until bar t+1 opens: indicative reference + TP, dashed.
        level(s.referencePrice, style.pending, dashed: true, tag: 'Ref*');
        level(s.stopLoss, style.bear, dashed: false, tag: 'SL');
        level(s.indicativeTakeProfit, style.pending, dashed: true, tag: 'TP*');
      case SetupStatus.rejected:
        level(s.entry, style.muted, dashed: true, tag: 'Entry');
        level(s.stopLoss, style.muted, dashed: true, tag: 'SL');
        level(s.takeProfit, style.muted, dashed: true, tag: 'TP');
    }
  }

  void _paintMarker(Canvas canvas, SetupMark m, bool selected) {
    final SetupItem s = m.item;
    final Offset tip = SetupMarkerGeometry.tip(m, data, viewport, scale);
    final Path arrow = SetupMarkerGeometry.arrow(tip, s.direction);
    final Color sideColor = s.direction == TradeSide.buy ? style.bull : style.bear;
    switch (s.status) {
      case SetupStatus.accepted:
        canvas.drawPath(arrow, style.fillPaint..color = sideColor);
      case SetupStatus.pendingEntry:
        canvas.drawPath(arrow, style.fillPaint..color = style.pending.withValues(alpha: 0.25));
        canvas.drawPath(
            arrow,
            style.strokePaint
              ..color = style.pending
              ..strokeWidth = 1.6);
      case SetupStatus.rejected:
        canvas.drawPath(
            arrow,
            style.strokePaint
              ..color = style.muted
              ..strokeWidth = 1.4);
        final Rect b = SetupMarkerGeometry.bounds(tip, s.direction);
        canvas.drawLine(b.topLeft, b.bottomRight, style.strokePaint..strokeWidth = 1.2);
        canvas.drawLine(b.topRight, b.bottomLeft, style.strokePaint);
    }
    if (selected) {
      final Rect b = SetupMarkerGeometry.bounds(tip, s.direction).inflate(4);
      canvas.drawRect(
          b,
          style.strokePaint
            ..color = style.entry
            ..strokeWidth = 1.5);
    }
  }

  @override
  bool shouldRepaint(ChartPainter old) =>
      !identical(old.data, data) ||
      old.viewport != viewport ||
      old.layout != layout ||
      old.scale != scale ||
      !identical(old.style, style) ||
      old.selectedSetupId != selectedSetupId ||
      old.highlightIndex != highlightIndex;
}

/// Hover crosshair layer; repaints on [hover] changes without rebuilding.
class CrosshairPainter extends CustomPainter {
  CrosshairPainter({
    required this.hover,
    required this.data,
    required this.viewport,
    required this.layout,
    required this.scale,
    required this.style,
  }) : super(repaint: hover);

  final ValueListenable<Offset?> hover;
  final ChartData data;
  final ChartViewport viewport;
  final ChartLayout layout;
  final PriceScale scale;
  final ChartStyle style;

  @override
  void paint(Canvas canvas, Size size) {
    final Offset? p = hover.value;
    if (p == null || !layout.inPlot(p)) return;
    final Rect plot = layout.plot;
    final int? i = viewport.barAt(p.dx);
    final double x = i != null ? viewport.xOf(i) : p.dx;
    final Paint line = style.strokePaint
      ..color = style.crosshair
      ..strokeWidth = 1;
    _dashedVLine(canvas, x, plot.top, plot.bottom, line);
    _dashedHLine(canvas, plot.left, plot.right, p.dy, line);
    _labelBox(
        canvas, formatChartPrice(scale.priceAt(p.dy), data.digits), Offset(layout.priceAxis.left + 1, p.dy), style,
        vertical: true);
    if (i != null) {
      _labelBox(canvas, formatMt5Time(data.candles[i].time), Offset(x, layout.timeAxis.top + 1), style,
          vertical: false);
    }
  }

  @override
  bool shouldRepaint(CrosshairPainter old) =>
      !identical(old.data, data) ||
      old.viewport != viewport ||
      old.layout != layout ||
      old.scale != scale ||
      !identical(old.style, style) ||
      !identical(old.hover, hover);
}
