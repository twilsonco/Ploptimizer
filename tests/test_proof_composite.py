"""Tests for the composite proof sheet (``generate.proof_composite``).

The sheet tiles the per-plate ``all_*`` previews into one JPEG for job
sign-off. These tests drive the layout/composition layer with solid PIL
panels, so the geometry (grid, gutters, margins, aspect-preserving fit,
flatten, long-side cap, encoding) is pinned without a matplotlib render.
"""

from pathlib import Path
from typing import List, Sequence, Tuple, Union

import pytest
from PIL import Image

from plt_optimizer.generate.proof_composite import (
    PROOF_JPEG_QUALITY,
    PROOF_PANEL_COLUMNS,
    PROOF_PANEL_DPI,
    PROOF_PANEL_GUTTER_PX,
    PROOF_SHEET_MARGIN_PX,
    compose_proof_sheet,
    proof_image_name,
    proof_sheet_layout,
)

WHITE: Tuple[int, int, int] = (255, 255, 255)
BLACK: Tuple[int, int, int] = (0, 0, 0)


def _panel(size: Tuple[int, int], color: Tuple[int, int, int]) -> Image.Image:
    """A solid RGB panel of the given size."""
    return Image.new("RGB", size, color)


def _dominant(pixel: Union[float, Tuple[int, ...], None]) -> int:
    """Index of the strongest channel of a sampled pixel.

    JPEG is lossy, so the sheet is read back by dominant channel (red/green/blue)
    rather than by exact RGB. ``getpixel`` is typed loosely by the stubs; a
    scalar/``None`` return would be a broken sample, so it is asserted here.
    """
    assert isinstance(pixel, tuple)
    return max(range(3), key=lambda channel: int(pixel[channel]))


class TestProofImageName:
    """The sheet is named after the sanitized job id plus the JPEG suffix."""

    def test_matches_the_preview_sanitization(self) -> None:
        """Naming parity with the previews: same id, ``_proof.jpeg`` suffix."""
        assert proof_image_name("job 13 sheet1") == "job 13 sheet1_proof.jpeg"
        assert proof_image_name("Coolsys_Walmart_5168") == "Coolsys_Walmart_5168_proof.jpeg"

    def test_suffix_is_jpeg(self) -> None:
        """The contract is a JPEG, not a PNG under a jpeg name."""
        assert proof_image_name("job").endswith("_proof.jpeg")


class TestProofSheetLayout:
    """Panels flow two-up, left to right, top to bottom, in plate order."""

    def test_rows_follow_the_column_cap(self) -> None:
        """A 5-panel run is 2 columns x 3 rows, left-aligned."""
        assert proof_sheet_layout(1) == [(0, 0)]
        assert proof_sheet_layout(2) == [(0, 0), (0, 1)]
        assert proof_sheet_layout(3) == [(0, 0), (0, 1), (1, 0)]
        assert proof_sheet_layout(5) == [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0)]

    def test_custom_column_count(self) -> None:
        """The column cap is a parameter, not a magic number."""
        assert proof_sheet_layout(6, columns=3) == [
            (0, 0),
            (0, 1),
            (0, 2),
            (1, 0),
            (1, 1),
            (1, 2),
        ]

    @pytest.mark.parametrize("panel_count, columns", [(0, 2), (3, 0)])
    def test_non_positive_counts_are_rejected(self, panel_count: int, columns: int) -> None:
        """There is no sheet with no panels and no sheet with no columns."""
        with pytest.raises(ValueError):
            proof_sheet_layout(panel_count, columns)


class TestComposeProofSheet:
    """The composition itself: cell fit, gutters, flatten, cap, encoding."""

    def test_single_panel_sheets_the_panel_plus_the_frame(self, tmp_path: Path) -> None:
        """One whole-sheet proof (the single-plate case) is one panel wide.

        The usual job fills one plate: it must not pay for an empty second
        column, only for the frame.
        """
        out = tmp_path / "job_proof.jpeg"
        panel = _panel((640, 180), BLACK)

        written = compose_proof_sheet([panel], out)

        assert written == out
        assert out.read_bytes()[:2] == b"\xff\xd8"  # JPEG magic
        with Image.open(out) as sheet:
            assert sheet.format == "JPEG"
            assert sheet.mode == "RGB"
            assert sheet.size == (
                640 + 2 * PROOF_SHEET_MARGIN_PX,
                180 + 2 * PROOF_SHEET_MARGIN_PX,
            )
            # The panel fills the cell; the frame stays background.
            assert sheet.getpixel((320, 90)) == BLACK
            assert sheet.getpixel((1, 1)) == WHITE

    def test_grid_size_is_the_columns_plus_gutters_and_frame(self, tmp_path: Path) -> None:
        """Panels are fitted to the narrowest width; the sheet is the grid."""
        out = tmp_path / "proof.jpeg"
        panels = [_panel((400, 200), BLACK), _panel((300, 300), BLACK)]

        compose_proof_sheet(panels, out)

        with Image.open(out) as sheet:
            # Column width = min(400, 300) = 300; the 400-wide panel is fitted
            # down to 300x150, so the row is 300 tall.
            assert sheet.size == (
                2 * 300 + PROOF_PANEL_GUTTER_PX + 2 * PROOF_SHEET_MARGIN_PX,
                300 + 2 * PROOF_SHEET_MARGIN_PX,
            )

    def test_panels_keep_their_aspect_ratio(self, tmp_path: Path) -> None:
        """Each panel is fit inside its cell, never stretched to fill it."""
        out = tmp_path / "proof.jpeg"
        panels: List[Image.Image] = [_panel((800, 100), BLACK), _panel((400, 400), BLACK)]

        compose_proof_sheet(panels, out)

        with Image.open(out) as sheet:
            # The wide panel is 400x50 centred in the 400x400 cell, so the
            # column above and below its band is background.
            assert sheet.getpixel((212, 212)) == BLACK  # inside the band
            assert sheet.getpixel((212, 14)) == WHITE  # above it, in the frame
            assert sheet.getpixel((624, 212)) == BLACK  # the square panel

    def test_alpha_panels_are_flattened_on_the_background(self, tmp_path: Path) -> None:
        """Transparent margins read as the sheet background, not as black."""
        out = tmp_path / "proof.jpeg"
        panel = Image.new("RGBA", (200, 100), (255, 0, 0, 0))  # fully transparent
        panel.paste(Image.new("RGBA", (50, 100), (0, 0, 0, 255)), (0, 0))

        compose_proof_sheet([panel], out)

        with Image.open(out) as sheet:
            margin = PROOF_SHEET_MARGIN_PX
            assert sheet.getpixel((margin + 150, margin + 50)) == WHITE
            assert _dominant(sheet.getpixel((margin + 10, margin + 50))) == 0  # the ink bar

    def test_panel_order_flows_left_to_right_top_to_bottom(self, tmp_path: Path) -> None:
        """Panel order is the caller's order — plate 1 first (see the caller)."""
        out = tmp_path / "proof.jpeg"
        panels = [
            _panel((100, 100), (255, 0, 0)),
            _panel((100, 100), (0, 255, 0)),
            _panel((100, 100), (0, 0, 255)),
        ]

        compose_proof_sheet(panels, out)

        margin = PROOF_SHEET_MARGIN_PX
        gutter = PROOF_PANEL_GUTTER_PX
        with Image.open(out) as sheet:
            assert sheet.size == (212 + 2 * margin, 212 + 2 * margin)
            cells = [
                ((margin + 50, margin + 50), 0),  # panel 1 -> red
                ((margin + 100 + gutter + 50, margin + 50), 1),  # panel 2 -> green
                ((margin + 50, margin + 100 + gutter + 50), 2),  # panel 3 -> blue
            ]
            for position, expected in cells:
                assert _dominant(sheet.getpixel(position)) == expected
            # The unused 4th cell is frame, not ink.
            assert sheet.getpixel((margin + 150, margin + 150)) == WHITE

    def test_long_side_is_capped(self, tmp_path: Path) -> None:
        """A sheet of large panels is scaled down to the cap."""
        out = tmp_path / "proof.jpeg"
        panels = [_panel((2000, 1000), BLACK), _panel((2000, 1000), BLACK)]

        compose_proof_sheet(panels, out, max_long_side_px=1000)

        with Image.open(out) as sheet:
            assert max(sheet.size) <= 1000
            # The grid keeps its proportions: one wide row, not a square blob.
            assert sheet.size[0] > sheet.size[1]

    def test_cap_of_zero_writes_native_size(self, tmp_path: Path) -> None:
        """``max_long_side_px=0`` disables the cap."""
        out = tmp_path / "proof.jpeg"
        panels = [_panel((2000, 1000), BLACK)]

        compose_proof_sheet(panels, out, max_long_side_px=0)

        with Image.open(out) as sheet:
            assert sheet.size == (
                2000 + 2 * PROOF_SHEET_MARGIN_PX,
                1000 + 2 * PROOF_SHEET_MARGIN_PX,
            )

    def test_quality_and_background_are_honored(self, tmp_path: Path) -> None:
        """A quality/background override changes the encoding, not the layout."""
        low, high = tmp_path / "low.jpeg", tmp_path / "high.jpeg"
        panel = _panel((600, 400), (10, 20, 30))

        compose_proof_sheet([panel], low, quality=20)
        compose_proof_sheet([panel], high, quality=PROOF_JPEG_QUALITY)
        assert low.stat().st_size < high.stat().st_size

        dark = tmp_path / "dark.jpeg"
        transparent = Image.new("RGBA", (200, 200), (255, 255, 255, 0))
        compose_proof_sheet([transparent], dark, background=BLACK)
        with Image.open(dark) as sheet:
            assert sheet.getpixel((100, 100)) == BLACK

    def test_defaults_match_the_documented_constants(self, tmp_path: Path) -> None:
        """The defaults a caller omits are the module constants."""
        assert (PROOF_PANEL_COLUMNS, PROOF_PANEL_DPI) == (2, 100)
        assert PROOF_PANEL_GUTTER_PX == PROOF_SHEET_MARGIN_PX

    def test_empty_panel_list_is_rejected(self, tmp_path: Path) -> None:
        """No panels, no sheet (and nothing is written)."""
        out = tmp_path / "proof.jpeg"
        panels: Sequence[Image.Image] = []

        with pytest.raises(ValueError, match="zero panels"):
            compose_proof_sheet(panels, out)

        assert not out.exists()

    def test_unwritable_target_raises_oserror(self, tmp_path: Path) -> None:
        """An unwritable target surfaces as OSError for the caller to log."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        panels: Sequence[Image.Image] = [_panel((50, 50), BLACK)]

        with pytest.raises(OSError):
            compose_proof_sheet(panels, blocker / "proof.jpeg")
