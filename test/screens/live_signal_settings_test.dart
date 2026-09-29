import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/screens/settings/live_signal_settings.dart';
import 'package:alpha_trader/screens/settings/settings_screen.dart';
import 'package:alpha_trader/services/signal_alerts.dart';
import 'package:alpha_trader/widgets/section_card.dart';

import '../helpers/engine_fakes.dart';
import '../helpers/plugin_fakes.dart';
import '../helpers/signal_fakes.dart';

Map<String, Object?> _settings({bool enabled = false, String strategy = 'stddev_channel', num grace = 20}) =>
    {'enabled': enabled, 'live_strategy': strategy, 'grace_s': grace};

/// Settings routes; [post] answers `POST /signals/settings`.
FakeEngineHttp _http({RouteHandler? post, bool pluginsFail = false}) => FakeEngineHttp({
      'GET /settings': (_) => jsonBody(settingsJson()),
      'GET /signals/settings': (_) => jsonBody(_settings()),
      'GET /strategies': (_) => jsonBody([strategyJson(), pluginStrategyJson()]),
      'GET /plugins': (_) => pluginsFail
          ? throw DioException.connectionError(requestOptions: RequestOptions(path: '/plugins'), reason: 'refused')
          : jsonBody([pluginJson()]),
      if (post != null) 'POST /signals/settings': post,
    });

Future<EngineHarness> _pump(WidgetTester tester, FakeEngineHttp http, {SignalAlertPrefs? prefs}) async {
  tester.view.physicalSize = const Size(1400, 2400);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final EngineHarness h = EngineHarness(http: http, alertPrefs: prefs);
  await tester.pumpWidget(h.wrap(const SettingsScreen()));
  await h.start(tester);
  await settle(tester, 10);
  return h;
}

Finder _key(String k) => find.byKey(ValueKey<String>(k));

bool _switch(WidgetTester tester, String key) => tester.widget<SwitchListTile>(_key(key)).value;

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('section loads: switch off, builtin strategies only (plugin not offered), grace value', (tester) async {
    final EngineHarness h = await _pump(tester, _http());
    expect(find.text(LiveSignalSettingsSection.title), findsOneWidget);
    expect(h.http.sent('GET /signals/settings'), hasLength(1));
    expect(_switch(tester, 'live-enabled-switch'), isFalse);
    expect(find.text('20'), findsOneWidget);
    expect(find.text(LiveSignalSettingsForm.pluginsNoteFa), findsOneWidget);

    await tester.tap(_key('live-strategy-selector'));
    await settle(tester);
    expect(_key('strategy-option-stddev_channel'), findsWidgets);
    expect(_key('strategy-option-$kPluginName'), findsNothing, reason: 'plugins cannot be used live');
    await tester.tapAt(const Offset(5, 5));
    await settle(tester);
    await h.dispose(tester);
  });

  testWidgets('enable -> POST {enabled: true}; success toast; the provider gets the returned status', (tester) async {
    final EngineHarness h = await _pump(
      tester,
      _http(post: (_) => jsonBody({..._settings(enabled: true), 'status': signalStatusJson(state: 'starting')})),
    );
    await tester.tap(_key('live-enabled-switch'));
    await settle(tester);

    expect(FakeEngineHttp.bodyOf(h.http.sent('POST /signals/settings').single), {'enabled': true});
    expect(_switch(tester, 'live-enabled-switch'), isTrue);
    expect(find.text(LiveSignalSettingsForm.savedToast), findsOneWidget);
    expect(h.signals.status!.enabled, isTrue);
    await h.dispose(tester);
  });

  testWidgets('409 plugin_not_live: the engine Persian message is shown, the live strategy stays', (tester) async {
    const String pluginNotLiveFa =
        'سیستم‌های بارگذاری‌شده (پلاگین) فعلا فقط در چارت و بک‌تست قابل استفاده‌اند و برای سیگنال لایو انتخاب نمی‌شوند.';
    // GET /plugins fails, so the selector cannot tell the plugin apart: the engine refuses it.
    final EngineHarness h = await _pump(
      tester,
      _http(pluginsFail: true, post: (_) => engineError(409, 'plugin_not_live', pluginNotLiveFa)),
    );
    await tester.tap(_key('live-strategy-selector'));
    await settle(tester);
    await tester.tap(find.text(kPluginTitleFa).last);
    await settle(tester);

    expect(FakeEngineHttp.bodyOf(h.http.sent('POST /signals/settings').single), {'live_strategy': kPluginName});
    expect(find.descendant(of: find.byType(ErrorBanner), matching: find.text(pluginNotLiveFa)), findsOneWidget);
    expect(find.text(LiveSignalSettingsForm.failedToast), findsOneWidget);
    expect(find.text('کانال انحراف معیار'), findsWidgets, reason: 'the saved strategy is still shown');
    await h.dispose(tester);
  });

  testWidgets('grace: out of range refused locally (no request); valid value posted; engine 422 under the field',
      (tester) async {
    int posts = 0;
    final EngineHarness h = await _pump(
      tester,
      _http(post: (_) {
        posts++;
        return posts == 1
            ? jsonBody({..._settings(grace: 45), 'status': signalStatusJson()})
            : engineError(422, 'invalid_body', 'تنظیمات سیگنال لایو نامعتبر است.',
                ['«مهلت پس از بسته شدن کندل (ثانیه)» باید بین 0 و 600 باشد.', 'فیلد ناشناخته: \'x\'.']);
      }),
    );

    await tester.enterText(_key('live-grace-field'), '700');
    await tester.tap(_key('live-grace-save'));
    await settle(tester);
    expect(find.textContaining('باید بین 0 و 600 ثانیه باشد'), findsOneWidget);
    expect(h.http.sent('POST /signals/settings'), isEmpty);

    await tester.enterText(_key('live-grace-field'), '45');
    await tester.tap(_key('live-grace-save'));
    await settle(tester);
    expect(FakeEngineHttp.bodyOf(h.http.sent('POST /signals/settings').single), {'grace_s': 45});
    await tester.pump(const Duration(milliseconds: 300)); // the old error fades out
    expect(find.textContaining('باید بین 0 و 600 ثانیه باشد'), findsNothing);

    await tester.enterText(_key('live-grace-field'), '30');
    await tester.tap(_key('live-grace-save'));
    await settle(tester);
    final TextField field = tester.widget<TextField>(_key('live-grace-field'));
    expect(field.decoration!.errorText, '«مهلت پس از بسته شدن کندل (ثانیه)» باید بین 0 و 600 باشد.');
    expect(find.descendant(of: find.byType(ErrorBanner), matching: find.text('تنظیمات سیگنال لایو نامعتبر است.')),
        findsOneWidget);
    expect(find.text('فیلد ناشناخته: \'x\'.'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('local alert toggles «صدا» / «اعلان ویندوز» default on and persist', (tester) async {
    final SignalAlertPrefs prefs = await SignalAlertPrefs.load();
    final EngineHarness h = await _pump(tester, _http(), prefs: prefs);
    expect(_switch(tester, 'alert-sound-switch'), isTrue);
    expect(_switch(tester, 'alert-notification-switch'), isTrue);

    await tester.tap(_key('alert-sound-switch'));
    await settle(tester);
    expect(prefs.sound, isFalse);
    expect(_switch(tester, 'alert-sound-switch'), isFalse);
    await tester.tap(_key('alert-notification-switch'));
    await settle(tester);
    expect(prefs.notification, isFalse);

    final SharedPreferences sp = await SharedPreferences.getInstance();
    expect(sp.getBool(SignalAlertPrefs.soundKey), isFalse);
    expect(sp.getBool(SignalAlertPrefs.notificationKey), isFalse);
    await h.dispose(tester);
  });

  testWidgets('engine not running: a short note instead of the form; toggles still usable', (tester) async {
    final EngineHarness h = EngineHarness(http: _http());
    await tester.pumpWidget(h.wrap(const SingleChildScrollView(child: LiveSignalSettingsSection())));
    await settle(tester);
    expect(find.text(LiveSignalSettingsSection.engineDownFa), findsOneWidget);
    expect(_key('alert-sound-switch'), findsOneWidget);
    expect(h.http.requests, isEmpty);
    await h.dispose(tester);
  });
}
