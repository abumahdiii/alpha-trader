// Display helpers of the live signal screens. Formatting only: every price,
// volume and amount is the engine's; nothing is derived here.

import 'package:flutter/material.dart';

import '../../chart/chart_format.dart';
import '../../models/chart_models.dart';
import '../../models/signal_models.dart';
import '../../models/strategy.dart';
import '../../theme/app_semantic_colors.dart';
import '../backtest/backtest_format.dart';

/// The permanent suggestion-only note of the signal screens.
const String kSuggestionOnlyFa = 'این برنامه فقط پیشنهاد می‌دهد و هیچ سفارشی ارسال نمی‌کند.';

/// Label of a time shown in the user's local zone.
const String kLocalTimeFa = 'به وقت محلی';

/// An engine price with the symbol's digits (unknown digits: unrounded).
String signalPrice(double? price, int? digits) => formatChartPrice(price, digits);

/// `0.13 لات`.
String signalVolume(double? v) => v == null ? kDash : '${formatValue(v, 2)} لات';

/// `R:R 2.00`.
String signalRr(double? rr) => rr == null ? kDash : 'R:R ${rr.toStringAsFixed(2)}';

/// Buy / sell colour (success / error).
Color directionColor(BuildContext context, TradeSide? side) {
  final AppSemanticColors c = context.appColors;
  return switch (side) {
    TradeSide.buy => c.success,
    TradeSide.sell => c.error,
    null => c.mutedText,
  };
}

/// «خرید» / «فروش» chip in the side's colour.
class DirectionChip extends StatelessWidget {
  const DirectionChip(this.side, {super.key});

  final TradeSide? side;

  @override
  Widget build(BuildContext context) {
    final Color color = directionColor(context, side);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withValues(alpha: 0.6)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(side == TradeSide.sell ? Icons.south_east : Icons.north_east, size: 14, color: color),
        const SizedBox(width: 4),
        Text(side?.titleFa ?? kDash, style: TextStyle(color: color, fontWeight: FontWeight.bold)),
      ]),
    );
  }
}

/// Colours of a signal status chip.
({Color background, Color foreground}) signalStatusColors(BuildContext context, LiveSignalStatus s) {
  final AppSemanticColors c = context.appColors;
  final ColorScheme cs = Theme.of(context).colorScheme;
  return switch (s) {
    LiveSignalStatus.active => (background: c.successContainer, foreground: c.onSuccessContainer),
    LiveSignalStatus.rejected => (background: cs.errorContainer, foreground: cs.onErrorContainer),
    LiveSignalStatus.superseded => (background: c.warningContainer, foreground: c.onWarningContainer),
    _ => (background: c.surfaceMuted, foreground: c.mutedText),
  };
}

class SignalStatusChip extends StatelessWidget {
  const SignalStatusChip(this.status, {super.key});

  final LiveSignalStatus status;

  @override
  Widget build(BuildContext context) {
    final colors = signalStatusColors(context, status);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(color: colors.background, borderRadius: BorderRadius.circular(10)),
      child: Text(status.labelFa, style: TextStyle(fontSize: 11, color: colors.foreground)),
    );
  }
}

/// A small tinted chip with an icon (warnings, notes).
class NoteChip extends StatelessWidget {
  const NoteChip({super.key, required this.icon, required this.text, required this.color, this.tooltip});

  final IconData icon;
  final String text;
  final Color color;
  final String? tooltip;

  @override
  Widget build(BuildContext context) {
    final Widget chip = Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: color.withValues(alpha: 0.5)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(icon, size: 16, color: color),
        const SizedBox(width: 6),
        Flexible(child: Text(text, style: TextStyle(fontSize: 12, color: color))),
      ]),
    );
    return tooltip == null ? chip : Tooltip(message: tooltip!, child: chip);
  }
}

/// `کانال انحراف معیار v1 — پارامتر v3` (+ plugin sha, short).
String strategyLineFa(LiveSignal s) {
  final String name = s.strategy ?? kDash;
  final String version = s.strategyVersion == null ? '' : ' v${s.strategyVersion}';
  final String params = s.paramsVersion == null ? '' : ' — پارامتر v${s.paramsVersion}';
  final String sha = s.isPlugin && s.strategySha256 != null ? ' — sha ${shortSha(s.strategySha256!)}' : '';
  return '$name$version$params$sha';
}

/// The values a user may copy to the terminal by hand (display text only).
String signalCopyText(LiveSignal s, int? digits) => [
      '${s.symbol} ${s.direction?.titleFa ?? kDash} (${s.direction?.name ?? '-'})',
      'Entry ≈ ${signalPrice(s.indicativeEntry, digits)}',
      'SL ${signalPrice(s.stopLoss, digits)}',
      'TP ≈ ${signalPrice(s.takeProfitIndicative, digits)}',
      'Volume ${formatValue(s.volume, 2)}',
    ].join('\n');
