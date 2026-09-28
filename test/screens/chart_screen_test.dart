// ChartScreen end to end over the real EngineApi and a fake HTTP transport:
// routes, query parameters, parsing, Persian errors.

import 'package:alpha_trader/chart/chart_controller.dart';
import 'package:alpha_trader/chart/chart_view.dart';
import 'package:alpha_trader/chart/widgets/chart_canvas.dart';
import 'package:alpha_trader/providers/shell_navigation.dart';
import 'package:alpha_trader/screens/chart/chart_screen.dart';
import 'package:alpha_trader/services/engine_api.dart';
import 'package:alpha_trader/widgets/engine_gate.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../helpers/engine_fakes.dart';

final DateTime _t0 = DateTime.utc(2026, 9, 21); // Monday
const int _bars = 48;

String _iso(DateTime t) => '${t.toIso8601String().substring(0, 19)}Z';
DateTime _time(int i) => _t0.add(Duration(hours: i));

const String _prov = 'stddev_channel';

Map<String, Object?> _provenance() => {
      'symbol': 'XAUUSD.x',
      'strategy': _prov,
      'strategy_version': 1,
      'params_version': 4,
      'params_hash': '8e223612f12a0000',
      'params': {'n': 100, 'k': 2.0},
    };

Map<String, Object?> _bar(int i) => {
      'time': _iso(_time(i)),
      'server_time': '2026.09.21 ${(i % 24).toString().padLeft(2, '0')}:00',
      'open': 2600.0 + i,
      'high': 2602.5 + i,
      'low': 2598.5 + i,
      'close': 2601.0 + i,
      'tick_volume': 1000 + i,
      'spread': 25,
      'real_volume': 0,
    };

Map<String, Object?> _point(int i) => i < 5
    ? {'time': _iso(_time(i)), 'valid': false}
    : {
        'time': _iso(_time(i)),
        'valid': true,
        'mid': 2600.0 + i,
        'upper': 2620.0 + i,
        'lower': 2580.0 + i,
        'slope': 0.2,
        'sigma': 10.0,
        'is_flat': false,
        'direction': 'up',
        'atr_h1': 4.5,
        'atr_h4': 10.9,
        'h4_open': _iso(_time(i - i % 4 - 4)),
        'bars_ahead': (i % 4 + 1) / 4,
      };

Map<String, Object?> _setup(int i) => {
      'id': 'XAUUSD.x:$i:8e223612f12a',
      'symbol': 'XAUUSD.x',
      'status': 'accepted',
      'rejection_reason_fa': null,
      'setup_type': 'bounce_lower',
      'setup_title_fa': 'برگشت از خط پایین',
      'direction': 'buy',
      'pattern': 'bullish_pin_bar',
      'line': 'lower',
      'line_value': 2580.0 + i,
      'channel_direction': 'up',
      'confirmation_bar_time': _iso(_time(i)),
      'decision_time': _iso(_time(i + 1)),
      'entry_time': _iso(_time(i + 1)),
      'entry': 2601.0 + i,
      'stop_loss': 2597.0 + i,
      'take_profit': 2609.0 + i,
      'rr': 2.0,
      'risk_distance': 4.0,
      'reference_price': 2601.0 + i,
      'indicative_take_profit': 2609.0 + i,
      'volume': 0.02,
      'actual_risk': 8.0,
      'margin': 52.02,
      'risk_amount': 10.0,
      'volume_note_fa': null,
      'sizing_warnings_fa': <String>[],
      'reason_fa': 'لمس خط پایین و پین‌بار صعودی',
      'indicators': {'atr_h1': 4.5},
      // Phase 5 (engine/README.md "/chart/setups").
      'entry_bid_open': 2600.75 + i,
      'spread_at_entry_points': 25,
      'entry_spread_source': 'historical',
      'outcome': {
        'result': 'tp',
        'exit_reason': 'tp',
        'exit_reason_fa': 'حد سود',
        'exit_bar_time': _iso(_time(i + 3)),
        'exit_time': _iso(_time(i + 4)),
        'exit_price': 2609.0 + i,
        'pnl_price': 8.0,
        'gross_pnl': 16.0,
        'commission': 0.0,
        'net_pnl': 16.0,
        'r_multiple': 2.0,
        'bars_held': 3,
        'held_over_weekend': false,
        'flags': <String>[],
      },
      'backtest': {'traded': true, 'reason': null, 'reason_fa': null, 'trade_index': 1, 'net_pnl': 16.0},
    };

const String _windowFrom = '2026-08-23T23:00:00Z';
const String _windowTo = '2026-09-22T23:00:00Z';

Map<String, Object?> _phase5Blocks() => {
      'evaluation': {
        'available': true,
        'message_fa': null,
        'basis': 'independent_setups',
        'label_fa': 'ارزیابی مستقل هر ستاپ، بدون قید یک معامله باز؛ با نتیجه بک‌تست فرق دارد',
        'end_of_data_label_fa': 'پایان داده؛ ستاپ تا آخرین کندل کش باز ماند (نتیجه موقت)',
        'cost_model': {
          'spread': 'historical',
          'fallback_spread_points': null,
          'commission_per_lot_per_side': 0.0,
          'swap': 'none',
        },
        'spread_fallback': null,
        'labels_fa': ['موقت تا تایید چک داده', 'کمیسیون صفر', 'بدون سوآپ'],
      },
      'summary': {
        'total': 1,
        'accepted': 1,
        'rejected': 0,
        'pending_entry': 0,
        'closed': 1,
        'wins': 1,
        'losses': 0,
        'breakeven': 0,
        'open_end_of_data': 0,
        'win_rate': 1.0,
        'net_pnl': 16.0,
        'gross_profit': 16.0,
        'gross_loss': 0.0,
        'profit_factor': null,
        'profit_factor_infinite': true,
        'total_r': 2.0,
        'avg_r': 2.0,
        'r_count': 1,
        'open_net_pnl': 0.0,
      },
      'backtest_window': {
        'from': _windowFrom,
        'to': _windowTo,
        'clipped': false,
        'note_fa': null,
        'available': true,
        'code': null,
        'message_fa': null,
        'trades': 1,
        'net_profit': 16.0,
      },
    };

FakeEngineHttp _http({ResponseBody Function(RequestOptions r)? channel}) => FakeEngineHttp({
      'GET /settings': (_) => jsonBody(settingsJson()),
      'GET /strategies': (_) => jsonBody([strategyJson()]),
      'GET /symbols': (_) => jsonBody({
            'mt5_state': 'connected',
            'symbols': [
              {
                'symbol': 'XAUUSD.x',
                'source': 'cache',
                'spec': {
                  'name': 'XAUUSD.x',
                  'digits': 2,
                  'point': 0.01,
                  'trade_contract_size': 100.0,
                  'trade_tick_value': 1.0,
                  'trade_tick_size': 0.01,
                  'volume_min': 0.01,
                  'volume_step': 0.01,
                  'volume_max': 100.0,
                  'currency_profit': 'USD',
                  'currency_base': 'XAU',
                },
                'fetched_at_utc': null,
                'error': null,
              },
            ],
          }),
      'GET /rates/meta': (r) => jsonBody({
            'symbol': 'XAUUSD.x',
            'timeframe': r.queryParameters['timeframe'],
            'cached': true,
            'rows': _bars,
            'first_bar_utc': _iso(_time(0)),
            'last_bar_utc': _iso(_time(_bars - 1)),
            'first_available_utc': null,
            'requested_start_utc': null,
            'history_short': false,
            'offset_model': 'fixed(0)',
            'source': 'mt5',
            'fetched_at_utc': null,
          }),
      'GET /rates': (r) => jsonBody({
            'symbol': 'XAUUSD.x',
            'timeframe': r.queryParameters['timeframe'],
            'from': r.queryParameters['from'],
            'to': r.queryParameters['to'],
            'source': 'cache',
            'stale': false,
            'offset_model': 'fixed(0)',
            'count': _bars,
            'bars': [for (int i = 0; i < _bars; i++) _bar(i)],
            'gaps': <Object>[],
            'gap_counts': {'weekend': 0, 'holiday': 0, 'session_break': 0, 'missing': 0},
            'message': null,
          }),
      'GET /chart/channel': channel ??
          (r) => jsonBody({
                ..._provenance(),
                'timeframe': r.queryParameters['timeframe'],
                'from': r.queryParameters['from'],
                'to': r.queryParameters['to'],
                'count': _bars,
                'valid_count': _bars - 5,
                'points': [for (int i = 0; i < _bars; i++) _point(i)],
                'message_fa': null,
              }),
      'GET /chart/setups': (r) => jsonBody({
            ..._provenance(),
            'from': r.queryParameters['from'],
            'to': r.queryParameters['to'],
            'account': settingsJson(),
            'symbol_spec': null,
            'count': 1,
            'status_counts': {'accepted': 1, 'rejected': 0, 'pending_entry': 0},
            'setups': [_setup(40)],
            'note_fa': 'یادداشت',
            'message_fa': null,
            ..._phase5Blocks(),
          }),
      'GET /rates/gaps': (r) => jsonBody({
            'symbol': 'XAUUSD.x',
            'timeframe': r.queryParameters['timeframe'],
            'cached': true,
            'rows': _bars,
            'first_bar_utc': _iso(_time(0)),
            'last_bar_utc': _iso(_time(_bars - 1)),
            'offset_model': 'fixed(0)',
            'count': 0,
            'gaps': <Object>[],
            'gap_counts': {'weekend': 0, 'holiday': 0, 'session_break': 0, 'missing': 0},
            'missing_bars_total': 0,
            'session_break_slots': <String>[],
          }),
      'POST /rates/update': (_) => engineError(
            409,
            'offset_model_changed',
            'مدل اختلاف ساعت سرور (fixed(0)) با مدل کش XAUUSD.x H1 (us_dst(2)) فرق دارد.',
          ),
    });

Future<EngineHarness> _pumpScreen(WidgetTester tester, FakeEngineHttp http) async {
  tester.view.physicalSize = const Size(1600, 900);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  final EngineHarness h = EngineHarness(http: http);
  await tester.pumpWidget(h.wrap(const ChartScreen()));
  await h.start(tester);
  await settle(tester, 12);
  return h;
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('engine stopped -> the gate empty state, no chart requests', (WidgetTester tester) async {
    final FakeEngineHttp http = _http();
    final EngineHarness h = EngineHarness(http: http);
    await tester.pumpWidget(h.wrap(const ChartScreen()));
    await tester.pump();
    expect(find.byType(EngineUnavailableView), findsOneWidget);
    expect(find.byType(ChartView), findsNothing);
    expect(http.requests, isEmpty);
    await h.dispose(tester);
  });

  testWidgets('loads symbols -> meta range -> rates/channel/setups and draws the chart', (WidgetTester tester) async {
    final FakeEngineHttp http = _http();
    final EngineHarness h = await _pumpScreen(tester, http);

    expect(find.byType(ChartCanvas), findsOneWidget);
    final RequestOptions rates = http.sent('GET /rates').last;
    expect(rates.queryParameters['symbol'], 'XAUUSD.x');
    expect(rates.queryParameters['timeframe'], 'H1');
    // Default range = the last 30 days of cached data, ISO UTC.
    expect(rates.queryParameters['to'], '2026-09-22T23:00:00.000Z');
    expect(rates.queryParameters['from'], '2026-08-23T23:00:00.000Z');
    // Setups window = decision times of the loaded bars (+1 h).
    final RequestOptions setups = http.sent('GET /chart/setups').last;
    expect(setups.queryParameters['to'], '2026-09-23T00:00:00.000Z');
    expect(http.sent('GET /chart/channel').last.queryParameters['timeframe'], 'H1');

    // Setup table lists the engine's setup.
    expect(find.byKey(const ValueKey<String>('setup-row-XAUUSD.x:40:8e223612f12a')), findsOneWidget);
    // The candle panel is closed until a candle is clicked; then it shows digits=2 prices.
    const ValueKey<String> panel = ValueKey<String>('candle-info-panel');
    expect(find.byKey(panel), findsNothing);
    final ChartCanvasState canvas = tester.state<ChartCanvasState>(find.byType(ChartCanvas));
    await tester.tapAt(tester.getTopLeft(find.byType(ChartCanvas)) +
        Offset(canvas.viewport!.xOf(_bars - 1), canvas.priceScale!.yOf(2648.0)));
    await settle(tester);
    expect(find.byKey(panel), findsOneWidget);
    expect(
        find.descendant(of: find.byKey(panel), matching: find.text('2648.00')), findsWidgets); // last close (2601 + 47)
    expect(find.textContaining('پارامترها: نسخه 4'), findsOneWidget);
    await h.dispose(tester);
  });

  testWidgets('«بک‌تست همین بازه» opens the backtest page with the engine window and autoRun',
      (WidgetTester tester) async {
    final EngineHarness h = await _pumpScreen(tester, _http());
    // The engine's summary and outcome reach the pane over the real EngineApi.
    expect(find.descendant(of: find.byKey(const ValueKey<String>('summary-pf')), matching: find.text('∞')),
        findsOneWidget);
    expect(find.descendant(of: find.byKey(const ValueKey<String>('summary-net-pnl')), matching: find.text('+16.00 \$')),
        findsOneWidget);
    expect(h.navigation.page, ShellPage.chart);

    final Finder button = find.byKey(const ValueKey<String>('setups-backtest-range'));
    expect(tester.widget<ButtonStyleButton>(button).onPressed, isNotNull);
    await tester.tap(button);
    await settle(tester);
    expect(h.navigation.page, ShellPage.backtest);
    final BacktestPrefill p = h.navigation.pendingBacktest!;
    expect(p.symbol, 'XAUUSD.x');
    expect(p.from, DateTime.utc(2026, 8, 23, 23), reason: 'backtest_window.from as sent by the engine');
    expect(p.to, DateTime.utc(2026, 9, 22, 23), reason: 'backtest_window.to as sent by the engine');
    expect(p.autoRun, isTrue);
    await h.dispose(tester);
  });

  testWidgets('update from MT5: 409 offset_model_changed shows engine message + explanation',
      (WidgetTester tester) async {
    final EngineHarness h = await _pumpScreen(tester, _http());
    await tester.tap(find.text('اطلاعات داده'));
    await settle(tester, 40); // tab animation
    expect(find.text('fixed(0)'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey<String>('update-from-mt5')));
    await settle(tester);
    expect(find.textContaining('مدل اختلاف ساعت سرور (fixed(0))'), findsOneWidget);
    expect(find.text(updateErrorExplanationFa('offset_model_changed')!), findsOneWidget);
    expect(h.http.sent('POST /rates/update').single.data, contains('"symbol":"XAUUSD.x"'));
    await h.dispose(tester);
  });

  testWidgets('engine error on /chart/channel -> Persian error state with retry', (WidgetTester tester) async {
    final EngineHarness h = await _pumpScreen(
      tester,
      _http(channel: (_) => engineError(409, 'stored_params_invalid', 'پارامترهای ذخیره‌شده سیستم معتبر نیستند.')),
    );
    expect(find.byType(ChartCanvas), findsNothing);
    expect(find.text('پارامترهای ذخیره‌شده سیستم معتبر نیستند.'), findsOneWidget);
    expect(find.text('تلاش دوباره'), findsOneWidget);
    await h.dispose(tester);
  });

  test('EngineApi chart methods: query, parsing, error codes', () async {
    final FakeEngineHttp http = _http();
    final EngineApi api = EngineApi(port: 8765, httpClientAdapter: http);
    final ch = await api.getChartChannel(
        symbol: 'XAUUSD.x', timeframe: 'H4', from: DateTime.utc(2026, 9, 21), to: DateTime.utc(2026, 9, 22));
    expect(http.sent('GET /chart/channel').single.queryParameters, {
      'symbol': 'XAUUSD.x',
      'timeframe': 'H4',
      'from': '2026-09-21T00:00:00.000Z',
      'to': '2026-09-22T00:00:00.000Z'
    });
    expect(ch.points.first.valid, isFalse);
    expect(ch.provenance.paramsVersion, 4);
    final gaps = await api.getRatesGaps(symbol: 'XAUUSD.x', timeframe: 'H1');
    expect(gaps.cached, isTrue);
    try {
      await api.postRatesUpdate(symbol: 'XAUUSD.x');
      fail('expected 409');
    } on EngineApiException catch (e) {
      expect(e.statusCode, 409);
      expect(e.code, 'offset_model_changed');
      expect(e.messageFa, startsWith('مدل اختلاف ساعت سرور'));
    }
    api.close();
  });
}
