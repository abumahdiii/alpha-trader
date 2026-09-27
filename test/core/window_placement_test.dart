import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/core/window_placement.dart';

/// The user's desk (reproduced 2026-09-27): DISPLAY1 1366x768 at (0,0) with
/// a 40 px taskbar at the bottom, DISPLAY2 1280x720 at (1366,-131).
const MonitorArea display1 = MonitorArea(
  bounds: ScreenRect.ltwh(0, 0, 1366, 768),
  workArea: ScreenRect.ltwh(0, 0, 1366, 728),
  isPrimary: true,
);
const MonitorArea display2 = MonitorArea(
  bounds: ScreenRect.ltwh(1366, -131, 1280, 720),
  workArea: ScreenRect.ltwh(1366, -131, 1280, 720),
);
const List<MonitorArea> userDesk = [display1, display2];

/// The rect the window actually had in the repro (IsZoomed=false).
const ScreenRect reproRect = ScreenRect(1148, -161, 2428, 575);

InitialWindowPlacement plan(
  List<MonitorArea> monitors, {
  double w = 1280,
  double h = 800,
}) =>
    planInitialWindowPlacement(
      monitors: monitors,
      desiredWidth: w,
      desiredHeight: h,
      minimumWidth: 1000,
      minimumHeight: 700,
    )!;

void main() {
  group('user dual-monitor desk', () {
    test('restore rect is clamped to and centered in the PRIMARY work area',
        () {
      final InitialWindowPlacement p = plan(userDesk);
      expect(p.restoreRect, const ScreenRect(43, 0, 1323, 728));
      expect(p.restoreRect.width, 1280);
      expect(p.restoreRect.height, 728); // 800 does not fit in 728
      expect(isTitleBarReachable(p.restoreRect, userDesk), isTrue);
      expect(p.minimumWidth, 1000);
      expect(p.minimumHeight, 700);
    });

    test('same result when the primary is listed second', () {
      expect(
        plan(const [display2, display1]).restoreRect,
        const ScreenRect(43, 0, 1323, 728),
      );
    });

    test('the reproduced rect L=1148 T=-161 is detected and rescued', () {
      expect(isTitleBarReachable(reproRect, userDesk), isFalse);
      final ScreenRect? fixed = rescueWindowRect(reproRect, userDesk);
      // 1280x736 -> height clamped to 728, centered in the primary work area.
      expect(fixed, const ScreenRect(43, 0, 1323, 728));
      expect(isTitleBarReachable(fixed!, userDesk), isTrue);
      expect(rescueWindowRect(fixed, userDesk), isNull);
    });

    test('a window fully on DISPLAY2 (negative top) is left alone', () {
      const ScreenRect onDisplay2 = ScreenRect(1400, -120, 2600, 500);
      expect(isTitleBarReachable(onDisplay2, userDesk), isTrue);
      expect(rescueWindowRect(onDisplay2, userDesk), isNull);
    });

    test('caption buttons straddling DISPLAY1 / DISPLAY2 still count', () {
      // Minimize button on DISPLAY1, close button on DISPLAY2.
      const ScreenRect straddling = ScreenRect(200, 20, 1440, 700);
      expect(isTitleBarReachable(straddling, userDesk), isTrue);
    });

    test('title bar above DISPLAY2 top (-131) is off-screen', () {
      const ScreenRect aboveDisplay2 = ScreenRect(1400, -200, 2600, 400);
      expect(isTitleBarReachable(aboveDisplay2, userDesk), isFalse);
      expect(
        rescueWindowRect(aboveDisplay2, userDesk),
        centeredRectIn(display1.workArea, 1200, 600),
      );
    });

    test('caption buttons off the right edge of DISPLAY2 are off-screen', () {
      const ScreenRect pastRight = ScreenRect(2000, 0, 2800, 600);
      expect(isTitleBarReachable(pastRight, userDesk), isFalse);
    });

    test('a window in the gap below DISPLAY2 (x>1366, y>589) is off-screen',
        () {
      const ScreenRect inGap = ScreenRect(1500, 600, 2500, 1200);
      expect(isTitleBarReachable(inGap, userDesk), isFalse);
    });

    test('a window whose top sits in the taskbar strip is off-screen', () {
      const ScreenRect overTaskbar = ScreenRect(100, 735, 1100, 1400);
      expect(isTitleBarReachable(overTaskbar, userDesk), isFalse);
    });
  });

  group('single monitor', () {
    const MonitorArea fullHd = MonitorArea(
      bounds: ScreenRect.ltwh(0, 0, 1920, 1080),
      workArea: ScreenRect.ltwh(0, 0, 1920, 1040),
      isPrimary: true,
    );

    test('1280x800 fits and is centered', () {
      final InitialWindowPlacement p = plan(const [fullHd]);
      expect(p.restoreRect, const ScreenRect(320, 120, 1600, 920));
    });

    test('150 % scale converts logical to physical and still clamps', () {
      const MonitorArea scaled = MonitorArea(
        bounds: ScreenRect.ltwh(0, 0, 1920, 1080),
        workArea: ScreenRect.ltwh(0, 0, 1920, 1032),
        isPrimary: true,
        scale: 1.5,
      );
      final InitialWindowPlacement p = plan(const [scaled]);
      // 1280x800 logical = 1920x1200 physical -> clamped to 1920x1032.
      expect(p.restoreRect, const ScreenRect(0, 0, 1920, 1032));
      // 1032 / 1.5 = 688 logical < 700: the minimum height shrinks too.
      expect(p.minimumWidth, 1000);
      expect(p.minimumHeight, 688);
    });

    test('work area with a top taskbar keeps the rect below it', () {
      const MonitorArea topBar = MonitorArea(
        bounds: ScreenRect.ltwh(0, 0, 1920, 1080),
        workArea: ScreenRect(0, 48, 1920, 1080),
        isPrimary: true,
      );
      final ScreenRect r = plan(const [topBar]).restoreRect;
      expect(r.top, greaterThanOrEqualTo(48));
      expect(r, const ScreenRect(320, 164, 1600, 964));
    });

    test('a window dragged above the screen is rescued', () {
      const ScreenRect above = ScreenRect(300, -40, 1580, 760);
      expect(isTitleBarReachable(above, const [fullHd]), isFalse);
      expect(
        rescueWindowRect(above, const [fullHd]),
        const ScreenRect(320, 120, 1600, 920),
      );
    });

    test('the invisible resize border at x=-7 is tolerated', () {
      // A window snapped to the left edge reports left=-7 on Windows 10/11.
      const ScreenRect snapped = ScreenRect(-7, 0, 1273, 800);
      expect(isTitleBarReachable(snapped, const [fullHd]), isTrue);
      // ... and so is the right edge.
      const ScreenRect snappedRight = ScreenRect(647, 0, 1927, 800);
      expect(isTitleBarReachable(snappedRight, const [fullHd]), isTrue);
    });
  });

  group('small screen', () {
    const MonitorArea small = MonitorArea(
      bounds: ScreenRect.ltwh(0, 0, 1024, 600),
      workArea: ScreenRect.ltwh(0, 0, 1024, 560),
      isPrimary: true,
    );

    test('restore rect and minimum size shrink to the work area', () {
      final InitialWindowPlacement p = plan(const [small]);
      expect(p.restoreRect, const ScreenRect(0, 0, 1024, 560));
      expect(p.minimumWidth, 1000);
      expect(p.minimumHeight, 560);
      expect(isTitleBarReachable(p.restoreRect, const [small]), isTrue);
    });

    test('an oversized off-screen window is shrunk while rescued', () {
      final ScreenRect? r = rescueWindowRect(
        const ScreenRect(-500, -300, 1500, 900),
        const [small],
      );
      expect(r, const ScreenRect(0, 0, 1024, 560));
    });
  });

  group('negative-coordinate monitors', () {
    // Secondary to the LEFT of and above the primary.
    const MonitorArea primary = MonitorArea(
      bounds: ScreenRect.ltwh(0, 0, 1920, 1080),
      workArea: ScreenRect.ltwh(0, 0, 1920, 1040),
      isPrimary: true,
    );
    const MonitorArea leftUp = MonitorArea(
      bounds: ScreenRect.ltwh(-1600, -400, 1600, 900),
      workArea: ScreenRect.ltwh(-1600, -400, 1600, 900),
    );
    const List<MonitorArea> desk = [leftUp, primary];

    test('a window on the negative monitor is reachable', () {
      const ScreenRect r = ScreenRect(-1500, -350, -300, 400);
      expect(isTitleBarReachable(r, desk), isTrue);
    });

    test('above the negative monitor -> rescued onto the primary', () {
      const ScreenRect r = ScreenRect(-1500, -450, -300, 300);
      expect(isTitleBarReachable(r, desk), isFalse);
      expect(rescueWindowRect(r, desk), const ScreenRect(360, 145, 1560, 895));
    });

    test('primary is picked by flag, not by list order or origin', () {
      expect(pickPrimaryMonitor(desk), primary);
    });

    test('without a primary flag the monitor at the origin is primary', () {
      const MonitorArea unflagged = MonitorArea(
        bounds: ScreenRect.ltwh(0, 0, 1920, 1080),
        workArea: ScreenRect.ltwh(0, 0, 1920, 1040),
      );
      expect(pickPrimaryMonitor(const [leftUp, unflagged]), unflagged);
    });
  });

  group('no monitor information', () {
    test('nothing is planned or rescued', () {
      expect(
        planInitialWindowPlacement(
          monitors: const [],
          desiredWidth: 1280,
          desiredHeight: 800,
          minimumWidth: 1000,
          minimumHeight: 700,
        ),
        isNull,
      );
      expect(rescueWindowRect(reproRect, const []), isNull);
      expect(pickPrimaryMonitor(const []), isNull);
    });
  });
}
