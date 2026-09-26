import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

class ThemeProvider extends ChangeNotifier {
  late ThemeMode themeMode;

  ThemeProvider({ThemeMode initialThemeMode = ThemeMode.system}) {
    themeMode = initialThemeMode;
    readTheme();
  }

  Future<String> readTheme() async {
    final prefs = await SharedPreferences.getInstance();

    final String theme = prefs.getString('theme') ?? 'light';

    if (theme == 'system') {
      toggleTheme(ThemeMode.system);
    } else if (theme == 'light') {
      toggleTheme(ThemeMode.light);
    } else if (theme == 'dark') {
      toggleTheme(ThemeMode.dark);
    }

    return theme;
  }

  bool isDark() {
    return themeMode == ThemeMode.dark;
  }

  void toggleTheme(ThemeMode theme) async {
    themeMode = theme;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    String themeStr = 'light';
    if (theme == ThemeMode.dark) themeStr = 'dark';
    if (theme == ThemeMode.system) themeStr = 'system';
    await prefs.setString('theme', themeStr);
  }
}
