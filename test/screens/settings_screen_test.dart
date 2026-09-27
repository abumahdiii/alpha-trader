import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/screens/settings/settings_screen.dart';
import 'package:alpha_trader/widgets/engine_gate.dart';
import 'package:alpha_trader/widgets/engine_status_indicator.dart';

import '../helpers/engine_fakes.dart';

Finder _field(String label) => find.ancestor(of: find.text(label), matching: find.byType(TextFormField));

Future<void> _type(WidgetTester tester, String label, String text) async {
  await tester.enterText(_field(label), text);
  await tester.pump();
}

Finder _button(String label) =>
    find.ancestor(of: find.text(label), matching: find.bySubtype<ButtonStyleButton>());

bool _enabled(WidgetTester tester, String label) =>
    tester.widget<ButtonStyleButton>(_button(label).first).onPressed != null;

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('engine not running: friendly empty state, MT5 block unknown, no request', (tester) async {
    final EngineHarness h = EngineHarness();
    await tester.pumpWidget(h.wrap(const SettingsScreen()));
    await settle(tester);

    expect(find.text(EngineUnavailableView.title), findsOneWidget);
    expect(find.textContaining('وضعیت: متوقف'), findsOneWidget);
    expect(find.text(EngineStatusIndicator.mt5UnknownWhileEngineDown), findsOneWidget);
    expect(h.http.requests, isEmpty);
    await h.dispose(tester);
  });

  testWidgets('loads GET /settings when the engine starts and shows the MT5 block masked', (tester) async {
    final EngineHarness h = EngineHarness(
      http: FakeEngineHttp({'GET /settings': (_) => jsonBody(settingsJson(balance: 2500, riskPct: 0.5))}),
    );
    await tester.pumpWidget(h.wrap(const SettingsScreen()));
    await h.start(tester);

    expect(h.http.sent('GET /settings'), hasLength(1));
    expect(find.text('2500'), findsOneWidget);
    expect(find.text('0.5'), findsOneWidget);
    expect(find.text('100'), findsOneWidget);
    expect(find.text('2'), findsOneWidget);

    expect(find.text('****1234'), findsOneWidget);
    expect(find.text('SarmayeGozareBartar-Real'), findsOneWidget);
    expect(find.text('متصل'), findsOneWidget);

    // Nothing changed yet: save and discard are disabled.
    expect(_enabled(tester, AccountSettingsForm.saveLabel), isFalse);
    expect(_enabled(tester, AccountSettingsForm.discardLabel), isFalse);
    await h.dispose(tester);
  });

  testWidgets('edit -> dirty -> save sends only the change and shows a success toast', (tester) async {
    final EngineHarness h = EngineHarness(
      http: FakeEngineHttp({
        'GET /settings': (_) => jsonBody(settingsJson()),
        'PUT /settings': (_) => jsonBody(settingsJson(riskPct: 0.75)),
      }),
    );
    await tester.pumpWidget(h.wrap(const SettingsScreen()));
    await h.start(tester);

    await _type(tester, 'درصد ریسک هر معامله', '0.75');
    expect(find.text('تغییرات ذخیره نشده'), findsOneWidget);
    expect(_enabled(tester, AccountSettingsForm.saveLabel), isTrue);

    await tester.tap(_button(AccountSettingsForm.saveLabel));
    await settle(tester);

    expect(FakeEngineHttp.bodyOf(h.http.sent('PUT /settings').single), {'risk_pct': 0.75});
    expect(find.text(AccountSettingsForm.savedToast), findsOneWidget);
    expect(find.text('تغییرات ذخیره نشده'), findsNothing);
    expect(_enabled(tester, AccountSettingsForm.saveLabel), isFalse);
    await h.dispose(tester);
  });

  testWidgets('422: engine messages appear under their field, the rest on top', (tester) async {
    final EngineHarness h = EngineHarness(
      http: FakeEngineHttp({
        'GET /settings': (_) => jsonBody(settingsJson()),
        'PUT /settings': (_) => engineError(422, 'invalid_settings', 'تنظیمات حساب نامعتبر است.', [
              '«اهرم» نباید بیشتر از 1000 باشد.',
              'پیام عمومی بدون فیلد.',
            ]),
      }),
    );
    await tester.pumpWidget(h.wrap(const SettingsScreen()));
    await h.start(tester);

    await _type(tester, 'اهرم (1:N)', '5000');
    await tester.tap(_button(AccountSettingsForm.saveLabel));
    await settle(tester);

    final Finder leverageError = find.text('«اهرم» نباید بیشتر از 1000 باشد.');
    expect(leverageError, findsOneWidget);
    // Rendered in the leverage field's column, below the field.
    final Offset fieldPos = tester.getCenter(_field('اهرم (1:N)'));
    final Offset errorPos = tester.getCenter(leverageError);
    expect(errorPos.dy, greaterThan(fieldPos.dy));
    expect((errorPos.dx - fieldPos.dx).abs(), lessThan(200));

    expect(find.text('تنظیمات حساب نامعتبر است.'), findsWidgets);
    expect(find.text('پیام عمومی بدون فیلد.'), findsOneWidget);
    // Still dirty: the user can fix the value.
    expect(find.text('تغییرات ذخیره نشده'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('«بازگشت» discards the edits', (tester) async {
    final EngineHarness h = EngineHarness(
      http: FakeEngineHttp({'GET /settings': (_) => jsonBody(settingsJson())}),
    );
    await tester.pumpWidget(h.wrap(const SettingsScreen()));
    await h.start(tester);

    await _type(tester, 'موجودی حساب (دلار)', '5000');
    expect(find.text('5000'), findsOneWidget);
    await tester.tap(_button(AccountSettingsForm.discardLabel));
    await settle(tester);

    expect(find.text('5000'), findsNothing);
    expect(find.text('1000'), findsOneWidget);
    expect(find.text('تغییرات ذخیره نشده'), findsNothing);
    expect(h.http.sent('PUT /settings'), isEmpty);
    await h.dispose(tester);
  });

  testWidgets('load failure shows the Persian error and retries', (tester) async {
    int calls = 0;
    final EngineHarness h = EngineHarness(
      http: FakeEngineHttp({
        'GET /settings': (_) => ++calls == 1
            ? engineError(503, 'db_unavailable', 'پایگاه داده engine در دسترس نیست.')
            : jsonBody(settingsJson()),
      }),
    );
    await tester.pumpWidget(h.wrap(const SettingsScreen()));
    await h.start(tester);

    expect(find.text('خواندن تنظیمات ناموفق بود'), findsOneWidget);
    expect(find.textContaining('پایگاه داده engine در دسترس نیست.'), findsOneWidget);

    await tester.tap(find.text('تلاش دوباره'));
    await settle(tester);
    expect(find.text('1000'), findsOneWidget);
    await h.dispose(tester);
  });

  test('a login that is not masked is masked again before display', () {
    expect(Mt5ConnectionBlock.maskedLogin('****1234'), '****1234');
    expect(Mt5ConnectionBlock.maskedLogin('55501234'), '****1234');
    expect(Mt5ConnectionBlock.maskedLogin('12'), '****');
    expect(Mt5ConnectionBlock.maskedLogin(null), isNull);
  });
}
