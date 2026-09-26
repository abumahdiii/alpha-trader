import 'dart:async';
import 'dart:collection';

import 'package:flutter/material.dart';

import '../core/dev_mode.dart';

/// Project-wide stacked top notices.
///
/// Replaces the `top_snackbar_flutter` package entry point, whose
/// `showTopSnackBar` inserted one overlay entry per call at the same top
/// position, so two notices fired in the same instant (e.g. two signals
/// arriving together) were drawn on top of each other.
///
/// Here every [OverlayState] gets exactly one host [OverlayEntry] that lays the
/// notices out as a vertical stack under the top edge:
/// * oldest on top, newer ones below, [kTopNoticeSpacing] apart;
/// * a notice leaves after its display duration, on tap, or on swipe-up, and
///   the ones below slide up into its place (size-collapse + fade);
/// * hovering a notice with the mouse pauses its countdown;
/// * at most [kTopNoticeMaxVisible] are shown; the rest wait in a FIFO queue
///   and their countdown only starts once they are shown — nothing is dropped.
///
/// All state (list, queue, countdowns, entered/leaving flags) lives in
/// [_NoticeStackModel], not in widget state, so re-inserting the host entry
/// (done to keep it above freshly opened dialogs) neither resets countdowns nor
/// replays enter animations.

/// Default time a notice stays fully shown (same as the old package default).
const Duration kTopNoticeDisplayDuration = Duration(milliseconds: 3000);

/// Enter animation (slide-down + fade + size-grow).
const Duration kTopNoticeEnterDuration = Duration(milliseconds: 300);

/// Exit animation (fade + size-collapse, the ones below move up meanwhile).
const Duration kTopNoticeExitDuration = Duration(milliseconds: 250);

/// Vertical gap between two stacked notices.
const double kTopNoticeSpacing = 8;

/// Outer margin of the stack (left/right/top, like the old package padding).
const double kTopNoticeMargin = 16;

/// Maximum number of notices on screen at once; the rest are queued.
const int kTopNoticeMaxVisible = 5;

/// Countdown granularity.
const Duration _kTick = Duration(milliseconds: 50);

/// Drop-in replacement for `top_snackbar_flutter`'s `showTopSnackBar`.
///
/// Shows [child] (e.g. a `CustomSnackBar.success(...)`) in the stacked notice
/// area of [overlayState]. [displayDuration] is how long the notice stays
/// fully shown after its enter animation (paused while hovered).
void showTopSnackBar(
  OverlayState overlayState,
  Widget child, {
  Duration displayDuration = kTopNoticeDisplayDuration,
}) {
  _purgeDeadStacks();
  if (!overlayState.mounted) {
    devLog('TopNotice: overlay not mounted, notice skipped');
    return;
  }
  final model = _stacks.putIfAbsent(
    overlayState,
    () => _NoticeStackModel(overlayState),
  );
  model.add(child, displayDuration);
}

/// Dismisses (animated) every shown notice and clears the queue of every
/// stack. Handy before a full-screen transition.
void dismissAllTopNotices() {
  for (final model in _stacks.values.toList()) {
    model.dismissAll();
  }
}

/// Number of notices currently on screen for [overlayState] (including ones
/// that are playing their exit animation).
@visibleForTesting
int debugTopNoticeVisibleCount(OverlayState overlayState) =>
    _stacks[overlayState]?._active.length ?? 0;

/// Number of notices waiting in the queue for [overlayState].
@visibleForTesting
int debugTopNoticeQueuedCount(OverlayState overlayState) =>
    _stacks[overlayState]?._queue.length ?? 0;

/// Cancels every timer and removes every host entry. For tests only.
@visibleForTesting
void debugResetTopNotices() {
  for (final model in _stacks.values.toList()) {
    model.dispose();
  }
  _stacks.clear();
}

final Map<OverlayState, _NoticeStackModel> _stacks =
    <OverlayState, _NoticeStackModel>{};

/// Drops stacks whose overlay was disposed (e.g. after `Phoenix.rebirth` or a
/// navigator replacement), cancelling their timers.
void _purgeDeadStacks() {
  final dead = _stacks.entries.where((e) => !e.key.mounted).toList();
  for (final e in dead) {
    devLog('TopNotice: overlay disposed, dropping its stack '
        '(visible=${e.value._active.length}, queued=${e.value._queue.length})');
    e.value.dispose();
    _stacks.remove(e.key);
  }
}

class _Notice {
  _Notice(this.id, this.child, this.displayDuration);

  final int id;
  final Widget child;
  final Duration displayDuration;

  /// Time left before auto-dismiss; set when the notice becomes visible.
  Duration remaining = Duration.zero;

  /// Enter animation already started once (never replayed on host re-insert).
  bool entered = false;

  /// Exit in progress (animated) — no longer counts down, cannot re-dismiss.
  bool leaving = false;

  /// Mouse is over the notice — countdown paused.
  bool hovered = false;

  Timer? exitTimer;
}

class _NoticeStackModel extends ChangeNotifier {
  _NoticeStackModel(this._overlay);

  final OverlayState _overlay;
  OverlayEntry? _entry;
  final List<_Notice> _active = <_Notice>[];
  final Queue<_Notice> _queue = Queue<_Notice>();
  Timer? _ticker;
  int _nextId = 0;
  bool _disposed = false;

  String get _counts => 'visible=${_active.length}, queued=${_queue.length}';

  void add(Widget child, Duration displayDuration) {
    if (_disposed) return;
    final notice = _Notice(_nextId++, child, displayDuration);
    if (_active.length < kTopNoticeMaxVisible) {
      _activate(notice);
      devLog('TopNotice: add #${notice.id} shown ($_counts)');
    } else {
      _queue.add(notice);
      devLog('TopNotice: add #${notice.id} queued ($_counts)');
    }
    // Always bring the host to the top so the new notice is above any dialog
    // opened since the host was inserted.
    _insertHost();
    _notify();
  }

  void _activate(_Notice notice) {
    notice.remaining = kTopNoticeEnterDuration + notice.displayDuration;
    _active.add(notice);
    _ensureTicker();
  }

  void _ensureTicker() {
    if (_ticker != null || _disposed) return;
    _ticker = Timer.periodic(_kTick, (_) => _onTick());
  }

  void _onTick() {
    if (_disposed) return;
    var counting = false;
    for (final notice in _active.toList()) {
      if (notice.leaving || notice.hovered) continue;
      notice.remaining -= _kTick;
      if (notice.remaining <= Duration.zero) {
        devLog('TopNotice: #${notice.id} expired');
        dismiss(notice);
      } else {
        counting = true;
      }
    }
    if (!counting) {
      _ticker?.cancel();
      _ticker = null;
    }
  }

  /// Starts the animated exit of [notice]; removed after the exit animation.
  void dismiss(_Notice notice) {
    if (_disposed || notice.leaving || !_active.contains(notice)) return;
    notice.leaving = true;
    notice.exitTimer = Timer(kTopNoticeExitDuration, () => _remove(notice));
    devLog('TopNotice: dismiss #${notice.id} ($_counts)');
    _notify();
  }

  /// Removes [notice] right away (used by swipe, whose Dismissible already
  /// collapsed it and requires it to leave the tree immediately).
  void removeNow(_Notice notice) {
    if (_disposed) return;
    devLog('TopNotice: swiped #${notice.id}');
    notice.leaving = true;
    _remove(notice);
  }

  void _remove(_Notice notice) {
    if (_disposed) return;
    notice.exitTimer?.cancel();
    if (!_active.remove(notice)) return;
    var promoted = false;
    while (_active.length < kTopNoticeMaxVisible && _queue.isNotEmpty) {
      final next = _queue.removeFirst();
      _activate(next);
      promoted = true;
      devLog('TopNotice: queued #${next.id} shown ($_counts)');
    }
    devLog('TopNotice: removed #${notice.id} ($_counts)');
    if (_active.isEmpty && _queue.isEmpty) {
      _removeHost();
      _ticker?.cancel();
      _ticker = null;
    } else if (promoted) {
      // A dialog may have been opened since; keep the promoted ones visible.
      _insertHost();
    }
    _notify();
  }

  void setHovered(_Notice notice, bool hovered) {
    if (_disposed || notice.hovered == hovered) return;
    notice.hovered = hovered;
    devLog('TopNotice: #${notice.id} ${hovered ? 'paused' : 'resumed'} '
        '(remaining=${notice.remaining.inMilliseconds}ms)');
    if (!hovered) _ensureTicker();
  }

  void dismissAll() {
    if (_disposed) return;
    _queue.clear();
    for (final notice in _active.toList()) {
      dismiss(notice);
    }
  }

  /// (Re)inserts the host entry at the top of the overlay. A fresh entry is
  /// used each time; the old one is removed in the same frame. Tiles restore
  /// their state from the model, so nothing restarts.
  void _insertHost() {
    if (_disposed) return;
    if (!_overlay.mounted) {
      devLog('TopNotice: overlay gone while inserting host ($_counts)');
      _stacks.remove(_overlay);
      dispose();
      return;
    }
    final old = _entry;
    final entry = OverlayEntry(builder: (_) => _NoticeStackHost(model: this));
    try {
      _overlay.insert(entry);
      _entry = entry;
    } catch (e, st) {
      devLog('TopNotice: host insert failed: $e\n$st');
      entry.dispose();
      return;
    }
    _disposeEntry(old);
  }

  void _removeHost() {
    final old = _entry;
    _entry = null;
    _disposeEntry(old);
  }

  static void _disposeEntry(OverlayEntry? entry) {
    if (entry == null) return;
    try {
      // remove() returns early (without touching the overlay) when the
      // overlay itself was already disposed.
      entry.remove();
    } catch (_) {
      // Entry was never inserted or already removed.
    }
    try {
      entry.dispose();
    } catch (e) {
      devLog('TopNotice: entry dispose failed: $e');
    }
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    if (_disposed) return;
    _ticker?.cancel();
    _ticker = null;
    for (final notice in _active) {
      notice.exitTimer?.cancel();
    }
    _active.clear();
    _queue.clear();
    final old = _entry;
    _entry = null;
    _disposeEntry(old);
    _disposed = true;
    super.dispose();
  }
}

/// The single per-overlay host: a top-anchored column of notices.
class _NoticeStackHost extends StatelessWidget {
  const _NoticeStackHost({required this.model});

  final _NoticeStackModel model;

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.paddingOf(context).top + kTopNoticeMargin;
    return Positioned(
      top: top,
      left: kTopNoticeMargin,
      right: kTopNoticeMargin,
      child: ListenableBuilder(
        listenable: model,
        builder: (context, _) {
          if (model._disposed) return const SizedBox.shrink();
          return Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final notice in model._active)
                _NoticeTile(
                  key: ValueKey<int>(notice.id),
                  notice: notice,
                  model: model,
                ),
            ],
          );
        },
      ),
    );
  }
}

class _NoticeTile extends StatefulWidget {
  const _NoticeTile({super.key, required this.notice, required this.model});

  final _Notice notice;
  final _NoticeStackModel model;

  @override
  State<_NoticeTile> createState() => _NoticeTileState();
}

class _NoticeTileState extends State<_NoticeTile>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  late final CurvedAnimation _curve;
  late final Animation<Offset> _slide;

  @override
  void initState() {
    super.initState();
    final notice = widget.notice;
    _controller = AnimationController(
      vsync: this,
      duration: kTopNoticeEnterDuration,
      reverseDuration: kTopNoticeExitDuration,
      // A host re-insert must not replay the enter animation.
      value: notice.entered ? 1 : 0,
    );
    _curve = CurvedAnimation(
      parent: _controller,
      curve: Curves.easeOutCubic,
      reverseCurve: Curves.easeInCubic,
    );
    _slide = Tween<Offset>(
      begin: const Offset(0, -0.35),
      end: Offset.zero,
    ).animate(_curve);
    if (notice.leaving) {
      _controller.reverse();
    } else if (!notice.entered) {
      notice.entered = true;
      _controller.forward();
    }
  }

  @override
  void didUpdateWidget(covariant _NoticeTile oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.notice.leaving &&
        _controller.status != AnimationStatus.reverse &&
        _controller.status != AnimationStatus.dismissed) {
      _controller.reverse();
    }
  }

  @override
  void dispose() {
    _curve.dispose();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final notice = widget.notice;
    final model = widget.model;
    // SizeTransition clips, so the slide never paints over the notice above;
    // collapsing it (with the spacing) makes the ones below move up.
    return SizeTransition(
      sizeFactor: _curve,
      alignment: Alignment.topCenter,
      child: FadeTransition(
        opacity: _curve,
        child: SlideTransition(
          position: _slide,
          // SizeTransition loosens the width; keep notices full-width.
          child: Padding(
            padding: const EdgeInsets.only(bottom: kTopNoticeSpacing),
            child: SizedBox(
              width: double.infinity,
              child: MouseRegion(
                onEnter: (_) => model.setHovered(notice, true),
                onExit: (_) => model.setHovered(notice, false),
                child: GestureDetector(
                  behavior: HitTestBehavior.opaque,
                  onTap: () => model.dismiss(notice),
                  child: Dismissible(
                    key: ValueKey<String>('top-notice-${notice.id}'),
                    direction: DismissDirection.up,
                    dismissThresholds: const {DismissDirection.up: 0.2},
                    resizeDuration: kTopNoticeExitDuration,
                    onDismissed: (_) => model.removeNow(notice),
                    child: Material(
                      type: MaterialType.transparency,
                      child: notice.child,
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
