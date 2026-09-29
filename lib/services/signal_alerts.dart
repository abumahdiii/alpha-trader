import 'dart:async';
import 'dart:ffi' as ffi;
import 'dart:io';

import 'package:ffi/ffi.dart';
import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:win32/win32.dart';

import '../chart/chart_format.dart';
import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/signal_models.dart';

// Sound + Windows notification for each NEW live signal (phase 6).
// Display only: an alert tells the user a suggestion exists; nothing here
// (or anywhere in the app) can place an order.

/// Plays the alert sound and shows the OS notification. Implementations
/// must never throw into the UI (the dispatcher also guards every call).
abstract interface class SignalAlerter {
  Future<void> playSound();

  /// A tray balloon / toast with a Persian [title] and [body].
  Future<void> showNotification({required String title, required String body});

  /// Removes whatever the alerter left in the OS (the tray icon).
  Future<void> dispose();
}

/// Does nothing (non-Windows hosts).
class NoopSignalAlerter implements SignalAlerter {
  const NoopSignalAlerter();

  @override
  Future<void> playSound() async {}

  @override
  Future<void> showNotification({required String title, required String body}) async {}

  @override
  Future<void> dispose() async {}
}

/// Resolves the main window handle (`windowManager.getId()` in the app).
typedef WindowHandleResolver = Future<int> Function();

typedef _MessageBeepNative = ffi.Int32 Function(ffi.Uint32 uType);
typedef _MessageBeepDart = int Function(int uType);

/// Windows implementation over `package:win32` (no new package, no asset):
/// * sound: the system «Notification» sound alias via `PlaySound`
///   (`SND_ALIAS | SND_ASYNC`), falling back to `MessageBeep`;
/// * notification: a tray icon (the app icon) added once with
///   `Shell_NotifyIcon(NIM_ADD)`, then an `NIF_INFO` balloon per signal
///   (shown by Windows 10/11 as a toast). The balloon is silent
///   (`NIIF_NOSOUND`): the sound is the separate «صدا» toggle. The icon is
///   removed by [dispose] (app close).
class WindowsSignalAlerter implements SignalAlerter {
  WindowsSignalAlerter({required WindowHandleResolver windowHandle}) : _windowHandle = windowHandle;

  final WindowHandleResolver _windowHandle;

  /// Tray icon id of this app (any constant; one icon per window).
  static const int _iconId = 0xA17A;

  /// `IDI_APP_ICON` of `windows/runner/resource.h`.
  static const int _appIconResource = 101;

  static const String _soundAlias = 'SystemNotification';

  /// Kept for the process lifetime: `SND_ASYNC` may read it after the call.
  static final ffi.Pointer<Utf16> _soundAliasPtr = _soundAlias.toNativeUtf16();

  int? _hwnd;
  bool _iconAdded = false;

  @override
  Future<void> playSound() async {
    final int ok = PlaySound(_soundAliasPtr, 0, SND_ALIAS | SND_ASYNC | SND_NODEFAULT);
    if (ok != 0) {
      _log('sound: PlaySound($_soundAlias) ok');
      return;
    }
    final _MessageBeepDart beep =
        ffi.DynamicLibrary.open('user32.dll').lookupFunction<_MessageBeepNative, _MessageBeepDart>('MessageBeep');
    final int beeped = beep(MB_ICONASTERISK);
    _log('sound: PlaySound($_soundAlias) failed -> MessageBeep ${beeped != 0 ? 'ok' : 'failed'}');
  }

  @override
  Future<void> showNotification({required String title, required String body}) async {
    final int hwnd = _hwnd ??= await _windowHandle();
    final ffi.Pointer<NOTIFYICONDATA> nid = calloc<NOTIFYICONDATA>();
    try {
      _fill(nid, hwnd);
      if (!_iconAdded) {
        nid.ref
          ..uFlags = NIF_ICON | NIF_TIP
          ..hIcon = _icon()
          ..szTip = _clip('Alpha Trader — سیگنال لایو (فقط پیشنهاد)', 127);
        if (Shell_NotifyIcon(NIM_ADD, nid) == 0) {
          _log('notification: NIM_ADD failed (hwnd=$hwnd)');
          return;
        }
        _iconAdded = true;
        _log('notification: tray icon added (hwnd=$hwnd)');
      }
      _fill(nid, hwnd);
      nid.ref
        ..uFlags = NIF_INFO
        ..szInfoTitle = _clip(title, 63)
        ..szInfo = _clip(body, 255)
        ..dwInfoFlags = NIIF_INFO | NIIF_NOSOUND;
      final int ok = Shell_NotifyIcon(NIM_MODIFY, nid);
      _log('notification: balloon ${ok != 0 ? 'shown' : 'FAILED'} "$title"');
    } finally {
      calloc.free(nid);
    }
  }

  @override
  Future<void> dispose() async {
    final int? hwnd = _hwnd;
    if (!_iconAdded || hwnd == null) return;
    final ffi.Pointer<NOTIFYICONDATA> nid = calloc<NOTIFYICONDATA>();
    try {
      _fill(nid, hwnd);
      final int ok = Shell_NotifyIcon(NIM_DELETE, nid);
      _iconAdded = false;
      _log('notification: tray icon removed (${ok != 0 ? 'ok' : 'failed'})');
    } finally {
      calloc.free(nid);
    }
  }

  static void _fill(ffi.Pointer<NOTIFYICONDATA> nid, int hwnd) {
    nid.ref
      ..cbSize = ffi.sizeOf<NOTIFYICONDATA>()
      ..hWnd = hwnd
      ..uID = _iconId;
  }

  static int _icon() {
    final int app = LoadIcon(GetModuleHandle(ffi.nullptr), MAKEINTRESOURCE(_appIconResource));
    return app != 0 ? app : LoadIcon(0, IDI_INFORMATION);
  }

  /// Fits a fixed WCHAR buffer of [max] characters + the terminator.
  static String _clip(String s, int max) => s.length <= max ? s : '${s.substring(0, max - 1)}…';

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[SignalAlerts] $message');
    AppLogger.log('[SignalAlerts] $message');
  }
}

/// The alerter for this host: Windows -> [WindowsSignalAlerter], else no-op.
SignalAlerter platformSignalAlerter({required WindowHandleResolver windowHandle}) =>
    Platform.isWindows ? WindowsSignalAlerter(windowHandle: windowHandle) : const NoopSignalAlerter();

// ------------------------------------------------------------------ preferences

/// Local alert toggles «صدا» and «اعلان ویندوز» (both default ON),
/// persisted with `shared_preferences`.
class SignalAlertPrefs extends ChangeNotifier {
  SignalAlertPrefs({bool sound = true, bool notification = true})
      : _sound = sound,
        _notification = notification;

  static const String soundKey = 'signals.alert_sound';
  static const String notificationKey = 'signals.alert_notification';

  /// Reads the stored toggles (missing = on).
  static Future<SignalAlertPrefs> load() async {
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    final SignalAlertPrefs p = SignalAlertPrefs(
      sound: prefs.getBool(soundKey) ?? true,
      notification: prefs.getBool(notificationKey) ?? true,
    );
    devLog('[SignalAlerts] prefs loaded: sound=${p.sound} notification=${p.notification}');
    return p;
  }

  bool _sound;
  bool _notification;

  bool get sound => _sound;
  bool get notification => _notification;

  Future<void> setSound(bool value) => _set(soundKey, value, () => _sound = value, _sound);

  Future<void> setNotification(bool value) =>
      _set(notificationKey, value, () => _notification = value, _notification);

  Future<void> _set(String key, bool value, VoidCallback apply, bool current) async {
    if (value == current) return;
    apply();
    notifyListeners();
    devLog('[SignalAlerts] pref $key -> $value');
    try {
      final SharedPreferences prefs = await SharedPreferences.getInstance();
      await prefs.setBool(key, value);
    } catch (e) {
      devLog('[SignalAlerts] pref $key could not be stored: $e');
    }
  }
}

// ------------------------------------------------------------------ dispatcher

/// Persian notification title of a signal: `سیگنال جدید: XAUUSD — خرید`.
String signalAlertTitle(LiveSignal s) => 'سیگنال جدید: ${s.symbol} — ${s.direction?.titleFa ?? '—'}';

/// Persian notification body: indicative entry and SL with the symbol's
/// digits (engine values, formatting only) and the suggestion-only note.
String signalAlertBody(LiveSignal s, int? digits) =>
    'ورود تقریبی ${formatChartPrice(s.indicativeEntry, digits)} | حد ضرر ${formatChartPrice(s.stopLoss, digits)}\n'
    'فقط پیشنهاد است؛ هیچ سفارشی ارسال نمی‌شود.';

/// Fires the sound and the OS notification for every NEW signal of
/// [signals] (e.g. `SignalsProvider.newSignals`), each only when its
/// [prefs] toggle is on. Every failure is caught and logged: an alert can
/// never break the app.
class SignalAlertDispatcher {
  SignalAlertDispatcher({
    required Stream<LiveSignal> signals,
    required this.alerter,
    required this.prefs,
    int? Function(String symbol)? digitsOf,
  }) : _digitsOf = digitsOf ?? ((_) => null) {
    _sub = signals.listen((LiveSignal s) => unawaited(alert(s)));
  }

  final SignalAlerter alerter;
  final SignalAlertPrefs prefs;
  final int? Function(String symbol) _digitsOf;
  late final StreamSubscription<LiveSignal> _sub;

  /// Sound / notification calls made (read by tests and DEV logs).
  int soundsFired = 0;
  int notificationsFired = 0;
  int failures = 0;

  Future<void> alert(LiveSignal s) async {
    _log('new signal #${s.id} ${s.symbol}: sound=${prefs.sound} notification=${prefs.notification}');
    if (prefs.sound) {
      try {
        await alerter.playSound();
        soundsFired++;
      } catch (e, st) {
        failures++;
        _log('sound failed: $e\n$st');
      }
    }
    if (prefs.notification) {
      try {
        await alerter.showNotification(title: signalAlertTitle(s), body: signalAlertBody(s, _digitsOf(s.symbol)));
        notificationsFired++;
      } catch (e, st) {
        failures++;
        _log('notification failed: $e\n$st');
      }
    }
  }

  /// Stops listening and removes the tray icon (app close).
  Future<void> dispose() async {
    await _sub.cancel();
    try {
      await alerter.dispose();
    } catch (e) {
      _log('alerter dispose failed: $e');
    }
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[SignalAlerts] $message');
    AppLogger.log('[SignalAlerts] $message');
  }
}
