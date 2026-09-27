// Typed mirrors of the engine's chart / market-data responses used by the
// candlestick chart (`/rates`, `/rates/gaps`, `/rates/update`,
// `/chart/channel`, `/chart/setups`). `/symbols` and `/rates/meta` live in
// market_data.dart.
//
// Each class names the pydantic model it mirrors (engine/alpha_engine/...).
// Field names map 1:1 (snake_case -> camelCase); `T | None` in Python is a
// nullable Dart field. Nothing here computes trading values: prices, channel
// lines, levels and volumes are exactly what the engine sent.

import 'package:flutter/foundation.dart';

import 'json_reader.dart';
import 'market_data.dart';

/// Required ISO-8601 UTC timestamp.
DateTime _utc(JsonReader r, String key) {
  final DateTime? t = r.utcOrNull(key);
  if (t == null) throw FormatException('${r.what}.$key is missing (expected an ISO-8601 UTC timestamp)');
  return t;
}

/// `dict[str, int]` (gap_counts, status_counts).
Map<String, int> _intMap(JsonReader r, String key) {
  final JsonReader o = r.object(key);
  final Object? raw = r.raw(key);
  return Map<String, int>.unmodifiable(<String, int>{
    for (final Object? k in (raw as Map).keys) '$k': o.integer('$k'),
  });
}

/// A free-form JSON object kept as-is (params, indicators; values may be null).
Map<String, Object?> _rawMap(JsonReader r, String key) {
  r.object(key); // type check
  return Map<String, Object?>.unmodifiable((r.raw(key) as Map).map((Object? k, Object? v) => MapEntry('$k', v)));
}

/// `list[str]`, strictly (JsonReader.strings is lenient on null).
List<String> _strings(JsonReader r, String key) => List<String>.unmodifiable(r.list(key).map((Object? e) =>
    e is String ? e : throw FormatException('${r.what}.$key must be a list of strings, got ${e.runtimeType}')));

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

  factory Candle.fromJson(Object? json) => Candle.read(JsonReader(json, 'bar'));

  factory Candle.read(JsonReader r) => Candle(
        time: _utc(r, 'time'),
        serverTime: r.strOrNull('server_time'),
        open: r.number('open'),
        high: r.number('high'),
        low: r.number('low'),
        close: r.number('close'),
        tickVolume: r.integer('tick_volume'),
        spread: r.integer('spread'),
        realVolume: r.integer('real_volume'),
      );
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

  factory Gap.read(JsonReader r) => Gap(
        kind: GapKind.parse(r.str('kind')),
        after: _utc(r, 'after'),
        before: _utc(r, 'before'),
        start: _utc(r, 'start'),
        missingBars: r.integer('missing_bars'),
        durationHours: r.number('duration_hours'),
      );
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
    final JsonReader r = JsonReader(json, 'rates');
    return RatesResult(
      symbol: r.str('symbol'),
      timeframe: r.str('timeframe'),
      from: r.str('from'),
      to: r.str('to'),
      source: r.str('source'),
      stale: r.boolean('stale'),
      offsetModel: r.strOrNull('offset_model'),
      count: r.integer('count'),
      bars: r.objects('bars', Candle.read),
      gaps: r.objects('gaps', Gap.read),
      gapCounts: _intMap(r, 'gap_counts'),
      message: r.strOrNull('message'),
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
    final JsonReader r = JsonReader(json, 'rates_gaps');
    return GapsResult(
      symbol: r.str('symbol'),
      timeframe: r.str('timeframe'),
      cached: r.boolean('cached'),
      rows: r.integer('rows'),
      firstBarUtc: r.utcOrNull('first_bar_utc'),
      lastBarUtc: r.utcOrNull('last_bar_utc'),
      offsetModel: r.strOrNull('offset_model'),
      count: r.integer('count'),
      gaps: r.objects('gaps', Gap.read),
      gapCounts: _intMap(r, 'gap_counts'),
      missingBarsTotal: r.integer('missing_bars_total'),
      sessionBreakSlots: _strings(r, 'session_break_slots'),
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

  factory TimeframeUpdate.read(JsonReader r) => TimeframeUpdate(
        timeframe: r.str('timeframe'),
        barsAdded: r.integer('bars_added'),
        rows: r.integer('rows'),
        firstBarUtc: r.utcOrNull('first_bar_utc'),
        lastBarUtc: r.utcOrNull('last_bar_utc'),
        fetched: r.integer('fetched'),
      );
}

/// `POST /rates/update`. Mirrors `RatesUpdateResponse` (routes/rates.py:276-281).
@immutable
class RatesUpdateResult {
  const RatesUpdateResult({required this.symbol, required this.updated, required this.messageFa});

  final String symbol;
  final List<TimeframeUpdate> updated;
  final String messageFa;

  factory RatesUpdateResult.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'rates_update');
    return RatesUpdateResult(
      symbol: r.str('symbol'),
      updated: r.objects('updated', TimeframeUpdate.read),
      messageFa: r.str('message_fa'),
    );
  }
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

  factory ChannelPoint.read(JsonReader r) => ChannelPoint(
        time: _utc(r, 'time'),
        valid: r.boolean('valid'),
        mid: r.numberOrNull('mid'),
        upper: r.numberOrNull('upper'),
        lower: r.numberOrNull('lower'),
        slope: r.numberOrNull('slope'),
        sigma: r.numberOrNull('sigma'),
        isFlat: r.boolOrNull('is_flat'),
        direction: ChannelDirection.parse(r.strOrNull('direction')),
        atrH1: r.numberOrNull('atr_h1'),
        atrH4: r.numberOrNull('atr_h4'),
        h4Open: r.utcOrNull('h4_open'),
        barsAhead: r.numberOrNull('bars_ahead'),
      );
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

  factory StrategyProvenance.read(JsonReader r) => StrategyProvenance(
        strategy: r.str('strategy'),
        strategyVersion: r.integer('strategy_version'),
        paramsVersion: r.integer('params_version'),
        paramsHash: r.str('params_hash'),
        params: _rawMap(r, 'params'),
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
    final JsonReader r = JsonReader(json, 'channel');
    return ChannelResult(
      symbol: r.str('symbol'),
      provenance: StrategyProvenance.read(r),
      timeframe: ChartTimeframe.parse(r.str('timeframe')),
      from: r.utcOrNull('from'),
      to: r.utcOrNull('to'),
      count: r.integer('count'),
      validCount: r.integer('valid_count'),
      points: r.objects('points', ChannelPoint.read),
      messageFa: r.strOrNull('message_fa'),
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

  factory SetupItem.read(JsonReader r) => SetupItem(
        id: r.str('id'),
        symbol: r.str('symbol'),
        status: SetupStatus.parse(r.str('status')),
        rejectionReasonFa: r.strOrNull('rejection_reason_fa'),
        setupType: r.str('setup_type'),
        setupTitleFa: r.str('setup_title_fa'),
        direction: TradeSide.parse(r.str('direction')),
        pattern: r.str('pattern'),
        line: ChannelLine.parse(r.str('line')),
        lineValue: r.numberOrNull('line_value'),
        channelDirection: r.strOrNull('channel_direction'),
        confirmationBarTime: _utc(r, 'confirmation_bar_time'),
        decisionTime: _utc(r, 'decision_time'),
        entryTime: r.utcOrNull('entry_time'),
        entry: r.numberOrNull('entry'),
        stopLoss: r.number('stop_loss'),
        takeProfit: r.numberOrNull('take_profit'),
        rr: r.number('rr'),
        riskDistance: r.numberOrNull('risk_distance'),
        referencePrice: r.number('reference_price'),
        indicativeTakeProfit: r.number('indicative_take_profit'),
        volume: r.numberOrNull('volume'),
        actualRisk: r.numberOrNull('actual_risk'),
        margin: r.numberOrNull('margin'),
        riskAmount: r.numberOrNull('risk_amount'),
        volumeNoteFa: r.strOrNull('volume_note_fa'),
        sizingWarningsFa: _strings(r, 'sizing_warnings_fa'),
        reasonFa: r.str('reason_fa'),
        indicators: _rawMap(r, 'indicators'),
      );
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

  factory ChartAccount.read(JsonReader r) => ChartAccount(
        balance: r.number('balance'),
        riskPct: r.number('risk_pct'),
        leverage: r.integer('leverage'),
        rr: r.number('rr'),
      );
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
  final SymbolSpec? symbolSpec;
  final int count;
  final Map<String, int> statusCounts;
  final List<SetupItem> setups;
  final String noteFa;
  final String? messageFa;

  factory SetupsResult.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'setups');
    final JsonReader? spec = r.objectOrNull('symbol_spec');
    return SetupsResult(
      symbol: r.str('symbol'),
      provenance: StrategyProvenance.read(r),
      from: r.utcOrNull('from'),
      to: r.utcOrNull('to'),
      account: ChartAccount.read(r.object('account')),
      symbolSpec: spec == null ? null : SymbolSpec.fromJson(spec),
      count: r.integer('count'),
      statusCounts: _intMap(r, 'status_counts'),
      setups: r.objects('setups', SetupItem.read),
      noteFa: r.str('note_fa'),
      messageFa: r.strOrNull('message_fa'),
    );
  }
}
