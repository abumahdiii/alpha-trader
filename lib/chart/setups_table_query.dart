import 'package:flutter/foundation.dart';

import '../models/chart_models.dart';

/// Sortable columns of the setups table.
enum SetupSortKey { time, direction, type, status, result, netPnl, r }

/// «نتیجه» filter values; [none] = no outcome (rejected / pending / no evaluation).
enum SetupResultFilter {
  tp('حد سود'),
  sl('حد ضرر'),
  endOfData('پایان داده'),
  none('بدون نتیجه');

  const SetupResultFilter(this.titleFa);

  final String titleFa;

  bool matches(SetupOutcome? o) => switch (this) {
        SetupResultFilter.tp => o?.result == SetupResult.tp,
        SetupResultFilter.sl => o?.result == SetupResult.sl,
        SetupResultFilter.endOfData => o?.result == SetupResult.endOfData,
        SetupResultFilter.none => o == null,
      };
}

const Object _keep = Object();

/// Sort + filters of the setups table. Pure presentation: rows are only
/// reordered and hidden, never summed (the sum row is the engine's summary).
@immutable
class SetupsTableQuery {
  const SetupsTableQuery({
    this.sortKey = SetupSortKey.time,
    this.ascending = true,
    this.status,
    this.direction,
    this.result,
    this.traded,
  });

  final SetupSortKey sortKey;
  final bool ascending;

  /// null = all.
  final SetupStatus? status;
  final TradeSide? direction;
  final SetupResultFilter? result;

  /// true = traded in the range backtest, false = not traded (a flag with
  /// `traded: false`); rows without a flag match neither.
  final bool? traded;

  bool get hasFilters => status != null || direction != null || result != null || traded != null;

  SetupsTableQuery copyWith({
    SetupSortKey? sortKey,
    bool? ascending,
    Object? status = _keep,
    Object? direction = _keep,
    Object? result = _keep,
    Object? traded = _keep,
  }) =>
      SetupsTableQuery(
        sortKey: sortKey ?? this.sortKey,
        ascending: ascending ?? this.ascending,
        status: identical(status, _keep) ? this.status : status as SetupStatus?,
        direction: identical(direction, _keep) ? this.direction : direction as TradeSide?,
        result: identical(result, _keep) ? this.result : result as SetupResultFilter?,
        traded: identical(traded, _keep) ? this.traded : traded as bool?,
      );

  /// Same sort, no filters.
  SetupsTableQuery cleared() => SetupsTableQuery(sortKey: sortKey, ascending: ascending);

  /// A header click: the same column flips the order, another column sorts
  /// by it (time ascending, P&L / R descending first).
  SetupsTableQuery sortedBy(SetupSortKey key) => key == sortKey
      ? copyWith(ascending: !ascending)
      : copyWith(sortKey: key, ascending: !(key == SetupSortKey.netPnl || key == SetupSortKey.r));

  bool matches(SetupItem s) =>
      (status == null || s.status == status) &&
      (direction == null || s.direction == direction) &&
      (result == null || result!.matches(s.outcome)) &&
      (traded == null || (s.backtest != null && s.backtest!.traded == traded));

  /// Filtered and sorted copy of [rows]. Ties (and rows without a value for
  /// the key, which always go last) keep the chronological order.
  List<SetupItem> apply(List<SetupItem> rows) {
    final List<SetupItem> out = rows.where(matches).toList();
    int chrono(SetupItem a, SetupItem b) {
      final int c = a.decisionTime.compareTo(b.decisionTime);
      return c != 0 ? c : a.id.compareTo(b.id);
    }

    int cmp(SetupItem a, SetupItem b) {
      final Comparable<dynamic>? va = _value(a);
      final Comparable<dynamic>? vb = _value(b);
      if (va == null || vb == null) {
        if (va == null && vb == null) return chrono(a, b);
        return va == null ? 1 : -1; // missing values last in both directions
      }
      final int c = ascending ? va.compareTo(vb) : vb.compareTo(va);
      return c != 0 ? c : chrono(a, b);
    }

    out.sort(
        sortKey == SetupSortKey.time ? (SetupItem a, SetupItem b) => ascending ? chrono(a, b) : chrono(b, a) : cmp);
    return out;
  }

  Comparable<dynamic>? _value(SetupItem s) => switch (sortKey) {
        SetupSortKey.time => s.decisionTime.millisecondsSinceEpoch,
        SetupSortKey.direction => s.direction.index,
        SetupSortKey.type => s.typeTitleFa,
        SetupSortKey.status => s.status.index,
        SetupSortKey.result => s.outcome?.result.index,
        SetupSortKey.netPnl => s.outcome?.netPnl,
        SetupSortKey.r => s.outcome?.rMultiple,
      };

  @override
  String toString() => 'sort=${sortKey.name}${ascending ? '↑' : '↓'} status=${status?.code ?? '*'} '
      'direction=${direction?.name ?? '*'} result=${result?.name ?? '*'} traded=${traded ?? '*'}';
}
