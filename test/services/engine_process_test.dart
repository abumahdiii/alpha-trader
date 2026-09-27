import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;

import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/services/engine_process.dart';

EngineHealth _health(String? service, {int? pid}) =>
    EngineHealth(status: 'ok', service: service, pid: pid, mt5: Mt5Status.unknown);

void main() {
  group('chooseEnginePort', () {
    Future<EnginePortDecision> choose({
      required Set<int> busy,
      EngineHealth? onPreferred,
    }) =>
        chooseEnginePort(
          preferred: 8765,
          fallbacks: kFallbackEnginePorts,
          isPortFree: (port) async => !busy.contains(port),
          probeHealth: (port) async => port == 8765 ? onPreferred : null,
        );

    test('preferred port free -> spawn on it', () async {
      final d = await choose(busy: {});
      expect(d.port, 8765);
      expect(d.attach, isFalse);
    });

    test('preferred port busy with our engine -> attach', () async {
      final d = await choose(busy: {8765}, onPreferred: _health('alpha_engine', pid: 42));
      expect(d.port, 8765);
      expect(d.attach, isTrue);
      expect(d.attachedPid, 42);
    });

    test('preferred port busy with a foreign service -> first free fallback', () async {
      final d = await choose(busy: {8765, 8766, 8767}, onPreferred: _health('something_else'));
      expect(d.port, 8768);
      expect(d.attach, isFalse);
    });

    test('preferred port busy and not answering -> fallback', () async {
      final d = await choose(busy: {8765});
      expect(d.port, 8766);
      expect(d.attach, isFalse);
    });

    test('everything busy -> EngineLaunchException with a Persian message', () async {
      await expectLater(
        choose(busy: {8765, ...kFallbackEnginePorts}),
        throwsA(isA<EngineLaunchException>().having((e) => e.message, 'message', contains('پورت'))),
      );
    });

    test('fallback range is exactly 8766..8775', () {
      expect(kFallbackEnginePorts, List<int>.generate(10, (i) => 8766 + i));
    });
  });

  group('findEngineRoot', () {
    final String repo = p.join(Directory.systemTemp.path, 'fake_alpha_repo');
    final String python = p.join(repo, 'engine', '.venv', 'Scripts', 'python.exe');
    bool exists(String path) => p.equals(path, python);

    test('walks up from a nested start directory (debug exe under build/)', () {
      final String exeDir = p.join(repo, 'build', 'windows', 'x64', 'runner', 'Debug');
      expect(
        findEngineRoot(searchFrom: [Directory(exeDir)], fileExists: exists),
        p.normalize(repo),
      );
    });

    test('tries every start directory (CWD first, then executable)', () {
      final String unrelated = p.join(Directory.systemTemp.path, 'elsewhere');
      expect(
        findEngineRoot(
          searchFrom: [Directory(unrelated), Directory(p.join(repo, 'build'))],
          fileExists: exists,
        ),
        p.normalize(repo),
      );
    });

    test('returns null when nothing is found', () {
      expect(
        findEngineRoot(searchFrom: [Directory(Directory.systemTemp.path)], fileExists: (_) => false),
        isNull,
      );
    });

    test('dart-define override wins, and may point at engine/', () {
      expect(findEngineRoot(dartDefineRoot: repo, searchFrom: const [], fileExists: exists),
          p.normalize(repo));
      expect(
        findEngineRoot(
          dartDefineRoot: p.join(repo, 'engine'),
          searchFrom: const [],
          fileExists: exists,
        ),
        p.normalize(repo),
      );
    });

    test('env override is used when no dart-define is set', () {
      expect(
        findEngineRoot(dartDefineRoot: '', envRoot: repo, searchFrom: const [], fileExists: exists),
        p.normalize(repo),
      );
    });

    test('an invalid override fails loudly instead of falling back', () {
      expect(
        () => findEngineRoot(
          envRoot: p.join(Directory.systemTemp.path, 'wrong'),
          searchFrom: [Directory(repo)],
          fileExists: exists,
        ),
        throwsA(isA<EngineLaunchException>()),
      );
    });
  });

  test('isLoopbackPortFree detects a bound port', () async {
    final ServerSocket server = await ServerSocket.bind(InternetAddress.loopbackIPv4, 0);
    final int port = server.port;
    expect(await isLoopbackPortFree(port), isFalse);
    await server.close();
    expect(await isLoopbackPortFree(port), isTrue);
  });
}
