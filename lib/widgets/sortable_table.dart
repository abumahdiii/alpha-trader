import 'package:flutter/material.dart';

import '../core/dev_mode.dart';
import '../theme/app_semantic_colors.dart';

/// One column of a [SortableTable].
class SortableColumn<T> {
  const SortableColumn({
    required this.label,
    required this.width,
    required this.cell,
    this.sortKey,
    this.tooltip,
  });

  final String label;
  final double width;
  final Widget Function(BuildContext context, T row) cell;

  /// Value to sort by; null = the column is not sortable. Null values sort last.
  final Comparable<Object>? Function(T row)? sortKey;
  final String? tooltip;
}

/// A lazily built table (thousands of rows stay cheap): a fixed header whose
/// sortable column titles toggle ascending / descending, and fixed-height rows
/// in a [ListView.builder]. Scrolls horizontally when wider than its box.
/// Needs a bounded height.
///
/// Keys for tests: header cell `'$keyPrefix-h-<column index>'`, row
/// `'$keyPrefix-row-<position after sorting>'`.
class SortableTable<T> extends StatefulWidget {
  const SortableTable({
    super.key,
    required this.keyPrefix,
    required this.columns,
    required this.rows,
    this.onRowTap,
    this.isSelected,
    this.rowHeight = 34,
    this.initialSortColumn,
    this.initialAscending = true,
  });

  final String keyPrefix;
  final List<SortableColumn<T>> columns;
  final List<T> rows;
  final ValueChanged<T>? onRowTap;
  final bool Function(T row)? isSelected;
  final double rowHeight;
  final int? initialSortColumn;
  final bool initialAscending;

  @override
  State<SortableTable<T>> createState() => _SortableTableState<T>();
}

class _SortableTableState<T> extends State<SortableTable<T>> {
  late int? _sortColumn = widget.initialSortColumn;
  late bool _ascending = widget.initialAscending;
  final ScrollController _horizontal = ScrollController();
  final ScrollController _vertical = ScrollController();

  List<T>? _sorted;
  List<T>? _sortedFrom;
  (int?, bool)? _sortedBy;

  @override
  void dispose() {
    _horizontal.dispose();
    _vertical.dispose();
    super.dispose();
  }

  List<T> _rows() {
    final (int?, bool) by = (_sortColumn, _ascending);
    if (identical(_sortedFrom, widget.rows) && _sortedBy == by && _sorted != null) return _sorted!;
    final Comparable<Object>? Function(T)? key = _sortColumn == null ? null : widget.columns[_sortColumn!].sortKey;
    List<T> rows = widget.rows;
    if (key != null) {
      final List<(int, T)> indexed = [for (int i = 0; i < rows.length; i++) (i, rows[i])];
      indexed.sort(((int, T) a, (int, T) b) {
        final Comparable<Object>? ka = key(a.$2);
        final Comparable<Object>? kb = key(b.$2);
        int c;
        if (ka == null && kb == null) {
          c = 0;
        } else if (ka == null) {
          return 1; // nulls last in both directions
        } else if (kb == null) {
          return -1;
        } else {
          c = _ascending ? ka.compareTo(kb) : kb.compareTo(ka);
        }
        return c != 0 ? c : a.$1.compareTo(b.$1); // stable
      });
      rows = [for (final (int, T) e in indexed) e.$2];
    }
    _sorted = rows;
    _sortedFrom = widget.rows;
    _sortedBy = by;
    return rows;
  }

  void _toggleSort(int column) {
    setState(() {
      if (_sortColumn == column) {
        _ascending = !_ascending;
      } else {
        _sortColumn = column;
        _ascending = true;
      }
    });
    devLog('[SortableTable ${widget.keyPrefix}] sort by ${widget.columns[column].label} '
        '${_ascending ? 'asc' : 'desc'}');
  }

  @override
  Widget build(BuildContext context) {
    final List<T> rows = _rows();
    final AppSemanticColors colors = context.appColors;
    final ThemeData theme = Theme.of(context);
    final double tableWidth = widget.columns.fold(0.0, (double s, SortableColumn<T> c) => s + c.width);
    return LayoutBuilder(builder: (BuildContext context, BoxConstraints box) {
      final double width = box.maxWidth.isFinite && box.maxWidth > tableWidth ? box.maxWidth : tableWidth;
      return Scrollbar(
        controller: _horizontal,
        thumbVisibility: width > box.maxWidth,
        notificationPredicate: (ScrollNotification n) => n.depth == 0,
        child: SingleChildScrollView(
          controller: _horizontal,
          scrollDirection: Axis.horizontal,
          child: SizedBox(
            width: width,
            height: box.maxHeight,
            child: Column(children: [
              Container(
                height: 40,
                color: colors.surfaceMuted,
                child: Row(children: [
                  for (int i = 0; i < widget.columns.length; i++) _header(context, i),
                ]),
              ),
              Expanded(
                child: Scrollbar(
                  controller: _vertical,
                  child: ListView.builder(
                    controller: _vertical,
                    itemCount: rows.length,
                    itemExtent: widget.rowHeight,
                    itemBuilder: (BuildContext context, int index) {
                      final T row = rows[index];
                      final bool selected = widget.isSelected?.call(row) ?? false;
                      return Material(
                        key: ValueKey<String>('${widget.keyPrefix}-row-$index'),
                        color: selected
                            ? theme.colorScheme.primary.withValues(alpha: 0.12)
                            : (index.isOdd ? colors.surfaceMuted.withValues(alpha: 0.35) : Colors.transparent),
                        child: InkWell(
                          onTap: widget.onRowTap == null ? null : () => widget.onRowTap!(row),
                          child: Row(children: [
                            for (final SortableColumn<T> c in widget.columns)
                              SizedBox(
                                width: c.width,
                                child: Padding(
                                  padding: const EdgeInsets.symmetric(horizontal: 6),
                                  child:
                                      Align(alignment: AlignmentDirectional.centerStart, child: c.cell(context, row)),
                                ),
                              ),
                          ]),
                        ),
                      );
                    },
                  ),
                ),
              ),
            ]),
          ),
        ),
      );
    });
  }

  Widget _header(BuildContext context, int i) {
    final SortableColumn<T> c = widget.columns[i];
    final bool active = _sortColumn == i;
    final TextStyle? style = Theme.of(context).textTheme.labelMedium?.copyWith(fontWeight: FontWeight.bold);
    Widget label = Row(mainAxisSize: MainAxisSize.min, children: [
      Flexible(child: Text(c.label, style: style, maxLines: 2, overflow: TextOverflow.ellipsis)),
      if (active) Icon(_ascending ? Icons.arrow_upward : Icons.arrow_downward, size: 14),
    ]);
    if (c.tooltip != null) label = Tooltip(message: c.tooltip!, child: label);
    return SizedBox(
      key: ValueKey<String>('${widget.keyPrefix}-h-$i'),
      width: c.width,
      child: InkWell(
        onTap: c.sortKey == null ? null : () => _toggleSort(i),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 6),
          child: Align(alignment: AlignmentDirectional.centerStart, child: label),
        ),
      ),
    );
  }
}
