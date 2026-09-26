import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/providers/engine_status_provider.dart';
import 'package:alpha_trader/services/engine_client.dart';
import 'package:alpha_trader/services/engine_process.dart';

class FakeLauncher implements EngineLauncher {
  FakeLauncher({this.attached = false, this.error});

  final bool attached;
  EngineLaunchException? error;
  int launches = 0;
  int stops = 0;
  Completer<int>? childExit;

  @override
  Future<EngineLaunchResult> launch() async {
    launches++;
    if (error != null) throw error!;
    childExit = Completer<int>();
    return EngineLaunchResult(
      port: 8765,
      attached: attached,
      exitCode: attached ? null : childExit!.future,
    );
  }

  @override
  Future<void> stop() async => stops++;
}

class FakeClient implements EngineClient {
  FakeClient(this.port, this.onHealth);

  @override
  final int port;
  final Future<EngineHealth> Function(int call) onHealth;
  int calls = 0;
  bool closed = false;

  @override
  Future<EngineHealth> health({Duration? timeout}) => onHealth(++calls);

  @override
  Future<void> shutdown({Duration? timeout}) async {}

  @override
  void close() => closed = true;
}

EngineHealth healthy([Mt5State mt5 = Mt5State.connected]) => EngineHealth(
      status: 'ok',
      service: EngineHealth.alphaEngineService,
      version: '0.1.0',
      pid: 1234,
      mt5: Mt5Status(state: mt5, loginMasked: '****1234'),
    );

const EngineClientException refused =
    EngineClientException(EngineClientErrorKind.connectionRefused, 'refused');

Future<void> until(bool Function() condition, {Duration timeout = const Duration(seconds: 3)}) async {
  final Stopwatch sw = Stopwatch()..start();
  while (!condition()) {
    if (sw.elapsed > timeout) fail('condition not reached within $timeout');
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}

void main() {
  late FakeLauncher launcher;
  late List<FakeClient> clients;
  late List<EngineState> states;

  EngineStatusProvider build(
    Future<EngineHealth> Function(int call) onHealth, {
    Duration startupTimeout = const Duration(seconds: 2),
  }) {
    clients = [];
    states = [];
    final provider = EngineStatusProvider(
      launcher: launcher,
      clientFactory: (int port) {
        final c = FakeClient(port, onHealth);
        clients.add(c);
        return c;
      },
      startupPollInterval: const Duration(milliseconds: 10),
      startupTimeout: startupTimeout,
      runningPollInterval: const Duration(milliseconds: 10),
      failureThreshold: 3,
    );
    provider.addListener(() {
      if (states.isEmpty || states.last != provider.state) states.add(provider.state);
    });
    addTearDown(provider.dispose);
    return provider;
  }

  setUp(() => launcher = FakeLauncher());

  test('starting -> running once /health answers, retrying while not ready', () async {
    final provider = build((call) async => call < 3 ? throw refused : healthy());
    expect(provider.state, EngineState.stopped);

    unawaited(provider.start());
    expect(provider.state, EngineState.starting);
    await until(() => provider.state == EngineState.running);

    expect(states, [EngineState.starting, EngineState.running]);
    expect(launcher.launches, 1);
    expect(clients.single.calls, greaterThanOrEqualTo(3));
    expect(provider.port, 8765);
    expect(provider.attached, isFalse);
    expect(provider.health!.version, '0.1.0');
    expect(provider.mt5.state, Mt5State.connected);
    expect(provider.errorMessage, isNull);
  });

  test('keeps polling while running and reflects MT5 changes', () async {
    final provider = build((call) async => healthy(call < 4 ? Mt5State.connected : Mt5State.disconnected));
    unawaited(provider.start());
    await until(() => provider.mt5.state == Mt5State.disconnected);
    expect(provider.state, EngineState.running);
  });

  test('running -> error after 3 consecutive health failures', () async {
    final provider = build((call) async => call == 1 ? healthy() : throw refused);
    unawaited(provider.start());
    await until(() => provider.state == EngineState.running);
    await until(() => provider.state == EngineState.error);

    expect(states, [EngineState.starting, EngineState.running, EngineState.error]);
    expect(clients.single.calls, 4); // 1 healthy + 3 failures, then polling stops
    expect(provider.errorMessage, contains('ارتباط با موتور قطع شد'));
    expect(provider.health, isNull);
    expect(provider.mt5.state, Mt5State.unknown);
  });

  test('a single failed poll between healthy ones does not trip the threshold', () async {
    final provider = build((call) async => (call == 2 || call == 3) ? throw refused : healthy());
    unawaited(provider.start());
    await until(() => clients.isNotEmpty && clients.single.calls >= 8);
    expect(provider.state, EngineState.running);
  });

  test('launch failure -> error with the launcher\'s Persian message', () async {
    launcher.error = const EngineLaunchException('هیچ پورت آزادی برای موتور پیدا نشد');
    final provider = build((_) async => healthy());
    await provider.start();
    expect(provider.state, EngineState.error);
    expect(provider.errorMessage, 'هیچ پورت آزادی برای موتور پیدا نشد');
  });

  test('no healthy answer within the startup timeout -> error', () async {
    final provider = build(
      (_) async => throw refused,
      startupTimeout: const Duration(milliseconds: 100),
    );
    await provider.start();
    expect(provider.state, EngineState.error);
    expect(provider.errorMessage, contains('پاسخ نداد'));
  });

  test('unexpected child exit -> error carrying the exit code', () async {
    final provider = build((_) async => healthy());
    unawaited(provider.start());
    await until(() => provider.state == EngineState.running);
    launcher.childExit!.complete(3);
    await until(() => provider.state == EngineState.error);
    expect(provider.errorMessage, contains('3'));
  });

  test('stop() delegates to the launcher and ends in stopped', () async {
    final provider = build((_) async => healthy());
    unawaited(provider.start());
    await until(() => provider.state == EngineState.running);

    await provider.stop();
    expect(provider.state, EngineState.stopped);
    expect(launcher.stops, 1);
    expect(clients.single.closed, isTrue);
    expect(provider.health, isNull);
    expect(provider.port, isNull);

    // The child exiting because we asked it to is not an error.
    launcher.childExit!.complete(0);
    await Future<void>.delayed(const Duration(milliseconds: 30));
    expect(provider.state, EngineState.stopped);
    final int callsAfterStop = clients.single.calls;
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(clients.single.calls, callsAfterStop, reason: 'polling must stop');
  });

  test('stop() during startup abandons the startup loop', () async {
    final provider = build((_) async => throw refused);
    unawaited(provider.start());
    await until(() => clients.isNotEmpty);
    await provider.stop();
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(provider.state, EngineState.stopped);
  });

  test('start() from error cleans up the old launch first', () async {
    launcher.error = const EngineLaunchException('خطا');
    final provider = build((_) async => healthy());
    await provider.start();
    expect(provider.state, EngineState.error);

    launcher.error = null;
    unawaited(provider.start());
    await until(() => provider.state == EngineState.running);
    expect(launcher.stops, 1);
    expect(launcher.launches, 2);
  });

  test('attached engine is reported as attached', () async {
    launcher = FakeLauncher(attached: true);
    final provider = build((_) async => healthy());
    unawaited(provider.start());
    await until(() => provider.state == EngineState.running);
    expect(provider.attached, isTrue);
  });

  test('start() while already running is a no-op', () async {
    final provider = build((_) async => healthy());
    unawaited(provider.start());
    await until(() => provider.state == EngineState.running);
    await provider.start();
    expect(launcher.launches, 1);
  });
}
