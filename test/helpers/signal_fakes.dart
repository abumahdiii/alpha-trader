// Live signal fixtures (engine `signal_to_api` / `status()` shapes) and a
// recording alerter. Numbers are fixtures, not trading math.

import 'package:alpha_trader/services/signal_alerts.dart';

const String kTpNoteFa = 'حد سود نمایش‌داده‌شده تقریبی است و با قیمت ورود تقریبی حساب شده؛ حد سود واقعی در لحظه ورود با '
    'قیمت واقعی (open کندل ورود) و همان نسبت R:R تعیین می‌شود.';
const String kSignalNoteFa = 'این فقط یک پیشنهاد است؛ برنامه هیچ سفارشی ارسال نمی‌کند.';
const String kPositionOpenFa = 'در بک‌تست پوزیشن دیگری باز بود (حداکثر یک معامله باز)؛ این ستاپ معامله نشد.';

/// A full signal item like the engine's `signal_to_api` (worked example of
/// the engine README: gold buy at the ask).
Map<String, Object?> signalJson({
  int id = 1,
  String symbol = 'XAUUSD.x',
  String status = 'active',
  String direction = 'buy',
  String confirmation = '2021-10-26T13:00:00Z',
  String? expires = '2021-10-26T15:00:00Z',
  bool? expiryPending,
  String entrySource = 'tick',
  bool wouldSkip = false,
  bool gapProvisional = false,
  String strategy = 'stddev_channel',
  String strategySource = 'builtin',
  String? sha,
  String? rejection,
  Map<String, Object?>? mismatch,
  double entry = 2064.68,
  double sl = 2062.8835288730947,
  double tp = 2068.27294,
  double volume = 0.13,
}) =>
    {
      'id': id,
      'symbol': symbol,
      'status': status,
      'strategy': strategy,
      'strategy_version': 1,
      'strategy_source': strategySource,
      'strategy_sha256': sha,
      'params_version': 3,
      'params_hash': 'abcdef0123456789abcdef',
      'confirmation_bar_time': confirmation,
      'decision_time': '2021-10-26T14:00:00Z',
      'entry_bar_time': '2021-10-26T14:00:00Z',
      'expires_time': expires,
      'expiry_pending': expiryPending ?? (status == 'active' && expires == null),
      'direction': direction,
      'setup_type': 'bounce_lower',
      'setup_title_fa': 'برگشت از خط پایین',
      'pattern': 'bullish_pin_bar',
      'line': 'lower',
      'reference_price': 2064.10,
      'indicative_entry': entry,
      'entry_source': entrySource,
      'stop_loss': sl,
      'take_profit_indicative': tp,
      'take_profit_note_fa': kTpNoteFa,
      'rr': 2.0,
      'volume': volume,
      'risk_amount': 23.354,
      'sizing': {
        'accepted': true,
        'volume': volume,
        'raw_volume': 0.13916,
        'risk_amount': 25.0,
        'value_per_unit': 1.0,
        'loss_per_lot': 179.647,
        'actual_risk': 23.354,
        'margin': 268.41,
        'reason_fa': null,
        'warnings': <String>[],
      },
      'account': {'balance': 2500.0, 'risk_pct': 1.0, 'leverage': 100, 'rr': 2.0},
      'reason_fa': 'لمس خط پایین کانال صعودی و پین‌بار صعودی.',
      'rejection_reason_fa': rejection,
      'backtest_would_skip': wouldSkip,
      'backtest_skip_reason_fa': wouldSkip ? kPositionOpenFa : null,
      'gap_class_provisional': gapProvisional,
      'entry_gap': gapProvisional
          ? {
              'kind': 'session_break',
              'provisional': true,
              'entry_bar_time': null,
              'note_fa': 'وقفه جلسه معاملاتی؛ پس از بازگشایی قطعی می‌شود.'
            }
          : null,
      'indicators': {'atr_h1': 4.25},
      'mismatch': mismatch,
      'superseded': null,
      'note_fa': kSignalNoteFa,
      'created_time': '2021-10-26T14:00:21Z',
      'updated_time': '2021-10-26T14:00:21Z',
    };

/// `GET /signals/status` like the engine's `LiveSignals.status()`.
Map<String, Object?> signalStatusJson({
  bool enabled = true,
  String state = 'running',
  String mt5State = 'connected',
  bool skewWarning = false,
  double? skew = 0.4,
  String? lastError,
  String liveStrategy = 'stddev_channel',
}) =>
    {
      'enabled': enabled,
      'state': state,
      'mt5_state': mt5State,
      'live_strategy': liveStrategy,
      'grace_s': 20.0,
      'server_offset': 'us_dst(2)',
      'server_time_utc': '2021-10-26T14:05:00Z',
      'clock_skew_s': skew,
      'clock_skew_warning': skewWarning,
      'last_tick_utc': {'XAUUSD.x': '2021-10-26T14:04:58Z', 'BRNUSD.x': null},
      'last_check_utc': '2021-10-26T14:00:20Z',
      'next_check_utc': '2021-10-26T15:00:20Z',
      'checks': {
        'XAUUSD.x': {'boundary_utc': '2021-10-26T14:00:00Z', 'result': 'signal', 'reason_fa': null, 'signal_id': 1},
        'BRNUSD.x': {
          'boundary_utc': '2021-10-26T14:00:00Z',
          'result': 'no_setup',
          'reason_fa': 'در کندل بسته‌شده ستاپی نبود.'
        },
      },
      'last_error_fa': lastError,
      'last_error_utc': lastError == null ? null : '2021-10-26T13:00:05Z',
    };

Map<String, Object?> snapshotMsg(List<Map<String, Object?>> active, {Map<String, Object?>? status, int seq = 5}) =>
    {'type': 'snapshot', 'seq': seq, 'status': status ?? signalStatusJson(), 'active': active};

Map<String, Object?> signalMsg(Map<String, Object?> item, {String type = 'signal'}) => {'type': type, 'signal': item};

Map<String, Object?> statusMsg(Map<String, Object?> status) => {'type': 'status', 'status': status};

Map<String, Object?> keepaliveMsg() => {'type': 'keepalive', 'time_utc': '2021-10-26T14:10:00Z'};

Map<String, Object?> signalsPageJson(List<Map<String, Object?>> items, {int? total, int offset = 0, int limit = 50}) =>
    {'count': items.length, 'total': total ?? items.length, 'offset': offset, 'limit': limit, 'signals': items};

/// Records alerter calls; [fail] makes every call throw.
class FakeSignalAlerter implements SignalAlerter {
  FakeSignalAlerter({this.fail = false});

  final bool fail;
  int sounds = 0;
  final List<(String, String)> notifications = [];
  bool disposed = false;

  @override
  Future<void> playSound() async {
    if (fail) throw StateError('no audio device');
    sounds++;
  }

  @override
  Future<void> showNotification({required String title, required String body}) async {
    if (fail) throw StateError('Shell_NotifyIcon failed');
    notifications.add((title, body));
  }

  @override
  Future<void> dispose() async => disposed = true;
}
