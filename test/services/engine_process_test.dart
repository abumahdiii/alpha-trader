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

  group('packaged engine (release launcher)', () {
    final String app = p.join(Directory.systemTemp.path, 'AlphaTrader');
    final String exe = p.join(app, 'engine', 'alpha_engine', 'alpha_engine.exe');
    final String repo = p.join(Directory.systemTemp.path, 'fake_alpha_repo');
    final String python = p.join(repo, 'engine', '.venv', 'Scripts', 'python.exe');
    final String repoApp = p.join(repo, 'build', 'windows', 'x64', 'runner', 'Release');

    bool Function(String) existing(Set<String> files) => (String path) => files.any((f) => p.equals(f, path));

    test('findPackagedEngine: exe present -> paths below the app folder', () {
      final PackagedEngine? e = findPackagedEngine(appDir: app, fileExists: existing({exe}));
      expect(e, isNotNull);
      expect(p.equals(e!.executable, exe), isTrue);
      expect(p.equals(e.workingDirectory, p.join(app, 'engine', 'alpha_engine')), isTrue);
      expect(p.equals(e.dataDir, p.join(app, 'user_data')), isTrue);
      expect(p.equals(e.envFile, p.join(app, '.env')), isTrue);
    });

    test('findPackagedEngine: exe missing -> null (only the exact path counts)', () {
      expect(findPackagedEngine(appDir: app, fileExists: existing({})), isNull);
      // An engine folder without the exe, or the exe one level off, is not a packaged engine.
      expect(
        findPackagedEngine(appDir: app, fileExists: existing({p.join(app, 'engine', 'alpha_engine.exe')})),
        isNull,
      );
    });

    test('release prefers the packaged exe even when a venv is reachable', () {
      final EngineLocation? l = resolveEngineLocation(
        preferPackaged: true,
        appDir: app,
        searchFrom: [Directory(repo)],
        fileExists: existing({exe, python}),
      );
      expect(l, isA<PackagedEngineLocation>());
    });

    test('release without a packaged exe (run from the repo) falls back to the venv', () {
      final EngineLocation? l = resolveEngineLocation(
        preferPackaged: true,
        appDir: repoApp,
        searchFrom: [Directory(repoApp)],
        fileExists: existing({python}),
      );
      expect(l, isA<DevEngineLocation>());
      expect(p.equals((l! as DevEngineLocation).repoRoot, repo), isTrue);
    });

    test('debug prefers the venv; the packaged exe is only the last resort', () {
      expect(
        resolveEngineLocation(
          preferPackaged: false,
          appDir: app,
          searchFrom: [Directory(repo)],
          fileExists: existing({exe, python}),
        ),
        isA<DevEngineLocation>(),
      );
      expect(
        resolveEngineLocation(
          preferPackaged: false,
          appDir: app,
          searchFrom: [Directory(app)],
          fileExists: existing({exe}),
        ),
        isA<PackagedEngineLocation>(),
      );
    });

    test('debug keeps the ENGINE_ROOT / ALPHA_ENGINE_ROOT overrides (invalid still throws)', () {
      final EngineLocation? l = resolveEngineLocation(
        preferPackaged: false,
        appDir: app,
        envRoot: p.join(repo, 'engine'),
        searchFrom: const [],
        fileExists: existing({exe, python}),
      );
      expect(l, isA<DevEngineLocation>());
      expect(
        () => resolveEngineLocation(
          preferPackaged: false,
          appDir: app,
          dartDefineRoot: p.join(Directory.systemTemp.path, 'nope'),
          searchFrom: const [],
          fileExists: existing({exe}),
        ),
        throwsA(isA<EngineLaunchException>()),
      );
    });

    test('nothing anywhere -> null; the Persian message names both locations', () {
      expect(
        resolveEngineLocation(preferPackaged: true, appDir: app, searchFrom: const [], fileExists: existing({})),
        isNull,
      );
      expect(kEngineNotFoundFa, contains(r'engine\alpha_engine\alpha_engine.exe'));
      expect(kEngineNotFoundFa, contains(r'engine\.venv\Scripts\python.exe'));
      expect(kEngineNotFoundFa, contains('ALPHA_ENGINE_ROOT'));
    });

    test('launch spec, packaged: the exe, no args, exe folder, data dir + env file', () {
      final EngineLaunchSpec s = buildEngineLaunchSpec(
        location: PackagedEngineLocation(PackagedEngine(appDir: app)),
        port: 8766,
        devMode: false,
      );
      expect(s.isPackaged, isTrue);
      expect(p.equals(s.executable, exe), isTrue);
      expect(s.arguments, isEmpty);
      expect(p.equals(s.workingDirectory, p.join(app, 'engine', 'alpha_engine')), isTrue);
      expect(s.environment['DEV_MODE'], 'false');
      expect(s.environment['ENGINE_PORT'], '8766');
      expect(p.equals(s.environment['ALPHA_TRADER_DATA_DIR']!, p.join(app, 'user_data')), isTrue);
      expect(p.equals(s.environment['ALPHA_TRADER_ENV_FILE']!, p.join(app, '.env')), isTrue);
    });

    test('launch spec, dev: unchanged venv command line and environment', () {
      final EngineLaunchSpec s = buildEngineLaunchSpec(location: DevEngineLocation(repo), port: 8765, devMode: true);
      expect(s.isPackaged, isFalse);
      expect(p.equals(s.executable, python), isTrue);
      expect(s.arguments, ['-m', 'alpha_engine']);
      expect(p.equals(s.workingDirectory, p.join(repo, 'engine')), isTrue);
      expect(s.environment, {
        'DEV_MODE': 'true',
        'ENGINE_PORT': '8765',
        'PYTHONUNBUFFERED': '1',
        'PYTHONIOENCODING': 'utf-8',
      });
    });

    test('launch spec never carries credentials', () {
      for (final EngineLocation l in [PackagedEngineLocation(PackagedEngine(appDir: app)), DevEngineLocation(repo)]) {
        final EngineLaunchSpec s = buildEngineLaunchSpec(location: l, port: 8765, devMode: true);
        expect(s.environment.keys.where((k) => k.contains('PASSWORD') || k.contains('LOGIN')), isEmpty);
      }
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
