import 'package:flutter/material.dart';

import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../models/chart_models.dart';
import '../../theme/app_semantic_colors.dart';
import '../backtest_range_request.dart';
import '../chart_controller.dart';
import '../chart_data.dart';
import '../setups_table_query.dart';
import 'setup_widgets.dart';
import 'setups_summary_bar.dart';
import 'setups_table.dart';

const String kSetupsH1OnlyFa = 'ستاپ‌ها روی چارت H1 نمایش داده می‌شوند.';

/// Bottom pane of the chart page: header (visible-row count, filters,
/// collapse), the range summary bar and the full setups table. Sorting and
/// filtering only reorder / hide the engine's rows.
class SetupsPane extends StatefulWidget {
  const SetupsPane({
    super.key,
    required this.controller,
    required this.collapsed,
    required this.onToggleCollapsed,
    this.onBacktestRange,
  });

  static const double headerHeight = 40;

  final ChartController controller;
  final bool collapsed;
  final VoidCallback onToggleCollapsed;
  final ValueChanged<BacktestRangeRequest>? onBacktestRange;

  @override
  State<SetupsPane> createState() => _SetupsPaneState();
}

class _SetupsPaneState extends State<SetupsPane> {
  SetupsTableQuery _query = const SetupsTableQuery();

  void _setQuery(SetupsTableQuery q, String what) {
    if (kDevMode) {
      devLog('[Setups] $what -> $q');
      AppLogger.log('[Setups] $what -> $q');
    }
    setState(() => _query = q);
  }

  void _select(SetupItem s) {
    devLog('[Setups] row ${s.id} -> select + jump to ${s.confirmationBarTime.toIso8601String()}');
    widget.controller.selectSetup(s.id, jump: true);
  }

  Future<void> _details(SetupItem s) async {
    final ChartController c = widget.controller;
    devLog('[Setups] details ${s.id}');
    c.selectSetup(s.id, jump: true);
    await SetupDetails.show(
      context,
      s,
      digits: c.data?.digits,
      endOfDataLabelFa: c.data?.setupsResult?.evaluation?.endOfDataLabelFa,
    );
  }

  @override
  Widget build(BuildContext context) {
    final ChartController c = widget.controller;
    final ChartData? data = c.data;
    final bool h1 = c.timeframe == ChartTimeframe.h1;
    final SetupsResult? result = h1 ? data?.setupsResult : null;
    final List<SetupItem> all = result?.setups ?? const <SetupItem>[];
    final List<SetupItem> rows = _query.apply(all);

    return Material(
      key: const ValueKey<String>('setups-pane'),
      color: Theme.of(context).colorScheme.surface,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          _PaneHeader(
            result: result,
            visible: rows.length,
            total: all.length,
            query: _query,
            collapsed: widget.collapsed,
            onToggleCollapsed: widget.onToggleCollapsed,
            onQuery: _setQuery,
          ),
          if (!widget.collapsed)
            Expanded(
              child: !h1
                  ? const _Empty(text: kSetupsH1OnlyFa)
                  : result == null
                      ? _Empty(text: c.isLoading ? 'در حال دریافت ستاپ‌ها…' : 'داده‌ای بارگذاری نشده است.')
                      : LayoutBuilder(builder: (BuildContext context, BoxConstraints box) {
                          // The summary scrolls when the pane is short; the table keeps >= 100 px.
                          final double summaryMax = (box.maxHeight - 100).clamp(0.0, box.maxHeight * 0.5).toDouble();
                          return Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: <Widget>[
                              ConstrainedBox(
                                constraints: BoxConstraints(maxHeight: summaryMax),
                                child: SingleChildScrollView(
                                  child: SetupsSummaryBar(result: result, onBacktestRange: widget.onBacktestRange),
                                ),
                              ),
                              const Divider(height: 1),
                              Expanded(
                                child: SetupsTable(
                                  result: result,
                                  rows: rows,
                                  query: _query,
                                  selectedId: c.selectedSetupId,
                                  digits: data?.digits,
                                  onSort: (SetupSortKey k) => _setQuery(_query.sortedBy(k), 'sort ${k.name}'),
                                  onSelect: _select,
                                  onDetails: _details,
                                ),
                              ),
                            ],
                          );
                        }),
            ),
        ],
      ),
    );
  }
}

class _Empty extends StatelessWidget {
  const _Empty({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Text(
            text,
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: context.appColors.mutedText),
          ),
        ),
      );
}

class _PaneHeader extends StatelessWidget {
  const _PaneHeader({
    required this.result,
    required this.visible,
    required this.total,
    required this.query,
    required this.collapsed,
    required this.onToggleCollapsed,
    required this.onQuery,
  });

  final SetupsResult? result;
  final int visible;
  final int total;
  final SetupsTableQuery query;
  final bool collapsed;
  final VoidCallback onToggleCollapsed;
  final void Function(SetupsTableQuery q, String what) onQuery;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final SetupsResult? r = result;
    final bool evaluated = r?.evaluationAvailable ?? false;
    final bool hasFlags = r?.setups.any((SetupItem s) => s.backtest != null) ?? false;
    final SetupsTableQuery q = query;

    return Container(
      height: SetupsPane.headerHeight,
      padding: const EdgeInsetsDirectional.only(start: 8, end: 4),
      decoration: BoxDecoration(border: Border(bottom: BorderSide(color: colors.borderColor))),
      child: Row(children: <Widget>[
        Text('ستاپ‌های بازه', style: tt.titleSmall),
        const SizedBox(width: 8),
        if (r != null)
          Text(
            '$visible از $total ردیف',
            key: const ValueKey<String>('setups-visible-count'),
            style: tt.bodySmall?.copyWith(color: q.hasFilters ? colors.warning : colors.mutedText),
          ),
        const SizedBox(width: 12),
        Expanded(
          child: r == null
              ? const SizedBox.shrink()
              : SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: Row(children: <Widget>[
                    _Filter<SetupStatus>(
                      key: const ValueKey<String>('setups-filter-status'),
                      label: 'وضعیت',
                      value: q.status,
                      options: <SetupStatus, String>{for (final SetupStatus s in SetupStatus.values) s: s.titleFa},
                      onChanged: (SetupStatus? v) => onQuery(q.copyWith(status: v), 'filter status'),
                    ),
                    _Filter<TradeSide>(
                      key: const ValueKey<String>('setups-filter-direction'),
                      label: 'جهت',
                      value: q.direction,
                      options: <TradeSide, String>{for (final TradeSide s in TradeSide.values) s: s.titleFa},
                      onChanged: (TradeSide? v) => onQuery(q.copyWith(direction: v), 'filter direction'),
                    ),
                    if (evaluated)
                      _Filter<SetupResultFilter>(
                        key: const ValueKey<String>('setups-filter-result'),
                        label: 'نتیجه',
                        value: q.result,
                        options: <SetupResultFilter, String>{
                          for (final SetupResultFilter f in SetupResultFilter.values) f: f.titleFa,
                        },
                        onChanged: (SetupResultFilter? v) => onQuery(q.copyWith(result: v), 'filter result'),
                      ),
                    if (evaluated && hasFlags)
                      _Filter<bool>(
                        key: const ValueKey<String>('setups-filter-traded'),
                        label: 'در بک‌تست',
                        value: q.traded,
                        options: const <bool, String>{true: 'معامله شد', false: 'معامله نشد'},
                        onChanged: (bool? v) => onQuery(q.copyWith(traded: v), 'filter traded'),
                      ),
                    TextButton.icon(
                      key: const ValueKey<String>('setups-clear-filters'),
                      onPressed: q.hasFilters ? () => onQuery(q.cleared(), 'clear filters') : null,
                      icon: const Icon(Icons.filter_alt_off, size: 18),
                      label: const Text('پاک کردن فیلترها'),
                    ),
                  ]),
                ),
        ),
        IconButton(
          key: const ValueKey<String>('setups-pane-toggle'),
          tooltip: collapsed ? 'باز کردن جدول ستاپ‌ها' : 'جمع کردن جدول ستاپ‌ها',
          onPressed: onToggleCollapsed,
          icon: Icon(collapsed ? Icons.expand_less : Icons.expand_more),
        ),
      ]),
    );
  }
}

/// A compact «label: [all | option…]» filter.
class _Filter<T extends Object> extends StatelessWidget {
  const _Filter({super.key, required this.label, required this.value, required this.options, required this.onChanged});

  final String label;
  final T? value;
  final Map<T, String> options;
  final ValueChanged<T?> onChanged;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    return Padding(
      padding: const EdgeInsetsDirectional.only(end: 10),
      child: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
        Text('$label: ', style: TextStyle(fontSize: 12, color: colors.mutedText)),
        DropdownButton<T?>(
          value: value,
          isDense: true,
          underline: const SizedBox.shrink(),
          style: TextStyle(
            fontSize: 12,
            color: value == null ? Theme.of(context).colorScheme.onSurface : Theme.of(context).colorScheme.primary,
            fontWeight: value == null ? null : FontWeight.bold,
          ),
          items: <DropdownMenuItem<T?>>[
            DropdownMenuItem<T?>(value: null, child: const Text('همه')),
            for (final MapEntry<T, String> e in options.entries)
              DropdownMenuItem<T?>(value: e.key, child: Text(e.value)),
          ],
          onChanged: onChanged,
        ),
      ]),
    );
  }
}
