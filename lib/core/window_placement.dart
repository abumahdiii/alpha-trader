import 'dart:math' as math;

import 'package:flutter/foundation.dart';

/// Pure geometry for placing the main window. No FFI, no platform channel:
/// the thin Win32 wrapper in `main_window.dart` feeds real monitor data in,
/// and the unit tests feed in fixed layouts (e.g. the user's dual-monitor
/// desk, where the old "1280x800 + center" put the title bar off-screen).
///
/// Every value is in PHYSICAL pixels in Windows virtual-screen coordinates,
/// exactly as Win32 reports them to a per-monitor-DPI-aware process. The
/// primary monitor's top-left is always (0, 0); other monitors may have
/// negative coordinates.

/// Caption height, caption-button strip width and invisible resize-border
/// tolerance of a standard `WS_OVERLAPPEDWINDOW` at 96 DPI (100 %). They are
/// scaled by the monitor's DPI factor before use.
const int kCaptionHeightAt96Dpi = 32;
const int kCaptionButtonsWidthAt96Dpi = 138; // minimize + maximize + close
const int kFrameToleranceAt96Dpi = 8;

/// Integer rectangle (right/bottom exclusive, like a Win32 `RECT`).
@immutable
class ScreenRect {
  const ScreenRect(this.left, this.top, this.right, this.bottom);

  const ScreenRect.ltwh(int left, int top, int width, int height)
      : this(left, top, left + width, top + height);

  final int left;
  final int top;
  final int right;
  final int bottom;

  int get width => right - left;
  int get height => bottom - top;
  bool get isEmpty => width <= 0 || height <= 0;

  bool containsPoint(int x, int y) =>
      x >= left && x < right && y >= top && y < bottom;

  @override
  bool operator ==(Object other) =>
      other is ScreenRect &&
      other.left == left &&
      other.top == top &&
      other.right == right &&
      other.bottom == bottom;

  @override
  int get hashCode => Object.hash(left, top, right, bottom);

  @override
  String toString() => 'L=$left T=$top R=$right B=$bottom (${width}x$height)';
}

/// One display as seen by `GetMonitorInfo`.
@immutable
class MonitorArea {
  const MonitorArea({
    required this.bounds,
    required this.workArea,
    this.isPrimary = false,
    this.scale = 1.0,
  });

  /// Whole monitor (`rcMonitor`).
  final ScreenRect bounds;

  /// Monitor minus taskbar / docked app bars (`rcWork`).
  final ScreenRect workArea;

  /// `MONITORINFOF_PRIMARY`.
  final bool isPrimary;

  /// Effective DPI / 96 (1.0 = 100 %, 1.5 = 150 %).
  final double scale;

  @override
  String toString() =>
      '${isPrimary ? 'primary ' : ''}monitor $bounds work=$workArea '
      'scale=$scale';
}

/// Result of [planInitialWindowPlacement].
@immutable
class InitialWindowPlacement {
  const InitialWindowPlacement({
    required this.restoreRect,
    required this.minimumWidth,
    required this.minimumHeight,
  });

  /// Physical-pixel restore-down rect (what "restore down" returns to).
  final ScreenRect restoreRect;

  /// Logical minimum size, shrunk so it never exceeds the primary work area
  /// (a minimum larger than the screen would push the title bar off it).
  final double minimumWidth;
  final double minimumHeight;

  @override
  String toString() =>
      'restore=$restoreRect min=${minimumWidth}x$minimumHeight (logical)';
}

/// The primary monitor: the one flagged primary, else the one containing
/// the origin (Windows always puts the primary there), else the first one.
MonitorArea? pickPrimaryMonitor(List<MonitorArea> monitors) {
  if (monitors.isEmpty) return null;
  for (final MonitorArea m in monitors) {
    if (m.isPrimary) return m;
  }
  for (final MonitorArea m in monitors) {
    if (m.bounds.containsPoint(0, 0)) return m;
  }
  return monitors.first;
}

/// A [width] x [height] rect, shrunk to fit [area] if needed, centered in it.
ScreenRect centeredRectIn(ScreenRect area, int width, int height) {
  final int w = math.max(1, math.min(width, area.width));
  final int h = math.max(1, math.min(height, area.height));
  final int left = area.left + (area.width - w) ~/ 2;
  final int top = area.top + (area.height - h) ~/ 2;
  return ScreenRect.ltwh(left, top, w, h);
}

/// Restore-down rect and minimum size for the main window: the desired
/// logical size, converted to physical pixels with the primary monitor's
/// scale, clamped to and centered in the primary monitor's WORK area (never
/// the whole virtual desktop, which is what spread the window across two
/// monitors before). Returns null when no monitor is known.
InitialWindowPlacement? planInitialWindowPlacement({
  required List<MonitorArea> monitors,
  required double desiredWidth,
  required double desiredHeight,
  required double minimumWidth,
  required double minimumHeight,
}) {
  final MonitorArea? primary = pickPrimaryMonitor(monitors);
  if (primary == null || primary.workArea.isEmpty) return null;
  final double scale = primary.scale > 0 ? primary.scale : 1.0;
  final ScreenRect work = primary.workArea;

  final ScreenRect restore = centeredRectIn(
    work,
    (desiredWidth * scale).round(),
    (desiredHeight * scale).round(),
  );
  return InitialWindowPlacement(
    restoreRect: restore,
    minimumWidth: math.min(minimumWidth, (work.width / scale).floorToDouble()),
    minimumHeight: math.min(
      minimumHeight,
      (work.height / scale).floorToDouble(),
    ),
  );
}

/// Whether the caption buttons (minimize and close) of a NON-maximized
/// window with outer rect [window] can be reached with the mouse: the
/// vertical middle of the caption, at both ends of the button strip, must
/// lie inside the work area of some monitor (not necessarily the same one,
/// so a window straddling two side-by-side monitors still counts).
///
/// The points are inset by the invisible resize border that
/// `GetWindowRect` includes on Windows 10/11.
bool isTitleBarReachable(
  ScreenRect window,
  List<MonitorArea> monitors, {
  double scale = 1.0,
}) {
  final int caption = (kCaptionHeightAt96Dpi * scale).round();
  final int buttons = (kCaptionButtonsWidthAt96Dpi * scale).round();
  final int tolerance = (kFrameToleranceAt96Dpi * scale).round();

  final int y = window.top + caption ~/ 2;
  final int closeX = window.right - tolerance - 1;
  final int minimizeX = math.max(
    window.left + tolerance,
    window.right - tolerance - buttons,
  );

  bool onSomeWorkArea(int x) =>
      monitors.any((MonitorArea m) => m.workArea.containsPoint(x, y));
  return onSomeWorkArea(closeX) && onSomeWorkArea(minimizeX);
}

/// Null when [window]'s title bar is reachable (or nothing can be judged
/// because no monitor is known); otherwise the same-size rect (shrunk to fit
/// if needed) centered in the primary monitor's work area.
ScreenRect? rescueWindowRect(ScreenRect window, List<MonitorArea> monitors) {
  final MonitorArea? primary = pickPrimaryMonitor(monitors);
  if (primary == null || primary.workArea.isEmpty) return null;
  if (isTitleBarReachable(window, monitors, scale: primary.scale)) return null;
  return centeredRectIn(primary.workArea, window.width, window.height);
}
