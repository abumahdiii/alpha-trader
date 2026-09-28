import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/account_settings.dart';
import '../models/backtest_models.dart';
import '../models/chart_models.dart';
import '../models/json_reader.dart';
import '../models/market_data.dart';
import '../models/strategy.dart';

/// Why an [EngineApi] call failed.
enum EngineApiErrorKind {
  /// No answer within the call's time budget.
  timeout,

  /// Nothing listening on the port / connection dropped.
  connection,

  /// HTTP 422: the engine rejected the input; [EngineApiException.errorsFa]
  /// holds its Persian per-field messages.
  validation,

  /// Any other non-2xx answer (404, 409, 503, 500, ...).
  badStatus,

  /// 2xx, but the body does not match the contract this UI expects.
  malformedBody,
  unknown,
}

/// A failed engine call, carrying what the user should read.
///
/// [messageFa] is always Persian: the engine's own `detail.message_fa`
/// when it sent one, else a generic Persian sentence for [kind]/[statusCode].
/// [errorsFa] are the engine's `detail.errors_fa` (one Persian sentence per
/// problem; they name the field as `«label»` and, for strategy params, as
/// `(name)`).
class EngineApiException implements Exception {
  const EngineApiException(
    this.kind,
    this.messageFa, {
    this.statusCode,
    this.code,
    this.errorsFa = const [],
    this.detail,
    this.cause,
  });

  final EngineApiErrorKind kind;
  final String messageFa;
  final int? statusCode;

  /// Engine error code (`invalid_settings`, `invalid_params`,
  /// `strategy_not_found`, `db_unavailable`, ...), when it sent one.
  final String? code;
  final List<String> errorsFa;

  /// Non-Persian technical detail (a plain-string `detail`, FastAPI's
  /// request-validation messages, the exception text). Shown only as a
  /// secondary line.
  final String? detail;
  final Object? cause;

  bool get isValidation => kind == EngineApiErrorKind.validation;

  /// Assigns each of [errorsFa] to the first field whose marker it contains
  /// (e.g. `«اهرم»` for a setting, `(sigma_ddof)` for a strategy param);
  /// the rest (unknown keys, cross-field rules) go to `general`.
  ({Map<String, List<String>> byField, List<String> general}) splitErrors(
    Map<String, List<String>> markersByField,
  ) {
    final Map<String, List<String>> byField = {};
    final List<String> general = [];
    for (final String message in errorsFa) {
      String? field;
      for (final MapEntry<String, List<String>> e in markersByField.entries) {
        if (e.value.any(message.contains)) {
          field = e.key;
          break;
        }
      }
      if (field == null) {
        general.add(message);
      } else {
        (byField[field] ??= []).add(message);
      }
    }
    return (byField: byField, general: general);
  }

  /// Everything the user should see, one sentence per line.
  String get userMessage => [
        messageFa,
        ...errorsFa,
        if (detail != null && detail!.isNotEmpty && errorsFa.isEmpty) detail!,
      ].join('\n');

  @override
  String toString() => 'EngineApiException(${kind.name}'
      '${statusCode != null ? ', HTTP $statusCode' : ''}'
      '${code != null ? ', $code' : ''}): $messageFa'
      '${errorsFa.isNotEmpty ? ' $errorsFa' : ''}'
      '${detail != null ? ' | $detail' : ''}';
}

/// Builds an API client bound to one engine port (injected in tests).
typedef EngineApiFactory = EngineApi Function(int port);

/// REST client for the engine's data routes (`/symbols`, `/rates/meta`,
/// `/settings`, `/strategies`), bound to `http://127.0.0.1:<port>`.
///
/// Deliberately separate from `EngineClient`, which only does the tight
/// lifecycle calls (`/health`, `/shutdown`, 2 s budget): these calls can
/// take longer (an MT5 refresh behind `/rates`), send query parameters and
/// JSON bodies, and must surface the engine's Persian error messages.
///
/// Adding an endpoint is one method: build the path/query/body and hand a
/// parser to [_call], e.g.
/// ```dart
/// Future<Foo> getFoo(String symbol) => _call('GET', '/foo',
///     query: {'symbol': symbol}, timeout: slowTimeout, parse: Foo.fromJson);
/// ```
///
/// Tests inject an [HttpClientAdapter] so no socket is ever opened.
class EngineApi {
  EngineApi({
    required this.port,
    this.defaultTimeout = defaultCallTimeout,
    HttpClientAdapter? httpClientAdapter,
  }) : _dio = Dio(
          BaseOptions(
            baseUrl: 'http://127.0.0.1:$port',
            connectTimeout: defaultTimeout,
            receiveTimeout: defaultTimeout,
            sendTimeout: defaultTimeout,
            // Plain text + manual status check: the error body must be read
            // (Persian messages) and a bad body must become malformedBody.
            responseType: ResponseType.plain,
            validateStatus: (_) => true,
          ),
        ) {
    if (httpClientAdapter != null) _dio.httpClientAdapter = httpClientAdapter;
  }

  static const Duration defaultCallTimeout = Duration(seconds: 10);

  /// For calls that may wait on MT5 (a `/rates` refresh, chart scans).
  static const Duration slowCallTimeout = Duration(seconds: 60);

  final int port;
  final Duration defaultTimeout;
  final Dio _dio;

  // ---------------------------------------------------------------- market data

  /// `GET /symbols` -- configured symbols with their contract spec.
  Future<SymbolsResponse> getSymbols({Duration? timeout}) =>
      _call('GET', '/symbols', timeout: timeout, parse: SymbolsResponse.fromJson);

  /// `GET /rates/meta?symbol&timeframe` -- what the cache holds.
  Future<RatesMeta> getRatesMeta({
    required String symbol,
    required String timeframe,
    Duration? timeout,
  }) =>
      _call(
        'GET',
        '/rates/meta',
        query: {'symbol': symbol, 'timeframe': timeframe},
        timeout: timeout,
        parse: RatesMeta.fromJson,
      );

  /// `GET /rates?symbol&timeframe&from&to` -- cached bars of a window (the
  /// engine may first append closed bars from MT5, hence the slow budget).
  Future<RatesResult> getRates({
    required String symbol,
    required String timeframe,
    DateTime? from,
    DateTime? to,
    Duration? timeout,
  }) =>
      _call(
        'GET',
        '/rates',
        query: {'symbol': symbol, 'timeframe': timeframe, ..._range(from, to)},
        timeout: timeout ?? slowCallTimeout,
        parse: RatesResult.fromJson,
      );

  /// `GET /rates/gaps?symbol&timeframe` -- every gap of the whole cached series.
  Future<GapsResult> getRatesGaps({required String symbol, required String timeframe, Duration? timeout}) => _call(
        'GET',
        '/rates/gaps',
        query: {'symbol': symbol, 'timeframe': timeframe},
        timeout: timeout,
        parse: GapsResult.fromJson,
      );

  /// `POST /rates/update` -- incremental, read-only MT5 fetch into the cache
  /// ([timeframe] null = H1 and H4). 409 `no_cache` / `not_incremental` /
  /// `offset_model_changed`, 503 `mt5_unavailable` / `update_failed`.
  Future<RatesUpdateResult> postRatesUpdate({required String symbol, String? timeframe, Duration? timeout}) => _call(
        'POST',
        '/rates/update',
        body: {'symbol': symbol, if (timeframe != null) 'timeframe': timeframe},
        timeout: timeout ?? slowCallTimeout,
        parse: RatesUpdateResult.fromJson,
      );

  // --------------------------------------------------------------------- chart

  /// `GET /chart/channel?symbol&timeframe&from&to` -- the strategy's channel
  /// lines per bar (cache only; first call computes the full history).
  Future<ChannelResult> getChartChannel({
    required String symbol,
    required String timeframe,
    DateTime? from,
    DateTime? to,
    Duration? timeout,
  }) =>
      _call(
        'GET',
        '/chart/channel',
        query: {'symbol': symbol, 'timeframe': timeframe, ..._range(from, to)},
        timeout: timeout ?? slowCallTimeout,
        parse: ChannelResult.fromJson,
      );

  /// `GET /chart/setups?symbol&from&to` -- scanned setups by decision time.
  /// Always pass a window: the full-history list is heavy.
  Future<SetupsResult> getChartSetups({
    required String symbol,
    DateTime? from,
    DateTime? to,
    Duration? timeout,
  }) =>
      _call(
        'GET',
        '/chart/setups',
        query: {'symbol': symbol, ..._range(from, to)},
        timeout: timeout ?? slowCallTimeout,
        parse: SetupsResult.fromJson,
      );

  /// `from`/`to` query parameters as ISO-8601 UTC (`...Z`).
  static Map<String, Object> _range(DateTime? from, DateTime? to) => {
        if (from != null) 'from': from.toUtc().toIso8601String(),
        if (to != null) 'to': to.toUtc().toIso8601String(),
      };

  // ------------------------------------------------------------------ settings

  /// `GET /settings`.
  Future<AccountSettings> getSettings({Duration? timeout}) =>
      _call('GET', '/settings', timeout: timeout, parse: AccountSettings.fromJson);

  /// `PUT /settings` -- partial update: only the keys in [changes] are
  /// changed (see [AccountSettings.changesFrom]). Returns the new settings.
  /// Invalid values -> [EngineApiErrorKind.validation] with Persian
  /// `errorsFa`.
  Future<AccountSettings> putSettings(Map<String, Object> changes, {Duration? timeout}) =>
      _call('PUT', '/settings', body: changes, timeout: timeout, parse: AccountSettings.fromJson);

  // ---------------------------------------------------------------- strategies

  /// `GET /strategies` -- every registered strategy with its active params.
  Future<List<StrategyInfo>> listStrategies({Duration? timeout}) => _call(
        'GET',
        '/strategies',
        timeout: timeout,
        parse: (Object? json) {
          if (json is! List) {
            throw FormatException('strategies is not a JSON list: ${json.runtimeType}');
          }
          return List<StrategyInfo>.unmodifiable(json.map(StrategyInfo.fromJson));
        },
      );

  /// `GET /strategies/{name}`.
  Future<StrategyInfo> getStrategy(String name, {Duration? timeout}) => _call(
        'GET',
        '/strategies/${Uri.encodeComponent(name)}',
        timeout: timeout,
        parse: StrategyInfo.fromJson,
      );

  /// `PUT /strategies/{name}` with `{"params": params}`. FULL replacement:
  /// keys left out take their schema defaults, so an empty map resets the
  /// strategy to its defaults. A different set becomes params version n+1.
  Future<StrategyUpdateResult> putStrategyParams(
    String name,
    Map<String, Object> params, {
    Duration? timeout,
  }) =>
      _call(
        'PUT',
        '/strategies/${Uri.encodeComponent(name)}',
        body: {'params': params},
        timeout: timeout,
        parse: StrategyUpdateResult.fromJson,
      );

  // ----------------------------------------------------------------- backtests

  /// Budget for result reads (a 5-year run: thousands of trades as JSON).
  static const Duration resultCallTimeout = Duration(seconds: 30);

  /// Most trades one `GET /backtests/{id}/trades` returns (engine `TRADES_LIMIT_MAX`).
  static const int maxTradesPerPage = 5000;

  /// `POST /backtests` -> 202 `{id, status: "queued", ...}`. The engine
  /// validates the period against the cache first (slow budget); a bad
  /// period is a 422 with a Persian `message_fa` (`window_too_early`,
  /// `window_beyond_data`, ...), bad fields a 422 with `errors_fa`.
  Future<BacktestSubmitResult> submitBacktest(BacktestRequest request, {Duration? timeout}) => _call(
        'POST',
        '/backtests',
        body: request.toJson(),
        timeout: timeout ?? slowCallTimeout,
        parse: BacktestSubmitResult.fromJson,
      );

  /// `GET /backtests?limit` -- newest first (engine: 1..500, default 50).
  Future<BacktestRunList> listBacktests({int? limit, Duration? timeout}) => _call(
        'GET',
        '/backtests',
        query: {if (limit != null) 'limit': limit},
        timeout: timeout,
        parse: BacktestRunList.fromJson,
      );

  /// `GET /backtests/{id}` -- config, plan, labels, metrics, windows, live progress.
  Future<BacktestRunDetail> getBacktest(int id, {Duration? timeout}) =>
      _call('GET', '/backtests/$id', timeout: timeout, parse: BacktestRunDetail.fromJson);

  /// `GET /backtests/{id}/trades?window&offset&limit` (limit 1..[maxTradesPerPage]).
  Future<BacktestTradesPage> getBacktestTrades(
    int id, {
    int? window,
    int offset = 0,
    int limit = maxTradesPerPage,
    Duration? timeout,
  }) =>
      _call(
        'GET',
        '/backtests/$id/trades',
        query: {if (window != null) 'window': window, 'offset': offset, 'limit': limit},
        timeout: timeout ?? resultCallTimeout,
        parse: BacktestTradesPage.fromJson,
      );

  /// `GET /backtests/{id}/equity?window` -- the stored (downsampled) curve.
  Future<BacktestEquity> getBacktestEquity(int id, {int? window, Duration? timeout}) => _call(
        'GET',
        '/backtests/$id/equity',
        query: {if (window != null) 'window': window},
        timeout: timeout ?? resultCallTimeout,
        parse: BacktestEquity.fromJson,
      );

  /// `GET /backtests/{id}/skipped?window` -- candidates not traded, with Persian reasons.
  Future<BacktestSkippedList> getBacktestSkipped(int id, {int? window, Duration? timeout}) => _call(
        'GET',
        '/backtests/$id/skipped',
        query: {if (window != null) 'window': window},
        timeout: timeout ?? resultCallTimeout,
        parse: BacktestSkippedList.fromJson,
      );

  /// `POST /backtests/{id}/cancel`; 409 `not_cancellable` when already finished.
  Future<BacktestCancelResult> cancelBacktest(int id, {Duration? timeout}) =>
      _call('POST', '/backtests/$id/cancel', timeout: timeout, parse: BacktestCancelResult.fromJson);

  /// `DELETE /backtests/{id}`; 409 `run_active` while queued/running.
  Future<void> deleteBacktest(int id, {Duration? timeout}) => _call(
        'DELETE',
        '/backtests/$id',
        timeout: timeout,
        parse: (Object? json) {
          final JsonReader r = JsonReader(json, 'backtest_delete');
          if (r.boolOrNull('deleted') != true) {
            throw const FormatException('backtest_delete.deleted is not true');
          }
        },
      );

  /// `ws://127.0.0.1:<port>/ws/backtests/{id}` -- the run's progress socket.
  Uri backtestProgressUri(int id) => Uri.parse('ws://127.0.0.1:$port/ws/backtests/$id');

  void close() => _dio.close(force: true);

  // ------------------------------------------------------------------ plumbing

  Future<T> _call<T>(
    String method,
    String path, {
    Map<String, Object>? query,
    Object? body,
    Duration? timeout,
    required T Function(Object? json) parse,
  }) async {
    final Duration limit = timeout ?? defaultTimeout;
    final String queryText = query == null || query.isEmpty
        ? ''
        : '?${Uri(queryParameters: {for (final e in query.entries) e.key: '${e.value}'}).query}';
    final String what = '$method $path$queryText';
    final String? encodedBody = body == null ? null : jsonEncode(body);
    _log('-> $what (timeout ${limit.inMilliseconds} ms)'
        '${encodedBody != null ? ' body=${_clip(encodedBody)}' : ''}');

    final Stopwatch watch = Stopwatch()..start();
    final CancelToken cancel = CancelToken();
    final Response<String> response;
    try {
      response = await _dio
          .request<String>(
        path,
        data: encodedBody,
        queryParameters: query,
        cancelToken: cancel,
        options: Options(
          method: method,
          receiveTimeout: limit,
          sendTimeout: limit,
          contentType: encodedBody != null ? Headers.jsonContentType : null,
        ),
      )
          // connectTimeout is a BaseOptions setting only; the per-call budget
          // is enforced here as a hard ceiling.
          .timeout(limit, onTimeout: () {
        cancel.cancel('timeout');
        throw TimeoutException(what, limit);
      });
    } on TimeoutException catch (e) {
      throw _logged(
        EngineApiException(
          EngineApiErrorKind.timeout,
          'موتور در ${_seconds(limit)} ثانیه پاسخ نداد.',
          detail: what,
          cause: e,
        ),
        watch,
      );
    } on DioException catch (e) {
      throw _logged(_fromDio(e, what, limit), watch);
    } catch (e) {
      throw _logged(
        EngineApiException(EngineApiErrorKind.unknown, 'درخواست به موتور ناموفق بود.', detail: '$what: $e', cause: e),
        watch,
      );
    }

    final int status = response.statusCode ?? 0;
    final String raw = response.data ?? '';
    _log('<- $what HTTP $status (${watch.elapsedMilliseconds} ms, ${raw.length} chars)');

    if (status < 200 || status >= 300) {
      _log('   error body: ${_clip(raw)}');
      throw _logged(errorFromResponse(status, raw), watch);
    }
    try {
      return parse(jsonDecode(raw));
    } on FormatException catch (e) {
      _log('   unparsable body: ${_clip(raw)}');
      throw _logged(
        EngineApiException(
          EngineApiErrorKind.malformedBody,
          'پاسخ موتور با این نسخه برنامه سازگار نیست.',
          statusCode: status,
          detail: '$what: ${e.message}',
          cause: e,
        ),
        watch,
      );
    }
  }

  /// Turns a non-2xx body into an [EngineApiException]. Handles the three
  /// shapes the engine sends:
  /// * `{"detail": {"code", "message_fa", "errors_fa": [...]}}` (strategies,
  ///   settings),
  /// * `{"detail": "text"}` (rates/symbols: bad symbol/timeframe, 404),
  /// * `{"detail": [{"loc", "msg", "type"}, ...]}` (FastAPI's own
  ///   request validation, e.g. a missing query parameter).
  static EngineApiException errorFromResponse(int status, String raw) {
    final EngineApiErrorKind kind = status == 422 ? EngineApiErrorKind.validation : EngineApiErrorKind.badStatus;
    Object? detail;
    try {
      final Object? decoded = jsonDecode(raw);
      detail = decoded is Map ? decoded['detail'] : null;
    } on FormatException {
      detail = null;
    }

    if (detail is Map) {
      final Object? message = detail['message_fa'];
      final Object? errors = detail['errors_fa'];
      final Object? code = detail['code'];
      return EngineApiException(
        kind,
        message is String && message.trim().isNotEmpty ? message : _genericFa(status),
        statusCode: status,
        code: code is String ? code : null,
        errorsFa: errors is List ? List<String>.unmodifiable(errors.map((Object? e) => '$e')) : const [],
      );
    }
    if (detail is String) {
      return EngineApiException(kind, _genericFa(status), statusCode: status, detail: detail);
    }
    if (detail is List) {
      final String text = detail
          .map((Object? e) => e is Map ? '${(e['loc'] as List?)?.join('.') ?? ''}: ${e['msg']}' : '$e')
          .join('; ');
      return EngineApiException(kind, _genericFa(status), statusCode: status, detail: text);
    }
    return EngineApiException(
      kind,
      _genericFa(status),
      statusCode: status,
      detail: raw.isEmpty ? null : _clip(raw),
    );
  }

  static String _genericFa(int status) => switch (status) {
        404 => 'موردی که از موتور خواسته شد پیدا نشد.',
        409 => 'داده ذخیره‌شده در موتور با نسخه فعلی سازگار نیست.',
        422 => 'موتور ورودی را نپذیرفت.',
        503 => 'این بخش موتور فعلاً در دسترس نیست.',
        _ => 'موتور با خطا پاسخ داد (HTTP $status).',
      };

  static EngineApiException _fromDio(DioException e, String what, Duration limit) {
    final String technical = '$what: ${e.message ?? e.error ?? e.type.name}';
    return switch (e.type) {
      DioExceptionType.connectionTimeout ||
      DioExceptionType.sendTimeout ||
      DioExceptionType.receiveTimeout ||
      DioExceptionType.cancel =>
        EngineApiException(
          EngineApiErrorKind.timeout,
          'موتور در ${_seconds(limit)} ثانیه پاسخ نداد.',
          detail: technical,
          cause: e,
        ),
      DioExceptionType.connectionError => EngineApiException(
          EngineApiErrorKind.connection,
          'اتصال به موتور برقرار نشد.',
          detail: technical,
          cause: e,
        ),
      _ when e.error is SocketException => EngineApiException(
          EngineApiErrorKind.connection,
          'اتصال به موتور برقرار نشد.',
          detail: technical,
          cause: e,
        ),
      _ => EngineApiException(
          EngineApiErrorKind.unknown,
          'درخواست به موتور ناموفق بود.',
          statusCode: e.response?.statusCode,
          detail: technical,
          cause: e,
        ),
    };
  }

  static String _seconds(Duration d) {
    final double s = d.inMilliseconds / 1000;
    return s == s.roundToDouble() ? '${s.round()}' : s.toStringAsFixed(1);
  }

  EngineApiException _logged(EngineApiException e, Stopwatch watch) {
    _log('xx $e (${watch.elapsedMilliseconds} ms)');
    return e;
  }

  static String _clip(String s) => s.length <= 800 ? s : '${s.substring(0, 800)}…';

  // Nothing secret goes through these routes (no credentials exist in the
  // UI at all), so paths, queries and bodies can be logged under DEV_MODE.
  void _log(String message) {
    if (!kDevMode) return;
    final String line = '[EngineApi :$port] $message';
    devLog(line);
    AppLogger.log(line);
  }
}
