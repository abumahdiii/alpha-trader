import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/models/signal_models.dart';
import 'package:alpha_trader/services/signal_alerts.dart';

import '../helpers/signal_fakes.dart';

LiveSignal _signal({String direction = 'buy'}) => LiveSignal.fromJson(signalJson(id: 11, direction: direction));

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('both toggles on: sound + Windows notification with a Persian title/body (symbol, side, entry, SL)', () async {
    final FakeSignalAlerter alerter = FakeSignalAlerter();
    final StreamController<LiveSignal> stream = StreamController<LiveSignal>();
    final SignalAlertDispatcher d = SignalAlertDispatcher(
      signals: stream.stream,
      alerter: alerter,
      prefs: SignalAlertPrefs(),
      digitsOf: (String s) => s == 'XAUUSD.x' ? 2 : null,
    );
    stream.add(_signal());
    await pumpEventQueue();

    expect(alerter.sounds, 1);
    final (String title, String body) = alerter.notifications.single;
    expect(title, 'سیگنال جدید: XAUUSD.x — خرید');
    expect(body, contains('ورود تقریبی 2064.68'));
    expect(body, contains('حد ضرر 2062.88'), reason: 'SL formatted with the symbol digits');
    expect(body, contains('هیچ سفارشی ارسال نمی‌شود'));
    expect(d.soundsFired, 1);
    expect(d.notificationsFired, 1);

    await d.dispose();
    expect(alerter.disposed, isTrue, reason: 'the tray icon is removed on close');
    await stream.close();
  });

  test('toggles are respected: sound only / notification only / none', () async {
    final FakeSignalAlerter alerter = FakeSignalAlerter();
    final SignalAlertPrefs prefs = SignalAlertPrefs(sound: true, notification: false);
    final SignalAlertDispatcher d =
        SignalAlertDispatcher(signals: const Stream<LiveSignal>.empty(), alerter: alerter, prefs: prefs);

    await d.alert(_signal());
    expect((alerter.sounds, alerter.notifications.length), (1, 0));

    await prefs.setSound(false);
    await prefs.setNotification(true);
    await d.alert(_signal(direction: 'sell'));
    expect((alerter.sounds, alerter.notifications.length), (1, 1));
    expect(alerter.notifications.single.$1, 'سیگنال جدید: XAUUSD.x — فروش');

    await prefs.setNotification(false);
    await d.alert(_signal());
    expect((alerter.sounds, alerter.notifications.length), (1, 1));
    await d.dispose();
  });

  test('a failing alerter never throws into the caller', () async {
    final FakeSignalAlerter alerter = FakeSignalAlerter(fail: true);
    final SignalAlertDispatcher d =
        SignalAlertDispatcher(signals: const Stream<LiveSignal>.empty(), alerter: alerter, prefs: SignalAlertPrefs());
    await expectLater(d.alert(_signal()), completes);
    expect(d.failures, 2);
    expect(d.soundsFired, 0);
    await d.dispose();
  });

  test('prefs default ON and persist in shared_preferences', () async {
    final SignalAlertPrefs first = await SignalAlertPrefs.load();
    expect((first.sound, first.notification), (true, true));
    int notified = 0;
    first.addListener(() => notified++);
    await first.setSound(false);
    await first.setSound(false); // unchanged: no write, no notify
    await first.setNotification(false);
    expect(notified, 2);

    final SharedPreferences sp = await SharedPreferences.getInstance();
    expect(sp.getBool(SignalAlertPrefs.soundKey), isFalse);
    expect(sp.getBool(SignalAlertPrefs.notificationKey), isFalse);
    final SignalAlertPrefs reloaded = await SignalAlertPrefs.load();
    expect((reloaded.sound, reloaded.notification), (false, false));
  });

  test('the host alerter: Windows implementation on Windows, no-op elsewhere (not invoked here)', () {
    final SignalAlerter a = platformSignalAlerter(windowHandle: () async => 0);
    expect(a, Platform.isWindows ? isA<WindowsSignalAlerter>() : isA<NoopSignalAlerter>());
  });
}
