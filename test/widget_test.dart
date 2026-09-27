import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/providers/engine_status_provider.dart';
import 'package:alpha_trader/screens/chart/chart_screen.dart';
import 'package:alpha_trader/screens/shell_screen.dart';

import 'helpers/engine_fakes.dart';

FakeEngineHttp _http() => FakeEngineHttp({
      'GET /settings': (_) => jsonBody(settingsJson()),
      'GET /strategies': (_) => jsonBody([strategyJson()]),
    });

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('home is the shell: app title and the chart page', (tester) async {
    final EngineHarness h = EngineHarness(http: _http());
    await tester.pumpWidget(h.wrap(const ShellScreen()));
    await tester.pumpAndSettle();

    expect(find.text('Alpha Trader'), findsOneWidget);
    expect(find.byType(ChartScreen), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('theme button switches between light and dark', (tester) async {
    final EngineHarness h = EngineHarness(http: _http());
    await tester.pumpWidget(h.wrap(const ShellScreen()));
    await tester.pumpAndSettle();

    expect(find.byIcon(Icons.dark_mode), findsOneWidget);
    await tester.tap(find.byIcon(Icons.dark_mode));
    await tester.pumpAndSettle();
    expect(find.byIcon(Icons.light_mode), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('status indicator shows stopped / unknown before the engine starts', (tester) async {
    final EngineHarness h = EngineHarness(http: _http());
    await tester.pumpWidget(h.wrap(const ShellScreen()));
    await tester.pumpAndSettle();

    expect(find.text('موتور: متوقف'), findsOneWidget);
    expect(find.text('MT5: نامشخص'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('status indicator shows running / connected once healthy', (tester) async {
    final EngineHarness h = EngineHarness(http: _http());
    await tester.pumpWidget(h.wrap(const ShellScreen()));
    await tester.pump();

    await h.start(tester);

    expect(find.text('موتور: فعال'), findsOneWidget);
    expect(find.text('MT5: متصل'), findsOneWidget);
    expect(find.text('Alpha Trader'), findsOneWidget);

    final Tooltip mt5Tooltip = tester.widget<Tooltip>(
      find.ancestor(of: find.text('MT5: متصل'), matching: find.byType(Tooltip)).first,
    );
    expect(mt5Tooltip.message, contains('****1234'));
    expect(mt5Tooltip.message, contains('SarmayeGozareBartar-Real'));

    await h.engine.stop();
    await tester.pump();
    expect(h.engine.state, EngineState.stopped);
    expect(find.text('موتور: متوقف'), findsOneWidget);
    await h.dispose(tester);
  });
}
