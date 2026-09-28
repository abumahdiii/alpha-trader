// BacktestExporter + FileSelectorExportSaver with a fake save dialog: files
// are only ever written below a fresh temp folder, no dialog opens and no
// Explorer starts.

import 'dart:io';

import 'package:file_selector/file_selector.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;

import 'package:alpha_trader/models/backtest_export.dart';
import 'package:alpha_trader/services/backtest_exporter.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';

/// Records the dialog calls; answers [answer] (null = cancelled).
class _Dialog {
  String? answer;
  final List<({String? initialDirectory, String? suggestedName, List<XTypeGroup> groups})> calls = [];

  Future<FileSaveLocation?> call({
    String? initialDirectory,
    String? suggestedName,
    List<XTypeGroup> acceptedTypeGroups = const <XTypeGroup>[],
  }) async {
    calls.add((initialDirectory: initialDirectory, suggestedName: suggestedName, groups: acceptedTypeGroups));
    return answer == null ? null : FileSaveLocation(answer!);
  }
}

void main() {
  late Directory tmp;
  late _Dialog dialog;
  late List<(String, List<String>)> started;
  late FakeEngineHttp http;
  late BacktestExporter exporter;

  final List<int> bytes = exportBytes(7, 'csv', 'trades');

  setUp(() {
    tmp = Directory.systemTemp.createTempSync('alpha_export_test_');
    dialog = _Dialog();
    started = [];
    http = FakeEngineHttp({
      'GET /backtests/7/export': (r) => exportBody(
            r.uri.queryParameters['format'] == 'xlsx' ? exportBytes(7, 'xlsx', 'all') : bytes,
            r.uri.queryParameters['format'] == 'xlsx'
                ? 'AlphaTrader_bt7_XAUUSD.x_manual_all.xlsx'
                : 'AlphaTrader_bt7_XAUUSD.x_manual_trades.csv',
            format: r.uri.queryParameters['format'] ?? 'xlsx',
          ),
      'GET /backtests/5/export': (_) =>
          engineError(409, 'run_not_finished', 'این بک‌تست هنوز تمام نشده است؛ خروجی بعد از پایان اجرا ممکن است.'),
    });
    exporter = BacktestExporter(
      api: EngineApi(port: 8765, httpClientAdapter: http),
      saver: FileSelectorExportSaver(
        picker: dialog.call,
        startExplorer: (String exe, List<String> args) async => started.add((exe, args)),
        isWindows: true,
      ),
    );
  });

  tearDown(() => tmp.deleteSync(recursive: true));

  test('success: suggested name + type group from the engine, bytes written exactly', () async {
    final String target = p.join(tmp.path, 'trades.csv');
    dialog.answer = target;
    final BacktestExportOutcome o = await exporter.export(7, BacktestExportKind.csvTrades);
    expect(o, isA<BacktestExportSaved>());
    final BacktestExportSaved saved = o as BacktestExportSaved;
    expect(saved.path, target);
    expect(saved.byteCount, bytes.length);
    expect(File(target).readAsBytesSync(), bytes);
    expect(dialog.calls.single.suggestedName, 'AlphaTrader_bt7_XAUUSD.x_manual_trades.csv');
    expect(dialog.calls.single.groups.single.extensions, ['csv']);
    expect(http.requests.single.uri.queryParameters, {'format': 'csv', 'table': 'trades', 'header': 'fa'});
  });

  test('a name typed without the extension gets it; the next dialog opens in the same folder', () async {
    dialog.answer = p.join(tmp.path, 'report');
    final BacktestExportSaved o = await exporter.export(7, BacktestExportKind.excelAll) as BacktestExportSaved;
    expect(o.path, p.join(tmp.path, 'report.xlsx'));
    expect(File(o.path).existsSync(), isTrue);
    expect(dialog.calls.single.groups.single.extensions, ['xlsx']);
    expect(dialog.calls.single.initialDirectory, isNull);

    dialog.answer = p.join(tmp.path, 'again.XLSX');
    final BacktestExportSaved o2 = await exporter.export(7, BacktestExportKind.excelAll) as BacktestExportSaved;
    expect(o2.path, p.join(tmp.path, 'again.XLSX'));
    expect(dialog.calls.last.initialDirectory, tmp.path);
  });

  test('cancel: nothing is written', () async {
    dialog.answer = null;
    final BacktestExportOutcome o = await exporter.export(7, BacktestExportKind.csvTrades);
    expect(o, isA<BacktestExportCancelled>());
    expect(tmp.listSync(), isEmpty);
  });

  test('engine 409: Persian message, the dialog never opens', () async {
    final BacktestExportOutcome o = await exporter.export(5, BacktestExportKind.excelAll);
    expect(o, isA<BacktestExportFailed>());
    expect((o as BacktestExportFailed).messageFa, contains('تمام نشده'));
    expect(dialog.calls, isEmpty);
  });

  test('write failure (folder gone): Persian message with the OS detail', () async {
    dialog.answer = p.join(tmp.path, 'missing_dir', 'x.csv');
    final BacktestExportOutcome o = await exporter.export(7, BacktestExportKind.csvTrades);
    expect(o, isA<BacktestExportFailed>());
    final BacktestExportFailed f = o as BacktestExportFailed;
    expect(f.messageFa, startsWith('ذخیره فایل ناموفق بود'));
    expect(f.detail, contains('x.csv'));
    expect(tmp.listSync(), isEmpty);
  });

  test('a dialog that throws becomes a Persian failure', () async {
    final BacktestExporter broken = BacktestExporter(
      api: EngineApi(port: 8765, httpClientAdapter: http),
      saver: FileSelectorExportSaver(
        picker: ({String? initialDirectory, String? suggestedName, List<XTypeGroup> acceptedTypeGroups = const []}) =>
            throw StateError('no window'),
        isWindows: true,
      ),
    );
    final BacktestExportOutcome o = await broken.export(7, BacktestExportKind.csvTrades);
    expect((o as BacktestExportFailed).messageFa, 'پنجره انتخاب محل ذخیره باز نشد.');
  });

  test('«باز کردن پوشه»: Explorer with /select on Windows only', () async {
    final String file = p.join(tmp.path, 'a.csv');
    await exporter.reveal(file);
    expect(started.single.$1, 'explorer.exe');
    expect(started.single.$2, ['/select,', p.normalize(file)]);

    final FileSelectorExportSaver other = FileSelectorExportSaver(
      picker: dialog.call,
      startExplorer: (String exe, List<String> args) async => started.add((exe, args)),
      isWindows: false,
    );
    expect(other.canRevealInFolder, isFalse);
    await other.revealInFolder(file);
    expect(started, hasLength(1));
  });

  test('withExtension', () {
    expect(FileSelectorExportSaver.withExtension(r'C:\x\a', BacktestExportFormat.csv), r'C:\x\a.csv');
    expect(FileSelectorExportSaver.withExtension(r'C:\x\a.CSV', BacktestExportFormat.csv), r'C:\x\a.CSV');
    expect(FileSelectorExportSaver.withExtension(r'C:\x\a.csv', BacktestExportFormat.xlsx), r'C:\x\a.csv.xlsx');
  });
}
