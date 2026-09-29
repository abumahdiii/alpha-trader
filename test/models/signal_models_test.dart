import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/chart_models.dart';
import 'package:alpha_trader/models/engine_health.dart';
import 'package:alpha_trader/models/signal_models.dart';
import 'package:alpha_trader/models/strategy.dart';

import '../helpers/signal_fakes.dart';

void main() {
  group('LiveSignal.fromJson', () {
    test('full engine item: every field read as sent (no rounding, no derivation)', () {
      final LiveSignal s = LiveSignal.fromJson(signalJson(wouldSkip: true, gapProvisional: true));
      expect(s.id, 1);
      expect(s.symbol, 'XAUUSD.x');
      expect(s.status, LiveSignalStatus.active);
      expect(s.direction, TradeSide.buy);
      expect(s.strategy, 'stddev_channel');
      expect(s.strategyVersion, 1);
      expect(s.strategySource, StrategySource.builtin);
      expect(s.paramsVersion, 3);
      expect(s.confirmationBarTime, DateTime.utc(2021, 10, 26, 13));
      expect(s.expiresTime, DateTime.utc(2021, 10, 26, 15));
      expect(s.expiryPending, isFalse);
      expect(s.indicativeEntry, 2064.68);
      expect(s.stopLoss, 2062.8835288730947);
      expect(s.takeProfitIndicative, 2068.27294);
      expect(s.entrySource, SignalEntrySource.tick);
      expect(s.rr, 2.0);
      expect(s.volume, 0.13);
      expect(s.riskAmount, 23.354);
      expect(s.sizing!.accepted, isTrue);
      expect(s.sizing!.margin, 268.41);
      expect(s.account!.balance, 2500.0);
      expect(s.account!.leverage, 100.0);
      expect(s.backtestWouldSkip, isTrue);
      expect(s.backtestSkipReasonFa, kPositionOpenFa);
      expect(s.gapClassProvisional, isTrue);
      expect(s.entryGap!.kind, 'session_break');
      expect(s.entryGap!.provisional, isTrue);
      expect(s.takeProfitNoteFa, kTpNoteFa);
      expect(s.indicators, {'atr_h1': 4.25});
      expect(s.noteFa, kSignalNoteFa);
      expect(s.createdTime, DateTime.utc(2021, 10, 26, 14, 0, 21));
    });

    test('tolerant: only an integer id is required; missing / null / mistyped fields do not crash', () {
      final LiveSignal s = LiveSignal.fromJson({
        'id': 7,
        'status': 'brand_new_status',
        'direction': 'sideways',
        'indicative_entry': 'not a number',
        'stop_loss': null,
        'expires_time': 'garbage',
        'sizing': 'nope',
        'account': null,
        'entry_gap': 3,
        'indicators': null,
        'mismatch': {'fields': ['direction', 'stop_loss'], 'scan': {}, 'evaluate': {}},
        'strategy_source': 'plugin',
        'strategy_sha256': 'aa' * 32,
        'future_field': {'x': 1},
      });
      expect(s.id, 7);
      expect(s.symbol, '—');
      expect(s.status, LiveSignalStatus.unknown);
      expect(s.direction, isNull);
      expect(s.indicativeEntry, isNull);
      expect(s.stopLoss, isNull);
      expect(s.expiresTime, isNull);
      expect(s.expiryUnknown, isTrue);
      expect(s.sizing, isNull);
      expect(s.entryGap, isNull);
      expect(s.indicators, isEmpty);
      expect(s.hasMismatch, isTrue);
      expect(s.mismatchFields, ['direction', 'stop_loss']);
      expect(s.isPlugin, isTrue);
      expect(s.entrySource, SignalEntrySource.unknown);
    });

    test('no id / not an object -> FormatException; tryParse and listFrom skip them', () {
      expect(() => LiveSignal.fromJson({'symbol': 'X'}), throwsFormatException);
      expect(() => LiveSignal.fromJson([1]), throwsFormatException);
      expect(LiveSignal.tryParse('x'), isNull);
      final List<LiveSignal> list = LiveSignal.listFrom([signalJson(id: 1), {'id': 'two'}, null, signalJson(id: 3)]);
      expect(list.map((LiveSignal s) => s.id), [1, 3]);
    });

    test('expiry pending (market closed after the bar)', () {
      final LiveSignal s = LiveSignal.fromJson(signalJson(expires: null, entrySource: 'last_close'));
      expect(s.expiryPending, isTrue);
      expect(s.expiryUnknown, isTrue);
      expect(s.entrySource, SignalEntrySource.lastClose);
    });

    test('status reason: rejection / mismatch / superseded', () {
      expect(LiveSignal.fromJson(signalJson(status: 'rejected', rejection: 'قیمت از حد ضرر گذشته است.')).statusReasonFa,
          'قیمت از حد ضرر گذشته است.');
      expect(LiveSignal.fromJson(signalJson(status: 'rejected', mismatch: {'fields': <String>[]})).statusReasonFa,
          'ناهمخوانی اسکن و ارزیابی');
      final Map<String, Object?> sup = signalJson(status: 'superseded')
        ..['superseded'] = {'reason_fa': 'اسکن تازه ستاپ را دیگر تولید نمی‌کند.', 'fields': ['stop_loss']};
      final LiveSignal s = LiveSignal.fromJson(sup);
      expect(s.statusReasonFa, 'اسکن تازه ستاپ را دیگر تولید نمی‌کند.');
      expect(s.supersededFields, ['stop_loss']);
      expect(LiveSignal.fromJson(signalJson()).statusReasonFa, isNull);
    });
  });

  test('entry source labels (Persian; the last-close rule per side)', () {
    expect(SignalEntrySource.tick.labelFa(TradeSide.buy), 'قیمت لحظه‌ای');
    expect(SignalEntrySource.lastClose.labelFa(TradeSide.buy), 'آخرین بسته + اسپرد، بازار بسته');
    expect(SignalEntrySource.lastClose.labelFa(TradeSide.sell), 'آخرین بسته، بازار بسته');
  });

  test('SignalsPage: counts, paging flags, bad items skipped', () {
    final SignalsPage p = SignalsPage.fromJson({
      'count': 2,
      'total': 120,
      'offset': 50,
      'limit': 50,
      'signals': [signalJson(id: 9), signalJson(id: 8), 'junk'],
    });
    expect(p.signals.map((LiveSignal s) => s.id), [9, 8]);
    expect(p.total, 120);
    expect(p.hasPrevious, isTrue);
    expect(p.hasNext, isTrue);
    expect(SignalsPage.fromJson({'signals': <Object?>[]}).hasNext, isFalse);
    expect(() => SignalsPage.fromJson('x'), throwsFormatException);
  });

  group('LiveSignalsStatus', () {
    test('full status', () {
      final LiveSignalsStatus st = LiveSignalsStatus.fromJson(signalStatusJson(skewWarning: true, skew: -14.2));
      expect(st.enabled, isTrue);
      expect(st.state, LiveSignalsState.running);
      expect(st.state.labelFa, 'در حال اجرا');
      expect(st.mt5State, Mt5State.connected);
      expect(st.liveStrategy, 'stddev_channel');
      expect(st.graceS, 20.0);
      expect(st.clockSkewS, -14.2);
      expect(st.clockSkewWarning, isTrue);
      expect(st.lastTickUtc['BRNUSD.x'], isNull);
      expect(st.lastTickUtc['XAUUSD.x'], DateTime.utc(2021, 10, 26, 14, 4, 58));
      expect(st.checks['XAUUSD.x']!.result, 'signal');
      expect(st.checks['XAUUSD.x']!.signalId, 1);
      expect(st.checks['BRNUSD.x']!.resultFa, 'ستاپی نبود');
      expect(st.checks['BRNUSD.x']!.reasonFa, 'در کندل بسته‌شده ستاپی نبود.');
      expect(st.symbols, ['BRNUSD.x', 'XAUUSD.x']);
      expect(st.nextCheckUtc, DateTime.utc(2021, 10, 26, 15, 0, 20));
    });

    test('tolerant: unknown state / missing blocks', () {
      final LiveSignalsStatus st = LiveSignalsStatus.fromJson({'state': 'hibernating', 'checks': 'x', 'mt5_state': 3});
      expect(st.enabled, isFalse);
      expect(st.state, LiveSignalsState.unknown);
      expect(st.mt5State, Mt5State.unknown);
      expect(st.checks, isEmpty);
      expect(st.symbols, isEmpty);
    });

    test('every state has a Persian label', () {
      for (final LiveSignalsState s in LiveSignalsState.values) {
        expect(s.labelFa, isNotEmpty);
      }
      expect(LiveSignalsState.parse('mt5_down'), LiveSignalsState.mt5Down);
      expect(checkResultFa('market_closed'), 'بازار بسته');
      expect(checkResultFa('new_result'), 'new_result');
    });
  });

  test('settings + POST result with status; grace validation bounds', () {
    final LiveSignalSettingsResult r =
        LiveSignalSettingsResult.fromJson({'enabled': true, 'live_strategy': 'stddev_channel', 'grace_s': 30, 'status': signalStatusJson()});
    expect(r.settings, const LiveSignalSettings(enabled: true, liveStrategy: 'stddev_channel', graceS: 30));
    expect(r.status!.state, LiveSignalsState.running);
    expect(LiveSignalSettingsResult.fromJson({'enabled': false}).status, isNull);
    expect(LiveSignalSettings.validateGraceFa(0), isNull);
    expect(LiveSignalSettings.validateGraceFa(600), isNull);
    expect(LiveSignalSettings.validateGraceFa(600.5), contains('بین 0 و 600'));
    expect(LiveSignalSettings.validateGraceFa(-1), isNotNull);
    expect(LiveSignalSettings.validateGraceFa(null), isNotNull);
  });

  group('WS messages', () {
    test('snapshot / signal / expired / superseded / status / keepalive / unknown', () {
      final SignalsWsMessage snap = SignalsWsMessage.fromJson(snapshotMsg([signalJson(id: 1), signalJson(id: 2)]))!;
      expect(snap.type, SignalsWsType.snapshot);
      expect(snap.seq, 5);
      expect(snap.active.map((LiveSignal s) => s.id), [1, 2]);
      expect(snap.status!.enabled, isTrue);
      expect(SignalsWsMessage.fromJson(signalMsg(signalJson(id: 4)))!.signal!.id, 4);
      expect(SignalsWsMessage.fromJson(signalMsg(signalJson(id: 4), type: 'expired'))!.type, SignalsWsType.expired);
      expect(SignalsWsMessage.fromJson(signalMsg(signalJson(id: 4), type: 'superseded'))!.type, SignalsWsType.superseded);
      expect(SignalsWsMessage.fromJson(statusMsg(signalStatusJson(enabled: false)))!.status!.enabled, isFalse);
      expect(SignalsWsMessage.fromJson(keepaliveMsg())!.type, SignalsWsType.keepalive);
      final SignalsWsMessage odd = SignalsWsMessage.fromJson({'type': 'tick', 'foo': 1})!;
      expect(odd.type, SignalsWsType.unknown);
      expect(odd.rawType, 'tick');
      expect(SignalsWsMessage.fromJson('text'), isNull);
      expect(SignalsWsMessage.fromJson({'type': 'signal', 'signal': {'no': 'id'}})!.signal, isNull);
    });
  });

  group('countdown formatting', () {
    final DateTime now = DateTime.utc(2021, 10, 26, 14, 17, 55);

    test('h:mm:ss until expires_time', () {
      final LiveSignal s = LiveSignal.fromJson(signalJson(expires: '2021-10-26T15:00:00Z'));
      expect(formatSignalCountdown(s, now), 'انقضا تا 0:42:05');
      expect(formatSignalCountdown(s, now.toLocal()), 'انقضا تا 0:42:05', reason: 'the zone of now does not matter');
      expect(formatSignalCountdown(s, DateTime.utc(2021, 10, 26, 14, 59, 59)), 'انقضا تا 0:00:01');
    });

    test('expiry pending / no expires_time -> «انقضا پس از بازگشایی بازار»', () {
      expect(formatSignalCountdown(LiveSignal.fromJson(signalJson(expires: null)), now), kExpiryPendingFa);
      expect(kExpiryPendingFa, 'انقضا پس از بازگشایی بازار');
      final LiveSignal flagged = LiveSignal.fromJson(signalJson(expiryPending: true));
      expect(formatSignalCountdown(flagged, now), kExpiryPendingFa, reason: 'expiry_pending wins over a time');
    });

    test('past expiry -> waiting for the engine', () {
      final LiveSignal s = LiveSignal.fromJson(signalJson(expires: '2021-10-26T14:00:00Z'));
      expect(formatSignalCountdown(s, now), kExpiryReachedFa);
    });

    test('formatCountdown', () {
      expect(formatCountdown(const Duration(hours: 26, minutes: 3, seconds: 9)), '26:03:09');
      expect(formatCountdown(const Duration(seconds: -5)), '0:00:00');
    });
  });
}
