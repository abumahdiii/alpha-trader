import 'package:flutter/material.dart';

import '../../theme/app_semantic_colors.dart';
import '../chart_controller.dart';
import '../chart_format.dart';
import '../../models/chart_models.dart';
import '../../models/market_data.dart';

/// «اطلاعات داده»: cache metadata of the selected series, the full gap list
/// (tap -> jump to the gap on the chart) and «به‌روزرسانی از MT5».
class DataInfoPanel extends StatefulWidget {
  const DataInfoPanel({super.key, required this.controller});

  final ChartController controller;

  @override
  State<DataInfoPanel> createState() => _DataInfoPanelState();
}

class _DataInfoPanelState extends State<DataInfoPanel> {
  /// Weekends are hundreds of expected gaps; hidden by default.
  final Set<GapKind> _kinds = <GapKind>{GapKind.holiday, GapKind.sessionBreak, GapKind.missing};

  @override
  Widget build(BuildContext context) {
    final ChartController c = widget.controller;
    final TextTheme tt = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final DataInfo? info = c.dataInfo;

    final List<Widget> header = <Widget>[
      _UpdateBlock(controller: c),
      const Divider(),
    ];
    if (c.dataInfoLoading && info == null) {
      return ListView(children: <Widget>[...header, const Center(child: CircularProgressIndicator())]);
    }
    if (info == null) {
      return ListView(children: <Widget>[
        ...header,
        Padding(
          padding: const EdgeInsets.all(12),
          child: Text(c.dataInfoError ?? 'اطلاعات داده هنوز دریافت نشده است.',
              style: TextStyle(color: c.dataInfoError != null ? colors.error : colors.mutedText)),
        ),
      ]);
    }

    final RatesMeta m = info.meta;
    final GapsResult g = info.gaps;
    final List<Gap> shown = g.gaps.where((Gap x) => _kinds.contains(x.kind)).toList(growable: false);
    return CustomScrollView(
      key: const ValueKey<String>('data-info-panel'),
      slivers: <Widget>[
        SliverList(
          delegate: SliverChildListDelegate(<Widget>[
            ...header,
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: <Widget>[
                Text('${m.symbol} ${m.timeframe}', style: tt.titleSmall),
                _kv('مدل offset ساعت سرور', m.offsetModel ?? '—'),
                _kv('digits', info.digits?.toString() ?? '—'),
                _kv('اولین کندل کش (UTC)', m.firstBarUtc == null ? '—' : formatMt5Time(m.firstBarUtc!)),
                _kv('آخرین کندل کش (UTC)', m.lastBarUtc == null ? '—' : formatMt5Time(m.lastBarUtc!)),
                _kv('تعداد کندل', '${m.rows}'),
                _kv(
                    'منبع داده',
                    switch (m.source) {
                      'mt5' => 'MT5',
                      'seed' => 'داده آزمایشی',
                      null => '—',
                      final String s => s,
                    }),
                if (m.fetchedAtUtc != null) _kv('آخرین دریافت (UTC)', formatMt5Time(m.fetchedAtUtc!)),
                if (m.historyShort == true)
                  Text('تاریخچه بروکر کوتاه‌تر از بازه درخواستی است.', style: TextStyle(color: colors.warning)),
                if (!m.cached)
                  Text('برای این نماد و تایم‌فریم کشی وجود ندارد.', style: TextStyle(color: colors.warning)),
                const Divider(),
                Text('گپ‌ها: ${g.count} (کندل‌های گم‌شده: ${g.missingBarsTotal})', style: tt.titleSmall),
                const SizedBox(height: 4),
                Wrap(spacing: 6, runSpacing: 4, children: <Widget>[
                  for (final GapKind k in GapKind.values)
                    FilterChip(
                      key: ValueKey<String>('gap-filter-${k.code}'),
                      label: Text('${k.titleFa}: ${g.gapCounts[k.code] ?? 0}'),
                      selected: _kinds.contains(k),
                      onSelected: (bool on) => setState(() => on ? _kinds.add(k) : _kinds.remove(k)),
                    ),
                ]),
                const SizedBox(height: 4),
                Text('برای رفتن به محل گپ روی چارت، روی آن بزنید.',
                    style: tt.bodySmall?.copyWith(color: colors.mutedText)),
              ]),
            ),
          ]),
        ),
        SliverList(
          delegate: SliverChildBuilderDelegate(
            (BuildContext context, int i) {
              final Gap x = shown[i];
              return ListTile(
                key: ValueKey<String>('gap-row-$i'),
                dense: true,
                visualDensity: VisualDensity.compact,
                title: Text('${x.kind.titleFa} — ${x.missingBars} کندل (${formatValue(x.durationHours, 1)} ساعت)'),
                subtitle: Text(
                  '${formatMt5Time(x.after)}  →  ${formatMt5Time(x.before)}  UTC',
                  textDirection: TextDirection.ltr,
                  textAlign: TextAlign.right,
                ),
                onTap: () => c.jumpToTime(x.after),
              );
            },
            childCount: shown.length,
          ),
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

class _UpdateBlock extends StatelessWidget {
  const _UpdateBlock({required this.controller});

  final ChartController controller;

  @override
  Widget build(BuildContext context) {
    final ChartController c = controller;
    final AppSemanticColors colors = context.appColors;
    final UpdateOutcome? out = c.updateOutcome;
    return Padding(
      padding: const EdgeInsets.all(8),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: <Widget>[
        ElevatedButton.icon(
          key: const ValueKey<String>('update-from-mt5'),
          onPressed: c.updating || c.symbol == null ? null : c.updateFromMt5,
          icon: c.updating
              ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.sync),
          label: const Text('به‌روزرسانی از MT5'),
        ),
        const SizedBox(height: 4),
        Text(
          'فقط کندل‌های بسته‌شده تازه را (فقط‌خواندنی) از MT5 به کش اضافه می‌کند.',
          style: Theme.of(context).textTheme.bodySmall?.copyWith(color: colors.mutedText),
        ),
        if (out != null) ...<Widget>[
          const SizedBox(height: 6),
          Text(
            out.messageFa,
            key: const ValueKey<String>('update-message'),
            style: TextStyle(color: out.ok ? colors.success : colors.error),
          ),
          if (out.explanationFa != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(out.explanationFa!, key: const ValueKey<String>('update-explanation')),
            ),
          for (final String e in out.errorsFa) Text('• $e', style: TextStyle(color: colors.mutedText)),
        ],
      ]),
    );
  }
}
