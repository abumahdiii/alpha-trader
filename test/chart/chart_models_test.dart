// Parsing of engine payloads. Samples are copied from engine/README.md
// ("Chart data", "Routes") and the pydantic models in
// engine/alpha_engine/routes/{rates,chart,symbols}.py.

import 'dart:convert';

import 'package:alpha_trader/chart/chart_models.dart';
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

  test('rates meta, gaps, update and symbols', () {
    final RatesMeta m = RatesMeta.fromJson(jsonDecode('''{"symbol": "XAUUSD.x", "timeframe": "H1",
      "cached": true, "rows": 31012, "first_bar_utc": "2021-09-27T00:00:00Z",
      "last_bar_utc": "2026-09-27T09:00:00Z", "first_available_utc": null, "requested_start_utc": null,
      "history_short": false, "offset_model": "fixed(0)", "source": "mt5", "fetched_at_utc": null}'''));
    expect(m.rows, 31012);
    expect(m.lastBarUtc, DateTime.utc(2026, 9, 27, 9));
    expect(m.firstAvailableUtc, isNull);

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

    final List<ChartSymbol> syms = ChartSymbol.listFromSymbolsResponse(jsonDecode('''{"mt5_state": "connected",
      "symbols": [{"symbol": "XAUUSD.x", "source": "cache", "spec": null, "fetched_at_utc": null,
      "error": null}]}'''));
    expect(syms.single.digits, isNull);
  });

  test('contract drift is a FormatException naming the key', () {
    expect(
      () => Candle.fromJson(<String, Object?>{'time': '2025-01-01T00:00:00Z'}),
      throwsA(isA<FormatException>().having((FormatException e) => e.message, 'message', contains('"open"'))),
    );
    expect(() => SetupStatus.parse('filled'), throwsFormatException);
    expect(() => ChannelDirection.parse('sideways'), throwsFormatException);
  });
}
