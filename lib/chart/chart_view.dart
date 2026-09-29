import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/dev_mode.dart';
import '../models/market_data.dart';
import '../models/strategy.dart';
import '../theme/app_semantic_colors.dart';
import '../widgets/strategy_selector.dart';
import 'backtest_range_request.dart';
import 'chart_controller.dart';
import 'chart_data.dart';
import 'chart_format.dart';
import '../models/chart_models.dart';
import 'widgets/candle_info_panel.dart';
import 'widgets/chart_canvas.dart';
import 'widgets/data_info_panel.dart';
import 'widgets/setup_widgets.dart';
import 'widgets/setups_pane.dart';

/// The whole chart page body: toolbar, candlestick canvas with the candle
/// info panel (opened by a click on a candle), the «اطلاعات داده» side
/// panel, and a resizable / collapsible bottom pane with the range summary
/// and the full setups table.
/// Everything is read from [controller]; nothing is computed here.
///
/// Embedded (the backtest result's «نمودار» tab): [bottomPane] replaces the
/// setups pane, [symbolLocked] fixes the series' symbol and [onTradeTap]
/// opens a backtest trade clicked on the chart.
class ChartView extends StatefulWidget {
  const ChartView({
    super.key,
    required this.controller,
    this.onBacktestRange,
    this.bottomPane,
    this.onTradeTap,
    this.symbolLocked = false,
    this.initialShowDataInfo = true,
  });

  final ChartController controller;

  /// «بک‌تست همین بازه» (the engine's `backtest_window` of the loaded range).
  /// null = the button stays disabled.
  final ValueChanged<BacktestRangeRequest>? onBacktestRange;

  /// Builds the resizable bottom pane instead of the setups pane. It must
  /// show a [SetupsPane.headerHeight] header with the collapse toggle.
  final ChartBottomPaneBuilder? bottomPane;

  /// A click on a backtest trade marker (after the controller selected it).
  final ValueChanged<TradeMark>? onTradeTap;

  /// The symbol dropdown is shown but cannot change.
  final bool symbolLocked;
  final bool initialShowDataInfo;

  @override
  State<ChartView> createState() => _ChartViewState();
}

/// Bottom pane of an embedded [ChartView]: [collapsed] shows only its header.
typedef ChartBottomPaneBuilder = Widget Function(BuildContext context, bool collapsed, VoidCallback onToggleCollapsed);

/// The canvas and the candle panel form one tap region: a click on either
/// is routed by the canvas / panel itself; any other click closes the panel.
const Object _kCandlePanelTapGroup = #chartCandlePanel;

class _ChartViewState extends State<ChartView> {
  final FocusNode _chartFocus = FocusNode(debugLabel: 'chart-area', skipTraversal: true);

  static const double _minChartHeight = 180;
  static const double _minPaneHeight = 150;
  static const double _handleHeight = 6;

  /// Share of the height the setups pane takes until the user drags it.
  static const double _defaultPaneFraction = 0.45;

  /// Dragged pane height; null = [_defaultPaneFraction].
  double? _paneHeight;
  bool _paneCollapsed = false;
  late bool _showDataInfo = widget.initialShowDataInfo;

  @override
  void dispose() {
    _chartFocus.dispose();
    super.dispose();
  }

  void _onCandleTap(int index) {
    widget.controller.inspectCandle(index);
    // Esc closes the panel only while the chart area has the focus.
    _chartFocus.requestFocus();
  }

  Future<void> _openSetup(SetupMark m) async {
    final ChartController c = widget.controller;
    c.selectSetup(m.item.id);
    await SetupDetails.show(
      context,
      m.item,
      digits: c.data?.digits,
      endOfDataLabelFa: c.data?.setupsResult?.evaluation?.endOfDataLabelFa,
    );
  }

  void _openTrade(TradeMark m) {
    devLog('[Chart] trade marker ${m.key} tapped -> details');
    widget.controller.selectTrade(m.key);
    widget.onTradeTap?.call(m);
  }

  void _togglePane() {
    setState(() => _paneCollapsed = !_paneCollapsed);
    devLog('[Chart] setups pane ${_paneCollapsed ? 'collapsed' : 'expanded'}');
  }

  void _toggleDataInfo() {
    setState(() => _showDataInfo = !_showDataInfo);
    devLog('[Chart] data info panel ${_showDataInfo ? 'shown' : 'hidden'}');
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.controller,
      builder: (BuildContext context, _) {
        final ChartController c = widget.controller;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            _ChartToolbar(
              controller: c,
              showDataInfo: _showDataInfo,
              onToggleDataInfo: _toggleDataInfo,
              symbolLocked: widget.symbolLocked,
            ),
            if (c.isLoading) const LinearProgressIndicator(minHeight: 2) else const SizedBox(height: 2),
            Expanded(child: LayoutBuilder(builder: (BuildContext context, BoxConstraints box) => _body(c, box))),
            _StatusLine(controller: c),
          ],
        );
      },
    );
  }

  /// Chart (+ data info) above, drag handle, setups pane below.
  Widget _body(ChartController c, BoxConstraints box) {
    final double total = box.maxHeight;
    // Never squeeze the chart below its minimum (a short window shrinks the pane first).
    final double maxPane =
        (total - _minChartHeight - _handleHeight).clamp(SetupsPane.headerHeight, double.infinity).toDouble();
    final double minPane = _minPaneHeight < maxPane ? _minPaneHeight : maxPane;
    final double pane = _paneCollapsed
        ? SetupsPane.headerHeight
        : (_paneHeight ?? total * _defaultPaneFraction).clamp(minPane, maxPane).toDouble();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Expanded(
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              Expanded(child: _chartArea(context, c)),
              if (_showDataInfo) ...<Widget>[
                const VerticalDivider(width: 1),
                SizedBox(width: 340, child: _DataInfoSide(controller: c)),
              ],
            ],
          ),
        ),
        _PaneHandle(
          height: _handleHeight,
          enabled: !_paneCollapsed,
          onDrag: (double dy) => setState(() => _paneHeight = (pane - dy).clamp(minPane, maxPane).toDouble()),
        ),
        SizedBox(
          height: pane,
          child: widget.bottomPane?.call(context, _paneCollapsed, _togglePane) ??
              SetupsPane(
                controller: c,
                collapsed: _paneCollapsed,
                onToggleCollapsed: _togglePane,
                onBacktestRange: widget.onBacktestRange,
              ),
        ),
      ],
    );
  }

  Widget _chartArea(BuildContext context, ChartController c) {
    final ChartData? data = c.data;
    final bool showData = data != null && !data.isEmpty && c.state != ChartLoadState.error;
    final int? inspected = c.inspectedIndex;
    final bool showPanel = showData && inspected != null && inspected < data.length;
    return CallbackShortcuts(
      bindings: <ShortcutActivator, VoidCallback>{
        const SingleActivator(LogicalKeyboardKey.escape): () => c.clearInspection(reason: 'Esc'),
      },
      child: Focus(
        focusNode: _chartFocus,
        child: Stack(
          fit: StackFit.expand,
          children: <Widget>[
            if (showData)
              TapRegion(
                groupId: _kCandlePanelTapGroup,
                child: ChartCanvas(
                  key: const ValueKey<String>('chart-canvas'),
                  data: data,
                  viewRequest: c.viewRequest,
                  selectedSetupId: c.selectedSetupId,
                  selectedTradeKey: c.selectedTradeKey,
                  highlightIndex: c.highlightIndex,
                  onSetupTap: _openSetup,
                  onTradeTap: _openTrade,
                  onCandleTap: _onCandleTap,
                  onEmptyTap: () => c.clearInspection(reason: 'empty chart tap'),
                ),
              ),
            if (showPanel)
              // Physically top-left: the newest bars and the price axis are on the right.
              Positioned(
                top: 8,
                left: 8,
                child: TapRegion(
                  groupId: _kCandlePanelTapGroup,
                  onTapOutside: (_) => c.clearInspection(reason: 'tap outside the chart'),
                  child: CandleInfoPanel(
                    data: data,
                    index: inspected,
                    noChannelFa: c.hasChannel ? null : kNoChannelFa,
                    onClose: () => c.clearInspection(reason: 'close button'),
                  ),
                ),
              ),
            if (!showData) _StateMessage(controller: c),
          ],
        ),
      ),
    );
  }
}

class _StateMessage extends StatelessWidget {
  const _StateMessage({required this.controller});

  final ChartController controller;

  @override
  Widget build(BuildContext context) {
    final ChartController c = controller;
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final (IconData icon, Color color, String text) = switch (c.state) {
      ChartLoadState.idle || ChartLoadState.loading => (Icons.hourglass_empty, colors.mutedText, 'در حال دریافت داده…'),
      ChartLoadState.error when c.engineUnavailable => (
          Icons.power_off,
          colors.warning,
          c.messageFa ?? kEngineUnavailableFa
        ),
      ChartLoadState.error => (Icons.error_outline, colors.error, c.messageFa ?? kUnexpectedErrorFa),
      _ => (Icons.candlestick_chart_outlined, colors.mutedText, c.messageFa ?? kEmptyRangeFa),
    };
    return Center(
      key: const ValueKey<String>('chart-state-message'),
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(mainAxisSize: MainAxisSize.min, children: <Widget>[
          if (c.isLoading) const CircularProgressIndicator() else Icon(icon, size: 48, color: color),
          const SizedBox(height: 12),
          Text(text, textAlign: TextAlign.center, style: tt.bodyLarge?.copyWith(color: color)),
          if (c.state == ChartLoadState.error) ...<Widget>[
            const SizedBox(height: 12),
            OutlinedButton.icon(
              onPressed: c.symbol == null ? c.init : c.load,
              icon: const Icon(Icons.refresh),
              label: const Text('تلاش دوباره'),
            ),
          ],
        ]),
      ),
    );
  }
}

class _ChartToolbar extends StatelessWidget {
  const _ChartToolbar({
    required this.controller,
    required this.showDataInfo,
    required this.onToggleDataInfo,
    this.symbolLocked = false,
  });

  final ChartController controller;
  final bool showDataInfo;
  final VoidCallback onToggleDataInfo;
  final bool symbolLocked;

  Future<void> _pickDay(BuildContext context, {required bool isFrom}) async {
    final ChartController c = controller;
    final DateTime now = DateTime.now().toUtc();
    final DateTime initial = (isFrom ? c.from : c.to) ?? now;
    final DateTime? picked = await showDatePicker(
      context: context,
      initialDate: DateTime(initial.year, initial.month, initial.day),
      firstDate: DateTime(2000),
      lastDate: DateTime(now.year + 1, 12, 31),
      helpText: isFrom ? 'شروع بازه (UTC)' : 'پایان بازه (UTC)',
    );
    if (picked == null) return;
    if (isFrom) {
      c.setRangeDays(fromDay: picked);
    } else {
      c.setRangeDays(toDay: picked);
    }
  }

  @override
  Widget build(BuildContext context) {
    final ChartController c = controller;
    final bool busy = c.isLoading;
    final String? rangeError = c.rangeErrorFa;
    // Highlight only an actionable button (an invalid range disables it).
    final bool dirty = c.rangeDirty && rangeError == null;
    final VoidCallback? onLoad = busy || c.symbol == null || rangeError != null ? null : c.load;
    const Icon loadIcon = Icon(Icons.refresh, size: 18);
    const Text loadLabel = Text('به‌روزرسانی نمودار');
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
      child: Wrap(
        spacing: 8,
        runSpacing: 6,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: <Widget>[
          DropdownButton<String>(
            key: const ValueKey<String>('chart-symbol'),
            value: c.symbol,
            hint: const Text('نماد'),
            items: <DropdownMenuItem<String>>[
              for (final SymbolItem s in c.symbols)
                DropdownMenuItem<String>(value: s.symbol, child: Text(s.symbol, textDirection: TextDirection.ltr)),
            ],
            onChanged: symbolLocked
                ? null
                : (String? s) {
                    if (s != null) c.setSymbol(s);
                  },
            // A locked symbol stays readable (the default disabled hint would hide it).
            disabledHint: c.symbol == null ? null : Text(c.symbol!, textDirection: TextDirection.ltr),
          ),
          if (c.strategies.isNotEmpty)
            StrategySelector(
              key: const ValueKey<String>('chart-strategy'),
              options: c.strategies,
              value: c.strategy,
              enabled: !busy,
              onChanged: (String name) => unawaited(c.setStrategy(name)),
            ),
          if (!c.hasChannel)
            Tooltip(
              message: 'فقط سیستم کانال انحراف معیار خطوط کانال دارد؛ برای این سیستم فقط ستاپ‌ها نمایش داده می‌شوند.',
              child: Text(
                kNoChannelFa,
                key: const ValueKey<String>('chart-no-channel'),
                style: Theme.of(context).textTheme.bodySmall?.copyWith(color: context.appColors.mutedText),
              ),
            ),
          ToggleButtons(
            key: const ValueKey<String>('chart-timeframe'),
            constraints: const BoxConstraints(minHeight: 34, minWidth: 48),
            isSelected: <bool>[for (final ChartTimeframe t in ChartTimeframe.values) t == c.timeframe],
            onPressed: (int i) => c.setTimeframe(ChartTimeframe.values[i]),
            children: <Widget>[for (final ChartTimeframe t in ChartTimeframe.values) Text(t.code)],
          ),
          OutlinedButton.icon(
            key: const ValueKey<String>('chart-from'),
            onPressed: () => _pickDay(context, isFrom: true),
            icon: const Icon(Icons.event, size: 18),
            label: Text('از ${c.from == null ? '—' : formatDate(c.from!)}'),
          ),
          OutlinedButton.icon(
            key: const ValueKey<String>('chart-to'),
            onPressed: () => _pickDay(context, isFrom: false),
            icon: const Icon(Icons.event, size: 18),
            label: Text('تا ${c.to == null ? '—' : formatDate(c.to!)}'),
          ),
          if (dirty)
            Tooltip(
              key: const ValueKey<String>('chart-range-dirty'),
              message: 'بازه تغییر کرده؛ برای دیدن نمودار جدید به‌روزرسانی کنید',
              child: Badge(
                smallSize: 8,
                child: FilledButton.icon(
                  key: const ValueKey<String>('chart-load'),
                  onPressed: onLoad,
                  icon: loadIcon,
                  label: loadLabel,
                ),
              ),
            )
          else
            ElevatedButton.icon(
              key: const ValueKey<String>('chart-load'),
              onPressed: onLoad,
              icon: loadIcon,
              label: loadLabel,
            ),
          if (rangeError != null)
            Text(
              rangeError,
              key: const ValueKey<String>('chart-range-error'),
              style: Theme.of(context).textTheme.bodySmall?.copyWith(color: context.appColors.error),
            ),
          IconButton(
            key: const ValueKey<String>('chart-reset-zoom'),
            tooltip: 'بازنشانی زوم (آخرین کندل‌ها)',
            onPressed: c.data == null ? null : c.resetView,
            icon: const Icon(Icons.fit_screen),
          ),
          IconButton(
            key: const ValueKey<String>('chart-toggle-data-info'),
            tooltip: showDataInfo ? 'بستن پنل اطلاعات داده' : 'نمایش پنل اطلاعات داده',
            isSelected: showDataInfo,
            onPressed: onToggleDataInfo,
            icon: const Icon(Icons.dataset_outlined),
          ),
          Tooltip(
            message: c.tradeOverlay != null
                ? 'چرخ ماوس: زوم روی نشانگر — Shift + چرخ یا کشیدن: جابه‌جایی — کلیک روی فلش ورود یا نشانگر خروج: '
                    'جزئیات معامله — کلیک روی کندل: اطلاعات کندل (Esc: بستن) — کلیک روی ردیف جدول معاملات: زوم روی '
                    'آن معامله؛ مرز بالای جدول را برای تغییر اندازه بکشید'
                : 'چرخ ماوس: زوم روی نشانگر — Shift + چرخ یا کشیدن: جابه‌جایی — کلیک روی فلش: جزئیات ستاپ — '
                    'کلیک روی کندل: اطلاعات کندل (Esc: بستن) — کلیک روی ردیف جدول ستاپ‌ها: رفتن به آن ستاپ؛ '
                    'مرز بالای جدول را برای تغییر اندازه بکشید',
            child: Icon(Icons.help_outline, size: 20, color: context.appColors.mutedText),
          ),
        ],
      ),
    );
  }
}

/// «اطلاعات داده» beside the chart (the setups moved to the bottom pane).
class _DataInfoSide extends StatelessWidget {
  const _DataInfoSide({required this.controller});

  final ChartController controller;

  @override
  Widget build(BuildContext context) {
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: <Widget>[
      Container(
        height: 36,
        padding: const EdgeInsets.symmetric(horizontal: 12),
        alignment: AlignmentDirectional.centerStart,
        decoration: BoxDecoration(border: Border(bottom: BorderSide(color: context.appColors.borderColor))),
        child: Text('اطلاعات داده', style: Theme.of(context).textTheme.titleSmall),
      ),
      Expanded(child: DataInfoPanel(controller: controller)),
    ]);
  }
}

/// Drag handle between the chart and the setups pane.
class _PaneHandle extends StatelessWidget {
  const _PaneHandle({required this.height, required this.enabled, required this.onDrag});

  final double height;
  final bool enabled;
  final ValueChanged<double> onDrag;

  @override
  Widget build(BuildContext context) {
    final Widget bar = Container(
      height: height,
      color: context.appColors.surfaceMuted,
      alignment: Alignment.center,
      child: Container(
        width: 40,
        height: 2,
        decoration: BoxDecoration(color: context.appColors.borderColor, borderRadius: BorderRadius.circular(1)),
      ),
    );
    if (!enabled) return bar;
    return MouseRegion(
      cursor: SystemMouseCursors.resizeRow,
      child: GestureDetector(
        key: const ValueKey<String>('setups-pane-handle'),
        behavior: HitTestBehavior.opaque,
        onVerticalDragUpdate: (DragUpdateDetails d) => onDrag(d.delta.dy),
        child: bar,
      ),
    );
  }
}

class _StatusLine extends StatelessWidget {
  const _StatusLine({required this.controller});

  final ChartController controller;

  @override
  Widget build(BuildContext context) {
    final ChartData? d = controller.data;
    final AppSemanticColors colors = context.appColors;
    final bool trades = controller.tradeOverlay != null;
    final List<String> parts = <String>[
      'زمان‌های محور و پنل کندل: UTC — جدول ${trades ? 'معاملات' : 'ستاپ‌ها'}: وقت محلی (UTC در راهنمای هر خانه)'
    ];
    if (d != null) {
      parts.add('${d.length} کندل');
      final TradeOverlay? o = d.tradeOverlay;
      if (o != null) parts.add('${d.trades.length} معامله از ${o.trades.length} در این بازه');
      final StrategyProvenance? p = d.channelResult?.provenance ?? d.setupsResult?.provenance;
      if (p != null) {
        parts.add('${p.strategy} v${p.strategyVersion}'
            '${p.strategySource == StrategySource.plugin ? ' (پلاگین${p.strategySha256 == null ? '' : '، ${shortSha(p.strategySha256!)}'})' : ''}'
            ' — پارامترها: نسخه ${p.paramsVersion}');
      }
      if (d.rates.stale) parts.add('MT5 در دسترس نبود؛ ممکن است کندل‌های اخیر در کش نباشند');
      final String? msg = d.channelResult?.messageFa ?? d.setupsResult?.messageFa;
      if (msg != null) parts.add(msg);
    }
    return Container(
      color: colors.surfaceMuted,
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      child: Text(parts.join('  •  '), style: TextStyle(fontSize: 11, color: colors.mutedText)),
    );
  }
}
