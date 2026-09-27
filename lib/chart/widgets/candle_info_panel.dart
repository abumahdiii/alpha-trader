import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../../theme/app_semantic_colors.dart';
import '../chart_data.dart';
import '../chart_format.dart';
import '../../models/chart_models.dart';

/// Values of the hovered bar (or the newest bar), laid out for a
/// side-by-side check against MT5's Data Window: server time AND UTC, OHLC
/// with the symbol's digits, tick volume, spread, and the engine's channel
/// lines / slope / direction / ATR at that bar.
class CandleInfoPanel extends StatelessWidget {
  const CandleInfoPanel({super.key, required this.data, required this.hoverIndex});

  final ChartData data;
  final ValueListenable<int?> hoverIndex;

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<int?>(
      valueListenable: hoverIndex,
      builder: (BuildContext context, int? hovered, _) {
        if (data.isEmpty) return const SizedBox.shrink();
        final int i = (hovered ?? data.length - 1).clamp(0, data.length - 1);
        return _Panel(data: data, index: i, hovering: hovered != null);
      },
    );
  }
}

class _Panel extends StatelessWidget {
  const _Panel({required this.data, required this.index, required this.hovering});

  final ChartData data;
  final int index;
  final bool hovering;

  @override
  Widget build(BuildContext context) {
    final Candle c = data.candles[index];
    final ChannelPoint? p = data.channel[index];
    final int? d = data.digits;
    final TextTheme tt = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final List<Widget> rows = <Widget>[
      Text(hovering ? 'کندل زیر نشانگر' : 'آخرین کندل بازه', style: tt.labelLarge),
      const SizedBox(height: 4),
      _row('زمان سرور', c.serverTime ?? 'نامشخص (مدل offset در کش نیست)'),
      _row('زمان UTC', formatMt5Time(c.time)),
      _row('زمان محلی', formatLocal(c.time)),
      const Divider(height: 10),
      _row('Open', formatChartPrice(c.open, d)),
      _row('High', formatChartPrice(c.high, d)),
      _row('Low', formatChartPrice(c.low, d)),
      _row('Close', formatChartPrice(c.close, d)),
      _row('حجم تیک', '${c.tickVolume}'),
      _row('اسپرد (پوینت)', '${c.spread}'),
      const Divider(height: 10),
    ];
    if (p == null || !p.drawable) {
      rows.add(Text(
        p == null ? 'برای این کندل مقدار کانال نیامده است.' : 'کانال در این کندل معتبر نیست (دوره گرم‌شدن).',
        style: tt.bodySmall?.copyWith(color: colors.mutedText),
      ));
    } else {
      final String dir = p.direction?.titleFa ?? '—';
      rows.addAll(<Widget>[
        _row('خط بالا', formatChartPrice(p.upper, d)),
        _row('خط میانی', formatChartPrice(p.mid, d)),
        _row('خط پایین', formatChartPrice(p.lower, d)),
        _row('شیب (هر کندل H4)', formatValue(p.slope, (d ?? 2) + 3)),
        _row('جهت کانال', p.isFlat == true ? '$dir (افقی)' : dir, ltr: false),
        if (data.timeframe == ChartTimeframe.h1) _row('ATR H1', formatChartPrice(p.atrH1, d)),
        _row('ATR H4', formatChartPrice(p.atrH4, d)),
        if (p.h4Open != null) _row('کانال کندل H4', formatMt5Time(p.h4Open!)),
        if (p.barsAhead != null) _row('امتداد (کندل H4)', formatValue(p.barsAhead, 2)),
      ]);
    }
    return Card(
      key: const ValueKey<String>('candle-info-panel'),
      elevation: 2,
      child: Padding(
        padding: const EdgeInsets.all(8),
        child: SizedBox(
          width: 250,
          child: DefaultTextStyle.merge(
            style: const TextStyle(fontSize: 12),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: rows,
            ),
          ),
        ),
      ),
    );
  }

  /// Persian label on the start side, value on the end side. Numbers and
  /// times are LTR so they read exactly like the terminal.
  Widget _row(String label, String value, {bool ltr = true}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 1),
        child: Row(
          children: <Widget>[
            Expanded(child: Text(label)),
            Text(value, textDirection: ltr ? TextDirection.ltr : null),
          ],
        ),
      );
}
