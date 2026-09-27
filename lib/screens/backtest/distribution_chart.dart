// Histogram of the random windows' net profits (CustomPainter).
//
// The bars count the engine's per-window `net_profit` values into equal-width
// bins (a display grouping only); the mean and median markers are the
// engine's `distribution.mean_net_profit` / `median_net_profit`. Value axis
// left -> right (forced LTR), zero line drawn, hover shows the bin range and
// its window count.

import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import '../../core/number_format.dart';
import 'backtest_format.dart';
import 'equity_chart.dart';

/// One histogram bin `[from, to)` (the last one closed).
@immutable
class HistogramBin {
  const HistogramBin(this.from, this.to, this.count);

  final double from;
  final double to;
  final int count;
}

/// Equal-width bins over [values] (~sqrt(n), 3..20). Pure display grouping.
List<HistogramBin> histogramBins(List<double> values) {
  final List<double> v = [
    for (final double x in values)
      if (x.isFinite) x
  ];
  if (v.isEmpty) return const [];
  final double lo = v.reduce(math.min), hi = v.reduce(math.max);
  if (hi - lo < 1e-9) return [HistogramBin(lo - 0.5, hi + 0.5, v.length)];
  final int n = math.sqrt(v.length).ceil().clamp(3, 20);
  final double w = (hi - lo) / n;
  final List<int> counts = List<int>.filled(n, 0);
  for (final double x in v) {
    counts[math.min(n - 1, ((x - lo) / w).floor())]++;
  }
  return [for (int i = 0; i < n; i++) HistogramBin(lo + i * w, i == n - 1 ? hi : lo + (i + 1) * w, counts[i])];
}

const EdgeInsets _insets = EdgeInsets.fromLTRB(28, 10, 12, 26);

class DistributionChart extends StatefulWidget {
  const DistributionChart({super.key, required this.values, this.mean, this.median});

  /// Net profit of every window (engine values).
  final List<double> values;
  final double? mean;
  final double? median;

  @override
  State<DistributionChart> createState() => _DistributionChartState();
}

class _DistributionChartState extends State<DistributionChart> {
  int? _hover;
  late List<HistogramBin> _bins = histogramBins(widget.values);

  @override
  void didUpdateWidget(DistributionChart old) {
    super.didUpdateWidget(old);
    if (!identical(old.values, widget.values)) {
      _bins = histogramBins(widget.values);
      _hover = null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final BacktestChartStyle style = BacktestChartStyle.of(context);
    final TextStyle axisStyle = TextStyle(fontSize: 10, color: style.axisText);
    return Directionality(
      textDirection: TextDirection.ltr,
      child: LayoutBuilder(builder: (BuildContext context, BoxConstraints box) {
        final Size size = Size(box.maxWidth, box.maxHeight.isFinite ? box.maxHeight : 220);
        final int? hover = _hover != null && _hover! < _bins.length ? _hover : null;
        return SizedBox(
          width: size.width,
          height: size.height,
          child: MouseRegion(
            onHover: (PointerHoverEvent e) {
              final int? i = DistributionPainter.binAt(_bins, e.localPosition.dx, size, widget.mean, widget.median);
              if (i != _hover) setState(() => _hover = i);
            },
            onExit: (_) => setState(() => _hover = null),
            child: Stack(children: [
              Positioned.fill(
                child: CustomPaint(
                  painter: DistributionPainter(
                    bins: _bins,
                    mean: widget.mean,
                    median: widget.median,
                    style: style,
                    axisStyle: axisStyle,
                    hover: hover,
                  ),
                ),
              ),
              if (hover != null)
                Positioned(
                  top: _insets.top,
                  right: _insets.right + 4,
                  child: IgnorePointer(child: _readout(context, _bins[hover])),
                ),
            ]),
          ),
        );
      }),
    );
  }

  Widget _readout(BuildContext context, HistogramBin b) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Container(
      key: const ValueKey<String>('bt-distribution-readout'),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
      decoration:
          BoxDecoration(color: cs.inverseSurface.withValues(alpha: 0.92), borderRadius: BorderRadius.circular(6)),
      child: Directionality(
        textDirection: TextDirection.rtl,
        child: Text(
          'سود خالص پنجره: ${fmtMoney(b.from)} تا ${fmtMoney(b.to)}\n${b.count} پنجره',
          style: TextStyle(fontSize: 11, color: cs.onInverseSurface),
        ),
      ),
    );
  }
}

class DistributionPainter extends CustomPainter {
  DistributionPainter({
    required this.bins,
    required this.style,
    required this.axisStyle,
    this.mean,
    this.median,
    this.hover,
  });

  final List<HistogramBin> bins;
  final double? mean;
  final double? median;
  final BacktestChartStyle style;
  final TextStyle axisStyle;
  final int? hover;

  /// Value range of the x axis: the bins plus zero, mean and median.
  static (double, double) range(List<HistogramBin> bins, double? mean, double? median) {
    double lo = bins.first.from, hi = bins.last.to;
    for (final double? v in [0.0, mean, median]) {
      if (v == null || !v.isFinite) continue;
      lo = math.min(lo, v);
      hi = math.max(hi, v);
    }
    final double pad = (hi - lo) * 0.04;
    return (lo - pad, hi + pad);
  }

  static int? binAt(List<HistogramBin> bins, double x, Size size, double? mean, double? median) {
    if (bins.isEmpty) return null;
    final Rect plot = _insets.deflateRect(Offset.zero & size);
    final (double lo, double hi) = range(bins, mean, median);
    final double v = lo + (x - plot.left) / math.max(1, plot.width) * (hi - lo);
    for (int i = 0; i < bins.length; i++) {
      if (v >= bins[i].from && (v < bins[i].to || (i == bins.length - 1 && v <= bins[i].to))) return i;
    }
    return null;
  }

  @override
  void paint(Canvas canvas, Size size) {
    if (bins.isEmpty || size.width <= _insets.horizontal || size.height <= _insets.vertical) return;
    final Rect plot = _insets.deflateRect(Offset.zero & size);
    final (double lo, double hi) = range(bins, mean, median);
    double x(double v) => plot.left + (v - lo) / (hi - lo) * plot.width;
    final int maxCount = bins.map((HistogramBin b) => b.count).reduce(math.max);
    double y(int c) => plot.bottom - c / math.max(1, maxCount) * plot.height;

    final Paint grid = Paint()
      ..color = style.grid
      ..strokeWidth = 1;
    for (int i = 0; i <= 4; i++) {
      final double yy = plot.bottom - plot.height * i / 4;
      canvas.drawLine(Offset(plot.left, yy), Offset(plot.right, yy), grid);
      final double c = maxCount * i / 4;
      if (c == c.roundToDouble()) _text(canvas, '${c.round()}', Offset(2, yy - 6));
    }

    for (int i = 0; i < bins.length; i++) {
      final HistogramBin b = bins[i];
      final double mid = (b.from + b.to) / 2;
      final Color c = mid >= 0 ? style.profit : style.loss;
      final Rect r = Rect.fromLTRB(x(b.from) + 1, y(b.count), x(b.to) - 1, plot.bottom);
      canvas.drawRect(r, Paint()..color = c.withValues(alpha: i == hover ? 0.95 : 0.65));
    }

    // Value labels: range ends and zero.
    _text(canvas, formatNumber(lo, decimals: 0), Offset(plot.left, plot.bottom + 6));
    _text(canvas, formatNumber(hi, decimals: 0), Offset(plot.right - 40, plot.bottom + 6));

    void marker(double? v, Color color, {bool dashed = false, double width = 1.4}) {
      if (v == null || !v.isFinite) return;
      final double xx = x(v);
      final Paint p = Paint()
        ..color = color
        ..strokeWidth = width;
      if (!dashed) {
        canvas.drawLine(Offset(xx, plot.top), Offset(xx, plot.bottom), p);
        return;
      }
      for (double yy = plot.top; yy < plot.bottom; yy += 8) {
        canvas.drawLine(Offset(xx, yy), Offset(xx, math.min(yy + 4, plot.bottom)), p);
      }
    }

    marker(0, style.zero, width: 1.2);
    _text(canvas, '0', Offset(x(0) - 3, plot.bottom + 6));
    marker(mean, style.mean, dashed: true, width: 2);
    marker(median, style.median, dashed: true, width: 2);
  }

  void _text(Canvas canvas, String text, Offset at) {
    final TextPainter tp = TextPainter(
      text: TextSpan(text: text, style: axisStyle),
      textDirection: TextDirection.ltr,
    )..layout();
    tp.paint(canvas, at);
  }

  @override
  bool shouldRepaint(DistributionPainter old) =>
      !identical(old.bins, bins) ||
      old.mean != mean ||
      old.median != median ||
      old.hover != hover ||
      old.style != style ||
      old.axisStyle != axisStyle;
}
