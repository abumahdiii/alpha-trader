// Shared "disable while busy + show spinner" pattern for buttons that start
// work (save a strategy, run a backtest), so a double click never submits twice.

import 'package:flutter/material.dart';

import '../core/dev_mode.dart';

/// Mixin for [State] classes with a button triggering a critical async write.
mixin AsyncActionGuard<T extends StatefulWidget> on State<T> {
  bool _isAsyncActionRunning = false;

  bool get isAsyncActionRunning => _isAsyncActionRunning;

  /// Runs [action] once; further calls are dropped until it settles.
  Future<void> runGuardedAsyncAction(
    Future<void> Function() action, {
    void Function(Object error, StackTrace stackTrace)? onError,
  }) async {
    if (_isAsyncActionRunning) {
      // Checked synchronously ahead of any `await`, so a rapid double-tap can't race in.
      if (kDevMode) {
        devLog('AsyncActionGuard: dropped re-entrant tap, action already running');
      }
      return;
    }

    _isAsyncActionRunning = true;
    if (mounted) setState(() {});
    if (kDevMode) devLog('AsyncActionGuard: guarded action started');

    try {
      await action();
      if (kDevMode) devLog('AsyncActionGuard: guarded action completed');
    } catch (error, stackTrace) {
      if (kDevMode) {
        devLog('AsyncActionGuard: guarded action threw: $error');
      }
      if (onError != null) {
        onError(error, stackTrace);
      } else {
        rethrow;
      }
    } finally {
      _isAsyncActionRunning = false;
      if (mounted) setState(() {});
    }
  }
}

/// Drop-in widget wrapping [AsyncActionGuard] for screens that don't mix it in directly.
class LoadingActionButton extends StatefulWidget {
  const LoadingActionButton({
    super.key,
    required this.onPressed,
    required this.builder,
    this.onError,
  });

  final Future<void> Function() onPressed;

  /// `onPressed` is `null` while [isLoading] is true.
  final Widget Function(
    BuildContext context,
    VoidCallback? onPressed,
    bool isLoading,
  ) builder;

  final void Function(Object error, StackTrace stackTrace)? onError;

  @override
  State<LoadingActionButton> createState() => _LoadingActionButtonState();
}

class _LoadingActionButtonState extends State<LoadingActionButton>
    with AsyncActionGuard<LoadingActionButton> {
  void _handleTap() {
    runGuardedAsyncAction(widget.onPressed, onError: widget.onError);
  }

  @override
  Widget build(BuildContext context) {
    return widget.builder(
      context,
      isAsyncActionRunning ? null : _handleTap,
      isAsyncActionRunning,
    );
  }
}

/// [ElevatedButton] whose label turns into a small spinner while busy.
class LoadingElevatedButton extends StatelessWidget {
  const LoadingElevatedButton({
    super.key,
    required this.onPressed,
    required this.child,
    this.onError,
    this.style,
  });

  final Future<void> Function() onPressed;
  final Widget child;

  final void Function(Object error, StackTrace stackTrace)? onError;
  final ButtonStyle? style;

  @override
  Widget build(BuildContext context) {
    return LoadingActionButton(
      onPressed: onPressed,
      onError: onError,
      builder: (context, onPressed, isLoading) {
        return ElevatedButton(
          onPressed: onPressed,
          style: style,
          child: isLoading
              ? SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    valueColor: AlwaysStoppedAnimation<Color>(
                      style?.foregroundColor?.resolve(<WidgetState>{}) ??
                          Theme.of(context).colorScheme.onPrimary,
                    ),
                  ),
                )
              : child,
        );
      },
    );
  }
}
