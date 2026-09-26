import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../models/engine_health.dart';

/// Why an engine call failed. The provider/UI decide what to show from this;
/// nothing above this layer has to understand dio's exception types.
enum EngineClientErrorKind { timeout, connectionRefused, badStatus, malformedBody, unknown }

class EngineClientException implements Exception {
  const EngineClientException(this.kind, this.message, {this.statusCode, this.cause});

  final EngineClientErrorKind kind;
  final String message;
  final int? statusCode;
  final Object? cause;

  @override
  String toString() => 'EngineClientException(${kind.name}'
      '${statusCode != null ? ', HTTP $statusCode' : ''}): $message';
}

/// Builds a client bound to one engine port. Injected into the process
/// launcher and the status provider so tests can substitute a fake.
typedef EngineClientFactory = EngineClient Function(int port);

/// REST client for the local Python engine (`http://127.0.0.1:<port>`).
///
/// Short timeouts on purpose: the engine is on loopback, so anything slower
/// than ~2 s means it is busy starting, wedged, or gone — and the health
/// poll and window-close shutdown both have tight time budgets.
class EngineClient {
  EngineClient({
    required this.port,
    Duration timeout = defaultTimeout,
    HttpClientAdapter? httpClientAdapter,
  }) : _dio = Dio(
          BaseOptions(
            baseUrl: 'http://127.0.0.1:$port',
            connectTimeout: timeout,
            receiveTimeout: timeout,
            sendTimeout: timeout,
            // Decode JSON ourselves so a malformed body becomes a typed
            // malformedBody error instead of a generic dio failure.
            responseType: ResponseType.plain,
            // Status is checked by hand for the same reason.
            validateStatus: (_) => true,
          ),
        ) {
    if (httpClientAdapter != null) _dio.httpClientAdapter = httpClientAdapter;
  }

  static const Duration defaultTimeout = Duration(seconds: 2);

  final int port;
  final Dio _dio;

  /// `GET /health`.
  Future<EngineHealth> health({Duration? timeout}) async {
    final Object? body = await _request('GET', '/health', timeout: timeout);
    try {
      return EngineHealth.fromJson(body);
    } on FormatException catch (e) {
      throw EngineClientException(EngineClientErrorKind.malformedBody, e.message, cause: e);
    }
  }

  /// `POST /shutdown` — asks the engine to exit on its own.
  Future<void> shutdown({Duration? timeout}) async {
    await _request('POST', '/shutdown', timeout: timeout, decodeBody: false);
  }

  void close() => _dio.close(force: true);

  Future<Object?> _request(
    String method,
    String path, {
    Duration? timeout,
    bool decodeBody = true,
  }) async {
    final CancelToken cancel = CancelToken();
    final Duration limit = timeout ?? _dio.options.receiveTimeout ?? defaultTimeout;
    _log('[EngineClient] -> $method :$port$path (timeout ${limit.inMilliseconds} ms)');
    final Response<String> response;
    try {
      response = await _dio
          .request<String>(
            path,
            cancelToken: cancel,
            options: Options(method: method, receiveTimeout: limit, sendTimeout: limit),
          )
          // Hard ceiling: connectTimeout lives in BaseOptions only, so a
          // per-call budget (e.g. 1 s for shutdown) is enforced here.
          .timeout(limit, onTimeout: () {
        cancel.cancel('timeout');
        throw TimeoutException('$method $path', limit);
      });
    } on TimeoutException catch (e) {
      throw _logged(EngineClientException(EngineClientErrorKind.timeout, '$method $path timed out', cause: e));
    } on DioException catch (e) {
      throw _logged(_fromDio(e, '$method $path'));
    } catch (e) {
      throw _logged(EngineClientException(EngineClientErrorKind.unknown, '$method $path: $e', cause: e));
    }

    final int status = response.statusCode ?? 0;
    final String raw = response.data ?? '';
    _log('[EngineClient] <- $method :$port$path HTTP $status ${raw.length} chars'
        '${kDevMode ? ': ${_clip(raw)}' : ''}');
    if (status < 200 || status >= 300) {
      throw _logged(EngineClientException(
        EngineClientErrorKind.badStatus,
        '$method $path returned HTTP $status',
        statusCode: status,
      ));
    }
    if (!decodeBody) return null;
    try {
      return jsonDecode(raw);
    } on FormatException catch (e) {
      throw _logged(EngineClientException(
        EngineClientErrorKind.malformedBody,
        '$method $path: body is not valid JSON',
        statusCode: status,
        cause: e,
      ));
    }
  }

  static EngineClientException _fromDio(DioException e, String what) {
    final EngineClientErrorKind kind = switch (e.type) {
      DioExceptionType.connectionTimeout ||
      DioExceptionType.sendTimeout ||
      DioExceptionType.receiveTimeout =>
        EngineClientErrorKind.timeout,
      DioExceptionType.connectionError => EngineClientErrorKind.connectionRefused,
      DioExceptionType.badResponse => EngineClientErrorKind.badStatus,
      DioExceptionType.cancel => EngineClientErrorKind.timeout,
      _ when e.error is SocketException => EngineClientErrorKind.connectionRefused,
      _ => EngineClientErrorKind.unknown,
    };
    return EngineClientException(
      kind,
      '$what failed: ${e.message ?? e.error ?? e.type.name}',
      statusCode: e.response?.statusCode,
      cause: e,
    );
  }

  static EngineClientException _logged(EngineClientException e) {
    _log('[EngineClient] error: $e');
    return e;
  }

  static String _clip(String s) => s.length <= 500 ? s : '${s.substring(0, 500)}…';

  static void _log(String message) {
    if (!kDevMode) return;
    devLog(message);
    AppLogger.log(message);
  }
}
