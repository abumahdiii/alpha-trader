import 'package:flutter/foundation.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';

/// The shell's destinations, in NavigationRail order.
enum ShellPage { chart, systems, backtest, signal, settings }

/// A request to open the backtest page with a symbol and a manual period
/// (e.g. «بک‌تست همین بازه» from the chart). Consumed once by the page.
@immutable
class BacktestPrefill {
  const BacktestPrefill({required this.symbol, this.from, this.to, this.autoRun = false});

  final String symbol;

  /// Manual period `[from, to)` (UTC instants); null keeps the form's value.
  final DateTime? from;
  final DateTime? to;

  /// Submit right away once the form is filled.
  final bool autoRun;

  @override
  String toString() => 'BacktestPrefill($symbol ${from?.toUtc().toIso8601String()} .. '
      '${to?.toUtc().toIso8601String()} autoRun=$autoRun)';
}

/// Which shell page is shown, plus cross-page requests (a pending backtest
/// prefill). Registered in `main.dart`; the shell rail and any page can
/// switch pages through it.
class ShellNavigation extends ChangeNotifier {
  ShellNavigation({ShellPage initial = ShellPage.chart}) : _page = initial;

  ShellPage _page;
  BacktestPrefill? _pendingBacktest;

  ShellPage get page => _page;

  /// A prefill the backtest page has not consumed yet.
  BacktestPrefill? get pendingBacktest => _pendingBacktest;

  void select(ShellPage page) {
    if (page == _page) return;
    _log('[ShellNavigation] ${_page.name} -> ${page.name}');
    _page = page;
    notifyListeners();
  }

  /// Opens the backtest page with [symbol] and the manual period
  /// `[from, to)`; with [autoRun] the page submits it at once.
  void openBacktest({required String symbol, DateTime? from, DateTime? to, bool autoRun = false}) {
    _pendingBacktest = BacktestPrefill(symbol: symbol, from: from, to: to, autoRun: autoRun);
    _log('[ShellNavigation] openBacktest $_pendingBacktest');
    _page = ShellPage.backtest;
    notifyListeners();
  }

  /// Hands the pending prefill to the backtest page (once).
  BacktestPrefill? takeBacktestPrefill() {
    final BacktestPrefill? p = _pendingBacktest;
    _pendingBacktest = null;
    if (p != null) _log('[ShellNavigation] backtest prefill consumed');
    return p;
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog(message);
    AppLogger.log(message);
  }
}
