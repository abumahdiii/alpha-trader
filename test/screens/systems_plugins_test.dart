import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/core/date_format.dart';
import 'package:alpha_trader/screens/systems/plugin_dialogs.dart';
import 'package:alpha_trader/screens/systems/plugins_panel.dart';
import 'package:alpha_trader/screens/systems/strategy_editor.dart';
import 'package:alpha_trader/screens/systems/systems_screen.dart';
import 'package:alpha_trader/services/engine_api.dart';
import 'package:alpha_trader/services/plugin_files.dart';
import 'package:alpha_trader/widgets/strategy_selector.dart';

import '../helpers/engine_fakes.dart';
import '../helpers/plugin_fakes.dart';

Future<EngineHarness> _pump(WidgetTester tester, FakePluginEngine engine, FakePluginFiles files) async {
  tester.view.physicalSize = const Size(1800, 1600);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final EngineHarness h = EngineHarness(http: FakeEngineHttp(engine.routes()));
  await tester.pumpWidget(h.wrap(SystemsScreen(pluginFiles: files)));
  await h.start(tester);
  return h;
}

Finder _button(String label) => find.ancestor(of: find.text(label), matching: find.bySubtype<ButtonStyleButton>());

/// Text of the plugin version's status chip.
String? _status(WidgetTester tester) => tester
    .widget<Text>(find.descendant(
      of: find.byKey(const ValueKey<String>('plugin-status-$kPluginName@2')),
      matching: find.byType(Text),
    ))
    .data;

Future<void> _menu(WidgetTester tester, String action) async {
  await tester.tap(find.byKey(const ValueKey<String>('plugin-menu-$kPluginName@2')));
  await tester.pumpAndSettle();
  await tester.tap(find.text(action).last);
  await settle(tester);
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('safety note, empty plugin list, both actions enabled', (tester) async {
    final EngineHarness h = await _pump(tester, FakePluginEngine(), FakePluginFiles());

    expect(find.text(PluginActionsFa.safetyNote), findsOneWidget);
    expect(PluginActionsFa.safetyNote, contains('بدون دسترسی به متاتریدر، شبکه و فایل'));
    expect(find.textContaining('هنوز سیستمی بارگذاری نشده است'), findsOneWidget);
    expect(tester.widget<ButtonStyleButton>(_button(PluginActionsFa.upload)).onPressed, isNotNull);
    expect(tester.widget<ButtonStyleButton>(_button(PluginActionsFa.download)).onPressed, isNotNull);
    expect(h.http.sent('GET /plugins'), hasLength(1));
    await h.dispose(tester);
  });

  testWidgets('download template: engine content saved UTF-8 under the suggested name, «باز کردن پوشه» reveals it',
      (tester) async {
    final FakePluginFiles files = FakePluginFiles();
    final EngineHarness h = await _pump(tester, FakePluginEngine(), files);

    await tester.tap(_button(PluginActionsFa.download));
    await settle(tester);

    expect(h.http.sent('GET /plugins/template'), hasLength(1));
    expect(files.suggestedNames, [kTemplateFilename]);
    expect(files.written, {files.savePath!: kTemplateContent});
    expect(find.byKey(const ValueKey<String>('plugin-template-saved')), findsOneWidget);
    expect(find.textContaining(files.savePath!), findsOneWidget);

    await tester.pump(const Duration(milliseconds: 500)); // snackbar entrance animation
    await tester.tap(find.text(PluginActionsFa.openFolder));
    await settle(tester);
    expect(files.revealed, [files.savePath]);
    await h.dispose(tester);
  });

  testWidgets('download template cancelled: nothing written, no snackbar', (tester) async {
    final FakePluginFiles files = FakePluginFiles()..savePath = null;
    final EngineHarness h = await _pump(tester, FakePluginEngine(), files);

    await tester.tap(_button(PluginActionsFa.download));
    await settle(tester);
    expect(files.written, isEmpty);
    expect(find.byKey(const ValueKey<String>('plugin-template-saved')), findsNothing);
    await h.dispose(tester);
  });

  testWidgets('upload success: validating state with the long timeout, then the report, list + strategies refresh',
      (tester) async {
    final FakePluginEngine engine = FakePluginEngine();
    final Completer<void> release = Completer<void>();
    engine.onUpload = (RequestOptions r) async {
      await release.future;
      engine.plugins.insert(0, engine.uploaded);
      engine.strategies.add(pluginStrategyJson());
      return jsonBody(engine.uploaded, 201);
    };
    final FakePluginFiles files = FakePluginFiles();
    final EngineHarness h = await _pump(tester, engine, files);

    await tester.tap(_button(PluginActionsFa.upload));
    await settle(tester);

    // While the engine validates: the Persian progress state, buttons disabled.
    expect(find.textContaining(PluginActionsFa.validating), findsOneWidget);
    expect(find.textContaining('ma_cross.py'), findsWidgets);
    expect(tester.widget<ButtonStyleButton>(_button(PluginActionsFa.download)).onPressed, isNull);
    final RequestOptions sent = h.http.sent('POST /plugins').single;
    expect(FakeEngineHttp.bodyOf(sent), {'filename': 'ma_cross.py', 'source': 'class MaCross: ...\n'});
    expect(sent.receiveTimeout, EngineApi.pluginUploadTimeout);
    expect(EngineApi.pluginUploadTimeout, greaterThanOrEqualTo(const Duration(seconds: 90)));

    release.complete();
    await settle(tester, 10);

    // The report dialog: identity + static + dynamic checks, all engine values.
    expect(find.byKey(const ValueKey<String>('plugin-report')), findsOneWidget);
    expect(find.textContaining(PluginTextsFa.acceptedTitle), findsOneWidget);
    expect(find.text(kPluginSha), findsWidgets);
    expect(find.text('نحو پایتون (بدون خطای نگارشی)'), findsOneWidget);
    expect(find.byKey(const ValueKey<String>('plugin-static-strategy_class')), findsOneWidget);
    expect(find.text('3,000'), findsOneWidget); // bars
    expect(find.text('11'), findsOneWidget); // candidates
    expect(find.text('60 کندل'), findsOneWidget); // prefix checks
    expect(find.text('281 MiB'), findsOneWidget);
    expect(find.text('5.51 ثانیه از سقف 30 ثانیه'), findsOneWidget);
    expect(find.text('1.40 ثانیه'), findsOneWidget); // worker boot

    await tester.tap(find.byTooltip('بستن').first);
    await settle(tester);

    // The new version is listed (registered, active) and the strategy list got the plugin with its badge.
    expect(h.http.sent('GET /plugins').length, greaterThanOrEqualTo(2));
    expect(h.http.sent('GET /strategies').length, greaterThanOrEqualTo(2));
    expect(find.byKey(const ValueKey<String>('plugin-$kPluginName@2')), findsOneWidget);
    expect(find.byKey(const ValueKey<String>('plugin-registered-$kPluginName@2-true')), findsOneWidget);
    expect(_status(tester), 'فعال');
    expect(
      find.text('هش: 2d070ea08a45 — ${formatLocalDateTime(DateTime.utc(2026, 9, 28, 9, 15), seconds: false)} (وقت محلی)'),
      findsOneWidget,
    );
    expect(find.byTooltip('هش کامل فایل (sha256): $kPluginSha'), findsOneWidget);
    expect(find.byKey(const ValueKey<String>('strategy-tile-$kPluginName')), findsOneWidget);
    expect(
      find.descendant(
          of: find.byKey(const ValueKey<String>('strategy-tile-$kPluginName')), matching: find.byType(PluginBadge)),
      findsOneWidget,
    );
    await h.dispose(tester);
  });

  testWidgets('upload 422 invalid_plugin: every error with its line number, readable', (tester) async {
    final FakePluginEngine engine = FakePluginEngine()
      ..onUpload = (_) => engineError(422, 'invalid_plugin', 'فایل سیستم پذیرفته نشد؛ خطاها را برطرف کنید و دوباره بارگذاری کنید.', [
            'خط 3: import «os» مجاز نیست.',
            'خط 17: فراخوانی «open» مجاز نیست.',
            'کلاس Strategy پیدا نشد.',
          ]);
    final EngineHarness h = await _pump(tester, engine, FakePluginFiles());

    await tester.tap(_button(PluginActionsFa.upload));
    await settle(tester, 10);

    expect(find.byKey(const ValueKey<String>('plugin-upload-error')), findsOneWidget);
    expect(find.text(PluginTextsFa.rejectedTitle), findsOneWidget);
    expect(find.text('خطاها (3)'), findsOneWidget);
    expect(find.text('خط 3'), findsOneWidget);
    expect(find.text('خط 17'), findsOneWidget);
    expect(find.text('import «os» مجاز نیست.'), findsOneWidget);
    expect(find.text('فراخوانی «open» مجاز نیست.'), findsOneWidget);
    expect(find.text('فایل'), findsOneWidget); // the error without a line
    expect(find.byKey(const ValueKey<String>('plugin-bump-version')), findsNothing);
    expect(find.textContaining(PluginActionsFa.validating), findsNothing);
    await h.dispose(tester);
  });

  testWidgets('upload 409 version_conflict: «برای تغییر منطق، version را بالا ببرید»', (tester) async {
    final FakePluginEngine engine = FakePluginEngine(plugins: [pluginJson()])
      ..onUpload = (_) => engineError(409, 'version_conflict',
          'نسخه 2 سیستم «ma_cross» قبلا با محتوای دیگری بارگذاری شده است؛ برای هر تغییر، عدد version را در فایل بالا ببرید.');
    final EngineHarness h = await _pump(tester, engine, FakePluginFiles());

    await tester.tap(_button(PluginActionsFa.upload));
    await settle(tester, 10);

    expect(find.text(PluginTextsFa.conflictTitle), findsOneWidget);
    expect(find.textContaining('قبلا با محتوای دیگری بارگذاری شده است'), findsOneWidget);
    expect(find.byKey(const ValueKey<String>('plugin-bump-version')), findsOneWidget);
    expect(find.textContaining(PluginTextsFa.bumpVersionFa), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('a file refused before sending (not UTF-8) never reaches the engine', (tester) async {
    final FakePluginFiles files = FakePluginFiles()
      ..pickError = const PluginFileException('فایل با کدگذاری UTF-8 ذخیره نشده است.');
    final EngineHarness h = await _pump(tester, FakePluginEngine(), files);

    await tester.tap(_button(PluginActionsFa.upload));
    await settle(tester);
    expect(h.http.sent('POST /plugins'), isEmpty);
    expect(find.textContaining('UTF-8'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('disable -> enable -> archive (confirmed), each refreshing the list', (tester) async {
    final FakePluginEngine engine =
        FakePluginEngine(plugins: [pluginJson()], strategies: [strategyJson(), pluginStrategyJson()]);
    final EngineHarness h = await _pump(tester, engine, FakePluginFiles());
    expect(_status(tester), 'فعال');

    await _menu(tester, PluginActionsFa.disable);
    expect(h.http.sent('POST /plugins/$kPluginName/2/disable'), hasLength(1));
    expect(_status(tester), 'غیرفعال');
    expect(find.byKey(const ValueKey<String>('plugin-registered-$kPluginName@2-false')), findsOneWidget);
    expect(find.byKey(const ValueKey<String>('strategy-tile-$kPluginName')), findsNothing,
        reason: 'a disabled plugin is no longer a registered strategy');

    await _menu(tester, PluginActionsFa.enable);
    expect(h.http.sent('POST /plugins/$kPluginName/2/enable'), hasLength(1));
    expect(_status(tester), 'فعال');
    expect(find.byKey(const ValueKey<String>('strategy-tile-$kPluginName')), findsOneWidget);

    await _menu(tester, PluginActionsFa.archive);
    expect(find.byKey(const ValueKey<String>('plugin-archive-confirm')), findsOneWidget);
    expect(h.http.sent('DELETE /plugins/$kPluginName/2'), isEmpty, reason: 'nothing before the confirmation');
    await tester.tap(find.byKey(const ValueKey<String>('plugin-archive-ok')));
    await settle(tester);
    expect(h.http.sent('DELETE /plugins/$kPluginName/2'), hasLength(1));
    expect(_status(tester), 'بایگانی');
    await h.dispose(tester);
  });

  testWidgets('archive cancelled sends nothing; 409 plugin_in_use shows the engine message', (tester) async {
    const String inUseFa =
        'یک بک‌تست در صف یا در حال اجرا از نسخه 2 سیستم «ma_cross» استفاده می‌کند؛ بعد از پایان آن دوباره تلاش کنید.';
    final FakePluginEngine engine = FakePluginEngine(plugins: [pluginJson()])
      ..onArchive = (_) => engineError(409, 'plugin_in_use', inUseFa);
    final EngineHarness h = await _pump(tester, engine, FakePluginFiles());

    await _menu(tester, PluginActionsFa.archive);
    await tester.tap(find.text(PluginActionsFa.cancel));
    await settle(tester);
    expect(h.http.sent('DELETE /plugins/$kPluginName/2'), isEmpty);

    await _menu(tester, PluginActionsFa.archive);
    await tester.tap(find.byKey(const ValueKey<String>('plugin-archive-ok')));
    await settle(tester);
    expect(h.http.sent('DELETE /plugins/$kPluginName/2'), hasLength(1));
    expect(find.text('بایگانی انجام نشد'), findsOneWidget);
    expect(find.textContaining(inUseFa), findsOneWidget);
    expect(_status(tester), 'فعال', reason: 'still active');
    await h.dispose(tester);
  });

  testWidgets('«گزارش اعتبارسنجی» from the list shows the stored report', (tester) async {
    final EngineHarness h = await _pump(tester, FakePluginEngine(plugins: [pluginJson()]), FakePluginFiles());
    await _menu(tester, PluginActionsFa.report);
    expect(find.byKey(const ValueKey<String>('plugin-report')), findsOneWidget);
    expect(find.textContaining(PluginTextsFa.reportTitle), findsWidgets);
    expect(find.text('ma_cross.py'), findsWidgets);
    await h.dispose(tester);
  });

  testWidgets('param form of a plugin strategy is built from its schema and saved via PUT /strategies/{name}',
      (tester) async {
    final FakePluginEngine engine =
        FakePluginEngine(plugins: [pluginJson()], strategies: [strategyJson(), pluginStrategyJson()]);
    final Map<String, RouteHandler> routes = engine.routes()
      ..['PUT /strategies/$kPluginName'] = (RequestOptions r) {
        final Map<String, Object?> params = (FakeEngineHttp.bodyOf(r)! as Map)['params'] as Map<String, Object?>;
        return jsonBody(pluginStrategyJson(paramsVersion: 2, params: params, createdNewVersion: true));
      };
    tester.view.physicalSize = const Size(1800, 1600);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final EngineHarness h = EngineHarness(http: FakeEngineHttp(routes));
    await tester.pumpWidget(h.wrap(SystemsScreen(pluginFiles: FakePluginFiles())));
    await h.start(tester);

    await tester.tap(find.byKey(const ValueKey<String>('strategy-tile-$kPluginName')));
    await settle(tester);
    expect(find.byKey(const ValueKey<String>('editor-plugin-chip')), findsOneWidget);
    expect(find.text('پلاگین — فایل 2d070ea08a45'), findsOneWidget);
    expect(find.text('میانگین سریع'), findsOneWidget);
    expect(find.text('میانگین کند'), findsOneWidget);

    final Finder fast = find.ancestor(of: find.text('میانگین سریع'), matching: find.byType(TextFormField));
    await tester.enterText(fast, '12');
    await settle(tester);
    await tester.tap(_button(StrategyEditor.saveLabel));
    await settle(tester);

    expect(FakeEngineHttp.bodyOf(h.http.sent('PUT /strategies/$kPluginName').single), {
      'params': {'fast': 12, 'slow': 30},
    });
    expect(find.text('نسخه پارامترها: 2'), findsOneWidget);
    await h.dispose(tester);
  });
}
