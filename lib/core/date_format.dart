/// Gregorian date-time formatting. The engine sends UTC; the UI shows local
/// time unless a label says otherwise.
library;

String _two(int v) => v.toString().padLeft(2, '0');

/// `2026-09-27 21:30:05` in the user's local time zone (Gregorian).
String formatLocalDateTime(DateTime time, {bool seconds = true}) {
  final DateTime t = time.toLocal();
  final String hm = '${_two(t.hour)}:${_two(t.minute)}';
  return '${t.year}-${_two(t.month)}-${_two(t.day)} ${seconds ? '$hm:${_two(t.second)}' : hm}';
}
