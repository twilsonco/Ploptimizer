import subprocess
result = subprocess.run(
    ["uv", "run", "pytest", "tests/test_optimizer.py::TestParallelEnsembleStrategy", "-v"],
    capture_output=True,
    text=True,
    cwd="/Users/haiiro/NoSync/PLT-Optimizer"
)
print(result.stdout[:4000])
if result.stderr:
    print("STDERR:", result.stderr[:2000])
