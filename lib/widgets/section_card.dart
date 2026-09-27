import 'package:flutter/material.dart';

/// A titled card section of a settings-like page.
class SectionCard extends StatelessWidget {
  const SectionCard({
    super.key,
    required this.title,
    required this.child,
    this.subtitle,
    this.trailing,
  });

  final String title;
  final String? subtitle;

  /// Shown at the end of the title row (e.g. version chips).
  final Widget? trailing;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final TextTheme text = Theme.of(context).textTheme;
    return Card(
      margin: const EdgeInsets.only(bottom: 16),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(title, style: text.titleMedium),
                      if (subtitle != null) ...[
                        const SizedBox(height: 4),
                        Text(subtitle!, style: text.bodySmall),
                      ],
                    ],
                  ),
                ),
                if (trailing != null) trailing!,
              ],
            ),
            const SizedBox(height: 16),
            child,
          ],
        ),
      ),
    );
  }
}

/// Persian validation messages under a field, in the error color.
class FieldErrors extends StatelessWidget {
  const FieldErrors(this.messages, {super.key});

  final List<String> messages;

  @override
  Widget build(BuildContext context) {
    if (messages.isEmpty) return const SizedBox.shrink();
    final Color color = Theme.of(context).colorScheme.error;
    return Padding(
      padding: const EdgeInsetsDirectional.only(top: 6, start: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final String m in messages)
            Text(m, style: Theme.of(context).textTheme.bodySmall?.copyWith(color: color)),
        ],
      ),
    );
  }
}

/// A block of general (not field-specific) error messages with an icon.
class ErrorBanner extends StatelessWidget {
  const ErrorBanner({super.key, required this.title, this.messages = const []});

  final String title;
  final List<String> messages;

  @override
  Widget build(BuildContext context) {
    final ColorScheme scheme = Theme.of(context).colorScheme;
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: scheme.errorContainer,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.error_outline, color: scheme.onErrorContainer),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: TextStyle(color: scheme.onErrorContainer, fontWeight: FontWeight.bold)),
                for (final String m in messages) Text(m, style: TextStyle(color: scheme.onErrorContainer)),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
