"""
Unit tests for the composite proof sheet (``proof_composite``).

The proof sheet tiles the pipeline's whole-sheet plate proofs into one JPEG
(``<job_id>_proof.jpeg``) through the vendored PyPhotoCollage engine, driven by
a shop-tunable string of that tool's own command-line arguments. These tests
cover the adapter's contract end to end (panels are real matplotlib figures
rasterized offscreen, so the assertions inspect actual decoded pixels):

* argument handling - shop defaults, per-flag overrides, invalid strings;
* caption text - plate number, material size, material name;
* composition - decodable JPEG, panel content present, size caps, gutter,
  background, truncation, every collage type the tool offers;
* determinism - the same job twice yields byte-identical sheets, including the
  seeded ``nested`` type and the seeded ``random`` order;
* panel geometry - the crop carries the plate's aspect, so the collage lays out
  plate shapes rather than figure margins;
* robustness - a panel that cannot be rasterized is skipped, and a job is never
  failed by the proof sheet.

This file is self-contained: the repo root is prepended to ``sys.path`` so it
also runs as a plain script (``.venv/bin/python tests/test_proof_composite.py``)
without installing the package.
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plt_optimizer.generate import proof_composite  # noqa: E402
from plt_optimizer.generate.proof_composite import (  # noqa: E402
    PROOF_COLLAGE_DEFAULT_ARGS,
    PROOF_SHEET_MAX_SIZE_PX,
    PROOF_SHEET_MIN_SIZE_PX,
    aspect_provided,
    collage_layout,
    compose_proof_sheet,
    max_size_provided,
    panel_caption,
    parse_collage_args,
    plate_size_in,
    proof_image_name,
    row_forcing_aspect,
)

pytest.importorskip("matplotlib")

from matplotlib.figure import Figure  # noqa: E402

# Distinct, saturated colors: the assertions read them back out of the sheet.
RED = "#c1121f"
GREEN = "#2a9d8f"
BLUE = "#1d3557"


def make_panel(
    color: str = RED,
    width_in: float = 2.0,
    height_in: float = 1.5,
) -> Figure:
    """A minimal plate-proof stand-in: a figure with known data limits + ink.

    Args:
        color: Stroke color drawn across the axes.
        width_in: Figure width in inches.
        height_in: Figure height in inches.

    Returns:
        A figure whose data box has a 2:1 aspect regardless of the figure size,
        exercising the panel crop.
    """
    fig = Figure(figsize=(width_in, height_in))
    ax = fig.add_subplot(111)
    ax.plot([0, 20], [0, 10], color=color, lw=3)
    ax.set_xlim(-1, 21)
    ax.set_ylim(-1, 11)
    fig.tight_layout()
    return fig


def panels(n: int = 3) -> list[tuple[str, Figure]]:
    """Captioned panels in plate order, one per color in :data:`_COLORS`."""
    colors = (RED, GREEN, BLUE)
    sizes = ((2.0, 1.5), (1.5, 2.0), (1.8, 1.8))
    return [
        (
            panel_caption(i + 1, sizes[i % len(sizes)], "ACM"),
            make_panel(colors[i % 3], *sizes[i % len(sizes)]),
        )
        for i in range(n)
    ]


def open_sheet(path: Path) -> Image.Image:
    """Decode the written sheet (fails the test when the JPEG is unreadable)."""
    assert path.exists(), f"proof sheet was not written: {path}"
    assert path.stat().st_size > 0, f"proof sheet is empty: {path}"
    with Image.open(path) as image:
        image.load()
        assert image.format == "JPEG", f"sheet is not JPEG: {image.format}"
        assert image.mode == "RGB", f"JPEG sheet must be RGB, got {image.mode}"
        return cast(Image.Image, image.convert("RGB"))


def dominant_colors(image: Image.Image) -> set[tuple[int, int, int]]:
    """Saturated colors present in the sheet (whites/greys ignored)."""
    found: set[tuple[int, int, int]] = set()
    colors: Any = image.getcolors(maxcolors=1_000_000) or []
    for _count, rgb in colors:
        r, g, b = rgb
        if max(r, g, b) - min(r, g, b) > 40 and max(r, g, b) < 250:
            found.add((r, g, b))
    return found


def stub_figure() -> Figure:
    """A panel figure that cannot be rasterized (the plotting-stub case)."""
    return cast(Figure, object())


class TestArgumentHandling:
    """The shop's argument string drives the sheet; the adapter owns defaults."""

    def test_defaults_are_the_sheet_recipe(self) -> None:
        opts = parse_collage_args("")
        assert opts.collage_type == "rows"
        assert int(opts.max_size) == 5000
        # Upstream's `-i 1000` default caps the whole sheet near 1000 px, which
        # renders a 24 in plate's glyph strokes unreadable.
        assert int(opts.init_size) == 2400
        assert int(opts.imagegap) == 12
        assert tuple(opts.background) == (255, 255, 255)
        assert opts.order == "input_order"
        # `-r` is deliberately NOT part of the defaults: it is computed from the
        # panels' own aspects (see effective_collage_aspect).
        assert not aspect_provided(PROOF_COLLAGE_DEFAULT_ARGS)

    def test_order_alias_the_help_text_advertises_is_normalized(self) -> None:
        # Upstream's `--order` help text advertises `input`; its validator only
        # accepts `input_order`, so the documented spelling would crash the run.
        assert parse_collage_args("-O input").order == "input_order"
        assert parse_collage_args("--order input").order == "input_order"
        assert parse_collage_args("-O filename").order == "filename"

    def test_user_args_override_named_flags_only(self) -> None:
        opts = parse_collage_args("-t columns -g 24")
        assert opts.collage_type == "columns"
        assert int(opts.imagegap) == 24
        # Unnamed flags keep the sheet's defaults.
        assert int(opts.max_size) == 5000

    def test_aspect_ratio_provided_detection(self) -> None:
        assert aspect_provided("-r 2.4")
        assert aspect_provided("-r2.4")
        assert aspect_provided("--target-aspect-ratio=2.4")
        assert not aspect_provided("-t nested -g 24")

    def test_quoted_background_value_parses(self) -> None:
        # Upstream's own rgb() parser takes bare "255,255,255"; parentheses are
        # rejected, so the defaults must not use them.
        opts = parse_collage_args("-b 10,20,30")
        assert tuple(opts.background) == (10, 20, 30)

    def test_defaults_string_itself_parses(self) -> None:
        assert PROOF_COLLAGE_DEFAULT_ARGS
        opts = parse_collage_args(PROOF_COLLAGE_DEFAULT_ARGS)
        assert opts.collage_type == "rows"

    def test_invalid_args_raise_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            parse_collage_args("--not-a-flag")

    def test_invalid_args_raise_through_the_config_validator(self) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        # An unknown flag is what the parser rejects; note `-t` is NOT
        # choice-constrained upstream (a typo'd collage type silently means
        # `rows`), so that is not a usable negative case.
        with pytest.raises(Exception, match="proof-sheet collage argument string"):
            JobDefaults(proof_collage_args="--not-a-flag")
        assert JobDefaults(proof_collage_args="  -t columns  ").proof_collage_args == "-t columns"
        assert JobDefaults(proof_collage_args="   ").proof_collage_args is None


class TestCollageLayout:
    """`-r` forces one row, `-S` is sized to the layout, `-g` survives the squeeze.

    Upstream picks its row count from a target row width of `(avg_h + avg_w) /
    2 * sqrt(n * -r)`, resizes the panels to a common height bounded by `-S`,
    resamples every row to the width of the narrowest one and finally resizes
    the sheet to `-S` - three chances to shrink a panel and its gutter. The
    computed `-r` is the value that lands the partition on one row and the
    computed `-S` is sized to the sheet that layout produces, so the panels reach
    the sheet at scale 1.0; a shop-pinned value is priced for its squeeze instead.
    """

    @staticmethod
    def _opts(collage_type: str = "rows", args: str = "") -> Any:
        return parse_collage_args(f"-t {collage_type} {args}".strip())

    def test_rows_are_forced_to_one_row_of_panels(self) -> None:
        """`-r` = (2*ar/(ar+1))**2 * n**2 puts every panel in one row."""
        layout = collage_layout(self._opts("rows"), [1.5, 1.5, 1.5], mean_height_px=1000)
        assert layout.aspect == pytest.approx(row_forcing_aspect(1.5, 3))
        assert layout.aspect == pytest.approx(1.2 * 1.2 * 9)  # (2*1.5/2.5)**2 * 3**2
        assert layout.row_scale == pytest.approx(1.0, abs=0.01)
        assert layout.gap_px == 12

    def test_sheet_size_is_the_layout_it_produces(self) -> None:
        """`-S` is sized to the sheet, so upstream's final resize is a no-op."""
        layout = collage_layout(self._opts("rows"), [1.5, 1.5, 1.5], mean_height_px=1000)
        assert layout.max_size_px == PROOF_SHEET_MIN_SIZE_PX
        assert layout.row_scale == pytest.approx(1.0, abs=0.01)
        # A bigger panel set (a 12-plate job) is priced the same way and still
        # lands at full panel size: the layout, not -S, sets the sheet.
        big = collage_layout(self._opts("rows"), [1.5] * 12, mean_height_px=1000)
        assert big.max_size_px <= PROOF_SHEET_MAX_SIZE_PX
        assert big.row_scale == pytest.approx(1.0, abs=0.02)

    def test_sheet_size_stays_inside_its_bounds(self) -> None:
        """Upstream's floor is the minimum; the memory ceiling the maximum."""
        assert collage_layout(self._opts("rows"), [1.5], mean_height_px=100).max_size_px == (
            PROOF_SHEET_MIN_SIZE_PX
        )
        huge = collage_layout(self._opts("rows"), [1.5] * 60, mean_height_px=2400)
        assert huge.max_size_px == PROOF_SHEET_MAX_SIZE_PX
        # Squeezed past the ceiling, the gutter is asked for larger to land 12 px.
        assert huge.row_scale < 1.0
        assert huge.gap_px > 12

    def test_every_layout_type_forces_one_row(self) -> None:
        """`columns` is partitioned by the same math, so its -r is not inverted."""
        layout = collage_layout(self._opts("columns"), [1.5, 1.5], mean_height_px=1000)
        assert layout.aspect == pytest.approx(row_forcing_aspect(1.5, 2))
        assert layout.row_scale == pytest.approx(1.0, abs=0.01)

    def test_nested_packs_by_area_and_pays_for_its_batches(self) -> None:
        """A `nested` job that recurses composites 3-panel batches at half scale."""
        opts = self._opts("nested", "-S 2000")
        layout = collage_layout(
            opts, [1.5] * 12, mean_height_px=1000, pinned_max_size=True, pinned_aspect=1.0
        )
        assert layout.row_scale == pytest.approx(0.43, abs=0.02)  # 0.87 sheet x 0.5 batch
        # 12 px lands ~12 px after the sheet squeeze and the batch's half scale.
        assert layout.gap_px == 28
        # A shop-pinned -r wins over the computed one there too.
        pinned = collage_layout(opts, [1.5] * 12, mean_height_px=1000, pinned_aspect=2.0)
        assert pinned.aspect == pytest.approx(2.0)

    def test_a_pinned_ratio_wins_over_the_row_forcing_value(self) -> None:
        """A shop `-r` is handed to the engine untouched, squeeze priced or not."""
        pinned = collage_layout(
            self._opts("rows", "-S 2000"),
            [2.0, 2.0, 2.0],
            mean_height_px=1000,
            pinned_aspect=1.375,
            pinned_max_size=True,
        )
        assert pinned.aspect == pytest.approx(1.375)
        # Upstream sizes the panels from `-S` for a 2-row layout (common height
        # `-S / rows`), which lands the sheet on its height: gutters ~12 px.
        assert pinned.row_scale == pytest.approx(0.99, abs=0.02)
        assert pinned.gap_px == 12

    def test_a_pinned_sheet_size_wins(self) -> None:
        """`-S` from the shop is honoured exactly, floor included."""
        opts = self._opts("rows", "-S 2000")
        pinned = collage_layout(opts, [1.5, 1.5, 1.5], mean_height_px=1000, pinned_max_size=True)
        assert pinned.max_size_px == 2000
        assert pinned.row_scale == pytest.approx(0.99, abs=0.02)
        # Below upstream's own default it is still the shop's number, not 5000.
        small = collage_layout(
            self._opts("rows", "-S 300"), [1.5] * 3, mean_height_px=100, pinned_max_size=True
        )
        assert small.max_size_px == 300
        assert small.row_scale < 1.0
        assert small.gap_px > 12
        # Without the pin, the sheet is sized to its own layout (no squeeze).
        auto = collage_layout(self._opts("rows"), [1.5, 1.5, 1.5], mean_height_px=1000)
        assert auto.max_size_px > 3000
        assert auto.row_scale == pytest.approx(1.0, abs=0.01)
        assert max_size_provided("-t rows -S 6000") is True
        assert max_size_provided("-t rows -S6000") is True
        assert max_size_provided("-t rows --max-collage-size 6000") is True
        assert max_size_provided("-t rows -i 2400") is False

    def test_gap_compensation_is_capped(self) -> None:
        # A far-off -r must not ask for a gutter bigger than the sheet.
        assert collage_layout(self._opts("rows"), [4.0] * 4, pinned_aspect=0.01).gap_px <= 160

    def test_no_panels_is_square_not_zero(self) -> None:
        assert collage_layout(self._opts("rows"), []).aspect == 1.0
        # A shop-pinned -r is not needed to get a sane sheet from no panels.
        assert collage_layout(self._opts("rows"), [], pinned_aspect=2.0).aspect == 1.0


class TestPanelLabels:
    def test_label_lists_plate_size_material(self) -> None:
        assert panel_caption(1, (23.0, 16.0), "ACM") == "Plate 1 \u2014 23.0 x 16.0 in \u2014 ACM"

    def test_caption_drops_unknown_parts(self) -> None:
        assert panel_caption(2) == "Plate 2"
        assert panel_caption(2, None, "ACM") == "Plate 2 \u2014 ACM"
        assert panel_caption(2, (9.0, 16.0)) == "Plate 2 \u2014 9.0 x 16.0 in"

    def test_plate_size_in_reads_packed_plates(self) -> None:
        class _Plate:
            def __init__(self, w: float, h: float) -> None:
                self.width = w
                self.height = h

        plates = [_Plate(23.0, 16.0), _Plate(9.0, 16.0)]
        assert plate_size_in(plates, 1) == (23.0, 16.0)
        assert plate_size_in(plates, 2) == (9.0, 16.0)
        assert plate_size_in(plates, 3) is None  # stubbed layout in tests
        assert plate_size_in([], 1) is None

    def test_proof_image_name(self) -> None:
        assert proof_image_name("2026-11-23 CES") == "2026-11-23 CES_proof.jpeg"


class TestComposition:
    """The composed sheet, inspected as decoded pixels."""

    def test_rows_sheet_is_a_decodable_jpeg(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        written = compose_proof_sheet(panels(3), out)
        assert written == out
        image = open_sheet(out)
        assert image.width > 100 and image.height > 100

    def test_sheet_carries_no_title_band(self, tmp_path: Path) -> None:
        """The sheet is the previews only: no caption strips, no title band."""
        out = tmp_path / "proof.jpeg"
        compose_proof_sheet(panels(3), out)
        image = open_sheet(out).convert("L")
        # The white frame is the first thing on the sheet, so the top row (and
        # the panel interiors' own margins) must be pure paper - a title band or
        # a caption strip would put dark glyph pixels up there.
        assert all(image.getpixel((x, 0)) == 255 for x in range(image.width))
        assert all(image.getpixel((x, 5)) == 255 for x in range(image.width))
        assert all(image.getpixel((0, y)) == 255 for y in range(image.height))

    def test_every_panel_lands_in_the_sheet(self, tmp_path: Path) -> None:
        compose_proof_sheet(panels(3), tmp_path / "proof.jpeg")
        colors = dominant_colors(open_sheet(tmp_path / "proof.jpeg"))
        for hex_color in (RED, GREEN, BLUE):
            target = tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
            assert any(
                abs(c[0] - target[0]) < 40
                and abs(c[1] - target[1]) < 40
                and abs(c[2] - target[2]) < 40
                for c in colors
            ), f"{hex_color} missing from sheet"

    @pytest.mark.parametrize("collage_type", ["rows", "columns", "nested"])
    def test_every_collage_type_composes(self, tmp_path: Path, collage_type: str) -> None:
        out = tmp_path / f"{collage_type}.jpeg"
        assert compose_proof_sheet(panels(3), out, args=f"-t {collage_type}") == out
        open_sheet(out)

    def test_gutter_changes_the_sheet_size(self, tmp_path: Path) -> None:
        tight = tmp_path / "tight.jpeg"
        wide = tmp_path / "wide.jpeg"
        compose_proof_sheet(panels(3), tight, args="-g 0")
        compose_proof_sheet(panels(3), wide, args="-g 60")
        # The panels are composited in one row, so the gutters are horizontal:
        # a wider gutter widens the sheet, and its scale stays 1.0.
        assert open_sheet(wide).width > open_sheet(tight).width
        assert open_sheet(wide).height == pytest.approx(open_sheet(tight).height, abs=2)

    def test_background_color_is_used(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        compose_proof_sheet(panels(2), out, args="-t columns -g 40 -b 255,0,0")
        colors = dominant_colors(open_sheet(out))
        assert any(c[0] > 200 and c[1] < 60 and c[2] < 60 for c in colors)

    def test_count_truncates_the_panel_list(self, tmp_path: Path) -> None:
        four = tmp_path / "four.jpeg"
        three = tmp_path / "three.jpeg"
        compose_proof_sheet(panels(4), four, args="-t columns")
        compose_proof_sheet(panels(4), three, args="-t columns -c 3")
        # One row of panels: dropping a panel shortens the sheet. (`-c`
        # truncates only above 2 - upstream's own rule, kept verbatim.)
        assert open_sheet(three).width < open_sheet(four).width

    def test_max_size_caps_the_long_side(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        # -S only caps, it never upscales, so the panels have to be bigger than
        # the cap for it to bite: one row of three 200 px panels is ~624 px.
        compose_proof_sheet(panels(3), out, args="-S 300")
        image = open_sheet(out)
        # `-S` caps the sheet's longest edge (upstream's own CLI closes with
        # `resize_img_to_max_size`, which the library call does not, so the
        # adapter applies it) - our white frame is inside the cap, not added to
        # it, and the sheet carries no title band.
        assert max(image.size) <= 300 + 4

    def test_empty_panel_list_writes_nothing(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        assert compose_proof_sheet([], out) is None
        assert not out.exists()

    def test_plotting_unavailable_is_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "proof.jpeg"
        monkeypatch.setattr(proof_composite, "PLOTTER_AVAILABLE", False)
        assert compose_proof_sheet(panels(2), out) is None
        assert not out.exists()


class TestPanelGeometry:
    """Panels carry the plate's aspect, which is what makes the collage honest."""

    def test_tight_crop_shapes_the_panel_like_the_drawn_content(self) -> None:
        from plt_optimizer.diagnostics.plotter import figure_to_image, tight_content_box

        fig = make_panel(width_in=6.0, height_in=2.0)  # fat figure, 2:1 data
        box = tight_content_box(fig, 80)
        assert box is not None
        image = figure_to_image(fig, dpi=80, crop_tight=True)
        # Cropped to the content, not resized: it cannot invent pixels.
        assert image.width <= 6 * 80
        assert image.height < 2 * 80
        # The cropped panel's aspect is the content's aspect, i.e. the plate's.
        assert image.width / image.height == pytest.approx(
            proof_composite.panel_aspect_inches(box), rel=0.02
        )

    def test_uncropped_panel_is_the_whole_canvas(self) -> None:
        from plt_optimizer.diagnostics.plotter import figure_to_image

        fig = make_panel(width_in=6.0, height_in=2.0)
        canvas = figure_to_image(fig, dpi=80)
        assert (canvas.width, canvas.height) == (480, 160)
        assert figure_to_image(fig, dpi=80, crop_tight=True).height < canvas.height

    def test_panel_aspect_of_a_degenerate_box_is_none(self) -> None:
        assert proof_composite.panel_aspect_inches(None) is None

    def test_panel_jpegs_are_written_and_cleaned_up(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        scratch = tmp_path / "proof_panels"
        scratch.mkdir()
        (scratch / "stale.jpeg").write_bytes(b"stale")
        written: list[Path] = []
        write_panel = proof_composite._write_panel_jpeg

        def _write(raster: Image.Image, path: Path) -> None:
            """Write the panel, then snapshot what the scratch dir holds."""
            write_panel(raster, path)
            written.clear()
            written.extend(sorted(path.parent.glob("plate*.jpeg")))

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(proof_composite, "_write_panel_jpeg", _write)
        try:
            compose_proof_sheet(panels(3), out, panel_jpeg_dir=scratch)
        finally:
            monkeypatch.undo()
        open_sheet(out)
        # One JPEG per plate reached the engine, and none of them survive the
        # call: the panel JPEGs are scratch. The directory the caller supplied is
        # left alone (with the file that was already in it).
        assert [p.name for p in written] == ["plate01.jpeg", "plate02.jpeg", "plate03.jpeg"]
        assert scratch.exists()
        assert [p.name for p in sorted(scratch.iterdir())] == ["stale.jpeg"]

    def test_panel_jpeg_directory_is_created(self, tmp_path: Path) -> None:
        """A scratch directory this call created is removed on the way out."""
        out = tmp_path / "proof.jpeg"
        scratch = tmp_path / "nested" / "proof_panels"
        compose_proof_sheet(panels(2), out, panel_jpeg_dir=scratch)
        open_sheet(out)
        assert not scratch.exists()

    def test_panel_jpegs_carry_the_plate_aspect(self, tmp_path: Path) -> None:
        from plt_optimizer.diagnostics.plotter import figure_to_image

        raster = figure_to_image(make_panel(width_in=6.0, height_in=2.0), dpi=100, crop_tight=True)
        out = tmp_path / "panel.jpeg"
        proof_composite._write_panel_jpeg(raster, out)
        with Image.open(out) as written:
            assert written.format == "JPEG"
            assert written.width / written.height == pytest.approx(
                raster.width / raster.height, 0.01
            )

    def test_no_panel_jpegs_without_the_argument(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        compose_proof_sheet(panels(2), out)
        assert open_sheet(out) is not None
        assert not list(tmp_path.glob("proof_panels*"))


class TestLayoutCollapse:
    """The gutter must survive the engine's row resample (the collapse bug).

    Upstream composites the panels at a common height, then resamples the layout
    to `(avg_h + avg_w) / 2 * sqrt(n * -r)`. A pinned `-r` is therefore a
    resample factor for every panel set it was not written for - the 12 px gutter
    collapsed to ~3 px, panels touched, and the resample bled their edges
    together. `proof_composite` computes `-r` from the panels and asks for a
    gutter that lands at the requested width, and a pinned `-r` is compensated
    the same way.
    """

    GAP = 12

    @staticmethod
    def _gutters(sheet: Image.Image) -> list[int]:
        """Interior white bands of the sheet, in both directions.

        A `rows` sheet stacks panels vertically, so its gutters are horizontal
        white bands; the sheet's own frame margin and the caption strip inside a
        panel are not gutters, so the widest bands are the ones to read.
        """
        w, h = sheet.size
        px: Any = sheet.load()
        white = (255, 255, 255)
        bands: list[int] = []
        # Columns of the sheet (x fixed, scanning y) and rows of the sheet (y
        # fixed, scanning x): a `rows` sheet's gutters are the horizontal bands.
        columns = [any(px[x, i] != white for i in range(0, h, 2)) for x in range(w)]
        rows = [any(px[i, y] != white for i in range(0, w, 2)) for y in range(h)]
        for ink in (columns, rows):
            run = 0
            for is_ink in ink:
                if is_ink:
                    if run > 1:
                        bands.append(run)
                    run = 0
                else:
                    run += 1
        return bands

    def _compose(self, tmp_path: Path, name: str, args: str = "") -> Image.Image:
        out = tmp_path / f"{name}.jpeg"
        compose_proof_sheet(panels(3), out, args=args)
        return open_sheet(out)

    def test_auto_aspect_keeps_the_gutter(self, tmp_path: Path) -> None:
        gutters = self._gutters(self._compose(tmp_path, "auto"))
        assert gutters, "the panels touch: the sheet has no gutters"
        assert max(gutters) >= self.GAP, f"gutters collapsed to {gutters}"

    def test_pinned_aspect_is_honoured_and_gutters_survive(self, tmp_path: Path) -> None:
        """A pinned `-r` goes to the engine as written; the gutters stay readable."""
        pinned = collage_layout(
            parse_collage_args("-S 2000"),
            [2.0, 2.0, 2.0],
            mean_height_px=1000,
            pinned_aspect=1.375,
            pinned_max_size=True,
        )
        assert pinned.aspect == pytest.approx(1.375)
        gutters = self._gutters(self._compose(tmp_path, "pinned", args="-r 1.375"))
        assert max(gutters) >= self.GAP - 2, f"gutters collapsed to {gutters}"

    def test_auto_aspect_is_the_measured_panel_aspect(self, tmp_path: Path) -> None:
        # The adapter's formula must match the panels the renderer produced.
        from plt_optimizer.diagnostics.plotter import figure_to_image, tight_content_box

        figs = [fig for _caption, fig in panels(3)]
        aspects = [
            cast("float", proof_composite.panel_aspect_inches(tight_content_box(fig, 100)))
            for fig in figs
        ]
        assert all(a is not None for a in aspects)
        opts = parse_collage_args("")
        mean = sum(aspects) / len(aspects)
        layout = collage_layout(opts, aspects, mean_height_px=1000)
        assert layout.aspect == pytest.approx(row_forcing_aspect(mean, len(aspects)))
        assert layout.row_scale == pytest.approx(1.0, abs=0.01)
        # Sanity: the panels really are cropped to their content (not 16:9).
        for fig in figs:
            image = figure_to_image(fig, dpi=100, crop_tight=True)
            assert image.width / image.height == pytest.approx(
                proof_composite.panel_aspect_inches(tight_content_box(fig, 100)), rel=0.02
            )


class TestDeterminism:
    """Identical input produces byte-identical sheets (AGENTS.md invariant)."""

    @pytest.mark.parametrize(
        "args",
        ["", "-t nested", "-t rows -m 25 -M 10", "-O random -t rows", "-t nested -m 25 -M 10"],
    )
    def test_two_runs_are_byte_identical(self, tmp_path: Path, args: str) -> None:
        a = tmp_path / "a.jpeg"
        b = tmp_path / "b.jpeg"
        compose_proof_sheet(panels(3), a, args=args)
        compose_proof_sheet(panels(3), b, args=args)
        assert a.read_bytes() == b.read_bytes(), f"sheets differ for args={args!r}"

    def test_crc_of_the_jpeg_stream_is_stable(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        compose_proof_sheet(panels(3), out, args="-t nested")
        first = zlib.crc32(out.read_bytes())
        compose_proof_sheet(panels(3), out, args="-t nested")
        assert zlib.crc32(out.read_bytes()) == first


class TestRobustness:
    def test_unrasterizable_panel_is_skipped(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        good_caption, good_figure = panels(1)[0]
        written = compose_proof_sheet(
            [("Plate 9 \u2014 stub", stub_figure()), (good_caption, good_figure)],
            out,
        )
        assert written == out
        open_sheet(out)

    def test_all_panels_failing_writes_nothing(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        assert compose_proof_sheet([("Plate 1 \u2014 stub", stub_figure())], out) is None
        assert not out.exists()

    def test_unsupported_order_modes_keep_plate_order(self, tmp_path: Path) -> None:
        # File mtimes do not exist for in-memory panels: warn, compose anyway.
        out = tmp_path / "proof.jpeg"
        assert compose_proof_sheet(panels(3), out, args="-O oldest_first") == out
        open_sheet(out)

    def test_filename_order_sorts_by_caption(self) -> None:
        items: list[tuple[str, Image.Image]] = [
            ("Plate 3 \u2014 ACM", Image.new("RGB", (10, 10))),
            ("Plate 1 \u2014 ACM", Image.new("RGB", (10, 10))),
            ("Plate 2 \u2014 ACM", Image.new("RGB", (10, 10))),
        ]
        ordered = proof_composite._order_panels(items, "filename")
        assert [caption for caption, _image in ordered] == [
            "Plate 1 \u2014 ACM",
            "Plate 2 \u2014 ACM",
            "Plate 3 \u2014 ACM",
        ]

    def test_input_order_is_the_default(self) -> None:
        items: list[tuple[str, Image.Image]] = [
            ("Plate 3", Image.new("RGB", (10, 10))),
            ("Plate 1", Image.new("RGB", (10, 10))),
        ]
        assert proof_composite._order_panels(items, "input") == items

    def test_unused_upstream_flags_are_ignored_silently(self, tmp_path: Path) -> None:
        # -s (debug overlay) and -a (no antialias) are accepted; -s is not
        # honoured for in-memory panels (its labels name temp files).
        out = tmp_path / "proof.jpeg"
        assert compose_proof_sheet(panels(2), out, args="-s -a") == out
        open_sheet(out)


if __name__ == "__main__":  # pragma: no cover - direct-run convenience
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
