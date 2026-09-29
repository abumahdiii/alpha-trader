// Result models of a backtest run: metrics, windows, distribution, trades,
// equity and skipped candidates (engine `backtest/metrics.py`,
// `backtest/results.py`, `routes/backtests.py`). Imported through
// `backtest_models.dart`.
//
// Display-only: every number is the engine's own, unrounded. `win_rate` is a
// fraction 0..1, `pct_profitable` and `*_pct` are percents, `avg_loss` /
// `largest_loss` are negative, money is in the account currency (USD).

import 'package:flutter/foundation.dart';

import 'json_reader.dart';

/// `BacktestMetrics.to_dict()` (one window / a manual run). Also used for the
/// `pooled` block of a random run, which carries only the trade-level keys
/// (the rest stay null).
@immutable
class BacktestMetrics {
  const BacktestMetrics({
    this.initialBalance,
    this.finalBalance,
    this.tradeCount,
    this.winCount,
    this.lossCount,
    this.breakevenCount,
    this.winRate,
    this.grossProfit,
    this.grossLoss,
    this.netProfit,
    this.netProfitPct,
    this.profitFactor,
    this.profitFactorInfinite = false,
    this.expectancy,
    this.avgWin,
    this.avgLoss,
    this.largestWin,
    this.largestLoss,
    this.maxConsecutiveWins,
    this.maxConsecutiveLosses,
    this.avgR,
    this.rCount,
    this.maxDrawdownAbs,
    this.maxDrawdownPct,
    this.maxDrawdownPeakTime,
    this.maxDrawdownTroughTime,
    this.sharpe,
    this.sharpeReturnCount,
  });

  final double? initialBalance;
  final double? finalBalance;
  final int? tradeCount;
  final int? winCount;
  final int? lossCount;
  final int? breakevenCount;

  /// Fraction 0..1 (null without trades).
  final double? winRate;
  final double? grossProfit;

  /// Positive magnitude.
  final double? grossLoss;
  final double? netProfit;
  final double? netProfitPct;

  /// null when there is no loss; then [profitFactorInfinite] tells ∞ from «—».
  final double? profitFactor;
  final bool profitFactorInfinite;
  final double? expectancy;
  final double? avgWin;

  /// Negative.
  final double? avgLoss;
  final double? largestWin;

  /// Most negative P&L.
  final double? largestLoss;
  final int? maxConsecutiveWins;
  final int? maxConsecutiveLosses;
  final double? avgR;
  final int? rCount;
  final double? maxDrawdownAbs;
  final double? maxDrawdownPct;
  final DateTime? maxDrawdownPeakTime;
  final DateTime? maxDrawdownTroughTime;
  final double? sharpe;
  final int? sharpeReturnCount;

  static BacktestMetrics? fromReaderOrNull(JsonReader? r) => r == null ? null : BacktestMetrics.fromReader(r);

  factory BacktestMetrics.fromReader(JsonReader r) => BacktestMetrics(
        initialBalance: r.numberOrNull('initial_balance'),
        finalBalance: r.numberOrNull('final_balance'),
        tradeCount: r.intOrNull('trade_count'),
        winCount: r.intOrNull('win_count'),
        lossCount: r.intOrNull('loss_count'),
        breakevenCount: r.intOrNull('breakeven_count'),
        winRate: r.numberOrNull('win_rate'),
        grossProfit: r.numberOrNull('gross_profit'),
        grossLoss: r.numberOrNull('gross_loss'),
        netProfit: r.numberOrNull('net_profit'),
        netProfitPct: r.numberOrNull('net_profit_pct'),
        profitFactor: r.numberOrNull('profit_factor'),
        profitFactorInfinite: r.boolOrNull('profit_factor_infinite') ?? false,
        expectancy: r.numberOrNull('expectancy'),
        avgWin: r.numberOrNull('avg_win'),
        avgLoss: r.numberOrNull('avg_loss'),
        largestWin: r.numberOrNull('largest_win'),
        largestLoss: r.numberOrNull('largest_loss'),
        maxConsecutiveWins: r.intOrNull('max_consecutive_wins'),
        maxConsecutiveLosses: r.intOrNull('max_consecutive_losses'),
        avgR: r.numberOrNull('avg_r'),
        rCount: r.intOrNull('r_count'),
        maxDrawdownAbs: r.numberOrNull('max_drawdown_abs'),
        maxDrawdownPct: r.numberOrNull('max_drawdown_pct'),
        maxDrawdownPeakTime: r.utcOrNull('max_drawdown_peak_time'),
        maxDrawdownTroughTime: r.utcOrNull('max_drawdown_trough_time'),
        sharpe: r.numberOrNull('sharpe'),
        sharpeReturnCount: r.intOrNull('sharpe_return_count'),
      );
}

/// `mean` block of a random run: means over the windows.
@immutable
class BacktestMeanMetrics {
  const BacktestMeanMetrics({
    this.netProfit,
    this.netProfitPct,
    this.maxDrawdownAbs,
    this.maxDrawdownPct,
    this.tradeCount,
    this.winRate,
    this.winRateWindows,
    this.sharpe,
    this.sharpeWindows,
  });

  final double? netProfit;
  final double? netProfitPct;
  final double? maxDrawdownAbs;
  final double? maxDrawdownPct;

  /// Mean trades per window (may be fractional).
  final double? tradeCount;

  /// Fraction 0..1, over the [winRateWindows] windows with trades.
  final double? winRate;
  final int? winRateWindows;
  final double? sharpe;
  final int? sharpeWindows;

  factory BacktestMeanMetrics.fromReader(JsonReader r) => BacktestMeanMetrics(
        netProfit: r.numberOrNull('net_profit'),
        netProfitPct: r.numberOrNull('net_profit_pct'),
        maxDrawdownAbs: r.numberOrNull('max_drawdown_abs'),
        maxDrawdownPct: r.numberOrNull('max_drawdown_pct'),
        tradeCount: r.numberOrNull('trade_count'),
        winRate: r.numberOrNull('win_rate'),
        winRateWindows: r.intOrNull('win_rate_windows'),
        sharpe: r.numberOrNull('sharpe'),
        sharpeWindows: r.intOrNull('sharpe_windows'),
      );
}

/// `worst` block of a random run.
@immutable
class BacktestWorstMetrics {
  const BacktestWorstMetrics({this.maxDrawdownAbs, this.maxDrawdownPct, this.netProfitPct});

  final double? maxDrawdownAbs;
  final double? maxDrawdownPct;
  final double? netProfitPct;

  factory BacktestWorstMetrics.fromReader(JsonReader r) => BacktestWorstMetrics(
        maxDrawdownAbs: r.numberOrNull('max_drawdown_abs'),
        maxDrawdownPct: r.numberOrNull('max_drawdown_pct'),
        netProfitPct: r.numberOrNull('net_profit_pct'),
      );
}

/// `metrics` of a random run (`metrics_kind: "random_aggregate"`).
@immutable
class BacktestRandomMetrics {
  const BacktestRandomMetrics({
    this.windowCount,
    this.windowsWithTrades,
    this.initialBalance,
    this.totalTrades,
    this.pooled,
    this.mean,
    this.worst,
    this.noteFa,
  });

  final int? windowCount;
  final int? windowsWithTrades;
  final double? initialBalance;
  final int? totalTrades;

  /// All trades of all windows together (trade-level fields only).
  final BacktestMetrics? pooled;
  final BacktestMeanMetrics? mean;
  final BacktestWorstMetrics? worst;
  final String? noteFa;

  factory BacktestRandomMetrics.fromReader(JsonReader r) {
    final JsonReader? mean = r.objectOrNull('mean');
    final JsonReader? worst = r.objectOrNull('worst');
    return BacktestRandomMetrics(
      windowCount: r.intOrNull('window_count'),
      windowsWithTrades: r.intOrNull('windows_with_trades'),
      initialBalance: r.numberOrNull('initial_balance'),
      totalTrades: r.intOrNull('total_trades'),
      pooled: BacktestMetrics.fromReaderOrNull(r.objectOrNull('pooled')),
      mean: mean == null ? null : BacktestMeanMetrics.fromReader(mean),
      worst: worst == null ? null : BacktestWorstMetrics.fromReader(worst),
      noteFa: r.strOrNull('note_fa'),
    );
  }
}

/// `worst_window` / `best_window` of the distribution.
@immutable
class BacktestWindowRef {
  const BacktestWindowRef({required this.index, this.netProfit, this.netProfitPct, this.tradeCount});

  /// 0-based window index.
  final int index;
  final double? netProfit;
  final double? netProfitPct;
  final int? tradeCount;

  static BacktestWindowRef? fromReader(JsonReader? r) => r == null
      ? null
      : BacktestWindowRef(
          index: r.integer('index'),
          netProfit: r.numberOrNull('net_profit'),
          netProfitPct: r.numberOrNull('net_profit_pct'),
          tradeCount: r.intOrNull('trade_count'),
        );
}

/// `distribution` (`summarize_windows`) of a random run.
@immutable
class BacktestDistribution {
  const BacktestDistribution({
    this.count,
    this.windowsWithoutTrades,
    this.meanNetProfit,
    this.medianNetProfit,
    this.meanNetProfitPct,
    this.medianNetProfitPct,
    this.pctProfitable,
    this.worstWindow,
    this.bestWindow,
  });

  final int? count;
  final int? windowsWithoutTrades;
  final double? meanNetProfit;
  final double? medianNetProfit;
  final double? meanNetProfitPct;
  final double? medianNetProfitPct;

  /// Percent 0..100 of windows with a net profit.
  final double? pctProfitable;
  final BacktestWindowRef? worstWindow;
  final BacktestWindowRef? bestWindow;

  static BacktestDistribution? fromReader(JsonReader? r) => r == null
      ? null
      : BacktestDistribution(
          count: r.intOrNull('count'),
          windowsWithoutTrades: r.intOrNull('windows_without_trades'),
          meanNetProfit: r.numberOrNull('mean_net_profit'),
          medianNetProfit: r.numberOrNull('median_net_profit'),
          meanNetProfitPct: r.numberOrNull('mean_net_profit_pct'),
          medianNetProfitPct: r.numberOrNull('median_net_profit_pct'),
          pctProfitable: r.numberOrNull('pct_profitable'),
          worstWindow: BacktestWindowRef.fromReader(r.objectOrNull('worst_window')),
          bestWindow: BacktestWindowRef.fromReader(r.objectOrNull('best_window')),
        );
}

/// One entry of the detail's `windows` (`WindowOut`), with its own metrics.
@immutable
class BacktestWindowResult {
  const BacktestWindowResult({
    required this.index,
    this.start,
    this.end,
    this.initialBalance,
    this.finalBalance,
    this.netProfit,
    this.netProfitPct,
    this.tradeCount,
    this.skippedCount,
    this.candidates,
    this.bars,
    this.weekendHolds,
    this.spreadFallbackBars,
    this.stoppedReason,
    this.metrics,
  });

  /// 0-based (shown 1-based).
  final int index;
  final DateTime? start;
  final DateTime? end;
  final double? initialBalance;
  final double? finalBalance;
  final double? netProfit;
  final double? netProfitPct;
  final int? tradeCount;
  final int? skippedCount;
  final int? candidates;
  final int? bars;
  final int? weekendHolds;
  final int? spreadFallbackBars;

  /// `balance_depleted` when the window stopped early.
  final String? stoppedReason;
  final BacktestMetrics? metrics;

  factory BacktestWindowResult.fromReader(JsonReader r) => BacktestWindowResult(
        index: r.integer('index'),
        start: r.utcOrNull('start'),
        end: r.utcOrNull('end'),
        initialBalance: r.numberOrNull('initial_balance'),
        finalBalance: r.numberOrNull('final_balance'),
        netProfit: r.numberOrNull('net_profit'),
        netProfitPct: r.numberOrNull('net_profit_pct'),
        tradeCount: r.intOrNull('trade_count'),
        skippedCount: r.intOrNull('skipped_count'),
        candidates: r.intOrNull('candidates'),
        bars: r.intOrNull('bars'),
        weekendHolds: r.intOrNull('weekend_holds'),
        spreadFallbackBars: r.intOrNull('spread_fallback_bars'),
        stoppedReason: r.strOrNull('stopped_reason'),
        metrics: BacktestMetrics.fromReaderOrNull(r.objectOrNull('metrics')),
      );
}

// ----------------------------------------------------------------- trades

/// `buy` / `sell` in Persian.
String directionFa(String? direction) => switch (direction) {
      'buy' => 'خرید',
      'sell' => 'فروش',
      null => '—',
      _ => direction,
    };

/// One simulated trade (`Trade.model_dump(mode="json")`). Prices unrounded,
/// times UTC.
@immutable
class BacktestTrade {
  const BacktestTrade({
    required this.windowIndex,
    required this.tradeIndex,
    this.direction,
    this.setupType,
    this.line,
    this.pattern,
    this.confirmationBarTime,
    this.decisionTime,
    this.entryTime,
    this.entry,
    this.stopLoss,
    this.takeProfit,
    this.rr,
    this.volume,
    this.riskAmount,
    this.balanceBefore,
    this.exitBarTime,
    this.exitTime,
    this.exitPrice,
    this.exitReason,
    this.exitReasonFa,
    this.grossPnl,
    this.commission,
    this.netPnl,
    this.rMultiple,
    this.balanceAfter,
    this.barsHeld,
    this.spreadAtEntryPoints,
    this.heldOverWeekend,
    this.reasonFa,
    this.sizingWarningsFa = const [],
  });

  final int windowIndex;
  final int tradeIndex;

  /// `buy` | `sell`.
  final String? direction;
  final String? setupType;
  final String? line;
  final String? pattern;
  final DateTime? confirmationBarTime;
  final DateTime? decisionTime;

  /// Open of the entry bar (the fill instant).
  final DateTime? entryTime;
  final double? entry;
  final double? stopLoss;
  final double? takeProfit;
  final double? rr;
  final double? volume;
  final double? riskAmount;
  final double? balanceBefore;
  final DateTime? exitBarTime;
  final DateTime? exitTime;
  final double? exitPrice;

  /// `sl` | `tp` | `sl_gap` | `tp_gap` | `end_of_period`.
  final String? exitReason;
  final String? exitReasonFa;
  final double? grossPnl;
  final double? commission;
  final double? netPnl;
  final double? rMultiple;
  final double? balanceAfter;
  final int? barsHeld;
  final int? spreadAtEntryPoints;
  final bool? heldOverWeekend;

  /// The strategy's rationale.
  final String? reasonFa;
  final List<String> sizingWarningsFa;

  factory BacktestTrade.fromReader(JsonReader r) => BacktestTrade(
        windowIndex: r.intOrNull('window_index') ?? 0,
        tradeIndex: r.intOrNull('trade_index') ?? 0,
        direction: r.strOrNull('direction'),
        setupType: r.strOrNull('setup_type'),
        line: r.strOrNull('line'),
        pattern: r.strOrNull('pattern'),
        confirmationBarTime: r.utcOrNull('confirmation_bar_time'),
        decisionTime: r.utcOrNull('decision_time'),
        entryTime: r.utcOrNull('entry_time'),
        entry: r.numberOrNull('entry'),
        stopLoss: r.numberOrNull('stop_loss'),
        takeProfit: r.numberOrNull('take_profit'),
        rr: r.numberOrNull('rr'),
        volume: r.numberOrNull('volume'),
        riskAmount: r.numberOrNull('risk_amount'),
        balanceBefore: r.numberOrNull('balance_before'),
        exitBarTime: r.utcOrNull('exit_bar_time'),
        exitTime: r.utcOrNull('exit_time'),
        exitPrice: r.numberOrNull('exit_price'),
        exitReason: r.strOrNull('exit_reason'),
        exitReasonFa: r.strOrNull('exit_reason_fa'),
        grossPnl: r.numberOrNull('gross_pnl'),
        commission: r.numberOrNull('commission'),
        netPnl: r.numberOrNull('net_pnl'),
        rMultiple: r.numberOrNull('r_multiple'),
        balanceAfter: r.numberOrNull('balance_after'),
        barsHeld: r.intOrNull('bars_held'),
        spreadAtEntryPoints: r.intOrNull('spread_at_entry_points'),
        heldOverWeekend: r.boolOrNull('held_over_weekend'),
        reasonFa: r.strOrNull('reason_fa'),
        sizingWarningsFa: r.strings('sizing_warnings_fa'),
      );
}

/// `GET /backtests/{id}/trades` -> `{run_id, window, total, offset, limit, trades}`.
@immutable
class BacktestTradesPage {
  const BacktestTradesPage({
    required this.runId,
    required this.total,
    required this.offset,
    required this.limit,
    required this.trades,
    this.window,
  });

  final int runId;
  final int? window;

  /// All trades of the run (or window); [trades] may be fewer (paging).
  final int total;
  final int offset;
  final int limit;
  final List<BacktestTrade> trades;

  bool get isPartial => offset + trades.length < total || offset > 0;

  factory BacktestTradesPage.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'backtest_trades');
    final List<BacktestTrade> trades = r.objects('trades', BacktestTrade.fromReader);
    return BacktestTradesPage(
      runId: r.integer('run_id'),
      window: r.intOrNull('window'),
      total: r.intOrNull('total') ?? trades.length,
      offset: r.intOrNull('offset') ?? 0,
      limit: r.intOrNull('limit') ?? trades.length,
      trades: trades,
    );
  }
}

// ----------------------------------------------------------------- equity

@immutable
class BacktestEquityPoint {
  const BacktestEquityPoint({required this.windowIndex, required this.time, this.balance, this.equity});

  final int windowIndex;
  final DateTime time;

  /// Realized balance.
  final double? balance;

  /// Mark-to-market equity.
  final double? equity;

  factory BacktestEquityPoint.fromReader(JsonReader r) {
    final DateTime? time = r.utcOrNull('time');
    if (time == null) throw FormatException('${r.what}.time is missing');
    return BacktestEquityPoint(
      windowIndex: r.intOrNull('window_index') ?? 0,
      time: time,
      balance: r.numberOrNull('balance'),
      equity: r.numberOrNull('equity'),
    );
  }
}

/// `GET /backtests/{id}/equity` -> `{run_id, window, downsampled, rule, count, points}`.
/// The stored curve is downsampled (display only; metrics come from the full curve).
@immutable
class BacktestEquity {
  const BacktestEquity({required this.runId, required this.points, this.window, this.downsampled = true, this.rule});

  final int runId;
  final int? window;
  final bool downsampled;
  final String? rule;
  final List<BacktestEquityPoint> points;

  /// The points of one window, in engine order (display filter only).
  List<BacktestEquityPoint> ofWindow(int index) => [
        for (final BacktestEquityPoint p in points)
          if (p.windowIndex == index) p
      ];

  factory BacktestEquity.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'backtest_equity');
    return BacktestEquity(
      runId: r.integer('run_id'),
      window: r.intOrNull('window'),
      downsampled: r.boolOrNull('downsampled') ?? true,
      rule: r.strOrNull('rule'),
      points: r.objects('points', BacktestEquityPoint.fromReader),
    );
  }
}

// ---------------------------------------------------------------- skipped

/// A scan candidate the simulator did not trade, with the Persian reason.
@immutable
class BacktestSkippedCandidate {
  const BacktestSkippedCandidate({
    required this.windowIndex,
    this.time,
    this.confirmationBarTime,
    this.direction,
    this.setupType,
    this.reason,
    this.reasonFa,
    this.detail,
  });

  final int windowIndex;

  /// Decision time (close of the confirmation bar).
  final DateTime? time;
  final DateTime? confirmationBarTime;
  final String? direction;
  final String? setupType;
  final String? reason;
  final String? reasonFa;

  /// The numbers behind the reason (fill price, SL, sizing message, ...).
  final String? detail;

  factory BacktestSkippedCandidate.fromReader(JsonReader r) => BacktestSkippedCandidate(
        windowIndex: r.intOrNull('window_index') ?? 0,
        time: r.utcOrNull('time'),
        confirmationBarTime: r.utcOrNull('confirmation_bar_time'),
        direction: r.strOrNull('direction'),
        setupType: r.strOrNull('setup_type'),
        reason: r.strOrNull('reason'),
        reasonFa: r.strOrNull('reason_fa'),
        detail: r.strOrNull('detail'),
      );
}

/// `GET /backtests/{id}/skipped` -> `{run_id, window, count, skipped}`.
@immutable
class BacktestSkippedList {
  const BacktestSkippedList({required this.runId, required this.skipped, this.window});

  final int runId;
  final int? window;
  final List<BacktestSkippedCandidate> skipped;

  factory BacktestSkippedList.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'backtest_skipped');
    return BacktestSkippedList(
      runId: r.integer('run_id'),
      window: r.intOrNull('window'),
      skipped: r.objects('skipped', BacktestSkippedCandidate.fromReader),
    );
  }
}
