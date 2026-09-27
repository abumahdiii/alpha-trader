import 'dart:async';

import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/chart/chart_data.dart';
import 'package:alpha_trader/services/engine_api.dart';
import 'package:alpha_trader/models/chart_models.dart';
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
    src.error = const EngineApiException(EngineApiErrorKind.badStatus, 'پارامترهای ذخیره‌شده معتبر نیستند.',
        statusCode: 409, code: 'stored_params_invalid');
    await c.load();
    expect(c.state, ChartLoadState.error);
    expect(c.messageFa, 'پارامترهای ذخیره‌شده معتبر نیستند.');
    expect(c.engineUnavailable, isFalse);
  });

  test('engine not running -> error flagged engineUnavailable', () async {
    src.error = const EngineApiException(EngineApiErrorKind.connection, 'اتصال به موتور برقرار نشد.');
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

  test('rangeDirty: set by a range change, cleared when the load starts; auto-loads never leave it dirty', () async {
    expect(c.rangeDirty, isFalse, reason: 'nothing loaded yet');
    await c.init();
    expect(c.rangeDirty, isFalse);

    c.setRange(from: c.from!.subtract(const Duration(days: 3)));
    expect(c.rangeDirty, isTrue);
    final Future<void> pending = c.load();
    expect(c.rangeDirty, isFalse, reason: 'snapshot taken when load() starts');
    await pending;
    expect(c.rangeDirty, isFalse);

    // Back to the loaded value = clean again, without a load.
    final DateTime loadedTo = c.to!;
    c.setRange(to: loadedTo.subtract(const Duration(days: 1)));
    expect(c.rangeDirty, isTrue);
    c.setRange(to: loadedTo);
    expect(c.rangeDirty, isFalse);

    // jumpToTime outside the bars reloads around the time by itself.
    final List<bool> seen = <bool>[];
    c.addListener(() => seen.add(c.rangeDirty));
    await c.jumpToTime(src.bars('XAUUSD.x', ChartTimeframe.h1)[40].time);
    expect(c.rangeDirty, isFalse);
    // setSymbol / setTimeframe: default range + load, no highlight meanwhile.
    await c.setSymbol('BRNUSD.x');
    expect(c.rangeDirty, isFalse);
    await c.setTimeframe(ChartTimeframe.h4);
    expect(c.rangeDirty, isFalse);
    expect(seen, isNot(contains(true)), reason: 'no dirty flicker during automatic loads');
  });

  test('rangeErrorFa: start after end is kept by setRange but refused by load (no request)', () async {
    await c.init();
    final DateTime to = c.to!;
    c.setRange(from: to.add(const Duration(hours: 1)));
    expect(c.from, to.add(const Duration(hours: 1)));
    expect(c.rangeErrorFa, kRangeOrderErrorFa);
    src.calls.clear();
    final ChartData? before = c.data;
    await c.load();
    expect(src.calls, isEmpty);
    expect(c.state, ChartLoadState.ready);
    expect(c.data, same(before));

    c.setRange(from: to);
    expect(c.rangeErrorFa, isNull, reason: 'from == to is allowed');
    c.setRange(from: to.subtract(const Duration(days: 2)));
    expect(c.rangeErrorFa, isNull);
    await c.load();
    expect(src.calls.where((String s) => s.startsWith('rates')), hasLength(1));
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
      src.updateError =
          EngineApiException(EngineApiErrorKind.badStatus, 'پیام engine $code', statusCode: 409, code: code);
      await c.updateFromMt5();
      expect(c.updateOutcome!.ok, isFalse);
      expect(c.updateOutcome!.messageFa, 'پیام engine $code');
      expect(c.updateOutcome!.explanationFa, updateErrorExplanationFa(code));
      expect(c.updateOutcome!.explanationFa, isNotNull);
    }
    src.updateError = const EngineApiException(EngineApiErrorKind.badStatus, 'اتصال به MT5 برقرار نیست',
        statusCode: 503, code: 'mt5_unavailable');
    await c.updateFromMt5();
    expect(c.updateOutcome!.explanationFa, contains('MT5'));
    expect(c.updating, isFalse);
  });
}
