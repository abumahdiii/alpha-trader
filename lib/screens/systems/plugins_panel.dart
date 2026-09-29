import 'dart:async';

import 'package:flutter/material.dart';

import '../../core/date_format.dart';
import '../../core/dev_mode.dart';
import '../../models/strategy_plugin.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/scrollable_dialog.dart';
import 'plugin_dialogs.dart';
import 'plugins_controller.dart';

/// Labels of the plugin actions (also used by tests).
abstract final class PluginActionsFa {
  static const String download = 'دانلود قالب';
  static const String upload = 'بارگذاری سیستم';
  static const String openFolder = 'باز کردن پوشه';
  static const String validating = 'در حال بررسی ایمنی و اعتبارسنجی… (تا حدود ۳۰ ثانیه)';
  static const String safetyNote =
      'کد بارگذاری‌شده فقط در یک فرآیند جدا و بدون دسترسی به متاتریدر، شبکه و فایل اجرا می‌شود؛ برنامه هیچ سفارشی ارسال نمی‌کند.';
  static const String report = 'گزارش اعتبارسنجی';
  static const String disable = 'غیرفعال کردن';
  static const String enable = 'فعال کردن';
  static const String archive = 'بایگانی';
  static const String cancel = 'انصراف';
  static const String sectionTitle = 'فایل‌های بارگذاری‌شده';
}

/// Top of the «سیستم‌ها» page: «بارگذاری سیستم», «دانلود قالب», the
/// validation progress of an upload and the safety note.
class PluginActionsBar extends StatelessWidget {
  const PluginActionsBar({super.key, required this.controller});

  final PluginsController controller;

  Future<void> _download(BuildContext context) async {
    final TemplateDownloadOutcome outcome = await controller.downloadTemplate();
    if (!context.mounted) return;
    switch (outcome) {
      case TemplateDownloadCancelled():
        break;
      case TemplateDownloadSaved(:final String path):
        final ScaffoldMessengerState? messenger = ScaffoldMessenger.maybeOf(context);
        if (messenger == null) {
          showSuccessToast(context, title: 'قالب ذخیره شد', subtitle: path);
          break;
        }
        messenger
          ..hideCurrentSnackBar()
          ..showSnackBar(SnackBar(
            key: const ValueKey<String>('plugin-template-saved'),
            content: Text('قالب سیستم ذخیره شد:\n$path'),
            duration: const Duration(seconds: 6),
            action: controller.files.canRevealInFolder
                ? SnackBarAction(
                    label: PluginActionsFa.openFolder,
                    onPressed: () => unawaited(_reveal(path)),
                  )
                : null,
          ));
      case TemplateDownloadFailed(:final String messageFa, :final String? detail):
        showErrorToast(
          context,
          title: 'دانلود قالب انجام نشد',
          subtitle: [messageFa, if (detail != null && detail.isNotEmpty) detail].join('\n'),
        );
    }
  }

  Future<void> _reveal(String path) async {
    try {
      await controller.files.revealInFolder(path);
      devLog('[Plugins] reveal in folder: $path');
    } catch (e) {
      devLog('[Plugins] reveal in folder failed for $path: $e');
    }
  }

  Future<void> _upload(BuildContext context) async {
    final PluginUploadOutcome outcome = await controller.upload();
    if (!context.mounted) return;
    switch (outcome) {
      case PluginUploadCancelled():
        break;
      case PluginUploadFileError(:final error):
        showErrorToast(
          context,
          title: PluginTextsFa.uploadFailedTitle,
          subtitle: [error.messageFa, if (error.detail != null) error.detail!].join('\n'),
        );
      case PluginUploadAccepted(:final result):
        await PluginReportView.show(context, result.plugin, created: result.created);
      case PluginUploadRejected(:final error, :final fileName):
        await PluginUploadErrorView.show(context, error, fileName: fileName);
    }
  }

  @override
  Widget build(BuildContext context) {
    final PluginsController c = controller;
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final bool busy = c.uploading || c.downloading;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Wrap(spacing: 12, runSpacing: 8, crossAxisAlignment: WrapCrossAlignment.center, children: [
          FilledButton.icon(
            key: const ValueKey<String>('plugin-upload'),
            onPressed: busy ? null : () => unawaited(_upload(context)),
            icon: c.uploading
                ? const SizedBox.square(dimension: 16, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.upload_file),
            label: const Text(PluginActionsFa.upload),
          ),
          OutlinedButton.icon(
            key: const ValueKey<String>('plugin-download-template'),
            onPressed: busy ? null : () => unawaited(_download(context)),
            icon: c.downloading
                ? const SizedBox.square(dimension: 16, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.download_outlined),
            label: const Text(PluginActionsFa.download),
          ),
          Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(Icons.shield_outlined, size: 18, color: colors.info),
            const SizedBox(width: 6),
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 720),
              child: Text(
                PluginActionsFa.safetyNote,
                key: const ValueKey<String>('plugin-safety-note'),
                style: tt.bodySmall?.copyWith(color: colors.mutedText),
              ),
            ),
          ]),
        ]),
        if (c.uploading) ...[
          const SizedBox(height: 10),
          Container(
            key: const ValueKey<String>('plugin-validating'),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: colors.surfaceMuted, borderRadius: BorderRadius.circular(8)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              Text('${PluginActionsFa.validating} — ${c.uploadingFile}'),
              const SizedBox(height: 6),
              const LinearProgressIndicator(minHeight: 3),
            ]),
          ),
        ],
      ]),
    );
  }
}

/// «فایل‌های بارگذاری‌شده»: every stored plugin version with its status,
/// short hash, upload time and whether the engine runs it, plus the
/// report / enable / disable / archive actions.
class PluginListSection extends StatelessWidget {
  const PluginListSection({super.key, required this.controller});

  final PluginsController controller;

  Future<void> _run(BuildContext context, String titleFa, Future<EngineApiException?> Function() action) async {
    final EngineApiException? e = await action();
    if (e == null || !context.mounted) return;
    showErrorToast(context, title: titleFa, subtitle: e.userMessage);
  }

  Future<void> _confirmArchive(BuildContext context, StrategyPlugin p) async {
    final bool? ok = await ScrollableDialog.show<bool>(
      context: context,
      title: const Text('بایگانی سیستم'),
      content: Text(
        'نسخه ${p.version} سیستم «${p.titleFa}» بایگانی می‌شود: دیگر اجرا نمی‌شود و در چارت و بک‌تست قابل انتخاب '
        'نیست، ولی فایل آن برای بازتولید نتایج قبلی نگه داشته می‌شود. برای استفاده دوباره، همان فایل را بارگذاری کنید.',
        key: const ValueKey<String>('plugin-archive-confirm'),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text(PluginActionsFa.cancel)),
        FilledButton(
          key: const ValueKey<String>('plugin-archive-ok'),
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text(PluginActionsFa.archive),
        ),
      ],
    );
    devLog('[Plugins] archive ${p.key}: ${ok == true ? 'confirmed' : 'cancelled'}');
    if (ok != true || !context.mounted) return;
    await _run(context, 'بایگانی انجام نشد', () => controller.archive(p));
  }

  @override
  Widget build(BuildContext context) {
    final PluginsController c = controller;
    final TextTheme tt = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final EngineApiException? error = c.error;
    return Column(
      key: const ValueKey<String>('plugin-list'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
          child: Row(children: [
            Expanded(child: Text(PluginActionsFa.sectionTitle, style: tt.titleSmall)),
            if (c.loading) const SizedBox.square(dimension: 14, child: CircularProgressIndicator(strokeWidth: 2)),
          ]),
        ),
        if (error != null && c.plugins.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Row(children: [
              Expanded(
                child: Text('خواندن فهرست پلاگین‌ها ناموفق بود: ${error.messageFa}',
                    style: tt.bodySmall?.copyWith(color: colors.error)),
              ),
              TextButton(onPressed: c.load, child: const Text('تلاش دوباره')),
            ]),
          )
        else if (c.plugins.isEmpty && !c.loading)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
            child: Text(
              'هنوز سیستمی بارگذاری نشده است. «دانلود قالب» را بزنید، فایل را کامل کنید و با «بارگذاری سیستم» بفرستید.',
              style: tt.bodySmall?.copyWith(color: colors.mutedText),
            ),
          ),
        for (final StrategyPlugin p in c.plugins) _tile(context, p),
      ],
    );
  }

  Widget _tile(BuildContext context, StrategyPlugin p) {
    final PluginsController c = controller;
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final bool busy = c.isBusy(p);
    final Color statusColor = switch (p.status) {
      PluginStatus.active => colors.success,
      PluginStatus.disabled => colors.warning,
      _ => colors.mutedText,
    };
    return ListTile(
      key: ValueKey<String>('plugin-${p.key}'),
      dense: true,
      leading: Tooltip(
        message: p.registered
            ? 'ثبت در موتور: همین نسخه برای «${p.name}» اجرا می‌شود'
            : 'ثبت نشده: این نسخه الان اجرا نمی‌شود',
        child: Icon(
          p.registered ? Icons.play_circle_fill : Icons.pause_circle_outline,
          key: ValueKey<String>('plugin-registered-${p.key}-${p.registered}'),
          color: p.registered ? colors.success : colors.mutedText,
        ),
      ),
      title: Row(children: [
        Flexible(child: Text('${p.titleFa} — v${p.version}', overflow: TextOverflow.ellipsis)),
        const SizedBox(width: 6),
        Container(
          key: ValueKey<String>('plugin-status-${p.key}'),
          padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
          decoration: BoxDecoration(
            color: statusColor.withValues(alpha: 0.15),
            borderRadius: BorderRadius.circular(6),
          ),
          child: Text(p.status.titleFa, style: TextStyle(fontSize: 11, color: statusColor)),
        ),
      ]),
      subtitle: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(
          [p.name, if (p.filename != null) p.filename!].join(' · '),
          textDirection: TextDirection.ltr,
          style: tt.bodySmall,
        ),
        Tooltip(
          message: 'هش کامل فایل (sha256): ${p.sha256}',
          child: Text(
            'هش: ${p.shortSha256}'
            '${p.createdUtc == null ? '' : ' — ${formatLocalDateTime(p.createdUtc!, seconds: false)} (وقت محلی)'}',
            style: tt.bodySmall?.copyWith(color: colors.mutedText),
          ),
        ),
      ]),
      trailing: busy
          ? const SizedBox.square(dimension: 18, child: CircularProgressIndicator(strokeWidth: 2))
          : PopupMenuButton<String>(
              key: ValueKey<String>('plugin-menu-${p.key}'),
              tooltip: 'عملیات',
              onSelected: (String action) {
                devLog('[Plugins] menu ${p.key}: $action');
                switch (action) {
                  case 'report':
                    unawaited(PluginReportView.show(context, p));
                  case 'disable':
                    unawaited(_run(context, 'غیرفعال‌سازی انجام نشد', () => c.setEnabled(p, false)));
                  case 'enable':
                    unawaited(_run(context, 'فعال‌سازی انجام نشد', () => c.setEnabled(p, true)));
                  case 'archive':
                    unawaited(_confirmArchive(context, p));
                }
              },
              itemBuilder: (BuildContext context) => [
                const PopupMenuItem<String>(value: 'report', child: Text(PluginActionsFa.report)),
                if (p.status == PluginStatus.active)
                  const PopupMenuItem<String>(value: 'disable', child: Text(PluginActionsFa.disable)),
                if (p.status == PluginStatus.disabled)
                  const PopupMenuItem<String>(value: 'enable', child: Text(PluginActionsFa.enable)),
                if (p.status == PluginStatus.active || p.status == PluginStatus.disabled)
                  const PopupMenuItem<String>(value: 'archive', child: Text(PluginActionsFa.archive)),
              ],
            ),
    );
  }
}
