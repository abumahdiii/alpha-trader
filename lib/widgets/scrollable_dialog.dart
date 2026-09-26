import 'package:flutter/material.dart';

import '../core/dev_mode.dart';

/// Responsive dialog shell replacing the manual `MediaQuery` height-offset
/// calculations duplicated across the app's dialogs (fixed offsets can go
/// negative and crash on small windows); sizes to content up to a fraction
/// of available screen height, then scrolls instead of overflowing.
class ScrollableDialog extends StatelessWidget {
  const ScrollableDialog({
    super.key,
    this.title,
    required this.content,
    this.actions,
    this.width,
    this.maxWidth = 560,
    this.maxHeightFraction = 0.85,
    this.minHeight = 120,
    this.contentPadding = const EdgeInsets.fromLTRB(24, 12, 24, 8),
    this.titlePadding = const EdgeInsets.fromLTRB(24, 20, 16, 4),
    this.actionsPadding = const EdgeInsets.fromLTRB(16, 4, 16, 16),
    this.insetPadding = const EdgeInsets.symmetric(horizontal: 40, vertical: 24),
    this.shape,
    this.scrollController,
    this.showCloseButton = false,
    this.onClose,
  });

  final Widget? title;

  /// Automatically wrapped in a [SingleChildScrollView] — callers should
  /// NOT add their own outer scroll view / manual height calculation.
  final Widget content;

  final List<Widget>? actions;

  /// Desired width, capped to the current screen width; falls back to [maxWidth].
  final double? width;

  final double maxWidth;

  /// Fraction of available screen height the dialog may grow to before scrolling.
  final double maxHeightFraction;

  /// Floor on the height constraint so it can never go zero/negative.
  final double minHeight;

  final EdgeInsetsGeometry contentPadding;
  final EdgeInsetsGeometry titlePadding;
  final EdgeInsetsGeometry actionsPadding;
  final EdgeInsets insetPadding;

  final ShapeBorder? shape;
  final ScrollController? scrollController;

  /// Renders a trailing close [IconButton] next to [title] when true.
  final bool showCloseButton;

  /// Defaults to `Navigator.of(context).maybePop()`.
  final VoidCallback? onClose;

  /// Opens a [ScrollableDialog] via [showDialog].
  static Future<T?> show<T>({
    required BuildContext context,
    Widget? title,
    required Widget content,
    List<Widget>? actions,
    double? width,
    double maxWidth = 560,
    double maxHeightFraction = 0.85,
    double minHeight = 120,
    bool barrierDismissible = true,
    Color? barrierColor,
    bool showCloseButton = false,
    VoidCallback? onClose,
    ShapeBorder? shape,
    ScrollController? scrollController,
  }) {
    devLog(
      'ScrollableDialog.show(): opening dialog '
      '(width=$width, maxWidth=$maxWidth, maxHeightFraction=$maxHeightFraction, '
      'showCloseButton=$showCloseButton)',
    );
    return showDialog<T>(
      context: context,
      barrierDismissible: barrierDismissible,
      barrierColor: barrierColor,
      builder: (dialogContext) => ScrollableDialog(
        title: title,
        content: content,
        actions: actions,
        width: width,
        maxWidth: maxWidth,
        maxHeightFraction: maxHeightFraction,
        minHeight: minHeight,
        showCloseButton: showCloseButton,
        onClose: onClose,
        shape: shape,
        scrollController: scrollController,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final media = MediaQuery.of(context);

    // Re-read on every build so a live window resize reflows correctly.
    final double availableHeight =
        media.size.height - media.viewInsets.bottom - media.padding.vertical;
    final double heightUpperBound =
        availableHeight > minHeight ? availableHeight : minHeight;
    final double effectiveMaxHeight =
        (availableHeight * maxHeightFraction).clamp(minHeight, heightUpperBound).toDouble();

    final double horizontalInset =
        insetPadding.horizontal + 16; // small breathing room from edges
    final double widthUpperBound =
        media.size.width - horizontalInset > 0 ? media.size.width - horizontalInset : media.size.width;
    final double effectiveMaxWidth = (width ?? maxWidth).clamp(0.0, widthUpperBound).toDouble();

    Widget? titleWidget = title;
    if (titleWidget != null) {
      titleWidget = DefaultTextStyle(
        style: theme.textTheme.titleLarge ??
            const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
        child: titleWidget,
      );
      if (showCloseButton) {
        titleWidget = Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Expanded(child: titleWidget),
            IconButton(
              tooltip: 'بستن',
              onPressed: onClose ?? () => Navigator.of(context).maybePop(),
              icon: const Icon(Icons.close),
            ),
          ],
        );
      }
    }

    return Dialog(
      insetPadding: insetPadding,
      shape: shape ??
          const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(20.0)),
          ),
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: effectiveMaxWidth,
          maxHeight: effectiveMaxHeight,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (titleWidget != null) Padding(padding: titlePadding, child: titleWidget),
            Flexible(
              child: SingleChildScrollView(
                controller: scrollController,
                padding: contentPadding,
                child: content,
              ),
            ),
            if (actions != null && actions!.isNotEmpty)
              Padding(
                padding: actionsPadding,
                child: Wrap(
                  alignment: WrapAlignment.end,
                  spacing: 8,
                  runSpacing: 8,
                  children: actions!,
                ),
              ),
          ],
        ),
      ),
    );
  }
}
