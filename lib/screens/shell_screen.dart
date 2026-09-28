import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/dev_mode.dart';
import '../providers/shell_navigation.dart';
import '../theme/app_semantic_colors.dart';
import '../theme/theme_provider.dart';
import '../widgets/engine_status_indicator.dart';
import 'backtest/backtest_screen.dart';
import 'chart/chart_screen.dart';
import 'settings/settings_screen.dart';
import 'systems/systems_screen.dart';

/// One entry of the navigation rail. [page] is null for a destination that
/// is not available yet (shown disabled with «به‌زودی»).
class ShellDestination {
  const ShellDestination({
    required this.id,
    required this.label,
    required this.icon,
    required this.selectedIcon,
    this.page,
  });

  final ShellPage id;
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
/// state (an unsaved form stays as it was). The shown page is
/// [ShellNavigation.page], so other pages can switch it too (e.g.
/// [ShellNavigation.openBacktest]).
///
/// Destinations are listed in [ShellPage] order.
class ShellScreen extends StatefulWidget {
  const ShellScreen({super.key});

  static const String comingSoon = 'به‌زودی';

  static const List<ShellDestination> destinations = [
    ShellDestination(
      id: ShellPage.chart,
      label: 'چارت',
      icon: Icons.candlestick_chart_outlined,
      selectedIcon: Icons.candlestick_chart,
      page: ChartScreen(),
    ),
    ShellDestination(
      id: ShellPage.systems,
      label: 'سیستم‌ها',
      icon: Icons.tune_outlined,
      selectedIcon: Icons.tune,
      page: SystemsScreen(),
    ),
    ShellDestination(
      id: ShellPage.backtest,
      label: 'بک‌تست',
      icon: Icons.history_outlined,
      selectedIcon: Icons.history,
      page: BacktestScreen(),
    ),
    ShellDestination(
      id: ShellPage.signal,
      label: 'سیگنال',
      icon: Icons.notifications_active_outlined,
      selectedIcon: Icons.notifications_active,
    ),
    ShellDestination(
      id: ShellPage.settings,
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
  void _select(ShellNavigation navigation, int index) {
    final ShellDestination target = ShellScreen.destinations[index];
    if (!target.enabled || target.id == navigation.page) return;
    devLog('[Shell] rail -> ${target.label}');
    navigation.select(target.id);
  }

  @override
  Widget build(BuildContext context) {
    final ShellNavigation navigation = context.watch<ShellNavigation>();
    final int selected = ShellScreen.destinations.indexWhere((ShellDestination d) => d.id == navigation.page);
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
            selectedIndex: selected < 0 ? 0 : selected,
            onDestinationSelected: (int i) => _select(navigation, i),
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
              index: selected < 0 ? 0 : selected,
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
