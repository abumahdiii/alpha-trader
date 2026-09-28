import 'dart:io';
import 'dart:typed_data';

import 'package:file_selector/file_selector.dart';
import 'package:path/path.dart' as p;

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/backtest_export.dart';
import 'engine_api.dart';

/// Where an export is saved: the "save as" dialog, the file write and the
/// «باز کردن پوشه» action. The real one is [FileSelectorExportSaver]; tests
/// inject a fake so no dialog ever opens and nothing is written outside a
/// temp folder.
abstract class ExportFileSaver {
  /// Asks the user where to save; null = cancelled.
  Future<String?> pickSaveLocation({required String suggestedName, required BacktestExportFormat format});

  /// Writes [bytes] to [path] (overwriting: the dialog already asked).
  Future<void> writeBytes(String path, Uint8List bytes);

  /// Whether [revealInFolder] is supported here (Windows Explorer).
  bool get canRevealInFolder;

  /// Opens the folder of [path] with the file selected.
  Future<void> revealInFolder(String path);
}

/// Opens the native save dialog (file_selector).
typedef SaveLocationPicker = Future<FileSaveLocation?> Function({
  String? initialDirectory,
  String? suggestedName,
  List<XTypeGroup> acceptedTypeGroups,
});

/// Starts a detached process (Explorer).
typedef ExplorerStarter = Future<void> Function(String executable, List<String> arguments);

/// Native save dialog + `dart:io` write + Windows Explorer.
class FileSelectorExportSaver implements ExportFileSaver {
  FileSelectorExportSaver({SaveLocationPicker? picker, ExplorerStarter? startExplorer, bool? isWindows})
      : _picker = picker ?? _defaultPicker,
        _startExplorer = startExplorer ?? _defaultStartExplorer,
        _isWindows = isWindows ?? Platform.isWindows;

  final SaveLocationPicker _picker;
  final ExplorerStarter _startExplorer;
  final bool _isWindows;

  /// Folder of the last save in this session (the dialog reopens there).
  String? _lastDirectory;

  static Future<FileSaveLocation?> _defaultPicker({
    String? initialDirectory,
    String? suggestedName,
    List<XTypeGroup> acceptedTypeGroups = const <XTypeGroup>[],
  }) =>
      getSaveLocation(
        initialDirectory: initialDirectory,
        suggestedName: suggestedName,
        acceptedTypeGroups: acceptedTypeGroups,
        confirmButtonText: 'ذخیره',
      );

  static Future<void> _defaultStartExplorer(String executable, List<String> arguments) async {
    await Process.start(executable, arguments, mode: ProcessStartMode.detached);
  }

  @override
  Future<String?> pickSaveLocation({required String suggestedName, required BacktestExportFormat format}) async {
    final FileSaveLocation? location = await _picker(
      initialDirectory: _lastDirectory,
      suggestedName: suggestedName,
      acceptedTypeGroups: [
        XTypeGroup(label: format.label, extensions: [format.extension]),
      ],
    );
    if (location == null) return null;
    final String path = withExtension(location.path, format);
    _lastDirectory = p.dirname(path);
    return path;
  }

  /// [path] with the format's extension appended when the user typed a name without it.
  static String withExtension(String path, BacktestExportFormat format) =>
      p.extension(path).toLowerCase() == '.${format.extension}' ? path : '$path.${format.extension}';

  @override
  Future<void> writeBytes(String path, Uint8List bytes) async {
    await File(path).writeAsBytes(bytes, flush: true);
  }

  @override
  bool get canRevealInFolder => _isWindows;

  @override
  Future<void> revealInFolder(String path) async {
    if (!_isWindows) return;
    // `/select,<path>` as ONE argument: Explorer parses its own command line.
    await _startExplorer('explorer.exe', ['/select,', p.normalize(path)]);
  }
}

/// Result of [BacktestExporter.export].
sealed class BacktestExportOutcome {
  const BacktestExportOutcome();
}

/// The user closed the save dialog: nothing was written.
class BacktestExportCancelled extends BacktestExportOutcome {
  const BacktestExportCancelled();
}

class BacktestExportSaved extends BacktestExportOutcome {
  const BacktestExportSaved({required this.path, required this.byteCount});

  final String path;
  final int byteCount;
}

class BacktestExportFailed extends BacktestExportOutcome {
  const BacktestExportFailed({required this.messageFa, this.detail});

  /// Persian, for the user.
  final String messageFa;

  /// Technical secondary line (engine error detail, OS error).
  final String? detail;
}

/// Export flow of the results page: download from the engine -> ask where
/// -> write. Never throws: every failure becomes a Persian
/// [BacktestExportFailed].
class BacktestExporter {
  BacktestExporter({required this.api, required this.saver});

  final EngineApi api;
  final ExportFileSaver saver;

  Future<BacktestExportOutcome> export(
    int runId,
    BacktestExportKind kind, {
    BacktestExportHeader header = BacktestExportHeader.fa,
  }) async {
    _log('export requested: run=$runId kind=${kind.name} format=${kind.format.code} '
        'table=${kind.table.code} header=${header.code}');
    final BacktestExportFile file;
    try {
      file = await api.exportBacktest(runId, format: kind.format, table: kind.table, header: header);
    } on EngineApiException catch (e) {
      _log('export run=$runId download failed: $e');
      return BacktestExportFailed(
        messageFa: e.messageFa,
        detail: e.errorsFa.isNotEmpty ? e.errorsFa.join('\n') : e.detail,
      );
    }
    _log('export run=$runId downloaded ${file.bytes.length} bytes as "${file.fileName}" '
        '(run status ${file.runStatus ?? '?'})');

    final String? path;
    try {
      path = await saver.pickSaveLocation(suggestedName: file.fileName, format: file.format);
    } catch (e) {
      _log('export run=$runId save dialog failed: $e');
      return BacktestExportFailed(messageFa: 'پنجره انتخاب محل ذخیره باز نشد.', detail: '$e');
    }
    if (path == null) {
      _log('export run=$runId: save dialog cancelled, nothing written');
      return const BacktestExportCancelled();
    }
    _log('export run=$runId: save path chosen: $path');

    try {
      await saver.writeBytes(path, file.bytes);
    } on FileSystemException catch (e) {
      _log('export run=$runId write failed: $e');
      return BacktestExportFailed(
        messageFa:
            'ذخیره فایل ناموفق بود؛ شاید فایل در برنامه دیگری (مثلاً Excel) باز است یا اجازه نوشتن در این پوشه نیست.',
        detail: '${e.message}${e.osError != null ? ' (${e.osError!.message})' : ''}: $path',
      );
    } catch (e) {
      _log('export run=$runId write failed: $e');
      return BacktestExportFailed(messageFa: 'ذخیره فایل ناموفق بود.', detail: '$e');
    }
    _log('export run=$runId saved ${file.bytes.length} bytes to $path');
    return BacktestExportSaved(path: path, byteCount: file.bytes.length);
  }

  /// Opens Explorer on the saved file; failures are only logged (a convenience action).
  Future<void> reveal(String path) async {
    try {
      await saver.revealInFolder(path);
      _log('reveal in folder: $path');
    } catch (e) {
      _log('reveal in folder failed for $path: $e');
    }
  }

  static void _log(String message) {
    if (!kDevMode) return;
    final String line = '[BacktestExport] $message';
    devLog(line);
    AppLogger.log(line);
  }
}
