#!/usr/bin/env python3
import subprocess
import sys

# Run pytest
r1 = subprocess.run(
    ["uv", "run", "pytest", "tests/test_parser.py", "tests/test_identity.py", "-v", "--no-cov"],
    cwd="/home/haiiro/dev/PLT-Optimizer",
    capture_output=True, text=True
)

# Run mypy  
r2 = subprocess.run(
    ["uv", "run", "mypy", 
     "plt_optimizer/core/profiler.py",
     "plt_optimizer/core/chunker.py", 
     "plt_optimizer/core/optimizer.py",
     "plt_optimizer/core/reassembler.py"],
    cwd="/home/haiiro/dev/PLT-Optimizer",
    capture_output=True, text=True
)

print(r1.stdout)
if r1.stderr: print("PYTEST STDERR:", r1.stderr, file=sys.stderr)

print(r2.stdout) 
if r2.stderr: print("MYPY STDERR:", r2.stderr, file=sys.stderr)
