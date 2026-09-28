import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/backtest_models.dart';

import '../helpers/backtest_fixtures.dart';

void main() {
  group('BacktestRunDetail', () {
    test('manual run: summary, config, plan, labels, fingerprint, spread fallback, single-window metrics', () {
      final BacktestRunDetail d = BacktestRunDetail.fromJson(manualDetailJson(id: 7));

      expect(d.id, 7);
      expect(d.status, BacktestStatus.done);
      expect(d.mode, BacktestMode.manual);
      expect(d.summary.symbol, 'XAUUSD.x');
      expect(d.summary.from, DateTime.utc(2023, 1, 2));
      expect(d.summary.to, DateTime.utc(2024, 1, 1));
      expect(d.summary.paramsHash, kHash);
      expect(d.summary.isMeanOfWindows, isFalse);
      expect(d.labelsFa, hasLength(4));
      expect(d.provisionalLabelFa, 'موقت تا تایید چک داده');
      expect(d.config!.account!.balance, 2500.0);
      expect(d.config!.account!.leverage, 100);
      expect(d.config!.costModel!.commissionPerLotPerSide, 0.0);
      expect(d.config!.costModel!.fallbackSpreadPoints, isNull);
      expect(d.config!.params, {'n': 100, 'k': 2.0});
      expect(d.plan!.warmupH4Bars, 140);
      expect(d.plan!.windows.single.end, DateTime.utc(2024, 1, 1));
      expect(d.fingerprint!.specDigits, 2);
      expect(d.fingerprint!.h1Source, 'mt5');
      expect(d.spreadFallback!.points, 34);
      expect(d.spreadFallback!.sourceFa, contains('میانه'));
      expect(d.spreadFallback!.fallbackBars, 1400);
      expect(d.metricsKind, 'single_window');
      expect(d.randomMetrics, isNull);
      final BacktestMetrics m = d.metrics!;
      expect(m.tradeCount, 3);
      expect(m.winRate, closeTo(0.6667, 1e-4));
      expect(m.profitFactor, 2.5025);
      expect(m.avgLoss, -100.0);
      expect(m.maxDrawdownPct, 4.6);
      expect(m.maxDrawdownPeakTime, DateTime.utc(2023, 3, 1, 10)); // "+00:00" form
      expect(m.sharpe, 1.234);
      expect(d.windows.single.metrics!.netProfit, 150.25);
      expect(d.windows.single.spreadFallbackBars, 1400);
    });

    test('random run: pooled / mean / worst metrics, distribution, windows, seed from the plan', () {
      final BacktestRunDetail d = BacktestRunDetail.fromJson(randomDetailJson(id: 8));

      expect(d.mode, BacktestMode.random);
      expect(d.metrics, isNull);
      expect(d.seed, 42);
      expect(d.plan!.algorithm, 'numpy.PCG64');
      final BacktestRandomMetrics r = d.randomMetrics!;
      expect(r.windowCount, 3);
      expect(r.pooled!.tradeCount, 6);
      expect(r.pooled!.winRate, 0.5);
      expect(r.pooled!.maxDrawdownAbs, isNull, reason: 'pooled carries trade-level fields only');
      expect(r.mean!.netProfit, 40.0);
      expect(r.mean!.winRateWindows, 2);
      expect(r.worst!.netProfitPct, -4.0);
      expect(r.noteFa, isNotEmpty);
      final BacktestDistribution dist = d.distribution!;
      expect(dist.medianNetProfit, 20.0);
      expect(dist.pctProfitable, closeTo(66.67, 0.01));
      expect(dist.worstWindow!.index, 2);
      expect(dist.bestWindow!.netProfit, 200.0);
      expect([for (final w in d.windows) w.netProfit], [200.0, 20.0, -100.0]);
      expect(d.summary.isMeanOfWindows, isTrue);
    });

    test('a queued run: nulls everywhere, empty windows, live progress snapshot', () {
      final Map<String, Object?> json = {
        ...runSummaryJson(id: 9, status: 'running', progress: 42.5),
        'labels_fa': ['موقت تا تایید چک داده', 'بدون سوآپ'],
        'provisional_label_fa': 'موقت تا تایید چک داده',
        'request': {},
        'config': null,
        'plan': null,
        'fingerprint': null,
        'result_meta': null,
        'spread_fallback': null,
        'metrics_kind': null,
        'metrics': null,
        'distribution': null,
        'windows': <Object?>[],
        'equity_storage_rule': 'x',
        'timings': null,
        'live': {
          'id': 9,
          'status': 'running',
          'phase': 'simulating',
          'percent': 42.5,
          'window_index': 0,
          'window': 1,
          'windows': 1,
          'error_code': null,
          'message_fa': null,
          'trade_count': null,
          'net_profit': null,
          'net_profit_pct': null,
          'cancel_requested': false,
          'version': 12,
        },
      };
      final BacktestRunDetail d = BacktestRunDetail.fromJson(json);
      expect(d.status.isActive, isTrue);
      expect(d.metrics, isNull);
      expect(d.windows, isEmpty);
      expect(d.live!.phase, 'simulating');
      expect(d.live!.isFinal, isFalse);
      final BacktestProgress p = BacktestProgress.fromDetail(d);
      expect(p.type, 'progress');
      expect(p.percent, 42.5);
      expect(p.phase, 'simulating');
    });

    test('an errored run becomes a final progress message with the Persian error', () {
      final BacktestRunDetail d = BacktestRunDetail.fromJson({
        ...runSummaryJson(
          id: 10,
          status: 'error',
          error: {'code': 'internal_error', 'message_fa': 'خطای داخلی موتور'},
        ),
        'windows': <Object?>[],
      });
      final BacktestProgress p = BacktestProgress.fromDetail(d);
      expect(p.isFinal, isTrue);
      expect(p.type, 'error');
      expect(p.code, 'internal_error');
      expect(p.messageFa, 'خطای داخلی موتور');
    });

    test('a missing identity field is a FormatException (contract mismatch)', () {
      final Map<String, Object?> json = runSummaryJson()..remove('symbol');
      expect(() => BacktestRunSummary.fromJson(json), throwsFormatException);
    });

    test('an unknown status is tolerated', () {
      final BacktestRunSummary s = BacktestRunSummary.fromJson(runSummaryJson(status: 'paused'));
      expect(s.status, BacktestStatus.unknown);
    });
  });

  group('trades / equity / skipped', () {
    test('trade page', () {
      final BacktestTradesPage page = BacktestTradesPage.fromJson(
        tradesPageJson(7, [tradeJson(0), tradeJson(1, netPnl: -25, direction: 'sell')], total: 900),
      );
      expect(page.total, 900);
      expect(page.isPartial, isTrue);
      final BacktestTrade t = page.trades[1];
      expect(t.direction, 'sell');
      expect(t.entry, 1851.12345);
      expect(t.netPnl, -25.0);
      expect(t.rMultiple, -1.0);
      expect(t.exitReasonFa, 'حد ضرر');
      expect(t.entryTime, DateTime.utc(2023, 2, 2, 10));
      expect(t.reasonFa, isNotEmpty);
    });

    test('equity points and the per-window filter', () {
      final BacktestEquity e = BacktestEquity.fromJson(equityJson(8, windows: 3));
      expect(e.points, hasLength(12));
      expect(e.ofWindow(1), hasLength(4));
      expect(e.ofWindow(1).first.windowIndex, 1);
      expect(e.downsampled, isTrue);
    });

    test('skipped candidates with the Persian reason', () {
      final BacktestSkippedList s = BacktestSkippedList.fromJson(skippedJson(7, count: 2));
      expect(s.skipped, hasLength(2));
      expect(s.skipped.first.reason, 'position_open');
      expect(s.skipped.first.reasonFa, contains('معامله باز'));
    });
  });

  group('progress messages (WS)', () {
    test('progress / done / error / not found', () {
      final BacktestProgress p = BacktestProgress.fromJson(progressMsg(3, 'scanning', 12.5));
      expect(p.isFinal, isFalse);
      expect(p.phase, 'scanning');
      expect(backtestPhaseFa(p.phase), 'اسکن ستاپ‌ها');

      final BacktestProgress done = BacktestProgress.fromJson(doneMsg(3));
      expect(done.isDone, isTrue);
      expect(done.tradeCount, 3);

      final BacktestProgress err = BacktestProgress.fromJson({
        'type': 'error',
        'id': 999,
        'status': null,
        'code': 'backtest_not_found',
        'message_fa': 'بک‌تستی با این شناسه پیدا نشد.',
      });
      expect(err.isFinal, isTrue);
      expect(err.status, BacktestStatus.unknown);
      expect(err.messageFa, contains('پیدا نشد'));
    });
  });

  group('BacktestRequest', () {
    test('manual sends [from, to) in UTC and no random fields', () {
      final BacktestRequest r = BacktestRequest.manual(
        symbol: 'XAUUSD.x',
        from: DateTime.utc(2023, 1, 2),
        to: DateTime.utc(2024, 1, 1),
        commissionPerLotPerSide: 3.5,
      );
      expect(r.toJson(), {
        'symbol': 'XAUUSD.x',
        'mode': 'manual',
        'from': '2023-01-02T00:00:00.000Z',
        'to': '2024-01-01T00:00:00.000Z',
        'commission_per_lot_per_side': 3.5,
      });
    });

    test('random sends count, months, seed and the fallback spread; no period', () {
      const BacktestRequest r = BacktestRequest.random(
        symbol: 'XAUUSD.x',
        windowsCount: 20,
        windowMonths: 3,
        seed: 42,
        commissionPerLotPerSide: 0,
        fallbackSpreadPoints: 0,
      );
      expect(r.toJson(), {
        'symbol': 'XAUUSD.x',
        'mode': 'random',
        'windows_count': 20,
        'window_months': 3,
        'seed': 42,
        'commission_per_lot_per_side': 0.0,
        'fallback_spread_points': 0,
      });
    });
  });
}
