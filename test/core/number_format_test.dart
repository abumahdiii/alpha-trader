import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/core/duration_format.dart';
import 'package:alpha_trader/core/number_format.dart';

void main() {
  group('digits', () {
    test('numberFa / numberEn round-trip', () {
      expect(numberFa('1.08452'), '۱.۰۸۴۵۲');
      expect(numberEn('۱۲۳۴'), '1234');
      expect(numberEn(numberFa('9876543210')), '9876543210');
    });
  });

  group('formatNumber', () {
    test('groups thousands', () {
      expect(formatNumber(1250000), '1,250,000');
      expect(formatNumber(0), '0');
    });
    test('keeps fixed decimals', () {
      expect(formatNumber(10432.5, decimals: 2), '10,432.50');
      expect(formatNumber(-1234.567, decimals: 2), '-1,234.57');
    });
  });

  group('formatPrice', () {
    test('uses the symbol digits', () {
      expect(formatPrice(1.084523, 5), '1.08452');
      expect(formatPrice(2345.6, 2), '2345.60');
    });
  });

  group('formatPercent', () {
    test('signs non-zero values', () {
      expect(formatPercent(0.1234), '+12.34%');
      expect(formatPercent(-0.05), '-5.00%');
      expect(formatPercent(0), '0.00%');
    });
  });

  group('duration', () {
    test('formatMinutesHm', () {
      expect(formatMinutesHm(0), '0:00');
      expect(formatMinutesHm(61), '1:01');
      expect(formatMinutesHm(-80), '-1:20');
    });
    test('formatMinutesFa', () {
      expect(formatMinutesFa(0), '0 دقیقه');
      expect(formatMinutesFa(120), '2 ساعت');
      expect(formatMinutesFa(61), '1 ساعت و 1 دقیقه');
    });
  });
}
