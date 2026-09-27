import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/date_format.dart';
import '../core/dev_mode.dart';
import '../models/engine_health.dart';
import '../providers/engine_status_provider.dart';
import '../theme/app_semantic_colors.dart';

/// Engine + MT5 status for an AppBar: a colored dot and a short Persian
/// label per item, details in the tooltip. Clicking the engine item opens a
/// menu with "راه‌اندازی مجدد موتور". Pure presenter of
/// [EngineStatusProvider]; it never talks to the engine itself.
class EngineStatusIndicator extends StatelessWidget {
  const EngineStatusIndicator({super.key});

  /// Label of the restart action in the engine item's menu.
  static const String restartLabel = 'راه‌اندازی مجدد موتور';

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
    final (String mt5Label, Color mt5Color) = mt5Appearance(mt5.state, colors);

    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        PopupMenuButton<_EngineMenuAction>(
          tooltip: engineTooltip(engine),
          onSelected: (_EngineMenuAction action) {
            switch (action) {
              case _EngineMenuAction.restart:
                devLog(
                  '[EngineStatusIndicator] restart requested from menu '
                  '(state=${engine.state.name})',
                );
                unawaited(engine.restart());
            }
          },
          itemBuilder: (BuildContext context) => const [
            PopupMenuItem<_EngineMenuAction>(
              value: _EngineMenuAction.restart,
              child: ListTile(
                leading: Icon(Icons.restart_alt),
                title: Text(restartLabel),
                contentPadding: EdgeInsets.zero,
              ),
            ),
          ],
          child: _StatusChip(label: 'موتور: $engineLabel', color: engineColor),
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

  /// Short Persian label and dot color of an MT5 state (also used by the
  /// settings page's connection block).
  static (String, Color) mt5Appearance(Mt5State state, AppSemanticColors colors) =>
      switch (state) {
        Mt5State.connected => ('متصل', colors.success),
        Mt5State.disconnected => ('قطع', colors.warning),
        Mt5State.error => ('خطا', colors.error),
        Mt5State.accountMismatch => ('حساب نامطابق', colors.error),
        Mt5State.notInitialized => ('راه‌اندازی نشده', colors.mutedText),
        Mt5State.unknown => ('نامشخص', colors.mutedText),
      };

  /// One Persian sentence describing an MT5 state.
  static String mt5Description(Mt5State state) => switch (state) {
        Mt5State.connected => 'به ترمینال متاتریدر ۵ متصل است (فقط خواندنی).',
        Mt5State.disconnected => 'اتصال به ترمینال متاتریدر ۵ قطع است.',
        Mt5State.error => 'خطا در ارتباط با ترمینال متاتریدر ۵.',
        Mt5State.accountMismatch =>
          'حساب لاگین‌شده در ترمینال با حساب مورد انتظار یکی نیست.',
        Mt5State.notInitialized =>
          'موتور هنوز به ترمینال متاتریدر ۵ وصل نشده است.',
        Mt5State.unknown => 'وضعیت ترمینال متاتریدر ۵ نامشخص است.',
      };

  /// Shown instead of any MT5 detail while the engine is not running.
  static const String mt5UnknownWhileEngineDown =
      'وضعیت متاتریدر تا فعال شدن موتور نامشخص است.';

  /// Tooltip of the engine item (public for tests).
  static String engineTooltip(EngineStatusProvider engine) {
    final EngineHealth? h = engine.health;
    final List<String> lines = [
      switch (engine.state) {
        EngineState.running => 'موتور تحلیل در حال کار است.',
        EngineState.starting => 'موتور تحلیل در حال راه‌اندازی است…',
        EngineState.stopped => 'موتور تحلیل اجرا نشده است.',
        EngineState.error => 'موتور تحلیل با خطا روبه‌رو شد.',
      },
      if (engine.state == EngineState.starting)
        'اجرای اول بعد از نصب یا راه‌اندازی مجدد ویندوز ممکن است تا '
            '${engine.startupTimeout.inSeconds} ثانیه طول بکشد.',
      if (engine.errorMessage != null) engine.errorMessage!,
      if (engine.awaitingRecovery)
        'بررسی سلامت موتور ادامه دارد؛ اگر موتور پاسخ بدهد، وضعیت خودکار '
            'به «فعال» برمی‌گردد.',
      if (engine.port != null)
        'پورت: ${engine.port}${engine.attached ? ' (اتصال به موتوری که از قبل اجرا بود)' : ''}',
      if (h?.version != null) 'نسخه: ${h!.version}',
      if (h?.pid != null) 'شناسه پروسس: ${h!.pid}',
      if (h?.devMode == true) 'حالت توسعه (DEV_MODE) فعال است',
      if (h?.timeUtc != null)
        'آخرین پاسخ (به وقت محلی): ${formatLocalDateTime(h!.timeUtc!)}',
      'برای «$restartLabel» کلیک کنید.',
    ];
    return lines.join('\n');
  }

  static String _mt5Tooltip(EngineStatusProvider engine, Mt5Status mt5) {
    if (engine.state != EngineState.running) return mt5UnknownWhileEngineDown;
    final List<String> lines = [
      mt5Description(mt5.state),
      if (mt5.server != null) 'سرور: ${mt5.server}',
      if (mt5.loginMasked != null) 'حساب: ${mt5.loginMasked}',
      if (mt5.tradeMode != null) 'نوع حساب: ${mt5.tradeMode}',
      if (mt5.message != null) 'پیام: ${mt5.message}',
    ];
    return lines.join('\n');
  }
}

enum _EngineMenuAction { restart }

class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.label, required this.color, this.tooltip});

  final String label;
  final Color color;

  /// Null when an ancestor (the engine item's menu button) shows it.
  final String? tooltip;

  @override
  Widget build(BuildContext context) {
    final Widget chip = Padding(
      padding: const EdgeInsetsDirectional.symmetric(
        horizontal: 4,
        vertical: 8,
      ),
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
    );
    return tooltip == null ? chip : Tooltip(message: tooltip, child: chip);
  }
}
