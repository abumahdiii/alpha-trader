import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../chart/chart_data_source.dart';
import '../../core/dev_mode.dart';
import '../../providers/shell_navigation.dart';
import '../../services/backtest_exporter.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/engine_gate.dart';
import 'backtest_controller.dart';
import 'backtest_form.dart';
import 'backtest_progress_panel.dart';
import 'backtest_result_view.dart';
import 'backtest_runs_list.dart';

/// «بک‌تست»: a side panel with the run form and the previous runs, the live
/// progress of the followed run, and the opened run's result.
///
/// Shown only while the engine runs ([EngineGate]); an engine restart
/// recreates the page, which then finds a still-running job again through
/// `GET /backtests`. A pending [ShellNavigation] prefill (chart «بک‌تست همین
/// بازه») fills the form and may run it.
class BacktestScreen extends StatelessWidget {
  const BacktestScreen({super.key, this.watcherFactory, this.chartSource, this.exportSaver});

  /// Progress watcher of a run (tests inject a fake socket).
  final BacktestWatcherFactory? watcherFactory;

  /// Bars of the result's «نمودار» tab (tests inject a fake); null = the engine.
  final ChartDataSource? chartSource;

  /// Save dialog + file write of the «خروجی» menu (tests inject a fake); null = the native dialog.
  final ExportFileSaver? exportSaver;

  @override
  Widget build(BuildContext context) => EngineGate(
        builder: (BuildContext context, EngineApi api) => BacktestPage(
          api: api,
          watcherFactory: watcherFactory,
          chartSource: chartSource,
          exportSaver: exportSaver,
        ),
      );
}

class BacktestPage extends StatefulWidget {
  const BacktestPage({super.key, required this.api, this.watcherFactory, this.chartSource, this.exportSaver});

  final EngineApi api;
  final BacktestWatcherFactory? watcherFactory;
  final ChartDataSource? chartSource;
  final ExportFileSaver? exportSaver;

  static const String formTab = 'اجرای جدید';
  static const String runsTab = 'اجراهای قبلی';

  @override
  State<BacktestPage> createState() => _BacktestPageState();
}

class _BacktestPageState extends State<BacktestPage> {
  late final BacktestController _controller =
      BacktestController(api: widget.api, watcherFactory: widget.watcherFactory);

  /// One saver per page: it remembers the last save folder of the session.
  late final ExportFileSaver _exportSaver = widget.exportSaver ?? FileSelectorExportSaver();
  ShellNavigation? _navigation;
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    devLog('[BacktestScreen] engine API :${widget.api.port} -> loading');
    _navigation = context.read<ShellNavigation>()..addListener(_consumePrefill);
    unawaited(_init());
  }

  Future<void> _init() async {
    await _controller.init();
    if (!mounted) return;
    _ready = true;
    _consumePrefill();
  }

  /// Applies a pending prefill once the symbols are loaded.
  void _consumePrefill() {
    if (!_ready || !mounted) return;
    final ShellNavigation? nav = _navigation;
    if (nav == null || nav.pendingBacktest == null) return;
    final prefill = nav.takeBacktestPrefill();
    if (prefill == null) return;
    devLog('[BacktestScreen] applying $prefill');
    unawaited(_controller.applyPrefill(prefill));
  }

  @override
  void dispose() {
    _navigation?.removeListener(_consumePrefill);
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _controller,
      builder: (BuildContext context, _) => Row(children: [
        SizedBox(
          width: 400,
          child: DefaultTabController(
            length: 2,
            child: Column(children: [
              TabBar(
                labelColor: Theme.of(context).colorScheme.primary,
                unselectedLabelColor: context.appColors.mutedText,
                tabs: const [
                  Tab(key: ValueKey<String>('bt-side-form'), text: BacktestPage.formTab),
                  Tab(key: ValueKey<String>('bt-side-runs'), text: BacktestPage.runsTab),
                ],
              ),
              Expanded(
                child: TabBarView(children: [
                  BacktestFormPanel(controller: _controller),
                  BacktestRunsList(controller: _controller),
                ]),
              ),
            ]),
          ),
        ),
        const VerticalDivider(width: 1),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            BacktestProgressPanel(controller: _controller),
            Expanded(
              child: BacktestResultView(
                controller: _controller,
                chartSource: widget.chartSource,
                exportSaver: _exportSaver,
              ),
            ),
          ]),
        ),
      ]),
    );
  }
}
