import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/screens/systems/strategy_editor.dart';
import 'package:alpha_trader/screens/systems/systems_screen.dart';
import 'package:alpha_trader/widgets/engine_gate.dart';

import '../helpers/engine_fakes.dart';

Finder _numberField(String label) =>
    find.ancestor(of: find.text(label), matching: find.byType(TextFormField));

Finder _button(String label) =>
    find.ancestor(of: find.text(label), matching: find.bySubtype<ButtonStyleButton>());

Future<EngineHarness> _pump(WidgetTester tester, FakeEngineHttp http) async {
  // Large enough that the whole generated form and its buttons are visible.
  tester.view.physicalSize = const Size(1800, 1600);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final EngineHarness h = EngineHarness(http: http);
  await tester.pumpWidget(h.wrap(const SystemsScreen()));
  await h.start(tester);
  return h;
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('engine not running: empty state, no request', (tester) async {
    final EngineHarness h = EngineHarness();
    await tester.pumpWidget(h.wrap(const SystemsScreen()));
    await settle(tester);

    expect(find.text(EngineUnavailableView.title), findsOneWidget);
    expect(h.http.requests, isEmpty);
    await h.dispose(tester);
  });

  testWidgets('form is generated from param_schema, with versions and ddof choices', (tester) async {
    final EngineHarness h = await _pump(
      tester,
      FakeEngineHttp({
        'GET /strategies': (_) => jsonBody([strategyJson(paramsVersion: 3)])
      }),
    );

    // Every schema entry has a field with the engine's Persian label, including
    // one this UI has never heard of.
    for (final Map<String, Object?> spec in sampleSchema()) {
      expect(find.text(spec['label_fa']! as String), findsOneWidget, reason: '${spec['name']}');
    }
    expect(_numberField('پنجره آزمایشی'), findsOneWidget);
    expect(find.byType(TextFormField), findsNWidgets(3)); // n, k, future_window
    expect(find.byType(Switch), findsOneWidget); // use_pin_bar
    expect(find.byType(SegmentedButton<int>), findsNWidgets(2)); // sigma_ddof, projection_mode

    expect(find.text('σ: ddof=0 (جمعیت)'), findsOneWidget);
    expect(find.text('σ: ddof=1 (نمونه)'), findsOneWidget);
    expect(find.text('bar_count (شمارش کندل)'), findsOneWidget);

    expect(find.text('نسخه کد: 1'), findsOneWidget);
    expect(find.text('نسخه پارامترها: 3'), findsOneWidget);
    expect(find.textContaining('(وقت محلی)'), findsOneWidget);
    expect(find.textContaining('بازه: 10 تا 500 · پیش‌فرض: 100 (فعلی)'), findsOneWidget);

    expect(tester.widget<ButtonStyleButton>(_button(StrategyEditor.saveLabel)).onPressed, isNull);
    await h.dispose(tester);
  });

  testWidgets('save sends the FULL parameter set, including the edited sigma_ddof', (tester) async {
    final Map<String, Object?> saved = {...defaultParams(), 'n': 120, 'sigma_ddof': 1};
    final EngineHarness h = await _pump(
      tester,
      FakeEngineHttp({
        'GET /strategies': (_) => jsonBody([strategyJson()]),
        'PUT /strategies/stddev_channel': (_) =>
            jsonBody(strategyJson(paramsVersion: 2, params: saved, createdNewVersion: true)),
      }),
    );

    await tester.tap(find.text('σ: ddof=1 (نمونه)'));
    await tester.pump();
    await tester.enterText(_numberField('طول کانال (تعداد کندل H4)'), '120');
    await tester.pump();
    expect(find.text('تغییرات ذخیره نشده'), findsOneWidget);

    await tester.tap(_button(StrategyEditor.saveLabel));
    await settle(tester);

    final Object? body = FakeEngineHttp.bodyOf(h.http.sent('PUT /strategies/stddev_channel').single);
    expect(body, {'params': saved});
    expect(find.text('نسخه پارامترها: 2'), findsOneWidget);
    expect(find.text('پارامترها ذخیره شدند'), findsOneWidget);
    expect(find.text('تغییرات ذخیره نشده'), findsNothing);
    // The list on the side follows the new version too.
    expect(find.text('پارامترها: نسخه 2'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('«بازگشت به پیش‌فرض» asks first, then sends {"params": {}}', (tester) async {
    final EngineHarness h = await _pump(
      tester,
      FakeEngineHttp({
        'GET /strategies': (_) => jsonBody([
              strategyJson(paramsVersion: 4, params: {...defaultParams(), 'k': 2.5}),
            ]),
        'PUT /strategies/stddev_channel': (_) =>
            jsonBody(strategyJson(paramsVersion: 5, createdNewVersion: true)),
      }),
    );
    Finder confirm() => find.descendant(
          of: find.byType(Dialog),
          matching: find.widgetWithText(FilledButton, StrategyEditor.resetLabel),
        );

    await tester.tap(_button(StrategyEditor.resetLabel));
    await tester.pumpAndSettle();
    expect(find.byType(Dialog), findsOneWidget);
    await tester.tap(find.text(StrategyEditor.cancelLabel));
    await tester.pumpAndSettle();
    expect(h.http.sent('PUT /strategies/stddev_channel'), isEmpty);

    await tester.tap(_button(StrategyEditor.resetLabel));
    await tester.pumpAndSettle();
    await tester.tap(confirm());
    await settle(tester);

    final Object? body = FakeEngineHttp.bodyOf(h.http.sent('PUT /strategies/stddev_channel').single);
    expect(body, {'params': <String, Object?>{}});
    expect(find.text('نسخه پارامترها: 5'), findsOneWidget);
    expect(find.text('پارامترها به پیش‌فرض برگشتند'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('422 invalid_params: per-param messages under the field, cross-field on top', (tester) async {
    const String kError = 'پارامتر «ضریب انحراف معیار (k)» (k) نباید بیشتر از 5 باشد (مقدار داده‌شده: 9).';
    const String crossField = 'حداقل یکی از الگوهای تایید (پین‌بار یا اینگالف) باید فعال باشد.';
    final EngineHarness h = await _pump(
      tester,
      FakeEngineHttp({
        'GET /strategies': (_) => jsonBody([strategyJson()]),
        'PUT /strategies/stddev_channel': (_) =>
            engineError(422, 'invalid_params', 'پارامترهای استراتژی نامعتبر هستند.', [kError, crossField]),
      }),
    );

    await tester.tap(find.byType(Switch));
    await tester.pump();
    await tester.tap(_button(StrategyEditor.saveLabel));
    await settle(tester);

    expect(find.text(kError), findsOneWidget);
    final Offset kField = tester.getCenter(_numberField('ضریب انحراف معیار (k)'));
    final Offset kMsg = tester.getCenter(find.text(kError));
    expect(kMsg.dy, greaterThan(kField.dy));
    expect((kMsg.dx - kField.dx).abs(), lessThan(250));

    expect(find.text(crossField), findsOneWidget);
    expect(find.text('پارامترهای استراتژی نامعتبر هستند.'), findsWidgets);
    expect(find.text('تغییرات ذخیره نشده'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('«بازگشت» discards the draft', (tester) async {
    final EngineHarness h = await _pump(
      tester,
      FakeEngineHttp({
        'GET /strategies': (_) => jsonBody([strategyJson()])
      }),
    );

    await tester.tap(find.text('calendar (زمان تقویمی)'));
    await tester.pump();
    expect(find.text('تغییرات ذخیره نشده'), findsOneWidget);

    await tester.tap(_button(StrategyEditor.discardLabel));
    await tester.pump();
    expect(find.text('تغییرات ذخیره نشده'), findsNothing);
    expect(h.http.sent('PUT /strategies/stddev_channel'), isEmpty);
    await h.dispose(tester);
  });

  testWidgets('stored params that no longer fit the schema are reported', (tester) async {
    final EngineHarness h = await _pump(
      tester,
      FakeEngineHttp({
        'GET /strategies': (_) => jsonBody([
              strategyJson(paramsErrorsFa: ['پارامتر ناشناخته: \'old\'.']),
            ]),
      }),
    );
    expect(find.text('پارامتر ناشناخته: \'old\'.'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('list failure shows the engine message and retries', (tester) async {
    int calls = 0;
    final EngineHarness h = await _pump(
      tester,
      FakeEngineHttp({
        'GET /strategies': (_) => ++calls == 1
            ? engineError(503, 'db_unavailable', 'پایگاه داده engine در دسترس نیست.')
            : jsonBody([strategyJson()]),
      }),
    );
    expect(find.text('خواندن سیستم‌ها ناموفق بود'), findsOneWidget);
    expect(find.textContaining('پایگاه داده engine در دسترس نیست.'), findsOneWidget);

    await tester.tap(find.text('تلاش دوباره'));
    await settle(tester);
    expect(find.text('نسخه پارامترها: 1'), findsOneWidget);
    await h.dispose(tester);
  });
}
