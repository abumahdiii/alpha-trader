// Typed models of the engine's `/backtests` routes and `/ws/backtests/{id}`
// progress messages (engine/alpha_engine/routes/backtests.py,
// engine/alpha_engine/backtest/models.py, engine/README.md "Backtests").
//
// Runs, requests, config, plan and progress live here; metrics, windows,
// trades, equity and skipped candidates in `backtest_result_models.dart`
// (re-exported, so one import is enough).
//
// Every value is shown as the engine sent it: nothing here derives a
// trading number. Unknown keys are ignored and a `null` becomes a Dart
// `null` (display: «—»); only identity fields (id, status, symbol, mode)
// are required, so a newer engine that adds fields keeps working.

import 'package:flutter/foundation.dart';

import 'account_settings.dart';
import 'backtest_result_models.dart';
import 'json_reader.dart';

export 'backtest_result_models.dart';

// ------------------------------------------------------------------ enums

/// `manual` = one [from, to) period, `random` = N random windows (seeded).
enum BacktestMode {
  manual('manual', 'دستی'),
  random('random', 'تصادفی');

  const BacktestMode(this.code, this.labelFa);

  final String code;
  final String labelFa;

  static BacktestMode parse(String raw) => raw == 'random' ? BacktestMode.random : BacktestMode.manual;
}

/// Run status: `queued -> running -> done | error | cancelled`,
/// `queued -> cancelled`, `* -> interrupted` (engine restart).
enum BacktestStatus {
  queued('queued', 'در صف'),
  running('running', 'در حال اجرا'),
  done('done', 'پایان‌یافته'),
  error('error', 'ناموفق'),
  cancelled('cancelled', 'لغوشده'),
  interrupted('interrupted', 'نیمه‌کاره'),
  unknown('unknown', 'نامشخص');

  const BacktestStatus(this.code, this.labelFa);

  final String code;
  final String labelFa;

  bool get isActive => this == queued || this == running;

  bool get isTerminal => this == done || this == error || this == cancelled || this == interrupted;

  static BacktestStatus parse(String? raw) => BacktestStatus.values.firstWhere(
        (BacktestStatus s) => s.code == raw,
        orElse: () => BacktestStatus.unknown,
      );
}

/// Persian label of a progress `phase` (queued|loading|scanning|simulating|saving|finished).
String backtestPhaseFa(String? phase) => switch (phase) {
      'queued' => 'در صف اجرا',
      'loading' => 'بارگذاری داده',
      'scanning' => 'اسکن ستاپ‌ها',
      'simulating' => 'شبیه‌سازی معاملات',
      'saving' => 'ذخیره نتایج',
      'finished' => 'پایان',
      'running' => 'در حال اجرا',
      null => 'در انتظار',
      _ => phase,
    };

// ---------------------------------------------------------------- request

/// Body of `POST /backtests`. Fields of the other mode are never sent (the
/// engine answers 422 `field_not_for_mode`); `null` = "not given" (engine
/// default: random `seed` generated and stored, spread fallback = auto).
@immutable
class BacktestRequest {
  const BacktestRequest.manual({
    required this.symbol,
    required DateTime this.from,
    required DateTime this.to,
    this.commissionPerLotPerSide,
    this.fallbackSpreadPoints,
  })  : mode = BacktestMode.manual,
        windowsCount = null,
        windowMonths = null,
        seed = null;

  const BacktestRequest.random({
    required this.symbol,
    required int this.windowsCount,
    required int this.windowMonths,
    this.seed,
    this.commissionPerLotPerSide,
    this.fallbackSpreadPoints,
  })  : mode = BacktestMode.random,
        from = null,
        to = null;

  final String symbol;
  final BacktestMode mode;

  /// Manual: `[from, to)` in UTC.
  final DateTime? from;
  final DateTime? to;
  final int? windowsCount;
  final int? windowMonths;
  final int? seed;
  final double? commissionPerLotPerSide;

  /// null = auto (median observed spread), 0 = zero cost, > 0 = user value.
  final int? fallbackSpreadPoints;

  Map<String, Object> toJson() => {
        'symbol': symbol,
        'mode': mode.code,
        if (from != null) 'from': from!.toUtc().toIso8601String(),
        if (to != null) 'to': to!.toUtc().toIso8601String(),
        if (windowsCount != null) 'windows_count': windowsCount!,
        if (windowMonths != null) 'window_months': windowMonths!,
        if (seed != null) 'seed': seed!,
        if (commissionPerLotPerSide != null) 'commission_per_lot_per_side': commissionPerLotPerSide!,
        if (fallbackSpreadPoints != null) 'fallback_spread_points': fallbackSpreadPoints!,
      };

  @override
  String toString() => 'BacktestRequest(${toJson()})';
}

/// `POST /backtests` -> 202 `{id, status: "queued", provisional, labels_fa}`.
@immutable
class BacktestSubmitResult {
  const BacktestSubmitResult({
    required this.id,
    required this.status,
    required this.provisional,
    required this.labelsFa,
  });

  final int id;
  final BacktestStatus status;
  final bool provisional;
  final List<String> labelsFa;

  factory BacktestSubmitResult.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'backtest_submit');
    return BacktestSubmitResult(
      id: r.integer('id'),
      status: BacktestStatus.parse(r.strOrNull('status')),
      provisional: r.boolOrNull('provisional') ?? true,
      labelsFa: r.strings('labels_fa'),
    );
  }
}

/// `POST /backtests/{id}/cancel` -> `{id, status: "cancelled" | "running", cancel_requested}`.
@immutable
class BacktestCancelResult {
  const BacktestCancelResult({required this.id, required this.status, required this.cancelRequested});

  final int id;

  /// `cancelled` = it was queued and is final now; `running` = it stops at
  /// the next check (the final message follows on the WebSocket).
  final BacktestStatus status;
  final bool cancelRequested;

  factory BacktestCancelResult.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'backtest_cancel');
    return BacktestCancelResult(
      id: r.integer('id'),
      status: BacktestStatus.parse(r.strOrNull('status')),
      cancelRequested: r.boolOrNull('cancel_requested') ?? false,
    );
  }
}

// ---------------------------------------------------------------- summary

/// `error: {code, message_fa}` of a failed / cancelled / interrupted run.
@immutable
class BacktestRunError {
  const BacktestRunError({this.code, this.messageFa});

  final String? code;
  final String? messageFa;

  static BacktestRunError? fromReader(JsonReader? r) =>
      r == null ? null : BacktestRunError(code: r.strOrNull('code'), messageFa: r.strOrNull('message_fa'));
}

/// One run as listed by `GET /backtests` (`RunSummary`).
@immutable
class BacktestRunSummary {
  const BacktestRunSummary({
    required this.id,
    required this.status,
    required this.symbol,
    required this.mode,
    this.createdAt,
    this.startedAt,
    this.finishedAt,
    this.progress,
    this.from,
    this.to,
    this.windowsCount,
    this.windowMonths,
    this.seed,
    this.seedGenerated,
    this.strategy,
    this.strategyVersion,
    this.paramsVersion,
    this.paramsHash,
    this.provisional = true,
    this.tradeCount,
    this.netProfit,
    this.netProfitPct,
    this.summaryBasis,
    this.elapsedS,
    this.error,
  });

  final int id;
  final BacktestStatus status;
  final String symbol;
  final BacktestMode mode;
  final DateTime? createdAt;
  final DateTime? startedAt;
  final DateTime? finishedAt;

  /// 0..100.
  final double? progress;

  /// Manual period `[from, to)` (UTC).
  final DateTime? from;
  final DateTime? to;
  final int? windowsCount;
  final int? windowMonths;

  /// The seed used (null until a seedless random run is done).
  final int? seed;
  final bool? seedGenerated;
  final String? strategy;
  final int? strategyVersion;
  final int? paramsVersion;
  final String? paramsHash;
  final bool provisional;
  final int? tradeCount;

  /// Manual: the run's net profit; random: the MEAN window net profit
  /// ([summaryBasis] `window_mean`).
  final double? netProfit;
  final double? netProfitPct;

  /// `single_window` | `window_mean`.
  final String? summaryBasis;
  final double? elapsedS;
  final BacktestRunError? error;

  bool get isMeanOfWindows => summaryBasis == 'window_mean';

  factory BacktestRunSummary.fromJson(Object? json) => BacktestRunSummary.fromReader(JsonReader(json, 'backtest'));

  factory BacktestRunSummary.fromReader(JsonReader r) => BacktestRunSummary(
        id: r.integer('id'),
        status: BacktestStatus.parse(r.strOrNull('status')),
        symbol: r.str('symbol'),
        mode: BacktestMode.parse(r.str('mode')),
        createdAt: r.utcOrNull('created_at'),
        startedAt: r.utcOrNull('started_at'),
        finishedAt: r.utcOrNull('finished_at'),
        progress: r.numberOrNull('progress'),
        from: r.utcOrNull('from'),
        to: r.utcOrNull('to'),
        windowsCount: r.intOrNull('windows_count'),
        windowMonths: r.intOrNull('window_months'),
        seed: r.intOrNull('seed'),
        seedGenerated: r.boolOrNull('seed_generated'),
        strategy: r.strOrNull('strategy'),
        strategyVersion: r.intOrNull('strategy_version'),
        paramsVersion: r.intOrNull('params_version'),
        paramsHash: r.strOrNull('params_hash'),
        provisional: r.boolOrNull('provisional') ?? true,
        tradeCount: r.intOrNull('trade_count'),
        netProfit: r.numberOrNull('net_profit'),
        netProfitPct: r.numberOrNull('net_profit_pct'),
        summaryBasis: r.strOrNull('summary_basis'),
        elapsedS: r.numberOrNull('elapsed_s'),
        error: BacktestRunError.fromReader(r.objectOrNull('error')),
      );
}

/// `GET /backtests?limit` -> `{count, runs}` (newest first).
@immutable
class BacktestRunList {
  const BacktestRunList({required this.count, required this.runs});

  final int count;
  final List<BacktestRunSummary> runs;

  factory BacktestRunList.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'backtests');
    final List<BacktestRunSummary> runs = r.objects('runs', BacktestRunSummary.fromReader);
    return BacktestRunList(count: r.intOrNull('count') ?? runs.length, runs: runs);
  }
}

// ----------------------------------------------------------------- limits

/// `GET /backtests/limits?symbol` -- the manual period the engine accepts
/// for the ACTIVE params (computed by the same code `POST /backtests`
/// validates with): `earliest_start <= from < to <= data_end`.
@immutable
class BacktestLimits {
  const BacktestLimits({
    required this.symbol,
    this.earliestStart,
    this.dataStart,
    this.dataEnd,
    this.warmupH4Bars,
    this.windowMonthsMax,
    this.windowsCountMax,
    this.paramsVersion,
    this.paramsHash,
    this.noteFa,
  });

  final String symbol;

  /// First allowed `from` (after the channel + ATR warm-up), UTC.
  final DateTime? earliestStart;
  final DateTime? dataStart;

  /// Last allowed `to` (exclusive end), UTC.
  final DateTime? dataEnd;
  final int? warmupH4Bars;
  final int? windowMonthsMax;
  final int? windowsCountMax;
  final int? paramsVersion;
  final String? paramsHash;
  final String? noteFa;

  /// Whole UTC days a manual form may pick (the form sends `from` = the
  /// start day's midnight and `to` = the midnight after the end day):
  /// the first day starting at or after [earliestStart], and the last day
  /// ending at or before [dataEnd]. Calendar arithmetic only. null when the
  /// engine sent no bounds or no whole day fits.
  ({DateTime first, DateTime last})? get pickableDays {
    final DateTime? start = earliestStart?.toUtc();
    final DateTime? end = dataEnd?.toUtc();
    if (start == null || end == null) return null;
    DateTime first = DateTime.utc(start.year, start.month, start.day);
    if (first.isBefore(start)) first = first.add(const Duration(days: 1));
    final DateTime last = DateTime.utc(end.year, end.month, end.day).subtract(const Duration(days: 1));
    return last.isBefore(first) ? null : (first: first, last: last);
  }

  factory BacktestLimits.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'backtest_limits');
    return BacktestLimits(
      symbol: r.str('symbol'),
      earliestStart: r.utcOrNull('earliest_start'),
      dataStart: r.utcOrNull('data_start'),
      dataEnd: r.utcOrNull('data_end'),
      warmupH4Bars: r.intOrNull('warmup_h4_bars'),
      windowMonthsMax: r.intOrNull('window_months_max'),
      windowsCountMax: r.intOrNull('windows_count_max'),
      paramsVersion: r.intOrNull('params_version'),
      paramsHash: r.strOrNull('params_hash'),
      noteFa: r.strOrNull('note_fa'),
    );
  }

  @override
  String toString() => 'BacktestLimits($symbol earliest_start=${earliestStart?.toIso8601String()} '
      'data_end=${dataEnd?.toIso8601String()} params_hash=$paramsHash)';
}

// ----------------------------------------------------------------- config

/// `CostModel`: spread is always the broker's history; the fallback covers
/// bars without an earlier broker spread.
@immutable
class BacktestCostModel {
  const BacktestCostModel({
    this.spread,
    this.fallbackSpreadPoints,
    this.commissionPerLotPerSide,
    this.swap,
  });

  final String? spread;

  /// null = auto, 0 = zero cost, > 0 = user value.
  final int? fallbackSpreadPoints;
  final double? commissionPerLotPerSide;
  final String? swap;

  static BacktestCostModel? fromReader(JsonReader? r) => r == null
      ? null
      : BacktestCostModel(
          spread: r.strOrNull('spread'),
          fallbackSpreadPoints: r.intOrNull('fallback_spread_points'),
          commissionPerLotPerSide: r.numberOrNull('commission_per_lot_per_side'),
          swap: r.strOrNull('swap'),
        );
}

/// `RunConfig` as submitted (snapshotted at POST time).
@immutable
class BacktestConfig {
  const BacktestConfig({
    this.symbol,
    this.mode,
    this.start,
    this.end,
    this.windowsCount,
    this.windowMonths,
    this.seed,
    this.costModel,
    this.account,
    this.strategyName,
    this.strategyVersion,
    this.params = const {},
    this.paramsVersion,
    this.paramsHash,
    this.provisional,
  });

  final String? symbol;
  final BacktestMode? mode;
  final DateTime? start;
  final DateTime? end;
  final int? windowsCount;
  final int? windowMonths;

  /// As submitted (null = generated; the used seed is in the plan).
  final int? seed;
  final BacktestCostModel? costModel;

  /// Balance (initial balance of every window), risk %, leverage, R:R.
  final AccountSettings? account;
  final String? strategyName;
  final int? strategyVersion;
  final Map<String, Object?> params;
  final int? paramsVersion;
  final String? paramsHash;
  final bool? provisional;

  static BacktestConfig? fromReader(JsonReader? r) {
    if (r == null) return null;
    final Object? account = r.raw('account');
    final Object? params = r.raw('params');
    AccountSettings? parsedAccount;
    if (account is Map) {
      try {
        parsedAccount = AccountSettings.fromJson(account);
      } on FormatException {
        parsedAccount = null; // shown as «—»; the run itself is still readable
      }
    }
    final String? mode = r.strOrNull('mode');
    return BacktestConfig(
      symbol: r.strOrNull('symbol'),
      mode: mode == null ? null : BacktestMode.parse(mode),
      start: r.utcOrNull('start'),
      end: r.utcOrNull('end'),
      windowsCount: r.intOrNull('windows_count'),
      windowMonths: r.intOrNull('window_months'),
      seed: r.intOrNull('seed'),
      costModel: BacktestCostModel.fromReader(r.objectOrNull('cost_model')),
      account: parsedAccount,
      strategyName: r.strOrNull('strategy_name'),
      strategyVersion: r.intOrNull('strategy_version'),
      params: params is Map ? Map<String, Object?>.unmodifiable(params.map((k, v) => MapEntry('$k', v))) : const {},
      paramsVersion: r.intOrNull('params_version'),
      paramsHash: r.strOrNull('params_hash'),
      provisional: r.boolOrNull('provisional'),
    );
  }
}

/// One window of the plan: `[start, end)` UTC.
@immutable
class BacktestWindowRange {
  const BacktestWindowRange({required this.index, this.start, this.end});

  final int index;
  final DateTime? start;
  final DateTime? end;

  factory BacktestWindowRange.fromReader(JsonReader r) =>
      BacktestWindowRange(index: r.integer('index'), start: r.utcOrNull('start'), end: r.utcOrNull('end'));
}

/// `WindowPlan`: how the windows were chosen; [seed] is the seed actually used.
@immutable
class BacktestWindowPlan {
  const BacktestWindowPlan({
    this.mode,
    this.windows = const [],
    this.seed,
    this.seedGenerated,
    this.algorithm,
    this.numpyVersion,
    this.windowsCount,
    this.windowMonths,
    this.earliestStart,
    this.dataEnd,
    this.eligibleStarts,
    this.warmupH4Bars,
  });

  final BacktestMode? mode;
  final List<BacktestWindowRange> windows;
  final int? seed;
  final bool? seedGenerated;
  final String? algorithm;
  final String? numpyVersion;
  final int? windowsCount;
  final int? windowMonths;
  final DateTime? earliestStart;
  final DateTime? dataEnd;
  final int? eligibleStarts;
  final int? warmupH4Bars;

  static BacktestWindowPlan? fromReader(JsonReader? r) {
    if (r == null) return null;
    final String? mode = r.strOrNull('mode');
    return BacktestWindowPlan(
      mode: mode == null ? null : BacktestMode.parse(mode),
      windows: r.raw('windows') is List ? r.objects('windows', BacktestWindowRange.fromReader) : const [],
      seed: r.intOrNull('seed'),
      seedGenerated: r.boolOrNull('seed_generated'),
      algorithm: r.strOrNull('algorithm'),
      numpyVersion: r.strOrNull('numpy_version'),
      windowsCount: r.intOrNull('windows_count'),
      windowMonths: r.intOrNull('window_months'),
      earliestStart: r.utcOrNull('earliest_start'),
      dataEnd: r.utcOrNull('data_end'),
      eligibleStarts: r.intOrNull('eligible_starts'),
      warmupH4Bars: r.intOrNull('warmup_h4_bars'),
    );
  }
}

/// `DataFingerprint`: identity of the cached data a run used (display subset).
@immutable
class BacktestDataFingerprint {
  const BacktestDataFingerprint({
    this.h1Rows,
    this.h4Rows,
    this.h1FirstBar,
    this.h1LastBar,
    this.h1Source,
    this.h4Source,
    this.h1Sha256,
    this.h4Sha256,
    this.specDigits,
    this.specSource,
  });

  final int? h1Rows;
  final int? h4Rows;
  final DateTime? h1FirstBar;
  final DateTime? h1LastBar;
  final String? h1Source;
  final String? h4Source;
  final String? h1Sha256;
  final String? h4Sha256;

  /// `spec.digits` of the symbol snapshot (price precision), when present.
  final int? specDigits;
  final String? specSource;

  static BacktestDataFingerprint? fromReader(JsonReader? r) {
    if (r == null) return null;
    final Object? spec = r.raw('spec');
    final Object? digits = spec is Map ? spec['digits'] : null;
    return BacktestDataFingerprint(
      h1Rows: r.intOrNull('h1_rows'),
      h4Rows: r.intOrNull('h4_rows'),
      h1FirstBar: r.utcOrNull('h1_first_bar'),
      h1LastBar: r.utcOrNull('h1_last_bar'),
      h1Source: r.strOrNull('h1_source'),
      h4Source: r.strOrNull('h4_source'),
      h1Sha256: r.strOrNull('h1_sha256'),
      h4Sha256: r.strOrNull('h4_sha256'),
      specDigits: digits is int ? digits : null,
      specSource: r.strOrNull('spec_source'),
    );
  }
}

/// `SpreadFallback`: the spread a run used for bars without an earlier broker spread.
@immutable
class BacktestSpreadFallback {
  const BacktestSpreadFallback({
    this.points,
    this.source,
    this.observedFrom,
    this.observedTo,
    this.observedBars,
    this.observedMedian,
    this.fallbackBars,
    this.zeroBars,
    this.totalBars,
  });

  final int? points;

  /// `auto_median_observed` | `user` | `none`.
  final String? source;
  final DateTime? observedFrom;
  final DateTime? observedTo;
  final int? observedBars;
  final double? observedMedian;
  final int? fallbackBars;
  final int? zeroBars;
  final int? totalBars;

  String get sourceFa => switch (source) {
        'auto_median_observed' => 'خودکار: میانه اسپرد مشاهده‌شده',
        'user' => 'تعیین کاربر',
        'none' => 'خودکار، ولی داده اسپردی در دسترس نبود',
        _ => source ?? '—',
      };

  static BacktestSpreadFallback? fromReader(JsonReader? r) => r == null
      ? null
      : BacktestSpreadFallback(
          points: r.intOrNull('points'),
          source: r.strOrNull('source'),
          observedFrom: r.utcOrNull('observed_from'),
          observedTo: r.utcOrNull('observed_to'),
          observedBars: r.intOrNull('observed_bars'),
          observedMedian: r.numberOrNull('observed_median'),
          fallbackBars: r.intOrNull('fallback_bars'),
          zeroBars: r.intOrNull('zero_bars'),
          totalBars: r.intOrNull('total_bars'),
        );
}

// ----------------------------------------------------------------- detail

/// `GET /backtests/{id}`: summary + config, plan, labels, metrics, windows.
/// `plan`, `fingerprint`, `metrics`, `windows` are empty until `done`.
@immutable
class BacktestRunDetail {
  const BacktestRunDetail({
    required this.summary,
    this.labelsFa = const [],
    this.provisionalLabelFa,
    this.config,
    this.plan,
    this.fingerprint,
    this.spreadFallback,
    this.metricsKind,
    this.metrics,
    this.randomMetrics,
    this.distribution,
    this.windows = const [],
    this.equityStorageRule,
    this.live,
  });

  final BacktestRunSummary summary;

  /// Cost / data labels to show with every result (engine text).
  final List<String> labelsFa;

  /// «موقت تا تایید چک داده» while the raw-data check is not confirmed.
  final String? provisionalLabelFa;
  final BacktestConfig? config;
  final BacktestWindowPlan? plan;
  final BacktestDataFingerprint? fingerprint;
  final BacktestSpreadFallback? spreadFallback;

  /// `single_window` | `random_aggregate`.
  final String? metricsKind;

  /// Manual run: the full metrics of its single window.
  final BacktestMetrics? metrics;

  /// Random run: pooled / mean / worst block.
  final BacktestRandomMetrics? randomMetrics;

  /// Random run: `summarize_windows` distribution.
  final BacktestDistribution? distribution;
  final List<BacktestWindowResult> windows;
  final String? equityStorageRule;

  /// Queued / running only: the live progress snapshot.
  final BacktestProgress? live;

  int get id => summary.id;
  BacktestStatus get status => summary.status;
  BacktestMode get mode => summary.mode;

  /// The seed actually used (plan), else the summary's.
  int? get seed => plan?.seed ?? summary.seed;

  factory BacktestRunDetail.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'backtest');
    final BacktestRunSummary summary = BacktestRunSummary.fromReader(r);
    final String? kind = r.strOrNull('metrics_kind');
    final JsonReader? metrics = r.objectOrNull('metrics');
    final bool random = kind == 'random_aggregate' || (kind == null && summary.mode == BacktestMode.random);
    final JsonReader? live = r.objectOrNull('live');
    return BacktestRunDetail(
      summary: summary,
      labelsFa: r.strings('labels_fa'),
      provisionalLabelFa: r.strOrNull('provisional_label_fa'),
      config: BacktestConfig.fromReader(r.objectOrNull('config')),
      plan: BacktestWindowPlan.fromReader(r.objectOrNull('plan')),
      fingerprint: BacktestDataFingerprint.fromReader(r.objectOrNull('fingerprint')),
      spreadFallback: BacktestSpreadFallback.fromReader(r.objectOrNull('spread_fallback')),
      metricsKind: kind,
      metrics: metrics == null || random ? null : BacktestMetrics.fromReader(metrics),
      randomMetrics: metrics == null || !random ? null : BacktestRandomMetrics.fromReader(metrics),
      distribution: BacktestDistribution.fromReader(r.objectOrNull('distribution')),
      windows: r.raw('windows') is List ? r.objects('windows', BacktestWindowResult.fromReader) : const [],
      equityStorageRule: r.strOrNull('equity_storage_rule'),
      live: live == null ? null : BacktestProgress.fromReader(live),
    );
  }
}

// --------------------------------------------------------------- progress

/// One `/ws/backtests/{id}` message (or the same built from a polled
/// `GET /backtests/{id}`):
/// * `{"type": "progress", status, phase, percent, window, window_index, windows, cancel_requested}`
/// * `{"type": "done", status, percent, trade_count, net_profit, net_profit_pct}`
/// * `{"type": "error"|"cancelled"|"interrupted", status, percent, code, message_fa}`
@immutable
class BacktestProgress {
  const BacktestProgress({
    required this.type,
    this.id,
    this.status = BacktestStatus.unknown,
    this.phase,
    this.percent,
    this.window,
    this.windows,
    this.cancelRequested = false,
    this.tradeCount,
    this.netProfit,
    this.netProfitPct,
    this.code,
    this.messageFa,
  });

  /// `progress` | `done` | `error` | `cancelled` | `interrupted`
  /// (+ `lost`: the UI could no longer follow the run).
  final String type;
  final int? id;
  final BacktestStatus status;
  final String? phase;

  /// Overall 0..100.
  final double? percent;

  /// 1-based window being simulated, of [windows].
  final int? window;
  final int? windows;
  final bool cancelRequested;
  final int? tradeCount;
  final double? netProfit;
  final double? netProfitPct;
  final String? code;
  final String? messageFa;

  static const String lostType = 'lost';
  static const String progressType = 'progress';

  /// Engine heartbeat while a run is queued/running (`{"type":"keepalive"}`);
  /// carries no progress and is never a final state.
  static const String keepaliveType = 'keepalive';

  /// Message types that end a run (whitelist: an unknown type is never final).
  static const Set<String> finalTypes = {'done', 'error', 'cancelled', 'interrupted', lostType};

  bool get isFinal => finalTypes.contains(type);

  bool get isProgress => type == progressType;

  bool get isDone => type == 'done';

  factory BacktestProgress.fromJson(Object? json) => BacktestProgress.fromReader(JsonReader(json, 'progress'));

  /// A WebSocket message, or the `live` snapshot of the detail (no `type`).
  factory BacktestProgress.fromReader(JsonReader r) {
    final BacktestStatus status = BacktestStatus.parse(r.strOrNull('status'));
    final String type = r.strOrNull('type') ?? (status.isTerminal ? status.code : 'progress');
    return BacktestProgress(
      type: type,
      id: r.intOrNull('id'),
      status: status,
      phase: r.strOrNull('phase'),
      percent: r.numberOrNull('percent'),
      window: r.intOrNull('window'),
      windows: r.intOrNull('windows'),
      cancelRequested: r.boolOrNull('cancel_requested') ?? false,
      tradeCount: r.intOrNull('trade_count'),
      netProfit: r.numberOrNull('net_profit'),
      netProfitPct: r.numberOrNull('net_profit_pct'),
      code: r.strOrNull('code') ?? r.strOrNull('error_code'),
      messageFa: r.strOrNull('message_fa'),
    );
  }

  /// The progress of a polled run: its `live` snapshot while active, a
  /// final message built from the summary once it is terminal.
  factory BacktestProgress.fromDetail(BacktestRunDetail d) {
    final BacktestRunSummary s = d.summary;
    if (s.status.isTerminal) {
      return BacktestProgress(
        type: s.status.code,
        id: s.id,
        status: s.status,
        phase: 'finished',
        percent: s.status == BacktestStatus.done ? 100.0 : s.progress,
        tradeCount: s.tradeCount,
        netProfit: s.netProfit,
        netProfitPct: s.netProfitPct,
        code: s.error?.code,
        messageFa: s.error?.messageFa,
      );
    }
    final BacktestProgress? live = d.live;
    return BacktestProgress(
      type: 'progress',
      id: s.id,
      status: s.status,
      phase: live?.phase ?? s.status.code,
      percent: live?.percent ?? s.progress,
      window: live?.window,
      windows: live?.windows,
      cancelRequested: live?.cancelRequested ?? false,
    );
  }

  @override
  String toString() => 'BacktestProgress($type ${status.code} ${phase ?? '-'} ${percent ?? '-'}%'
      '${window != null ? ' w$window/$windows' : ''}${cancelRequested ? ' cancel' : ''}'
      '${code != null ? ' $code' : ''})';

  @override
  bool operator ==(Object other) =>
      other is BacktestProgress &&
      other.type == type &&
      other.status == status &&
      other.phase == phase &&
      other.percent == percent &&
      other.window == window &&
      other.cancelRequested == cancelRequested &&
      other.code == code;

  @override
  int get hashCode => Object.hash(type, status, phase, percent, window, cancelRequested, code);
}
