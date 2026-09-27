import 'package:flutter/material.dart';

import '../theme/app_semantic_colors.dart';
import 'top_notice_stack.dart';

/// Green "done" toast in the theme's success colors.
void showSuccessToast(BuildContext context, {required String title, String subtitle = ''}) {
  final AppSemanticColors colors = context.appColors;
  showCustomToast(
    context: context,
    title: title,
    subtitle: subtitle,
    backgroundColor: colors.successContainer,
    foregroundColor: colors.onSuccessContainer,
    duration: const Duration(seconds: 3),
  );
}

/// Red "failed" toast in the theme's error colors.
void showErrorToast(BuildContext context, {required String title, String subtitle = ''}) {
  final AppSemanticColors colors = context.appColors;
  showCustomToast(
    context: context,
    title: title,
    subtitle: subtitle,
    backgroundColor: colors.error,
    foregroundColor: colors.onError,
    icon: Icons.error_outline,
    duration: const Duration(seconds: 4),
  );
}

Future showCustomToast({
  required BuildContext context,
  required String title,
  required String subtitle,
  Color foregroundColor = Colors.white,
  Color backgroundColor = Colors.lightGreen,
  IconData icon = Icons.check_circle_outline,
  double iconSize = 30,
  Duration duration = const Duration(seconds: 2),
  double width = 600 ,
}) async {
  // Shown in the shared top-notice stack so it never overlaps other notices.
  showTopSnackBar(
    Overlay.of(context),
    Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Container(
          width: width,
          padding: const EdgeInsets.symmetric(vertical: 10),
          margin: const EdgeInsets.symmetric(horizontal: 10),
          decoration: BoxDecoration(
            color: backgroundColor,
            borderRadius: BorderRadius.circular(20),
          ),
          child: ListTile(
            leading: Icon(
            icon,
              size: iconSize,
              color: foregroundColor,
            ),
            title: Text(
              title,
              style: TextStyle(
                fontSize: 18,
                fontWeight: FontWeight.bold,
                color: foregroundColor,
              ),
            ),
            subtitle: Text(
              subtitle,
              style: TextStyle(fontSize: 14, color: foregroundColor),
            ),
          ),
        ),
      ],
    ),
    displayDuration: duration,
  );
}
