import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/backtest_models.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';

EngineApi _api(FakeEngineHttp http) => EngineApi(port: 8765, httpClientAdapter: http);

Future<EngineApiException> _failure(Future<Object?> call) async {
  try {
    await call;
  } on EngineApiException catch (e) {
    return e;
  }
  fail('expected EngineApiException');
}

void main() {
  test('POST /backtests sends the request body and parses the 202', () async {
    final http = FakeEngineHttp({
      'POST /backtests': (_) => jsonBody({
            'id': 12,
            'status': 'queued',
            'provisional': true,
            'labels_fa': ['موقت تا تایید چک داده', 'بدون سوآپ'],
          }, 202),
    });
    final BacktestSubmitResult r = await _api(http).submitBacktest(
      const BacktestRequest.random(symbol: 'XAUUSD.x', windowsCount: 5, windowMonths: 2, seed: 7),
    );
    expect(r.id, 12);
    expect(r.status, BacktestStatus.queued);
    expect(r.labelsFa, hasLength(2));
    expect(FakeEngineHttp.bodyOf(http.requests.single), {
      'symbol': 'XAUUSD.x',
      'mode': 'random',
      'windows_count': 5,
      'window_months': 2,
      'seed': 7,
    });
  });

  test('a period 422 keeps the engine code and Persian message', () async {
    final http = FakeEngineHttp({
      'POST /backtests': (_) => engineError(422, 'window_too_early', 'ابتدای بازه زودتر از آماده شدن کانال است.'),
    });
    final EngineApiException e = await _failure(_api(http).submitBacktest(BacktestRequest.manual(
      symbol: 'XAUUSD.x',
      from: DateTime.utc(2020),
      to: DateTime.utc(2021),
    )));
    expect(e.isValidation, isTrue);
    expect(e.code, 'window_too_early');
    expect(e.messageFa, contains('کانال'));
  });

  test('field validation 422 carries errors_fa', () async {
    final http = FakeEngineHttp({
      'POST /backtests': (_) => engineError(
            422,
            'invalid_request',
            'درخواست بک‌تست نامعتبر است.',
            ['«تعداد پنجره‌ها» نباید بیشتر از 500 باشد.'],
          ),
    });
    final EngineApiException e = await _failure(_api(http).submitBacktest(
      const BacktestRequest.random(symbol: 'XAUUSD.x', windowsCount: 900, windowMonths: 3),
    ));
    expect(e.errorsFa.single, contains('«تعداد پنجره‌ها»'));
  });

  test('GET /backtests?limit, detail, trades, equity, skipped', () async {
    final http = FakeEngineHttp({
      'GET /backtests': (_) => jsonBody({
            'count': 2,
            'runs': [runSummaryJson(id: 8, mode: 'random'), runSummaryJson(id: 7)],
          }),
      'GET /backtests/7': (_) => jsonBody(manualDetailJson(id: 7)),
      'GET /backtests/7/trades': (_) => jsonBody(tradesPageJson(7, [tradeJson(0)])),
      'GET /backtests/7/equity': (_) => jsonBody(equityJson(7)),
      'GET /backtests/7/skipped': (_) => jsonBody(skippedJson(7)),
    });
    final EngineApi api = _api(http);

    final BacktestRunList list = await api.listBacktests(limit: 100);
    expect(list.runs.map((r) => r.id), [8, 7]);
    expect(http.requests.last.uri.queryParameters, {'limit': '100'});

    expect((await api.getBacktest(7)).metrics!.tradeCount, 3);

    final BacktestTradesPage t = await api.getBacktestTrades(7);
    expect(t.trades, hasLength(1));
    expect(http.requests.last.uri.queryParameters, {'offset': '0', 'limit': '5000'});

    await api.getBacktestTrades(7, window: 2, limit: 10);
    expect(http.requests.last.uri.queryParameters, {'window': '2', 'offset': '0', 'limit': '10'});

    expect((await api.getBacktestEquity(7)).points, hasLength(4));
    expect((await api.getBacktestSkipped(7)).skipped, hasLength(1));
  });

  test('cancel and delete; 409 run_active in Persian', () async {
    final http = FakeEngineHttp({
      'POST /backtests/5/cancel': (_) => jsonBody({'id': 5, 'status': 'running', 'cancel_requested': true}),
      'DELETE /backtests/5': (_) =>
          engineError(409, 'run_active', 'اجرای در صف یا در حال اجرا حذف نمی‌شود؛ ابتدا آن را لغو کنید.'),
      'DELETE /backtests/6': (_) => jsonBody({'id': 6, 'deleted': true}),
    });
    final EngineApi api = _api(http);
    final BacktestCancelResult c = await api.cancelBacktest(5);
    expect(c.status, BacktestStatus.running);
    expect(c.cancelRequested, isTrue);

    final EngineApiException e = await _failure(api.deleteBacktest(5));
    expect(e.statusCode, 409);
    expect(e.code, 'run_active');
    expect(e.messageFa, contains('لغو'));

    await api.deleteBacktest(6);
    expect(http.sent('DELETE /backtests/6'), hasLength(1));
  });

  test('404 backtest_not_found', () async {
    final http = FakeEngineHttp({
      'GET /backtests/99': (_) => engineError(404, 'backtest_not_found', 'بک‌تستی با این شناسه پیدا نشد.'),
    });
    final EngineApiException e = await _failure(_api(http).getBacktest(99));
    expect(e.statusCode, 404);
    expect(e.messageFa, 'بک‌تستی با این شناسه پیدا نشد.');
  });

  test('progress socket URI on the engine port', () {
    expect(_api(FakeEngineHttp()).backtestProgressUri(3).toString(), 'ws://127.0.0.1:8765/ws/backtests/3');
  });
}
