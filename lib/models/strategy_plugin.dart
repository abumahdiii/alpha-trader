import 'package:flutter/foundation.dart';

import 'json_reader.dart';
import 'strategy.dart';

// Uploaded strategy plugins: mirrors `PluginOut` / `TemplateOut` of
// engine/alpha_engine/routes/plugins.py. The engine validates, stores and
// runs the file (sandboxed worker); this UI only shows its answers.

/// `active` | `disabled` | `archived` (`strategy_plugins.status`).
enum PluginStatus {
  active('active', 'فعال'),
  disabled('disabled', 'غیرفعال'),
  archived('archived', 'بایگانی'),

  /// A newer engine's status: shown as-is, no action offered.
  unknown('unknown', 'نامشخص');

  const PluginStatus(this.code, this.titleFa);

  final String code;
  final String titleFa;

  static PluginStatus parse(String code) =>
      PluginStatus.values.firstWhere((PluginStatus s) => s.code == code, orElse: () => PluginStatus.unknown);
}

/// Persian names of the engine's static check groups
/// (`plugins/validator.py`, `report.checks`). Unknown groups show their key.
const Map<String, String> pluginStaticCheckTitlesFa = {
  'size_encoding': 'حجم فایل و کدگذاری UTF-8',
  'syntax': 'نحو پایتون (بدون خطای نگارشی)',
  'order_api_names': 'نبود هیچ نام API سفارش یا متاتریدر',
  'imports_names_attributes': 'فقط importها، نام‌ها و ویژگی‌های مجاز',
  'strategy_class': 'دقیقا یک کلاس Strategy با name، version، title_fa، param_schema و evaluate',
};

/// `validation.dynamic`: the sandboxed run on the engine's synthetic fixture.
/// Every field may be null (an older record, or checks that did not run).
@immutable
class PluginDynamicReport {
  const PluginDynamicReport({
    this.bars,
    this.candidates,
    this.prefixChecks,
    this.determinism,
    this.futureMutation,
    this.elapsedS,
    this.budgetS,
    this.workerBootS,
    this.peakMemoryMib,
  });

  /// Fixture H1 bars scanned.
  final int? bars;

  /// Candidates the plugin produced on them.
  final int? candidates;

  /// Bars where `evaluate` on the closed prefix was compared with the scan.
  final int? prefixChecks;

  /// Same result on repeated scans (same worker + a fresh one).
  final bool? determinism;

  /// Changing / cutting future bars left earlier candidates unchanged.
  final bool? futureMutation;
  final double? elapsedS;
  final double? budgetS;
  final double? workerBootS;
  final double? peakMemoryMib;

  static PluginDynamicReport? read(JsonReader? r) => r == null
      ? null
      : PluginDynamicReport(
          bars: r.intOrNull('bars'),
          candidates: r.intOrNull('candidates'),
          prefixChecks: r.intOrNull('prefix_checks'),
          determinism: r.boolOrNull('determinism'),
          futureMutation: r.boolOrNull('future_mutation'),
          elapsedS: r.numberOrNull('elapsed_s'),
          budgetS: r.numberOrNull('budget_s'),
          workerBootS: r.numberOrNull('worker_boot_s'),
          peakMemoryMib: r.numberOrNull('peak_memory_mib'),
        );
}

/// `validation = {static: [passed check groups], dynamic: {...} | null}`.
@immutable
class PluginValidation {
  const PluginValidation({this.staticChecks = const [], this.dynamicReport});

  final List<String> staticChecks;
  final PluginDynamicReport? dynamicReport;

  static PluginValidation read(JsonReader? r) {
    if (r == null) return const PluginValidation();
    final Object? checks = r.raw('static');
    return PluginValidation(
      staticChecks: checks is List ? List<String>.unmodifiable(checks.map((Object? c) => '$c')) : const [],
      dynamicReport: PluginDynamicReport.read(r.raw('dynamic') is Map ? r.object('dynamic') : null),
    );
  }
}

/// One stored plugin version (`PluginOut`).
@immutable
class StrategyPlugin {
  const StrategyPlugin({
    required this.name,
    required this.version,
    required this.sha256,
    required this.titleFa,
    required this.status,
    this.paramSchema = const [],
    this.validation = const PluginValidation(),
    this.filename,
    this.createdUtc,
    required this.registered,
  });

  /// Strategy name (the `name` class attribute; the key of `/strategies`).
  final String name;
  final int version;

  /// SHA-256 of the file text (provenance of every result made with it).
  final String sha256;
  final String titleFa;
  final PluginStatus status;
  final List<ParamSpec> paramSchema;
  final PluginValidation validation;

  /// The uploaded file name (null for records without one).
  final String? filename;
  final DateTime? createdUtc;

  /// This version is the one the engine runs for [name] right now.
  final bool registered;

  String get shortSha256 => shortSha(sha256);

  PluginRef get ref => (name: name, version: version, sha256: sha256);

  /// Identity for per-row state (busy spinners) and widget keys.
  String get key => '$name@$version';

  factory StrategyPlugin.fromJson(Object? json) => StrategyPlugin.read(JsonReader(json, 'plugin'));

  factory StrategyPlugin.read(JsonReader r) => StrategyPlugin(
        name: r.str('name'),
        version: r.integer('version'),
        sha256: r.str('sha256'),
        titleFa: r.strOrNull('title_fa') ?? r.str('name'),
        status: PluginStatus.parse(r.str('status')),
        paramSchema: r.raw('param_schema') is List ? r.objects('param_schema', ParamSpec.fromJson) : const [],
        validation: PluginValidation.read(r.raw('validation') is Map ? r.object('validation') : null),
        filename: r.strOrNull('filename'),
        createdUtc: r.utcOrNull('created_utc'),
        registered: r.boolOrNull('registered') ?? false,
      );

  static List<StrategyPlugin> listFromJson(Object? json) {
    if (json is! List) throw FormatException('plugins is not a JSON list: ${json.runtimeType}');
    return List<StrategyPlugin>.unmodifiable([
      for (int i = 0; i < json.length; i++) StrategyPlugin.read(JsonReader(json[i], 'plugins[$i]')),
    ]);
  }

  @override
  String toString() => 'StrategyPlugin($name v$version ${status.code} sha=$shortSha256 registered=$registered)';
}

/// `POST /plugins` answer: the stored version, and whether it is new (201)
/// or the same file uploaded again (200; an archived copy is restored).
@immutable
class PluginUploadResult {
  const PluginUploadResult({required this.plugin, required this.created});

  final StrategyPlugin plugin;
  final bool created;
}

/// `GET /plugins/template` -> `{filename, content}`.
@immutable
class PluginTemplate {
  const PluginTemplate({required this.filename, required this.content});

  final String filename;
  final String content;

  factory PluginTemplate.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'plugin_template');
    return PluginTemplate(filename: r.str('filename'), content: r.str('content'));
  }
}
