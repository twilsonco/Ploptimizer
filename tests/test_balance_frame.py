"""Frame contract of the gap-balance renderer adapter.

The balance rule's geometry lives in exactly one frame: **label-local inches,
``+y`` up, label bottom edge at ``y = 0``**. Three independent producers feed
it, and they must agree, because a disagreement is silent -- the solve still
returns a plausible number, it just moves the block the wrong way by the size
of the mismatch:

* the emitted HPGL stream (the border ring, the drill circles), read in plotter
  units in that frame by ``_section_border_ink_in_inches`` /
  ``_section_circles_in_inches``;
* ``TextBlock.as_ink()``, in the renderer's own frame, which the adapter lifts
  by ``lift_units`` to sit where the legacy centering is about to put it;
* the block's swept faces, lifted by the same move.

The stronger half of these tests run the real adapter
(``_center_text_layer_vertically``) over a written PLT file and then re-measure
A and B **on the emitted stream**, so a mirrored, negated or unlifted frame
cannot pass: the label is round, the strokes are horizontal, and only a correct
frame makes the hole-side gap and the border-side gap agree after the shift.

Fixture: a 2in round label (border ring centre ``(1.0, 1.0)``, radius ``1.0``),
a two-line text block 0.5in tall sitting 0.1in ABOVE the label's centre -- so
the legacy centering has travel to give the rule (it redirects travel it was
already making, it never adds its own) -- two horizontal strokes 1.0in wide,
and one lone drill hole whose engraved edge sits 0.40in above the top stroke.
The ring curves inward past the stroke ends, so the border-side gap is smaller
than the corner geometry suggests: the block has to move up a little.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate.geometry import ink_circle_gap, polyline_gap
from plt_optimizer.generate.label_renderer import (
    LAYER_BOUNDARY,
    LAYER_HOLES,
    LAYER_TEXT,
    _balance_shift_inches,
    _center_text_layer_vertically,
    _collect_hpgl_geometry,
    _section_border_ink_in_inches,
    _section_circles_in_inches,
)
from plt_optimizer.generate.resolution import (
    DEFAULT_BALANCED_CENTERING_HOLE_BORDER_FACTOR,
    DEFAULT_BALANCED_CENTERING_HOLE_TEXT_MODE,
    ResolvedHoleSpec,
    ResolvedLabel,
)

U = HPGL_UNITS_PER_INCH
UNIT = 1.0 / U

# Label frame: 2in x 2in, border ring centred on the label, 1in radius.
HEIGHT = 2.0
WIDTH = 2.0
V_MARGIN = 0.2
RING_CENTER = (1.0, 1.0)
RING_RADIUS = 1.0

# Text block: 0.5in tall, strokes 1.0in wide. It STARTS 0.2in above the label
# centre, so the legacy centering has 0.2in of travel to give the rule -- the
# balanced fixture is the same block at POST_*, and the starting offset exists
# only to supply that travel.
STROKE_X_MIN = 0.5
STROKE_X_MAX = 1.5
BOTTOM_LINE_Y = 0.95
TOP_LINE_Y = 1.45
POST_BOTTOM_Y = 0.75
POST_TOP_Y = 1.25
# The legacy centering move: the band centre is y = 1.0, the ink centre 1.20,
# so the block travels DOWN by 0.2in before any balance rule runs -- a negative
# lift, because ``+y`` is up.
LEGACY_LIFT_IN = HEIGHT / 2.0 - (BOTTOM_LINE_Y + TOP_LINE_Y) / 2.0

# Lone hole: engraved radius 0.05in, its edge HOLE_GAP from the BALANCED
# position's stroke, so the starting offset does not move the target.
HOLE_RADIUS = 0.05
HOLE_GAP = 0.40
TOP_HOLE_Y = POST_TOP_Y + HOLE_GAP + HOLE_RADIUS
BOTTOM_HOLE_Y = POST_BOTTOM_Y - HOLE_GAP - HOLE_RADIUS

Polyline = Tuple[Tuple[float, float], ...]


def _ring_commands(center: Tuple[float, float], radius: float) -> str:
    """Return four ``AA`` quadrants tracing a full circle, in plotter units."""
    cx = round(center[0] * U)
    cy = round(center[1] * U)
    r = round(radius * U)
    commands: List[str] = []
    for start_deg in (0, 90, 180, 270):
        angle = math.radians(start_deg)
        sx = round(cx + r * math.cos(angle))
        sy = round(cy + r * math.sin(angle))
        # A PU onto the arc's start, then the arc: the collector derives the
        # radius from the pen position, exactly as the emitter writes it.
        commands.append(f"PU{sx},{sy};PD;AA{cx},{cy},90;")
    return "".join(commands)


def _text_commands(bottom_y: float, top_y: float) -> str:
    """Return one pen-down horizontal run per line, in plotter units."""
    x0 = round(STROKE_X_MIN * U)
    x1 = round(STROKE_X_MAX * U)
    return "".join(f"PU{x0},{round(y * U)};PD{x1},{round(y * U)};" for y in (top_y, bottom_y))


def _stream(hole_y: float, bottom_y: float = BOTTOM_LINE_Y, top_y: float = TOP_LINE_Y) -> str:
    """Return a whole label's HPGL stream with one lone drill hole."""
    return "".join(
        [
            "IN;PA;",
            f"SP{LAYER_BOUNDARY};",
            _ring_commands(RING_CENTER, RING_RADIUS),
            f"SP{LAYER_HOLES};",
            _ring_commands((RING_CENTER[0], hole_y), HOLE_RADIUS),
            f"SP{LAYER_TEXT};",
            _text_commands(bottom_y, top_y),
            "SP0;",
        ]
    )


def _label(
    location: str,
    *,
    mode: Optional[str] = None,
    factor: Optional[float] = None,
) -> ResolvedLabel:
    """Return the fixture label carrying one lone drill hole on ``location``.

    ``mode``/``factor`` set the two cascading gap-balancing parameters on the
    resolved label; leaving them unset keeps the code defaults
    (``closest_ink`` / ``0.5``), the shipped rule.
    """
    return ResolvedLabel(
        id="L1",
        count=1,
        width=WIDTH,
        height=HEIGHT,
        margin=0.125,
        h_margin=0.125,
        v_margin=V_MARGIN,
        holes=[ResolvedHoleSpec(diameter=2 * HOLE_RADIUS, location=location)],
        balanced_centering=True,
        balanced_centering_hole_text_mode=(
            mode if mode is not None else DEFAULT_BALANCED_CENTERING_HOLE_TEXT_MODE
        ),
        balanced_centering_hole_border_factor=(
            factor if factor is not None else DEFAULT_BALANCED_CENTERING_HOLE_BORDER_FACTOR
        ),
    )


def _ink(*lines: float) -> Tuple[Polyline, ...]:
    """Return the fixture's engraved stroke ink for the given line y positions."""
    return tuple(((STROKE_X_MIN, y), (STROKE_X_MAX, y)) for y in lines)


def _parts(stream: str) -> List[str]:
    """Return ``stream`` as the adapter's ``[prefix, "SPn;", body, ...]`` list."""
    return re.split(r"(SP\d+;)", stream)


def _sections(content: str) -> Dict[int, str]:
    """Return a stream's pen bodies, keyed by pen number."""
    parts = _parts(content)
    return {int(parts[i][2:-1]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _horizontal_strokes(section: str) -> List[Polyline]:
    """Return a pen body's pen-down runs as polylines, in inches."""
    points, _arcs = _collect_hpgl_geometry(section)
    polylines: List[Polyline] = []
    run: List[Tuple[float, float]] = []
    for x, y in points:
        point = (x / U, y / U)
        if run and abs(run[-1][1] - point[1]) > 1e-9:
            polylines.append(tuple(run))
            run = []
        run.append(point)
    if run:
        polylines.append(tuple(run))
    return polylines


def _shift_inches(
    hole_y: float,
    location: str,
    lift_in: Optional[float] = None,
    bottom_y: float = BOTTOM_LINE_Y,
    top_y: float = TOP_LINE_Y,
) -> float:
    """Return the adapter's solve for the fixture, ink lifted by ``lift_in``."""
    lift = LEGACY_LIFT_IN if lift_in is None else lift_in
    return _balance_shift_inches(
        _label(location),
        _parts(_stream(hole_y, bottom_y, top_y)),
        {LAYER_TEXT},
        STROKE_X_MIN * U,
        bottom_y * U,
        STROKE_X_MAX * U,
        top_y * U,
        lift * U,
        top_line_ink=_ink(top_y + lift),
        bottom_line_ink=_ink(bottom_y + lift),
    )


def _emitted(
    hole_y: float,
    location: str,
    tmp_path: Path,
    *,
    mode: Optional[str] = None,
    factor: Optional[float] = None,
    name: str = "frame",
) -> str:
    """Run the real centering adapter over the fixture; return the emitted stream."""
    path = tmp_path / f"{name}-{location}.plt"
    path.write_text(_stream(hole_y), encoding="utf-8")
    _center_text_layer_vertically(
        path,
        _label(location, mode=mode, factor=factor),
        text_pens={LAYER_TEXT},
        balance=True,
        top_line_ink=_ink(TOP_LINE_Y),
        bottom_line_ink=_ink(BOTTOM_LINE_Y),
    )
    return path.read_text(encoding="utf-8")


def _border_gap(stroke: Polyline, border: List[Polyline]) -> float:
    """Return the gap from ``stroke`` to the nearest border path."""
    return min(polyline_gap(stroke, path) for path in border)


def _emitted_gaps(content: str, hole_side: str) -> Tuple[float, float]:
    """Return ``(hole-side gap A, border-side gap B)`` from an emitted stream."""
    sections = _sections(content)
    holes = _section_circles_in_inches(sections[LAYER_HOLES])
    border = _section_border_ink_in_inches(sections[LAYER_BOUNDARY])
    strokes = sorted(_horizontal_strokes(sections[LAYER_TEXT]), key=lambda s: s[0][1])
    # A full drill circle emits as four quadrants: one centre, one radius.
    assert len(set(holes)) == 1, holes
    assert len(strokes) == 2, strokes
    bottom_stroke, top_stroke = strokes
    cx, cy, radius = holes[0]
    if hole_side == "top":
        return (
            ink_circle_gap((top_stroke,), (cx, cy), radius),
            _border_gap(bottom_stroke, border),
        )
    return (
        ink_circle_gap((bottom_stroke,), (cx, cy), radius),
        _border_gap(top_stroke, border),
    )


class TestRendererAdapterFrame:
    """The adapter's frame must make the emitted gaps agree, on both sides."""

    def test_top_hole_balances_on_the_emitted_stream(self, tmp_path: Path) -> None:
        a, b = _emitted_gaps(_emitted(TOP_HOLE_Y, "top", tmp_path), "top")
        assert abs(a - b) <= UNIT

    def test_bottom_hole_balances_on_the_emitted_stream(self, tmp_path: Path) -> None:
        a, b = _emitted_gaps(_emitted(BOTTOM_HOLE_Y, "bottom", tmp_path), "bottom")
        assert abs(a - b) <= UNIT

    def test_the_non_balanced_stream_is_untouched(self, tmp_path: Path) -> None:
        """``balance=False`` keeps the legacy placement byte for byte."""
        stream = _stream(TOP_HOLE_Y)
        path = tmp_path / "off.plt"
        path.write_text(stream, encoding="utf-8")
        _center_text_layer_vertically(
            path,
            _label("top"),
            text_pens={LAYER_TEXT},
            balance=False,
            top_line_ink=_ink(TOP_LINE_Y),
            bottom_line_ink=_ink(BOTTOM_LINE_Y),
        )
        # Only the legacy centering move applies: the ink centre drops onto the
        # band centre, and nothing else moves.
        strokes = _horizontal_strokes(_sections(path.read_text(encoding="utf-8"))[LAYER_TEXT])
        centre = sum(stroke[0][1] for stroke in strokes) / len(strokes)
        assert centre == pytest.approx(HEIGHT / 2.0, abs=UNIT)


class TestRendererAdapterMirror:
    """Frame orientation: the solve must commute with the y mirror."""

    def test_mirrored_fixture_gives_the_mirrored_shift(self) -> None:
        """Mirror the block and the hole through the label centre, flip the side.

        A mirrored frame -- or the wrong line ink for the side -- breaks the
        antisymmetry, because the two fixtures differ only in which edge of the
        label carries the hole.
        """
        top_shift = _shift_inches(TOP_HOLE_Y, "top")
        mirrored_shift = _shift_inches(
            HEIGHT - TOP_HOLE_Y,
            "bottom",
            lift_in=-LEGACY_LIFT_IN,
            bottom_y=HEIGHT - TOP_LINE_Y,
            top_y=HEIGHT - BOTTOM_LINE_Y,
        )
        assert top_shift != 0.0
        assert mirrored_shift == pytest.approx(-top_shift, abs=2 * UNIT)

    def test_the_lift_is_honoured(self) -> None:
        """The ink must ride with the centering move, not stay where it was drawn.

        The strokes arrive in the renderer's pre-centering frame while the hole
        and the border are already label-local, so the solve is measured on one
        frame or the other -- never half of each. Feeding the unlifted ink while
        the block faces keep their lift puts the two sides in different frames,
        and the answer moves by a large fraction of an inch, which is the size
        of the centering travel itself.
        """
        lifted = _shift_inches(TOP_HOLE_Y, "top", lift_in=LEGACY_LIFT_IN)
        unlifted = _shift_inches(TOP_HOLE_Y, "top", lift_in=0.0)
        assert abs(lifted - unlifted) > 10 * UNIT

    def test_no_legacy_travel_means_no_solve(self) -> None:
        """The rule redirects travel, it never adds its own.

        With the block already band-centred there is no legacy centering move,
        so the bracket collapses to zero and the adapter declines rather than
        inventing a slide.
        """
        assert _shift_inches(TOP_HOLE_Y, "top", lift_in=0.0) == 0.0


class TestHoleTextModeAndFactorEndToEnd:
    """The two cascading parameters, through the real centering adapter.

    The fixture's top line is a flat horizontal stroke sitting directly below
    the hole's centre, so its closest ink point and its box face are the same
    ``y`` -- which makes this the place to pin that ``top_line`` is inert at a
    flat block (byte-identical output) and that the factor's landing obeys
    ``f * A = (1 - f) * B`` on the emitted stream. The metrics' disagreement is
    pinned on a shape where they differ in
    :class:`tests.test_collision_balance.TestHoleTextMode`.
    """

    @staticmethod
    def _top_stroke_y(content: str) -> float:
        """Return the emitted block's top stroke height, inches."""
        strokes = sorted(_horizontal_strokes(_sections(content)[LAYER_TEXT]), key=lambda s: s[0][1])
        return strokes[-1][0][1]

    def test_top_line_is_byte_identical_to_closest_ink_on_a_flat_block(
        self, tmp_path: Path
    ) -> None:
        """Legacy values are inert: the enum cannot move a flat block's solve."""
        closest = _emitted(TOP_HOLE_Y, "top", tmp_path, mode="closest_ink", name="ci")
        face = _emitted(TOP_HOLE_Y, "top", tmp_path, mode="top_line", name="tl")
        assert face == closest

    def test_half_is_byte_identical_to_the_shipped_rule(self, tmp_path: Path) -> None:
        """``f = 0.5`` (with the default mode) reproduces the shipped stream."""
        shipped = _emitted(TOP_HOLE_Y, "top", tmp_path, name="ship")
        half = _emitted(TOP_HOLE_Y, "top", tmp_path, factor=0.5, name="half")
        assert half == shipped
        # ...and the shipped stream is the balanced one the frame tests pin.
        a, b = _emitted_gaps(shipped, "top")
        assert abs(a - b) <= UNIT

    def test_the_factor_moves_the_block_monotonically(self, tmp_path: Path) -> None:
        """``f = 0.2`` sits nearer the border, ``f = 0.8`` nearer the hole."""
        ys = {
            f: self._top_stroke_y(_emitted(TOP_HOLE_Y, "top", tmp_path, factor=f, name=f"m{f}"))
            for f in (0.2, 0.5, 0.8)
        }
        assert ys[0.2] < ys[0.5] < ys[0.8]
        assert ys[0.5] - ys[0.2] > 0.1
        assert ys[0.8] - ys[0.5] > 0.1

    @pytest.mark.parametrize("f", [0.4, 0.5, 0.6])
    def test_the_landing_satisfies_the_factor_equation(self, tmp_path: Path, f: float) -> None:
        """The weighted gaps close to within two device quanta.

        Only factors whose root lies inside the travel the caps allow: this
        fixture gives the block 0.2in of travel, and ``f = 0.2`` / ``f = 0.8``
        want more than that, so they land on the clamp (pinned by
        :meth:`test_an_unclosable_factor_reports_a_capped_partial`).
        """
        content = _emitted(TOP_HOLE_Y, "top", tmp_path, factor=f, name=f"eq{f}")
        a, b = _emitted_gaps(content, "top")
        assert abs(f * a - (1.0 - f) * b) <= 2 * UNIT

    def test_an_unclosable_factor_reports_a_capped_partial(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A factor asking for more travel than the caps give says so loudly."""
        import logging

        with caplog.at_level(logging.INFO):
            _emitted(TOP_HOLE_Y, "top", tmp_path, factor=0.8, name="cap")
        text = caplog.text
        assert "PARTIAL" in text
        assert "factor=0.800" in text
        assert "the maximum the caps allow" in text or "(the maximum)" in text

    def test_the_solve_logs_the_mode_and_the_factor(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Operator transparency: the balance: INFO line names both parameters."""
        import logging

        with caplog.at_level(logging.INFO):
            _emitted(TOP_HOLE_Y, "top", tmp_path, mode="top_line", factor=0.8, name="log")
        text = caplog.text
        assert "mode=top_line" in text
        assert "factor=0.800" in text
