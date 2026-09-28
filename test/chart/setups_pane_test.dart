// The bottom setups pane: full table, sort / filter, engine sum row, range
// summary bar and the «بک‌تست همین بازه» button contract.

import 'package:alpha_trader/chart/backtest_range_request.dart';
import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/chart/chart_format.dart';
import 'package:alpha_trader/chart/chart_view.dart';
import 'package:alpha_trader/chart/widgets/setups_pane.dart';
import 'package:alpha_trader/chart/widgets/setups_table.dart';
import 'package:alpha_trader/models/chart_models.dart';
import 'package:alpha_trader/theme/theme.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fake_chart_data_source.dart';

Widget _app(ChartController c, {ValueChanged<BacktestRangeRequest>? onBacktestRange, bool dark = false}) => MaterialApp(
      theme: dark ? MyThemes.getDarkTheme() : MyThemes.getLightTheme(),
      locale: const Locale('fa', 'IR'),
      supportedLocales: const <Locale>[Locale('fa', 'IR'), Locale('en', 'US')],
      localizationsDelegates: const <LocalizationsDelegate<Object>>[
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      home: Scaffold(body: ChartView(controller: c, onBacktestRange: onBacktestRange)),
    );

/// Full range (all 5 fixture setups) on a tall window so every row is built.
Future<ChartController> _pump(
  WidgetTester tester,
  FakeChartDataSource src, {
  ValueChanged<BacktestRangeRequest>? onBacktestRange,
  Size size = const Size(1900, 1300),
  bool dark = false,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final ChartController c = ChartController(source: src);
  addTearDown(c.dispose);
  await tester.pumpWidget(_app(c, onBacktestRange: onBacktestRange, dark: dark));
  await tester.runAsync(c.init);
  final List<Candle> all = src.bars('XAUUSD.x', ChartTimeframe.h1);
  c.setRange(from: all.first.time, to: all.last.time);
  await tester.runAsync(c.load);
  await tester.pumpAndSettle();
  return c;
}

const int _n = 1200;
const String _tp = 'XAUUSD.x:600:abcdef012345';
const String _slGap = 'XAUUSD.x:1160:abcdef012345';
const String _eod = 'XAUUSD.x:1180:abcdef012345';
const String _rejected = 'XAUUSD.x:1188:abcdef012345';
const String _pending = 'XAUUSD.x:1199:abcdef012345';

Finder _row(String id) => find.byKey(ValueKey<String>('setup-row-$id'));
Finder _inRow(String id, String text) => find.descendant(of: _row(id), matching: find.text(text));
Finder _inKey(String key, String text) =>
    find.descendant(of: find.byKey(ValueKey<String>(key)), matching: find.text(text), matchRoot: true);

/// Row ids from top to bottom.
List<String> _order(WidgetTester tester) {
  final List<String> ids = <String>[_tp, _slGap, _eod, _rejected, _pending].where((String id) {
    return _row(id).evaluate().isNotEmpty;
  }).toList();
  ids.sort((String a, String b) => tester.getTopLeft(_row(a)).dy.compareTo(tester.getTopLeft(_row(b)).dy));
  return ids;
}

Future<void> _pick(WidgetTester tester, String filterKey, String option) async {
  await tester.tap(find.byKey(ValueKey<String>(filterKey)));
  await tester.pumpAndSettle();
  await tester.tap(find.text(option).last);
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('full table: every column with Persian labels for TP / SL gap / end of data / rejected / pending',
      (WidgetTester tester) async {
    final FakeChartDataSource src = FakeChartDataSource(h1Count: _n);
    final ChartController c = await _pump(tester, src);
    expect(find.byKey(const ValueKey<String>('setups-pane')), findsOneWidget);
    for (final SetupColumn col in SetupColumn.values.where((SetupColumn x) => x.titleFa.isNotEmpty)) {
      expect(_inKey('setup-table', col.titleFa), findsWidgets, reason: col.titleFa);
    }

    final SetupItem tp = c.data!.setupsResult!.setups.firstWhere((SetupItem s) => s.id == _tp);
    // Decision time in local time, the UTC value in the tooltip.
    expect(_inRow(_tp, formatLocal(tp.decisionTime)), findsOneWidget);
    expect(find.byTooltip(formatUtc(tp.decisionTime)), findsWidgets);
    expect(_inRow(_tp, 'خرید'), findsOneWidget);
    expect(_inRow(_tp, 'برگشت از خط پایین'), findsOneWidget);
    expect(_inRow(_tp, 'پین‌بار صعودی (چکش)'), findsOneWidget);
    expect(_inRow(_tp, 'پذیرفته'), findsOneWidget);
    expect(_inRow(_tp, formatChartPrice(tp.entry, 2)), findsOneWidget);
    expect(_inRow(_tp, formatChartPrice(tp.stopLoss, 2)), findsOneWidget);
    expect(_inRow(_tp, formatChartPrice(tp.takeProfit, 2)), findsOneWidget);
    expect(_inRow(_tp, '0.06'), findsOneWidget);
    expect(_inRow(_tp, 'حد سود'), findsOneWidget);
    expect(_inRow(_tp, formatLocal(tp.outcome!.exitTime)), findsOneWidget);
    expect(_inRow(_tp, '2010.75'), findsOneWidget);
    expect(_inRow(_tp, '+10.50'), findsOneWidget);
    expect(_inRow(_tp, '+2.00'), findsOneWidget);
    expect(_inRow(_tp, '+38.74'), findsOneWidget);
    expect(_inRow(_tp, '✓'), findsOneWidget);
    expect(_inRow(_tp, 'معامله 3'), findsOneWidget);
    expect(_inRow(_tp, tp.reasonFa), findsOneWidget);
    // Entry = the fill; the raw bid open is in its tooltip.
    expect(find.byTooltip('open کندل ورود (bid): ${formatChartPrice(tp.entryBidOpen, 2)} — اسپرد 25 پوینت'),
        findsOneWidget);

    expect(_inRow(_slGap, 'فروش'), findsOneWidget);
    expect(_inRow(_slGap, 'حد ضرر (گپ)'), findsOneWidget);
    expect(find.byTooltip(kFakeSlGapFa), findsOneWidget, reason: 'the engine exit_reason_fa');
    expect(_inRow(_slGap, '-24.90'), findsOneWidget);
    expect(_inRow(_slGap, '-1.05'), findsOneWidget);
    expect(_inRow(_slGap, '✗'), findsOneWidget);
    expect(_inRow(_slGap, kFakePositionOpenFa), findsOneWidget);

    expect(_inRow(_eod, 'پایان داده'), findsOneWidget);
    expect(find.byTooltip(kFakeEndOfDataFa), findsWidgets, reason: 'evaluation.end_of_data_label_fa');
    expect(_inRow(_eod, '+5.12'), findsOneWidget);

    expect(_inRow(_rejected, 'ردشده'), findsOneWidget);
    expect(_inRow(_rejected, 'رد: حجم محاسبه‌شده کمتر از حداقل حجم است.'), findsOneWidget);
    expect(_inRow(_rejected, kFakeGapStopFa), findsOneWidget);
    expect(_inRow(_rejected, '—'), findsWidgets, reason: 'no outcome');
    expect(find.byTooltip(RegExp('دلیل رد: حجم محاسبه‌شده')), findsOneWidget, reason: 'full «چرا؟» text');

    expect(_inRow(_pending, 'منتظر ورود'), findsOneWidget);
    expect(_inRow(_pending, '✓'), findsNothing);
    expect(_inRow(_pending, '✗'), findsNothing);

    // Sum row = the engine summary of the whole range.
    expect(
        find.descendant(
            of: find.byKey(const ValueKey<String>('setups-sum-row')),
            matching: find.textContaining('جمع کل بازه (از موتور)', findRichText: true)),
        findsOneWidget);
    expect(_inKey('setups-sum-row', '+103.90'), findsOneWidget);
    expect(_inKey('setups-sum-row', '+2.50'), findsOneWidget);
    expect(_inKey('setups-sum-row', '4 برد / 3 باخت'), findsOneWidget);
    expect(_inKey('setups-visible-count', '5 از 5 ردیف'), findsOneWidget);
  });

  testWidgets('sorting: time (default ascending) and P&L / R with rows without an outcome last',
      (WidgetTester tester) async {
    await _pump(tester, FakeChartDataSource(h1Count: _n));
    expect(_order(tester), <String>[_tp, _slGap, _eod, _rejected, _pending]);

    await tester.tap(find.byKey(const ValueKey<String>('setups-sort-netPnl')));
    await tester.pumpAndSettle();
    expect(_order(tester), <String>[_tp, _eod, _slGap, _rejected, _pending], reason: 'P&L descending first');
    await tester.tap(find.byKey(const ValueKey<String>('setups-sort-netPnl')));
    await tester.pumpAndSettle();
    expect(_order(tester), <String>[_slGap, _eod, _tp, _rejected, _pending], reason: 'ascending; nulls still last');

    await tester.tap(find.byKey(const ValueKey<String>('setups-sort-r')));
    await tester.pumpAndSettle();
    expect(_order(tester), <String>[_tp, _eod, _slGap, _rejected, _pending]);

    await tester.tap(find.byKey(const ValueKey<String>('setups-sort-time')));
    await tester.pumpAndSettle();
    expect(_order(tester), <String>[_tp, _slGap, _eod, _rejected, _pending]);
    await tester.tap(find.byKey(const ValueKey<String>('setups-sort-time')));
    await tester.pumpAndSettle();
    expect(_order(tester), <String>[_pending, _rejected, _eod, _slGap, _tp]);
  });

  testWidgets('filters hide rows and update «n از N»; the sum row keeps the engine totals; clear',
      (WidgetTester tester) async {
    await _pump(tester, FakeChartDataSource(h1Count: _n));
    final Finder clear = find.byKey(const ValueKey<String>('setups-clear-filters'));
    expect(tester.widget<TextButton>(clear).onPressed, isNull);

    await _pick(tester, 'setups-filter-status', 'ردشده');
    expect(_order(tester), <String>[_rejected]);
    expect(_inKey('setups-visible-count', '1 از 5 ردیف'), findsOneWidget);
    expect(_inKey('setups-sum-row', '+103.90'), findsOneWidget, reason: 'engine summary, not the visible rows');
    expect(_inKey('setups-sum-row', '+2.50'), findsOneWidget);

    await tester.tap(clear);
    await tester.pumpAndSettle();
    expect(_inKey('setups-visible-count', '5 از 5 ردیف'), findsOneWidget);

    await _pick(tester, 'setups-filter-traded', 'معامله نشد');
    expect(_order(tester), <String>[_slGap, _rejected]);
    await _pick(tester, 'setups-filter-direction', 'فروش');
    expect(_order(tester), <String>[_slGap]);
    expect(_inKey('setups-visible-count', '1 از 5 ردیف'), findsOneWidget);
    await tester.tap(clear);
    await tester.pumpAndSettle();

    await _pick(tester, 'setups-filter-result', 'پایان داده');
    expect(_order(tester), <String>[_eod]);
    await _pick(tester, 'setups-filter-result', 'بدون نتیجه');
    expect(_order(tester), <String>[_rejected, _pending]);
    await _pick(tester, 'setups-filter-status', 'پذیرفته');
    expect(_order(tester), isEmpty);
    expect(find.text('هیچ ردیفی با فیلترهای فعلی نیست.'), findsOneWidget);
    expect(_inKey('setups-sum-row', '+103.90'), findsOneWidget);
    await tester.tap(clear);
    await tester.pumpAndSettle();
    expect(_order(tester), hasLength(5));
  });

  testWidgets('summary bar: engine counts, W/L, win rate, P&L, R, PF, labels and backtest comparison',
      (WidgetTester tester) async {
    await _pump(tester, FakeChartDataSource(h1Count: _n));
    expect(_inKey('summary-total', '5'), findsOneWidget);
    expect(_inKey('summary-accepted', '3'), findsOneWidget);
    expect(_inKey('summary-rejected', '1'), findsOneWidget);
    expect(_inKey('summary-pending', '1'), findsOneWidget);
    expect(_inKey('summary-wins', '4'), findsOneWidget);
    expect(_inKey('summary-losses', '3'), findsOneWidget);
    expect(_inKey('summary-open', '1'), findsOneWidget);
    expect(_inKey('summary-win-rate', '57.1%'), findsOneWidget);
    expect(_inKey('summary-net-pnl', '+103.90 \$'), findsOneWidget);
    expect(_inKey('summary-total-r', '+2.50'), findsOneWidget);
    expect(_inKey('summary-pf', '3.18'), findsOneWidget);
    expect(_inKey('summary-evaluation-label', kFakeEvaluationFa), findsOneWidget);
    expect(find.text('موقت تا تایید چک داده'), findsOneWidget);
    expect(find.text('کمیسیون صفر'), findsOneWidget);
    expect(find.text('بک‌تست همین بازه: 5 معامله، سود خالص -12.69 \$'), findsOneWidget);
    expect(find.text(kFakeWindow.noteFa!), findsOneWidget);
  });

  testWidgets('PF: ∞ when the engine says infinite, — when null', (WidgetTester tester) async {
    const SetupsSummary onlyWins = SetupsSummary(
        total: 2,
        accepted: 2,
        rejected: 0,
        pendingEntry: 0,
        closed: 2,
        wins: 2,
        losses: 0,
        breakeven: 0, //
        openEndOfData: 0,
        winRate: 1,
        netPnl: 50,
        grossProfit: 50,
        grossLoss: 0,
        profitFactor: null, //
        profitFactorInfinite: true,
        totalR: 4,
        avgR: 2,
        rCount: 2,
        openNetPnl: 0);
    await _pump(tester, FakeChartDataSource(h1Count: _n, summary: onlyWins));
    expect(_inKey('summary-pf', '∞'), findsOneWidget);
    expect(_inKey('summary-win-rate', '100.0%'), findsOneWidget);
  });

  testWidgets('PF null (no closed setups) -> «—»', (WidgetTester tester) async {
    const SetupsSummary empty = SetupsSummary(
        total: 0,
        accepted: 0,
        rejected: 0,
        pendingEntry: 0,
        closed: 0,
        wins: 0,
        losses: 0,
        breakeven: 0, //
        openEndOfData: 0,
        winRate: null,
        netPnl: 0,
        grossProfit: 0,
        grossLoss: 0,
        profitFactor: null, //
        profitFactorInfinite: false,
        totalR: 0,
        avgR: null,
        rCount: 0,
        openNetPnl: 0);
    await _pump(tester, FakeChartDataSource(h1Count: _n, summary: empty));
    expect(_inKey('summary-pf', '—'), findsOneWidget);
    expect(_inKey('summary-win-rate', '—'), findsOneWidget);
  });

  testWidgets('evaluation unavailable: engine message, counts only, no outcome columns, button disabled',
      (WidgetTester tester) async {
    await _pump(tester, FakeChartDataSource(h1Count: _n, evaluationAvailable: false),
        onBacktestRange: (BacktestRangeRequest _) => fail('must not be called'));
    expect(_inKey('summary-evaluation-unavailable', kFakeEvaluationUnavailableFa), findsOneWidget);
    expect(_inKey('summary-total', '5'), findsOneWidget);
    expect(find.byKey(const ValueKey<String>('summary-wins')), findsNothing);
    expect(find.byKey(const ValueKey<String>('summary-net-pnl')), findsNothing);
    for (final SetupColumn col in SetupColumn.values.where((SetupColumn x) => x.outcome)) {
      expect(_inKey('setup-table', col.titleFa), findsNothing, reason: col.titleFa);
    }
    expect(find.byKey(const ValueKey<String>('setups-filter-result')), findsNothing);
    expect(find.byKey(const ValueKey<String>('setups-filter-traded')), findsNothing);
    expect(_row(_tp), findsOneWidget, reason: 'rows are still listed');
    expect(find.text('مشخصات نماد در کش نیست.'), findsOneWidget, reason: 'backtest_window.message_fa');
    final Finder button = find.byKey(const ValueKey<String>('setups-backtest-range'));
    expect(tester.widget<ButtonStyleButton>(button).onPressed, isNull);
  });

  testWidgets('«بک‌تست همین بازه»: disabled without a callback', (WidgetTester tester) async {
    await _pump(tester, FakeChartDataSource(h1Count: _n));
    final Finder button = find.byKey(const ValueKey<String>('setups-backtest-range'));
    expect(tester.widget<ButtonStyleButton>(button).onPressed, isNull);
  });

  testWidgets('«بک‌تست همین بازه»: disabled when the window is unavailable', (WidgetTester tester) async {
    const BacktestWindow early =
        BacktestWindow(available: false, code: 'window_too_early', messageFa: 'بازه پیش از اولین زمان مجاز است.');
    await _pump(tester, FakeChartDataSource(h1Count: _n, window: early),
        onBacktestRange: (BacktestRangeRequest _) => fail('must not be called'));
    final Finder button = find.byKey(const ValueKey<String>('setups-backtest-range'));
    expect(tester.widget<ButtonStyleButton>(button).onPressed, isNull);
    expect(find.text('بازه پیش از اولین زمان مجاز است.'), findsOneWidget);
  });

  testWidgets('«بک‌تست همین بازه»: sends the symbol and the exact engine window', (WidgetTester tester) async {
    final List<BacktestRangeRequest> sent = <BacktestRangeRequest>[];
    await _pump(tester, FakeChartDataSource(h1Count: _n), onBacktestRange: sent.add);
    final Finder button = find.byKey(const ValueKey<String>('setups-backtest-range'));
    expect(tester.widget<ButtonStyleButton>(button).onPressed, isNotNull);
    expect(find.byTooltip(RegExp(RegExp.escape(formatUtc(kFakeWindow.from!)))), findsOneWidget);
    await tester.tap(button);
    await tester.pumpAndSettle();
    expect(sent, <BacktestRangeRequest>[
      BacktestRangeRequest(symbol: 'XAUUSD.x', from: kFakeWindow.from!, to: kFakeWindow.to!, strategy: 'stddev_channel'),
    ]);
  });

  testWidgets('details button: outcome, entry bid open and backtest flag in the dialog', (WidgetTester tester) async {
    final ChartController c = await _pump(tester, FakeChartDataSource(h1Count: _n));
    await tester.tap(find.byKey(const ValueKey<String>('setup-details-$_tp')));
    await tester.pumpAndSettle();
    expect(c.selectedSetupId, _tp);
    const String d = 'setup-details';
    expect(_inKey(d, 'نتیجه (ارزیابی مستقل این ستاپ)'), findsOneWidget);
    expect(_inKey(d, 'open کندل ورود (bid)'), findsOneWidget);
    expect(_inKey(d, 'معامله شد (معامله 3)، سود خالص بک‌تست +38.74'), findsOneWidget);
    expect(_inKey(d, '+38.74'), findsWidgets, reason: 'gross and net');
  });

  testWidgets('end-of-data details carry the engine label; collapse hides the table', (WidgetTester tester) async {
    await _pump(tester, FakeChartDataSource(h1Count: _n));
    await tester.tap(find.byKey(const ValueKey<String>('setup-details-$_eod')));
    await tester.pumpAndSettle();
    expect(_inKey('setup-details', kFakeEndOfDataFa), findsOneWidget);
    await tester.tapAt(const Offset(5, 5)); // dismiss
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey<String>('setups-pane-toggle')));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey<String>('setup-table')), findsNothing);
    expect(find.byKey(const ValueKey<String>('setups-visible-count')), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey<String>('setups-pane-toggle')));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey<String>('setup-table')), findsOneWidget);
  });

  testWidgets('H4: explanatory empty state, no summary bar', (WidgetTester tester) async {
    final ChartController c = await _pump(tester, FakeChartDataSource(h1Count: _n));
    await tester.tap(find.text('H4'));
    await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
    await tester.pumpAndSettle();
    expect(c.timeframe, ChartTimeframe.h4);
    expect(find.text(kSetupsH1OnlyFa), findsOneWidget);
    expect(find.byKey(const ValueKey<String>('setups-summary-bar')), findsNothing);
    expect(find.byKey(const ValueKey<String>('setup-table')), findsNothing);
  });

  for (final bool dark in <bool>[false, true]) {
    testWidgets('1280x720 (${dark ? 'dark' : 'light'}): no overflow, table scrolls sideways, chart keeps height',
        (WidgetTester tester) async {
      await _pump(tester, FakeChartDataSource(h1Count: _n), size: const Size(1280, 720), dark: dark);
      expect(tester.takeException(), isNull);
      expect(tester.getSize(find.byKey(const ValueKey<String>('chart-canvas'))).height, greaterThanOrEqualTo(180));
      final ScrollableState h = tester.state<ScrollableState>(find
          .descendant(of: find.byKey(const ValueKey<String>('setups-table-hscroll')), matching: find.byType(Scrollable))
          .first);
      expect(h.position.maxScrollExtent, greaterThan(0), reason: 'the wide table scrolls horizontally');
      // Drag the handle down: the pane shrinks, never below its minimum.
      await tester.drag(find.byKey(const ValueKey<String>('setups-pane-handle')), const Offset(0, 400));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(tester.getSize(find.byKey(const ValueKey<String>('setups-pane'))).height, 150);
    });
  }
}
