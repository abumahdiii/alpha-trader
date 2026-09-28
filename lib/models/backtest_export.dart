import 'package:flutter/foundation.dart';

import 'backtest_models.dart';

/// `format` of `GET /backtests/{id}/export`.
enum BacktestExportFormat {
  /// Every table of the run, one sheet each.
  xlsx('xlsx', 'Excel'),

  /// One table.
  csv('csv', 'CSV');

  const BacktestExportFormat(this.code, this.label);

  final String code;

  /// File-type label of the save dialog.
  final String label;

  /// File extension without the dot (same as [code]).
  String get extension => code;
}

/// `table` of `GET /backtests/{id}/export` (csv: one of them; xlsx: [all]).
enum BacktestExportTable {
  all('all'),
  trades('trades'),
  windows('windows'),
  skipped('skipped'),
  equity('equity'),
  run('run'),
  metrics('metrics');

  const BacktestExportTable(this.code);

  final String code;
}

/// `header` of `GET /backtests/{id}/export`: English snake_case keys or Persian labels.
enum BacktestExportHeader {
  en('en'),
  fa('fa');

  const BacktestExportHeader(this.code);

  final String code;
}

/// One entry of the «خروجی» menu of the results page.
enum BacktestExportKind {
  excelAll(BacktestExportFormat.xlsx, BacktestExportTable.all, 'Excel (همه جدول‌ها)'),
  csvTrades(BacktestExportFormat.csv, BacktestExportTable.trades, 'CSV معاملات'),
  csvMetrics(BacktestExportFormat.csv, BacktestExportTable.metrics, 'CSV معیارها'),
  csvWindows(BacktestExportFormat.csv, BacktestExportTable.windows, 'CSV پنجره‌ها', randomOnly: true),
  csvSkipped(BacktestExportFormat.csv, BacktestExportTable.skipped, 'CSV ردشده‌ها');

  const BacktestExportKind(this.format, this.table, this.labelFa, {this.randomOnly = false});

  final BacktestExportFormat format;
  final BacktestExportTable table;
  final String labelFa;

  /// Offered only for random runs (the windows table of a manual run is one row).
  final bool randomOnly;

  /// Menu entries for a run of [mode], in menu order.
  static List<BacktestExportKind> forMode(BacktestMode mode) => [
        for (final BacktestExportKind k in values)
          if (!k.randomOnly || mode == BacktestMode.random) k
      ];
}

/// A downloaded export: the exact bytes the engine sent and the file name it suggested.
@immutable
class BacktestExportFile {
  const BacktestExportFile({
    required this.bytes,
    required this.fileName,
    required this.format,
    this.contentType,
    this.runStatus,
  });

  final Uint8List bytes;

  /// From `Content-Disposition` (sanitised to a bare file name), else a local fallback.
  final String fileName;
  final BacktestExportFormat format;
  final String? contentType;

  /// `X-Alpha-Run-Status` of the response (the run's status when exported).
  final String? runStatus;

  /// Name used when the engine sent no usable `Content-Disposition`.
  static String fallbackName(int runId, BacktestExportFormat format, BacktestExportTable table) =>
      'AlphaTrader_bt${runId}_${format == BacktestExportFormat.xlsx ? 'all' : table.code}.${format.extension}';

  /// The file name of a `Content-Disposition` header, or null when it has none.
  ///
  /// Accepts `filename="a b.csv"`, `filename=a.csv` and RFC 5987
  /// `filename*=UTF-8''a%20b.csv` (preferred when both are present). Only
  /// the last path segment is kept and characters Windows forbids in file
  /// names become `_`, so a header can never point the save dialog at
  /// another folder.
  static String? fileNameFromContentDisposition(String? header) {
    if (header == null || header.trim().isEmpty) return null;
    String? name;
    final RegExpMatch? extended =
        RegExp(r"""filename\*\s*=\s*(?:[\w-]+'[^']*')?("?)([^";]+)\1""", caseSensitive: false).firstMatch(header);
    if (extended != null) {
      try {
        name = Uri.decodeComponent(extended.group(2)!);
      } on ArgumentError {
        name = extended.group(2);
      }
    }
    if (name == null) {
      final RegExpMatch? quoted = RegExp(r'filename\s*=\s*"([^"]*)"', caseSensitive: false).firstMatch(header);
      final RegExpMatch? bare = RegExp(r'filename\s*=\s*([^";]+)', caseSensitive: false).firstMatch(header);
      name = quoted?.group(1) ?? bare?.group(1);
    }
    if (name == null) return null;
    final String base = name.trim().split(RegExp(r'[\\/]')).last;
    final String safe = base.replaceAll(RegExp(r'[<>:"|?*\x00-\x1F]'), '_').trim();
    return safe.isEmpty || safe == '.' || safe == '..' ? null : safe;
  }
}
