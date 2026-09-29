import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../chart/backtest_range_request.dart';
import '../../chart/chart_controller.dart';
import '../../chart/chart_data_source.dart';
import '../../chart/chart_view.dart';
import '../../core/dev_mode.dart';
import '../../providers/shell_navigation.dart';
import '../../services/engine_api.dart';
import '../../widgets/engine_gate.dart';

/// «چارت»: candlestick chart with the StdDev channel, setups and the data
/// info panel. Shown only while the engine runs ([EngineGate]); a new
/// [EngineApi] (engine restart) recreates the page state and reloads.
class ChartScreen extends StatelessWidget {
  const ChartScreen({super.key});

  @override
  Widget build(BuildContext context) =>
      EngineGate(builder: (BuildContext context, EngineApi api) => _ChartPage(api: api));
}

class _ChartPage extends StatefulWidget {
  const _ChartPage({required this.api});

  final EngineApi api;

  @override
  State<_ChartPage> createState() => _ChartPageState();
}

class _ChartPageState extends State<_ChartPage> {
  late final ChartController _controller = ChartController(source: EngineChartDataSource(widget.api));

  @override
  void initState() {
    super.initState();
    devLog('[ChartScreen] engine API :${widget.api.port} -> loading chart');
    unawaited(_controller.init());
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  /// «بک‌تست همین بازه»: the backtest page, prefilled with the symbol, the
  /// engine's backtest_window (exactly as received) and the strategy of the
  /// shown setups, runs it at once.
  void _openBacktest(BacktestRangeRequest r) {
    final String strategy = r.strategy ?? _controller.strategy;
    devLog('[ChartScreen] backtest this range -> $r (strategy $strategy)');
    context
        .read<ShellNavigation>()
        .openBacktest(symbol: r.symbol, from: r.from, to: r.to, strategy: strategy, autoRun: true);
  }

  @override
  Widget build(BuildContext context) => ChartView(controller: _controller, onBacktestRange: _openBacktest);
}
