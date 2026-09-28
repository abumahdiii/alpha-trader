// JSON shapes of the engine's /backtests routes (engine/alpha_engine/routes/
// backtests.py, backtest/models.py, backtest/metrics.py, backtest/results.py),
// a fake progress socket and a fake /backtests engine for widget and
// controller tests. No socket, no timer outside the test's control.

import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';

import 'package:alpha_trader/services/backtest_progress.dart';
import 'package:alpha_trader/services/engine_api.dart';

import 'engine_fakes.dart';

const String kHash = '8e223612f12a0000b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4';

Map<String, Object?> runSummaryJson({
  int id = 7,
  String status = 'done',
  String mode = 'manual',
  String symbol = 'XAUUSD.x',
  double? progress,
  int? tradeCount = 3,
  double? netProfit = 150.25,
  double? netProfitPct = 6.01,
  int? seed,
  Map<String, Object?>? error,
}) =>
    {
      'id': id,
      'created_at': '2026-09-27T18:00:00Z',
      'started_at': '2026-09-27T18:00:01Z',
      'finished_at': status == 'done' ? '2026-09-27T18:00:03Z' : null,
      'status': status,
      'progress': progress ?? (status == 'done' ? 100.0 : 0.0),
      'symbol': symbol,
      'mode': mode,
      'from': mode == 'manual' ? '2023-01-02T00:00:00Z' : null,
      'to': mode == 'manual' ? '2024-01-01T00:00:00Z' : null,
      'windows_count': mode == 'random' ? 3 : null,
      'window_months': mode == 'random' ? 3 : null,
      'seed': mode == 'random' ? (seed ?? 42) : null,
      'seed_generated': mode == 'random' ? false : null,
      'strategy': 'stddev_channel',
      'strategy_version': 1,
      'params_version': 4,
      'params_hash': kHash,
      'provisional': true,
      'trade_count': status == 'done' ? tradeCount : null,
      'net_profit': status == 'done' ? netProfit : null,
      'net_profit_pct': status == 'done' ? netProfitPct : null,
      'summary_basis': mode == 'manual' ? 'single_window' : 'window_mean',
      'elapsed_s': status == 'done' ? 2.1 : null,
      'error': error,
    };

Map<String, Object?> metricsJson({int trades = 3, double net = 150.25}) => {
      'initial_balance': 2500.0,
      'final_balance': 2500.0 + net,
      'trade_count': trades,
      'win_count': trades == 0 ? 0 : 2,
      'loss_count': trades == 0 ? 0 : 1,
      'breakeven_count': 0,
      'win_rate': trades == 0 ? null : 0.6666666667,
      'gross_profit': trades == 0 ? 0.0 : 250.25,
      'gross_loss': trades == 0 ? 0.0 : 100.0,
      'net_profit': net,
      'net_profit_pct': net / 2500 * 100,
      'profit_factor': trades == 0 ? null : 2.5025,
      'profit_factor_infinite': false,
      'expectancy': trades == 0 ? null : net / trades,
      'avg_win': trades == 0 ? null : 125.125,
      'avg_loss': trades == 0 ? null : -100.0,
      'largest_win': trades == 0 ? null : 150.25,
      'largest_loss': trades == 0 ? null : -100.0,
      'max_consecutive_wins': trades == 0 ? 0 : 2,
      'max_consecutive_losses': trades == 0 ? 0 : 1,
      'avg_r': trades == 0 ? null : 0.8333,
      'r_count': trades,
      'max_drawdown_abs': trades == 0 ? 0.0 : 120.5,
      'max_drawdown_pct': trades == 0 ? 0.0 : 4.6,
      'max_drawdown_peak_time': trades == 0 ? null : '2023-03-01T10:00:00+00:00',
      'max_drawdown_trough_time': trades == 0 ? null : '2023-03-02T14:00:00+00:00',
      'sharpe': trades == 0 ? null : 1.234,
      'sharpe_return_count': 250,
    };

Map<String, Object?> windowJson(int index, {double net = 150.25, int trades = 3, String? start, String? end}) => {
      'index': index,
      'start': start ?? '2023-0${index + 1}-02T00:00:00Z',
      'end': end ?? '2023-0${index + 4}-02T00:00:00Z',
      'initial_balance': 2500.0,
      'final_balance': 2500.0 + net,
      'net_profit': net,
      'net_profit_pct': net / 2500 * 100,
      'trade_count': trades,
      'skipped_count': 1,
      'candidates': trades + 1,
      'bars': 1500,
      'first_bar_time': '2023-01-02T00:00:00Z',
      'last_bar_time': '2023-03-31T23:00:00Z',
      'zero_spread_bars_filled': 10,
      'zero_spread_bars_unfilled': 0,
      'weekend_holds': 1,
      'spread_fallback_bars': 1400,
      'stopped_reason': null,
      'equity_points_full': 1501,
      'equity_points_stored': 95,
      'metrics': metricsJson(trades: trades, net: net),
    };

Map<String, Object?> _detailCommon(Map<String, Object?> summary, String mode) => {
      ...summary,
      'labels_fa': [
        'موقت تا تایید چک داده',
        'کمیسیون صفر',
        'بدون سوآپ',
        'اسپرد تاریخی بروکر فقط از 2026-08-10 19:00 UTC؛ برای 95.0٪ کندل‌ها اسپرد ثابت 34 پوینت (میانه اسپرد مشاهده‌شده) فرض شد',
      ],
      'provisional_label_fa': 'موقت تا تایید چک داده',
      'request': {'symbol': 'XAUUSD.x', 'mode': mode},
      'config': {
        'symbol': 'XAUUSD.x',
        'mode': mode,
        'start': mode == 'manual' ? '2023-01-02T00:00:00Z' : null,
        'end': mode == 'manual' ? '2024-01-01T00:00:00Z' : null,
        'windows_count': 3,
        'window_months': 3,
        'seed': mode == 'random' ? 42 : null,
        'cost_model': {
          'spread': 'historical',
          'fallback_spread_points': null,
          'commission_per_lot_per_side': 0.0,
          'swap': 'none',
        },
        'account': {'balance': 2500.0, 'risk_pct': 1.0, 'leverage': 100, 'rr': 2.0},
        'strategy_name': 'stddev_channel',
        'strategy_version': 1,
        'params': {'n': 100, 'k': 2.0},
        'params_version': 4,
        'params_hash': kHash,
        'provisional': true,
      },
      'fingerprint': {
        'symbol': 'XAUUSD.x',
        'h1_rows': 30000,
        'h4_rows': 7500,
        'h1_first_bar': '2021-09-01T00:00:00Z',
        'h1_last_bar': '2026-09-25T20:00:00Z',
        'h4_first_bar': '2021-09-01T00:00:00Z',
        'h4_last_bar': '2026-09-25T16:00:00Z',
        'h1_source': 'mt5',
        'h4_source': 'mt5',
        'h1_fetched_at_utc': '2026-09-26T08:00:00Z',
        'h4_fetched_at_utc': '2026-09-26T08:00:00Z',
        'h1_sha256': 'aa' * 32,
        'h4_sha256': 'bb' * 32,
        'spec': {'name': 'XAUUSD.x', 'digits': 2, 'point': 0.01},
        'spec_source': 'cache',
      },
      'result_meta': {'entry_rule': 'next_bar_open', 'price_basis': 'bid_bars', 'future_key': 1},
      'spread_fallback': {
        'points': 34,
        'source': 'auto_median_observed',
        'observed_from': '2026-08-10T19:00:00Z',
        'observed_to': '2026-09-25T20:00:00Z',
        'observed_bars': 800,
        'observed_median': 33.5,
        'fallback_bars': 1400,
        'zero_bars': 0,
        'total_bars': 1500,
      },
      'equity_storage_rule': 'first + last + last point of each UTC day',
      'timings': {'load_s': 0.1, 'total_s': 2.0},
      'live': null,
      'some_future_field': {'x': 1},
    };

/// A finished manual run (`metrics_kind: single_window`).
Map<String, Object?> manualDetailJson({int id = 7, int trades = 3, double net = 150.25}) => {
      ..._detailCommon(runSummaryJson(id: id, tradeCount: trades, netProfit: net, netProfitPct: net / 25), 'manual'),
      'plan': {
        'mode': 'manual',
        'windows': [
          {'index': 0, 'start': '2023-01-02T00:00:00Z', 'end': '2024-01-01T00:00:00Z'},
        ],
        'seed': null,
        'seed_generated': false,
        'algorithm': null,
        'numpy_version': null,
        'windows_count': null,
        'window_months': null,
        'earliest_start': '2021-11-18T20:00:00Z',
        'data_end': '2026-09-25T21:00:00Z',
        'eligible_starts': null,
        'warmup_h4_bars': 140,
      },
      'metrics_kind': 'single_window',
      'metrics': metricsJson(trades: trades, net: net),
      'distribution': null,
      'windows': [windowJson(0, net: net, trades: trades, start: '2023-01-02T00:00:00Z', end: '2024-01-01T00:00:00Z')],
    };

/// A finished random run of 3 windows (`metrics_kind: random_aggregate`).
Map<String, Object?> randomDetailJson({int id = 8}) => {
      ..._detailCommon(
          runSummaryJson(id: id, mode: 'random', tradeCount: 6, netProfit: 40.0, netProfitPct: 1.6), 'random'),
      'plan': {
        'mode': 'random',
        'windows': [
          for (int i = 0; i < 3; i++)
            {'index': i, 'start': '2023-0${i + 1}-02T00:00:00Z', 'end': '2023-0${i + 4}-02T00:00:00Z'},
        ],
        'seed': 42,
        'seed_generated': false,
        'algorithm': 'numpy.PCG64',
        'numpy_version': '2.1.0',
        'windows_count': 3,
        'window_months': 3,
        'earliest_start': '2021-11-18T20:00:00Z',
        'data_end': '2026-09-25T21:00:00Z',
        'eligible_starts': 20000,
        'warmup_h4_bars': 140,
      },
      'metrics_kind': 'random_aggregate',
      'metrics': {
        'window_count': 3,
        'windows_with_trades': 2,
        'initial_balance': 2500.0,
        'total_trades': 6,
        'pooled': {
          'trade_count': 6,
          'win_count': 3,
          'loss_count': 3,
          'breakeven_count': 0,
          'win_rate': 0.5,
          'gross_profit': 400.0,
          'gross_loss': 280.0,
          'profit_factor': 1.4286,
          'profit_factor_infinite': false,
          'expectancy': 20.0,
          'avg_win': 133.33,
          'avg_loss': -93.33,
          'largest_win': 200.0,
          'largest_loss': -120.0,
          'avg_r': 0.4,
          'r_count': 6,
        },
        'mean': {
          'net_profit': 40.0,
          'net_profit_pct': 1.6,
          'max_drawdown_abs': 90.0,
          'max_drawdown_pct': 3.5,
          'trade_count': 2.0,
          'win_rate': 0.5,
          'win_rate_windows': 2,
          'sharpe': 0.8,
          'sharpe_windows': 2,
        },
        'worst': {'max_drawdown_abs': 150.0, 'max_drawdown_pct': 5.9, 'net_profit_pct': -4.0},
        'note_fa': 'پنجره‌های تصادفی مستقل هستند.',
      },
      'distribution': {
        'count': 3,
        'windows_without_trades': 1,
        'mean_net_profit': 40.0,
        'median_net_profit': 20.0,
        'mean_net_profit_pct': 1.6,
        'median_net_profit_pct': 0.8,
        'pct_profitable': 66.66666666666667,
        'worst_window': {'index': 2, 'net_profit': -100.0, 'net_profit_pct': -4.0, 'trade_count': 3},
        'best_window': {'index': 0, 'net_profit': 200.0, 'net_profit_pct': 8.0, 'trade_count': 3},
      },
      'windows': [
        windowJson(0, net: 200.0, trades: 3),
        windowJson(1, net: 20.0, trades: 0),
        windowJson(2, net: -100.0, trades: 3),
      ],
    };

Map<String, Object?> tradeJson(int i, {int window = 0, double netPnl = 50.0, String direction = 'buy'}) => {
      'window_index': window,
      'trade_index': i,
      'direction': direction,
      'setup_type': 'bounce',
      'line': 'lower',
      'pattern': 'pin_bar',
      'confirmation_bar_time': '2023-02-0${i + 1}T09:00:00Z',
      'decision_time': '2023-02-0${i + 1}T10:00:00Z',
      'entry_time': '2023-02-0${i + 1}T10:00:00Z',
      'entry': 1850.12345 + i,
      'entry_bid_open': 1849.8,
      'stop_loss': 1840.5,
      'take_profit': 1870.0,
      'rr': 2.0,
      'volume': 0.05,
      'risk_amount': 25.0,
      'balance_before': 2500.0,
      'exit_bar_time': '2023-02-0${i + 2}T12:00:00Z',
      'exit_time': '2023-02-0${i + 2}T13:00:00Z',
      'exit_price': 1870.0,
      'exit_reason': netPnl >= 0 ? 'tp' : 'sl',
      'exit_reason_fa': netPnl >= 0 ? 'حد سود' : 'حد ضرر',
      'gross_pnl': netPnl,
      'commission': 0.0,
      'net_pnl': netPnl,
      'r_multiple': netPnl / 25,
      'balance_after': 2500.0 + netPnl,
      'bars_held': 27,
      'spread_at_entry_points': 34,
      'spread_at_exit_points': null,
      'held_over_weekend': false,
      'flags': <String>[],
      'sizing_warnings_fa': <String>[],
      'reason_fa': 'بازگشت از خط پایین کانال با پین‌بار',
      'indicators': {'atr_h1': 4.2},
    };

Map<String, Object?> tradesPageJson(int runId, List<Map<String, Object?>> trades, {int? total}) => {
      'run_id': runId,
      'window': null,
      'total': total ?? trades.length,
      'offset': 0,
      'limit': 5000,
      'trades': trades,
    };

Map<String, Object?> equityJson(int runId, {int windows = 1}) => {
      'run_id': runId,
      'window': null,
      'downsampled': true,
      'rule': 'first + last + ...',
      'count': windows * 4,
      'points': [
        for (int w = 0; w < windows; w++)
          for (int i = 0; i < 4; i++)
            {
              'window_index': w,
              'time': '2023-01-0${i + 2}T00:00:00Z',
              'balance': 2500.0 + i * 10,
              'equity': 2500.0 + i * 12 - (i == 2 ? 30 : 0),
            },
      ],
    };

Map<String, Object?> skippedJson(int runId, {int count = 1, int window = 0}) => {
      'run_id': runId,
      'window': null,
      'count': count,
      'skipped': [
        for (int i = 0; i < count; i++)
          {
            'window_index': window,
            'time': '2023-02-10T10:00:00Z',
            'confirmation_bar_time': '2023-02-10T09:00:00Z',
            'direction': 'sell',
            'setup_type': 'bounce',
            'reason': 'position_open',
            'reason_fa': 'در زمان تصمیم یک معامله باز وجود داشت (حداکثر یک معامله باز)؛ کاندید کنار گذاشته شد.',
            'detail': null,
          },
      ],
    };

Map<String, Object?> symbolsJson() => {
      'mt5_state': 'connected',
      'symbols': [
        for (final String s in ['XAUUSD.x', 'BRENT.x'])
          {
            'symbol': s,
            'source': 'cache',
            'spec': {
              'name': s,
              'digits': s == 'XAUUSD.x' ? 2 : 3,
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
            'fetched_at_utc': '2026-09-26T08:00:00Z',
            'error': null,
          },
      ],
    };

// ------------------------------------------------------------------ socket

/// A progress socket driven by the test.
class FakeProgressSocket implements ProgressSocket {
  FakeProgressSocket(this.uri, {this.failReady = false});

  final Uri uri;
  final bool failReady;
  final StreamController<Object?> controller = StreamController<Object?>();
  bool closed = false;

  @override
  Stream<Object?> get messages => controller.stream;

  @override
  Future<void> get ready => failReady ? Future<void>.error(StateError('connection refused')) : Future<void>.value();

  /// Sends one engine message (JSON text, like the real socket).
  void send(Map<String, Object?> message) => controller.add(jsonEncode(message));

  @override
  Future<void> close() async {
    closed = true;
    if (!controller.isClosed) await controller.close();
  }
}

/// Records every socket it opens.
class FakeSocketConnector {
  FakeSocketConnector({this.failReady = false, this.throwOnConnect = false});

  final bool failReady;
  final bool throwOnConnect;
  final List<FakeProgressSocket> sockets = [];

  FakeProgressSocket get last => sockets.last;

  ProgressSocket call(Uri uri) {
    if (throwOnConnect) throw StateError('no socket');
    final FakeProgressSocket s = FakeProgressSocket(uri, failReady: failReady);
    sockets.add(s);
    return s;
  }

  /// Watchers with this connector and a short poll interval.
  BacktestProgressWatcher watcher(EngineApi api, int id) => BacktestProgressWatcher(
        api: api,
        runId: id,
        connect: call,
        pollInterval: const Duration(milliseconds: 20),
        connectTimeout: const Duration(milliseconds: 200),
      );
}

Map<String, Object?> progressMsg(int id, String phase, double percent, {String status = 'running'}) => {
      'type': 'progress',
      'id': id,
      'status': status,
      'phase': phase,
      'percent': percent,
      'window': 1,
      'window_index': 0,
      'windows': 1,
      'cancel_requested': false,
    };

Map<String, Object?> doneMsg(int id) => {
      'type': 'done',
      'id': id,
      'status': 'done',
      'percent': 100.0,
      'trade_count': 3,
      'net_profit': 150.25,
      'net_profit_pct': 6.01,
    };

/// A fake engine for the /backtests routes around a mutable list of runs.
class FakeBacktestEngine {
  FakeBacktestEngine({List<Map<String, Object?>>? runs, Map<int, Map<String, Object?>>? details})
      : runs = runs ?? [],
        details = details ?? {};

  final List<Map<String, Object?>> runs;
  final Map<int, Map<String, Object?>> details;
  final Map<int, List<Map<String, Object?>>> trades = {};
  int nextId = 20;

  /// Response of POST /backtests (null = 202 queued with [nextId]).
  ResponseBody Function(RequestOptions r)? onSubmit;

  static final RegExp _id = RegExp(r'^/backtests/(\d+)(/(\w+))?$');

  FakeEngineHttp http({Map<String, RouteHandler>? extra}) {
    final FakeEngineHttp http = FakeEngineHttp({
      'GET /symbols': (_) => jsonBody(symbolsJson()),
      'GET /backtests': (_) => jsonBody({'count': runs.length, 'runs': runs}),
      'POST /backtests': (RequestOptions r) {
        if (onSubmit != null) return onSubmit!(r);
        final int id = nextId++;
        final Map<String, Object?> body = FakeEngineHttp.bodyOf(r)! as Map<String, Object?>;
        runs.insert(0, runSummaryJson(id: id, status: 'queued', mode: body['mode'] as String));
        return jsonBody({
          'id': id,
          'status': 'queued',
          'provisional': true,
          'labels_fa': ['موقت تا تایید چک داده'],
        }, 202);
      },
      ...?extra,
    });
    return _RoutedHttp(http, this);
  }
}

/// Adds the `/backtests/{id}...` routes (path parameters) to a [FakeEngineHttp].
class _RoutedHttp extends FakeEngineHttp {
  _RoutedHttp(FakeEngineHttp base, this.engine) : super(base.routes);

  final FakeBacktestEngine engine;

  @override
  Future<ResponseBody> fetch(
      RequestOptions options, Stream<List<int>>? requestStream, Future<void>? cancelFuture) async {
    final RegExpMatch? m = FakeBacktestEngine._id.firstMatch(options.uri.path);
    if (m == null || routes.containsKey('${options.method} ${options.uri.path}')) {
      return super.fetch(options, null, cancelFuture);
    }
    requests.add(options);
    final int id = int.parse(m.group(1)!);
    final String? sub = m.group(3);
    final Map<String, Object?>? detail = engine.details[id];
    ResponseBody notFound() => engineError(404, 'backtest_not_found', 'بک‌تستی با این شناسه پیدا نشد.');
    switch ((options.method, sub)) {
      case ('GET', null):
        return detail == null ? notFound() : jsonBody(detail);
      case ('GET', 'trades'):
        return detail == null ? notFound() : jsonBody(tradesPageJson(id, engine.trades[id] ?? const []));
      case ('GET', 'equity'):
        return detail == null
            ? notFound()
            : jsonBody(equityJson(id, windows: (detail['windows'] as List?)?.length ?? 1));
      case ('GET', 'skipped'):
        return detail == null ? notFound() : jsonBody(skippedJson(id));
      case ('POST', 'cancel'):
        return jsonBody({'id': id, 'status': 'running', 'cancel_requested': true});
      case ('DELETE', null):
        engine.runs.removeWhere((Map<String, Object?> r) => r['id'] == id);
        engine.details.remove(id);
        return jsonBody({'id': id, 'deleted': true});
    }
    return notFound();
  }
}

/// `GET /backtests/limits?symbol` (engine README worked example).
Map<String, Object?> limitsJson({
  String symbol = 'XAUUSD.x',
  String earliestStart = '2024-07-29T09:00:00Z',
  String dataEnd = '2025-03-03T00:00:00Z',
}) =>
    {
      'symbol': symbol,
      'earliest_start': earliestStart,
      'data_start': '2024-06-03T00:00:00Z',
      'data_end': dataEnd,
      'warmup_h4_bars': 140,
      'window_months_max': 60,
      'windows_count_max': 500,
      'seed_max': 9223372036854775807,
      'default_fallback_spread_points': {'points': 30, 'source': 'auto_median_observed'},
      'params_version': 1,
      'params_hash': kHash,
      'note_fa': 'بازه دستی نیم‌باز است.',
    };
