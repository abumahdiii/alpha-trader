import 'package:flutter/material.dart';

import '../../widgets/status_message.dart';

/// Chart page placeholder; the candlestick chart (phase 3, group 3B)
/// replaces it.
class ChartScreen extends StatelessWidget {
  const ChartScreen({super.key});

  static const String underConstruction = 'چارت — در حال ساخت';

  @override
  Widget build(BuildContext context) => const StatusMessage(
        icon: Icons.candlestick_chart,
        title: underConstruction,
        message: 'چارت کندلی با خطوط کانال و ستاپ‌ها در همین فاز به این صفحه اضافه می‌شود.',
      );
}
