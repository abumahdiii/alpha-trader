import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/services/engine_client.dart';

/// Minimal fake transport: no sockets, no mock packages.
class _FakeAdapter implements HttpClientAdapter {
  _FakeAdapter(this.handler);

  final Future<ResponseBody> Function(RequestOptions options) handler;
  final List<RequestOptions> requests = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) {
    requests.add(options);
    return handler(options);
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(Object body, [int status = 200]) => ResponseBody.fromString(
      body is String ? body : jsonEncode(body),
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );

Map<String, Object?> _healthBody({Object? mt5State = 'connected'}) => {
      'status': 'ok',
      'service': 'alpha_engine',
      'version': '0.1.0',
      'pid': 1234,
      'dev_mode': false,
      'time_utc': '2026-09-26T18:00:00Z',
      'mt5': {
        'state': mt5State,
        'server': 'SarmayeGozareBartar-Real',
        'login_masked': '****1234',
        'trade_mode': 'demo',
        'message': null,
      },
    };

EngineClient _client(_FakeAdapter adapter, {Duration timeout = const Duration(seconds: 2)}) =>
    EngineClient(port: 8765, timeout: timeout, httpClientAdapter: adapter);

Future<EngineClientException> _expectFailure(Future<Object?> call) async {
  try {
    await call;
  } on EngineClientException catch (e) {
    return e;
  }
  fail('expected EngineClientException');
}

void main() {
  test('healthy response is parsed field by field, time as UTC', () async {
    final adapter = _FakeAdapter((_) async => _json(_healthBody()));
    final EngineHealth h = await _client(adapter).health();

    expect(adapter.requests.single.method, 'GET');
    expect(adapter.requests.single.uri.toString(), 'http://127.0.0.1:8765/health');
    expect(h.status, 'ok');
    expect(h.service, 'alpha_engine');
    expect(h.isAlphaEngine, isTrue);
    expect(h.version, '0.1.0');
    expect(h.pid, 1234);
    expect(h.devMode, isFalse);
    expect(h.timeUtc, DateTime.utc(2026, 9, 26, 18));
    expect(h.timeUtc!.isUtc, isTrue);
    expect(h.mt5.state, Mt5State.connected);
    expect(h.mt5.server, 'SarmayeGozareBartar-Real');
    expect(h.mt5.loginMasked, '****1234');
    expect(h.mt5.tradeMode, 'demo');
    expect(h.mt5.message, isNull);
  });

  test('every mt5 state string maps to its enum, anything else to unknown', () async {
    const Map<Object?, Mt5State> cases = {
      'not_initialized': Mt5State.notInitialized,
      'connected': Mt5State.connected,
      'disconnected': Mt5State.disconnected,
      'error': Mt5State.error,
      'account_mismatch': Mt5State.accountMismatch,
      'something_new': Mt5State.unknown,
      null: Mt5State.unknown,
      42: Mt5State.unknown,
    };
    for (final MapEntry<Object?, Mt5State> c in cases.entries) {
      final adapter = _FakeAdapter((_) async => _json(_healthBody(mt5State: c.key)));
      final EngineHealth h = await _client(adapter).health();
      expect(h.mt5.state, c.value, reason: 'state=${c.key}');
    }
  });

  test('null and missing fields are tolerated', () async {
    final adapter = _FakeAdapter((_) async => _json({
          'status': 'ok',
          'service': 'alpha_engine',
          'version': null,
          'pid': null,
          'dev_mode': null,
          'time_utc': null,
          'mt5': {
            'state': 'not_initialized',
            'server': null,
            'login_masked': null,
            'trade_mode': null,
            'message': null,
          },
        }));
    final EngineHealth h = await _client(adapter).health();
    expect(h.version, isNull);
    expect(h.pid, isNull);
    expect(h.devMode, isNull);
    expect(h.timeUtc, isNull);
    expect(h.mt5.state, Mt5State.notInitialized);
    expect(h.mt5.server, isNull);
    expect(h.mt5.loginMasked, isNull);

    final bare = await _client(_FakeAdapter((_) async => _json({'status': 'ok'}))).health();
    expect(bare.service, isNull);
    expect(bare.isAlphaEngine, isFalse);
    expect(bare.mt5.state, Mt5State.unknown);
  });

  test('time_utc without zone is read as UTC, offsets are normalised to UTC', () {
    expect(EngineHealth.parseUtc('2026-09-26T18:00:00'), DateTime.utc(2026, 9, 26, 18));
    expect(EngineHealth.parseUtc('2026-09-26T21:30:00+03:30'), DateTime.utc(2026, 9, 26, 18));
    expect(EngineHealth.parseUtc('2026-09-26'), DateTime.utc(2026, 9, 26));
    expect(EngineHealth.parseUtc('not a date'), isNull);
  });

  test('malformed JSON body -> EngineClientException(malformedBody)', () async {
    final adapter = _FakeAdapter((_) async => ResponseBody.fromString('{"status": "ok"', 200));
    final e = await _expectFailure(_client(adapter).health());
    expect(e.kind, EngineClientErrorKind.malformedBody);
  });

  test('JSON that is not an object -> EngineClientException(malformedBody)', () async {
    final adapter = _FakeAdapter((_) async => _json('[1, 2, 3]'));
    final e = await _expectFailure(_client(adapter).health());
    expect(e.kind, EngineClientErrorKind.malformedBody);
  });

  test('HTTP 500 -> EngineClientException(badStatus) with the code', () async {
    final adapter = _FakeAdapter((_) async => _json({'detail': 'boom'}, 500));
    final e = await _expectFailure(_client(adapter).health());
    expect(e.kind, EngineClientErrorKind.badStatus);
    expect(e.statusCode, 500);
  });

  test('no answer within the timeout -> EngineClientException(timeout)', () async {
    final adapter = _FakeAdapter((_) => Completer<ResponseBody>().future);
    final sw = Stopwatch()..start();
    final e = await _expectFailure(
      _client(adapter, timeout: const Duration(milliseconds: 100)).health(),
    );
    expect(e.kind, EngineClientErrorKind.timeout);
    expect(sw.elapsed, lessThan(const Duration(seconds: 2)));
  });

  test('per-call timeout overrides the client default', () async {
    final adapter = _FakeAdapter((_) => Completer<ResponseBody>().future);
    final e = await _expectFailure(
      _client(adapter).shutdown(timeout: const Duration(milliseconds: 50)),
    );
    expect(e.kind, EngineClientErrorKind.timeout);
  });

  test('connection refused -> EngineClientException(connectionRefused)', () async {
    final adapter = _FakeAdapter((o) async => throw DioException.connectionError(
          requestOptions: o,
          reason: 'refused',
          error: const SocketException('Connection refused'),
        ));
    final e = await _expectFailure(_client(adapter).health());
    expect(e.kind, EngineClientErrorKind.connectionRefused);

    final raw = _FakeAdapter((_) async => throw const SocketException('Connection refused'));
    final e2 = await _expectFailure(_client(raw).health());
    expect(e2.kind, EngineClientErrorKind.connectionRefused);
  });

  test('shutdown() sends POST /shutdown', () async {
    final adapter = _FakeAdapter((_) async => _json({'status': 'shutting_down'}));
    await _client(adapter).shutdown();
    expect(adapter.requests.single.method, 'POST');
    expect(adapter.requests.single.path, '/shutdown');
    expect(adapter.requests.single.uri.toString(), 'http://127.0.0.1:8765/shutdown');
  });

  test('client is configured with connect/receive timeouts', () async {
    RequestOptions? seen;
    final adapter = _FakeAdapter((o) async {
      seen = o;
      return _json(_healthBody());
    });
    await _client(adapter, timeout: const Duration(milliseconds: 1500)).health();
    expect(seen!.connectTimeout, const Duration(milliseconds: 1500));
    expect(seen!.receiveTimeout, const Duration(milliseconds: 1500));
  });
}
