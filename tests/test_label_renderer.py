class TestStrikethroughRenderDispatch:
    """``_render_line_block`` draws one unbroken ``<s>`` stroke per struck run."""

    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    @classmethod
    def _resolve(cls, *lines, **job_kwargs):
        """Resolve a markup job against the inventory (so a step has rungs)."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        return resolve_job_spec(_markup_job(*lines, **job_kwargs), cls._INVENTORY)

    def test_markup_renders_plain_text_not_the_tags(self) -> None:
        """The engraving measures the plain text; the tags never appear."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_strike_sits_at_the_cap_band_midline(self) -> None:
        """Unset offset centres the stroke on half the rendered cap height."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_explicit_offset_places_the_stroke(self) -> None:
        """``strikethrough_offset`` is the height above the baseline, 0.0 included."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_stroke_is_never_broken_by_descenders(self) -> None:
        """A descender punches through the strike (the "no breaks" contract).

        The underline's ``<u>Depth</u>`` twin breaks around the ``p``; the
        strikethrough draws one continuous stroke over the very same glyphs.
        """
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_mid_cap_strike_never_grows_the_ink_box(self) -> None:
        """The mid-cap stroke sits inside the cap band, so bounds are unchanged."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_offset_above_the_cap_grows_the_box(self) -> None:
        """A stroke above the capline extends the block upward (counts for margins)."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_strikethrough_render_gate(self) -> None:
        """The gate is empty for plain / italic-only lines, runs for ``<s>``."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_nested_strikethrough_draws_one_stroke(self) -> None:
        """``<s>a<s>b</s>c</s>`` is one struck run -- one stroke (idempotent)."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_break_spaces_splits_runs_at_spaces(self) -> None:
        """``strikethrough_break_spaces`` ends each run at a space (bare gap).

        False (the default) keeps one continuous stroke across the spaces;
        True splits the run at each space, so every word gets its own strike
        spanning its own ink and the inter-word space stays bare.
        """
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_break_spaces_leaves_the_space_bare(self) -> None:
        """With the flag on, no strike stroke covers the inter-word space."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_word_chunk_mode_attaches_strike_to_last_word(self) -> None:
        """WORD mode keeps the word split and never drops the strike."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_strike_and_underline_coexist(self) -> None:
        """``<u><s>AV</s></u>`` engraves the underline first, then the strike."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_strike_splits_at_the_scale_boundary(self) -> None:
        """One ``<s>`` span across a scale boundary draws two strokes.

        Each struck run engraves with its own tool, so the gate splits the span
        where the cutter changes (the plain word at the line cutter, the
        scaled word at its effective-height cutter).
        """

    def test_strike_strokes_land_on_their_run_cutters(self) -> None:
        """The pen split recovers each strike stroke's tool from its span."""

    def test_strike_alone_on_a_single_tool_line_keeps_one_pen(self) -> None:
        """A strike on a uniform-tool line needs no pen split (fast path)."""

    def test_nested_markup_shears_and_strikes(self) -> None:
        """``<i>AV <s>WX</s> YZ</i>`` shears every glyph, strikes the run."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_ttf_strike_draws_one_stroke(self) -> None:
        """The TTF path draws the same unbroken stroke (PLT parity)."""
        from plt_optimizer.generate.resolution import resolve_job_spec

    def test_natural_width_is_unchanged_by_the_mid_cap_strike(self) -> None:
        """The mid-cap stroke spans the glyphs' own ink box (width unchanged)."""
        from plt_optimizer.generate.resolution import resolve_job_spec
