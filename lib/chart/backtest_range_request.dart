import 'package:flutter/foundation.dart';

import '../models/chart_models.dart';

/// «بک‌تست همین بازه»: run a manual backtest of the chart range. [from] /
/// [to] are the engine's `backtest_window.from/to` exactly as returned by
/// `/chart/setups` (the manual window `[from, to)` of the bars whose
/// decisions fall in the chart range, already clipped and validated by the
/// engine) — never recomputed here.
@immutable
class BacktestRangeRequest {
  const BacktestRangeRequest({required this.symbol, required this.from, required this.to, this.strategy});

  final String symbol;

  /// UTC, inclusive.
  final DateTime from;

  /// UTC, exclusive.
  final DateTime to;

  /// The strategy whose setups (and backtest window) the chart shows.
  final String? strategy;

  /// The request for [setups], or null when its window is not available.
  static BacktestRangeRequest? of(SetupsResult? setups) {
    final BacktestWindow? w = setups?.backtestWindow;
    final DateTime? from = w?.from;
    final DateTime? to = w?.to;
    if (setups == null || w == null || !w.available || from == null || to == null) return null;
    return BacktestRangeRequest(symbol: setups.symbol, from: from, to: to, strategy: setups.provenance.strategy);
  }

  @override
  bool operator ==(Object other) =>
      other is BacktestRangeRequest &&
      other.symbol == symbol &&
      other.from == from &&
      other.to == to &&
      other.strategy == strategy;

  @override
  int get hashCode => Object.hash(symbol, from, to, strategy);

  @override
  String toString() =>
      'BacktestRangeRequest($symbol ${from.toIso8601String()} .. ${to.toIso8601String()} strategy=${strategy ?? '-'})';
}
