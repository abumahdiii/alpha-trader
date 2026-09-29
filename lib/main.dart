import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:window_manager/window_manager.dart';

import 'core/app_logger.dart';
import 'core/dev_mode.dart';
import 'core/main_window.dart';
import 'core/single_instance_guard.dart';
import 'providers/engine_api_provider.dart';
import 'providers/engine_status_provider.dart';
import 'providers/shell_navigation.dart';
import 'providers/signals_provider.dart';
import 'screens/shell_screen.dart';
import 'services/engine_process.dart';
import 'services/signal_alerts.dart';
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
      // bar and its minimize/restore/close buttons. The window stays hidden
      // here; only its restore-down rect (clamped to the primary monitor's
      // work area) and minimum size are set. The maximize itself happens
      // after the runner has shown the window -- see main_window.dart.
      await prepareMainWindow();

      // Intercept the native close button so shutdown work (stopping the
      // Python engine, flushing the diagnostic log) can finish before the
      // process exits. See _AlphaTraderAppState.onWindowClose.
      await windowManager.setPreventClose(true);
    }

    final prefs = await SharedPreferences.getInstance();
    final ThemeMode initialThemeMode = switch (prefs.getString('theme')) {
      'dark' => ThemeMode.dark,
      'system' => ThemeMode.system,
      _ => ThemeMode.light,
    };

    // Created here (not lazily inside the tree) so the engine can be started
    // right after runApp without waiting for a widget to read it.
    final EngineStatusProvider engineStatus = EngineStatusProvider(launcher: EngineProcess());
    // Data API for the pages; non-null only while the engine runs.
    final EngineApiProvider engineApis = EngineApiProvider(engine: engineStatus);
    // Live signals for the whole app lifetime (WS /ws/signals while the
    // engine runs), with sound + Windows notification per NEW signal.
    // SUGGESTIONS ONLY: nothing in the app can send an order.
    final SignalsProvider signals = SignalsProvider(apis: engineApis);
    final SignalAlertPrefs alertPrefs = await SignalAlertPrefs.load();
    final SignalAlertDispatcher alerts = SignalAlertDispatcher(
      signals: signals.newSignals,
      alerter: platformSignalAlerter(windowHandle: windowManager.getId),
      prefs: alertPrefs,
      digitsOf: signals.digitsOf,
    );

    runApp(
      MultiProvider(
        providers: [
          ChangeNotifierProvider(
            create: (context) => ThemeProvider(initialThemeMode: initialThemeMode),
          ),
          ChangeNotifierProvider<EngineStatusProvider>.value(value: engineStatus),
          ChangeNotifierProvider<EngineApiProvider>.value(value: engineApis),
          ChangeNotifierProvider<SignalsProvider>.value(value: signals),
          ChangeNotifierProvider<SignalAlertPrefs>.value(value: alertPrefs),
          Provider<SignalAlertDispatcher>.value(value: alerts),
          // Shown shell page + cross-page requests (chart -> backtest prefill).
          ChangeNotifierProvider<ShellNavigation>(create: (_) => ShellNavigation()),
        ],
        child: const AlphaTraderApp(),
      ),
    );

    // Start the engine only after the first frame and never await it here:
    // the Python import alone can take seconds, and the window must stay
    // responsive meanwhile. start() reports failures through its own state.
    // Non-Windows hosts (tests, CI) spawn nothing.
    if (Platform.isWindows) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        unawaited(maximizeMainWindowWhenShown());
        unawaited(engineStatus.start());
      });
    }
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
      // _shutdown's finally never ran; flush what we can before exiting.
      AppLogger.shutdown();
    }
    exit(0);
  }

  /// Stops the engine (POST /shutdown -> wait -> kill, < 4 s; see
  /// EngineProcess.stop), then flushes the diagnostic log no matter what.
  Future<void> _shutdown() async {
    final EngineStatusProvider engine = context.read<EngineStatusProvider>();
    final SignalAlertDispatcher alerts = context.read<SignalAlertDispatcher>();
    try {
      // Removes the tray icon of the signal notifications.
      await alerts.dispose();
    } catch (e) {
      devLog('[Shutdown] signal alerts dispose failed: $e');
    }
    try {
      devLog('[Shutdown] stopping engine (state=${engine.state.name}, port=${engine.port})');
      await engine.stop();
    } catch (e, s) {
      devLog('[Shutdown] engine stop failed: $e');
      devLog('Stack trace:\n$s');
    } finally {
      AppLogger.shutdown();
    }
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
      home: const ShellScreen(),
    );
  }
}
