import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../models/backtest_models.dart';
import '../../models/market_data.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/loading_button.dart';
import '../../widgets/number_stepper_field.dart';
import '../../widgets/section_card.dart';
import 'backtest_controller.dart';
import 'backtest_format.dart';

/// «اجرای جدید»: symbol, mode (manual Gregorian period / random windows with
/// a visible seed), optional costs, and the run button. Shows the engine's
/// Persian 422 messages under the fields they are about.
class BacktestFormPanel extends StatelessWidget {
  const BacktestFormPanel({super.key, required this.controller});

  final BacktestController controller;

  static const String runLabel = 'اجرای بک‌تست';
  static const String newSeedLabel = 'seed تصادفی جدید';
  static const String zeroSpreadLabel = 'بدون هزینه اسپرد';

  @override
  Widget build(BuildContext context) {
    final BacktestController c = controller;
    final TextTheme text = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    return ListView(
      key: const ValueKey<String>('bt-form'),
      padding: const EdgeInsets.all(16),
      children: [
        _label(context, 'نماد'),
        _symbolField(context),
        FieldErrors(c.errorsOf(BacktestField.symbol)),
        const SizedBox(height: 16),
        _label(context, 'حالت اجرا'),
        SegmentedButton<BacktestMode>(
          key: const ValueKey<String>('bt-mode'),
          segments: [
            for (final BacktestMode m in BacktestMode.values)
              ButtonSegment<BacktestMode>(
                value: m,
                label: Text(m.labelFa, key: ValueKey<String>('bt-mode-${m.code}')),
                icon: Icon(m == BacktestMode.manual ? Icons.date_range : Icons.shuffle),
              ),
          ],
          selected: {c.mode},
          onSelectionChanged: (Set<BacktestMode> s) => c.setMode(s.first),
        ),
        const SizedBox(height: 16),
        if (c.mode == BacktestMode.manual) _manualFields(context) else _randomFields(context),
        const SizedBox(height: 20),
        _costFields(context),
        const SizedBox(height: 20),
        if (c.submitError != null)
          ErrorBanner(
            key: const ValueKey<String>('bt-submit-error'),
            title: c.submitError!.messageFa,
            messages: c.submitError!.errorsFa.isNotEmpty
                ? c.generalErrors
                : [if (c.submitError!.detail != null) c.submitError!.detail!],
          ),
        LoadingActionButton(
          key: const ValueKey<String>('bt-run'),
          onPressed: c.submit,
          builder: (BuildContext context, VoidCallback? onPressed, bool busy) => ElevatedButton.icon(
            onPressed: c.canSubmit && !busy ? onPressed : null,
            icon: busy || c.isSubmitting
                ? const SizedBox.square(dimension: 16, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.play_arrow),
            label: const Text(runLabel),
          ),
        ),
        if (c.activeRunId != null)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              'اجرای #${c.activeRunId} در حال انجام است؛ بعد از پایان یا لغو آن می‌توانید اجرای جدیدی بسازید.',
              style: text.bodySmall?.copyWith(color: colors.mutedText),
            ),
          ),
      ],
    );
  }

  Widget _label(BuildContext context, String s) => Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Text(s, style: Theme.of(context).textTheme.titleSmall),
      );

  Widget _hint(BuildContext context, String s) => Padding(
        padding: const EdgeInsets.only(top: 6),
        child: Text(s, style: Theme.of(context).textTheme.bodySmall?.copyWith(color: context.appColors.mutedText)),
      );

  Widget _symbolField(BuildContext context) {
    final BacktestController c = controller;
    if (c.symbolsError != null) {
      return Row(children: [
        Expanded(
          child: Text(c.symbolsError!.messageFa, style: TextStyle(color: context.appColors.error)),
        ),
        TextButton(onPressed: c.retrySymbols, child: const Text('تلاش دوباره')),
      ]);
    }
    final List<String> names = [for (final SymbolItem s in c.symbols) s.symbol];
    // A prefilled symbol the engine does not know stays visible (the engine will reject it).
    if (c.symbol != null && !names.contains(c.symbol)) names.add(c.symbol!);
    return DropdownButton<String>(
      key: const ValueKey<String>('bt-symbol'),
      value: c.symbol,
      isExpanded: true,
      hint: Text(c.symbolsLoading ? 'در حال بارگذاری نمادها…' : 'انتخاب نماد'),
      items: [
        for (final String s in names)
          DropdownMenuItem<String>(value: s, child: Text(s, textDirection: TextDirection.ltr)),
      ],
      onChanged: c.setSymbol,
    );
  }

  Future<void> _pickDay(BuildContext context, {required bool isFrom}) async {
    final BacktestController c = controller;
    final DateTime now = DateTime.now();
    final DateTime? current = isFrom ? c.fromDay : c.toDay;
    // The engine's allowed days (GET /backtests/limits); unbounded when unknown.
    final ({DateTime first, DateTime last})? days = c.limits?.pickableDays;
    final DateTime first = days == null ? DateTime(2000) : _calendarDay(days.first);
    final DateTime last = days == null ? DateTime(now.year + 1, 12, 31) : _calendarDay(days.last);
    DateTime initial = current == null ? DateTime(now.year, now.month, now.day) : _calendarDay(current);
    if (initial.isBefore(first)) initial = first;
    if (initial.isAfter(last)) initial = last;
    final DateTime? picked = await showDatePicker(
      context: context,
      initialDate: initial,
      firstDate: first,
      lastDate: last,
      helpText: isFrom ? 'روز شروع بازه (UTC)' : 'روز پایان بازه (UTC)',
    );
    if (picked == null) return;
    if (isFrom) {
      c.setFromDay(picked);
    } else {
      c.setToDay(picked);
    }
  }

  Widget _manualFields(BuildContext context) {
    final BacktestController c = controller;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      _label(context, 'بازه (میلادی)'),
      Row(children: [
        Expanded(
          child: OutlinedButton.icon(
            key: const ValueKey<String>('bt-from'),
            onPressed: () => _pickDay(context, isFrom: true),
            icon: const Icon(Icons.event, size: 18),
            label: Text('از ${fmtDay(c.fromDay)}'),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: OutlinedButton.icon(
            key: const ValueKey<String>('bt-to'),
            onPressed: () => _pickDay(context, isFrom: false),
            icon: const Icon(Icons.event, size: 18),
            label: Text('تا ${fmtDay(c.toDay)}'),
          ),
        ),
      ]),
      if (c.hasExactPeriod) _exactPeriodNote(context),
      if (c.limits?.pickableDays != null) _limitsNote(context),
      FieldErrors(c.errorsOf(BacktestField.period), key: const ValueKey<String>('bt-period-errors')),
      _hint(
          context,
          'روزها به وقت UTC هستند و روز پایان هم جزو بازه است. ابتدای بازه باید بعد از زمان لازم برای '
          'آماده شدن کانال و ATR باشد؛ در غیر این صورت موتور پیام خطا می‌دهد.'),
    ]);
  }

  /// A UTC day as the date picker's calendar date.
  static DateTime _calendarDay(DateTime utcDay) => DateTime(utcDay.year, utcDay.month, utcDay.day);

  /// «بازه مجاز»: the whole days the pickers allow, with the engine's exact
  /// instants in the tooltip.
  Widget _limitsNote(BuildContext context) {
    final BacktestLimits l = controller.limits!;
    final ({DateTime first, DateTime last}) days = l.pickableDays!;
    String minute(DateTime? t) {
      if (t == null) return kDash;
      final DateTime u = t.toUtc();
      return '${fmtDay(u)} ${u.hour.toString().padLeft(2, '0')}:${u.minute.toString().padLeft(2, '0')}';
    }

    return Tooltip(
      message: [
        'موتور: از ${minute(l.earliestStart)} تا ${minute(l.dataEnd)} UTC (پایان باز).',
        'روز اول بعد از آماده شدن کانال و ATR است و روز آخر، آخرین روز کامل داده کش.',
        if (l.noteFa != null) l.noteFa!,
      ].join('\n'),
      child: Padding(
        padding: const EdgeInsets.only(top: 6),
        child: Text(
          'بازه مجاز: ${fmtDay(days.first)} تا ${fmtDay(days.last)} (UTC)',
          key: const ValueKey<String>('bt-limits-note'),
          style: Theme.of(context).textTheme.bodySmall?.copyWith(color: context.appColors.info),
        ),
      ),
    );
  }

  /// The exact period a chart prefill will run (instead of the whole days above).
  Widget _exactPeriodNote(BuildContext context) {
    final BacktestController c = controller;
    String minute(DateTime t) {
      final DateTime u = t.toUtc();
      return '${fmtDay(u)} ${u.hour.toString().padLeft(2, '0')}:${u.minute.toString().padLeft(2, '0')}';
    }

    final AppSemanticColors colors = context.appColors;
    return Container(
      key: const ValueKey<String>('bt-exact-period'),
      margin: const EdgeInsets.only(top: 6),
      padding: const EdgeInsetsDirectional.only(start: 8, end: 2, top: 2, bottom: 2),
      decoration: BoxDecoration(color: colors.warningContainer, borderRadius: BorderRadius.circular(6)),
      child: Row(children: [
        Expanded(
          child: Text(
            'بازه دقیق از چارت: ${minute(c.exactFrom!)} تا ${minute(c.exactTo!)} UTC (پایان باز)',
            style: TextStyle(fontSize: 12, color: colors.onWarningContainer),
          ),
        ),
        TextButton(
          key: const ValueKey<String>('bt-exact-period-clear'),
          onPressed: c.clearExactPeriod,
          child: const Text('حذف'),
        ),
      ]),
    );
  }

  Widget _randomFields(BuildContext context) {
    final BacktestController c = controller;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            NumberStepperField(
              key: const ValueKey<String>('bt-windows-count'),
              label: 'تعداد پنجره‌ها',
              value: c.windowsCount.toDouble(),
              min: BacktestController.windowsCountMin.toDouble(),
              max: BacktestController.windowsCountMax.toDouble(),
              decimalDigits: 0,
              maxLength: 3,
              onChanged: (double v) => c.setWindowsCount(v.round()),
            ),
            FieldErrors(c.errorsOf(BacktestField.windowsCount)),
          ]),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            NumberStepperField(
              key: const ValueKey<String>('bt-window-months'),
              label: 'طول هر پنجره (ماه)',
              value: c.windowMonths.toDouble(),
              min: BacktestController.windowMonthsMin.toDouble(),
              max: BacktestController.windowMonthsMax.toDouble(),
              decimalDigits: 0,
              maxLength: 2,
              onChanged: (double v) => c.setWindowMonths(v.round()),
            ),
            FieldErrors(c.errorsOf(BacktestField.windowMonths)),
          ]),
        ),
      ]),
      const SizedBox(height: 12),
      Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Expanded(
          child: KeyedSubtree(
            key: ValueKey<int>(c.formGeneration),
            child: TextFormField(
              key: const ValueKey<String>('bt-seed'),
              initialValue: c.seedText,
              textDirection: TextDirection.ltr,
              keyboardType: TextInputType.number,
              inputFormatters: [FilteringTextInputFormatter.allow(RegExp('[0-9۰-۹]'))],
              maxLength: 19,
              decoration: const InputDecoration(
                labelText: 'seed',
                counterText: '',
                border: OutlineInputBorder(borderRadius: BorderRadius.all(Radius.circular(20))),
              ),
              onChanged: c.setSeedText,
            ),
          ),
        ),
        const SizedBox(width: 8),
        Padding(
          padding: const EdgeInsets.only(top: 6),
          child: OutlinedButton.icon(
            key: const ValueKey<String>('bt-new-seed'),
            onPressed: c.newRandomSeed,
            icon: const Icon(Icons.casino_outlined, size: 18),
            label: const Text(newSeedLabel),
          ),
        ),
      ]),
      FieldErrors(c.errorsOf(BacktestField.seed)),
      FieldErrors(c.errorsOf(BacktestField.period), key: const ValueKey<String>('bt-period-errors')),
      _hint(
          context,
          'پنجره‌ها با همین seed انتخاب می‌شوند و seed همراه نتیجه ذخیره می‌شود؛ با همان seed نتیجه دقیقا '
          'تکرار می‌شود. اگر خالی بماند، موتور یک seed می‌سازد و ذخیره می‌کند.'),
    ]);
  }

  Widget _costFields(BuildContext context) {
    final BacktestController c = controller;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      _label(context, 'هزینه‌ها'),
      NumberStepperField(
        key: const ValueKey<String>('bt-commission'),
        label: 'کمیسیون هر لات در هر طرف (\$)',
        value: c.commission,
        min: 0,
        max: BacktestController.commissionMax,
        step: 0.5,
        decimalDigits: 2,
        maxLength: 7,
        onChanged: c.setCommission,
      ),
      FieldErrors(c.errorsOf(BacktestField.commission)),
      const SizedBox(height: 12),
      Text('اسپرد جایگزین (کندل‌های بدون اسپرد ثبت‌شده بروکر)', style: Theme.of(context).textTheme.bodyMedium),
      const SizedBox(height: 6),
      SegmentedButton<SpreadChoice>(
        key: const ValueKey<String>('bt-spread'),
        showSelectedIcon: false,
        segments: const [
          ButtonSegment<SpreadChoice>(value: SpreadChoice.auto, label: Text('خودکار', key: ValueKey('bt-spread-auto'))),
          ButtonSegment<SpreadChoice>(
            value: SpreadChoice.zero,
            label: Text(zeroSpreadLabel, key: ValueKey('bt-spread-zero')),
          ),
          ButtonSegment<SpreadChoice>(
            value: SpreadChoice.custom,
            label: Text('مقدار دلخواه', key: ValueKey('bt-spread-custom')),
          ),
        ],
        selected: {c.spreadChoice},
        onSelectionChanged: (Set<SpreadChoice> s) => c.setSpreadChoice(s.first),
      ),
      if (c.spreadChoice == SpreadChoice.custom) ...[
        const SizedBox(height: 8),
        NumberStepperField(
          key: const ValueKey<String>('bt-custom-spread'),
          label: 'اسپرد جایگزین (پوینت)',
          value: c.customSpreadPoints.toDouble(),
          min: 0,
          max: BacktestController.fallbackSpreadMax.toDouble(),
          decimalDigits: 0,
          maxLength: 6,
          onChanged: (double v) => c.setCustomSpreadPoints(v.round()),
        ),
      ],
      FieldErrors(c.errorsOf(BacktestField.fallbackSpread)),
      _hint(
        context,
        switch (c.spreadChoice) {
          SpreadChoice.auto => 'اسپرد تاریخی بروکر همیشه استفاده می‌شود. برای کندل‌هایی که بروکر اسپردی ثبت نکرده، '
              'موتور میانه اسپرد مشاهده‌شده را به کار می‌برد.',
          SpreadChoice.zero => 'برای کندل‌های بدون اسپرد ثبت‌شده، هزینه اسپرد صفر فرض می‌شود و نتیجه با همین برچسب '
              'نمایش داده می‌شود.',
          SpreadChoice.custom => 'برای کندل‌های بدون اسپرد ثبت‌شده، همین مقدار (پوینت) استفاده می‌شود.',
        },
      ),
      _hint(context, 'سوآپ مدل نمی‌شود (برچسب «بدون سوآپ» روی نتیجه می‌آید).'),
    ]);
  }
}
