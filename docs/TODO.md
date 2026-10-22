# TODO List
- [x] Revisit the stdout output when using the generation pipeline. We want to streamline it so that when an agent uses it, it isn't flooded with verbose or unnecessary messages. It should output the bare minimum, with only WARNING/ERROR messages displayed. Then, report txt/json files should be made with each toolpath generation and put in a `generation_report` directory adjacent to the created pdf/plt directories.
  - **Shipped.** The `generate` CLI's stdout is now **exactly one summary line** (`Generated '<job>': N PLT file(s), M PDF(s) in <out> (report: <out>/generation_report/report.json)`); WARNING/ERROR records go to **stderr**. `-v` is the only verbose affordance (it prints the full layout report to stdout and raises the log level).
  - **The pipeline logs at WARNING, not INFO.** The operational chatter (stage timers, per-plate optimization summaries, the per-strategy benchmark tables) is INFO, and the report already carries everything worth knowing, so the default run keeps it out of the console *and* out of `logs_generate/generate.log` (the log file is WARNING too, so a default run writes no INFO records at all). `-v` escalates the whole pipeline — logger, console handler and file handler — to DEBUG.
  - **The per-stage wall clock is decoupled from the log level.** The stage timing line is a log *record*, and the logging level gates it -- at WARNING the INFO record never exists. The new `utils.stage_timing.StageTimings` collector records the elapsed seconds directly, so the report's `timings` section is unchanged; the profiling harness (which runs the pipeline at INFO) still reads the log records.
  - **The spawned ensemble workers inherit the log policy.** The optimizer's `ParallelEnsembleStrategy` runs its strategies in a spawned `ProcessPoolExecutor` pool, and a worker starts with a clean interpreter -- it builds its OWN logger and never saw the parent's WARNING level / stderr console / `generate.log`. Left alone it logged at the default INFO to stdout (the `Running <strategy> on N blocks` chatter) and created a stray `logs/optimizer.log`. The CLI now calls `utils.logging.publish_log_policy(...)`, which records the policy in the environment (`PLT_LOG_LEVEL` / `PLT_LOG_CONSOLE` / `PLT_LOG_FILE`); the `spawn` start method copies it into the child.
  - **Report files** (a new `generation_report/` directory, a sibling of `plt/` and `pdf/`): `report.json` (machine-readable: `job` / `summary` / `timings` / `outputs` / `labels[]` → `lines[]` → typed `findings[]` / `warnings[]` / `errors[]`) and `summary.txt` (a human-readable digest of the same data). The report is written **on success AND on failure** — a failed job leaves `job.status = "failed"` with `job.error = {type, message, stage}` plus the warnings collected before the failure, so the reason is available without re-reading stdout.
  - **Structured findings**: `layout_report.collect_line_findings()` returns the per-line effects as `LineFinding` dataclasses; the text report and the JSON report are both rendered from it, so they can never diverge (the text output stays byte-identical to before — pinned by `tests/test_layout_report.py`).
  - **Implementation**: `plt_optimizer/generate/generation_report.py` (new: `ReportCapture`, `build_report`, `format_summary`, `write_report`), `utils.stage_timing.StageTimings` (the level-independent timing collector), `utils.logging.apply_log_levels` (the per-handler level/stream retuning, shared with `cli/benchmark.py`), and the CLI's rewritten `run()`.
  - **Pins**: `tests/test_generation_report.py` (JSON shape, typed findings, the log capture, the summary, the on-disk artifacts), `tests/test_stage_timers.py` (the timing collector), and the updated `tests/test_cli.py` (one stdout line, the WARNING default level, an INFO-free log file, the report files on success and on failure, the error path's stderr + failure report).
  - **Follow-up considered, not shipped**: a helper script that parses the report JSON into on-the-fly summaries / tables of contents. The nested schema is designed so plain `jq` does this directly (e.g. `jq -r '.labels[].lines[] | select(.findings[]? | select(.type=="compression")) | .plain_text'`), so the script stays optional.
- [x] Make a new html markup that is similar to the explicit cutter specification `<c:D>`, but for specifying that the cutter *should not change as a result of any other html markup applied to the text segment. We'll call this "original cutter" and use `<oc>` for the start and `</oc>` for the end of the text segment. For example `<oc><scale:2.0>larger text without larger cutter</scale></oc>` would scale up the text without affecting the cutter size. Now, significantly, `<oc>` only prevents cutter changes *due to other html markup that applies to the text segment; the cutter can still change e.g. due to horizontal text compression on long lines, or due to global horizontal text scaling due to compression applied to some other line. The motivation for `<oc>` is to be able to easily make sure that marked-up text doesn't end up with a different cutter from the non-marked-up text around it.
  - **Shipped** as the eleventh markup tag: `Style.original_cutter` (a set-to-True flag field), `register_tag("oc", ...)` registered LAST, `StyledText.original_cutter_flags`, `ResolvedTextLine.original_cutter_flags` / `has_original_cutter`, and the `<oc>` branch in `per_char_cutters` (between the `<c:D>` override and the scale/font ladder). The pin is a DERIVED reference to the line's own `cutter_diameter`, so the compression-driven downsize moves it with the line (the carve-out) while the markup-induced ladders never touch it.
- [ ] Add a new optional bool field, `new_line` that *only* exists as a TextAttribute (i.e. at line-level), and not in job-config.json. Default false. This field interacts with `optimize_line_content`. If true, then it forces the line to begin on its own line in the label. 
  - This allows for a job spec to, e.g., specify three very long lines, each of which begins on a new line but has its content optimized onto the following lines as necessary. 
  - Combines with `optimize_line_content_max_lines` so that the user can specify the max number of lines each line is potentially expanded to.
  - e.g.
    ```yaml
    job:
      job_name: Coolsys Walmart 5168
      width: 24
      height: 16
      text_height: 1.0
      v_margin: 1.0
      h_margin: 1.0
      optimize_line_content: true
      h_compress_global: true
      font: dino sc
      labels:
        - id: label
          content:
            - text: "Always charge secondary CO2 system through a filter drier."
              new_line: true
              optimize_line_content_max_lines: 2
            - text: "• Do not trap liquid by closing charging station and isolation valves while servicing."
              new_line: true
              optimize_line_content_max_lines: 2
            - text: "• Always vacuum charging line after charging is complete."
              new_line: true
              optimize_line_content_max_lines: 2
            - text: "• Charge secondary CO2 system with 99.9% pure or higher food and beverage grade CO2."
              new_line: true
              optimize_line_content_max_lines: 3
    ```
- [ ] Add support for bullet lists and enumerated lists.
  - This will take some thought for how to implement.
  - The bullet/enumerated list fields could be alternatives to `text` within a `content` block, allowing each `text` item to either be a simple text line or a bullet/enumerated list item, called `itemize`/`enumerate` respectively.
    - If we decide this should be an alternative to `content`, then it's important to maintain the order, so that e.g. a bullet list may follow a `content` block and precede another `content` block or an enumerated list, and that order is reflected in the rendered label.
  - For bullet lists, we could include a `bullet_string` field to specify the character(s) used for each bullet (e.g., "•").
    - `bullet_string` should be specifiable for each `text` item in the bullet list as well.
  - For enumerated lists, we could include a few helper fields:
    - `start_index`: The starting number for the enumerated list (default 1).
    - `counter_style`: The style of the enumerated counter ("decimal", "lower-alpha", "upper-alpha", "lower-roman", "upper-roman").
    - `counter_prefix`: A string to prefix each enumerated item with (e.g., "Step " for "Step 1", "Step 2", etc.).
    - `counter_suffix`: A string to suffix each enumerated item with (e.g., "." for "1.", "2.", or ")" for "1)", "2)", etc.).
  - In addition to handling the placement of bullets and counters, these will work well with `optimize_line_content` (and `optimize_line_content_max_lines`), and when a bullet/enumerated item spans multiple lines, its 2nd and subsequent lines will be properly left-aligned with the start of the text, not the bullet or counter, similar to how it works in MS Word, LaTeX, etc.