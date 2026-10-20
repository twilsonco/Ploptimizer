# TODO List
- [ ] Move @font_showcase.py into `fonts/` and have it save the showcases in the `fonts/showcases/` directory.
  - The showcases need some changes as well. 
    - They should be simplified compared to normal toolpath previews; no axes, no grid. Just the title and the showcase "label". This can be achieved by adding more optional args to @plotter.py to control the visibility of axes and grid.
    - They should be saved as jpg instead of PDF.
    - Symbol font showcase titles need the font name displayed in an alpha font; do not include the font name using the symbol font. I mean the title in the showcase, not the plot title (the plot title can remain as is).
- [ ] Revisit the stdout output when using the generation pipeline. We want to streamline it so that when an agent uses it, it isn't flooded with verbose or unnecessary messages. It should output the bare minimum, with only WARNING/ERROR messages displayed. Then, a report txt file should be made with each generation and put adjacent to the created pdf/plt directories.