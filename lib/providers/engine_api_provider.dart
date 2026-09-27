import 'package:flutter/foundation.dart';

import '../core/app_logger.dart';
import '../core/dev_mode.dart';
import '../services/engine_api.dart';
import 'engine_status_provider.dart';

/// Hands pages an [EngineApi] while (and only while) the engine is running.
///
/// Follows [EngineStatusProvider]: [api] is non-null exactly in
/// [EngineState.running], and a NEW instance is created every time the
/// engine (re)enters running -- after a restart, a crash recovery or a port
/// change. Pages key their content on the instance (see `EngineGate`), so
/// they reload by themselves when the engine comes back.
class EngineApiProvider extends ChangeNotifier {
  EngineApiProvider({required EngineStatusProvider engine, EngineApiFactory? apiFactory})
      : _engine = engine,
        _factory = apiFactory ?? ((int port) => EngineApi(port: port)) {
    _engine.addListener(_sync);
    _sync();
  }

  final EngineStatusProvider _engine;
  final EngineApiFactory _factory;
  EngineApi? _api;
  bool _disposed = false;

  /// Null unless the engine is running.
  EngineApi? get api => _api;

  void _sync() {
    if (_disposed) return;
    final int? port = _engine.state == EngineState.running ? _engine.port : null;
    if (port == _api?.port) return; // same session (health polls notify often)
    final EngineApi? old = _api;
    _api = port == null ? null : _factory(port);
    old?.close();
    _log(port == null
        ? '[EngineApiProvider] engine ${_engine.state.name}: API unavailable'
        : '[EngineApiProvider] engine running on :$port: API ready');
    notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _engine.removeListener(_sync);
    _api?.close();
    _api = null;
    super.dispose();
  }

  static void _log(String message) {
    if (!kDevMode) return;
    devLog(message);
    AppLogger.log(message);
  }
}
