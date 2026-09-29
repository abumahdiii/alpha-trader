import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/signal_models.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/engine_fakes.dart';
import '../helpers/signal_fakes.dart';

void main() {
  test('GET /signals sends status, symbol, limit, offset and parses the page', () async {
    final FakeEngineHttp http = FakeEngineHttp({
      'GET /signals': (_) => jsonBody(signalsPageJson([signalJson(id: 3)], total: 7, offset: 50)),
    });
    final EngineApi api = EngineApi(port: 8765, httpClientAdapter: http);
    final SignalsPage p =
        await api.listSignals(status: LiveSignalStatus.rejected, symbol: 'XAUUSD.x', limit: 25, offset: 50);
    expect(http.requests.single.uri.queryParameters,
        {'status': 'rejected', 'symbol': 'XAUUSD.x', 'limit': '25', 'offset': '50'});
    expect(p.signals.single.id, 3);
    expect(p.total, 7);

    await api.listSignals();
    expect(http.requests.last.uri.queryParameters, {'limit': '50', 'offset': '0'});
  });

  test('GET /signals 422 invalid_query -> validation with the Persian message', () async {
    final EngineApi api = EngineApi(
      port: 1,
      httpClientAdapter: FakeEngineHttp({
        'GET /signals': (_) => engineError(422, 'invalid_query', '«limit» باید بین ۱ و 500 باشد.'),
      }),
    );
    await expectLater(
      api.listSignals(limit: 999),
      throwsA(isA<EngineApiException>()
          .having((e) => e.isValidation, 'validation', isTrue)
          .having((e) => e.messageFa, 'messageFa', '«limit» باید بین ۱ و 500 باشد.')),
    );
  });

  test('GET /signals/{id}: item; 404 signal_not_found', () async {
    final EngineApi api = EngineApi(
      port: 1,
      httpClientAdapter: FakeEngineHttp({
        'GET /signals/4': (_) => jsonBody(signalJson(id: 4)),
        'GET /signals/5': (_) => engineError(404, 'signal_not_found', 'سیگنال پیدا نشد.'),
      }),
    );
    expect((await api.getSignal(4)).id, 4);
    await expectLater(
      api.getSignal(5),
      throwsA(isA<EngineApiException>()
          .having((e) => e.statusCode, 'status', 404)
          .having((e) => e.code, 'code', 'signal_not_found')
          .having((e) => e.messageFa, 'messageFa', 'سیگنال پیدا نشد.')),
    );
  });

  test('GET /signals/status and 503 db_unavailable', () async {
    int calls = 0;
    final EngineApi api = EngineApi(
      port: 1,
      httpClientAdapter: FakeEngineHttp({
        'GET /signals/status': (_) => ++calls == 1
            ? jsonBody(signalStatusJson(state: 'mt5_down'))
            : engineError(503, 'db_unavailable', 'پایگاه داده engine در دسترس نیست.'),
      }),
    );
    expect((await api.getSignalsStatus()).state, LiveSignalsState.mt5Down);
    await expectLater(
        api.getSignalsStatus(), throwsA(isA<EngineApiException>().having((e) => e.code, 'code', 'db_unavailable')));
  });

  test('GET /signals/settings and a partial POST (only the given keys)', () async {
    final FakeEngineHttp http = FakeEngineHttp({
      'GET /signals/settings': (_) => jsonBody({'enabled': false, 'live_strategy': 'stddev_channel', 'grace_s': 20}),
      'POST /signals/settings': (_) =>
          jsonBody({'enabled': true, 'live_strategy': 'stddev_channel', 'grace_s': 20, 'status': signalStatusJson()}),
    });
    final EngineApi api = EngineApi(port: 1, httpClientAdapter: http);
    expect(await api.getSignalSettings(),
        const LiveSignalSettings(enabled: false, liveStrategy: 'stddev_channel', graceS: 20));

    final LiveSignalSettingsResult r = await api.postSignalSettings(enabled: true);
    expect(FakeEngineHttp.bodyOf(http.sent('POST /signals/settings').single), {'enabled': true});
    expect(r.settings.enabled, isTrue);
    expect(r.status!.state, LiveSignalsState.running);

    await api.postSignalSettings(graceS: 45);
    expect(FakeEngineHttp.bodyOf(http.sent('POST /signals/settings').last), {'grace_s': 45});
    await api.postSignalSettings(graceS: 12.5, liveStrategy: 'stddev_channel');
    expect(FakeEngineHttp.bodyOf(http.sent('POST /signals/settings').last),
        {'live_strategy': 'stddev_channel', 'grace_s': 12.5});
  });

  test('POST /signals/settings 409 plugin_not_live / 404 strategy_not_found keep the engine message', () async {
    final EngineApi api = EngineApi(
      port: 1,
      httpClientAdapter: FakeEngineHttp({
        'POST /signals/settings': (r) => (FakeEngineHttp.bodyOf(r)! as Map)['live_strategy'] == 'ma_cross'
            ? engineError(409, 'plugin_not_live', 'سیستم‌های بارگذاری‌شده (پلاگین) فعلا فقط در چارت و بک‌تست قابل استفاده‌اند.')
            : engineError(404, 'strategy_not_found', 'سیستم «nope» پیدا نشد.'),
      }),
    );
    await expectLater(
      api.postSignalSettings(liveStrategy: 'ma_cross'),
      throwsA(isA<EngineApiException>()
          .having((e) => e.statusCode, 'status', 409)
          .having((e) => e.code, 'code', 'plugin_not_live')
          .having((e) => e.messageFa, 'messageFa', contains('پلاگین'))),
    );
    await expectLater(
      api.postSignalSettings(liveStrategy: 'nope'),
      throwsA(isA<EngineApiException>().having((e) => e.code, 'code', 'strategy_not_found')),
    );
    // Without message_fa, the 409 code still gets a Persian sentence.
    expect(EngineApi.errorFromResponse(409, '{"detail": {"code": "plugin_not_live"}}').messageFa, contains('پلاگین'));
  });

  test('the signals socket URI', () {
    expect(EngineApi(port: 8765).signalsSocketUri().toString(), 'ws://127.0.0.1:8765/ws/signals');
  });
}
