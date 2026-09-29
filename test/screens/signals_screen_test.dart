// The «سیگنال» page: status strip, active signal cards (values, countdown,
// warnings, mini chart, details, copy), the history (paging, filters), and
// the absence of any order action.

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:alpha_trader/chart/chart_controller.dart' show kNoChannelFa;
import 'package:alpha_trader/chart/chart_painter.dart';
import 'package:alpha_trader/models/signal_models.dart';
import 'package:alpha_trader/providers/shell_navigation.dart';
import 'package:alpha_trader/screens/shell_screen.dart';
import 'package:alpha_trader/screens/signals/signal_card.dart';
import 'package:alpha_trader/screens/signals/signal_format.dart';
import 'package:alpha_trader/screens/signals/signal_mini_chart.dart';
import 'package:alpha_trader/screens/signals/signals_history.dart';
import 'package:alpha_trader/screens/signals/signals_screen.dart';
import 'package:alpha_trader/screens/signals/signals_status_strip.dart';

import '../chart/fake_chart_data_source.dart';
import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';
import '../helpers/signal_fakes.dart';

Finder _key(String k) => find.byKey(ValueKey<String>(k));

String _text(WidgetTester tester, String key) => tester.widget<Text>(_key(key)).data!;

/// 14:17:55 UTC on the fixture day: 42:05 before the 15:00 expiry.
DateTime _now = DateTime.utc(2021, 10, 26, 14, 17, 55);

class _Page {
  _Page(this.h, this.sockets, this.source);

  final EngineHarness h;
  final FakeSocketConnector sockets;
  final FakeChartDataSource source;

  Future<void> send(WidgetTester tester, Map<String, Object?> message) async {
    sockets.last.send(message);
    await settle(tester);
  }
}

Future<_Page> _pump(
  WidgetTester tester, {
  Map<String, RouteHandler>? routes,
  Widget? page,
  Size size = const Size(1500, 1400),
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  _now = DateTime.utc(2021, 10, 26, 14, 17, 55);
  final FakeSocketConnector sockets = FakeSocketConnector();
  final FakeChartDataSource source = FakeChartDataSource();
  final EngineHarness h = EngineHarness(
    http: FakeEngineHttp({'GET /symbols': (_) => jsonBody(symbolsJson()), ...?routes}),
    signalSocket: sockets.call,
  );
  await tester.pumpWidget(h.wrap(page ?? SignalsScreen(chartSource: (_) => source, clock: () => _now)));
  await h.start(tester);
  await settle(tester);
  return _Page(h, sockets, source);
}

/// Two active signals: a buy at the tick (countdown, backtest-skip and
/// provisional-gap warnings) and a sell at the last close (market closed:
/// expiry pending).
Map<String, Object?> _twoActive({Map<String, Object?>? status}) => snapshotMsg([
      signalJson(id: 1, wouldSkip: true, gapProvisional: true),
      signalJson(
        id: 2,
        symbol: 'BRENT.x',
        direction: 'sell',
        expires: null,
        entrySource: 'last_close',
        confirmation: '2021-10-26T12:00:00Z',
        entry: 84.123,
        sl: 84.9,
        tp: 82.569,
        volume: 0.5,
      ),
    ], status: status);

const List<String> _forbidden = ['ارسال', 'سفارش', 'خرید کن', 'فروش کن', 'معامله کن', 'Buy', 'Sell', 'Order'];

/// Labels (texts + tooltips) of every button-like widget on screen that
/// contain a forbidden word; the number of buttons scanned.
({List<String> offenders, int scanned}) _scanButtons(WidgetTester tester) {
  final List<String> offenders = [];
  int scanned = 0;
  bool isButton(Widget w) =>
      w is ButtonStyleButton ||
      w is IconButton ||
      w is FloatingActionButton ||
      w is PopupMenuButton ||
      w is MenuItemButton ||
      w is InkWell ||
      w is SwitchListTile;
  for (final Element e in find.byWidgetPredicate(isButton, skipOffstage: false).evaluate()) {
    scanned++;
    final Widget w = e.widget;
    final List<String> parts = [
      if (w is IconButton && w.tooltip != null) w.tooltip!,
      if (w is FloatingActionButton && w.tooltip != null) w.tooltip!,
    ];
    void visit(Element child) {
      final Widget cw = child.widget;
      if (cw is Text) parts.add(cw.data ?? cw.textSpan?.toPlainText() ?? '');
      if (cw is RichText) parts.add(cw.text.toPlainText());
      if (cw is Tooltip) parts.add(cw.message ?? '');
      child.visitChildElements(visit);
    }

    e.visitChildElements(visit);
    final String label = parts.join(' ');
    if (_forbidden.any(label.contains)) offenders.add(label);
  }
  return (offenders: offenders, scanned: scanned);
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('dashboard: status strip and active cards with the engine values, sources, warnings, countdown',
      (tester) async {
    final _Page p = await _pump(tester);
    expect(find.text(kSuggestionOnlyFa), findsOneWidget);
    expect(find.text(SignalsScreen.noActiveFa), findsOneWidget);

    await p.send(tester, _twoActive());
    expect(find.text(SignalsScreen.noActiveFa), findsNothing);
    expect(_key('signal-card-1'), findsOneWidget);
    expect(_key('signal-card-2'), findsOneWidget);
    expect(find.text('${SignalsScreen.activeTabFa} (2)'), findsOneWidget);

    // Values with the symbol digits (XAUUSD.x: 2, BRENT.x: 3), LTR, Latin digits.
    expect(_text(tester, 'signal-entry-1'), '2064.68');
    expect(find.text('2062.88'), findsOneWidget);
    expect(find.text('2068.27'), findsOneWidget);
    expect(_text(tester, 'signal-volume-1'), '0.13 لات');
    expect(find.text('R:R 2.00'), findsNWidgets(2));
    expect(find.text('23.35 \$'), findsNWidgets(2));
    expect(_text(tester, 'signal-entry-2'), '84.123');
    expect(find.text('84.900'), findsOneWidget);
    expect(find.text('قیمت لحظه‌ای'), findsOneWidget);
    expect(find.text('آخرین بسته، بازار بسته'), findsOneWidget);
    expect(find.text(kTpNoteFa), findsNWidgets(2));
    expect(find.text('دلیل: لمس خط پایین کانال صعودی و پین‌بار صعودی.'), findsNWidgets(2));

    // Warnings.
    expect(find.descendant(of: _key('signal-skip-1'), matching: find.textContaining(kPositionOpenFa)), findsOneWidget);
    expect(_key('signal-skip-2'), findsNothing);
    expect(find.descendant(of: _key('signal-gap-1'), matching: find.textContaining('موقت')), findsOneWidget);
    expect(find.textContaining('سیستم: stddev_channel v1 — پارامتر v3'), findsNWidgets(2));

    // Countdown every second; the pending one has no timer.
    expect(_text(tester, 'signal-countdown-1'), 'انقضا تا 0:42:05');
    expect(_text(tester, 'signal-countdown-2'), kExpiryPendingFa);
    _now = _now.add(const Duration(seconds: 1));
    await tester.pump(const Duration(seconds: 1));
    expect(_text(tester, 'signal-countdown-1'), 'انقضا تا 0:42:04');

    // Status strip.
    expect(_text(tester, 'signals-state'), 'در حال اجرا');
    expect(
        find.descendant(
            of: _key('signals-check-BRNUSD.x'), matching: find.textContaining('در کندل بسته‌شده ستاپی نبود.')),
        findsOneWidget);
    expect(find.descendant(of: _key('signals-check-XAUUSD.x'), matching: find.text('سیگنال صادر شد')), findsOneWidget);
    expect(_key('signals-disabled-notice'), findsNothing);
    expect(_key('signals-skew-warning'), findsNothing);
    expect(_key('signals-connection-banner'), findsNothing);
    await p.h.dispose(tester);
  });

  testWidgets('live signals off: a clear notice whose button opens the settings', (tester) async {
    final _Page p = await _pump(tester);
    await p.send(tester, snapshotMsg(const [], status: signalStatusJson(enabled: false, state: 'disabled')));
    expect(_key('signals-disabled-notice'), findsOneWidget);
    expect(find.text(SignalsStatusStrip.disabledTitle), findsOneWidget);
    expect(_text(tester, 'signals-state'), 'غیرفعال');
    await tester.tap(_key('signals-to-settings'));
    await settle(tester);
    expect(p.h.navigation.page, ShellPage.settings);
    await p.h.dispose(tester);
  });

  testWidgets('clock skew warning, last engine error, MT5 down; reconnect banner while the socket is down',
      (tester) async {
    final _Page p = await _pump(tester);
    expect(_key('signals-connection-banner'), findsOneWidget, reason: 'no snapshot yet');
    await p.send(
        tester,
        snapshotMsg(const [],
            status: signalStatusJson(
                skewWarning: true,
                skew: -14.6,
                lastError: 'اتصال به متاتریدر قطع شد.',
                state: 'mt5_down',
                mt5State: 'disconnected')));
    expect(_key('signals-skew-warning'), findsOneWidget);
    expect(
        find.descendant(of: _key('signals-skew-warning'), matching: find.textContaining('15 ثانیه')), findsOneWidget);
    expect(find.descendant(of: _key('signals-last-error'), matching: find.text('اتصال به متاتریدر قطع شد.')),
        findsOneWidget);
    expect(_text(tester, 'signals-state'), 'قطع اتصال متاتریدر');
    expect(_key('signals-connection-banner'), findsNothing);

    await p.sockets.last.controller.close();
    await settle(tester);
    expect(_key('signals-connection-banner'), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('a new signal over WS appears as a card; expired removes it', (tester) async {
    final _Page p = await _pump(tester);
    await p.send(tester, snapshotMsg(const []));
    await p.send(tester, signalMsg(signalJson(id: 7, direction: 'sell')));
    expect(_key('signal-card-7'), findsOneWidget);
    await p.send(tester, signalMsg(signalJson(id: 7, status: 'expired'), type: 'expired'));
    expect(_key('signal-card-7'), findsNothing);
    expect(find.text(SignalsScreen.noActiveFa), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('mini chart in the card: H1 bars around the confirmation, channel, highlighted candle, entry/SL/TP lines',
      (tester) async {
    final _Page p = await _pump(tester);
    await p.send(tester, _twoActive());
    await tester.tap(_key('signal-chart-toggle-1'));
    await settle(tester, 10);

    expect(find.text(SignalCard.hideChartLabel), findsOneWidget);
    final SignalMiniChartState chart = tester.state<SignalMiniChartState>(find.byType(SignalMiniChart));
    final int conf = chart.confirmationIndex!;
    expect(chart.data!.candles[conf].time, DateTime.utc(2021, 10, 26, 13));
    expect(chart.data!.candles.first.time.isBefore(DateTime.utc(2021, 10, 26, 13).subtract(const Duration(hours: 60))),
        isTrue);
    expect(chart.data!.channel.whereType<Object>(), isNotEmpty, reason: 'stddev_channel draws its channel');
    expect(p.source.channelStrategies, ['stddev_channel']);

    final ChartPainter painter = tester
        .widgetList<CustomPaint>(find.descendant(of: find.byType(SignalMiniChart), matching: find.byType(CustomPaint)))
        .map((CustomPaint c) => c.painter)
        .whereType<ChartPainter>()
        .single;
    expect(painter.highlightIndex, conf);
    expect([
      for (final ChartPriceLevel l in painter.levels) (l.kind, l.price, l.fromIndex)
    ], [
      (ChartLevelKind.entry, 2064.68, conf + 1),
      (ChartLevelKind.stopLoss, 2062.8835288730947, conf + 1),
      (ChartLevelKind.takeProfit, 2068.27294, conf + 1),
    ]);
    // Every level inside the price scale.
    for (final ChartPriceLevel l in painter.levels) {
      expect(l.price, inInclusiveRange(painter.scale.min, painter.scale.max));
    }
    expect(find.text(SignalMiniChart.legendFa), findsOneWidget);

    // Other provider traffic rebuilds the page but never reloads the bars.
    int ratesCalls() => p.source.calls.where((String c) => c.startsWith('rates')).length;
    expect(ratesCalls(), 1);
    await p.send(tester, signalMsg(signalJson(id: 9, symbol: 'BRENT.x')));
    await p.send(tester, signalMsg(signalJson(id: 1, volume: 0.14, wouldSkip: true, gapProvisional: true)));
    await settle(tester);
    expect(ratesCalls(), 1);
    expect(find.byType(SignalMiniChart), findsOneWidget);

    await tester.tap(_key('signal-chart-toggle-1'));
    await settle(tester);
    expect(find.byType(SignalMiniChart), findsNothing);
    await p.h.dispose(tester);
  });

  testWidgets('mini chart of a strategy without a channel: no channel request, the chart page note', (tester) async {
    final _Page p = await _pump(tester);
    await p.send(
        tester,
        snapshotMsg([
          signalJson(id: 3, strategy: kFakePluginName, strategySource: 'plugin', sha: kFakePluginSha),
        ]));
    expect(find.textContaining('sha 2d070ea08a45'), findsOneWidget);
    await tester.tap(_key('signal-chart-toggle-3'));
    await settle(tester, 10);
    expect(p.source.channelStrategies, isEmpty);
    expect(find.text(kNoChannelFa), findsOneWidget);
    final SignalMiniChartState chart = tester.state<SignalMiniChartState>(find.byType(SignalMiniChart));
    expect(chart.data!.channel.whereType<Object>(), isEmpty);
    await p.h.dispose(tester);
  });

  testWidgets('details dialog: every value, local + UTC times, the mini chart, the suggestion note', (tester) async {
    final _Page p = await _pump(tester);
    await p.send(tester, _twoActive());
    await tester.tap(_key('signal-details-1'));
    await settle(tester, 10);

    expect(find.text('سیگنال #1 — XAUUSD.x'), findsOneWidget);
    final Finder dialog = find.byType(Dialog);
    expect(find.descendant(of: dialog, matching: find.byType(SignalMiniChart)), findsOneWidget);
    expect(find.descendant(of: dialog, matching: find.textContaining('2021.10.26 13:00 UTC')), findsOneWidget);
    expect(find.descendant(of: dialog, matching: find.textContaining('2064.68 (قیمت لحظه‌ای)')), findsOneWidget);
    expect(find.descendant(of: dialog, matching: find.textContaining('0.1392')), findsOneWidget);
    expect(find.descendant(of: dialog, matching: find.textContaining('رد می‌کرد')), findsOneWidget);
    expect(find.descendant(of: dialog, matching: find.text(kSignalNoteFa)), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('copy action puts entry / SL / TP / volume on the clipboard as text', (tester) async {
    final List<String> copied = [];
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, (MethodCall call) async {
      if (call.method == 'Clipboard.setData') copied.add((call.arguments as Map)['text'] as String);
      return null;
    });
    addTearDown(() => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, null));
    final _Page p = await _pump(tester);
    await p.send(tester, _twoActive());
    await tester.tap(_key('signal-copy-1'));
    await settle(tester);
    expect(copied.single, 'XAUUSD.x خرید (buy)\nEntry ≈ 2064.68\nSL 2062.88\nTP ≈ 2068.27\nVolume 0.13');
    expect(find.text(SignalCard.copiedToast), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('NO order / trade / send button anywhere on the signal screens (cards, chart, details, history)',
      (tester) async {
    final _Page p = await _pump(tester, routes: {
      'GET /signals': (_) => jsonBody(signalsPageJson([
            signalJson(id: 1),
            signalJson(id: 2, status: 'rejected', rejection: 'قیمت فعلی آن طرف حد ضرر است.'),
          ])),
    });
    await p.send(tester, _twoActive());
    await tester.tap(_key('signal-chart-toggle-1'));
    await settle(tester, 10);
    ({List<String> offenders, int scanned}) r = _scanButtons(tester);
    expect(r.offenders, isEmpty);
    expect(r.scanned, greaterThanOrEqualTo(6), reason: 'the scan saw the page buttons');

    await tester.tap(_key('signal-details-1'));
    await settle(tester, 10);
    r = _scanButtons(tester);
    expect(r.offenders, isEmpty);
    await tester.tapAt(const Offset(5, 5)); // barrier: closes the dialog
    await settle(tester, 10);
    await tester.pump(const Duration(milliseconds: 400)); // exit transition
    expect(find.byType(Dialog), findsNothing);

    await tester.tap(find.text(SignalsScreen.historyTabFa));
    await settle(tester, 40);
    expect(_key('signals-history-row-0'), findsOneWidget);
    r = _scanButtons(tester);
    expect(r.offenders, isEmpty);
    await p.h.dispose(tester);
  });

  testWidgets('the button scan itself catches an order-like button (control)', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Column(children: [
          FilledButton(onPressed: () {}, child: const Text('ارسال سفارش')),
          IconButton(onPressed: () {}, tooltip: 'Buy now', icon: const Icon(Icons.add)),
        ]),
      ),
    ));
    final List<String> offenders = _scanButtons(tester).offenders;
    expect(offenders.where((String l) => l.contains('ارسال سفارش')), isNotEmpty);
    expect(offenders.where((String l) => l.contains('Buy now')), isNotEmpty);
  });

  testWidgets('history: paged table with filters; a row opens the details', (tester) async {
    final List<Map<String, String>> queries = [];
    Map<String, Object?> page(Map<String, String> q) {
      final int offset = int.parse(q['offset'] ?? '0');
      final int limit = int.parse(q['limit'] ?? '50');
      const int total = 120;
      final int n = (total - offset).clamp(0, limit);
      return signalsPageJson([
        for (int i = 0; i < n; i++)
          signalJson(
            id: total - offset - i,
            status: q['status'] ?? (i.isEven ? 'expired' : 'rejected'),
            rejection: i.isEven ? null : 'حجم محاسبه‌شده کمتر از حداقل حجم است.',
            symbol: q['symbol'] ?? 'XAUUSD.x',
          ),
      ], total: total, offset: offset, limit: limit);
    }

    final _Page p = await _pump(tester, routes: {
      'GET /signals': (r) {
        queries.add(r.uri.queryParameters);
        return jsonBody(page(r.uri.queryParameters));
      },
    });
    await p.send(tester, snapshotMsg(const []));
    queries.clear();
    await tester.tap(find.text(SignalsScreen.historyTabFa));
    await settle(tester, 40);

    expect(queries.single, {'limit': '50', 'offset': '0'});
    expect(_text(tester, 'signals-history-range'), '1–50 از 120');
    expect(
        find.descendant(
            of: _key('signals-history-row-1'), matching: find.text('حجم محاسبه‌شده کمتر از حداقل حجم است.')),
        findsOneWidget);
    expect(find.descendant(of: _key('signals-history-row-0'), matching: find.text('منقضی')), findsOneWidget);
    expect(tester.widget<ButtonStyleButton>(_key('signals-history-prev')).onPressed, isNull);

    await tester.tap(_key('signals-history-next'));
    await settle(tester, 20);
    expect(queries.last, {'limit': '50', 'offset': '50'});
    expect(_text(tester, 'signals-history-range'), '51–100 از 120');
    await tester.tap(_key('signals-history-next'));
    await settle(tester, 20);
    expect(_text(tester, 'signals-history-range'), '101–120 از 120');
    expect(tester.widget<ButtonStyleButton>(_key('signals-history-next')).onPressed, isNull);

    // Status filter: back to the first page.
    await tester.tap(_key('signals-history-status'));
    await settle(tester);
    await tester.tap(find.text(LiveSignalStatus.rejected.labelFa).last);
    await settle(tester, 20);
    expect(queries.last, {'status': 'rejected', 'limit': '50', 'offset': '0'});

    // Symbol filter (the engine's symbols).
    await tester.tap(_key('signals-history-symbol'));
    await settle(tester);
    await tester.tap(find.text('BRENT.x').last);
    await settle(tester, 20);
    expect(queries.last, {'status': 'rejected', 'symbol': 'BRENT.x', 'limit': '50', 'offset': '0'});

    // «همه وضعیت‌ها» clears the status filter.
    await tester.tap(_key('signals-history-status'));
    await settle(tester);
    await tester.tap(find.text(SignalsHistory.allStatusesFa).last);
    await settle(tester, 20);
    expect(queries.last, {'symbol': 'BRENT.x', 'limit': '50', 'offset': '0'});

    await tester.tap(_key('signals-history-row-0'));
    await settle(tester, 10);
    expect(find.text('سیگنال #120 — BRENT.x'), findsOneWidget);
    expect(find.descendant(of: find.byType(Dialog), matching: find.byType(SignalMiniChart)), findsOneWidget);
    await p.h.dispose(tester);
  });

  testWidgets('shell: badge with the active count; a NEW signal shows an in-app notice (not the snapshot)',
      (tester) async {
    final _Page p = await _pump(tester, page: const ShellScreen(), routes: {
      'GET /settings': (_) => jsonBody(settingsJson()),
      'GET /strategies': (_) => jsonBody([strategyJson()]),
    });
    final Badge badge0 = tester.widget<Badge>(_key('shell-signal-badge'));
    expect(badge0.isLabelVisible, isFalse);

    await p.send(tester, _twoActive());
    Badge badge = tester.widget<Badge>(_key('shell-signal-badge'));
    expect(badge.isLabelVisible, isTrue);
    expect((badge.label! as Text).data, '2');
    expect(find.byKey(const ValueKey<String>('signal-notice-1')), findsNothing, reason: 'snapshot: no notice');

    await p.send(tester, signalMsg(signalJson(id: 8, symbol: 'BRENT.x', direction: 'sell', entry: 84.5, sl: 85.25)));
    badge = tester.widget<Badge>(_key('shell-signal-badge'));
    expect((badge.label! as Text).data, '3');
    expect(_key('signal-notice-8'), findsOneWidget);
    expect(find.descendant(of: _key('signal-notice-8'), matching: find.text('سیگنال جدید: BRENT.x — فروش')),
        findsOneWidget);
    expect(find.descendant(of: _key('signal-notice-8'), matching: find.textContaining('ورود تقریبی 84.500')),
        findsOneWidget);

    await tester.tap(find.text('مشاهده سیگنال'));
    await settle(tester);
    expect(p.h.navigation.page, ShellPage.signal);
    await p.h.dispose(tester);
  });
}
