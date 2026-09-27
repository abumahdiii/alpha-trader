import 'dart:async';

import 'package:flutter/material.dart';

import '../../models/backtest_models.dart';
import '../../theme/app_semantic_colors.dart';
import 'backtest_controller.dart';
import 'backtest_format.dart';

/// Live progress of the followed run (stage in Persian, percent bar,
/// «لغو»), and the final state of a run that did not finish normally.
/// Hidden when there is nothing to show (no run, or the run is done: the
/// result view takes over).
class BacktestProgressPanel extends StatelessWidget {
  const BacktestProgressPanel({super.key, required this.controller});

  final BacktestController controller;

  static const String cancelLabel = 'لغو';

  @override
  Widget build(BuildContext context) {
    final BacktestController c = controller;
    final BacktestProgress? p = c.progress;
    if (p == null || p.isDone) return const SizedBox.shrink();
    final bool active = c.activeRunId != null && !p.isFinal;
    return Card(
      key: const ValueKey<String>('bt-progress'),
      margin: const EdgeInsets.fromLTRB(16, 12, 16, 4),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: active ? _active(context, p) : _final(context, p),
      ),
    );
  }

  Widget _active(BuildContext context, BacktestProgress p) {
    final BacktestController c = controller;
    final TextTheme text = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final double? percent = p.percent;
    final bool cancelling = c.isCancelling || p.cancelRequested;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Row(children: [
        Expanded(
          child: Text(
            'اجرای #${c.activeRunId} — ${cancelling ? 'در حال لغو…' : backtestPhaseFa(p.phase)}'
            '${p.windows != null && p.windows! > 1 && p.window != null ? ' (پنجره ${p.window} از ${p.windows})' : ''}',
            key: const ValueKey<String>('bt-progress-stage'),
            style: text.titleSmall,
          ),
        ),
        LtrText(percent == null ? kDash : '${percent.toStringAsFixed(1)}%',
            key: const ValueKey<String>('bt-progress-percent')),
        const SizedBox(width: 12),
        OutlinedButton.icon(
          key: const ValueKey<String>('bt-cancel'),
          onPressed: cancelling ? null : () => unawaited(c.cancel()),
          icon: const Icon(Icons.stop_circle_outlined, size: 18),
          label: const Text(cancelLabel),
        ),
      ]),
      const SizedBox(height: 10),
      LinearProgressIndicator(
        key: const ValueKey<String>('bt-progress-bar'),
        value: percent == null ? null : (percent / 100).clamp(0.0, 1.0),
        minHeight: 6,
        borderRadius: BorderRadius.circular(3),
      ),
      if (c.progressViaPolling)
        Padding(
          padding: const EdgeInsets.only(top: 6),
          child: Text(
            'اتصال زنده برقرار نشد؛ پیشرفت با استعلام دوره‌ای از موتور به‌روز می‌شود.',
            key: const ValueKey<String>('bt-progress-polling'),
            style: text.bodySmall?.copyWith(color: colors.mutedText),
          ),
        ),
      if (c.cancelError != null)
        Padding(
          padding: const EdgeInsets.only(top: 6),
          child: Text(c.cancelError!.messageFa, style: text.bodySmall?.copyWith(color: colors.error)),
        ),
    ]);
  }

  Widget _final(BuildContext context, BacktestProgress p) {
    final BacktestController c = controller;
    final AppSemanticColors colors = context.appColors;
    final bool lost = p.type == BacktestProgress.lostType;
    final (IconData icon, Color color, String title) = switch (p.type) {
      'cancelled' => (Icons.cancel_outlined, colors.warning, 'اجرای #${p.id ?? ''} لغو شد.'),
      'interrupted' => (Icons.pause_circle_outline, colors.warning, 'اجرای #${p.id ?? ''} نیمه‌کاره ماند.'),
      BacktestProgress.lostType => (Icons.link_off, colors.error, 'پیگیری اجرای #${p.id ?? ''} قطع شد.'),
      _ => (Icons.error_outline, colors.error, 'اجرای #${p.id ?? ''} ناموفق بود.'),
    };
    return Row(key: ValueKey<String>('bt-progress-final-${p.type}'), children: [
      Icon(icon, color: color),
      const SizedBox(width: 10),
      Expanded(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(title, style: Theme.of(context).textTheme.titleSmall),
          if (p.messageFa != null && p.messageFa!.isNotEmpty) Text(p.messageFa!),
        ]),
      ),
      if (lost && p.id != null)
        TextButton.icon(
          onPressed: () {
            unawaited(c.loadRuns());
            unawaited(c.openRun(p.id!));
          },
          icon: const Icon(Icons.refresh),
          label: const Text('بارگذاری دوباره'),
        ),
    ]);
  }
}
