"""Word-level line-content reflow (pre-pass).

Paragraph-form labels (warnings, instructions, data plates) rarely care
*which* words land on *which* line -- only that the text reads in order and
fills the label evenly. Authors break lines by hand, so one long line forces
horizontal compression (or overflows) while its short siblings engrave at
natural width.

This module closes that gap as an **export pre-pass**: for every maximal run
of consecutive text lines that enable ``optimize_line_content``, collect the
group's words in order, repartition them into the same number of lines so the
lines' *natural rendered widths* are as equal as possible, and emit a
``ResolvedLabel`` clone carrying the reflowed text. Word order is never
changed -- only the line breaks move, exactly like LaTeX filling a
paragraph. Equalizing the **pre-compression** widths is the same objective as
minimizing the compression the label would otherwise need.

The repartition is the classic linear-partition problem solved exactly by
dynamic programming: the concatenated word sequence is cut into one
contiguous segment per group line (every line keeps at least one word),
minimizing the widest segment's rendered width, tie-broken by the sum of
squared widths (preferring the most balanced partition). Segment widths use
each line's own typography: word widths are measured by rendering the word
alone through the same renderer the label render path uses, and a space
advances a per-typography constant (measured once per line when its own text
has two words, else the ``space_width_fraction * text_height +
character_spacing`` formula) -- kerning never crosses a word boundary, so the
sum is exact for PLT fonts and matches matplotlib shaping for TTF.

Runs **before** the cutter-reduction and shared-compression pre-passes (see
:mod:`plt_optimizer.generate.cutter_downsize` and
:mod:`plt_optimizer.generate.h_compress`), so those see the reflowed text and
measure compression on the geometry that is actually engraved.

Scope guards (the pass is a no-op, and costs nothing, when any of these
apply):

- no label has a line with ``optimize_line_content`` enabled;
- every enabled run is a single line without a line cap (nothing to exchange
  words with, and nothing to grow into);
- a run contains a blank line, or fewer words than lines (every line must
  keep at least one word).

Growth cap (``optimize_line_content_max_lines``)
------------------------------------------------

A group whose lines set the cap to ``N`` may **gain** lines: the repartition
starts at the group's existing line count ``G`` (which is exactly the
full-group pass above) and, while the group's lines still need horizontal
compression, grows by one line -- inserting a clone of the group's own
typography after the group's last line -- up to ``min(N, word_count)``. The
loop stops at the first line count whose rendered group needs no compression,
logging INFO; a group that is still compressed at the ceiling keeps its
widest layout and logs a WARNING.

The option is **growth-only**: it never removes, blanks or collapses a line,
so a label's line count never decreases. ``N <= G`` leaves the cap inert (the
group simply runs the full-group pass). Detecting "needs compression" renders
the candidate label through the injectable :data:`ScaleProbe` (default:
:func:`plt_optimizer.generate.label_renderer.render_label_to_plt`), so a job
without a cap never pays for a render -- the probe is never called.

Example:
    >>> from plt_optimizer.generate.line_content import apply_line_content_reflow
    >>> labels = apply_line_content_reflow(labels)
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from plt_optimizer.generate.cutter_downsize import ScaleProbe
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine

logger = logging.getLogger(__name__)

# Signature of the probe measuring one resolved text line's natural rendered
# width in inches (pre-compression, at the line's own typography). Injectable
# so the solver is unit-testable without matplotlib.
LineWidthProbe = Callable[[ResolvedTextLine], float]

# Width comparison tolerance: partitions within this of each other count as
# equal, so float noise cannot flip the chosen cut points.
_WIDTH_TOLERANCE: float = 1e-12

# Effective-scale comparison tolerance: a scale within this of ``1.0`` counts
# as uncompressed (matches the shared-compression pre-pass tolerance, so the
# two pre-passes agree on what "compressed" means).
_SCALE_TOLERANCE: float = 1e-9

# Cost of one partition: lexicographic (widest line, sum of squared widths).
# Minimizing the widest line minimizes the compression the label needs;
# minimizing the squared sum among ties prefers the most even fill.
_Cost = Tuple[float, float]


def _default_probe(line: ResolvedTextLine) -> float:
    """Render ``line`` alone and return its natural width in inches.

    Imported lazily so this module stays importable on the Python 3.8 /
    Windows 7 path where matplotlib is unavailable (nothing in the
    ``watch``/``optimize`` commands imports it, and the lazy import keeps that
    guarantee even if a future caller does).

    Args:
        line: The resolved text line to measure.

    Returns:
        The rendered width in inches (``0.0`` for a line with no geometry).
    """
    from plt_optimizer.generate.label_renderer import measure_line_natural_width

    return measure_line_natural_width(line)


def _default_scale_probe(label: ResolvedLabel) -> Dict[int, float]:
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


def _reflow_groups(content: Sequence[ResolvedTextLine]) -> List[List[int]]:
    """Split ``content`` into maximal runs of consecutive enabled line indices.

    Args:
        content: The label's resolved text lines.

    Returns:
        One list of line indices per maximal run of consecutive lines with
        ``optimize_line_content`` enabled. Runs of length 1 are included; the
        caller skips them (a lone line has no sibling to exchange words with).
    """
    groups: List[List[int]] = []
    current: List[int] = []
    for index, line in enumerate(content):
        if line.optimize_line_content:
            current.append(index)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _partition_cost(
    widths: Sequence[float],
    space_advance: float,
    start: int,
    end: int,
) -> float:
    """Rendered width of words ``[start, end)`` joined on one line.

    Args:
        widths: Per-word rendered widths (same order as the word list).
        space_advance: The line typography's space advance in inches.
        start: Index of the segment's first word (inclusive).
        end: Index after the segment's last word (exclusive).

    Returns:
        The segment's natural width: word widths plus one space advance per
        interior junction (kerning never crosses a word boundary).
    """
    total = sum(widths[start:end])
    count = end - start
    if count > 1:
        return total + (count - 1) * space_advance
    return total


def _combine(cost: _Cost, width: float) -> _Cost:
    """Extend a partial partition cost with one more line of ``width``.

    Args:
        cost: The cost of the lines assigned so far.
        width: The natural width of the newly assigned line.

    Returns:
        ``(max(widest, width), sum_of_squares + width**2)``.
    """
    return (max(cost[0], width), cost[1] + width * width)


def _partition_words(
    line_widths: Sequence[Sequence[float]],
    space_advances: Sequence[float],
) -> List[Tuple[int, int]]:
    """Cut the concatenated word sequence into one segment per line.

    Dynamic program over ``(word_count, line_count)``: ``dp[i][m]`` is the
    lexicographically best cost of assigning the first ``i`` words to the
    first ``m`` lines, each line receiving at least one word. Segment widths
    are measured with each line's own typography (``line_widths[m]`` holds the
    widths of *every* word as rendered on line ``m``).

    Args:
        line_widths: ``line_widths[m][k]`` is word ``k``'s rendered width on
            line ``m`` (word order is shared across lines; the global word
            list is the concatenation of the lines' words in order).
        space_advances: ``space_advances[m]`` is line ``m``'s space advance.

    Returns:
        One ``(start, end)`` word-index pair per line (``end`` exclusive),
        covering the whole word list in order. Empty input yields no segments.
    """
    line_count = len(line_widths)
    word_count = len(line_widths[0]) if line_count else 0
    if line_count == 0 or word_count == 0:
        return []
    if word_count < line_count:
        return []  # Infeasible: every line must keep at least one word.

    inf = float("inf")
    unreach: _Cost = (inf, inf)
    # dp[i][m]: best cost for the first i words on the first m lines.
    dp: List[List[_Cost]] = [[unreach] * (line_count + 1) for _ in range(word_count + 1)]
    cut: List[List[int]] = [[0] * (line_count + 1) for _ in range(word_count + 1)]
    dp[0][0] = (0.0, 0.0)

    for m in range(1, line_count + 1):
        widths_m = line_widths[m - 1]
        space_m = space_advances[m - 1]
        for i in range(m, word_count + 1):
            best: _Cost = unreach
            best_cut = 0
            for j in range(m - 1, i):
                prev = dp[j][m - 1]
                if prev[0] == inf:
                    continue
                width = _partition_cost(widths_m, space_m, j, i)
                candidate = _combine(prev, width)
                if candidate[0] < best[0] - _WIDTH_TOLERANCE or (
                    abs(candidate[0] - best[0]) <= _WIDTH_TOLERANCE
                    and candidate[1] < best[1] - _WIDTH_TOLERANCE
                ):
                    best = candidate
                    best_cut = j
            dp[i][m] = best
            cut[i][m] = best_cut

    # Walk the parents back from (word_count, line_count).
    segments: List[Tuple[int, int]] = []
    i, m = word_count, line_count
    while m > 0:
        j = cut[i][m]
        segments.append((j, i))
        i, m = j, m - 1
    segments.reverse()
    return segments


def _measure_space_advance(
    line: ResolvedTextLine,
    words: Sequence[str],
    probe: LineWidthProbe,
    cache: Dict[Tuple[int, str], float],
    line_index: int,
) -> float:
    """Return ``line``'s space advance, measured when its text allows.

    When the line's own text has at least two words, the advance is measured
    by rendering the first two words joined and subtracting their standalone
    widths (exact for PLT fonts, matches matplotlib shaping for TTF).
    Otherwise the documented PLT formula
    ``space_width_fraction * toolpath_text_height + character_spacing``
    applies.

    Args:
        line: The resolved line providing the typography.
        words: The line's whitespace-split words.
        probe: The line-width probe.
        cache: Shared word-width cache (mutated for the pair measurement).
        line_index: Index of the line (cache key).

    Returns:
        The space advance in inches.
    """
    if len(words) >= 2:
        pair = f"{words[0]} {words[1]}"
        pair_key = (line_index, pair)
        pair_width = cache.get(pair_key)
        if pair_width is None:
            pair_width = probe(dataclasses.replace(line, text=pair))
            cache[pair_key] = pair_width
        first = cache[(line_index, words[0])]
        second = cache[(line_index, words[1])]
        return max(pair_width - first - second, 0.0)
    return line.space_width_fraction * line.toolpath_text_height + line.character_spacing


def _measure_typography(
    line: ResolvedTextLine,
    cache_index: int,
    words: Sequence[str],
    own_words: Sequence[str],
    probe: LineWidthProbe,
    cache: Dict[Tuple[int, str], float],
) -> Tuple[List[float], float]:
    """Measure ``line``'s typography: every word's width plus the space advance.

    Args:
        line: The resolved line providing the typography.
        cache_index: Index identifying this typography in the shared cache.
            Inserted (grown) lines pass their donor line's index, because
            they clone its typography exactly and the measurements coincide.
        words: The words to measure.
        own_words: The line's own whitespace-split words, used to measure the
            space advance from its own text when it has two of them.
        probe: The line-width probe.
        cache: Shared word-width cache (mutated).

    Returns:
        ``(word_widths, space_advance)`` for this typography.
    """
    widths: List[float] = []
    for word in words:
        key = (cache_index, word)
        width = cache.get(key)
        if width is None:
            width = probe(dataclasses.replace(line, text=word))
            cache[key] = width
        widths.append(width)
    space_advance = _measure_space_advance(line, own_words, probe, cache, cache_index)
    return widths, space_advance


def _group_cap(lines: Sequence[ResolvedTextLine]) -> Optional[int]:
    """Return the line cap governing a group (resolution fans it out).

    Args:
        lines: The group's resolved lines.

    Returns:
        The cap declared on the group (its first carrying line), else ``None``.
    """
    for line in lines:
        if line.optimize_line_content_max_lines is not None:
            return line.optimize_line_content_max_lines
    return None


def _group_layout(
    content: Sequence[ResolvedTextLine],
    group: Sequence[int],
    segments: Sequence[Tuple[int, int]],
    all_words: Sequence[str],
) -> List[ResolvedTextLine]:
    """Build the label content realising one group's partition.

    Args:
        content: The label's current content (earlier groups already applied).
        group: The group's line indices in ``content``.
        segments: One ``(start, end)`` word-index pair per layout line
            (``len(segments) >= len(group)``; the surplus is inserted).
        all_words: The group's concatenated word list.

    Returns:
        A new content list: the group's existing lines carry the leading
        segments and any surplus segments become clone lines inserted after
        the group's last line (the group grows downward, its start anchored).
        Lines outside the group keep their object identity.
    """
    donor = content[group[-1]]
    layout = list(content)
    for position, (start, end) in enumerate(segments):
        text = " ".join(all_words[start:end])
        if position < len(group):
            index = group[position]
            line = content[index]
            layout[index] = line if line.text == text else dataclasses.replace(line, text=text)
        else:
            layout.insert(
                group[-1] + 1 + (position - len(group)),
                dataclasses.replace(donor, text=text),
            )
    return layout


def _group_is_compressed(scales: Dict[int, float], group_start: int, line_count: int) -> bool:
    """Return whether any line of a laid-out group needs horizontal compression.

    Args:
        scales: The candidate label's effective per-line scales.
        group_start: Index of the group's first line in the candidate.
        line_count: Number of lines the group occupies (``M``).

    Returns:
        ``True`` when a group line's scale falls below ``1 - tolerance``.
    """
    return any(
        scale < 1.0 - _SCALE_TOLERANCE
        for index, scale in scales.items()
        if group_start <= index < group_start + line_count
    )


def _reflow_label(
    label: ResolvedLabel,
    probe: LineWidthProbe,
    scale_probe: Optional[ScaleProbe] = None,
) -> Optional[ResolvedLabel]:
    """Reflow every enabled group of ``label``; clone when text changed.

    A group without a line cap runs the single full-group repartition and
    never touches the scale probe. A capped group grows (see the module
    docstring) through the M = G..N loop, which is the only path that renders
    candidates.

    Args:
        label: The label to reflow.
        probe: The line-width probe.
        scale_probe: Optional label-level compression probe used by the growth
            loop. Defaults to rendering through
            :func:`plt_optimizer.generate.label_renderer.render_label_to_plt`;
            injectable for tests. Never called for uncapped groups.

    Returns:
        A cloned label carrying the reflowed content when any group changed,
        otherwise ``None`` (the label is left untouched).
    """
    groups: List[List[int]] = []
    for group in _reflow_groups(label.content):
        cap = _group_cap([label.content[index] for index in group])
        if len(group) >= 2 or (cap is not None and cap > 1):
            groups.append(group)
    if not groups:
        return None

    new_content: List[ResolvedTextLine] = list(label.content)
    changed = False
    cache: Dict[Tuple[int, str], float] = {}
    shift = 0  # Lines inserted by earlier groups; shifts later group indices.

    for group in groups:
        offset_group = [index + shift for index in group]
        lines = [new_content[index] for index in offset_group]
        per_line_words = [line.text.split() for line in lines]
        if any(not words for words in per_line_words):
            logger.debug(
                "Label %s: skipping line-content reflow for lines %s-%s (a line has no words).",
                label.id,
                offset_group[0],
                offset_group[-1],
            )
            continue
        all_words = [word for words in per_line_words for word in words]
        if len(all_words) < len(offset_group):
            continue  # Cannot keep every line non-empty; leave the group alone.

        cap = _group_cap(lines)
        group_size = len(offset_group)
        ceiling = min(cap, len(all_words)) if cap is not None else group_size
        if cap is not None and cap <= group_size:
            logger.debug(
                "Label %s: line-content cap %d leaves lines %s-%s unchanged (no growth room).",
                label.id,
                cap,
                offset_group[0],
                offset_group[-1],
            )
            ceiling = group_size
        # Without a compression budget the renderer can never compress, so the
        # growth loop's probe could not report progress; skip the renders.
        budget = any(line.max_h_compress > 0.0 for line in lines)
        probing = cap is not None and cap > group_size and budget
        if cap is not None and cap > group_size and not budget:
            logger.debug(
                "Label %s: line-content cap %d on lines %s-%s is inert "
                "(no max_h_compress budget to measure).",
                label.id,
                cap,
                offset_group[0],
                offset_group[-1],
            )

        donor = lines[-1]
        donor_words = donor.text.split()
        line_widths: List[List[float]] = []
        space_advances: List[float] = []
        accepted: Optional[List[ResolvedTextLine]] = None
        accepted_m = group_size
        accepted_compressed = False

        for m in range(group_size, ceiling + 1):
            while len(line_widths) < m:
                position = len(line_widths)
                if position < group_size:
                    line = lines[position]
                    cache_index = offset_group[position]
                    words = per_line_words[position]
                else:  # Grown line: clones the group's last line typography.
                    line = donor
                    cache_index = offset_group[-1]
                    words = donor_words
                widths, space_advance = _measure_typography(
                    line, cache_index, all_words, words, probe, cache
                )
                line_widths.append(widths)
                space_advances.append(space_advance)

            segments = _partition_words(line_widths[:m], space_advances[:m])
            if not segments:
                break  # Infeasible (fewer words than lines); keep prior layout.
            candidate = _group_layout(new_content, offset_group, segments, all_words)
            accepted = candidate
            accepted_m = m
            if not probing:
                break  # Uncapped (or inert cap): the full-group pass, no renders.
            scales = (scale_probe or _default_scale_probe)(
                dataclasses.replace(label, content=candidate)
            )
            accepted_compressed = _group_is_compressed(scales, offset_group[0], m)
            if not accepted_compressed:
                break  # This line count engraves without compression; stop growing.

        if accepted is None:
            continue
        if accepted_m > group_size:
            logger.info(
                "Label %s: line-content reflow grew lines %s-%s to %d of %d lines.",
                label.id,
                offset_group[0],
                offset_group[-1],
                accepted_m,
                cap,
            )
        if accepted_m == ceiling and accepted_compressed:
            logger.warning(
                "Label %s: lines %s-%s still need horizontal compression at "
                "the optimize_line_content_max_lines cap (%d lines); keeping "
                "the widest layout.",
                label.id,
                offset_group[0],
                offset_group[-1],
                accepted_m,
            )
        for index, line in enumerate(accepted):
            previous = new_content[index] if index < len(new_content) else None
            if line is previous:
                continue
            logger.info(
                "Label %s: line-content reflow updated line %d: %r -> %r.",
                label.id,
                index,
                previous.text if previous is not None else None,
                line.text,
            )
            changed = True
        new_content = accepted
        shift += accepted_m - group_size

    if not changed:
        return None
    return dataclasses.replace(label, content=new_content)


def apply_line_content_reflow(
    resolved_labels: Sequence[ResolvedLabel],
    *,
    probe: Optional[LineWidthProbe] = None,
    scale_probe: Optional[ScaleProbe] = None,
) -> List[ResolvedLabel]:
    """Reflow word-level line content across consecutive enabled lines.

    Runs before the cutter-reduction and shared-compression pre-passes so the
    reflowed text flows into the rendered geometry, the measured compression
    scales and the PLT output. Labels that need no reflow are returned as the
    *same objects*, so callers can pass the result straight through and
    untouched labels keep their identity (and their downstream render-cache
    hits).

    Args:
        resolved_labels: The job's resolved labels, in content order.
        probe: Optional probe returning a resolved line's natural rendered
            width in inches. Defaults to rendering through
            :func:`plt_optimizer.generate.label_renderer.measure_line_natural_width`;
            injectable for tests.
        scale_probe: Optional probe returning a label's effective per-line
            horizontal scales, used only by the ``optimize_line_content_max_lines``
            growth loop to decide whether a group needs more lines. Defaults
            to rendering through
            :func:`plt_optimizer.generate.label_renderer.render_label_to_plt`;
            injectable for tests. Never called for jobs without a line cap.

    Returns:
        A list of resolved labels, with reflowed clones substituted for the
        labels whose line breaks moved.
    """
    if not any(line.optimize_line_content for label in resolved_labels for line in label.content):
        return list(resolved_labels)

    width_probe: LineWidthProbe = probe if probe is not None else _default_probe

    result: List[ResolvedLabel] = []
    for label in resolved_labels:
        if not any(line.optimize_line_content for line in label.content):
            result.append(label)
            continue
        clone = _reflow_label(label, width_probe, scale_probe)
        result.append(clone if clone is not None else label)
    return result
