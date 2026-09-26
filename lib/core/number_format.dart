import 'package:flutter/services.dart';
import 'package:intl/intl.dart';

const enToFaNumberMap = {
  '0': '۰',
  '1': '۱',
  '2': '۲',
  '3': '۳',
  '4': '۴',
  '5': '۵',
  '6': '۶',
  '7': '۷',
  '8': '۸',
  '9': '۹',
};

const faToEnNumberMap = {
  '۰': '0',
  '۱': '1',
  '۲': '2',
  '۳': '3',
  '۴': '4',
  '۵': '5',
  '۶': '6',
  '۷': '7',
  '۸': '8',
  '۹': '9',
};

/// Latin digits → Persian digits, for labels in running Persian text.
/// Keep prices in tables/charts as Latin digits: traders read them against
/// the MT5 terminal, which always shows Latin digits.
String numberFa(String str) {
  enToFaNumberMap.forEach((key, value) {
    str = str.replaceAll(key, value);
  });
  return str;
}

/// Persian digits → Latin digits, for parsing user input.
String numberEn(String str) {
  faToEnNumberMap.forEach((key, value) {
    str = str.replaceAll(key, value);
  });
  return str;
}

/// Thousands-grouped number with a fixed number of decimals:
///   `formatNumber(1250000)` → `1,250,000`
///   `formatNumber(10432.5, decimals: 2)` → `10,432.50`
String formatNumber(num value, {int decimals = 0}) {
  final String pattern = decimals > 0 ? '#,##0.${'0' * decimals}' : '#,##0';
  return NumberFormat(pattern, 'en_US').format(value);
}

/// A price with the symbol's own precision (MT5 `symbol_info.digits`), so
/// EURUSD shows `1.08452` and XAUUSD shows `2345.67`. No thousands grouping —
/// prices are compared digit by digit against the terminal.
String formatPrice(double price, int digits) => price.toStringAsFixed(digits);

/// A ratio as a signed percentage: `0.1234` → `+12.34%`, `-0.05` → `-5.00%`.
/// Zero is shown unsigned.
String formatPercent(double ratio, {int decimals = 2}) {
  final double pct = ratio * 100;
  final String text = '${pct.abs().toStringAsFixed(decimals)}%';
  if (pct > 0) return '+$text';
  if (pct < 0) return '-$text';
  return text;
}

/// Live-formats a whole-number [TextFormField] with thousands separators as
/// the user types (e.g. "1,250,000"), accepting both English and Persian
/// digits. Strip the commas (`.replaceAll(',', '')`) before parsing the
/// field's text back to an [int].
class ThousandsSeparatorInputFormatter extends TextInputFormatter {
  ThousandsSeparatorInputFormatter({this.maxDigits});

  /// Caps how many digits are accepted, applied before formatting so a
  /// pasted/typed overflow is truncated instead of rejected outright.
  final int? maxDigits;

  @override
  TextEditingValue formatEditUpdate(
    TextEditingValue oldValue,
    TextEditingValue newValue,
  ) {
    String digitsOnly = numberEn(newValue.text).replaceAll(RegExp('[^0-9]'), '');
    if (maxDigits != null && digitsOnly.length > maxDigits!) {
      digitsOnly = digitsOnly.substring(0, maxDigits);
    }
    if (digitsOnly.isEmpty) {
      return newValue.copyWith(text: '');
    }
    final String formatted = formatNumber(int.parse(digitsOnly));
    return TextEditingValue(
      text: formatted,
      selection: TextSelection.collapsed(offset: formatted.length),
    );
  }
}
