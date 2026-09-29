import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:file_selector/file_selector.dart';
import 'package:path/path.dart' as p;

import 'backtest_exporter.dart' show ExplorerStarter, SaveLocationPicker;

/// Largest plugin file the engine accepts (`plugins/validator.py`
/// `MAX_SOURCE_BYTES` = 256 KiB). Checked here only so a huge file is never
/// read into a request; the engine enforces it again.
const int kPluginMaxBytes = 256 * 1024;

/// A `.py` file the user picked for upload, read as UTF-8 text.
class PickedPluginFile {
  const PickedPluginFile({required this.name, required this.path, required this.text, required this.byteCount});

  /// File name only (what `POST /plugins` gets as `filename`).
  final String name;
  final String path;

  /// The file text (UTF-8; a leading BOM removed, as the engine hashes it).
  final String text;
  final int byteCount;
}

/// The picked file cannot be sent (too large, not UTF-8, unreadable).
/// [messageFa] is for the user; nothing was sent to the engine.
class PluginFileException implements Exception {
  const PluginFileException(this.messageFa, {this.detail});

  final String messageFa;
  final String? detail;

  @override
  String toString() => 'PluginFileException: $messageFa${detail == null ? '' : ' ($detail)'}';
}

/// File dialogs and file access of the «سیستم‌ها» page: save the template,
/// pick a `.py` to upload, reveal a saved file. The real one is
/// [FileSelectorPluginFiles]; tests inject a fake so no dialog opens.
abstract class PluginFileIo {
  /// «ذخیره قالب»: where to save [suggestedName]; null = cancelled.
  Future<String?> pickTemplateSaveLocation({required String suggestedName});

  /// Writes [content] as UTF-8 to [path] (overwriting: the dialog asked).
  Future<void> writeText(String path, String content);

  /// «بارگذاری سیستم»: picks and reads one `.py`; null = cancelled. Throws
  /// [PluginFileException] when the file cannot be sent.
  Future<PickedPluginFile?> pickPluginFile();

  /// Whether [revealInFolder] is supported here (Windows Explorer).
  bool get canRevealInFolder;

  /// Opens the folder of [path] with the file selected.
  Future<void> revealInFolder(String path);
}

/// Opens the native open dialog (file_selector).
typedef OpenFilePicker = Future<XFile?> Function({
  String? initialDirectory,
  List<XTypeGroup> acceptedTypeGroups,
});

/// Native dialogs (file_selector) + `dart:io` + Windows Explorer.
class FileSelectorPluginFiles implements PluginFileIo {
  FileSelectorPluginFiles({
    SaveLocationPicker? savePicker,
    OpenFilePicker? openPicker,
    ExplorerStarter? startExplorer,
    bool? isWindows,
  })  : _savePicker = savePicker ?? _defaultSavePicker,
        _openPicker = openPicker ?? _defaultOpenPicker,
        _startExplorer = startExplorer ?? _defaultStartExplorer,
        _isWindows = isWindows ?? Platform.isWindows;

  final SaveLocationPicker _savePicker;
  final OpenFilePicker _openPicker;
  final ExplorerStarter _startExplorer;
  final bool _isWindows;

  /// Last folder used in this session (both dialogs reopen there).
  String? _lastDirectory;

  static const XTypeGroup _python = XTypeGroup(label: 'Python', extensions: ['py']);

  static Future<FileSaveLocation?> _defaultSavePicker({
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

  static Future<XFile?> _defaultOpenPicker({
    String? initialDirectory,
    List<XTypeGroup> acceptedTypeGroups = const <XTypeGroup>[],
  }) =>
      openFile(
        initialDirectory: initialDirectory,
        acceptedTypeGroups: acceptedTypeGroups,
        confirmButtonText: 'بارگذاری',
      );

  static Future<void> _defaultStartExplorer(String executable, List<String> arguments) async {
    await Process.start(executable, arguments, mode: ProcessStartMode.detached);
  }

  @override
  Future<String?> pickTemplateSaveLocation({required String suggestedName}) async {
    final FileSaveLocation? location = await _savePicker(
      initialDirectory: _lastDirectory,
      suggestedName: suggestedName,
      acceptedTypeGroups: const [_python],
    );
    if (location == null) return null;
    final String path = withPyExtension(location.path);
    _lastDirectory = p.dirname(path);
    return path;
  }

  /// [path] with `.py` appended when the user typed a name without it.
  static String withPyExtension(String path) => p.extension(path).toLowerCase() == '.py' ? path : '$path.py';

  @override
  Future<void> writeText(String path, String content) async {
    await File(path).writeAsString(content, encoding: utf8, flush: true);
  }

  @override
  Future<PickedPluginFile?> pickPluginFile() async {
    final XFile? file = await _openPicker(initialDirectory: _lastDirectory, acceptedTypeGroups: const [_python]);
    if (file == null) return null;
    _lastDirectory = p.dirname(file.path);
    final int length = await file.length();
    if (length > kPluginMaxBytes) {
      throw PluginFileException(
        'حجم فایل ($length بایت) از سقف ${kPluginMaxBytes ~/ 1024} کیلوبایت بیشتر است؛ فایل فرستاده نشد.',
        detail: file.path,
      );
    }
    final Uint8List bytes = await file.readAsBytes();
    return PickedPluginFile(name: file.name, path: file.path, text: decodePluginSource(bytes), byteCount: bytes.length);
  }

  /// Strict UTF-8 (the engine refuses anything else); a leading BOM is
  /// dropped, exactly as the engine hashes the file.
  static String decodePluginSource(List<int> bytes) {
    final String text;
    try {
      text = utf8.decode(bytes);
    } on FormatException catch (e) {
      throw PluginFileException(
        'فایل با کدگذاری UTF-8 ذخیره نشده است؛ آن را با UTF-8 ذخیره کنید و دوباره بارگذاری کنید.',
        detail: e.message,
      );
    }
    return text.startsWith('﻿') ? text.substring(1) : text;
  }

  @override
  bool get canRevealInFolder => _isWindows;

  @override
  Future<void> revealInFolder(String path) async {
    if (!_isWindows) return;
    // Same argument shape as the backtest export: `/select,` then the path.
    await _startExplorer('explorer.exe', ['/select,', p.normalize(path)]);
  }
}
