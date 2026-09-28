import 'package:flutter/material.dart';

import '../../core/dev_mode.dart';
import '../../models/chart_models.dart';
import '../../theme/app_semantic_colors.dart';
import '../backtest_range_request.dart';
import '../chart_format.dart';
import 'setup_widgets.dart';

/// «خلاصه بازه» above the setups table: counts, the engine's independent
/// evaluation of the accepted setups and the range backtest comparison.
/// Every number is the engine's (`summary`, `evaluation`, `backtest_window`);
/// nothing is added up here.
class SetupsSummaryBar extends StatelessWidget {
  const SetupsSummaryBar({super.key, required this.result, this.onBacktestRange});

  final SetupsResult result;

  /// «بک‌تست همین بازه»; null = not wired yet (button disabled).
  final ValueChanged<BacktestRangeRequest>? onBacktestRange;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final SetupsSummary? s = result.summary;
    final SetupsEvaluation? ev = result.evaluation;
    final bool evaluated = result.evaluationAvailable && s != null;
    final BacktestWindow? w = result.backtestWindow;

    int count(String code, int? fromSummary) => fromSummary ?? result.statusCounts[code] ?? 0;
    final List<Widget> stats = <Widget>[
      _Stat(key: const ValueKey<String>('summary-total'), label: 'کل ستاپ‌ها', value: '${s?.total ?? result.count}'),
      _Stat(
        key: const ValueKey<String>('summary-accepted'),
        label: SetupStatus.accepted.titleFa,
        value: '${count('accepted', s?.accepted)}',
        color: setupStatusColor(context, SetupStatus.accepted),
      ),
      _Stat(
        key: const ValueKey<String>('summary-rejected'),
        label: SetupStatus.rejected.titleFa,
        value: '${count('rejected', s?.rejected)}',
      ),
      _Stat(
        key: const ValueKey<String>('summary-pending'),
        label: SetupStatus.pendingEntry.titleFa,
        value: '${count('pending_entry', s?.pendingEntry)}',
        color: setupStatusColor(context, SetupStatus.pendingEntry),
      ),
      if (evaluated) ...<Widget>[
        const SizedBox(width: 8),
        _Stat(key: const ValueKey<String>('summary-wins'), label: 'برد', value: '${s.wins}', color: colors.success),
        _Stat(key: const ValueKey<String>('summary-losses'), label: 'باخت', value: '${s.losses}', color: colors.error),
        if (s.breakeven > 0)
          _Stat(key: const ValueKey<String>('summary-breakeven'), label: 'سر به سر', value: '${s.breakeven}'),
        _Stat(
          key: const ValueKey<String>('summary-open'),
          label: 'باز در پایان داده',
          value: '${s.openEndOfData}',
          color: s.openEndOfData > 0 ? colors.warning : null,
          tooltip: '${ev!.endOfDataLabelFa}\nسود/زیان خالص این ستاپ‌های باز (جدا از جمع‌ها): '
              '${formatSigned(s.openNetPnl, 2)} \$',
        ),
        _Stat(
          key: const ValueKey<String>('summary-win-rate'),
          label: 'درصد برد',
          value: formatFractionPct(s.winRate),
          tooltip: 'برد تقسیم بر ستاپ‌های بسته‌شده با حد سود یا حد ضرر (${s.closed} ستاپ)',
        ),
        _Stat(
          key: const ValueKey<String>('summary-net-pnl'),
          label: 'سود/زیان خالص',
          value: '${formatSigned(s.netPnl, 2)} \$',
          color: pnlColor(context, s.netPnl),
          tooltip: 'سود ناخالص ${formatValue(s.grossProfit, 2)} \$ — زیان ناخالص ${formatValue(s.grossLoss, 2)} \$',
        ),
        _Stat(
          key: const ValueKey<String>('summary-total-r'),
          label: 'جمع R',
          value: formatSigned(s.totalR, 2),
          color: pnlColor(context, s.totalR),
          tooltip: 'میانگین R: ${formatSigned(s.avgR, 2)} (${s.rCount} ستاپ)',
        ),
        _Stat(
          key: const ValueKey<String>('summary-pf'),
          label: 'PF',
          value: formatProfitFactor(s.profitFactor, infinite: s.profitFactorInfinite),
        ),
      ],
    ];

    return Padding(
      key: const ValueKey<String>('setups-summary-bar'),
      padding: const EdgeInsets.fromLTRB(8, 4, 8, 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Wrap(spacing: 6, runSpacing: 4, crossAxisAlignment: WrapCrossAlignment.center, children: stats),
          const SizedBox(height: 6),
          if (ev != null && !ev.available)
            Text(
              ev.messageFa ?? 'نتیجه ستاپ‌ها در دسترس نیست.',
              key: const ValueKey<String>('summary-evaluation-unavailable'),
              style: tt.bodySmall?.copyWith(color: colors.warning),
            )
          else if (ev != null)
            _EvaluationLine(evaluation: ev),
          const SizedBox(height: 4),
          _BacktestLine(result: result, window: w, onBacktestRange: onBacktestRange),
        ],
      ),
    );
  }
}

/// The explicit evaluation label + the cost / data labels.
class _EvaluationLine extends StatelessWidget {
  const _EvaluationLine({required this.evaluation});

  final SetupsEvaluation evaluation;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final SetupsEvaluation ev = evaluation;
    final SetupsCostModel? cm = ev.costModel;
    final SetupsSpreadFallback? fb = ev.spreadFallback;
    final List<String> costLines = <String>[
      if (cm != null) 'کمیسیون هر لات در هر طرف: ${formatValue(cm.commissionPerLotPerSide, 2)} \$',
      if (fb != null)
        'اسپرد جایگزین: ${fb.points} پوینت (${_fallbackSourceFa(fb.source)}) — ${fb.fallbackBars} از ${fb.totalBars} کندل با آن '
            'قیمت‌گذاری شد',
    ];
    return Wrap(
      spacing: 6,
      runSpacing: 4,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: <Widget>[
        Container(
          key: const ValueKey<String>('summary-evaluation-label'),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
          decoration: BoxDecoration(
            color: colors.warningContainer,
            borderRadius: BorderRadius.circular(6),
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
            Icon(Icons.info_outline, size: 16, color: colors.onWarningContainer),
            const SizedBox(width: 6),
            Text(
              ev.labelFa,
              style: tt.bodySmall?.copyWith(color: colors.onWarningContainer, fontWeight: FontWeight.bold),
            ),
          ]),
        ),
        for (final String label in ev.labelsFa)
          Tooltip(
            message: costLines.isEmpty ? label : '$label\n${costLines.join('\n')}',
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
              decoration: BoxDecoration(
                border: Border.all(color: colors.borderColor),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(label, style: tt.labelSmall?.copyWith(color: colors.mutedText)),
            ),
          ),
      ],
    );
  }
}

/// «بک‌تست همین بازه: N معامله، سود خالص X» + note + the button.
class _BacktestLine extends StatelessWidget {
  const _BacktestLine({required this.result, required this.window, required this.onBacktestRange});

  final SetupsResult result;
  final BacktestWindow? window;
  final ValueChanged<BacktestRangeRequest>? onBacktestRange;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final BacktestWindow? w = window;
    final BacktestRangeRequest? request = BacktestRangeRequest.of(result);
    final ValueChanged<BacktestRangeRequest>? onPressed = onBacktestRange;

    final String compare;
    if (w == null) {
      compare = 'مقایسه با بک‌تست در دسترس نیست.';
    } else if (w.available) {
      compare = 'بک‌تست همین بازه: ${w.trades ?? '—'} معامله، سود خالص ${formatSigned(w.netProfit, 2)} \$';
    } else {
      compare = w.messageFa ?? 'بک‌تست این بازه ممکن نیست.';
    }
    final String tooltip = request != null
        ? 'بازه بک‌تست (از engine): ${formatUtc(request.from)} تا ${formatUtc(request.to)} (پایان جزو بازه نیست)'
            '${onPressed == null ? '\nاتصال به صفحه بک‌تست هنوز فعال نشده است.' : ''}'
        : (w?.messageFa ?? 'بازه بک‌تست در دسترس نیست.');

    return Wrap(
      spacing: 8,
      runSpacing: 4,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: <Widget>[
        Text(
          compare,
          key: const ValueKey<String>('summary-backtest-compare'),
          style: tt.bodySmall?.copyWith(
            color: w != null && !w.available ? colors.warning : null,
            fontWeight: FontWeight.w600,
          ),
        ),
        if (w?.noteFa != null)
          Text(
            w!.noteFa!,
            key: const ValueKey<String>('summary-backtest-note'),
            style: tt.bodySmall?.copyWith(color: colors.mutedText),
          ),
        Tooltip(
          message: tooltip,
          child: OutlinedButton.icon(
            key: const ValueKey<String>('setups-backtest-range'),
            style: OutlinedButton.styleFrom(visualDensity: VisualDensity.compact),
            onPressed: request != null && onPressed != null
                ? () {
                    devLog('[Setups] «بک‌تست همین بازه» pressed: $request');
                    onPressed(request);
                  }
                : null,
            icon: const Icon(Icons.play_arrow, size: 18),
            label: const Text('بک‌تست همین بازه'),
          ),
        ),
      ],
    );
  }
}

String _fallbackSourceFa(String source) => switch (source) {
      'auto_median_observed' => 'میانه اسپرد مشاهده‌شده',
      'user' => 'تعیین کاربر',
      'none' => 'اسپردی مشاهده نشد',
      _ => source,
    };

class _Stat extends StatelessWidget {
  const _Stat({super.key, required this.label, required this.value, this.color, this.tooltip});

  final String label;
  final String value;
  final Color? color;
  final String? tooltip;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final Widget chip = Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: colors.surfaceMuted,
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
        Text('$label: ', style: TextStyle(fontSize: 12, color: colors.mutedText)),
        Text(
          value,
          textDirection: TextDirection.ltr,
          style: TextStyle(fontSize: 12, fontWeight: FontWeight.bold, color: color),
        ),
      ]),
    );
    return tooltip == null ? chip : Tooltip(message: tooltip, child: chip);
  }
}
