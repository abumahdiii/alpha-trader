import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/providers/engine_api_provider.dart';
import 'package:alpha_trader/providers/engine_status_provider.dart';
import 'package:alpha_trader/services/engine_api.dart';

import '../helpers/engine_fakes.dart';

void main() {
  // testWidgets for its fake clock: the engine's health poll timers.
  testWidgets('api exists only while running and is replaced after a restart', (tester) async {
    final List<EngineApi> built = [];
    final EngineStatusProvider engine =
        EngineStatusProvider(launcher: NoProcessLauncher(), clientFactory: HealthyClient.new);
    final EngineApiProvider apis = EngineApiProvider(
      engine: engine,
      apiFactory: (int port) {
        final EngineApi api = EngineApi(port: port, httpClientAdapter: FakeEngineHttp());
        built.add(api);
        return api;
      },
    );
    int notified = 0;
    apis.addListener(() => notified++);

    expect(apis.api, isNull);

    unawaited(engine.start());
    await tester.pump();
    await tester.pump();
    expect(engine.state, EngineState.running);
    expect(apis.api, same(built.single));
    expect(apis.api!.port, 8765);

    // Health polls notify the status provider but must not rebuild the API.
    await tester.pump(const Duration(seconds: 11));
    expect(built, hasLength(1));

    await engine.restart();
    await tester.pump();
    await tester.pump();
    expect(engine.state, EngineState.running);
    expect(built, hasLength(2));
    expect(apis.api, same(built.last));
    expect(notified, 3); // ready -> unavailable -> ready

    await engine.stop();
    expect(apis.api, isNull);

    apis.dispose();
    engine.dispose();
  });
}
