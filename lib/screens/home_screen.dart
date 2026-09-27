import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../theme/theme_provider.dart';
import '../widgets/engine_status_indicator.dart';

/// Placeholder home screen for the setup step. Replaced by the real shell
/// (strategies / backtests / live signals) in the first UI phase.
class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final themeProvider = context.watch<ThemeProvider>();
    final textTheme = Theme.of(context).textTheme;

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
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.candlestick_chart, size: 72),
            const SizedBox(height: 16),
            Text('به Alpha Trader خوش آمدید', style: textTheme.headlineSmall),
            const SizedBox(height: 8),
            Text(
              'تعریف سیستم معاملاتی، بک‌تست و پیشنهاد پوزیشن — به‌زودی',
              style: textTheme.bodyMedium,
            ),
          ],
        ),
      ),
    );
  }
}
