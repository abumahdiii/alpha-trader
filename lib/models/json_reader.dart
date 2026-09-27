import 'engine_health.dart';

/// Strict reader for one JSON object of an engine response.
///
/// The engine's pydantic response models use `extra="forbid"` and always
/// send every field, so a missing or mistyped *required* field means the
/// engine and this UI disagree about the contract. That is reported as a
/// [FormatException] (turned into a "malformed body" error by `EngineApi`)
/// instead of being papered over with a silent default.
///
/// Numbers are read tolerantly across int/double (JSON `2` vs `2.0`), never
/// across bool/number or string/number.
class JsonReader {
  JsonReader(Object? json, this.what)
      : _map = json is Map ? json : throw FormatException('$what is not a JSON object: ${json.runtimeType}');

  /// Human-readable location for error messages, e.g. `strategy.param_schema[2]`.
  final String what;
  final Map<dynamic, dynamic> _map;

  bool has(String key) => _map.containsKey(key);

  Object? raw(String key) => _map[key];

  String str(String key) => _required(key, strOrNull(key), 'a string');

  String? strOrNull(String key) {
    final Object? v = _map[key];
    if (v == null) return null;
    if (v is String) return v;
    throw _wrongType(key, 'a string', v);
  }

  int integer(String key) => _required(key, intOrNull(key), 'an integer');

  int? intOrNull(String key) {
    final Object? v = _map[key];
    if (v == null) return null;
    if (v is int) return v;
    if (v is double && v == v.truncateToDouble() && v.isFinite) return v.toInt();
    throw _wrongType(key, 'an integer', v);
  }

  double number(String key) => _required(key, numberOrNull(key), 'a number');

  double? numberOrNull(String key) {
    final Object? v = _map[key];
    if (v == null) return null;
    if (v is num) return v.toDouble();
    throw _wrongType(key, 'a number', v);
  }

  bool boolean(String key) => _required(key, boolOrNull(key), 'a boolean');

  bool? boolOrNull(String key) {
    final Object? v = _map[key];
    if (v == null) return null;
    if (v is bool) return v;
    throw _wrongType(key, 'a boolean', v);
  }

  JsonReader object(String key) {
    final Object? v = _map[key];
    if (v is! Map) throw _wrongType(key, 'an object', v);
    return JsonReader(v, '$what.$key');
  }

  JsonReader? objectOrNull(String key) => _map[key] == null ? null : object(key);

  List<Object?> list(String key) {
    final Object? v = _map[key];
    if (v is! List) throw _wrongType(key, 'a list', v);
    return List<Object?>.unmodifiable(v);
  }

  /// `list(key)` with every element read as an object.
  List<T> objects<T>(String key, T Function(JsonReader item) read) {
    final List<Object?> items = list(key);
    return List<T>.unmodifiable([
      for (int i = 0; i < items.length; i++) read(JsonReader(items[i], '$what.$key[$i]')),
    ]);
  }

  List<String> strings(String key) {
    final Object? v = _map[key];
    if (v == null) return const [];
    if (v is! List) throw _wrongType(key, 'a list of strings', v);
    return List<String>.unmodifiable(v.map((Object? e) => e.toString()));
  }

  /// A `{"key": <scalar>}` object kept as-is (bool / int / double / String).
  Map<String, Object> scalarMap(String key) {
    final Object? v = _map[key];
    if (v is! Map) throw _wrongType(key, 'an object', v);
    final Map<String, Object> out = {};
    v.forEach((Object? k, Object? value) {
      if (value is! bool && value is! num && value is! String) {
        throw _wrongType('$key.$k', 'a scalar', value);
      }
      out[k.toString()] = value as Object;
    });
    return Map<String, Object>.unmodifiable(out);
  }

  /// ISO-8601 timestamp read as UTC (engine contract: UTC with `Z`).
  DateTime? utcOrNull(String key) {
    final String? s = strOrNull(key);
    if (s == null) return null;
    final DateTime? t = EngineHealth.parseUtc(s);
    if (t == null) throw _wrongType(key, 'an ISO-8601 UTC timestamp', s);
    return t;
  }

  T _required<T>(String key, T? value, String expected) {
    if (value == null) {
      throw FormatException('$what.$key is missing (expected $expected)');
    }
    return value;
  }

  FormatException _wrongType(String key, String expected, Object? got) =>
      FormatException('$what.$key must be $expected, got ${got.runtimeType}: $got');
}
