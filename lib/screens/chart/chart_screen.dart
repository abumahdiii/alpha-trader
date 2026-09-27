import 'dart:async';

import 'package:flutter/material.dart';

import '../../chart/chart_controller.dart';
import '../../chart/chart_data_source.dart';
import '../../chart/chart_view.dart';
import '../../core/dev_mode.dart';
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

  @override
  Widget build(BuildContext context) => ChartView(controller: _controller);
}
