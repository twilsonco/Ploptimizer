# TODO List

- [ ] Add support for "simple" label layouts that do not use rectpack. The simple layout modes will be `layout: columns-simple` and `layout: rows-simple`. `columns-simple` is a simple top-to-bottom left-to-right layout of one label (e.g. a single label specification) at a time with no label rotation, while `rows-simple` is a simple left-to-right top-to-bottom layout of one label at a time.
  - This mimics the behavior of the VisionPro "Multiples" feature ("Badges" in EngraveLab).
  - The only "optimization" will be a check to ensure no overlap between labels; e.g. if one column has half 1x3 and half 1x4, and the 1x4 labels continue to the next column, it needs to either start the second column at x=3 so they fit flush and then move over to x=4 once it gets down the column to the point where the 1x4 labels are in the first column, OR it can just start at x=4 for the whole second column in order to keep things simpler. I think the latter sounds better.
  - This will only be an option for rectangular labels; round labels will always be optimized with their non-rectpack layout optimization. 
- [ ] Improve extracted PLT fonts by transforming certain characters to guarantee character height. For example:
  - taking "line height" to mean (capline - baseline),
    - forward slash and backslash should always descend by 5% of the line height and ascend by 5% of the line height
    - vertical bar should always descend by 10% of the line height and extend up to the capline
    - braces {} should always descend and ascend by 10% of the line height
    - parentheses () should always descend and ascend by 5% of the line height
    - brackets [] should always descend and ascend by 2% of the line height
  - We want to do this in an abstracted manner, e.g. by defining a class for this type of modification in `extract_plt_fonts.py` so that we can easily construct a list of such transformation for extracted characters in terms of signed ascent and/or descent in units of percent of line height. Or better would be to do so in a new json file that lives in the `fonts/` directory.
  - Only applies to non-symbol fonts.