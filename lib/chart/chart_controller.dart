import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import 'chart_data.dart';
import '../models/market_data.dart';
import '../services/engine_api.dart';
import 'chart_data_source.dart';
import 'chart_format.dart';
import '../models/chart_models.dart';

enum ChartLoadState { idle, loading, ready, empty, error }

/// A one-shot instruction for the chart canvas (it owns the viewport, which
/// depends on its pixel width). [serial] changes on every new request.
@immutable
class ChartViewRequest {
  const ChartViewRequest._(this.serial, {this.focusIndex, this.reset = false});

  static const ChartViewRequest none = ChartViewRequest._(0);

  final int serial;

  /// Centre this bar.
  final int? focusIndex;

  /// Back to the newest bars at the default zoom.
  final bool reset;
}

/// Meta + full gap list of the selected series (the «اطلاعات داده» panel).
@immutable
class DataInfo {
  const DataInfo({required this.meta, required this.gaps, this.digits});

  final RatesMeta meta;
  final GapsResult gaps;
  final int? digits;
}

/// Result of «به‌روزرسانی از MT5».
@immutable
class UpdateOutcome {
  const UpdateOutcome({required this.ok, required this.messageFa, this.explanationFa, this.errorsFa = const []});

  final bool ok;

  /// The engine's own Persian message.
  final String messageFa;

  /// What the user should do about it (for known error codes).
  final String? explanationFa;
  final List<String> errorsFa;
}

/// Persian explanation of the `/rates/update` refusal codes
/// (routes/rates.py: `_update_precheck`, `post_rates_update`).
String? updateErrorExplanationFa(String? code) => switch (code) {
      'no_cache' => 'برای این نماد هنوز تاریخچه‌ای در کش نیست. به‌روزرسانی فقط کندل‌های تازه را اضافه می‌کند؛ '
          'اول باید تاریخچه کامل با ابزار fetch_history دریافت شود.',
      'not_incremental' => 'داده فعلی کش، داده آزمایشی است نه داده MT5، پس نمی‌شود فقط کندل‌های تازه را به آن '
          'اضافه کرد. برای جایگزینی آن، تاریخچه کامل را با fetch_history دریافت کنید.',
      'offset_model_changed' => 'مدل اختلاف ساعت سرور بروکر با مدلی که کش با آن ساخته شده فرق دارد. '
          'اضافه کردن کندل با مدل جدید زمان‌ها را به‌هم می‌ریزد؛ کش دست نخورد و باید تاریخچه کامل دوباره دریافت شود.',
      'mt5_unavailable' => 'ترمینال MT5 باز یا لاگین نیست، یا engine به آن وصل نشده است. ترمینال را باز کنید، '
          'چند لحظه صبر کنید و دوباره امتحان کنید. داده کش دست نخورد.',
      'update_failed' => 'دریافت از MT5 وسط کار خطا داد. جزئیات در ادامه آمده است؛ کندل‌های قبلی کش سالم هستند.',
      _ => null,
    };

/// The engine process is not reachable (not started, crashed, port closed).
bool isEngineUnreachable(EngineApiException e) => e.kind == EngineApiErrorKind.connection;

const String kEngineUnavailableFa =
    'موتور تحلیل در دسترس نیست. صبر کنید تا نشانگر موتور «فعال» شود و دوباره تلاش کنید.';
const String kEmptyRangeFa = 'در این بازه هیچ کندلی در کش نیست. بازه دیگری انتخاب کنید یا داده را به‌روز کنید.';
const String kUnexpectedErrorFa = 'خطای غیرمنتظره در دریافت داده چارت.';
const String kRangeOrderErrorFa = 'تاریخ شروع باید قبل از تاریخ پایان باشد.';

/// State of the chart page: inputs (symbol, timeframe, UTC date range),
/// loading/error/empty states, the loaded [ChartData], setup selection and
/// the data-info / update panel. All data comes from a [ChartDataSource];
/// this class never computes a trading value.
class ChartController extends ChangeNotifier {
  ChartController({required ChartDataSource source}) : _source = source;

  final ChartDataSource _source;

  /// Default window when no range is chosen: the last 30 days of data.
  static const Duration defaultWindow = Duration(days: 30);
  static const Duration _h1 = Duration(hours: 1);

  List<SymbolItem> _symbols = const <SymbolItem>[];
  String? _symbol;
  ChartTimeframe _timeframe = ChartTimeframe.h1;
  DateTime? _from;
  DateTime? _to;
  ChartLoadState _state = ChartLoadState.idle;
  String? _messageFa;
  bool _engineUnavailable = false;
  ChartData? _data;
  String? _selectedSetupId;
  int? _highlightIndex;
  ChartViewRequest _viewRequest = ChartViewRequest.none;
  DataInfo? _dataInfo;
  bool _dataInfoLoading = false;
  String? _dataInfoError;
  bool _updating = false;
  UpdateOutcome? _updateOutcome;

  // Inputs of the last load() that went out (snapshot at its start), for
  // the «به‌روزرسانی نمودار» highlight.
  bool _hasLoaded = false;
  String? _loadedSymbol;
  ChartTimeframe? _loadedTimeframe;
  DateTime? _loadedFrom;
  DateTime? _loadedTo;

  /// setSymbol/setTimeframe load by themselves; until that load starts the
  /// selection is not "dirty" (no highlight flicker while the meta arrives).
  bool _autoLoadPending = false;
  bool _wasDirty = false;

  int _loadSerial = 0;
  int _infoSerial = 0;
  int _metaSerial = 0;
  bool _disposed = false;

  List<SymbolItem> get symbols => _symbols;
  String? get symbol => _symbol;
  ChartTimeframe get timeframe => _timeframe;

  /// UTC range start (inclusive); null = engine default.
  DateTime? get from => _from;

  /// UTC range end (inclusive); null = engine default.
  DateTime? get to => _to;
  ChartLoadState get state => _state;

  /// Persian text for the error / empty state.
  String? get messageFa => _messageFa;
  bool get engineUnavailable => _engineUnavailable;
  ChartData? get data => _data;
  String? get selectedSetupId => _selectedSetupId;
  SetupMark? get selectedSetup => _data?.setupById(_selectedSetupId);
  int? get highlightIndex => _highlightIndex;
  ChartViewRequest get viewRequest => _viewRequest;
  DataInfo? get dataInfo => _dataInfo;
  bool get dataInfoLoading => _dataInfoLoading;
  String? get dataInfoError => _dataInfoError;
  bool get updating => _updating;
  UpdateOutcome? get updateOutcome => _updateOutcome;
  bool get isLoading => _state == ChartLoadState.loading;

  /// True when the selected symbol / timeframe / range differs from what the
  /// chart currently shows, i.e. «به‌روزرسانی نمودار» would change it.
  bool get rangeDirty =>
      _hasLoaded &&
      !_autoLoadPending &&
      (_symbol != _loadedSymbol || _timeframe != _loadedTimeframe || _from != _loadedFrom || _to != _loadedTo);

  /// Persian validation error of the selected range (start after end), or
  /// null. [setRange] accepts it (from and to are picked one after the
  /// other); [load] refuses it.
  String? get rangeErrorFa {
    final DateTime? from = _from;
    final DateTime? to = _to;
    return (from != null && to != null && from.isAfter(to)) ? kRangeOrderErrorFa : null;
  }

  int? get digits {
    for (final SymbolItem s in _symbols) {
      if (s.symbol == _symbol) return s.spec?.digits;
    }
    return null;
  }

  // ------------------------------------------------------------ inputs

  /// Symbols -> first symbol -> its default range -> load.
  Future<void> init() async {
    final int serial = ++_loadSerial;
    _setState(ChartLoadState.loading);
    try {
      final List<SymbolItem> list = await _source.symbols();
      if (_stale(serial, 'symbols')) return;
      _symbols = list;
      _log('symbols: ${list.map((SymbolItem s) => s.symbol).join(', ')}');
      if (list.isEmpty) {
        _setState(ChartLoadState.empty, message: 'هیچ نمادی در engine تعریف نشده است.');
        return;
      }
      _symbol ??= list.first.symbol;
    } catch (e) {
      if (_stale(serial, 'symbols')) return;
      _fail(e, 'symbols');
      return;
    }
    await _applyDefaultRange();
    await load();
    unawaited(refreshDataInfo());
  }

  Future<void> setSymbol(String symbol) async {
    if (symbol == _symbol) return;
    _log('symbol -> $symbol');
    _symbol = symbol;
    _resetSelection();
    _dataInfo = null;
    _updateOutcome = null;
    _autoLoadPending = true;
    notifyListeners();
    await _applyDefaultRange();
    await load();
    unawaited(refreshDataInfo());
  }

  Future<void> setTimeframe(ChartTimeframe tf) async {
    if (tf == _timeframe) return;
    _log('timeframe -> ${tf.code}');
    _timeframe = tf;
    _resetSelection();
    _dataInfo = null;
    _autoLoadPending = true;
    notifyListeners();
    await load();
    unawaited(refreshDataInfo());
  }

  /// Sets the UTC range without loading: the «به‌روزرسانی نمودار» button
  /// loads it ([rangeDirty] highlights it meanwhile). A start after the end
  /// is kept as picked and reported by [rangeErrorFa] instead of rejected.
  void setRange({DateTime? from, DateTime? to}) {
    _from = from?.toUtc() ?? _from;
    _to = to?.toUtc() ?? _to;
    _log('range -> ${_from == null ? '-' : formatUtc(_from!)} .. ${_to == null ? '-' : formatUtc(_to!)}');
    notifyListeners();
  }

  /// Picked calendar days (Gregorian, interpreted as UTC days) to a range:
  /// [fromDay] 00:00 .. [toDay] 23:59:59 UTC.
  void setRangeDays({DateTime? fromDay, DateTime? toDay}) => setRange(
        from: fromDay == null ? null : DateTime.utc(fromDay.year, fromDay.month, fromDay.day),
        to: toDay == null ? null : DateTime.utc(toDay.year, toDay.month, toDay.day, 23, 59, 59),
      );

  /// The last [defaultWindow] of cached data of the current series.
  Future<void> _applyDefaultRange() async {
    final String? symbol = _symbol;
    if (symbol == null) return;
    final int serial = ++_metaSerial;
    try {
      final RatesMeta meta = await _source.ratesMeta(symbol, _timeframe);
      if (serial != _metaSerial || _disposed) return;
      final DateTime? last = meta.lastBarUtc;
      if (last != null) {
        _to = last;
        _from = last.subtract(defaultWindow);
      } else {
        _from = null;
        _to = null;
      }
      _log('default range $symbol ${_timeframe.code}: ${_from ?? '-'} .. ${_to ?? '-'} (cached=${meta.cached})');
    } catch (e) {
      // The load that follows reports the error; the engine default range applies.
      _log('ratesMeta failed, using engine default range: $e');
      _from = null;
      _to = null;
    }
  }

  // ------------------------------------------------------------ loading

  /// Loads bars + channel (+ setups on H1) for the current inputs. A newer
  /// call makes older in-flight responses stale; they are dropped. An
  /// invalid range ([rangeErrorFa]) is refused without any request.
  Future<void> load() async {
    final String? symbol = _symbol;
    if (symbol == null) return;
    _autoLoadPending = false;
    if (rangeErrorFa != null) {
      _log('load refused: invalid range ${formatUtc(_from!)} > ${formatUtc(_to!)}');
      notifyListeners();
      return;
    }
    final int serial = ++_loadSerial;
    final ChartTimeframe tf = _timeframe;
    final DateTime? from = _from;
    final DateTime? to = _to;
    _hasLoaded = true;
    _loadedSymbol = symbol;
    _loadedTimeframe = tf;
    _loadedFrom = from;
    _loadedTo = to;
    final Stopwatch sw = Stopwatch()..start();
    _log('load #$serial $symbol ${tf.code} ${from ?? 'default'} .. ${to ?? 'default'}');
    _setState(ChartLoadState.loading);
    try {
      // In parallel; Future.wait also keeps a second failure from escaping
      // as an unhandled error.
      final List<Object> results = await Future.wait<Object>(<Future<Object>>[
        _source.rates(symbol, tf, from: from, to: to),
        _source.channel(symbol, tf, from: from, to: to),
        // /chart/setups filters by decision time (= confirmation bar close);
        // shifting the window by one H1 bar selects exactly the setups whose
        // confirmation bar is among the loaded bars (incl. a pending setup
        // on the newest bar).
        if (tf == ChartTimeframe.h1) _source.setups(symbol, from: from?.add(_h1), to: to?.add(_h1)),
      ]);
      final RatesResult rates = results[0] as RatesResult;
      final ChannelResult channel = results[1] as ChannelResult;
      final SetupsResult? setups = results.length > 2 ? results[2] as SetupsResult : null;
      if (_stale(serial, 'load')) return;
      final ChartData data = ChartData.build(
        rates: rates,
        timeframe: tf,
        channelResult: channel,
        setupsResult: setups,
        digits: digits,
      );
      _data = data;
      _resetSelection();
      _log('load #$serial done in ${sw.elapsedMilliseconds} ms: ${data.length} bars, '
          '${channel.validCount}/${channel.count} valid channel points, ${data.setups.length} setups, '
          '${data.gaps.length} gaps, source=${rates.source} stale=${rates.stale}');
      if (data.isEmpty) {
        _setState(ChartLoadState.empty, message: rates.message ?? channel.messageFa ?? kEmptyRangeFa);
      } else {
        _viewRequest = ChartViewRequest._(_viewRequest.serial + 1, reset: true);
        _setState(ChartLoadState.ready);
      }
    } catch (e) {
      if (_stale(serial, 'load')) return;
      _fail(e, 'load #$serial after ${sw.elapsedMilliseconds} ms');
    }
  }

  bool _stale(int serial, String what) {
    if (_disposed) return true;
    if (serial != _loadSerial) {
      _log('$what #$serial: stale response ignored (current #$_loadSerial)');
      return true;
    }
    return false;
  }

  void _fail(Object e, String what) {
    _log('$what failed: $e');
    if (e is EngineApiException) {
      _engineUnavailable = isEngineUnreachable(e);
      _setState(ChartLoadState.error, message: _engineUnavailable ? kEngineUnavailableFa : e.messageFa);
    } else {
      _engineUnavailable = false;
      _setState(ChartLoadState.error, message: kUnexpectedErrorFa);
    }
  }

  void _setState(ChartLoadState s, {String? message}) {
    _state = s;
    _messageFa = message;
    if (s != ChartLoadState.error) _engineUnavailable = false;
    if (!_disposed) notifyListeners();
  }

  // ------------------------------------------------------------ selection & navigation

  void _resetSelection() {
    _selectedSetupId = null;
    _highlightIndex = null;
  }

  /// Selects a setup (marker click or table row). With [jump] the chart
  /// centres its confirmation bar.
  void selectSetup(String? id, {bool jump = false}) {
    _selectedSetupId = id;
    final SetupMark? m = selectedSetup;
    _log('select setup ${id ?? '-'}${jump ? ' (jump)' : ''}');
    if (m != null) {
      _highlightIndex = m.barIndex;
      if (jump) _viewRequest = ChartViewRequest._(_viewRequest.serial + 1, focusIndex: m.barIndex);
    }
    notifyListeners();
  }

  /// Centres the bar nearest to [t]. Outside the loaded bars, the range moves
  /// to a [defaultWindow] around [t] first (e.g. a gap from the full list).
  Future<void> jumpToTime(DateTime t) async {
    final ChartData? data = _data;
    if (data == null || !data.covers(t)) {
      _log('jump to ${formatUtc(t)}: outside loaded bars, reloading around it');
      final Duration half = Duration(milliseconds: defaultWindow.inMilliseconds ~/ 2);
      _from = t.toUtc().subtract(half);
      _to = t.toUtc().add(half);
      _autoLoadPending = true;
      notifyListeners();
      await load();
    }
    final ChartData? now = _data;
    if (now == null || now.isEmpty) return;
    final int? i = now.nearestIndex(t);
    if (i == null) return;
    _log('jump to ${formatUtc(t)} -> bar $i (${formatUtc(now.candles[i].time)})');
    _highlightIndex = i;
    _viewRequest = ChartViewRequest._(_viewRequest.serial + 1, focusIndex: i);
    notifyListeners();
  }

  void resetView() {
    _log('reset view');
    _viewRequest = ChartViewRequest._(_viewRequest.serial + 1, reset: true);
    notifyListeners();
  }

  // ------------------------------------------------------------ data info & update

  Future<void> refreshDataInfo() async {
    final String? symbol = _symbol;
    if (symbol == null) return;
    final ChartTimeframe tf = _timeframe;
    final int serial = ++_infoSerial;
    _dataInfoLoading = true;
    _dataInfoError = null;
    notifyListeners();
    final Stopwatch sw = Stopwatch()..start();
    try {
      final List<Object> results = await Future.wait<Object>(<Future<Object>>[
        _source.ratesMeta(symbol, tf),
        _source.gaps(symbol, tf),
      ]);
      final RatesMeta meta = results[0] as RatesMeta;
      final GapsResult gaps = results[1] as GapsResult;
      if (serial != _infoSerial || _disposed) {
        _log('data info #$serial: stale response ignored');
        return;
      }
      _dataInfo = DataInfo(meta: meta, gaps: gaps, digits: digits);
      _log('data info $symbol ${tf.code} in ${sw.elapsedMilliseconds} ms: rows=${meta.rows} '
          'gaps=${gaps.count} ${gaps.gapCounts}');
    } catch (e) {
      if (serial != _infoSerial || _disposed) return;
      _log('data info failed: $e');
      _dataInfoError =
          e is EngineApiException ? (isEngineUnreachable(e) ? kEngineUnavailableFa : e.messageFa) : kUnexpectedErrorFa;
    } finally {
      if (serial == _infoSerial && !_disposed) {
        _dataInfoLoading = false;
        notifyListeners();
      }
    }
  }

  /// `POST /rates/update` (read-only MT5 fetch into the cache), then reloads
  /// the data info and the chart.
  Future<void> updateFromMt5() async {
    final String? symbol = _symbol;
    if (symbol == null || _updating) return;
    _updating = true;
    _updateOutcome = null;
    notifyListeners();
    _log('update from MT5: $symbol');
    try {
      final RatesUpdateResult r = await _source.update(symbol);
      if (_disposed) return;
      _updateOutcome = UpdateOutcome(ok: true, messageFa: r.messageFa);
      _log('update ok: ${r.updated.map((TimeframeUpdate u) => '${u.timeframe}+${u.barsAdded}').join(', ')}');
    } catch (e) {
      if (_disposed) return;
      _log('update failed: $e');
      if (e is EngineApiException) {
        _updateOutcome = UpdateOutcome(
          ok: false,
          messageFa: isEngineUnreachable(e) ? kEngineUnavailableFa : e.messageFa,
          explanationFa: updateErrorExplanationFa(e.code),
          errorsFa: e.errorsFa,
        );
      } else {
        _updateOutcome = const UpdateOutcome(ok: false, messageFa: kUnexpectedErrorFa);
      }
    } finally {
      if (!_disposed) {
        _updating = false;
        notifyListeners();
      }
    }
    if (_updateOutcome?.ok ?? false) {
      await refreshDataInfo();
      await load();
    }
  }

  /// Logs [rangeDirty] transitions (every state change goes through here).
  @override
  void notifyListeners() {
    if (kDevMode) {
      final bool dirty = rangeDirty;
      if (dirty != _wasDirty) {
        _wasDirty = dirty;
        _log(dirty
            ? 'range dirty: selection $_symbol ${_timeframe.code} ${_fmt(_from)} .. ${_fmt(_to)} != loaded '
                '$_loadedSymbol ${_loadedTimeframe?.code} ${_fmt(_loadedFrom)} .. ${_fmt(_loadedTo)}'
            : 'range clean: selection = loaded');
      }
    }
    super.notifyListeners();
  }

  static String _fmt(DateTime? t) => t == null ? '-' : formatUtc(t);

  void _log(String message) {
    if (!kDevMode) return;
    devLog('[Chart] $message');
    AppLogger.log('[Chart] $message');
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
