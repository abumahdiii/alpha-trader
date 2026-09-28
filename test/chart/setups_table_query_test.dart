import 'package:alpha_trader/chart/setups_table_query.dart';
import 'package:alpha_trader/models/chart_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fake_chart_data_source.dart';

void main() {
  final FakeChartDataSource src = FakeChartDataSource(h1Count: 1200);
  final List<SetupItem> rows = src.allSetups('XAUUSD.x');
  List<int> order(List<SetupItem> r) => <int>[for (final SetupItem s in r) rows.indexOf(s)];

  test('default: chronological, no filters', () {
    const SetupsTableQuery q = SetupsTableQuery();
    expect(q.hasFilters, isFalse);
    expect(order(q.apply(rows.reversed.toList())), <int>[0, 1, 2, 3, 4]);
  });

  test('header clicks: P&L / R start descending, same column flips, nulls always last', () {
    SetupsTableQuery q = const SetupsTableQuery().sortedBy(SetupSortKey.netPnl);
    expect(q.ascending, isFalse);
    expect(order(q.apply(rows)), <int>[0, 2, 1, 3, 4]);
    q = q.sortedBy(SetupSortKey.netPnl);
    expect(order(q.apply(rows)), <int>[1, 2, 0, 3, 4]);
    q = q.sortedBy(SetupSortKey.status);
    expect(q.ascending, isTrue);
    expect(order(q.apply(rows)), <int>[0, 1, 2, 3, 4]);
    q = q.sortedBy(SetupSortKey.direction);
    expect(order(q.apply(rows)), <int>[0, 3, 1, 2, 4], reason: 'buy first, chronological within');
    q = const SetupsTableQuery().sortedBy(SetupSortKey.result);
    expect(order(q.apply(rows)), <int>[0, 1, 2, 3, 4], reason: 'tp, sl, end of data, then no outcome');
  });

  test('filters combine; traded matches only rows with a flag; cleared keeps the sort', () {
    SetupsTableQuery q = const SetupsTableQuery(ascending: false).copyWith(traded: false);
    expect(order(q.apply(rows)), <int>[3, 1]);
    q = q.copyWith(direction: TradeSide.sell);
    expect(order(q.apply(rows)), <int>[1]);
    q = q.copyWith(traded: null, direction: null, result: SetupResultFilter.none);
    expect(order(q.apply(rows)), <int>[4, 3]);
    q = q.copyWith(status: SetupStatus.pendingEntry);
    expect(order(q.apply(rows)), <int>[4]);
    expect(q.hasFilters, isTrue);
    final SetupsTableQuery c = q.cleared();
    expect(c.hasFilters, isFalse);
    expect(c.ascending, isFalse);
    expect(const SetupsTableQuery().copyWith(traded: true).apply(rows).map((SetupItem s) => s.backtest!.traded),
        everyElement(isTrue));
  });
}
