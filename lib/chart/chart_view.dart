import 'package:flutter/material.dart';

import '../models/market_data.dart';
import '../theme/app_semantic_colors.dart';
import 'chart_controller.dart';
import 'chart_data.dart';
import 'chart_format.dart';
import '../models/chart_models.dart';
import 'widgets/candle_info_panel.dart';
import 'widgets/chart_canvas.dart';
import 'widgets/data_info_panel.dart';
import 'widgets/setup_widgets.dart';

/// The whole chart page body: toolbar, candlestick canvas with the candle
/// info panel, and a side panel with the setups table and the data info.
/// Everything is read from [controller]; nothing is computed here.
class ChartView extends StatefulWidget {
  const ChartView({super.key, required this.controller});

  final ChartController controller;

  @override
  State<ChartView> createState() => _ChartViewState();
}

class _ChartViewState extends State<ChartView> {
  final ValueNotifier<int?> _hoverIndex = ValueNotifier<int?>(null);

  @override
  void dispose() {
    _hoverIndex.dispose();
    super.dispose();
  }

  Future<void> _openSetup(SetupMark m) async {
    final ChartController c = widget.controller;
    c.selectSetup(m.item.id);
    await SetupDetails.show(context, m.item, digits: c.data?.digits);
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
            _ChartToolbar(controller: c),
            if (c.isLoading) const LinearProgressIndicator(minHeight: 2) else const SizedBox(height: 2),
            Expanded(
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: <Widget>[
                  Expanded(child: _chartArea(context, c)),
                  const VerticalDivider(width: 1),
                  SizedBox(width: 340, child: _SidePanel(controller: c, onSetupSelected: _onTableSelect)),
                ],
              ),
            ),
            _StatusLine(controller: c),
          ],
        );
      },
    );
  }

  void _onTableSelect(SetupMark m) => widget.controller.selectSetup(m.item.id, jump: true);

  Widget _chartArea(BuildContext context, ChartController c) {
    final ChartData? data = c.data;
    final bool showData = data != null && !data.isEmpty && c.state != ChartLoadState.error;
    return Stack(
      fit: StackFit.expand,
      children: <Widget>[
        if (showData)
          ChartCanvas(
            key: const ValueKey<String>('chart-canvas'),
            data: data,
            viewRequest: c.viewRequest,
            hoverIndex: _hoverIndex,
            selectedSetupId: c.selectedSetupId,
            highlightIndex: c.highlightIndex,
            onSetupTap: _openSetup,
          ),
        if (showData)
          // Physically top-left: the newest bars and the price axis are on the right.
          Positioned(
            top: 8,
            left: 8,
            child: IgnorePointer(child: CandleInfoPanel(data: data, hoverIndex: _hoverIndex)),
          ),
        if (!showData) _StateMessage(controller: c),
      ],
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
  const _ChartToolbar({required this.controller});

  final ChartController controller;

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
            onChanged: (String? s) {
              if (s != null) c.setSymbol(s);
            },
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
          Tooltip(
            message: 'چرخ ماوس: زوم روی نشانگر — Shift + چرخ یا کشیدن: جابه‌جایی — کلیک روی فلش: جزئیات ستاپ',
            child: Icon(Icons.help_outline, size: 20, color: context.appColors.mutedText),
          ),
        ],
      ),
    );
  }
}

class _SidePanel extends StatelessWidget {
  const _SidePanel({required this.controller, required this.onSetupSelected});

  final ChartController controller;
  final ValueChanged<SetupMark> onSetupSelected;

  @override
  Widget build(BuildContext context) {
    final ChartController c = controller;
    final ChartData? data = c.data;
    final bool h1 = c.timeframe == ChartTimeframe.h1;
    return DefaultTabController(
      length: 2,
      child: Column(children: <Widget>[
        TabBar(
          labelColor: Theme.of(context).colorScheme.primary,
          unselectedLabelColor: context.appColors.mutedText,
          tabs: const <Widget>[Tab(text: 'ستاپ‌ها'), Tab(text: 'اطلاعات داده')],
        ),
        Expanded(
          child: TabBarView(children: <Widget>[
            SetupTable(
              setups: h1 ? (data?.setups ?? const <SetupMark>[]) : const <SetupMark>[],
              selectedId: c.selectedSetupId,
              digits: data?.digits,
              onSelect: onSetupSelected,
              emptyText: !h1
                  ? 'ستاپ‌ها روی چارت H1 نمایش داده می‌شوند.'
                  : data == null
                      ? 'داده‌ای بارگذاری نشده است.'
                      : null,
              noteFa: h1 ? data?.setupsResult?.noteFa : null,
            ),
            DataInfoPanel(controller: c),
          ]),
        ),
      ]),
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
    final List<String> parts = <String>['زمان‌های محور و جدول‌ها: UTC'];
    if (d != null) {
      parts.add('${d.length} کندل');
      final StrategyProvenance? p = d.channelResult?.provenance;
      if (p != null) parts.add('پارامترها: نسخه ${p.paramsVersion}');
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
