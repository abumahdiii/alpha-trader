// Strategy selection on the backtest page: the selector's entries, the
// `strategy` in the limits query and the POST body, the chart prefill's
// strategy, and the provenance (source badge, file hash) in the result header.

import 'dart:math' as math;

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/providers/shell_navigation.dart';
import 'package:alpha_trader/screens/backtest/backtest_controller.dart';
import 'package:alpha_trader/screens/backtest/backtest_screen.dart';
import 'package:alpha_trader/services/engine_api.dart';
import 'package:alpha_trader/widgets/strategy_selector.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';
import '../helpers/plugin_fakes.dart';

Map<String, RouteHandler> _extra() => {
      ...FakePluginEngine(plugins: [pluginJson()], strategies: [strategyJson(), pluginStrategyJson()]).routes(),
      'GET /backtests/limits': (RequestOptions r) => jsonBody(limitsJson(symbol: r.uri.queryParameters['symbol']!)),
    };

Future<void> _until(bool Function() condition) async {
  final Stopwatch w = Stopwatch()..start();
  while (!condition()) {
    if (w.elapsed > const Duration(seconds: 2)) fail('timed out');
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}

void main() {
  group('controller', () {
    late FakeBacktestEngine engine;
    late FakeEngineHttp http;
    late BacktestController c;

    setUp(() {
      engine = FakeBacktestEngine();
      http = engine.http(extra: _extra());
      c = BacktestController(
        api: EngineApi(port: 8765, httpClientAdapter: http),
        watcherFactory: FakeSocketConnector().watcher,
        random: math.Random(1),
      );
    });

    tearDown(() => c.dispose());

    test('strategies: builtin first, plugin badged; default stddev_channel', () async {
      await c.init();
      expect(c.strategies.map((o) => '${o.name}:${o.isPlugin}'), ['stddev_channel:false', '$kPluginName:true']);
      expect(c.strategy, 'stddev_channel');
      await _until(() => c.limits != null);
      expect(http.sent('GET /backtests/limits').last.uri.queryParameters['strategy'], 'stddev_channel');
    });

    test('picking a plugin reloads the limits for it and the POST carries strategy + strategy_version', () async {
      await c.init();
      c.setStrategy(kPluginName);
      expect(c.limits, isNull, reason: 'the old strategy limits no longer apply');
      await _until(() => c.limits != null);
      expect(
          http.sent('GET /backtests/limits').last.uri.queryParameters, {'symbol': 'XAUUSD.x', 'strategy': kPluginName});

      c.setFromDay(DateTime(2024, 8, 1));
      c.setToDay(DateTime(2024, 9, 30));
      expect(await c.submit(), isTrue);
      final Map<String, Object?> body =
          FakeEngineHttp.bodyOf(http.sent('POST /backtests').single)! as Map<String, Object?>;
      expect(body, containsPair('strategy', kPluginName));
      expect(body, containsPair('strategy_version', 2));
    });

    test('a chart prefill carries its strategy (limits + body)', () async {
      await c.init();
      await c.applyPrefill(BacktestPrefill(
        symbol: 'XAUUSD.x',
        from: DateTime.utc(2024, 8, 1, 9),
        to: DateTime.utc(2024, 9, 1, 21),
        strategy: kPluginName,
        autoRun: true,
      ));
      expect(c.strategy, kPluginName);
      final Map<String, Object?> body =
          FakeEngineHttp.bodyOf(http.sent('POST /backtests').single)! as Map<String, Object?>;
      expect(body['strategy'], kPluginName);
      await _until(
          () => http.sent('GET /backtests/limits').any((r) => r.uri.queryParameters['strategy'] == kPluginName));
    });

    test('ShellNavigation.openBacktest keeps the strategy in the pending prefill', () {
      final ShellNavigation nav = ShellNavigation();
      addTearDown(nav.dispose);
      nav.openBacktest(symbol: 'BRNUSD.x', strategy: kPluginName, autoRun: true);
      expect(nav.page, ShellPage.backtest);
      expect(nav.takeBacktestPrefill()!.strategy, kPluginName);
    });
  });

  group('page', () {
    testWidgets('form selector lists the plugin with its badge; result header shows source + file hash',
        (tester) async {
      SharedPreferences.setMockInitialValues({});
      tester.view.physicalSize = const Size(2200, 1100);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final FakeBacktestEngine engine = FakeBacktestEngine();
      final Map<String, Object?> summary = {
        ...runSummaryJson(id: 9),
        'strategy': kPluginName,
        'strategy_version': 2,
        'strategy_source': 'plugin',
        'strategy_sha256': kPluginSha,
      };
      engine.runs.add(summary);
      engine.details[9] = {...manualDetailJson(id: 9), ...summary};
      final EngineHarness h = EngineHarness(http: engine.http(extra: _extra()));
      await tester.pumpWidget(h.wrap(BacktestScreen(watcherFactory: FakeSocketConnector().watcher)));
      await h.start(tester);
      await settle(tester, 20);

      expect(find.byKey(const ValueKey<String>('bt-strategy')), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey<String>('bt-strategy')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey<String>('strategy-option-$kPluginName')), findsWidgets);
      expect(find.byType(PluginBadge), findsWidgets);
      await tester.tap(find.text(kPluginTitleFa).last);
      await settle(tester, 10);
      expect(find.textContaining('این سیستم از فایل بارگذاری‌شده است'), findsOneWidget);

      await tester.tap(find.byKey(const ValueKey<String>('bt-side-runs')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      await tester.tap(find.byKey(const ValueKey<String>('bt-run-9')));
      await settle(tester, 20);

      expect(find.byKey(const ValueKey<String>('bt-header-plugin')), findsOneWidget);
      expect(find.textContaining('استراتژی: $kPluginName v2 — فایل 2d070ea08a45'), findsOneWidget);
      expect(
        find.byWidgetPredicate(
            (Widget w) => w is Tooltip && (w.message ?? '').contains('هش کامل فایل سیستم (sha256): $kPluginSha')),
        findsOneWidget,
      );
      expect(
        find.byWidgetPredicate((Widget w) => w is Tooltip && (w.message ?? '').contains('منبع کد: پلاگین')),
        findsOneWidget,
      );
      await h.dispose(tester);
    });
  });
}
