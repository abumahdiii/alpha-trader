import 'package:flutter/material.dart';

import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../models/strategy.dart';
import '../../services/engine_api.dart';
import '../../widgets/engine_gate.dart';
import '../../widgets/status_message.dart';
import 'strategy_editor.dart';

/// «سیستم‌ها»: the strategies registered in the engine and the parameter
/// editor of the selected one.
class SystemsScreen extends StatelessWidget {
  const SystemsScreen({super.key});

  @override
  Widget build(BuildContext context) => EngineGate(builder: (context, api) => StrategiesPanel(api: api));
}

/// Loads `GET /strategies` and shows a list + [StrategyEditor].
class StrategiesPanel extends StatefulWidget {
  const StrategiesPanel({super.key, required this.api});

  final EngineApi api;

  @override
  State<StrategiesPanel> createState() => _StrategiesPanelState();
}

class _StrategiesPanelState extends State<StrategiesPanel> {
  List<StrategyInfo>? _items;
  String? _selected;
  bool _loading = true;
  EngineApiException? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final List<StrategyInfo> items = await widget.api.listStrategies();
      _log('loaded ${items.length} strategies: '
          '${items.map((s) => '${s.name} v${s.version}/p${s.paramsVersion}').join(', ')}');
      if (!mounted) return;
      setState(() {
        _items = items;
        _loading = false;
        if (_selected == null || !items.any((s) => s.name == _selected)) {
          _selected = items.isEmpty ? null : items.first.name;
        }
      });
    } on EngineApiException catch (e) {
      _log('load failed: $e');
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e;
      });
    }
  }

  void _onSaved(StrategyInfo updated) {
    setState(() {
      _items = [
        for (final StrategyInfo s in _items ?? const <StrategyInfo>[]) s.name == updated.name ? updated : s,
      ];
    });
  }

  void _select(String name) {
    if (name == _selected) return;
    _log('select $name');
    setState(() => _selected = name);
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    final EngineApiException? error = _error;
    if (error != null) {
      return StatusMessage.apiError(title: 'خواندن سیستم‌ها ناموفق بود', error: error, onRetry: _load);
    }
    final List<StrategyInfo> items = _items ?? const [];
    if (items.isEmpty) {
      return const StatusMessage(
        icon: Icons.inbox_outlined,
        title: 'هیچ سیستمی در موتور ثبت نشده است',
      );
    }
    final StrategyInfo current = items.firstWhere((s) => s.name == _selected, orElse: () => items.first);

    return Row(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SizedBox(
          width: 260,
          child: ListView(
            padding: const EdgeInsets.symmetric(vertical: 16),
            children: [
              for (final StrategyInfo s in items)
                ListTile(
                  selected: s.name == current.name,
                  leading: const Icon(Icons.show_chart),
                  title: Text(s.titleFa),
                  subtitle: Text('پارامترها: نسخه ${s.paramsVersion}'),
                  onTap: () => _select(s.name),
                ),
            ],
          ),
        ),
        const VerticalDivider(width: 1),
        Expanded(
          child: StrategyEditor(
            key: ValueKey<String>('strategy-${current.name}'),
            api: widget.api,
            strategy: current,
            onSaved: _onSaved,
          ),
        ),
      ],
    );
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[SystemsPage] $message');
    AppLogger.log('[SystemsPage] $message');
  }
}
