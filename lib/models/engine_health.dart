import 'package:flutter/foundation.dart';

/// Connection state of the MetaTrader 5 terminal as reported by the engine's
/// `GET /health` (`mt5.state`). [unknown] covers any value this UI version
/// does not recognise (a newer engine, a typo, a missing field) so a contract
/// drift degrades to "unknown" instead of crashing the status bar.
enum Mt5State {
  notInitialized,
  connected,
  disconnected,
  error,
  accountMismatch,
  unknown;

  static Mt5State parse(Object? raw) => switch (raw) {
        'not_initialized' => Mt5State.notInitialized,
        'connected' => Mt5State.connected,
        'disconnected' => Mt5State.disconnected,
        'error' => Mt5State.error,
        'account_mismatch' => Mt5State.accountMismatch,
        _ => Mt5State.unknown,
      };
}

/// The `mt5` block of `GET /health`. Every field except [state] is optional:
/// the engine sends nulls until it has attached to a terminal.
@immutable
class Mt5Status {
  const Mt5Status({
    required this.state,
    this.server,
    this.loginMasked,
    this.tradeMode,
    this.message,
  });

  static const Mt5Status unknown = Mt5Status(state: Mt5State.unknown);

  final Mt5State state;
  final String? server;

  /// Already masked by the engine (e.g. `****1234`). The UI never receives
  /// or shows the full login.
  final String? loginMasked;

  /// Free-form account trade mode as reported by the engine (e.g. `demo`).
  final String? tradeMode;
  final String? message;

  /// Tolerant: a missing or non-object `mt5` block yields [unknown].
  factory Mt5Status.fromJson(Object? json) {
    if (json is! Map) return Mt5Status.unknown;
    return Mt5Status(
      state: Mt5State.parse(json['state']),
      server: _asString(json['server']),
      loginMasked: _asString(json['login_masked']),
      tradeMode: _asString(json['trade_mode']),
      message: _asString(json['message']),
    );
  }
}

/// Parsed `GET /health` response of the local Python engine.
@immutable
class EngineHealth {
  const EngineHealth({
    required this.status,
    required this.service,
    required this.mt5,
    this.version,
    this.pid,
    this.devMode,
    this.timeUtc,
  });

  /// Service name the engine announces; used to tell our own engine apart
  /// from an unrelated program that happens to own the port.
  static const String alphaEngineService = 'alpha_engine';

  final String? status;
  final String? service;
  final String? version;

  /// PID of the interpreter that serves HTTP. Note: with a venv launcher
  /// this is the real python.exe, not the launcher process we spawned.
  final int? pid;
  final bool? devMode;

  /// Engine clock, always UTC. Convert with `toLocal()` only for display.
  final DateTime? timeUtc;
  final Mt5Status mt5;

  bool get isAlphaEngine => service == alphaEngineService;

  /// Throws [FormatException] only when the body is not a JSON object at all;
  /// individual fields are tolerant (wrong type or null -> null / unknown).
  factory EngineHealth.fromJson(Object? json) {
    if (json is! Map) {
      throw FormatException('health body is not a JSON object: ${json.runtimeType}');
    }
    return EngineHealth(
      status: _asString(json['status']),
      service: _asString(json['service']),
      version: _asString(json['version']),
      pid: _asInt(json['pid']),
      devMode: json['dev_mode'] is bool ? json['dev_mode'] as bool : null,
      timeUtc: parseUtc(json['time_utc']),
      mt5: Mt5Status.fromJson(json['mt5']),
    );
  }

  /// A zone designator is only meaningful right after a time of day; anchoring
  /// on the time keeps a date-only value (`2026-09-26`) from reading its
  /// trailing `-26` as an offset.
  static final RegExp _zoneAfterTime = RegExp(
    r'\d{2}:\d{2}(:\d{2}([.,]\d+)?)?\s*(Z|[+-]\d{2}(:?\d{2})?)$',
    caseSensitive: false,
  );
  static final RegExp _hasTime = RegExp('[T ]', caseSensitive: false);

  /// Parses an ISO-8601 timestamp as UTC. A value without a zone designator
  /// is taken as UTC (the engine contract), never as local time.
  static DateTime? parseUtc(Object? raw) {
    if (raw is! String || raw.trim().isEmpty) return null;
    final String s = raw.trim();
    final String normalized = _zoneAfterTime.hasMatch(s)
        ? s
        : (_hasTime.hasMatch(s) ? '${s}Z' : '${s}T00:00:00Z');
    return DateTime.tryParse(normalized)?.toUtc();
  }
}

String? _asString(Object? v) => v?.toString();

int? _asInt(Object? v) => switch (v) {
      int() => v,
      num() => v.toInt(),
      String() => int.tryParse(v),
      _ => null,
    };
