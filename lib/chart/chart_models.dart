// Typed mirrors of the engine's chart / market-data responses.
//
// Each class names the pydantic model it mirrors (engine/alpha_engine/...).
// Field names map 1:1 (snake_case -> camelCase); `T | None` in Python is a
// nullable Dart field. Nothing here computes trading values: prices, channel
// lines, levels and volumes are exactly what the engine sent.

import 'package:flutter/foundation.dart';

import 'json_read.dart';

/// `H1` / `H4` — the only timeframes the chart routes accept.
enum ChartTimeframe {
  h1('H1', Duration(hours: 1)),
  h4('H4', Duration(hours: 4));

  const ChartTimeframe(this.code, this.duration);

  final String code;
  final Duration duration;

  static ChartTimeframe parse(String code) => switch (code) {
        'H1' => ChartTimeframe.h1,
        'H4' => ChartTimeframe.h4,
        _ => throw FormatException('unsupported timeframe "$code"'),
      };
}

// ------------------------------------------------------------------ /rates

/// One OHLCV bar. Mirrors `Bar` (routes/rates.py:40-49).
@immutable
class Candle {
  const Candle({
    required this.time,
    required this.serverTime,
    required this.open,
    required this.high,
    required this.low,
    required this.close,
    required this.tickVolume,
    required this.spread,
    required this.realVolume,
  });

  /// Bar open time, UTC (`time`).
  final DateTime time;

  /// Bar open on the broker server clock, MT5 Data Window style
  /// (`"2025.01.06 01:00"`); null when the cache has no offset model.
  final String? serverTime;
  final double open;
  final double high;
  final double low;
  final double close;
  final int tickVolume;
  final int spread;
  final int realVolume;

  bool get isBull => close >= open;

  factory Candle.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'bar');
    return Candle(
      time: reqUtc(m, 'time'),
      serverTime: optString(m, 'server_time'),
      open: reqDouble(m, 'open'),
      high: reqDouble(m, 'high'),
      low: reqDouble(m, 'low'),
      close: reqDouble(m, 'close'),
      tickVolume: reqInt(m, 'tick_volume'),
      spread: reqInt(m, 'spread'),
      realVolume: reqInt(m, 'real_volume'),
    );
  }
}

/// Kinds of `data.gaps.GapKind` (data/gaps.py:62).
enum GapKind {
  weekend('weekend', 'آخر هفته'),
  holiday('holiday', 'تعطیلی'),
  sessionBreak('session_break', 'وقفه روزانه'),
  missing('missing', 'کندل گم‌شده');

  const GapKind(this.code, this.titleFa);

  final String code;
  final String titleFa;

  static GapKind parse(String code) => GapKind.values.firstWhere(
        (GapKind k) => k.code == code,
        orElse: () => throw FormatException('unknown gap kind "$code"'),
      );
}

/// A hole in the cached series. Mirrors `Gap` (data/gaps.py:70-78).
@immutable
class Gap {
  const Gap({
    required this.kind,
    required this.after,
    required this.before,
    required this.start,
    required this.missingBars,
    required this.durationHours,
  });

  final GapKind kind;

  /// Open time of the last bar before the gap.
  final DateTime after;

  /// Open time of the first bar after the gap.
  final DateTime before;

  /// Open time of the first missing slot (= after + timeframe).
  final DateTime start;
  final int missingBars;
  final double durationHours;

  factory Gap.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'gap');
    return Gap(
      kind: GapKind.parse(reqString(m, 'kind')),
      after: reqUtc(m, 'after'),
      before: reqUtc(m, 'before'),
      start: reqUtc(m, 'start'),
      missingBars: reqInt(m, 'missing_bars'),
      durationHours: reqDouble(m, 'duration_hours'),
    );
  }
}

/// `GET /rates`. Mirrors `RatesResponse` (routes/rates.py:52-66); `from` is
/// the serialization alias of `from_`.
@immutable
class RatesResult {
  const RatesResult({
    required this.symbol,
    required this.timeframe,
    required this.from,
    required this.to,
    required this.source,
    required this.stale,
    required this.offsetModel,
    required this.count,
    required this.bars,
    required this.gaps,
    required this.gapCounts,
    this.message,
  });

  final String symbol;
  final String timeframe;

  /// Effective window (ISO UTC strings; may be empty when the engine had none).
  final String from;
  final String to;

  /// `cache` or `mt5`.
  final String source;
  final bool stale;
  final String? offsetModel;
  final int count;
  final List<Candle> bars;
  final List<Gap> gaps;
  final Map<String, int> gapCounts;
  final String? message;

  factory RatesResult.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'rates');
    return RatesResult(
      symbol: reqString(m, 'symbol'),
      timeframe: reqString(m, 'timeframe'),
      from: reqString(m, 'from'),
      to: reqString(m, 'to'),
      source: reqString(m, 'source'),
      stale: reqBool(m, 'stale'),
      offsetModel: optString(m, 'offset_model'),
      count: reqInt(m, 'count'),
      bars: reqList(m, 'bars', Candle.fromJson),
      gaps: reqList(m, 'gaps', Gap.fromJson),
      gapCounts: reqIntMap(m, 'gap_counts'),
      message: optString(m, 'message'),
    );
  }
}

/// `GET /rates/meta`. Mirrors `RatesMetaResponse` (routes/rates.py:69-83).
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
  final String timeframe;
  final bool cached;
  final int rows;
  final DateTime? firstBarUtc;
  final DateTime? lastBarUtc;
  final DateTime? firstAvailableUtc;
  final DateTime? requestedStartUtc;
  final bool? historyShort;
  final String? offsetModel;

  /// `mt5` or `seed` (test data).
  final String? source;
  final DateTime? fetchedAtUtc;

  factory RatesMeta.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'rates meta');
    return RatesMeta(
      symbol: reqString(m, 'symbol'),
      timeframe: reqString(m, 'timeframe'),
      cached: reqBool(m, 'cached'),
      rows: reqInt(m, 'rows'),
      firstBarUtc: optUtc(m, 'first_bar_utc'),
      lastBarUtc: optUtc(m, 'last_bar_utc'),
      firstAvailableUtc: optUtc(m, 'first_available_utc'),
      requestedStartUtc: optUtc(m, 'requested_start_utc'),
      historyShort: optBool(m, 'history_short'),
      offsetModel: optString(m, 'offset_model'),
      source: optString(m, 'source'),
      fetchedAtUtc: optUtc(m, 'fetched_at_utc'),
    );
  }
}

/// `GET /rates/gaps`. Mirrors `RatesGapsResponse` (routes/rates.py:201-215).
@immutable
class GapsResult {
  const GapsResult({
    required this.symbol,
    required this.timeframe,
    required this.cached,
    required this.rows,
    this.firstBarUtc,
    this.lastBarUtc,
    this.offsetModel,
    required this.count,
    required this.gaps,
    required this.gapCounts,
    required this.missingBarsTotal,
    required this.sessionBreakSlots,
  });

  final String symbol;
  final String timeframe;
  final bool cached;
  final int rows;
  final DateTime? firstBarUtc;
  final DateTime? lastBarUtc;
  final String? offsetModel;
  final int count;
  final List<Gap> gaps;
  final Map<String, int> gapCounts;
  final int missingBarsTotal;
  final List<String> sessionBreakSlots;

  factory GapsResult.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'rates gaps');
    return GapsResult(
      symbol: reqString(m, 'symbol'),
      timeframe: reqString(m, 'timeframe'),
      cached: reqBool(m, 'cached'),
      rows: reqInt(m, 'rows'),
      firstBarUtc: optUtc(m, 'first_bar_utc'),
      lastBarUtc: optUtc(m, 'last_bar_utc'),
      offsetModel: optString(m, 'offset_model'),
      count: reqInt(m, 'count'),
      gaps: reqList(m, 'gaps', Gap.fromJson),
      gapCounts: reqIntMap(m, 'gap_counts'),
      missingBarsTotal: reqInt(m, 'missing_bars_total'),
      sessionBreakSlots: reqStringList(m, 'session_break_slots'),
    );
  }
}

/// One timeframe of `POST /rates/update`. Mirrors `TimeframeUpdate`
/// (routes/rates.py:265-273).
@immutable
class TimeframeUpdate {
  const TimeframeUpdate({
    required this.timeframe,
    required this.barsAdded,
    required this.rows,
    this.firstBarUtc,
    this.lastBarUtc,
    required this.fetched,
  });

  final String timeframe;
  final int barsAdded;
  final int rows;
  final DateTime? firstBarUtc;
  final DateTime? lastBarUtc;
  final int fetched;

  factory TimeframeUpdate.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'timeframe update');
    return TimeframeUpdate(
      timeframe: reqString(m, 'timeframe'),
      barsAdded: reqInt(m, 'bars_added'),
      rows: reqInt(m, 'rows'),
      firstBarUtc: optUtc(m, 'first_bar_utc'),
      lastBarUtc: optUtc(m, 'last_bar_utc'),
      fetched: reqInt(m, 'fetched'),
    );
  }
}

/// `POST /rates/update`. Mirrors `RatesUpdateResponse` (routes/rates.py:276-281).
@immutable
class RatesUpdateResult {
  const RatesUpdateResult({required this.symbol, required this.updated, required this.messageFa});

  final String symbol;
  final List<TimeframeUpdate> updated;
  final String messageFa;

  factory RatesUpdateResult.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'rates update');
    return RatesUpdateResult(
      symbol: reqString(m, 'symbol'),
      updated: reqList(m, 'updated', TimeframeUpdate.fromJson),
      messageFa: reqString(m, 'message_fa'),
    );
  }
}

// ------------------------------------------------------------------ /symbols

/// Contract data. Mirrors `SymbolSpec` (data/symbols.py:32-54; the engine
/// model ignores extra keys, so does this one).
@immutable
class ChartSymbolSpec {
  const ChartSymbolSpec({
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

  factory ChartSymbolSpec.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'symbol spec');
    return ChartSymbolSpec(
      name: reqString(m, 'name'),
      digits: reqInt(m, 'digits'),
      point: reqDouble(m, 'point'),
      tradeContractSize: reqDouble(m, 'trade_contract_size'),
      tradeTickValue: reqDouble(m, 'trade_tick_value'),
      tradeTickSize: reqDouble(m, 'trade_tick_size'),
      volumeMin: reqDouble(m, 'volume_min'),
      volumeStep: reqDouble(m, 'volume_step'),
      volumeMax: reqDouble(m, 'volume_max'),
      currencyProfit: reqString(m, 'currency_profit'),
      currencyBase: reqString(m, 'currency_base'),
      description: optString(m, 'description') ?? '',
    );
  }
}

/// One configured symbol. Mirrors `SymbolItem` (routes/symbols.py:19-26).
@immutable
class ChartSymbol {
  const ChartSymbol({required this.symbol, required this.source, this.spec, this.fetchedAtUtc, this.error});

  final String symbol;

  /// `mt5` | `cache` | `none`.
  final String source;
  final ChartSymbolSpec? spec;
  final String? fetchedAtUtc;
  final String? error;

  /// Price precision for display; null until a spec was cached once.
  int? get digits => spec?.digits;

  factory ChartSymbol.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'symbol item');
    final Object? spec = m['spec'];
    return ChartSymbol(
      symbol: reqString(m, 'symbol'),
      source: reqString(m, 'source'),
      spec: spec == null ? null : ChartSymbolSpec.fromJson(spec),
      fetchedAtUtc: optString(m, 'fetched_at_utc'),
      error: optString(m, 'error'),
    );
  }

  /// `GET /symbols` -> `SymbolsResponse.symbols` (routes/symbols.py:29-33).
  static List<ChartSymbol> listFromSymbolsResponse(Object? json) =>
      reqList(jsonObject(json, 'symbols'), 'symbols', ChartSymbol.fromJson);
}

// ------------------------------------------------------------------ /chart

/// `up` | `down` | `flat` | `none` (routes/chart.py:80, `Direction`).
enum ChannelDirection {
  up('صعودی'),
  down('نزولی'),
  flat('افقی'),
  none('نامعتبر');

  const ChannelDirection(this.titleFa);

  final String titleFa;

  static ChannelDirection? parse(String? code) => switch (code) {
        null => null,
        'up' => ChannelDirection.up,
        'down' => ChannelDirection.down,
        'flat' => ChannelDirection.flat,
        'none' => ChannelDirection.none,
        _ => throw FormatException('unknown channel direction "$code"'),
      };
}

/// One channel sample. Mirrors `ChannelPoint` (routes/chart.py:92-107).
///
/// Warm-up rows arrive as `{"time": ..., "valid": false}` with every other
/// field null — the chart draws nothing for them.
@immutable
class ChannelPoint {
  const ChannelPoint({
    required this.time,
    required this.valid,
    this.mid,
    this.upper,
    this.lower,
    this.slope,
    this.sigma,
    this.isFlat,
    this.direction,
    this.atrH1,
    this.atrH4,
    this.h4Open,
    this.barsAhead,
  });

  final DateTime time;
  final bool valid;
  final double? mid;
  final double? upper;
  final double? lower;

  /// Price per H4 bar.
  final double? slope;
  final double? sigma;
  final bool? isFlat;
  final ChannelDirection? direction;

  /// H1 points only.
  final double? atrH1;
  final double? atrH4;

  /// Open of the H4 bar whose channel this is (H4 points: == [time]).
  final DateTime? h4Open;

  /// H1 points only: projection distance in H4 bars.
  final double? barsAhead;

  /// All three lines present (valid points always have them; defensive).
  bool get drawable => valid && mid != null && upper != null && lower != null;

  factory ChannelPoint.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'channel point');
    return ChannelPoint(
      time: reqUtc(m, 'time'),
      valid: reqBool(m, 'valid'),
      mid: optDouble(m, 'mid'),
      upper: optDouble(m, 'upper'),
      lower: optDouble(m, 'lower'),
      slope: optDouble(m, 'slope'),
      sigma: optDouble(m, 'sigma'),
      isFlat: optBool(m, 'is_flat'),
      direction: ChannelDirection.parse(optString(m, 'direction')),
      atrH1: optDouble(m, 'atr_h1'),
      atrH4: optDouble(m, 'atr_h4'),
      h4Open: optUtc(m, 'h4_open'),
      barsAhead: optDouble(m, 'bars_ahead'),
    );
  }
}

/// Strategy provenance shared by both chart responses. Mirrors
/// `_StrategyInfo` (routes/chart.py:110-116).
@immutable
class StrategyProvenance {
  const StrategyProvenance({
    required this.strategy,
    required this.strategyVersion,
    required this.paramsVersion,
    required this.paramsHash,
    required this.params,
  });

  final String strategy;
  final int strategyVersion;
  final int paramsVersion;
  final String paramsHash;
  final Map<String, Object?> params;

  factory StrategyProvenance.fromMap(Map<String, Object?> m) => StrategyProvenance(
        strategy: reqString(m, 'strategy'),
        strategyVersion: reqInt(m, 'strategy_version'),
        paramsVersion: reqInt(m, 'params_version'),
        paramsHash: reqString(m, 'params_hash'),
        params: reqRawMap(m, 'params'),
      );
}

/// `GET /chart/channel`. Mirrors `ChannelResponse` (routes/chart.py:119-128).
@immutable
class ChannelResult {
  const ChannelResult({
    required this.symbol,
    required this.provenance,
    required this.timeframe,
    this.from,
    this.to,
    required this.count,
    required this.validCount,
    required this.points,
    this.messageFa,
  });

  final String symbol;
  final StrategyProvenance provenance;
  final ChartTimeframe timeframe;
  final DateTime? from;
  final DateTime? to;
  final int count;
  final int validCount;
  final List<ChannelPoint> points;
  final String? messageFa;

  factory ChannelResult.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'channel');
    return ChannelResult(
      symbol: reqString(m, 'symbol'),
      provenance: StrategyProvenance.fromMap(m),
      timeframe: ChartTimeframe.parse(reqString(m, 'timeframe')),
      from: optUtc(m, 'from'),
      to: optUtc(m, 'to'),
      count: reqInt(m, 'count'),
      validCount: reqInt(m, 'valid_count'),
      points: reqList(m, 'points', ChannelPoint.fromJson),
      messageFa: optString(m, 'message_fa'),
    );
  }
}

/// `accepted` | `rejected` | `pending_entry` (routes/chart.py:81, `SetupStatus`).
enum SetupStatus {
  accepted('accepted', 'پذیرفته'),
  rejected('rejected', 'رد شده'),
  pendingEntry('pending_entry', 'در انتظار ورود');

  const SetupStatus(this.code, this.titleFa);

  final String code;
  final String titleFa;

  static SetupStatus parse(String code) => SetupStatus.values.firstWhere(
        (SetupStatus s) => s.code == code,
        orElse: () => throw FormatException('unknown setup status "$code"'),
      );
}

/// `buy` | `sell`.
enum TradeSide {
  buy('خرید'),
  sell('فروش');

  const TradeSide(this.titleFa);

  final String titleFa;

  static TradeSide parse(String code) => switch (code) {
        'buy' => TradeSide.buy,
        'sell' => TradeSide.sell,
        _ => throw FormatException('unknown direction "$code"'),
      };
}

/// `lower` | `mid` | `upper`.
enum ChannelLine {
  lower('خط پایین'),
  mid('خط میانی'),
  upper('خط بالا');

  const ChannelLine(this.titleFa);

  final String titleFa;

  static ChannelLine parse(String code) => switch (code) {
        'lower' => ChannelLine.lower,
        'mid' => ChannelLine.mid,
        'upper' => ChannelLine.upper,
        _ => throw FormatException('unknown channel line "$code"'),
      };
}

/// Persian names of the confirmation patterns
/// (strategies/stddev_channel/setups.py:87-92, `PATTERN_FA`).
const Map<String, String> patternTitlesFa = <String, String>{
  'bullish_pin_bar': 'پین‌بار صعودی (چکش)',
  'bearish_pin_bar': 'پین‌بار نزولی (ستاره دنباله‌دار)',
  'bullish_engulfing': 'اینگالف صعودی',
  'bearish_engulfing': 'اینگالف نزولی',
};

/// One scanned setup. Mirrors `SetupItem` (routes/chart.py:131-162).
///
/// `pending_entry`: [entry], [entryTime], [takeProfit] and all sizing
/// fields are null; only the indicative [referencePrice] /
/// [indicativeTakeProfit] exist. `rejected`: [takeProfit]/[volume] may be
/// null and [rejectionReasonFa] says why.
@immutable
class SetupItem {
  const SetupItem({
    required this.id,
    required this.symbol,
    required this.status,
    this.rejectionReasonFa,
    required this.setupType,
    required this.setupTitleFa,
    required this.direction,
    required this.pattern,
    required this.line,
    this.lineValue,
    this.channelDirection,
    required this.confirmationBarTime,
    required this.decisionTime,
    this.entryTime,
    this.entry,
    required this.stopLoss,
    this.takeProfit,
    required this.rr,
    this.riskDistance,
    required this.referencePrice,
    required this.indicativeTakeProfit,
    this.volume,
    this.actualRisk,
    this.margin,
    this.riskAmount,
    this.volumeNoteFa,
    this.sizingWarningsFa = const <String>[],
    required this.reasonFa,
    this.indicators = const <String, Object?>{},
  });

  final String id;
  final String symbol;
  final SetupStatus status;
  final String? rejectionReasonFa;
  final String setupType;
  final String setupTitleFa;
  final TradeSide direction;
  final String pattern;
  final ChannelLine line;
  final double? lineValue;
  final String? channelDirection;

  /// OPEN of the confirmation H1 bar t.
  final DateTime confirmationBarTime;

  /// CLOSE of bar t (the decision is taken here).
  final DateTime decisionTime;

  /// OPEN of the next cached H1 bar t+1; null while pending.
  final DateTime? entryTime;
  final double? entry;
  final double stopLoss;
  final double? takeProfit;
  final double rr;
  final double? riskDistance;

  /// Close of bar t — indicative only.
  final double referencePrice;

  /// TP from [referencePrice] — indicative only.
  final double indicativeTakeProfit;
  final double? volume;
  final double? actualRisk;
  final double? margin;
  final double? riskAmount;
  final String? volumeNoteFa;
  final List<String> sizingWarningsFa;
  final String reasonFa;

  /// `SignalCandidate.extra` — the inputs of the decision.
  final Map<String, Object?> indicators;

  String get patternTitleFa => patternTitlesFa[pattern] ?? pattern;

  factory SetupItem.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'setup item');
    return SetupItem(
      id: reqString(m, 'id'),
      symbol: reqString(m, 'symbol'),
      status: SetupStatus.parse(reqString(m, 'status')),
      rejectionReasonFa: optString(m, 'rejection_reason_fa'),
      setupType: reqString(m, 'setup_type'),
      setupTitleFa: reqString(m, 'setup_title_fa'),
      direction: TradeSide.parse(reqString(m, 'direction')),
      pattern: reqString(m, 'pattern'),
      line: ChannelLine.parse(reqString(m, 'line')),
      lineValue: optDouble(m, 'line_value'),
      channelDirection: optString(m, 'channel_direction'),
      confirmationBarTime: reqUtc(m, 'confirmation_bar_time'),
      decisionTime: reqUtc(m, 'decision_time'),
      entryTime: optUtc(m, 'entry_time'),
      entry: optDouble(m, 'entry'),
      stopLoss: reqDouble(m, 'stop_loss'),
      takeProfit: optDouble(m, 'take_profit'),
      rr: reqDouble(m, 'rr'),
      riskDistance: optDouble(m, 'risk_distance'),
      referencePrice: reqDouble(m, 'reference_price'),
      indicativeTakeProfit: reqDouble(m, 'indicative_take_profit'),
      volume: optDouble(m, 'volume'),
      actualRisk: optDouble(m, 'actual_risk'),
      margin: optDouble(m, 'margin'),
      riskAmount: optDouble(m, 'risk_amount'),
      volumeNoteFa: optString(m, 'volume_note_fa'),
      sizingWarningsFa: reqStringList(m, 'sizing_warnings_fa'),
      reasonFa: reqString(m, 'reason_fa'),
      indicators: reqRawMap(m, 'indicators'),
    );
  }
}

/// Account settings used for sizing. Mirrors `AccountSettings`
/// (storage/account_settings.py:49-57).
@immutable
class ChartAccount {
  const ChartAccount({required this.balance, required this.riskPct, required this.leverage, required this.rr});

  final double balance;
  final double riskPct;
  final int leverage;
  final double rr;

  factory ChartAccount.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'account');
    return ChartAccount(
      balance: reqDouble(m, 'balance'),
      riskPct: reqDouble(m, 'risk_pct'),
      leverage: reqInt(m, 'leverage'),
      rr: reqDouble(m, 'rr'),
    );
  }
}

/// `GET /chart/setups`. Mirrors `SetupsResponse` (routes/chart.py:165-176).
@immutable
class SetupsResult {
  const SetupsResult({
    required this.symbol,
    required this.provenance,
    this.from,
    this.to,
    required this.account,
    this.symbolSpec,
    required this.count,
    required this.statusCounts,
    required this.setups,
    required this.noteFa,
    this.messageFa,
  });

  final String symbol;
  final StrategyProvenance provenance;
  final DateTime? from;
  final DateTime? to;
  final ChartAccount account;
  final ChartSymbolSpec? symbolSpec;
  final int count;
  final Map<String, int> statusCounts;
  final List<SetupItem> setups;
  final String noteFa;
  final String? messageFa;

  factory SetupsResult.fromJson(Object? json) {
    final Map<String, Object?> m = jsonObject(json, 'setups');
    final Object? spec = m['symbol_spec'];
    return SetupsResult(
      symbol: reqString(m, 'symbol'),
      provenance: StrategyProvenance.fromMap(m),
      from: optUtc(m, 'from'),
      to: optUtc(m, 'to'),
      account: ChartAccount.fromJson(m['account']),
      symbolSpec: spec == null ? null : ChartSymbolSpec.fromJson(spec),
      count: reqInt(m, 'count'),
      statusCounts: reqIntMap(m, 'status_counts'),
      setups: reqList(m, 'setups', SetupItem.fromJson),
      noteFa: reqString(m, 'note_fa'),
      messageFa: optString(m, 'message_fa'),
    );
  }
}
