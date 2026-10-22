# TODO List

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
- [ ] When using a PLT font and text is horizontally compressed, it's polygonized and represented as a series of line segments. This results in good-quality, accurate renderings. However, it increases engraving time. When VisionPro or EngraveLab horizontally scales text, it's resulting toolpaths still utilize arc commands rather than polygonizing the text, so we should be able to do it to. It amounts to fitting one arc with a series of arc commands. It's a tractable problem. We need to figure it out. The resulting toolpaths will contain far fewer commands and run faster.