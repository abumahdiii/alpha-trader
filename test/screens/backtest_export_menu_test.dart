// «خروجی» menu of the backtest results header, end to end over the real
// EngineApi and a fake transport. The save layer is a fake: no dialog opens,
// nothing is written to disk, no Explorer starts.

import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/models/backtest_export.dart';
import 'package:alpha_trader/screens/backtest/backtest_export_menu.dart';
import 'package:alpha_trader/screens/backtest/backtest_screen.dart';
import 'package:alpha_trader/services/backtest_exporter.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';

Finder _key(String k) => find.byKey(ValueKey<String>(k));

/// In-memory [ExportFileSaver].
class _FakeSaver implements ExportFileSaver {
  /// Answer of the "dialog" (null = cancelled).
  String? nextPath = r'C:\Exports\out';
  Object? writeError;
  final List<({String suggestedName, BacktestExportFormat format})> picks = [];
  final Map<String, Uint8List> written = {};
  final List<String> revealed = [];

  @override
  bool canRevealInFolder = true;

  @override
  Future<String?> pickSaveLocation({required String suggestedName, required BacktestExportFormat format}) async {
    picks.add((suggestedName: suggestedName, format: format));
    final String? path = nextPath;
    return path == null ? null : FileSelectorExportSaver.withExtension(path, format);
  }

  @override
  Future<void> writeBytes(String path, Uint8List bytes) async {
    if (writeError != null) throw writeError!;
    written[path] = bytes;
  }

  @override
  Future<void> revealInFolder(String path) async => revealed.add(path);
}

class _Page {
  _Page(this.h, this.saver);

  final EngineHarness h;
  final _FakeSaver saver;
}

Future<_Page> _pump(WidgetTester tester, FakeBacktestEngine engine, {_FakeSaver? saver}) async {
  tester.view.physicalSize = const Size(2200, 1100);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final _FakeSaver s = saver ?? _FakeSaver();
  final EngineHarness h = EngineHarness(http: engine.http());
  await tester.pumpWidget(
    h.wrap(BacktestScreen(watcherFactory: FakeSocketConnector().watcher, exportSaver: s)),
  );
  await h.start(tester);
  await settle(tester, 20);
  return _Page(h, s);
}

Future<void> _openRun(WidgetTester tester, int id) async {
  await tester.tap(_key('bt-side-runs'));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 500));
  await tester.tap(_key('bt-run-$id'));
  await settle(tester, 20);
}

Future<void> _openMenu(WidgetTester tester) async {
  await tester.tap(_key('bt-export'));
  await settle(tester);
}

Future<void> _choose(WidgetTester tester, BacktestExportKind kind) async {
  await tester.tap(_key('bt-export-${kind.name}'));
  await settle(tester, 12);
}

FakeBacktestEngine _manual() => FakeBacktestEngine(
      runs: [runSummaryJson(id: 7)],
      details: {7: manualDetailJson(id: 7)},
    );

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('manual run: Excel + trades/metrics/skipped CSV, no windows CSV, English-header option', (tester) async {
    final _Page p = await _pump(tester, _manual());
    await _openRun(tester, 7);
    expect(tester.widget<TextButton>(_key('bt-export')).onPressed, isNotNull);

    await _openMenu(tester);
    expect(_key('bt-export-excelAll'), findsOneWidget);
    expect(find.text('Excel (همه جدول‌ها)'), findsOneWidget);
    expect(find.text('CSV معاملات'), findsOneWidget);
    expect(find.text('CSV معیارها'), findsOneWidget);
    expect(find.text('CSV ردشده‌ها'), findsOneWidget);
    expect(_key('bt-export-csvWindows'), findsNothing);
    expect(find.text(BacktestExportMenu.englishHeaderLabel), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('random run: the windows CSV is offered', (tester) async {
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [runSummaryJson(id: 8, mode: 'random')],
      details: {8: randomDetailJson(id: 8)},
    );
    final _Page p = await _pump(tester, engine);
    await _openRun(tester, 8);
    await _openMenu(tester);
    expect(find.text('CSV پنجره‌ها'), findsOneWidget);
    for (final BacktestExportKind k in BacktestExportKind.values) {
      expect(_key('bt-export-${k.name}'), findsOneWidget);
    }
    await p.h.dispose(tester);
  });

  testWidgets('a running run: the button is disabled and says why', (tester) async {
    final Map<String, Object?> running = runSummaryJson(id: 11, status: 'running', progress: 20);
    final FakeBacktestEngine engine = FakeBacktestEngine(
      runs: [running],
      details: {
        11: {
          ...running,
          'windows': <Object?>[],
          'live': {'id': 11, 'status': 'running', 'phase': 'scanning', 'percent': 20.0},
        },
      },
    );
    final _Page p = await _pump(tester, engine);
    expect(_key('bt-result-running'), findsOneWidget);
    expect(tester.widget<TextButton>(_key('bt-export')).onPressed, isNull);
    expect(
      find
          .ancestor(of: _key('bt-export'), matching: find.byType(Tooltip))
          .evaluate()
          .map((e) => (e.widget as Tooltip).message),
      contains(BacktestExportMenu.notFinishedTooltip),
    );
    await tester.tap(_key('bt-export'), warnIfMissed: false);
    await settle(tester);
    expect(_key('bt-export-excelAll'), findsNothing);
    expect(p.h.http.sent('GET /backtests/11/export'), isEmpty);
    await p.h.dispose(tester);
  });

  testWidgets('Excel: engine bytes saved under the engine file name, snackbar + «باز کردن پوشه»', (tester) async {
    final FakeBacktestEngine engine = _manual();
    final _Page p = await _pump(tester, engine);
    await _openRun(tester, 7);
    await _openMenu(tester);
    await _choose(tester, BacktestExportKind.excelAll);

    final List<Uri> sent = [
      for (final r in p.h.http.requests)
        if (r.uri.path == '/backtests/7/export') r.uri
    ];
    expect(sent.single.queryParameters, {'format': 'xlsx', 'table': 'all', 'header': 'fa'});
    expect(p.saver.picks.single.suggestedName, 'AlphaTrader_bt7_XAUUSD.x_manual_all.xlsx');
    expect(p.saver.picks.single.format, BacktestExportFormat.xlsx);
    expect(p.saver.written.keys.single, r'C:\Exports\out.xlsx');
    expect(p.saver.written.values.single, exportBytes(7, 'xlsx', 'all'));

    expect(_key('bt-export-saved'), findsOneWidget);
    expect(find.textContaining(r'C:\Exports\out.xlsx'), findsOneWidget);
    await tester.tap(find.text(BacktestExportMenu.openFolderLabel));
    await settle(tester);
    expect(p.saver.revealed, [r'C:\Exports\out.xlsx']);
    await p.h.dispose(tester);
  });

  testWidgets('CSV with «سرستون انگلیسی»: header=en and the table of the entry', (tester) async {
    final _Page p = await _pump(tester, _manual());
    await _openRun(tester, 7);
    await _openMenu(tester);
    await tester.tap(find.byType(CheckboxMenuButton));
    await settle(tester);
    // The checkbox keeps the menu open.
    expect(_key('bt-export-csvMetrics'), findsOneWidget);
    await _choose(tester, BacktestExportKind.csvMetrics);

    final Uri sent = p.h.http.requests.lastWhere((r) => r.uri.path == '/backtests/7/export').uri;
    expect(sent.queryParameters, {'format': 'csv', 'table': 'metrics', 'header': 'en'});
    expect(p.saver.written.keys.single, r'C:\Exports\out.csv');
    expect(p.saver.picks.single.suggestedName, 'AlphaTrader_bt7_XAUUSD.x_manual_metrics.csv');
    await p.h.dispose(tester);
  });

  testWidgets('cancel in the dialog: nothing written, no message', (tester) async {
    final _Page p = await _pump(tester, _manual(), saver: _FakeSaver()..nextPath = null);
    await _openRun(tester, 7);
    await _openMenu(tester);
    await _choose(tester, BacktestExportKind.csvTrades);
    expect(p.saver.picks, hasLength(1));
    expect(p.saver.written, isEmpty);
    expect(_key('bt-export-saved'), findsNothing);
    expect(find.text(BacktestExportMenu.failedTitle), findsNothing);
    await p.h.dispose(tester);
  });

  testWidgets('engine error: Persian error notice, the dialog never opens', (tester) async {
    final FakeBacktestEngine engine = _manual()
      ..onExport = (int id, _) => engineError(409, 'run_not_finished', 'این بک‌تست هنوز تمام نشده است.');
    final _Page p = await _pump(tester, engine);
    await _openRun(tester, 7);
    await _openMenu(tester);
    await _choose(tester, BacktestExportKind.csvSkipped);
    expect(find.text(BacktestExportMenu.failedTitle), findsOneWidget);
    expect(find.textContaining('هنوز تمام نشده است'), findsOneWidget);
    expect(p.saver.picks, isEmpty);
    expect(p.saver.written, isEmpty);
    await p.h.dispose(tester);
  });

  testWidgets('write error: Persian error notice', (tester) async {
    final _Page p = await _pump(
      tester,
      _manual(),
      saver: _FakeSaver()..writeError = const _WriteFailure(),
    );
    await _openRun(tester, 7);
    await _openMenu(tester);
    await _choose(tester, BacktestExportKind.csvTrades);
    expect(find.text(BacktestExportMenu.failedTitle), findsOneWidget);
    expect(find.textContaining('ذخیره فایل ناموفق بود'), findsOneWidget);
    expect(_key('bt-export-saved'), findsNothing);
    await p.h.dispose(tester);
  });
}

/// A non-FileSystemException write failure (the generic branch).
class _WriteFailure implements Exception {
  const _WriteFailure();

  @override
  String toString() => 'disk full';
}
