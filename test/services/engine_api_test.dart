import 'dart:async';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/account_settings.dart';
import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/models/market_data.dart';
import 'package:alpha_trader/models/strategy.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/engine_fakes.dart';

EngineApi _api(FakeEngineHttp http, {Duration timeout = EngineApi.defaultCallTimeout}) =>
    EngineApi(port: 8765, defaultTimeout: timeout, httpClientAdapter: http);

Future<EngineApiException> _failure(Future<Object?> call) async {
  try {
    await call;
  } on EngineApiException catch (e) {
    return e;
  }
  fail('expected EngineApiException');
}

void main() {
  group('success parsing (shapes of the engine pydantic models)', () {
    test('GET /settings -> AccountSettings', () async {
      final http = FakeEngineHttp({'GET /settings': (_) => jsonBody(settingsJson(riskPct: 0.5))});
      final AccountSettings s = await _api(http).getSettings();

      expect(http.requests.single.uri.toString(), 'http://127.0.0.1:8765/settings');
      expect(s, const AccountSettings(balance: 1000, riskPct: 0.5, leverage: 100, rr: 2));
    });

    test('PUT /settings sends only the given changes as JSON', () async {
      final http = FakeEngineHttp({'PUT /settings': (_) => jsonBody(settingsJson(leverage: 200))});
      const AccountSettings base = AccountSettings(balance: 1000, riskPct: 1, leverage: 100, rr: 2);
      final AccountSettings s = await _api(http).putSettings(base.copyWith(leverage: 200).changesFrom(base));

      expect(FakeEngineHttp.bodyOf(http.requests.single), {'leverage': 200});
      expect(http.requests.single.contentType, contains('application/json'));
      expect(s.leverage, 200);
    });

    test('GET /strategies -> StrategyInfo with a typed param schema', () async {
      final http = FakeEngineHttp({
        'GET /strategies': (_) => jsonBody([strategyJson(paramsVersion: 3)]),
      });
      final List<StrategyInfo> list = await _api(http).listStrategies();

      final StrategyInfo s = list.single;
      expect(s.name, 'stddev_channel');
      expect(s.titleFa, 'کانال انحراف معیار');
      expect(s.version, 1);
      expect(s.paramsVersion, 3);
      expect(s.paramsSavedUtc, DateTime.utc(2026, 9, 27, 8, 30));
      expect(s.paramsSavedUtc!.isUtc, isTrue);
      expect(s.params['sigma_ddof'], 0);
      expect(s.paramSchema.map((p) => p.kind), [
        ParamKind.integer,
        ParamKind.float,
        ParamKind.choice,
        ParamKind.boolean,
        ParamKind.choice,
        ParamKind.integer,
      ]);
      final ParamSpec n = s.paramSchema.first;
      expect((n.min, n.max, n.step, n.defaultValue), (10.0, 500.0, 1.0, 100));
      expect(s.paramSchema[2].choices, [0, 1]);
      expect(s.paramSchema[4].choices, ['bar_count', 'calendar']);
    });

    test('GET /strategies/{name} and PUT /strategies/{name} with {"params": ...}', () async {
      final http = FakeEngineHttp({
        'GET /strategies/stddev_channel': (_) => jsonBody(strategyJson()),
        'PUT /strategies/stddev_channel': (_) =>
            jsonBody(strategyJson(paramsVersion: 2, createdNewVersion: true)),
      });
      final EngineApi api = _api(http);
      expect((await api.getStrategy('stddev_channel')).paramsVersion, 1);

      final StrategyUpdateResult r = await api.putStrategyParams('stddev_channel', {'n': 120});
      expect(FakeEngineHttp.bodyOf(http.requests.last), {
        'params': {'n': 120},
      });
      expect(r.createdNewVersion, isTrue);
      expect(r.strategy.paramsVersion, 2);
    });

    test('GET /symbols and GET /rates/meta send query parameters and parse', () async {
      final http = FakeEngineHttp({
        'GET /symbols': (_) => jsonBody({
              'mt5_state': 'connected',
              'symbols': [
                {
                  'symbol': 'XAUUSD.x',
                  'source': 'cache',
                  'spec': {
                    'name': 'XAUUSD.x',
                    'digits': 2,
                    'point': 0.01,
                    'trade_contract_size': 100.0,
                    'trade_tick_value': 1.0,
                    'trade_tick_size': 0.01,
                    'volume_min': 0.01,
                    'volume_step': 0.01,
                    'volume_max': 100.0,
                    'currency_profit': 'USD',
                    'currency_base': 'XAU',
                    'description': 'Gold',
                  },
                  'fetched_at_utc': '2026-09-27T06:00:00Z',
                  'error': null,
                },
                {'symbol': 'BRNUSD.x', 'source': 'none', 'spec': null, 'fetched_at_utc': null, 'error': 'x'},
              ],
            }),
        'GET /rates/meta': (_) => jsonBody({
              'symbol': 'XAUUSD.x',
              'timeframe': 'H1',
              'cached': true,
              'rows': 29000,
              'first_bar_utc': '2021-09-27T00:00:00Z',
              'last_bar_utc': '2026-09-26T20:00:00Z',
              'first_available_utc': '2021-09-27T00:00:00Z',
              'requested_start_utc': '2021-09-27T00:00:00Z',
              'history_short': false,
              'offset_model': 'us_dst(2)',
              'source': 'mt5',
              'fetched_at_utc': '2026-09-27T06:00:00Z',
            }),
      });
      final EngineApi api = _api(http);

      final SymbolsResponse symbols = await api.getSymbols();
      expect(symbols.mt5State, Mt5State.connected);
      expect(symbols.symbols.first.spec!.digits, 2);
      expect(symbols.symbols.first.source, SymbolSource.cache);
      expect(symbols.symbols.last.spec, isNull);

      final RatesMeta meta = await api.getRatesMeta(symbol: 'XAUUSD.x', timeframe: 'H1');
      expect(http.requests.last.uri.queryParameters, {'symbol': 'XAUUSD.x', 'timeframe': 'H1'});
      expect(meta.rows, 29000);
      expect(meta.lastBarUtc, DateTime.utc(2026, 9, 26, 20));
      expect(meta.offsetModel, 'us_dst(2)');
    });
  });

  group('errors carry the engine\'s Persian messages', () {
    test('422 detail.errors_fa -> validation with message_fa + errors_fa', () async {
      final http = FakeEngineHttp({
        'PUT /settings': (_) => engineError(422, 'invalid_settings', 'تنظیمات حساب نامعتبر است.', [
              '«اهرم» نباید بیشتر از 1000 باشد.',
              "فیلد ناشناخته: 'foo'.",
            ]),
      });
      final EngineApiException e = await _failure(_api(http).putSettings({'leverage': 5000}));

      expect(e.kind, EngineApiErrorKind.validation);
      expect(e.isValidation, isTrue);
      expect(e.statusCode, 422);
      expect(e.code, 'invalid_settings');
      expect(e.messageFa, 'تنظیمات حساب نامعتبر است.');
      expect(e.errorsFa, hasLength(2));
      expect(e.userMessage, contains('«اهرم» نباید بیشتر از 1000 باشد.'));

      final split = e.splitErrors({
        'leverage': ['«اهرم»'],
        'rr': ['«نسبت ریسک به ریوارد (R:R)»'],
      });
      expect(split.byField, {
        'leverage': ['«اهرم» نباید بیشتر از 1000 باشد.'],
      });
      expect(split.general, ["فیلد ناشناخته: 'foo'."]);
    });

    test('404 strategy_not_found keeps the engine message', () async {
      final http = FakeEngineHttp({
        'GET /strategies/nope': (_) => engineError(404, 'strategy_not_found', 'استراتژی «nope» پیدا نشد.'),
      });
      final EngineApiException e = await _failure(_api(http).getStrategy('nope'));
      expect(e.kind, EngineApiErrorKind.badStatus);
      expect((e.statusCode, e.code, e.messageFa), (404, 'strategy_not_found', 'استراتژی «nope» پیدا نشد.'));
    });

    test('plain-string detail (rates/symbols) -> generic Persian message + detail', () async {
      final http = FakeEngineHttp({
        'GET /rates/meta': (_) => jsonBody({'detail': "invalid timeframe 'M5'"}, 422),
      });
      final EngineApiException e =
          await _failure(_api(http).getRatesMeta(symbol: 'XAUUSD.x', timeframe: 'M5'));
      expect(e.kind, EngineApiErrorKind.validation);
      expect(e.messageFa, 'موتور ورودی را نپذیرفت.');
      expect(e.detail, "invalid timeframe 'M5'");
    });

    test('FastAPI request-validation list detail is summarised', () async {
      final http = FakeEngineHttp({
        'GET /rates/meta': (_) => jsonBody({
              'detail': [
                {
                  'loc': ['query', 'symbol'],
                  'msg': 'Field required',
                  'type': 'missing',
                },
              ],
            }, 422),
      });
      final EngineApiException e = await _failure(_api(http).getRatesMeta(symbol: '', timeframe: 'H1'));
      expect(e.detail, 'query.symbol: Field required');
    });

    test('503 db_unavailable and a non-JSON 500', () async {
      final http = FakeEngineHttp({
        'GET /settings': (_) => engineError(503, 'db_unavailable', 'پایگاه داده engine در دسترس نیست.'),
        'GET /strategies': (_) => ResponseBody.fromString('Internal Server Error', 500),
      });
      final EngineApi api = _api(http);
      final EngineApiException db = await _failure(api.getSettings());
      expect((db.statusCode, db.messageFa), (503, 'پایگاه داده engine در دسترس نیست.'));

      final EngineApiException boom = await _failure(api.listStrategies());
      expect(boom.kind, EngineApiErrorKind.badStatus);
      expect(boom.messageFa, 'موتور با خطا پاسخ داد (HTTP 500).');
      expect(boom.detail, 'Internal Server Error');
    });

    test('2xx body that breaks the contract -> malformedBody', () async {
      final http = FakeEngineHttp({
        'GET /settings': (_) => jsonBody({'balance': 1000.0, 'risk_pct': 1.0, 'leverage': 'x', 'rr': 2.0}),
        'GET /strategies': (_) => jsonBody({'not': 'a list'}),
      });
      final EngineApi api = _api(http);
      expect((await _failure(api.getSettings())).kind, EngineApiErrorKind.malformedBody);
      final EngineApiException e = await _failure(api.listStrategies());
      expect(e.kind, EngineApiErrorKind.malformedBody);
      expect(e.messageFa, 'پاسخ موتور با این نسخه برنامه سازگار نیست.');
    });

    test('no answer within the per-call budget -> timeout', () async {
      final Completer<ResponseBody> never = Completer<ResponseBody>();
      final http = FakeEngineHttp({'GET /settings': (_) => never.future});
      final EngineApiException e = await _failure(
        _api(http).getSettings(timeout: const Duration(milliseconds: 50)),
      );
      expect(e.kind, EngineApiErrorKind.timeout);
      expect(e.messageFa, contains('پاسخ نداد'));
    });

    test('refused connection -> connection', () async {
      final http = FakeEngineHttp({
        'GET /settings': (_) => throw const SocketException('Connection refused'),
        'GET /strategies': (o) => throw DioException.connectionError(requestOptions: o, reason: 'refused'),
      });
      final EngineApi api = _api(http);
      final EngineApiException a = await _failure(api.getSettings());
      expect(a.kind, EngineApiErrorKind.connection);
      expect(a.messageFa, 'اتصال به موتور برقرار نشد.');
      expect((await _failure(api.listStrategies())).kind, EngineApiErrorKind.connection);
    });
  });
}
