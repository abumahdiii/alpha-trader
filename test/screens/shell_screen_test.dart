import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/screens/chart/chart_screen.dart';
import 'package:alpha_trader/screens/shell_screen.dart';
import 'package:alpha_trader/widgets/engine_gate.dart';
import 'package:alpha_trader/widgets/engine_status_indicator.dart';

import '../helpers/engine_fakes.dart';

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  FakeEngineHttp http() => FakeEngineHttp({
        'GET /settings': (_) => jsonBody(settingsJson()),
        'GET /strategies': (_) => jsonBody([strategyJson()]),
      });

  testWidgets('rail has 5 destinations; backtest and signal are disabled with «به‌زودی»', (tester) async {
    final EngineHarness h = EngineHarness(http: http());
    await tester.pumpWidget(h.wrap(const ShellScreen()));
    await tester.pumpAndSettle();

    final NavigationRail rail = tester.widget<NavigationRail>(find.byType(NavigationRail));
    expect(rail.destinations, hasLength(5));
    expect(
      [for (final d in rail.destinations) d.disabled],
      [false, false, true, true, false],
    );
    for (final String label in ['چارت', 'سیستم‌ها', 'بک‌تست', 'سیگنال', 'تنظیمات']) {
      expect(find.text(label), findsOneWidget, reason: label);
    }
    expect(find.text(ShellScreen.comingSoon), findsNWidgets(2));
    expect(rail.selectedIndex, 0);
    expect(find.byType(ChartScreen), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('the engine indicator lives in the top bar', (tester) async {
    final EngineHarness h = EngineHarness(http: http());
    await tester.pumpWidget(h.wrap(const ShellScreen()));
    await tester.pumpAndSettle();

    expect(
      find.descendant(of: find.byType(AppBar), matching: find.byType(EngineStatusIndicator)),
      findsOneWidget,
    );
    await h.dispose(tester);
  });

  testWidgets('RTL: the rail is on the right of the page content', (tester) async {
    final EngineHarness h = EngineHarness(http: http());
    await tester.pumpWidget(h.wrap(const ShellScreen()));
    await tester.pumpAndSettle();

    expect(Directionality.of(tester.element(find.byType(ShellScreen))), TextDirection.rtl);
    final double railX = tester.getCenter(find.byType(NavigationRail)).dx;
    final double pageX = tester.getCenter(find.byType(IndexedStack)).dx;
    expect(railX, greaterThan(pageX));
    await h.dispose(tester);
  });

  testWidgets('navigates to enabled pages; disabled ones do nothing', (tester) async {
    final EngineHarness h = EngineHarness(http: http());
    await tester.pumpWidget(h.wrap(const ShellScreen()));
    await tester.pumpAndSettle();

    await tester.tap(find.text('بک‌تست'));
    await tester.pumpAndSettle();
    expect(tester.widget<NavigationRail>(find.byType(NavigationRail)).selectedIndex, 0);

    await tester.tap(find.text('تنظیمات'));
    await tester.pumpAndSettle();
    expect(tester.widget<NavigationRail>(find.byType(NavigationRail)).selectedIndex, 4);
    // Engine not started yet: the page explains instead of failing.
    expect(find.text(EngineUnavailableView.title), findsOneWidget);
    expect(find.text('حساب و ریسک'), findsOneWidget);

    await tester.tap(find.text('سیستم‌ها'));
    await tester.pumpAndSettle();
    expect(tester.widget<NavigationRail>(find.byType(NavigationRail)).selectedIndex, 1);
    expect(find.text(EngineUnavailableView.title), findsOneWidget);

    // Once the engine runs, the page loads by itself.
    await h.start(tester);
    expect(find.text(EngineUnavailableView.title), findsNothing);
    expect(find.text('کانال انحراف معیار'), findsWidgets);
    await h.dispose(tester);
  });
}
