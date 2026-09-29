import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/dev_mode.dart';
import '../../models/signal_models.dart';
import '../../providers/shell_navigation.dart';
import '../../providers/signals_provider.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/engine_status_indicator.dart';
import '../backtest/backtest_format.dart';
import 'signal_format.dart';

/// The live scheduler at a glance (engine `/signals/status`, pushed over
/// the socket): state, MT5, live strategy, last / next check in local time,
/// the last check of every symbol with its Persian reason, the clock skew
/// warning, the last error, and — when live signals are off — a notice with
/// a button to the settings.
class SignalsStatusStrip extends StatelessWidget {
  const SignalsStatusStrip({super.key});

  static const String disabledTitle = 'سیگنال لایو خاموش است';
  static const String disabledBody =
      'موتور تا زمانی که سیگنال لایو را در تنظیمات روشن نکنید، کندل‌ها را بررسی نمی‌کند و پیشنهادی نمی‌سازد.';
  static const String toSettingsLabel = 'رفتن به تنظیمات';
  static const String waitingFa = 'در انتظار وضعیت از موتور…';

  @override
  Widget build(BuildContext context) {
    final SignalsProvider p = context.watch<SignalsProvider>();
    final LiveSignalsStatus? st = p.status;
    final AppSemanticColors c = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (p.connection != SignalsConnection.connected)
        _Banner(
          key: const ValueKey<String>('signals-connection-banner'),
          icon: Icons.sync_problem,
          color: c.warning,
          text: p.connectionErrorFa ?? 'در حال اتصال به جریان زنده سیگنال‌ها…',
        ),
      if (st == null)
        Padding(padding: const EdgeInsets.all(8), child: Text(waitingFa, style: tt.bodyMedium))
      else ...[
        if (!st.enabled)
          _Banner(
            key: const ValueKey<String>('signals-disabled-notice'),
            icon: Icons.notifications_off_outlined,
            color: c.info,
            title: disabledTitle,
            text: disabledBody,
            action: FilledButton.tonalIcon(
              key: const ValueKey<String>('signals-to-settings'),
              onPressed: () {
                devLog('[SignalsStatus] disabled notice -> settings');
                context.read<ShellNavigation>().select(ShellPage.settings);
              },
              icon: const Icon(Icons.settings_outlined),
              label: const Text(toSettingsLabel),
            ),
          ),
        if (st.clockSkewWarning)
          _Banner(
            key: const ValueKey<String>('signals-skew-warning'),
            icon: Icons.schedule,
            color: c.warning,
            title: 'اختلاف ساعت کامپیوتر با سرور بروکر',
            text: 'ساعت این کامپیوتر حدود ${st.clockSkewS?.abs().toStringAsFixed(0) ?? '?'} ثانیه با زمان تیک‌های '
                'سرور اختلاف دارد. موتور زمان سرور را از تیک‌ها تخمین می‌زند؛ ساعت ویندوز را همگام کنید.',
          ),
        if (st.lastErrorFa != null)
          _Banner(
            key: const ValueKey<String>('signals-last-error'),
            icon: Icons.error_outline,
            color: c.error,
            title: 'آخرین خطای موتور سیگنال (${fmtLocal(st.lastErrorUtc)})',
            text: st.lastErrorFa!,
          ),
        _Summary(status: st, connection: p.connection),
        if (st.symbols.isNotEmpty) _Checks(status: st),
      ],
    ]);
  }
}

class _Summary extends StatelessWidget {
  const _Summary({required this.status, required this.connection});

  final LiveSignalsStatus status;
  final SignalsConnection connection;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    final LiveSignalsStatus st = status;
    final Color stateColor = switch (st.state) {
      LiveSignalsState.running => c.success,
      LiveSignalsState.mt5Down => c.error,
      LiveSignalsState.starting => c.info,
      _ => c.mutedText,
    };
    final (String mt5Label, Color mt5Color) = EngineStatusIndicator.mt5Appearance(st.mt5State, c);
    return Wrap(spacing: 8, runSpacing: 8, children: [
      _Chip(label: 'وضعیت', value: st.state.labelFa, color: stateColor, valueKey: 'signals-state'),
      _Chip(label: 'متاتریدر', value: mt5Label, color: mt5Color),
      _Chip(label: 'سیستم لایو', value: st.liveStrategy ?? kDash),
      _Chip(label: 'آخرین بررسی ($kLocalTimeFa)', value: fmtLocal(st.lastCheckUtc)),
      _Chip(label: 'بررسی بعدی ($kLocalTimeFa)', value: fmtLocal(st.nextCheckUtc)),
      if (st.graceS != null) _Chip(label: 'مهلت پس از بسته شدن کندل', value: '${st.graceS!.round()} ثانیه'),
      _Chip(
        label: 'اتصال زنده',
        value: connection == SignalsConnection.connected ? 'برقرار' : 'در حال اتصال',
        color: connection == SignalsConnection.connected ? c.success : c.warning,
      ),
    ]);
  }
}

/// The last check of each symbol: result, Persian reason, boundary and last tick.
class _Checks extends StatelessWidget {
  const _Checks({required this.status});

  final LiveSignalsStatus status;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.only(top: 10),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        for (final String sym in status.symbols)
          Padding(
            key: ValueKey<String>('signals-check-$sym'),
            padding: const EdgeInsets.symmetric(vertical: 3),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              SizedBox(
                width: 110,
                child: Text(sym,
                    textDirection: TextDirection.ltr, style: tt.bodyMedium?.copyWith(fontWeight: FontWeight.w600)),
              ),
              SizedBox(
                  width: 170, child: Text(status.checks[sym]?.resultFa ?? 'هنوز بررسی نشده', style: tt.bodyMedium)),
              Expanded(
                child: Text(
                  [
                    if (status.checks[sym]?.reasonFa != null) status.checks[sym]!.reasonFa!,
                    if (status.checks[sym]?.boundaryUtc != null) 'کندل ${fmtLocal(status.checks[sym]!.boundaryUtc)}',
                    'آخرین تیک ${fmtLocal(status.lastTickUtc[sym])}',
                  ].join(' — '),
                  style: tt.bodySmall?.copyWith(color: c.mutedText),
                ),
              ),
            ]),
          ),
      ]),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({required this.label, required this.value, this.color, this.valueKey});

  final String label;
  final String value;
  final Color? color;
  final String? valueKey;

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: c.surfaceMuted.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: (color ?? c.borderColor).withValues(alpha: 0.5)),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Text('$label: ', style: TextStyle(fontSize: 12, color: c.mutedText)),
        Text(
          value,
          key: valueKey == null ? null : ValueKey<String>(valueKey!),
          style: TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: color),
        ),
      ]),
    );
  }
}

class _Banner extends StatelessWidget {
  const _Banner({super.key, required this.icon, required this.color, required this.text, this.title, this.action});

  final IconData icon;
  final Color color;
  final String? title;
  final String text;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withValues(alpha: 0.5)),
      ),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(icon, color: color),
        const SizedBox(width: 10),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            if (title != null) Text(title!, style: TextStyle(fontWeight: FontWeight.bold, color: color)),
            Text(text),
          ]),
        ),
        if (action != null) ...[const SizedBox(width: 12), action!],
      ]),
    );
  }
}
