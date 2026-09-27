// Display formatting for the chart. Latin digits on purpose: prices and
// times are compared digit by digit with the MT5 terminal (Data Window),
// which always shows Latin digits. Dates are Gregorian.

import '../core/number_format.dart';

String _two(int v) => v.toString().padLeft(2, '0');

/// `2025.01.06 01:00` — MT5 Data Window style, in the time's own zone
/// (pass a UTC value for UTC, `.toLocal()` for local time).
String formatMt5Time(DateTime t) => '${t.year}.${_two(t.month)}.${_two(t.day)} ${_two(t.hour)}:${_two(t.minute)}';

/// `2025-01-06` (Gregorian, UTC date of a UTC value).
String formatDate(DateTime t) => '${t.year}-${_two(t.month)}-${_two(t.day)}';

/// `2025-01-06 01:00 UTC`.
String formatUtc(DateTime t) => '${formatMt5Time(t.toUtc())} UTC';

/// Local wall-clock time of a UTC value, `2025.01.06 04:30`.
String formatLocal(DateTime t) => formatMt5Time(t.toLocal());

/// Time-axis label: date only when labels are a day or more apart.
String formatAxisTime(DateTime t, {required bool dateOnly}) =>
    dateOnly ? formatDate(t) : '${_two(t.month)}-${_two(t.day)} ${_two(t.hour)}:${_two(t.minute)}';

/// An engine price with the symbol's [digits]. Unknown digits -> the
/// shortest exact representation (never invents or drops precision).
String formatChartPrice(double? price, int? digits) {
  if (price == null) return '—';
  if (digits == null) return price.toString();
  return formatPrice(price, digits);
}

/// A plain engine number (volume, ATR, slope) with fixed decimals.
String formatValue(double? value, int decimals) => value == null ? '—' : value.toStringAsFixed(decimals);
