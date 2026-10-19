# TODO List
- [ ] Specify symbol (non-alphanumeric) PLT font files by a trailing `_sym` or `_symbol`. E.g. `wingdings_0.35_^_S4_sym.plt`. Check for this in @extract_plt_fonts.py and then store `symbol_font` as a bool attribute in the extracted font's json file. All fonts will have `symbol_font` set to either `True` or `False`.
- [ ] Move @font_showcase.py into `fonts/` and have it save the showcases in the `fonts/showcases/` directory.
  - The showcases need some differences as well. 
    - They should be simplified compared to normal toolpath previews; no axes, no grid. Just the title and the showcase "label". This can be achieved by adding more optional args to @plotter.py to control the visibility of axes and grid.
    - They should be saved as jpg instead of PDF.