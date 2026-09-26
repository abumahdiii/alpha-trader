import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/providers/engine_status_provider.dart';
import 'package:alpha_trader/screens/home_screen.dart';
import 'package:alpha_trader/services/engine_client.dart';
import 'package:alpha_trader/services/engine_process.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:alpha_trader/theme/theme_provider.dart';

/// Never spawns a process: widget tests only need the provider's state.
class _NoProcessLauncher implements EngineLauncher {
  @override
  Future<EngineLaunchResult> launch() async =>
      const EngineLaunchResult(port: 8765, attached: false);

  @override
  Future<void> stop() async {}
}

class _HealthyClient implements EngineClient {
  _HealthyClient(this.port);

  @override
  final int port;

  @override
  Future<EngineHealth> health({Duration? timeout}) async => EngineHealth(
        status: 'ok',
        service: EngineHealth.alphaEngineService,
        version: '0.1.0',
        timeUtc: DateTime.utc(2026, 9, 26, 18),
        mt5: const Mt5Status(
          state: Mt5State.connected,
          server: 'SarmayeGozareBartar-Real',
          loginMasked: '****1234',
        ),
      );

  @override
  Future<void> shutdown({Duration? timeout}) async {}

  @override
  void close() {}
}

EngineStatusProvider _engine() => EngineStatusProvider(
      launcher: _NoProcessLauncher(),
      clientFactory: _HealthyClient.new,
    );

Widget _app({EngineStatusProvider? engine}) => MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => ThemeProvider(initialThemeMode: ThemeMode.light)),
        ChangeNotifierProvider<EngineStatusProvider>(create: (_) => engine ?? _engine()),
      ],
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

  testWidgets('status indicator shows stopped / unknown before the engine starts', (tester) async {
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();

    expect(find.text('موتور: متوقف'), findsOneWidget);
    expect(find.text('MT5: نامشخص'), findsOneWidget);
  });

  testWidgets('status indicator shows running / connected once healthy', (tester) async {
    final EngineStatusProvider engine = _engine();
    await tester.pumpWidget(_app(engine: engine));
    await tester.pump();

    unawaited(engine.start());
    await tester.pump();
    await tester.pump();

    expect(engine.state, EngineState.running);
    expect(find.text('موتور: فعال'), findsOneWidget);
    expect(find.text('MT5: متصل'), findsOneWidget);
    expect(find.text('Alpha Trader'), findsOneWidget);

    final Tooltip mt5Tooltip = tester.widget<Tooltip>(
      find.ancestor(of: find.text('MT5: متصل'), matching: find.byType(Tooltip)).first,
    );
    expect(mt5Tooltip.message, contains('****1234'));
    expect(mt5Tooltip.message, contains('SarmayeGozareBartar-Real'));

    await engine.stop();
    await tester.pump();
    expect(find.text('موتور: متوقف'), findsOneWidget);
  });
}
