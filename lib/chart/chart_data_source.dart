import '../models/chart_models.dart';
import '../models/market_data.dart';
import '../services/engine_api.dart';

/// Everything the chart reads from the engine. Implemented by
/// [EngineChartDataSource] in the app and by an in-memory fake in tests.
///
/// All times are UTC. `from`/`to` null means "engine default" (last 30 days
/// of data). Failures are [EngineApiException]s: Persian `messageFa` (the
/// engine's own `message_fa` for the chart/update routes), `code`,
/// `errorsFa`, and `kind == connection` when the engine is unreachable.
abstract interface class ChartDataSource {
  /// `GET /symbols`.
  Future<List<SymbolItem>> symbols();

  /// `GET /rates?symbol&timeframe&from&to`.
  Future<RatesResult> rates(String symbol, ChartTimeframe timeframe, {DateTime? from, DateTime? to});

  /// `GET /chart/channel?symbol&timeframe&from&to`.
  Future<ChannelResult> channel(String symbol, ChartTimeframe timeframe, {DateTime? from, DateTime? to});

  /// `GET /chart/setups?symbol&from&to` (H1 setups, filtered by decision time).
  Future<SetupsResult> setups(String symbol, {DateTime? from, DateTime? to});

  /// `GET /rates/gaps?symbol&timeframe` (full cached history).
  Future<GapsResult> gaps(String symbol, ChartTimeframe timeframe);

  /// `GET /rates/meta?symbol&timeframe`.
  Future<RatesMeta> ratesMeta(String symbol, ChartTimeframe timeframe);

  /// `POST /rates/update` for H1 and H4 of [symbol] (read-only MT5 fetch
  /// into the local cache; never an order).
  Future<RatesUpdateResult> update(String symbol);
}

/// [ChartDataSource] over the running engine's REST API.
class EngineChartDataSource implements ChartDataSource {
  const EngineChartDataSource(this.api);

  final EngineApi api;

  @override
  Future<List<SymbolItem>> symbols() async => (await api.getSymbols()).symbols;

  @override
  Future<RatesResult> rates(String symbol, ChartTimeframe timeframe, {DateTime? from, DateTime? to}) =>
      api.getRates(symbol: symbol, timeframe: timeframe.code, from: from, to: to);

  @override
  Future<ChannelResult> channel(String symbol, ChartTimeframe timeframe, {DateTime? from, DateTime? to}) =>
      api.getChartChannel(symbol: symbol, timeframe: timeframe.code, from: from, to: to);

  @override
  Future<SetupsResult> setups(String symbol, {DateTime? from, DateTime? to}) =>
      api.getChartSetups(symbol: symbol, from: from, to: to);

  @override
  Future<GapsResult> gaps(String symbol, ChartTimeframe timeframe) =>
      api.getRatesGaps(symbol: symbol, timeframe: timeframe.code);

  @override
  Future<RatesMeta> ratesMeta(String symbol, ChartTimeframe timeframe) =>
      api.getRatesMeta(symbol: symbol, timeframe: timeframe.code);

  @override
  Future<RatesUpdateResult> update(String symbol) => api.postRatesUpdate(symbol: symbol);
}
