import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../chart/chart_data_source.dart';
import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../models/signal_models.dart';
import '../../models/strategy.dart' show shortSha;
import '../../theme/app_semantic_colors.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/scrollable_dialog.dart';
import '../../widgets/strategy_selector.dart' show PluginBadge;
import '../backtest/backtest_format.dart';
import 'signal_format.dart';
import 'signal_mini_chart.dart';

/// The clock of the countdowns (injected in tests).
typedef SignalClock = DateTime Function();

/// The expiry line of an active signal, refreshed every second while there
/// is an expiry to count down to (no timer otherwise).
class SignalCountdown extends StatefulWidget {
  const SignalCountdown({super.key, required this.signal, this.clock = DateTime.now, this.style});

  final LiveSignal signal;
  final SignalClock clock;
  final TextStyle? style;

  @override
  State<SignalCountdown> createState() => _SignalCountdownState();
}

class _SignalCountdownState extends State<SignalCountdown> {
  Timer? _timer;

  bool get _ticks => widget.signal.isActive && !widget.signal.expiryUnknown;

  @override
  void initState() {
    super.initState();
    _sync();
  }

  @override
  void didUpdateWidget(SignalCountdown oldWidget) {
    super.didUpdateWidget(oldWidget);
    _sync();
  }

  void _sync() {
    if (_ticks && _timer == null) {
      _timer = Timer.periodic(const Duration(seconds: 1), (_) {
        if (mounted) setState(() {});
      });
    } else if (!_ticks) {
      _timer?.cancel();
      _timer = null;
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final LiveSignal s = widget.signal;
    final AppSemanticColors c = context.appColors;
    final String text = formatSignalCountdown(s, widget.clock());
    final bool pending = s.expiryUnknown;
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Icon(pending ? Icons.schedule_outlined : Icons.timer_outlined, size: 16, color: pending ? c.info : c.warning),
      const SizedBox(width: 4),
      Text(
        text,
        key: ValueKey<String>('signal-countdown-${s.id}'),
        style: (widget.style ?? const TextStyle()).copyWith(color: pending ? c.info : c.warning),
      ),
    ]);
  }
}

/// One active live signal: symbol, side, setup, indicative entry (with its
/// source), SL, indicative TP (with the engine's note), R:R, volume, risk,
/// rationale, expiry countdown, warnings and the strategy that produced it.
/// Actions: show the mini chart, open the details, copy the values as text.
/// SUGGESTION ONLY: there is no order action, by design.
class SignalCard extends StatefulWidget {
  const SignalCard({
    super.key,
    required this.signal,
    required this.source,
    this.digits,
    this.clock = DateTime.now,
    this.initiallyExpanded = false,
  });

  final LiveSignal signal;
  final ChartDataSource source;
  final int? digits;
  final SignalClock clock;
  final bool initiallyExpanded;

  static const String chartLabel = 'نمودار';
  static const String hideChartLabel = 'بستن نمودار';
  static const String detailsLabel = 'جزئیات';
  static const String copyTooltip = 'کپی مقادیر (متن)';
  static const String copiedToast = 'مقادیر سیگنال کپی شد';

  @override
  State<SignalCard> createState() => _SignalCardState();
}

class _SignalCardState extends State<SignalCard> {
  late bool _expanded = widget.initiallyExpanded;

  LiveSignal get s => widget.signal;

  void _toggleChart() {
    devLog('[SignalCard #${s.id}] chart ${_expanded ? 'collapsed' : 'expanded'}');
    setState(() => _expanded = !_expanded);
  }

  Future<void> _copy() async {
    final String text = signalCopyText(s, widget.digits);
    _log('copy values of #${s.id}');
    try {
      await Clipboard.setData(ClipboardData(text: text));
      if (mounted) showSuccessToast(context, title: SignalCard.copiedToast, subtitle: s.symbol);
    } catch (e) {
      _log('copy failed: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final int? d = widget.digits;
    final Color side = directionColor(context, s.direction);
    return Card(
      key: ValueKey<String>('signal-card-${s.id}'),
      margin: const EdgeInsets.only(bottom: 12),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: side.withValues(alpha: 0.55), width: 1.2),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          // ---------------------------------------------------------- header
          Wrap(spacing: 10, runSpacing: 6, crossAxisAlignment: WrapCrossAlignment.center, children: [
            Text(s.symbol,
                style: tt.titleLarge?.copyWith(fontWeight: FontWeight.bold), textDirection: TextDirection.ltr),
            DirectionChip(s.direction),
            Text(s.setupTitleFa ?? s.setupType ?? kDash, style: tt.titleSmall),
            if (s.isPlugin) PluginBadge(tooltip: s.strategySha256),
            SignalCountdown(signal: s, clock: widget.clock, style: tt.bodyMedium),
          ]),
          const SizedBox(height: 12),
          // ---------------------------------------------------------- values
          Wrap(spacing: 12, runSpacing: 10, children: [
            _Value(
              label: 'ورود تقریبی',
              value: signalPrice(s.indicativeEntry, d),
              caption: s.entrySource.labelFa(s.direction),
              valueKey: 'signal-entry-${s.id}',
            ),
            _Value(label: 'حد ضرر', value: signalPrice(s.stopLoss, d), color: c.error),
            _Value(
              label: 'حد سود (تقریبی)',
              value: signalPrice(s.takeProfitIndicative, d),
              color: c.success,
              tooltip: s.takeProfitNoteFa,
            ),
            _Value(label: 'نسبت', value: signalRr(s.rr)),
            _Value(label: 'حجم پیشنهادی', value: signalVolume(s.volume), valueKey: 'signal-volume-${s.id}'),
            _Value(label: 'ریسک این پیشنهاد', value: fmtMoney(s.riskAmount, signed: false)),
            _Value(label: 'کندل تایید ($kLocalTimeFa)', value: fmtLocal(s.confirmationBarTime)),
          ]),
          if (s.takeProfitNoteFa != null) ...[
            const SizedBox(height: 8),
            Text(s.takeProfitNoteFa!, style: tt.bodySmall?.copyWith(color: c.mutedText)),
          ],
          if (s.reasonFa != null) ...[
            const SizedBox(height: 8),
            Text('دلیل: ${s.reasonFa}', style: tt.bodyMedium),
          ],
          ..._warnings(context),
          const SizedBox(height: 8),
          // ---------------------------------------------------------- footer
          Row(children: [
            Expanded(
              child: Text(
                'سیستم: ${strategyLineFa(s)}',
                style: tt.bodySmall?.copyWith(color: c.mutedText),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            TextButton.icon(
              key: ValueKey<String>('signal-chart-toggle-${s.id}'),
              onPressed: _toggleChart,
              icon: Icon(_expanded ? Icons.expand_less : Icons.show_chart),
              label: Text(_expanded ? SignalCard.hideChartLabel : SignalCard.chartLabel),
            ),
            TextButton.icon(
              key: ValueKey<String>('signal-details-${s.id}'),
              onPressed: () => unawaited(showSignalDetails(context, s, source: widget.source, digits: d)),
              icon: const Icon(Icons.info_outline),
              label: const Text(SignalCard.detailsLabel),
            ),
            IconButton(
              key: ValueKey<String>('signal-copy-${s.id}'),
              tooltip: SignalCard.copyTooltip,
              onPressed: () => unawaited(_copy()),
              icon: const Icon(Icons.copy_outlined),
            ),
          ]),
          if (_expanded) ...[
            const SizedBox(height: 8),
            SignalMiniChart(source: widget.source, signal: s, digits: d),
          ],
        ]),
      ),
    );
  }

  List<Widget> _warnings(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    final String? gapNote = s.entryGap?.noteFa;
    final bool provisionalGap = s.gapClassProvisional || (s.entryGap?.provisional ?? false);
    final List<Widget> chips = [
      if (s.backtestWouldSkip)
        NoteChip(
          key: ValueKey<String>('signal-skip-${s.id}'),
          icon: Icons.warning_amber_rounded,
          color: c.warning,
          text: 'بک‌تست این ستاپ را رد می‌کرد: ${s.backtestSkipReasonFa ?? kDash}',
        ),
      if (provisionalGap)
        NoteChip(
          key: ValueKey<String>('signal-gap-${s.id}'),
          icon: Icons.hourglass_bottom,
          color: c.info,
          text: 'نوع گپ کندل ورود موقت است${gapNote != null ? ': $gapNote' : ' و پس از بازگشایی بازار قطعی می‌شود.'}',
        ),
      for (final String w in s.sizing?.warnings ?? const <String>[])
        NoteChip(icon: Icons.info_outline, color: c.warning, text: w),
    ];
    if (chips.isEmpty) return const [];
    return [const SizedBox(height: 8), Wrap(spacing: 8, runSpacing: 6, children: chips)];
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[SignalCard] $message');
    AppLogger.log('[SignalCard] $message');
  }
}

/// One labelled value of a card (prices LTR, Latin digits like MT5).
class _Value extends StatelessWidget {
  const _Value({required this.label, required this.value, this.caption, this.color, this.tooltip, this.valueKey});

  final String label;
  final String value;
  final String? caption;
  final Color? color;
  final String? tooltip;
  final String? valueKey;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final Widget box = Container(
      constraints: const BoxConstraints(minWidth: 130),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(color: c.surfaceMuted.withValues(alpha: 0.5), borderRadius: BorderRadius.circular(8)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
        Text(label, style: tt.labelSmall?.copyWith(color: c.mutedText)),
        Text(
          value,
          key: valueKey == null ? null : ValueKey<String>(valueKey!),
          textDirection: TextDirection.ltr,
          style: tt.titleMedium?.copyWith(color: color, fontWeight: FontWeight.w600),
        ),
        if (caption != null) Text(caption!, style: tt.labelSmall?.copyWith(color: c.info)),
      ]),
    );
    return tooltip == null ? box : Tooltip(message: tooltip!, child: box);
  }
}

/// Every field of [s] (local and UTC times, sizing, account, indicators,
/// mismatch / supersede reasons) with the mini chart on top. Read-only.
Future<void> showSignalDetails(BuildContext context, LiveSignal s, {required ChartDataSource source, int? digits}) {
  devLog('[SignalDetails] open #${s.id} ${s.symbol} ${s.status.code}');
  String price(double? p) => signalPrice(p, digits);
  String both(DateTime? t) => t == null ? kDash : '${fmtLocal(t)}  (${fmtUtc(t)})';
  final SignalSizing? z = s.sizing;
  final SignalAccount? a = s.account;
  final List<(String, String)> rows = [
    ('وضعیت', s.status.labelFa),
    if (s.statusReasonFa != null) ('دلیل وضعیت', s.statusReasonFa!),
    if (s.mismatchFields.isNotEmpty) ('فیلدهای ناهمخوان', s.mismatchFields.join('، ')),
    if (s.supersededFields.isNotEmpty) ('فیلدهای تغییرکرده', s.supersededFields.join('، ')),
    ('جهت', s.direction?.titleFa ?? kDash),
    ('ستاپ / خط / الگو', '${s.setupTitleFa ?? s.setupType ?? kDash} / ${s.line ?? kDash} / ${s.pattern ?? kDash}'),
    ('کندل تایید', both(s.confirmationBarTime)),
    ('زمان تصمیم', both(s.decisionTime)),
    ('کندل ورود', both(s.entryBarTime)),
    ('انقضا', s.expiryUnknown && s.isActive ? kExpiryPendingFa : both(s.expiresTime)),
    ('قیمت مرجع', price(s.referencePrice)),
    ('ورود تقریبی', '${price(s.indicativeEntry)} (${s.entrySource.labelFa(s.direction)})'),
    ('حد ضرر', price(s.stopLoss)),
    ('حد سود (تقریبی)', price(s.takeProfitIndicative)),
    ('نسبت', signalRr(s.rr)),
    ('حجم پیشنهادی', signalVolume(s.volume)),
    ('ریسک در ورود تقریبی', fmtMoney(s.riskAmount, signed: false)),
    if (z != null) ('حجم خام / مارجین', '${formatVolumeRaw(z.rawVolume)} / ${fmtMoney(z.margin, signed: false)}'),
    if (z?.reasonFa != null) ('حجم', z!.reasonFa!),
    if (a != null)
      (
        'حساب (از تنظیمات)',
        'موجودی ${fmtMoney(a.balance, signed: false)}، ریسک ${fmtPct(a.riskPct, signed: false)}، '
            'اهرم 1:${a.leverage?.round() ?? kDash}، R:R ${a.rr?.toStringAsFixed(2) ?? kDash}'
      ),
    if (s.reasonFa != null) ('دلیل', s.reasonFa!),
    if (s.backtestWouldSkip) ('بک‌تست', 'رد می‌کرد: ${s.backtestSkipReasonFa ?? kDash}'),
    if (s.entryGap != null)
      (
        'گپ کندل ورود',
        '${s.entryGap!.kind ?? kDash}${s.entryGap!.provisional ? ' (موقت)' : ''} ${s.entryGap!.noteFa ?? ''}'
      ),
    ('سیستم', strategyLineFa(s)),
    if (s.paramsHash != null) ('هش پارامترها', shortSha(s.paramsHash!)),
    for (final MapEntry<String, Object?> e in s.indicators.entries) ('شاخص ${e.key}', '${e.value}'),
    ('ثبت / به‌روزرسانی', '${fmtLocal(s.createdTime)} / ${fmtLocal(s.updatedTime)}'),
  ];
  return ScrollableDialog.show<void>(
    context: context,
    width: 900,
    maxWidth: 900,
    showCloseButton: true,
    title: Row(children: [
      Text('سیگنال #${s.id} — ${s.symbol}'),
      const SizedBox(width: 8),
      SignalStatusChip(s.status),
    ]),
    content: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      SignalMiniChart(source: source, signal: s, digits: digits, height: 300),
      const SizedBox(height: 12),
      for (final (String k, String v) in rows)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 3),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            SizedBox(width: 190, child: Text(k, style: TextStyle(color: context.appColors.mutedText))),
            Expanded(child: SelectableText(v)),
          ]),
        ),
      const SizedBox(height: 8),
      Text(s.noteFa ?? kSuggestionOnlyFa, style: TextStyle(color: context.appColors.info)),
    ]),
  );
}

/// Raw (unrounded-to-step) volume of the sizing block.
String formatVolumeRaw(double? v) => v == null ? kDash : v.toStringAsFixed(4);
