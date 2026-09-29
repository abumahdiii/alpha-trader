import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../core/number_format.dart';
import '../../models/signal_models.dart';
import '../../models/strategy.dart';
import '../../providers/signals_provider.dart';
import '../../services/engine_api.dart';
import '../../services/signal_alerts.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/engine_gate.dart';
import '../../widgets/section_card.dart';
import '../../widgets/status_message.dart';
import '../../widgets/strategy_selector.dart';

/// «سیگنال لایو» section of the settings page: the engine's live settings
/// (`GET|POST /signals/settings`: on/off, live strategy, grace seconds) and
/// the local alert toggles (sound, Windows notification).
class LiveSignalSettingsSection extends StatelessWidget {
  const LiveSignalSettingsSection({super.key});

  static const String title = 'سیگنال لایو';

  @override
  Widget build(BuildContext context) {
    return SectionCard(
      title: title,
      subtitle: 'موتور بعد از بسته شدن هر کندل H1 نمادها را بررسی می‌کند و فقط پیشنهاد می‌دهد؛ هیچ سفارشی ارسال نمی‌شود.',
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        EngineGate(builder: (BuildContext context, EngineApi api) => LiveSignalSettingsForm(api: api)),
        const Divider(height: 32),
        const SignalAlertToggles(),
      ]),
    );
  }
}

/// The engine part: every change is posted at once (partial body).
class LiveSignalSettingsForm extends StatefulWidget {
  const LiveSignalSettingsForm({super.key, required this.api});

  final EngineApi api;

  static const String enabledLabel = 'سیگنال لایو روشن باشد';
  static const String strategyLabel = 'سیستم لایو';
  static const String graceLabel = 'مهلت پس از بسته شدن کندل (ثانیه)';
  static const String graceSaveLabel = 'ذخیره مهلت';
  static const String savedToast = 'تنظیمات سیگنال لایو ذخیره شد';
  static const String failedToast = 'تنظیمات سیگنال لایو ذخیره نشد';
  static const String pluginsNoteFa = 'سیستم‌های بارگذاری‌شده (پلاگین) فعلا برای سیگنال لایو قابل انتخاب نیستند.';

  @override
  State<LiveSignalSettingsForm> createState() => _LiveSignalSettingsFormState();
}

class _LiveSignalSettingsFormState extends State<LiveSignalSettingsForm> {
  LiveSignalSettings? _settings;
  List<StrategyOption> _options = const [];
  bool _loading = true;
  bool _busy = false;
  EngineApiException? _loadError;

  final TextEditingController _grace = TextEditingController();
  String? _graceError;
  String? _errorTitle;
  List<String> _errorLines = const [];

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void dispose() {
    _grace.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    if (!_loading) {
      setState(() {
        _loading = true;
        _loadError = null;
      });
    }
    try {
      final Future<List<StrategyOption>> options = widget.api.listStrategyOptions().catchError((Object e) {
        _log('strategy options unavailable, default only: $e');
        return const <StrategyOption>[StrategyOption.fallback];
      });
      final LiveSignalSettings s = await widget.api.getSignalSettings();
      final List<StrategyOption> o = await options;
      _log('loaded $s; strategies ${o.map((StrategyOption x) => '${x.name}(${x.source.code})').join(', ')}');
      if (!mounted) return;
      setState(() {
        _settings = s;
        _options = o;
        _loading = false;
        _grace.text = _graceText(s.graceS);
      });
    } on EngineApiException catch (e) {
      _log('load failed: $e');
      if (!mounted) return;
      setState(() {
        _loading = false;
        _loadError = e;
      });
    }
  }

  static String _graceText(double? v) => v == null ? '' : (v == v.roundToDouble() ? '${v.round()}' : '$v');

  /// Posts one change; the shown values change only when the engine accepted it.
  Future<void> _post({bool? enabled, String? liveStrategy, double? graceS}) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _errorTitle = null;
      _errorLines = const [];
      _graceError = null;
    });
    _log('POST enabled=$enabled strategy=$liveStrategy grace=$graceS');
    try {
      final LiveSignalSettingsResult r =
          await widget.api.postSignalSettings(enabled: enabled, liveStrategy: liveStrategy, graceS: graceS);
      _log('saved -> ${r.settings} status=${r.status}');
      if (!mounted) return;
      final LiveSignalsStatus? status = r.status;
      if (status != null) context.read<SignalsProvider>().applyStatus(status);
      setState(() {
        _settings = r.settings;
        _grace.text = _graceText(r.settings.graceS);
      });
      showSuccessToast(context, title: LiveSignalSettingsForm.savedToast);
    } on EngineApiException catch (e) {
      _log('save failed: $e');
      if (!mounted) return;
      setState(() {
        if (e.isValidation) {
          final split = e.splitErrors({
            'grace': ['«مهلت'],
          });
          _graceError = split.byField['grace']?.join('\n');
          _errorTitle = e.messageFa;
          _errorLines = split.general;
        } else {
          _errorTitle = e.messageFa;
          _errorLines = [...e.errorsFa, if (e.errorsFa.isEmpty && e.detail != null) e.detail!];
        }
      });
      showErrorToast(context, title: LiveSignalSettingsForm.failedToast, subtitle: e.messageFa);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _saveGrace() {
    final double? v = double.tryParse(numberEn(_grace.text.trim()));
    final String? problem = LiveSignalSettings.validateGraceFa(v);
    if (problem != null) {
      _log('grace "${_grace.text}" refused locally: $problem');
      setState(() => _graceError = problem);
      return;
    }
    unawaited(_post(graceS: v));
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Padding(padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator()));
    }
    final EngineApiException? loadError = _loadError;
    if (loadError != null) {
      return StatusMessage.apiError(title: 'خواندن تنظیمات سیگنال لایو ناموفق بود', error: loadError, onRetry: _load);
    }
    final LiveSignalSettings s = _settings!;
    final List<StrategyOption> builtin = _options.where((StrategyOption o) => !o.isPlugin).toList();
    final bool hasPlugins = _options.any((StrategyOption o) => o.isPlugin);
    final AppSemanticColors c = context.appColors;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (_errorTitle != null) ErrorBanner(title: _errorTitle!, messages: _errorLines),
      SwitchListTile(
        key: const ValueKey<String>('live-enabled-switch'),
        contentPadding: EdgeInsets.zero,
        title: const Text(LiveSignalSettingsForm.enabledLabel),
        subtitle: Text(s.enabled
            ? 'روشن: بعد از هر کندل H1 بررسی انجام می‌شود.'
            : 'خاموش: هیچ بررسی و پیشنهادی انجام نمی‌شود (پیش‌فرض).'),
        value: s.enabled,
        onChanged: _busy ? null : (bool v) => unawaited(_post(enabled: v)),
      ),
      const SizedBox(height: 8),
      Row(children: [
        const SizedBox(width: 160, child: Text(LiveSignalSettingsForm.strategyLabel)),
        Expanded(
          child: StrategySelector(
            key: const ValueKey<String>('live-strategy-selector'),
            options: builtin,
            value: s.liveStrategy ?? kDefaultStrategyName,
            isExpanded: true,
            enabled: !_busy,
            onChanged: (String v) => unawaited(_post(liveStrategy: v)),
          ),
        ),
      ]),
      if (hasPlugins)
        Padding(
          padding: const EdgeInsetsDirectional.only(start: 160, top: 4),
          child: Text(LiveSignalSettingsForm.pluginsNoteFa, style: TextStyle(fontSize: 12, color: c.mutedText)),
        ),
      const SizedBox(height: 16),
      Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SizedBox(
          width: 300,
          child: TextField(
            key: const ValueKey<String>('live-grace-field'),
            controller: _grace,
            enabled: !_busy,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9۰-۹.]'))],
            decoration: InputDecoration(
              labelText: LiveSignalSettingsForm.graceLabel,
              helperText: 'بین ${LiveSignalSettings.graceMin.toInt()} و ${LiveSignalSettings.graceMax.toInt()} ثانیه',
              errorText: _graceError,
              errorMaxLines: 3,
              border: const OutlineInputBorder(),
            ),
            onSubmitted: (_) => _saveGrace(),
          ),
        ),
        const SizedBox(width: 12),
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: OutlinedButton.icon(
            key: const ValueKey<String>('live-grace-save'),
            onPressed: _busy ? null : _saveGrace,
            icon: _busy
                ? const SizedBox.square(dimension: 16, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.save_outlined),
            label: const Text(LiveSignalSettingsForm.graceSaveLabel),
          ),
        ),
      ]),
    ]);
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[LiveSignalSettings] $message');
    AppLogger.log('[LiveSignalSettings] $message');
  }
}

/// The local alert toggles (this PC only, default on).
class SignalAlertToggles extends StatelessWidget {
  const SignalAlertToggles({super.key});

  static const String soundLabel = 'صدا';
  static const String notificationLabel = 'اعلان ویندوز';

  @override
  Widget build(BuildContext context) {
    final SignalAlertPrefs prefs = context.watch<SignalAlertPrefs>();
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Text('هشدار سیگنال جدید (فقط روی همین کامپیوتر)', style: Theme.of(context).textTheme.titleSmall),
      SwitchListTile(
        key: const ValueKey<String>('alert-sound-switch'),
        contentPadding: EdgeInsets.zero,
        title: const Text(soundLabel),
        subtitle: const Text('پخش صدای اعلان ویندوز برای هر سیگنال جدید'),
        value: prefs.sound,
        onChanged: (bool v) => unawaited(prefs.setSound(v)),
      ),
      SwitchListTile(
        key: const ValueKey<String>('alert-notification-switch'),
        contentPadding: EdgeInsets.zero,
        title: const Text(notificationLabel),
        subtitle: const Text('نمایش اعلان ویندوز با نماد، جهت، ورود و حد ضرر'),
        value: prefs.notification,
        onChanged: (bool v) => unawaited(prefs.setNotification(v)),
      ),
    ]);
  }
}
