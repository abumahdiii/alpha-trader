// Equity curve of one backtest window (CustomPainter, no chart package).
//
// Draws the engine's stored points as they are: realized balance and
// mark-to-market equity against time. The time axis always runs left to
// right (forced LTR inside the RTL app). Hovering shows a crosshair and a
// readout with the local time, the UTC time and both values.

import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import '../../chart/chart_format.dart';
import '../../core/number_format.dart';
import '../../models/backtest_models.dart';
import '../../theme/app_semantic_colors.dart';
import 'backtest_format.dart';

/// Colors of the backtest charts, from the current theme (light / dark).
@immutable
class BacktestChartStyle {
  const BacktestChartStyle({
    required this.balance,
    required this.equity,
    required this.grid,
    required this.axisText,
    required this.crosshair,
    required this.profit,
    required this.loss,
    required this.mean,
    required this.median,
    required this.zero,
  });

  factory BacktestChartStyle.of(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    final ColorScheme cs = theme.colorScheme;
    final AppSemanticColors c = context.appColors;
    return BacktestChartStyle(
      balance: cs.primary,
      equity: c.info,
      grid: c.borderColor.withValues(alpha: 0.25),
      axisText: c.mutedText,
      crosshair: cs.onSurface.withValues(alpha: 0.55),
      profit: c.success,
      loss: c.error,
      mean: cs.primary,
      median: c.warning,
      zero: cs.onSurface.withValues(alpha: 0.6),
    );
  }

  final Color balance;
  final Color equity;
  final Color grid;
  final Color axisText;
  final Color crosshair;
  final Color profit;
  final Color loss;
  final Color mean;
  final Color median;
  final Color zero;

  @override
  bool operator ==(Object other) =>
      other is BacktestChartStyle &&
      other.balance == balance &&
      other.equity == equity &&
      other.grid == grid &&
      other.axisText == axisText &&
      other.profit == profit &&
      other.loss == loss;

  @override
  int get hashCode => Object.hash(balance, equity, grid, axisText, profit, loss);
}

/// Plot area insets shared by painter and hit testing.
const EdgeInsets _plotInsets = EdgeInsets.fromLTRB(8, 10, 72, 26);

/// Balance + equity lines of [points] (one window, engine order) with a hover readout.
class EquityChart extends StatefulWidget {
  const EquityChart({super.key, required this.points});

  final List<BacktestEquityPoint> points;

  @override
  State<EquityChart> createState() => _EquityChartState();
}

class _EquityChartState extends State<EquityChart> {
  int? _hover;

  int? _indexAt(double x, double width) {
    final List<BacktestEquityPoint> p = widget.points;
    if (p.isEmpty) return null;
    final _TimeAxis axis = _TimeAxis(p.first.time, p.last.time, _plotInsets.left, width - _plotInsets.right);
    final int t = axis.timeAt(x);
    // Nearest point by time (points are chronological).
    int lo = 0, hi = p.length - 1;
    while (lo < hi) {
      final int mid = (lo + hi) >> 1;
      if (p[mid].time.millisecondsSinceEpoch < t) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    if (lo > 0 && (t - p[lo - 1].time.millisecondsSinceEpoch).abs() < (p[lo].time.millisecondsSinceEpoch - t).abs()) {
      lo--;
    }
    return lo;
  }

  @override
  Widget build(BuildContext context) {
    final BacktestChartStyle style = BacktestChartStyle.of(context);
    final TextStyle axisStyle = TextStyle(fontSize: 10, color: style.axisText);
    return Directionality(
      textDirection: TextDirection.ltr,
      child: LayoutBuilder(builder: (BuildContext context, BoxConstraints box) {
        final Size size = Size(box.maxWidth, box.maxHeight.isFinite ? box.maxHeight : 320);
        final int? hover = _hover != null && _hover! < widget.points.length ? _hover : null;
        return SizedBox(
          width: size.width,
          height: size.height,
          child: MouseRegion(
            onHover: (PointerHoverEvent e) {
              final int? i = _indexAt(e.localPosition.dx, size.width);
              if (i != _hover) setState(() => _hover = i);
            },
            onExit: (_) => setState(() => _hover = null),
            child: Stack(children: [
              Positioned.fill(
                child: CustomPaint(
                  painter: EquityPainter(points: widget.points, style: style, axisStyle: axisStyle, hover: hover),
                ),
              ),
              if (hover != null) _readout(context, widget.points[hover], size),
            ]),
          ),
        );
      }),
    );
  }

  Widget _readout(BuildContext context, BacktestEquityPoint p, Size size) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    final TextStyle st = TextStyle(fontSize: 11, color: cs.onInverseSurface);
    return Positioned(
      top: _plotInsets.top,
      left: _plotInsets.left + 4,
      child: IgnorePointer(
        child: Container(
          key: const ValueKey<String>('bt-equity-readout'),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
          decoration:
              BoxDecoration(color: cs.inverseSurface.withValues(alpha: 0.92), borderRadius: BorderRadius.circular(6)),
          child: Directionality(
            textDirection: TextDirection.rtl,
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
              Text('زمان محلی: ${formatLocal(p.time)}', style: st),
              Text('UTC: ${formatUtc(p.time)}', style: st),
              Text('موجودی: ${fmtMoney(p.balance, signed: false)}', style: st),
              Text('اکوییتی: ${fmtMoney(p.equity, signed: false)}', style: st),
            ]),
          ),
        ),
      ),
    );
  }
}

/// Maps UTC times to x (left -> right).
class _TimeAxis {
  _TimeAxis(DateTime first, DateTime last, this.left, this.right)
      : t0 = first.millisecondsSinceEpoch,
        t1 = math.max(last.millisecondsSinceEpoch, first.millisecondsSinceEpoch + 1);

  final int t0;
  final int t1;
  final double left;
  final double right;

  double x(DateTime t) => left + (t.millisecondsSinceEpoch - t0) / (t1 - t0) * (right - left);

  int timeAt(double x) => (t0 + (x - left) / math.max(1, right - left) * (t1 - t0)).round();
}

/// Paints grid, balance and equity lines, axes and the hover crosshair.
class EquityPainter extends CustomPainter {
  EquityPainter({required this.points, required this.style, required this.axisStyle, this.hover});

  final List<BacktestEquityPoint> points;
  final BacktestChartStyle style;
  final TextStyle axisStyle;
  final int? hover;

  @override
  void paint(Canvas canvas, Size size) {
    if (points.isEmpty || size.width <= _plotInsets.horizontal || size.height <= _plotInsets.vertical) return;
    final Rect plot = _plotInsets.deflateRect(Offset.zero & size);
    double lo = double.infinity, hi = double.negativeInfinity;
    for (final BacktestEquityPoint p in points) {
      for (final double? v in [p.balance, p.equity]) {
        if (v == null || !v.isFinite) continue;
        lo = math.min(lo, v);
        hi = math.max(hi, v);
      }
    }
    if (!lo.isFinite) return;
    if (hi - lo < 1e-9) {
      lo -= 1;
      hi += 1;
    }
    final double pad = (hi - lo) * 0.06;
    lo -= pad;
    hi += pad;
    double y(double v) => plot.bottom - (v - lo) / (hi - lo) * plot.height;
    final _TimeAxis axis = _TimeAxis(points.first.time, points.last.time, plot.left, plot.right);

    final Paint grid = Paint()
      ..color = style.grid
      ..strokeWidth = 1;
    // Horizontal grid + value labels (right side).
    const int rows = 4;
    for (int i = 0; i <= rows; i++) {
      final double v = lo + (hi - lo) * i / rows;
      final double yy = y(v);
      canvas.drawLine(Offset(plot.left, yy), Offset(plot.right, yy), grid);
      _text(canvas, formatNumber(v, decimals: hi - lo < 20 ? 2 : 0), Offset(plot.right + 4, yy - 6));
    }
    // Vertical grid + date labels (bottom).
    final int cols = math.max(1, math.min(6, (plot.width / 140).floor()));
    for (int i = 0; i <= cols; i++) {
      final double xx = plot.left + plot.width * i / cols;
      canvas.drawLine(Offset(xx, plot.top), Offset(xx, plot.bottom), grid);
      final DateTime t = DateTime.fromMillisecondsSinceEpoch(axis.timeAt(xx), isUtc: true).toLocal();
      final double shift = i == 0 ? 0 : (i == cols ? 60 : 30);
      _text(canvas, formatDate(t), Offset(xx - shift, plot.bottom + 6));
    }

    _line(canvas, axis, y, (BacktestEquityPoint p) => p.equity, style.equity);
    _line(canvas, axis, y, (BacktestEquityPoint p) => p.balance, style.balance);

    final int? h = hover;
    if (h != null && h >= 0 && h < points.length) {
      final BacktestEquityPoint p = points[h];
      final double xx = axis.x(p.time);
      canvas.drawLine(
        Offset(xx, plot.top),
        Offset(xx, plot.bottom),
        Paint()
          ..color = style.crosshair
          ..strokeWidth = 1,
      );
      for (final (double? v, Color c) in [(p.equity, style.equity), (p.balance, style.balance)]) {
        if (v != null) canvas.drawCircle(Offset(xx, y(v)), 3.5, Paint()..color = c);
      }
    }
  }

  void _line(Canvas canvas, _TimeAxis axis, double Function(double) y, double? Function(BacktestEquityPoint) value,
      Color color) {
    final Path path = Path();
    bool open = false;
    for (final BacktestEquityPoint p in points) {
      final double? v = value(p);
      if (v == null || !v.isFinite) {
        open = false;
        continue;
      }
      final Offset o = Offset(axis.x(p.time), y(v));
      if (open) {
        path.lineTo(o.dx, o.dy);
      } else {
        path.moveTo(o.dx, o.dy);
        open = true;
      }
    }
    canvas.drawPath(
      path,
      Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.6
        ..isAntiAlias = true,
    );
  }

  void _text(Canvas canvas, String text, Offset at) {
    final TextPainter tp = TextPainter(
      text: TextSpan(text: text, style: axisStyle),
      textDirection: TextDirection.ltr,
    )..layout();
    tp.paint(canvas, at);
  }

  @override
  bool shouldRepaint(EquityPainter old) =>
      !identical(old.points, points) || old.hover != hover || old.style != style || old.axisStyle != axisStyle;
}

/// Legend chip: a colored line and a label.
class ChartLegendItem extends StatelessWidget {
  const ChartLegendItem({super.key, required this.color, required this.label, this.dashed = false});

  final Color color;
  final String label;
  final bool dashed;

  @override
  Widget build(BuildContext context) => Row(mainAxisSize: MainAxisSize.min, children: [
        Container(
          width: 18,
          height: dashed ? 2 : 3,
          decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(2)),
        ),
        const SizedBox(width: 6),
        Text(label, style: const TextStyle(fontSize: 12)),
      ]);
}
