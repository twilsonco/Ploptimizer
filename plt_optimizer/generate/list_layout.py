"""List marker-column layout (pre-pass).

A bullet/enumerated list engraves its markers in ONE flush column shared by
every item of the list, and every item's text (plus every continuation line
the item grows to) starts at the hanging-indent column right of the markers.
The column position is a pure geometry question -- the widest marker of the
list plus the cascaded ``list_marker_gap_fraction`` of the line's cap height
-- and it must be known BEFORE the word-level reflow pre-pass, because the
reflow partitions an item's words against the *text* budget, which is the
label's content width minus this indent.

This module runs as the FIRST export pre-pass (see
:mod:`plt_optimizer.generate.vectorize`): it measures every list line's
marker (rendered standalone through the same renderer dispatch the label
render path uses, so widths match what the label engraves), and writes the
per-line ``indent_left`` onto the resolved lines. Lines outside a list keep
``indent_left == 0.0`` and the pass is a free no-op for a job without lists.

Example:
    >>> from plt_optimizer.generate.list_layout import apply_list_marker_layout
    >>> labels = apply_list_marker_layout(labels)
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Callable, Dict, List, Optional, Sequence

from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine

logger = logging.getLogger(__name__)

# Signature of the probe measuring one resolved text line's natural rendered
# width in inches (pre-compression, at the line's own typography). Injectable
# so the pass is unit-testable without matplotlib; the default lazily imports
# ``label_renderer.measure_line_natural_width``.
MarkerWidthProbe = Callable[[ResolvedTextLine], float]


def _default_probe(line: ResolvedTextLine) -> float:
    """Render ``line`` alone and return its natural width in inches.

    Imported lazily so this module stays importable on the Python 3.8 /
    Windows 7 path where matplotlib is unavailable (the same guarantee the
    line-content reflow pre-pass carries).

    Args:
        line: The resolved text line to measure.

    Returns:
        The rendered width in inches (``0.0`` for a line with no geometry).
    """
    from plt_optimizer.generate.label_renderer import measure_line_natural_width

    return measure_line_natural_width(line)


def _marker_clone(line: ResolvedTextLine) -> ResolvedTextLine:
    """Return a copy of ``line`` whose text is just the marker (no marker).

    The marker engraves in the item's own typography (font, height, cutter,
    spacing, markup delimiters), so measuring ``line.with_markup(marker)``
    with the marker metadata cleared gives exactly the width the positioned
    render will occupy (the renderer's ``_marker_line_clone`` builds the same
    clone for the real render).

    Args:
        line: The resolved list line carrying a non-empty ``marker_text``.

    Returns:
        The marker-only measurement clone.
    """
    text_only = line.with_markup(line.marker_text)
    return dataclasses.replace(text_only, marker_text="", align_group_id=-1, list_item=False)


def _label_marker_widths(
    label: ResolvedLabel,
    probe: MarkerWidthProbe,
    cache: Dict[ResolvedTextLine, float],
) -> Dict[int, float]:
    """Measure the widest marker of every align group of one label.

    Args:
        label: The resolved label to scan.
        probe: The line-width probe.
        cache: Shared marker-width cache (mutated; keyed on the measurement
            clone, a frozen dataclass with value equality, so identical
            markers in identical typography measure once per job).

    Returns:
        Mapping of ``align_group_id`` to the group's widest marker width
        (inches). Groups whose markers carry no ink are absent (their items
        get a zero marker-column width, i.e. an indent-only list).
    """
    widths: Dict[int, float] = {}
    for line in label.content:
        if not line.list_item or line.align_group_id < 0 or not line.marker_text:
            continue
        clone = _marker_clone(line)
        width = cache.get(clone)
        if width is None:
            width = probe(clone)
            cache[clone] = width
        if width > widths.get(line.align_group_id, 0.0):
            widths[line.align_group_id] = width
    return widths


def apply_list_marker_layout(
    resolved_labels: Sequence[ResolvedLabel],
    *,
    probe: Optional[MarkerWidthProbe] = None,
) -> List[ResolvedLabel]:
    """Set every list line's ``indent_left`` (marker column + gap).

    The indent is per line: the widest marker of the line's align group plus
    the line's own ``list_marker_gap_fraction`` of its cap height, so items
    of one list share the marker column while each line's text gap scales
    with its own typography. Labels without list lines are returned as the
    *same objects*, so the pass costs nothing for the overwhelmingly common
    list-less job (and downstream render-cache identity is preserved).

    Args:
        resolved_labels: The job's resolved labels, in content order.
        probe: Optional probe returning a resolved line's natural rendered
            width in inches. Defaults to rendering through
            :func:`plt_optimizer.generate.label_renderer.measure_line_natural_width`;
            injectable for tests.

    Returns:
        A list of resolved labels, with indented clones substituted for the
        labels containing list lines.
    """
    if not any(line.list_item for label in resolved_labels for line in label.content):
        return list(resolved_labels)

    width_probe: MarkerWidthProbe = probe if probe is not None else _default_probe
    cache: Dict[ResolvedTextLine, float] = {}

    result: List[ResolvedLabel] = []
    for label in resolved_labels:
        marker_widths = _label_marker_widths(label, width_probe, cache)
        if not any(line.list_item for line in label.content):
            result.append(label)
            continue
        new_content: List[ResolvedTextLine] = []
        changed = False
        for line in label.content:
            if not line.list_item:
                new_content.append(line)
                continue
            column = marker_widths.get(line.align_group_id, 0.0)
            indent = column + line.list_marker_gap_fraction * line.toolpath_text_height
            if line.indent_left == indent:
                new_content.append(line)
                continue
            new_content.append(dataclasses.replace(line, indent_left=indent))
            changed = True
        if changed:
            logger.debug(
                "Label %s: list marker column set (%d group(s)).",
                label.id,
                len(marker_widths),
            )
            result.append(dataclasses.replace(label, content=new_content))
        else:
            result.append(label)
    return result
