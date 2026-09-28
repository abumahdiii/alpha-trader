import 'package:flutter/material.dart';

import '../../core/number_format.dart';
import '../../models/chart_models.dart';
import '../../theme/app_semantic_colors.dart';
import '../chart_format.dart';
import '../setups_table_query.dart';
import 'setup_widgets.dart';

/// Columns of the full setups table. [outcome] columns exist only when the
/// engine evaluated the setups (`evaluation.available`).
enum SetupColumn {
  time('زمان تصمیم (محلی)', 124,
      sortKey: SetupSortKey.time, tooltip: 'بسته شدن کندل تایید، به وقت محلی؛ UTC در راهنمای هر خانه'),
  direction('جهت', 56, sortKey: SetupSortKey.direction),
  type('نوع ستاپ', 200, sortKey: SetupSortKey.type),
  pattern('الگوی تایید', 170),
  status('وضعیت', 84, sortKey: SetupSortKey.status),
  entry('ورود (fill)', 86,
      tooltip: 'قیمت پر شدن شبیه‌ساز: خرید با ask (open کندل بعد + اسپرد)، فروش با bid. open خام (bid) در جزئیات است.'),
  stopLoss('حد ضرر', 86),
  takeProfit('حد سود', 86),
  volume('حجم', 58, tooltip: 'حجم پیشنهادی (لات) با تنظیمات فعلی حساب'),
  result('نتیجه', 112, sortKey: SetupSortKey.result, outcome: true),
  exitTime('زمان خروج (محلی)', 124, outcome: true),
  exitPrice('قیمت خروج', 86, outcome: true),
  pnlPrice('سود/زیان قیمت', 96, outcome: true, tooltip: 'تغییر قیمت به نفع معامله (فروش: ورود منهای خروج)'),
  r('R', 66, sortKey: SetupSortKey.r, outcome: true),
  netPnl('سود/زیان \$', 92, sortKey: SetupSortKey.netPnl, outcome: true, tooltip: 'سود خالص به دلار (بعد از کمیسیون)'),
  backtest('در بک‌تست', 170,
      outcome: true, tooltip: 'آیا بک‌تست همین بازه (با قید یک معامله باز) این ستاپ را معامله کرد؟'),
  why('چرا؟', 300, tooltip: 'دلیل باز شدن ستاپ (مقادیر کانال، ATR، شیب و الگو) و دلیل رد'),
  details('', 40);

  const SetupColumn(this.titleFa, this.width, {this.sortKey, this.outcome = false, this.tooltip});

  final String titleFa;
  final double width;
  final SetupSortKey? sortKey;
  final bool outcome;
  final String? tooltip;

  static List<SetupColumn> visible({required bool evaluated}) =>
      SetupColumn.values.where((SetupColumn c) => evaluated || !c.outcome).toList(growable: false);
}

const double _kRowHeight = 32;
const double _kHeaderHeight = 34;
const double _kSumHeight = 34;

/// The full, wide setups table: sortable header, one row per setup (row
/// tap = select + jump on the chart), and a sum row that is the engine's
/// `summary` of the WHOLE range (never a client-side sum of the visible rows).
class SetupsTable extends StatefulWidget {
  const SetupsTable({
    super.key,
    required this.result,
    required this.rows,
    required this.query,
    required this.onSort,
    required this.onSelect,
    required this.onDetails,
    this.selectedId,
    this.digits,
  });

  final SetupsResult result;

  /// Already filtered and sorted.
  final List<SetupItem> rows;
  final SetupsTableQuery query;
  final ValueChanged<SetupSortKey> onSort;
  final ValueChanged<SetupItem> onSelect;
  final ValueChanged<SetupItem> onDetails;
  final String? selectedId;
  final int? digits;

  @override
  State<SetupsTable> createState() => _SetupsTableState();
}

class _SetupsTableState extends State<SetupsTable> {
  final ScrollController _horizontal = ScrollController();
  final ScrollController _vertical = ScrollController();

  @override
  void dispose() {
    _horizontal.dispose();
    _vertical.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final bool evaluated = widget.result.evaluationAvailable;
    final List<SetupColumn> cols = SetupColumn.visible(evaluated: evaluated);
    final double tableWidth = cols.fold<double>(0, (double w, SetupColumn c) => w + c.width);
    final TextTheme tt = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;

    return LayoutBuilder(builder: (BuildContext context, BoxConstraints box) {
      final double width = box.maxWidth > tableWidth ? box.maxWidth : tableWidth;
      // Explicit, always-visible scrollbars (the wide table scrolls sideways).
      return ScrollConfiguration(
        behavior: ScrollConfiguration.of(context).copyWith(scrollbars: false),
        child: Scrollbar(
          controller: _horizontal,
          thumbVisibility: true,
          child: SingleChildScrollView(
            key: const ValueKey<String>('setups-table-hscroll'),
            controller: _horizontal,
            scrollDirection: Axis.horizontal,
            child: SizedBox(
              width: width,
              height: box.maxHeight,
              child: DefaultTextStyle.merge(
                style: const TextStyle(fontSize: 12),
                child: Column(
                  key: const ValueKey<String>('setup-table'),
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: <Widget>[
                    _header(context, cols),
                    const Divider(height: 1),
                    Expanded(
                      child: widget.rows.isEmpty
                          ? Center(
                              child: Text(
                                widget.result.setups.isEmpty
                                    ? 'در این بازه ستاپی پیدا نشد.'
                                    : 'هیچ ردیفی با فیلترهای فعلی نیست.',
                                style: tt.bodyMedium?.copyWith(color: colors.mutedText),
                              ),
                            )
                          : Scrollbar(
                              controller: _vertical,
                              thumbVisibility: true,
                              child: ListView.builder(
                                key: const ValueKey<String>('setups-table-rows'),
                                controller: _vertical,
                                itemExtent: _kRowHeight,
                                itemCount: widget.rows.length,
                                itemBuilder: (BuildContext context, int i) => _row(context, cols, widget.rows[i], i),
                              ),
                            ),
                    ),
                    const Divider(height: 1),
                    _sumRow(context, cols),
                  ],
                ),
              ),
            ),
          ),
        ),
      );
    });
  }

  Widget _cell(SetupColumn c, Widget child) => SizedBox(
        width: c.width,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 6),
          child: Align(alignment: AlignmentDirectional.centerStart, child: child),
        ),
      );

  Widget _header(BuildContext context, List<SetupColumn> cols) {
    final AppSemanticColors colors = context.appColors;
    final SetupsTableQuery q = widget.query;
    return Container(
      height: _kHeaderHeight,
      color: colors.surfaceMuted,
      child: Row(children: <Widget>[
        for (final SetupColumn c in cols)
          () {
            final SetupSortKey? key = c.sortKey;
            final bool active = key != null && key == q.sortKey;
            Widget label = Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
              Flexible(
                child: Text(
                  c.titleFa,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      fontWeight: FontWeight.bold, color: active ? Theme.of(context).colorScheme.primary : null),
                ),
              ),
              if (active) Icon(q.ascending ? Icons.arrow_upward : Icons.arrow_downward, size: 14),
            ]);
            if (c.tooltip != null) label = Tooltip(message: c.tooltip, child: label);
            final Widget cell = _cell(c, label);
            if (key == null) return cell;
            return InkWell(
              key: ValueKey<String>('setups-sort-${key.name}'),
              onTap: () => widget.onSort(key),
              child: SizedBox(height: _kHeaderHeight, child: cell),
            );
          }(),
      ]),
    );
  }

  Widget _row(BuildContext context, List<SetupColumn> cols, SetupItem s, int i) {
    final AppSemanticColors colors = context.appColors;
    final bool selected = s.id == widget.selectedId;
    final Color background = selected
        ? Theme.of(context).colorScheme.primary.withValues(alpha: 0.14)
        : (i.isOdd ? colors.surfaceMuted.withValues(alpha: 0.45) : Colors.transparent);
    return Material(
      color: background,
      child: InkWell(
        key: ValueKey<String>('setup-row-${s.id}'),
        onTap: () => widget.onSelect(s),
        child: Row(children: <Widget>[for (final SetupColumn c in cols) _cell(c, _content(context, c, s))]),
      ),
    );
  }

  Widget _content(BuildContext context, SetupColumn c, SetupItem s) {
    final AppSemanticColors colors = context.appColors;
    final SetupOutcome? o = s.outcome;
    final int? digits = widget.digits;
    String price(double? v) => formatChartPrice(v, digits);
    Widget numText(String text, {Color? color, String? tooltip}) {
      final Widget t =
          Text(text, textDirection: TextDirection.ltr, maxLines: 1, softWrap: false, style: TextStyle(color: color));
      return tooltip == null ? t : Tooltip(message: tooltip, child: t);
    }

    Widget faText(String text, {Color? color, String? tooltip, FontWeight? weight}) {
      final Widget t =
          Text(text, maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(color: color, fontWeight: weight));
      return tooltip == null ? t : Tooltip(message: tooltip, waitDuration: const Duration(milliseconds: 300), child: t);
    }

    const String dash = '—';
    return switch (c) {
      SetupColumn.time => numText(formatLocal(s.decisionTime), tooltip: formatUtc(s.decisionTime)),
      SetupColumn.direction =>
        faText(s.direction.titleFa, color: tradeSideColor(context, s.direction), weight: FontWeight.bold),
      SetupColumn.type => faText(s.typeTitleFa, tooltip: s.typeTitleFa),
      SetupColumn.pattern => faText(s.patternTitleFa, tooltip: s.patternTitleFa),
      SetupColumn.status =>
        faText(s.status.titleFa, color: setupStatusColor(context, s.status), weight: FontWeight.w600),
      SetupColumn.entry => s.status == SetupStatus.pendingEntry
          ? faText(dash, tooltip: 'کندل ورود هنوز باز نشده است.')
          : numText(price(s.entry),
              tooltip: s.entryBidOpen == null
                  ? null
                  : 'open کندل ورود (bid): ${price(s.entryBidOpen)}'
                      '${s.spreadAtEntryPoints == null ? '' : ' — اسپرد ${s.spreadAtEntryPoints} پوینت'}'),
      SetupColumn.stopLoss => numText(price(s.stopLoss)),
      SetupColumn.takeProfit => numText(price(s.takeProfit)),
      SetupColumn.volume =>
        numText(s.volume == null ? dash : formatNumber(s.volume!, decimals: 2), tooltip: s.volumeNoteFa),
      SetupColumn.result => o == null ? faText(dash) : _result(context, o),
      SetupColumn.exitTime =>
        o == null ? faText(dash) : numText(formatLocal(o.exitTime), tooltip: formatUtc(o.exitTime)),
      SetupColumn.exitPrice => o == null ? faText(dash) : numText(price(o.exitPrice)),
      SetupColumn.pnlPrice =>
        o == null ? faText(dash) : numText(formatSigned(o.pnlPrice, digits ?? 5), color: pnlColor(context, o.pnlPrice)),
      SetupColumn.r =>
        o == null ? faText(dash) : numText(formatSigned(o.rMultiple, 2), color: pnlColor(context, o.rMultiple)),
      SetupColumn.netPnl =>
        o == null ? faText(dash) : numText(formatSigned(o.netPnl, 2), color: pnlColor(context, o.netPnl)),
      SetupColumn.backtest => _backtest(context, s.backtest),
      SetupColumn.why =>
        faText(_whyShort(s), tooltip: _whyFull(s), color: s.status == SetupStatus.rejected ? colors.error : null),
      SetupColumn.details => IconButton(
          key: ValueKey<String>('setup-details-${s.id}'),
          tooltip: 'جزئیات کامل',
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints.tightFor(width: 28, height: 28),
          iconSize: 18,
          onPressed: () => widget.onDetails(s),
          icon: const Icon(Icons.info_outline),
        ),
    };
  }

  Widget _result(BuildContext context, SetupOutcome o) {
    final AppSemanticColors colors = context.appColors;
    final String label = o.isGap ? '${o.result.titleFa} (گپ)' : o.result.titleFa;
    final (Color color, String tooltip) = switch (o.result) {
      SetupResult.tp => (colors.success, o.exitReasonFa),
      SetupResult.sl => (colors.error, o.exitReasonFa),
      SetupResult.endOfData => (
          colors.warning,
          _nonEmpty(widget.result.evaluation?.endOfDataLabelFa) ?? o.exitReasonFa,
        ),
    };
    return Tooltip(
      message: tooltip,
      waitDuration: const Duration(milliseconds: 300),
      child: Text(label,
          maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(color: color, fontWeight: FontWeight.w600)),
    );
  }

  Widget _backtest(BuildContext context, SetupBacktestFlag? f) {
    final AppSemanticColors colors = context.appColors;
    if (f == null) return const Text('—');
    final String detail = f.traded
        ? (f.tradeIndex == null ? 'معامله شد' : 'معامله ${f.tradeIndex}')
        : (f.reasonFa ?? f.reason ?? 'معامله نشد');
    final String tooltip = f.traded
        ? 'در بک‌تست همین بازه معامله شد${f.netPnl == null ? '' : '؛ سود خالص بک‌تست ${formatSigned(f.netPnl, 2)} \$'}'
        : (f.reasonFa ?? f.reason ?? 'معامله نشد');
    return Tooltip(
      message: tooltip,
      waitDuration: const Duration(milliseconds: 300),
      child: Row(children: <Widget>[
        Text(f.traded ? '✓' : '✗',
            style: TextStyle(fontWeight: FontWeight.bold, color: f.traded ? colors.success : colors.mutedText)),
        const SizedBox(width: 4),
        Expanded(
          child: Text(detail,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(color: f.traded ? null : colors.mutedText)),
        ),
      ]),
    );
  }

  static String? _nonEmpty(String? s) => (s == null || s.isEmpty) ? null : s;

  static String _whyShort(SetupItem s) =>
      s.status == SetupStatus.rejected && s.rejectionReasonFa != null ? 'رد: ${s.rejectionReasonFa}' : s.reasonFa;

  static String _whyFull(SetupItem s) => <String>[
        if (s.rejectionReasonFa != null) 'دلیل رد: ${s.rejectionReasonFa}',
        'دلیل ستاپ: ${s.reasonFa}',
        if (s.volumeNoteFa != null) s.volumeNoteFa!,
        ...s.sizingWarningsFa,
      ].join('\n\n');

  /// The engine's summary of the whole range, placed under its columns.
  Widget _sumRow(BuildContext context, List<SetupColumn> cols) {
    final AppSemanticColors colors = context.appColors;
    final SetupsResult r = widget.result;
    final SetupsSummary? s = r.summary;
    final bool evaluated = r.evaluationAvailable && s != null;
    const Set<SetupColumn> labelSpan = <SetupColumn>{
      SetupColumn.time,
      SetupColumn.direction,
      SetupColumn.type,
      SetupColumn.pattern,
      SetupColumn.status,
      SetupColumn.entry,
      SetupColumn.stopLoss,
      SetupColumn.takeProfit,
      SetupColumn.volume,
    };
    final double labelWidth = cols.where(labelSpan.contains).fold<double>(0, (double w, SetupColumn c) => w + c.width);
    final String counts = 'کل ${s?.total ?? r.count}، پذیرفته ${s?.accepted ?? r.statusCounts['accepted'] ?? 0}، '
        'ردشده ${s?.rejected ?? r.statusCounts['rejected'] ?? 0}، '
        'منتظر ورود ${s?.pendingEntry ?? r.statusCounts['pending_entry'] ?? 0}';

    Widget value(String text, {Color? color, String? tooltip}) {
      final Widget t = Text(text,
          textDirection: TextDirection.ltr,
          maxLines: 1,
          softWrap: false,
          style: TextStyle(fontWeight: FontWeight.bold, color: color));
      return tooltip == null ? t : Tooltip(message: tooltip, child: t);
    }

    Widget? content(SetupColumn c) {
      if (!evaluated) return null;
      final BacktestWindow? w = r.backtestWindow;
      return switch (c) {
        SetupColumn.result => Tooltip(
            message: 'بسته‌شده با حد سود یا حد ضرر: ${s.closed}، سر به سر: ${s.breakeven}، '
                'باز در پایان داده: ${s.openEndOfData}',
            child: Text('${s.wins} برد / ${s.losses} باخت', maxLines: 1, overflow: TextOverflow.ellipsis),
          ),
        SetupColumn.r => value(formatSigned(s.totalR, 2), color: pnlColor(context, s.totalR), tooltip: 'جمع R'),
        SetupColumn.netPnl =>
          value(formatSigned(s.netPnl, 2), color: pnlColor(context, s.netPnl), tooltip: 'سود/زیان خالص (\$)'),
        SetupColumn.backtest => w == null || !w.available || w.trades == null
            ? null
            : Tooltip(
                message: 'بک‌تست همین بازه: ${w.trades} معامله، سود خالص ${formatSigned(w.netProfit, 2)} \$',
                child: Text('✓ ${w.trades} معامله', maxLines: 1, overflow: TextOverflow.ellipsis),
              ),
        SetupColumn.why => s.openEndOfData == 0
            ? null
            : Text(
                'باز در پایان داده: ${s.openEndOfData} (${formatSigned(s.openNetPnl, 2)} \$، جدا از جمع)',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: colors.warning),
              ),
        _ => null,
      };
    }

    return Container(
      key: const ValueKey<String>('setups-sum-row'),
      height: _kSumHeight,
      color: colors.surfaceMuted,
      child: Row(children: <Widget>[
        SizedBox(
          width: labelWidth,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 6),
            child: Tooltip(
              message: 'اعداد این ردیف خلاصه engine برای کل بازه‌اند و با فیلتر یا مرتب‌سازی تغییر نمی‌کنند.',
              child: Text.rich(
                TextSpan(children: <InlineSpan>[
                  const TextSpan(text: 'جمع کل بازه (از موتور)', style: TextStyle(fontWeight: FontWeight.bold)),
                  TextSpan(text: '  —  $counts', style: TextStyle(color: colors.mutedText)),
                ]),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ),
        ),
        for (final SetupColumn c in cols)
          if (!labelSpan.contains(c)) _cell(c, content(c) ?? const SizedBox.shrink()),
      ]),
    );
  }
}
