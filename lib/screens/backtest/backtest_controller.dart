import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';

import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../core/number_format.dart';
import '../../models/backtest_models.dart';
import '../../models/market_data.dart';
import '../../providers/shell_navigation.dart';
import '../../services/backtest_progress.dart';
import '../../services/engine_api.dart';

/// How the fallback spread (bars without an earlier broker spread) is set.
enum SpreadChoice {
  /// Omitted: the engine uses the median observed spread.
  auto,

  /// 0: explicit zero cost («بدون هزینه اسپرد»).
  zero,

  /// A user value in points.
  custom,
}

/// Form field keys, also used to attach the engine's 422 messages.
abstract final class BacktestField {
  static const String symbol = 'symbol';
  static const String period = 'period';
  static const String windowsCount = 'windows_count';
  static const String windowMonths = 'window_months';
  static const String seed = 'seed';
  static const String commission = 'commission_per_lot_per_side';
  static const String fallbackSpread = 'fallback_spread_points';

  /// `«label»` markers the engine writes into `errors_fa` (FIELD_LABELS_FA in routes/backtests.py).
  static const Map<String, List<String>> engineMarkers = {
    symbol: ['«نماد»'],
    period: ['«ابتدای بازه»', '«انتهای بازه»'],
    windowsCount: ['«تعداد پنجره‌ها»'],
    windowMonths: ['«طول هر پنجره (ماه)»'],
    seed: ['«seed»'],
    commission: ['«کمیسیون هر لات در هر طرف»'],
    fallbackSpread: ['«اسپرد جایگزین (پوینت)»'],
  };

  /// Engine error codes that are about the period (message under the period fields).
  static const Set<String> periodCodes = {
    'missing_period',
    'invalid_range',
    'window_too_early',
    'window_beyond_data',
    'window_empty',
    'data_too_short',
  };

  /// Engine error codes that are about the symbol / its data.
  static const Set<String> symbolCodes = {'invalid_symbol', 'symbol_not_configured', 'no_data', 'spec_missing'};
}

/// Builds the progress watcher of a run (injected in tests).
typedef BacktestWatcherFactory = BacktestProgressWatcher Function(EngineApi api, int runId);

/// State of the «بک‌تست» page: the form, submit, live progress, cancel, the
/// opened run's result and the list of previous runs.
///
/// Presenter only: every number shown comes from the engine; this class
/// builds requests, follows the run and loads what the engine stored.
class BacktestController extends ChangeNotifier {
  BacktestController({
    required this.api,
    BacktestWatcherFactory? watcherFactory,
    math.Random? random,
    this.runsLimit = 100,
  })  : _watcherFactory = watcherFactory ?? ((EngineApi api, int id) => BacktestProgressWatcher(api: api, runId: id)),
        _random = random ?? math.Random() {
    _seedText = _newSeed().toString();
  }

  final EngineApi api;
  final int runsLimit;
  final BacktestWatcherFactory _watcherFactory;
  final math.Random _random;
  bool _disposed = false;

  static const int seedMax = 0x7FFFFFFFFFFFFFFF; // engine SEED_MAX = 2**63 - 1
  static const int windowsCountMin = 1, windowsCountMax = 500;
  static const int windowMonthsMin = 1, windowMonthsMax = 60;
  static const double commissionMax = 1000;
  static const int fallbackSpreadMax = 100000;

  // ------------------------------------------------------------ symbols

  List<SymbolItem> _symbols = const [];
  bool _symbolsLoading = false;
  EngineApiException? _symbolsError;

  List<SymbolItem> get symbols => _symbols;
  bool get symbolsLoading => _symbolsLoading;
  EngineApiException? get symbolsError => _symbolsError;

  /// Price digits of [symbol] from `/symbols` (null = unknown).
  int? digitsOf(String? symbol) {
    for (final SymbolItem s in _symbols) {
      if (s.symbol == symbol) return s.spec?.digits;
    }
    return null;
  }

  // --------------------------------------------------------------- form

  String? _symbol;
  BacktestMode _mode = BacktestMode.manual;
  DateTime? _fromDay;
  DateTime? _toDay;

  /// Exact manual period `[from, to)` from a prefill (the chart's engine
  /// `backtest_window`), sent verbatim until the user edits the period,
  /// mode or symbol (then the day fields apply again).
  DateTime? _exactFrom;
  DateTime? _exactTo;
  int _windowsCount = 20;
  int _windowMonths = 3;
  late String _seedText;
  double _commission = 0;
  SpreadChoice _spreadChoice = SpreadChoice.auto;
  int _customSpreadPoints = 30;
  Map<String, List<String>> _fieldErrors = const {};
  EngineApiException? _submitError;
  List<String> _generalErrors = const [];
  bool _submitting = false;

  /// Bumped when the form values change from outside the fields (prefill):
  /// text fields rebuild with the new values.
  int formGeneration = 0;

  String? get symbol => _symbol;
  BacktestMode get mode => _mode;

  /// First day of the manual period (UTC midnight).
  DateTime? get fromDay => _fromDay;

  /// Last day of the manual period, INCLUDED (the request's `to` is the next midnight).
  DateTime? get toDay => _toDay;

  /// Exact period start from a prefill (UTC), or null.
  DateTime? get exactFrom => _exactFrom;

  /// Exact period end from a prefill (UTC, exclusive), or null.
  DateTime? get exactTo => _exactTo;

  /// The manual run will use [exactFrom] .. [exactTo] instead of the day fields.
  bool get hasExactPeriod => _exactFrom != null && _exactTo != null;

  /// «حذف» on the exact-period note: back to the day fields.
  void clearExactPeriod() {
    if (!_dropExactPeriod('removed by the user')) return;
    _notify();
  }

  /// Returns true when an exact period was active.
  bool _dropExactPeriod(String why) {
    if (!hasExactPeriod) return false;
    _log('form: exact period ${_exactFrom!.toIso8601String()} .. ${_exactTo!.toIso8601String()} cleared ($why); '
        'day fields apply');
    _exactFrom = null;
    _exactTo = null;
    return true;
  }

  int get windowsCount => _windowsCount;
  int get windowMonths => _windowMonths;
  String get seedText => _seedText;
  double get commission => _commission;
  SpreadChoice get spreadChoice => _spreadChoice;
  int get customSpreadPoints => _customSpreadPoints;
  Map<String, List<String>> get fieldErrors => _fieldErrors;
  List<String> errorsOf(String field) => _fieldErrors[field] ?? const [];

  /// A submit failure that is not about one field.
  EngineApiException? get submitError => _submitError;
  List<String> get generalErrors => _generalErrors;
  bool get isSubmitting => _submitting;

  /// A run can be submitted when the symbols are loaded and no run is being followed.
  bool get canSubmit => !_submitting && activeRunId == null && _symbols.isNotEmpty;

  void setSymbol(String? value) {
    if (value == _symbol) return;
    _log('form: symbol $_symbol -> $value');
    _symbol = value;
    _dropExactPeriod('symbol changed');
    _clearFieldError(BacktestField.symbol);
    _notify();
  }

  void setMode(BacktestMode value) {
    if (value == _mode) return;
    _log('form: mode ${_mode.code} -> ${value.code}');
    _mode = value;
    _dropExactPeriod('mode changed');
    _fieldErrors = const {};
    _submitError = null;
    _generalErrors = const [];
    _notify();
  }

  /// [day]'s calendar date is taken as a UTC day.
  void setFromDay(DateTime? day) {
    _fromDay = day == null ? null : DateTime.utc(day.year, day.month, day.day);
    _log('form: fromDay -> ${_fromDay?.toIso8601String()}');
    _dropExactPeriod('start day edited');
    _clearFieldError(BacktestField.period);
    _notify();
  }

  void setToDay(DateTime? day) {
    _toDay = day == null ? null : DateTime.utc(day.year, day.month, day.day);
    _log('form: toDay -> ${_toDay?.toIso8601String()}');
    _dropExactPeriod('end day edited');
    _clearFieldError(BacktestField.period);
    _notify();
  }

  void setWindowsCount(int value) {
    _windowsCount = value;
    _clearFieldError(BacktestField.windowsCount);
    _notify();
  }

  void setWindowMonths(int value) {
    _windowMonths = value;
    _clearFieldError(BacktestField.windowMonths);
    _notify();
  }

  void setSeedText(String value) {
    _seedText = numberEn(value.trim());
    _clearFieldError(BacktestField.seed);
    _notify();
  }

  /// Fills the seed with a new random integer (always visible, sent with the run).
  void newRandomSeed() {
    _seedText = _newSeed().toString();
    _log('form: new random seed $_seedText');
    _clearFieldError(BacktestField.seed);
    formGeneration++;
    _notify();
  }

  int _newSeed() => _random.nextInt(1 << 32);

  void setCommission(double value) {
    _commission = value;
    _clearFieldError(BacktestField.commission);
    _notify();
  }

  void setSpreadChoice(SpreadChoice value) {
    if (value == _spreadChoice) return;
    _log('form: spread ${_spreadChoice.name} -> ${value.name}');
    _spreadChoice = value;
    _clearFieldError(BacktestField.fallbackSpread);
    _notify();
  }

  void setCustomSpreadPoints(int value) {
    _customSpreadPoints = value;
    _clearFieldError(BacktestField.fallbackSpread);
    _notify();
  }

  void _clearFieldError(String field) {
    if (!_fieldErrors.containsKey(field)) return;
    _fieldErrors = {..._fieldErrors}..remove(field);
  }

  /// Client-side checks before any request (the engine re-validates
  /// everything; its Persian messages replace these on a 422).
  Map<String, List<String>> validate() {
    final Map<String, List<String>> errors = {};
    void add(String field, String message) => (errors[field] ??= []).add(message);
    if (_symbol == null) add(BacktestField.symbol, 'نماد را انتخاب کنید.');
    if (_mode == BacktestMode.manual && hasExactPeriod) {
      if (!_exactFrom!.isBefore(_exactTo!)) add(BacktestField.period, 'ابتدای بازه باید قبل از انتهای آن باشد.');
    } else if (_mode == BacktestMode.manual) {
      if (_fromDay == null) add(BacktestField.period, 'ابتدای بازه را انتخاب کنید.');
      if (_toDay == null) add(BacktestField.period, 'انتهای بازه را انتخاب کنید.');
      if (_fromDay != null && _toDay != null && _fromDay!.isAfter(_toDay!)) {
        add(BacktestField.period, 'ابتدای بازه باید قبل از انتهای آن باشد.');
      }
    } else {
      if (_seedText.isNotEmpty && _parseSeed(_seedText) == null) {
        add(BacktestField.seed, 'seed باید یک عدد صحیح بین 0 و $seedMax باشد.');
      }
    }
    return errors;
  }

  static int? _parseSeed(String text) {
    final int? v = int.tryParse(text);
    return v == null || v < 0 || v > seedMax ? null : v;
  }

  /// The request for the current form (call [validate] first).
  BacktestRequest buildRequest() {
    final int? fallback = switch (_spreadChoice) {
      SpreadChoice.auto => null,
      SpreadChoice.zero => 0,
      SpreadChoice.custom => _customSpreadPoints,
    };
    if (_mode == BacktestMode.manual) {
      final bool exact = hasExactPeriod;
      return BacktestRequest.manual(
        symbol: _symbol!,
        // Exact prefill instants verbatim; otherwise whole UTC days, the end day included.
        from: exact ? _exactFrom! : _fromDay!,
        to: exact ? _exactTo! : _toDay!.add(const Duration(days: 1)),
        commissionPerLotPerSide: _commission,
        fallbackSpreadPoints: fallback,
      );
    }
    return BacktestRequest.random(
      symbol: _symbol!,
      windowsCount: _windowsCount,
      windowMonths: _windowMonths,
      seed: _seedText.isEmpty ? null : _parseSeed(_seedText),
      commissionPerLotPerSide: _commission,
      fallbackSpreadPoints: fallback,
    );
  }

  /// Validates, submits and starts following the new run. Returns true when
  /// the engine accepted it.
  Future<bool> submit() async {
    if (_submitting) {
      _log('submit ignored: already submitting');
      return false;
    }
    if (activeRunId != null) {
      _log('submit ignored: run #$activeRunId is still being followed');
      return false;
    }
    final Map<String, List<String>> errors = validate();
    _submitError = null;
    _generalErrors = const [];
    if (errors.isNotEmpty) {
      _fieldErrors = errors;
      _log('submit blocked by form validation: $errors');
      _notify();
      return false;
    }
    _fieldErrors = const {};
    final BacktestRequest request = buildRequest();
    _submitting = true;
    _log('submit -> $request');
    _notify();
    try {
      final BacktestSubmitResult result = await api.submitBacktest(request);
      if (_disposed) return false;
      _log('submit <- run #${result.id} ${result.status.code} provisional=${result.provisional} '
          'labels=${result.labelsFa}');
      _submitting = false;
      _startFollowing(result.id);
      unawaited(openRun(result.id));
      unawaited(loadRuns());
      return true;
    } on EngineApiException catch (e) {
      if (_disposed) return false;
      _submitting = false;
      _applySubmitError(e);
      _notify();
      return false;
    }
  }

  void _applySubmitError(EngineApiException e) {
    _log('submit failed: $e');
    final String? code = e.code;
    if (e.errorsFa.isNotEmpty) {
      // Per-field messages under their fields; the headline and the rest in the banner.
      final split = e.splitErrors(BacktestField.engineMarkers);
      _fieldErrors = split.byField;
      _generalErrors = split.general;
      _submitError = e;
    } else if (code != null && BacktestField.periodCodes.contains(code)) {
      _fieldErrors = {
        BacktestField.period: [e.messageFa],
      };
    } else if (code != null && BacktestField.symbolCodes.contains(code)) {
      _fieldErrors = {
        BacktestField.symbol: [e.messageFa],
      };
    } else {
      _submitError = e;
    }
  }

  /// Fills the form from a shell request (chart «بک‌تست همین بازه»); with
  /// `autoRun` it also submits.
  Future<void> applyPrefill(BacktestPrefill p) async {
    _log('prefill $p');
    _symbol = p.symbol;
    _mode = BacktestMode.manual;
    final DateTime? from = p.from?.toUtc();
    final DateTime? to = p.to?.toUtc();
    if (from != null) _fromDay = DateTime.utc(from.year, from.month, from.day);
    if (to != null) {
      // `to` is exclusive: a period ending exactly at midnight ends the day before.
      final DateTime day = DateTime.utc(to.year, to.month, to.day);
      final bool midnight = to == day;
      _toDay = midnight && (from == null || day.isAfter(from)) ? day.subtract(const Duration(days: 1)) : day;
    }
    // The days above are only shown; a full [from, to) is run exactly as given.
    _exactFrom = from != null && to != null ? from : null;
    _exactTo = from != null && to != null ? to : null;
    if (hasExactPeriod) _log('prefill: exact period ${from!.toIso8601String()} .. ${to!.toIso8601String()}');
    _fieldErrors = const {};
    _submitError = null;
    _generalErrors = const [];
    formGeneration++;
    _notify();
    if (p.autoRun) await submit();
  }

  // ------------------------------------------------------------ progress

  int? _activeRunId;
  BacktestProgress? _progress;
  BacktestProgressWatcher? _watcher;
  StreamSubscription<BacktestProgress>? _watcherSub;
  bool _cancelling = false;
  EngineApiException? _cancelError;

  /// The run being followed (queued/running), if any.
  int? get activeRunId => _activeRunId;

  /// Last progress (or final) message of the followed run.
  BacktestProgress? get progress => _progress;
  bool get isCancelling => _cancelling;
  EngineApiException? get cancelError => _cancelError;

  /// True when the progress comes from polling instead of the socket.
  bool get progressViaPolling => _watcher?.isPolling ?? false;

  void _startFollowing(int id) {
    _stopFollowing();
    _activeRunId = id;
    _progress = BacktestProgress(type: 'progress', id: id, status: BacktestStatus.queued, phase: 'queued', percent: 0);
    _cancelling = false;
    _cancelError = null;
    _log('following run #$id');
    final BacktestProgressWatcher watcher = _watcherFactory(api, id);
    _watcher = watcher;
    _watcherSub = watcher.events.listen(
      (BacktestProgress m) => _onProgress(id, m),
      onDone: () => _log('progress stream of #$id ended'),
    );
    watcher.start();
    _notify();
  }

  void _stopFollowing() {
    unawaited(_watcherSub?.cancel());
    unawaited(_watcher?.close());
    _watcherSub = null;
    _watcher = null;
  }

  void _onProgress(int id, BacktestProgress m) {
    if (_disposed || id != _activeRunId) return;
    _log('run #$id progress: $m${progressViaPolling ? ' (polling)' : ''}');
    _progress = m;
    if (m.isFinal) {
      _activeRunId = null;
      _cancelling = false;
      _stopFollowing();
      _log('run #$id finished: ${m.type}${m.messageFa != null ? ' — ${m.messageFa}' : ''}');
      unawaited(loadRuns());
      if (m.type != BacktestProgress.lostType && _openRunId == id) unawaited(openRun(id));
    }
    _notify();
  }

  /// Asks the engine to cancel the followed run; the final `cancelled`
  /// message arrives through the watcher.
  Future<void> cancel() async {
    final int? id = _activeRunId;
    if (id == null || _cancelling) return;
    _cancelling = true;
    _cancelError = null;
    _log('cancel -> run #$id');
    _notify();
    try {
      final BacktestCancelResult r = await api.cancelBacktest(id);
      _log('cancel <- run #$id ${r.status.code} cancel_requested=${r.cancelRequested}');
    } on EngineApiException catch (e) {
      _log('cancel failed: $e');
      if (_disposed) return;
      _cancelling = false;
      _cancelError = e;
      if (e.code == 'not_cancellable') unawaited(loadRuns());
      _notify();
    }
  }

  // ------------------------------------------------------------ results

  int? _openRunId;
  BacktestRunDetail? _detail;
  bool _detailLoading = false;
  EngineApiException? _detailError;
  BacktestTradesPage? _trades;
  BacktestEquity? _equity;
  BacktestSkippedList? _skipped;
  bool _resultsLoading = false;
  EngineApiException? _resultsError;
  int? _selectedWindow;

  int? get openRunId => _openRunId;
  BacktestRunDetail? get detail => _detail;
  bool get detailLoading => _detailLoading;
  EngineApiException? get detailError => _detailError;
  BacktestTradesPage? get trades => _trades;
  BacktestEquity? get equity => _equity;
  BacktestSkippedList? get skipped => _skipped;
  bool get resultsLoading => _resultsLoading;
  EngineApiException? get resultsError => _resultsError;

  /// Random runs: the window whose trades / equity / skipped are shown
  /// (null = all windows; the equity chart then shows window 0).
  int? get selectedWindow => _selectedWindow;

  /// Price digits of the opened run (its data snapshot, else `/symbols`).
  int? get openRunDigits => _detail?.fingerprint?.specDigits ?? digitsOf(_detail?.summary.symbol);

  void selectWindow(int? index) {
    if (index == _selectedWindow) return;
    _log('results: window ${_selectedWindow ?? 'all'} -> ${index ?? 'all'}');
    _selectedWindow = index;
    _notify();
  }

  /// Loads a run's detail and, once it is done, its trades, equity and skipped candidates.
  Future<void> openRun(int id) async {
    final bool same = _openRunId == id;
    _openRunId = id;
    _detailLoading = true;
    _detailError = null;
    if (!same) {
      _detail = null;
      _trades = null;
      _equity = null;
      _skipped = null;
      _resultsError = null;
      _selectedWindow = null;
    }
    _log('open run #$id');
    _notify();
    try {
      final BacktestRunDetail d = await api.getBacktest(id);
      if (_disposed || _openRunId != id) return;
      _detail = d;
      _detailLoading = false;
      _log('run #$id detail: ${d.status.code} kind=${d.metricsKind} windows=${d.windows.length} '
          'labels=${d.labelsFa}');
      if (d.status.isActive && _activeRunId != id && _activeRunId == null) _startFollowing(id);
      _notify();
      if (d.status == BacktestStatus.done) await _loadResults(id);
    } on EngineApiException catch (e) {
      _log('open run #$id failed: $e');
      if (_disposed || _openRunId != id) return;
      _detailLoading = false;
      _detailError = e;
      _notify();
    }
  }

  Future<void> _loadResults(int id) async {
    _resultsLoading = true;
    _resultsError = null;
    _notify();
    try {
      final (BacktestTradesPage t, BacktestEquity e, BacktestSkippedList s) = await (
        api.getBacktestTrades(id),
        api.getBacktestEquity(id),
        api.getBacktestSkipped(id),
      ).wait;
      if (_disposed || _openRunId != id) return;
      _trades = t;
      _equity = e;
      _skipped = s;
      _log('run #$id results: ${t.trades.length}/${t.total} trades, ${e.points.length} equity points, '
          '${s.skipped.length} skipped');
    } on ParallelWaitError<(BacktestTradesPage?, BacktestEquity?, BacktestSkippedList?),
        (AsyncError?, AsyncError?, AsyncError?)> catch (err) {
      if (_disposed || _openRunId != id) return;
      final (AsyncError? a, AsyncError? b, AsyncError? c) = err.errors;
      final Object? first = (a ?? b ?? c)?.error;
      _log('run #$id results failed: $first');
      _trades = err.values.$1;
      _equity = err.values.$2;
      _skipped = err.values.$3;
      _resultsError = first is EngineApiException
          ? first
          : EngineApiException(EngineApiErrorKind.unknown, 'بارگذاری نتایج ناموفق بود.', detail: '$first');
    } finally {
      if (!_disposed && _openRunId == id) {
        _resultsLoading = false;
        _notify();
      }
    }
  }

  /// Leaves the result view (the list stays).
  void closeRun() {
    _openRunId = null;
    _detail = null;
    _detailError = null;
    _trades = null;
    _equity = null;
    _skipped = null;
    _resultsError = null;
    _selectedWindow = null;
    _notify();
  }

  // --------------------------------------------------------------- runs

  List<BacktestRunSummary> _runs = const [];
  bool _runsLoading = false;
  EngineApiException? _runsError;

  List<BacktestRunSummary> get runs => _runs;
  bool get runsLoading => _runsLoading;
  EngineApiException? get runsError => _runsError;

  Future<void> loadRuns() async {
    _runsLoading = true;
    _runsError = null;
    _notify();
    try {
      final BacktestRunList list = await api.listBacktests(limit: runsLimit);
      if (_disposed) return;
      _runs = list.runs;
      _log('runs: ${list.runs.length} (${list.runs.where((r) => r.status.isActive).length} active)');
    } on EngineApiException catch (e) {
      _log('runs failed: $e');
      if (_disposed) return;
      _runsError = e;
    } finally {
      if (!_disposed) {
        _runsLoading = false;
        _notify();
      }
    }
  }

  /// Deletes a finished run. Returns the engine's error, or null on success.
  Future<EngineApiException?> deleteRun(int id) async {
    _log('delete -> run #$id');
    try {
      await api.deleteBacktest(id);
      if (_disposed) return null;
      _log('delete <- run #$id deleted');
      _runs = [
        for (final BacktestRunSummary r in _runs)
          if (r.id != id) r
      ];
      if (_openRunId == id) closeRun();
      _notify();
      return null;
    } on EngineApiException catch (e) {
      _log('delete run #$id failed: $e');
      if (!_disposed && e.statusCode == 404) unawaited(loadRuns());
      return e;
    }
  }

  // ---------------------------------------------------------- lifecycle

  bool _initialized = false;

  bool get initialized => _initialized;

  /// Loads symbols and runs; a run still queued/running (e.g. the page was
  /// rebuilt after an engine restart) is followed and opened again.
  Future<void> init() async {
    _log('init (engine :${api.port})');
    await Future.wait([_loadSymbols(), loadRuns()]);
    if (_disposed) return;
    _initialized = true;
    BacktestRunSummary? active;
    for (final BacktestRunSummary r in _runs) {
      if (r.status.isActive) {
        active = r;
        break;
      }
    }
    if (active != null && _activeRunId == null) {
      _log('recovering active run #${active.id} (${active.status.code})');
      _startFollowing(active.id);
      unawaited(openRun(active.id));
    }
    _notify();
  }

  Future<void> _loadSymbols() async {
    _symbolsLoading = true;
    _symbolsError = null;
    _notify();
    try {
      final SymbolsResponse r = await api.getSymbols();
      if (_disposed) return;
      _symbols = r.symbols;
      _symbol ??= _symbols.isEmpty ? null : _symbols.first.symbol;
      _log('symbols: ${_symbols.map((s) => s.symbol).toList()}');
    } on EngineApiException catch (e) {
      _log('symbols failed: $e');
      if (_disposed) return;
      _symbolsError = e;
    } finally {
      if (!_disposed) {
        _symbolsLoading = false;
        _notify();
      }
    }
  }

  Future<void> retrySymbols() => _loadSymbols();

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _log('dispose');
    _disposed = true;
    _stopFollowing();
    super.dispose();
  }

  static void _log(String message) {
    if (!kDevMode) return;
    final String line = '[BacktestController] $message';
    devLog(line);
    AppLogger.log(line);
  }
}
