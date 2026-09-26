import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:window_manager/window_manager.dart';

import 'core/app_logger.dart';
import 'core/dev_mode.dart';
import 'core/single_instance_guard.dart';
import 'screens/home_screen.dart';
import 'theme/theme.dart';
import 'theme/theme_provider.dart';

Future<void> main() async {
  // Refuse to start a second instance before touching Flutter or spawning
  // the Python engine: two copies would fight over the engine's port and
  // the same data/ files.
  if (Platform.isWindows && !acquireSingleInstanceLock()) {
    showAlreadyRunningMessage();
    exit(0);
  }

  // Own error zone so errors thrown outside the Flutter framework's zone
  // (e.g. an unawaited Future) are still logged instead of silently
  // swallowed. See .claude/rules/01_logging_standard.md.
  await runZonedGuarded(() async {
    WidgetsFlutterBinding.ensureInitialized();

    // Adds visibility only (a DEV_MODE log); FlutterError.presentError still
    // runs, so the default behavior (red screen in debug) is unchanged.
    FlutterError.onError = (FlutterErrorDetails details) {
      if (kDevMode) {
        devLog('FlutterError.onError: ${details.exceptionAsString()}');
        devLog('Stack trace:\n${details.stack}');
      }
      FlutterError.presentError(details);
    };

    await windowManager.ensureInitialized();
    if (Platform.isWindows) {
      // Maximized, deliberately NOT full screen: full screen strips the title
      // bar and its minimize/restore/close buttons. The options below are the
      // restore-down size and position.
      const WindowOptions windowOptions = WindowOptions(
        size: Size(1280, 800),
        center: true,
        minimumSize: Size(1000, 700),
        title: 'Alpha Trader',
      );

      // maximize() MUST live inside the readiness callback:
      // waitUntilReadyToShow() calls unmaximize() while applying the options
      // above, so an earlier maximize() would simply be undone. The callback
      // is not awaited by the package, hence the completer.
      final Completer<void> windowReady = Completer<void>();
      try {
        await windowManager.waitUntilReadyToShow(windowOptions, () async {
          try {
            await windowManager.maximize();
            await windowManager.show();
          } catch (e, s) {
            // A window-chrome failure must never stop the app from starting.
            devLog('[Window] maximize/show failed: $e');
            devLog('Stack trace:\n$s');
          } finally {
            if (!windowReady.isCompleted) windowReady.complete();
          }
        });
        // Hard guarantee that startup never hangs on a wedged platform
        // channel: worst case the window is simply not maximized.
        await windowReady.future.timeout(
          const Duration(seconds: 5),
          onTimeout: () => devLog('[Window] maximize/show timed out -- continuing startup'),
        );
      } catch (e, s) {
        devLog('[Window] waitUntilReadyToShow failed: $e');
        devLog('Stack trace:\n$s');
      }

      // Intercept the native close button so shutdown work (later: stopping
      // the Python engine) can finish before the process exits.
      await windowManager.setPreventClose(true);
    }

    final prefs = await SharedPreferences.getInstance();
    final ThemeMode initialThemeMode = switch (prefs.getString('theme')) {
      'dark' => ThemeMode.dark,
      'system' => ThemeMode.system,
      _ => ThemeMode.light,
    };

    runApp(
      MultiProvider(
        providers: [
          ChangeNotifierProvider(
            create: (context) => ThemeProvider(initialThemeMode: initialThemeMode),
          ),
        ],
        child: const AlphaTraderApp(),
      ),
    );
  }, (Object error, StackTrace stack) {
    devLog('runZonedGuarded caught uncaught error: $error');
    devLog('Stack trace:\n$stack');
  });
}

class AlphaTraderApp extends StatefulWidget {
  const AlphaTraderApp({super.key});

  @override
  State<AlphaTraderApp> createState() => _AlphaTraderAppState();
}

class _AlphaTraderAppState extends State<AlphaTraderApp> with WindowListener {
  @override
  void initState() {
    super.initState();
    if (Platform.isWindows) windowManager.addListener(this);
  }

  @override
  void dispose() {
    if (Platform.isWindows) windowManager.removeListener(this);
    super.dispose();
  }

  /// Fires on the native ✕ (setPreventClose(true) holds the default close).
  /// Closing the window means full process exit with nothing left running;
  /// the timeout guarantees a hung shutdown step can never keep it alive.
  @override
  void onWindowClose() async {
    try {
      await _shutdown().timeout(const Duration(seconds: 6));
    } catch (e) {
      devLog('[Shutdown] did not finish in time: $e');
    }
    exit(0);
  }

  Future<void> _shutdown() async {
    // TODO(engine phase): stop the Python engine process here.
    AppLogger.shutdown();
  }

  @override
  Widget build(BuildContext context) {
    final themeProvider = context.watch<ThemeProvider>();
    return MaterialApp(
      title: 'Alpha Trader',
      themeMode: themeProvider.themeMode,
      theme: MyThemes.getLightTheme(),
      darkTheme: MyThemes.getDarkTheme(),
      debugShowCheckedModeBanner: false,
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: const [Locale('fa', 'IR'), Locale('en', 'US')],
      locale: const Locale('fa', 'IR'),
      home: const HomeScreen(),
    );
  }
}
