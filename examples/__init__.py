"""Examples package for PLT-Optimizer.

Adding this file ensures that the example scripts in this directory (e.g.
``examples.benchmark``) are importable as regular modules. This is required
when the benchmark script parallelizes work via ``ProcessPoolExecutor``,
because the worker processes must be able to ``import examples.benchmark``
to invoke the worker function — on Windows the ``spawn`` start method is
the default and re-imports modules from scratch in each child.
"""