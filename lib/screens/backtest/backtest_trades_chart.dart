import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../../chart/chart_controller.dart';
import '../../chart/chart_data.dart';
import '../../chart/chart_data_source.dart';
import '../../chart/chart_view.dart';
import '../../chart/widgets/setups_pane.dart';
import '../../core/dev_mode.dart';
import '../../models/backtest_models.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/status_message.dart';
import 'backtest_format.dart';
import 'backtest_tables.dart';
import 'metric_cards.dart';

/// Longest chart range the tab opens with (a 5-year run starts on its first
/// weeks; a row click reloads around any other trade).
const Duration kBacktestChartInitialWindow = Duration(days: 60);

/// The chart range the «نمودار» tab opens with: the run's (or the selected
/// window's) period start, at most [kBacktestChartInitialWindow] long.
/// Without a period: around the first trade; without trades: the engine's
/// default range (null, null).
(DateTime?, DateTime?) backtestChartInitialRange(DateTime? start, DateTime? end, List<BacktestTrade> trades) {
  DateTime? from = start?.toUtc();
  if (from == null) {
    final DateTime? first = trades.isEmpty ? null : trades.first.entryTime;
    if (first == null) return (null, null);
    from = first.toUtc().subtract(const Duration(days: 3));
  }
  final DateTime cap = from.add(kBacktestChartInitialWindow);
  final DateTime? e = end?.toUtc();
  return (from, e != null && e.isBefore(cap) ? e : cap);
}

/// «نمودار» tab of a backtest result: the phase 3 candlestick chart of the
/// run's symbol with the run's trades drawn on it (engine values only), and
/// the trades table below it. A row click zooms onto that trade; a click on
/// a trade marker opens its details.
///
/// Owns its [ChartController] (no coupling of the chart page to backtest
/// state); kept alive while the user switches tabs.
class BacktestTradesChart extends StatefulWidget {
  const BacktestTradesChart({
    super.key,
    required this.source,
    required this.runId,
    required this.symbol,
    required this.trades,
    this.periodStart,
    this.periodEnd,
    this.digits,
    this.showWindow = false,
  });

  final ChartDataSource source;
  final int runId;
  final String symbol;

  /// The trades to draw (a random run: those of the selected window, or all).
  final List<BacktestTrade> trades;

  /// Period of the run / selected window (UTC); the initial chart range.
  final DateTime? periodStart;
  final DateTime? periodEnd;
  final int? digits;

  /// Random runs: the table's window column.
  final bool showWindow;

  @override
  State<BacktestTradesChart> createState() => BacktestTradesChartState();
}

class BacktestTradesChartState extends State<BacktestTradesChart> with AutomaticKeepAliveClientMixin {
  late final ChartController _chart = ChartController(source: widget.source, showSetups: false);

  /// The chart's controller (read by tests).
  ChartController get chart => _chart;

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    _chart.setTradeOverlay(TradeOverlay(runId: widget.runId, trades: widget.trades));
    unawaited(_open());
  }

  Future<void> _open() {
    final (DateTime? from, DateTime? to) =
        backtestChartInitialRange(widget.periodStart, widget.periodEnd, widget.trades);
    devLog('[BacktestChart] run #${widget.runId} ${widget.symbol}: ${widget.trades.length} trades, '
        'initial range ${from?.toIso8601String() ?? 'default'} .. ${to?.toIso8601String() ?? 'default'}');
    return _chart.openSeries(symbol: widget.symbol, from: from, to: to);
  }

  @override
  void didUpdateWidget(BacktestTradesChart oldWidget) {
    super.didUpdateWidget(oldWidget);
    final BacktestTradesChart old = oldWidget;
    // A random run's window filter builds a new list on every rebuild: compare the trades themselves.
    if (!listEquals(old.trades, widget.trades) || old.runId != widget.runId) {
      devLog('[BacktestChart] trades changed: ${old.trades.length} -> ${widget.trades.length}');
      _chart.setTradeOverlay(TradeOverlay(runId: widget.runId, trades: widget.trades));
    }
    if (old.periodStart != widget.periodStart || old.periodEnd != widget.periodEnd) {
      // Another window of a random run: show its start.
      final (DateTime? from, DateTime? to) =
          backtestChartInitialRange(widget.periodStart, widget.periodEnd, widget.trades);
      devLog('[BacktestChart] period changed -> ${from?.toIso8601String()} .. ${to?.toIso8601String()}');
      _chart.setRange(from: from, to: to);
      unawaited(_chart.load());
    }
  }

  @override
  void dispose() {
    _chart.dispose();
    super.dispose();
  }

  int? get _digits => widget.digits ?? _chart.data?.digits;

  void _openTrade(TradeMark m) {
    devLog('[BacktestChart] trade ${m.key} details');
    unawaited(showTradeDetails(context, m.trade, _digits));
  }

  void _focus(BacktestTrade t) {
    devLog('[BacktestChart] row ${tradeKeyOf(t)} -> zoom');
    unawaited(_chart.focusTrade(t));
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    return ChartView(
      key: ValueKey<String>('bt-chart-${widget.runId}'),
      controller: _chart,
      symbolLocked: true,
      initialShowDataInfo: false,
      onTradeTap: _openTrade,
      bottomPane: (BuildContext context, bool collapsed, VoidCallback onToggle) => _TradesPane(
        controller: _chart,
        trades: widget.trades,
        digits: _digits,
        showWindow: widget.showWindow,
        collapsed: collapsed,
        onToggleCollapsed: onToggle,
        onRowTap: _focus,
      ),
    );
  }
}

/// The chart tab's bottom pane: header (counts, collapse) and the trades table.
class _TradesPane extends StatelessWidget {
  const _TradesPane({
    required this.controller,
    required this.trades,
    required this.digits,
    required this.showWindow,
    required this.collapsed,
    required this.onToggleCollapsed,
    required this.onRowTap,
  });

  final ChartController controller;
  final List<BacktestTrade> trades;
  final int? digits;
  final bool showWindow;
  final bool collapsed;
  final VoidCallback onToggleCollapsed;
  final ValueChanged<BacktestTrade> onRowTap;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final ChartData? data = controller.data;
    final TradeKey? selected = controller.selectedTradeKey;
    return Material(
      key: const ValueKey<String>('bt-chart-trades-pane'),
      color: Theme.of(context).colorScheme.surface,
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Container(
          height: SetupsPane.headerHeight,
          padding: const EdgeInsetsDirectional.only(start: 8, end: 4),
          decoration: BoxDecoration(border: Border(bottom: BorderSide(color: colors.borderColor))),
          child: Row(children: [
            Text('معاملات بک‌تست', style: tt.titleSmall),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                '${fmtInt(trades.length)} معامله'
                '${data == null ? '' : '؛ ${fmtInt(data.trades.length)} در بازه نمودار'}'
                ' — کلیک روی ردیف: زوم روی معامله؛ کلیک روی نشانگر: جزئیات',
                key: const ValueKey<String>('bt-chart-trades-count'),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: tt.bodySmall?.copyWith(color: colors.mutedText),
              ),
            ),
            _Legend(),
            IconButton(
              key: const ValueKey<String>('bt-chart-trades-toggle'),
              tooltip: collapsed ? 'باز کردن جدول معاملات' : 'جمع کردن جدول معاملات',
              onPressed: onToggleCollapsed,
              icon: Icon(collapsed ? Icons.expand_less : Icons.expand_more),
            ),
          ]),
        ),
        if (!collapsed)
          Expanded(
            child: trades.isEmpty
                ? const StatusMessage(
                    key: ValueKey<String>('bt-chart-trades-empty'),
                    icon: Icons.inbox_outlined,
                    title: kZeroTradesFa,
                  )
                : BacktestTradesTable(
                    keyPrefix: 'bt-chart-trades',
                    trades: trades,
                    digits: digits,
                    showWindow: showWindow,
                    onRowTap: onRowTap,
                    isSelected: (BacktestTrade t) => tradeKeyOf(t) == selected,
                  ),
          ),
      ]),
    );
  }
}

/// What the exit markers mean (colors from the chart style's semantics).
class _Legend extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    Widget item(IconData icon, Color color, String label) => Padding(
          padding: const EdgeInsetsDirectional.only(end: 10),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(icon, size: 12, color: color),
            const SizedBox(width: 3),
            Text(label, style: TextStyle(fontSize: 11, color: c.mutedText)),
          ]),
        );
    return Tooltip(
      message: 'فلش: ورود (خرید سبز، فروش قرمز) — خط‌ها: حد ضرر و حد سود تا کندل خروج — '
          'دایره: خروج با حد سود/ضرر، لوزی: خروج با گپ، مربع: بسته شدن در پایان بازه',
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        item(Icons.circle, c.success, 'حد سود'),
        item(Icons.circle, c.error, 'حد ضرر'),
        item(Icons.square, c.mutedText, 'پایان بازه'),
      ]),
    );
  }
}
