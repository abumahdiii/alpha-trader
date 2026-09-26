import 'package:flutter/material.dart';

/// Semantic color tokens that Material's [ColorScheme] does not provide
/// out of the box (success / warning), plus convenience aliases for
/// existing [ColorScheme] tokens (error / border / muted-text / muted-surface)
/// so that every "hardcoded `Colors.*`" pattern found across the app's
/// screens has a single, theme-aware, dark-mode-safe replacement.
///
/// Registered on both [ThemeData] instances built in `lib/theme/theme.dart`
/// via `ThemeData(extensions: [AppSemanticColors(...)])`.
///
/// Usage from any widget:
/// ```dart
/// final colors = context.appColors;
/// Icon(Icons.check, color: colors.success);
/// Container(color: colors.surfaceMuted);
/// ```
///
/// All color pairs below were chosen/verified to meet WCAG contrast
/// minimums (>= 4.5:1 for text, >= 3:1 for icons/graphical elements)
/// against the `surface`/`scaffoldBackgroundColor` they are meant to sit on.
@immutable
class AppSemanticColors extends ThemeExtension<AppSemanticColors> {
  const AppSemanticColors({
    required this.success,
    required this.onSuccess,
    required this.successContainer,
    required this.onSuccessContainer,
    required this.warning,
    required this.onWarning,
    required this.warningContainer,
    required this.onWarningContainer,
    required this.info,
    required this.error,
    required this.onError,
    required this.borderColor,
    required this.mutedText,
    required this.surfaceMuted,
    required this.primaryFill,
    required this.onPrimaryFill,
  });

  /// Green "success" foreground/icon color, tuned to be readable directly
  /// on top of [ColorScheme.surface] (both light and dark variants).
  /// Replaces bare `Colors.green` used as a `color:`/icon tint.
  final Color success;

  /// Text/icon color to place on top of a surface filled with [success]
  /// or [successContainer] (e.g. a SnackBar/toast/button background).
  final Color onSuccess;

  /// Green background fill suitable for SnackBars/badges/buttons that need
  /// [onSuccessContainer] text on top at >= 4.5:1 contrast.
  /// Replaces `backgroundColor: Colors.green`.
  final Color successContainer;
  final Color onSuccessContainer;

  /// Amber/orange "warning" foreground/icon color, readable directly on
  /// [ColorScheme.surface]. Replaces bare `Colors.orange`/`Colors.amber`
  /// used as a `color:`/icon tint.
  final Color warning;

  /// Text/icon color to place on top of a surface filled with [warning]
  /// or [warningContainer].
  final Color onWarning;

  /// Amber/orange background fill suitable for buttons/badges that need
  /// [onWarningContainer] text on top. Replaces
  /// `backgroundColor: Colors.amber.shade700` / `Colors.orange`.
  final Color warningContainer;
  final Color onWarningContainer;

  /// Blue "info" foreground/icon color, readable directly on
  /// [ColorScheme.surface]. Use instead of a bare `Colors.blue`.
  final Color info;

  /// Alias of `ColorScheme.error` — exposed here so screens have a single
  /// `context.appColors.*` entry point for success/warning/error instead of
  /// mixing `context.appColors` and `Theme.of(context).colorScheme`.
  /// Replaces bare `Colors.red`.
  final Color error;

  /// Alias of `ColorScheme.onError`.
  final Color onError;

  /// Alias of `ColorScheme.outline`. Replaces `Colors.grey`/`Colors.grey.shade300`
  /// etc. used for borders, dividers, and unselected outlines.
  final Color borderColor;

  /// Alias of `ColorScheme.onSurfaceVariant`. Replaces `Colors.grey` used for
  /// secondary/hint/disabled text and icons.
  final Color mutedText;

  /// Alias of `ColorScheme.surfaceContainerHighest`. Replaces
  /// `Colors.grey.shade100`/`Colors.grey.shade200` panel/chip backgrounds.
  final Color surfaceMuted;

  /// Background fill for primary-colored containers/buttons/chips that need
  /// the purple seed color as a fill (e.g. FloatingActionButton, CircleAvatar,
  /// column header rows). In dark mode this is the dark-seed purple (#8026DE)
  /// and in light mode it equals `ColorScheme.primary`.
  /// Use this, not `colorScheme.primary`, as a fill/backgroundColor.
  final Color primaryFill;

  /// Text/icon color to place on top of a [primaryFill] background.
  /// Always white in both modes for the standard purple fill.
  final Color onPrimaryFill;

  @override
  AppSemanticColors copyWith({
    Color? success,
    Color? onSuccess,
    Color? successContainer,
    Color? onSuccessContainer,
    Color? warning,
    Color? onWarning,
    Color? warningContainer,
    Color? onWarningContainer,
    Color? info,
    Color? error,
    Color? onError,
    Color? borderColor,
    Color? mutedText,
    Color? surfaceMuted,
    Color? primaryFill,
    Color? onPrimaryFill,
  }) {
    return AppSemanticColors(
      success: success ?? this.success,
      onSuccess: onSuccess ?? this.onSuccess,
      successContainer: successContainer ?? this.successContainer,
      onSuccessContainer: onSuccessContainer ?? this.onSuccessContainer,
      warning: warning ?? this.warning,
      onWarning: onWarning ?? this.onWarning,
      warningContainer: warningContainer ?? this.warningContainer,
      onWarningContainer: onWarningContainer ?? this.onWarningContainer,
      info: info ?? this.info,
      error: error ?? this.error,
      onError: onError ?? this.onError,
      borderColor: borderColor ?? this.borderColor,
      mutedText: mutedText ?? this.mutedText,
      surfaceMuted: surfaceMuted ?? this.surfaceMuted,
      primaryFill: primaryFill ?? this.primaryFill,
      onPrimaryFill: onPrimaryFill ?? this.onPrimaryFill,
    );
  }

  @override
  AppSemanticColors lerp(ThemeExtension<AppSemanticColors>? other, double t) {
    if (other is! AppSemanticColors) return this;
    return AppSemanticColors(
      success: Color.lerp(success, other.success, t)!,
      onSuccess: Color.lerp(onSuccess, other.onSuccess, t)!,
      successContainer: Color.lerp(successContainer, other.successContainer, t)!,
      onSuccessContainer: Color.lerp(onSuccessContainer, other.onSuccessContainer, t)!,
      warning: Color.lerp(warning, other.warning, t)!,
      onWarning: Color.lerp(onWarning, other.onWarning, t)!,
      warningContainer: Color.lerp(warningContainer, other.warningContainer, t)!,
      onWarningContainer: Color.lerp(onWarningContainer, other.onWarningContainer, t)!,
      info: Color.lerp(info, other.info, t)!,
      error: Color.lerp(error, other.error, t)!,
      onError: Color.lerp(onError, other.onError, t)!,
      borderColor: Color.lerp(borderColor, other.borderColor, t)!,
      mutedText: Color.lerp(mutedText, other.mutedText, t)!,
      surfaceMuted: Color.lerp(surfaceMuted, other.surfaceMuted, t)!,
      primaryFill: Color.lerp(primaryFill, other.primaryFill, t)!,
      onPrimaryFill: Color.lerp(onPrimaryFill, other.onPrimaryFill, t)!,
    );
  }
}

/// Convenience accessor: `context.appColors.success` instead of
/// `Theme.of(context).extension<AppSemanticColors>()!`.
extension AppSemanticColorsX on BuildContext {
  AppSemanticColors get appColors {
    final ext = Theme.of(this).extension<AppSemanticColors>();
    assert(
      ext != null,
      'AppSemanticColors extension not found on ThemeData. '
      'Make sure MyThemes.getLightTheme/getDarkTheme registered it via `extensions: [...]`.',
    );
    return ext!;
  }
}
