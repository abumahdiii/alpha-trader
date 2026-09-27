import 'package:flutter/foundation.dart';

import 'engine_health.dart';
import 'json_reader.dart';

/// Contract specification of a symbol: mirrors the engine's `SymbolSpec`
/// (`engine/alpha_engine/data/symbols.py`, from MT5 `symbol_info`).
/// Display-only here; sizing math stays in the engine.
@immutable
class SymbolSpec {
  const SymbolSpec({
    required this.name,
    required this.digits,
    required this.point,
    required this.tradeContractSize,
    required this.tradeTickValue,
    required this.tradeTickSize,
    required this.volumeMin,
    required this.volumeStep,
    required this.volumeMax,
    required this.currencyProfit,
    required this.currencyBase,
    this.description = '',
  });

  final String name;

  /// Price precision, for formatting prices like the terminal does.
  final int digits;
  final double point;
  final double tradeContractSize;
  final double tradeTickValue;
  final double tradeTickSize;
  final double volumeMin;
  final double volumeStep;
  final double volumeMax;
  final String currencyProfit;
  final String currencyBase;
  final String description;

  factory SymbolSpec.fromJson(JsonReader r) => SymbolSpec(
        name: r.str('name'),
        digits: r.integer('digits'),
        point: r.number('point'),
        tradeContractSize: r.number('trade_contract_size'),
        tradeTickValue: r.number('trade_tick_value'),
        tradeTickSize: r.number('trade_tick_size'),
        volumeMin: r.number('volume_min'),
        volumeStep: r.number('volume_step'),
        volumeMax: r.number('volume_max'),
        currencyProfit: r.str('currency_profit'),
        currencyBase: r.str('currency_base'),
        description: r.strOrNull('description') ?? '',
      );
}

/// Where a symbol's spec came from (`SymbolItem.source`).
enum SymbolSource {
  mt5,
  cache,
  none,
  unknown;

  static SymbolSource parse(String raw) => switch (raw) {
        'mt5' => SymbolSource.mt5,
        'cache' => SymbolSource.cache,
        'none' => SymbolSource.none,
        _ => SymbolSource.unknown,
      };
}

/// One configured symbol: mirrors `SymbolItem` in
/// `engine/alpha_engine/routes/symbols.py`.
@immutable
class SymbolItem {
  const SymbolItem({
    required this.symbol,
    required this.source,
    this.spec,
    this.fetchedAtUtc,
    this.error,
  });

  final String symbol;
  final SymbolSource source;

  /// Null when neither MT5 nor the cache had it ([source] == none).
  final SymbolSpec? spec;
  final DateTime? fetchedAtUtc;
  final String? error;

  factory SymbolItem.fromJson(JsonReader r) {
    final JsonReader? spec = r.objectOrNull('spec');
    return SymbolItem(
      symbol: r.str('symbol'),
      source: SymbolSource.parse(r.str('source')),
      spec: spec == null ? null : SymbolSpec.fromJson(spec),
      fetchedAtUtc: r.utcOrNull('fetched_at_utc'),
      error: r.strOrNull('error'),
    );
  }
}

/// `GET /symbols`: mirrors `SymbolsResponse` in `routes/symbols.py`.
@immutable
class SymbolsResponse {
  const SymbolsResponse({required this.mt5State, required this.symbols});

  final Mt5State mt5State;
  final List<SymbolItem> symbols;

  factory SymbolsResponse.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'symbols');
    return SymbolsResponse(
      mt5State: Mt5State.parse(r.str('mt5_state')),
      symbols: r.objects('symbols', SymbolItem.fromJson),
    );
  }
}

/// `GET /rates/meta`: what the engine's OHLCV cache holds for one
/// symbol/timeframe. Mirrors `RatesMetaResponse` in `routes/rates.py`.
/// All times are UTC; convert with `toLocal()` only for display.
@immutable
class RatesMeta {
  const RatesMeta({
    required this.symbol,
    required this.timeframe,
    required this.cached,
    required this.rows,
    this.firstBarUtc,
    this.lastBarUtc,
    this.firstAvailableUtc,
    this.requestedStartUtc,
    this.historyShort,
    this.offsetModel,
    this.source,
    this.fetchedAtUtc,
  });

  final String symbol;

  /// `H1` or `H4`.
  final String timeframe;
  final bool cached;
  final int rows;
  final DateTime? firstBarUtc;
  final DateTime? lastBarUtc;
  final DateTime? firstAvailableUtc;
  final DateTime? requestedStartUtc;
  final bool? historyShort;

  /// Broker-time model, e.g. `us_dst(2)`.
  final String? offsetModel;

  /// `mt5` or `seed`.
  final String? source;
  final DateTime? fetchedAtUtc;

  factory RatesMeta.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'rates_meta');
    return RatesMeta(
      symbol: r.str('symbol'),
      timeframe: r.str('timeframe'),
      cached: r.boolean('cached'),
      rows: r.integer('rows'),
      firstBarUtc: r.utcOrNull('first_bar_utc'),
      lastBarUtc: r.utcOrNull('last_bar_utc'),
      firstAvailableUtc: r.utcOrNull('first_available_utc'),
      requestedStartUtc: r.utcOrNull('requested_start_utc'),
      historyShort: r.boolOrNull('history_short'),
      offsetModel: r.strOrNull('offset_model'),
      source: r.strOrNull('source'),
      fetchedAtUtc: r.utcOrNull('fetched_at_utc'),
    );
  }
}
