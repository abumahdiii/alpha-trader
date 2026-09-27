import 'chart_models.dart';

/// Why a chart call failed, in the terms the chart UI needs.
enum ChartErrorKind {
  /// The engine is not reachable (not started, crashed, timed out).
  engineUnavailable,

  /// The engine answered with an HTTP error ([ChartDataException.statusCode]).
  http,

  /// The body did not match the contract.
  malformed,
}

/// Error surfaced by a [ChartDataSource]. [messageFa] is shown as-is; for the
/// chart and update routes it is the engine's own Persian `message_fa`
/// (`{"detail": {"code", "message_fa", "errors_fa"}}`), for `/rates` and
/// `/rates/gaps` (English string details) the adapter supplies a Persian one.
class ChartDataException implements Exception {
  const ChartDataException(
    this.kind,
    this.messageFa, {
    this.statusCode,
    this.code,
    this.errorsFa = const <String>[],
    this.detail,
  });

  final ChartErrorKind kind;
  final String messageFa;
  final int? statusCode;

  /// Engine error code (`no_cache`, `mt5_unavailable`, ...), when it sent one.
  final String? code;
  final List<String> errorsFa;

  /// Raw non-Persian detail for DEV_MODE logs only.
  final String? detail;

  @override
  String toString() => 'ChartDataException(${kind.name}'
      '${statusCode != null ? ', HTTP $statusCode' : ''}${code != null ? ', $code' : ''}): $messageFa';
}

/// Everything the chart reads from the engine. Implemented against
/// `EngineApi` in production and by an in-memory fake in tests.
///
/// All times are UTC. `from`/`to` null means "engine default" (last 30 days
/// of data). Implementations throw [ChartDataException] only.
abstract interface class ChartDataSource {
  /// `GET /symbols`.
  Future<List<ChartSymbol>> symbols();

  /// `GET /rates?symbol&timeframe&from&to`.
  Future<RatesResult> rates(String symbol, ChartTimeframe timeframe, {DateTime? from, DateTime? to});

  /// `GET /chart/channel?symbol&timeframe&from&to`.
  Future<ChannelResult> channel(String symbol, ChartTimeframe timeframe, {DateTime? from, DateTime? to});

  /// `GET /chart/setups?symbol&from&to` (H1 setups).
  Future<SetupsResult> setups(String symbol, {DateTime? from, DateTime? to});

  /// `GET /rates/gaps?symbol&timeframe` (full cached history).
  Future<GapsResult> gaps(String symbol, ChartTimeframe timeframe);

  /// `GET /rates/meta?symbol&timeframe`.
  Future<RatesMeta> ratesMeta(String symbol, ChartTimeframe timeframe);

  /// `POST /rates/update` for H1 and H4 of [symbol] (read-only MT5 fetch
  /// into the local cache; never an order).
  Future<RatesUpdateResult> update(String symbol);
}
