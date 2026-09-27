import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/providers/engine_status_provider.dart';
import 'package:alpha_trader/services/engine_client.dart';
import 'package:alpha_trader/services/engine_process.dart';

// These tests run inside `testWidgets` only for its fake clock: `tester.pump
// (d)` advances time by `d` and fires every timer due in between, so the
// real 90 s / 5 s / 3 s policy runs in milliseconds. testWidgets also fails a
// test that leaves a Timer pending, which checks that the provider cleans up
// after dispose()/stop().

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

const EngineClientException refused = EngineClientException(
  EngineClientErrorKind.connectionRefused,
  'refused',
);

const Duration s1 = Duration(seconds: 1);

Duration secs(int n) => Duration(seconds: n);

void main() {
  late FakeLauncher launcher;
  late List<FakeClient> clients;
  late List<EngineState> states;

  /// Production timings (1 s startup poll, 90 s cap, 5 s running poll,
  /// 3 s recovery poll, threshold 3).
  EngineStatusProvider build(Future<EngineHealth> Function(int call) onHealth) {
    clients = [];
    states = [];
    final provider = EngineStatusProvider(
      launcher: launcher,
      clientFactory: (int port) {
        final c = FakeClient(port, onHealth);
        clients.add(c);
        return c;
      },
    );
    provider.addListener(() {
      if (states.isEmpty || states.last != provider.state) {
        states.add(provider.state);
      }
    });
    return provider;
  }

  /// Disposes inside the test body so testWidgets' pending-timer check
  /// sees the state after dispose().
  Future<void> finish(WidgetTester tester, EngineStatusProvider p) async {
    p.dispose();
    await tester.pump();
  }

  setUp(() => launcher = FakeLauncher());

  test('default startup cap is 90 seconds (user decision)', () {
    expect(kEngineStartupTimeout, secs(90));
    final p = EngineStatusProvider(launcher: FakeLauncher());
    expect(p.startupTimeout, secs(90));
    p.dispose();
  });

  group('startup', () {
    testWidgets(
        '(a) normal start: starting -> running, retrying while not '
        'ready', (tester) async {
      final p = build((call) async => call < 3 ? throw refused : healthy());
      expect(p.state, EngineState.stopped);

      unawaited(p.start());
      await tester.pump();
      expect(p.state, EngineState.starting);
      await tester.pump(secs(3));

      expect(states, [EngineState.starting, EngineState.running]);
      expect(launcher.launches, 1);
      expect(clients.single.calls, 3);
      expect(p.port, 8765);
      expect(p.attached, isFalse);
      expect(p.health!.version, '0.1.0');
      expect(p.mt5.state, Mt5State.connected);
      expect(p.errorMessage, isNull);
      expect(p.awaitingRecovery, isFalse);
      await finish(tester, p);
    });

    testWidgets(
        '(b) slow cold start answering at 60 s -> running, never '
        'error', (tester) async {
      bool up = false;
      final p = build((_) async => up ? healthy() : throw refused);
      unawaited(p.start());
      await tester.pump(secs(59));
      expect(p.state, EngineState.starting);
      expect(states, [EngineState.starting]);

      up = true;
      await tester.pump(secs(2));
      expect(p.state, EngineState.running);
      expect(states, [EngineState.starting, EngineState.running]);
      expect(p.errorMessage, isNull);
      await finish(tester, p);
    });

    testWidgets(
        '(c) no answer by 90 s -> error, keeps polling, then '
        'auto-recovers to running', (tester) async {
      bool up = false;
      final p = build((_) async => up ? healthy() : throw refused);
      unawaited(p.start());
      await tester.pump(secs(89));
      expect(p.state, EngineState.starting);

      await tester.pump(secs(2));
      expect(p.state, EngineState.error);
      expect(p.errorMessage, contains('پاسخ نداد'));
      expect(p.errorMessage, contains('90'));
      expect(p.awaitingRecovery, isTrue);

      // Recovery poll every 3 s while the process is alive.
      final int callsAtError = clients.single.calls;
      await tester.pump(secs(9));
      expect(p.state, EngineState.error);
      expect(clients.single.calls, callsAtError + 3);

      up = true;
      await tester.pump(secs(3));
      expect(p.state, EngineState.running);
      expect(p.errorMessage, isNull);
      expect(p.awaitingRecovery, isFalse);
      expect(p.health, isNotNull);
      expect(p.mt5.state, Mt5State.connected);
      expect(states, [
        EngineState.starting,
        EngineState.error,
        EngineState.running,
      ]);

      // Back on the normal 5 s running poll.
      final int callsAtRecovery = clients.single.calls;
      await tester.pump(secs(10));
      expect(clients.single.calls, callsAtRecovery + 2);
      expect(launcher.launches, 1, reason: 'recovery must not relaunch');
      await finish(tester, p);
    });

    testWidgets(
        '(d) process exits while starting -> error and polling '
        'stops', (tester) async {
      final p = build((_) async => throw refused);
      unawaited(p.start());
      await tester.pump(secs(2));
      expect(p.state, EngineState.starting);

      launcher.childExit!.complete(3);
      await tester.pump();
      expect(p.state, EngineState.error);
      expect(p.errorMessage, contains('3'));
      expect(p.awaitingRecovery, isFalse);

      final int calls = clients.single.calls;
      await tester.pump(secs(120));
      expect(clients.single.calls, calls, reason: 'no polling after exit');
      expect(p.state, EngineState.error);
      await finish(tester, p);
    });

    testWidgets('process exits while waiting for recovery -> polling stops',
        (tester) async {
      bool up = false;
      final p = build((_) async => up ? healthy() : throw refused);
      unawaited(p.start());
      await tester.pump(secs(91));
      expect(p.awaitingRecovery, isTrue);

      launcher.childExit!.complete(1);
      await tester.pump();
      expect(p.state, EngineState.error);
      expect(p.awaitingRecovery, isFalse);
      expect(p.errorMessage, contains('کد خروج 1'));

      final int calls = clients.single.calls;
      up = true;
      await tester.pump(secs(30));
      expect(clients.single.calls, calls);
      expect(p.state, EngineState.error);
      await finish(tester, p);
    });

    testWidgets(
        'launch failure -> error with the launcher\'s message, '
        'nothing to poll', (tester) async {
      launcher.error = const EngineLaunchException(
        'هیچ پورت آزادی برای موتور پیدا نشد',
      );
      final p = build((_) async => healthy());
      unawaited(p.start());
      await tester.pump();
      expect(p.state, EngineState.error);
      expect(p.errorMessage, 'هیچ پورت آزادی برای موتور پیدا نشد');
      expect(p.awaitingRecovery, isFalse);
      await tester.pump(secs(30));
      expect(clients, isEmpty);
      expect(p.state, EngineState.error);
      await finish(tester, p);
    });

    testWidgets(
        'attached engine (no exit code): timeout still watches and '
        'recovers', (tester) async {
      launcher = FakeLauncher(attached: true);
      bool up = false;
      final p = build((_) async => up ? healthy() : throw refused);
      unawaited(p.start());
      await tester.pump(secs(91));
      expect(p.state, EngineState.error);
      expect(p.awaitingRecovery, isTrue);
      up = true;
      await tester.pump(secs(3));
      expect(p.state, EngineState.running);
      expect(p.attached, isTrue);
      await finish(tester, p);
    });

    testWidgets('dispose during startup leaves no timer behind',
        (tester) async {
      final p = build((_) async => throw refused);
      unawaited(p.start());
      await tester.pump(secs(5));
      await finish(tester, p);
      // testWidgets itself fails the test if any Timer is still pending.
    });
  });

  group('running', () {
    testWidgets('keeps polling and reflects MT5 changes', (tester) async {
      final p = build(
        (call) async =>
            healthy(call < 4 ? Mt5State.connected : Mt5State.disconnected),
      );
      unawaited(p.start());
      await tester.pump();
      expect(p.mt5.state, Mt5State.connected);
      await tester.pump(secs(15));
      expect(p.state, EngineState.running);
      expect(p.mt5.state, Mt5State.disconnected);
      await finish(tester, p);
    });

    testWidgets(
        '3 consecutive failures -> error, keeps polling while the '
        'process lives, auto-recovers', (tester) async {
      bool failing = false;
      final p = build((_) async => failing ? throw refused : healthy());
      unawaited(p.start());
      await tester.pump();
      expect(p.state, EngineState.running);

      failing = true;
      await tester.pump(secs(10));
      expect(p.state, EngineState.running, reason: '2 failures < threshold');
      await tester.pump(secs(5));
      expect(p.state, EngineState.error);
      expect(p.errorMessage, contains('ارتباط با موتور قطع شد'));
      expect(p.health, isNull);
      expect(p.mt5.state, Mt5State.unknown);
      expect(p.awaitingRecovery, isTrue);

      final int callsAtError = clients.single.calls;
      await tester.pump(secs(6));
      expect(clients.single.calls, callsAtError + 2);

      failing = false;
      await tester.pump(secs(3));
      expect(p.state, EngineState.running);
      expect(states, [
        EngineState.starting,
        EngineState.running,
        EngineState.error,
        EngineState.running,
      ]);
      await finish(tester, p);
    });

    testWidgets('health flapping cycles error <-> running without a restart',
        (tester) async {
      bool failing = false;
      final p = build((_) async => failing ? throw refused : healthy());
      unawaited(p.start());
      await tester.pump();

      for (int i = 0; i < 2; i++) {
        failing = true;
        await tester.pump(secs(15));
        expect(p.state, EngineState.error);
        failing = false;
        await tester.pump(secs(3));
        expect(p.state, EngineState.running);
      }
      expect(states, [
        EngineState.starting,
        EngineState.running,
        EngineState.error,
        EngineState.running,
        EngineState.error,
        EngineState.running,
      ]);
      expect(launcher.launches, 1);
      await finish(tester, p);
    });

    testWidgets(
        'a single failed poll between healthy ones does not trip the '
        'threshold', (tester) async {
      final p = build(
        (call) async => (call == 2 || call == 3) ? throw refused : healthy(),
      );
      unawaited(p.start());
      await tester.pump(secs(40));
      expect(clients.single.calls, 9);
      expect(states, [EngineState.starting, EngineState.running]);
      await finish(tester, p);
    });

    testWidgets(
        'unexpected child exit while running -> error carrying the '
        'exit code, polling stops', (tester) async {
      final p = build((_) async => healthy());
      unawaited(p.start());
      await tester.pump();
      launcher.childExit!.complete(3);
      await tester.pump();
      expect(p.state, EngineState.error);
      expect(p.errorMessage, contains('3'));
      expect(p.awaitingRecovery, isFalse);
      final int calls = clients.single.calls;
      await tester.pump(secs(30));
      expect(clients.single.calls, calls);
      await finish(tester, p);
    });

    testWidgets('start() while already running is a no-op', (tester) async {
      final p = build((_) async => healthy());
      unawaited(p.start());
      await tester.pump();
      await p.start();
      expect(launcher.launches, 1);
      await finish(tester, p);
    });
  });

  group('stop / restart', () {
    testWidgets('stop() delegates to the launcher and ends in stopped',
        (tester) async {
      final p = build((_) async => healthy());
      unawaited(p.start());
      await tester.pump();
      expect(p.state, EngineState.running);

      unawaited(p.stop());
      await tester.pump();
      expect(p.state, EngineState.stopped);
      expect(launcher.stops, 1);
      expect(clients.single.closed, isTrue);
      expect(p.health, isNull);
      expect(p.port, isNull);

      // The child exiting because we asked it to is not an error.
      launcher.childExit!.complete(0);
      await tester.pump();
      expect(p.state, EngineState.stopped);
      final int callsAfterStop = clients.single.calls;
      await tester.pump(secs(30));
      expect(clients.single.calls, callsAfterStop, reason: 'polling stops');
      await finish(tester, p);
    });

    testWidgets('stop() during startup abandons the startup loop',
        (tester) async {
      final p = build((_) async => throw refused);
      unawaited(p.start());
      await tester.pump(secs(3));
      unawaited(p.stop());
      await tester.pump();
      final int calls = clients.single.calls;
      await tester.pump(secs(120));
      expect(p.state, EngineState.stopped);
      expect(clients.single.calls, calls);
      await finish(tester, p);
    });

    testWidgets('stop() while waiting for recovery stops the recovery poll',
        (tester) async {
      final p = build((_) async => throw refused);
      unawaited(p.start());
      await tester.pump(secs(91));
      expect(p.awaitingRecovery, isTrue);
      unawaited(p.stop());
      await tester.pump();
      expect(p.state, EngineState.stopped);
      expect(p.awaitingRecovery, isFalse);
      final int calls = clients.single.calls;
      await tester.pump(secs(30));
      expect(clients.single.calls, calls);
      await finish(tester, p);
    });

    testWidgets('start() from error cleans up the old launch first',
        (tester) async {
      launcher.error = const EngineLaunchException('خطا');
      final p = build((_) async => healthy());
      unawaited(p.start());
      await tester.pump();
      expect(p.state, EngineState.error);

      launcher.error = null;
      unawaited(p.start());
      await tester.pump();
      expect(p.state, EngineState.running);
      expect(launcher.stops, 1);
      expect(launcher.launches, 2);
      await finish(tester, p);
    });

    testWidgets('(e) restart() from running: stopped -> starting -> running',
        (tester) async {
      final p = build((_) async => healthy());
      unawaited(p.start());
      await tester.pump();
      expect(p.state, EngineState.running);

      unawaited(p.restart());
      await tester.pump();
      expect(p.state, EngineState.running);
      expect(states, [
        EngineState.starting,
        EngineState.running,
        EngineState.stopped,
        EngineState.starting,
        EngineState.running,
      ]);
      expect(launcher.stops, 1);
      expect(launcher.launches, 2);
      expect(clients, hasLength(2));
      expect(clients.first.closed, isTrue);
      final int oldCalls = clients.first.calls;
      await tester.pump(secs(20));
      expect(clients.first.calls, oldCalls, reason: 'old client unused');
      expect(clients.last.calls, greaterThan(1));
      await finish(tester, p);
    });

    testWidgets('restart() while starting abandons the first attempt',
        (tester) async {
      bool up = false;
      final p = build((_) async => up ? healthy() : throw refused);
      unawaited(p.start());
      await tester.pump(secs(10));
      expect(p.state, EngineState.starting);

      unawaited(p.restart());
      await tester.pump();
      expect(p.state, EngineState.starting);
      expect(launcher.stops, 1);
      expect(launcher.launches, 2);
      final int firstCalls = clients.first.calls;

      up = true;
      await tester.pump(s1);
      expect(p.state, EngineState.running);
      expect(clients.first.calls, firstCalls, reason: 'first loop abandoned');
      expect(states, [
        EngineState.starting,
        EngineState.stopped,
        EngineState.starting,
        EngineState.running,
      ]);
      await finish(tester, p);
    });

    testWidgets('restart() from a timeout error gets back to running',
        (tester) async {
      int launchesWhenUp = -1;
      final p = build(
        (_) async =>
            launcher.launches == launchesWhenUp ? healthy() : throw refused,
      );
      unawaited(p.start());
      await tester.pump(secs(91));
      expect(p.state, EngineState.error);

      launchesWhenUp = 2; // the relaunched engine answers
      unawaited(p.restart());
      await tester.pump();
      expect(p.state, EngineState.running);
      expect(states, [
        EngineState.starting,
        EngineState.error,
        EngineState.stopped,
        EngineState.starting,
        EngineState.running,
      ]);
      await finish(tester, p);
    });

    testWidgets('a second restart() while the first is stopping is ignored',
        (tester) async {
      final p = build((_) async => healthy());
      unawaited(p.start());
      await tester.pump();

      unawaited(p.restart());
      unawaited(p.restart());
      await tester.pump();
      expect(p.state, EngineState.running);
      expect(launcher.stops, 1);
      expect(launcher.launches, 2);
      await finish(tester, p);
    });

    testWidgets('restart() from stopped simply starts', (tester) async {
      final p = build((_) async => healthy());
      unawaited(p.restart());
      await tester.pump();
      expect(p.state, EngineState.running);
      expect(launcher.launches, 1);
      await finish(tester, p);
    });
  });
}
