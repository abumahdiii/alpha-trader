import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/dev_mode.dart';
import '../providers/engine_api_provider.dart';
import '../providers/engine_status_provider.dart';
import '../services/engine_api.dart';
import 'engine_status_indicator.dart';
import 'status_message.dart';

/// Shows [builder]'s content only while the engine is running, else a
/// Persian "engine is not running" state.
///
/// The content is keyed on the [EngineApi] instance, which
/// [EngineApiProvider] replaces every time the engine (re)enters running:
/// the content's State is created afresh then, so a page that loads in
/// `initState` reloads by itself after a start, restart or recovery.
class EngineGate extends StatelessWidget {
  const EngineGate({super.key, required this.builder});

  final Widget Function(BuildContext context, EngineApi api) builder;

  @override
  Widget build(BuildContext context) {
    final EngineApi? api = context.watch<EngineApiProvider>().api;
    if (api != null) {
      return KeyedSubtree(key: ObjectKey(api), child: builder(context, api));
    }
    return const EngineUnavailableView();
  }
}

/// Empty state for a page that needs the engine while it is not running.
class EngineUnavailableView extends StatelessWidget {
  const EngineUnavailableView({super.key});

  static const String title = 'موتور در حال اجرا نیست';

  @override
  Widget build(BuildContext context) {
    final EngineStatusProvider engine = context.watch<EngineStatusProvider>();
    const String reload = 'این صفحه به‌محض فعال شدن موتور، خودکار بارگذاری می‌شود.';
    return switch (engine.state) {
      EngineState.starting => const StatusMessage(
          icon: Icons.hourglass_top,
          title: 'موتور در حال راه‌اندازی است…',
          message: reload,
          action: SizedBox.square(dimension: 24, child: CircularProgressIndicator(strokeWidth: 2)),
        ),
      EngineState.stopped => StatusMessage(
          icon: Icons.power_settings_new,
          title: title,
          message: 'وضعیت: متوقف. $reload',
          action: _restartButton(engine),
        ),
      // running is only reachable for a frame before EngineApiProvider syncs.
      EngineState.running => const StatusMessage(icon: Icons.hourglass_top, title: 'در حال اتصال به موتور…'),
      EngineState.error => StatusMessage(
          icon: Icons.error_outline,
          isError: true,
          title: title,
          message: [
            'وضعیت: خطا.',
            if (engine.errorMessage != null) engine.errorMessage!,
            if (engine.awaitingRecovery)
              'بررسی سلامت موتور ادامه دارد؛ اگر پاسخ بدهد، این صفحه خودکار بارگذاری می‌شود.',
          ].join('\n'),
          action: _restartButton(engine),
        ),
    };
  }

  static Widget _restartButton(EngineStatusProvider engine) => OutlinedButton.icon(
        onPressed: () {
          devLog('[EngineGate] restart requested from empty state (state=${engine.state.name})');
          unawaited(engine.restart());
        },
        icon: const Icon(Icons.restart_alt),
        label: const Text(EngineStatusIndicator.restartLabel),
      );
}
