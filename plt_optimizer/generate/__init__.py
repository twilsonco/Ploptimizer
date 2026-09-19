"""PLT-Optimizer generate module.

This package provides the generation pipeline for creating PLT files from
YAML job specifications (schema parsing, replacement expansion, resolution,
bounds-aware bin packing, per-label rendering, and per-cutter assembly).

Consumers import submodules directly (e.g.
``from plt_optimizer.generate.vectorize import export_per_cutter_plts``);
this package root intentionally exposes no re-export facade.
"""
