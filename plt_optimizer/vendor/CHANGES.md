# Vendored PyPhotoCollage — provenance and deviations

| | |
|---|---|
| Upstream | https://github.com/tawphoto/PyPhotoCollage (MIT, © Tim Wilson) |
| Upstream file | `PhotoCollage.py` |
| Local file | `pyphotocollage.py` |
| Upstream SHA-256 | `1aede908f7be0eaaae8199fadd3bdb81b9085e5c00be4dbfb29fec5cd92c9a33` |
| License | `LICENSE` (unmodified, bundled) |

## Source deviations

**None.** The file is byte-identical to upstream (hash above; verify with
`shasum -256 plt_optimizer/vendor/pyphotocollage.py`). Everything that makes it
usable as a proof-sheet engine — the panel list, the layout parameters,
determinism, and the CLI-argument surface — is supplied by the adapter,
`plt_optimizer/generate/proof_composite.py`, not by editing upstream.

Reasons the copy is vendored rather than installed:

* Upstream's package metadata requires a newer Python than the plotter PC runs
  (the deployment floor is Python 3.8 / Windows 7 — see root `AGENTS.md` §5),
  and vendoring keeps the build offline.
* The shop tunes the collage through the tool's **own CLI flags** (see the
  adapter's `PROOF_COLLAGE_DEFAULT_ARGS`), which is only an honest interface
  while the file is the upstream CLI, unmodified.
* The module's parallelism is `multiprocessing.pool.ThreadPool` (threads, not
  processes), so importing it adds no Windows spawn / PyInstaller freezing
  hazard; `run_tray.py` already calls `multiprocessing.freeze_support()`.

## Determinism

Upstream's `nested` collage type, its `random` image ordering, and
`--rotate-images-max-deg` draw from the global `random` module. A proof sheet
must be reproducible from the job, so the adapter seeds the generator
deterministically before every call and its default argument string
selects `rows` layout with input order. The module has **no** import-time
`random.seed()` side effect, so seeding in the adapter cannot leak into, or be
overwritten by, unrelated code.

## Quality gates

Excluded from the repo's gates on purpose: ruff (`[tool.ruff] extend-exclude`),
mypy (`[[tool.mypy.overrides]] ignore_errors`), coverage
(`[tool.coverage.report] omit`). The adapter is typed, linted, and tested to the
repo's bar.

## Resync checklist

1. Copy the new upstream `PhotoCollage.py` over `pyphotocollage.py`.
2. Re-record the SHA-256 above.
3. Run `pytest tests/test_proof_composite.py` — it pins the adapter's contract
   against the vendored engine, including the argument surface and layout
   geometry.
