// Display formatting of engine backtest values. Formatting only (grouping,
// decimals, sign, fraction -> percent text); no trading number is derived.
// Latin digits on purpose: amounts and prices are compared with MT5 and the
// engine's API output.

import 'package:flutter/material.dart';

import '../../chart/chart_format.dart';
import '../../core/number_format.dart';
import '../../models/backtest_models.dart';
import '../../theme/app_semantic_colors.dart';

const String kDash = '—';

/// `+1,234.56 $` / `-12.00 $` (account currency USD).
String fmtMoney(double? v, {bool signed = true}) {
  if (v == null) return kDash;
  final String body = '${formatNumber(v.abs(), decimals: 2)} \$';
  if (!signed) return v < 0 ? '-$body' : body;
  if (v > 0) return '+$body';
  if (v < 0) return '-$body';
  return body;
}

/// A value that already is a percent: `2.3456` -> `+2.35%`.
String fmtPct(double? pct, {bool signed = true, int decimals = 2}) {
  if (pct == null) return kDash;
  final String body = '${pct.abs().toStringAsFixed(decimals)}%';
  if (pct < 0) return '-$body';
  return signed && pct > 0 ? '+$body' : body;
}

/// A fraction 0..1 shown as a percent (`win_rate` 0.4231 -> `42.3%`).
String fmtFraction(double? f, {int decimals = 1}) => f == null ? kDash : '${(f * 100).toStringAsFixed(decimals)}%';

String fmtNum(double? v, {int decimals = 2}) => v == null ? kDash : formatNumber(v, decimals: decimals);

String fmtInt(int? v) => v == null ? kDash : formatNumber(v);

/// Profit factor: `∞` when the engine flags it infinite (profit, no loss).
String fmtProfitFactor(double? pf, bool infinite) => infinite ? '∞' : (pf == null ? kDash : pf.toStringAsFixed(2));

String fmtR(double? r) => r == null ? kDash : '${r >= 0 ? '+' : ''}${r.toStringAsFixed(2)}R';

/// Local wall-clock time (`2025.01.06 04:30`).
String fmtLocal(DateTime? t) => t == null ? kDash : formatLocal(t);

/// `2025.01.06 01:00 UTC`.
String fmtUtc(DateTime? t) => t == null ? kDash : formatUtc(t);

/// UTC calendar date (`2025-01-06`), for period boundaries.
String fmtDay(DateTime? t) => t == null ? kDash : formatDate(t.toUtc());

/// `[from, to)` as UTC dates.
String fmtPeriod(DateTime? from, DateTime? to) => '${fmtDay(from)} تا ${fmtDay(to)}';

/// Profit / loss text color: success for > 0, error for < 0, null otherwise.
Color? pnlColor(BuildContext context, double? v) {
  if (v == null || v == 0) return null;
  final AppSemanticColors c = context.appColors;
  return v > 0 ? c.success : c.error;
}

/// Status chip colors.
({Color background, Color foreground}) statusColors(BuildContext context, BacktestStatus s) {
  final AppSemanticColors c = context.appColors;
  final ColorScheme cs = Theme.of(context).colorScheme;
  return switch (s) {
    BacktestStatus.done => (background: c.successContainer, foreground: c.onSuccessContainer),
    BacktestStatus.error => (background: cs.errorContainer, foreground: cs.onErrorContainer),
    BacktestStatus.queued || BacktestStatus.running => (
        background: cs.primaryContainer,
        foreground: cs.onPrimaryContainer
      ),
    _ => (background: c.warningContainer, foreground: c.onWarningContainer),
  };
}

/// Small colored status chip.
class BacktestStatusChip extends StatelessWidget {
  const BacktestStatusChip(this.status, {super.key});

  final BacktestStatus status;

  @override
  Widget build(BuildContext context) {
    final colors = statusColors(context, status);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(color: colors.background, borderRadius: BorderRadius.circular(10)),
      child: Text(status.labelFa, style: TextStyle(fontSize: 11, color: colors.foreground)),
    );
  }
}

/// A number in a Persian row: always LTR so signs and `$` stay in place.
class LtrText extends StatelessWidget {
  const LtrText(this.text, {super.key, this.style, this.textAlign});

  final String text;
  final TextStyle? style;
  final TextAlign? textAlign;

  @override
  Widget build(BuildContext context) => Text(text,
      style: style,
      textAlign: textAlign,
      textDirection: TextDirection.ltr,
      maxLines: 1,
      overflow: TextOverflow.ellipsis);
}
