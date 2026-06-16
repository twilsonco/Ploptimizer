"""CLI tools for PLT-Optimizer.

This module provides command-line interface functionality including
the watch-directory daemon for automated file processing.

Modules:
    watch: Directory watching daemon for batch optimization.

Functions:
    None - import submodules to access functionality.
"""

from plt_optimizer.cli.watch import WatchCommand, main

__all__ = ["WatchCommand", "main"]