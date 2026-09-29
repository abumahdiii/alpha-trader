import 'package:flutter/material.dart';

import '../../core/number_format.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/scrollable_dialog.dart';
import '../chart_format.dart';
import '../../models/chart_models.dart';

/// Colour of a setup status (same meaning as on the chart).
Color setupStatusColor(BuildContext context, SetupStatus s) {
  final AppSemanticColors c = context.appColors;
  return switch (s) {
    SetupStatus.accepted => c.success,
    SetupStatus.pendingEntry => c.warning,
    SetupStatus.rejected => c.mutedText,
  };
}

/// Buy green, sell red.
Color tradeSideColor(BuildContext context, TradeSide side) =>
    side == TradeSide.buy ? context.appColors.success : context.appColors.error;

/// Colour of an engine P&L / R value: green above zero, red below.
Color? pnlColor(BuildContext context, double? v) {
  if (v == null || v == 0) return null;
  return v > 0 ? context.appColors.success : context.appColors.error;
}

/// Full details of one setup, exactly as the engine reported it (levels,
/// sizing, the independent outcome and the range-backtest flag). Display
/// only — there is deliberately no "send order" action.
class SetupDetails extends StatelessWidget {
  const SetupDetails({super.key, required this.setup, this.digits, this.endOfDataLabelFa});

  final SetupItem setup;
  final int? digits;

  /// The engine's explanation of a «پایان داده» result.
  final String? endOfDataLabelFa;

  static Future<void> show(BuildContext context, SetupItem setup, {int? digits, String? endOfDataLabelFa}) =>
      ScrollableDialog.show<void>(
        context: context,
        title: Text('${setup.setupTitleFa} — ${setup.direction.titleFa}'),
        showCloseButton: true,
        content: SetupDetails(setup: setup, digits: digits, endOfDataLabelFa: endOfDataLabelFa),
      );

  @override
  Widget build(BuildContext context) {
    final SetupItem s = setup;
    final TextTheme tt = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final bool pending = s.status == SetupStatus.pendingEntry;
    final SetupOutcome? o = s.outcome;
    final SetupBacktestFlag? bt = s.backtest;
    String price(double? v) => formatChartPrice(v, digits);
    String money(double? v) => formatSigned(v, 2);

    return Column(
      key: const ValueKey<String>('setup-details'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Wrap(spacing: 8, runSpacing: 4, children: <Widget>[
          Chip(
            label: Text(s.status.titleFa),
            backgroundColor: setupStatusColor(context, s.status).withValues(alpha: 0.18),
          ),
          Chip(label: Text(s.direction.titleFa)),
          if (s.pattern.isNotEmpty) Chip(label: Text(s.patternTitleFa)),
          if (s.line != null) Chip(label: Text(s.line!.titleFa)),
        ]),
        if (s.rejectionReasonFa != null) _Note(text: 'دلیل رد: ${s.rejectionReasonFa}', color: colors.error),
        const SizedBox(height: 8),
        _kv('زمان کندل تایید (UTC)', formatMt5Time(s.confirmationBarTime)),
        _kv('زمان تصمیم (UTC)', formatMt5Time(s.decisionTime)),
        _kv('زمان ورود (UTC)', s.entryTime == null ? 'هنوز باز نشده' : formatMt5Time(s.entryTime!)),
        const Divider(),
        if (pending) ...<Widget>[
          _kv('قیمت مرجع (تقریبی)', price(s.referencePrice)),
          _kv('حد سود تقریبی', price(s.indicativeTakeProfit)),
        ] else ...<Widget>[
          _kv('ورود (fill؛ خرید با ask، فروش با bid)', price(s.entry)),
          if (s.entryBidOpen != null) _kv('open کندل ورود (bid)', price(s.entryBidOpen)),
          if (s.spreadAtEntryPoints != null)
            _kv(
              'اسپرد ورود (پوینت)${s.entrySpreadSource == null ? '' : ' — ${s.entrySpreadSource!.titleFa}'}',
              '${s.spreadAtEntryPoints}',
            ),
        ],
        _kv('حد ضرر', price(s.stopLoss)),
        if (!pending) _kv('حد سود', price(s.takeProfit)),
        _kv('R:R', formatValue(s.rr, 2)),
        if (s.riskDistance != null) _kv('فاصله ریسک', price(s.riskDistance)),
        if (s.line != null && s.lineValue != null) _kv('مقدار ${s.line!.titleFa}', price(s.lineValue)),
        const Divider(),
        _kv('حجم پیشنهادی (لات)', s.volume == null ? '—' : formatNumber(s.volume!, decimals: 2)),
        _kv('ریسک واقعی', s.actualRisk == null ? '—' : formatNumber(s.actualRisk!, decimals: 2)),
        _kv('مبلغ ریسک', s.riskAmount == null ? '—' : formatNumber(s.riskAmount!, decimals: 2)),
        _kv('مارجین', s.margin == null ? '—' : formatNumber(s.margin!, decimals: 2)),
        if (s.volumeNoteFa != null) _Note(text: s.volumeNoteFa!, color: colors.warning),
        for (final String w in s.sizingWarningsFa) _Note(text: w, color: colors.warning),
        if (o != null) ...<Widget>[
          const Divider(),
          Text('نتیجه (ارزیابی مستقل این ستاپ)', style: tt.labelLarge),
          const SizedBox(height: 4),
          _kvFa('نتیجه', o.result.titleFa),
          _kvFa('دلیل خروج', o.exitReasonFa),
          _kv('زمان خروج (UTC)', formatMt5Time(o.exitTime)),
          _kv('قیمت خروج', price(o.exitPrice)),
          _kv('تغییر قیمت به نفع معامله', formatSigned(o.pnlPrice, digits ?? 5)),
          _kv('سود ناخالص (\$)', money(o.grossPnl)),
          _kv('کمیسیون (\$)', formatValue(o.commission, 2)),
          _kv('سود خالص (\$)', money(o.netPnl), color: pnlColor(context, o.netPnl)),
          _kv('R', formatSigned(o.rMultiple, 2), color: pnlColor(context, o.rMultiple)),
          _kv('تعداد کندل نگهداری', '${o.barsHeld}'),
          _kvFa('نگهداری در آخر هفته', o.heldOverWeekend ? 'بله' : 'خیر'),
          if (o.flags.isNotEmpty) _kv('پرچم‌ها', o.flags.join(', ')),
          // The engine's end-of-data explanation, unless «دلیل خروج» above already says exactly that.
          if (o.result == SetupResult.endOfData &&
              (endOfDataLabelFa ?? '').isNotEmpty &&
              endOfDataLabelFa != o.exitReasonFa)
            _Note(text: endOfDataLabelFa!, color: colors.warning),
        ],
        if (bt != null) ...<Widget>[
          const Divider(),
          _kvFa(
            'در بک‌تست همین بازه',
            bt.traded
                ? 'معامله شد${bt.tradeIndex == null ? '' : ' (معامله ${bt.tradeIndex})'}'
                    '${bt.netPnl == null ? '' : '، سود خالص بک‌تست ${money(bt.netPnl)}'}'
                : 'معامله نشد: ${bt.reasonFa ?? bt.reason ?? '—'}',
            color: bt.traded ? colors.success : colors.mutedText,
          ),
        ],
        const Divider(),
        Text('دلیل', style: tt.labelLarge),
        const SizedBox(height: 4),
        Text(s.reasonFa),
        if (pending)
          _Note(
            text: 'قیمت‌های «تقریبی» از بسته شدن کندل تایید آمده‌اند؛ ورود، حد سود و حجم واقعی بعد از باز شدن '
                'کندل بعدی مشخص می‌شوند.',
            color: colors.warning,
          ),
        const SizedBox(height: 8),
        Text(
          'این فقط پیشنهاد است؛ برنامه هیچ سفارشی ثبت نمی‌کند.',
          style: tt.bodySmall?.copyWith(color: colors.mutedText),
        ),
      ],
    );
  }

  /// A number (Latin digits, left-to-right).
  Widget _kv(String k, String v, {Color? color}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(children: <Widget>[
          Expanded(child: Text(k)),
          Text(v, textDirection: TextDirection.ltr, style: TextStyle(color: color)),
        ]),
      );

  /// A Persian text value (may wrap).
  Widget _kvFa(String k, String v, {Color? color}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
          Expanded(child: Text(k)),
          const SizedBox(width: 8),
          Flexible(flex: 2, child: Text(v, textAlign: TextAlign.end, style: TextStyle(color: color))),
        ]),
      );
}

class _Note extends StatelessWidget {
  const _Note({required this.text, required this.color});

  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 6),
        child: Text(text, style: TextStyle(color: color)),
      );
}
