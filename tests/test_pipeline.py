"""Unit tests for the shared optimization pipeline helper (core.pipeline).

Covers the three stages every entry point shares: preprocessing
bifurcation, chunking, and optimize+reassemble (including Parallel
Ensemble unwrapping, benchmark logging, and method naming/notes).
"""

from __future__ import annotations

import logging
import math
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


def _vpath(x: float, y0: float, y1: float) -> StrokePath:
    """One straight cutting path from ``(x, y0)`` to ``(x, y1)``, pen-up at start."""
    start = Coordinate(x=x, y=y0)
    end = Coordinate(x=x, y=y1)
    return StrokePath(pen_up_position=start, segments=(StrokeSegment(start, end, True),))


def _hpath(x0: float, x1: float) -> StrokePath:
    """One straight cutting path from ``(x0, 0)`` to ``(x1, 0)``, pen-up at start."""
    start = Coordinate(x=x0, y=0.0)
    end = Coordinate(x=x1, y=0.0)
    return StrokePath(pen_up_position=start, segments=(StrokeSegment(start, end, True),))


class TestIntraSweepIntegration:
    """The intra-chunk glyph direction sweep inside optimize_and_reassemble."""

    def _blocks(self) -> List[MacroBlock]:
        """Block 0: three single-path glyphs where the middle wants reversing.

        Glyph gaps forward: (1,0)->(2,0) = 1.0 and (1.2,0)->(3,0) = 1.8,
        total 2.8. Reversing the middle glyph: (1,0)->(1.2,0) = 0.2 and
        (2,0)->(3,0) = 1.0, total 1.2.
        Block 1's path enters forward at (10,1) (gap sqrt(37) from block 0's
        exit (4,0)) and reversed at (10,0) (gap 6.0), so the direction sweep
        flips it while the glyph sweep leaves it alone.
        """
        glyph_paths = (
            _hpath(0.0, 1.0),
            _hpath(2.0, 1.2),
            _hpath(3.0, 4.0),
        )
        block0 = MacroBlock(
            block_id=0,
            paths=glyph_paths,
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=4.0, y=0.0),
        )
        block1 = MacroBlock(
            block_id=1,
            paths=(_vpath(10.0, 1.0, 0.0),),
            entrance=Coordinate(x=10.0, y=1.0),
            exit=Coordinate(x=10.0, y=0.0),
        )
        return [block0, block1]

    def _chronological_result(self, blocks: List[MacroBlock]) -> OptimizationResult:
        """The all-forward chronological tour (no inter-chunk gain to find)."""
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
            total_travel_distance=7.0,
            initial_position=(0.0, 0.0),
        )

    class _StubEngine:
        def __init__(self, result: OptimizationResult) -> None:
            self._result = result

        def optimize(self, blocks: List[MacroBlock]) -> OptimizationResult:
            return self._result

    def _run(self, **kwargs: Any) -> OptimizationOutcome:
        blocks = self._blocks()
        engine = self._StubEngine(self._chronological_result(blocks))
        doc = _make_doc(n_paths=0)
        kwargs.setdefault("direction_sweep", False)
        return optimize_and_reassemble(
            doc, blocks, NoOpStrategy(), engine_factory=lambda **_: engine, **kwargs
        )

    def test_sweep_flips_the_glyph_and_reports_the_gain(self) -> None:
        """The middle glyph flips; intra drops 2.8 -> 1.2, emitted follows."""
        outcome = self._run(glyph_groups_by_block={0: ((0,), (1,), (2,))})

        assert outcome.intra_sweep_flips == 1
        assert outcome.intra_sweep_groups == 3
        assert outcome.intra_sweep_travel_before == pytest.approx(2.8)
        assert outcome.intra_sweep_travel_after == pytest.approx(1.2)
        # Emitted: intra 2.8 + inter (4,0)->(10,1) = sqrt(37) baseline; the
        # sweep's gain flows one-for-one into the emitted rapid travel.
        inter = math.sqrt(37.0)
        assert outcome.intra_sweep_emitted_before == pytest.approx(2.8 + inter)
        assert outcome.intra_sweep_emitted_after == pytest.approx(1.2 + inter)
        assert outcome.optimized_doc.rapid_distance() == pytest.approx(
            outcome.intra_sweep_emitted_after
        )
        assert "intra_sweep=2.800->1.200 (1 flips)" in outcome.method_notes

    def test_disabled_by_default_without_glyph_groups(self) -> None:
        """No glyph knowledge (the parsed-PLT path) -> zero intra reporting."""
        outcome = self._run()

        assert outcome.intra_sweep_flips == 0
        assert outcome.intra_sweep_groups == 0
        assert outcome.intra_sweep_travel_before is None
        assert outcome.intra_sweep_emitted_after is None
        assert "intra_sweep=" not in outcome.method_notes
        # The emission is the plain chronological one.
        assert outcome.optimized_doc.rapid_distance() == pytest.approx(2.8 + math.sqrt(37.0))

    def test_escape_hatch_reproduces_pre_sweep_output(self) -> None:
        """``intra_sweep=False`` keeps the grouped emission byte-identical."""
        groups = {0: ((0,), (1,), (2,))}
        swept = self._run(glyph_groups_by_block=groups)
        plain = self._run(glyph_groups_by_block=groups, intra_sweep=False)

        assert plain.intra_sweep_flips == 0
        assert plain.intra_sweep_travel_before is None
        assert "intra_sweep=" not in plain.method_notes
        assert plain.optimized_doc.rapid_distance() == swept.intra_sweep_emitted_before

    def test_composes_with_the_direction_sweep_monotonically(self) -> None:
        """With both sweeps on, the emitted travel never exceeds the baseline.

        The direction sweep flips block 1 (entering at its exit (10,1) is
        free), the glyph sweep flips the middle glyph; the composition enters
        block 1 reversed and leaves the swept block's endpoints untouched.
        """
        groups = {0: ((0,), (1,), (2,))}
        baseline = self._run(glyph_groups_by_block=groups, intra_sweep=False)
        composed = self._run(glyph_groups_by_block=groups, direction_sweep=True)

        assert composed.direction_sweep_flips >= 1
        assert composed.intra_sweep_flips == 1
        assert composed.optimized_doc.rapid_distance() <= baseline.optimized_doc.rapid_distance()
        assert "direction_sweep=" in composed.method_notes
        assert "intra_sweep=" in composed.method_notes

    def test_blocks_without_groups_keep_chronological_paths(self) -> None:
        """Block 1 (no groups) emits its single path untouched, forward."""
        outcome = self._run(glyph_groups_by_block={0: ((0,), (1,), (2,))})

        emitted = outcome.optimized_doc.stroke_paths
        assert len(emitted) == 4
        # Block 1's path is last and unchanged (starts at x=10).
        assert emitted[-1].segments[0].start.x == 10.0

    def test_sweep_logged_at_info_with_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        from plt_optimizer.utils.logging import TextLogger

        logger = TextLogger(name="plt_optimizer_test_intra")
        blocks = self._blocks()
        engine = self._StubEngine(self._chronological_result(blocks))

        with caplog.at_level(logging.DEBUG, logger="plt_optimizer_test_intra"):
            optimize_and_reassemble(
                _make_doc(n_paths=0),
                blocks,
                NoOpStrategy(),
                engine_factory=lambda **_: engine,
                logger=logger,
                log_prefix="[job9]",
                glyph_groups_by_block={0: ((0,), (1,), (2,))},
            )

        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "[job9] Intra-chunk glyph sweep: intra" in combined
        assert "glyph group(s)" in combined
        assert "emitted rapid travel" in combined

    def test_sweep_is_silent_without_a_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="plt_optimizer"):
            self._run(glyph_groups_by_block={0: ((0,), (1,), (2,))})

        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Intra-chunk glyph sweep" not in combined

    def test_no_improvement_leaves_outcome_clean(self) -> None:
        """Groups with nothing to gain report nothing and add no notes."""
        blocks = self._blocks()
        # A collinear chain: every glyph's forward gaps are already optimal.
        blocks[0] = MacroBlock(
            block_id=0,
            paths=(_vpath(0.0, 0.0, 1.0), _vpath(2.0, 0.0, 1.0)),
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=2.0, y=1.0),
        )
        engine = self._StubEngine(self._chronological_result(blocks))

        outcome = optimize_and_reassemble(
            _make_doc(n_paths=0),
            blocks,
            NoOpStrategy(),
            engine_factory=lambda **_: engine,
            glyph_groups_by_block={0: ((0,), (1,))},
        )

        assert outcome.intra_sweep_flips == 0
        assert outcome.intra_sweep_travel_before is None
        assert "intra_sweep=" not in outcome.method_notes


class TestCoincidentMergeIntegration:
    """The coincident-stroke merge inside optimize_and_reassemble."""

    def _tip_to_tail_blocks(self) -> List[MacroBlock]:
        """Three single-path blocks; blocks 0 and 1 form a tip-to-tail run.

        Block 0 ends at ``(10, 0)`` where block 1's pen-up and first cut both
        sit, so the tool-up between them is a zero-distance move. Block 2 is
        a genuine rapid away and must stay its own path.
        """
        paths = (
            _hpath(0.0, 10.0),
            _vpath(10.0, 0.0, 10.0),
            _hpath(50.0, 60.0),
        )
        return [
            MacroBlock(
                block_id=i,
                paths=(path,),
                entrance=path.segments[0].start,
                exit=path.segments[-1].end,
            )
            for i, path in enumerate(paths)
        ]

    def test_merge_collapses_junction_and_reports(self) -> None:
        """The tip-to-tail pair becomes one path; rapid travel is unchanged."""
        blocks = self._tip_to_tail_blocks()
        doc = _make_doc(n_paths=0)

        outcome = optimize_and_reassemble(doc, blocks, NoOpStrategy(), direction_sweep=False)

        assert outcome.merges_applied == 1
        assert outcome.merged_paths_before == 3
        assert outcome.merged_paths_after == 2
        assert len(outcome.optimized_doc.stroke_paths) == 2
        assert "merge=3->2 (1 merge(s))" in outcome.method_notes

        merged = outcome.optimized_doc.stroke_paths[0]
        # The run keeps the FIRST path's pen-up target and both segments.
        assert merged.pen_up_position == Coordinate(x=0.0, y=0.0)
        assert len(merged.segments) == 2
        # The untouched rapid path keeps its own pen-up.
        assert outcome.optimized_doc.stroke_paths[1].pen_up_position == Coordinate(x=50.0, y=0.0)

    def test_merge_is_metric_neutral(self) -> None:
        """Removing the tool-up moves no rapid travel."""
        blocks = self._tip_to_tail_blocks()
        doc = _make_doc(n_paths=0)

        merged = optimize_and_reassemble(doc, blocks, NoOpStrategy(), direction_sweep=False)
        plain = optimize_and_reassemble(
            doc, blocks, NoOpStrategy(), direction_sweep=False, merge_coincident=False
        )

        assert merged.optimized_doc.rapid_distance() == pytest.approx(
            plain.optimized_doc.rapid_distance()
        )

        # Cutting geometry is preserved: same undirected segment multiset.
        def spans(doc: PLTDocument) -> list:
            return sorted(
                (
                    min(s.start.x, s.end.x),
                    min(s.start.y, s.end.y),
                    max(s.start.x, s.end.x),
                    max(s.start.y, s.end.y),
                )
                for p in doc.stroke_paths
                for s in p.segments
            )

        assert spans(merged.optimized_doc) == spans(plain.optimized_doc)

    def test_escape_hatch_reproduces_pre_merge_output(self) -> None:
        """``merge_coincident=False`` keeps every path and reports nothing."""
        blocks = self._tip_to_tail_blocks()
        doc = _make_doc(n_paths=0)

        outcome = optimize_and_reassemble(
            doc,
            blocks,
            NoOpStrategy(),
            direction_sweep=False,
            merge_coincident=False,
        )

        assert outcome.merges_applied == 0
        assert outcome.merged_paths_before is None
        assert outcome.merged_paths_after is None
        assert "merge=" not in outcome.method_notes
        assert len(outcome.optimized_doc.stroke_paths) == 3

    def test_silent_when_nothing_merges(self) -> None:
        """A document of genuine rapids reports the counts and no note."""
        doc = _make_doc(n_paths=3)
        blocks = [
            MacroBlock(
                block_id=i,
                paths=(path,),
                entrance=path.segments[0].start,
                exit=path.segments[-1].end,
            )
            for i, path in enumerate(doc.stroke_paths)
        ]

        outcome = optimize_and_reassemble(doc, blocks, NoOpStrategy(), direction_sweep=False)

        # _make_doc spaces its paths 10 units apart: no coincident junction.
        assert outcome.merges_applied == 0
        assert outcome.merged_paths_before == 3
        assert outcome.merged_paths_after == 3
        assert "merge=" not in outcome.method_notes
        assert len(outcome.optimized_doc.stroke_paths) == 3

    def test_merge_runs_after_the_direction_sweep(self) -> None:
        """The swept tour's tip-to-tail junction merges, inside emitted_after.

        Block 1 is stale (entering at its exit is cheaper), so the sweep flips
        it; the reversal lands its exit exactly on block 2's pen-up, which the
        merge then collapses. ``direction_sweep_emitted_after`` measures the
        merged document, proving the merge is the final stage.
        """
        paths = (
            StrokePath(
                pen_up_position=Coordinate(x=0.0, y=0.0),
                segments=(
                    StrokeSegment(Coordinate(x=0.0, y=0.0), Coordinate(x=0.0, y=10.0), True),
                ),
            ),
            StrokePath(
                pen_up_position=Coordinate(x=100.0, y=20.0),
                segments=(
                    StrokeSegment(Coordinate(x=100.0, y=20.0), Coordinate(x=100.0, y=10.0), True),
                ),
            ),
            StrokePath(
                pen_up_position=Coordinate(x=100.0, y=20.0),
                segments=(
                    StrokeSegment(Coordinate(x=100.0, y=20.0), Coordinate(x=100.0, y=30.0), True),
                ),
            ),
        )
        blocks = [
            MacroBlock(
                block_id=i,
                paths=(path,),
                entrance=path.segments[0].start,
                exit=path.segments[-1].end,
            )
            for i, path in enumerate(paths)
        ]

        outcome = optimize_and_reassemble(
            _make_doc(n_paths=0), blocks, NoOpStrategy(), direction_sweep=True
        )

        assert outcome.direction_sweep_flips == 1
        assert outcome.merges_applied == 1
        assert len(outcome.optimized_doc.stroke_paths) == 2
        # Pre-sweep emission: (0,10)->(100,20) + (100,10)->(100,20).
        assert outcome.direction_sweep_emitted_before == pytest.approx(
            math.hypot(100.0, 10.0) + 10.0
        )
        # Post-sweep + post-merge emission: one 100-unit rapid remains.
        assert outcome.direction_sweep_emitted_after == pytest.approx(100.0)
        assert outcome.optimized_doc.rapid_distance() == pytest.approx(100.0)
