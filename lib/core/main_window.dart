import 'dart:async';
import 'dart:ffi' as ffi;

import 'package:ffi/ffi.dart';
import 'package:flutter/widgets.dart';
import 'package:win32/win32.dart';
import 'package:window_manager/window_manager.dart';

import 'app_logger.dart';
import 'dev_mode.dart';
import 'window_placement.dart';

/// Restore-down size of the main window (logical pixels). The window opens
/// maximized; this is only what "restore down" returns to.
const Size kRestoreWindowSize = Size(1280, 800);

/// Smallest useful layout (logical pixels); shrunk on screens that are
/// smaller than this (see [planInitialWindowPlacement]).
const Size kMinimumWindowSize = Size(1000, 700);

// Main-window placement on Windows, in two steps around `runApp`:
//
// 1. [prepareMainWindow] (window still hidden): restore-down rect clamped
//    to the primary monitor's work area, minimum size, title.
// 2. [maximizeMainWindowWhenShown] (after the first frame): wait until the
//    runner's first-frame `Show()` has actually made the window visible,
//    THEN maximize, verify with `isMaximized()` and retry once. As a last
//    resort, pull an off-screen title bar back onto the primary monitor.
//
// Why maximize only after `Show()`: the runner shows the window after the
// first frame, and a maximize done before that used to be undone by its
// `ShowWindow(SW_SHOWNORMAL)` (reproduced 2026-09-27: `IsZoomed=false`,
// title bar at top=-161 on a dual-monitor desk). The runner itself now also
// keeps an already visible/maximized window as it is (win32_window.cpp).
//
// All geometry decisions live in the pure `window_placement.dart`; this
// file only reads/writes the real window through Win32 and window_manager.
// A window-chrome failure must never stop the app, so everything here
// catches and logs.

/// Step 1 (before `runApp`). Never throws.
Future<void> prepareMainWindow() async {
  try {
    final List<MonitorArea> monitors = readMonitors();
    _log('[Window] monitors (${monitors.length}):');
    for (final MonitorArea m in monitors) {
      _log('[Window]   $m');
    }
    final InitialWindowPlacement? plan = planInitialWindowPlacement(
      monitors: monitors,
      desiredWidth: kRestoreWindowSize.width,
      desiredHeight: kRestoreWindowSize.height,
      minimumWidth: kMinimumWindowSize.width,
      minimumHeight: kMinimumWindowSize.height,
    );
    _log('[Window] initial placement: ${plan ?? 'none (no monitor info)'}');

    // No size/center here: those used to center 1280x800 on the whole
    // virtual desktop. The restore rect is applied below in physical px.
    final WindowOptions options = WindowOptions(
      minimumSize: plan != null
          ? Size(plan.minimumWidth, plan.minimumHeight)
          : kMinimumWindowSize,
      title: 'Alpha Trader',
    );
    // Hard guarantee that startup never hangs on a wedged platform channel.
    await windowManager
        .waitUntilReadyToShow(options)
        .timeout(const Duration(seconds: 5));

    if (plan != null) {
      final int hwnd = await windowManager.getId();
      final bool ok = _setWindowRect(hwnd, plan.restoreRect);
      _log('[Window] restore rect set to ${plan.restoreRect}: ok=$ok '
          '(now ${readWindowRect(hwnd)})');
    }
  } catch (e, s) {
    _log('[Window] prepareMainWindow failed: $e');
    _log('Stack trace:\n$s');
  }
}

/// Step 2 (schedule from a post-frame callback after `runApp`). Never throws.
Future<void> maximizeMainWindowWhenShown() async {
  try {
    await WidgetsBinding.instance.waitUntilFirstFrameRasterized;
    final bool visible = await _waitFor(
      windowManager.isVisible,
      const Duration(seconds: 3),
    );
    if (!visible) {
      _log('[Window] runner did not show the window within 3 s -> show()');
      await windowManager.show();
    }

    bool maximized = false;
    for (int attempt = 1; attempt <= 2 && !maximized; attempt++) {
      // maximize() only posts WM_SYSCOMMAND/SC_MAXIMIZE, so poll for it.
      await windowManager.maximize();
      maximized = await _waitFor(
        windowManager.isMaximized,
        const Duration(seconds: 1),
      );
      _log('[Window] maximize attempt $attempt: isMaximized=$maximized');
    }

    final int hwnd = await windowManager.getId();
    final ScreenRect? rect = readWindowRect(hwnd);
    _log('[Window] final window rect: $rect (maximized=$maximized)');
    if (maximized || rect == null) return;

    // Maximize failed twice: at least make sure the caption buttons are
    // reachable.
    final List<MonitorArea> monitors = readMonitors();
    final ScreenRect? rescue = rescueWindowRect(rect, monitors);
    if (rescue == null) {
      _log('[Window] title bar is reachable -> leaving the window as is');
      return;
    }
    final bool ok = _setWindowRect(hwnd, rescue);
    _log('[Window] title bar off-screen -> moved to $rescue: ok=$ok');
  } catch (e, s) {
    _log('[Window] maximizeMainWindowWhenShown failed: $e');
    _log('Stack trace:\n$s');
  }
}

/// Polls [check] every 50 ms until it returns true or [limit] passes.
Future<bool> _waitFor(Future<bool> Function() check, Duration limit) async {
  final Stopwatch sw = Stopwatch()..start();
  while (true) {
    if (await check()) return true;
    if (sw.elapsed >= limit) return false;
    await Future<void>.delayed(const Duration(milliseconds: 50));
  }
}

// ---------------------------------------------------------------------------
// Thin Win32 wrapper (physical pixels, virtual-screen coordinates)
// ---------------------------------------------------------------------------

/// Monitors collected by [_monitorEnumProc] during one [readMonitors] call.
/// EnumDisplayMonitors calls back synchronously on this thread.
List<MonitorArea>? _enumerated;

/// Every attached monitor with its work area and DPI scale; empty on failure.
List<MonitorArea> readMonitors() {
  final List<MonitorArea> result = <MonitorArea>[];
  _enumerated = result;
  try {
    final ffi.Pointer<ffi.NativeFunction<MONITORENUMPROC>> callback =
        ffi.Pointer.fromFunction<MONITORENUMPROC>(_monitorEnumProc, 0);
    if (EnumDisplayMonitors(NULL, ffi.nullptr, callback, 0) == 0) {
      _log('[Window] EnumDisplayMonitors failed');
    }
  } catch (e) {
    _log('[Window] monitor enumeration threw: $e');
  } finally {
    _enumerated = null;
  }
  return result;
}

int _monitorEnumProc(int hMonitor, int hdc, ffi.Pointer lpRect, int lParam) {
  final ffi.Pointer<MONITORINFO> info = calloc<MONITORINFO>();
  final ffi.Pointer<ffi.Uint32> dpiX = calloc<ffi.Uint32>();
  final ffi.Pointer<ffi.Uint32> dpiY = calloc<ffi.Uint32>();
  try {
    info.ref.cbSize = ffi.sizeOf<MONITORINFO>();
    if (GetMonitorInfo(hMonitor, info) == 0) return TRUE;
    double scale = 1.0;
    try {
      if (GetDpiForMonitor(hMonitor, MDT_EFFECTIVE_DPI, dpiX, dpiY) == S_OK &&
          dpiX.value > 0) {
        scale = dpiX.value / 96.0;
      }
    } catch (_) {
      // shcore missing (pre-8.1): assume 100 %.
    }
    _enumerated?.add(
      MonitorArea(
        bounds: _fromRect(info.ref.rcMonitor),
        workArea: _fromRect(info.ref.rcWork),
        isPrimary: (info.ref.dwFlags & MONITORINFOF_PRIMARY) != 0,
        scale: scale,
      ),
    );
    return TRUE;
  } finally {
    calloc.free(info);
    calloc.free(dpiX);
    calloc.free(dpiY);
  }
}

/// Outer window rect (`GetWindowRect`), or null on failure.
ScreenRect? readWindowRect(int hwnd) {
  final ffi.Pointer<RECT> rect = calloc<RECT>();
  try {
    if (GetWindowRect(hwnd, rect) == 0) return null;
    return _fromRect(rect.ref);
  } finally {
    calloc.free(rect);
  }
}

bool _setWindowRect(int hwnd, ScreenRect r) =>
    SetWindowPos(
      hwnd,
      NULL,
      r.left,
      r.top,
      r.width,
      r.height,
      SWP_NOZORDER | SWP_NOACTIVATE,
    ) !=
    0;

ScreenRect _fromRect(RECT r) => ScreenRect(r.left, r.top, r.right, r.bottom);

void _log(String message) {
  if (!kDevMode) return;
  devLog(message);
  AppLogger.log(message);
}
