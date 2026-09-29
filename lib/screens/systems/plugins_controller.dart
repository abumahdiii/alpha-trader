import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../models/strategy_plugin.dart';
import '../../services/engine_api.dart';
import '../../services/plugin_files.dart';

/// Result of «دانلود قالب».
sealed class TemplateDownloadOutcome {
  const TemplateDownloadOutcome();
}

class TemplateDownloadCancelled extends TemplateDownloadOutcome {
  const TemplateDownloadCancelled();
}

class TemplateDownloadSaved extends TemplateDownloadOutcome {
  const TemplateDownloadSaved({required this.path, required this.byteCount});

  final String path;
  final int byteCount;
}

class TemplateDownloadFailed extends TemplateDownloadOutcome {
  const TemplateDownloadFailed({required this.messageFa, this.detail});

  final String messageFa;
  final String? detail;
}

/// Result of «بارگذاری سیستم».
sealed class PluginUploadOutcome {
  const PluginUploadOutcome();
}

/// The open dialog was closed: nothing was sent.
class PluginUploadCancelled extends PluginUploadOutcome {
  const PluginUploadCancelled();
}

/// The picked file could not be read / is too large: nothing was sent.
class PluginUploadFileError extends PluginUploadOutcome {
  const PluginUploadFileError(this.error);

  final PluginFileException error;
}

/// 201 (new version) or 200 (same file again).
class PluginUploadAccepted extends PluginUploadOutcome {
  const PluginUploadAccepted(this.result);

  final PluginUploadResult result;
}

/// The engine refused the file or the call failed: 422 `invalid_plugin` /
/// `invalid_body` (errors with line numbers), 409 `version_conflict`,
/// timeout / connection / 5xx.
class PluginUploadRejected extends PluginUploadOutcome {
  const PluginUploadRejected(this.error, {required this.fileName});

  final EngineApiException error;
  final String fileName;

  bool get isVersionConflict => error.code == 'version_conflict';
}

/// Plugin management of the «سیستم‌ها» page: the stored versions
/// (`GET /plugins`), template download, upload, enable / disable / archive.
///
/// Presenter only: the engine validates, stores, registers and runs the
/// files; every state shown here is its answer. [onStrategiesChanged] runs
/// after anything that can change the registered strategies (the page then
/// re-reads `/strategies`).
class PluginsController extends ChangeNotifier {
  PluginsController({required this.api, required this.files, this.onStrategiesChanged});

  final EngineApi api;
  final PluginFileIo files;
  final VoidCallback? onStrategiesChanged;
  bool _disposed = false;

  List<StrategyPlugin> _plugins = const [];
  bool _loading = false;
  EngineApiException? _error;
  bool _downloading = false;
  String? _uploadingFile;
  final Set<String> _busy = <String>{};

  List<StrategyPlugin> get plugins => _plugins;
  bool get loading => _loading;
  EngineApiException? get error => _error;
  bool get downloading => _downloading;

  /// Name of the file being validated by the engine (null = no upload).
  String? get uploadingFile => _uploadingFile;
  bool get uploading => _uploadingFile != null;

  /// A per-version action (enable / disable / archive) is running.
  bool isBusy(StrategyPlugin p) => _busy.contains(p.key);

  /// The registered plugin version of strategy [name], if it is a plugin.
  StrategyPlugin? registeredFor(String name) {
    for (final StrategyPlugin p in _plugins) {
      if (p.name == name && p.registered) return p;
    }
    return null;
  }

  Future<void> load() async {
    _loading = true;
    _error = null;
    _notify();
    try {
      final List<StrategyPlugin> list = await api.listPlugins();
      if (_disposed) return;
      _plugins = list;
      _log('plugins: ${list.map((StrategyPlugin p) => '${p.key} ${p.status.code}'
          '${p.registered ? ' registered' : ''} sha=${p.shortSha256}').join(', ')}');
    } on EngineApiException catch (e) {
      if (_disposed) return;
      _log('plugins failed: $e');
      _error = e;
    } finally {
      if (!_disposed) {
        _loading = false;
        _notify();
      }
    }
  }

  /// `GET /plugins/template` -> save dialog -> UTF-8 file. Never throws.
  Future<TemplateDownloadOutcome> downloadTemplate() async {
    if (_downloading) return const TemplateDownloadCancelled();
    _downloading = true;
    _notify();
    try {
      final PluginTemplate template;
      try {
        template = await api.getPluginTemplate();
      } on EngineApiException catch (e) {
        _log('template download failed: $e');
        return TemplateDownloadFailed(messageFa: e.messageFa, detail: e.detail);
      }
      final int bytes = template.content.length;
      _log('template received: "${template.filename}" ($bytes chars)');
      final String? path;
      try {
        path = await files.pickTemplateSaveLocation(suggestedName: template.filename);
      } catch (e) {
        _log('template save dialog failed: $e');
        return TemplateDownloadFailed(messageFa: 'پنجره انتخاب محل ذخیره باز نشد.', detail: '$e');
      }
      if (path == null) {
        _log('template save cancelled: nothing written');
        return const TemplateDownloadCancelled();
      }
      try {
        await files.writeText(path, template.content);
      } catch (e) {
        _log('template write failed ($path): $e');
        return TemplateDownloadFailed(
          messageFa: 'ذخیره فایل قالب ناموفق بود؛ شاید اجازه نوشتن در این پوشه نیست.',
          detail: '$e',
        );
      }
      _log('template saved: $path ($bytes chars, UTF-8)');
      return TemplateDownloadSaved(path: path, byteCount: bytes);
    } finally {
      _downloading = false;
      _notify();
    }
  }

  /// Open dialog -> read UTF-8 -> `POST /plugins` (long budget: the engine
  /// runs the file in its sandbox first). Never throws.
  Future<PluginUploadOutcome> upload() async {
    if (uploading) return const PluginUploadCancelled();
    final PickedPluginFile? file;
    try {
      file = await files.pickPluginFile();
    } on PluginFileException catch (e) {
      _log('upload: file refused before sending: $e');
      return PluginUploadFileError(e);
    } catch (e) {
      _log('upload: open dialog / read failed: $e');
      return PluginUploadFileError(PluginFileException('خواندن فایل ناموفق بود.', detail: '$e'));
    }
    if (file == null) {
      _log('upload: open dialog cancelled');
      return const PluginUploadCancelled();
    }
    _uploadingFile = file.name;
    _notify();
    final Stopwatch watch = Stopwatch()..start();
    _log('upload "${file.name}" (${file.byteCount} bytes) from ${file.path}: validating in the engine...');
    try {
      final PluginUploadResult r = await api.uploadPlugin(filename: file.name, source: file.text);
      _log('upload "${file.name}" -> HTTP ${r.created ? 201 : 200} in ${watch.elapsedMilliseconds} ms: '
          '${r.plugin} validation=${r.plugin.validation.staticChecks.length} static checks, '
          'dynamic ${r.plugin.validation.dynamicReport?.elapsedS ?? '-'} s');
      if (_disposed) return PluginUploadAccepted(r);
      _upsert(r.plugin);
      onStrategiesChanged?.call();
      unawaited(load());
      return PluginUploadAccepted(r);
    } on EngineApiException catch (e) {
      _log('upload "${file.name}" -> ${e.statusCode ?? e.kind.name} ${e.code ?? ''} in '
          '${watch.elapsedMilliseconds} ms (${e.errorsFa.length} errors)');
      return PluginUploadRejected(e, fileName: file.name);
    } finally {
      _uploadingFile = null;
      _notify();
    }
  }

  /// Enable / disable one version. Returns the engine's error, or null.
  Future<EngineApiException?> setEnabled(StrategyPlugin p, bool enabled) => _action(
        p,
        enabled ? 'enable' : 'disable',
        () => enabled ? api.enablePlugin(p.name, p.version) : api.disablePlugin(p.name, p.version),
      );

  /// Archive one version (the engine keeps its files). Returns the
  /// engine's error (409 `plugin_in_use` while a backtest uses it), or null.
  Future<EngineApiException?> archive(StrategyPlugin p) =>
      _action(p, 'archive', () => api.archivePlugin(p.name, p.version));

  Future<EngineApiException?> _action(
    StrategyPlugin p,
    String what,
    Future<StrategyPlugin> Function() call,
  ) async {
    if (!_busy.add(p.key)) return null;
    _log('$what ${p.key} (${p.status.code}) -> ...');
    _notify();
    try {
      final StrategyPlugin updated = await call();
      _log('$what ${p.key} -> ${updated.status.code} registered=${updated.registered}');
      if (_disposed) return null;
      _upsert(updated);
      onStrategiesChanged?.call();
      // Another version of the same name may have become the registered one.
      unawaited(load());
      return null;
    } on EngineApiException catch (e) {
      _log('$what ${p.key} failed: $e');
      if (!_disposed && e.statusCode == 404) unawaited(load());
      return e;
    } finally {
      _busy.remove(p.key);
      _notify();
    }
  }

  void _upsert(StrategyPlugin p) {
    final List<StrategyPlugin> next = [
      for (final StrategyPlugin x in _plugins)
        if (x.key != p.key) x,
    ];
    _plugins = List<StrategyPlugin>.unmodifiable([p, ...next]);
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  static void _log(String message) {
    if (!kDevMode) return;
    final String line = '[Plugins] $message';
    devLog(line);
    AppLogger.log(line);
  }
}
