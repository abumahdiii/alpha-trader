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
