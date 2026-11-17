r"""Unit tests for :mod:`plt_optimizer.generate.usage_stats`.

Production-usage accounting for the generate pipeline: the ``usage`` block of
``report.json`` and the "Job usage:" section of ``summary.txt``.

* SP1 - label / character / hole / material aggregation.
* SP2 - tool distance measured from the emitted PLT files, attributed by
  cutter / material / plate / label / stroke class.
* SP3 - the ``summary.txt`` renderer and the ``report.json`` contract.

HPGL fixtures follow the parser's conventions (verified against the engine):
a plate's content is emitted in a y-up frame whose origin is the plate's
top-left, so a label placed at ``(x, y)`` with size ``(w, h)`` occupies
``[x, x + w] x [y - h, y]`` in device units. Distances are in inches, the
device scale being 1016 units/inch.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pytest

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.units import inches_to_units
from plt_optimizer.generate.generation_report import build_report, format_summary
from plt_optimizer.generate.markup import Run, Style
from plt_optimizer.generate.packing.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import (
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
    hole_engraved_radius,
)
from plt_optimizer.generate.usage_stats import (
    DISTANCE_CLASSES,
    SUMMARY_USAGE_LABEL_LIMIT,
    UNKNOWN_CUTTER,
    build_usage_stats,
    format_usage_lines,
)

# ==============================================================================
# Fixture builders
# ==============================================================================


def u(inches: float) -> int:
    """Device units for an inch measurement (HPGL 1016 units/inch)."""
    return int(round(inches_to_units(inches)))


def line(text: str, height: float, cutter: float, **overrides: Any) -> ResolvedTextLine:
    """A resolved text line; ``plain_text`` defaults to ``text``."""
    fields: Dict[str, Any] = {
        "text": text,
        "nominal_text_height": height,
        "toolpath_text_height": height,
        "cutter_diameter": cutter,
        "character_spacing": 0.01,
        "line_spacing": 0.05,
        "plain_text": text,
    }
    fields.update(overrides)
    return ResolvedTextLine(**fields)


def label(
    label_id: str,
    *,
    count: int = 1,
    width: float = 4.0,
    height: float = 2.0,
    material: Optional[str] = None,
    shape_type: str = "rect",
    relief: bool = False,
    lines: Sequence[ResolvedTextLine] = (),
    holes: Sequence[ResolvedHoleSpec] = (),
    **overrides: Any,
) -> ResolvedLabel:
    """A resolved label with shop-default margins filled in."""
    fields: Dict[str, Any] = {
        "id": label_id,
        "count": count,
        "width": width,
        "height": height,
        "margin": 0.1,
        "h_margin": 0.1,
        "v_margin": 0.1,
        "material": material,
        "shape_type": shape_type,
        "relief_engraving": relief,
        "content": list(lines),
        "holes": list(holes),
    }
    fields.update(overrides)
    return ResolvedLabel(**fields)


def placement(
    source: ResolvedLabel,
    *,
    label_id: Optional[str] = None,
    x: float = 0.0,
    y: float = 0.0,
    width: Optional[float] = None,
    height: Optional[float] = None,
    rotated: bool = False,
) -> PackedLabel:
    """A placement of ``source`` on a plate (``y`` is the y-up top edge)."""
    return PackedLabel(
        label_id=label_id or f"{source.id}_0",
        x=x,
        y=y,
        width=source.width if width is None else width,
        height=source.height if height is None else height,
        rotated=rotated,
        source_label=source,
    )


def plate(
    *,
    plate_id: str = "plate-1",
    width: float = 12.0,
    height: float = 8.0,
    left_clearance: float = 0.0,
    top_clearance: float = 0.0,
    material: Optional[str] = None,
    labels: Sequence[PackedLabel] = (),
) -> PackedPlate:
    """A packed plate frame."""
    return PackedPlate(
        plate_id=plate_id,
        width=width,
        height=height,
        left_clearance=left_clearance,
        top_clearance=top_clearance,
        material=material,
        labels=list(labels),
    )


def rect_path(x: float, y: float, w: float, h: float) -> str:
    """Pen-up-led closed rectangle from the y-up top-left ``(x, y)``.

    Cutting distance: ``2 * (w + h)`` inches.
    """
    x0, y0, x1, y1 = u(x), u(y), u(x + w), u(y - h)
    return f"PU{x0},{y0};PD{x1},{y0},{x1},{y1},{x0},{y1},{x0},{y0};"


def circle_path(cx: float, cy: float, side: float) -> str:
    """Pen-up-led closed square of ``side`` inches centred at ``(cx, cy)``.

    Named for the drill-hole geometry it stands in for. Line segments only:
    the engine's ``AA`` arc reading is quirky (see :meth:`PLTParser._parse_arc_command`,
    which takes the sweep from the third parameter), so fixtures pin exact
    Euclidean lengths with polylines. Cutting distance: ``4 * side`` inches.
    """
    h = side / 2.0
    return rect_path(cx - h, cy + h, side, side)


def section(*parts: str) -> str:
    """One assembled label section: the production anchor plus its content.

    :func:`~plt_optimizer.generate.vectorize.assemble_plt_from_rendered_labels`
    emits the literal ``PU0,0;`` (pen up to the safe position) before every
    label it places, which is the anchor the per-label attribution splits on.
    """
    return "PU0,0;" + "".join(parts)


def document(*sections: str) -> str:
    """Header + sections + the writer's pen-up return home.

    Joined with **no** separators, matching production assembly
    (:func:`~plt_optimizer.generate.vectorize.assemble_plt_from_rendered_labels`
    uses ``"".join``). A ``\\n`` before ``SP2;`` would hide the pen select from
    :func:`~plt_optimizer.generate.vectorize.extract_pens_from_plt_text`, which
    splits on ``;`` and checks ``cmd.startswith("SP")``.
    """
    return "IN;PA;" + "".join(sections) + "PU0,0;"


def write_plt(directory: Path, name: str, content: str) -> Path:
    """Write a PLT fixture with the production CRLF line endings."""
    path = directory / name
    path.write_bytes(content.replace("\n", "\r\n").encode("ascii"))
    return path


def stats_for(
    *,
    resolved_labels: Sequence[ResolvedLabel] = (),
    packed_plates: Sequence[Any] = (),
    combined_by_plate: Optional[Dict[int, str]] = None,
    plt_files: Sequence[Path] = (),
    material_by_plate: Optional[Dict[int, Optional[str]]] = None,
) -> Dict[str, Any]:
    """Call the engine with every argument defaulting to empty."""
    return build_usage_stats(
        resolved_labels=resolved_labels,
        packed_plates=packed_plates,
        combined_by_plate=combined_by_plate or {},
        plt_files=list(plt_files),
        material_by_plate=material_by_plate or {},
    )


def row_by(rows: Sequence[Dict[str, Any]], key: str, value: Any) -> Dict[str, Any]:
    """The single row of ``rows`` whose ``key`` equals ``value``."""
    matches = [row for row in rows if row[key] == value]
    assert len(matches) == 1, f"expected exactly one {key}={value!r} in {rows!r}"
    return matches[0]


def file_row(usage: Dict[str, Any], name: str, root: Path) -> Dict[str, Any]:
    """The ``distance.files`` row of the PLT fixture called ``name``."""
    return row_by(usage["distance"]["files"], "path", str(root / name))


# ==============================================================================
# SP1 - labels
# ==============================================================================


class TestLabelsUsage:
    def test_counts_physical_labels_not_specs(self) -> None:
        usage = stats_for(
            resolved_labels=[
                label("a", count=3, material="uv"),
                label("b", count=2, material="wb", shape_type="round"),
            ]
        )["labels"]
        assert usage["specs"] == 2
        assert usage["instances"] == 5

    def test_empty_job_is_all_zero(self) -> None:
        assert stats_for()["labels"] == {
            "instances": 0,
            "specs": 0,
            "by_shape": [],
            "by_relief": [],
            "by_material": [],
            "by_size": [],
            "by_cutter": [],
        }

    def test_breakdowns_by_material_shape_and_mode(self) -> None:
        usage = stats_for(
            resolved_labels=[
                label("a", count=2, material="uv"),
                label("b", count=1, material="uv", relief=True),
                label("c", count=1, material="wb", shape_type="round"),
            ]
        )["labels"]
        assert {row["material"]: row["count"] for row in usage["by_material"]} == {
            "uv": 3,
            "wb": 1,
        }
        assert {row["shape"]: row["count"] for row in usage["by_shape"]} == {
            "rect": 3,
            "round": 1,
        }
        assert {row["engraving"]: row["count"] for row in usage["by_relief"]} == {
            "plain": 3,
            "relief": 1,
        }

    def test_unset_material_is_labelled_forhumans(self) -> None:
        usage = stats_for(resolved_labels=[label("a")])["labels"]
        assert usage["by_material"] == [{"material": None, "material_label": "(unset)", "count": 1}]

    def test_cutter_rows_count_each_diameter_a_label_engraves_with(self) -> None:
        # Two lines on distinct cutters plus a hole on the routing tool:
        # each physical label is counted ONCE per distinct diameter.
        spec = label(
            "a",
            count=2,
            lines=[line("AA", 0.25, 0.030), line("BB", 0.25, 0.015)],
            holes=[ResolvedHoleSpec(diameter=0.25, location="top-left")],
            hole_cutter_diameter=0.0625,
        )
        cutters = {
            row["cutter_diameter"]: row["count"]
            for row in stats_for(resolved_labels=[spec])["labels"]["by_cutter"]
        }
        assert cutters == {0.03: 2, 0.015: 2, 0.0625: 2}

    def test_size_rows_carry_shape_material_and_count(self) -> None:
        usage = stats_for(
            resolved_labels=[
                label("a", count=2, width=4.0, height=2.0, material="uv"),
                label("b", count=1, width=4.0, height=2.0, material="wb"),
                label("c", count=1, width=6.0, height=3.0, material="uv", shape_type="round"),
            ]
        )["labels"]
        assert [
            (row["width"], row["height"], row["shape"], row["material"], row["count"])
            for row in usage["by_size"]
        ] == [
            (4.0, 2.0, "rect", "uv", 2),
            (4.0, 2.0, "rect", "wb", 1),
            (6.0, 3.0, "round", "uv", 1),
        ]


# ==============================================================================
# SP1 - characters
# ==============================================================================


class TestCharactersUsage:
    def test_spaces_are_excluded(self) -> None:
        usage = stats_for(resolved_labels=[label("a", lines=[line("HELLO WORLD", 0.25, 0.030)])])[
            "characters"
        ]
        assert usage["total"] == 10
        assert usage["spaces_excluded"] == 1
        assert usage["markers"] == 0

    def test_markup_counts_the_engraved_text_and_markers_separately(self) -> None:
        usage = stats_for(
            resolved_labels=[
                label(
                    "a",
                    count=2,
                    lines=[
                        line(
                            "<b>DENSE</b>",
                            0.25,
                            0.030,
                            plain_text="DENSE",
                            marker_text="1. ",
                        )
                    ],
                )
            ]
        )["characters"]
        assert usage["total"] == 10  # five engraved glyphs, two copies
        assert usage["markers"] == 4  # "1." (the marker's space is excluded)

    def test_bands_use_the_machine_text_height(self) -> None:
        usage = stats_for(
            resolved_labels=[
                label(
                    "a",
                    lines=[
                        line("AB", 0.25, 0.030, toolpath_text_height=0.26),
                        line(
                            "<b>CD</b>",
                            0.25,
                            0.030,
                            plain_text="CD",
                            bold_toolpath_text_height=0.045,
                        ),
                    ],
                )
            ]
        )["characters"]
        bands = {row["text_height"]: row["count"] for row in usage["by_text_height"]}
        assert bands == {0.26: 2, 0.045: 2}

    def test_scale_tags_band_by_the_scaled_height(self) -> None:
        styled = line("ABCD", 0.5, 0.030)
        scaled = dataclasses.replace(
            styled,
            styled_runs=(
                Run(text="AB", style=Style(scale=2.0)),
                Run(text="CD", style=Style(scale=1.0)),
            ),
        )
        bands = {
            row["text_height"]: row["count"]
            for row in stats_for(resolved_labels=[label("a", lines=[scaled])])["characters"][
                "by_text_height"
            ]
        }
        assert bands == {1.0: 2, 0.5: 2}

    def test_missing_toolpath_height_falls_back_to_nominal(self) -> None:
        usage = stats_for(
            resolved_labels=[label("a", lines=[line("AB", 0.375, 0.030, toolpath_text_height=0.0)])]
        )["characters"]
        assert [row["text_height"] for row in usage["by_text_height"]] == [0.375]
        assert [row["nominal_text_height"] for row in usage["by_text_height"]] == [0.375]

    def test_band_carries_the_nominal_and_the_engraved_height(self) -> None:
        """The ordered size and the cutter-adjusted size are both reported."""
        usage = stats_for(
            resolved_labels=[
                label("a", lines=[line("AB", 0.25, 0.030, toolpath_text_height=0.205)])
            ]
        )["characters"]
        assert usage["by_text_height"] == [
            {"nominal_text_height": 0.25, "text_height": 0.205, "count": 2}
        ]

    def test_scale_tags_scale_both_heights_of_the_band(self) -> None:
        """``<scale:N>`` moves the nominal and the engraved height together."""
        styled = line("AB", 0.25, 0.030, toolpath_text_height=0.2)
        scaled = dataclasses.replace(
            styled,
            styled_runs=(
                Run(text="A", style=Style(scale=2.0)),
                Run(text="B", style=Style(scale=0.6)),
            ),
        )
        usage = stats_for(resolved_labels=[label("a", lines=[scaled])])["characters"]
        assert usage["by_text_height"] == [
            {"nominal_text_height": 0.15, "text_height": 0.12, "count": 1},
            {"nominal_text_height": 0.5, "text_height": 0.4, "count": 1},
        ]

    def test_same_nominal_split_by_engraved_height_stays_separate(self) -> None:
        """Two lines ordered at 0.25 in but compressed differently are two bands."""
        usage = stats_for(
            resolved_labels=[
                label(
                    "a",
                    lines=[
                        line("AB", 0.25, 0.030, toolpath_text_height=0.205),
                        line("CD", 0.25, 0.030, toolpath_text_height=0.25),
                    ],
                )
            ]
        )["characters"]
        assert [
            (row["nominal_text_height"], row["text_height"], row["count"])
            for row in usage["by_text_height"]
        ] == [(0.25, 0.205, 2), (0.25, 0.25, 2)]

    def test_rows_are_sorted_and_per_label_totals_are_kept(self) -> None:
        usage = stats_for(
            resolved_labels=[
                label("a", count=2, lines=[line("AA", 0.5, 0.030), line("BB", 0.25, 0.030)]),
                label("b", lines=[line("CCC", 0.25, 0.030)]),
            ]
        )["characters"]
        assert [row["text_height"] for row in usage["by_text_height"]] == [0.25, 0.5]
        assert [row["count"] for row in usage["by_text_height"]] == [7, 4]
        assert usage["by_label"] == [
            {"label_id": "a", "count": 8},
            {"label_id": "b", "count": 3},
        ]


# ==============================================================================
# SP1 - holes
# ==============================================================================


class TestHolesUsage:
    def test_totals_and_per_label_counts(self) -> None:
        usage = stats_for(
            resolved_labels=[
                label(
                    "a",
                    count=2,
                    holes=[
                        ResolvedHoleSpec(diameter=0.25, location="top-left"),
                        ResolvedHoleSpec(diameter=0.25, location="top-right"),
                        ResolvedHoleSpec(diameter=0.125, location="bottom-left"),
                    ],
                ),
                label("b", count=1),
            ]
        )["holes"]
        assert usage["total"] == 6
        assert {row["diameter"]: row["count"] for row in usage["by_diameter"]} == {
            0.25: 4,
            0.125: 2,
        }
        # A label without holes is listed at zero so no spec is dropped.
        assert usage["by_label"] == [{"label_id": "a", "count": 6}]

    def test_engraved_diameter_is_the_compensated_circle(self) -> None:
        kwargs: Dict[str, Any] = {
            "holes": [ResolvedHoleSpec(diameter=0.25, location="top-left")],
            "hole_cutter_diameter": 0.0625,
        }
        plain = label("a", hole_adjust_for_cutter=False, **kwargs)
        adjusted = label("a", hole_adjust_for_cutter=True, **kwargs)
        plain_row = stats_for(resolved_labels=[plain])["holes"]["by_diameter"][0]
        adjusted_row = stats_for(resolved_labels=[adjusted])["holes"]["by_diameter"][0]
        assert plain_row["engraved_diameter"] == pytest.approx(0.25)
        assert adjusted_row["engraved_diameter"] == pytest.approx(
            2.0 * hole_engraved_radius(0.25, adjusted.effective_hole_cutter_diameter, adjust=True)
        )
        assert adjusted_row["engraved_diameter"] < plain_row["engraved_diameter"]

    def test_no_holes_is_zero_not_missing(self) -> None:
        assert stats_for(resolved_labels=[label("a")])["holes"] == {
            "total": 0,
            "by_diameter": [],
            "by_label": [],
        }


# ==============================================================================
# SP1 - material
# ==============================================================================


class TestMaterialUsage:
    def test_plate_area_includes_the_clearance_frame(self) -> None:
        material = stats_for(
            packed_plates=[plate(width=12.0, height=8.0, left_clearance=0.5, top_clearance=1.0)]
        )["material"]
        assert material["total"]["plates"] == 1
        assert material["total"]["sq_in"] == pytest.approx(12.5 * 9.0)
        assert material["total"]["sq_ft"] == pytest.approx(12.5 * 9.0 / 144.0, abs=1e-9)

    def test_areas_group_by_the_material_lookup(self) -> None:
        # The lookup (plate number -> material) wins over the frame's own
        # attribute, which is what export glue provides.
        usage = stats_for(
            packed_plates=[
                plate(width=12.0, height=8.0, material="ignored"),
                plate(width=10.0, height=6.0, material="ignored"),
            ],
            material_by_plate={1: "uv", 2: "wb"},
        )["material"]
        rows = {row["material"]: row["sq_in"] for row in usage["by_material"]}
        assert rows == {"uv": pytest.approx(96.0), "wb": pytest.approx(60.0)}
        assert usage["total"]["sq_in"] == pytest.approx(156.0)
        assert usage["total"]["sq_ft"] == pytest.approx(156.0 / 144.0)

    def test_nominal_area_is_the_spec_size_times_the_count(self) -> None:
        specs = [label("a", count=3, width=4.0, height=2.0)]
        material = stats_for(
            resolved_labels=specs,
            packed_plates=[plate(labels=[placement(spec, x=0.0, y=spec.height) for spec in specs])],
        )["material"]
        assert material["label_nominal_area"]["sq_in"] == pytest.approx(24.0)
        assert material["label_nominal_area"]["sq_ft"] == pytest.approx(24.0 / 144.0, abs=1e-6)

    def test_bbox_area_uses_the_reserved_box_not_the_nominal_size(self) -> None:
        spec = label("a", count=2, width=4.0, height=2.0)
        # The packer rotated the placement: the reserved box is 2.0 x 4.0.
        packed = plate(
            material="uv",
            labels=[placement(spec, x=0.0, y=4.0, width=2.0, height=4.0, rotated=True)],
        )
        material = stats_for(resolved_labels=[spec], packed_plates=[packed])["material"]
        assert material["label_nominal_area"]["sq_in"] == pytest.approx(16.0)
        assert material["label_bbox_area"]["sq_in"] == pytest.approx(8.0)
        assert material["by_material"][0]["label_bbox_sq_in"] == pytest.approx(8.0)
        assert material["by_material"][0]["label_bbox_sq_ft"] == pytest.approx(
            8.0 / 144.0, abs=1e-6
        )

    def test_label_instances_count_placements_times_count(self) -> None:
        spec = label("a", count=2, width=4.0, height=2.0)
        packed = plate(
            material="uv",
            labels=[placement(spec, x=0.0, y=2.0), placement(spec, x=4.0, y=2.0)],
        )
        material = stats_for(resolved_labels=[spec], packed_plates=[packed])["material"]
        assert material["by_material"][0]["label_instances"] == 4
        assert material["by_material"][0]["plates"] == 1

    def test_material_agnostic_plate_buckets_as_none(self) -> None:
        assert stats_for(packed_plates=[plate(material=None)])["material"] == {
            "total": {
                "plates": 1,
                "sq_in": pytest.approx(96.0),
                "sq_ft": pytest.approx(96.0 / 144.0),
            },
            "by_material": [
                {
                    "material": None,
                    "material_label": "(unset)",
                    "plates": 1,
                    "sq_in": pytest.approx(96.0),
                    "sq_ft": pytest.approx(96.0 / 144.0),
                    "label_instances": 0,
                    "label_bbox_sq_in": pytest.approx(0.0),
                    "label_bbox_sq_ft": pytest.approx(0.0),
                }
            ],
            "label_nominal_area": {"sq_in": 0.0, "sq_ft": 0.0},
            "label_bbox_area": {"sq_in": 0.0, "sq_ft": 0.0},
        }

    def test_empty_job_is_all_zero(self) -> None:
        assert stats_for()["material"]["total"] == {"plates": 0, "sq_in": 0.0, "sq_ft": 0.0}


# ==============================================================================
# SP2 - tool distance
# ==============================================================================

RECT_SPEC = label("rect-a", material="uv", width=4.0, height=2.0, lines=[line("SIGN", 0.25, 0.030)])
ROUND_RELIEF_SPEC = label(
    "round-b",
    material="wb",
    width=6.0,
    height=3.0,
    shape_type="round",
    relief=True,
    lines=[line("LABEL", 0.25, 0.030)],
    # The bh layer's geometry stands in for this hole: a 0.250 in square.
    holes=[ResolvedHoleSpec(diameter=0.25, location="top-left")],
    hole_cutter_diameter=0.060,
)

# Cutting distances of the fixture geometry, in inches.
UV_TEXT_IN = 3.0  # rect_path(0.5, 2.5, 1.0, 0.5)
WB_TEXT_IN = 6.0  # rect_path(1.0, 4.0, 2.0, 1.0)
WB_BORDER_IN = 18.0  # rect_path(1.0, 4.0, 6.0, 3.0)
WB_HOLE_SIDE_IN = 0.25  # circle_path(1.5, 3.5, 0.25)
WB_HOLE_IN = 4 * WB_HOLE_SIDE_IN
# Pen-up travel between the bh file's boundary and its hole: from the
# rectangle's closing point (1.0, 4.0) to the hole's pen-up (1.375, 3.625).
WB_BH_RAPID_IN = math.hypot(0.375, -0.375)


def make_job(tmp_path: Path) -> Dict[str, Any]:
    """A two-plate job whose emitted files have known total lengths.

    Plate 1 (uv): ``rect-a`` at y-up (0.5, 2.5), engraved by the 0.030 txt
    cutter as a 1.0 x 0.5 rectangle.
    Plate 2 (wb): ``round-b`` (relief) at y-up (1.0, 4.0), engraved by the
    0.030 txt cutter as a 2.0 x 1.0 rectangle, plus a boundary (pen 2) and a
    drill hole (pen 3) by the 0.060 bh cutter.
    """
    rect_placement = placement(RECT_SPEC, label_id="rect-a_0", x=0.5, y=2.5)
    round_placement = placement(ROUND_RELIEF_SPEC, label_id="round-b_0", x=1.0, y=4.0)
    return {
        "resolved_labels": [RECT_SPEC, ROUND_RELIEF_SPEC],
        "packed_plates": [
            plate(plate_id="p1", material="uv", labels=[rect_placement]),
            plate(plate_id="p2", material="wb", labels=[round_placement]),
        ],
        "combined_by_plate": {
            1: document(section(rect_path(0.5, 2.5, 1.0, 0.5))),
            2: document(
                section(rect_path(1.0, 4.0, 2.0, 1.0)),
                section(
                    f"SP2;{rect_path(1.0, 4.0, 6.0, 3.0)}",
                    f"SP3;{circle_path(1.5, 3.5, WB_HOLE_SIDE_IN)}",
                ),
            ),
        },
        "plt_files": [
            write_plt(tmp_path, "01_uv_30_txt_job.plt", document(rect_path(0.5, 2.5, 1.0, 0.5))),
            write_plt(tmp_path, "02_wb_30_txt_job.plt", document(rect_path(1.0, 4.0, 2.0, 1.0))),
            write_plt(
                tmp_path,
                "02_wb_60_bh_job.plt",
                document(
                    rect_path(1.0, 4.0, 6.0, 3.0),
                    circle_path(1.5, 3.5, WB_HOLE_SIDE_IN),
                ),
            ),
        ],
        "material_by_plate": {1: "uv", 2: "wb"},
    }


class TestDistanceCutting:
    def test_cutting_distance_is_measured_from_the_emitted_file(self, tmp_path: Path) -> None:
        usage = stats_for(**make_job(tmp_path))
        row = file_row(usage, "01_uv_30_txt_job.plt", tmp_path)
        assert row["cutting"] == pytest.approx(UV_TEXT_IN)
        assert row["rapid"] == pytest.approx(0.0)
        assert row["strokes"] == 1
        assert row["plate"] == 1
        assert row["material"] == "uv"
        assert row["cutter"] == pytest.approx(0.03)
        assert row["kind"] == "txt"

    def test_arc_length_counts_as_cutting(self, tmp_path: Path) -> None:
        usage = stats_for(**make_job(tmp_path))
        row = file_row(usage, "02_wb_60_bh_job.plt", tmp_path)
        assert row["cutting"] == pytest.approx(WB_BORDER_IN + WB_HOLE_IN)
        assert row["strokes"] == 2

    def test_totals_and_foot_conversion(self, tmp_path: Path) -> None:
        total = stats_for(**make_job(tmp_path))["distance"]["total"]
        cutting = UV_TEXT_IN + WB_TEXT_IN + WB_BORDER_IN + WB_HOLE_IN
        assert total["cutting"] == pytest.approx(cutting)
        assert total["rapid"] == pytest.approx(WB_BH_RAPID_IN)
        assert total["total"] == pytest.approx(cutting + WB_BH_RAPID_IN)
        assert total["total_ft"] == pytest.approx((cutting + WB_BH_RAPID_IN) / 12.0)
        assert stats_for(**make_job(tmp_path))["distance"]["units"] == "inches"

    def test_empty_job_reports_distance_as_unavailable(self) -> None:
        distance = stats_for()["distance"]
        assert distance["available"] is False
        assert distance["note"]
        assert distance["files"] == []
        assert distance["by_class"] == dict.fromkeys(DISTANCE_CLASSES, 0.0)
        assert distance["total"] == {
            "cutting": 0.0,
            "rapid": 0.0,
            "total": 0.0,
            "cutting_ft": 0.0,
            "rapid_ft": 0.0,
            "total_ft": 0.0,
        }

    def test_job_with_labels_but_no_files_has_no_distance_rows(self, tmp_path: Path) -> None:
        job = make_job(tmp_path)
        usage = stats_for(**{**job, "plt_files": []})
        assert usage["distance"]["available"] is False
        # The non-distance sections keep counting from the spec.
        assert usage["labels"]["instances"] == 2

    def test_unparsable_file_is_reported_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = make_job(tmp_path)

        def boom(path: Path) -> Any:
            raise RuntimeError("simulated parser failure")

        monkeypatch.setattr(PLTParser, "parse_file", boom)
        usage = stats_for(**job)
        rows = usage["distance"]["files"]
        assert len(rows) == 3
        assert all("error" in row for row in rows)
        assert all(row["cutting"] == pytest.approx(0.0) for row in rows)
        # The report is still produced, so the job is not silently clean.
        assert usage["distance"]["available"] is True


class TestDistanceStrokeClasses:
    def test_text_relief_borders_and_holes_are_separated(self, tmp_path: Path) -> None:
        by_class = stats_for(**make_job(tmp_path))["distance"]["by_class"]
        assert by_class["text"] == pytest.approx(UV_TEXT_IN)
        assert by_class["relief"] == pytest.approx(WB_TEXT_IN)
        assert by_class["borders"] == pytest.approx(WB_BORDER_IN)
        assert by_class["holes"] == pytest.approx(WB_HOLE_IN)
        assert by_class["unattributed"] == pytest.approx(0.0)

    def test_rapid_travel_is_never_attributed_to_a_stroke_class(self, tmp_path: Path) -> None:
        usage = stats_for(**make_job(tmp_path))
        distance = usage["distance"]
        assert sum(distance["by_class"].values()) == pytest.approx(distance["total"]["cutting"])
        assert distance["by_movement"] == {
            "cutting": pytest.approx(distance["total"]["cutting"]),
            "rapid": pytest.approx(distance["total"]["rapid"]),
        }

    def test_relief_label_text_lands_in_the_relief_class(self, tmp_path: Path) -> None:
        spec = label("r", material="uv", relief=True)
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        usage = stats_for(
            resolved_labels=[spec],
            packed_plates=[
                plate(material="uv", labels=[placement(spec, label_id="r_0", x=0.5, y=2.5)])
            ],
            combined_by_plate={1: document(content)},
            plt_files=[write_plt(tmp_path, "01_uv_30_txt_job.plt", document(content))],
            material_by_plate={1: "uv"},
        )["distance"]
        assert usage["by_class"]["relief"] == pytest.approx(UV_TEXT_IN)
        assert usage["by_class"]["text"] == pytest.approx(0.0)

    def test_strokes_outside_every_label_are_unattributed(self, tmp_path: Path) -> None:
        spec = label("a", material="uv")
        inside = rect_path(0.5, 2.5, 1.0, 0.5)
        outside = rect_path(9.0, 7.5, 1.0, 0.5)  # beyond every slot on the plate
        usage = stats_for(
            resolved_labels=[spec],
            # No placements: the whole plate's strokes are unattributable.
            packed_plates=[plate(material="uv")],
            combined_by_plate={1: document(inside, outside)},
            plt_files=[write_plt(tmp_path, "01_uv_30_txt_job.plt", document(inside, outside))],
            material_by_plate={1: "uv"},
        )["distance"]
        assert usage["by_class"]["unattributed"] == pytest.approx(2 * UV_TEXT_IN)
        assert usage["unattributed"] == {
            "strokes": 2,
            "cutting": pytest.approx(2 * UV_TEXT_IN),
            # The pen-up gap between the two rects travels to the second
            # (unattributed) stroke and is charged to it.
            "rapid": pytest.approx(math.hypot(8.5, 5.0)),
        }
        # Nothing was attributed, so no per-label rows exist.
        assert usage["by_label"] == []


class TestDistanceRapid:
    def test_pen_up_travel_between_strokes_counts_as_rapid(self, tmp_path: Path) -> None:
        spec = label("a", material="uv")
        place = placement(spec, label_id="a_0", x=0.4, y=1.1)
        content = document(
            section(
                f"PU{u(0.5)},{u(0.5)};PD{u(0.5)},{u(1.0)};",
                f"PU{u(1.0)},{u(1.0)};PD{u(1.0)},{u(0.5)};",
            )
        )
        usage = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[place])],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "01_uv_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
        )["distance"]
        assert usage["total"]["cutting"] == pytest.approx(1.0)
        # 0.5 in between the two strokes, inside the label's slot.
        assert usage["total"]["rapid"] == pytest.approx(0.5)
        assert usage["total"]["total"] == pytest.approx(1.5)
        assert usage["by_label"][0]["rapid"] == pytest.approx(0.5)


class TestDistanceCounters:
    def test_every_counter_reconciles_with_the_total(self, tmp_path: Path) -> None:
        distance = stats_for(**make_job(tmp_path))["distance"]
        for key in ("by_cutter", "by_material", "by_plate", "by_label"):
            cutting = sum(row["cutting"] for row in distance[key])
            rapid = sum(row["rapid"] for row in distance[key])
            assert cutting == pytest.approx(distance["total"]["cutting"]), key
            assert rapid == pytest.approx(distance["total"]["rapid"]), key

    def test_cutter_and_kind_come_from_the_file_name(self, tmp_path: Path) -> None:
        distance = stats_for(**make_job(tmp_path))["distance"]
        rows = {(row["cutter"], row["kind"]): row for row in distance["by_cutter"]}
        assert set(rows) == {(0.03, "txt"), (0.06, "bh")}
        assert rows[(0.03, "txt")]["cutting"] == pytest.approx(UV_TEXT_IN + WB_TEXT_IN)
        assert rows[(0.03, "txt")]["files"] == 2
        assert rows[(0.06, "bh")]["cutting"] == pytest.approx(WB_BORDER_IN + WB_HOLE_IN)
        assert rows[(0.06, "bh")]["rapid"] == pytest.approx(WB_BH_RAPID_IN)

    def test_unidentifiable_cutter_is_its_own_bucket(self, tmp_path: Path) -> None:
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            # No plate/material/cutter tokens the stem parser can read, and no
            # plate frame: the tool is genuinely unknown and must not vanish.
            plt_files=[write_plt(tmp_path, "uv_txt_job.plt", document(content))],
        )["distance"]
        assert distance["by_cutter"] == [
            {
                "cutter": UNKNOWN_CUTTER,
                "kind": "txt",
                "cutting": pytest.approx(UV_TEXT_IN),
                "rapid": pytest.approx(0.0),
                "files": 1,
            }
        ]
        # The stroke is unattributed: there is no plate frame to place it.
        assert distance["by_material"] == [
            {
                "material": None,
                "material_label": "(unset)",
                "cutting": pytest.approx(UV_TEXT_IN),
                "rapid": pytest.approx(0.0),
            }
        ]

    def test_plate_and_material_fall_back_to_the_file_name(self, tmp_path: Path) -> None:
        spec = label("a", material="uv")
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.5, y=2.5)])],
            combined_by_plate={},
            # Plate 3 does not exist in this job, so its material can only
            # come from the file name.
            plt_files=[write_plt(tmp_path, "03_wbuv_30_txt_job.plt", document(content))],
            material_by_plate={1: "uv"},
        )["distance"]
        assert [row["plate"] for row in distance["by_plate"]] == [3]
        assert [row["material"] for row in distance["by_material"]] == ["wbuv"]

    def test_cutter_from_the_file_beats_the_inferred_frame(self, tmp_path: Path) -> None:
        spec = label("a", material="uv")
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.5, y=2.5)])],
            combined_by_plate={1: document(content)},
            plt_files=[write_plt(tmp_path, "01_uv_15_txt_job.plt", document(content))],
            material_by_plate={1: "uv"},
        )["distance"]
        assert distance["by_cutter"][0]["cutter"] == pytest.approx(0.015)

    def test_label_rows_carry_the_spec_and_placement_identity(self, tmp_path: Path) -> None:
        distance = stats_for(**make_job(tmp_path))["distance"]
        row = row_by(distance["by_label"], "label_id", "round-b_0")
        assert row["spec_id"] == "round-b"
        assert row["plate"] == 2
        assert row["material"] == "wb"
        assert row["cutting"] == pytest.approx(WB_TEXT_IN + WB_BORDER_IN + WB_HOLE_IN)
        assert row["by_class"]["relief"] == pytest.approx(WB_TEXT_IN)

    def test_rows_are_sorted_by_distance_then_key(self, tmp_path: Path) -> None:
        distance = stats_for(**make_job(tmp_path))["distance"]
        assert [row["label_id"] for row in distance["by_label"]] == ["round-b_0", "rect-a_0"]
        assert [row["plate"] for row in distance["by_plate"]] == [2, 1]
        assert [row["material"] for row in distance["by_material"]] == ["wb", "uv"]
        assert [row["cutter"] for row in distance["by_cutter"]] == [0.06, 0.03]

    def test_files_are_listed_in_write_order(self, tmp_path: Path) -> None:
        job = make_job(tmp_path)
        distance = stats_for(**job)["distance"]
        assert [row["path"] for row in distance["files"]] == [
            str(path) for path in job["plt_files"]
        ]


class TestUsageEdgeCases:
    """The defensive corners of the accounting helpers."""

    def test_arc_segment_box_spans_the_sweep(self) -> None:
        # The parser approximates large ``AA`` sweeps into line segments, so an
        # ``ArcSegment`` reaches the geometry helpers from an optimised
        # (arc-preserving) path only - box it directly.
        from plt_optimizer.core.models import ArcSegment, Coordinate, StrokePath
        from plt_optimizer.generate.usage_stats import _segment_box

        arc = ArcSegment(
            start=Coordinate(x=u(1.0), y=u(1.0)),
            end=Coordinate(x=u(3.0), y=u(1.0)),
            center=Coordinate(x=u(2.0), y=u(1.0)),
            sweep_angle=180.0,
            is_cutting=True,
        )
        assert StrokePath is not None  # documentation: the box takes a path's segments
        box = _segment_box(arc)
        # Boxed by centre and radius, so the result is the full circle's extent
        # (conservative for a partial sweep, which is what slot attribution needs).
        assert (box.x_max - box.x_min) == pytest.approx(u(2.0))
        assert (box.y_max - box.y_min) == pytest.approx(u(2.0))

    def test_round_passes_none_through_and_bands_to_the_machine_grid(self) -> None:
        from plt_optimizer.generate.usage_stats import _band, _round

        assert _round(None) is None
        assert _round(0.123456789) == pytest.approx(0.123457)
        assert _band(0.25 + 1e-12) == _band(0.25)

    def test_sort_token_orders_mixed_bucket_keys(self) -> None:
        from plt_optimizer.generate.usage_stats import _sort_token

        # None first, then numbers, then strings; composite keys order by part.
        assert sorted(["wb", 0.06, None, True, 2], key=_sort_token) == [
            None,
            0.06,
            True,
            2,
            "wb",
        ]
        assert _sort_token((4.0, "rect", "uv"))[1] == pytest.approx(4.0)

    def test_cutter_and_material_formatters_cover_every_kind(self) -> None:
        from plt_optimizer.generate.usage_stats import _fmt_cutter, _material_label

        assert _fmt_cutter(None) == "none"
        assert _fmt_cutter("(unset)") == "(unset)"
        assert _fmt_cutter(0.0625) == "0.062"
        assert _material_label(None) == "(unset)"
        assert _material_label("uv") == "uv"

    def test_relief_cutter_ladder_is_part_of_the_declared_set(self) -> None:
        spec = label(
            "r",
            relief=True,
            lines=[line("AB", 0.25, 0.060)],
            relief_engraving_cutters=[0.090, 0.060],
        )
        rows = stats_for(resolved_labels=[spec])["labels"]["by_cutter"]
        # 0.015 is the line's declared bold companion, 0.060 the line's own tool
        # (and the ladder's second rung), 0.090 the ladder's coarse rung.
        assert [(row["cutter_diameter"], row["count"]) for row in rows] == [
            (0.015, 1),
            (0.06, 1),
            (0.09, 1),
        ]

    def test_line_without_engravable_text_contributes_nothing(self) -> None:
        usage = stats_for(
            resolved_labels=[label("a", lines=[line("", 0.25, 0.030), line("AB", 0.25, 0.030)])]
        )["characters"]
        assert usage["total"] == 2
        assert [row["count"] for row in usage["by_text_height"]] == [2]

    def test_pen_up_only_stroke_does_not_break_position_tracking(self, tmp_path: Path) -> None:
        spec = label("a", material="uv")
        content = document(
            "PU200,200;PD400,200;",
            "PU1000,1000;",
            f"PU{u(0.6)},{u(1.0)};PD{u(0.6)},{u(0.6)};",
        )
        distance = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.5, y=1.1)])],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "01_uv_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
        )["distance"]
        # Measured on the device grid: 200 units + (1016 - 610) units.
        assert distance["total"]["cutting"] == pytest.approx((200 + 406) / 1016.0)

    def test_routing_file_without_a_plate_frame_is_unattributed(self, tmp_path: Path) -> None:
        content = rect_path(1.0, 4.0, 6.0, 3.0)
        distance = stats_for(
            plt_files=[write_plt(tmp_path, "02_wb_60_bh_job.plt", document(content))],
        )["distance"]
        assert distance["by_class"]["unattributed"] == pytest.approx(WB_BORDER_IN)
        assert distance["by_class"]["borders"] == pytest.approx(0.0)

    def test_plate_token_too_large_to_be_a_real_plate_still_keys_the_row(
        self, tmp_path: Path
    ) -> None:
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            packed_plates=[plate(plate_id="p1", material="uv")],
            plt_files=[write_plt(tmp_path, "99999999999999_uv_30_txt_job.plt", document(content))],
        )["distance"]
        # Python integers are unbounded, so the token is a plate number; the
        # strokes are unattributed (no such plate frame) and the distance is
        # still reported against the plate the name claims.
        assert [row["plate"] for row in distance["by_plate"]] == [99999999999999]
        assert distance["files"][0]["material"] == "uv"
        assert distance["by_class"]["unattributed"] == pytest.approx(UV_TEXT_IN)

    def test_single_plate_job_resolves_a_plate_less_file_name(self, tmp_path: Path) -> None:
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            packed_plates=[plate(plate_id="p1", material="uv")],
            plt_files=[write_plt(tmp_path, "uv_30_txt_job.plt", document(content))],
        )["distance"]
        assert [row["plate"] for row in distance["by_plate"]] == [1]

    def test_material_tag_resolves_the_plate_among_several(self, tmp_path: Path) -> None:
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            packed_plates=[
                plate(plate_id="p1", material="uv"),
                plate(plate_id="p2", material="wb"),
            ],
            plt_files=[write_plt(tmp_path, "wb_60_bh_job.plt", document(content))],
        )["distance"]
        assert [row["plate"] for row in distance["by_plate"]] == [2]
        assert [row["material"] for row in distance["by_material"]] == ["wb"]

    def test_ambiguous_material_tag_resolves_to_no_plate(self, tmp_path: Path) -> None:
        # Two plates of the SAME material and a file name without a plate
        # number: the plate is genuinely ambiguous, so the travel is reported
        # without one rather than guessed.
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            packed_plates=[
                plate(plate_id="p1", material="uv"),
                plate(plate_id="p2", material="uv"),
            ],
            plt_files=[write_plt(tmp_path, "uv_60_bh_job.plt", document(content))],
        )["distance"]
        assert distance["files"][0]["plate"] is None
        # The travel is not dropped: it is reported against no plate.
        assert distance["by_plate"] == [
            {
                "plate": None,
                "material": None,
                "cutting": pytest.approx(UV_TEXT_IN),
                "rapid": pytest.approx(0.0),
            }
        ]

    def test_stroke_without_a_pen_up_still_signatures(self, tmp_path: Path) -> None:
        # A hand-edited file can cut without the writer's leading pen-up; the
        # signature must not require one.
        spec = label("a", material="uv", relief=True)
        content = document("PD100,100,200,100;")
        distance = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.0, y=2.0)])],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "01_uv_60_bh_job.plt", content)],
            material_by_plate={1: "uv"},
        )["distance"]
        assert distance["total"]["cutting"] > 0.0
        assert distance["available"] is True


# ==============================================================================
# SP3 - summary.txt rendering and the report.json contract
# ==============================================================================


class TestFormatUsageLines:
    def test_text_height_row_shows_nominal_then_engraved(self, tmp_path: Path) -> None:
        """The ordered height leads, the cutter-adjusted height is parenthesised."""
        usage = stats_for(
            resolved_labels=[
                label("a", lines=[line("AB", 0.25, 0.030, toolpath_text_height=0.205)])
            ]
        )
        assert "by text height: 0.250 (0.205) x2" in "\n".join(format_usage_lines(usage))

    def test_text_height_row_drops_the_parenthetical_when_unadjusted(self, tmp_path: Path) -> None:
        """A line that engraves at its ordered size is printed once, not twice."""
        usage = stats_for(
            resolved_labels=[label("a", lines=[line("AB", 0.25, 0.030, toolpath_text_height=0.25)])]
        )
        assert "by text height: 0.250 x2" in "\n".join(format_usage_lines(usage))

    def test_every_section_is_rendered(self, tmp_path: Path) -> None:
        text = "\n".join(format_usage_lines(stats_for(**make_job(tmp_path))))
        assert "Job usage:" in text
        assert "labels: 2 labels (2 specs)" in text
        assert "rect=1  round=1" in text
        assert "plain=1  relief=1" in text
        assert "by material: uv=1  wb=1" in text
        # The relief label's text engraves on the finer rung of its cutter
        # ladder, so the declared set spans 0.015 / 0.030 / 0.060.
        assert "by cutter: 0.030 x2  0.015 x1  0.060 x1" in text
        assert "characters: 9 engraved glyphs (0 space(s) excluded" in text
        assert "by text height: 0.250 x9" in text
        assert "drill holes: 1" in text
        # The hole is cutter-compensated (the spec default), so the circle the
        # machine cuts is narrower than the requested 0.250 in.
        assert "by diameter: 0.250 (engraved 0.190) x1" in text
        assert "material: 2 plates  192.000 sq in (1.333 sq ft)" in text
        assert "uv: 1 plate  96.000 sq in (0.667 sq ft)" in text
        assert "label nominal area: 26.000 sq in" in text
        assert "label bounding-box area: 26.000 sq in" in text
        assert "tool distance (inches, measured from the emitted PLT files):" in text

    def test_distance_total_reads_cut_plus_rapid_equals(self, tmp_path: Path) -> None:
        text = "\n".join(format_usage_lines(stats_for(**make_job(tmp_path))))
        row = next(line for line in text.splitlines() if "total: cutting" in line)
        assert " + rapid " in row and " = " in row
        assert row.index("rapid") < row.index("=")

    def test_per_label_rows_are_truncated_with_a_pointer_to_json(self) -> None:
        total = SUMMARY_USAGE_LABEL_LIMIT + 7
        usage = stats_for()
        usage["distance"] = {
            "available": True,
            "note": "",
            "units": "inches",
            "total": {"cutting": 1.0, "rapid": 0.0, "total": 1.0},
            "by_movement": {"cutting": 1.0, "rapid": 0.0},
            "by_class": dict.fromkeys(DISTANCE_CLASSES, 1.0),
            "by_cutter": [],
            "by_material": [],
            "by_plate": [],
            "by_label": [
                {
                    "label_id": f"l{index:03d}_0",
                    "spec_id": f"l{index:03d}",
                    "plate": 1,
                    "material": "uv",
                    "cutting": 1.0,
                    "rapid": 0.0,
                    "by_class": {DISTANCE_CLASSES[0]: 1.0},
                }
                for index in range(total)
            ],
            "files": [{"path": "01_uv_30_txt_job.plt"}],
            "unattributed": {"strokes": 0, "cutting": 0.0, "rapid": 0.0},
        }
        text = "\n".join(format_usage_lines(usage))
        assert f"... and {total - SUMMARY_USAGE_LABEL_LIMIT} more (see report.json)" in text
        assert text.count("  cut 1.000") == SUMMARY_USAGE_LABEL_LIMIT

    def test_unavailable_distance_says_so(self) -> None:
        text = "\n".join(format_usage_lines(stats_for()))
        assert "Not measured: the job produced no PLT files" in text
        assert "by cutter:" not in text.split("tool distance")[1]

    def test_unattributed_strokes_are_reported_in_the_summary(self, tmp_path: Path) -> None:
        spec = label("a", material="uv")
        inside = rect_path(0.5, 2.5, 1.0, 0.5)
        outside = rect_path(9.0, 7.5, 1.0, 0.5)
        usage = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.5, y=2.5)])],
            combined_by_plate={1: document(inside, outside)},
            plt_files=[write_plt(tmp_path, "01_uv_30_txt_job.plt", document(inside, outside))],
            material_by_plate={1: "uv"},
        )
        text = "\n".join(format_usage_lines(usage))
        assert "unattributed: 1 stroke(s)" in text

    def test_partial_usage_block_renders_zeroes_not_crashes(self) -> None:
        text = "\n".join(format_usage_lines({}))
        assert "Job usage:" in text
        assert "labels: 0 labels (0 specs)" in text
        assert "Not measured" in text


class TestReportContract:
    def test_usage_round_trips_through_json(self, tmp_path: Path) -> None:
        usage = stats_for(**make_job(tmp_path))
        assert json.loads(json.dumps(usage)) == usage

    def test_usage_is_json_key_safe(self, tmp_path: Path) -> None:
        # Every mapping key must be a str/int the JSON writer accepts.
        def walk(node: Any) -> Any:
            if isinstance(node, dict):
                assert all(isinstance(key, (str, int)) for key in node)
                return [walk(value) for value in node.values()]
            if isinstance(node, list):
                return [walk(value) for value in node]
            assert isinstance(node, (str, int, float, bool, type(None)))
            return node

        walk(stats_for(**make_job(tmp_path)))

    def _report(self, tmp_path: Path, usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return build_report(
            job_name="job",
            job_id="job_0001",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=tmp_path / "generation_report",
            status="success",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
            duration_seconds=1.0,
            usage=usage,
        )

    def test_report_carries_the_usage_block_in_summary(self, tmp_path: Path) -> None:
        usage = stats_for(**make_job(tmp_path))
        report = self._report(tmp_path, usage)
        assert report["usage"]["labels"]["instances"] == 2
        summary = format_summary(report)
        assert "Job usage:" in summary
        assert "tool distance (inches" in summary
        # The usage section is rendered, not dumped as JSON.
        assert '"instances"' not in summary

    def test_failure_report_omits_the_usage_section(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, None)
        assert report["usage"] is None
        assert "Job usage:" not in format_summary(report)
