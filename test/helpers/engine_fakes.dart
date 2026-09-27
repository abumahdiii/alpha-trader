import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/providers/engine_api_provider.dart';
import 'package:alpha_trader/providers/engine_status_provider.dart';
import 'package:alpha_trader/services/engine_api.dart';
import 'package:alpha_trader/services/engine_client.dart';
import 'package:alpha_trader/services/engine_process.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:alpha_trader/theme/theme_provider.dart';
import 'package:alpha_trader/widgets/top_notice_stack.dart';

// Shared fakes for widget/unit tests: no process is spawned and no socket
// is opened. Response bodies mirror the engine's pydantic models.

/// Never spawns a process.
class NoProcessLauncher implements EngineLauncher {
  @override
  Future<EngineLaunchResult> launch() async => const EngineLaunchResult(port: 8765, attached: false);

  @override
  Future<void> stop() async {}
}

/// Answers every /health with a healthy engine and a connected MT5.
class HealthyClient implements EngineClient {
  HealthyClient(this.port);

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
          tradeMode: 'demo',
        ),
      );

  @override
  Future<void> shutdown({Duration? timeout}) async {}

  @override
  void close() {}
}

typedef RouteHandler = FutureOr<ResponseBody> Function(RequestOptions request);

/// Fake HTTP transport for [EngineApi], routed by `"METHOD /path"`.
/// Unrouted requests answer 404 like FastAPI does.
class FakeEngineHttp implements HttpClientAdapter {
  FakeEngineHttp([Map<String, RouteHandler>? routes]) : routes = {...?routes};

  final Map<String, RouteHandler> routes;
  final List<RequestOptions> requests = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    final RouteHandler? handler = routes['${options.method} ${options.uri.path}'];
    if (handler == null) return jsonBody({'detail': 'Not Found'}, 404);
    return handler(options);
  }

  @override
  void close({bool force = false}) {}

  /// Requests to `"METHOD /path"`.
  List<RequestOptions> sent(String route) =>
      requests.where((r) => '${r.method} ${r.uri.path}' == route).toList();

  /// Decoded JSON body of a request ([EngineApi] sends JSON text).
  static Object? bodyOf(RequestOptions r) => r.data == null ? null : jsonDecode(r.data as String);
}

ResponseBody jsonBody(Object body, [int status = 200]) => ResponseBody.fromString(
      body is String ? body : jsonEncode(body),
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );

/// The engine's `{"detail": {"code", "message_fa", "errors_fa"}}` error.
ResponseBody engineError(int status, String code, String messageFa, [List<String> errorsFa = const []]) =>
    jsonBody({
      'detail': {'code': code, 'message_fa': messageFa, 'errors_fa': errorsFa},
    }, status);

Map<String, Object?> settingsJson({
  double balance = 1000.0,
  double riskPct = 1.0,
  int leverage = 100,
  double rr = 2.0,
}) =>
    {'balance': balance, 'risk_pct': riskPct, 'leverage': leverage, 'rr': rr};

/// A schema like the engine's `stddev_channel` (subset) plus one param the
/// UI has never heard of, to prove the form is generated from the schema.
List<Map<String, Object?>> sampleSchema() => [
      {
        'name': 'n',
        'type': 'int',
        'default': 100,
        'min': 10.0,
        'max': 500.0,
        'choices': null,
        'step': 1.0,
        'label_fa': 'طول کانال (تعداد کندل H4)',
        'description_fa': 'تعداد کندل‌های بسته‌شده H4.',
      },
      {
        'name': 'k',
        'type': 'float',
        'default': 2.0,
        'min': 0.5,
        'max': 5.0,
        'choices': null,
        'step': 0.1,
        'label_fa': 'ضریب انحراف معیار (k)',
        'description_fa': '',
      },
      {
        'name': 'sigma_ddof',
        'type': 'choice',
        'default': 0,
        'min': null,
        'max': null,
        'choices': [0, 1],
        'step': null,
        'label_fa': 'روش محاسبه انحراف معیار',
        'description_fa': '۰ = جمعیت (پیش‌فرض)، ۱ = نمونه.',
      },
      {
        'name': 'use_pin_bar',
        'type': 'bool',
        'default': true,
        'min': null,
        'max': null,
        'choices': null,
        'step': null,
        'label_fa': 'تایید با پین‌بار',
        'description_fa': '',
      },
      {
        'name': 'projection_mode',
        'type': 'choice',
        'default': 'bar_count',
        'min': null,
        'max': null,
        'choices': ['bar_count', 'calendar'],
        'step': null,
        'label_fa': 'روش امتداد خط کانال H4 روی H1',
        'description_fa': '',
      },
      {
        'name': 'future_window',
        'type': 'int',
        'default': 7,
        'min': 1.0,
        'max': 50.0,
        'choices': null,
        'step': 1.0,
        'label_fa': 'پنجره آزمایشی',
        'description_fa': '',
      },
    ];

Map<String, Object?> defaultParams() => {
      'n': 100,
      'k': 2.0,
      'sigma_ddof': 0,
      'use_pin_bar': true,
      'projection_mode': 'bar_count',
      'future_window': 7,
    };

Map<String, Object?> strategyJson({
  int paramsVersion = 1,
  Map<String, Object?>? params,
  bool? createdNewVersion,
  List<String> paramsErrorsFa = const [],
}) =>
    {
      'name': 'stddev_channel',
      'title_fa': 'کانال انحراف معیار',
      'version': 1,
      'params_version': paramsVersion,
      'params': params ?? defaultParams(),
      'params_hash': 'ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12',
      'params_saved_utc': '2026-09-27T08:30:00Z',
      'param_schema': sampleSchema(),
      'params_errors_fa': paramsErrorsFa,
      if (createdNewVersion != null) 'created_new_version': createdNewVersion,
    };

/// Providers + MaterialApp (Persian locale, RTL) around [child], with an
/// engine that becomes running on [start] and an [EngineApi] on [http].
class EngineHarness {
  EngineHarness({FakeEngineHttp? http}) : http = http ?? FakeEngineHttp() {
    engine = EngineStatusProvider(launcher: NoProcessLauncher(), clientFactory: HealthyClient.new);
    apis = EngineApiProvider(
      engine: engine,
      apiFactory: (int port) => EngineApi(port: port, httpClientAdapter: this.http),
    );
  }

  final FakeEngineHttp http;
  late final EngineStatusProvider engine;
  late final EngineApiProvider apis;

  Widget wrap(Widget child) => MultiProvider(
        providers: [
          ChangeNotifierProvider(create: (_) => ThemeProvider(initialThemeMode: ThemeMode.light)),
          ChangeNotifierProvider<EngineStatusProvider>.value(value: engine),
          ChangeNotifierProvider<EngineApiProvider>.value(value: apis),
        ],
        child: Builder(
          builder: (context) => MaterialApp(
            theme: MyThemes.getLightTheme(),
            darkTheme: MyThemes.getDarkTheme(),
            themeMode: context.watch<ThemeProvider>().themeMode,
            localizationsDelegates: const [
              GlobalMaterialLocalizations.delegate,
              GlobalWidgetsLocalizations.delegate,
              GlobalCupertinoLocalizations.delegate,
            ],
            supportedLocales: const [Locale('fa', 'IR'), Locale('en', 'US')],
            locale: const Locale('fa', 'IR'),
            home: Scaffold(body: child),
          ),
        ),
      );

  /// Brings the engine to running and lets the pages load.
  Future<void> start(WidgetTester tester) async {
    unawaited(engine.start());
    await settle(tester);
    expect(engine.state, EngineState.running);
  }

  /// Stops the engine (cancels its poll timer), drops toasts (their
  /// countdown timers) and disposes the providers.
  Future<void> dispose(WidgetTester tester) async {
    debugResetTopNotices();
    await engine.stop();
    await tester.pumpWidget(const SizedBox.shrink());
    apis.dispose();
    engine.dispose();
  }
}

/// A few frames: enough for the fake transport's futures and rebuilds,
/// without waiting on the engine's periodic health poll.
Future<void> settle(WidgetTester tester, [int frames = 6]) async {
  for (int i = 0; i < frames; i++) {
    await tester.pump(const Duration(milliseconds: 10));
  }
}
