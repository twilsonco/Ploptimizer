"""Vendored third-party layout engine: PyPhotoCollage (MIT, © Tim Wilson).

``pyphotocollage`` is an unmodified copy of ``PhotoCollage.py`` from
PyPhotoCollage (https://github.com/tawphoto/PyPhotoCollage), vendored so the
build stays offline and the platform floor (Python 3.8 / Windows 7) is honoured
— upstream's own package metadata asks for newer Python than the plotter PC
runs. ``LICENSE`` is upstream's, unmodified; ``CHANGES.md`` records the
provenance hash and the (current: zero) source deviations.

It is imported by :mod:`plt_optimizer.generate.proof_composite`, which renders
each plate's proof with matplotlib, captions it, and hands the panels to
``pyphotocollage.makeCollage``. Nothing else in the package may import it: the
adapter is the single seam through which the shop's CLI-arg string reaches
upstream's geometry, and it is the place that pins determinism.

Excluded from the repo's quality gates on purpose — ruff
(``[tool.ruff] extend-exclude``), mypy (``ignore_errors`` override) and
coverage (``omit``) — because it is third-party source; the adapter around it
is typed and tested to the repo's bar.
"""
