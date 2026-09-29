import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../models/account_settings.dart';
import '../../models/engine_health.dart';
import '../../providers/engine_status_provider.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/engine_gate.dart';
import '../../widgets/engine_status_indicator.dart';
import '../../widgets/loading_button.dart';
import '../../widgets/number_stepper_field.dart';
import '../../widgets/section_card.dart';
import '../../widgets/status_message.dart';
import 'live_signal_settings.dart';

/// «تنظیمات»: account/risk settings (engine `GET|PUT /settings`), live
/// signals (`/signals/settings` + local alert toggles) and the MT5
/// connection as reported by `/health` (display only).
class SettingsScreen extends StatelessWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Align(
      alignment: AlignmentDirectional.topStart,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 760),
        child: ListView(
          padding: const EdgeInsets.all(24),
          children: [
            SectionCard(
              title: 'حساب و ریسک',
              subtitle: 'موتور حجم پیشنهادی هر ستاپ را با این مقادیر محاسبه می‌کند.',
              child: EngineGate(builder: (context, api) => AccountSettingsForm(api: api)),
            ),
            const SectionCard(
              title: 'اتصال متاتریدر ۵',
              subtitle: 'فقط نمایش. موتور به ترمینال باز و لاگین‌شده وصل می‌شود و رمزی ذخیره نمی‌کند.',
              child: Mt5ConnectionBlock(),
            ),
            const LiveSignalSettingsSection(),
          ],
        ),
      ),
    );
  }
}

/// Editable account settings: load, edit (dirty tracking), save with the
/// engine's Persian validation messages per field, or discard.
class AccountSettingsForm extends StatefulWidget {
  const AccountSettingsForm({super.key, required this.api});

  final EngineApi api;

  static const String saveLabel = 'ذخیره';
  static const String discardLabel = 'بازگشت';
  static const String savedToast = 'تنظیمات حساب ذخیره شد';

  @override
  State<AccountSettingsForm> createState() => _AccountSettingsFormState();
}

class _AccountSettingsFormState extends State<AccountSettingsForm> {
  AccountSettings? _saved;
  AccountSettings? _draft;
  bool _loading = true;
  EngineApiException? _loadError;

  Map<String, List<String>> _fieldErrors = const {};
  List<String> _generalErrors = const [];
  String? _saveError;

  /// Bumped whenever values are replaced from outside the fields (load,
  /// discard, save): gives the fields fresh state so their text resyncs.
  int _generation = 0;

  bool get _dirty => _draft != null && _saved != null && _draft != _saved;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _loadError = null;
    });
    try {
      final AccountSettings s = await widget.api.getSettings();
      _log('loaded $s');
      if (!mounted) return;
      setState(() {
        _saved = s;
        _draft = s;
        _loading = false;
        _clearErrors();
        _generation++;
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

  Future<void> _save() async {
    final AccountSettings? saved = _saved;
    final AccountSettings? draft = _draft;
    if (saved == null || draft == null) return;
    final Map<String, Object> changes = draft.changesFrom(saved);
    if (changes.isEmpty) return;
    _log('saving changes $changes');
    try {
      final AccountSettings result = await widget.api.putSettings(changes);
      _log('saved -> $result');
      if (!mounted) return;
      setState(() {
        _saved = result;
        _draft = result;
        _clearErrors();
        _generation++;
      });
      showSuccessToast(context, title: AccountSettingsForm.savedToast);
    } on EngineApiException catch (e) {
      _log('save failed: $e');
      if (!mounted) return;
      setState(() {
        _clearErrors();
        if (e.isValidation) {
          final split = e.splitErrors({
            for (final MapEntry<String, String> l in AccountSettings.engineLabelsFa.entries) l.key: ['«${l.value}»'],
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

  void _discard() {
    _log('discard: $_draft -> $_saved');
    setState(() {
      _draft = _saved;
      _clearErrors();
      _generation++;
    });
  }

  void _clearErrors() {
    _fieldErrors = const {};
    _generalErrors = const [];
    _saveError = null;
  }

  void _edit(AccountSettings next) {
    if (next == _draft) return;
    setState(() => _draft = next);
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Padding(
        padding: EdgeInsets.all(24),
        child: Center(child: CircularProgressIndicator()),
      );
    }
    final EngineApiException? loadError = _loadError;
    if (loadError != null) {
      return StatusMessage.apiError(title: 'خواندن تنظیمات ناموفق بود', error: loadError, onRetry: _load);
    }
    final AccountSettings draft = _draft!;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (_saveError != null) ErrorBanner(title: _saveError!, messages: _generalErrors),
        Wrap(
          spacing: 24,
          runSpacing: 20,
          children: [
            _field(
              key: AccountSettings.balanceKey,
              label: 'موجودی حساب (دلار)',
              value: draft.balance,
              step: 100,
              decimals: 2,
              suffix: 'USD',
              onChanged: (v) => _edit(draft.copyWith(balance: v)),
            ),
            _field(
              key: AccountSettings.riskPctKey,
              label: 'درصد ریسک هر معامله',
              value: draft.riskPct,
              step: 0.1,
              decimals: 2,
              suffix: '%',
              onChanged: (v) => _edit(draft.copyWith(riskPct: v)),
            ),
            _field(
              key: AccountSettings.rrKey,
              label: 'نسبت ریسک به ریوارد (R:R)',
              value: draft.rr,
              step: 0.1,
              decimals: 2,
              onChanged: (v) => _edit(draft.copyWith(rr: v)),
            ),
            _field(
              key: AccountSettings.leverageKey,
              label: 'اهرم (1:N)',
              value: draft.leverage.toDouble(),
              step: 1,
              decimals: 0,
              onChanged: (v) => _edit(draft.copyWith(leverage: v.round())),
            ),
          ],
        ),
        const SizedBox(height: 24),
        Row(
          children: [
            LoadingActionButton(
              onPressed: _save,
              builder: (context, onPressed, isLoading) => FilledButton.icon(
                onPressed: _dirty ? onPressed : null,
                icon: isLoading
                    ? const SizedBox.square(dimension: 18, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.save_outlined),
                label: const Text(AccountSettingsForm.saveLabel),
              ),
            ),
            const SizedBox(width: 12),
            OutlinedButton.icon(
              onPressed: _dirty ? _discard : null,
              icon: const Icon(Icons.undo),
              label: const Text(AccountSettingsForm.discardLabel),
            ),
            const SizedBox(width: 12),
            if (_dirty) Text('تغییرات ذخیره نشده', style: TextStyle(color: context.appColors.warning)),
          ],
        ),
      ],
    );
  }

  Widget _field({
    required String key,
    required String label,
    required double value,
    required double step,
    required int decimals,
    required ValueChanged<double> onChanged,
    String? suffix,
  }) {
    return SizedBox(
      width: 300,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          NumberStepperField(
            key: ValueKey<String>('settings-$key-$_generation'),
            value: value,
            // Only "not negative" here; the real bounds are the engine's, and
            // its Persian 422 messages are shown under the field.
            min: 0,
            step: step,
            decimalDigits: decimals,
            maxLength: 14,
            label: label,
            suffixText: suffix,
            onChanged: onChanged,
          ),
          FieldErrors(_fieldErrors[key] ?? const []),
        ],
      ),
    );
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[SettingsPage] $message');
    AppLogger.log('[SettingsPage] $message');
  }
}

/// MT5 connection details from `/health` (via [EngineStatusProvider]).
/// Display only; the login is shown masked.
class Mt5ConnectionBlock extends StatelessWidget {
  const Mt5ConnectionBlock({super.key});

  @override
  Widget build(BuildContext context) {
    final EngineStatusProvider engine = context.watch<EngineStatusProvider>();
    if (engine.state != EngineState.running) {
      return const Text(EngineStatusIndicator.mt5UnknownWhileEngineDown);
    }
    final Mt5Status mt5 = engine.mt5;
    final (String label, Color color) = EngineStatusIndicator.mt5Appearance(mt5.state, context.appColors);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _InfoRow(
          label: 'وضعیت',
          value: Row(
            children: [
              Container(
                width: 10,
                height: 10,
                decoration: BoxDecoration(color: color, shape: BoxShape.circle),
              ),
              const SizedBox(width: 6),
              Text(label),
            ],
          ),
        ),
        _InfoRow(label: 'توضیح', value: Text(EngineStatusIndicator.mt5Description(mt5.state))),
        _InfoRow(label: 'سرور', value: _ltr(mt5.server)),
        _InfoRow(label: 'حساب', value: _ltr(maskedLogin(mt5.loginMasked))),
        _InfoRow(label: 'نوع حساب', value: _ltr(mt5.tradeMode)),
        if (mt5.message != null) _InfoRow(label: 'پیام', value: Text(mt5.message!)),
      ],
    );
  }

  /// The engine already masks the login (`****1234`); anything that does
  /// not look masked is masked again here, as a second line of defense.
  static String? maskedLogin(String? login) {
    if (login == null || login.isEmpty) return null;
    if (login.startsWith('****')) return login;
    final String tail = login.length <= 4 ? '' : login.substring(login.length - 4);
    return '****$tail';
  }

  static Widget _ltr(String? value) => Align(
        alignment: AlignmentDirectional.centerStart,
        child: Text(value ?? '—', textDirection: TextDirection.ltr),
      );
}

class _InfoRow extends StatelessWidget {
  const _InfoRow({required this.label, required this.value});

  final String label;
  final Widget value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 110,
            child: Text(label, style: TextStyle(color: context.appColors.mutedText)),
          ),
          Expanded(child: value),
        ],
      ),
    );
  }
}
