"""CLI tools for PLT-Optimizer.

This module provides command-line interface functionality including
the watch-directory daemon for automated file processing and the
optimize/generate subcommands.

Modules:
    optimize: Single-file optimization pipeline.
    generate: YAML specification to PLT generation (stub).
    watch: Directory watching daemon for batch optimization.

Functions:
    None - import submodules to access functionality.
"""

import plt_optimizer.cli.generate as generate
import plt_optimizer.cli.optimize as optimize
from plt_optimizer.cli.watch import WatchCommand, main, run_watcher_from_config

__all__ = [
    "optimize",
    "generate",
    "WatchCommand",
    "main",
    "run_watcher_from_config",
]
