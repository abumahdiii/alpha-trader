import 'dart:async';

import 'package:flutter/material.dart';

import '../../models/backtest_models.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/scrollable_dialog.dart';
import '../../widgets/status_message.dart';
import 'backtest_controller.dart';
import 'backtest_format.dart';

/// «اجراهای قبلی»: newest first; open a run, delete a finished one (after a
/// confirmation), refresh.
class BacktestRunsList extends StatelessWidget {
  const BacktestRunsList({super.key, required this.controller});

  final BacktestController controller;

  static const String emptyFa = 'هنوز بک‌تستی اجرا نشده است.';
  static const String deleteLabel = 'حذف';

  @override
  Widget build(BuildContext context) {
    final BacktestController c = controller;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 4, 0),
        child: Row(children: [
          Expanded(
            child: Text('${c.runs.length} اجرا (جدیدترین بالا)', style: Theme.of(context).textTheme.bodySmall),
          ),
          if (c.runsLoading) const SizedBox.square(dimension: 16, child: CircularProgressIndicator(strokeWidth: 2)),
          IconButton(
            key: const ValueKey<String>('bt-runs-refresh'),
            tooltip: 'بارگذاری دوباره',
            onPressed: c.runsLoading ? null : () => unawaited(c.loadRuns()),
            icon: const Icon(Icons.refresh),
          ),
        ]),
      ),
      Expanded(child: _content(context)),
    ]);
  }

  Widget _content(BuildContext context) {
    final BacktestController c = controller;
    if (c.runsError != null && c.runs.isEmpty) {
      return StatusMessage.apiError(
        key: const ValueKey<String>('bt-runs-error'),
        title: 'فهرست بک‌تست‌ها بارگذاری نشد',
        error: c.runsError!,
        onRetry: () => unawaited(c.loadRuns()),
      );
    }
    if (c.runs.isEmpty) {
      if (c.runsLoading) return const Center(child: CircularProgressIndicator());
      return const StatusMessage(key: ValueKey<String>('bt-runs-empty'), icon: Icons.inbox_outlined, title: emptyFa);
    }
    return ListView.builder(
      padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
      itemCount: c.runs.length,
      itemBuilder: (BuildContext context, int i) => _RunTile(
        run: c.runs[i],
        selected: c.runs[i].id == c.openRunId,
        onOpen: () => unawaited(c.openRun(c.runs[i].id)),
        onDelete: () => _confirmDelete(context, c.runs[i]),
      ),
    );
  }

  Future<void> _confirmDelete(BuildContext context, BacktestRunSummary run) async {
    final bool? ok = await ScrollableDialog.show<bool>(
      context: context,
      title: Text('حذف بک‌تست #${run.id}؟'),
      content: Text(
        'نتیجه، معاملات و منحنی سرمایه این اجرا (${run.symbol}، ${run.mode.labelFa}) برای همیشه حذف می‌شوند.',
      ),
      actions: [
        TextButton(
          key: const ValueKey<String>('bt-delete-cancel'),
          onPressed: () => Navigator.of(context, rootNavigator: true).pop(false),
          child: const Text('انصراف'),
        ),
        FilledButton(
          key: const ValueKey<String>('bt-delete-confirm'),
          style: FilledButton.styleFrom(backgroundColor: Theme.of(context).colorScheme.error),
          onPressed: () => Navigator.of(context, rootNavigator: true).pop(true),
          child: const Text(deleteLabel),
        ),
      ],
    );
    if (ok != true || !context.mounted) return;
    final EngineApiException? error = await controller.deleteRun(run.id);
    if (!context.mounted) return;
    if (error == null) {
      showSuccessToast(context, title: 'بک‌تست #${run.id} حذف شد');
    } else {
      showErrorToast(context, title: 'حذف بک‌تست #${run.id} ناموفق بود', subtitle: error.messageFa);
    }
  }
}

class _RunTile extends StatelessWidget {
  const _RunTile({required this.run, required this.selected, required this.onOpen, required this.onDelete});

  final BacktestRunSummary run;
  final bool selected;
  final VoidCallback onOpen;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final BacktestRunSummary r = run;
    final TextTheme text = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final String period = r.mode == BacktestMode.manual
        ? fmtPeriod(r.from, r.to)
        : '${fmtInt(r.windowsCount)} پنجره ${fmtInt(r.windowMonths)} ماهه${r.seed == null ? '' : '، seed ${r.seed}'}';
    return Card(
      key: ValueKey<String>('bt-run-${r.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      color: selected ? Theme.of(context).colorScheme.primary.withValues(alpha: 0.10) : null,
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: onOpen,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
          child: Row(children: [
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  Flexible(
                    child: Text(
                      '#${r.id}  ${r.symbol}  ${r.mode.labelFa}',
                      style: text.titleSmall,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  const SizedBox(width: 6),
                  BacktestStatusChip(r.status),
                ]),
                Text(period, style: text.bodySmall),
                Text('ساخته‌شده: ${fmtLocal(r.createdAt)}', style: text.bodySmall?.copyWith(color: colors.mutedText)),
                if (r.status == BacktestStatus.done)
                  Row(children: [
                    LtrText(
                      '${fmtMoney(r.netProfit)} (${fmtPct(r.netProfitPct)})',
                      style:
                          text.bodySmall?.copyWith(color: pnlColor(context, r.netProfit), fontWeight: FontWeight.bold),
                    ),
                    const SizedBox(width: 6),
                    Flexible(
                      child: Text(
                        '${r.isMeanOfWindows ? 'میانگین پنجره — ' : ''}${fmtInt(r.tradeCount)} معامله',
                        style: text.bodySmall,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ])
                else if (r.status.isActive)
                  Text('پیشرفت: ${r.progress == null ? kDash : '${r.progress!.toStringAsFixed(0)}%'}',
                      style: text.bodySmall)
                else if (r.error?.messageFa != null)
                  Text(r.error!.messageFa!,
                      style: text.bodySmall?.copyWith(color: colors.error),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis),
              ]),
            ),
            IconButton(
              key: ValueKey<String>('bt-run-delete-${r.id}'),
              tooltip: r.status.isActive ? 'اجرای فعال حذف نمی‌شود؛ ابتدا آن را لغو کنید' : 'حذف',
              onPressed: r.status.isActive ? null : onDelete,
              icon: Icon(Icons.delete_outline, color: r.status.isActive ? null : colors.error),
            ),
          ]),
        ),
      ),
    );
  }
}
