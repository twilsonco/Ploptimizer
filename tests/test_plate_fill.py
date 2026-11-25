"""Tests for the ``plate_fill`` plate-ranking knob.

Three layers, mirroring ``rotation_preference``:

* the plan ranking itself
  (:func:`plt_optimizer.generate.packing.layout._plate_fill_rank_key` and the
  plan metrics that feed it),
* the packing search
  (:func:`plt_optimizer.generate.packing.layout._pack_best` ranking and the
  flush-grid candidate that :func:`plt_optimizer.generate.packing.layout._pack_group`
  admits under ``plate_major``),
* the schema / plumbing surface (job field, cascade onto plates, the shop
  default, the jobset pool gate, the export entry point, the benchmark
  ``--set`` override).

The packing fixtures call ``_pack_group`` with the raw entry form the
production path builds, ``(width, height, (rect_id, label, pack_width,
sequence))``, so plans are measured without rendering typography. Expected
plans are the ones measured on a 24x16 sheet, not hand-derived: the point is
that the ranking is deterministic and material-first, not that a hand formula
predicts ``rectpack``.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from plt_optimizer.generate.job_config import JobDefaults, load_job_config
from plt_optimizer.generate.packing import layout as packing_layout
from plt_optimizer.generate.packing.layout import (
    PLATE_FILL_BAND,
    _group_plate_fill,
    _plan_metrics_from_plates,
    _plate_fill_rank_key,
)
from plt_optimizer.generate.schema import (
    DEFAULT_PLATE_FILL,
    ContentEntry,
    JobSpec,
    LabelSpec,
    LayoutMode,
    PlateFill,
    PlateSpec,
    RotationPreference,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
JOB_CONFIG = REPO_ROOT / "job-config.json"
BENCHMARK_SCRIPT = REPO_ROOT / "scripts" / "packing_benchmark.py"

MAJOR = PlateFill.PLATE_MAJOR
GLOBAL = PlateFill.GLOBAL
FILL_AXIS = RotationPreference.FILL_AXIS

SHEET = (24.0, 16.0)


def _bins(count: int = 40) -> list[tuple[float, float, str]]:
    """``count`` auto-allocated 24x16 sheets (unbounded mode)."""
    return [(SHEET[0], SHEET[1], str(index)) for index in range(count)]


def _entries(count: int, width: float, height: float, start: int = 0) -> list[Any]:
    """Build ``count`` raw entries of one size (the production entry shape)."""
    return [
        (width, height, (str(index), None, width, index)) for index in range(start, start + count)
    ]


def _pack(
    entries: list[Any],
    plate_fill: PlateFill,
    layout: LayoutMode = LayoutMode.ROWS,
    rotation_preference: RotationPreference = FILL_AXIS,
) -> list[Any]:
    """Pack one group with one ranking; return the produced plates."""
    return packing_layout._pack_group(
        entries,
        _bins(),
        True,
        layout,
        rotation_preference=rotation_preference,
        plate_fill=plate_fill,
    )


def _counts(plates: list[Any]) -> list[int]:
    """Labels per plate, in plate order."""
    return [len(plate.labels) for plate in plates]


def _sheet_area(plates: list[Any]) -> float:
    """Total sheet area of the non-empty plates (the material the shop buys)."""
    return float(sum(plate.width * plate.height for plate in plates if plate.labels))


def _job(**kwargs: object) -> JobSpec:
    """Build a minimal job for the schema-level assertions."""
    return JobSpec(
        name="Plate Fill Job",
        width=2.0,
        height=1.0,
        labels=[LabelSpec(id="l1", width=2.0, height=1.0, content=[ContentEntry(text="X")])],
        **kwargs,  # type: ignore[arg-type]
    )


def _metrics(
    plates: list[Any],
) -> Any:
    """Measure a plate list as a ranking input."""
    return _plan_metrics_from_plates(plates)


class TestPlateFillRankKey:
    """The plan ranking tuple: material first, then plate-major fill."""

    def _plan(self, fills: tuple[float, ...], **kwargs: Any) -> Any:
        """Build plan metrics from per-plate fill ratios (0..1 of the fill axis)."""
        sheet = kwargs.get("sheet", 384.0 * len(fills))
        bbox = kwargs.get("bbox", 100.0)
        rotations = int(kwargs.get("rotations", 0))
        fill_axis = kwargs.get("fill_axis", min(fills))
        bands = tuple(int(round(fill / PLATE_FILL_BAND)) for fill in fills)
        areas = self._areas(fills, **kwargs)
        return SimpleNamespace(
            sheet_area=sheet,
            fill_bands=bands,
            fill_axis=fill_axis,
            cross_axis=kwargs.get("cross_axis", 0.5),
            area_fills=areas,
            bbox_area=bbox,
            rotations=rotations,
        )

    def _areas(self, fills: tuple[float, ...], **kwargs: Any) -> tuple[float, ...]:
        """Per-plate area coverage of a plan, tied by default with the fill ratios.

        The key compares the area vector between the banded fill vector and the
        whole-layout terms, so a fixture that pins a later term must leave the
        areas equal (they follow the fills) and only override them when the test
        is about area itself.
        """
        override = kwargs.get("area_fills")
        if override is not None:
            return tuple(override)
        return fills

    def test_plate_material_leads_the_key(self) -> None:
        """A saturated first plate never buys an extra sheet."""
        one_sheet = self._plan((1.0,))
        two_sheets = self._plan((1.0, 0.5))
        assert _plate_fill_rank_key(two_sheets, FILL_AXIS) > _plate_fill_rank_key(
            one_sheet, FILL_AXIS
        )

    def test_fill_vector_is_plate_major_not_max_min(self) -> None:
        """At equal material the fullest first plate wins, even with a thin tail."""
        front = self._plan((1.0, 0.02), sheet=768.0)
        spread = self._plan((0.5, 0.5), sheet=768.0)
        assert _plate_fill_rank_key(front, FILL_AXIS) < _plate_fill_rank_key(spread, FILL_AXIS)
        # The historical max-min ranking prefers the opposite plan: the two
        # policies really do disagree, this is not a relabeled same ranking.
        assert packing_layout._fill_rank_key(768.0, 0.02, 0.5, 0, FILL_AXIS) > (
            packing_layout._fill_rank_key(768.0, 0.5, 0.5, 0, FILL_AXIS)
        )

    def test_fill_axis_term_survives_the_plate_vector(self) -> None:
        """Equal plate fills: the plan that fills the layout's own axis wins.

        The fill vector is measured **along** the fill axis and the aggregate
        fill-axis ratio stays behind it in the key. Drop that term and a plate
        filled sideways -- identical area, layout order ignored -- outranks the
        plan the declared ``layout`` asks for, which is the fill-order contract
        (``rows`` fills width, ``columns`` fills height).
        """
        along = self._plan((0.5, 0.5), sheet=768.0, fill_axis=0.9, cross_axis=0.2)
        sideways = self._plan((0.5, 0.5), sheet=768.0, fill_axis=0.2, cross_axis=0.9)
        assert _plate_fill_rank_key(along, FILL_AXIS) < _plate_fill_rank_key(sideways, FILL_AXIS)
        assert _plate_fill_rank_key(along, RotationPreference.MINIMIZE) < _plate_fill_rank_key(
            sideways, RotationPreference.MINIMIZE
        )

    def test_band_absorbs_sub_band_differences(self) -> None:
        """Fills inside one band tie, so near-equal plans keep the incumbent.

        Areas are pinned equal: the area vector sits right behind the banded
        fill vector, so a fixture about band absorption must not differ in how
        much label area each plate carries.
        """
        a = self._plan((0.5, 0.1), sheet=768.0, area_fills=(0.5, 0.1))
        b = self._plan((0.5 + PLATE_FILL_BAND / 4, 0.1), sheet=768.0, area_fills=(0.5, 0.1))
        assert _plate_fill_rank_key(a, FILL_AXIS) == _plate_fill_rank_key(b, FILL_AXIS)

    def test_full_sheet_beats_the_tighter_ink_box(self) -> None:
        """A plate covered to the last square inch wins over a shorter ink box.

        The shop's complaint: ``rectpack`` ships 121 of 128 3x1 labels on a
        24x16 sheet (98.958 % of the area) where the flush grid fits all 128, and
        the grid's ink box is a few square inches larger because the sheet is
        genuinely full. Saturation is the point; ink tightness is the tiebreak.
        """
        full = self._plan((1.0, 0.3), sheet=768.0, bbox=520.0, area_fills=(1.0, 0.3))
        almost = self._plan((1.0, 0.3), sheet=768.0, bbox=500.0, area_fills=(0.98958, 0.3))
        assert _plate_fill_rank_key(full, FILL_AXIS) < _plate_fill_rank_key(almost, FILL_AXIS)

    def test_bbox_breaks_a_fill_tie(self) -> None:
        """Equal material, fill and fill order: the tighter ink box wins."""
        tight = self._plan((0.5, 0.1), sheet=768.0, bbox=200.0)
        loose = self._plan((0.5, 0.1), sheet=768.0, bbox=300.0)
        assert _plate_fill_rank_key(tight, FILL_AXIS) < _plate_fill_rank_key(loose, FILL_AXIS)

    def test_minimize_promotes_the_rotation_count(self) -> None:
        """``minimize`` ranks rotations above fill; ``fill_axis`` below the box."""
        upright = self._plan((0.5, 0.1), sheet=768.0, bbox=300.0, rotations=0)
        rotated = self._plan((0.6, 0.1), sheet=768.0, bbox=200.0, rotations=6)
        assert _plate_fill_rank_key(upright, RotationPreference.MINIMIZE) < _plate_fill_rank_key(
            rotated, RotationPreference.MINIMIZE
        )
        assert _plate_fill_rank_key(upright, FILL_AXIS) > _plate_fill_rank_key(rotated, FILL_AXIS)

    def test_plan_metrics_from_plates_matches_the_packer_measurement(self) -> None:
        """Grid plans and ``rectpack`` plans are measured on identical terms."""
        plates = _pack(_entries(128, 3.0, 1.0), MAJOR)
        metrics = _metrics(plates)
        assert metrics.sheet_area == pytest.approx(384.0)
        assert metrics.rotations == 0
        # One plate filled end to end along the row axis: ratio 1.0 -> full band.
        assert metrics.fill_axis == pytest.approx(1.0)
        assert metrics.fill_bands == (int(round(1.0 / PLATE_FILL_BAND)),)


class TestPackGroupPlateFill:
    """The search: what each ranking hands back for real rect sets."""

    def test_uniform_run_fills_one_sheet_completely(self) -> None:
        """128 3x1 tags on a 24x16 sheet: one plate, 100 %, nothing rotated."""
        plates = _pack(_entries(128, 3.0, 1.0), MAJOR)
        assert _counts(plates) == [128]
        assert _sheet_area(plates) == pytest.approx(384.0)
        assert all(not label.rotated for label in plates[0].labels)

    def test_plate_major_admits_the_flush_grid_plan(self) -> None:
        """Content order that tiles wins: 3 sheets, not the 4 ``rectpack`` spends.

        128 3x1 tags followed by six 10x10 labels: the plate-major ranking takes
        the plan whose front sheets are saturated -- three sheets, each ~full --
        while the historical whole-layout ranking spends a fourth sheet to keep
        plate 1 at exactly 100 % and nothing rotated. Material leads the key, so
        the greedy plan wins even though it rotates 30 labels; the shop's
        ``rotation_preference: minimize`` cannot outweigh a whole sheet.
        """
        entries = _entries(128, 3.0, 1.0) + _entries(6, 10.0, 10.0, 500)
        major = _pack(entries, MAJOR)
        glob = _pack(entries, GLOBAL)
        assert sum(_counts(major)) == 134 and sum(_counts(glob)) == 134
        assert _counts(major) == [63, 63, 8]
        assert _counts(glob) == [128, 2, 2, 2]
        assert _sheet_area(major) == pytest.approx(1152.0)
        assert _sheet_area(glob) == pytest.approx(1536.0)
        front = [
            sum(label.width * label.height for label in plate.labels) / (plate.width * plate.height)
            for plate in major[:2]
        ]
        assert all(coverage > 0.99 for coverage in front)

    def test_grid_candidate_is_dropped_when_it_cannot_place_everything(self) -> None:
        """An incomplete grid plan never wins: no label is ever lost."""
        # Sizes the flush grid cannot tile: a 5x13 label on a 24x16 sheet
        # wastes a whole cell band, so the grid leaves labels unpacked.
        entries = _entries(40, 5.0, 13.0) + _entries(30, 7.0, 3.0, 500)
        plates = _pack(entries, MAJOR)
        assert sum(_counts(plates)) == 70
        assert _sheet_area(plates) <= _sheet_area(_pack(entries, GLOBAL))

    def test_grid_prefix_never_uses_more_material_than_rectpack(self) -> None:
        """A grid plan that needs an extra sheet loses on the leading term.

        A flush grid saturates a sheet only when its pitch divides the plate with
        no remainder (the case above). When it leaves a partial band — 7 columns
        of a 3.25 pitch across a 24-wide plate — a `rectpack` plate carrying more
        of the run wins the fill vector, and material keeps the search honest:
        two plates beat three at any fill.
        """
        entries = _entries(84, 3.0, 1.0) + _entries(3, 12.5, 8.125, 900)
        major = _pack(entries, MAJOR)
        glob = _pack(entries, GLOBAL)
        assert sum(_counts(major)) == sum(_counts(glob)) == 87
        assert _sheet_area(major) <= _sheet_area(glob)

    def test_ranking_is_deterministic(self) -> None:
        """Same input, same plate map (the shop re-engraves identical files)."""
        entries = _entries(128, 3.0, 1.0) + _entries(6, 10.0, 10.0, 500)
        first = _pack(entries, MAJOR)
        second = _pack(entries, MAJOR)
        assert _counts(first) == _counts(second)
        assert [
            (plate.plate_id, [(label.label_id, label.x, label.y) for label in plate.labels])
            for plate in first
        ] == [
            (plate.plate_id, [(label.label_id, label.x, label.y) for label in plate.labels])
            for plate in second
        ]

    def test_global_keeps_the_historical_best_of_plan(self) -> None:
        """``global`` reproduces the pre-knob search exactly (seed stays out)."""
        entries = _entries(128, 3.0, 1.0) + _entries(6, 10.0, 10.0, 500)
        signature = inspect.signature(packing_layout._pack_group)
        assert signature.parameters["plate_fill"].default is MAJOR
        glob = _pack(entries, GLOBAL)
        assert _counts(glob) == [128, 2, 2, 2]

    def test_knob_is_inert_on_the_simple_grid_layouts(self) -> None:
        """``columns-simple`` already grids; the knob cannot change its plan."""
        entries = _entries(60, 3.0, 1.0)
        major = packing_layout._pack_group(
            entries, _bins(), True, LayoutMode.COLUMNS_SIMPLE, plate_fill=MAJOR
        )
        glob = packing_layout._pack_group(
            entries, _bins(), True, LayoutMode.COLUMNS_SIMPLE, plate_fill=GLOBAL
        )
        assert _counts(major) == _counts(glob)


class TestSchemaSurface:
    """The YAML surface: field, values, cascade, per-plate override."""

    def test_job_default_is_plate_major(self) -> None:
        assert _job().plate_fill is DEFAULT_PLATE_FILL is MAJOR

    def test_yaml_accepts_both_spellings(self) -> None:
        assert _job(plate_fill="global").plate_fill is GLOBAL
        assert _job(plate_fill="plate_major").plate_fill is MAJOR

    def test_yaml_rejects_an_unknown_policy(self) -> None:
        with pytest.raises(ValidationError):
            _job(plate_fill="greedy")

    def test_job_value_cascades_onto_plates(self) -> None:
        spec = _job(
            plate_fill="global",
            plates=[PlateSpec(id="p1", width=24.0, height=16.0)],
        )
        assert spec.plates is not None
        assert spec.plates[0].plate_fill is GLOBAL

    def test_explicit_plate_value_beats_the_job(self) -> None:
        spec = _job(
            plate_fill="global",
            plates=[
                PlateSpec(id="p1", width=24.0, height=16.0),
                PlateSpec(id="p2", width=24.0, height=16.0, plate_fill=PlateFill.PLATE_MAJOR),
            ],
        )
        assert spec.plates is not None
        assert [plate.plate_fill for plate in spec.plates] == [GLOBAL, MAJOR]

    def test_plate_field_defaults_to_none_for_the_cascade(self) -> None:
        assert PlateSpec(id="p1", width=24.0, height=16.0).plate_fill is None

    def test_export_entry_point_takes_the_knob(self) -> None:
        from plt_optimizer.generate.vectorize import export_per_cutter_plts

        parameter = inspect.signature(export_per_cutter_plts).parameters["plate_fill"]
        assert parameter.default is None  # None = read it from the JobSpec

    def test_group_plate_fill_needs_a_unanimous_pool(self) -> None:
        def plate(value: PlateFill | None) -> PlateSpec:
            return PlateSpec(id="p", width=24.0, height=16.0, plate_fill=value)

        assert _group_plate_fill([plate(GLOBAL), plate(GLOBAL)], MAJOR) is GLOBAL
        assert _group_plate_fill([plate(GLOBAL), plate(MAJOR)], MAJOR) is MAJOR
        assert _group_plate_fill([], GLOBAL) is GLOBAL


class TestShopDefaults:
    """``job-config.json`` stays the shop's source of truth (never pinned)."""

    def test_config_layer_declares_the_field(self) -> None:
        assert "plate_fill" in JobDefaults.model_fields

    def test_absent_config_value_leaves_the_schema_default_in_charge(self) -> None:
        assert JobDefaults.model_validate({}).plate_fill is None

    def test_config_layer_accepts_a_shop_pin(self) -> None:
        assert JobDefaults.model_validate({"plate_fill": "global"}).plate_fill is GLOBAL

    def test_config_layer_rejects_an_unknown_policy(self) -> None:
        with pytest.raises(ValidationError):
            JobDefaults.model_validate({"plate_fill": "squish"})

    def test_the_shop_file_loads(self) -> None:
        """Whatever the shop declares must stay valid (values are never pinned)."""
        config = load_job_config(JOB_CONFIG)
        assert config is not None


class TestJobsetPoolGate:
    """A jobset pools one layout plan, so the pool must agree."""

    def _context(self, display_name: str, plate_fill: PlateFill) -> Any:
        return SimpleNamespace(
            record=SimpleNamespace(display_name=display_name),
            spec=SimpleNamespace(
                rotation_preference=RotationPreference.FILL_AXIS, plate_fill=plate_fill
            ),
            allow_rotation=True,
            default_plate_clearance=None,
        )

    def test_agreement_is_silent(self) -> None:
        from plt_optimizer.generate.jobset import _gate_pool_setting_warnings

        contexts = [self._context("a", MAJOR), self._context("b", MAJOR)]
        assert _gate_pool_setting_warnings(contexts) == []

    def test_disagreement_names_the_winning_member(self) -> None:
        from plt_optimizer.generate.jobset import _gate_pool_setting_warnings

        warnings = _gate_pool_setting_warnings(
            [self._context("a", MAJOR), self._context("b", GLOBAL)]
        )
        assert len(warnings) == 1
        assert "plate_fill differs across members" in warnings[0]
        assert "b=global" in warnings[0]
        assert "'a' (plate_major) wins for the whole pool" in warnings[0]


class TestBenchmarkHarness:
    """The U0 harness can sweep the knob (``--set plate_fill=global``)."""

    def _module(self) -> Any:
        spec = importlib.util.spec_from_file_location("packing_benchmark", BENCHMARK_SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        cast(Any, spec.loader).exec_module(module)
        return module

    def test_set_override_reaches_the_raw_yaml(self) -> None:
        module = self._module()
        raw = {"name": "j", "width": 2.0, "height": 1.0, "labels": []}
        assert module.apply_overrides(raw, ["plate_fill=global"])["plate_fill"] == "global"

    def test_set_override_rejects_an_unknown_value(self) -> None:
        module = self._module()
        raw = module.apply_overrides(
            {"name": "j", "width": 2.0, "height": 1.0, "labels": []}, ["plate_fill=squish"]
        )
        with pytest.raises(ValidationError):
            JobSpec.model_validate(
                {
                    "name": "j",
                    "width": 2.0,
                    "height": 1.0,
                    "labels": [
                        {"id": "l1", "width": 2.0, "height": 1.0, "content": [{"text": "X"}]}
                    ],
                    **raw,
                }
            )


def test_pack_best_documented_signature_is_stable() -> None:
    """The knob reaches the search entry point (guards the plumbing)."""
    parameters = inspect.signature(packing_layout._pack_best).parameters
    assert parameters["plate_fill"].default is MAJOR
    doc = packing_layout._pack_best.__doc__ or ""
    assert "plate_fill" in doc and "plate-major" in doc
