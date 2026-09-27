import 'dart:async';

import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/chart/chart_data_source.dart';
import 'package:alpha_trader/chart/chart_models.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fake_chart_data_source.dart';

void main() {
  late FakeChartDataSource src;
  late ChartController c;

  setUp(() {
    src = FakeChartDataSource(h1Count: 1200);
    c = ChartController(source: src);
  });

  tearDown(() => c.dispose());

  test('init: loading -> ready, default range = last 30 days of data', () async {
    final List<ChartLoadState> states = <ChartLoadState>[];
    c.addListener(() => states.add(c.state));
    await c.init();
    expect(states.first, ChartLoadState.loading);
    expect(c.state, ChartLoadState.ready);
    expect(c.symbol, 'XAUUSD.x');
    final DateTime last = src.bars('XAUUSD.x', ChartTimeframe.h1).last.time;
    expect(c.to, last);
    expect(c.from, last.subtract(const Duration(days: 30)));
    final int expected = src.bars('XAUUSD.x', ChartTimeframe.h1).where((Candle b) => !b.time.isBefore(c.from!)).length;
    expect(c.data!.length, expected);
    expect(c.data!.digits, 2);
    // Setups are windowed by decision time shifted one bar: the pending
    // setup on the newest bar is included.
    expect(c.data!.setups.map((m) => m.item.status), contains(SetupStatus.pendingEntry));
    expect(src.calls, contains('setups XAUUSD.x'));
  });

  test('H4 does not request setups', () async {
    await c.init();
    src.calls.clear();
    await c.setTimeframe(ChartTimeframe.h4);
    expect(c.state, ChartLoadState.ready);
    expect(src.calls.where((String s) => s.startsWith('setups')), isEmpty);
    expect(c.data!.timeframe, ChartTimeframe.h4);
    expect(c.data!.setups, isEmpty);
  });

  test('empty range -> empty state with a Persian message', () async {
    await c.init();
    c.setRange(from: DateTime.utc(2000), to: DateTime.utc(2000, 2));
    await c.load();
    expect(c.state, ChartLoadState.empty);
    expect(c.messageFa, isNotEmpty);
  });

  test('engine error -> error state with the engine Persian message', () async {
    await c.init();
    src.error = const ChartDataException(ChartErrorKind.http, 'پارامترهای ذخیره‌شده معتبر نیستند.',
        statusCode: 409, code: 'stored_params_invalid');
    await c.load();
    expect(c.state, ChartLoadState.error);
    expect(c.messageFa, 'پارامترهای ذخیره‌شده معتبر نیستند.');
    expect(c.engineUnavailable, isFalse);
  });

  test('engine not running -> error flagged engineUnavailable', () async {
    src.error = const ChartDataException(ChartErrorKind.engineUnavailable, 'x');
    await c.init();
    expect(c.state, ChartLoadState.error);
    expect(c.engineUnavailable, isTrue);
    expect(c.messageFa, kEngineUnavailableFa);
  });

  test('a stale response (inputs changed meanwhile) is ignored', () async {
    await c.init();
    final Completer<void> gate = Completer<void>();
    src.gates['XAUUSD.x'] = gate;
    final Future<void> slow = c.load(); // blocked in rates()
    src.gates.remove('XAUUSD.x');
    await c.setSymbol('BRNUSD.x');
    expect(c.data!.symbol, 'BRNUSD.x');
    gate.complete();
    await slow;
    expect(c.data!.symbol, 'BRNUSD.x', reason: 'old XAUUSD response must not overwrite');
    expect(c.state, ChartLoadState.ready);
  });

  test('selectSetup with jump centres its bar; jumpToTime inside and outside the range', () async {
    await c.init();
    final m = c.data!.setups.first;
    final int before = c.viewRequest.serial;
    c.selectSetup(m.item.id, jump: true);
    expect(c.selectedSetup, same(m));
    expect(c.highlightIndex, m.barIndex);
    expect(c.viewRequest.serial, before + 1);
    expect(c.viewRequest.focusIndex, m.barIndex);

    final DateTime inside = c.data!.candles[10].time;
    await c.jumpToTime(inside);
    expect(c.viewRequest.focusIndex, 10);

    final DateTime old = src.bars('XAUUSD.x', ChartTimeframe.h1)[40].time;
    await c.jumpToTime(old);
    expect(c.data!.covers(old), isTrue);
    expect(c.data!.candles[c.viewRequest.focusIndex!].time, old);
    expect(c.highlightIndex, c.viewRequest.focusIndex);
  });

  test('data info loads meta + gaps', () async {
    await c.init();
    await c.refreshDataInfo();
    expect(c.dataInfo!.meta.rows, 1200);
    expect(c.dataInfo!.gaps.gapCounts['missing'], 1);
    expect(c.dataInfo!.digits, 2);
  });

  test('update: success reloads; 409 codes get a Persian explanation; 503 MT5', () async {
    await c.init();
    src.calls.clear();
    await c.updateFromMt5();
    expect(c.updateOutcome!.ok, isTrue);
    expect(src.calls, containsAllInOrder(<String>['update XAUUSD.x', 'meta XAUUSD.x H1']));
    expect(src.calls.any((String s) => s.startsWith('rates')), isTrue);

    for (final String code in <String>['no_cache', 'not_incremental', 'offset_model_changed']) {
      src.updateError = ChartDataException(ChartErrorKind.http, 'پیام engine $code', statusCode: 409, code: code);
      await c.updateFromMt5();
      expect(c.updateOutcome!.ok, isFalse);
      expect(c.updateOutcome!.messageFa, 'پیام engine $code');
      expect(c.updateOutcome!.explanationFa, updateErrorExplanationFa(code));
      expect(c.updateOutcome!.explanationFa, isNotNull);
    }
    src.updateError = const ChartDataException(ChartErrorKind.http, 'اتصال به MT5 برقرار نیست',
        statusCode: 503, code: 'mt5_unavailable');
    await c.updateFromMt5();
    expect(c.updateOutcome!.explanationFa, contains('MT5'));
    expect(c.updating, isFalse);
  });
}
