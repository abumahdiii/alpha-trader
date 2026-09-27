// Small strict readers for engine JSON. Every chart model parses through
// these so a contract drift (missing key, wrong type) surfaces as one
// FormatException naming the key instead of a TypeError deep in a widget.

/// The decoded JSON object, or a [FormatException] naming [what].
Map<String, Object?> jsonObject(Object? json, String what) {
  if (json is Map<String, Object?>) return json;
  if (json is Map) return json.cast<String, Object?>();
  throw FormatException('$what: expected a JSON object, got ${json.runtimeType}');
}

Object? _need(Map<String, Object?> m, String key) {
  if (!m.containsKey(key)) throw FormatException('missing key "$key"');
  return m[key];
}

String reqString(Map<String, Object?> m, String key) {
  final Object? v = _need(m, key);
  if (v is String) return v;
  throw FormatException('"$key": expected a string, got ${v.runtimeType}');
}

String? optString(Map<String, Object?> m, String key) {
  final Object? v = m[key];
  if (v == null || v is String) return v as String?;
  throw FormatException('"$key": expected a string or null, got ${v.runtimeType}');
}

/// A JSON number as a double (Python may serialize `2064.0` or `2064`).
double reqDouble(Map<String, Object?> m, String key) {
  final Object? v = _need(m, key);
  if (v is num) return v.toDouble();
  throw FormatException('"$key": expected a number, got ${v.runtimeType}');
}

double? optDouble(Map<String, Object?> m, String key) {
  final Object? v = m[key];
  if (v == null) return null;
  if (v is num) return v.toDouble();
  throw FormatException('"$key": expected a number or null, got ${v.runtimeType}');
}

int reqInt(Map<String, Object?> m, String key) {
  final Object? v = _need(m, key);
  if (v is int) return v;
  if (v is double && v == v.roundToDouble()) return v.toInt();
  throw FormatException('"$key": expected an integer, got $v');
}

int? optInt(Map<String, Object?> m, String key) {
  if (m[key] == null) return null;
  return reqInt(m, key);
}

bool reqBool(Map<String, Object?> m, String key) {
  final Object? v = _need(m, key);
  if (v is bool) return v;
  throw FormatException('"$key": expected a boolean, got ${v.runtimeType}');
}

bool? optBool(Map<String, Object?> m, String key) {
  final Object? v = m[key];
  if (v == null || v is bool) return v as bool?;
  throw FormatException('"$key": expected a boolean or null, got ${v.runtimeType}');
}

/// Engine times are ISO-8601 UTC with a `Z` suffix; the result is always UTC.
DateTime parseUtc(String text) {
  final DateTime? t = DateTime.tryParse(text);
  if (t == null) throw FormatException('not an ISO-8601 time: "$text"');
  return t.toUtc();
}

DateTime reqUtc(Map<String, Object?> m, String key) => parseUtc(reqString(m, key));

DateTime? optUtc(Map<String, Object?> m, String key) {
  final String? s = optString(m, key);
  return s == null ? null : parseUtc(s);
}

List<T> reqList<T>(Map<String, Object?> m, String key, T Function(Object? item) parse) {
  final Object? v = _need(m, key);
  if (v is! List) throw FormatException('"$key": expected a list, got ${v.runtimeType}');
  return List<T>.unmodifiable(v.map(parse));
}

List<String> reqStringList(Map<String, Object?> m, String key) => reqList<String>(m, key, (Object? e) {
      if (e is String) return e;
      throw FormatException('"$key": expected strings, got ${e.runtimeType}');
    });

/// `dict[str, int]` (e.g. gap_counts, status_counts).
Map<String, int> reqIntMap(Map<String, Object?> m, String key) {
  final Map<String, Object?> obj = jsonObject(_need(m, key), key);
  return Map<String, int>.unmodifiable(obj.map((String k, Object? v) {
    if (v is int) return MapEntry<String, int>(k, v);
    throw FormatException('"$key.$k": expected an integer, got ${v.runtimeType}');
  }));
}

/// A free-form JSON object kept as-is (params, indicators).
Map<String, Object?> reqRawMap(Map<String, Object?> m, String key) =>
    Map<String, Object?>.unmodifiable(jsonObject(_need(m, key), key));
