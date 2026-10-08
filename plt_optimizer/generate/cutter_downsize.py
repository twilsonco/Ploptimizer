"""Compression-driven automatic cutter reduction (pre-pass).

The cutter for a text line is chosen during resolution (Phase 2), from the
nominal text height alone. Whether the line will need horizontal compression
is only knowable after rendering, when the natural width is measured against
the label's inner content area (``max_h_compress`` margin compression) and
against the drill holes (Phase 3 collision compression). A line that has to
be squeezed is a line the chosen cutter is too fat for: the glyphs are being
pushed together, so a *smaller* tool engraves the same text with less
bleeding and needs less squeeze.

This module closes that loop as an **export pre-pass**: render each label
once, read the effective per-line horizontal scale, and where a line was
compressed past the midpoint toward the next smaller inventory tool, swap in
that tool and re-render at ``nominal_text_height - smaller_cutter``. The
result is a ``ResolvedLabel`` clone carrying the reduced cutter, produced
*before* :func:`plt_optimizer.generate.resolution.build_cutter_pen_map` runs,
so the pen map, the per-cutter layers and the PLT file names all see the tool
that is actually going to be used.

The swap is **one-way**: a downsized cutter is never enlarged again within the
same text line. Each step re-measures the line, because a smaller cutter
renders *wider* (bigger toolpath height, wider kerning clearance), which can
deepen the compression and justify a further step. The number of steps is
capped by ``max_cutter_downsizes``; the cutter strictly decreases every step,
so the loop always terminates.

Per-label sharing (``cutter_downsize_global``, on by default): when a line's
compression triggers a swap, every other *eligible* line of the same nominal
text height **within the same label** receives the same swap, so one label
engraves one text size with one tool. The shared swap bypasses the midpoint
trigger check for the receiving lines (that is the point of sharing) and is
followed by each receiver's own one-way step loop; the group converges to the
smallest final cutter among its members. A line opts out of sharing (both
directions) with ``cutter_downsize_global: false``; the eligibility opt-outs
below apply to receivers too. Sharing is always per-label -- the job level
only enables the option for its labels.

Scope guards (the pass is a no-op, and costs nothing, when any of these
apply):

- no ``tools.json`` ``available_cutters`` inventory -- the ladder is the
  shop's real tool list, and there is nothing to snap to without it;
- the line opts out via ``cutter_downsize: false``;
- ``max_cutter_downsizes: 0``;
- the line declares an explicit ``cutter_size`` (an explicit tool is a human
  decision and is never overridden automatically);
- the line's ``max_h_compress`` budget is ``0.0`` -- both compression
  mechanisms (margin overflow and collision avoidance) require budget > 0, so
  no line in the label can ever be squeezed.

Example:
    >>> from plt_optimizer.generate.cutter_downsize import (
    ...     apply_compression_cutter_downsize,
    ... )
    >>> labels = apply_compression_cutter_downsize(labels, [0.03, 0.045, 0.06])
"""

from __future__ import annotations

import dataclasses
import logging
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from plt_optimizer.generate.resolution import (
    ResolvedLabel,
    ResolvedTextLine,
    next_smaller_cutter,
    should_downsize_cutter,
)

logger = logging.getLogger(__name__)

# Signature of the render probe used to measure a label's effective per-line
# horizontal scales. Injectable so the step loop is unit-testable without
# matplotlib.
ScaleProbe = Callable[[ResolvedLabel], Dict[int, float]]

# Cutter comparison tolerance for the shared-downsize ladder (matches the
# ``next_smaller_cutter`` inventory comparison) and the nominal text height
# grouping tolerance.
_CUTTER_TOLERANCE: float = 1e-9
_HEIGHT_TOLERANCE: float = 1e-9


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
    """Return whether a resolved line may participate in cutter reduction.

    Args:
        line: The resolved text line to classify.

    Returns:
        ``True`` when the permission is on, a step budget remains, no explicit
        cutter overrides the automatic selection, and a compression budget
        exists (without ``max_h_compress`` no compression mechanism can ever
        fire, so the line's scale is always ``1.0``).
    """
    return (
        line.cutter_downsize
        and line.max_cutter_downsizes > 0
        and line.cutter_size is None
        and line.max_h_compress > 0.0
    )


def _label_has_candidates(label: ResolvedLabel) -> bool:
    """Return whether any text line of ``label`` is eligible for reduction."""
    return any(_is_eligible(line) for line in label.content)


def _measure_scale(
    probe: ScaleProbe,
    label: ResolvedLabel,
    line_index: int,
) -> float:
    """Measure one line's effective horizontal scale on a rendered label.

    Args:
        probe: The render probe.
        label: The label to render and measure.
        line_index: Index of the line to measure.

    Returns:
        The effective scale, ``1.0`` when the line rendered at natural width.
    """
    return probe(label).get(line_index, 1.0)


def _reduce_line(
    label: ResolvedLabel,
    line_index: int,
    probe: ScaleProbe,
    inventory: Sequence[float],
    initial_scale: float,
    forced_cutter: Optional[float] = None,
) -> Tuple[ResolvedLabel, Optional[Tuple[float, float]]]:
    """Walk one line down the cutter ladder as far as its compression justifies.

    Args:
        label: The label owning the line (already rendered; the caller passes
            the render's ``source_label`` so collision-avoidance adjustments
            are preserved).
        line_index: Index of the line to reduce.
        probe: The render probe used to re-measure after each step.
        inventory: Shop cutter diameters in inches (non-empty).
        initial_scale: The line's effective scale on ``label``.
        forced_cutter: Optional shared (global) cutter for the first step,
            applied directly without the midpoint trigger check (the
            ``cutter_downsize_global`` contract: a sibling's trigger is this
            line's trigger). The step consumes one unit of the line's budget
            and the toolpath floor still applies; the remaining steps follow
            the normal midpoint rule. ``None`` = trigger-driven reduction only.

    Returns:
        ``(label, (original, final))`` when at least one step was applied,
        otherwise ``(label, None)``. ``label`` is the (possibly cloned) label
        whose geometry matches the returned measurements.
    """
    line = label.content[line_index]
    working = label
    current_cutter = line.cutter_diameter
    original_cutter = current_cutter
    scale = initial_scale
    steps = 0

    while steps < working.content[line_index].max_cutter_downsizes:
        is_seed = steps == 0 and forced_cutter is not None
        if is_seed:
            assert forced_cutter is not None  # narrowed by is_seed; for type checkers.
            if forced_cutter >= current_cutter - _CUTTER_TOLERANCE:
                break  # Already at (or below) the shared tool: nothing to share.
            candidate = forced_cutter
        else:
            next_candidate = next_smaller_cutter(current_cutter, list(inventory))
            if next_candidate is None:
                break  # Already the smallest tool the shop owns.
            if not should_downsize_cutter(scale, current_cutter, next_candidate):
                break  # Closer to 100% width than to the smaller tool.
            candidate = next_candidate
        toolpath_height = working.content[line_index].nominal_text_height - candidate
        if toolpath_height <= 0.0:
            break  # The smaller tool leaves nothing to engrave at this height.

        working = _with_cutter(working, line_index, candidate, toolpath_height)
        if is_seed:
            logger.warning(
                "Label %s line %d (%r): sharing the label's cutter downsize, "
                "swapping the automatic cutter from %.3fin to %.3fin "
                "(toolpath height %.3fin).",
                working.id,
                line_index,
                working.content[line_index].text,
                current_cutter,
                candidate,
                toolpath_height,
            )
        else:
            logger.warning(
                "Label %s line %d (%r): compressed to %.1f%% width, downsizing the "
                "automatic cutter from %.3fin to %.3fin (toolpath height %.3fin).",
                working.id,
                line_index,
                working.content[line_index].text,
                scale * 100.0,
                current_cutter,
                candidate,
                toolpath_height,
            )
        current_cutter = candidate
        steps += 1

        if steps < working.content[line_index].max_cutter_downsizes:
            # A smaller cutter renders wider: re-measure to see whether the
            # deeper compression justifies a further step.
            scale = _measure_scale(probe, working, line_index)

    if steps == 0:
        return label, None
    return working, (original_cutter, current_cutter)


def _height_groups(content: Sequence[ResolvedTextLine]) -> List[List[int]]:
    """Bucket line indices by nominal text height (content order preserved).

    Args:
        content: The label's resolved text lines.

    Returns:
        Lists of line indices whose ``nominal_text_height`` matches within
        ``_HEIGHT_TOLERANCE``, in first-appearance order.
    """
    groups: List[Tuple[float, List[int]]] = []
    for index, line in enumerate(content):
        for key, members in groups:
            if math.isclose(
                key,
                line.nominal_text_height,
                rel_tol=_HEIGHT_TOLERANCE,
                abs_tol=_HEIGHT_TOLERANCE,
            ):
                members.append(index)
                break
        else:
            groups.append((line.nominal_text_height, [index]))
    return [members for _, members in groups]


def _share_downsizes(
    label: ResolvedLabel,
    downsized: Dict[int, Tuple[float, float]],
    probe: ScaleProbe,
    inventory: Sequence[float],
) -> Tuple[ResolvedLabel, Dict[int, Tuple[float, float]]]:
    """Propagate per-line cutter downsizes to same-height siblings (per label).

    Implements ``cutter_downsize_global``: every height group containing a
    trigger line (a line that downsized and shares) converges to the group's
    smallest final cutter. Receivers get the shared cutter applied directly
    (no midpoint check), then run their own one-way step loop, so a receiver
    whose compression deepens below the shared tool lowers the group minimum
    and the pass repeats. Termination: every step strictly decreases some
    line's cutter on a finite ladder within each line's step budget.

    Args:
        label: The label whose trigger downsizes are already applied.
        downsized: Trigger deltas (line index -> (original, final)); mutated
            copy returned, never the caller's dict.
        probe: The render probe used by the receivers' step loops.
        inventory: Shop cutter diameters in inches (non-empty).

    Returns:
        ``(label, downsized)`` with propagated swaps folded in. ``label`` is
        the (possibly cloned) label matching the returned deltas; untouched
        labels are returned as the same object.
    """
    if not any(label.content[index].cutter_downsize_global for index in downsized):
        return label, downsized

    working = label
    result = dict(downsized)
    while True:
        propagated = False
        for members in _height_groups(working.content):
            shares = any(
                index in result and working.content[index].cutter_downsize_global
                for index in members
            )
            if not shares:
                continue
            # Only mechanism-eligible *sharing* lines join the group:
            # explicit-cutter, budget-less, cutter_downsize_global-false and
            # cutter_downsize-false lines keep their tool and never set (nor
            # follow) the group minimum.
            eligible = [
                index
                for index in members
                if working.content[index].cutter_downsize_global
                and _is_eligible(working.content[index])
            ]
            if len(eligible) < 2:
                continue
            group_min = min(working.content[index].cutter_diameter for index in eligible)
            for index in eligible:
                line = working.content[index]
                if line.cutter_diameter <= group_min + _CUTTER_TOLERANCE:
                    continue  # Already at (or below) the shared tool.
                working, delta = _reduce_line(
                    working,
                    index,
                    probe,
                    inventory,
                    _measure_scale(probe, working, index),
                    forced_cutter=group_min,
                )
                if delta is not None:
                    original = result[index][0] if index in result else delta[0]
                    result[index] = (original, delta[1])
                    propagated = True
        if not propagated:
            break
    return working, result


def _with_cutter(
    label: ResolvedLabel,
    line_index: int,
    cutter_diameter: float,
    toolpath_text_height: float,
) -> ResolvedLabel:
    """Clone ``label`` with one line's cutter and toolpath height replaced.

    The nominal text height is deliberately untouched: it stays the user's
    intent, exactly like the explicit ``cutter_size`` contract, so vertical
    fit math keeps using the requested height.

    Args:
        label: The label to clone.
        line_index: Index of the line to change.
        cutter_diameter: The reduced cutter diameter in inches.
        toolpath_text_height: The recomputed toolpath height in inches.

    Returns:
        A new label whose ``content`` carries the modified line.
    """
    content = list(label.content)
    content[line_index] = dataclasses.replace(
        content[line_index],
        cutter_diameter=cutter_diameter,
        toolpath_text_height=toolpath_text_height,
    )
    return dataclasses.replace(label, content=content)


def apply_compression_cutter_downsize(
    resolved_labels: Sequence[ResolvedLabel],
    available_cutters: Optional[Sequence[float]],
    *,
    probe: Optional[ScaleProbe] = None,
) -> List[ResolvedLabel]:
    """Reduce automatic cutters on text lines that had to be compressed.

    Runs before the pen map is built so the reduced cutter flows into the
    per-cutter layers, the PLT file names, the kerning clearance, the
    collision stroke floor and the plate-space routing. Labels that need no
    reduction are returned as the *same objects*, so callers can pass the
    result straight through and untouched labels keep their identity (and
    their downstream render-cache hits).

    When a downsized line has ``cutter_downsize_global`` enabled, the swap is
    shared with the label's other eligible same-height lines (see
    :func:`_share_downsizes`) before the label is returned.

    Args:
        resolved_labels: The job's resolved labels, in content order.
        available_cutters: Shop cutter diameters in inches (``tools.json``
            ``available_cutters``). ``None``/empty disables the mechanism --
            there is no ladder to walk down.
        probe: Optional render probe returning a label's effective per-line
            horizontal scales. Defaults to rendering through
            :func:`plt_optimizer.generate.label_renderer.render_label_to_plt`;
            injectable for tests.

    Returns:
        A list of resolved labels, with downsized clones substituted for the
        labels whose automatic cutters were reduced.
    """
    if not available_cutters:
        return list(resolved_labels)
    if not any(_label_has_candidates(label) for label in resolved_labels):
        return list(resolved_labels)

    scale_probe: ScaleProbe = probe if probe is not None else _default_probe
    inventory = sorted(set(available_cutters))

    result: List[ResolvedLabel] = []
    # One measurement render per unique label id (labels with count > 1 render
    # once downstream too), reused across every step of every line.
    measured: Dict[str, Dict[int, float]] = {}
    for label in resolved_labels:
        if not _label_has_candidates(label):
            result.append(label)
            continue

        scales = measured.get(label.id)
        if scales is None:
            scales = scale_probe(label)
            measured[label.id] = scales
        compressed = {index: scale for index, scale in scales.items() if scale < 1.0}
        if not compressed:
            result.append(label)
            continue

        working = label
        downsized: Dict[int, Tuple[float, float]] = {}
        for line_index, initial_scale in sorted(compressed.items()):
            if line_index >= len(working.content) or not _is_eligible(working.content[line_index]):
                continue
            working, delta = _reduce_line(
                working,
                line_index,
                scale_probe,
                inventory,
                initial_scale,
            )
            if delta is not None:
                downsized[line_index] = delta

        if not downsized:
            result.append(label)
            continue
        working, downsized = _share_downsizes(working, downsized, scale_probe, inventory)
        result.append(dataclasses.replace(working, cutter_downsize_by_line=downsized))
    return result
