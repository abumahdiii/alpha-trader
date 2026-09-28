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
  rejected('rejected', 'ردشده'),
  pendingEntry('pending_entry', 'منتظر ورود');

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

/// `historical` | `filled` | `fallback` | `zero` (routes/chart.py
/// `SpreadSourceName`): where the entry bar's spread came from.
enum EntrySpreadSource {
  historical('historical', 'اسپرد تاریخی بروکر'),
  filled('filled', 'اسپرد صفر، پرشده با آخرین اسپرد قبلی'),
  fallback('fallback', 'اسپرد جایگزین (پیش از اولین اسپرد بروکر)'),
  zero('zero', 'بدون اسپرد (صفر)');

  const EntrySpreadSource(this.code, this.titleFa);

  final String code;
  final String titleFa;

  /// Unknown codes are kept out (null) instead of failing the whole table.
  static EntrySpreadSource? parse(String? code) {
    for (final EntrySpreadSource s in EntrySpreadSource.values) {
      if (s.code == code) return s;
    }
    return null;
  }
}

/// `tp` | `sl` | `end_of_data` (backtest/setup_outcomes.py `SetupResult`).
enum SetupResult {
  tp('tp', 'حد سود'),
  sl('sl', 'حد ضرر'),
  endOfData('end_of_data', 'پایان داده');

  const SetupResult(this.code, this.titleFa);

  final String code;

  /// Short table label. The engine's own wording is in
  /// [SetupOutcome.exitReasonFa] / [SetupsEvaluation.endOfDataLabelFa].
  final String titleFa;

  static SetupResult parse(String code) => SetupResult.values.firstWhere(
        (SetupResult r) => r.code == code,
        orElse: () => throw FormatException('unknown setup result "$code"'),
      );
}

/// Independent result of an accepted setup (no "one open trade" constraint).
/// Mirrors `SetupOutcomeOut` (routes/chart.py). Every number is the
/// engine's; the UI only formats it.
@immutable
class SetupOutcome {
  const SetupOutcome({
    required this.result,
    required this.exitReason,
    required this.exitReasonFa,
    required this.exitBarTime,
    required this.exitTime,
    required this.exitPrice,
    required this.pnlPrice,
    required this.grossPnl,
    required this.commission,
    required this.netPnl,
    this.rMultiple,
    required this.barsHeld,
    required this.heldOverWeekend,
    this.flags = const <String>[],
  });

  final SetupResult result;

  /// `sl` | `tp` | `sl_gap` | `tp_gap` | `end_of_period`.
  final String exitReason;
  final String exitReasonFa;

  /// OPEN of the exit bar (chart marker).
  final DateTime exitBarTime;

  /// Gap exits: bar open; intrabar hits and end of data: bar close.
  final DateTime exitTime;
  final double exitPrice;

  /// Price move in the trade's favour (sell: entry - exit).
  final double pnlPrice;
  final double grossPnl;
  final double commission;
  final double netPnl;
  final double? rMultiple;
  final int barsHeld;
  final bool heldOverWeekend;
  final List<String> flags;

  /// Exit at the open of a bar that gapped through SL/TP.
  bool get isGap => exitReason == 'sl_gap' || exitReason == 'tp_gap';

  factory SetupOutcome.read(JsonReader r) => SetupOutcome(
        result: SetupResult.parse(r.str('result')),
        exitReason: r.str('exit_reason'),
        exitReasonFa: r.str('exit_reason_fa'),
        exitBarTime: _utc(r, 'exit_bar_time'),
        exitTime: _utc(r, 'exit_time'),
        exitPrice: r.number('exit_price'),
        pnlPrice: r.number('pnl_price'),
        grossPnl: r.number('gross_pnl'),
        commission: r.number('commission'),
        netPnl: r.number('net_pnl'),
        rMultiple: r.numberOrNull('r_multiple'),
        barsHeld: r.integer('bars_held'),
        heldOverWeekend: r.boolean('held_over_weekend'),
        flags: r.has('flags') ? _strings(r, 'flags') : const <String>[],
      );
}

/// Was this setup traded by the manual backtest of the chart range
/// (`backtest_window`)? Mirrors `SetupBacktestFlag` (routes/chart.py).
@immutable
class SetupBacktestFlag {
  const SetupBacktestFlag({required this.traded, this.reason, this.reasonFa, this.tradeIndex, this.netPnl});

  final bool traded;

  /// Not traded: `position_open` | `entry_outside_window` | `missing_gap` |
  /// `gap_through_stop` | `invalid_levels` | `sizing_rejected` |
  /// `outside_window` | `not_reached`.
  final String? reason;
  final String? reasonFa;
  final int? tradeIndex;

  /// The backtest trade's net P&L (the backtest's momentary balance).
  final double? netPnl;

  factory SetupBacktestFlag.read(JsonReader r) => SetupBacktestFlag(
        traded: r.boolean('traded'),
        reason: r.strOrNull('reason'),
        reasonFa: r.strOrNull('reason_fa'),
        tradeIndex: r.intOrNull('trade_index'),
        netPnl: r.numberOrNull('net_pnl'),
      );
}

/// One scanned setup. Mirrors `SetupItem` (routes/chart.py).
///
/// `pending_entry`: [entry], [entryTime], [takeProfit] and all sizing
/// fields are null; only the indicative [referencePrice] /
/// [indicativeTakeProfit] exist. `rejected`: [takeProfit]/[volume] may be
/// null and [rejectionReasonFa] says why. Phase 5: [entry] is the
/// simulator fill (buy = ask open, sell = bid open); the raw bid open of
/// the entry bar is [entryBidOpen].
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
    this.entryBidOpen,
    this.spreadAtEntryPoints,
    this.entrySpreadSource,
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
    this.outcome,
    this.backtest,
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

  /// Simulator fill: buy = ask open (bid open + spread), sell = bid open.
  final double? entry;

  /// Raw (bid) open of bar t+1 (the phase-3 meaning of "entry").
  final double? entryBidOpen;

  /// Spread of bar t+1 actually used (after the zero fill / fallback).
  final int? spreadAtEntryPoints;
  final EntrySpreadSource? entrySpreadSource;
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

  /// Independent outcome (accepted + evaluation available), else null.
  final SetupOutcome? outcome;

  /// Traded in the backtest of this range? Null without a backtest window.
  final SetupBacktestFlag? backtest;

  String get patternTitleFa => patternTitlesFa[pattern] ?? pattern;

  /// The setup title, plus the channel line when the title does not name it.
  String get typeTitleFa => setupTitleFa.contains(line.titleFa) ? setupTitleFa : '$setupTitleFa (${line.titleFa})';

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
        entryBidOpen: r.numberOrNull('entry_bid_open'),
        spreadAtEntryPoints: r.intOrNull('spread_at_entry_points'),
        entrySpreadSource: EntrySpreadSource.parse(r.strOrNull('entry_spread_source')),
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
        outcome: _objectOrNull(r, 'outcome', SetupOutcome.read),
        backtest: _objectOrNull(r, 'backtest', SetupBacktestFlag.read),
      );
}

/// An optional nested object: absent (older engine) or null -> null.
T? _objectOrNull<T>(JsonReader r, String key, T Function(JsonReader) read) {
  final JsonReader? o = r.objectOrNull(key);
  return o == null ? null : read(o);
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

/// Costs of the setup evaluation. Mirrors `backtest.models.CostModel`.
@immutable
class SetupsCostModel {
  const SetupsCostModel({
    required this.spread,
    this.fallbackSpreadPoints,
    required this.commissionPerLotPerSide,
    required this.swap,
  });

  /// Always `historical` (bars are bid; ask = bid + spread).
  final String spread;

  /// null = auto, 0 = explicit zero cost, > 0 = user value.
  final int? fallbackSpreadPoints;
  final double commissionPerLotPerSide;

  /// Always `none`.
  final String swap;

  factory SetupsCostModel.read(JsonReader r) => SetupsCostModel(
        spread: r.strOrNull('spread') ?? 'historical',
        fallbackSpreadPoints: r.intOrNull('fallback_spread_points'),
        commissionPerLotPerSide: r.numberOrNull('commission_per_lot_per_side') ?? 0,
        swap: r.strOrNull('swap') ?? 'none',
      );
}

/// The fallback spread the evaluation used for bars with no earlier broker
/// spread. Mirrors `backtest.models.SpreadFallback`.
@immutable
class SetupsSpreadFallback {
  const SetupsSpreadFallback({
    required this.points,
    required this.source,
    this.observedFrom,
    this.observedTo,
    required this.observedBars,
    this.observedMedian,
    required this.fallbackBars,
    required this.zeroBars,
    required this.totalBars,
  });

  final int points;

  /// `auto_median_observed` | `user` | `none`.
  final String source;
  final DateTime? observedFrom;
  final DateTime? observedTo;
  final int observedBars;
  final double? observedMedian;

  /// Evaluated bars priced with [points] / with zero / in total.
  final int fallbackBars;
  final int zeroBars;
  final int totalBars;

  factory SetupsSpreadFallback.read(JsonReader r) => SetupsSpreadFallback(
        points: r.integer('points'),
        source: r.str('source'),
        observedFrom: r.utcOrNull('observed_from'),
        observedTo: r.utcOrNull('observed_to'),
        observedBars: r.integer('observed_bars'),
        observedMedian: r.numberOrNull('observed_median'),
        fallbackBars: r.integer('fallback_bars'),
        zeroBars: r.integer('zero_bars'),
        totalBars: r.integer('total_bars'),
      );
}

/// How the per-setup outcomes were evaluated. Mirrors `SetupsEvaluation`
/// (routes/chart.py). [available] false: no outcomes / flags / summary
/// numbers (only counts) and [messageFa] says why.
@immutable
class SetupsEvaluation {
  const SetupsEvaluation({
    required this.available,
    this.messageFa,
    this.basis = 'independent_setups',
    required this.labelFa,
    required this.endOfDataLabelFa,
    this.costModel,
    this.spreadFallback,
    this.labelsFa = const <String>[],
  });

  final bool available;
  final String? messageFa;
  final String basis;

  /// «ارزیابی مستقل هر ستاپ، بدون قید یک معامله باز؛ با نتیجه بک‌تست فرق دارد».
  final String labelFa;

  /// Tooltip of the «پایان داده» result.
  final String endOfDataLabelFa;
  final SetupsCostModel? costModel;
  final SetupsSpreadFallback? spreadFallback;

  /// Provisional-data, spread, commission and swap labels.
  final List<String> labelsFa;

  factory SetupsEvaluation.read(JsonReader r) => SetupsEvaluation(
        available: r.boolean('available'),
        messageFa: r.strOrNull('message_fa'),
        basis: r.strOrNull('basis') ?? 'independent_setups',
        labelFa: r.strOrNull('label_fa') ?? '',
        endOfDataLabelFa: r.strOrNull('end_of_data_label_fa') ?? '',
        costModel: _objectOrNull(r, 'cost_model', SetupsCostModel.read),
        spreadFallback: _objectOrNull(r, 'spread_fallback', SetupsSpreadFallback.read),
        labelsFa: r.has('labels_fa') && r.raw('labels_fa') != null ? _strings(r, 'labels_fa') : const <String>[],
      );
}

/// Range summary of the listed setups. Mirrors `SetupsSummary`
/// (routes/chart.py): counts over all setups; wins / losses / win rate /
/// P&L / PF / R over the CLOSED (TP/SL) outcomes; [openNetPnl] = end-of-data
/// P&L (never in the other numbers). Computed by the engine only.
@immutable
class SetupsSummary {
  const SetupsSummary({
    required this.total,
    required this.accepted,
    required this.rejected,
    required this.pendingEntry,
    required this.closed,
    required this.wins,
    required this.losses,
    required this.breakeven,
    required this.openEndOfData,
    this.winRate,
    required this.netPnl,
    required this.grossProfit,
    required this.grossLoss,
    this.profitFactor,
    required this.profitFactorInfinite,
    required this.totalR,
    this.avgR,
    required this.rCount,
    required this.openNetPnl,
  });

  final int total;
  final int accepted;
  final int rejected;
  final int pendingEntry;
  final int closed;
  final int wins;
  final int losses;
  final int breakeven;
  final int openEndOfData;

  /// wins / closed, 0..1.
  final double? winRate;
  final double netPnl;
  final double grossProfit;
  final double grossLoss;

  /// null without losses; see [profitFactorInfinite].
  final double? profitFactor;
  final bool profitFactorInfinite;
  final double totalR;
  final double? avgR;
  final int rCount;
  final double openNetPnl;

  factory SetupsSummary.read(JsonReader r) => SetupsSummary(
        total: r.integer('total'),
        accepted: r.integer('accepted'),
        rejected: r.integer('rejected'),
        pendingEntry: r.integer('pending_entry'),
        closed: r.integer('closed'),
        wins: r.integer('wins'),
        losses: r.integer('losses'),
        breakeven: r.integer('breakeven'),
        openEndOfData: r.integer('open_end_of_data'),
        winRate: r.numberOrNull('win_rate'),
        netPnl: r.number('net_pnl'),
        grossProfit: r.number('gross_profit'),
        grossLoss: r.number('gross_loss'),
        profitFactor: r.numberOrNull('profit_factor'),
        profitFactorInfinite: r.boolOrNull('profit_factor_infinite') ?? false,
        totalR: r.number('total_r'),
        avgR: r.numberOrNull('avg_r'),
        rCount: r.integer('r_count'),
        openNetPnl: r.number('open_net_pnl'),
      );
}

/// The manual backtest window of the chart range (what «بک‌تست همین بازه»
/// submits) and that run's result. Mirrors `BacktestWindowOut`
/// (routes/chart.py). [from]/[to] are sent back exactly as received.
@immutable
class BacktestWindow {
  const BacktestWindow({
    this.from,
    this.to,
    this.clipped = false,
    this.noteFa,
    required this.available,
    this.code,
    this.messageFa,
    this.trades,
    this.netProfit,
  });

  final DateTime? from;

  /// Exclusive end.
  final DateTime? to;
  final bool clipped;
  final String? noteFa;
  final bool available;

  /// `window_too_early` | `spec_missing` | `no_data` | ... when unavailable.
  final String? code;
  final String? messageFa;
  final int? trades;
  final double? netProfit;

  factory BacktestWindow.read(JsonReader r) => BacktestWindow(
        from: r.utcOrNull('from'),
        to: r.utcOrNull('to'),
        clipped: r.boolOrNull('clipped') ?? false,
        noteFa: r.strOrNull('note_fa'),
        available: r.boolean('available'),
        code: r.strOrNull('code'),
        messageFa: r.strOrNull('message_fa'),
        trades: r.intOrNull('trades'),
        netProfit: r.numberOrNull('net_profit'),
      );
}

/// `GET /chart/setups`. Mirrors `SetupsResponse` (routes/chart.py).
/// [evaluation], [summary] and [backtestWindow] are null only for an older
/// engine that does not send them.
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
    this.evaluation,
    this.summary,
    this.backtestWindow,
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
  final SetupsEvaluation? evaluation;
  final SetupsSummary? summary;
  final BacktestWindow? backtestWindow;

  /// Outcomes, flags and summary numbers are present.
  bool get evaluationAvailable => evaluation?.available ?? false;

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
      evaluation: _objectOrNull(r, 'evaluation', SetupsEvaluation.read),
      summary: _objectOrNull(r, 'summary', SetupsSummary.read),
      backtestWindow: _objectOrNull(r, 'backtest_window', BacktestWindow.read),
    );
  }
}
