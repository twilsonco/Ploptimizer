"""Per-label shared horizontal compression (pre-pass).

Horizontal compression is decided per text line during rendering: a line
wider than the label's inner content area is squeezed (bounded by its
``max_h_compress`` budget), and a line colliding with a drill hole is squeezed
by the Phase-3 collision sweep. Two lines of the *same nominal text height*
in one label can therefore end up engraved at visibly different glyph
densities -- the long line compressed, its short sibling left at natural
width.

This module closes that gap as an **export pre-pass**: render each label once,
read the effective per-line horizontal scale, and where one line of a
same-height group was compressed, apply the group's *most-compressed* (minimum)
scale to every other eligible line of that group **within the same label**.
The result is a ``ResolvedLabel`` clone carrying a ``global_compress_by_line``
map, produced *before* :func:`plt_optimizer.generate.resolution.build_cutter_pen_map`
runs, so the pen map, the per-cutter layers and the PLT file names all see the
label that is actually going to be engraved.

The shared scale is the minimum effective scale among the group's triggering
lines, clamped to each receiver's own ``1 - max_h_compress`` budget floor (a
line is never squeezed past its own budget). Because the group minimum is
fixed by the triggering lines' natural scales, and applying it to a fitting
sibling never *deepens* any line's natural compression, a single pass
converges -- there is no fixpoint loop.

Sharing is opt-in via ``h_compress_global`` (off by default). A line opts out
of sharing (both directions) with ``h_compress_global: false``; a line with a
zero ``max_h_compress`` budget is never touched (no compression mechanism can
ever fire on it). Sharing is always per-label -- the job level only enables
the option for its labels.

Accepted staleness: this pre-pass runs *after* the compression-driven cutter
reduction (see :mod:`plt_optimizer.generate.cutter_downsize`). A shared
compression can deepen a line's compression and would, in principle, justify a
further cutter downsize the cutter pre-pass (which ran first) does not see.
The two pre-passes are deliberately not looped to a joint fixpoint.

Scope guards (the pass is a no-op, and costs nothing, when any of these
apply):

- no label has an eligible line (``h_compress_global`` on and
  ``max_h_compress > 0``);
- no triggering line in a same-height group with two or more eligible lines.

Example:
    >>> from plt_optimizer.generate.h_compress import apply_global_h_compress
    >>> labels = apply_global_h_compress(labels)
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Dict, List, Optional, Sequence

from plt_optimizer.generate.cutter_downsize import ScaleProbe, _height_groups
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine

logger = logging.getLogger(__name__)

# Effective-scale comparison tolerance: a scale within this of ``1.0`` counts
# as uncompressed (matches the reporting tolerance for compressed lines).
_SCALE_TOLERANCE: float = 1e-9


def _default_probe(label: ResolvedLabel) -> Dict[int, float]:
    """Render ``label`` and return its effective per-line horizontal scales.

    Imported lazily so this module stays importable on the Python 3.8 /
    Windows 7 path where matplotlib is unavailable (nothing in the
    ``watch``/``optimize`` commands imports it, and the lazy import keeps that
    guarantee even if a future caller does).

    Args:
        label: The resolved label to render.

    Returns:
        Mapping of line index to effective horizontal scale (only lines
        compressed below ``1.0`` are present).
    """
    from plt_optimizer.generate.label_renderer import render_label_to_plt

    return dict(render_label_to_plt(label).compression_by_line)


def _is_eligible(line: ResolvedTextLine) -> bool:
    """Return whether a resolved line may participate in shared compression.

    Args:
        line: The resolved text line to classify.

    Returns:
        ``True`` when the sharing permission is on and a compression budget
        exists (without ``max_h_compress`` no compression mechanism can ever
        fire, so the line's scale is always ``1.0`` and it can neither trigger
        nor receive a shared compression).
    """
    return line.h_compress_global and line.max_h_compress > 0.0


def _label_has_candidates(label: ResolvedLabel) -> bool:
    """Return whether any text line of ``label`` is eligible for sharing."""
    return any(_is_eligible(line) for line in label.content)


def _shared_scale(line: ResolvedTextLine, group_min: float) -> float:
    """Clamp a group's shared scale to one line's own budget floor.

    Args:
        line: The resolved line receiving the shared scale.
        group_min: The group's most-compressed (minimum) triggering scale.

    Returns:
        ``max(group_min, 1 - max_h_compress)``: the shared scale never
        squeezes a line past its own ``max_h_compress`` budget.
    """
    floor = 1.0 - min(max(line.max_h_compress, 0.0), 1.0)
    return max(group_min, floor)


def _with_global_compress(label: ResolvedLabel, shared: Dict[int, float]) -> ResolvedLabel:
    """Clone ``label`` carrying the shared per-line compression scales.

    Args:
        label: The label to clone.
        shared: Mapping of line index to shared horizontal scale.

    Returns:
        A new label carrying ``global_compress_by_line``.
    """
    return dataclasses.replace(label, global_compress_by_line=dict(shared))


def _share_label(
    label: ResolvedLabel,
    scales: Dict[int, float],
) -> Optional[ResolvedLabel]:
    """Compute one label's shared-compression clone from its measured scales.

    Every same-height group containing at least one triggering eligible line
    (measured scale below ``1.0``) and two or more eligible lines receives the
    group's minimum scale, clamped to each line's own budget floor. Groups
    without a trigger, and groups with a single eligible line, are left alone.

    Args:
        label: The label to measure-share.
        scales: The label's measured effective per-line horizontal scales.

    Returns:
        A cloned label carrying ``global_compress_by_line`` when any group
        shares, otherwise ``None`` (the label is left untouched).
    """
    shared: Dict[int, float] = {}
    for members in _height_groups(label.content):
        eligible = [index for index in members if _is_eligible(label.content[index])]
        if len(eligible) < 2:
            continue
        triggering = [
            index for index in eligible if scales.get(index, 1.0) < 1.0 - _SCALE_TOLERANCE
        ]
        if not triggering:
            continue
        group_min = min(scales[index] for index in triggering)
        for index in eligible:
            line = label.content[index]
            scale = _shared_scale(line, group_min)
            if scale >= 1.0 - _SCALE_TOLERANCE:
                continue  # This line's own budget floor keeps it uncompressed.
            shared[index] = scale
            if index not in triggering:
                logger.warning(
                    "Label %s: text line %d (%r) sharing the label's horizontal "
                    "compression at scale %.4f (%.1f%%).",
                    label.id,
                    index,
                    line.text,
                    scale,
                    (1.0 - scale) * 100.0,
                )
    if not shared:
        return None
    return _with_global_compress(label, shared)


def apply_global_h_compress(
    resolved_labels: Sequence[ResolvedLabel],
    *,
    probe: Optional[ScaleProbe] = None,
) -> List[ResolvedLabel]:
    """Share horizontal compression across same-height lines of each label.

    Runs before the pen map is built so the shared compression flows into the
    rendered geometry, the per-cutter layers and the PLT file names. Labels
    that need no sharing are returned as the *same objects*, so callers can
    pass the result straight through and untouched labels keep their identity
    (and their downstream render-cache hits).

    Args:
        resolved_labels: The job's resolved labels, in content order.
        probe: Optional render probe returning a label's effective per-line
            horizontal scales. Defaults to rendering through
            :func:`plt_optimizer.generate.label_renderer.render_label_to_plt`;
            injectable for tests.

    Returns:
        A list of resolved labels, with shared-compression clones substituted
        for the labels whose compression was shared.
    """
    if not any(_label_has_candidates(label) for label in resolved_labels):
        return list(resolved_labels)

    scale_probe: ScaleProbe = probe if probe is not None else _default_probe

    result: List[ResolvedLabel] = []
    # One measurement render per unique label id (labels with count > 1 render
    # once downstream too), reused across every clone of that id.
    measured: Dict[str, Dict[int, float]] = {}
    for label in resolved_labels:
        if not _label_has_candidates(label):
            result.append(label)
            continue

        scales = measured.get(label.id)
        if scales is None:
            scales = scale_probe(label)
            measured[label.id] = scales
        if not any(scale < 1.0 - _SCALE_TOLERANCE for scale in scales.values()):
            result.append(label)
            continue

        clone = _share_label(label, scales)
        result.append(clone if clone is not None else label)
    return result
