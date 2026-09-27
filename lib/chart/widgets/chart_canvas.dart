import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../core/dev_mode.dart';
import '../chart_controller.dart';
import '../chart_data.dart';
import '../chart_painter.dart';
import '../chart_viewport.dart';

/// Interactive candlestick canvas: owns the viewport (it depends on the
/// pixel width), turns wheel / drag / pinch into zoom and pan, draws the
/// crosshair under the mouse and reports clicks: a setup marker first, else
/// the candle under the click, else an empty-area click. Always
/// left-to-right (time axis), even inside the RTL app.
class ChartCanvas extends StatefulWidget {
  const ChartCanvas({
    super.key,
    required this.data,
    required this.viewRequest,
    this.selectedSetupId,
    this.highlightIndex,
    this.onSetupTap,
    this.onCandleTap,
    this.onEmptyTap,
  });

  final ChartData data;
  final ChartViewRequest viewRequest;
  final String? selectedSetupId;
  final int? highlightIndex;

  /// A click on a setup marker (has priority over [onCandleTap]).
  final ValueChanged<SetupMark>? onSetupTap;

  /// A click on the column of bar `index` (not on a marker).
  final ValueChanged<int>? onCandleTap;

  /// A click on the chart that hits neither a marker nor a bar (beyond the
  /// data, or on the price / time axis).
  final VoidCallback? onEmptyTap;

  @override
  State<ChartCanvas> createState() => ChartCanvasState();
}

class ChartCanvasState extends State<ChartCanvas> {
  final ValueNotifier<Offset?> _hover = ValueNotifier<Offset?>(null);
  ChartViewport? _viewport;
  int _appliedRequest = -1;
  ChartStyle? _style;
  ThemeData? _styleTheme;
  double _lastScale = 1;

  PriceScale? _scale;
  ChartLayout? _layout;

  /// Current viewport, price scale and layout (read by tests).
  ChartViewport? get viewport => _viewport;
  PriceScale? get priceScale => _scale;
  ChartLayout? get layout => _layout;

  @override
  void didUpdateWidget(ChartCanvas oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.data, widget.data)) {
      // New load: start from the newest bars; a pending request re-applies.
      _viewport = null;
      _appliedRequest = -1;
    }
  }

  @override
  void dispose() {
    _hover.dispose();
    super.dispose();
  }

  ChartStyle _styleFor(ThemeData theme) {
    if (_style == null || !identical(_styleTheme, theme)) {
      _style = ChartStyle.fromTheme(theme);
      _styleTheme = theme;
    }
    return _style!;
  }

  /// Syncs the viewport with the plot width and applies a new view request.
  /// Runs during build without setState: it only derives state for this build.
  ChartViewport _syncViewport(double plotWidth) {
    ChartViewport v = _viewport ?? ChartViewport(barCount: widget.data.length, width: plotWidth);
    v = v.withWidth(plotWidth);
    final ChartViewRequest req = widget.viewRequest;
    if (req.serial != _appliedRequest) {
      _appliedRequest = req.serial;
      if (req.reset) v = v.reset();
      final int? focus = req.focusIndex;
      if (focus != null && focus >= 0 && focus < widget.data.length) v = v.centerOn(focus);
      devLog('[Chart] view request #${req.serial}: reset=${req.reset} focus=${req.focusIndex} -> $v');
    }
    return _viewport = v;
  }

  void _update(ChartViewport Function(ChartViewport v) change) {
    final ChartViewport? v = _viewport;
    if (v == null) return;
    final ChartViewport next = change(v);
    if (next != v) setState(() => _viewport = next);
  }

  void _setHover(Offset? p) => _hover.value = p;

  void _onPointerSignal(PointerSignalEvent event, ChartLayout layout) {
    if (event is! PointerScrollEvent) return;
    GestureBinding.instance.pointerSignalResolver.register(event, (PointerSignalEvent e) {
      final PointerScrollEvent s = e as PointerScrollEvent;
      final bool shift = HardwareKeyboard.instance.isShiftPressed;
      if (shift || (s.scrollDelta.dx != 0 && s.scrollDelta.dy == 0)) {
        // Shift+wheel / horizontal wheel: pan.
        final double d = s.scrollDelta.dx != 0 ? s.scrollDelta.dx : s.scrollDelta.dy;
        _update((ChartViewport v) => v.pan(-d));
      } else {
        // Wheel (with or without Ctrl): zoom around the cursor. One notch
        // (~100 px) = x1.2.
        final double factor = math.pow(1.2, -s.scrollDelta.dy / 100).toDouble();
        final double anchor = s.localPosition.dx.clamp(0.0, layout.plot.width);
        _update((ChartViewport v) => v.zoom(factor, anchor));
      }
    });
  }

  void _onTapUp(TapUpDetails d, ChartLayout layout, PriceScale scale) {
    final ChartViewport? v = _viewport;
    if (v == null) return;
    final Offset p = d.localPosition;
    if (!layout.inPlot(p)) {
      devLog('[Chart] tap outside the plot (axis) -> empty');
      widget.onEmptyTap?.call();
      return;
    }
    final SetupMark? hit = hitTestSetup(widget.data, v, scale, p);
    if (hit != null) {
      devLog('[Chart] tap -> setup marker ${hit.item.id} (marker has priority over the candle)');
      widget.onSetupTap?.call(hit);
      return;
    }
    final int? bar = v.barAt(p.dx);
    if (bar != null) {
      devLog('[Chart] tap -> candle $bar');
      widget.onCandleTap?.call(bar);
    } else {
      devLog('[Chart] tap -> no bar under x=${p.dx.toStringAsFixed(1)} -> empty');
      widget.onEmptyTap?.call();
    }
  }

  @override
  Widget build(BuildContext context) {
    final ChartStyle style = _styleFor(Theme.of(context));
    return Directionality(
      textDirection: TextDirection.ltr,
      child: LayoutBuilder(builder: (BuildContext context, BoxConstraints c) {
        final ChartLayout layout = ChartLayout(Size(c.maxWidth, c.maxHeight));
        final ChartViewport v = _syncViewport(layout.plot.width);
        final IndexRange? range = v.visibleRange;
        final PriceScale scale = range == null
            ? PriceScale(min: 0, max: 1, top: layout.plot.top, bottom: layout.plot.bottom)
            : autoscalePrice(
                widget.data,
                range,
                top: layout.plot.top + ChartLayout.plotPadTop,
                bottom: layout.plot.bottom - ChartLayout.plotPadBottom,
              );
        _scale = scale;
        _layout = layout;
        return Listener(
          onPointerSignal: (PointerSignalEvent e) => _onPointerSignal(e, layout),
          child: MouseRegion(
            cursor: SystemMouseCursors.precise,
            onHover: (PointerHoverEvent e) => _setHover(e.localPosition),
            onExit: (_) => _setHover(null),
            child: GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTapUp: (TapUpDetails d) => _onTapUp(d, layout, scale),
              onScaleStart: (ScaleStartDetails d) => _lastScale = 1,
              onScaleUpdate: (ScaleUpdateDetails d) {
                final double ratio = d.scale / _lastScale;
                _lastScale = d.scale;
                _update((ChartViewport v) {
                  ChartViewport next = v.pan(d.focalPointDelta.dx);
                  if (ratio != 1 && ratio.isFinite && ratio > 0) {
                    next = next.zoom(ratio, d.localFocalPoint.dx.clamp(0.0, layout.plot.width));
                  }
                  return next;
                });
                _setHover(d.localFocalPoint);
              },
              child: Stack(
                fit: StackFit.expand,
                children: <Widget>[
                  RepaintBoundary(
                    child: CustomPaint(
                      key: const ValueKey<String>('chart-static-layer'),
                      painter: ChartPainter(
                        data: widget.data,
                        viewport: v,
                        layout: layout,
                        scale: scale,
                        style: style,
                        selectedSetupId: widget.selectedSetupId,
                        highlightIndex: widget.highlightIndex,
                      ),
                    ),
                  ),
                  RepaintBoundary(
                    child: CustomPaint(
                      key: const ValueKey<String>('chart-crosshair-layer'),
                      painter: CrosshairPainter(
                        hover: _hover,
                        data: widget.data,
                        viewport: v,
                        layout: layout,
                        scale: scale,
                        style: style,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
      }),
    );
  }
}
