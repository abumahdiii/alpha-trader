import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../../core/app_logger.dart';
import '../../core/date_format.dart';
import '../../core/dev_mode.dart';
import '../../models/strategy.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/loading_button.dart';
import '../../widgets/scrollable_dialog.dart';
import '../../widgets/section_card.dart';
import 'param_field.dart';

/// Parameter editor of one strategy. The form is generated from the
/// engine's `param_schema` -- no field is hard-coded here -- and saves the
/// FULL parameter set with `PUT /strategies/{name}`. «بازگشت به پیش‌فرض»
/// sends `{"params": {}}` (full replacement with nothing = all defaults)
/// after a confirmation.
class StrategyEditor extends StatefulWidget {
  const StrategyEditor({
    super.key,
    required this.api,
    required this.strategy,
    required this.onSaved,
    this.plugin,
  });

  final EngineApi api;
  final StrategyInfo strategy;

  /// Set when [strategy] is an uploaded plugin (its registered version):
  /// the header shows the source and the file hash.
  final PluginRef? plugin;

  /// Reports the engine's answer after a save/reset so the list is updated.
  final ValueChanged<StrategyInfo> onSaved;

  static const String saveLabel = 'ذخیره';
  static const String discardLabel = 'بازگشت';
  static const String resetLabel = 'بازگشت به پیش‌فرض';
  static const String cancelLabel = 'انصراف';

  @override
  State<StrategyEditor> createState() => _StrategyEditorState();
}

class _StrategyEditorState extends State<StrategyEditor> with AsyncActionGuard<StrategyEditor> {
  late StrategyInfo _info;
  late Map<String, Object> _draft;
  Map<String, List<String>> _fieldErrors = const {};
  List<String> _generalErrors = const [];
  String? _saveError;

  /// Bumped when values are replaced from outside the fields (discard,
  /// save, reset) so the numeric fields drop their typed text.
  int _generation = 0;

  bool get _dirty => !mapEquals(_draft, _info.completeParams);

  @override
  void initState() {
    super.initState();
    _adopt(widget.strategy);
  }

  void _adopt(StrategyInfo info) {
    _info = info;
    _draft = Map<String, Object>.of(info.completeParams);
    _clearErrors();
    _generation++;
  }

  void _clearErrors() {
    _fieldErrors = const {};
    _generalErrors = const [];
    _saveError = null;
  }

  void _edit(String name, Object value) {
    if (_draft[name] == value) return;
    _log('edit $name: ${_draft[name]} -> $value');
    setState(() => _draft = {..._draft, name: value});
  }

  void _discard() {
    _log('discard draft');
    setState(() => _adopt(_info));
  }

  Future<void> _save() => _put(Map<String, Object>.of(_draft), reset: false);

  Future<void> _confirmReset() async {
    final bool? ok = await ScrollableDialog.show<bool>(
      context: context,
      title: const Text(StrategyEditor.resetLabel),
      content: Text(
        'همه پارامترهای «${_info.titleFa}» به مقدار پیش‌فرض برمی‌گردند. '
        'اگر مقادیر فعلی با پیش‌فرض فرق داشته باشد، موتور یک نسخه پارامتر جدید می‌سازد '
        'و نسخه‌های قبلی حفظ می‌شوند.',
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text(StrategyEditor.cancelLabel),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text(StrategyEditor.resetLabel),
        ),
      ],
    );
    _log('reset confirmation: ${ok == true ? 'confirmed' : 'cancelled'}');
    if (ok != true || !mounted) return;
    await runGuardedAsyncAction(() => _put(const {}, reset: true));
  }

  Future<void> _put(Map<String, Object> params, {required bool reset}) async {
    _log('${reset ? 'reset' : 'save'} ${_info.name}: params=$params');
    try {
      final StrategyUpdateResult r = await widget.api.putStrategyParams(_info.name, params);
      _log('saved: params v${r.strategy.paramsVersion} (new=${r.createdNewVersion}) '
          'hash=${r.strategy.paramsHash}');
      if (!mounted) return;
      setState(() => _adopt(r.strategy));
      widget.onSaved(r.strategy);
      showSuccessToast(
        context,
        title: reset ? 'پارامترها به پیش‌فرض برگشتند' : 'پارامترها ذخیره شدند',
        subtitle: r.createdNewVersion
            ? 'نسخه پارامتر ${r.strategy.paramsVersion} ساخته و فعال شد.'
            : 'مقادیر با نسخه فعال یکی بود؛ نسخه ${r.strategy.paramsVersion} فعال ماند.',
      );
    } on EngineApiException catch (e) {
      _log('${reset ? 'reset' : 'save'} failed: $e');
      if (!mounted) return;
      setState(() {
        _clearErrors();
        if (e.isValidation) {
          final split = e.splitErrors({
            for (final ParamSpec s in _info.paramSchema) s.name: ['» (${s.name})'],
          });
          _fieldErrors = split.byField;
          _generalErrors = split.general;
          _saveError = e.messageFa;
        } else {
          _saveError = e.userMessage;
        }
      });
      showErrorToast(context, title: 'ذخیره انجام نشد', subtitle: e.messageFa);
    }
  }

  @override
  Widget build(BuildContext context) {
    final TextTheme text = Theme.of(context).textTheme;
    final bool busy = isAsyncActionRunning;

    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        Text(_info.titleFa, style: text.headlineSmall),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            _chip('شناسه: ${_info.name}'),
            _chip('نسخه کد: ${_info.version}'),
            if (widget.plugin != null)
              Tooltip(
                message: 'هش کامل فایل سیستم (sha256): ${widget.plugin!.sha256}',
                child: _chip('پلاگین — فایل ${shortSha(widget.plugin!.sha256)}',
                    key: const ValueKey<String>('editor-plugin-chip')),
              ),
            _chip('نسخه پارامترها: ${_info.paramsVersion}'),
            if (_info.paramsSavedUtc != null)
              _chip('ذخیره‌شده در: ${formatLocalDateTime(_info.paramsSavedUtc!)} (وقت محلی)'),
            Tooltip(
              message: 'هش پارامترها (sha256): ${_info.paramsHash}',
              child:
                  _chip('هش: ${_info.paramsHash.length > 12 ? _info.paramsHash.substring(0, 12) : _info.paramsHash}'),
            ),
          ],
        ),
        const SizedBox(height: 16),
        if (_info.paramsErrorsFa.isNotEmpty)
          ErrorBanner(
            title: 'پارامترهای ذخیره‌شده با شمای فعلی سازگار نیستند؛ مقادیر درست را ذخیره کنید.',
            messages: _info.paramsErrorsFa,
          ),
        if (_saveError != null) ErrorBanner(title: _saveError!, messages: _generalErrors),
        SectionCard(
          title: 'پارامترها',
          subtitle: 'فرم از شمای پارامترهای موتور ساخته شده است. ذخیره، کل مجموعه را جایگزین می‌کند.',
          child: Wrap(
            spacing: 24,
            runSpacing: 24,
            children: [
              for (final ParamSpec spec in _info.paramSchema)
                ParamField(
                  key: ValueKey<String>('param-${spec.name}-$_generation'),
                  spec: spec,
                  value: _draft[spec.name] ?? spec.defaultValue,
                  errors: _fieldErrors[spec.name] ?? const [],
                  enabled: !busy,
                  onChanged: (Object v) => _edit(spec.name, v),
                ),
            ],
          ),
        ),
        Row(
          children: [
            FilledButton.icon(
              onPressed: _dirty && !busy ? () => runGuardedAsyncAction(_save) : null,
              icon: busy
                  ? const SizedBox.square(dimension: 18, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.save_outlined),
              label: const Text(StrategyEditor.saveLabel),
            ),
            const SizedBox(width: 12),
            OutlinedButton.icon(
              onPressed: _dirty && !busy ? _discard : null,
              icon: const Icon(Icons.undo),
              label: const Text(StrategyEditor.discardLabel),
            ),
            const SizedBox(width: 12),
            TextButton.icon(
              onPressed: busy ? null : _confirmReset,
              icon: const Icon(Icons.restore),
              label: const Text(StrategyEditor.resetLabel),
            ),
            const SizedBox(width: 12),
            if (_dirty) Text('تغییرات ذخیره نشده', style: TextStyle(color: context.appColors.warning)),
          ],
        ),
      ],
    );
  }

  Widget _chip(String label, {Key? key}) => Chip(
        key: key,
        label: Text(label),
        visualDensity: VisualDensity.compact,
      );

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[SystemsPage] $message');
    AppLogger.log('[SystemsPage] $message');
  }
}
