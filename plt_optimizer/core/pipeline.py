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
from typing import Callable, List, Optional, Tuple

from plt_optimizer.core.chunker import Chunker, ChunkerConfig, MacroBlock
from plt_optimizer.core.models import PLTDocument
from plt_optimizer.core.optimizer import (
    OptimizationStrategy,
    OptimizerEngine,
    ParallelEnsembleOptimizationResult,
    StrategyBenchmarkResult,
)
from plt_optimizer.core.profiler import ProfileResult
from plt_optimizer.core.reassembler import Reassembler
from plt_optimizer.utils.geometry import fracture_linear_paths, remove_redundant_strokes
from plt_optimizer.utils.logging import TextLogger

# Production chunking threshold used by every entry point (optimize, watch,
# benchmark, generate). Kept here as the single source of truth.
DEFAULT_THRESHOLD_MULTIPLIER: float = 2.0

# Tolerance used when removing redundant strokes in the structural pipeline.
_REDUNDANCY_TOL: float = 1e-3

# Method label recorded for non-ensemble (fast-mode style) results. Matches
# the historical ``optimize``/``watch`` CSV method column verbatim.
_FAST_MODE_METHOD_NAME: str = "NearestNeighbor + 2-Opt (Fast Mode)"


@dataclass(frozen=True)
class OptimizationOutcome:
    """Result of :func:`optimize_and_reassemble`.

    Attributes:
        optimized_doc: Reassembled document with strokes in optimized order.
        optimized_distance: Total rapid travel distance after optimization.
        method_name: Winning strategy name (ensemble) or the fast-mode label.
        method_notes: Human-readable notes describing per-strategy results,
            formatted for the CSV metrics ``notes`` column.
        ensemble_benchmarks: Per-strategy benchmark results when a Parallel
            Ensemble run occurred; empty tuple otherwise.
    """

    optimized_doc: PLTDocument
    optimized_distance: float
    method_name: str
    method_notes: str
    ensemble_benchmarks: Tuple[StrategyBenchmarkResult, ...] = field(default=())


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
            :func:`plt_optimizer.utils.geometry.remove_redundant_strokes`
            (test seam; defaults to the production implementation).
        logger: Optional text logger for DEBUG diagnostics.
        log_prefix: Prefix prepended to log messages (e.g. ``"[job123]"``).

    Returns:
        A preprocessed document (new object for structural input, the
        original object for text input).
    """
    prefix = f"{log_prefix} " if log_prefix else ""
    fracture = fracture_factory or fracture_linear_paths
    dedupe = dedupe_factory or remove_redundant_strokes

    if is_structural:
        # STRUCTURAL PIPELINE: fracture linear paths into independent
        # segments, then cull overlapping coincident lines.
        fractured = fracture(document)
        if logger is not None:
            logger.debug(
                f"{prefix}Fractured structural document (linear paths -> independent segments)"
            )
        deduplicated = dedupe(fractured, tol=_REDUNDANCY_TOL)
        if logger is not None:
            logger.debug(f"{prefix}Removed redundant strokes from fractured document")
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


def optimize_and_reassemble(
    document: PLTDocument,
    blocks: List[MacroBlock],
    strategy: OptimizationStrategy,
    *,
    engine_factory: Optional[Callable[..., OptimizerEngine]] = None,
    reassembler_factory: Optional[Callable[[], Reassembler]] = None,
    logger: Optional[TextLogger] = None,
    log_prefix: str = "",
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
    optimized_doc = reassembler.reassemble(document, blocks, result_for_reassembly)

    if logger is not None:
        logger.debug(f"{prefix}Reassembled optimized document with {method_name}")

    return OptimizationOutcome(
        optimized_doc=optimized_doc,
        optimized_distance=optimized_distance,
        method_name=method_name,
        method_notes=method_notes,
        ensemble_benchmarks=ensemble_benchmarks,
    )
