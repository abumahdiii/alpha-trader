import 'package:flutter/foundation.dart';

import 'json_reader.dart';

/// Account / risk settings used by the engine to size suggestions.
///
/// Mirrors the engine's `AccountSettings` pydantic model
/// (`engine/alpha_engine/storage/account_settings.py`), served by
/// `GET /settings` and returned by `PUT /settings`:
/// `{balance: float, risk_pct: float, leverage: int, rr: float}`.
///
/// The UI only edits and displays these values; every derived quantity
/// (risk amount, volume, take-profit) is computed by the engine.
@immutable
class AccountSettings {
  const AccountSettings({
    required this.balance,
    required this.riskPct,
    required this.leverage,
    required this.rr,
  });

  /// Account balance in the account currency (USD).
  final double balance;

  /// Percent of the balance risked per trade (1.0 = 1 %).
  final double riskPct;

  /// Leverage as the `N` of `1:N`.
  final int leverage;

  /// Reward-to-risk ratio (take-profit distance / stop distance).
  final double rr;

  /// JSON keys, also used to address per-field validation messages.
  static const String balanceKey = 'balance';
  static const String riskPctKey = 'risk_pct';
  static const String leverageKey = 'leverage';
  static const String rrKey = 'rr';

  /// Persian field labels exactly as the engine writes them inside `«…»` in
  /// its 422 `errors_fa` (`FIELD_LABELS_FA` in account_settings.py), so a
  /// message can be attached to the field it is about.
  static const Map<String, String> engineLabelsFa = {
    balanceKey: 'موجودی حساب',
    riskPctKey: 'درصد ریسک',
    leverageKey: 'اهرم',
    rrKey: 'نسبت ریسک به ریوارد (R:R)',
  };

  factory AccountSettings.fromJson(Object? json) {
    final JsonReader r = JsonReader(json, 'settings');
    return AccountSettings(
      balance: r.number(balanceKey),
      riskPct: r.number(riskPctKey),
      leverage: r.integer(leverageKey),
      rr: r.number(rrKey),
    );
  }

  Map<String, Object> toJson() => {
        balanceKey: balance,
        riskPctKey: riskPct,
        leverageKey: leverage,
        rrKey: rr,
      };

  /// Only the fields that differ from [base] -- the body of a partial
  /// `PUT /settings`.
  Map<String, Object> changesFrom(AccountSettings base) {
    final Map<String, Object> mine = toJson();
    final Map<String, Object> theirs = base.toJson();
    return {
      for (final MapEntry<String, Object> e in mine.entries)
        if (theirs[e.key] != e.value) e.key: e.value,
    };
  }

  AccountSettings copyWith({double? balance, double? riskPct, int? leverage, double? rr}) => AccountSettings(
        balance: balance ?? this.balance,
        riskPct: riskPct ?? this.riskPct,
        leverage: leverage ?? this.leverage,
        rr: rr ?? this.rr,
      );

  @override
  bool operator ==(Object other) =>
      other is AccountSettings &&
      other.balance == balance &&
      other.riskPct == riskPct &&
      other.leverage == leverage &&
      other.rr == rr;

  @override
  int get hashCode => Object.hash(balance, riskPct, leverage, rr);

  @override
  String toString() => 'AccountSettings(balance=$balance, risk_pct=$riskPct, leverage=$leverage, rr=$rr)';
}
