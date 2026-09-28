import 'dart:async';

import 'package:flutter/material.dart';

import '../../core/dev_mode.dart';
import '../../models/backtest_export.dart';
import '../../models/backtest_models.dart';
import '../../services/backtest_exporter.dart';
import '../../services/engine_api.dart';
import '../../widgets/app_toast.dart';

/// «خروجی» button of the results header: Excel (every table) or one table
/// as CSV, saved where the user picks. Enabled once the run has ended
/// (the engine answers 409 while it is queued/running). Column headers are
/// Persian by default; «سرستون انگلیسی» switches to the stable English keys.
class BacktestExportMenu extends StatefulWidget {
  const BacktestExportMenu({super.key, required this.api, required this.summary, required this.saver});

  final EngineApi api;
  final BacktestRunSummary summary;
  final ExportFileSaver saver;

  static const String buttonLabel = 'خروجی';
  static const String englishHeaderLabel = 'سرستون انگلیسی';
  static const String notFinishedTooltip = 'خروجی بعد از پایان اجرا فعال می‌شود.';
  static const String readyTooltip = 'ذخیره نتایج این اجرا به‌صورت Excel یا CSV';
  static const String openFolderLabel = 'باز کردن پوشه';
  static const String failedTitle = 'خروجی گرفته نشد';

  @override
  State<BacktestExportMenu> createState() => _BacktestExportMenuState();
}

class _BacktestExportMenuState extends State<BacktestExportMenu> {
  bool _englishHeader = false;
  bool _busy = false;

  bool get _enabled => widget.summary.status.isTerminal && !_busy;

  Future<void> _export(BacktestExportKind kind) async {
    final int runId = widget.summary.id;
    final BacktestExporter exporter = BacktestExporter(api: widget.api, saver: widget.saver);
    setState(() => _busy = true);
    final BacktestExportOutcome outcome;
    try {
      outcome = await exporter.export(
        runId,
        kind,
        header: _englishHeader ? BacktestExportHeader.en : BacktestExportHeader.fa,
      );
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    if (!mounted) return;
    switch (outcome) {
      case BacktestExportCancelled():
        break;
      case BacktestExportSaved(:final String path):
        final ScaffoldMessengerState? messenger = ScaffoldMessenger.maybeOf(context);
        if (messenger == null) {
          showSuccessToast(context, title: 'خروجی ذخیره شد', subtitle: path);
          break;
        }
        messenger
          ..hideCurrentSnackBar()
          ..showSnackBar(SnackBar(
            key: const ValueKey<String>('bt-export-saved'),
            content: Text('${kind.labelFa} بک‌تست #$runId ذخیره شد:\n$path'),
            duration: const Duration(seconds: 6),
            action: widget.saver.canRevealInFolder
                ? SnackBarAction(
                    label: BacktestExportMenu.openFolderLabel,
                    onPressed: () => unawaited(exporter.reveal(path)),
                  )
                : null,
          ));
      case BacktestExportFailed(:final String messageFa, :final String? detail):
        showErrorToast(
          context,
          title: BacktestExportMenu.failedTitle,
          subtitle: [messageFa, if (detail != null && detail.isNotEmpty) detail].join('\n'),
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    final BacktestRunSummary s = widget.summary;
    final List<BacktestExportKind> kinds = BacktestExportKind.forMode(s.mode);
    return MenuAnchor(
      builder: (BuildContext context, MenuController menu, Widget? _) => Tooltip(
        message: s.status.isTerminal ? BacktestExportMenu.readyTooltip : BacktestExportMenu.notFinishedTooltip,
        child: TextButton.icon(
          key: const ValueKey<String>('bt-export'),
          onPressed: _enabled
              ? () {
                  devLog('[BacktestExportMenu] menu ${menu.isOpen ? 'closed' : 'opened'} for run #${s.id}');
                  menu.isOpen ? menu.close() : menu.open();
                }
              : null,
          icon: _busy
              ? const SizedBox.square(dimension: 16, child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.file_download_outlined),
          label: const Text(BacktestExportMenu.buttonLabel),
        ),
      ),
      menuChildren: [
        for (final BacktestExportKind k in kinds)
          MenuItemButton(
            key: ValueKey<String>('bt-export-${k.name}'),
            leadingIcon: Icon(
              k.format == BacktestExportFormat.xlsx ? Icons.table_view_outlined : Icons.description_outlined,
            ),
            onPressed: () => unawaited(_export(k)),
            child: Text(k.labelFa),
          ),
        const Divider(height: 1),
        CheckboxMenuButton(
          key: const ValueKey<String>('bt-export-english-header'),
          value: _englishHeader,
          closeOnActivate: false,
          onChanged: (bool? v) {
            setState(() => _englishHeader = v ?? false);
            devLog('[BacktestExportMenu] english header = $_englishHeader');
          },
          child: const Text(BacktestExportMenu.englishHeaderLabel),
        ),
      ],
    );
  }
}
