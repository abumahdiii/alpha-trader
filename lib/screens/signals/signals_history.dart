import 'dart:async';

import 'package:flutter/material.dart';

import '../../chart/chart_data_source.dart';
import '../../chart/chart_format.dart' show formatValue;
import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../models/signal_models.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/sortable_table.dart';
import '../../widgets/status_message.dart';
import '../backtest/backtest_format.dart';
import 'signal_card.dart';
import 'signal_format.dart';

/// «تاریخچه»: every stored signal (`GET /signals`), paged and filtered by
/// status and symbol, newest confirmation bar first. A row opens the
/// details with the mini chart. Read-only.
class SignalsHistory extends StatefulWidget {
  const SignalsHistory({
    super.key,
    required this.api,
    required this.source,
    this.symbols = const [],
    this.digitsOf,
    this.pageSize = 50,
  });

  final EngineApi api;
  final ChartDataSource source;

  /// Choices of the symbol filter (the engine's symbols).
  final List<String> symbols;
  final int? Function(String symbol)? digitsOf;
  final int pageSize;

  static const String allStatusesFa = 'همه وضعیت‌ها';
  static const String allSymbolsFa = 'همه نمادها';
  static const String previousLabel = 'صفحه قبل';
  static const String nextLabel = 'صفحه بعد';
  static const String reloadLabel = 'بارگذاری دوباره';
  static const String emptyFa = 'سیگنالی با این فیلترها ثبت نشده است.';

  @override
  State<SignalsHistory> createState() => SignalsHistoryState();
}

class SignalsHistoryState extends State<SignalsHistory> {
  LiveSignalStatus? _status;
  String? _symbol;
  int _offset = 0;
  SignalsPage? _page;
  EngineApiException? _error;
  bool _loading = true;
  int _serial = 0;

  /// Shown page (read by tests).
  SignalsPage? get page => _page;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    final int serial = ++_serial;
    if (mounted && !_loading) setState(() => _loading = true);
    _log('load status=${_status?.code ?? 'all'} symbol=${_symbol ?? 'all'} offset=$_offset limit=${widget.pageSize}');
    try {
      final SignalsPage page = await widget.api.listSignals(
        status: _status,
        symbol: _symbol,
        limit: widget.pageSize,
        offset: _offset,
      );
      if (!mounted || serial != _serial) return;
      _log('page ${page.offset}+${page.signals.length} of ${page.total}');
      setState(() {
        _page = page;
        _error = null;
        _loading = false;
      });
    } on EngineApiException catch (e) {
      _log('load failed: $e');
      if (!mounted || serial != _serial) return;
      setState(() {
        _error = e;
        _loading = false;
      });
    }
  }

  void _filter({LiveSignalStatus? status, String? symbol, bool clearStatus = false, bool clearSymbol = false}) {
    setState(() {
      if (status != null || clearStatus) _status = status;
      if (symbol != null || clearSymbol) _symbol = symbol;
      _offset = 0;
    });
    unawaited(_load());
  }

  void _goTo(int offset) {
    setState(() => _offset = offset < 0 ? 0 : offset);
    unawaited(_load());
  }

  int? _digits(String symbol) => widget.digitsOf?.call(symbol);

  @override
  Widget build(BuildContext context) {
    final SignalsPage? page = _page;
    final EngineApiException? error = _error;
    final TextTheme tt = Theme.of(context).textTheme;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Wrap(spacing: 16, runSpacing: 8, crossAxisAlignment: WrapCrossAlignment.center, children: [
        DropdownButton<LiveSignalStatus?>(
          key: const ValueKey<String>('signals-history-status'),
          value: _status,
          items: [
            const DropdownMenuItem<LiveSignalStatus?>(value: null, child: Text(SignalsHistory.allStatusesFa)),
            for (final LiveSignalStatus s in LiveSignalStatus.filterable)
              DropdownMenuItem<LiveSignalStatus?>(value: s, child: Text(s.labelFa)),
          ],
          onChanged: (LiveSignalStatus? v) => _filter(status: v, clearStatus: v == null),
        ),
        DropdownButton<String?>(
          key: const ValueKey<String>('signals-history-symbol'),
          value: _symbol,
          items: [
            const DropdownMenuItem<String?>(value: null, child: Text(SignalsHistory.allSymbolsFa)),
            for (final String s in {...widget.symbols, if (_symbol != null) _symbol!})
              DropdownMenuItem<String?>(value: s, child: Text(s, textDirection: TextDirection.ltr)),
          ],
          onChanged: (String? v) => _filter(symbol: v, clearSymbol: v == null),
        ),
        OutlinedButton.icon(
          key: const ValueKey<String>('signals-history-reload'),
          onPressed: _loading ? null : () => unawaited(_load()),
          icon: const Icon(Icons.refresh),
          label: const Text(SignalsHistory.reloadLabel),
        ),
        if (_loading) const SizedBox.square(dimension: 18, child: CircularProgressIndicator(strokeWidth: 2)),
      ]),
      const SizedBox(height: 8),
      Expanded(
        child: error != null
            ? StatusMessage.apiError(title: 'خواندن تاریخچه سیگنال‌ها ناموفق بود', error: error, onRetry: _load)
            : page == null
                ? const Center(child: CircularProgressIndicator())
                : page.signals.isEmpty
                    ? const StatusMessage(
                        key: ValueKey<String>('signals-history-empty'),
                        icon: Icons.inbox_outlined,
                        title: SignalsHistory.emptyFa,
                      )
                    : SortableTable<LiveSignal>(
                        keyPrefix: 'signals-history',
                        columns: _columns(context),
                        rows: page.signals,
                        onRowTap: (LiveSignal s) {
                          _log('row #${s.id} -> details');
                          unawaited(showSignalDetails(context, s, source: widget.source, digits: _digits(s.symbol)));
                        },
                      ),
      ),
      if (page != null && page.total > 0)
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Row(children: [
            Text(
              '${fmtInt(page.offset + 1)}–${fmtInt(page.offset + page.signals.length)} از ${fmtInt(page.total)}',
              key: const ValueKey<String>('signals-history-range'),
              style: tt.bodyMedium,
            ),
            const Spacer(),
            OutlinedButton.icon(
              key: const ValueKey<String>('signals-history-prev'),
              onPressed: _loading || !page.hasPrevious ? null : () => _goTo(page.offset - widget.pageSize),
              icon: const Icon(Icons.chevron_right),
              label: const Text(SignalsHistory.previousLabel),
            ),
            const SizedBox(width: 8),
            OutlinedButton.icon(
              key: const ValueKey<String>('signals-history-next'),
              onPressed: _loading || !page.hasNext ? null : () => _goTo(page.offset + page.signals.length),
              icon: const Icon(Icons.chevron_left),
              label: const Text(SignalsHistory.nextLabel),
            ),
          ]),
        ),
    ]);
  }

  List<SortableColumn<LiveSignal>> _columns(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    String levels(LiveSignal s) {
      final int? d = _digits(s.symbol);
      return '${signalPrice(s.indicativeEntry, d)} / ${signalPrice(s.stopLoss, d)} / ${signalPrice(s.takeProfitIndicative, d)}';
    }

    String reason(LiveSignal s) => [
          if (s.statusReasonFa != null) s.statusReasonFa!,
          if (s.mismatchFields.isNotEmpty) 'فیلدها: ${s.mismatchFields.join('، ')}',
        ].join(' — ');

    return [
      SortableColumn<LiveSignal>(
        label: 'کندل تایید ($kLocalTimeFa)',
        width: 150,
        cell: (_, LiveSignal s) => LtrText(fmtLocal(s.confirmationBarTime)),
        sortKey: (LiveSignal s) => s.confirmationBarTime,
      ),
      SortableColumn<LiveSignal>(
        label: 'نماد',
        width: 110,
        cell: (_, LiveSignal s) => LtrText(s.symbol),
        sortKey: (LiveSignal s) => s.symbol,
      ),
      SortableColumn<LiveSignal>(
        label: 'جهت',
        width: 70,
        cell: (BuildContext ctx, LiveSignal s) =>
            Text(s.direction?.titleFa ?? kDash, style: TextStyle(color: directionColor(ctx, s.direction))),
      ),
      SortableColumn<LiveSignal>(
        label: 'ستاپ',
        width: 170,
        cell: (_, LiveSignal s) =>
            Text(s.setupTitleFa ?? s.setupType ?? kDash, maxLines: 1, overflow: TextOverflow.ellipsis),
      ),
      SortableColumn<LiveSignal>(
        label: 'ورود≈ / حد ضرر / حد سود≈',
        width: 260,
        cell: (_, LiveSignal s) => LtrText(levels(s)),
      ),
      SortableColumn<LiveSignal>(
        label: 'حجم',
        width: 80,
        cell: (_, LiveSignal s) => LtrText(formatValue(s.volume, 2)),
        sortKey: (LiveSignal s) => s.volume,
      ),
      SortableColumn<LiveSignal>(
        label: 'وضعیت',
        width: 110,
        cell: (_, LiveSignal s) => SignalStatusChip(s.status),
        sortKey: (LiveSignal s) => s.status.labelFa,
      ),
      SortableColumn<LiveSignal>(
        label: 'دلیل رد / ناهمخوانی',
        width: 340,
        cell: (_, LiveSignal s) => Tooltip(
          message: reason(s),
          child: Text(reason(s).isEmpty ? kDash : reason(s),
              maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(color: c.mutedText)),
        ),
      ),
    ];
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[SignalsHistory] $message');
    AppLogger.log('[SignalsHistory] $message');
  }
}
