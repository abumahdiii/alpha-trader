// Parsing of the strategy-plugin payloads and of the provenance / non-channel
// fields the engine added for plugins (README "Strategy plugins", "Chart data").

import 'dart:convert';
import 'dart:typed_data';

import 'package:file_selector/file_selector.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:alpha_trader/models/backtest_models.dart';
import 'package:alpha_trader/models/chart_models.dart';
import 'package:alpha_trader/models/json_reader.dart';
import 'package:alpha_trader/models/strategy.dart';
import 'package:alpha_trader/models/strategy_plugin.dart';
import 'package:alpha_trader/screens/systems/plugin_dialogs.dart';
import 'package:alpha_trader/services/plugin_files.dart';

import '../helpers/backtest_fixtures.dart';
import '../helpers/engine_fakes.dart';
import '../helpers/plugin_fakes.dart';

/// A `/chart/setups` item of a plugin: slug type, own title, `line: null`.
Map<String, Object?> _pluginSetup({Object? title = 'کراس صعودی میانگین‌ها', Object? line}) => {
      'id': 'BRNUSD.x:20260812T1500Z:99887766aabb:ma_cross',
      'symbol': 'BRNUSD.x',
      'status': 'accepted',
      'rejection_reason_fa': null,
      'setup_type': 'ma_cross_up',
      'setup_title_fa': title,
      'direction': 'sell',
      'pattern': 'ma_cross',
      'line': line,
      'line_value': null,
      'channel_direction': null,
      'confirmation_bar_time': '2026-08-12T15:00:00Z',
      'decision_time': '2026-08-12T16:00:00Z',
      'entry_time': '2026-08-12T16:00:00Z',
      'entry': 87.43,
      'entry_bid_open': 87.43,
      'spread_at_entry_points': 3,
      'entry_spread_source': 'historical',
      'stop_loss': 88.6939,
      'take_profit': 84.902,
      'rr': 2.0,
      'risk_distance': 1.2639,
      'reference_price': 87.5,
      'indicative_take_profit': 84.97,
      'volume': 0.01,
      'actual_risk': 12.64,
      'margin': 8.74,
      'risk_amount': 25.0,
      'volume_note_fa': null,
      'sizing_warnings_fa': <String>[],
      'reason_fa': 'کراس نزولی میانگین‌ها.',
      'indicators': {'fast': 87.1, 'slow': 87.6},
    };

void main() {
  test('plugin item: identity, status, schema, validation report, registered', () {
    final StrategyPlugin p = StrategyPlugin.fromJson(pluginJson());
    expect(p.name, kPluginName);
    expect(p.version, 2);
    expect(p.status, PluginStatus.active);
    expect(p.shortSha256, '2d070ea08a45');
    expect(p.registered, isTrue);
    expect(p.paramSchema.map((s) => s.name), ['fast', 'slow']);
    expect(p.validation.staticChecks, hasLength(5));
    final PluginDynamicReport d = p.validation.dynamicReport!;
    expect(d.bars, 3000);
    expect(d.candidates, 11);
    expect(d.prefixChecks, 60);
    expect(d.determinism, isTrue);
    expect(d.futureMutation, isTrue);
    expect(d.elapsedS, 5.512);
    expect(d.peakMemoryMib, 281.4);
    expect(p.createdUtc, DateTime.utc(2026, 9, 28, 9, 15));
    expect(p.ref, (name: kPluginName, version: 2, sha256: kPluginSha));
  });

  test('plugin item tolerates dynamic: null, unknown status and a missing filename', () {
    final StrategyPlugin p = StrategyPlugin.fromJson({
      ...pluginJson(status: 'quarantined'),
      'validation': {'static': <String>[], 'dynamic': null},
      'filename': null,
    });
    expect(p.status, PluginStatus.unknown);
    expect(p.validation.dynamicReport, isNull);
    expect(p.filename, isNull);
    expect(StrategyPlugin.listFromJson([pluginJson(), pluginJson(version: 1, status: 'archived')]), hasLength(2));
    expect(() => StrategyPlugin.listFromJson({'x': 1}), throwsFormatException);
  });

  test('template', () {
    final PluginTemplate t = PluginTemplate.fromJson({'filename': kTemplateFilename, 'content': kTemplateContent});
    expect(t.filename, kTemplateFilename);
    expect(t.content, kTemplateContent);
  });

  test('setup item of a plugin: line null, slug type, setup_title_fa is the title', () {
    final SetupItem s = SetupItem.read(JsonReader(_pluginSetup(), 'setup'));
    expect(s.line, isNull);
    expect(s.setupType, 'ma_cross_up');
    expect(s.setupTitleFa, 'کراس صعودی میانگین‌ها');
    expect(s.typeTitleFa, 'کراس صعودی میانگین‌ها', reason: 'no channel line appended');
    expect(s.patternTitleFa, 'ma_cross', reason: 'unknown pattern shown as sent');
  });

  test('setup item: missing title falls back to the slug; unknown line is dropped, not fatal', () {
    final SetupItem a = SetupItem.read(JsonReader(_pluginSetup(title: null), 'setup'));
    expect(a.setupTitleFa, 'ma_cross_up');
    final SetupItem b = SetupItem.read(JsonReader(_pluginSetup(line: 'outer'), 'setup'));
    expect(b.line, isNull);
    final SetupItem c = SetupItem.read(JsonReader(_pluginSetup(line: 'lower', title: 'برگشت'), 'setup'));
    expect(c.line, ChannelLine.lower);
    expect(c.typeTitleFa, 'برگشت (خط پایین)');
    expect(ChannelLine.tryParse(null), isNull);
    expect(() => ChannelLine.parse('outer'), throwsFormatException);
  });

  test('chart provenance: strategy_source + strategy_sha256 (plugins), builtin when absent', () {
    final StrategyProvenance plugin = StrategyProvenance.read(JsonReader(jsonDecode('''{
      "strategy": "ma_cross", "strategy_version": 2, "strategy_source": "plugin",
      "strategy_sha256": "$kPluginSha", "params_version": 1, "params_hash": "99", "params": {"fast": 10}}'''), 'p'));
    expect(plugin.strategySource, StrategySource.plugin);
    expect(plugin.strategySha256, kPluginSha);
    final StrategyProvenance old = StrategyProvenance.read(JsonReader(jsonDecode('''{
      "strategy": "stddev_channel", "strategy_version": 1, "params_version": 1, "params_hash": "8e",
      "params": {}}'''), 'p'));
    expect(old.strategySource, StrategySource.builtin);
    expect(old.strategySha256, isNull);
  });

  test('backtest run summary / detail: strategy_source + strategy_sha256', () {
    final BacktestRunSummary s = BacktestRunSummary.fromJson({
      ...runSummaryJson(),
      'strategy': kPluginName,
      'strategy_version': 2,
      'strategy_source': 'plugin',
      'strategy_sha256': kPluginSha,
    });
    expect(s.strategySource, StrategySource.plugin);
    expect(s.strategySha256, kPluginSha);
    final BacktestRunSummary old = BacktestRunSummary.fromJson(runSummaryJson());
    expect(old.strategySource, StrategySource.builtin);
    expect(old.strategySha256, isNull);
    final BacktestRunDetail d =
        BacktestRunDetail.fromJson({...manualDetailJson(), 'strategy_source': 'plugin', 'strategy_sha256': kPluginSha});
    expect(d.summary.strategySha256, kPluginSha);
  });

  test('request body carries strategy + strategy_version only when given', () {
    final BacktestRequest r = BacktestRequest.manual(
      symbol: 'BRNUSD.x',
      from: DateTime.utc(2024, 1, 1),
      to: DateTime.utc(2024, 2, 1),
      strategy: kPluginName,
      strategyVersion: 2,
    );
    expect(r.toJson(), containsPair('strategy', kPluginName));
    expect(r.toJson(), containsPair('strategy_version', 2));
    final BacktestRequest plain = BacktestRequest.random(symbol: 'XAUUSD.x', windowsCount: 2, windowMonths: 1);
    expect(plain.toJson().containsKey('strategy'), isFalse);
  });

  test('strategy options: builtin first, registered plugins badged and sorted', () {
    final List<StrategyInfo> strategies = [
      StrategyInfo.fromJson(pluginStrategyJson()),
      StrategyInfo.fromJson(strategyJson()),
    ];
    final List<StrategyOption> options = StrategyOption.merge(strategies, [StrategyPlugin.fromJson(pluginJson()).ref]);
    expect(options.map((o) => o.name), ['stddev_channel', kPluginName]);
    expect(options.first.isPlugin, isFalse);
    expect(options.first.hasChannel, isTrue);
    expect(options.last.isPlugin, isTrue);
    expect(options.last.hasChannel, isFalse);
    expect(options.last.sha256, kPluginSha);
    expect(options.last.version, 2);
    expect(StrategyOption.merge(strategies, const []).every((o) => !o.isPlugin), isTrue);
  });

  test('plugin error lines: «خط N:» prefix split off', () {
    expect(splitPluginError('خط 12: import «os» مجاز نیست.'), (line: '12', text: 'import «os» مجاز نیست.'));
    expect(splitPluginError('خط ۷: خطای نحوی'), (line: '۷', text: 'خطای نحوی'));
    expect(splitPluginError('کلاس Strategy پیدا نشد.'), (line: null, text: 'کلاس Strategy پیدا نشد.'));
  });

  test('picked file: strict UTF-8, BOM dropped', () {
    expect(FileSelectorPluginFiles.decodePluginSource([0xEF, 0xBB, 0xBF, 0x61, 0x3D, 0x31]), 'a=1');
    expect(FileSelectorPluginFiles.decodePluginSource(utf8.encode('title_fa = "کراس"')), 'title_fa = "کراس"');
    expect(() => FileSelectorPluginFiles.decodePluginSource([0xFF, 0xFE, 0x41]),
        throwsA(isA<PluginFileException>().having((e) => e.messageFa, 'messageFa', contains('UTF-8'))));
    expect(FileSelectorPluginFiles.withPyExtension(r'C:\x\t'), r'C:\x\t.py');
    expect(FileSelectorPluginFiles.withPyExtension(r'C:\x\t.PY'), r'C:\x\t.PY');
  });

  test('file_selector adapter with injected dialogs: .py filter, name + text, size cap, save path + reveal', () async {
    List<XTypeGroup>? openGroups;
    XFile? next =
        XFile.fromData(utf8.encode('\uFEFFname = "ma_cross"'), name: 'ma_cross.py', path: r'C:\s\ma_cross.py');
    final List<(String, List<String>)> started = [];
    final FileSelectorPluginFiles files = FileSelectorPluginFiles(
      openPicker: ({String? initialDirectory, List<XTypeGroup> acceptedTypeGroups = const []}) async {
        openGroups = acceptedTypeGroups;
        return next;
      },
      savePicker: (
              {String? initialDirectory,
              String? suggestedName,
              List<XTypeGroup> acceptedTypeGroups = const []}) async =>
          FileSaveLocation(r'C:\s\template'),
      startExplorer: (String exe, List<String> args) async => started.add((exe, args)),
      isWindows: true,
    );

    final PickedPluginFile picked = (await files.pickPluginFile())!;
    expect(openGroups!.single.extensions, ['py']);
    expect(picked.name, 'ma_cross.py');
    expect(picked.text, 'name = "ma_cross"');

    next = XFile.fromData(Uint8List(kPluginMaxBytes + 1), name: 'big.py', path: r'C:\s\big.py');
    await expectLater(files.pickPluginFile(), throwsA(isA<PluginFileException>()));
    next = null;
    expect(await files.pickPluginFile(), isNull);

    expect(await files.pickTemplateSaveLocation(suggestedName: kTemplateFilename), r'C:\s\template.py');
    await files.revealInFolder(r'C:\s\template.py');
    expect(started.single.$1, 'explorer.exe');
    expect(started.single.$2.first, '/select,');
  });
}
