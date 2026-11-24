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
    compose_proof_sheet,
    panel_caption,
    parse_collage_args,
    plate_size_in,
    proof_image_name,
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
        assert opts.target_aspect_ratio == pytest.approx(1.375)
        assert int(opts.max_size) == 4096
        assert int(opts.imagegap) == 12
        assert tuple(opts.background) == (255, 255, 255)
        assert opts.order == "input"

    def test_user_args_override_named_flags_only(self) -> None:
        opts = parse_collage_args("-t columns -g 24")
        assert opts.collage_type == "columns"
        assert int(opts.imagegap) == 24
        # Unnamed flags keep the sheet's defaults.
        assert opts.target_aspect_ratio == pytest.approx(1.375)
        assert int(opts.max_size) == 4096

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


class TestCaptions:
    def test_caption_lists_plate_size_material(self) -> None:
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
        written = compose_proof_sheet(panels(3), out, title="Job X \u2014 3 plate(s)")
        assert written == out
        image = open_sheet(out)
        assert image.width > 100 and image.height > 100

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
        # Spacing enters the sheet height between rows; the rows algorithm sets
        # the width from the panel widths alone.
        assert open_sheet(wide).height > open_sheet(tight).height

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
        # Columns stack vertically: dropping a panel shortens the sheet. (`-c`
        # truncates only above 2 - upstream's own rule, kept verbatim.)
        assert open_sheet(three).height < open_sheet(four).height

    def test_max_size_caps_the_long_side(self, tmp_path: Path) -> None:
        out = tmp_path / "proof.jpeg"
        compose_proof_sheet(panels(3), out, args="-S 400")
        image = open_sheet(out)
        assert max(image.size) <= 400 + 2 * proof_composite.PROOF_SHEET_MARGIN_PX + 4

    def test_title_band_is_added(self, tmp_path: Path) -> None:
        titled = tmp_path / "titled.jpeg"
        plain = tmp_path / "plain.jpeg"
        compose_proof_sheet(panels(2), titled, title="Plate proof")
        compose_proof_sheet(panels(2), plain)
        assert open_sheet(titled).height > open_sheet(plain).height

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
    """Panels carry the plate's aspect, which is what makes the tiling dense."""

    def test_crop_aspect_shapes_the_panel(self) -> None:
        from plt_optimizer.diagnostics.plotter import figure_to_image, plot_data_aspect

        fig = make_panel(width_in=6.0, height_in=2.0)  # fat figure, 2:1 data
        aspect = plot_data_aspect(fig)
        assert aspect == pytest.approx(22.0 / 12.0, rel=1e-6)
        image = figure_to_image(fig, dpi=80, crop_aspect=aspect)
        assert image.width / image.height == pytest.approx(aspect, rel=0.02)
        # The crop is a content crop, not a resize: it cannot invent pixels.
        assert image.width <= 6 * 80

    def test_caption_strip_is_added_below_the_panel(self) -> None:
        from plt_optimizer.diagnostics.plotter import figure_to_image

        image = figure_to_image(make_panel(), dpi=60)
        captioned = proof_composite._add_caption(
            image,
            "Plate 1 \u2014 ACM",
            strip_px=proof_composite.PROOF_PANEL_LABEL_PX,
            font=proof_composite._load_font(proof_composite.PROOF_LABEL_FONT_SIZE_PX),
        )
        assert captioned.height == image.height + proof_composite.PROOF_PANEL_LABEL_PX


class TestDeterminism:
    """Identical input produces byte-identical sheets (AGENTS.md invariant)."""

    @pytest.mark.parametrize(
        "args",
        ["", "-t nested", "-t rows -d 30", "-O random -t rows", "-t nested -m 25 -M 10"],
    )
    def test_two_runs_are_byte_identical(self, tmp_path: Path, args: str) -> None:
        a = tmp_path / "a.jpeg"
        b = tmp_path / "b.jpeg"
        compose_proof_sheet(panels(3), a, args=args, title="Same job")
        compose_proof_sheet(panels(3), b, args=args, title="Same job")
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
