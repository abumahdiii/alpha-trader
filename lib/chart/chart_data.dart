import 'package:flutter/foundation.dart';

import '../core/dev_mode.dart';
import 'chart_models.dart';

/// A setup placed on the loaded bars: [barIndex] = its confirmation bar,
/// [entryIndex] = the bar it would be filled at (null while pending).
@immutable
class SetupMark {
  const SetupMark({required this.item, required this.barIndex, this.entryIndex});

  final SetupItem item;
  final int barIndex;
  final int? entryIndex;

  /// First bar of the entry/SL/TP segments: the entry bar, or (pending) the
  /// slot right after the confirmation bar.
  int get levelsStartIndex => entryIndex ?? barIndex + 1;
}

/// A gap placed on the loaded bars: drawn between [afterIndex] and the next bar.
@immutable
class GapMark {
  const GapMark({required this.gap, required this.afterIndex});

  final Gap gap;
  final int afterIndex;
}

/// Engine responses of one load, aligned to bar indices once so painting and
/// hit-testing are index lookups. Pure presentation: no value is derived
/// except the positions of engine values on the time axis.
@immutable
class ChartData {
  const ChartData._({
    required this.symbol,
    required this.timeframe,
    required this.digits,
    required this.candles,
    required this.channel,
    required this.setups,
    required this.gaps,
    required this.rates,
    required this.channelResult,
    required this.setupsResult,
    required List<int> timesMs,
  }) : _timesMs = timesMs;

  /// Aligns [channelResult] points and [setupsResult] items to [rates] bars
  /// by exact UTC open time. Points/setups whose bar is not loaded are dropped
  /// (logged under DEV_MODE) — they cannot be placed truthfully.
  factory ChartData.build({
    required RatesResult rates,
    required ChartTimeframe timeframe,
    ChannelResult? channelResult,
    SetupsResult? setupsResult,
    int? digits,
  }) {
    final List<Candle> candles = rates.bars;
    final List<int> timesMs = List<int>.generate(
      candles.length,
      (int i) => candles[i].time.millisecondsSinceEpoch,
      growable: false,
    );
    final Map<int, int> indexByMs = <int, int>{
      for (int i = 0; i < timesMs.length; i++) timesMs[i]: i,
    };

    final List<ChannelPoint?> channel = List<ChannelPoint?>.filled(candles.length, null);
    int unplacedPoints = 0;
    for (final ChannelPoint p in channelResult?.points ?? const <ChannelPoint>[]) {
      final int? i = indexByMs[p.time.millisecondsSinceEpoch];
      if (i == null) {
        unplacedPoints++;
      } else {
        channel[i] = p;
      }
    }

    final List<SetupMark> marks = <SetupMark>[];
    for (final SetupItem s in setupsResult?.setups ?? const <SetupItem>[]) {
      final int? i = indexByMs[s.confirmationBarTime.millisecondsSinceEpoch];
      if (i == null) {
        devLog('[Chart] setup ${s.id} not placed: confirmation bar not loaded');
        continue;
      }
      final DateTime? et = s.entryTime;
      marks.add(SetupMark(
        item: s,
        barIndex: i,
        entryIndex: et == null ? null : indexByMs[et.millisecondsSinceEpoch],
      ));
    }
    marks.sort((SetupMark a, SetupMark b) => a.barIndex.compareTo(b.barIndex));

    final List<GapMark> gaps = <GapMark>[];
    for (final Gap g in rates.gaps) {
      final int? i = indexByMs[g.after.millisecondsSinceEpoch];
      if (i != null) gaps.add(GapMark(gap: g, afterIndex: i));
    }
    gaps.sort((GapMark a, GapMark b) => a.afterIndex.compareTo(b.afterIndex));

    if (unplacedPoints > 0) devLog('[Chart] $unplacedPoints channel points without a loaded bar');
    return ChartData._(
      symbol: rates.symbol,
      timeframe: timeframe,
      digits: digits ?? setupsResult?.symbolSpec?.digits,
      candles: List<Candle>.unmodifiable(candles),
      channel: List<ChannelPoint?>.unmodifiable(channel),
      setups: List<SetupMark>.unmodifiable(marks),
      gaps: List<GapMark>.unmodifiable(gaps),
      rates: rates,
      channelResult: channelResult,
      setupsResult: setupsResult,
      timesMs: timesMs,
    );
  }

  final String symbol;
  final ChartTimeframe timeframe;

  /// Symbol price precision (MT5 `digits`); null when no spec is cached.
  final int? digits;
  final List<Candle> candles;

  /// Channel point of each bar (same length as [candles]).
  final List<ChannelPoint?> channel;
  final List<SetupMark> setups;
  final List<GapMark> gaps;
  final RatesResult rates;
  final ChannelResult? channelResult;
  final SetupsResult? setupsResult;
  final List<int> _timesMs;

  int get length => candles.length;
  bool get isEmpty => candles.isEmpty;

  /// First bar whose open time is >= [t] (== [length] if none).
  int lowerBound(DateTime t) {
    final int ms = t.millisecondsSinceEpoch;
    int lo = 0;
    int hi = _timesMs.length;
    while (lo < hi) {
      final int mid = (lo + hi) >> 1;
      if (_timesMs[mid] < ms) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    return lo;
  }

  /// Bar whose open time is closest to [t]; null when there are no bars.
  int? nearestIndex(DateTime t) {
    if (isEmpty) return null;
    final int i = lowerBound(t);
    if (i <= 0) return 0;
    if (i >= length) return length - 1;
    final int ms = t.millisecondsSinceEpoch;
    return (ms - _timesMs[i - 1]) <= (_timesMs[i] - ms) ? i - 1 : i;
  }

  /// Whether [t] lies within the loaded bars (first open .. last open).
  bool covers(DateTime t) =>
      !isEmpty && t.millisecondsSinceEpoch >= _timesMs.first && t.millisecondsSinceEpoch <= _timesMs.last;

  SetupMark? setupById(String? id) {
    if (id == null) return null;
    for (final SetupMark m in setups) {
      if (m.item.id == id) return m;
    }
    return null;
  }

  /// Index of the first setup with `barIndex >= index` (setups are sorted).
  int firstSetupAtOrAfter(int index) {
    int lo = 0;
    int hi = setups.length;
    while (lo < hi) {
      final int mid = (lo + hi) >> 1;
      if (setups[mid].barIndex < index) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    return lo;
  }

  /// Index of the first gap with `afterIndex >= index` (gaps are sorted).
  int firstGapAtOrAfter(int index) {
    int lo = 0;
    int hi = gaps.length;
    while (lo < hi) {
      final int mid = (lo + hi) >> 1;
      if (gaps[mid].afterIndex < index) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    return lo;
  }
}
