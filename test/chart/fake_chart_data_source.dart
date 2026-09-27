// In-memory ChartDataSource for tests: deterministic synthetic bars (FX
// week: no Saturday, Sunday from 22:00 UTC), fake channel values and a few
// setups of every status. The numbers are fixtures, not trading math.

import 'dart:async';
import 'dart:math' as math;

import 'package:alpha_trader/chart/chart_data_source.dart';
import 'package:alpha_trader/chart/chart_format.dart';
import 'package:alpha_trader/chart/chart_models.dart';

/// Synthetic open times from [start] every [step], skipping the weekend.
List<DateTime> fxTimes(DateTime start, Duration step, int count) {
  final List<DateTime> out = <DateTime>[];
  DateTime t = start;
  while (out.length < count) {
    final bool weekend = t.weekday == DateTime.saturday || (t.weekday == DateTime.sunday && t.hour < 22);
    if (!weekend) out.add(t);
    t = t.add(step);
  }
  return out;
}

List<Candle> syntheticCandles(List<DateTime> times, {double base = 2000}) => <Candle>[
      for (int i = 0; i < times.length; i++)
        () {
          final double open = base + 20 * math.sin(i / 50) + (i % 7) * 0.3;
          final double close = open + ((i % 3) - 1) * 1.1;
          return Candle(
            time: times[i],
            // Like a us_dst(+2) winter broker: server clock = UTC + 2 h.
            serverTime: formatMt5Time(times[i].add(const Duration(hours: 2))),
            open: open,
            high: math.max(open, close) + 1.5,
            low: math.min(open, close) - 1.5,
            close: close,
            tickVolume: 1000 + i % 500,
            spread: 20 + i % 5,
            realVolume: 0,
          );
        }(),
    ];

ChannelPoint syntheticPoint(Candle c, int i, ChartTimeframe tf) {
  if (i < 20) return ChannelPoint(time: c.time, valid: false);
  final double mid = 2000 + i * 0.01;
  final DateTime h4 = DateTime.utc(c.time.year, c.time.month, c.time.day, c.time.hour - c.time.hour % 4);
  return ChannelPoint(
    time: c.time,
    valid: true,
    mid: mid,
    upper: mid + 25,
    lower: mid - 25,
    slope: 0.19758661866186655,
    sigma: 12.5,
    isFlat: false,
    direction: ChannelDirection.up,
    atrH1: tf == ChartTimeframe.h1 ? 4.25 : null,
    atrH4: 10.5,
    h4Open: tf == ChartTimeframe.h1 ? h4 : c.time,
    barsAhead: tf == ChartTimeframe.h1 ? 0.75 : null,
  );
}

SetupItem syntheticSetup(String symbol, Candle conf, Candle? next, SetupStatus status, TradeSide side, int n) {
  final double sl = side == TradeSide.buy ? conf.low - 1 : conf.high + 1;
  final double? entry = status == SetupStatus.pendingEntry ? null : next?.open;
  return SetupItem(
    id: '$symbol:$n:abcdef012345',
    symbol: symbol,
    status: status,
    rejectionReasonFa: status == SetupStatus.rejected ? 'حجم محاسبه‌شده کمتر از حداقل حجم است.' : null,
    setupType: 'bounce_lower',
    setupTitleFa: 'برگشت از خط پایین',
    direction: side,
    pattern: side == TradeSide.buy ? 'bullish_pin_bar' : 'bearish_pin_bar',
    line: ChannelLine.lower,
    lineValue: conf.low,
    channelDirection: 'up',
    confirmationBarTime: conf.time,
    decisionTime: conf.time.add(const Duration(hours: 1)),
    entryTime: entry == null ? null : next!.time,
    entry: entry,
    stopLoss: sl,
    takeProfit: status == SetupStatus.accepted ? entry! + (entry - sl) * 2 : null,
    rr: 2,
    riskDistance: entry == null ? null : (entry - sl).abs(),
    referencePrice: conf.close,
    indicativeTakeProfit: conf.close + (conf.close - sl) * 2,
    volume: status == SetupStatus.accepted ? 0.06 : null,
    actualRisk: status == SetupStatus.accepted ? 8.92 : null,
    margin: status == SetupStatus.accepted ? 123.86 : null,
    riskAmount: status == SetupStatus.accepted ? 10 : null,
    volumeNoteFa: status == SetupStatus.pendingEntry ? 'کندل بعد هنوز در کش نیست.' : null,
    reasonFa: 'لمس خط پایین کانال صعودی و پین‌بار صعودی.',
  );
}

class FakeChartDataSource implements ChartDataSource {
  FakeChartDataSource({
    this.h1Count = 800,
    this.symbolNames = const <String>['XAUUSD.x', 'BRNUSD.x'],
    this.digits = 2,
  });

  final int h1Count;
  final List<String> symbolNames;
  final int? digits;

  /// Thrown by rates/channel/setups/meta/gaps when set.
  ChartDataException? error;
  ChartDataException? updateError;
  RatesUpdateResult? updateResult;

  /// Per-symbol gate: rates() waits for it (to test stale responses).
  final Map<String, Completer<void>> gates = <String, Completer<void>>{};

  final List<String> calls = <String>[];

  final Map<String, List<Candle>> _h1 = <String, List<Candle>>{};
  final Map<String, List<Candle>> _h4 = <String, List<Candle>>{};

  static final DateTime start = DateTime.utc(2021, 10, 4); // a Monday

  List<Candle> bars(String symbol, ChartTimeframe tf) {
    final Map<String, List<Candle>> cache = tf == ChartTimeframe.h1 ? _h1 : _h4;
    return cache.putIfAbsent(symbol, () {
      final int count = tf == ChartTimeframe.h1 ? h1Count : (h1Count / 4).ceil();
      final double base = symbol.startsWith('BRN') ? 80 : 2000;
      return syntheticCandles(fxTimes(start, tf.duration, count), base: base);
    });
  }

  /// Setup confirmation bar indices in the full H1 series.
  List<int> setupIndices() => <int>[h1Count ~/ 2, h1Count - 20, h1Count - 12, h1Count - 1];

  List<SetupItem> allSetups(String symbol) {
    final List<Candle> b = bars(symbol, ChartTimeframe.h1);
    final List<int> idx = setupIndices();
    const List<SetupStatus> st = <SetupStatus>[
      SetupStatus.accepted,
      SetupStatus.accepted,
      SetupStatus.rejected,
      SetupStatus.pendingEntry,
    ];
    return <SetupItem>[
      for (int k = 0; k < idx.length; k++)
        syntheticSetup(symbol, b[idx[k]], idx[k] + 1 < b.length ? b[idx[k] + 1] : null, st[k],
            k.isEven ? TradeSide.buy : TradeSide.sell, idx[k]),
    ];
  }

  /// Full-history gaps: every weekend, plus one old "missing" gap.
  List<Gap> allGaps(String symbol, ChartTimeframe tf) {
    final List<Candle> b = bars(symbol, tf);
    final List<Gap> out = <Gap>[];
    for (int i = 1; i < b.length; i++) {
      final Duration d = b[i].time.difference(b[i - 1].time);
      if (d > tf.duration) {
        out.add(Gap(
          kind: GapKind.weekend,
          after: b[i - 1].time,
          before: b[i].time,
          start: b[i - 1].time.add(tf.duration),
          missingBars: d.inHours ~/ tf.duration.inHours - 1,
          durationHours: d.inHours.toDouble(),
        ));
      }
    }
    final int m = 30;
    out.insert(
        0,
        Gap(
          kind: GapKind.missing,
          after: b[m].time,
          before: b[m + 1].time,
          start: b[m].time.add(tf.duration),
          missingBars: 1,
          durationHours: 2,
        ));
    return out;
  }

  bool _within(DateTime t, DateTime? from, DateTime? to) =>
      (from == null || !t.isBefore(from)) && (to == null || !t.isAfter(to));

  void _maybeThrow() {
    final ChartDataException? e = error;
    if (e != null) throw e;
  }

  @override
  Future<List<ChartSymbol>> symbols() async {
    calls.add('symbols');
    _maybeThrow();
    return <ChartSymbol>[
      for (final String s in symbolNames)
        ChartSymbol(
          symbol: s,
          source: 'cache',
          spec: digits == null
              ? null
              : ChartSymbolSpec(
                  name: s,
                  digits: digits!,
                  point: 0.01,
                  tradeContractSize: 100,
                  tradeTickValue: 1,
                  tradeTickSize: 0.01,
                  volumeMin: 0.01,
                  volumeStep: 0.01,
                  volumeMax: 100,
                  currencyProfit: 'USD',
                  currencyBase: 'XAU',
                ),
        ),
    ];
  }

  @override
  Future<RatesResult> rates(String symbol, ChartTimeframe timeframe, {DateTime? from, DateTime? to}) async {
    calls.add('rates $symbol ${timeframe.code}');
    final Completer<void>? gate = gates[symbol];
    if (gate != null) await gate.future;
    _maybeThrow();
    final List<Candle> b =
        bars(symbol, timeframe).where((Candle c) => _within(c.time, from, to)).toList(growable: false);
    final List<Gap> g = allGaps(symbol, timeframe)
        .where((Gap x) => b.isNotEmpty && !x.after.isBefore(b.first.time) && x.before.isBefore(b.last.time))
        .toList();
    return RatesResult(
      symbol: symbol,
      timeframe: timeframe.code,
      from: from?.toIso8601String() ?? '',
      to: to?.toIso8601String() ?? '',
      source: 'cache',
      stale: false,
      offsetModel: 'us_dst(2)',
      count: b.length,
      bars: b,
      gaps: g,
      gapCounts: const <String, int>{'weekend': 0, 'holiday': 0, 'session_break': 0, 'missing': 0},
      message: b.isEmpty ? 'در این بازه داده‌ای نیست.' : null,
    );
  }

  static const StrategyProvenance provenance = StrategyProvenance(
    strategy: 'stddev_channel',
    strategyVersion: 1,
    paramsVersion: 3,
    paramsHash: 'abcdef0123456789',
    params: <String, Object?>{'n': 100, 'k': 2},
  );

  @override
  Future<ChannelResult> channel(String symbol, ChartTimeframe timeframe, {DateTime? from, DateTime? to}) async {
    calls.add('channel $symbol ${timeframe.code}');
    _maybeThrow();
    final List<Candle> all = bars(symbol, timeframe);
    final List<ChannelPoint> pts = <ChannelPoint>[
      for (int i = 0; i < all.length; i++)
        if (_within(all[i].time, from, to)) syntheticPoint(all[i], i, timeframe),
    ];
    return ChannelResult(
      symbol: symbol,
      provenance: provenance,
      timeframe: timeframe,
      from: from,
      to: to,
      count: pts.length,
      validCount: pts.where((ChannelPoint p) => p.valid).length,
      points: pts,
    );
  }

  @override
  Future<SetupsResult> setups(String symbol, {DateTime? from, DateTime? to}) async {
    calls.add('setups $symbol');
    _maybeThrow();
    final List<SetupItem> s =
        allSetups(symbol).where((SetupItem x) => _within(x.decisionTime, from, to)).toList(growable: false);
    return SetupsResult(
      symbol: symbol,
      provenance: provenance,
      from: from,
      to: to,
      account: const ChartAccount(balance: 1000, riskPct: 1, leverage: 100, rr: 2),
      count: s.length,
      statusCounts: const <String, int>{'accepted': 2, 'rejected': 1, 'pending_entry': 1},
      setups: s,
      noteFa: 'یادداشت ردها',
    );
  }

  @override
  Future<GapsResult> gaps(String symbol, ChartTimeframe timeframe) async {
    calls.add('gaps $symbol ${timeframe.code}');
    _maybeThrow();
    final List<Gap> g = allGaps(symbol, timeframe);
    return GapsResult(
      symbol: symbol,
      timeframe: timeframe.code,
      cached: true,
      rows: bars(symbol, timeframe).length,
      offsetModel: 'us_dst(2)',
      count: g.length,
      gaps: g,
      gapCounts: <String, int>{
        for (final GapKind k in GapKind.values) k.code: g.where((Gap x) => x.kind == k).length,
      },
      missingBarsTotal: 1,
      sessionBreakSlots: const <String>[],
    );
  }

  @override
  Future<RatesMeta> ratesMeta(String symbol, ChartTimeframe timeframe) async {
    calls.add('meta $symbol ${timeframe.code}');
    _maybeThrow();
    final List<Candle> b = bars(symbol, timeframe);
    return RatesMeta(
      symbol: symbol,
      timeframe: timeframe.code,
      cached: true,
      rows: b.length,
      firstBarUtc: b.first.time,
      lastBarUtc: b.last.time,
      offsetModel: 'us_dst(2)',
      source: 'mt5',
    );
  }

  @override
  Future<RatesUpdateResult> update(String symbol) async {
    calls.add('update $symbol');
    final ChartDataException? e = updateError;
    if (e != null) throw e;
    return updateResult ??
        RatesUpdateResult(symbol: symbol, updated: const <TimeframeUpdate>[], messageFa: 'کش $symbol به‌روز شد.');
  }
}
