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
    CUTTING_VELOCITY_KEY,
    DISTANCE_CLASSES,
    DWELL_TIME_KEY,
    OZ_PER_LB,
    PLUNGE_VELOCITY_KEY,
    SUMMARY_USAGE_LABEL_LIMIT,
    TIME_OPERATIONS,
    UNKNOWN_CUTTER,
    Z_CLEARANCE_KEY,
    Z_UP_VELOCITY_KEY,
    _fmt_hms,
    _fmt_lb,
    build_usage_stats,
    format_usage_lines,
)
from plt_optimizer.generate.vectorize import _format_cutter, _format_plate_prefix

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
    """A placement of ``source`` on a plate.

    ``x``/``y`` are the slot's lower-left corner in the emitted plate frame (the
    frame :func:`rect_path` draws into), inches.
    """
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
    """Pen-up-led closed rectangle with its lower-left corner at ``(x, y)``.

    The plate frame is the additive one the assembly produces: a label placed at
    ``(x, y)`` draws into ``[x, x + w] x [y, y + h]`` (device Y up), so a fixture
    built around a placement at ``(x, y)`` lands inside that placement's slot by
    passing the same ``y`` here.

    Cutting distance: ``2 * (w + h)`` inches.
    """
    x0, y0, x1, y1 = u(x), u(y), u(x + w), u(y + h)
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
    emits the literal ``PU0,0;`` (pen up to the safe position) before every label
    it places, so the fixture content looks like a real assembled plate. The
    attribution does not split on it: slots come from the placements and geometry
    is owned by point containment.
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
    job_spec: Any = None,
    job_config: Any = None,
) -> Dict[str, Any]:
    """Call the engine with every argument defaulting to empty."""
    return build_usage_stats(
        resolved_labels=resolved_labels,
        packed_plates=packed_plates,
        combined_by_plate=combined_by_plate or {},
        plt_files=list(plt_files),
        material_by_plate=material_by_plate or {},
        job_spec=job_spec,
        job_config=job_config,
    )


def row_by(rows: Sequence[Dict[str, Any]], key: str, value: Any) -> Dict[str, Any]:
    """The single row of ``rows`` whose ``key`` equals ``value``."""
    matches = [row for row in rows if row[key] == value]
    assert len(matches) == 1, f"expected exactly one {key}={value!r} in {rows!r}"
    return matches[0]


def file_row(usage: Dict[str, Any], name: str, root: Path) -> Dict[str, Any]:
    """The ``distance.files`` row of the PLT fixture called ``name``."""
    return row_by(usage["distance"]["files"], "path", str(root / name))


# ---------------------------------------------------------------------------
# Run-time estimate fixtures (SP6)
#
# The registry below is test-owned: every expectation is exact arithmetic on
# these numbers, so retuning the shop's ``job-config.json`` cannot break them
# (tests/AGENTS.md). Rates are chosen to divide the fixture distances cleanly.
# ---------------------------------------------------------------------------

# Seconds of one stroke's Z cycle at these parameters: 0.2 in / 2.0 in-per-sec
# plunge, plus 1000 ms z-up, plus 50 ms dwell.
TIME_STROKE_CYCLE_S = 0.1 + 1.0 + 0.05
TIME_TXT_VELOCITY = 2.0
TIME_BH_VELOCITY = 4.0


def tool_registry(
    *,
    txt_velocity: float = TIME_TXT_VELOCITY,
    bh_velocity: float = TIME_BH_VELOCITY,
    cutting_max: float = 100.0,
) -> Dict[str, Any]:
    """A ``tool_options`` registry carrying the five parameters of the time model.

    Args:
        txt_velocity: The ``text`` layer's cutting velocity (in/sec).
        bh_velocity: The ``borders_holes`` layer's cutting velocity (in/sec).
        cutting_max: The registry's ``max`` for ``cutting_velocity``, so a
            clamp can be provoked without touching the other options.

    Returns:
        The registry mapping for :class:`JobDefaults`.
    """
    return {
        "cutting_velocity": {
            "name": "Cutting Velocity",
            "command": "VS",
            "type": "float",
            "default": {"text": txt_velocity, "borders_holes": bh_velocity},
            "min": 0.1,
            "max": cutting_max,
        },
        "plunge_velocity": {
            "name": "Plunge Velocity",
            "command": "VZ",
            "type": "float",
            "default": 2.0,
            "min": 0.1,
            "max": 100.0,
        },
        "z_clearance": {
            "name": "Z Clearance",
            "command": "ZU",
            "type": "float",
            "default": 0.2,
            "min": 0.0,
            "max": 10.0,
        },
        "z_up_velocity": {
            "name": "Z Up",
            "command": "ZO105,",
            "type": "int",
            "default": 1000,
            "min": 0,
            "max": 10000,
        },
        "dwell_time": {
            "name": "Dwell",
            "command": "ZO124,",
            "type": "int",
            "default": 50,
            "min": 0,
            "max": 10000,
        },
    }


def tool_config(**overrides: Any) -> Any:
    """A :class:`JobConfig` whose defaults carry the time-model registry.

    Args:
        **overrides: Fields to set on the defaults, e.g. ``rapid_velocity=3.0``
            or ``tool_options=tool_registry(cutting_max=1.0)``.

    Returns:
        The config.
    """
    from plt_optimizer.generate.job_config import JobConfig, JobDefaults

    fields: Dict[str, Any] = {"tool_options": tool_registry()}
    fields.update(overrides)
    return JobConfig(path=Path("test-job-config.json"), defaults=JobDefaults(**fields))


def job_spec(**tool_options: Any) -> Any:
    """A minimal :class:`JobSpec`, optionally overriding tool options.

    Args:
        **tool_options: The spec's ``tool_options`` overrides.

    Returns:
        The spec.
    """
    from plt_optimizer.generate.schema import ContentEntry, JobSpec

    return JobSpec(
        job_name="timing",
        width=4.0,
        height=2.0,
        content=[ContentEntry(text="SIGN")],
        tool_options=dict(tool_options) or None,
    )


def time_stats(tmp_path: Path, **kwargs: Any) -> Dict[str, Any]:
    """The ``usage`` block of :func:`make_job` measured with a rate registry.

    Args:
        tmp_path: Directory for the PLT fixtures.
        **kwargs: Forwarded to :func:`tool_config` (the registry overrides).

    Returns:
        The full ``usage`` block.
    """
    return stats_for(**make_job(tmp_path), job_spec=job_spec(), job_config=tool_config(**kwargs))


def total_of(node: Any) -> float:
    """Sum one ``usage.time`` bucket into seconds, list of rows or mapping.

    Args:
        node: A bucket: a list of rows carrying ``total``, or a mapping of
            name to seconds.

    Returns:
        The seconds.
    """
    if isinstance(node, list):
        return float(sum(float(row["total"]) for row in node))
    return float(sum(float(value) for value in node.values()))


# ==============================================================================
# SP1 - labels
# ==============================================================================


class TestLabelsUsage:
    def test_counts_physical_labels_not_specs(self) -> None:
        usage = stats_for(
            resolved_labels=[
                label("a", count=3, material="uv"),
                label("b", count=2, material="wk", shape_type="round"),
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
                label("c", count=1, material="wk", shape_type="round"),
            ]
        )["labels"]
        assert {row["material"]: row["count"] for row in usage["by_material"]} == {
            "uv": 3,
            "wk": 1,
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
                label("b", count=1, width=4.0, height=2.0, material="wk"),
                label("c", count=1, width=6.0, height=3.0, material="uv", shape_type="round"),
            ]
        )["labels"]
        assert [
            (row["width"], row["height"], row["shape"], row["material"], row["count"])
            for row in usage["by_size"]
        ] == [
            (4.0, 2.0, "rect", "uv", 2),
            (4.0, 2.0, "rect", "wk", 1),
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
            material_by_plate={1: "uv", 2: "wk"},
        )["material"]
        rows = {row["material"]: row["sq_in"] for row in usage["by_material"]}
        assert rows == {"uv": pytest.approx(96.0), "wk": pytest.approx(60.0)}
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
    material="wk",
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
WK_TEXT_IN = 6.0  # rect_path(1.0, 4.0, 2.0, 1.0)
WK_BORDER_IN = 18.0  # rect_path(1.0, 4.0, 6.0, 3.0)
WK_HOLE_SIDE_IN = 0.25  # circle_path(1.5, 4.5, 0.25)
WK_HOLE_IN = 4 * WK_HOLE_SIDE_IN
# Pen-up travel between the bh file's boundary and its hole: from the
# rectangle's closing point (1.0, 4.0) to the hole's pen-up (1.375, 4.625).
WK_BH_RAPID_IN = math.hypot(0.375, 0.625)


def make_job(tmp_path: Path) -> Dict[str, Any]:
    """A two-plate job whose emitted files have known total lengths.

    Every piece of a label's geometry is drawn inside the box the packer
    reserved for it - that is what production does, and it is what lets the
    per-segment attribution name an owner for each stroke.

    Plate 1 (uv): ``rect-a`` in the slot at ``(0.5, 2.5)``, engraved by the
    0.030 txt cutter as a 1.0 x 0.5 rectangle.
    Plate 2 (wk): ``round-b`` (relief) in the 6.0 x 3.0 slot at ``(1.0, 4.0)``
    (the frame it engraves), engraved by the 0.030 txt cutter as a 2.0 x 1.0
    rectangle, plus a boundary (pen 2) and a drill hole (pen 3) by the 0.060
    bh cutter.
    """
    rect_placement = placement(RECT_SPEC, label_id="rect-a_0", x=0.5, y=2.5)
    round_placement = placement(
        ROUND_RELIEF_SPEC, label_id="round-b_0", x=1.0, y=4.0, width=6.0, height=3.0
    )
    return {
        "resolved_labels": [RECT_SPEC, ROUND_RELIEF_SPEC],
        "packed_plates": [
            plate(plate_id="p1", material="uv", labels=[rect_placement]),
            plate(plate_id="p2", material="wk", labels=[round_placement]),
        ],
        "combined_by_plate": {
            1: document(section(rect_path(0.5, 2.5, 1.0, 0.5))),
            2: document(
                section(rect_path(1.0, 4.0, 2.0, 1.0)),
                section(
                    f"SP2;{rect_path(1.0, 4.0, 6.0, 3.0)}",
                    f"SP3;{circle_path(1.5, 4.5, WK_HOLE_SIDE_IN)}",
                ),
            ),
        },
        "plt_files": [
            write_plt(tmp_path, "uv_p01_30_txt_job.plt", document(rect_path(0.5, 2.5, 1.0, 0.5))),
            write_plt(tmp_path, "wk_p02_30_txt_job.plt", document(rect_path(1.0, 4.0, 2.0, 1.0))),
            write_plt(
                tmp_path,
                "wk_p02_60_bh_job.plt",
                document(
                    rect_path(1.0, 4.0, 6.0, 3.0),
                    circle_path(1.5, 4.5, WK_HOLE_SIDE_IN),
                ),
            ),
        ],
        "material_by_plate": {1: "uv", 2: "wk"},
    }


class TestDistanceCutting:
    def test_cutting_distance_is_measured_from_the_emitted_file(self, tmp_path: Path) -> None:
        usage = stats_for(**make_job(tmp_path))
        row = file_row(usage, "uv_p01_30_txt_job.plt", tmp_path)
        assert row["cutting"] == pytest.approx(UV_TEXT_IN)
        assert row["rapid"] == pytest.approx(0.0)
        assert row["strokes"] == 1
        assert row["plate"] == 1
        assert row["material"] == "uv"
        assert row["cutter"] == pytest.approx(0.03)
        assert row["kind"] == "txt"

    def test_arc_length_counts_as_cutting(self, tmp_path: Path) -> None:
        usage = stats_for(**make_job(tmp_path))
        row = file_row(usage, "wk_p02_60_bh_job.plt", tmp_path)
        assert row["cutting"] == pytest.approx(WK_BORDER_IN + WK_HOLE_IN)
        assert row["strokes"] == 2

    def test_totals_and_foot_conversion(self, tmp_path: Path) -> None:
        total = stats_for(**make_job(tmp_path))["distance"]["total"]
        cutting = UV_TEXT_IN + WK_TEXT_IN + WK_BORDER_IN + WK_HOLE_IN
        assert total["cutting"] == pytest.approx(cutting)
        assert total["rapid"] == pytest.approx(WK_BH_RAPID_IN)
        assert total["total"] == pytest.approx(cutting + WK_BH_RAPID_IN)
        assert total["total_ft"] == pytest.approx((cutting + WK_BH_RAPID_IN) / 12.0)
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
        assert by_class["relief"] == pytest.approx(WK_TEXT_IN)
        assert by_class["borders"] == pytest.approx(WK_BORDER_IN)
        assert by_class["holes"] == pytest.approx(WK_HOLE_IN)
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
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", document(content))],
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
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", document(inside, outside))],
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


class TestDistanceAttribution:
    """How geometry is matched to the label that owns it."""

    def test_one_path_spanning_two_labels_is_split_between_them(self, tmp_path: Path) -> None:
        # The sweep and merge optimizers join contours that were laid out apart,
        # so one parsed path can carry geometry of several labels. Attribution
        # walks segments: each part lands on the label it sits on, and the
        # per-label rows still sum to the file total.
        a = label("a", material="uv")
        b = label("b", material="uv")
        content = document(
            f"PU{u(0.5)},{u(0.5)};PD{u(1.5)},{u(0.5)},{u(2.5)},{u(0.5)},{u(3.5)},{u(0.5)};"
        )
        usage = stats_for(
            resolved_labels=[a, b],
            packed_plates=[
                plate(
                    material="uv",
                    labels=[
                        placement(a, label_id="a_0", x=0.0, y=0.0, width=2.0, height=1.0),
                        placement(b, label_id="b_0", x=2.0, y=0.0, width=2.0, height=1.0),
                    ],
                )
            ],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
        )["distance"]
        rows = {row["label_id"]: row for row in usage["by_label"]}
        # Three 1 in segments: the first two sit on a (the crossing segment's
        # midpoint is on the shared edge, which a covers), the last on b.
        assert rows["a_0"]["cutting"] == pytest.approx(2.0)
        assert rows["b_0"]["cutting"] == pytest.approx(1.0)
        assert usage["total"]["cutting"] == pytest.approx(3.0)
        assert usage["by_class"]["unattributed"] == pytest.approx(0.0)
        assert usage["unattributed"]["strokes"] == 0

    def test_a_placement_at_y_draws_into_the_row_above_it(self, tmp_path: Path) -> None:
        # The assembly translates a label ADDITIVELY: a placement at ``(x, y)``
        # draws into ``[x, x + width] x [y, y + height]``. This pins that frame -
        # a mirrored one leaves every total correct and silently shifts each
        # label's distance onto the label stacked above it.
        a = label("a", material="uv")
        b = label("b", material="uv")
        content = document(rect_path(0.0, 0.625, 1.0, 0.25))  # a box inside b's row
        usage = stats_for(
            resolved_labels=[a, b],
            packed_plates=[
                plate(
                    material="uv",
                    labels=[
                        placement(a, label_id="a_0", x=0.0, y=0.0, width=1.0, height=0.5),
                        placement(b, label_id="b_0", x=0.0, y=0.5, width=1.0, height=0.5),
                    ],
                )
            ],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
        )["distance"]
        assert [row["label_id"] for row in usage["by_label"]] == ["b_0"]
        assert usage["by_label"][0]["cutting"] == pytest.approx(2 * (1.0 + 0.25))
        assert usage["by_class"]["unattributed"] == pytest.approx(0.0)

    def test_rapid_travel_lands_on_the_label_it_arrives_at(self, tmp_path: Path) -> None:
        # The air gap between two strokes is charged to the DESTINATION label:
        # the tool is travelling to the label it is about to engrave.
        a = label("a", material="uv")
        b = label("b", material="uv")
        content = document(
            rect_path(0.0, 0.0, 1.0, 0.5),
            rect_path(0.0, 1.0, 1.0, 0.5),
        )
        usage = stats_for(
            resolved_labels=[a, b],
            packed_plates=[
                plate(
                    material="uv",
                    labels=[
                        placement(a, label_id="a_0", x=0.0, y=0.0, width=1.0, height=0.5),
                        placement(b, label_id="b_0", x=0.0, y=1.0, width=1.0, height=0.5),
                    ],
                )
            ],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
        )["distance"]
        rows = {row["label_id"]: row for row in usage["by_label"]}
        # The gap runs from a's closing point (0, 0) to b's pen-up (0, 1): 1 in,
        # charged to b.
        assert rows["a_0"]["rapid"] == pytest.approx(0.0)
        assert rows["b_0"]["rapid"] == pytest.approx(1.0)


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
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", content)],
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
        assert rows[(0.03, "txt")]["cutting"] == pytest.approx(UV_TEXT_IN + WK_TEXT_IN)
        assert rows[(0.03, "txt")]["files"] == 2
        assert rows[(0.06, "bh")]["cutting"] == pytest.approx(WK_BORDER_IN + WK_HOLE_IN)
        assert rows[(0.06, "bh")]["rapid"] == pytest.approx(WK_BH_RAPID_IN)

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
            plt_files=[write_plt(tmp_path, "wkuv_p03_30_txt_job.plt", document(content))],
            material_by_plate={1: "uv"},
        )["distance"]
        assert [row["plate"] for row in distance["by_plate"]] == [3]
        assert [row["material"] for row in distance["by_material"]] == ["wkuv"]

    def test_cutter_from_the_file_beats_the_inferred_frame(self, tmp_path: Path) -> None:
        spec = label("a", material="uv")
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.5, y=2.5)])],
            combined_by_plate={1: document(content)},
            plt_files=[write_plt(tmp_path, "uv_p01_15_txt_job.plt", document(content))],
            material_by_plate={1: "uv"},
        )["distance"]
        assert distance["by_cutter"][0]["cutter"] == pytest.approx(0.015)

    def test_label_rows_carry_the_spec_and_placement_identity(self, tmp_path: Path) -> None:
        distance = stats_for(**make_job(tmp_path))["distance"]
        row = row_by(distance["by_label"], "label_id", "round-b_0")
        assert row["spec_id"] == "round-b"
        assert row["plate"] == 2
        assert row["material"] == "wk"
        assert row["cutting"] == pytest.approx(WK_TEXT_IN + WK_BORDER_IN + WK_HOLE_IN)
        assert row["by_class"]["relief"] == pytest.approx(WK_TEXT_IN)

    def test_rows_are_sorted_by_distance_then_key(self, tmp_path: Path) -> None:
        distance = stats_for(**make_job(tmp_path))["distance"]
        assert [row["label_id"] for row in distance["by_label"]] == ["round-b_0", "rect-a_0"]
        assert [row["plate"] for row in distance["by_plate"]] == [2, 1]
        assert [row["material"] for row in distance["by_material"]] == ["wk", "uv"]
        assert [row["cutter"] for row in distance["by_cutter"]] == [0.06, 0.03]

    def test_files_are_listed_in_write_order(self, tmp_path: Path) -> None:
        job = make_job(tmp_path)
        distance = stats_for(**job)["distance"]
        assert [row["path"] for row in distance["files"]] == [
            str(path) for path in job["plt_files"]
        ]


class TestUsageEdgeCases:
    """The defensive corners of the accounting helpers."""

    def test_arc_segment_is_measured_by_length_and_owned_by_its_point(self, tmp_path: Path) -> None:
        # The parser approximates large ``AA`` sweeps into line segments, so an
        # ``ArcSegment`` reaches the measurement loop from an optimised
        # (arc-preserving) path only. Build one directly: a full circle starts
        # and ends on the circle, so its travel is the arc length and its owner
        # is the slot holding the point the tool is at.
        from plt_optimizer.core.models import (
            ArcSegment,
            Coordinate,
            PLTDocument,
            StrokePath,
        )
        from plt_optimizer.generate.usage_stats import (
            KIND_STRUCTURAL,
            _DistanceTotals,
            _measure_document,
            _path_signature,
            _PlateFrame,
            _Rect,
            _Slot,
        )

        radius = 0.25
        centre = (1.0, 4.5)
        arc = ArcSegment(
            start=Coordinate(x=u(centre[0] + radius), y=u(centre[1])),
            end=Coordinate(x=u(centre[0] + radius), y=u(centre[1])),
            center=Coordinate(x=u(centre[0]), y=u(centre[1])),
            sweep_angle=360.0,
            is_cutting=True,
        )
        path = StrokePath(
            pen_up_position=Coordinate(x=u(centre[0] + radius), y=u(centre[1])),
            segments=(arc,),
        )
        slot = _Slot(
            label_id="round-b_0",
            spec_id="round-b",
            rect=_Rect(x_min=u(1.0), y_min=u(4.0), x_max=u(3.0), y_max=u(5.0)),
            is_relief=True,
        )
        frame = _PlateFrame(
            plate_number=2,
            material="wk",
            slots=[slot],
            stroke_class={_path_signature(path): "holes"},
        )
        totals = _DistanceTotals()
        strokes = _measure_document(
            PLTDocument(stroke_paths=[path]), frame, KIND_STRUCTURAL, totals
        )

        assert strokes == 1
        assert totals.by_class["holes"] == pytest.approx(math.pi * 2 * radius)
        assert totals.by_label["round-b_0"][0] == pytest.approx(math.pi * 2 * radius)
        assert totals.class_by_label["round-b_0"]["holes"] == pytest.approx(math.pi * 2 * radius)
        assert totals.by_class["unattributed"] == pytest.approx(0.0)

    def test_round_passes_none_through_and_bands_to_the_machine_grid(self) -> None:
        from plt_optimizer.generate.usage_stats import _band, _round

        assert _round(None) is None
        assert _round(0.123456789) == pytest.approx(0.123457)
        assert _band(0.25 + 1e-12) == _band(0.25)

    def test_sort_token_orders_mixed_bucket_keys(self) -> None:
        from plt_optimizer.generate.usage_stats import _sort_token

        # None first, then numbers, then strings; composite keys order by part.
        assert sorted(["wk", 0.06, None, True, 2], key=_sort_token) == [
            None,
            0.06,
            True,
            2,
            "wk",
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
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
        )["distance"]
        # Measured on the device grid: 200 units + (1016 - 610) units.
        assert distance["total"]["cutting"] == pytest.approx((200 + 406) / 1016.0)

    def test_routing_file_without_a_plate_frame_is_unattributed(self, tmp_path: Path) -> None:
        content = rect_path(1.0, 4.0, 6.0, 3.0)
        distance = stats_for(
            plt_files=[write_plt(tmp_path, "wk_p02_60_bh_job.plt", document(content))],
        )["distance"]
        assert distance["by_class"]["unattributed"] == pytest.approx(WK_BORDER_IN)
        assert distance["by_class"]["borders"] == pytest.approx(0.0)

    def test_a_name_built_by_the_emitter_attributes_to_its_plate(self, tmp_path: Path) -> None:
        """Names assembled by the production helpers key plate attribution.

        ``_format_plate_prefix`` marks the plate number (``p02``) and
        ``_format_cutter_token`` pads the tool; ``build_usage_stats`` reads that
        name back through ``_parse_plt_stem``, which strips the marker, so the
        plate number (not the material tag) keys the report rows.
        """
        name = f"{_format_plate_prefix(2, 2, 'wk(uv)')}{_format_cutter(0.03)}_txt_job.plt"
        assert name == "wk_uv_p02_30_txt_job.plt"
        distance = stats_for(
            packed_plates=[plate(plate_id="p1", material="wk(uv)")],
            plt_files=[write_plt(tmp_path, name, document(rect_path(0.5, 2.5, 1.0, 0.5)))],
        )["distance"]
        assert [row["plate"] for row in distance["by_plate"]] == [2]
        assert distance["files"][0]["material"] == "wk_uv"
        assert distance["files"][0]["cutter"] == pytest.approx(0.03)

    def test_plate_token_too_large_to_be_a_real_plate_still_keys_the_row(
        self, tmp_path: Path
    ) -> None:
        content = rect_path(0.5, 2.5, 1.0, 0.5)
        distance = stats_for(
            packed_plates=[plate(plate_id="p1", material="uv")],
            plt_files=[write_plt(tmp_path, "uv_p99999999999999_30_txt_job.plt", document(content))],
        )["distance"]
        # Python integers are unbounded, so a marked token is a plate number at
        # any width (the emitter pads to two digits; the parser puts no cap on
        # it); the strokes are unattributed (no such plate frame) and the
        # distance is still reported against the plate the name claims.
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
                plate(plate_id="p2", material="wk"),
            ],
            plt_files=[write_plt(tmp_path, "wk_60_bh_job.plt", document(content))],
        )["distance"]
        assert [row["plate"] for row in distance["by_plate"]] == [2]
        assert [row["material"] for row in distance["by_material"]] == ["wk"]

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
            plt_files=[write_plt(tmp_path, "uv_p01_60_bh_job.plt", content)],
            material_by_plate={1: "uv"},
        )["distance"]
        assert distance["total"]["cutting"] > 0.0
        assert distance["available"] is True

    # ==============================================================================
    # SP3 - summary.txt rendering and the report.json contract
    # ==============================================================================

    def test_rapid_segment_and_segmentless_path_are_measured(self) -> None:
        # Defensive shape coverage: the parser puts the pen-up lead in
        # ``pen_up_position`` (never as a segment) and drops a pen-up with no
        # following cut, so both shapes reach the loop only from a synthetic
        # document. A rapid segment is owned by its DESTINATION and a segmentless
        # path still moves the tool's position (the next gap is measured from it).
        from plt_optimizer.core.models import (
            Coordinate,
            PLTDocument,
            StrokePath,
            StrokeSegment,
        )
        from plt_optimizer.generate.usage_stats import (
            KIND_TEXT,
            _DistanceTotals,
            _measure_document,
            _PlateFrame,
            _Rect,
            _Slot,
        )

        slot = _Slot(
            label_id="a_0",
            spec_id="a",
            rect=_Rect(x_min=u(1.0), y_min=u(1.0), x_max=u(2.0), y_max=u(2.0)),
            is_relief=False,
        )
        frame = _PlateFrame(plate_number=1, material="uv", slots=[slot])
        document = PLTDocument(
            stroke_paths=[
                # A pen-up move into the slot, then a cut inside it.
                StrokePath(
                    pen_up_position=Coordinate(x=u(0.0), y=u(0.0)),
                    segments=(
                        StrokeSegment(
                            start=Coordinate(x=u(0.0), y=u(0.0)),
                            end=Coordinate(x=u(1.5), y=u(1.5)),
                            is_cutting=False,
                        ),
                        StrokeSegment(
                            start=Coordinate(x=u(1.5), y=u(1.5)),
                            end=Coordinate(x=u(1.5), y=u(2.0)),
                            is_cutting=True,
                        ),
                    ),
                ),
                # A path with no segments at all: the return home.
                StrokePath(pen_up_position=Coordinate(x=u(5.0), y=u(5.0)), segments=()),
                # A path with neither segments nor a pen-up: it cannot move the
                # tool, so the next gap is still measured from where we are.
                StrokePath(pen_up_position=None, segments=()),
            ]
        )
        totals = _DistanceTotals()
        strokes = _measure_document(document, frame, KIND_TEXT, totals)

        assert strokes == 1  # only the path that cuts
        # The rapid lead (1.5*sqrt(2) in) is owned by the slot its destination
        # sits in; the 0.5 in cut is text. The gap to the segmentless path's
        # pen-up (5, 5) flies far outside every slot, so it is unattributed.
        assert totals.by_class["text"] == pytest.approx(0.5)
        assert totals.rapid == pytest.approx(math.hypot(1.5, 1.5) + math.hypot(3.5, 3.0))
        assert totals.by_label["a_0"][1] == pytest.approx(math.hypot(1.5, 1.5))
        assert totals.unattributed_rapid == pytest.approx(math.hypot(3.5, 3.0))

    def test_label_cutter_set_skips_missing_diameters(self) -> None:
        # ``_label_cutters`` is defensive about None diameters (a line's own is
        # non-optional today; the hole tools are): the declared set must stay
        # empty rather than inventing a diameter. Reach it with stubs.
        from types import SimpleNamespace
        from typing import cast

        from plt_optimizer.generate.resolution import ResolvedLabel
        from plt_optimizer.generate.usage_stats import _label_cutters

        stub = cast(
            ResolvedLabel,
            SimpleNamespace(
                content=[SimpleNamespace(effective_cutter_diameter=None)],
                effective_hole_cutter_diameter=None,
                hole_cutter_diameter=0.25,
                relief_engraving_cutters=None,
            ),
        )
        assert _label_cutters(stub) == [0.25]

    def test_text_height_row_without_a_nominal_shows_the_engraved_size(self) -> None:
        from plt_optimizer.generate.usage_stats import _height_band_label

        assert _height_band_label({"text_height": 0.2, "nominal_text_height": None}) == "0.200"
        assert (
            _height_band_label({"text_height": 0.2, "nominal_text_height": 0.25}) == "0.250 (0.200)"
        )

    def test_rect_area_never_goes_negative(self) -> None:
        # A degenerate rectangle (a zero-width slot, an inverted box) contributes
        # no area rather than subtracting from the material totals.
        from plt_optimizer.generate.usage_stats import _Rect

        assert _Rect(x_min=u(1.0), y_min=u(1.0), x_max=u(0.0), y_max=u(0.0)).area == 0.0
        assert _Rect(x_min=u(1.0), y_min=u(1.0), x_max=u(1.0), y_max=u(2.0)).area == 0.0

    def test_arc_signature_carries_the_arc_fields(self) -> None:
        # An arc's signature is its centre, radius and sweep - two circles that
        # differ only by radius must not share a class entry.
        from plt_optimizer.core.models import ArcSegment, Coordinate, StrokePath
        from plt_optimizer.generate.usage_stats import _path_signature

        def circle(radius: float) -> StrokePath:
            return StrokePath(
                pen_up_position=Coordinate(x=u(1.0 + radius), y=u(1.0)),
                segments=(
                    ArcSegment(
                        start=Coordinate(x=u(1.0 + radius), y=u(1.0)),
                        end=Coordinate(x=u(1.0 + radius), y=u(1.0)),
                        center=Coordinate(x=u(1.0), y=u(1.0)),
                        sweep_angle=360.0,
                        is_cutting=True,
                    ),
                ),
            )

        assert _path_signature(circle(0.25)) != _path_signature(circle(0.5))
        # A stroke the writer emitted without a leading pen-up has no PU term.
        bare = StrokePath(pen_up_position=None, segments=(circle(0.25).segments[0],))
        assert _path_signature(bare) == _path_signature(circle(0.25))[1:]


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
        assert "by material: uv=1  wk=1" in text
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
        # No density is configured in this fixture, so the section states that
        # the weight is not estimated (it must never read as 0 lb).
        assert "weight (estimated" in text
        assert "Not estimated:" in text
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
            "files": [{"path": "uv_p01_30_txt_job.plt"}],
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
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", document(inside, outside))],
            material_by_plate={1: "uv"},
        )
        text = "\n".join(format_usage_lines(usage))
        assert "unattributed: 1 stroke(s)" in text

    def test_partial_usage_block_renders_zeroes_not_crashes(self) -> None:
        text = "\n".join(format_usage_lines({}))
        assert "Job usage:" in text
        assert "labels: 0 labels (0 specs)" in text
        assert "Not measured" in text


# ==============================================================================
# SP6 - run time estimated from the emitted tool_options
# ==============================================================================

# The registry's per-stroke Z cycle: z_clearance 0.2 / plunge 2.0 = 0.1 s,
# z_up 1000 ms = 1.0 s, dwell 50 ms = 0.05 s.
CYCLE_PLUNGE_S = 0.1
CYCLE_Z_UP_S = 1.0
CYCLE_DWELL_S = 0.05
# The bh file's two strokes are the 18.0 in boundary and the 1.0 in hole; the
# txt files contribute 3.0 in (uv) and 6.0 in (wk) and one stroke each.
TXT_CUTTING_IN = UV_TEXT_IN + WK_TEXT_IN
BH_CUTTING_IN = WK_BORDER_IN + WK_HOLE_IN
JOB_STROKES = 4


class TestTimeEstimate:
    def test_the_time_block_is_always_present_beside_distance(self, tmp_path: Path) -> None:
        assert list(time_stats(tmp_path)) == [
            "labels",
            "characters",
            "holes",
            "material",
            "weight",
            "distance",
            "time",
        ]

    def test_no_config_means_no_estimate(self, tmp_path: Path) -> None:
        # The counts and the distance are unaffected: only the estimate stops.
        usage = stats_for(**make_job(tmp_path))
        assert usage["distance"]["total"]["cutting"] == pytest.approx(28.0)
        assert usage["time"]["available"] is False
        assert usage["time"]["total"] is None
        assert usage["time"]["by_cutter"] == []
        assert CUTTING_VELOCITY_KEY in usage["time"]["note"]

    def test_a_registry_without_cutting_velocity_disables_the_estimate(
        self, tmp_path: Path
    ) -> None:
        registry = tool_registry()
        del registry[CUTTING_VELOCITY_KEY]
        usage = time_stats(tmp_path, tool_options=registry)
        assert usage["time"]["available"] is False

    def test_a_layer_that_resolves_no_velocity_disables_the_whole_estimate(
        self, tmp_path: Path
    ) -> None:
        # Half an estimate is worse than none: the totals would not reconcile
        # with the distance the machine will actually travel. A non-positive
        # velocity is the config's way of saying "not set" (a dual default
        # that omits a layer is rejected outright by JobDefaults).
        registry = tool_registry()
        registry[CUTTING_VELOCITY_KEY]["min"] = 0.0
        registry[CUTTING_VELOCITY_KEY]["default"] = {
            "text": TIME_TXT_VELOCITY,
            "borders_holes": 0.0,
        }
        usage = time_stats(tmp_path, tool_options=registry)
        assert usage["time"]["available"] is False
        assert usage["time"]["total"] is None

    def test_cutting_time_divides_the_distance_by_the_layer_velocity(self, tmp_path: Path) -> None:
        operations = time_stats(tmp_path)["time"]["by_operation"]
        assert operations["cutting"] == pytest.approx(
            TXT_CUTTING_IN / TIME_TXT_VELOCITY + BH_CUTTING_IN / TIME_BH_VELOCITY
        )

    def test_rapid_velocity_falls_back_to_the_layer_cutting_velocity(self, tmp_path: Path) -> None:
        usage = time_stats(tmp_path)
        model = usage["time"]["model"]["borders_holes"]
        assert model["rapid_velocity_source"] == CUTTING_VELOCITY_KEY
        assert model["rapid_velocity"] == pytest.approx(TIME_BH_VELOCITY)
        assert usage["time"]["by_operation"]["rapid"] == pytest.approx(
            WK_BH_RAPID_IN / TIME_BH_VELOCITY, abs=1e-6
        )

    def test_a_configured_rapid_velocity_moves_only_the_rapid_term(self, tmp_path: Path) -> None:
        baseline = time_stats(tmp_path)["time"]
        usage = time_stats(tmp_path, rapid_velocity=8.0)["time"]
        assert usage["model"]["borders_holes"]["rapid_velocity_source"] == "rapid_velocity"
        assert usage["by_operation"]["rapid"] == pytest.approx(WK_BH_RAPID_IN / 8.0, abs=1e-6)
        for name in ("cutting", "plunge", "z_up", "dwell"):
            assert usage["by_operation"][name] == pytest.approx(
                baseline["by_operation"][name], abs=1e-6
            )

    def test_every_stroke_pays_one_z_cycle(self, tmp_path: Path) -> None:
        total = time_stats(tmp_path)["time"]["total"]
        assert total["strokes"] == JOB_STROKES
        assert total["plunge"] == pytest.approx(JOB_STROKES * CYCLE_PLUNGE_S)
        assert total["z_up"] == pytest.approx(JOB_STROKES * CYCLE_Z_UP_S)
        assert total["dwell"] == pytest.approx(JOB_STROKES * CYCLE_DWELL_S)

    def test_a_multi_subpath_stroke_pays_one_cycle(self, tmp_path: Path) -> None:
        # One pen-down with two cutting sub-paths is one plunge, one z-up.
        spec = label("a", material="uv")
        content = document(
            section(
                f"PU{u(0.5)},{u(0.5)};PD{u(1.0)},{u(0.5)};PD{u(1.0)},{u(1.0)};",
            )
        )
        usage = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.4, y=0.4)])],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
            job_spec=job_spec(),
            job_config=tool_config(),
        )["time"]
        assert usage["total"]["strokes"] == 1
        assert usage["total"]["total"] == pytest.approx(
            1.0 / TIME_TXT_VELOCITY + TIME_STROKE_CYCLE_S
        )

    def test_the_layers_are_charged_independently(self, tmp_path: Path) -> None:
        # Same inches, different rates: the 4.0 in/sec bh layer costs half the
        # seconds per inch of the 2.0 in/sec txt layer. Proves the dual
        # defaults are split, not blended.
        rows = {row["kind"]: row for row in time_stats(tmp_path)["time"]["by_cutter"]}
        txt = rows["txt"]
        bh = rows["bh"]
        assert txt["by_operation"]["cutting"] / TXT_CUTTING_IN == pytest.approx(
            1.0 / TIME_TXT_VELOCITY
        )
        assert bh["by_operation"]["cutting"] / BH_CUTTING_IN == pytest.approx(
            1.0 / TIME_BH_VELOCITY
        )
        assert bh["files"] == 1
        assert txt["files"] == 2

    def test_a_job_spec_override_beats_the_config_default(self, tmp_path: Path) -> None:
        usage = stats_for(
            **make_job(tmp_path),
            job_spec=job_spec(**{CUTTING_VELOCITY_KEY: 1.0}),
            job_config=tool_config(),
        )["time"]
        assert usage["model"]["text"][CUTTING_VELOCITY_KEY] == pytest.approx(1.0)
        assert usage["model"]["borders_holes"][CUTTING_VELOCITY_KEY] == pytest.approx(1.0)
        assert usage["by_operation"]["cutting"] == pytest.approx(28.0)

    def test_the_configured_clamp_bounds_the_estimate(self, tmp_path: Path) -> None:
        # The registry caps cutting_velocity at 1.0 in/sec, so the 2.0/4.0
        # defaults both clamp to it - and the estimate uses the clamped value
        # the header was emitted with, not the configured one.
        usage = time_stats(tmp_path, tool_options=tool_registry(cutting_max=1.0))["time"]
        assert usage["model"]["text"][CUTTING_VELOCITY_KEY] == pytest.approx(1.0)
        assert usage["model"]["borders_holes"][CUTTING_VELOCITY_KEY] == pytest.approx(1.0)
        assert usage["by_operation"]["cutting"] == pytest.approx(28.0)

    def test_missing_optional_parameters_charge_only_the_travel(self, tmp_path: Path) -> None:
        registry = tool_registry()
        for key in (PLUNGE_VELOCITY_KEY, Z_CLEARANCE_KEY, Z_UP_VELOCITY_KEY, DWELL_TIME_KEY):
            del registry[key]
        usage = time_stats(tmp_path, tool_options=registry)["time"]
        assert usage["available"] is True
        operations = usage["by_operation"]
        # Travel is all that is left: the cutting and rapid terms, no Z cycle.
        assert usage["total"]["total"] == pytest.approx(
            operations["cutting"] + operations["rapid"], abs=1e-6
        )
        for name in ("plunge", "z_up", "dwell"):
            assert operations[name] == pytest.approx(0.0)
        assert usage["model"]["text"]["stroke_z_cycle_seconds"] == pytest.approx(0.0)

    def test_a_zero_length_stroke_still_pays_its_z_cycle(self, tmp_path: Path) -> None:
        # The pen travels nothing, but it still plunges, lifts and dwells: the
        # stroke's Z cycle is charged on the stroke, not on its distance.
        spec = label("a", material="uv")
        content = document(section(f"PU{u(0.5)},{u(0.5)};PD{u(0.5)},{u(0.5)};"))
        usage = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.4, y=0.4)])],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
            job_spec=job_spec(),
            job_config=tool_config(),
        )["time"]
        assert usage["available"] is True
        assert usage["total"]["strokes"] == 1
        assert usage["by_operation"]["cutting"] == pytest.approx(0.0)
        assert usage["total"]["total"] == pytest.approx(TIME_STROKE_CYCLE_S)
        assert usage["total"]["total_hms"] == "0:00:01"
        assert usage["by_cutter"][0]["share"] == 100.0

    def test_an_unattributed_stroke_pays_its_layers_cycle(self, tmp_path: Path) -> None:
        spec = label("a", material="uv")
        inside = f"PU{u(0.5)},{u(0.5)};PD{u(1.0)},{u(0.5)};"
        outside = f"PU{u(9.0)},{u(9.0)};PD{u(10.0)},{u(9.0)};"
        content = document(section(inside, outside))
        usage = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(material="uv", labels=[placement(spec, x=0.4, y=0.4)])],
            combined_by_plate={1: content},
            plt_files=[write_plt(tmp_path, "uv_p01_30_txt_job.plt", content)],
            material_by_plate={1: "uv"},
            job_spec=job_spec(),
            job_config=tool_config(),
        )["time"]
        unattributed = usage["unattributed"]
        assert unattributed["strokes"] == 1
        # The outside stroke owns its own 1.0 in of cutting AND the pen-up
        # travel that reaches it, both at the txt layer's rates.
        outbound_in = 1.0
        inbound_in = math.hypot(9.0 - 1.0, 9.0 - 0.5)
        assert unattributed["by_operation"]["cutting"] == pytest.approx(
            outbound_in / TIME_TXT_VELOCITY, abs=1e-6
        )
        assert unattributed["by_operation"]["rapid"] == pytest.approx(
            inbound_in / TIME_TXT_VELOCITY, abs=1e-6
        )
        assert unattributed["total"] == pytest.approx(
            (outbound_in + inbound_in) / TIME_TXT_VELOCITY + TIME_STROKE_CYCLE_S, abs=1e-6
        )
        assert unattributed["total"] > 0.0

    def test_every_bucket_reconciles_with_the_total(self, tmp_path: Path) -> None:
        """Every breakdown must sum to the same job total.

        This is the invariant that catches double-charging: an accumulator fed
        from two places reads correctly inside its own bucket and inside
        ``total``, so a per-bucket check alone passes - only the cross-bucket
        comparison fails.
        """
        usage = time_stats(tmp_path)
        time_block = usage["time"]
        total = time_block["total"]["total"]
        assert total > 0.0
        assert sum(time_block["by_operation"].values()) == pytest.approx(total)
        assert sum(time_block["by_class"].values()) == pytest.approx(total)
        for key in ("by_cutter", "by_material", "by_plate", "by_label"):
            assert total_of(time_block[key]) == pytest.approx(total, abs=1e-3), (
                f"{key} does not reconcile with the total"
            )
        for row in time_block["by_cutter"]:
            assert sum(row["by_operation"].values()) == pytest.approx(row["total"])

    def test_the_stroke_count_matches_the_measured_geometry(self, tmp_path: Path) -> None:
        usage = time_stats(tmp_path)
        assert usage["time"]["total"]["strokes"] == sum(
            int(row["strokes"]) for row in usage["distance"]["files"]
        )

    def test_the_estimate_survives_the_json_round_trip(self, tmp_path: Path) -> None:
        usage = time_stats(tmp_path)
        assert json.loads(json.dumps(usage)) == usage

    def test_shopping_the_model_from_the_report_is_self_documenting(self, tmp_path: Path) -> None:
        model = time_stats(tmp_path)["time"]["model"]["text"]
        assert model["stroke_z_cycle_seconds"] == pytest.approx(TIME_STROKE_CYCLE_S)
        assert model[CUTTING_VELOCITY_KEY] == pytest.approx(TIME_TXT_VELOCITY)
        assert model["rapid_velocity"] == pytest.approx(TIME_TXT_VELOCITY)
        assert model[PLUNGE_VELOCITY_KEY] == pytest.approx(2.0)
        assert model[Z_CLEARANCE_KEY] == pytest.approx(0.2)
        assert model[Z_UP_VELOCITY_KEY] == pytest.approx(1000.0)
        assert model[DWELL_TIME_KEY] == pytest.approx(50.0)


class TestRunTimeJobRollup:
    """``usage.time.by_spec`` - one label's time summed over its placements."""

    def test_the_fixture_labels_roll_up_one_row_each(self, tmp_path: Path) -> None:
        rows = time_stats(tmp_path)["time"]["by_spec"]
        assert [row["label_id"] for row in rows] == ["round-b", "rect-a"]
        assert [row["copies"] for row in rows] == [1, 1]
        assert [row["plates"] for row in rows] == [[2], [1]]
        assert [row["materials"] for row in rows] == [["wk"], ["uv"]]
        assert [row["placements"] for row in rows] == [["round-b_0"], ["rect-a_0"]]

    def test_the_job_rows_partition_the_placement_time(self, tmp_path: Path) -> None:
        time_block = time_stats(tmp_path)["time"]
        assert total_of(time_block["by_spec"]) == pytest.approx(total_of(time_block["by_label"]))

    def test_a_multi_copy_label_is_one_job_row(self, tmp_path: Path) -> None:
        """Two copies of one spec on one plate: one row, both placements inside."""
        spec = label("dup", width=1.0, height=1.0, material="ACM")
        first = placement(spec, label_id="dup:0", x=0.0, y=0.0, width=1.0, height=1.0)
        second = placement(spec, label_id="dup:1", x=2.0, y=0.0, width=1.0, height=1.0)
        rect = document(
            rect_path(0.0, 0.0, 1.0, 1.0),
            rect_path(2.0, 0.0, 1.0, 1.0),
        )
        usage = stats_for(
            resolved_labels=[spec],
            packed_plates=[plate(plate_id="p1", material="ACM", labels=[first, second])],
            combined_by_plate={
                1: document(
                    section(rect_path(0.0, 0.0, 1.0, 1.0)), section(rect_path(2.0, 0.0, 1.0, 1.0))
                )
            },
            plt_files=[write_plt(tmp_path, "ACM_p01_30_txt_job.plt", rect)],
            material_by_plate={1: "ACM"},
            job_spec=job_spec(),
            job_config=tool_config(),
        )
        rows = usage["time"]["by_spec"]
        assert [row["label_id"] for row in rows] == ["dup"]
        assert rows[0]["copies"] == 2
        assert rows[0]["placements"] == ["dup:0", "dup:1"]
        assert rows[0]["plates"] == [1]
        assert rows[0]["materials"] == ["ACM"]
        assert rows[0]["total"] == pytest.approx(
            sum(float(row["total"]) for row in usage["time"]["by_label"])
        )

    def test_row_shape(self, tmp_path: Path) -> None:
        rows = time_stats(tmp_path)["time"]["by_spec"]
        assert set(rows[0]) == {
            "label_id",
            "plates",
            "materials",
            "placements",
            "copies",
            "by_operation",
            "total",
            "share",
        }
        assert set(rows[0]["by_operation"]) == set(TIME_OPERATIONS)

    def test_the_job_shares_match_the_placement_shares(self, tmp_path: Path) -> None:
        time_block = time_stats(tmp_path)["time"]
        assert sum(float(row["share"]) for row in time_block["by_spec"]) == pytest.approx(
            sum(float(row["share"]) for row in time_block["by_label"]),
            abs=0.2,
        )

    def test_the_rollup_is_empty_when_the_time_cannot_be_estimated(self, tmp_path: Path) -> None:
        assert stats_for(**make_job(tmp_path))["time"]["by_spec"] == []


class TestTimeSummaryLines:
    def test_the_time_section_renders_beside_the_distance(self, tmp_path: Path) -> None:
        text = "\n".join(format_usage_lines(time_stats(tmp_path)))
        assert "  run time (seconds, estimated from the emitted tool_options):" in text
        assert (
            "    total: cutting 9.250 + rapid 0.182 + plunge 0.400 + z_up 4.000 "
            "+ dwell 0.200 = 14.032 s (0:00:14)"
        ) in text
        assert "    strokes: 4 (one Z cycle each)" in text
        assert "    by cutter: 0.060 bh [1 file(s)] 7.232 s (51.5%)" in text

    def test_the_time_section_names_its_exclusions(self, tmp_path: Path) -> None:
        text = "\n".join(format_usage_lines(time_stats(tmp_path)))
        assert "Estimated from tool_options" in text
        assert "excludes tool changes, setup, and loading" in text

    def test_the_plate_lines_add_a_readable_duration(self, tmp_path: Path) -> None:
        # The seconds stay authoritative; the compact duration is the readable
        # companion, inside the same parentheses as the share.
        text = "\n".join(format_usage_lines(time_stats(tmp_path)))
        assert "    by plate: 2 11.382 s (11s; 81.1%)  1 2.650 s (3s; 18.9%)" in text

    def test_the_readable_duration_drops_zero_leading_units(self) -> None:
        assert _fmt_hms(3857.321) == "1h4m17s"
        assert _fmt_hms(1347.340) == "22m27s"
        assert _fmt_hms(350.4) == "5m50s"
        assert _fmt_hms(45.2) == "45s"
        assert _fmt_hms(0.0) == "0s"
        # An interior zero is NOT a leading zero: it keeps its place value.
        assert _fmt_hms(60.0) == "1m0s"
        assert _fmt_hms(3599.0) == "59m59s"
        assert _fmt_hms(3600.0) == "1h0m0s"
        assert _fmt_hms(90061.5) == "25h1m2s"
        assert _fmt_hms(None) == "n/a"

    def test_an_unestimable_job_says_so_instead_of_zero(self, tmp_path: Path) -> None:
        text = "\n".join(format_usage_lines(stats_for(**make_job(tmp_path))))
        assert f"Not estimated: No {CUTTING_VELOCITY_KEY} is configured" in text
        assert "total: cutting 0.000" not in text


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


# ---------------------------------------------------------------------------
# Weight estimate (material_weight_per_square_inch)
#
# Test-owned density, and chosen to be the shop's own arithmetic check: a
# 16 x 24 in sheet at 0.00260417 lb/in2 weighs approximately 1 lb
# (docs/TODO.md). The repo's job-config.json default is covered by
# tests/test_job_config.py's per-field injection test, not pinned here.
# ---------------------------------------------------------------------------

WEIGHT_LB_PER_SQ_IN = 0.00260417
SHEET_WIDTH_IN = 16.0
SHEET_HEIGHT_IN = 24.0
SHEET_SQ_IN = SHEET_WIDTH_IN * SHEET_HEIGHT_IN  # 384.0
MAKES_A_POUND = pytest.approx(1.0, abs=1e-5)


def weight_spec(
    *,
    job_density: Optional[float] = None,
    p1_density: Optional[float] = None,
    p2_density: Optional[float] = None,
) -> Any:
    """A JobSpec declaring :func:`make_job`'s plates ``p1``/``p2`` with densities.

    Args:
        job_density: The job-level ``material_weight_per_square_inch``, or
            ``None`` to leave the key undeclared.
        p1_density: Density declared on plate ``p1``; ``None`` means the plate
            omits the key and inherits the job-level value.
        p2_density: Density declared on plate ``p2`` (same rule).

    Returns:
        The spec.
    """
    from plt_optimizer.generate.schema import ContentEntry, JobSpec, PlateSpec

    def declared(plate_id: str, material: str, density: Optional[float]) -> PlateSpec:
        fields: Dict[str, Any] = {
            "id": plate_id,
            "width": 12.0,
            "height": 8.0,
            "material": material,
        }
        if density is not None:
            fields["material_weight_per_square_inch"] = density
        return PlateSpec(**fields)

    return JobSpec(
        job_name="weighing",
        width=4.0,
        height=2.0,
        content=[ContentEntry(text="SIGN")],
        material_weight_per_square_inch=job_density,
        plates=[declared("p1", "uv", p1_density), declared("p2", "wk", p2_density)],
    )


def weight_job_stats(tmp_path: Path, **spec_kwargs: Any) -> Dict[str, Any]:
    """The ``usage`` block of :func:`make_job` with a density-configured spec.

    Args:
        tmp_path: Directory for the PLT fixtures.
        **spec_kwargs: Forwarded to :func:`weight_spec`.

    Returns:
        The full ``usage`` block.
    """
    return stats_for(**make_job(tmp_path), job_spec=weight_spec(**spec_kwargs))


def single_plate_job(
    tmp_path: Path, *, width: float, height: float, copies: int = 1
) -> Dict[str, Any]:
    """One ``p1`` plate carrying ``copies`` instances of a single spec.

    Args:
        tmp_path: Unused; kept so callers read like the other builders.
        width: The spec's nominal width in inches.
        height: The spec's nominal height in inches.
        copies: How many placements to lay out.

    Returns:
        The ``stats_for`` kwargs.
    """
    del tmp_path  # Part of the builder family's signature, not needed here.
    spec = label("one", material="uv", width=width, height=height)
    plates = [
        plate(
            plate_id="p1",
            material="uv",
            labels=[placement(spec, label_id=f"one_{i}", x=3.0 * i, y=0.0) for i in range(copies)],
        )
    ]
    return {
        "resolved_labels": [spec],
        "packed_plates": plates,
        "material_by_plate": {1: "uv"},
    }


class TestWeightUsage:
    def test_the_job_density_weighs_every_plate(self, tmp_path: Path) -> None:
        weight = weight_job_stats(tmp_path, job_density=WEIGHT_LB_PER_SQ_IN)["weight"]
        assert weight["available"] is True
        assert weight["total"]["label_instances"] == 2
        assert weight["total"]["label_sq_in"] == pytest.approx(26.0)
        assert weight["total"]["lb"] == pytest.approx(26.0 * WEIGHT_LB_PER_SQ_IN, abs=1e-6)
        assert [row["material_weight_per_square_inch"] for row in weight["by_plate"]] == [
            WEIGHT_LB_PER_SQ_IN,
            WEIGHT_LB_PER_SQ_IN,
        ]

    def test_a_sixteen_by_twentyfour_label_weighs_a_pound(self, tmp_path: Path) -> None:
        """The shop's sanity check: one 16x24 sheet's area x its density = 1 lb."""
        usage = stats_for(
            **single_plate_job(tmp_path, width=SHEET_WIDTH_IN, height=SHEET_HEIGHT_IN),
            job_spec=weight_spec(job_density=WEIGHT_LB_PER_SQ_IN),
        )
        weight = usage["weight"]
        assert weight["total"]["label_sq_in"] == pytest.approx(SHEET_SQ_IN)
        assert weight["total"]["lb"] == MAKES_A_POUND
        assert weight["total"]["oz"] == pytest.approx(16.0, abs=1e-4)

    def test_a_plate_density_wins_and_moves_only_that_plate(self, tmp_path: Path) -> None:
        weight = weight_job_stats(tmp_path, job_density=WEIGHT_LB_PER_SQ_IN, p1_density=0.01)[
            "weight"
        ]
        rows = {int(row["plate"]): row for row in weight["by_plate"]}
        # Plate 1 carries the 8 sq in rect spec, plate 2 the 18 sq in round one.
        assert rows[1]["lb"] == pytest.approx(8.0 * 0.01)
        assert rows[2]["lb"] == pytest.approx(18.0 * WEIGHT_LB_PER_SQ_IN, abs=1e-6)
        assert weight["total"]["lb"] == pytest.approx(
            8.0 * 0.01 + 18.0 * WEIGHT_LB_PER_SQ_IN, abs=1e-6
        )

    def test_the_four_grains_reconcile(self, tmp_path: Path) -> None:
        """Plate, material, per-label and total all sum to the same weight."""
        weight = weight_job_stats(tmp_path, job_density=WEIGHT_LB_PER_SQ_IN)["weight"]

        def total_of(key: str) -> float:
            return sum(float(row["lb"]) for row in weight[key])

        assert total_of("by_plate") == pytest.approx(float(weight["total"]["lb"]), abs=1e-5)
        assert total_of("by_material") == pytest.approx(float(weight["total"]["lb"]), abs=1e-5)
        assert total_of("by_label") == pytest.approx(float(weight["total"]["lb"]), abs=1e-5)
        assert sum(int(row["label_instances"]) for row in weight["by_plate"]) == 2
        assert sum(int(row["instances"]) for row in weight["by_label"]) == 2

    def test_by_material_rows_carry_the_plate_materials(self, tmp_path: Path) -> None:
        weight = weight_job_stats(tmp_path, job_density=WEIGHT_LB_PER_SQ_IN)["weight"]
        rows = {str(row["material"]): row for row in weight["by_material"]}
        assert set(rows) == {"uv", "wk"}
        assert rows["uv"]["label_sq_in"] == pytest.approx(8.0)
        assert rows["wk"]["lb"] == pytest.approx(18.0 * WEIGHT_LB_PER_SQ_IN, abs=1e-6)
        # Heaviest first: wk (18 sq in) leads uv (8 sq in).
        assert [row["material"] for row in weight["by_material"]] == ["wk", "uv"]

    def test_count_copies_are_counted_per_instance(self, tmp_path: Path) -> None:
        """Packing unrolls ``count`` into placements; each one weighs."""
        usage = stats_for(
            **single_plate_job(tmp_path, width=2.0, height=2.0, copies=3),
            job_spec=weight_spec(job_density=0.01),
        )
        row = usage["weight"]["by_label"][0]
        assert row["instances"] == 3
        assert row["label_sq_in"] == pytest.approx(12.0)
        assert row["lb"] == pytest.approx(12.0 * 0.01)
        assert usage["weight"]["total"]["lb"] == pytest.approx(row["lb"])

    def test_no_density_anywhere_disables_the_estimate(self, tmp_path: Path) -> None:
        weight = weight_job_stats(tmp_path)["weight"]
        assert weight["available"] is False
        assert weight["total"] is None
        assert weight["by_plate"] == []
        assert weight["by_material"] == []
        assert weight["by_label"] == []
        # The bucket stays a dict (never null), so a reader of "what did not get
        # weighed" never has to branch on ``available``: all of it did not.
        assert weight["unconfigured"] == {"label_instances": 2, "label_sq_in": 26.0}
        assert "material_weight_per_square_inch" in str(weight["note"])

    def test_a_spec_less_call_disables_the_estimate(self, tmp_path: Path) -> None:
        assert stats_for(**make_job(tmp_path))["weight"]["available"] is False

    def test_a_plate_without_a_density_is_not_silently_zero(self, tmp_path: Path) -> None:
        weight = weight_job_stats(tmp_path, p1_density=WEIGHT_LB_PER_SQ_IN)["weight"]
        rows = {int(row["plate"]): row for row in weight["by_plate"]}
        assert weight["available"] is True
        assert rows[1]["lb"] == pytest.approx(8.0 * WEIGHT_LB_PER_SQ_IN, abs=1e-6)
        assert rows[2]["lb"] is None  # Not 0.0: the density is simply unknown.
        assert rows[2]["material_weight_per_square_inch"] is None
        # The total covers the configured plate only, and says so.
        assert weight["total"]["lb"] == pytest.approx(8.0 * WEIGHT_LB_PER_SQ_IN, abs=1e-6)
        assert weight["unconfigured"] == {"label_instances": 1, "label_sq_in": 18.0}
        assert "excluded from the weights" in str(weight["note"])

    def test_a_zero_density_is_a_real_zero_not_missing(self, tmp_path: Path) -> None:
        weight = weight_job_stats(tmp_path, p1_density=0.0, p2_density=0.0)["weight"]
        assert weight["available"] is True
        assert weight["total"]["lb"] == 0.0
        assert weight["total"]["oz"] == 0.0
        assert all(row["lb"] == 0.0 for row in weight["by_plate"])
        assert weight["unconfigured"]["label_instances"] == 0

    def test_the_density_is_echoed_verbatim_on_every_row(self, tmp_path: Path) -> None:
        """Densities are configured numbers, not measurements: never 3-dp rounded."""
        weight = weight_job_stats(tmp_path, job_density=WEIGHT_LB_PER_SQ_IN)["weight"]
        assert all(
            row["material_weight_per_square_inch"] == WEIGHT_LB_PER_SQ_IN
            for row in weight["by_plate"] + weight["by_label"]
        )

    def test_ounces_are_sixteenths_of_the_pounds(self, tmp_path: Path) -> None:
        weight = weight_job_stats(tmp_path, job_density=WEIGHT_LB_PER_SQ_IN)["weight"]
        for row in weight["by_plate"] + weight["by_material"] + weight["by_label"]:
            assert float(row["oz"]) == pytest.approx(float(row["lb"]) * OZ_PER_LB, abs=1e-3)

    def test_a_jobset_member_falls_back_to_its_job_density(self, tmp_path: Path) -> None:
        """A member's packed plate carries the batch's composite id, which never
        matches the member's declared plates - so its job-level density is used.

        A merged sheet is numbered from the batch, so an exact per-plate density is
        not resolvable in a member report; the job-level value is.
        """
        spec = label("one", material="uv", width=4.0, height=2.0)
        usage = stats_for(
            resolved_labels=[spec],
            packed_plates=[
                plate(
                    plate_id="Export_Alpha::p1",
                    material="uv",
                    labels=[placement(spec, label_id="one_0")],
                )
            ],
            material_by_plate={1: "uv"},
            job_spec=weight_spec(job_density=WEIGHT_LB_PER_SQ_IN),
        )
        weight = usage["weight"]
        assert weight["total"]["lb"] == pytest.approx(8.0 * WEIGHT_LB_PER_SQ_IN, abs=1e-6)
        assert weight["by_plate"][0]["material_weight_per_square_inch"] == WEIGHT_LB_PER_SQ_IN

    def test_the_block_is_json_serializable(self, tmp_path: Path) -> None:
        payload = json.loads(json.dumps(weight_job_stats(tmp_path, job_density=0.01)))
        assert payload["weight"]["total"]["lb"] == pytest.approx(26.0 * 0.01)


class TestFmtLb:
    """The pounds-and-ounces renderer of ``summary.txt``."""

    def test_a_pound_and_a_quarter(self) -> None:
        assert _fmt_lb(1.25) == "1 lb 4 oz (1.250 lb)"

    def test_the_ounce_part_drops_trailing_zeros(self) -> None:
        assert _fmt_lb(1.0078125) == "1 lb 0.12 oz (1.008 lb)"

    def test_exactly_one_pound(self) -> None:
        assert _fmt_lb(1.0) == "1 lb 0 oz (1.000 lb)"

    def test_two_pounds(self) -> None:
        assert _fmt_lb(2.0) == "2 lb 0 oz (2.000 lb)"

    @pytest.mark.parametrize(
        ("pounds", "expected"),
        [
            (0.042, "0.67 oz (0.042 lb)"),
            (0.0625, "1 oz (0.062 lb)"),
            (0.999, "15.98 oz (0.999 lb)"),
        ],
    )
    def test_a_sub_pound_weight_drops_the_pound_part(self, pounds: float, expected: str) -> None:
        assert _fmt_lb(pounds) == expected

    def test_zero_is_a_real_zero(self) -> None:
        assert _fmt_lb(0.0) == "0.000 lb"

    def test_none_is_missing_not_zero(self) -> None:
        assert _fmt_lb(None) == "n/a"


class TestWeightSummaryLines:
    @staticmethod
    def _usage(lb: Optional[float], *, instances: int = 1, sq_in: float = 100.0) -> Dict[str, Any]:
        """A usage block carrying one hand-built ``weight`` block."""
        usage = stats_for()
        usage["weight"] = {
            "available": True,
            "note": "",
            "units": "lb",
            "density_units": "lb/in2",
            "basis": "label nominal area (spec width x height, per instance)",
            "total": {
                "label_instances": instances,
                "label_sq_in": sq_in,
                "lb": lb,
                "oz": None if lb is None else lb * OZ_PER_LB,
            },
            "by_material": [
                {
                    "material": "uv",
                    "material_label": "uv",
                    "label_instances": instances,
                    "label_sq_in": sq_in,
                    "lb": lb,
                    "oz": None if lb is None else lb * OZ_PER_LB,
                }
            ],
            "by_plate": [
                {
                    "plate": 1,
                    "plate_id": "plate1",
                    "material": "uv",
                    "material_label": "uv",
                    "material_weight_per_square_inch": WEIGHT_LB_PER_SQ_IN,
                    "label_instances": instances,
                    "label_sq_in": sq_in,
                    "lb": lb,
                    "oz": None if lb is None else lb * OZ_PER_LB,
                }
            ],
            "by_label": [
                {
                    "spec_id": "SPEC-1",
                    "plate": 1,
                    "material": "uv",
                    "material_label": "uv",
                    "material_weight_per_square_inch": WEIGHT_LB_PER_SQ_IN,
                    "instances": instances,
                    "label_sq_in": sq_in,
                    "lb": lb,
                    "oz": None if lb is None else lb * OZ_PER_LB,
                }
            ],
            "unconfigured": {"label_instances": 0, "label_sq_in": 0.0},
        }
        return usage

    def test_a_pound_and_a_quarter_renders_compound(self) -> None:
        text = "\n".join(format_usage_lines(self._usage(1.25)))
        assert "total: 1 lb 4 oz (1.250 lb)" in text
        assert "1 (uv, 0.00260417 lb/in2) 1 lb 4 oz (1.250 lb) x1" in text
        assert "SPEC-1  plate 1  uv  x1  1 lb 4 oz (1.250 lb)" in text

    def test_exactly_one_pound_keeps_the_zero_ounce_part(self) -> None:
        text = "\n".join(format_usage_lines(self._usage(1.0)))
        assert "total: 1 lb 0 oz (1.000 lb)" in text

    def test_a_sub_pound_weight_renders_in_ounces(self) -> None:
        text = "\n".join(format_usage_lines(self._usage(0.042)))
        assert "total: 0.67 oz (0.042 lb)" in text

    def test_a_missing_weight_renders_as_unknown(self) -> None:
        text = "\n".join(format_usage_lines(self._usage(None)))
        assert "total: n/a" in text

    def test_the_not_estimated_case_is_a_single_line(self) -> None:
        usage = stats_for()
        usage["weight"] = {"available": False, "note": "No density configured."}
        text = "\n".join(format_usage_lines(usage))
        assert "Not estimated: No density configured." in text
        assert "by plate" not in text.split("weight (")[1].split("\n\n")[0]

    def test_the_weight_section_sits_between_material_and_distance(self, tmp_path: Path) -> None:
        text = "\n".join(
            format_usage_lines(weight_job_stats(tmp_path, job_density=WEIGHT_LB_PER_SQ_IN))
        )
        assert (
            text.index("  material:")
            < text.index("  weight (estimated")
            < text.index("  tool distance")
        )

    def test_the_unconfigured_bucket_is_listed(self) -> None:
        usage = self._usage(0.25)
        usage["weight"]["unconfigured"] = {"label_instances": 3, "label_sq_in": 42.0}
        text = "\n".join(format_usage_lines(usage))
        assert "unconfigured: 3 label(s)  42.000 sq in" in text
