// Fixtures for the strategy-plugin UI: engine bodies of /plugins and a
// plugin entry of /strategies (mirroring routes/plugins.py `PluginOut` and
// routes/strategies.py `StrategyOut`), a stateful fake of the /plugins
// routes, and injectable file dialogs (no real dialog, no file written).

import 'dart:async';

import 'package:dio/dio.dart';

import 'package:alpha_trader/services/plugin_files.dart';

import 'engine_fakes.dart';

const String kPluginName = 'ma_cross';
const String kPluginTitleFa = 'کراس دو میانگین';
const String kPluginSha = '2d070ea08a45aa11bb22cc33dd44ee55ff66778899aabbccddeeff0011223344';
const String kPluginSha2 = '77aa88bb99cc00dd11ee22ff33aa44bb55cc66dd77ee88ff99aa00bb11cc22dd';
const String kTemplateFilename = 'alpha_strategy_template.py';
const String kTemplateContent = '"""قالب سیستم معاملاتی Alpha Trader."""\nclass MyStrategy:\n    pass\n';

List<Map<String, Object?>> pluginSchema() => [
      {
        'name': 'fast',
        'type': 'int',
        'default': 10,
        'min': 2.0,
        'max': 100.0,
        'choices': null,
        'step': 1.0,
        'label_fa': 'میانگین سریع',
        'description_fa': '',
      },
      {
        'name': 'slow',
        'type': 'int',
        'default': 30,
        'min': 5.0,
        'max': 400.0,
        'choices': null,
        'step': 1.0,
        'label_fa': 'میانگین کند',
        'description_fa': '',
      },
    ];

Map<String, Object?> validationJson() => {
      'static': ['size_encoding', 'syntax', 'order_api_names', 'imports_names_attributes', 'strategy_class'],
      'dynamic': {
        'bars': 3000,
        'candidates': 11,
        'prefix_checks': 60,
        'determinism': true,
        'future_mutation': true,
        'elapsed_s': 5.512,
        'budget_s': 30.0,
        'worker_boot_s': 1.402,
        'peak_memory_mib': 281.4,
      },
    };

Map<String, Object?> pluginJson({
  int version = 2,
  String status = 'active',
  bool registered = true,
  String sha256 = kPluginSha,
  String filename = 'ma_cross.py',
}) =>
    {
      'name': kPluginName,
      'version': version,
      'sha256': sha256,
      'title_fa': kPluginTitleFa,
      'status': status,
      'param_schema': pluginSchema(),
      'validation': validationJson(),
      'filename': filename,
      'created_utc': '2026-09-28T09:15:00Z',
      'registered': registered,
    };

/// The plugin as `/strategies` lists it once registered.
Map<String, Object?> pluginStrategyJson(
        {int paramsVersion = 1, Map<String, Object?>? params, bool? createdNewVersion}) =>
    {
      'name': kPluginName,
      'title_fa': kPluginTitleFa,
      'version': 2,
      'params_version': paramsVersion,
      'params': params ?? {'fast': 10, 'slow': 30},
      'params_hash': '99887766554433221100aabbccddeeff99887766554433221100aabbccddeeff',
      'params_saved_utc': '2026-09-28T09:15:01Z',
      'param_schema': pluginSchema(),
      'params_errors_fa': <String>[],
      if (createdNewVersion != null) 'created_new_version': createdNewVersion,
    };

/// Stateful fake of `/strategies` + `/plugins`: [plugins] and [strategies]
/// change with the actions, like the engine's store and registry.
class FakePluginEngine {
  FakePluginEngine({List<Map<String, Object?>>? plugins, List<Map<String, Object?>>? strategies})
      : plugins = plugins ?? [],
        strategies = strategies ?? [strategyJson()];

  final List<Map<String, Object?>> plugins;
  final List<Map<String, Object?>> strategies;

  /// Answer of POST /plugins (null = 201 with [uploaded] registered).
  FutureOr<ResponseBody> Function(RequestOptions r)? onUpload;
  Map<String, Object?> uploaded = pluginJson();

  /// Answer of DELETE /plugins/... (null = archived).
  ResponseBody Function(RequestOptions r)? onArchive;

  ResponseBody _status(String status) {
    final Map<String, Object?> p = {...plugins.first, 'status': status, 'registered': status == 'active'};
    plugins[0] = p;
    strategies.removeWhere((s) => s['name'] == kPluginName);
    if (status == 'active') strategies.add(pluginStrategyJson());
    return jsonBody(p);
  }

  Map<String, RouteHandler> routes() => {
        'GET /strategies': (_) => jsonBody(strategies),
        'GET /plugins': (_) => jsonBody(plugins),
        'GET /plugins/template': (_) => jsonBody({'filename': kTemplateFilename, 'content': kTemplateContent}),
        'POST /plugins': (RequestOptions r) {
          if (onUpload != null) return onUpload!(r);
          plugins.insert(0, uploaded);
          strategies.add(pluginStrategyJson());
          return jsonBody(uploaded, 201);
        },
        'POST /plugins/$kPluginName/2/disable': (_) => _status('disabled'),
        'POST /plugins/$kPluginName/2/enable': (_) => _status('active'),
        'DELETE /plugins/$kPluginName/2': (RequestOptions r) => onArchive?.call(r) ?? _status('archived'),
      };
}

/// [PluginFileIo] without dialogs: answers are set by the test, every call
/// is recorded, nothing touches the disk.
class FakePluginFiles implements PluginFileIo {
  /// Save location answered by the save dialog (null = cancelled).
  String? savePath = r'C:\Users\me\Documents\alpha_strategy_template.py';

  /// File answered by the open dialog (null = cancelled).
  PickedPluginFile? picked = const PickedPluginFile(
    name: 'ma_cross.py',
    path: r'C:\Users\me\Documents\ma_cross.py',
    text: 'class MaCross: ...\n',
    byteCount: 19,
  );

  /// Thrown by the open dialog when set.
  PluginFileException? pickError;

  final List<String> suggestedNames = [];
  final Map<String, String> written = {};
  final List<String> revealed = [];
  int opened = 0;

  @override
  Future<String?> pickTemplateSaveLocation({required String suggestedName}) async {
    suggestedNames.add(suggestedName);
    return savePath;
  }

  @override
  Future<void> writeText(String path, String content) async => written[path] = content;

  @override
  Future<PickedPluginFile?> pickPluginFile() async {
    opened++;
    if (pickError != null) throw pickError!;
    return picked;
  }

  @override
  bool get canRevealInFolder => true;

  @override
  Future<void> revealInFolder(String path) async => revealed.add(path);
}
