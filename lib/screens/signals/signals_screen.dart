import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../chart/chart_data_source.dart';
import '../../models/signal_models.dart';
import '../../providers/signals_provider.dart';
import '../../services/engine_api.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/engine_gate.dart';
import '../../widgets/status_message.dart';
import 'signal_card.dart';
import 'signal_format.dart';
import 'signals_history.dart';
import 'signals_status_strip.dart';

/// Builds the chart data source of the mini charts (injected in tests).
typedef SignalChartSourceFactory = ChartDataSource Function(EngineApi api);

/// «سیگنال»: the live signal dashboard (scheduler status + active signal
/// cards, from the app-level [SignalsProvider]) and the stored history.
///
/// SUGGESTIONS ONLY: the page shows entry / SL / TP / volume / rationale and
/// has no order, trade or send action anywhere — see
/// `.claude/rules/03_trading_safety.md`. The note [kSuggestionOnlyFa] is
/// always visible.
class SignalsScreen extends StatelessWidget {
  const SignalsScreen({super.key, this.chartSource, this.clock = DateTime.now});

  final SignalChartSourceFactory? chartSource;
  final SignalClock clock;

  static const String activeTabFa = 'سیگنال‌های فعال';
  static const String historyTabFa = 'تاریخچه';
  static const String noActiveFa = 'فعلا سیگنال فعالی نیست';
  static const String noActiveBodyFa =
      'هر کندل H1 که بسته شود، موتور آن را بررسی می‌کند؛ سیگنال جدید اینجا با صدا و اعلان ویندوز نمایش داده می‌شود.';

  ChartDataSource _source(EngineApi api) => (chartSource ?? EngineChartDataSource.new)(api);

  @override
  Widget build(BuildContext context) {
    final int count = context.select<SignalsProvider, int>((SignalsProvider p) => p.activeCount);
    return DefaultTabController(
      length: 2,
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const SuggestionOnlyNote(),
        TabBar(tabs: [
          Tab(text: count > 0 ? '$activeTabFa ($count)' : activeTabFa),
          const Tab(text: historyTabFa),
        ]),
        Expanded(
          child: TabBarView(children: [
            EngineGate(
                builder: (BuildContext context, EngineApi api) => _ActiveTab(source: _source(api), clock: clock)),
            EngineGate(
              builder: (BuildContext context, EngineApi api) {
                final SignalsProvider p = context.read<SignalsProvider>();
                return Padding(
                  padding: const EdgeInsets.all(16),
                  child: SignalsHistory(
                    api: api,
                    source: _source(api),
                    symbols: p.knownSymbols,
                    digitsOf: p.digitsOf,
                  ),
                );
              },
            ),
          ]),
        ),
      ]),
    );
  }
}

/// The permanent «suggestion only» note.
class SuggestionOnlyNote extends StatelessWidget {
  const SuggestionOnlyNote({super.key});

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    return Container(
      key: const ValueKey<String>('signals-suggestion-only'),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      color: c.info.withValues(alpha: 0.10),
      child: Row(children: [
        Icon(Icons.verified_user_outlined, color: c.info, size: 18),
        const SizedBox(width: 8),
        Expanded(child: Text(kSuggestionOnlyFa, style: TextStyle(color: c.info, fontWeight: FontWeight.w600))),
      ]),
    );
  }
}

class _ActiveTab extends StatelessWidget {
  const _ActiveTab({required this.source, required this.clock});

  final ChartDataSource source;
  final SignalClock clock;

  @override
  Widget build(BuildContext context) {
    final SignalsProvider p = context.watch<SignalsProvider>();
    final List<LiveSignal> active = p.active;
    return ListView(
      key: const ValueKey<String>('signals-active-list'),
      padding: const EdgeInsets.all(16),
      children: [
        const SignalsStatusStrip(),
        const SizedBox(height: 16),
        if (active.isEmpty)
          const StatusMessage(
            key: ValueKey<String>('signals-none-active'),
            icon: Icons.notifications_none,
            title: SignalsScreen.noActiveFa,
            message: SignalsScreen.noActiveBodyFa,
          )
        else
          for (final LiveSignal s in active)
            SignalCard(
              key: ValueKey<String>('signal-card-host-${s.id}'),
              signal: s,
              source: source,
              digits: p.digitsOf(s.symbol),
              clock: clock,
            ),
      ],
    );
  }
}
