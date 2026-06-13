"""Core parsing and writing functionality for PLT files."""

from plt_optimizer.core.parser import PLTParser, ParseError
from plt_optimizer.core.writer import PLTWriter, WriteError
from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    FooterCommand,
    HeaderCommand,
    PenState,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.profiler import Extent, ProfileResult, Profiler, ProfilerError
from plt_optimizer.core.chunker import (
    Chunker,
    ChunkerConfig,
    ChunkerError,
    LinearChunker,
    MacroBlock,
)
from plt_optimizer.core.optimizer import (
    BlockConnection,
    BlockTraverseState,
    NoOpStrategy,
    OptimizationError,
    OptimizationResult,
    OptimizationStrategy,
    OptimizerEngine,
    NearestNeighbor2OptStrategy,
)
from plt_optimizer.core.reassembler import (
    MetricsCalculator,
    Reassembler,
    ReassemblerError,
)

__all__ = [
    # Parser/Writer
    "PLTParser",
    "ParseError",
    "PLTWriter",
    "WriteError",
    # Models
    "ArcSegment",
    "Coordinate",
    "FooterCommand",
    "HeaderCommand",
    "PenState",
    "PLTDocument",
    "Segment",
    "StrokePath",
    "StrokeSegment",
    # Profiler
    "Extent",
    "ProfileResult",
    "Profiler",
    "ProfilerError",
    # Chunker
    "Chunker",
    "ChunkerConfig",
    "ChunkerError",
    "LinearChunker",
    "MacroBlock",
    # Optimizer
    "BlockConnection",
    "BlockTraverseState",
    "NoOpStrategy",
    "OptimizationError",
    "OptimizationResult",
    "OptimizationStrategy",
    "OptimizerEngine",
    "NearestNeighbor2OptStrategy",
    # Reassembler
    "MetricsCalculator",
    "Reassembler",
    "ReassemblerError",
]