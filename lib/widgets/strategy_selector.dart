import 'package:flutter/material.dart';

import '../models/strategy.dart';
import '../theme/app_semantic_colors.dart';

/// Small «پلاگین» badge of an uploaded (sandboxed) strategy.
class PluginBadge extends StatelessWidget {
  const PluginBadge({super.key, this.tooltip});

  /// e.g. the full file SHA-256.
  final String? tooltip;

  static const String labelFa = 'پلاگین';

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors colors = context.appColors;
    final Widget badge = Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(
        color: colors.info.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: colors.info.withValues(alpha: 0.5)),
      ),
      child: Text(labelFa, style: TextStyle(fontSize: 11, color: colors.info)),
    );
    return tooltip == null ? badge : Tooltip(message: tooltip!, child: badge);
  }
}

/// Dropdown of the registered strategies (builtin first, plugins with a
/// [PluginBadge]). A [value] the list does not contain (not loaded yet, or
/// a prefill of a strategy the engine no longer has) stays visible.
class StrategySelector extends StatelessWidget {
  const StrategySelector({
    super.key,
    required this.options,
    required this.value,
    required this.onChanged,
    this.enabled = true,
    this.isExpanded = false,
  });

  final List<StrategyOption> options;
  final String value;
  final ValueChanged<String>? onChanged;
  final bool enabled;
  final bool isExpanded;

  static const String tooltipFa = 'سیستم معاملاتی';

  @override
  Widget build(BuildContext context) {
    final List<StrategyOption> items = [
      ...options,
      if (!options.any((StrategyOption o) => o.name == value)) StrategyOption(name: value),
    ];
    return Tooltip(
      message: tooltipFa,
      child: DropdownButton<String>(
        value: value,
        isExpanded: isExpanded,
        items: [
          for (final StrategyOption o in items)
            DropdownMenuItem<String>(
              key: ValueKey<String>('strategy-option-${o.name}'),
              value: o.name,
              child: Row(mainAxisSize: isExpanded ? MainAxisSize.max : MainAxisSize.min, children: [
                Flexible(child: Text(o.displayTitle, overflow: TextOverflow.ellipsis)),
                if (o.isPlugin) ...[
                  const SizedBox(width: 6),
                  const PluginBadge(),
                ],
              ]),
            ),
        ],
        onChanged: enabled && onChanged != null
            ? (String? v) {
                if (v != null && v != value) onChanged!(v);
              }
            : null,
      ),
    );
  }
}
