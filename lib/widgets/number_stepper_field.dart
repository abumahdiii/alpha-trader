import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/dev_mode.dart';
import '../core/number_format.dart';
import '../theme/app_semantic_colors.dart';

/// Generic controlled numeric input: a decimal text field plus ▲/▼ step
/// buttons that increment/decrement by [step].
///
/// Suited to strategy parameters (periods, multipliers, risk %), where a
/// value is nudged step by step as often as it is typed.
///
/// Behaves like a normal controlled Flutter field: the caller owns [value]
/// and must apply [onChanged] (e.g. via `setState`/a `StatefulBuilder`'s
/// `dropState`) to actually update it — this widget never mutates [value]
/// itself, it only reports what the new value should be.
///
/// Manual typing is sanitized to digits + a single decimal point capped at
/// [decimalDigits] fractional digits. [min]/[max] are enforced two ways:
///   * every value this widget reports via [onChanged] — including live
///     keystrokes and step-button taps — is already clamped, so a caller can
///     never observe an out-of-range value even transiently;
///   * on top of that, the visible text is only reformatted/snapped to the
///     clamped value on blur or Enter/Done (not on every keystroke), so
///     typing "1.5" doesn't fight the user while they're still on "1.".
class NumberStepperField extends StatefulWidget {
  const NumberStepperField({
    super.key,
    required this.value,
    required this.onChanged,
    this.step = 1,
    this.min,
    this.max,
    this.decimalDigits = 1,
    this.label,
    this.suffixText,
    this.hintText,
    this.autofocus = false,
    this.maxLength = 6,
    this.onEditingComplete,
  });

  /// Current numeric value. The displayed text re-syncs to this whenever it
  /// changes from outside this widget's own edits (e.g. the dialog was
  /// reopened for a different record).
  final double value;

  /// Fired whenever the value changes (step tap, or a keystroke that parses
  /// to a valid number). Always already clamped to [min]/[max].
  final ValueChanged<double> onChanged;

  final double step;
  final double? min;
  final double? max;

  /// Fractional digits allowed when typing manually (and used to format the
  /// displayed value). `0` disallows a decimal point entirely.
  final int decimalDigits;

  final String? label;
  final String? suffixText;
  final String? hintText;
  final bool autofocus;
  final int maxLength;

  /// Fired after the user presses Enter/Done in the text field, AFTER
  /// [onChanged] has already been called with the final clamped value —
  /// mirrors `TextFormField.onEditingComplete`, for callers that need a
  /// side effect (e.g. a DB write) gated on an explicit "done" action.
  ///
  /// Deliberately NOT fired on simple focus loss/blur (blur only clamps and
  /// calls [onChanged]) — a caller's own button that also steals focus from
  /// this field (e.g. an "apply" button next to it) must not race this
  /// callback into performing its side effect twice, or into using a
  /// `BuildContext`/state that the blur handler already invalidated.
  final VoidCallback? onEditingComplete;

  @override
  State<NumberStepperField> createState() => _NumberStepperFieldState();
}

class _NumberStepperFieldState extends State<NumberStepperField> {
  late final TextEditingController _controller;
  late final FocusNode _focusNode;

  /// Last value this widget itself reported via [NumberStepperField.onChanged],
  /// so [didUpdateWidget] can tell "the parent echoed our own edit back"
  /// apart from "the parent changed the value out from under us" (e.g. a
  /// different record loaded into the same dialog) and only resync the text
  /// field's contents in the latter case.
  double? _lastNotifiedValue;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: _format(widget.value));
    _focusNode = FocusNode()..addListener(_handleFocusChange);
  }

  @override
  void didUpdateWidget(covariant NumberStepperField oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.value != oldWidget.value && widget.value != _lastNotifiedValue) {
      _controller.text = _format(widget.value);
    }
  }

  @override
  void dispose() {
    _focusNode.removeListener(_handleFocusChange);
    _focusNode.dispose();
    _controller.dispose();
    super.dispose();
  }

  int get _decimalDigits => widget.decimalDigits < 0 ? 0 : widget.decimalDigits;

  String _format(double value) {
    String text = value.toStringAsFixed(_decimalDigits);
    if (text.contains('.')) {
      text = text.replaceFirst(RegExp(r'0+$'), '');
      text = text.replaceFirst(RegExp(r'\.$'), '');
    }
    return text;
  }

  double _clamp(double value) {
    double result = value;
    if (widget.min != null && result < widget.min!) result = widget.min!;
    if (widget.max != null && result > widget.max!) result = widget.max!;
    return result;
  }

  void _emit(double value) {
    final double clamped = _clamp(value);
    _lastNotifiedValue = clamped;
    widget.onChanged(clamped);
  }

  void _handleFocusChange() {
    if (!_focusNode.hasFocus) {
      // Blur only normalizes the value/text — it must NOT call
      // `widget.onEditingComplete`, which a caller reserves for an explicit
      // "done" side effect (see the doc comment on that field).
      _commitText();
    }
  }

  void _commitText() {
    final double parsed = double.tryParse(_controller.text) ?? widget.value;
    final double clamped = _clamp(parsed);
    if (kDevMode) {
      devLog(
        'NumberStepperField: commit text="${_controller.text}" '
        'parsed=$parsed clamped=$clamped (min=${widget.min}, max=${widget.max})',
      );
    }
    _setControllerText(_format(clamped));
    _emit(clamped);
  }

  void _setControllerText(String text) {
    _controller.value = TextEditingValue(
      text: text,
      selection: TextSelection.collapsed(offset: text.length),
    );
  }

  void _step(double delta) {
    final double next = _clamp(widget.value + delta);
    if (kDevMode) {
      devLog('NumberStepperField: step delta=$delta from ${widget.value} -> $next');
    }
    _setControllerText(_format(next));
    _emit(next);
  }

  void _handleTextChanged(String raw) {
    final String normalized = numberEn(raw);
    final StringBuffer sanitized = StringBuffer();
    bool dotSeen = false;
    int decimalCount = 0;
    for (final String ch in normalized.split('')) {
      if (ch == '.') {
        if (dotSeen || _decimalDigits <= 0) continue;
        dotSeen = true;
        sanitized.write(ch);
      } else if (int.tryParse(ch) != null) {
        if (dotSeen) {
          if (decimalCount >= _decimalDigits) continue;
          decimalCount++;
        }
        sanitized.write(ch);
      }
    }
    final String value = sanitized.toString();
    if (value != _controller.text) {
      _setControllerText(value);
    }

    // Live-propagate as soon as it fully parses, so a caller gating e.g. an
    // "apply" button's enabled state on "has this changed" stays responsive
    // while typing — but leave the displayed text alone (don't reformat/clamp
    // it) until commit, so typing "1." while heading for "1.5" isn't fought.
    //
    // Exception: if the typed value actually falls outside [min]/[max] (not
    // just mid-typing an in-range value), snap the displayed text to the
    // clamped value right now instead of waiting for blur/Enter. Otherwise a
    // caller that reacts to `onChanged` by immediately closing/dismissing
    // (e.g. an "apply" button next to this field) can make the correction
    // look like it silently never happened, even though the value reported
    // via `onChanged` was already clamped. `_clamp` enforces both [min] and
    // [max], so this is symmetric for both without extra branching.
    final double? parsed = double.tryParse(value);
    if (parsed != null) {
      _emit(parsed);
      final double clamped = _clamp(parsed);
      if (clamped != parsed) {
        if (kDevMode) {
          devLog(
            'NumberStepperField: live-snap out-of-range typed value $parsed '
            '-> $clamped (min=${widget.min}, max=${widget.max})',
          );
        }
        _setControllerText(_format(clamped));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final colors = context.appColors;
    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        Expanded(
          child: TextFormField(
            controller: _controller,
            focusNode: _focusNode,
            autofocus: widget.autofocus,
            maxLength: widget.maxLength,
            inputFormatters: [
              FilteringTextInputFormatter.allow(RegExp('[0-9۰-۹.]')),
            ],
            textDirection: TextDirection.ltr,
            textAlign: TextAlign.center,
            style: const TextStyle(letterSpacing: 1.2),
            decoration: InputDecoration(
              contentPadding: const EdgeInsetsDirectional.symmetric(horizontal: 20),
              label: widget.label != null
                  ? Text(widget.label!, style: const TextStyle(letterSpacing: 0))
                  : null,
              hintText: widget.hintText,
              border: const OutlineInputBorder(
                borderRadius: BorderRadius.all(Radius.circular(20)),
              ),
              counterText: '',
              suffix: widget.suffixText != null
                  ? Text(widget.suffixText!, style: TextStyle(color: colors.mutedText))
                  : null,
              hintTextDirection: TextDirection.ltr,
              hintStyle: TextStyle(color: colors.mutedText),
            ),
            onChanged: _handleTextChanged,
            onEditingComplete: () {
              _commitText();
              widget.onEditingComplete?.call();
            },
          ),
        ),
        const SizedBox(width: 4),
        Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            IconButton(
              icon: const Icon(Icons.keyboard_arrow_up),
              iconSize: 20,
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(minWidth: 32, minHeight: 28),
              visualDensity: VisualDensity.compact,
              tooltip: 'افزایش',
              onPressed: (widget.max == null || widget.value < widget.max!)
                  ? () => _step(widget.step)
                  : null,
            ),
            IconButton(
              icon: const Icon(Icons.keyboard_arrow_down),
              iconSize: 20,
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(minWidth: 32, minHeight: 28),
              visualDensity: VisualDensity.compact,
              tooltip: 'کاهش',
              onPressed: (widget.min == null || widget.value > widget.min!)
                  ? () => _step(-widget.step)
                  : null,
            ),
          ],
        ),
      ],
    );
  }
}
