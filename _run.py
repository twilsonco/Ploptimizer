import subprocess, sys
r1 = subprocess.run(["uv", "run", "pytest", "tests/test_parser.py", "tests/test_identity.py", "-v", "--no-cov"], cwd="/home/haiiro/dev/PLT-Optimizer", capture_output=True, text=True)
r2 = subprocess.run(["uv", "run", "mypy", "plt_optimizer/core/profiler.py", "plt_optimizer/core/chunker.py", "plt_optimizer/core/optimizer.py", "plt_optimizer/core/reassembler.py"], cwd="/home/haiiro/dev/PLT-Optimizer", capture_output=True, text=True)
with open("/home/haiiro/dev/PLT-Optimizer/_output.txt", "w") as f:
    f.write("=== PYTEST ===\n" + r1.stdout)
    if r1.stderr: f.write("=== PYTEST STDERR ===\n" + r1.stderr)
    f.write("\n=== MYPY ===\n" + r2.stdout)
    if r2.stderr: f.write("=== MYPY STDERR ===\n" + r2.stderr)
