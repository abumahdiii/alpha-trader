// EngineApi.exportBacktest over a fake transport: raw bytes, the file name
// from Content-Disposition, query parameters and the Persian errors.

import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/backtest_export.dart';
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
  // Every byte value, incl. a BOM, NUL and 0xFF: nothing may be decoded or changed.
  final List<int> binary = [0xEF, 0xBB, 0xBF, 0x00, 0x0D, 0x0A, 0xFF, 0x50, 0x4B, 0x03, 0x04];

  test('xlsx: exact bytes, quoted file name, table=all, Persian header by default', () async {
    final http = FakeEngineHttp({
      'GET /backtests/7/export': (_) =>
          exportBody(binary, 'AlphaTrader_bt7_XAUUSD.x_manual_all.xlsx', format: 'xlsx', status: 'done'),
    });
    final BacktestExportFile f = await _api(http).exportBacktest(7);
    expect(f.bytes, binary);
    expect(f.fileName, 'AlphaTrader_bt7_XAUUSD.x_manual_all.xlsx');
    expect(f.format, BacktestExportFormat.xlsx);
    expect(f.runStatus, 'done');
    expect(f.contentType, contains('spreadsheetml'));
    expect(http.requests.single.uri.queryParameters, {'format': 'xlsx', 'table': 'all', 'header': 'fa'});
  });

  test('csv: one table, English header on request, bare (unquoted) file name', () async {
    final http = FakeEngineHttp({
      'GET /backtests/8/export': (_) => exportBody(binary, 'AlphaTrader_bt8_XAUUSD.x_random_windows.csv', quoted: false),
    });
    final BacktestExportFile f = await _api(http).exportBacktest(
      8,
      format: BacktestExportFormat.csv,
      table: BacktestExportTable.windows,
      header: BacktestExportHeader.en,
    );
    expect(f.fileName, 'AlphaTrader_bt8_XAUUSD.x_random_windows.csv');
    expect(f.bytes, binary);
    expect(http.requests.single.uri.queryParameters, {'format': 'csv', 'table': 'windows', 'header': 'en'});
  });

  test('no Content-Disposition -> a local fallback name', () async {
    final http = FakeEngineHttp({'GET /backtests/9/export': (_) => exportBody(binary, null)});
    final BacktestExportFile f =
        await _api(http).exportBacktest(9, format: BacktestExportFormat.csv, table: BacktestExportTable.metrics);
    expect(f.fileName, 'AlphaTrader_bt9_metrics.csv');
    expect(BacktestExportFile.fallbackName(9, BacktestExportFormat.xlsx, BacktestExportTable.trades),
        'AlphaTrader_bt9_all.xlsx');
  });

  group('fileNameFromContentDisposition', () {
    String? parse(String? h) => BacktestExportFile.fileNameFromContentDisposition(h);

    test('quoted, bare, RFC 5987 and missing', () {
      expect(parse('attachment; filename="AlphaTrader_bt1_XAUUSD.x_manual_trades.csv"'),
          'AlphaTrader_bt1_XAUUSD.x_manual_trades.csv');
      expect(parse('attachment; filename=AlphaTrader_bt1_all.xlsx'), 'AlphaTrader_bt1_all.xlsx');
      expect(parse('attachment; filename=a.csv; size=10'), 'a.csv');
      expect(parse('attachment; FILENAME="a b.csv"'), 'a b.csv');
      expect(parse("attachment; filename=\"x.csv\"; filename*=UTF-8''%D8%A8%DA%A9.csv"), 'بک.csv');
      expect(parse('attachment'), isNull);
      expect(parse(''), isNull);
      expect(parse(null), isNull);
    });

    test('never a path or a forbidden character', () {
      expect(parse(r'attachment; filename="..\..\evil.csv"'), 'evil.csv');
      expect(parse('attachment; filename="/tmp/x.csv"'), 'x.csv');
      expect(parse('attachment; filename="a:b*c?.csv"'), 'a_b_c_.csv');
      expect(parse('attachment; filename=".."'), isNull);
    });
  });

  test('409 run_not_finished keeps the engine code and Persian message', () async {
    final http = FakeEngineHttp({
      'GET /backtests/5/export': (_) =>
          engineError(409, 'run_not_finished', 'این بک‌تست هنوز تمام نشده است؛ خروجی بعد از پایان اجرا ممکن است.'),
    });
    final EngineApiException e = await _failure(_api(http).exportBacktest(5));
    expect(e.statusCode, 409);
    expect(e.code, 'run_not_finished');
    expect(e.messageFa, contains('تمام نشده'));
  });

  test('409 without message_fa -> the local Persian sentence for run_not_finished', () async {
    final http = FakeEngineHttp({
      'GET /backtests/5/export': (_) => jsonBody({
            'detail': {'code': 'run_not_finished'},
          }, 409),
    });
    final EngineApiException e = await _failure(_api(http).exportBacktest(5));
    expect(e.messageFa, 'این اجرا هنوز تمام نشده است؛ خروجی بعد از پایان اجرا گرفته می‌شود.');
  });

  test('404 and 422 are Persian too', () async {
    final http = FakeEngineHttp({
      'GET /backtests/6/export': (_) => engineError(422, 'invalid_query', '«جدول» نامعتبر است.'),
    });
    final EngineApi api = _api(http);
    final EngineApiException notFound = await _failure(api.exportBacktest(404));
    expect(notFound.statusCode, 404);
    expect(notFound.messageFa, 'موردی که از موتور خواسته شد پیدا نشد.');

    final EngineApiException invalid = await _failure(api.exportBacktest(6));
    expect(invalid.isValidation, isTrue);
    expect(invalid.code, 'invalid_query');
    expect(invalid.messageFa, contains('«جدول»'));
  });

  test('an empty 200 body is malformed, not an empty file', () async {
    final http = FakeEngineHttp({'GET /backtests/3/export': (_) => exportBody(const [], 'x.csv')});
    final EngineApiException e = await _failure(_api(http).exportBacktest(3));
    expect(e.kind, EngineApiErrorKind.malformedBody);
    expect(e.messageFa, contains('خالی'));
  });

  test('menu entries per mode: windows CSV only for random runs', () {
    expect(BacktestExportKind.forMode(BacktestMode.manual), [
      BacktestExportKind.excelAll,
      BacktestExportKind.csvTrades,
      BacktestExportKind.csvMetrics,
      BacktestExportKind.csvSkipped,
    ]);
    expect(BacktestExportKind.forMode(BacktestMode.random), contains(BacktestExportKind.csvWindows));
    expect(BacktestExportKind.forMode(BacktestMode.random), hasLength(5));
  });
}
