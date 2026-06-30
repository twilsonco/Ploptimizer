"""Tests for plt_optimizer/core/chunker.py module.

This module groups sequential stroke paths into MacroBlocks based on
tool-up jump distance thresholds.
"""

from __future__ import annotations

import pytest

from plt_optimizer.core.chunker import (
    Chunker,
    ChunkerConfig,
    ChunkerError,
    LinearChunker,
    MacroBlock,
)
from plt_optimizer.core.models import Coordinate, StrokePath, StrokeSegment


def _make_path(
    start: tuple[float, float],
    end: tuple[float, float],
    is_cutting: bool = True,
) -> StrokePath:
    """Helper to create a simple single-segment stroke path."""
    segment = StrokeSegment(
        start=Coordinate(x=start[0], y=start[1]),
        end=Coordinate(x=end[0], y=end[1]),
        is_cutting=is_cutting,
    )
    return StrokePath(pen_up_position=None, segments=(segment,))


class TestMacroBlock:
    """Tests for MacroBlock dataclass."""

    def test_path_count(self) -> None:
        """Test path_count property."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((20, 0), (30, 0)),
        ]
        block = MacroBlock(
            block_id=0,
            paths=tuple(paths),
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=30.0, y=0.0),
        )
        assert block.path_count == 2

    def test_total_segment_count(self) -> None:
        """Test total_segment_count property."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((20, 0), (30, 0)),
        ]
        block = MacroBlock(
            block_id=0,
            paths=tuple(paths),
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=30.0, y=0.0),
        )
        assert block.total_segment_count == 2


class TestChunkerConfig:
    """Tests for ChunkerConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = ChunkerConfig()
        assert config.threshold_multiplier == 1.5
        assert config.min_block_size == 1
        assert config.same_row_preference == 1.0

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = ChunkerConfig(threshold_multiplier=2.0, min_block_size=3)
        assert config.threshold_multiplier == 2.0
        assert config.min_block_size == 3


class TestChunkerChunk:
    """Tests for Chunker.chunk() method."""

    def test_chunk_empty_sequence_raises_error(self) -> None:
        """Test that chunking empty sequence raises ChunkerError."""
        chunker = Chunker()
        with pytest.raises(ChunkerError) as exc_info:
            chunker.chunk([], baseline_extent=100.0)
        assert "empty sequence" in str(exc_info.value.message).lower()

    def test_chunk_single_path_creates_one_block(self) -> None:
        """Test single path creates exactly one block."""
        paths = [_make_path((0, 0), (10, 0))]
        chunker = Chunker()
        blocks = chunker.chunk(paths, baseline_extent=100.0)

        assert len(blocks) == 1
        assert blocks[0].block_id == 0
        assert blocks[0].path_count == 1

    def test_chunk_close_paths_grouped_together(self) -> None:
        """Test that close paths are grouped into same block."""
        # Paths with small jumps between them (< threshold)
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((15, 0), (25, 0)),  # Jump of 5 units
            _make_path((30, 0), (40, 0)),  # Jump of 5 units
        ]
        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=1.5))
        baseline_extent = 10.0  # Threshold will be 15.0
        blocks = chunker.chunk(paths, baseline_extent)

        # All paths should be in single block since jump (5) < threshold (15)
        assert len(blocks) == 1
        assert blocks[0].path_count == 3

    def test_chunk_far_paths_separate_blocks(self) -> None:
        """Test that far paths create separate blocks."""
        # Paths with large jumps between them (> threshold)
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((1000, 0), (1010, 0)),  # Jump of ~990 units
        ]
        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=1.5))
        baseline_extent = 10.0  # Threshold will be 15.0
        blocks = chunker.chunk(paths, baseline_extent)

        # Paths should be in separate blocks since jump (990) > threshold (15)
        assert len(blocks) == 2

    def test_chunk_min_block_size_respected(self) -> None:
        """Test that min_block_size prevents tiny blocks."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((15, 0), (25, 0)),  # Close to first (jump ~5)
            _make_path((1000, 0), (1010, 0)),  # Isolated path
        ]
        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=1.5, min_block_size=2))
        baseline_extent = 10.0

        blocks = chunker.chunk(paths, baseline_extent)

        assert len(blocks) == 1
        assert blocks[0].path_count >= 2

    def test_chunk_all_paths_isolated_raises_error(self) -> None:
        """Test that completely isolated paths raise error when min_block_size > 1."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((10000, 0), (10010, 0)),  # Very far apart
            _make_path((20000, 0), (20010, 0)),  # Even farther
        ]
        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=1.5, min_block_size=2))
        baseline_extent = 10.0

        with pytest.raises(ChunkerError) as exc_info:
            chunker.chunk(paths, baseline_extent)
        assert "Could not create any valid blocks" in str(exc_info.value.message)

    def test_chunk_entrance_exit_coordinates(self) -> None:
        """Test that block entrance/exit coordinates are correct."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((15, 5), (25, 5)),  # Different y
        ]
        chunker = Chunker()
        blocks = chunker.chunk(paths, baseline_extent=100.0)

        assert len(blocks) == 1
        block = blocks[0]
        # Entrance should be first path's start
        assert block.entrance.x == 0.0
        assert block.entrance.y == 0.0
        # Exit should be last path's end
        assert block.exit.x == 25.0
        assert block.exit.y == 5.0

    def test_chunk_path_with_no_segments_skipped(self) -> None:
        """Test that paths with no segments are skipped."""
        empty_path = StrokePath(pen_up_position=None, segments=())
        valid_path = _make_path((100, 0), (110, 0))

        chunker = Chunker()
        blocks = chunker.chunk([empty_path, valid_path], baseline_extent=10.0)

        # Empty path should be skipped
        assert len(blocks) == 1
        assert blocks[0].path_count == 1

    def test_chunk_threshold_calculation(self) -> None:
        """Test threshold is correctly calculated from multiplier and baseline."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((100, 0), (110, 0)),  # Jump of ~90
        ]
        config = ChunkerConfig(threshold_multiplier=10.0)  # threshold = 10 * baseline
        chunker = Chunker(config=config)
        baseline_extent = 10.0  # So threshold = 100.0

        blocks = chunker.chunk(paths, baseline_extent)

        # With threshold=100, jump of ~90 should be within threshold -> single block
        assert len(blocks) == 1


class TestLinearChunker:
    """Tests for LinearChunker subclass."""

    def test_default_threshold_is_tighter(self) -> None:
        """Test that LinearChunker uses tighter threshold (1.2x vs 1.5x)."""
        linear = LinearChunker()
        assert linear._config.threshold_multiplier == 1.2

        standard = Chunker()
        assert standard._config.threshold_multiplier == 1.5

    def test_linear_chunker_respects_custom_config(self) -> None:
        """Test that custom config overrides default."""
        config = ChunkerConfig(threshold_multiplier=3.0)
        linear = LinearChunker(config=config)
        assert linear._config.threshold_multiplier == 3.0


class TestChunkerHelpers:
    """Tests for Chunker helper methods."""

    def test_get_segment_start(self) -> None:
        """Test _get_segment_start returns correct coordinate."""
        segment = StrokeSegment(
            start=Coordinate(x=10.0, y=20.0),
            end=Coordinate(x=30.0, y=40.0),
            is_cutting=True,
        )
        chunker = Chunker()
        start = chunker._get_segment_start(segment)
        assert (start.x, start.y) == (10.0, 20.0)

    def test_get_segment_end(self) -> None:
        """Test _get_segment_end returns correct coordinate."""
        segment = StrokeSegment(
            start=Coordinate(x=10.0, y=20.0),
            end=Coordinate(x=30.0, y=40.0),
            is_cutting=True,
        )
        chunker = Chunker()
        end = chunker._get_segment_end(segment)
        assert (end.x, end.y) == (30.0, 40.0)

    def test_create_macro_block(self) -> None:
        """Test _create_macro_block creates block with correct properties."""
        paths = [_make_path((5, 10), (15, 20))]
        chunker = Chunker()
        existing_blocks: list[MacroBlock] = []

        block = chunker._create_macro_block(existing_blocks, paths)

        assert block.block_id == 0
        assert len(block.paths) == 1
        assert block.entrance.x == 5.0
        assert block.entrance.y == 10.0
        assert block.exit.x == 15.0
        assert block.exit.y == 20.0

    def test_create_macro_block_increments_id(self) -> None:
        """Test that block IDs increment correctly."""
        paths = [_make_path((i * 10, 0), (i * 10 + 5, 0)) for i in range(3)]
        chunker = Chunker()

        block1 = chunker._create_macro_block([], [paths[0]])
        assert block1.block_id == 0

        block2 = chunker._create_macro_block([block1], [paths[1]])
        assert block2.block_id == 1

        block3 = chunker._create_macro_block([block1, block2], [paths[2]])
        assert block3.block_id == 2


class TestChunkerSameRowPreference:
    """Tests for same_row_preference feature in Chunker."""

    def test_same_row_preference_default_is_neutral(self) -> None:
        """Test that default same_row_preference=1.0 applies no y-penalty."""
        config = ChunkerConfig(same_row_preference=1.0)
        assert config.same_row_preference == 1.0

    def test_same_row_large_x_gap_grouped_with_pref(self) -> None:
        """Test that strokes on same row with large x-gap are grouped when pref > 1.

        Note: For same-row strokes (dy=0), y_penalty is always 0 regardless of
        same_row_preference, so grouping behavior is identical to Euclidean distance.
        This test verifies the feature doesn't break existing same-row behavior.
        """
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((200, 0), (210, 0)),  # Same y=0, dx=190
        ]
        baseline_extent = 50.0
        threshold_multiplier = 4.5  # threshold = 225

        config_pref1 = ChunkerConfig(
            threshold_multiplier=threshold_multiplier,
            same_row_preference=1.0,  # No penalty - jump is ~190 < 225 so grouped
        )
        chunker_pref1 = Chunker(config=config_pref1)
        blocks_pref1 = chunker_pref1.chunk(paths, baseline_extent)

        config_pref2 = ChunkerConfig(
            threshold_multiplier=threshold_multiplier,
            same_row_preference=10.0,  # y-penalty active but dy=0 so no effect
        )
        chunker_pref2 = Chunker(config=config_pref2)
        blocks_pref2 = chunker_pref2.chunk(paths, baseline_extent)

        assert len(blocks_pref1) == 1
        assert len(blocks_pref2) == 1

    def test_same_column_large_y_gap_separate_with_pref(self) -> None:
        """Test that strokes in same column with large y-gap stay separate when pref > 1."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((5, 200), (15, 210)),  # Same x~5, but dy=200
        ]
        baseline_extent = 50.0
        threshold_multiplier = 6.0  # threshold = 300

        config_pref1 = ChunkerConfig(
            threshold_multiplier=threshold_multiplier,
            same_row_preference=1.0,  # No penalty - euclidean dist ~200 < 300 so grouped
        )
        chunker_pref1 = Chunker(config=config_pref1)
        blocks_pref1 = chunker_pref1.chunk(paths, baseline_extent)

        config_pref2 = ChunkerConfig(
            threshold_multiplier=threshold_multiplier,
            same_row_preference=3.0,  # y_penalty = (3-1)*200 = 400, effective dist ~618 > 300
        )
        chunker_pref2 = Chunker(config=config_pref2)
        blocks_pref2 = chunker_pref2.chunk(paths, baseline_extent)

        assert len(blocks_pref1) == 1
        assert len(blocks_pref2) == 2

    def test_same_row_preference_equivalent_to_euclidean_at_default(self) -> None:
        """Test that same_row_preference=1.0 produces Euclidean distance behavior."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((200, 100), (210, 110)),  # dx=190, dy=100
        ]
        baseline_extent = 50.0

        config = ChunkerConfig(threshold_multiplier=5.0)  # threshold = 250.0
        chunker = Chunker(config=config)
        blocks = chunker.chunk(paths, baseline_extent)

        assert len(blocks) == 1


class TestChunkerStructuralMode:
    """Tests for is_structural=True mode in Chunker."""

    def test_chunk_structural_creates_one_block_per_path(self) -> None:
        """Test that structural mode creates one block per path."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((100, 0), (110, 0)),  # Far apart
            _make_path((200, 0), (210, 0)),
        ]
        chunker = Chunker()
        blocks = chunker.chunk(paths, baseline_extent=10.0, is_structural=True)

        assert len(blocks) == 3
        for i, block in enumerate(blocks):
            assert block.block_id == i
            assert block.path_count == 1

    def test_chunk_structural_skips_empty_paths(self) -> None:
        """Test that structural mode skips paths with no segments."""
        empty_path = StrokePath(pen_up_position=None, segments=())
        valid_path = _make_path((100, 0), (110, 0))

        chunker = Chunker()
        blocks = chunker.chunk([empty_path, valid_path], baseline_extent=10.0, is_structural=True)

        assert len(blocks) == 1
        assert blocks[0].path_count == 1

    def test_chunk_structural_ignores_threshold(self) -> None:
        """Test that structural mode ignores threshold and groups nothing."""
        paths = [
            _make_path((0, 0), (10, 0)),
            _make_path((10000, 0), (10010, 0)),  # Massive gap
        ]
        chunker = Chunker()
        blocks = chunker.chunk(paths, baseline_extent=1.0, is_structural=True)

        # Even though paths are very far apart, structural mode puts each in its own block
        assert len(blocks) == 2

    def test_chunk_structural_single_path(self) -> None:
        """Test structural mode with a single path."""
        paths = [_make_path((0, 0), (10, 0))]
        chunker = Chunker()
        blocks = chunker.chunk(paths, baseline_extent=100.0, is_structural=True)

        assert len(blocks) == 1
        assert blocks[0].block_id == 0

    def test_chunk_structural_block_entrance_exit(self) -> None:
        """Test that structural mode sets correct entrance/exit coordinates."""
        path = _make_path((5.0, 10.0), (15.0, 20.0))
        chunker = Chunker()
        blocks = chunker.chunk([path], baseline_extent=100.0, is_structural=True)

        assert len(blocks) == 1
        block = blocks[0]
        assert block.entrance.x == 5.0
        assert block.entrance.y == 10.0
        assert block.exit.x == 15.0
        assert block.exit.y == 20.0
