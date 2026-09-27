import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/engine_health.dart';
import '../providers/engine_status_provider.dart';
import '../theme/app_semantic_colors.dart';
import '../theme/theme_provider.dart';

/// Placeholder home screen for the setup step. Replaced by the real shell
/// (strategies / backtests / live signals) in the first UI phase.
class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final themeProvider = context.watch<ThemeProvider>();
    final textTheme = Theme.of(context).textTheme;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Alpha Trader'),
        actions: [
          const _EngineStatusIndicator(),
          IconButton(
            tooltip: themeProvider.isDark() ? 'تم روشن' : 'تم تاریک',
            icon: Icon(themeProvider.isDark() ? Icons.light_mode : Icons.dark_mode),
            onPressed: () => themeProvider.toggleTheme(
              themeProvider.isDark() ? ThemeMode.light : ThemeMode.dark,
            ),
          ),
        ],
      ),
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.candlestick_chart, size: 72),
            const SizedBox(height: 16),
            Text('به Alpha Trader خوش آمدید', style: textTheme.headlineSmall),
            const SizedBox(height: 8),
            Text(
              'تعریف سیستم معاملاتی، بک‌تست و پیشنهاد پوزیشن — به‌زودی',
              style: textTheme.bodyMedium,
            ),
          ],
        ),
      ),
    );
  }
}

/// Engine + MT5 status for the AppBar: a colored dot and a short Persian
/// label per item, details in the tooltip. Pure presenter of
/// [EngineStatusProvider]; it never talks to the engine itself.
class _EngineStatusIndicator extends StatelessWidget {
  const _EngineStatusIndicator();

  @override
  Widget build(BuildContext context) {
    final EngineStatusProvider engine = context.watch<EngineStatusProvider>();
    final AppSemanticColors colors = context.appColors;

    final (String engineLabel, Color engineColor) = switch (engine.state) {
      EngineState.running => ('فعال', colors.success),
      EngineState.starting => ('در حال راه‌اندازی', colors.warning),
      EngineState.stopped => ('متوقف', colors.mutedText),
      EngineState.error => ('خطا', colors.error),
    };

    final Mt5Status mt5 = engine.mt5;
    final (String mt5Label, Color mt5Color) = switch (mt5.state) {
      Mt5State.connected => ('متصل', colors.success),
      Mt5State.disconnected => ('قطع', colors.warning),
      Mt5State.error => ('خطا', colors.error),
      Mt5State.accountMismatch => ('حساب نامطابق', colors.error),
      Mt5State.notInitialized => ('راه‌اندازی نشده', colors.mutedText),
      Mt5State.unknown => ('نامشخص', colors.mutedText),
    };

    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        _StatusChip(
          label: 'موتور: $engineLabel',
          color: engineColor,
          tooltip: _engineTooltip(engine),
        ),
        const SizedBox(width: 12),
        _StatusChip(
          label: 'MT5: $mt5Label',
          color: mt5Color,
          tooltip: _mt5Tooltip(engine, mt5),
        ),
        const SizedBox(width: 8),
      ],
    );
  }

  static String _engineTooltip(EngineStatusProvider engine) {
    final EngineHealth? h = engine.health;
    final List<String> lines = [
      switch (engine.state) {
        EngineState.running => 'موتور تحلیل در حال کار است.',
        EngineState.starting => 'موتور تحلیل در حال راه‌اندازی است…',
        EngineState.stopped => 'موتور تحلیل اجرا نشده است.',
        EngineState.error => 'موتور تحلیل با خطا روبه‌رو شد.',
      },
      if (engine.errorMessage != null) engine.errorMessage!,
      if (engine.port != null)
        'پورت: ${engine.port}${engine.attached ? ' (اتصال به موتوری که از قبل اجرا بود)' : ''}',
      if (h?.version != null) 'نسخه: ${h!.version}',
      if (h?.pid != null) 'شناسه پروسس: ${h!.pid}',
      if (h?.devMode == true) 'حالت توسعه (DEV_MODE) فعال است',
      if (h?.timeUtc != null) 'آخرین پاسخ (به وقت محلی): ${_formatLocal(h!.timeUtc!)}',
    ];
    return lines.join('\n');
  }

  static String _mt5Tooltip(EngineStatusProvider engine, Mt5Status mt5) {
    if (engine.state != EngineState.running) {
      return 'وضعیت متاتریدر تا فعال شدن موتور نامشخص است.';
    }
    final List<String> lines = [
      switch (mt5.state) {
        Mt5State.connected => 'به ترمینال متاتریدر ۵ متصل است (فقط خواندنی).',
        Mt5State.disconnected => 'اتصال به ترمینال متاتریدر ۵ قطع است.',
        Mt5State.error => 'خطا در ارتباط با ترمینال متاتریدر ۵.',
        Mt5State.accountMismatch => 'حساب لاگین‌شده در ترمینال با حساب مورد انتظار یکی نیست.',
        Mt5State.notInitialized => 'موتور هنوز به ترمینال متاتریدر ۵ وصل نشده است.',
        Mt5State.unknown => 'وضعیت ترمینال متاتریدر ۵ نامشخص است.',
      },
      if (mt5.server != null) 'سرور: ${mt5.server}',
      if (mt5.loginMasked != null) 'حساب: ${mt5.loginMasked}',
      if (mt5.tradeMode != null) 'نوع حساب: ${mt5.tradeMode}',
      if (mt5.message != null) 'پیام: ${mt5.message}',
    ];
    return lines.join('\n');
  }

  /// Gregorian, local time (the engine sends UTC).
  static String _formatLocal(DateTime utc) {
    final DateTime t = utc.toLocal();
    String two(int v) => v.toString().padLeft(2, '0');
    return '${t.year}-${two(t.month)}-${two(t.day)} ${two(t.hour)}:${two(t.minute)}:${two(t.second)}';
  }
}

class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.label, required this.color, required this.tooltip});

  final String label;
  final Color color;
  final String tooltip;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: Padding(
        padding: const EdgeInsetsDirectional.symmetric(horizontal: 4, vertical: 8),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 10,
              height: 10,
              decoration: BoxDecoration(color: color, shape: BoxShape.circle),
            ),
            const SizedBox(width: 6),
            Text(label, style: Theme.of(context).textTheme.labelLarge),
          ],
        ),
      ),
    );
  }
}
