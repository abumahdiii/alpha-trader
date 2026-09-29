import 'dart:async';

import 'package:flutter/material.dart';

import '../../core/app_logger.dart';
import '../../core/dev_mode.dart';
import '../../models/strategy.dart';
import '../../models/strategy_plugin.dart';
import '../../services/engine_api.dart';
import '../../services/plugin_files.dart';
import '../../widgets/engine_gate.dart';
import '../../widgets/status_message.dart';
import '../../widgets/strategy_selector.dart';
import 'plugins_controller.dart';
import 'plugins_panel.dart';
import 'strategy_editor.dart';

/// «سیستم‌ها»: the strategies registered in the engine, the parameter
/// editor of the selected one, and the uploaded Python strategy files
/// (template download, upload with the engine's sandboxed validation,
/// enable / disable / archive).
class SystemsScreen extends StatelessWidget {
  const SystemsScreen({super.key, this.pluginFiles});

  /// File dialogs of the plugin actions (tests inject a fake); null = native.
  final PluginFileIo? pluginFiles;

  @override
  Widget build(BuildContext context) =>
      EngineGate(builder: (context, api) => StrategiesPanel(api: api, pluginFiles: pluginFiles));
}

/// Loads `GET /strategies` and `GET /plugins` and shows the plugin actions,
/// the strategy list (plugins badged), the plugin versions and a
/// [StrategyEditor].
class StrategiesPanel extends StatefulWidget {
  const StrategiesPanel({super.key, required this.api, this.pluginFiles});

  final EngineApi api;
  final PluginFileIo? pluginFiles;

  @override
  State<StrategiesPanel> createState() => _StrategiesPanelState();
}

class _StrategiesPanelState extends State<StrategiesPanel> {
  List<StrategyInfo>? _items;
  String? _selected;
  bool _loading = true;
  EngineApiException? _error;

  late final PluginsController _plugins = PluginsController(
    api: widget.api,
    files: widget.pluginFiles ?? FileSelectorPluginFiles(),
    onStrategiesChanged: () => unawaited(_load(quiet: true)),
  );

  @override
  void initState() {
    super.initState();
    _plugins.addListener(_onPlugins);
    _load();
    unawaited(_plugins.load());
  }

  @override
  void dispose() {
    _plugins.removeListener(_onPlugins);
    _plugins.dispose();
    super.dispose();
  }

  void _onPlugins() {
    if (mounted) setState(() {});
  }

  /// [quiet]: refresh after a plugin change without the full-page spinner.
  Future<void> _load({bool quiet = false}) async {
    if (!quiet) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final List<StrategyInfo> items = await widget.api.listStrategies();
      _log('loaded ${items.length} strategies: '
          '${items.map((s) => '${s.name} v${s.version}/p${s.paramsVersion}').join(', ')}');
      if (!mounted) return;
      setState(() {
        _items = items;
        _loading = false;
        _error = null;
        if (_selected == null || !items.any((s) => s.name == _selected)) {
          _selected = items.isEmpty ? null : items.first.name;
        }
      });
    } on EngineApiException catch (e) {
      _log('load failed: $e');
      if (!mounted) return;
      if (quiet && _items != null) return; // keep the list; the next refresh retries
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
    _log('select $name${_plugins.registeredFor(name) == null ? '' : ' (plugin)'}');
    setState(() => _selected = name);
  }

  @override
  Widget build(BuildContext context) {
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      PluginActionsBar(controller: _plugins),
      const Divider(height: 1),
      Expanded(child: _body(context)),
    ]);
  }

  Widget _body(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    final EngineApiException? error = _error;
    if (error != null) {
      return StatusMessage.apiError(title: 'خواندن سیستم‌ها ناموفق بود', error: error, onRetry: _load);
    }
    final List<StrategyInfo> items = _items ?? const [];
    final List<Widget> list = [
      for (final StrategyInfo s in items) _strategyTile(s),
      const Divider(),
      PluginListSection(controller: _plugins),
    ];
    if (items.isEmpty) {
      return Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SizedBox(width: 380, child: ListView(padding: const EdgeInsets.symmetric(vertical: 8), children: list)),
        const VerticalDivider(width: 1),
        const Expanded(
          child: StatusMessage(icon: Icons.inbox_outlined, title: 'هیچ سیستمی در موتور ثبت نشده است'),
        ),
      ]);
    }
    final StrategyInfo current = items.firstWhere((s) => s.name == _selected, orElse: () => items.first);
    final StrategyPlugin? plugin = _plugins.registeredFor(current.name);

    return Row(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SizedBox(
          width: 380,
          child: ListView(padding: const EdgeInsets.symmetric(vertical: 8), children: list),
        ),
        const VerticalDivider(width: 1),
        Expanded(
          child: StrategyEditor(
            key: ValueKey<String>('strategy-${current.name}'),
            api: widget.api,
            strategy: current,
            plugin: plugin?.ref,
            onSaved: _onSaved,
          ),
        ),
      ],
    );
  }

  Widget _strategyTile(StrategyInfo s) {
    final StrategyPlugin? plugin = _plugins.registeredFor(s.name);
    return ListTile(
      key: ValueKey<String>('strategy-tile-${s.name}'),
      selected: s.name == _selected,
      leading: Icon(plugin == null ? Icons.show_chart : Icons.extension_outlined),
      title: Row(children: [
        Flexible(child: Text(s.titleFa, overflow: TextOverflow.ellipsis)),
        if (plugin != null) ...[
          const SizedBox(width: 6),
          PluginBadge(tooltip: 'فایل بارگذاری‌شده v${plugin.version} — هش ${plugin.sha256}'),
        ],
      ]),
      subtitle: Text('پارامترها: نسخه ${s.paramsVersion}'),
      onTap: () => _select(s.name),
    );
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog('[SystemsPage] $message');
    AppLogger.log('[SystemsPage] $message');
  }
}
