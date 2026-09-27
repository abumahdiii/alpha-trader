import 'package:flutter/material.dart';

import '../services/engine_api.dart';
import '../theme/app_semantic_colors.dart';

/// Centered icon + title + message (+ optional action) for empty, waiting
/// and error states of a page or a card.
class StatusMessage extends StatelessWidget {
  const StatusMessage({
    super.key,
    required this.icon,
    required this.title,
    this.message,
    this.action,
    this.isError = false,
  });

  /// A failed engine call, with a «تلاش دوباره» button when [onRetry] is set.
  factory StatusMessage.apiError({
    Key? key,
    required String title,
    required EngineApiException error,
    VoidCallback? onRetry,
  }) =>
      StatusMessage(
        key: key,
        icon: Icons.error_outline,
        title: title,
        message: error.userMessage,
        isError: true,
        action: onRetry == null
            ? null
            : OutlinedButton.icon(
                onPressed: onRetry,
                icon: const Icon(Icons.refresh),
                label: const Text('تلاش دوباره'),
              ),
      );

  final IconData icon;
  final String title;
  final String? message;
  final Widget? action;

  /// Tints the icon with the error color instead of the muted one.
  final bool isError;

  @override
  Widget build(BuildContext context) {
    final TextTheme text = Theme.of(context).textTheme;
    final AppSemanticColors colors = context.appColors;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 48, color: isError ? colors.error : colors.mutedText),
              const SizedBox(height: 12),
              Text(title, style: text.titleMedium, textAlign: TextAlign.center),
              if (message != null && message!.isNotEmpty) ...[
                const SizedBox(height: 8),
                Text(
                  message!,
                  style: text.bodyMedium?.copyWith(color: colors.mutedText),
                  textAlign: TextAlign.center,
                ),
              ],
              if (action != null) ...[const SizedBox(height: 16), action!],
            ],
          ),
        ),
      ),
    );
  }
}
