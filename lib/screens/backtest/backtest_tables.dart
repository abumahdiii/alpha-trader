import 'package:flutter/material.dart';

import '../../chart/chart_format.dart';
import '../../models/backtest_models.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/scrollable_dialog.dart';
import '../../widgets/sortable_table.dart';
import 'backtest_format.dart';

/// A local time cell with the UTC time in its tooltip.
class TimeCell extends StatelessWidget {
  const TimeCell(this.time, {super.key});

  final DateTime? time;

  @override
  Widget build(BuildContext context) =>
      time == null ? const Text(kDash) : Tooltip(message: fmtUtc(time), child: LtrText(fmtLocal(time)));
}

Widget _money(BuildContext context, double? v) =>
    LtrText(fmtMoney(v), style: TextStyle(color: pnlColor(context, v), fontWeight: FontWeight.w600));

/// Sortable list of simulated trades; times in local time (UTC in the
/// tooltip). A click opens the trade's details, or calls [onRowTap] (the
/// chart tab: zoom onto the trade).
class BacktestTradesTable extends StatelessWidget {
  const BacktestTradesTable({
    super.key,
    required this.trades,
    this.digits,
    this.showWindow = false,
    this.onRowTap,
    this.isSelected,
    this.keyPrefix = 'bt-trades',
  });

  final List<BacktestTrade> trades;

  /// Price digits of the symbol (null = engine value as is).
  final int? digits;

  /// Random runs: a window column.
  final bool showWindow;

  /// Replaces «open the details» on a row click.
  final ValueChanged<BacktestTrade>? onRowTap;
  final bool Function(BacktestTrade t)? isSelected;

  /// Test keys of the table (the chart tab's copy has its own).
  final String keyPrefix;

  @override
  Widget build(BuildContext context) {
    String price(double? p) => formatChartPrice(p, digits);
    return SortableTable<BacktestTrade>(
      keyPrefix: keyPrefix,
      rows: trades,
      isSelected: isSelected,
      onRowTap: onRowTap ?? (BacktestTrade t) => showTradeDetails(context, t, digits),
      columns: [
        if (showWindow)
          SortableColumn<BacktestTrade>(
            label: 'پنجره',
            width: 60,
            sortKey: (BacktestTrade t) => t.windowIndex,
            cell: (_, BacktestTrade t) => LtrText('${t.windowIndex + 1}'),
          ),
        SortableColumn<BacktestTrade>(
          label: '#',
          width: 50,
          sortKey: (BacktestTrade t) => t.windowIndex * 1000000 + t.tradeIndex,
          cell: (_, BacktestTrade t) => LtrText('${t.tradeIndex + 1}'),
        ),
        SortableColumn<BacktestTrade>(
          label: 'جهت',
          width: 60,
          sortKey: (BacktestTrade t) => t.direction,
          cell: (BuildContext context, BacktestTrade t) => Text(
            directionFa(t.direction),
            style: TextStyle(color: t.direction == 'buy' ? context.appColors.success : context.appColors.error),
          ),
        ),
        SortableColumn<BacktestTrade>(
          label: 'نوع ستاپ',
          width: 110,
          sortKey: (BacktestTrade t) => t.setupType,
          cell: (_, BacktestTrade t) => Text(t.setupType ?? kDash, maxLines: 1, overflow: TextOverflow.ellipsis),
        ),
        SortableColumn<BacktestTrade>(
          label: 'زمان ورود (محلی)',
          width: 130,
          sortKey: (BacktestTrade t) => t.entryTime,
          cell: (_, BacktestTrade t) => TimeCell(t.entryTime),
        ),
        SortableColumn<BacktestTrade>(
          label: 'ورود',
          width: 90,
          sortKey: (BacktestTrade t) => t.entry,
          cell: (_, BacktestTrade t) => LtrText(price(t.entry)),
        ),
        SortableColumn<BacktestTrade>(
          label: 'حد ضرر',
          width: 90,
          sortKey: (BacktestTrade t) => t.stopLoss,
          cell: (_, BacktestTrade t) => LtrText(price(t.stopLoss)),
        ),
        SortableColumn<BacktestTrade>(
          label: 'حد سود',
          width: 90,
          sortKey: (BacktestTrade t) => t.takeProfit,
          cell: (_, BacktestTrade t) => LtrText(price(t.takeProfit)),
        ),
        SortableColumn<BacktestTrade>(
          label: 'زمان خروج (محلی)',
          width: 130,
          sortKey: (BacktestTrade t) => t.exitTime,
          cell: (_, BacktestTrade t) => TimeCell(t.exitTime),
        ),
        SortableColumn<BacktestTrade>(
          label: 'قیمت خروج',
          width: 90,
          sortKey: (BacktestTrade t) => t.exitPrice,
          cell: (_, BacktestTrade t) => LtrText(price(t.exitPrice)),
        ),
        SortableColumn<BacktestTrade>(
          label: 'دلیل خروج',
          width: 150,
          sortKey: (BacktestTrade t) => t.exitReasonFa,
          cell: (_, BacktestTrade t) => Tooltip(
            message: t.exitReasonFa ?? t.exitReason ?? kDash,
            child: Text(t.exitReasonFa ?? t.exitReason ?? kDash, maxLines: 1, overflow: TextOverflow.ellipsis),
          ),
        ),
        SortableColumn<BacktestTrade>(
          label: 'حجم (لات)',
          width: 70,
          sortKey: (BacktestTrade t) => t.volume,
          cell: (_, BacktestTrade t) => LtrText(formatValue(t.volume, 2)),
        ),
        SortableColumn<BacktestTrade>(
          label: 'سود/زیان خالص',
          width: 110,
          sortKey: (BacktestTrade t) => t.netPnl,
          cell: (BuildContext context, BacktestTrade t) => _money(context, t.netPnl),
        ),
        SortableColumn<BacktestTrade>(
          label: 'R',
          width: 70,
          sortKey: (BacktestTrade t) => t.rMultiple,
          cell: (BuildContext context, BacktestTrade t) =>
              LtrText(fmtR(t.rMultiple), style: TextStyle(color: pnlColor(context, t.rMultiple))),
        ),
      ],
    );
  }
}

/// Every engine field of one trade, in a dialog.
Future<void> showTradeDetails(BuildContext context, BacktestTrade t, int? digits) {
  String price(double? p) => formatChartPrice(p, digits);
  final List<(String, String)> rows = [
    ('جهت', directionFa(t.direction)),
    ('نوع ستاپ / خط / الگو', '${t.setupType ?? kDash} / ${t.line ?? kDash} / ${t.pattern ?? kDash}'),
    ('کندل تایید', '${fmtLocal(t.confirmationBarTime)}  (${fmtUtc(t.confirmationBarTime)})'),
    ('ورود', '${fmtLocal(t.entryTime)}  (${fmtUtc(t.entryTime)})'),
    ('قیمت ورود / حد ضرر / حد سود', '${price(t.entry)} / ${price(t.stopLoss)} / ${price(t.takeProfit)}'),
    ('خروج', '${fmtLocal(t.exitTime)}  (${fmtUtc(t.exitTime)})'),
    ('قیمت خروج', price(t.exitPrice)),
    ('دلیل خروج', t.exitReasonFa ?? t.exitReason ?? kDash),
    ('حجم (لات)', formatValue(t.volume, 2)),
    ('ریسک این معامله', fmtMoney(t.riskAmount, signed: false)),
    ('سود/زیان ناخالص', fmtMoney(t.grossPnl)),
    ('کمیسیون', fmtMoney(t.commission, signed: false)),
    ('سود/زیان خالص', fmtMoney(t.netPnl)),
    ('R', fmtR(t.rMultiple)),
    ('موجودی قبل / بعد', '${fmtMoney(t.balanceBefore, signed: false)} / ${fmtMoney(t.balanceAfter, signed: false)}'),
    ('تعداد کندل H1 باز بودن', fmtInt(t.barsHeld)),
    ('اسپرد کندل ورود (پوینت)', fmtInt(t.spreadAtEntryPoints)),
    ('باز ماندن در آخر هفته', t.heldOverWeekend == true ? 'بله (سوآپ مدل نشده)' : 'خیر'),
  ];
  return ScrollableDialog.show<void>(
    context: context,
    title: Text('معامله ${t.tradeIndex + 1}${t.windowIndex > 0 ? ' — پنجره ${t.windowIndex + 1}' : ''}'),
    showCloseButton: true,
    content: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      for (final (String k, String v) in rows)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 3),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            SizedBox(width: 190, child: Text(k, style: TextStyle(color: context.appColors.mutedText))),
            Expanded(child: Text(v, textDirection: TextDirection.ltr, textAlign: TextAlign.right)),
          ]),
        ),
      if (t.reasonFa != null) ...[
        const Divider(),
        Text('دلیل استراتژی', style: Theme.of(context).textTheme.titleSmall),
        Text(t.reasonFa!),
      ],
      for (final String w in t.sizingWarningsFa) Text(w, style: TextStyle(color: context.appColors.warning)),
    ]),
  );
}

/// Windows of a random run with their engine metrics; a click selects the
/// window for the trades / equity / skipped tabs.
class BacktestWindowsTable extends StatelessWidget {
  const BacktestWindowsTable({super.key, required this.windows, this.selected, this.onSelect});

  final List<BacktestWindowResult> windows;
  final int? selected;
  final ValueChanged<int>? onSelect;

  @override
  Widget build(BuildContext context) {
    return SortableTable<BacktestWindowResult>(
      keyPrefix: 'bt-windows',
      rows: windows,
      isSelected: (BacktestWindowResult w) => w.index == selected,
      onRowTap: onSelect == null ? null : (BacktestWindowResult w) => onSelect!(w.index),
      columns: [
        SortableColumn<BacktestWindowResult>(
          label: 'پنجره',
          width: 60,
          sortKey: (BacktestWindowResult w) => w.index,
          cell: (_, BacktestWindowResult w) => LtrText('${w.index + 1}'),
        ),
        SortableColumn<BacktestWindowResult>(
          label: 'از (UTC)',
          width: 100,
          sortKey: (BacktestWindowResult w) => w.start,
          cell: (_, BacktestWindowResult w) => LtrText(fmtDay(w.start)),
        ),
        SortableColumn<BacktestWindowResult>(
          label: 'تا (UTC)',
          width: 100,
          sortKey: (BacktestWindowResult w) => w.end,
          cell: (_, BacktestWindowResult w) => LtrText(fmtDay(w.end)),
        ),
        SortableColumn<BacktestWindowResult>(
          label: 'معاملات',
          width: 70,
          sortKey: (BacktestWindowResult w) => w.tradeCount,
          cell: (_, BacktestWindowResult w) => LtrText(fmtInt(w.tradeCount)),
        ),
        SortableColumn<BacktestWindowResult>(
          label: 'سود خالص',
          width: 110,
          sortKey: (BacktestWindowResult w) => w.netProfit,
          cell: (BuildContext context, BacktestWindowResult w) => _money(context, w.netProfit),
        ),
        SortableColumn<BacktestWindowResult>(
          label: 'سود خالص %',
          width: 90,
          sortKey: (BacktestWindowResult w) => w.netProfitPct,
          cell: (BuildContext context, BacktestWindowResult w) =>
              LtrText(fmtPct(w.netProfitPct), style: TextStyle(color: pnlColor(context, w.netProfitPct))),
        ),
        SortableColumn<BacktestWindowResult>(
          label: 'PF',
          width: 60,
          sortKey: (BacktestWindowResult w) =>
              w.metrics?.profitFactorInfinite == true ? double.infinity : w.metrics?.profitFactor,
          cell: (_, BacktestWindowResult w) =>
              LtrText(fmtProfitFactor(w.metrics?.profitFactor, w.metrics?.profitFactorInfinite ?? false)),
        ),
        SortableColumn<BacktestWindowResult>(
          label: 'نرخ برد',
          width: 70,
          sortKey: (BacktestWindowResult w) => w.metrics?.winRate,
          cell: (_, BacktestWindowResult w) => LtrText(fmtFraction(w.metrics?.winRate)),
        ),
        SortableColumn<BacktestWindowResult>(
          label: 'حداکثر افت',
          width: 150,
          sortKey: (BacktestWindowResult w) => w.metrics?.maxDrawdownPct,
          cell: (_, BacktestWindowResult w) => LtrText(
            '${fmtMoney(w.metrics?.maxDrawdownAbs, signed: false)} (${fmtPct(w.metrics?.maxDrawdownPct, signed: false)})',
          ),
        ),
      ],
    );
  }
}

/// Candidates the simulator did not trade, with the engine's Persian reason.
class BacktestSkippedTable extends StatelessWidget {
  const BacktestSkippedTable({super.key, required this.skipped, this.showWindow = false});

  final List<BacktestSkippedCandidate> skipped;
  final bool showWindow;

  @override
  Widget build(BuildContext context) {
    return SortableTable<BacktestSkippedCandidate>(
      keyPrefix: 'bt-skipped',
      rows: skipped,
      rowHeight: 38,
      columns: [
        if (showWindow)
          SortableColumn<BacktestSkippedCandidate>(
            label: 'پنجره',
            width: 60,
            sortKey: (BacktestSkippedCandidate s) => s.windowIndex,
            cell: (_, BacktestSkippedCandidate s) => LtrText('${s.windowIndex + 1}'),
          ),
        SortableColumn<BacktestSkippedCandidate>(
          label: 'زمان تصمیم (محلی)',
          width: 130,
          sortKey: (BacktestSkippedCandidate s) => s.time,
          cell: (_, BacktestSkippedCandidate s) => TimeCell(s.time),
        ),
        SortableColumn<BacktestSkippedCandidate>(
          label: 'جهت',
          width: 60,
          sortKey: (BacktestSkippedCandidate s) => s.direction,
          cell: (_, BacktestSkippedCandidate s) => Text(directionFa(s.direction)),
        ),
        SortableColumn<BacktestSkippedCandidate>(
          label: 'نوع ستاپ',
          width: 110,
          sortKey: (BacktestSkippedCandidate s) => s.setupType,
          cell: (_, BacktestSkippedCandidate s) => Text(s.setupType ?? kDash),
        ),
        SortableColumn<BacktestSkippedCandidate>(
          label: 'دلیل',
          width: 520,
          sortKey: (BacktestSkippedCandidate s) => s.reason,
          cell: (_, BacktestSkippedCandidate s) => Tooltip(
            message: s.detail ?? s.reasonFa ?? kDash,
            child: Text(s.reasonFa ?? s.reason ?? kDash, maxLines: 2, overflow: TextOverflow.ellipsis),
          ),
        ),
      ],
    );
  }
}
