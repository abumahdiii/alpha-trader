/// Two renderings of a minute count live here; pick by where the text lands:
///
///   * [formatMinutesFa] — words (`90` → `1 ساعت و 30 دقیقه`). Use it in
///     prose: sentences, notices, confirmation dialogs.
///   * [formatMinutesHm] — numeric `h:mm` (`90` → `1:30`). Use it in table
///     columns (e.g. trade holding time) and anywhere a fixed-width,
///     scannable value beats a readable one.
///
/// [formatMinutesFa] always shows the real value:
///   0 → `0 دقیقه`, 1 → `1 دقیقه`, 61 → `1 ساعت و 1 دقیقه`, 120 → `2 ساعت`.
/// The sign is dropped (callers that can go negative add their own marker).
String formatMinutesFa(int minutes) {
  final int abs = minutes.abs();
  final int h = abs ~/ 60;
  final int m = abs % 60;
  if (h == 0 && m == 0) return '0 دقیقه';
  return [
    if (h > 0) '$h ساعت',
    if (m > 0) '$m دقیقه',
  ].join(' و ');
}

/// The same minute count as a numeric `h:mm`, minutes always zero-padded to
/// two digits so a column of them stays aligned:
///   0 → `0:00`, 61 → `1:01`, 90 → `1:30`, 600 → `10:00`.
/// Unlike [formatMinutesFa] the sign is kept, as a leading `-`
/// (`-80` → `-1:20`).
String formatMinutesHm(int minutes) {
  final int abs = minutes.abs();
  final String text = '${abs ~/ 60}:${(abs % 60).toString().padLeft(2, '0')}';
  return minutes < 0 ? '-$text' : text;
}
