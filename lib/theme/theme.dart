import 'package:flutter/material.dart';

import 'app_semantic_colors.dart';

class MyThemes {
  static ThemeData getDarkTheme([Color? customPrimary]) {
    // In dark mode, use a light lavender as `primary` so that text/icons
    // coloured with `colorScheme.primary` are readable on the dark surface
    // (#1E1B26 / #2B2735).  Contrast of #CF9FFF on #1E1B26 ≈ 8.0:1 (WCAG AA+).
    // The raw seed colour is preserved in `inversePrimary` so custom-primary
    // callers still get a meaningful tonal reference.
    final seedPrimary = customPrimary ?? const Color(0xFF8026DE);
    // Light lavender for foreground/text/icon use (high contrast on dark bg).
    final primary = customPrimary != null
        ? _lightenForDark(customPrimary)
        : const Color(0xFFCF9FFF);
    final colorScheme = ColorScheme(
      brightness: Brightness.dark,
      primary: primary,
      onPrimary: const Color(0xFF29004E), // dark text on lavender button bg
      primaryContainer: seedPrimary.withValues(alpha: 0.35), // mid-purple fill
      onPrimaryContainer: Colors.white,
      // `secondary` stays paired with the light lavender + dark-navy
      // combination: a FAB without an explicit `foregroundColor` fills itself
      // with `primary`, and flipping this pair would make its label
      // white-on-lavender.
      secondary: primary.withValues(alpha: 0.85),
      onSecondary: const Color(0xFF29004E),
      secondaryContainer: seedPrimary.withValues(alpha: 0.25),
      onSecondaryContainer: Colors.white,
      tertiary: const Color(0xFFF2AFFF),
      onTertiary: const Color(0xFF4E155E),
      tertiaryContainer: const Color(0xFF672F77),
      onTertiaryContainer: const Color(0xFFFCD7FF),
      error: const Color(0xFFFFB4AB),
      errorContainer: const Color(0xFF93000A),
      onError: const Color(0xFF690005),
      onErrorContainer: const Color(0xFFFFDAD6),
      surface: const Color(0xFF2B2735),
      onSurface: Colors.white,
      surfaceContainerHighest: const Color(0xFF49454E),
      onSurfaceVariant: const Color(0xFFCAC4CF),
      outline: const Color(0xFF948F99),
      onInverseSurface: const Color(0xFF3C002C),
      inverseSurface: const Color(0xFFFFD8EB),
      inversePrimary: seedPrimary, // original seed for tonal references
      shadow: const Color(0xFF000000),
      surfaceTint: seedPrimary,
      outlineVariant: const Color(0xFF49454E),
      scrim: const Color(0xFF000000),
    );
    return ThemeData(
      useMaterial3: false,
      fontFamily: 'Mikhak',
      textTheme: const TextTheme(
        displayLarge: TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
        displayMedium: TextStyle(color: Colors.white),
        displaySmall: TextStyle(color: Colors.white),
        headlineMedium: TextStyle(color: Colors.white),
        headlineSmall: TextStyle(color: Colors.white),
        titleLarge: TextStyle(color: Colors.white),
        titleMedium: TextStyle(color: Colors.white),
        titleSmall: TextStyle(color: Colors.white),
        bodyLarge: TextStyle(color: Colors.white),
        bodyMedium: TextStyle(color: Colors.white),
        bodySmall: TextStyle(color: Colors.white),
        labelLarge: TextStyle(color: Colors.white),
        labelSmall: TextStyle(color: Colors.white),
      ),
      scaffoldBackgroundColor: const Color(0xFF1E1B26),
      iconTheme: const IconThemeData(color: Colors.white),
      colorScheme: colorScheme,
      extensions: [
        AppSemanticColors(
          // Dark-mode success: a light green readable directly on `surface`
          // (contrast 8.58:1) — see report table for full WCAG figures.
          success: const Color(0xFF6FDD8B),
          onSuccess: Colors.white,
          // Darker green fill so `onSuccessContainer` (white) reaches >=4.5:1.
          successContainer: const Color(0xFF2F7A45),
          onSuccessContainer: Colors.white,
          warning: const Color(0xFFFFC468),
          onWarning: Colors.white,
          warningContainer: const Color(0xFF8A5300),
          onWarningContainer: Colors.white,
          // Light blue on the dark surface (#1E1B26): about 7.7:1.
          info: const Color(0xFF82B1FF),
          // Aliases of already-vetted ColorScheme tokens (no new literals,
          // no drift risk).
          error: colorScheme.error,
          onError: colorScheme.onError,
          borderColor: colorScheme.outline,
          mutedText: colorScheme.onSurfaceVariant,
          surfaceMuted: colorScheme.surfaceContainerHighest,
          // In dark mode, fills (FAB, CircleAvatar, header rows) use the
          // dark seed purple so they remain visually purple on dark surfaces.
          // `primary` is now the light lavender foreground/text token.
          primaryFill: seedPrimary,
          onPrimaryFill: _onFill(seedPrimary),
        ),
      ],
    );
  }

  static ThemeData getLightTheme([Color? customPrimary]) {
    final primary = customPrimary ?? const Color(0xFF8026DE);
    final colorScheme = ColorScheme(
      brightness: Brightness.light,
      primary: primary,
      onPrimary: Colors.white,
      primaryContainer: primary.withValues(alpha: 0.15),
      onPrimaryContainer: Colors.black,
      secondary: const Color(0xFF655A6F),
      onSecondary: Colors.white,
      secondaryContainer: primary.withValues(alpha: 0.1),
      onSecondaryContainer: const Color(0xFF20182A),
      tertiary: const Color(0xFF805159),
      onTertiary: Colors.white,
      tertiaryContainer: const Color(0xFFFFD9DE),
      onTertiaryContainer: const Color(0xFF321018),
      error: const Color(0xFFBA1A1A),
      errorContainer: const Color(0xFFFFDAD6),
      onError: Colors.white,
      onErrorContainer: const Color(0xFF410002),
      surface: const Color(0xFFFFFBFF),
      onSurface: const Color(0xFF1D1B1E),
      surfaceContainerHighest: const Color(0xFFE8E0EB),
      onSurfaceVariant: const Color(0xFF4A454E),
      outline: const Color(0xFF7B757F),
      onInverseSurface: const Color(0xFFF5EFF4),
      inverseSurface: const Color(0xFF322F33),
      inversePrimary: primary.withValues(alpha: 0.7),
      shadow: const Color(0xFF000000),
      surfaceTint: primary,
      outlineVariant: const Color(0xFFCCC4CF),
      scrim: const Color(0xFF000000),
    );
    return ThemeData(
      useMaterial3: false,
      fontFamily: 'Mikhak',
      textTheme: const TextTheme(
        titleLarge: TextStyle(color: Colors.black, fontWeight: FontWeight.bold),
        bodySmall: TextStyle(color: Colors.black),
        labelSmall: TextStyle(color: Colors.black38),
        titleSmall: TextStyle(color: Colors.black),
        displayLarge: TextStyle(color: Colors.black),
        displayMedium: TextStyle(color: Colors.black),
        displaySmall: TextStyle(color: Colors.black),
        headlineLarge: TextStyle(color: Colors.black),
        headlineMedium: TextStyle(color: Colors.black),
        headlineSmall: TextStyle(color: Colors.black),
        labelLarge: TextStyle(color: Colors.black),
        labelMedium: TextStyle(color: Colors.black),
        titleMedium: TextStyle(color: Colors.black),
      ),
      iconTheme: const IconThemeData(color: Colors.black),
      colorScheme: colorScheme,
      extensions: [
        AppSemanticColors(
          // Light-mode success: a dark green readable directly on `surface`
          // (contrast 5.00:1) AND as a fill behind white text (5.13:1), so
          // one value covers both the foreground and container role.
          success: const Color(0xFF2E7D32),
          onSuccess: Colors.white,
          successContainer: const Color(0xFF2E7D32),
          onSuccessContainer: Colors.white,
          warning: const Color(0xFF8A5300),
          onWarning: Colors.white,
          warningContainer: const Color(0xFF8A5300),
          onWarningContainer: Colors.white,
          // Dark blue on the light surface (#FFFBFF): about 5.8:1.
          info: const Color(0xFF1565C0),
          // Aliases of already-vetted ColorScheme tokens (no new literals,
          // no drift risk).
          error: colorScheme.error,
          onError: colorScheme.onError,
          borderColor: colorScheme.outline,
          mutedText: colorScheme.onSurfaceVariant,
          surfaceMuted: colorScheme.surfaceContainerHighest,
          // In light mode, fills are the same primary (dark purple is
          // readable on white).
          primaryFill: primary,
          onPrimaryFill: _onFill(primary),
        ),
      ],
    );
  }

  /// Picks black or white — whichever reads better — for text/icons painted
  /// on top of an opaque [fill]. The primary colour is configurable per build
  /// (and per customer, via `custom_primary_color`), so a hardcoded white
  /// label would silently break on a pale seed.
  static Color _onFill(Color fill) =>
      ThemeData.estimateBrightnessForColor(fill) == Brightness.dark
          ? Colors.white
          : Colors.black;

  /// Converts an arbitrary seed colour into a lighter, high-contrast variant
  /// suitable for use as `primary` on a dark background.
  /// Raises lightness to ~0.80 in HSL space while keeping hue + saturation,
  /// which typically gives ≥ 7:1 contrast on a surface as dark as #1E1B26.
  static Color _lightenForDark(Color seed) {
    final hsl = HSLColor.fromColor(seed);
    return hsl.withLightness(0.80).toColor();
  }

  static final darkTheme = getDarkTheme();
  static final lightTheme = getLightTheme();
}
