import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/screens/home_screen.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:alpha_trader/theme/theme_provider.dart';

Widget _app() => ChangeNotifierProvider(
      create: (_) => ThemeProvider(initialThemeMode: ThemeMode.light),
      child: Builder(
        builder: (context) => MaterialApp(
          theme: MyThemes.getLightTheme(),
          darkTheme: MyThemes.getDarkTheme(),
          themeMode: context.watch<ThemeProvider>().themeMode,
          home: const Directionality(
            textDirection: TextDirection.rtl,
            child: HomeScreen(),
          ),
        ),
      ),
    );

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('home screen shows the app title and welcome text', (tester) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    expect(find.text('Alpha Trader'), findsOneWidget);
    expect(find.text('به Alpha Trader خوش آمدید'), findsOneWidget);
  });

  testWidgets('theme button switches between light and dark', (tester) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    expect(find.byIcon(Icons.dark_mode), findsOneWidget);
    await tester.tap(find.byIcon(Icons.dark_mode));
    await tester.pumpAndSettle();
    expect(find.byIcon(Icons.light_mode), findsOneWidget);
  });
}
