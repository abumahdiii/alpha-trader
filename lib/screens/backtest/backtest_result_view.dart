import 'dart:async';

import 'package:flutter/material.dart';

import '../../models/account_settings.dart';
import '../../models/backtest_models.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/status_message.dart';
import 'backtest_controller.dart';
import 'backtest_format.dart';
import 'backtest_tables.dart';
import 'equity_chart.dart';
import 'metric_cards.dart';

/// The opened run: a header with everything that defines it (period or
/// windows + seed, strategy version / params hash, account, costs) and the
/// engine's honest labels, then tabs with metrics, windows (random),
/// trades, equity curve and skipped candidates.
class BacktestResultView extends StatelessWidget {
  const BacktestResultView({super.key, required this.controller});

  final BacktestController controller;

  static const String noRunsFa = 'هنوز بک‌تستی اجرا نشده است. فرم «اجرای جدید» را پر کنید و «اجرای بک‌تست» را بزنید.';
  static const String pickRunFa = 'یک اجرا را از «اجراهای قبلی» باز کنید یا اجرای جدیدی بسازید.';

  @override
  Widget build(BuildContext context) {
    final BacktestController c = controller;
    final BacktestRunDetail? d = c.detail;
    if (c.openRunId == null) {
      final bool none = c.initialized && c.runs.isEmpty && c.runsError == null;
      return StatusMessage(
        key: const ValueKey<String>('bt-result-empty'),
        icon: Icons.history,
        title: none ? 'هنوز بک‌تستی وجود ندارد' : 'نتیجه‌ای باز نشده است',
        message: none ? noRunsFa : pickRunFa,
      );
    }
    if (d == null) {
      if (c.detailError != null) {
        return StatusMessage.apiError(
          key: const ValueKey<String>('bt-result-error'),
          title: 'بارگذاری بک‌تست #${c.openRunId} ناموفق بود',
          error: c.detailError!,
          onRetry: () => unawaited(c.openRun(c.openRunId!)),
        );
      }
      return const Center(child: CircularProgressIndicator());
    }
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      _RunHeader(detail: d, onClose: c.closeRun),
      const Divider(height: 1),
      Expanded(child: _body(context, d)),
    ]);
  }

  Widget _body(BuildContext context, BacktestRunDetail d) {
    final BacktestController c = controller;
    switch (d.status) {
      case BacktestStatus.queued:
      case BacktestStatus.running:
        return const StatusMessage(
          key: ValueKey<String>('bt-result-running'),
          icon: Icons.hourglass_top,
          title: 'این اجرا هنوز در حال انجام است',
          message: 'نتیجه بعد از پایان اجرا خودکار نمایش داده می‌شود.',
        );
      case BacktestStatus.done:
        break;
      default:
        return StatusMessage(
          key: const ValueKey<String>('bt-result-failed'),
          icon: d.status == BacktestStatus.error ? Icons.error_outline : Icons.cancel_outlined,
          isError: d.status == BacktestStatus.error,
          title: switch (d.status) {
            BacktestStatus.cancelled => 'این اجرا لغو شد',
            BacktestStatus.interrupted => 'این اجرا نیمه‌کاره ماند',
            _ => 'این اجرا ناموفق بود',
          },
          message: [
            if (d.summary.error?.messageFa != null) d.summary.error!.messageFa!,
            if (d.summary.error?.code != null) '(${d.summary.error!.code})',
          ].join(' '),
        );
    }
    final bool random = d.mode == BacktestMode.random;
    final int trades = d.summary.tradeCount ?? c.trades?.total ?? 0;
    final List<(String, String, Widget)> tabs = [
      ('metrics', 'معیارها', _metricsTab(d)),
      if (random) ('windows', 'پنجره‌ها (${d.windows.length})', _windowsTab(context, d)),
      ('trades', 'معاملات ($trades)', _tradesTab(context, d)),
      ('equity', 'منحنی سرمایه', _equityTab(context, d)),
      ('skipped', 'ردشده‌ها${c.skipped == null ? '' : ' (${c.skipped!.skipped.length})'}', _skippedTab(context, d)),
    ];
    return DefaultTabController(
      key: ValueKey<String>('bt-tabs-${d.id}-${tabs.length}'),
      length: tabs.length,
      child: Column(children: [
        TabBar(
          isScrollable: true,
          tabAlignment: TabAlignment.start,
          tabs: [for (final (String k, String label, _) in tabs) Tab(key: ValueKey<String>('bt-tab-$k'), text: label)],
        ),
        Expanded(child: TabBarView(children: [for (final (_, _, Widget w) in tabs) w])),
      ]),
    );
  }

  Widget _metricsTab(BacktestRunDetail d) {
    if (d.mode == BacktestMode.random) return RandomRunMetrics(detail: d);
    final BacktestMetrics? m = d.metrics;
    if (m == null) {
      return const StatusMessage(icon: Icons.info_outline, title: 'موتور معیاری برای این اجرا نفرستاد');
    }
    return SingleWindowMetrics(
      metrics: m,
      stoppedReason: d.windows.isEmpty ? null : d.windows.first.stoppedReason,
    );
  }

  Widget _windowsTab(BuildContext context, BacktestRunDetail d) => Column(children: [
        Padding(
          padding: const EdgeInsets.all(8),
          child: Text(
            'برای دیدن معاملات، منحنی سرمایه و ستاپ‌های ردشده یک پنجره، روی آن کلیک کنید.',
            style: TextStyle(color: context.appColors.mutedText, fontSize: 12),
          ),
        ),
        Expanded(
          child: BacktestWindowsTable(
            windows: d.windows,
            selected: controller.selectedWindow,
            onSelect: controller.selectWindow,
          ),
        ),
      ]);

  /// Loading / error state of the result lists (trades, equity, skipped).
  Widget? _resultsState(BuildContext context, Object? loaded) {
    final BacktestController c = controller;
    if (loaded != null) return null;
    if (c.resultsLoading) return const Center(child: CircularProgressIndicator());
    if (c.resultsError != null) {
      return StatusMessage.apiError(
        title: 'بارگذاری نتایج ناموفق بود',
        error: c.resultsError!,
        onRetry: () => unawaited(c.openRun(c.openRunId!)),
      );
    }
    return const SizedBox.shrink();
  }

  Widget _windowFilter(BuildContext context, BacktestRunDetail d, {bool allowAll = true}) {
    final BacktestController c = controller;
    final int? value = allowAll ? c.selectedWindow : (c.selectedWindow ?? 0);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Row(children: [
        const Text('پنجره: '),
        const SizedBox(width: 8),
        DropdownButton<int?>(
          key: const ValueKey<String>('bt-window-filter'),
          value: value,
          items: [
            if (allowAll) const DropdownMenuItem<int?>(value: null, child: Text('همه پنجره‌ها')),
            for (final BacktestWindowResult w in d.windows)
              DropdownMenuItem<int?>(
                value: w.index,
                child: Text('پنجره ${w.index + 1} (${fmtDay(w.start)} تا ${fmtDay(w.end)})'),
              ),
          ],
          onChanged: c.selectWindow,
        ),
      ]),
    );
  }

  Widget _tradesTab(BuildContext context, BacktestRunDetail d) {
    final BacktestController c = controller;
    final Widget? state = _resultsState(context, c.trades);
    if (state != null) return state;
    final BacktestTradesPage page = c.trades!;
    final bool random = d.mode == BacktestMode.random;
    final int? w = c.selectedWindow;
    final List<BacktestTrade> rows = random && w != null
        ? [
            for (final BacktestTrade t in page.trades)
              if (t.windowIndex == w) t
          ]
        : page.trades;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (random) _windowFilter(context, d),
      if (page.isPartial)
        Padding(
          padding: const EdgeInsets.all(8),
          child: Text(
            'نمایش ${page.trades.length} معامله از ${page.total} (سقف هر درخواست موتور).',
            key: const ValueKey<String>('bt-trades-partial'),
            style: TextStyle(color: context.appColors.warning),
          ),
        ),
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        child: Text(
          'زمان‌ها به وقت محلی؛ نشانگر ماوس روی زمان، UTC را نشان می‌دهد. برای جزئیات روی یک معامله کلیک کنید.',
          style: TextStyle(color: context.appColors.mutedText, fontSize: 12),
        ),
      ),
      Expanded(
        child: rows.isEmpty
            ? const StatusMessage(
                key: ValueKey<String>('bt-trades-empty'),
                icon: Icons.inbox_outlined,
                title: kZeroTradesFa,
              )
            : BacktestTradesTable(trades: rows, digits: c.openRunDigits, showWindow: random),
      ),
    ]);
  }

  Widget _equityTab(BuildContext context, BacktestRunDetail d) {
    final BacktestController c = controller;
    final Widget? state = _resultsState(context, c.equity);
    if (state != null) return state;
    final bool random = d.mode == BacktestMode.random;
    final int window = c.selectedWindow ?? 0;
    final List<BacktestEquityPoint> points = random ? c.equity!.ofWindow(window) : c.equity!.points;
    final BacktestChartStyle style = BacktestChartStyle.of(context);
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (random) _windowFilter(context, d, allowAll: false),
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        child: Wrap(spacing: 16, runSpacing: 4, crossAxisAlignment: WrapCrossAlignment.center, children: [
          ChartLegendItem(color: style.balance, label: 'موجودی (تحقق‌یافته)'),
          ChartLegendItem(color: style.equity, label: 'اکوییتی (با سود/زیان شناور)'),
          Text(
            'منحنی ذخیره‌شده خلاصه‌شده است (${points.length} نقطه)؛ معیارها روی منحنی کامل حساب شده‌اند. '
            'محور زمان به وقت محلی.',
            style: TextStyle(color: context.appColors.mutedText, fontSize: 12),
          ),
        ]),
      ),
      Expanded(
        child: points.isEmpty
            ? const StatusMessage(icon: Icons.show_chart, title: 'نقطه‌ای برای منحنی سرمایه ذخیره نشده است')
            : Padding(
                padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
                child: EquityChart(key: const ValueKey<String>('bt-equity-chart'), points: points),
              ),
      ),
    ]);
  }

  Widget _skippedTab(BuildContext context, BacktestRunDetail d) {
    final BacktestController c = controller;
    final Widget? state = _resultsState(context, c.skipped);
    if (state != null) return state;
    final bool random = d.mode == BacktestMode.random;
    final int? w = c.selectedWindow;
    final List<BacktestSkippedCandidate> all = c.skipped!.skipped;
    final List<BacktestSkippedCandidate> rows = random && w != null
        ? [
            for (final BacktestSkippedCandidate s in all)
              if (s.windowIndex == w) s
          ]
        : all;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (random) _windowFilter(context, d),
      Expanded(
        child: rows.isEmpty
            ? const StatusMessage(
                key: ValueKey<String>('bt-skipped-empty'),
                icon: Icons.inbox_outlined,
                title: 'هیچ ستاپی کنار گذاشته نشد',
              )
            : BacktestSkippedTable(skipped: rows, showWindow: random),
      ),
    ]);
  }
}

class _RunHeader extends StatelessWidget {
  const _RunHeader({required this.detail, required this.onClose});

  final BacktestRunDetail detail;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context) {
    final BacktestRunDetail d = detail;
    final BacktestRunSummary s = d.summary;
    final BacktestConfig? cfg = d.config;
    final TextTheme text = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final String? provisional = d.provisionalLabelFa ?? (s.provisional ? 'موقت تا تایید چک داده' : null);
    final List<String> labels = [
      for (final String l in d.labelsFa)
        if (l != provisional) l
    ];
    final int? seed = d.seed;
    final bool? seedGenerated = d.plan?.seedGenerated ?? s.seedGenerated;
    final AccountSettings? account = cfg?.account;
    final BacktestCostModel? cost = cfg?.costModel;
    final BacktestSpreadFallback? fb = d.spreadFallback;
    final String? hash = s.paramsHash ?? cfg?.paramsHash;

    final List<Widget> facts = [
      if (d.mode == BacktestMode.manual)
        _fact(context, Icons.date_range, 'بازه: ${fmtPeriod(s.from ?? cfg?.start, s.to ?? cfg?.end)} (UTC، انتها باز)')
      else
        _fact(
          context,
          Icons.shuffle,
          '${fmtInt(s.windowsCount ?? cfg?.windowsCount)} پنجره ${fmtInt(s.windowMonths ?? cfg?.windowMonths)} ماهه',
        ),
      if (d.mode == BacktestMode.random)
        _fact(
          context,
          Icons.casino_outlined,
          'seed: ${seed ?? 'هنوز ساخته نشده'}${seedGenerated == true ? ' (ساخته‌شده توسط موتور)' : ''}',
          key: const ValueKey<String>('bt-header-seed'),
          tooltip:
              d.plan?.algorithm == null ? null : 'الگوریتم ${d.plan!.algorithm} — numpy ${d.plan!.numpyVersion ?? ''}',
        ),
      _fact(
        context,
        Icons.tune,
        'استراتژی: ${s.strategy ?? cfg?.strategyName ?? kDash} v${s.strategyVersion ?? cfg?.strategyVersion ?? '?'}'
        ' — پارامترها نسخه ${s.paramsVersion ?? cfg?.paramsVersion ?? 'موقت'}'
        '${hash == null ? '' : ' — هش ${hash.length > 12 ? hash.substring(0, 12) : hash}'}',
        key: const ValueKey<String>('bt-header-strategy'),
        tooltip: [
          if (hash != null) 'هش کامل پارامترها: $hash',
          if (cfg != null && cfg.params.isNotEmpty) 'پارامترها: ${cfg.params}',
        ].join('\n'),
      ),
      if (account != null)
        _fact(
          context,
          Icons.account_balance_wallet_outlined,
          'موجودی اولیه ${fmtMoney(account.balance, signed: false)}، ریسک ${account.riskPct}%، '
          'اهرم 1:${account.leverage}، R:R ${account.rr}',
        ),
      if (cost != null)
        _fact(
          context,
          Icons.payments_outlined,
          'کمیسیون هر لات در هر طرف: ${fmtMoney(cost.commissionPerLotPerSide, signed: false)}',
        ),
      if (fb != null)
        _fact(
          context,
          Icons.swap_vert,
          'اسپرد جایگزین: ${fmtInt(fb.points)} پوینت (${fb.sourceFa}) — برای ${fmtInt(fb.fallbackBars)} کندل از '
          '${fmtInt(fb.totalBars)}${(fb.zeroBars ?? 0) > 0 ? '؛ ${fmtInt(fb.zeroBars)} کندل با اسپرد صفر' : ''}',
          key: const ValueKey<String>('bt-header-spread'),
          tooltip: fb.observedFrom == null
              ? null
              : 'اسپرد ثبت‌شده بروکر از ${fmtUtc(fb.observedFrom)} تا ${fmtUtc(fb.observedTo)} '
                  '(${fmtInt(fb.observedBars)} کندل، میانه ${fmtNum(fb.observedMedian, decimals: 1)} پوینت)',
        ),
      if (s.createdAt != null) _fact(context, Icons.schedule, 'ساخته‌شده: ${fmtLocal(s.createdAt)} (وقت محلی)'),
    ];

    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 10),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(
            child: Text(
              'بک‌تست #${s.id} — ${s.symbol} — ${s.mode.labelFa}',
              key: const ValueKey<String>('bt-header-title'),
              style: text.titleMedium,
            ),
          ),
          BacktestStatusChip(s.status),
          IconButton(tooltip: 'بستن نتیجه', onPressed: onClose, icon: const Icon(Icons.close)),
        ]),
        const SizedBox(height: 6),
        Wrap(spacing: 16, runSpacing: 4, children: facts),
        if (provisional != null)
          Container(
            key: const ValueKey<String>('bt-provisional'),
            margin: const EdgeInsets.only(top: 8),
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            decoration: BoxDecoration(color: colors.warningContainer, borderRadius: BorderRadius.circular(8)),
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              Icon(Icons.warning_amber_rounded, size: 18, color: colors.onWarningContainer),
              const SizedBox(width: 6),
              Flexible(
                child: Text(
                  '$provisional — داده خام هنوز تایید نشده؛ این نتیجه موقت است.',
                  style: TextStyle(color: colors.onWarningContainer, fontWeight: FontWeight.bold),
                ),
              ),
            ]),
          ),
        if (labels.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Wrap(
              key: const ValueKey<String>('bt-labels'),
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final String l in labels)
                  Chip(
                    label: Text(l, style: const TextStyle(fontSize: 12)),
                    avatar: Icon(Icons.label_important_outline, size: 16, color: colors.warning),
                    visualDensity: VisualDensity.compact,
                    materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                  ),
              ],
            ),
          ),
      ]),
    );
  }

  Widget _fact(BuildContext context, IconData icon, String text, {Key? key, String? tooltip}) {
    final Widget row = Row(key: key, mainAxisSize: MainAxisSize.min, children: [
      Icon(icon, size: 16, color: context.appColors.mutedText),
      const SizedBox(width: 4),
      Flexible(child: Text(text, style: Theme.of(context).textTheme.bodySmall)),
    ]);
    return tooltip == null || tooltip.isEmpty ? row : Tooltip(message: tooltip, child: row);
  }
}
