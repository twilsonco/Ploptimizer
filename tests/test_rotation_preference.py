"""Tests for the ``rotation_preference`` packing knob (U1).

Covers the three layers the knob touches:

* the ranking policy itself (:func:`plt_optimizer.generate.packing.layout._fill_rank_key`
  and the ensemble search in :func:`plt_optimizer.generate.packing.layout._pack_best`),
* the schema surface (job-level field, the cascade onto plates, the
  simple-layout rejection, the shop default in ``job-config.json``),
* the benchmark harness ``--set`` override that lets a sweep flip the knob.

The packing fixtures call ``_pack_best`` / ``_extract_packed_plates`` directly
with the raw entry form the production path builds, ``(width, height,
(rect_id, label, pack_width, sequence))``, so the policy is measured without
rendering typography.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from plt_optimizer.generate.job_config import JobConfigError, load_job_config
from plt_optimizer.generate.packing import PACK_CONFIGS
from plt_optimizer.generate.packing import layout as packing_layout
from plt_optimizer.generate.schema import (
    DEFAULT_ROTATION_PREFERENCE,
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

FILL_AXIS = RotationPreference.FILL_AXIS
MINIMIZE = RotationPreference.MINIMIZE


def _load_benchmark_module() -> Any:
    """Load ``scripts/packing_benchmark.py`` (a script, not an importable module)."""
    spec = importlib.util.spec_from_file_location("packing_benchmark", BENCHMARK_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the script's dataclasses resolve their string
    # annotations through ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    cast(Any, spec.loader).exec_module(module)
    return module


def _rank(
    footprint: float,
    fill_axis: float,
    cross_axis: float,
    rotations: int,
    preference: RotationPreference | None = None,
) -> tuple[float, ...]:
    """Call the ranking key, passing the preference only when one is given."""
    args: list[Any] = [footprint, fill_axis, cross_axis, rotations]
    if preference is not None:
        args.append(preference)
    packed = cast("Any", packing_layout._fill_rank_key)(*args)
    return cast("tuple[float, ...]", packed)


def _row_entries(count: int, width: float = 6.0, height: float = 2.0) -> list[Any]:
    """Build ``count`` raw entries of one size (the production entry shape)."""
    return [(width, height, (str(index), None, width, index)) for index in range(count)]


def _pack(
    entries: list[Any],
    bins: list[tuple[float, float, str]],
    layout: LayoutMode,
    preference: RotationPreference,
) -> tuple[int, int]:
    """Pack with one policy; return ``(plate_count, rotated_count)``.

    Pinned to ``plate_fill=global``: this module pins ``rotation_preference``,
    whose documented semantics are the whole-layout ranking (rotations sit
    between the fill span and the ink box). The shipped default ranks plate
    saturation first, where a fuller plate 1 may legally buy rotations --
    ``tests/test_plate_fill.py`` pins that policy.
    """
    packer = packing_layout._pack_best(
        entries, bins, True, layout, preference, plate_fill=PlateFill.GLOBAL
    )
    plates = packing_layout._extract_packed_plates(packer, transpose=layout == LayoutMode.COLUMNS)
    rotated = sum(1 for plate in plates for label in plate.labels if label.rotated)
    return len(plates), rotated


def _job(**kwargs: object) -> JobSpec:
    """Build a minimal job for the schema-level assertions."""
    return JobSpec(
        name="Rotation Preference Job",
        width=2.0,
        height=1.0,
        labels=[LabelSpec(id="l1", width=2.0, height=1.0, content=[ContentEntry(text="X")])],
        **kwargs,  # type: ignore[arg-type]
    )


class TestFillRankKey:
    """The ranking key is the whole policy; it is tested directly."""

    def test_footprint_leads_in_both_policies(self) -> None:
        """Extra plate material never buys a layout, in either preference."""
        tight_more_rotations = _rank(300.0, 0.9, 0.9, 40, FILL_AXIS)
        loose_fewer_rotations = _rank(320.0, 0.4, 0.4, 0, FILL_AXIS)
        assert tight_more_rotations < loose_fewer_rotations
        assert _rank(300.0, 0.9, 0.9, 40, MINIMIZE) < _rank(320.0, 0.4, 0.4, 0, MINIMIZE)

    def test_fill_axis_prefers_fill_over_rotations(self) -> None:
        """``fill_axis`` ranks the fill-order span above the rotation count."""
        upright_worse_fill = _rank(384.0, 0.40, 0.90, 0, FILL_AXIS)
        rotated_better_fill = _rank(384.0, 0.95, 0.90, 12, FILL_AXIS)
        assert rotated_better_fill < upright_worse_fill

    def test_minimize_prefers_rotations_over_fill(self) -> None:
        """``minimize`` ranks the rotation count above the fill-order span."""
        upright_worse_fill = _rank(384.0, 0.40, 0.90, 0, MINIMIZE)
        rotated_better_fill = _rank(384.0, 0.95, 0.90, 12, MINIMIZE)
        assert upright_worse_fill < rotated_better_fill

    def test_fill_axis_is_the_default(self) -> None:
        """Omitting the preference ranks exactly like ``fill_axis``."""
        assert DEFAULT_ROTATION_PREFERENCE is FILL_AXIS
        assert _rank(384.0, 0.40, 0.90, 0) == _rank(384.0, 0.40, 0.90, 0, FILL_AXIS)
        assert _rank(384.0, 0.95, 0.90, 12) == _rank(384.0, 0.95, 0.90, 12, FILL_AXIS)

    def test_identical_geometry_ranks_exactly(self) -> None:
        """Two geometrically identical layouts tie, so output stays reproducible."""
        assert _rank(384.0, 0.9, 0.9, 3, FILL_AXIS) == _rank(384.0, 0.9, 0.9, 3, FILL_AXIS)
        assert _rank(384.0, 0.9, 0.9, 3, MINIMIZE) == _rank(384.0, 0.9, 0.9, 3, MINIMIZE)


class TestPackBestPolicy:
    """The ensemble search honours the policy at equal plate material."""

    BINS: list[tuple[float, float, str]] = [(10.0, 10.0, "p1")]

    def test_minimize_takes_fewer_rotations_at_equal_material(self) -> None:
        """Four labels: the fill order rotates one, ``minimize`` rotates none."""
        entries = _row_entries(4)
        fill_plates, fill_rotations = _pack(entries, self.BINS, LayoutMode.ROWS, FILL_AXIS)
        min_plates, min_rotations = _pack(entries, self.BINS, LayoutMode.ROWS, MINIMIZE)
        assert fill_rotations == 1
        assert min_rotations == 0
        assert min_plates == fill_plates  # same plate count = same material

    def test_minimize_reduces_rotations_on_a_crowded_plate(self) -> None:
        """Six labels: the fill order rotates two, ``minimize`` rotates one."""
        entries = _row_entries(6)
        fill_plates, fill_rotations = _pack(entries, self.BINS, LayoutMode.ROWS, FILL_AXIS)
        min_plates, min_rotations = _pack(entries, self.BINS, LayoutMode.ROWS, MINIMIZE)
        assert fill_rotations == 2
        assert min_rotations == 1
        assert min_plates == fill_plates

    def test_default_path_is_deterministic(self) -> None:
        """The default path packs identically twice (reproducibility contract)."""
        entries = _row_entries(6)
        assert _pack(entries, self.BINS, LayoutMode.ROWS, FILL_AXIS) == _pack(
            entries, self.BINS, LayoutMode.ROWS, FILL_AXIS
        )

    def test_generate_layout_exposes_the_knob_with_the_library_default(self) -> None:
        """``generate_layout`` accepts the knob and defaults to ``fill_axis``."""
        signature = inspect.signature(packing_layout.generate_layout)
        assert signature.parameters["rotation_preference"].default is FILL_AXIS


class TestSchemaSurface:
    """Schema defaults, the plate cascade, and the simple-layout rejection."""

    def test_job_default_is_fill_axis(self) -> None:
        """A job that declares nothing keeps the historical ranking."""
        assert _job().rotation_preference is FILL_AXIS

    def test_plate_field_defaults_to_none(self) -> None:
        """A bare plate declares no preference (the cascade fills it in)."""
        assert PlateSpec(id="p1", width=24.0, height=16.0).rotation_preference is None

    def test_job_value_cascades_to_plates_omitting_it(self) -> None:
        """Plates without an explicit preference inherit the job-level one."""
        job = _job(
            plates=[PlateSpec(id="p1", width=24.0, height=16.0)],
            rotation_preference=MINIMIZE,
        )
        assert job.rotation_preference is MINIMIZE
        assert job.plates is not None
        assert [plate.rotation_preference for plate in job.plates] == [MINIMIZE]

    def test_explicit_plate_value_wins(self) -> None:
        """A plate that declares its own preference keeps it."""
        job = _job(
            plates=[
                PlateSpec(
                    id="p1",
                    width=24.0,
                    height=16.0,
                    rotation_preference=FILL_AXIS,
                )
            ],
            rotation_preference=MINIMIZE,
        )
        assert job.plates is not None
        assert job.plates[0].rotation_preference is FILL_AXIS

    def test_explicit_fill_axis_is_rejected_on_a_simple_layout(self) -> None:
        """A simple grid never rotates, so an explicit ``fill_axis`` is a lie."""
        with pytest.raises(ValidationError, match="rotation_preference"):
            _job(layout=LayoutMode.COLUMNS_SIMPLE, rotation_preference=FILL_AXIS)

    def test_simple_layout_accepts_minimize(self) -> None:
        """``minimize`` is a legal declaration on a simple layout (it is a no-op)."""
        job = _job(layout=LayoutMode.ROWS_SIMPLE, rotation_preference=MINIMIZE)
        assert job.rotation_preference is MINIMIZE


class TestShopDefaults:
    """``job-config.json`` is the shop's layout source of truth."""

    def test_repo_config_prefers_minimize(self) -> None:
        """The shop default is the fewest-rotations ranking (decision D2)."""
        config = load_job_config(JOB_CONFIG)
        assert config is not None and config.defaults.rotation_preference is MINIMIZE

    @pytest.mark.parametrize(
        "spelling,expected",
        [("fill_axis", FILL_AXIS), ("minimize", MINIMIZE)],
    )
    def test_config_accepts_both_spellings(
        self, tmp_path: Path, spelling: str, expected: RotationPreference
    ) -> None:
        """Both enum spellings load from the shop config."""
        config = tmp_path / "job-config.json"
        config.write_text(json.dumps({"rotation_preference": spelling}), encoding="utf-8")
        loaded = load_job_config(config)
        assert loaded is not None and loaded.defaults.rotation_preference is expected

    def test_config_rejects_an_unknown_spelling(self, tmp_path: Path) -> None:
        """A typo in the shop config fails loudly, it never silently defaults."""
        config = tmp_path / "job-config.json"
        config.write_text(json.dumps({"rotation_preference": "upright_please"}), encoding="utf-8")
        with pytest.raises(JobConfigError, match="rotation_preference"):
            load_job_config(config)

    def test_config_rejects_a_non_string(self, tmp_path: Path) -> None:
        """A boolean is not a preference."""
        config = tmp_path / "job-config.json"
        config.write_text(json.dumps({"rotation_preference": True}), encoding="utf-8")
        with pytest.raises(JobConfigError, match="rotation_preference"):
            load_job_config(config)


class TestEnsemble:
    """The candidate ensemble the policy ranks over."""

    def test_ensemble_size_is_pinned(self) -> None:
        """11 algorithm/sort pairs, each run rotation-off and rotation-on."""
        assert len(PACK_CONFIGS) == 11


class TestBenchmarkHarness:
    """The sweep harness can flip the knob without editing the spec."""

    def test_set_override_injects_the_preference(self) -> None:
        """``--set job.rotation_preference=minimize`` lands under the ``job`` root."""
        module = _load_benchmark_module()
        raw: dict[str, Any] = {"job": {"name": "Probe", "layout": "rows"}}
        overridden = module.apply_overrides(raw, ["job.rotation_preference=minimize"])
        assert overridden["job"]["rotation_preference"] == "minimize"

    def test_prepare_carries_the_override_into_the_spec(self, tmp_path: Path) -> None:
        """A ``--set`` job-level preference beats the shop config in a prepared job."""
        module = _load_benchmark_module()
        spec_path = tmp_path / "probe.yaml"
        spec_path.write_text(
            "job:\n"
            "  name: Rotation Probe\n"
            "  width: 24\n"
            "  height: 16\n"
            "  text_height: 1\n"
            "  font: helv1\n"
            "  labels:\n"
            "    - id: a\n"
            "      content:\n"
            "        - text: AB\n",
            encoding="utf-8",
        )
        # The shop config says 'minimize'; the override asks for the other one.
        unoverridden = module.prepare(
            spec_path,
            (),
            config_path=JOB_CONFIG,
            tools_path=REPO_ROOT / "tools.json",
        )
        assert unoverridden.job is not None
        assert unoverridden.job.rotation_preference is MINIMIZE

        prepared = module.prepare(
            spec_path,
            ["job.rotation_preference=fill_axis"],
            config_path=JOB_CONFIG,
            tools_path=REPO_ROOT / "tools.json",
        )
        assert prepared.job is not None
        assert prepared.job.rotation_preference is FILL_AXIS
