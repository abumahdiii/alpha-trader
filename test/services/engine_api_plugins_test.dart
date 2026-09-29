import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/strategy.dart';
import 'package:alpha_trader/models/strategy_plugin.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';
import '../helpers/plugin_fakes.dart';

void main() {
  late FakeEngineHttp http;
  late EngineApi api;

  EngineApi make(Map<String, RouteHandler> routes) {
    http = FakeEngineHttp(routes);
    return api = EngineApi(port: 8765, httpClientAdapter: http);
  }

  tearDown(() => api.close());

  test('upload: JSON {filename, source}, 201 = created, 200 = same file again, long budget', () async {
    int status = 201;
    make({'POST /plugins': (_) => jsonBody(pluginJson(), status)});
    final PluginUploadResult a = await api.uploadPlugin(filename: 'ma_cross.py', source: 'x = 1\n');
    expect(a.created, isTrue);
    expect(a.plugin.name, kPluginName);
    status = 200;
    final PluginUploadResult b = await api.uploadPlugin(filename: 'ma_cross.py', source: 'x = 1\n');
    expect(b.created, isFalse);
    final RequestOptions r = http.sent('POST /plugins').first;
    expect(FakeEngineHttp.bodyOf(r), {'filename': 'ma_cross.py', 'source': 'x = 1\n'});
    expect(r.receiveTimeout, const Duration(seconds: 120));
    expect(r.sendTimeout, const Duration(seconds: 120));
  });

  test('upload errors: 422 invalid_plugin keeps errors_fa (line numbers), 409 version_conflict', () async {
    make({
      'POST /plugins': (_) =>
          engineError(422, 'invalid_plugin', 'فایل سیستم پذیرفته نشد.', ['خط 3: import «os» مجاز نیست.']),
    });
    await expectLater(
      api.uploadPlugin(filename: 'a.py', source: 'import os'),
      throwsA(isA<EngineApiException>()
          .having((e) => e.isValidation, 'validation', isTrue)
          .having((e) => e.code, 'code', 'invalid_plugin')
          .having((e) => e.errorsFa, 'errorsFa', ['خط 3: import «os» مجاز نیست.'])),
    );
    make({
      'POST /plugins': (_) => jsonBody({
            'detail': {'code': 'version_conflict', 'errors_fa': []}
          }, 409)
    });
    await expectLater(
      api.uploadPlugin(filename: 'a.py', source: 'x'),
      throwsA(isA<EngineApiException>()
          .having((e) => e.code, 'code', 'version_conflict')
          .having((e) => e.messageFa, 'fallback message', contains('version'))),
    );
  });

  test('template, list and the version actions hit the documented routes', () async {
    make({
      'GET /plugins/template': (_) => jsonBody({'filename': kTemplateFilename, 'content': kTemplateContent}),
      'GET /plugins': (_) => jsonBody([pluginJson(), pluginJson(version: 1, status: 'archived', registered: false)]),
      'POST /plugins/$kPluginName/2/disable': (_) => jsonBody(pluginJson(status: 'disabled', registered: false)),
      'POST /plugins/$kPluginName/2/enable': (_) => jsonBody(pluginJson()),
      'DELETE /plugins/$kPluginName/2': (_) =>
          engineError(409, 'plugin_in_use', 'یک بک‌تست از این نسخه استفاده می‌کند.'),
    });
    expect((await api.getPluginTemplate()).filename, kTemplateFilename);
    expect((await api.listPlugins()).map((p) => p.status), [PluginStatus.active, PluginStatus.archived]);
    expect((await api.disablePlugin(kPluginName, 2)).status, PluginStatus.disabled);
    expect((await api.enablePlugin(kPluginName, 2)).registered, isTrue);
    await expectLater(
      api.archivePlugin(kPluginName, 2),
      throwsA(isA<EngineApiException>().having((e) => e.code, 'code', 'plugin_in_use')),
    );
  });

  test('strategy options: plugins badged; /plugins failing only drops the badges', () async {
    make({
      'GET /strategies': (_) => jsonBody([strategyJson(), pluginStrategyJson()]),
      'GET /plugins': (_) => jsonBody([pluginJson()]),
    });
    final List<StrategyOption> options = await api.listStrategyOptions();
    expect(options.map((o) => '${o.name}:${o.source.code}'), ['stddev_channel:builtin', '$kPluginName:plugin']);

    make({
      'GET /strategies': (_) => jsonBody([strategyJson(), pluginStrategyJson()])
    }); // /plugins -> 404
    final List<StrategyOption> plain = await api.listStrategyOptions();
    expect(plain.map((o) => o.name), ['stddev_channel', kPluginName]);
    expect(plain.any((o) => o.isPlugin), isFalse);
  });

  test('strategy query on /chart/setups, /chart/channel and /backtests/limits (omitted when null)', () async {
    make({
      'GET /backtests/limits': (_) => jsonBody(limitsJson()),
      'GET /chart/channel': (_) => engineError(409, 'channel_not_available', 'این سیستم کانال ندارد.'),
    });
    await api.getBacktestLimits('XAUUSD.x', strategy: kPluginName);
    await api.getBacktestLimits('XAUUSD.x');
    expect(http.sent('GET /backtests/limits').map((r) => r.uri.queryParameters['strategy']), [kPluginName, null]);

    await expectLater(
      api.getChartChannel(symbol: 'XAUUSD.x', timeframe: 'H1', strategy: kPluginName),
      throwsA(isA<EngineApiException>().having((e) => e.code, 'code', 'channel_not_available')),
    );
    expect(http.sent('GET /chart/channel').single.uri.queryParameters['strategy'], kPluginName);

    await expectLater(api.getChartSetups(symbol: 'XAUUSD.x', strategy: kPluginName), throwsA(anything));
    expect(http.sent('GET /chart/setups').single.uri.queryParameters, {'symbol': 'XAUUSD.x', 'strategy': kPluginName});
  });
}
