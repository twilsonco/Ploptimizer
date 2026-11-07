# TODO List

- [x] Add relief engraving support
  - A ground truth example has been created with EngraveLab and provided:
    - `examples/plt_relief_engraving/A_no_relief.plt` is the toolpath for a capital "A" centered on a 1x1 plate using single-stroke dino font, 1/2" text height (0.44" with a 0.06" cutter).
    - The relief version takes two different cutters, a coarse one for the background and a fine one for the details. Only one cutter could have been used, but EngraveLab provided the option to specify a main and a fine cutter.
    - `examples/plt_relief_engraving/A_relief_s-sweep_coarse_stroke_0.06.plt` is the toolpath for the coarse stroke of the relief engraving using a 0.06" cutter. A larger cutter, e.g. a 0.125" cutter, wasn't used because the "A"'s hole is so small that the larger cutter couldn't be used at all.
    - `examples/plt_relief_engraving/A_relief_s-sweep_fine_stroke_0.005.plt` is the toolpath for the fine stroke of the relief engraving using a 0.06" cutter. This includes the corners of the plate, as well as the interior corners of the "A" (the three corners of the triangular hole, and the two corners of the "A"'s outer shape).
    - `examples/job_specs/relief_simple.yaml` is a job spec yaml that should reproduce the two reference toolpaths for the relief engraving.
    - **GOAL: The goal for this implementation is to be able to run the job spec in `examples/job_specs/relief_simple.yaml` and reproduce the reference toolpaths for the relief engraving. Not byte-for-byte, but our toolpaths should be geometricaly equivalent.**
  - Our implementation will take inspiration from this ground truth, but ours will be simpler to use.
    - Cutter selection will be automatic based on a quality preset. The system will use the largest cutter in selection for the parts of the relief engraving that can accommodate it, and smaller cutters for the finer details, progressively down to the smallest cutter available.
  - Introduce a new optional bool field to the job spec yaml schema, `relief_engraving` (default false), to indicate whether relief engraving should be performed for the job.
    - Cascade job -> plate -> label. So some labels can be relief and others not.
  - Introduce a new optional field to specify the quality level for relief engraving, e.g. `relief_engraving_quality` with possible values "coarse", "medium", and "fine", that will automatically select cutters for relief engraving.
    - The quality level can also cascade from job -> plate -> label, allowing different levels of detail for different parts of the job.
    - Also in job-config.json set to "medium".
    - The presets' meaning is simple: they determine the maximum number of cutters that will be used for the relief engraving. Coarse is 1, medium is 2, and fine is 3.
    - Which cutters are used for a given preset is dynamic, based on the available cutters and the requirements of the relief engraving. It may be the case that the engraved text is so small that only the finest cutter can be used, even if the "fine" preset is used.
    - In general, the reason for multiple cutters is for thinks like interior holes on engraved text. If an "A" is to be engraved, then we want to be able to use a coarse cutter for the background and perhaps for part of the interior hole on the "A", and then a fine cutter in order to properly handle the sharp corners at the label and text corners.
    - We'll need to discuss the logic for selecting cutters when a low quality preset is used. For example, if "coarse" is selected, then which size should be chosen when a number of valid cutter sizes are available that could be used for the background and larger areas of the engraving. 
    - The basic rule will be that there can be no regions that are entirely untouched. So for coarse, we'll use the largest cutter that can be used without needing to skip any text's interior holes. In this way, "coarse" will always be able to recover the level of detail necessary to make out the text.
    - The main reason to use "medium" over "coarse" is then not so much for increased detail, but for the ability to use a larger cutter for the background while a finer cutter can be used for letter interior regions. Thus it's more of a time saver.
    - All quality presets above "coarse" will utilize the smallest cutter in inventory for their finest cutter. So if the finest cutter is 0.005", then it is guaranteed to be used for medium and find quality.
    - The difference between medium and fine is then the ability to have an intermediate cutter that will be able to handle areas that are too small for the coarse cutter but large enough that ample time can be saved by not using the finest cutter for those areas. Thus we will use a metric defined by number of passes and/or toolpath length when determining which cutters are best. 
    - The "fine" `relief_engraving_quality` should include 1) the largest cutter that can be used in inventory (i.e. the largest cutter that can fit *anywhere* to help quickly remove the background), 2) a medium-size cutter that is as large as possible but small enough to be able to fit at least one stroke within interior text holes (e.g. interior holes of "o", "A", etc.), and then 3) the finest cutter in inventory in order to get as fine of detail as possible.
  - Introduce `relief_engraving_cutters` as an optional list field, cascading job -> plate -> label. If not specified, the normal behavior based on `relief_engraving_quality` will be used.
    - e.g. 
    ```yaml
    job:
      job_name: relief_simple
      relief_engraving_cutters: [0.06, 0.005]
    ```
    - Importantly, this *completely bypasses* the use of the tool inventory json file. The specified cutters will be used directly for relief engraving, regardless of what is available in the inventory. 
    - This is necessary so that our tests can be written so as to reproduce the reference files exactly, regardless of the shop inventory.
    - This is also necessary so that the implementation goal can be achieved.
  - The raster filling algorithm itself will be one or more simple CNC filling algorithms. The initial algorithm to use, and that demonstrated in the provided reference files, is the "S-Sweep" algorithm.
    - For S-Sweep, you start with an outline around the to-be-engraved text (and other symbols or markup) such that the resulting *unengraved, remaining* material is a relief of the desired text with the correct stroke width (the stroke width that would have been produced by the normal, concave engraving).
    - Then with the outline(s) established, you fill in with a rastoring pattern, back and forth, top to bottom.
    - The rastering is done using as contiguous of strokes as possible. So one stroke might have multiple passes, left-right-down-left-down-right-etc.. Now, I say "down" here but often the direction of the "vertical" parts of the stroke between horizontal passes will be at an angle, based on what best adheres to the existing outline. In `examples/plt_relief_engraving/A_relief_s-sweep_coarse_stroke_0.06.plt` lines 12-15, we see one such case where it goes right, then down and to the right diagonally, then back left for the next horizontal pass.
    - This process will be carried out until all areas that can be treated with the current cutter are filled, then we repeat the process with a/the finer cutter, where now the initial outline is that of the remaining unengraved material from the previous pass.
    - Drill holes are ignored by the relief filling; they're filled over.
    - Label borders may be filled or not, may be raised islands or not, depending on the value of `relief_engraving_preserve_border`, another optional field in the label spec that cascades and defaults to whatever produces the observed behavior in the reference files.
    - We'll abstract the rastering algorithm so that more rastering patterns can be easily added in the future, such as spiral rastering, without changing the core relief engraving logic.
  - During research/planning for this substantial feature, We'll include web research for CNC relief engraving techniques, optimal cutter selection strategies, and rastering algorithms to ensure efficient and high-quality engraving results.
- [ ] Add spiral rastering pattern for relief engraving. See `examples/plt_relief_engraving/A_relief_spiral_coarse_stroke_0.06.plt` and `examples/plt_relief_engraving/A_relief_spiral_fine_stroke_0.005.plt` for reference.
- [ ] Optimize unit tests so they complete faster. Let's aim for 120s or less for the entire test suite.