"""Shared optimization pipeline stages for known-kind and profiled documents.

This module centralizes the operational sequence that every PLT-Optimizer
entry point (``optimize`` CLI, ``watch`` daemon, ``benchmark`` tool, and the
generate pipeline's plate-space optimization) performs after a document has
been parsed and classified:

1. :func:`preprocess_document` -- structural documents are fractured into
   independent segments and deduplicated; text documents pass through
   untouched (contiguous paths are preserved).
2. :func:`chunk_document` -- chronological chunking with the production
   ``threshold_multiplier`` of 2.0.
3. :func:`optimize_and_reassemble` -- run the supplied strategy through the
   :class:`~plt_optimizer.core.optimizer.OptimizerEngine`, unwrap Parallel
   Ensemble results (benchmark logging + method naming/notes), and
   reassemble the optimized document.

Callers keep ownership of parsing, profiling (or supplying a known-kind
:class:`~plt_optimizer.core.profiler.ProfileResult` directly), strategy
construction, writing, and metrics logging. This split lets generated
toolpaths skip the parser/profiler entirely while reusing the exact same
optimization semantics as the file-based CLIs.

Python 3.8 / Windows 7 compatible: no matplotlib, no generate-pipeline
imports, and no syntax newer than 3.8 at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from plt_optimizer.core.chunker import Chunker, ChunkerConfig, MacroBlock
from plt_optimizer.core.direction_sweep import sweep_tour_directions
from plt_optimizer.core.glyph_sweep import sweep_glyph_directions
from plt_optimizer.core.intra_chunk_optimizer import IntraChunkResult
from plt_optimizer.core.models import PLTDocument
from plt_optimizer.core.optimizer import (
    OptimizationStrategy,
    OptimizerEngine,
    ParallelEnsembleOptimizationResult,
    StrategyBenchmarkResult,
)
from plt_optimizer.core.path_merger import MergeResult, merge_coincident_paths
from plt_optimizer.core.profiler import ProfileResult
from plt_optimizer.core.reassembler import Reassembler
from plt_optimizer.core.stroke_simplifier import simplify_overlapping_strokes
from plt_optimizer.utils.geometry import fracture_linear_paths
from plt_optimizer.utils.logging import TextLogger

# Production chunking threshold used by every entry point (optimize, watch,
# benchmark, generate). Kept here as the single source of truth.
DEFAULT_THRESHOLD_MULTIPLIER: float = 2.0

# Tolerance used when removing redundant strokes in the structural pipeline.
_REDUNDANCY_TOL: float = 1e-3

# Supporting-line merge tolerance for the structural dedupe pass, in plotter
# units. CAD exports (EngraveLab) emit the same physical line multiple times
# at small perpendicular offsets -- observed up to 6 units (0.006") in
# tests_deps/2026-07-10 SW0914 1230sheet0.plt -- which the strict 1e-3 segment
# tolerance cannot pair. 10 units (0.0098") absorbs that jitter while staying
# far below the smallest genuine parallel-line spacing in the corpus (>=196
# units / 0.19"), so distinct design lines never merge.
_REDUNDANCY_LINE_TOL: float = 10.0

# Method label recorded for non-ensemble (fast-mode style) results. Matches
# the historical ``optimize``/``watch`` CSV method column verbatim.
_FAST_MODE_METHOD_NAME: str = "NearestNeighbor + 2-Opt (Fast Mode)"


@dataclass(frozen=True)
class OptimizationOutcome:
    """Result of :func:`optimize_and_reassemble`.

    Attributes:
        optimized_doc: Reassembled document with strokes in optimized order.
        optimized_distance: Total rapid travel distance after optimization
            (inter-block gaps -- the strategies' own metric).
        method_name: Winning strategy name (ensemble) or the fast-mode label.
        method_notes: Human-readable notes describing per-strategy results,
            formatted for the CSV metrics ``notes`` column.
        ensemble_benchmarks: Per-strategy benchmark results when a Parallel
            Ensemble run occurred; empty tuple otherwise.
        direction_sweep_travel_before: Inter-chunk travel entering the
            direction sweep, or ``None`` when the sweep did not improve the
            tour (or was disabled).
        direction_sweep_travel_after: Inter-chunk travel after the direction
            sweep, or ``None`` when the sweep did not improve the tour.
        direction_sweep_emitted_before: ``rapid_distance()`` (intra + inter) of
            the document the pre-sweep result would have emitted, or ``None``
            when the sweep found no improvement.
        direction_sweep_emitted_after: ``rapid_distance()`` of
            :attr:`optimized_doc`, or ``None`` when the sweep found no
            improvement.
        direction_sweep_passes: Sweeps accepted by the direction sweep.
        direction_sweep_flips: Blocks whose traversal direction changed.
        intra_sweep_travel_before: Intra-chunk (inside-text-chunk) rapid
            travel entering the glyph direction sweep, or ``None`` when the
            sweep did not improve any chunk (or was disabled / had no glyph
            groups).
        intra_sweep_travel_after: Intra-chunk travel after the glyph
            direction sweep, or ``None`` when the sweep did not improve.
        intra_sweep_emitted_before: ``rapid_distance()`` of the emission
            without the glyph sweep, or ``None`` when the sweep did not
            improve.
        intra_sweep_emitted_after: ``rapid_distance()`` of
            :attr:`optimized_doc` including the glyph sweep, or ``None``
            when the sweep did not improve.
        intra_sweep_groups: Glyph groups considered by the sweep.
        intra_sweep_flips: Glyph groups re-traced in reverse.
        merged_paths_before: Segment-bearing paths in the pre-merge emission, or
            ``None`` when the merge was disabled (see ``merge_coincident``).
            When the merge ran and found nothing to do, this equals
            :attr:`merged_paths_after`.
        merged_paths_after: Segment-bearing paths in :attr:`optimized_doc` when
            the merge ran (``None`` when it was disabled).
        merges_applied: Tip-to-tail junctions collapsed by the merge (0 when
            nothing merged or the merge was disabled).
    """

    optimized_doc: PLTDocument
    optimized_distance: float
    method_name: str
    method_notes: str
    ensemble_benchmarks: Tuple[StrategyBenchmarkResult, ...] = field(default=())
    direction_sweep_travel_before: Optional[float] = None
    direction_sweep_travel_after: Optional[float] = None
    direction_sweep_emitted_before: Optional[float] = None
    direction_sweep_emitted_after: Optional[float] = None
    direction_sweep_passes: int = 0
    direction_sweep_flips: int = 0
    intra_sweep_travel_before: Optional[float] = None
    intra_sweep_travel_after: Optional[float] = None
    intra_sweep_emitted_before: Optional[float] = None
    intra_sweep_emitted_after: Optional[float] = None
    intra_sweep_groups: int = 0
    intra_sweep_flips: int = 0
    merged_paths_before: Optional[int] = None
    merged_paths_after: Optional[int] = None
    merges_applied: int = 0


def preprocess_document(
    document: PLTDocument,
    *,
    is_structural: bool,
    fracture_factory: Optional[Callable[[PLTDocument], PLTDocument]] = None,
    dedupe_factory: Optional[Callable[..., PLTDocument]] = None,
    logger: Optional[TextLogger] = None,
    log_prefix: str = "",
) -> PLTDocument:
    """Apply the type-dependent preprocessing bifurcation to a document.

    Structural documents (drill holes, score lines, grids) are fractured so
    every linear segment becomes an independently routable path, then
    overlapping coincident strokes are removed. Text documents are returned
    unchanged: simplification would destroy contiguous glyph paths.

    Args:
        document: Parsed document to preprocess.
        is_structural: Whether the document was classified (or declared) as
            structural content.
        fracture_factory: Optional replacement for
            :func:`plt_optimizer.utils.geometry.fracture_linear_paths`
            (test seam; defaults to the production implementation).
        dedupe_factory: Optional replacement for
            :func:`plt_optimizer.core.stroke_simplifier.simplify_overlapping_strokes`
            (test seam; defaults to the production implementation).
        logger: Optional text logger for DEBUG diagnostics.
        log_prefix: Prefix prepended to log messages (e.g. ``"[job123]"``).

    Returns:
        A preprocessed document (new object for structural input, the
        original object for text input).
    """
    prefix = f"{log_prefix} " if log_prefix else ""
    fracture = fracture_factory or fracture_linear_paths
    dedupe = dedupe_factory or simplify_overlapping_strokes

    if is_structural:
        # STRUCTURAL PIPELINE: fracture linear paths into independent
        # segments, then split overlapping collinear strokes at each other's
        # endpoints and cull the duplicated atomic pieces.
        fractured = fracture(document)
        if logger is not None:
            logger.debug(
                f"{prefix}Fractured structural document (linear paths -> independent segments)"
            )
        deduplicated = dedupe(fractured, tol=_REDUNDANCY_TOL, line_tol=_REDUNDANCY_LINE_TOL)
        if logger is not None:
            logger.debug(
                f"{prefix}Simplified overlapping strokes "
                f"({fractured.total_segments} -> {deduplicated.total_segments} segments)"
            )
        return deduplicated

    # TEXT PIPELINE: skip stroke simplification to preserve contiguous paths.
    if logger is not None:
        logger.debug(f"{prefix}Skipped stroke simplification for text document")
    return document


def chunk_document(
    document: PLTDocument,
    profile_result: ProfileResult,
    *,
    chunker_factory: Optional[Callable[..., Chunker]] = None,
    threshold_multiplier: float = DEFAULT_THRESHOLD_MULTIPLIER,
) -> List[MacroBlock]:
    """Chunk a (preprocessed) document into MacroBlocks.

    Args:
        document: Document whose stroke paths should be grouped.
        profile_result: Profiling output supplying ``baseline_extent`` and
            ``is_structural``. Callers that know the content kind (e.g. the
            generate pipeline) may construct this directly and skip the
            :class:`~plt_optimizer.core.profiler.Profiler` entirely.
        chunker_factory: Optional :class:`Chunker` factory (test seam;
            defaults to the production class).
        threshold_multiplier: Chunker jump-distance multiplier.

    Returns:
        List of MacroBlocks in chronological order.

    Raises:
        ChunkerError: If no valid blocks can be created (propagated from
            :meth:`plt_optimizer.core.chunker.Chunker.chunk`).
    """
    factory = chunker_factory or Chunker
    chunker = factory(config=ChunkerConfig(threshold_multiplier=threshold_multiplier))
    return chunker.chunk(
        document.stroke_paths,
        profile_result.baseline_extent,
        is_structural=profile_result.is_structural,
    )


@dataclass(frozen=True)
class _IntraSweepReport:
    """Measurements taken when the glyph direction sweep improved a chunk.

    Attributes:
        travel_before: Intra-chunk rapid travel of the chronological
            (all-forward) traversal, summed over swept blocks.
        travel_after: Intra-chunk rapid travel of the swept traversal.
        emitted_before: ``rapid_distance()`` of the emission without the
            glyph sweep.
        groups: Glyph groups considered.
        flips: Glyph groups re-traced in reverse.
        gain_percent: Percentage improvement on the intra-chunk objective.
    """

    travel_before: float
    travel_after: float
    emitted_before: float
    groups: int
    flips: int
    gain_percent: float


def _run_intra_sweep(
    blocks: Sequence[MacroBlock],
    glyph_groups_by_block: Mapping[int, Tuple[Tuple[int, ...], ...]],
) -> Tuple[List[Optional[IntraChunkResult]], float, float, int, int]:
    """Sweep every block's glyph directions once.

    Args:
        blocks: The blocks in positional order (the order the Reassembler
            expects ``intra_chunk_results`` in).
        glyph_groups_by_block: Per-glyph path groups keyed by ``block_id``.
            Blocks absent from the map keep their chronological traversal.

    Returns:
        ``(results, travel_before, travel_after, groups, flips)`` where
        ``results[i]`` is block ``i``'s :class:`IntraChunkResult` (``None``
        keeps the block's chronological order), and the travel totals are
        the intra-chunk rapid travel before (chronological) and after
        (swept), summed over the swept blocks.
    """
    results: List[Optional[IntraChunkResult]] = []
    travel_before = 0.0
    travel_after = 0.0
    groups = 0
    flips = 0
    for block in blocks:
        groups_for_block = glyph_groups_by_block.get(block.block_id)
        if not groups_for_block:
            results.append(None)
            continue
        sweep = sweep_glyph_directions(block.paths, groups_for_block)
        groups += sweep.groups
        if sweep.flips > 0:
            results.append(sweep.result)
            flips += sweep.flips
            travel_before += sweep.travel_before
            travel_after += sweep.travel_after
        else:
            results.append(None)
    return results, travel_before, travel_after, groups, flips


@dataclass(frozen=True)
class _DirectionSweepReport:
    """Measurements taken when the direction sweep improved the tour.

    Attributes:
        travel_before: Inter-chunk travel entering the sweep.
        travel_after: Inter-chunk travel after the sweep.
        emitted_before: ``rapid_distance()`` of the pre-sweep emission.
        passes: Sweeps accepted by the sweep.
        flips: Blocks whose traversal direction changed.
        gain_percent: Percentage improvement on the inter-chunk objective.
    """

    travel_before: float
    travel_after: float
    emitted_before: float
    passes: int
    flips: int
    gain_percent: float


def optimize_and_reassemble(
    document: PLTDocument,
    blocks: List[MacroBlock],
    strategy: OptimizationStrategy,
    *,
    engine_factory: Optional[Callable[..., OptimizerEngine]] = None,
    reassembler_factory: Optional[Callable[[], Reassembler]] = None,
    logger: Optional[TextLogger] = None,
    log_prefix: str = "",
    direction_sweep: bool = True,
    intra_sweep: bool = True,
    glyph_groups_by_block: Optional[Mapping[int, Tuple[Tuple[int, ...], ...]]] = None,
    merge_coincident: bool = True,
) -> OptimizationOutcome:
    """Optimize pre-built blocks and reassemble the document.

    Runs ``strategy`` through the engine, unwraps Parallel Ensemble results
    (logging the full benchmark table at INFO when a logger is provided),
    and reassembles the optimized document.

    Args:
        document: Preprocessed document the blocks were chunked from.
        blocks: MacroBlocks from :func:`chunk_document` (must be non-empty).
        strategy: Optimization strategy to run.
        engine_factory: Optional :class:`OptimizerEngine` factory invoked as
            ``factory(strategy=strategy)`` (test seam; defaults to the
            production class).
        reassembler_factory: Optional :class:`Reassembler` factory (test
            seam; defaults to the production class).
        logger: Optional text logger for ensemble benchmark reporting.
        log_prefix: Prefix prepended to log messages (e.g. ``"[job123]"``).
        direction_sweep: Run the post-TSP chunk direction sweep on the winning
            result before reassembly (default ``True``). The sweep keeps the
            block order fixed and re-picks each block's traversal direction,
            which is a deterministic, non-increasing improvement on the
            inter-chunk objective. Disable to reproduce pre-sweep output
            exactly (test/escape-hatch seam).
        intra_sweep: Run the intra-chunk glyph direction sweep after the
            inter-chunk routing (default ``True``). Requires
            ``glyph_groups_by_block``; blocks without glyph groups keep
            their chronological traversal. The sweep pins each swept block's
            entrance/exit, so the inter-chunk tour stays exactly valid and
            the emitted rapid travel is non-increasing. Disable to reproduce
            pre-sweep output exactly (test/escape-hatch seam).
        glyph_groups_by_block: Per-glyph path groups keyed by ``block_id``
            (indices into each block's ``paths``), produced by the generate
            pipeline's renderers. ``None`` (the default: parsed PLTs have no
            glyph knowledge) disables the intra sweep entirely, keeping the
            parsed path byte-identical.
        merge_coincident: Stitch tip-to-tail strokes in the reassembled document
            so the redundant tool-up between them disappears (default ``True``).
            The merge is metric-neutral -- it removes PU commands, paths, and
            bytes, never rapid travel -- and preserves the undirected segment
            multiset. Disable to reproduce the pre-merge emission exactly
            (test/escape-hatch seam).

    Returns:
        An :class:`OptimizationOutcome` with the reassembled document and
        method metadata for metrics logging.

    Raises:
        OptimizationError: If the strategy fails (propagated from the engine).
        ReassemblerError: If reassembly fails (propagated from Reassembler).
    """
    prefix = f"{log_prefix} " if log_prefix else ""

    engine = (engine_factory or OptimizerEngine)(strategy=strategy)
    optimization_result = engine.optimize(blocks)

    ensemble_benchmarks: Tuple[StrategyBenchmarkResult, ...] = ()

    if isinstance(optimization_result, ParallelEnsembleOptimizationResult):
        ensemble_result = optimization_result
        method_name = ensemble_result.winner_name
        optimized_distance = ensemble_result.result.total_travel_distance
        result_for_reassembly = ensemble_result.result
        ensemble_benchmarks = tuple(ensemble_result.all_benchmarks)

        # Log all strategy results at INFO level.
        if logger is not None:
            logger.info(f"{prefix}Strategy benchmark results:")
            for bench in ensemble_benchmarks:
                imp_str = (
                    f"{bench.improvement_percent:.2f}% improvement"
                    if bench.improvement_percent is not None
                    else "no baseline comparison"
                )
                logger.info(
                    f"  {bench.strategy_name}: "
                    f"distance={bench.result.total_travel_distance:.3f}, "
                    f"{imp_str} ({bench.execution_time_seconds:.3f}s)"
                )

        # Build notes from all benchmarks.
        notes_parts = []
        for bench in ensemble_benchmarks:
            imp_str = (
                f"{bench.improvement_percent:.2f}%"
                if bench.improvement_percent is not None
                else "N/A"
            )
            notes_parts.append(
                f"{bench.strategy_name}: {bench.result.total_travel_distance:.3f} "
                f"(improvement={imp_str})"
            )
        method_notes = "; ".join(notes_parts)
    else:
        method_name = _FAST_MODE_METHOD_NAME
        optimized_distance = optimization_result.total_travel_distance
        method_notes = f"optimized_distance={optimized_distance:.3f}"
        result_for_reassembly = optimization_result

    reassembler = (reassembler_factory or Reassembler)()

    sweep_report: Optional[_DirectionSweepReport] = None

    if direction_sweep:
        sweep = sweep_tour_directions(blocks, result_for_reassembly)
        if sweep.passes > 0:
            # Emitted travel (intra + inter) is what the plotter actually
            # travels; the sweep's own objective covers inter-chunk gaps only.
            emitted_before = reassembler.reassemble(
                document, blocks, result_for_reassembly
            ).rapid_distance()
            # travel_before > travel_after >= 0 whenever a pass was accepted,
            # so the ratio below can never divide by zero.
            gain = (sweep.travel_before - sweep.travel_after) / sweep.travel_before * 100.0
            sweep_report = _DirectionSweepReport(
                travel_before=sweep.travel_before,
                travel_after=sweep.travel_after,
                emitted_before=emitted_before,
                passes=sweep.passes,
                flips=sweep.flips,
                gain_percent=gain,
            )
            result_for_reassembly = sweep.result
            optimized_distance = sweep.result.total_travel_distance
            method_notes = (
                f"{method_notes}; direction_sweep="
                f"{sweep.travel_before:.3f}->{sweep.travel_after:.3f} "
                f"({sweep.passes} passes, {sweep.flips} flips)"
            )

    # Intra-chunk glyph direction sweep: runs after the inter-chunk routing
    # is final (block order + directions), pins every swept block's
    # entrance/exit, and only re-traces glyphs inside chunks -- so the
    # inter-chunk objective above is untouched and the emitted travel moves
    # by exactly the intra-chunk gain.
    intra_report: Optional[_IntraSweepReport] = None
    intra_results: Optional[List[Optional[IntraChunkResult]]] = None
    if intra_sweep and glyph_groups_by_block:
        results, intra_before, intra_after, intra_groups, intra_flips = _run_intra_sweep(
            blocks, glyph_groups_by_block
        )
        if intra_flips > 0:
            emitted_before_intra = reassembler.reassemble(
                document, blocks, result_for_reassembly
            ).rapid_distance()
            # intra_before > intra_after >= 0 whenever a chunk improved, so
            # the ratio below can never divide by zero.
            intra_gain = (intra_before - intra_after) / intra_before * 100.0
            intra_report = _IntraSweepReport(
                travel_before=intra_before,
                travel_after=intra_after,
                emitted_before=emitted_before_intra,
                groups=intra_groups,
                flips=intra_flips,
                gain_percent=intra_gain,
            )
            intra_results = results
            method_notes = (
                f"{method_notes}; intra_sweep="
                f"{intra_before:.3f}->{intra_after:.3f} "
                f"({intra_flips} flips)"
            )

    optimized_doc = reassembler.reassemble(
        document, blocks, result_for_reassembly, intra_chunk_results=intra_results
    )

    # Coincident-stroke merge: last stage, so the emitted-travel measurements
    # below (and every consumer of optimized_doc) describe what is written.
    # The merge is metric-neutral, so it never disturbs the sweep metrics.
    merge_report: Optional[MergeResult] = None
    if merge_coincident:
        merge_report = merge_coincident_paths(optimized_doc.stroke_paths)
        if merge_report.merges > 0:
            optimized_doc = PLTDocument(
                header_commands=optimized_doc.header_commands,
                stroke_paths=list(merge_report.paths),
                footer_commands=optimized_doc.footer_commands,
            )
            if logger is not None:
                logger.info(
                    f"{prefix}Merged coincident strokes: "
                    f"{merge_report.paths_before} -> {merge_report.paths_after} "
                    f"path(s) ({merge_report.merges} tool-up(s) removed)"
                )
            method_notes = (
                f"{method_notes}; merge="
                f"{merge_report.paths_before}->{merge_report.paths_after} "
                f"({merge_report.merges} merge(s))"
            )

    emitted_after: Optional[float] = None
    if sweep_report is not None:
        emitted_after = optimized_doc.rapid_distance()
        if logger is not None:
            logger.info(
                f"{prefix}Direction sweep: inter-chunk "
                f"{sweep_report.travel_before:.3f} -> {sweep_report.travel_after:.3f} "
                f"({sweep_report.gain_percent:.2f}% improvement, "
                f"{sweep_report.passes} pass(es), {sweep_report.flips} reversal(s)); "
                f"emitted rapid travel {sweep_report.emitted_before:.3f} -> "
                f"{emitted_after:.3f}"
            )

    intra_emitted_after: Optional[float] = None
    if intra_report is not None:
        intra_emitted_after = optimized_doc.rapid_distance()
        if logger is not None:
            logger.info(
                f"{prefix}Intra-chunk glyph sweep: intra "
                f"{intra_report.travel_before:.3f} -> {intra_report.travel_after:.3f} "
                f"({intra_report.gain_percent:.2f}% improvement, "
                f"{intra_report.groups} glyph group(s), {intra_report.flips} reversal(s)); "
                f"emitted rapid travel {intra_report.emitted_before:.3f} -> "
                f"{intra_emitted_after:.3f}"
            )

    if logger is not None:
        logger.debug(f"{prefix}Reassembled optimized document with {method_name}")

    return OptimizationOutcome(
        optimized_doc=optimized_doc,
        optimized_distance=optimized_distance,
        method_name=method_name,
        method_notes=method_notes,
        ensemble_benchmarks=ensemble_benchmarks,
        direction_sweep_travel_before=(
            sweep_report.travel_before if sweep_report is not None else None
        ),
        direction_sweep_travel_after=(
            sweep_report.travel_after if sweep_report is not None else None
        ),
        direction_sweep_emitted_before=(
            sweep_report.emitted_before if sweep_report is not None else None
        ),
        direction_sweep_emitted_after=emitted_after,
        direction_sweep_passes=sweep_report.passes if sweep_report is not None else 0,
        direction_sweep_flips=sweep_report.flips if sweep_report is not None else 0,
        intra_sweep_travel_before=(
            intra_report.travel_before if intra_report is not None else None
        ),
        intra_sweep_travel_after=(intra_report.travel_after if intra_report is not None else None),
        intra_sweep_emitted_before=(
            intra_report.emitted_before if intra_report is not None else None
        ),
        intra_sweep_emitted_after=intra_emitted_after,
        intra_sweep_groups=intra_report.groups if intra_report is not None else 0,
        intra_sweep_flips=intra_report.flips if intra_report is not None else 0,
        merged_paths_before=merge_report.paths_before if merge_report is not None else None,
        merged_paths_after=merge_report.paths_after if merge_report is not None else None,
        merges_applied=merge_report.merges if merge_report is not None else 0,
    )
