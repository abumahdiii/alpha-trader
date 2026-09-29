import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../chart/chart_controller.dart' show kNoChannelFa;
import '../../chart/chart_data.dart';
import '../../chart/chart_data_source.dart';
import '../../chart/chart_painter.dart';
import '../../chart/chart_viewport.dart';
import '../../core/dev_mode.dart';
import '../../models/chart_models.dart';
import '../../models/signal_models.dart';
import '../../models/strategy.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/status_message.dart';

/// Bars shown before the confirmation candle.
const int kMiniChartBarsBefore = 60;

/// Calendar span loaded before the confirmation bar: 60 H1 bars plus a
/// weekend fit in it.
const Duration kMiniChartLookback = Duration(days: 5);

/// A signal that is no longer active: bars loaded after its confirmation.
const Duration kMiniChartAfter = Duration(days: 2);

/// `[from, to)` of the mini chart's `GET /rates` (UTC): [kMiniChartLookback]
/// before the confirmation bar, up to the latest bar (`to` null = now) for an
/// active signal, else [kMiniChartAfter] after it (capped at [now]).
(DateTime, DateTime?) signalChartRange(DateTime confirmation, {required bool active, required DateTime now}) {
  final DateTime conf = confirmation.toUtc();
  final DateTime from = conf.subtract(kMiniChartLookback);
  if (active) return (from, null);
  final DateTime end = conf.add(kMiniChartAfter);
  return (from, end.isAfter(now.toUtc()) ? null : end);
}

/// The entry / SL / TP levels of [s] from the bar after [confIndex]
/// (engine prices; indicative entry and TP dashed).
List<ChartPriceLevel> signalLevels(LiveSignal s, int? confIndex) {
  final int? from = confIndex == null ? null : confIndex + 1;
  return <ChartPriceLevel>[
    if (s.indicativeEntry != null)
      ChartPriceLevel(
          kind: ChartLevelKind.entry, price: s.indicativeEntry!, tag: 'Entry≈', fromIndex: from, dashed: true),
    if (s.stopLoss != null)
      ChartPriceLevel(kind: ChartLevelKind.stopLoss, price: s.stopLoss!, tag: 'SL', fromIndex: from),
    if (s.takeProfitIndicative != null)
      ChartPriceLevel(
          kind: ChartLevelKind.takeProfit, price: s.takeProfitIndicative!, tag: 'TP≈', fromIndex: from, dashed: true),
  ];
}

/// Read-only H1 mini chart of one live signal: ~[kMiniChartBarsBefore] bars
/// before the confirmation candle up to the latest bar, the strategy's
/// channel (only for a strategy that has one, like the chart page), the
/// confirmation candle highlighted and horizontal indicative entry / SL /
/// TP lines with their prices. Reuses the chart page's [ChartData] and
/// [ChartPainter]; no zoom, no pan, no action.
class SignalMiniChart extends StatefulWidget {
  const SignalMiniChart({super.key, required this.source, required this.signal, this.digits, this.height = 260});

  final ChartDataSource source;
  final LiveSignal signal;
  final int? digits;
  final double height;

  static const String legendFa = 'کندل تایید پررنگ شده است؛ خط‌چین‌ها: ورود و حد سود تقریبی، خط پیوسته: حد ضرر.';

  @override
  State<SignalMiniChart> createState() => SignalMiniChartState();
}

class SignalMiniChartState extends State<SignalMiniChart> {
  ChartData? _data;
  int? _confIndex;
  String? _channelNote;
  EngineApiException? _error;
  bool _loading = true;
  int _serial = 0;

  /// Loaded bars (read by tests).
  ChartData? get data => _data;
  int? get confirmationIndex => _confIndex;

  /// True while [initState] runs: the synchronous start of [_load] must not
  /// call setState then.
  bool _inInit = false;

  @override
  void initState() {
    super.initState();
    _inInit = true;
    unawaited(_load());
    _inInit = false;
  }

  void _set(VoidCallback change) => _inInit ? change() : setState(change);

  @override
  void didUpdateWidget(SignalMiniChart oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.signal.id != widget.signal.id || !identical(oldWidget.source, widget.source)) unawaited(_load());
  }

  Future<void> _load() async {
    final int serial = ++_serial;
    final LiveSignal s = widget.signal;
    final DateTime? conf = s.confirmationBarTime;
    if (conf == null) {
      _set(() {
        _loading = false;
        _error = const EngineApiException(EngineApiErrorKind.malformedBody, 'زمان کندل تایید این سیگنال مشخص نیست.');
      });
      return;
    }
    _set(() {
      _loading = true;
      _error = null;
    });
    final (DateTime from, DateTime? to) = signalChartRange(conf, active: s.isActive, now: DateTime.now());
    final bool withChannel = strategyHasChannel(s.strategy);
    devLog('[SignalChart #${s.id}] ${s.symbol} H1 ${from.toIso8601String()} .. ${to?.toIso8601String() ?? 'latest'}'
        ' channel=${withChannel ? s.strategy : 'none'}');
    try {
      final Future<RatesResult> rates = widget.source.rates(s.symbol, ChartTimeframe.h1, from: from, to: to);
      final Future<(ChannelResult?, String?)> channel = withChannel
          ? widget.source
              .channel(s.symbol, ChartTimeframe.h1, from: from, to: to, strategy: s.strategy)
              .then<(ChannelResult?, String?)>((ChannelResult c) => (c, null), onError: (Object e) {
              devLog('[SignalChart #${s.id}] channel unavailable: $e');
              return (null, e is EngineApiException ? e.messageFa : 'کانال بارگذاری نشد.');
            })
          : Future<(ChannelResult?, String?)>.value((null, kNoChannelFa));
      final RatesResult r = await rates;
      final (ChannelResult? c, String? note) = await channel;
      if (!mounted || serial != _serial) return;
      final ChartData data = ChartData.build(
        rates: r,
        timeframe: ChartTimeframe.h1,
        channelResult: c,
        digits: widget.digits,
      );
      final int? confIndex = data.containingIndex(conf);
      devLog('[SignalChart #${s.id}] ${data.length} bars, confirmation index ${confIndex ?? 'not loaded'}');
      setState(() {
        _data = data;
        _confIndex = confIndex;
        _channelNote = note;
        _loading = false;
      });
    } on EngineApiException catch (e) {
      devLog('[SignalChart #${s.id}] load failed: $e');
      if (!mounted || serial != _serial) return;
      setState(() {
        _loading = false;
        _error = e;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final TextStyle? small = Theme.of(context).textTheme.bodySmall?.copyWith(color: colors.mutedText);
    final ChartData? data = _data;
    final EngineApiException? error = _error;
    final Widget body;
    if (_loading) {
      body = const Center(child: CircularProgressIndicator());
    } else if (error != null) {
      body = StatusMessage.apiError(title: 'نمودار سیگنال بارگذاری نشد', error: error, onRetry: _load);
    } else if (data == null || data.isEmpty) {
      body = const StatusMessage(icon: Icons.inbox_outlined, title: 'در این بازه داده‌ای در کش نیست.');
    } else {
      body =
          SignalChartCanvas(data: data, confirmationIndex: _confIndex, levels: signalLevels(widget.signal, _confIndex));
    }
    return Column(
      key: ValueKey<String>('signal-mini-chart-${widget.signal.id}'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        SizedBox(height: widget.height, child: body),
        const SizedBox(height: 4),
        Text(SignalMiniChart.legendFa, style: small),
        if (_channelNote != null) Text(_channelNote!, style: small),
        if (data != null && !_loading && _confIndex == null)
          Text('کندل تایید در داده کش نیست؛ فقط سطح‌ها نمایش داده می‌شوند.', style: small),
      ],
    );
  }
}

/// The static painting of the mini chart: all bars from
/// [kMiniChartBarsBefore] before [confirmationIndex] to the newest fit the
/// width; the price scale also covers every level. Always left-to-right.
class SignalChartCanvas extends StatelessWidget {
  const SignalChartCanvas({super.key, required this.data, required this.confirmationIndex, required this.levels});

  final ChartData data;
  final int? confirmationIndex;
  final List<ChartPriceLevel> levels;

  /// The viewport showing bars `first..newest` across [plotWidth].
  static ChartViewport viewportFor(ChartData data, int? confirmationIndex, double plotWidth) {
    final int first = math.max(0, (confirmationIndex ?? 0) - kMiniChartBarsBefore);
    final double slots = (data.length - first) + ChartViewport.rightPadBars;
    return ChartViewport(
      barCount: data.length,
      width: plotWidth,
      barWidth: plotWidth / math.max(slots, 1),
      startIndex: first.toDouble(),
    );
  }

  /// [autoscalePrice] over the visible bars, widened to include [levels].
  static PriceScale scaleFor(ChartData data, IndexRange range, ChartLayout layout, List<ChartPriceLevel> levels) {
    final double top = layout.plot.top + ChartLayout.plotPadTop;
    final double bottom = layout.plot.bottom - ChartLayout.plotPadBottom;
    final PriceScale bars = autoscalePrice(data, range, top: top, bottom: bottom);
    double lo = bars.min;
    double hi = bars.max;
    for (final ChartPriceLevel l in levels) {
      if (!l.price.isFinite) continue;
      lo = math.min(lo, l.price);
      hi = math.max(hi, l.price);
    }
    if (lo == bars.min && hi == bars.max) return bars;
    final double pad = (hi - lo) * 0.04;
    return PriceScale(min: lo - pad, max: hi + pad, top: top, bottom: bottom);
  }

  @override
  Widget build(BuildContext context) {
    final ChartStyle style = ChartStyle.fromTheme(Theme.of(context));
    return Directionality(
      textDirection: TextDirection.ltr,
      child: LayoutBuilder(builder: (BuildContext context, BoxConstraints box) {
        final ChartLayout layout = ChartLayout(Size(box.maxWidth, box.maxHeight));
        final ChartViewport viewport = viewportFor(data, confirmationIndex, layout.plot.width);
        final IndexRange? range = viewport.visibleRange;
        final PriceScale scale =
            range == null ? const PriceScale(min: 0, max: 1, top: 0, bottom: 1) : scaleFor(data, range, layout, levels);
        return ClipRect(
          child: CustomPaint(
            size: layout.size,
            painter: ChartPainter(
              data: data,
              viewport: viewport,
              layout: layout,
              scale: scale,
              style: style,
              highlightIndex: confirmationIndex,
              levels: levels,
            ),
          ),
        );
      }),
    );
  }
}
