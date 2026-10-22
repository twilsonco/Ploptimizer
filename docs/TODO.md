# TODO List

- [ ] Add support for bullet lists and enumerated lists. This hopefully can utilize some of the existing code for the `new_line` field.
  - This will take some thought for how to implement.
  - The bullet/enumerated list fields could be alternatives to `text` within a `content` block, allowing each `text` item to either be a simple text line or a bullet/enumerated list item, called `itemize`/`enumerate` respectively.
  - For bullet lists, we could include a `bullet_string` field to specify the character(s) used for each bullet (e.g., "•").
    - `bullet_string` should be specifiable for each `text` item in the bullet list as well.
  - For enumerated lists, we could include a few helper fields:
    - `start_index`: The starting number for the enumerated list (default 1).
    - `counter_style`: The style of the enumerated counter ("decimal", "lower-alpha", "upper-alpha", "lower-roman", "upper-roman").
    - `counter_prefix`: A string to prefix each enumerated item with (e.g., "Step " for "Step 1", "Step 2", etc.).
    - `counter_suffix`: A string to suffix each enumerated item with (e.g., "." for "1.", "2.", or ")" for "1)", "2)", etc.).
  - In addition to handling the placement of bullets and counters, these will work well with `optimize_line_content` (and `optimize_line_content_max_lines`), and when a bullet/enumerated item spans multiple lines, its 2nd and subsequent lines will be properly left-aligned with the start of the text, not the bullet or counter, similar to how it works in MS Word, LaTeX, etc.
- [ ] When using a PLT font and text is horizontally compressed, it's polygonized and represented as a series of line segments. This results in good-quality, accurate renderings. However, it increases engraving time. When VisionPro or EngraveLab horizontally scales text, it's resulting toolpaths still utilize arc commands rather than polygonizing the text, so we should be able to do it to. It amounts to fitting one arc with a series of arc commands. It's a tractable problem. We need to figure it out. The resulting toolpaths will contain far fewer commands and run faster.
- [ ] Implement `extra="forbid"` in the Pydantic models to prevent unexpected fields from being accepted. This will help catch typos and enforce strict schema adherence, ensuring that only the defined fields are allowed in the input data. This should be done everywhere on the basis that if a field is added, the user expects it to work, and if the field has a typo, the user needs to know about it.