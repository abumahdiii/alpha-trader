import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/providers/engine_status_provider.dart';
import 'package:alpha_trader/services/engine_client.dart';
import 'package:alpha_trader/services/engine_process.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:alpha_trader/widgets/engine_status_indicator.dart';

class _CountingLauncher implements EngineLauncher {
  int launches = 0;
  int stops = 0;

  @override
  Future<EngineLaunchResult> launch() async {
    launches++;
    return const EngineLaunchResult(port: 8765, attached: false);
  }

  @override
  Future<void> stop() async => stops++;
}

class _Client implements EngineClient {
  _Client(this.port, this.up);

  @override
  final int port;
  final bool Function() up;

  @override
  Future<EngineHealth> health({Duration? timeout}) async {
    if (!up()) {
      throw const EngineClientException(
        EngineClientErrorKind.connectionRefused,
        'refused',
      );
    }
    return const EngineHealth(
      status: 'ok',
      service: EngineHealth.alphaEngineService,
      version: '0.1.0',
      mt5: Mt5Status(state: Mt5State.connected, loginMasked: '****1234'),
    );
  }

  @override
  Future<void> shutdown({Duration? timeout}) async {}

  @override
  void close() {}
}

Widget _app(EngineStatusProvider engine) =>
    ChangeNotifierProvider<EngineStatusProvider>.value(
      value: engine,
      child: MaterialApp(
        theme: MyThemes.getLightTheme(),
        home: Directionality(
          textDirection: TextDirection.rtl,
          child: Scaffold(
            appBar: AppBar(actions: const [EngineStatusIndicator()]),
          ),
        ),
      ),
    );

void main() {
  testWidgets('starting tooltip warns that the first run may take a while',
      (tester) async {
    final launcher = _CountingLauncher();
    final engine = EngineStatusProvider(
      launcher: launcher,
      clientFactory: (port) => _Client(port, () => false),
    );
    await tester.pumpWidget(_app(engine));
    unawaited(engine.start());
    await tester.pump();

    expect(find.text('موتور: در حال راه‌اندازی'), findsOneWidget);
    final Tooltip tooltip = tester.widget<Tooltip>(
      find
          .ancestor(
            of: find.text('موتور: در حال راه‌اندازی'),
            matching: find.byType(Tooltip),
          )
          .first,
    );
    expect(tooltip.message, contains('اجرای اول'));
    expect(tooltip.message, contains('90 ثانیه'));
    expect(tooltip.message, contains(EngineStatusIndicator.restartLabel));

    await engine.stop();
    await tester.pump();
    engine.dispose();
  });

  testWidgets('error tooltip says the engine is still being watched',
      (tester) async {
    final engine = EngineStatusProvider(
      launcher: _CountingLauncher(),
      clientFactory: (port) => _Client(port, () => false),
    );
    await tester.pumpWidget(_app(engine));
    unawaited(engine.start());
    await tester.pump(const Duration(seconds: 91));

    expect(find.text('موتور: خطا'), findsOneWidget);
    final String message = EngineStatusIndicator.engineTooltip(engine);
    expect(message, contains('پاسخ نداد'));
    expect(message, contains('خودکار'));

    await engine.stop();
    await tester.pump();
    engine.dispose();
  });

  testWidgets('menu action "راه‌اندازی مجدد موتور" restarts the engine',
      (tester) async {
    final launcher = _CountingLauncher();
    final engine = EngineStatusProvider(
      launcher: launcher,
      clientFactory: (port) => _Client(port, () => true),
    );
    await tester.pumpWidget(_app(engine));
    unawaited(engine.start());
    await tester.pump();
    expect(find.text('موتور: فعال'), findsOneWidget);

    await tester.tap(find.text('موتور: فعال'));
    await tester.pumpAndSettle();
    expect(find.text(EngineStatusIndicator.restartLabel), findsOneWidget);

    final List<EngineState> seen = [];
    engine.addListener(() {
      if (seen.isEmpty || seen.last != engine.state) seen.add(engine.state);
    });
    await tester.tap(find.text(EngineStatusIndicator.restartLabel));
    await tester.pumpAndSettle();

    expect(launcher.stops, 1);
    expect(launcher.launches, 2);
    expect(
        seen, [EngineState.stopped, EngineState.starting, EngineState.running]);
    expect(find.text('موتور: فعال'), findsOneWidget);

    await engine.stop();
    await tester.pump();
    engine.dispose();
  });
}
