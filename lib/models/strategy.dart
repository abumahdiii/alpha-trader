import 'package:flutter/foundation.dart';

import 'json_reader.dart';

/// Kind of a strategy parameter, from `ParamSpec.type` in
/// `engine/alpha_engine/strategy/params.py` (`"int" | "float" | "bool" |
/// "choice"`). [unknown] keeps a newer engine's type from crashing the
/// form: such a param is shown read-only and sent back unchanged.
enum ParamKind {
  integer,
  float,
  boolean,
  choice,
  unknown;

  static ParamKind parse(String raw) => switch (raw) {
        'int' => ParamKind.integer,
        'float' => ParamKind.float,
        'bool' => ParamKind.boolean,
        'choice' => ParamKind.choice,
        _ => ParamKind.unknown,
      };

  bool get isNumeric => this == ParamKind.integer || this == ParamKind.float;
}

/// One tunable parameter of a strategy: mirrors the engine's `ParamSpec`
/// (`{name, type, default, min, max, choices, step, label_fa,
/// description_fa}`), as listed in `StrategyOut.param_schema`.
///
/// Values ([defaultValue], [choices]) are JSON scalars: `bool`, `int`,
/// `double` or `String`. [step] is a UI increment hint only (the engine
/// does not enforce it); [min]/[max] are inclusive and enforced by the
/// engine.
@immutable
class ParamSpec {
  const ParamSpec({
    required this.name,
    required this.kind,
    required this.type,
    required this.defaultValue,
    this.min,
    this.max,
    this.choices,
    this.step,
    this.labelFa = '',
    this.descriptionFa = '',
  });

  final String name;
  final ParamKind kind;

  /// The raw `type` string (kept for [ParamKind.unknown] diagnostics).
  final String type;
  final Object defaultValue;
  final double? min;
  final double? max;
  final List<Object>? choices;
  final double? step;
  final String labelFa;
  final String descriptionFa;

  /// Label to show: the engine's Persian label, else the key itself.
  String get displayLabel => labelFa.trim().isNotEmpty ? labelFa : name;

  factory ParamSpec.fromJson(JsonReader r) {
    final String type = r.str('type');
    final Object? def = r.raw('default');
    if (def is! bool && def is! num && def is! String) {
      throw FormatException('${r.what}.default must be a scalar, got ${def.runtimeType}');
    }
    final List<Object?>? rawChoices = r.raw('choices') == null ? null : r.list('choices');
    return ParamSpec(
      name: r.str('name'),
      kind: ParamKind.parse(type),
      type: type,
      defaultValue: def as Object,
      min: r.numberOrNull('min'),
      max: r.numberOrNull('max'),
      choices: rawChoices == null
          ? null
          : List<Object>.unmodifiable(rawChoices.map((Object? c) {
              if (c is! bool && c is! num && c is! String) {
                throw FormatException('${r.what}.choices has a non-scalar item: $c');
              }
              return c as Object;
            })),
      step: r.numberOrNull('step'),
      labelFa: r.strOrNull('label_fa') ?? '',
      descriptionFa: r.strOrNull('description_fa') ?? '',
    );
  }
}

/// One registered strategy with its active parameter set: mirrors the
/// engine's `StrategyOut` (`engine/alpha_engine/routes/strategies.py`),
/// served by `GET /strategies` and `GET /strategies/{name}`.
@immutable
class StrategyInfo {
  const StrategyInfo({
    required this.name,
    required this.titleFa,
    required this.version,
    required this.paramsVersion,
    required this.params,
    required this.paramsHash,
    required this.paramsSavedUtc,
    required this.paramSchema,
    this.paramsErrorsFa = const [],
  });

  /// Registry key, used in `/strategies/{name}`.
  final String name;
  final String titleFa;

  /// Code version of the strategy (changes only with a new engine build).
  final int version;

  /// Active parameter-set version (n+1 after each save of different params).
  final int paramsVersion;

  /// Active parameter values, keyed by [ParamSpec.name].
  final Map<String, Object> params;

  /// sha256 of the canonical validated params (provenance).
  final String paramsHash;

  /// When the active params version was created, UTC.
  final DateTime? paramsSavedUtc;
  final List<ParamSpec> paramSchema;

  /// Non-empty only when the stored params no longer fit the current
  /// schema; saving a new set fixes it.
  final List<String> paramsErrorsFa;

  factory StrategyInfo.fromJson(Object? json) => StrategyInfo.fromReader(JsonReader(json, 'strategy'));

  factory StrategyInfo.fromReader(JsonReader r) => StrategyInfo(
        name: r.str('name'),
        titleFa: r.str('title_fa'),
        version: r.integer('version'),
        paramsVersion: r.integer('params_version'),
        params: r.scalarMap('params'),
        paramsHash: r.str('params_hash'),
        paramsSavedUtc: r.utcOrNull('params_saved_utc'),
        paramSchema: r.objects('param_schema', ParamSpec.fromJson),
        paramsErrorsFa: r.strings('params_errors_fa'),
      );

  /// The active value of [spec], or its default when the engine left it out.
  Object valueOf(ParamSpec spec) => params[spec.name] ?? spec.defaultValue;

  /// Every schema param with its active value (defaults filled in), in
  /// schema order -- the starting point of an editable draft.
  Map<String, Object> get completeParams => {
        for (final ParamSpec spec in paramSchema) spec.name: valueOf(spec),
      };
}

/// `PUT /strategies/{name}` response: `StrategyUpdateOut` = [StrategyInfo]
/// plus whether a new params version was created (false when the saved set
/// equals the active one).
@immutable
class StrategyUpdateResult {
  const StrategyUpdateResult({required this.strategy, required this.createdNewVersion});

  final StrategyInfo strategy;
  final bool createdNewVersion;

  factory StrategyUpdateResult.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'strategy_update');
    return StrategyUpdateResult(
      strategy: StrategyInfo.fromReader(r),
      createdNewVersion: r.boolean('created_new_version'),
    );
  }
}

/// The built-in StdDev channel system: the default strategy of every page
/// and the only one with channel lines (`/chart/channel` answers 409
/// `channel_not_available` for any other).
const String kDefaultStrategyName = 'stddev_channel';

/// Whether [name] (null = the default) draws channel lines.
bool strategyHasChannel(String? name) => (name ?? kDefaultStrategyName) == kDefaultStrategyName;

/// Where a strategy's code comes from (`strategy_source`): shipped with the
/// engine, or an uploaded, sandboxed Python file.
enum StrategySource {
  builtin('builtin', 'داخلی'),
  plugin('plugin', 'پلاگین');

  const StrategySource(this.code, this.titleFa);

  final String code;
  final String titleFa;

  /// Unknown / missing (runs stored before the field existed) = builtin.
  static StrategySource parse(String? code) => code == 'plugin' ? plugin : builtin;
}

/// The identity of a registered plugin version (what [StrategyOption.merge] needs).
typedef PluginRef = ({String name, int version, String sha256});

/// First 12 hex characters of a SHA-256 (the full value goes in a tooltip).
String shortSha(String sha) => sha.length > 12 ? sha.substring(0, 12) : sha;

/// One entry of a strategy selector (chart toolbar, backtest form): a
/// registered strategy from `GET /strategies`, marked as a plugin when
/// `GET /plugins` lists a registered version of it.
@immutable
class StrategyOption {
  const StrategyOption({
    required this.name,
    this.titleFa,
    this.version,
    this.source = StrategySource.builtin,
    this.sha256,
  });

  /// Used when `/strategies` could not be read: only the default system.
  static const StrategyOption fallback = StrategyOption(name: kDefaultStrategyName);

  final String name;
  final String? titleFa;

  /// Code version (sent as `strategy_version`, so the engine refuses a run
  /// if the registered code changed meanwhile); null when unknown.
  final int? version;
  final StrategySource source;

  /// Plugin file SHA-256 (plugins only).
  final String? sha256;

  bool get isPlugin => source == StrategySource.plugin;
  bool get hasChannel => strategyHasChannel(name);
  String get displayTitle => (titleFa ?? '').trim().isEmpty ? name : titleFa!;

  /// Builtin strategies first (engine order), then plugins by name. A
  /// strategy is a plugin when a REGISTERED plugin version carries its name.
  static List<StrategyOption> merge(List<StrategyInfo> strategies, Iterable<PluginRef> registeredPlugins) {
    final Map<String, PluginRef> plugins = {for (final PluginRef p in registeredPlugins) p.name: p};
    final List<StrategyOption> builtin = [];
    final List<StrategyOption> uploaded = [];
    for (final StrategyInfo s in strategies) {
      final PluginRef? p = plugins[s.name];
      (p == null ? builtin : uploaded).add(StrategyOption(
        name: s.name,
        titleFa: s.titleFa,
        version: s.version,
        source: p == null ? StrategySource.builtin : StrategySource.plugin,
        sha256: p?.sha256,
      ));
    }
    uploaded.sort((StrategyOption a, StrategyOption b) => a.name.compareTo(b.name));
    return List<StrategyOption>.unmodifiable([...builtin, ...uploaded]);
  }

  @override
  bool operator ==(Object other) =>
      other is StrategyOption &&
      other.name == name &&
      other.titleFa == titleFa &&
      other.version == version &&
      other.source == source &&
      other.sha256 == sha256;

  @override
  int get hashCode => Object.hash(name, titleFa, version, source, sha256);

  @override
  String toString() => 'StrategyOption($name v${version ?? '?'} ${source.code})';
}
