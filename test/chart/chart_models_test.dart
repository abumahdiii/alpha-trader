// Parsing of engine payloads. Samples are copied from engine/README.md
// ("Chart data", "Routes") and the pydantic models in
// engine/alpha_engine/routes/{rates,chart,symbols}.py.

import 'dart:convert';

import 'package:alpha_trader/models/chart_models.dart';
import 'package:flutter_test/flutter_test.dart';

const String _params = '"params": {"n": 100, "k": 2.0, "sigma_ddof": 0, "projection_mode": "bar_count"}';
const String _prov = '"strategy": "stddev_channel", "strategy_version": 1, "params_version": 2, '
    '"params_hash": "8e223612f12a0000", $_params';

void main() {
  test('rates: bars with server_time, gaps and counts', () {
    final RatesResult r = RatesResult.fromJson(jsonDecode('''{
      "symbol": "XAUUSD.x", "timeframe": "H1", "from": "2025-01-01T00:00:00Z", "to": "2025-01-31T00:00:00Z",
      "source": "cache", "stale": false, "offset_model": "us_dst(2)", "count": 2,
      "bars": [
        {"time": "2025-01-05T23:00:00Z", "server_time": "2025.01.06 01:00", "open": 2640.5, "high": 2641,
         "low": 2639.12, "close": 2640.99, "tick_volume": 1234, "spread": 25, "real_volume": 0},
        {"time": "2025-01-06T00:00:00Z", "server_time": null, "open": 2641.0, "high": 2642.0,
         "low": 2640.0, "close": 2641.5, "tick_volume": 99, "spread": 20, "real_volume": 0}
      ],
      "gaps": [{"kind": "session_break", "after": "2025-02-24T21:00:00Z", "before": "2025-02-24T23:00:00Z",
                "start": "2025-02-24T22:00:00Z", "missing_bars": 1, "duration_hours": 1.0}],
      "gap_counts": {"weekend": 0, "holiday": 0, "session_break": 1, "missing": 0},
      "message": null}'''));
    expect(r.bars, hasLength(2));
    final Candle c = r.bars.first;
    expect(c.time, DateTime.utc(2025, 1, 5, 23));
    expect(c.time.isUtc, isTrue);
    expect(c.serverTime, '2025.01.06 01:00');
    expect(c.high, 2641.0); // JSON int -> double
    expect(c.tickVolume, 1234);
    expect(r.bars[1].serverTime, isNull);
    expect(r.gaps.single.kind, GapKind.sessionBreak);
    expect(r.gaps.single.start, DateTime.utc(2025, 2, 24, 22));
    expect(r.gapCounts['session_break'], 1);
  });

  test('channel: H1 worked example and a valid:false warm-up point', () {
    final ChannelResult ch = ChannelResult.fromJson(jsonDecode('''{
      "symbol": "XAUUSD.x", $_prov, "timeframe": "H1", "from": "2025-05-01T00:00:00Z",
      "to": "2025-05-05T00:00:00Z", "count": 2, "valid_count": 1,
      "points": [
        {"time": "2025-01-02T00:00:00Z", "valid": false, "mid": null, "upper": null, "lower": null,
         "slope": null, "sigma": null, "is_flat": null, "direction": null, "atr_h1": null, "atr_h4": null,
         "h4_open": null, "bars_ahead": null},
        {"time": "2025-05-02T20:00:00Z", "valid": true, "mid": 2076.8846275877586,
         "upper": 2100.192102180748, "lower": 2053.5771529947693, "slope": 0.19758661866186655,
         "sigma": 11.653738584711668, "is_flat": false, "direction": "up", "atr_h1": 4.63,
         "atr_h4": 10.8578, "h4_open": "2025-05-02T16:00:00Z", "bars_ahead": 0.75}
      ], "message_fa": null}'''));
    expect(ch.timeframe, ChartTimeframe.h1);
    expect(ch.provenance.paramsVersion, 2);
    expect(ch.provenance.params['n'], 100);
    final ChannelPoint warm = ch.points.first;
    expect(warm.valid, isFalse);
    expect(warm.drawable, isFalse);
    expect(warm.mid, isNull);
    final ChannelPoint p = ch.points[1];
    expect(p.mid, 2076.8846275877586);
    expect(p.direction, ChannelDirection.up);
    expect(p.h4Open, DateTime.utc(2025, 5, 2, 16));
    expect(p.barsAhead, 0.75);
  });

  test('channel: H4 point (atr_h1 / bars_ahead null) and a minimal warm-up row', () {
    final ChannelResult ch = ChannelResult.fromJson(jsonDecode('''{
      "symbol": "XAUUSD.x", $_prov, "timeframe": "H4", "from": null, "to": null, "count": 2,
      "valid_count": 1, "points": [
        {"time": "2025-01-02T00:00:00Z", "valid": false},
        {"time": "2025-05-02T17:00:00Z", "valid": true, "mid": 2076.7364376237624,
         "upper": 2100.0439147931856, "lower": 2053.428960454339, "slope": 0.19758661866186655,
         "sigma": 11.653738584711668, "is_flat": false, "direction": "up", "atr_h1": null,
         "atr_h4": 10.8578, "h4_open": "2025-05-02T17:00:00Z", "bars_ahead": null}]}'''));
    expect(ch.timeframe, ChartTimeframe.h4);
    expect(ch.from, isNull);
    expect(ch.messageFa, isNull);
    expect(ch.points.first.valid, isFalse);
    expect(ch.points[1].upper, 2100.0439147931856);
    expect(ch.points[1].atrH1, isNull);
  });

  test('setups: README accepted item, pending item, account and spec', () {
    final SetupsResult s = SetupsResult.fromJson(jsonDecode('''{
      "symbol": "XAUUSD.x", $_prov, "from": "2025-04-01T00:00:00Z", "to": "2025-05-01T00:00:00Z",
      "account": {"balance": 1000.0, "risk_pct": 1.0, "leverage": 100, "rr": 2.0},
      "symbol_spec": {"name": "XAUUSD.x", "digits": 2, "point": 0.01, "trade_contract_size": 100.0,
        "trade_tick_value": 1.0, "trade_tick_size": 0.01, "volume_min": 0.01, "volume_step": 0.01,
        "volume_max": 100.0, "currency_profit": "USD", "currency_base": "XAU", "description": "Gold"},
      "count": 2, "status_counts": {"accepted": 1, "rejected": 0, "pending_entry": 1},
      "setups": [
        {"id": "XAUUSD.x:20250428T1200Z:8e223612f12a", "symbol": "XAUUSD.x", "status": "accepted",
         "rejection_reason_fa": null, "setup_type": "bounce_lower", "setup_title_fa": "برگشت از خط پایین",
         "direction": "buy", "pattern": "bullish_pin_bar", "line": "lower", "line_value": 2063.5,
         "channel_direction": "up", "confirmation_bar_time": "2025-04-28T12:00:00Z",
         "decision_time": "2025-04-28T13:00:00Z", "entry_time": "2025-04-28T13:00:00Z", "entry": 2064.37,
         "stop_loss": 2062.8835288730947, "take_profit": 2067.34294225381, "rr": 2.0,
         "risk_distance": 1.486471126905144, "reference_price": 2064.2, "indicative_take_profit": 2066.83,
         "volume": 0.06, "actual_risk": 8.918826761430864, "margin": 123.8622, "risk_amount": 10.0,
         "volume_note_fa": null, "sizing_warnings_fa": [], "reason_fa": "لمس خط پایین",
         "indicators": {"atr_h1": 4.632355634525692, "line_value": 2063.5, "channel_direction": "up",
                        "touched": true, "n": 100, "note": null}},
        {"id": "XAUUSD.x:20250430T2300Z:8e223612f12a", "symbol": "XAUUSD.x", "status": "pending_entry",
         "rejection_reason_fa": null, "setup_type": "breakout_upper_pullback",
         "setup_title_fa": "شکست خط بالا و پولبک", "direction": "sell", "pattern": "bearish_engulfing",
         "line": "upper", "line_value": null, "channel_direction": null,
         "confirmation_bar_time": "2025-04-30T23:00:00Z", "decision_time": "2025-05-01T00:00:00Z",
         "entry_time": null, "entry": null, "stop_loss": 2100.5, "take_profit": null, "rr": 2.0,
         "risk_distance": null, "reference_price": 2095.0, "indicative_take_profit": 2084.0,
         "volume": null, "actual_risk": null, "margin": null, "risk_amount": null,
         "volume_note_fa": "کندل بعد هنوز در کش نیست", "sizing_warnings_fa": ["هشدار"],
         "reason_fa": "شکست", "indicators": {}}
      ],
      "note_fa": "یادداشت", "message_fa": null}'''));
    expect(s.account.leverage, 100);
    expect(s.symbolSpec!.digits, 2);
    expect(s.statusCounts['pending_entry'], 1);
    final SetupItem a = s.setups.first;
    expect(a.status, SetupStatus.accepted);
    expect(a.direction, TradeSide.buy);
    expect(a.line, ChannelLine.lower);
    expect(a.entry, 2064.37);
    expect(a.takeProfit, 2067.34294225381);
    expect(a.volume, 0.06);
    expect(a.patternTitleFa, 'پین‌بار صعودی (چکش)');
    expect(a.indicators['touched'], isTrue);
    expect(a.decisionTime.difference(a.confirmationBarTime), const Duration(hours: 1));
    final SetupItem p = s.setups[1];
    expect(p.status, SetupStatus.pendingEntry);
    expect(p.entry, isNull);
    expect(p.entryTime, isNull);
    expect(p.takeProfit, isNull);
    expect(p.volume, isNull);
    expect(p.indicativeTakeProfit, 2084.0);
    expect(p.sizingWarningsFa, <String>['هشدار']);
  });

  // engine/README.md "/chart/setups" phase-5 example (worked example item + top-level blocks).
  const String accepted = '''{"id": "XAUUSD.x:20250428T1200Z:8e223612f12a", "symbol": "XAUUSD.x",
    "status": "accepted", "rejection_reason_fa": null, "setup_type": "bounce_lower",
    "setup_title_fa": "برگشت از خط پایین", "direction": "buy", "pattern": "bullish_pin_bar", "line": "lower",
    "line_value": 2063.5, "channel_direction": "up", "confirmation_bar_time": "2025-04-28T12:00:00Z",
    "decision_time": "2025-04-28T13:00:00Z", "entry_time": "2025-04-28T13:00:00Z", "entry": 2064.68,
    "entry_bid_open": 2064.37, "spread_at_entry_points": 31, "entry_spread_source": "historical",
    "stop_loss": 2062.8835288730947, "take_profit": 2068.27294225381, "rr": 2.0,
    "risk_distance": 1.7964711269050895, "reference_price": 2064.2, "indicative_take_profit": 2066.83,
    "volume": 0.05, "actual_risk": 8.982355634525447, "margin": 103.234, "risk_amount": 10.0,
    "volume_note_fa": null, "sizing_warnings_fa": [], "reason_fa": "لمس خط پایین", "indicators": {"atr_h1": 4.63},
    "outcome": {"result": "sl", "exit_reason": "sl", "exit_reason_fa": "حد ضرر",
      "exit_bar_time": "2025-04-28T13:00:00Z", "exit_time": "2025-04-28T14:00:00Z",
      "exit_price": 2062.8835288730947, "pnl_price": -1.7964711269050895, "gross_pnl": -8.982355634525447,
      "commission": 0.0, "net_pnl": -8.982355634525447, "r_multiple": -1.0, "bars_held": 1,
      "held_over_weekend": false, "flags": []},
    "backtest": {"traded": false, "reason": "position_open",
      "reason_fa": "در بک‌تست پوزیشن دیگری باز بود (حداکثر یک معامله باز)؛ این ستاپ معامله نشد.",
      "trade_index": null, "net_pnl": null}}''';

  const String endOfData = '''{"id": "XAUUSD.x:20250504T2200Z:8e223612f12a", "symbol": "XAUUSD.x",
    "status": "accepted", "rejection_reason_fa": null, "setup_type": "bounce_upper",
    "setup_title_fa": "برگشت از خط بالا", "direction": "sell", "pattern": "bearish_engulfing", "line": "upper",
    "line_value": 2100.0, "channel_direction": "flat", "confirmation_bar_time": "2025-05-04T22:00:00Z",
    "decision_time": "2025-05-04T23:00:00Z", "entry_time": "2025-05-04T23:00:00Z", "entry": 2099.1,
    "entry_bid_open": 2099.1, "spread_at_entry_points": 30, "entry_spread_source": "fallback",
    "stop_loss": 2102.0, "take_profit": 2093.3, "rr": 2.0, "risk_distance": 2.9, "reference_price": 2099.0,
    "indicative_take_profit": 2093.0, "volume": 0.03, "actual_risk": 8.7, "margin": 62.97, "risk_amount": 10.0,
    "volume_note_fa": null, "sizing_warnings_fa": [], "reason_fa": "برگشت از خط بالا", "indicators": {},
    "outcome": {"result": "end_of_data", "exit_reason": "end_of_period",
      "exit_reason_fa": "پایان داده؛ ستاپ تا آخرین کندل کش باز ماند و با قیمت بسته شدن آن کندل بسته شد (نتیجه موقت)",
      "exit_bar_time": "2025-05-05T00:00:00Z", "exit_time": "2025-05-05T01:00:00Z", "exit_price": 2098.2,
      "pnl_price": 0.9, "gross_pnl": 2.7, "commission": 0.0, "net_pnl": 2.7, "r_multiple": 0.3103,
      "bars_held": 2, "held_over_weekend": false, "flags": ["exit_spread_fallback"]},
    "backtest": {"traded": true, "reason": null, "reason_fa": null, "trade_index": 5, "net_pnl": 2.71}}''';

  String setupsJson(
          {required String items, required String evaluation, required String summary, required String window}) =>
      '''{"symbol": "XAUUSD.x", $_prov, "from": "2025-04-22T10:00:00Z", "to": "2025-05-05T01:00:00Z",
      "account": {"balance": 1000.0, "risk_pct": 1.0, "leverage": 100, "rr": 2.0}, "symbol_spec": null,
      "count": 2, "status_counts": {"accepted": 2, "rejected": 0, "pending_entry": 0}, "setups": [$items],
      "note_fa": "یادداشت", "message_fa": null, "evaluation": $evaluation, "summary": $summary,
      "backtest_window": $window}''';

  const String evaluation = '''{"available": true, "message_fa": null, "basis": "independent_setups",
    "label_fa": "ارزیابی مستقل هر ستاپ، بدون قید یک معامله باز؛ با نتیجه بک‌تست فرق دارد",
    "end_of_data_label_fa": "پایان داده؛ ستاپ تا آخرین کندل کش باز ماند و با قیمت بسته شدن آن کندل بسته شد (نتیجه موقت)",
    "cost_model": {"spread": "historical", "fallback_spread_points": null, "commission_per_lot_per_side": 0.0,
      "swap": "none"},
    "spread_fallback": {"points": 30, "source": "auto_median_observed", "observed_from": "2025-04-01T00:00:00Z",
      "observed_to": "2025-05-05T00:00:00+00:00", "observed_bars": 580, "observed_median": 29.0,
      "fallback_bars": 12, "zero_bars": 0, "total_bars": 90},
    "labels_fa": ["موقت تا تایید چک داده", "اسپرد تاریخی بروکر (خرید با ask، فروش با bid)", "کمیسیون صفر",
      "بدون سوآپ"]}''';

  const String summary = '''{"total": 22, "accepted": 21, "rejected": 1, "pending_entry": 0, "closed": 21,
    "wins": 8, "losses": 13, "breakeven": 0, "open_end_of_data": 0, "win_rate": 0.38095238095238093,
    "net_pnl": 23.700120156467165, "gross_profit": 125.66829809407227, "gross_loss": 101.9681779376051,
    "profit_factor": 1.2324266318749895, "profit_factor_infinite": false, "total_r": 3.0,
    "avg_r": 0.14285714285714285, "r_count": 21, "open_net_pnl": 0.0}''';

  const String window = '''{"from": "2025-04-22T09:00:00Z", "to": "2025-05-05T00:00:00Z", "clipped": true,
    "note_fa": "شروع به اولین زمان مجاز محدود شد", "available": true, "code": null, "message_fa": null,
    "trades": 5, "net_profit": -12.69148116917313}''';

  test('setups (phase 5): README item with outcome + flag, end of data, evaluation, summary, window', () {
    final SetupsResult s = SetupsResult.fromJson(jsonDecode(
        setupsJson(items: '$accepted, $endOfData', evaluation: evaluation, summary: summary, window: window)));
    final SetupItem a = s.setups.first;
    expect(a.entry, 2064.68, reason: 'entry = simulator fill (buy = ask open)');
    expect(a.entryBidOpen, 2064.37);
    expect(a.spreadAtEntryPoints, 31);
    expect(a.entrySpreadSource, EntrySpreadSource.historical);
    final SetupOutcome o = a.outcome!;
    expect(o.result, SetupResult.sl);
    expect(o.exitReasonFa, 'حد ضرر');
    expect(o.exitBarTime, DateTime.utc(2025, 4, 28, 13));
    expect(o.exitTime, DateTime.utc(2025, 4, 28, 14));
    expect(o.netPnl, -8.982355634525447);
    expect(o.rMultiple, -1.0);
    expect(o.isGap, isFalse);
    expect(a.backtest!.traded, isFalse);
    expect(a.backtest!.reason, 'position_open');
    expect(a.backtest!.tradeIndex, isNull);

    final SetupItem e = s.setups[1];
    expect(e.outcome!.result, SetupResult.endOfData);
    expect(e.outcome!.flags, <String>['exit_spread_fallback']);
    expect(e.entrySpreadSource, EntrySpreadSource.fallback);
    expect(e.backtest!.traded, isTrue);
    expect(e.backtest!.tradeIndex, 5);
    expect(e.backtest!.netPnl, 2.71);
    expect(e.typeTitleFa, 'برگشت از خط بالا', reason: 'the title already names the line');

    final SetupsEvaluation ev = s.evaluation!;
    expect(s.evaluationAvailable, isTrue);
    expect(ev.labelFa, startsWith('ارزیابی مستقل هر ستاپ'));
    expect(ev.endOfDataLabelFa, startsWith('پایان داده'));
    expect(ev.costModel!.fallbackSpreadPoints, isNull);
    expect(ev.spreadFallback!.points, 30);
    expect(ev.spreadFallback!.observedTo, DateTime.utc(2025, 5, 5), reason: '+00:00 offset parsed as UTC');
    expect(ev.labelsFa, hasLength(4));

    final SetupsSummary sum = s.summary!;
    expect((sum.total, sum.accepted, sum.rejected, sum.pendingEntry), (22, 21, 1, 0));
    expect((sum.closed, sum.wins, sum.losses, sum.openEndOfData), (21, 8, 13, 0));
    expect(sum.winRate, 0.38095238095238093);
    expect(sum.netPnl, 23.700120156467165);
    expect(sum.profitFactor, 1.2324266318749895);
    expect(sum.profitFactorInfinite, isFalse);
    expect(sum.totalR, 3.0);
    expect(sum.rCount, 21);

    final BacktestWindow w = s.backtestWindow!;
    expect(w.available, isTrue);
    expect(w.from, DateTime.utc(2025, 4, 22, 9));
    expect(w.to, DateTime.utc(2025, 5, 5));
    expect(w.clipped, isTrue);
    expect(w.trades, 5);
    expect(w.netProfit, -12.69148116917313);
  });

  test('setups (phase 5): evaluation unavailable (no spec) -> counts only, no window, null outcomes', () {
    const String item = '''{"id": "XAUUSD.x:20250428T1200Z:8e223612f12a", "symbol": "XAUUSD.x",
      "status": "accepted", "rejection_reason_fa": null, "setup_type": "bounce_lower",
      "setup_title_fa": "برگشت از خط پایین", "direction": "buy", "pattern": "bullish_pin_bar", "line": "lower",
      "line_value": null, "channel_direction": null, "confirmation_bar_time": "2025-04-28T12:00:00Z",
      "decision_time": "2025-04-28T13:00:00Z", "entry_time": "2025-04-28T13:00:00Z", "entry": 2064.37,
      "entry_bid_open": 2064.37, "spread_at_entry_points": null, "entry_spread_source": null,
      "stop_loss": 2062.88, "take_profit": 2067.34, "rr": 2.0, "risk_distance": 1.49, "reference_price": 2064.2,
      "indicative_take_profit": 2066.83, "volume": null, "actual_risk": null, "margin": null, "risk_amount": null,
      "volume_note_fa": "مشخصات نماد در کش نیست", "sizing_warnings_fa": [], "reason_fa": "لمس",
      "indicators": {}, "outcome": null, "backtest": null}''';
    final SetupsResult s = SetupsResult.fromJson(jsonDecode(setupsJson(
      items: item,
      evaluation: '''{"available": false, "message_fa": "مشخصات نماد در کش نیست؛ نتیجه ستاپ‌ها محاسبه نمی‌شود",
        "basis": "independent_setups", "label_fa": "ارزیابی مستقل", "end_of_data_label_fa": "پایان داده",
        "cost_model": null, "spread_fallback": null, "labels_fa": []}''',
      summary: '''{"total": 1, "accepted": 1, "rejected": 0, "pending_entry": 0, "closed": 0, "wins": 0,
        "losses": 0, "breakeven": 0, "open_end_of_data": 0, "win_rate": null, "net_pnl": 0.0, "gross_profit": 0.0,
        "gross_loss": 0.0, "profit_factor": null, "profit_factor_infinite": false, "total_r": 0.0, "avg_r": null,
        "r_count": 0, "open_net_pnl": 0.0}''',
      window: '''{"from": null, "to": null, "clipped": false, "note_fa": null, "available": false,
        "code": "spec_missing", "message_fa": "مشخصات نماد در کش نیست", "trades": null, "net_profit": null}''',
    )));
    expect(s.evaluationAvailable, isFalse);
    expect(s.evaluation!.messageFa, startsWith('مشخصات نماد'));
    expect(s.evaluation!.costModel, isNull);
    expect(s.summary!.winRate, isNull);
    expect(s.summary!.profitFactor, isNull);
    expect(s.backtestWindow!.available, isFalse);
    expect(s.backtestWindow!.code, 'spec_missing');
    expect(s.backtestWindow!.from, isNull);
    final SetupItem a = s.setups.single;
    expect(a.outcome, isNull);
    expect(a.backtest, isNull);
    expect(a.entrySpreadSource, isNull);
    expect(a.spreadAtEntryPoints, isNull);
  });

  test('setups: an older engine without the phase-5 blocks still parses (all null)', () {
    final Map<String, Object?> json =
        jsonDecode(setupsJson(items: accepted, evaluation: 'null', summary: 'null', window: 'null'))
            as Map<String, Object?>;
    json.remove('evaluation');
    json.remove('summary');
    json.remove('backtest_window');
    final SetupsResult s = SetupsResult.fromJson(json);
    expect(s.evaluation, isNull);
    expect(s.summary, isNull);
    expect(s.backtestWindow, isNull);
    expect(s.evaluationAvailable, isFalse);
    expect(s.setups.single.outcome, isNotNull);
    expect(() => SetupResult.parse('open'), throwsFormatException);
    expect(EntrySpreadSource.parse('future_code'), isNull, reason: 'unknown spread source is dropped, not fatal');
  });

  test('gaps and update', () {
    final GapsResult g = GapsResult.fromJson(jsonDecode('''{"symbol": "XAUUSD.x", "timeframe": "H1",
      "cached": false, "rows": 0, "first_bar_utc": null, "last_bar_utc": null, "offset_model": null,
      "count": 0, "gaps": [], "gap_counts": {"weekend": 0, "holiday": 0, "session_break": 0, "missing": 0},
      "missing_bars_total": 0, "session_break_slots": ["22:00"]}'''));
    expect(g.cached, isFalse);
    expect(g.sessionBreakSlots, <String>['22:00']);

    final RatesUpdateResult u = RatesUpdateResult.fromJson(jsonDecode('''{"symbol": "XAUUSD.x",
      "updated": [{"timeframe": "H1", "bars_added": 3, "rows": 31012, "first_bar_utc": "2021-09-27T00:00:00Z",
      "last_bar_utc": "2026-09-27T09:00:00Z", "fetched": 5}], "message_fa": "کش به‌روز شد"}'''));
    expect(u.updated.single.barsAdded, 3);
  });

  test('contract drift is a FormatException naming the key', () {
    expect(
      () => Candle.fromJson(<String, Object?>{'time': '2025-01-01T00:00:00Z'}),
      throwsA(isA<FormatException>().having((FormatException e) => e.message, 'message', contains('bar.open'))),
    );
    expect(() => SetupStatus.parse('filled'), throwsFormatException);
    expect(() => ChannelDirection.parse('sideways'), throwsFormatException);
  });
}
