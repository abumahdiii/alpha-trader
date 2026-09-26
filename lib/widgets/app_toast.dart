import 'package:flutter/material.dart';

import 'top_notice_stack.dart';

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
