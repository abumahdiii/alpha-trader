import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/dev_mode.dart';
import '../../models/signal_models.dart';
import '../../providers/shell_navigation.dart';
import '../../providers/signals_provider.dart';
import '../../theme/app_semantic_colors.dart';
import '../../widgets/top_notice_stack.dart';
import 'signal_format.dart';

/// How long the in-app notice of a new signal stays (paused while hovered).
const Duration kSignalNoticeDuration = Duration(seconds: 10);

/// Shows an in-app top notice for every NEW live signal of the app-level
/// [SignalsProvider] (the sound and the Windows notification are the
/// `SignalAlertDispatcher`'s job). Wraps the shell, below the Navigator's
/// overlay.
class SignalNoticeListener extends StatefulWidget {
  const SignalNoticeListener({super.key, required this.child});

  final Widget child;

  @override
  State<SignalNoticeListener> createState() => _SignalNoticeListenerState();
}

class _SignalNoticeListenerState extends State<SignalNoticeListener> {
  SignalsProvider? _provider;
  StreamSubscription<LiveSignal>? _sub;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final SignalsProvider p = context.read<SignalsProvider>();
    if (identical(p, _provider)) return;
    unawaited(_sub?.cancel());
    _provider = p;
    _sub = p.newSignals.listen(_show);
  }

  void _show(LiveSignal s) {
    if (!mounted) return;
    devLog('[SignalNotice] in-app notice for #${s.id} ${s.symbol}');
    showTopSnackBar(
      Overlay.of(context),
      SignalNotice(
        signal: s,
        digits: _provider?.digitsOf(s.symbol),
        onOpen: () => context.read<ShellNavigation>().select(ShellPage.signal),
      ),
      displayDuration: kSignalNoticeDuration,
    );
  }

  @override
  void dispose() {
    unawaited(_sub?.cancel());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}

/// The notice body: symbol, side, indicative entry and SL, and a button
/// that opens the signal page (never an order action).
class SignalNotice extends StatelessWidget {
  const SignalNotice({super.key, required this.signal, required this.onOpen, this.digits});

  final LiveSignal signal;
  final int? digits;
  final VoidCallback onOpen;

  static const String openLabel = 'مشاهده سیگنال';

  @override
  Widget build(BuildContext context) {
    final AppSemanticColors c = context.appColors;
    final Color side = directionColor(context, signal.direction);
    return Center(
      child: Container(
        key: ValueKey<String>('signal-notice-${signal.id}'),
        width: 620,
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        decoration: BoxDecoration(
          color: Theme.of(context).colorScheme.surfaceContainerHigh,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: side, width: 1.5),
          boxShadow: const [BoxShadow(blurRadius: 12, color: Color(0x33000000))],
        ),
        child: Row(children: [
          Icon(Icons.notifications_active, color: side, size: 30),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
              Text(
                'سیگنال جدید: ${signal.symbol} — ${signal.direction?.titleFa ?? '—'}',
                style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16, color: side),
              ),
              Text(
                'ورود تقریبی ${signalPrice(signal.indicativeEntry, digits)} | حد ضرر ${signalPrice(signal.stopLoss, digits)}'
                '${signal.setupTitleFa != null ? ' — ${signal.setupTitleFa}' : ''}',
              ),
              Text(kSuggestionOnlyFa, style: TextStyle(fontSize: 12, color: c.mutedText)),
            ]),
          ),
          TextButton(onPressed: onOpen, child: const Text(openLabel)),
        ]),
      ),
    );
  }
}
