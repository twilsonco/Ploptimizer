#!/bin/bash
cd /Users/haiiro/NoSync/PLT-Optimizer && uv run pytest tests/test_optimizer.py::TestParallelEnsembleStrategy -v 2>&1 | head -60