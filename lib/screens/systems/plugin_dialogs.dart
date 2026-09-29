import 'package:flutter/material.dart';

import '../../core/date_format.dart';
import '../../core/number_format.dart';
import '../../models/strategy_plugin.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/scrollable_dialog.dart';

/// Persian texts of the plugin dialogs (also used by tests).
abstract final class PluginTextsFa {
  static const String acceptedTitle = 'سیستم پذیرفته شد';
  static const String sameFileTitle = 'این فایل قبلا بارگذاری شده بود';
  static const String reportTitle = 'گزارش اعتبارسنجی';
  static const String rejectedTitle = 'فایل سیستم پذیرفته نشد';
  static const String conflictTitle = 'نسخه تکراری';
  static const String bumpVersionFa = 'برای تغییر منطق، version را بالا ببرید';
  static const String uploadFailedTitle = 'بارگذاری انجام نشد';
  static const String closeLabel = 'بستن';
}

/// The engine's validation report of one plugin version: identity, the
/// static checks it passed and the sandboxed dynamic run. Every value is the
/// engine's.
class PluginReportView extends StatelessWidget {
  const PluginReportView({super.key, required this.plugin, this.created});

  final StrategyPlugin plugin;

  /// Upload answer: true = new version (201), false = same file (200);
  /// null = opened from the list.
  final bool? created;

  static Future<void> show(BuildContext context, StrategyPlugin plugin, {bool? created}) =>
      ScrollableDialog.show<void>(
        context: context,
        title: Text(switch (created) {
          true => '${PluginTextsFa.acceptedTitle}: «${plugin.titleFa}»',
          false => PluginTextsFa.sameFileTitle,
          null => '${PluginTextsFa.reportTitle}: «${plugin.titleFa}»',
        }),
        showCloseButton: true,
        maxWidth: 640,
        content: PluginReportView(key: const ValueKey<String>('plugin-report'), plugin: plugin, created: created),
      );

  @override
  Widget build(BuildContext context) {
    final StrategyPlugin p = plugin;
    final TextTheme tt = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    final PluginDynamicReport? d = p.validation.dynamicReport;
    String yesNo(bool? v) => v == null ? '—' : (v ? 'بله' : 'خیر');
    String seconds(double? v) => v == null ? '—' : '${formatNumber(v, decimals: 2)} ثانیه';
    String count(int? v) => v == null ? '—' : formatNumber(v);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        if (created != null)
          _Note(
            text: created!
                ? 'نسخه ${p.version} سیستم «${p.titleFa}» ذخیره و ${p.registered ? 'در موتور ثبت' : 'نگه‌داری'} شد'
                    '${p.registered ? '؛ حالا در چارت و بک‌تست قابل انتخاب است.' : '.'}'
                : 'همین فایل (همان نام، نسخه و هش) قبلا بارگذاری شده بود؛ همان نسخه دوباره استفاده می‌شود.',
            color: colors.success,
          ),
        const SizedBox(height: 8),
        _kv('شناسه (name)', p.name, ltr: true),
        _kv('نسخه (version)', '${p.version}', ltr: true),
        if (p.filename != null) _kv('فایل', p.filename!, ltr: true),
        _kv('وضعیت', p.status.titleFa),
        _kv('ثبت در موتور', p.registered ? 'بله (همین نسخه اجرا می‌شود)' : 'خیر'),
        if (p.createdUtc != null) _kv('زمان بارگذاری (وقت محلی)', formatLocalDateTime(p.createdUtc!), ltr: true),
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 2),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Expanded(child: Text('هش فایل (sha256)')),
            Flexible(
              flex: 2,
              child: SelectableText(p.sha256, textDirection: TextDirection.ltr, style: const TextStyle(fontSize: 12)),
            ),
          ]),
        ),
        const Divider(),
        Text('بررسی ایستا (پیش از هر اجرا)', style: tt.labelLarge),
        const SizedBox(height: 4),
        if (p.validation.staticChecks.isEmpty)
          Text('موتور فهرست بررسی‌ها را نفرستاد.', style: TextStyle(color: colors.mutedText))
        else
          for (final String c in p.validation.staticChecks)
            Padding(
              key: ValueKey<String>('plugin-static-$c'),
              padding: const EdgeInsets.symmetric(vertical: 1),
              child: Row(children: [
                Icon(Icons.check_circle_outline, size: 16, color: colors.success),
                const SizedBox(width: 6),
                Expanded(child: Text(pluginStaticCheckTitlesFa[c] ?? c)),
              ]),
            ),
        const Divider(),
        Text('بررسی پویا (اجرا در فرآیند جدا روی داده آزمایشی)', style: tt.labelLarge),
        const SizedBox(height: 4),
        if (d == null)
          Text('گزارش اجرای پویا موجود نیست.', style: TextStyle(color: colors.mutedText))
        else ...[
          _kv('کندل‌های آزمایشی (H1)', count(d.bars), ltr: true),
          _kv('کاندیدهای تولیدشده', count(d.candidates), ltr: true),
          _kv('مقایسه با prefix بسته (ضد نگاه به آینده)', '${count(d.prefixChecks)} کندل'),
          _kv('قطعیت (اجرای تکراری، نتیجه یکسان)', yesNo(d.determinism)),
          _kv('تغییر یا حذف داده آینده بی‌اثر بود', yesNo(d.futureMutation)),
          _kv('زمان بررسی', '${seconds(d.elapsedS)}${d.budgetS == null ? '' : ' از سقف ${seconds(d.budgetS)}'}'),
          _kv('راه‌اندازی فرآیند کارگر', seconds(d.workerBootS)),
          _kv('بیشینه حافظه', d.peakMemoryMib == null ? '—' : '${formatNumber(d.peakMemoryMib!, decimals: 0)} MiB'),
        ],
        const SizedBox(height: 8),
        Text(
          'کد فقط در فرآیند جدای موتور و بدون دسترسی به متاتریدر، شبکه و فایل اجرا می‌شود؛ برنامه هیچ سفارشی ارسال نمی‌کند.',
          style: tt.bodySmall?.copyWith(color: colors.mutedText),
        ),
      ],
    );
  }

  Widget _kv(String k, String v, {bool ltr = false}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Expanded(child: Text(k)),
          const SizedBox(width: 8),
          Flexible(
            flex: 2,
            child: Text(v, textAlign: TextAlign.end, textDirection: ltr ? TextDirection.ltr : null),
          ),
        ]),
      );
}

/// Splits the engine's `«خط N: ...»` prefix off an error (null line when
/// the message is not about one line).
({String? line, String text}) splitPluginError(String message) {
  final RegExpMatch? m = RegExp(r'^\s*خط\s*([0-9۰-۹]+)\s*[:：]\s*').firstMatch(message);
  if (m == null) return (line: null, text: message);
  return (line: m.group(1), text: message.substring(m.end));
}

/// Why an upload failed: the engine's Persian message, its per-line errors
/// (422 `invalid_plugin`), and what to do (409 `version_conflict`: bump the
/// version).
class PluginUploadErrorView extends StatelessWidget {
  const PluginUploadErrorView({super.key, required this.error, required this.fileName});

  final EngineApiException error;
  final String fileName;

  static Future<void> show(BuildContext context, EngineApiException error, {required String fileName}) =>
      ScrollableDialog.show<void>(
        context: context,
        title: Text(switch (error.code) {
          'version_conflict' => PluginTextsFa.conflictTitle,
          'invalid_plugin' || 'invalid_body' => PluginTextsFa.rejectedTitle,
          _ => PluginTextsFa.uploadFailedTitle,
        }),
        showCloseButton: true,
        maxWidth: 680,
        content: PluginUploadErrorView(
          key: const ValueKey<String>('plugin-upload-error'),
          error: error,
          fileName: fileName,
        ),
      );

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final TextTheme tt = Theme.of(context).textTheme;
    final bool conflict = error.code == 'version_conflict';
    final bool invalid = error.code == 'invalid_plugin' || error.code == 'invalid_body';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text('فایل: $fileName', textDirection: TextDirection.rtl, style: tt.bodySmall),
        const SizedBox(height: 6),
        Text(error.messageFa, style: TextStyle(color: colors.error, fontWeight: FontWeight.bold)),
        if (conflict) ...[
          const SizedBox(height: 8),
          Container(
            key: const ValueKey<String>('plugin-bump-version'),
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(color: colors.warningContainer, borderRadius: BorderRadius.circular(8)),
            child: Text(
              '${PluginTextsFa.bumpVersionFa}: هر نسخه سیستم فقط یک محتوا دارد تا نتیجه بک‌تست‌های قبلی دقیقا '
              'قابل بازتولید بماند. عدد version را در کلاس Strategy فایل یکی بالاتر ببرید و دوباره بارگذاری کنید؛ '
              'نسخه قبلی حفظ می‌شود.',
              style: TextStyle(color: colors.onWarningContainer),
            ),
          ),
        ],
        if (error.errorsFa.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text('خطاها (${error.errorsFa.length})', style: tt.labelLarge),
          const SizedBox(height: 4),
          for (int i = 0; i < error.errorsFa.length; i++) _errorRow(context, i, error.errorsFa[i]),
        ] else if (error.detail != null && !invalid) ...[
          const SizedBox(height: 6),
          Text(error.detail!, textDirection: TextDirection.ltr, style: tt.bodySmall?.copyWith(color: colors.mutedText)),
        ],
        if (invalid) ...[
          const SizedBox(height: 10),
          Text(
            'خطاها را در فایل برطرف کنید و دوباره بارگذاری کنید. راهنمای کامل قرارداد، ممنوعیت‌ها و دستور خودآزمایی در '
            'ابتدای «قالب» آمده است.',
            style: tt.bodySmall?.copyWith(color: colors.mutedText),
          ),
        ],
      ],
    );
  }

  Widget _errorRow(BuildContext context, int i, String message) {
    final ({String? line, String text}) e = splitPluginError(message);
    final AppSemanticColors colors = context.appColors;
    return Padding(
      key: ValueKey<String>('plugin-error-$i'),
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Container(
          width: 64,
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
          margin: const EdgeInsetsDirectional.only(end: 8),
          decoration: BoxDecoration(
            color: colors.error.withValues(alpha: 0.10),
            borderRadius: BorderRadius.circular(6),
          ),
          child: Text(
            e.line == null ? 'فایل' : 'خط ${e.line}',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 12, color: colors.error, fontWeight: FontWeight.bold),
          ),
        ),
        Expanded(child: SelectableText(e.text)),
      ]),
    );
  }
}

class _Note extends StatelessWidget {
  const _Note({required this.text, required this.color});

  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) => Text(text, style: TextStyle(color: color, fontWeight: FontWeight.w600));
}
