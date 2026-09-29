// Live signals (phase 6): typed models of the engine's `/signals` routes and
// the `/ws/signals` messages. SUGGESTIONS ONLY: nothing here (or anywhere in
// the UI) can send an order.
//
// Parsing is deliberately TOLERANT (unlike JsonReader): the live dashboard
// must keep running when the engine adds, drops or nulls a field. A missing
// or mistyped field becomes null / false / empty and is never recomputed
// here; only a signal without an integer `id` is refused (it could not be
// deduplicated or opened).

import 'package:flutter/foundation.dart';

import '../core/dev_mode.dart';
import 'chart_models.dart';
import 'engine_health.dart';
import 'strategy.dart';

// ------------------------------------------------------------------ tolerant readers

Map<String, Object?>? _map(Object? v) =>
    v is Map ? <String, Object?>{for (final MapEntry<Object?, Object?> e in v.entries) '${e.key}': e.value} : null;

String? _str(Object? v) => v is String ? v : null;

/// A non-blank string, else null.
String? _text(Object? v) => v is String && v.trim().isNotEmpty ? v : null;

double? _num(Object? v) => v is num && v.isFinite ? v.toDouble() : null;

int? _int(Object? v) => switch (v) {
      int() => v,
      double() when v.isFinite && v == v.truncateToDouble() => v.toInt(),
      _ => null,
    };

bool _bool(Object? v) => v == true;

DateTime? _time(Object? v) => EngineHealth.parseUtc(v);

List<String> _strings(Object? v) => v is List ? List<String>.unmodifiable(v.map((Object? e) => '$e')) : const [];

// ------------------------------------------------------------------ enums

/// `status` of a stored signal.
enum LiveSignalStatus {
  active('active', 'فعال'),
  expired('expired', 'منقضی'),
  superseded('superseded', 'جایگزین‌شده'),
  rejected('rejected', 'ردشده'),
  unknown('unknown', 'نامشخص');

  const LiveSignalStatus(this.code, this.labelFa);

  final String code;
  final String labelFa;

  static LiveSignalStatus parse(Object? raw) =>
      values.firstWhere((LiveSignalStatus s) => s.code == raw && s != unknown, orElse: () => unknown);

  /// The statuses `GET /signals?status=` accepts (the history filter).
  static const List<LiveSignalStatus> filterable = [active, expired, superseded, rejected];
}

/// Where the indicative entry came from (`entry_source`).
enum SignalEntrySource {
  /// The current tick (buy = ask, sell = bid).
  tick('tick'),

  /// No fresh tick at the decision time: last cached close (+ spread for a buy).
  lastClose('last_close'),
  unknown('unknown');

  const SignalEntrySource(this.code);

  final String code;

  static SignalEntrySource parse(Object? raw) => switch (raw) {
        'tick' => tick,
        'last_close' => lastClose,
        _ => unknown,
      };

  /// Persian label; the last-close rule differs per side (engine README:
  /// last close + spread for a buy, last close for a sell).
  String labelFa(TradeSide? side) => switch (this) {
        tick => 'قیمت لحظه‌ای',
        lastClose => side == TradeSide.sell ? 'آخرین بسته، بازار بسته' : 'آخرین بسته + اسپرد، بازار بسته',
        unknown => 'منبع نامشخص',
      };
}

/// Scheduler `state` of `GET /signals/status`.
enum LiveSignalsState {
  disabled('disabled', 'غیرفعال'),
  starting('starting', 'در حال آماده‌سازی'),
  running('running', 'در حال اجرا'),
  mt5Down('mt5_down', 'قطع اتصال متاتریدر'),
  stopped('stopped', 'متوقف'),
  unknown('unknown', 'نامشخص');

  const LiveSignalsState(this.code, this.labelFa);

  final String code;
  final String labelFa;

  static LiveSignalsState parse(Object? raw) =>
      values.firstWhere((LiveSignalsState s) => s.code == raw && s != unknown, orElse: () => unknown);
}

/// Persian label of a per-symbol check `result` (engine README list);
/// unknown values are shown as they are.
String checkResultFa(String? result) => switch (result) {
      'signal' => 'سیگنال صادر شد',
      'rejected' => 'ستاپ رد شد',
      'no_setup' => 'ستاپی نبود',
      'mismatch' => 'ناهمخوانی (بدون سیگنال)',
      'duplicate' => 'تکراری',
      'market_closed' => 'بازار بسته',
      'incomplete' => 'داده ناقص',
      'update_failed' => 'به‌روزرسانی داده ناموفق',
      'error' => 'خطا',
      null => '—',
      _ => result,
    };

// ------------------------------------------------------------------ signal item

/// `sizing` of a signal: the engine's position sizing at the indicative entry.
@immutable
class SignalSizing {
  const SignalSizing({
    this.accepted = false,
    this.volume,
    this.rawVolume,
    this.riskAmount,
    this.actualRisk,
    this.margin,
    this.reasonFa,
    this.warnings = const [],
  });

  final bool accepted;
  final double? volume;
  final double? rawVolume;
  final double? riskAmount;
  final double? actualRisk;
  final double? margin;
  final String? reasonFa;
  final List<String> warnings;

  static SignalSizing? fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) return null;
    return SignalSizing(
      accepted: _bool(m['accepted']),
      volume: _num(m['volume']),
      rawVolume: _num(m['raw_volume']),
      riskAmount: _num(m['risk_amount']),
      actualRisk: _num(m['actual_risk']),
      margin: _num(m['margin']),
      reasonFa: _text(m['reason_fa']),
      warnings: _strings(m['warnings']),
    );
  }
}

/// `account` of a signal: the `/settings` values used for sizing.
@immutable
class SignalAccount {
  const SignalAccount({this.balance, this.riskPct, this.leverage, this.rr});

  final double? balance;
  final double? riskPct;
  final double? leverage;
  final double? rr;

  static SignalAccount? fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) return null;
    return SignalAccount(
      balance: _num(m['balance']),
      riskPct: _num(m['risk_pct']),
      leverage: _num(m['leverage']),
      rr: _num(m['rr']),
    );
  }
}

/// `entry_gap`: how the step confirmation bar -> entry bar was classified.
@immutable
class SignalEntryGap {
  const SignalEntryGap({this.kind, this.provisional = false, this.entryBarTime, this.noteFa});

  final String? kind;
  final bool provisional;
  final DateTime? entryBarTime;
  final String? noteFa;

  static SignalEntryGap? fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) return null;
    return SignalEntryGap(
      kind: _text(m['kind']),
      provisional: _bool(m['provisional']),
      entryBarTime: _time(m['entry_bar_time']),
      noteFa: _text(m['note_fa']),
    );
  }
}

/// One stored live signal (`GET /signals`, `/signals/{id}`, WS `signal` /
/// `expired` / `superseded`). Every price is the engine's, unrounded; the UI
/// formats them with the symbol's digits and never derives a new one.
@immutable
class LiveSignal {
  const LiveSignal({
    required this.id,
    required this.symbol,
    this.status = LiveSignalStatus.unknown,
    this.strategy,
    this.strategyVersion,
    this.strategySource = StrategySource.builtin,
    this.strategySha256,
    this.paramsVersion,
    this.paramsHash,
    this.confirmationBarTime,
    this.decisionTime,
    this.entryBarTime,
    this.expiresTime,
    this.expiryPending = false,
    this.direction,
    this.setupType,
    this.setupTitleFa,
    this.pattern,
    this.line,
    this.referencePrice,
    this.indicativeEntry,
    this.entrySource = SignalEntrySource.unknown,
    this.stopLoss,
    this.takeProfitIndicative,
    this.takeProfitNoteFa,
    this.rr,
    this.volume,
    this.riskAmount,
    this.sizing,
    this.account,
    this.reasonFa,
    this.rejectionReasonFa,
    this.backtestWouldSkip = false,
    this.backtestSkipReasonFa,
    this.gapClassProvisional = false,
    this.entryGap,
    this.indicators = const {},
    this.mismatchFields = const [],
    this.hasMismatch = false,
    this.supersededReasonFa,
    this.supersededFields = const [],
    this.noteFa,
    this.createdTime,
    this.updatedTime,
  });

  final int id;
  final String symbol;
  final LiveSignalStatus status;
  final String? strategy;
  final int? strategyVersion;
  final StrategySource strategySource;
  final String? strategySha256;
  final int? paramsVersion;
  final String? paramsHash;
  final DateTime? confirmationBarTime;
  final DateTime? decisionTime;
  final DateTime? entryBarTime;

  /// Close of the entry bar; null while the market is closed after the bar
  /// ([expiryPending]).
  final DateTime? expiresTime;
  final bool expiryPending;
  final TradeSide? direction;
  final String? setupType;
  final String? setupTitleFa;
  final String? pattern;
  final String? line;
  final double? referencePrice;
  final double? indicativeEntry;
  final SignalEntrySource entrySource;
  final double? stopLoss;

  /// Indicative only: the real TP is resolved at the actual fill.
  final double? takeProfitIndicative;
  final String? takeProfitNoteFa;
  final double? rr;
  final double? volume;

  /// Actual risk at the indicative entry.
  final double? riskAmount;
  final SignalSizing? sizing;
  final SignalAccount? account;
  final String? reasonFa;
  final String? rejectionReasonFa;
  final bool backtestWouldSkip;
  final String? backtestSkipReasonFa;
  final bool gapClassProvisional;
  final SignalEntryGap? entryGap;
  final Map<String, Object?> indicators;

  /// Fields where the scan and the evaluation disagreed (`mismatch.fields`).
  final List<String> mismatchFields;
  final bool hasMismatch;
  final String? supersededReasonFa;
  final List<String> supersededFields;
  final String? noteFa;
  final DateTime? createdTime;
  final DateTime? updatedTime;

  bool get isActive => status == LiveSignalStatus.active;
  bool get isPlugin => strategySource == StrategySource.plugin;

  /// True when there is no expiry to count down to yet.
  bool get expiryUnknown => expiryPending || expiresTime == null;

  /// Parses one item; throws [FormatException] only when it is not an
  /// object or has no integer `id` (everything else is optional).
  factory LiveSignal.fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) throw FormatException('signal is not a JSON object: ${json.runtimeType}');
    final int? id = _int(m['id']);
    if (id == null) throw FormatException('signal.id is missing or not an integer: ${m['id']}');
    final String? dir = _str(m['direction']);
    final Map<String, Object?>? mismatch = _map(m['mismatch']);
    final Map<String, Object?>? superseded = _map(m['superseded']);
    return LiveSignal(
      id: id,
      symbol: _str(m['symbol']) ?? '—',
      status: LiveSignalStatus.parse(m['status']),
      strategy: _text(m['strategy']),
      strategyVersion: _int(m['strategy_version']),
      strategySource: StrategySource.parse(_str(m['strategy_source'])),
      strategySha256: _text(m['strategy_sha256']),
      paramsVersion: _int(m['params_version']),
      paramsHash: _text(m['params_hash']),
      confirmationBarTime: _time(m['confirmation_bar_time']),
      decisionTime: _time(m['decision_time']),
      entryBarTime: _time(m['entry_bar_time']),
      expiresTime: _time(m['expires_time']),
      expiryPending: _bool(m['expiry_pending']),
      direction: dir == 'buy' ? TradeSide.buy : (dir == 'sell' ? TradeSide.sell : null),
      setupType: _text(m['setup_type']),
      setupTitleFa: _text(m['setup_title_fa']),
      pattern: _text(m['pattern']),
      line: _text(m['line']),
      referencePrice: _num(m['reference_price']),
      indicativeEntry: _num(m['indicative_entry']),
      entrySource: SignalEntrySource.parse(m['entry_source']),
      stopLoss: _num(m['stop_loss']),
      takeProfitIndicative: _num(m['take_profit_indicative']),
      takeProfitNoteFa: _text(m['take_profit_note_fa']),
      rr: _num(m['rr']),
      volume: _num(m['volume']),
      riskAmount: _num(m['risk_amount']),
      sizing: SignalSizing.fromJson(m['sizing']),
      account: SignalAccount.fromJson(m['account']),
      reasonFa: _text(m['reason_fa']),
      rejectionReasonFa: _text(m['rejection_reason_fa']),
      backtestWouldSkip: _bool(m['backtest_would_skip']),
      backtestSkipReasonFa: _text(m['backtest_skip_reason_fa']),
      gapClassProvisional: _bool(m['gap_class_provisional']),
      entryGap: SignalEntryGap.fromJson(m['entry_gap']),
      indicators: Map<String, Object?>.unmodifiable(_map(m['indicators']) ?? const {}),
      hasMismatch: mismatch != null,
      mismatchFields: _strings(mismatch?['fields']),
      supersededReasonFa: _text(superseded?['reason_fa']),
      supersededFields: _strings(superseded?['fields']),
      noteFa: _text(m['note_fa']),
      createdTime: _time(m['created_time']),
      updatedTime: _time(m['updated_time']),
    );
  }

  /// [LiveSignal.fromJson] that answers null (logged under DEV_MODE)
  /// instead of throwing: one bad item never breaks a list or the socket.
  static LiveSignal? tryParse(Object? json, {String where = 'signal'}) {
    try {
      return LiveSignal.fromJson(json);
    } on FormatException catch (e) {
      devLog('[Signals] $where skipped: ${e.message}');
      return null;
    }
  }

  static List<LiveSignal> listFrom(Object? json, {String where = 'signals'}) {
    if (json is! List) return const [];
    return List<LiveSignal>.unmodifiable([
      for (int i = 0; i < json.length; i++)
        if (tryParse(json[i], where: '$where[$i]') case final LiveSignal s) s,
    ]);
  }

  /// Why the signal is not (or no longer) a suggestion, if it says so.
  String? get statusReasonFa => switch (status) {
        LiveSignalStatus.rejected => rejectionReasonFa ?? (hasMismatch ? 'ناهمخوانی اسکن و ارزیابی' : null),
        LiveSignalStatus.superseded => supersededReasonFa,
        _ => null,
      };

  @override
  bool operator ==(Object other) =>
      other is LiveSignal && other.id == id && other.status == status && other.updatedTime == updatedTime;

  @override
  int get hashCode => Object.hash(id, status, updatedTime);

  @override
  String toString() => 'LiveSignal(#$id $symbol ${direction?.name ?? '?'} ${status.code}'
      ' entry=$indicativeEntry sl=$stopLoss tp=$takeProfitIndicative vol=$volume'
      ' expires=${expiresTime?.toIso8601String() ?? (expiryPending ? 'pending' : '-')})';
}

/// `GET /signals` page.
@immutable
class SignalsPage {
  const SignalsPage({
    required this.signals,
    this.count = 0,
    this.total = 0,
    this.offset = 0,
    this.limit = 50,
  });

  final List<LiveSignal> signals;
  final int count;
  final int total;
  final int offset;
  final int limit;

  bool get hasPrevious => offset > 0;
  bool get hasNext => offset + signals.length < total;

  factory SignalsPage.fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) throw FormatException('signals page is not a JSON object: ${json.runtimeType}');
    final List<LiveSignal> items = LiveSignal.listFrom(m['signals']);
    return SignalsPage(
      signals: items,
      count: _int(m['count']) ?? items.length,
      total: _int(m['total']) ?? items.length,
      offset: _int(m['offset']) ?? 0,
      limit: _int(m['limit']) ?? 50,
    );
  }
}

// ------------------------------------------------------------------ status

/// The last check of one symbol (`status.checks[symbol]`).
@immutable
class SymbolCheck {
  const SymbolCheck({this.boundaryUtc, this.result, this.reasonFa, this.signalId});

  final DateTime? boundaryUtc;
  final String? result;
  final String? reasonFa;
  final int? signalId;

  String get resultFa => checkResultFa(result);

  static SymbolCheck? fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) return null;
    return SymbolCheck(
      boundaryUtc: _time(m['boundary_utc']),
      result: _text(m['result']),
      reasonFa: _text(m['reason_fa']),
      signalId: _int(m['signal_id']),
    );
  }
}

/// `GET /signals/status` (also the WS `snapshot.status` / `status.status`).
@immutable
class LiveSignalsStatus {
  const LiveSignalsStatus({
    this.enabled = false,
    this.state = LiveSignalsState.unknown,
    this.mt5State = Mt5State.unknown,
    this.liveStrategy,
    this.graceS,
    this.serverOffset,
    this.serverTimeUtc,
    this.clockSkewS,
    this.clockSkewWarning = false,
    this.lastTickUtc = const {},
    this.lastCheckUtc,
    this.nextCheckUtc,
    this.checks = const {},
    this.lastErrorFa,
    this.lastErrorUtc,
  });

  /// Before the first status arrived.
  static const LiveSignalsStatus unknown = LiveSignalsStatus();

  final bool enabled;
  final LiveSignalsState state;
  final Mt5State mt5State;
  final String? liveStrategy;
  final double? graceS;

  /// The broker offset model label (e.g. `us_dst(2)`).
  final String? serverOffset;
  final DateTime? serverTimeUtc;
  final double? clockSkewS;
  final bool clockSkewWarning;
  final Map<String, DateTime?> lastTickUtc;
  final DateTime? lastCheckUtc;
  final DateTime? nextCheckUtc;
  final Map<String, SymbolCheck> checks;
  final String? lastErrorFa;
  final DateTime? lastErrorUtc;

  factory LiveSignalsStatus.fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) throw FormatException('signals status is not a JSON object: ${json.runtimeType}');
    final Map<String, Object?> ticks = _map(m['last_tick_utc']) ?? const {};
    final Map<String, Object?> checks = _map(m['checks']) ?? const {};
    return LiveSignalsStatus(
      enabled: _bool(m['enabled']),
      state: LiveSignalsState.parse(m['state']),
      mt5State: Mt5State.parse(m['mt5_state']),
      liveStrategy: _text(m['live_strategy']),
      graceS: _num(m['grace_s']),
      serverOffset: _text(m['server_offset']),
      serverTimeUtc: _time(m['server_time_utc']),
      clockSkewS: _num(m['clock_skew_s']),
      clockSkewWarning: _bool(m['clock_skew_warning']),
      lastTickUtc: Map<String, DateTime?>.unmodifiable({for (final e in ticks.entries) e.key: _time(e.value)}),
      lastCheckUtc: _time(m['last_check_utc']),
      nextCheckUtc: _time(m['next_check_utc']),
      checks: Map<String, SymbolCheck>.unmodifiable({
        for (final e in checks.entries)
          if (SymbolCheck.fromJson(e.value) case final SymbolCheck c) e.key: c,
      }),
      lastErrorFa: _text(m['last_error_fa']),
      lastErrorUtc: _time(m['last_error_utc']),
    );
  }

  /// Every symbol the engine reports (ticks and checks), sorted.
  List<String> get symbols => ({...lastTickUtc.keys, ...checks.keys}.toList()..sort());

  @override
  String toString() => 'LiveSignalsStatus(enabled=$enabled state=${state.code} mt5=${mt5State.name} '
      'strategy=${liveStrategy ?? '-'} grace=${graceS ?? '-'} skew=${clockSkewS ?? '-'}'
      '${clockSkewWarning ? ' SKEW-WARNING' : ''} last=${lastCheckUtc?.toIso8601String() ?? '-'} '
      'next=${nextCheckUtc?.toIso8601String() ?? '-'} checks=${checks.length}'
      '${lastErrorFa != null ? ' error="$lastErrorFa"' : ''})';
}

// ------------------------------------------------------------------ settings

/// `GET|POST /signals/settings`.
@immutable
class LiveSignalSettings {
  const LiveSignalSettings({required this.enabled, this.liveStrategy, this.graceS});

  static const double graceMin = 0;
  static const double graceMax = 600;

  final bool enabled;
  final String? liveStrategy;
  final double? graceS;

  factory LiveSignalSettings.fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) throw FormatException('signal settings is not a JSON object: ${json.runtimeType}');
    return LiveSignalSettings(
      enabled: _bool(m['enabled']),
      liveStrategy: _text(m['live_strategy']),
      graceS: _num(m['grace_s']),
    );
  }

  /// Persian validation of a grace value (null = valid), same bounds as the engine.
  static String? validateGraceFa(double? v) {
    if (v == null) return 'مقدار «مهلت پس از بسته شدن کندل» را وارد کنید.';
    if (v < graceMin || v > graceMax) {
      return '«مهلت پس از بسته شدن کندل» باید بین ${graceMin.toInt()} و ${graceMax.toInt()} ثانیه باشد.';
    }
    return null;
  }

  @override
  bool operator ==(Object other) =>
      other is LiveSignalSettings &&
      other.enabled == enabled &&
      other.liveStrategy == liveStrategy &&
      other.graceS == graceS;

  @override
  int get hashCode => Object.hash(enabled, liveStrategy, graceS);

  @override
  String toString() => 'LiveSignalSettings(enabled=$enabled strategy=${liveStrategy ?? '-'} grace=${graceS ?? '-'})';
}

/// `POST /signals/settings` answer: the new settings and (when sent) the status.
@immutable
class LiveSignalSettingsResult {
  const LiveSignalSettingsResult({required this.settings, this.status});

  final LiveSignalSettings settings;
  final LiveSignalsStatus? status;

  factory LiveSignalSettingsResult.fromJson(Object? json) {
    final LiveSignalSettings settings = LiveSignalSettings.fromJson(json);
    final Object? status = (json as Map)['status'];
    return LiveSignalSettingsResult(
      settings: settings,
      status: status is Map ? LiveSignalsStatus.fromJson(status) : null,
    );
  }
}

// ------------------------------------------------------------------ WebSocket

/// Kinds of `/ws/signals` messages; anything else is [unknown] (ignored).
enum SignalsWsType {
  snapshot,
  signal,
  expired,
  superseded,
  status,
  keepalive,
  error,
  unknown;

  static SignalsWsType parse(Object? raw) => switch (raw) {
        'snapshot' => snapshot,
        'signal' => signal,
        'expired' => expired,
        'superseded' => superseded,
        'status' => status,
        'keepalive' => keepalive,
        'error' => error,
        _ => unknown,
      };
}

/// One parsed `/ws/signals` message.
@immutable
class SignalsWsMessage {
  const SignalsWsMessage({
    required this.type,
    this.rawType,
    this.seq,
    this.status,
    this.active = const [],
    this.signal,
    this.messageFa,
  });

  final SignalsWsType type;
  final String? rawType;
  final int? seq;
  final LiveSignalsStatus? status;

  /// `snapshot.active`.
  final List<LiveSignal> active;

  /// `signal` / `expired` / `superseded`.
  final LiveSignal? signal;

  /// `error.message_fa`.
  final String? messageFa;

  /// Tolerant parse of a decoded frame; null when it is not an object.
  static SignalsWsMessage? fromJson(Object? json) {
    final Map<String, Object?>? m = _map(json);
    if (m == null) return null;
    final SignalsWsType type = SignalsWsType.parse(m['type']);
    LiveSignalsStatus? status;
    if (m['status'] is Map) {
      try {
        status = LiveSignalsStatus.fromJson(m['status']);
      } on FormatException {
        status = null;
      }
    }
    return SignalsWsMessage(
      type: type,
      rawType: _str(m['type']),
      seq: _int(m['seq']),
      status: status,
      active: type == SignalsWsType.snapshot ? LiveSignal.listFrom(m['active'], where: 'snapshot.active') : const [],
      signal: m.containsKey('signal') ? LiveSignal.tryParse(m['signal'], where: 'ws ${m['type']}') : null,
      messageFa: _text(m['message_fa']),
    );
  }

  @override
  String toString() => 'SignalsWsMessage(${rawType ?? type.name}'
      '${seq != null ? ' seq=$seq' : ''}'
      '${type == SignalsWsType.snapshot ? ' active=${active.length}' : ''}'
      '${signal != null ? ' #${signal!.id} ${signal!.status.code}' : ''})';
}

// ------------------------------------------------------------------ countdown

/// Shown instead of a countdown while the market is closed after the entry bar.
const String kExpiryPendingFa = 'انقضا پس از بازگشایی بازار';

/// Shown once the expiry time passed but the engine has not marked it yet.
const String kExpiryReachedFa = 'زمان انقضا گذشت (در انتظار به‌روزرسانی موتور)';

/// The card's expiry line at [now]: `انقضا تا 0:42:05` (h:mm:ss, Latin
/// digits like the other timers), [kExpiryPendingFa] without an expiry, and
/// [kExpiryReachedFa] once it passed. Formatting only.
String formatSignalCountdown(LiveSignal s, DateTime now) {
  final DateTime? expires = s.expiresTime;
  if (s.expiryPending || expires == null) return kExpiryPendingFa;
  final Duration left = expires.difference(now.toUtc());
  if (left <= Duration.zero) return kExpiryReachedFa;
  return 'انقضا تا ${formatCountdown(left)}';
}

/// `h:mm:ss` (hours unbounded, never negative).
String formatCountdown(Duration d) {
  final int total = d.isNegative ? 0 : d.inSeconds;
  final int h = total ~/ 3600;
  final int m = (total % 3600) ~/ 60;
  final int s = total % 60;
  return '$h:${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}';
}
