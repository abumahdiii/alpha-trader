import 'package:flutter/material.dart';

import '../../models/strategy.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/number_stepper_field.dart';
import '../../widgets/section_card.dart';

/// Persian labels for choice values whose raw value means nothing to a
/// user. Anything not listed is shown as its raw value.
const Map<String, Map<String, String>> kChoiceLabelsFa = {
  'sigma_ddof': {
    '0': 'σ: ddof=0 (جمعیت)',
    '1': 'σ: ddof=1 (نمونه)',
  },
  'projection_mode': {
    'bar_count': 'bar_count (شمارش کندل)',
    'calendar': 'calendar (زمان تقویمی)',
  },
};

String choiceLabelFa(String param, Object choice) => kChoiceLabelsFa[param]?['$choice'] ?? '$choice';

/// Editor for one strategy parameter, chosen by the schema entry's type:
/// int/float -> [NumberStepperField] bounded by `min`/`max` and stepped by
/// `step`; bool -> switch; choice -> segmented buttons; an unknown type is
/// shown read-only (its value is sent back unchanged).
///
/// Controlled: [value] comes from the parent's draft and every edit goes
/// out through [onChanged] as a JSON scalar of the declared type.
class ParamField extends StatelessWidget {
  const ParamField({
    super.key,
    required this.spec,
    required this.value,
    required this.onChanged,
    this.errors = const [],
    this.enabled = true,
  });

  final ParamSpec spec;
  final Object value;
  final ValueChanged<Object> onChanged;

  /// The engine's Persian 422 messages about this parameter.
  final List<String> errors;
  final bool enabled;

  static const double width = 340;

  @override
  Widget build(BuildContext context) {
    final TextTheme text = Theme.of(context).textTheme;
    final Color muted = context.appColors.mutedText;
    final bool isDefault = value == spec.defaultValue;

    final List<String> hints = [
      if (spec.descriptionFa.isNotEmpty) spec.descriptionFa,
      [
        if (spec.kind.isNumeric && (spec.min != null || spec.max != null))
          'بازه: ${_fmt(spec.min) ?? '—'} تا ${_fmt(spec.max) ?? '—'}',
        'پیش‌فرض: ${_display(spec.defaultValue)}${isDefault ? ' (فعلی)' : ''}',
      ].join(' · '),
    ];

    return SizedBox(
      width: width,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _editor(context),
          Padding(
            padding: const EdgeInsetsDirectional.only(top: 6, start: 12),
            child: Text(
              hints.join('\n'),
              style: text.bodySmall?.copyWith(color: muted),
            ),
          ),
          FieldErrors(errors),
        ],
      ),
    );
  }

  Widget _editor(BuildContext context) {
    switch (spec.kind) {
      case ParamKind.integer:
      case ParamKind.float:
        final bool isInt = spec.kind == ParamKind.integer;
        return AbsorbPointer(
          absorbing: !enabled,
          child: NumberStepperField(
            value: (value is num) ? (value as num).toDouble() : 0,
            min: spec.min,
            max: spec.max,
            step: spec.step ?? (isInt ? 1 : 0.1),
            decimalDigits: isInt ? 0 : _decimalsFor(),
            maxLength: 12,
            label: spec.displayLabel,
            onChanged: (double v) => onChanged(isInt ? v.round() : v),
          ),
        );
      case ParamKind.boolean:
        return InputDecorator(
          decoration: _decoration(),
          child: Row(
            children: [
              Expanded(child: Text(value == true ? 'فعال' : 'غیرفعال')),
              Switch(value: value == true, onChanged: enabled ? (bool v) => onChanged(v) : null),
            ],
          ),
        );
      case ParamKind.choice:
        final List<Object> choices = spec.choices ?? const [];
        final int selected = choices.indexWhere((Object c) => c == value);
        return InputDecorator(
          decoration: _decoration(),
          child: SegmentedButton<int>(
            showSelectedIcon: false,
            emptySelectionAllowed: selected < 0,
            segments: [
              for (int i = 0; i < choices.length; i++)
                ButtonSegment<int>(value: i, label: Text(choiceLabelFa(spec.name, choices[i]))),
            ],
            selected: {if (selected >= 0) selected},
            onSelectionChanged: enabled
                ? (Set<int> s) {
                    if (s.isNotEmpty) onChanged(choices[s.first]);
                  }
                : null,
          ),
        );
      case ParamKind.unknown:
        return InputDecorator(
          decoration: _decoration(),
          child: Text(
            '${_display(value)}  (نوع «${spec.type}» در این نسخه برنامه قابل ویرایش نیست)',
          ),
        );
    }
  }

  InputDecoration _decoration() => InputDecoration(
        labelText: spec.displayLabel,
        border: const OutlineInputBorder(borderRadius: BorderRadius.all(Radius.circular(20))),
        contentPadding: const EdgeInsetsDirectional.symmetric(horizontal: 16, vertical: 8),
      );

  /// Enough fractional digits for the step, the default and the current
  /// value, so a blur never rounds a value the engine sent.
  int _decimalsFor() {
    int digits(Object? v) {
      if (v is! num) return 0;
      final String s = v.toString();
      final int dot = s.indexOf('.');
      if (s.contains('e') || dot < 0) return 0;
      return s.length - dot - 1;
    }

    final int d =
        [digits(spec.step), digits(spec.defaultValue), digits(value), 1].reduce((a, b) => a > b ? a : b);
    return d > 6 ? 6 : d;
  }

  String _display(Object v) => spec.kind == ParamKind.choice
      ? choiceLabelFa(spec.name, v)
      : spec.kind == ParamKind.boolean
          ? (v == true ? 'فعال' : 'غیرفعال')
          : '$v';

  static String? _fmt(double? v) {
    if (v == null) return null;
    return v == v.truncateToDouble() ? v.toInt().toString() : v.toString();
  }
}
