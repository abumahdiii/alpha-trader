import 'package:flutter/material.dart';

import '../../core/number_format.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/scrollable_dialog.dart';
import '../chart_data.dart';
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

/// Full details of one setup, exactly as the engine reported it. Display
/// only — there is deliberately no "send order" action.
class SetupDetails extends StatelessWidget {
  const SetupDetails({super.key, required this.setup, this.digits});

  final SetupItem setup;
  final int? digits;

  static Future<void> show(BuildContext context, SetupItem setup, {int? digits}) => ScrollableDialog.show<void>(
        context: context,
        title: Text('${setup.setupTitleFa} — ${setup.direction.titleFa}'),
        showCloseButton: true,
        content: SetupDetails(setup: setup, digits: digits),
      );

  @override
  Widget build(BuildContext context) {
    final SetupItem s = setup;
    final TextTheme tt = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final bool pending = s.status == SetupStatus.pendingEntry;
    String price(double? v) => formatChartPrice(v, digits);

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
          Chip(label: Text(s.patternTitleFa)),
          Chip(label: Text(s.line.titleFa)),
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
        ] else
          _kv('ورود', price(s.entry)),
        _kv('حد ضرر', price(s.stopLoss)),
        if (!pending) _kv('حد سود', price(s.takeProfit)),
        _kv('R:R', formatValue(s.rr, 2)),
        if (s.riskDistance != null) _kv('فاصله ریسک', price(s.riskDistance)),
        if (s.lineValue != null) _kv('مقدار ${s.line.titleFa}', price(s.lineValue)),
        const Divider(),
        _kv('حجم پیشنهادی (لات)', s.volume == null ? '—' : formatNumber(s.volume!, decimals: 2)),
        _kv('ریسک واقعی', s.actualRisk == null ? '—' : formatNumber(s.actualRisk!, decimals: 2)),
        _kv('مبلغ ریسک', s.riskAmount == null ? '—' : formatNumber(s.riskAmount!, decimals: 2)),
        _kv('مارجین', s.margin == null ? '—' : formatNumber(s.margin!, decimals: 2)),
        if (s.volumeNoteFa != null) _Note(text: s.volumeNoteFa!, color: colors.warning),
        for (final String w in s.sizingWarningsFa) _Note(text: w, color: colors.warning),
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

  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(children: <Widget>[
          Expanded(child: Text(k)),
          Text(v, textDirection: TextDirection.ltr),
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

/// Side table of the setups in the loaded range. Tapping a row selects the
/// setup and jumps the chart to it.
class SetupTable extends StatelessWidget {
  const SetupTable({
    super.key,
    required this.setups,
    required this.onSelect,
    this.selectedId,
    this.digits,
    this.emptyText,
    this.noteFa,
  });

  final List<SetupMark> setups;
  final String? selectedId;
  final int? digits;
  final ValueChanged<SetupMark> onSelect;

  /// Shown instead of the list when there is nothing to show.
  final String? emptyText;
  final String? noteFa;

  @override
  Widget build(BuildContext context) {
    final TextTheme tt = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    if (setups.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Text(
            emptyText ?? 'در این بازه ستاپی پیدا نشد.',
            textAlign: TextAlign.center,
            style: tt.bodyMedium?.copyWith(color: colors.mutedText),
          ),
        ),
      );
    }
    String price(double? v) => formatChartPrice(v, digits);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.fromLTRB(8, 8, 8, 4),
          child: Text('${setups.length} ستاپ در بازه (زمان‌ها UTC)', style: tt.labelLarge),
        ),
        Expanded(
          child: ListView.separated(
            key: const ValueKey<String>('setup-table'),
            itemCount: setups.length,
            separatorBuilder: (_, __) => const Divider(height: 1),
            itemBuilder: (BuildContext context, int i) {
              final SetupMark m = setups[i];
              final SetupItem s = m.item;
              final bool selected = s.id == selectedId;
              final bool pending = s.status == SetupStatus.pendingEntry;
              final Color sideColor = s.direction == TradeSide.buy ? colors.success : colors.error;
              return Material(
                color: selected ? Theme.of(context).colorScheme.primary.withValues(alpha: 0.12) : Colors.transparent,
                child: InkWell(
                  key: ValueKey<String>('setup-row-${s.id}'),
                  onTap: () => onSelect(m),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
                    child: DefaultTextStyle.merge(
                      style: const TextStyle(fontSize: 12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: <Widget>[
                          Row(children: <Widget>[
                            Text(s.direction.titleFa, style: TextStyle(color: sideColor, fontWeight: FontWeight.bold)),
                            const SizedBox(width: 6),
                            Expanded(child: Text(s.setupTitleFa, overflow: TextOverflow.ellipsis)),
                            Text(s.status.titleFa, style: TextStyle(color: setupStatusColor(context, s.status))),
                          ]),
                          const SizedBox(height: 2),
                          Row(children: <Widget>[
                            Text(formatMt5Time(s.confirmationBarTime), textDirection: TextDirection.ltr),
                            const Spacer(),
                            Text(
                              s.volume == null ? 'حجم: —' : 'حجم: ${formatNumber(s.volume!, decimals: 2)}',
                            ),
                          ]),
                          const SizedBox(height: 2),
                          Text(
                            pending
                                ? 'Ref* ${price(s.referencePrice)}  SL ${price(s.stopLoss)}  '
                                    'TP* ${price(s.indicativeTakeProfit)}'
                                : 'Entry ${price(s.entry)}  SL ${price(s.stopLoss)}  TP ${price(s.takeProfit)}',
                            textDirection: TextDirection.ltr,
                            textAlign: TextAlign.right,
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              );
            },
          ),
        ),
        if (noteFa != null)
          Padding(
            padding: const EdgeInsets.all(8),
            child: Text(noteFa!, style: tt.bodySmall?.copyWith(color: colors.mutedText)),
          ),
      ],
    );
  }
}
