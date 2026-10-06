"""Unit tests for the shared optimization pipeline helper (core.pipeline).

Covers the three stages every entry point shares: preprocessing
bifurcation, chunking, and optimize+reassemble (including Parallel
Ensemble unwrapping, benchmark logging, and method naming/notes).
"""

from __future__ import annotations

import logging
from typing import Any, List

import pytest

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.core.models import (
    Coordinate,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.optimizer import (
    BlockTraverseState,
    NoOpStrategy,
    OptimizationResult,
    ParallelEnsembleOptimizationResult,
    StrategyBenchmarkResult,
)
from plt_optimizer.core.pipeline import (
    DEFAULT_THRESHOLD_MULTIPLIER,
    OptimizationOutcome,
    chunk_document,
    optimize_and_reassemble,
    preprocess_document,
)
from plt_optimizer.core.profiler import ProfileResult


def _make_doc(n_paths: int = 3) -> PLTDocument:
    """Build a tiny document with ``n_paths`` single-segment cutting paths."""
    paths: List[StrokePath] = []
    for i in range(n_paths):
        start = Coordinate(x=float(i * 10), y=0.0)
        end = Coordinate(x=float(i * 10), y=5.0)
        paths.append(
            StrokePath(
                pen_up_position=start,
                segments=(StrokeSegment(start=start, end=end, is_cutting=True),),
            )
        )
    return PLTDocument(header_commands=[], stroke_paths=paths, footer_commands=[])


def _profile(is_structural: bool = False, baseline_extent: float = 10.0) -> ProfileResult:
    """Construct a ProfileResult directly (the known-kind bypass path)."""
    return ProfileResult(
        baseline_extent=baseline_extent,
        median_dx=1.0,
        median_dy=1.0,
        total_strokes=1,
        p95_index=0,
        is_structural=is_structural,
    )


class TestPreprocessDocument:
    """preprocess_document bifurcation semantics."""

    def test_text_document_passes_through_unchanged(self) -> None:
        """Text documents are returned as the same object (no simplification)."""
        doc = _make_doc()
        result = preprocess_document(doc, is_structural=False)
        assert result is doc

    def test_structural_document_is_fractured_and_deduped(self) -> None:
        """Structural documents run through fracture then dedupe factories."""
        doc = _make_doc()
        calls: List[str] = []

        def fake_fracture(d: PLTDocument) -> PLTDocument:
            calls.append("fracture")
            return d

        def fake_dedupe(  # noqa: ARG001
            d: PLTDocument, tol: float, line_tol: float
        ) -> PLTDocument:
            calls.append("dedupe")
            return d

        result = preprocess_document(
            doc,
            is_structural=True,
            fracture_factory=fake_fracture,
            dedupe_factory=fake_dedupe,
        )
        assert calls == ["fracture", "dedupe"]
        assert result is doc

    def test_structural_dedupe_receives_production_tolerance(self) -> None:
        """The dedupe factory gets tol=1e-3 and line_tol=10.0 (production)."""
        doc = _make_doc()
        seen_tol: List[float] = []
        seen_line_tol: List[float] = []

        def fake_dedupe(d: PLTDocument, tol: float, line_tol: float) -> PLTDocument:
            seen_tol.append(tol)
            seen_line_tol.append(line_tol)
            return d

        preprocess_document(
            doc,
            is_structural=True,
            fracture_factory=lambda d: d,
            dedupe_factory=fake_dedupe,
        )
        assert seen_tol == [1e-3]
        assert seen_line_tol == [10.0]

    def test_logging_emits_debug_messages(self, caplog: pytest.LogCaptureFixture) -> None:
        """A provided logger receives DEBUG bifurcation messages with the prefix."""
        from plt_optimizer.utils.logging import TextLogger

        logger = TextLogger(name="plt_optimizer_test_preprocess")
        doc = _make_doc()
        with caplog.at_level(logging.DEBUG, logger="plt_optimizer_test_preprocess"):
            preprocess_document(doc, is_structural=False, logger=logger, log_prefix="[job1]")
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "[job1] Skipped stroke simplification for text document" in combined

    def test_structural_logging_emits_debug_messages(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Structural preprocessing logs both fracture and dedupe DEBUG steps."""
        from plt_optimizer.utils.logging import TextLogger

        logger = TextLogger(name="plt_optimizer_test_preprocess_struct")
        doc = _make_doc()
        with caplog.at_level(logging.DEBUG, logger="plt_optimizer_test_preprocess_struct"):
            preprocess_document(
                doc,
                is_structural=True,
                logger=logger,
                log_prefix="[job2]",
                fracture_factory=lambda d: d,
                dedupe_factory=lambda d, tol=0.0, line_tol=0.0: d,
            )
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "[job2] Fractured structural document" in combined
        assert "[job2] Simplified overlapping strokes" in combined


class TestChunkDocument:
    """chunk_document wiring."""

    def test_uses_production_threshold_multiplier(self) -> None:
        """Default multiplier is the production 2.0."""
        assert DEFAULT_THRESHOLD_MULTIPLIER == 2.0
        doc = _make_doc()
        blocks = chunk_document(doc, _profile(is_structural=False))
        assert blocks  # 3 paths, jumps 10 < threshold 20 -> one block
        assert sum(len(b.paths) for b in blocks) == 3

    def test_structural_profile_yields_one_to_one_blocks(self) -> None:
        """A known-structural ProfileResult bypasses chronological chunking."""
        doc = _make_doc(n_paths=4)
        blocks = chunk_document(doc, _profile(is_structural=True))
        assert len(blocks) == 4
        assert all(len(b.paths) == 1 for b in blocks)

    def test_chunker_factory_seam_is_used(self) -> None:
        """The chunker_factory seam receives the ChunkerConfig and drives chunking."""
        from plt_optimizer.core.chunker import ChunkerConfig

        captured: List[Any] = []

        class _StubChunker:
            def __init__(self, config: ChunkerConfig | None = None) -> None:
                captured.append(config)

            def chunk(self, *args: Any, **kwargs: Any) -> List[MacroBlock]:
                return []

        doc = _make_doc()
        blocks = chunk_document(doc, _profile(), chunker_factory=_StubChunker)
        assert blocks == []
        assert captured and captured[0].threshold_multiplier == 2.0


class TestOptimizeAndReassemble:
    """optimize_and_reassemble result handling."""

    def _blocks(self) -> List[MacroBlock]:
        doc = _make_doc(n_paths=2)
        return [
            MacroBlock(
                block_id=i,
                paths=(path,),
                entrance=path.segments[0].start,
                exit=path.segments[0].end,
            )
            for i, path in enumerate(doc.stroke_paths)
        ]

    def test_plain_result_uses_fast_mode_method_name(self) -> None:
        """Non-ensemble results report the historical fast-mode method label."""
        doc = _make_doc(n_paths=2)
        blocks = self._blocks()
        outcome = optimize_and_reassemble(doc, blocks, NoOpStrategy())
        assert isinstance(outcome, OptimizationOutcome)
        assert outcome.method_name == "NearestNeighbor + 2-Opt (Fast Mode)"
        assert outcome.method_notes.startswith("optimized_distance=")
        assert outcome.ensemble_benchmarks == ()
        assert len(outcome.optimized_doc.stroke_paths) == 2

    def test_ensemble_result_unwrapped_with_notes(self) -> None:
        """Ensemble results expose winner, benchmark tuple, and notes format."""
        doc = _make_doc(n_paths=2)
        blocks = self._blocks()

        traverse = tuple(
            BlockTraverseState(
                block_id=b.block_id,
                reversed=False,
                entrance=b.entrance.as_tuple(),
                exit=b.exit.as_tuple(),
            )
            for b in blocks
        )
        inner = OptimizationResult(
            traverse_order=traverse,
            connections=(),
            total_travel_distance=42.0,
            initial_position=None,
        )
        bench_none = StrategyBenchmarkResult(
            strategy_name="NoOp (Baseline)",
            result=inner,
            execution_time_seconds=0.01,
            improvement_percent=None,
        )
        bench_val = StrategyBenchmarkResult(
            strategy_name="Genetic Algorithm",
            result=inner,
            execution_time_seconds=0.02,
            improvement_percent=25.0,
        )
        ensemble = ParallelEnsembleOptimizationResult(
            result=inner,
            winner_name="Genetic Algorithm",
            all_benchmarks=(bench_none, bench_val),
        )

        class _EnsembleEngine:
            def __init__(self, strategy: Any) -> None:
                self._strategy = strategy

            def optimize(self, blocks: List[MacroBlock]) -> Any:
                return ensemble

        outcome = optimize_and_reassemble(
            doc, blocks, NoOpStrategy(), engine_factory=_EnsembleEngine
        )
        assert outcome.method_name == "Genetic Algorithm"
        # The winning result's synthetic 42.0 is replaced by the direction
        # sweep's geometry-derived total: the two 5-unit blocks re-enter
        # forward-to-forward for a 10.0 inter-chunk gap.
        assert outcome.optimized_distance == pytest.approx(10.0)
        assert "NoOp (Baseline): 42.000 (improvement=N/A)" in outcome.method_notes
        assert "Genetic Algorithm: 42.000 (improvement=25.00%)" in outcome.method_notes
        assert outcome.ensemble_benchmarks == (bench_none, bench_val)
        # Benchmark rows keep their pre-sweep distances; the sweep is reported
        # separately in the notes.
        assert "direction_sweep=" in outcome.method_notes
        assert outcome.direction_sweep_passes >= 1

    def test_ensemble_benchmarks_logged_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """Provided logger receives the benchmark table at INFO level."""
        from plt_optimizer.utils.logging import TextLogger

        doc = _make_doc(n_paths=1)
        blocks = self._blocks()
        traverse = tuple(
            BlockTraverseState(
                block_id=b.block_id,
                reversed=False,
                entrance=b.entrance.as_tuple(),
                exit=b.exit.as_tuple(),
            )
            for b in blocks
        )
        inner = OptimizationResult(
            traverse_order=traverse,
            connections=(),
            total_travel_distance=10.0,
            initial_position=None,
        )
        bench = StrategyBenchmarkResult(
            strategy_name="Insertion Heuristic",
            result=inner,
            execution_time_seconds=0.5,
            improvement_percent=None,
        )
        ensemble = ParallelEnsembleOptimizationResult(
            result=inner,
            winner_name="Insertion Heuristic",
            all_benchmarks=(bench,),
        )

        class _EnsembleEngine:
            def __init__(self, strategy: Any) -> None:
                self._strategy = strategy

            def optimize(self, blocks: List[MacroBlock]) -> Any:
                return ensemble

        logger = TextLogger(name="plt_optimizer_test_ensemble")
        with caplog.at_level(logging.DEBUG, logger="plt_optimizer_test_ensemble"):
            optimize_and_reassemble(
                doc,
                blocks,
                NoOpStrategy(),
                engine_factory=_EnsembleEngine,
                logger=logger,
                log_prefix="[job9]",
            )
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "[job9] Strategy benchmark results:" in combined
        assert "no baseline comparison" in combined

    def test_reassembler_factory_seam_is_used(self) -> None:
        """The reassembler_factory seam drives reassembly."""
        doc = _make_doc(n_paths=2)
        blocks = self._blocks()
        calls: List[int] = []

        class _StubReassembler:
            def reassemble(
                self,
                original_document: PLTDocument,
                blocks: List[MacroBlock],
                optimization_result: OptimizationResult,
                intra_chunk_results: Any = None,
            ) -> PLTDocument:
                calls.append(len(blocks))
                return original_document

        outcome = optimize_and_reassemble(
            doc, blocks, NoOpStrategy(), reassembler_factory=_StubReassembler
        )
        # Two runs through the seam: the direction sweep improves this tour, so
        # it first reassembles the pre-sweep result to measure emitted travel,
        # then reassembles the swept result.
        assert calls == [2, 2]
        assert outcome.optimized_doc is doc

    def test_direction_sweep_disabled_reproduces_strategy_result(self) -> None:
        """direction_sweep=False keeps the winning result byte-for-byte."""
        doc = _make_doc(n_paths=2)
        blocks = self._blocks()

        outcome = optimize_and_reassemble(doc, blocks, NoOpStrategy(), direction_sweep=False)

        # Blocks at x=0 and x=10, 5 tall: entering block 1 forward costs the
        # hypotenuse 11.180, exactly what the strategy reports with the sweep
        # off (the sweep would reverse it to a 10.0 gap).
        assert outcome.optimized_distance == pytest.approx(11.180339887498949)
        assert outcome.direction_sweep_travel_before is None
        assert outcome.direction_sweep_travel_after is None
        assert outcome.direction_sweep_emitted_before is None
        assert outcome.direction_sweep_emitted_after is None
        assert outcome.direction_sweep_passes == 0
        assert outcome.direction_sweep_flips == 0
        assert "direction_sweep=" not in outcome.method_notes


class TestDirectionSweepIntegration:
    """The post-TSP chunk direction sweep inside optimize_and_reassemble."""

    def _stale_blocks(self) -> List[MacroBlock]:
        """Three blocks where block 1 is far away and block 2 sits near block 0.

        Entering block 1 at its exit (reversed) is much cheaper than entering
        at its entrance, so a tour carrying ``reversed=False`` there is stale.
        """
        doc = _make_doc(n_paths=3)
        blocks = [
            MacroBlock(
                block_id=i,
                paths=(path,),
                entrance=path.segments[0].start,
                exit=path.segments[0].end,
            )
            for i, path in enumerate(doc.stroke_paths)
        ]
        # Move block 2 far away and block 1 near the origin's right side.
        blocks[2] = MacroBlock(
            block_id=2,
            paths=(
                StrokePath(
                    pen_up_position=Coordinate(x=1000.0, y=0.0),
                    segments=(
                        StrokeSegment(
                            start=Coordinate(x=1000.0, y=0.0),
                            end=Coordinate(x=1000.0, y=5.0),
                            is_cutting=True,
                        ),
                    ),
                ),
            ),
            entrance=Coordinate(x=1000.0, y=0.0),
            exit=Coordinate(x=1000.0, y=5.0),
        )
        blocks[1] = MacroBlock(
            block_id=1,
            paths=(
                StrokePath(
                    pen_up_position=Coordinate(x=30.0, y=0.0),
                    segments=(
                        StrokeSegment(
                            start=Coordinate(x=30.0, y=0.0),
                            end=Coordinate(x=11.0, y=0.0),
                            is_cutting=True,
                        ),
                    ),
                ),
            ),
            entrance=Coordinate(x=30.0, y=0.0),
            exit=Coordinate(x=11.0, y=0.0),
        )
        return blocks

    def _stale_result(self, blocks: List[MacroBlock]) -> OptimizationResult:
        """Chronological tour with block 1 left forward (the stale flag)."""
        traverse = tuple(
            BlockTraverseState(
                block_id=b.block_id,
                reversed=False,
                entrance=b.entrance.as_tuple(),
                exit=b.exit.as_tuple(),
            )
            for b in blocks
        )
        return OptimizationResult(
            traverse_order=traverse,
            connections=(),
            total_travel_distance=999.0,
            initial_position=(0.0, 0.0),
        )

    class _StubEngine:
        def __init__(self, result: OptimizationResult) -> None:
            self._result = result

        def optimize(self, blocks: List[MacroBlock]) -> OptimizationResult:
            return self._result

    def test_stale_direction_flags_are_repaired(self) -> None:
        """The sweep fixes directions a strategy left stale after reordering."""
        doc = _make_doc(n_paths=3)
        blocks = self._stale_blocks()
        stub = self._stale_result(blocks)
        engine = self._StubEngine(stub)

        swept = optimize_and_reassemble(
            doc, blocks, NoOpStrategy(), engine_factory=lambda **_: engine
        )
        plain = optimize_and_reassemble(
            doc,
            blocks,
            NoOpStrategy(),
            engine_factory=lambda **_: engine,
            direction_sweep=False,
        )

        assert swept.direction_sweep_passes >= 1
        assert swept.direction_sweep_flips >= 1
        assert swept.optimized_distance < plain.optimized_distance
        # The reassembled document's own rapid travel matches the reported
        # emitted metric.
        assert swept.optimized_doc.rapid_distance() == pytest.approx(
            swept.direction_sweep_emitted_after
        )

    def test_method_notes_and_fields_report_the_sweep(self) -> None:
        doc = _make_doc(n_paths=3)
        blocks = self._stale_blocks()
        engine = self._StubEngine(self._stale_result(blocks))

        outcome = optimize_and_reassemble(
            doc, blocks, NoOpStrategy(), engine_factory=lambda **_: engine
        )

        assert outcome.direction_sweep_travel_before is not None
        assert outcome.direction_sweep_travel_after is not None
        assert outcome.direction_sweep_travel_after <= outcome.direction_sweep_travel_before
        assert outcome.direction_sweep_emitted_before is not None
        assert outcome.direction_sweep_emitted_after is not None
        assert "direction_sweep=" in outcome.method_notes
        assert "passes" in outcome.method_notes and "flips" in outcome.method_notes

    def test_sweep_logged_at_info_with_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        from plt_optimizer.utils.logging import TextLogger

        doc = _make_doc(n_paths=3)
        blocks = self._stale_blocks()
        engine = self._StubEngine(self._stale_result(blocks))
        logger = TextLogger(name="plt_optimizer_test_sweep")

        with caplog.at_level(logging.DEBUG, logger="plt_optimizer_test_sweep"):
            optimize_and_reassemble(
                doc,
                blocks,
                NoOpStrategy(),
                engine_factory=lambda **_: engine,
                logger=logger,
                log_prefix="[job7]",
            )

        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "[job7] Direction sweep: inter-chunk" in combined
        assert "emitted rapid travel" in combined

    def test_sweep_is_silent_without_a_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        """No logger supplied -> no INFO output (headless hot-watch path)."""
        doc = _make_doc(n_paths=3)
        blocks = self._stale_blocks()
        engine = self._StubEngine(self._stale_result(blocks))

        with caplog.at_level(logging.INFO, logger="plt_optimizer"):
            optimize_and_reassemble(doc, blocks, NoOpStrategy(), engine_factory=lambda **_: engine)

        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Direction sweep" not in combined

    def test_no_improvement_leaves_outcome_clean(self) -> None:
        """A tour with nothing to gain reports no sweep and adds no notes."""
        doc = _make_doc(n_paths=2)
        # A collinear run: forward-forward is already optimal at both blocks.
        blocks = [
            MacroBlock(
                block_id=0,
                paths=(
                    StrokePath(
                        pen_up_position=Coordinate(x=0.0, y=0.0),
                        segments=(
                            StrokeSegment(
                                start=Coordinate(x=0.0, y=0.0),
                                end=Coordinate(x=10.0, y=0.0),
                                is_cutting=True,
                            ),
                        ),
                    ),
                ),
                entrance=Coordinate(x=0.0, y=0.0),
                exit=Coordinate(x=10.0, y=0.0),
            ),
            MacroBlock(
                block_id=1,
                paths=(
                    StrokePath(
                        pen_up_position=Coordinate(x=11.0, y=0.0),
                        segments=(
                            StrokeSegment(
                                start=Coordinate(x=11.0, y=0.0),
                                end=Coordinate(x=21.0, y=0.0),
                                is_cutting=True,
                            ),
                        ),
                    ),
                ),
                entrance=Coordinate(x=11.0, y=0.0),
                exit=Coordinate(x=21.0, y=0.0),
            ),
        ]

        outcome = optimize_and_reassemble(doc, blocks, NoOpStrategy())

        assert outcome.direction_sweep_passes == 0
        assert outcome.direction_sweep_flips == 0
        # Nothing to gain -> the sweep reports nothing at all.
        assert outcome.direction_sweep_travel_before is None
        assert outcome.direction_sweep_travel_after is None
        assert outcome.direction_sweep_emitted_before is None
        assert outcome.direction_sweep_emitted_after is None
        assert "direction_sweep=" not in outcome.method_notes
