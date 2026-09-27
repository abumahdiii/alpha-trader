import 'package:flutter/material.dart';

import '../../models/backtest_models.dart';
import '../../theme/app_semantic_colors.dart';
import 'backtest_format.dart';
import 'distribution_chart.dart';
import 'equity_chart.dart';

/// «در این بازه هیچ معامله‌ای انجام نشد».
const String kZeroTradesFa = 'در این بازه هیچ معامله‌ای انجام نشد';

/// One metric: title, main value, optional second line, optional color.
class MetricCard extends StatelessWidget {
  const MetricCard({super.key, required this.title, required this.value, this.sub, this.color, this.tooltip});

  final String title;
  final String value;
  final String? sub;
  final Color? color;
  final String? tooltip;

  @override
  Widget build(BuildContext context) {
    final TextTheme text = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final Widget card = Container(
      width: 170,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: colors.surfaceMuted.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: colors.borderColor.withValues(alpha: 0.3)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
        Text(title, style: text.labelMedium?.copyWith(color: colors.mutedText), maxLines: 2),
        const SizedBox(height: 4),
        LtrText(value, style: text.titleMedium?.copyWith(color: color, fontWeight: FontWeight.bold)),
        if (sub != null) LtrText(sub!, style: text.bodySmall?.copyWith(color: color ?? colors.mutedText)),
      ]),
    );
    return tooltip == null ? card : Tooltip(message: tooltip!, child: card);
  }
}

class _CardsSection extends StatelessWidget {
  const _CardsSection({required this.title, required this.cards, this.subtitle});

  final String title;
  final String? subtitle;
  final List<Widget> cards;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(title, style: Theme.of(context).textTheme.titleSmall),
          if (subtitle != null)
            Text(subtitle!, style: Theme.of(context).textTheme.bodySmall?.copyWith(color: context.appColors.mutedText)),
          const SizedBox(height: 8),
          Wrap(spacing: 10, runSpacing: 10, children: cards),
        ]),
      );
}

/// Banner for a run / window without trades.
class ZeroTradesNotice extends StatelessWidget {
  const ZeroTradesNotice({super.key});

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    return Container(
      key: const ValueKey<String>('bt-zero-trades'),
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: c.warningContainer, borderRadius: BorderRadius.circular(12)),
      child: Row(children: [
        Icon(Icons.info_outline, color: c.onWarningContainer),
        const SizedBox(width: 8),
        Expanded(child: Text(kZeroTradesFa, style: TextStyle(color: c.onWarningContainer))),
      ]),
    );
  }
}

/// Cards of the trade-level metrics shared by a single window and the pooled block.
List<Widget> _tradeCards(BuildContext context, BacktestMetrics m, String prefix) => [
      MetricCard(
        key: ValueKey<String>('$prefix-trades'),
        title: 'تعداد معاملات',
        value: fmtInt(m.tradeCount),
        sub: 'برد ${fmtInt(m.winCount)} / باخت ${fmtInt(m.lossCount)} / سر‌به‌سر ${fmtInt(m.breakevenCount)}',
      ),
      MetricCard(key: ValueKey<String>('$prefix-win-rate'), title: 'نرخ برد', value: fmtFraction(m.winRate)),
      MetricCard(
        key: ValueKey<String>('$prefix-pf'),
        title: 'فاکتور سود (PF)',
        value: fmtProfitFactor(m.profitFactor, m.profitFactorInfinite),
        tooltip: 'سود ناخالص معاملات برنده تقسیم بر زیان معاملات بازنده (از موتور)',
      ),
      MetricCard(
        key: ValueKey<String>('$prefix-avg-r'),
        title: 'میانگین R',
        value: fmtR(m.avgR),
        sub: m.rCount == null ? null : 'از ${fmtInt(m.rCount)} معامله',
      ),
      MetricCard(
        key: ValueKey<String>('$prefix-expectancy'),
        title: 'امید ریاضی هر معامله',
        value: fmtMoney(m.expectancy),
        color: pnlColor(context, m.expectancy),
      ),
      MetricCard(
        title: 'سود ناخالص / زیان ناخالص',
        value: fmtMoney(m.grossProfit, signed: false),
        sub: fmtMoney(m.grossLoss, signed: false),
      ),
      MetricCard(
        title: 'میانگین برد / باخت',
        value: fmtMoney(m.avgWin),
        sub: fmtMoney(m.avgLoss),
        color: pnlColor(context, m.avgWin),
      ),
      MetricCard(title: 'بزرگ‌ترین برد / باخت', value: fmtMoney(m.largestWin), sub: fmtMoney(m.largestLoss)),
    ];

/// Metrics of a manual run (`metrics_kind: single_window`), exactly as the engine computed them.
class SingleWindowMetrics extends StatelessWidget {
  const SingleWindowMetrics({super.key, required this.metrics, this.stoppedReason});

  final BacktestMetrics metrics;
  final String? stoppedReason;

  @override
  Widget build(BuildContext context) {
    final BacktestMetrics m = metrics;
    return ListView(padding: const EdgeInsets.all(16), children: [
      if ((m.tradeCount ?? 0) == 0) const ZeroTradesNotice(),
      if (stoppedReason == 'balance_depleted')
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child:
              Text('موجودی تمام شد و شبیه‌سازی این بازه متوقف شد.', style: TextStyle(color: context.appColors.error)),
        ),
      _CardsSection(title: 'نتیجه', cards: [
        MetricCard(
          key: const ValueKey<String>('bt-metric-net-profit'),
          title: 'سود خالص',
          value: fmtMoney(m.netProfit),
          sub: fmtPct(m.netProfitPct),
          color: pnlColor(context, m.netProfit),
        ),
        MetricCard(
          title: 'موجودی اولیه / نهایی',
          value: fmtMoney(m.initialBalance, signed: false),
          sub: fmtMoney(m.finalBalance, signed: false),
        ),
        MetricCard(
          key: const ValueKey<String>('bt-metric-max-dd'),
          title: 'حداکثر افت سرمایه',
          value: fmtMoney(m.maxDrawdownAbs, signed: false),
          sub: fmtPct(m.maxDrawdownPct, signed: false),
          tooltip: m.maxDrawdownPeakTime == null
              ? null
              : 'اوج: ${fmtLocal(m.maxDrawdownPeakTime)}\nکف: ${fmtLocal(m.maxDrawdownTroughTime)} (وقت محلی)',
        ),
        MetricCard(
          key: const ValueKey<String>('bt-metric-sharpe'),
          title: 'نسبت شارپ (روزانه، سالانه‌شده)',
          value: fmtNum(m.sharpe),
          sub: m.sharpeReturnCount == null ? null : '${fmtInt(m.sharpeReturnCount)} روز',
        ),
        MetricCard(
          title: 'بیشترین برد / باخت پیاپی',
          value: fmtInt(m.maxConsecutiveWins),
          sub: fmtInt(m.maxConsecutiveLosses),
        ),
      ]),
      _CardsSection(title: 'معاملات', cards: _tradeCards(context, m, 'bt-metric')),
    ]);
  }
}

/// Metrics of a random run: pooled trades, window means, worst window, and
/// the distribution of window results (summary + histogram).
class RandomRunMetrics extends StatelessWidget {
  const RandomRunMetrics({super.key, required this.detail});

  final BacktestRunDetail detail;

  @override
  Widget build(BuildContext context) {
    final BacktestRandomMetrics? m = detail.randomMetrics;
    final BacktestDistribution? d = detail.distribution;
    final BacktestMetrics? pooled = m?.pooled;
    final BacktestMeanMetrics? mean = m?.mean;
    final BacktestWorstMetrics? worst = m?.worst;
    final BacktestChartStyle style = BacktestChartStyle.of(context);
    return ListView(padding: const EdgeInsets.all(16), children: [
      if ((m?.totalTrades ?? detail.summary.tradeCount ?? 0) == 0) const ZeroTradesNotice(),
      if (m?.noteFa != null)
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: Text(m!.noteFa!, style: TextStyle(color: context.appColors.mutedText)),
        ),
      if (d != null)
        _CardsSection(
          title: 'توزیع نتیجه پنجره‌ها',
          subtitle: '${fmtInt(d.count)} پنجره، ${fmtInt(d.windowsWithoutTrades)} پنجره بدون معامله',
          cards: [
            MetricCard(
              key: const ValueKey<String>('bt-dist-mean'),
              title: 'میانگین سود خالص پنجره',
              value: fmtMoney(d.meanNetProfit),
              sub: fmtPct(d.meanNetProfitPct),
              color: pnlColor(context, d.meanNetProfit),
            ),
            MetricCard(
              key: const ValueKey<String>('bt-dist-median'),
              title: 'میانه سود خالص پنجره',
              value: fmtMoney(d.medianNetProfit),
              sub: fmtPct(d.medianNetProfitPct),
              color: pnlColor(context, d.medianNetProfit),
            ),
            MetricCard(
              key: const ValueKey<String>('bt-dist-profitable'),
              title: 'پنجره‌های سودده',
              value: fmtPct(d.pctProfitable, signed: false, decimals: 1),
            ),
            if (d.worstWindow != null)
              MetricCard(
                key: const ValueKey<String>('bt-dist-worst'),
                title: 'بدترین پنجره (#${d.worstWindow!.index + 1})',
                value: fmtMoney(d.worstWindow!.netProfit),
                sub: fmtPct(d.worstWindow!.netProfitPct),
                color: pnlColor(context, d.worstWindow!.netProfit),
              ),
            if (d.bestWindow != null)
              MetricCard(
                key: const ValueKey<String>('bt-dist-best'),
                title: 'بهترین پنجره (#${d.bestWindow!.index + 1})',
                value: fmtMoney(d.bestWindow!.netProfit),
                sub: fmtPct(d.bestWindow!.netProfitPct),
                color: pnlColor(context, d.bestWindow!.netProfit),
              ),
          ],
        ),
      if (detail.windows.isNotEmpty) ...[
        SizedBox(
          height: 220,
          child: DistributionChart(
            key: const ValueKey<String>('bt-distribution'),
            values: [
              for (final BacktestWindowResult w in detail.windows)
                if (w.netProfit != null) w.netProfit!
            ],
            mean: d?.meanNetProfit,
            median: d?.medianNetProfit,
          ),
        ),
        Padding(
          padding: const EdgeInsets.only(top: 6, bottom: 16),
          child: Wrap(spacing: 16, runSpacing: 6, children: [
            ChartLegendItem(color: style.profit, label: 'پنجره‌های سودده'),
            ChartLegendItem(color: style.loss, label: 'پنجره‌های زیان‌ده'),
            ChartLegendItem(color: style.mean, label: 'میانگین', dashed: true),
            ChartLegendItem(color: style.median, label: 'میانه', dashed: true),
            ChartLegendItem(color: style.zero, label: 'صفر'),
          ]),
        ),
      ],
      if (mean != null)
        _CardsSection(
          title: 'میانگین پنجره‌ها',
          cards: [
            MetricCard(
              key: const ValueKey<String>('bt-mean-net-profit'),
              title: 'سود خالص',
              value: fmtMoney(mean.netProfit),
              sub: fmtPct(mean.netProfitPct),
              color: pnlColor(context, mean.netProfit),
            ),
            MetricCard(
              title: 'حداکثر افت سرمایه',
              value: fmtMoney(mean.maxDrawdownAbs, signed: false),
              sub: fmtPct(mean.maxDrawdownPct, signed: false),
            ),
            MetricCard(title: 'تعداد معاملات', value: fmtNum(mean.tradeCount, decimals: 1)),
            MetricCard(
              title: 'نرخ برد',
              value: fmtFraction(mean.winRate),
              sub: mean.winRateWindows == null ? null : 'از ${fmtInt(mean.winRateWindows)} پنجره',
            ),
            MetricCard(
              title: 'نسبت شارپ',
              value: fmtNum(mean.sharpe),
              sub: mean.sharpeWindows == null ? null : 'از ${fmtInt(mean.sharpeWindows)} پنجره',
            ),
          ],
        ),
      if (worst != null)
        _CardsSection(title: 'بدترین پنجره', cards: [
          MetricCard(
            key: const ValueKey<String>('bt-worst-dd'),
            title: 'بیشترین افت سرمایه',
            value: fmtMoney(worst.maxDrawdownAbs, signed: false),
            sub: fmtPct(worst.maxDrawdownPct, signed: false),
          ),
          MetricCard(
            title: 'کمترین سود خالص',
            value: fmtPct(worst.netProfitPct),
            color: pnlColor(context, worst.netProfitPct),
          ),
        ]),
      if (pooled != null)
        _CardsSection(
          title: 'تجمیعی (همه معاملات همه پنجره‌ها)',
          cards: _tradeCards(context, pooled, 'bt-pooled'),
        ),
    ]);
  }
}
