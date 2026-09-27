import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/dev_mode.dart';
import '../theme/app_semantic_colors.dart';
import '../theme/theme_provider.dart';
import '../widgets/engine_status_indicator.dart';
import 'chart/chart_screen.dart';
import 'settings/settings_screen.dart';
import 'systems/systems_screen.dart';

/// One entry of the navigation rail. [page] is null for a destination that
/// is not available yet (shown disabled with «به‌زودی»).
class ShellDestination {
  const ShellDestination({
    required this.label,
    required this.icon,
    required this.selectedIcon,
    this.page,
  });

  final String label;
  final IconData icon;
  final IconData selectedIcon;
  final Widget? page;

  bool get enabled => page != null;
}

/// App shell: top bar (title, engine/MT5 indicator, theme toggle) and a
/// NavigationRail, which sits on the right in the RTL layout.
///
/// Pages live in an [IndexedStack], so switching destinations keeps their
/// state (an unsaved form stays as it was).
class ShellScreen extends StatefulWidget {
  const ShellScreen({super.key});

  static const String comingSoon = 'به‌زودی';

  static const List<ShellDestination> destinations = [
    ShellDestination(
      label: 'چارت',
      icon: Icons.candlestick_chart_outlined,
      selectedIcon: Icons.candlestick_chart,
      page: ChartScreen(),
    ),
    ShellDestination(
      label: 'سیستم‌ها',
      icon: Icons.tune_outlined,
      selectedIcon: Icons.tune,
      page: SystemsScreen(),
    ),
    ShellDestination(
      label: 'بک‌تست',
      icon: Icons.history_outlined,
      selectedIcon: Icons.history,
    ),
    ShellDestination(
      label: 'سیگنال',
      icon: Icons.notifications_active_outlined,
      selectedIcon: Icons.notifications_active,
    ),
    ShellDestination(
      label: 'تنظیمات',
      icon: Icons.settings_outlined,
      selectedIcon: Icons.settings,
      page: SettingsScreen(),
    ),
  ];

  @override
  State<ShellScreen> createState() => _ShellScreenState();
}

class _ShellScreenState extends State<ShellScreen> {
  int _selected = 0;

  void _select(int index) {
    final ShellDestination target = ShellScreen.destinations[index];
    if (!target.enabled || index == _selected) return;
    devLog('[Shell] ${ShellScreen.destinations[_selected].label} -> ${target.label}');
    setState(() => _selected = index);
  }

  @override
  Widget build(BuildContext context) {
    final ThemeProvider themeProvider = context.watch<ThemeProvider>();
    final AppSemanticColors colors = context.appColors;
    final TextTheme text = Theme.of(context).textTheme;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Alpha Trader'),
        actions: [
          const EngineStatusIndicator(),
          IconButton(
            tooltip: themeProvider.isDark() ? 'تم روشن' : 'تم تاریک',
            icon: Icon(themeProvider.isDark() ? Icons.light_mode : Icons.dark_mode),
            onPressed: () => themeProvider.toggleTheme(
              themeProvider.isDark() ? ThemeMode.light : ThemeMode.dark,
            ),
          ),
        ],
      ),
      body: Row(
        children: [
          NavigationRail(
            selectedIndex: _selected,
            onDestinationSelected: _select,
            labelType: NavigationRailLabelType.all,
            destinations: [
              for (final ShellDestination d in ShellScreen.destinations)
                NavigationRailDestination(
                  icon: Icon(d.icon),
                  selectedIcon: Icon(d.selectedIcon),
                  disabled: !d.enabled,
                  label: d.enabled
                      ? Text(d.label)
                      : Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(d.label),
                            Text(
                              ShellScreen.comingSoon,
                              style: text.labelSmall?.copyWith(color: colors.mutedText),
                            ),
                          ],
                        ),
                ),
            ],
          ),
          const VerticalDivider(width: 1),
          Expanded(
            child: IndexedStack(
              index: _selected,
              children: [
                for (final ShellDestination d in ShellScreen.destinations) d.page ?? const SizedBox.shrink(),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
