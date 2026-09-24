"""Tests for the JSON job-defaults layer (plt_optimizer.generate.job_config).

This test suite validates:
- Loading / validation of ``job-config.json`` (including graceful handling
  of a missing file and loud failure on malformed content)
- Top-most-layer default injection (job-level cascade fields + plate-level
  dimension/clearance defaults), with YAML values always winning
- The required-when-unconfigured contract (config missing a required field
  AND the spec omitting it => parse error)
- ``parse_yaml`` integration with and without a config
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from plt_optimizer.generate.job_config import (
    REQUIRED_WHEN_UNCONFIGURED,
    JobConfig,
    JobConfigError,
    JobDefaults,
    apply_job_config_defaults,
    assert_required_fields,
    load_job_config,
)
from plt_optimizer.generate.schema import parse_yaml

# A complete config supplying every required field (plus a few optional
# ones), used as the baseline in injection tests.
_FULL_CONFIG: dict[str, Any] = {
    "max_h_compress": 0.5,
    "hole_margin": 0.0625,
    "min_hole_margin": 0.05,
    "hole_text_collision_distance": 0.1,
    "plate_width": 24.0,
    "plate_height": 16.0,
    "left_clearance": 0.0,
    "top_clearance": 0.0,
}


def _write_config(directory: Path, data: dict[str, Any], name: str = "job-config.json") -> Path:
    """Write a job-config JSON file into ``directory``.

    Args:
        directory: Directory to write into.
        data: Config payload to serialize.
        name: File name for the config.

    Returns:
        Path to the written config file.
    """
    config_path = directory / name
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path


def _minimal_job(**overrides: Any) -> dict[str, Any]:
    """Build a raw ``job`` mapping with the minimum valid structure.

    Args:
        overrides: Extra job-level keys merged into the mapping.

    Returns:
        A mutable raw job mapping (labels form, one label with content).
    """
    job: dict[str, Any] = {
        "job_name": "Config Test",
        "labels": [{"id": "l1", "content": [{"text": "Hello"}]}],
    }
    job.update(overrides)
    return job


class TestJobDefaultsModel:
    """Tests for the JobDefaults Pydantic model."""

    def test_all_fields_optional(self) -> None:
        """An empty config validates with every default unset."""
        defaults = JobDefaults()
        assert defaults.max_h_compress is None
        assert defaults.plate_width is None
        assert defaults.hole_diameter is None

    def test_unknown_key_rejected(self) -> None:
        """Typos in a shop config fail loudly instead of being ignored."""
        with pytest.raises(ValidationError):
            JobDefaults(**{"max_h_compress": 0.5, "maxh_compress": 0.5})  # type: ignore

    def test_description_key_allowed(self) -> None:
        """A free-form description (like tools.json) is accepted."""
        assert JobDefaults(**_FULL_CONFIG, description="shop A").description == "shop A"

    def test_range_validators(self) -> None:
        """Numeric fields enforce the same bounds as the job spec."""
        with pytest.raises(ValidationError):
            JobDefaults(max_h_compress=1.5)  # type: ignore
        with pytest.raises(ValidationError):
            JobDefaults(hole_margin=-0.1)  # type: ignore
        with pytest.raises(ValidationError):
            JobDefaults(hole_diameter=0.0)  # type: ignore

    def test_enum_fields_accept_strings(self) -> None:
        """Alignment and layout accept their enum string values."""
        defaults = JobDefaults(text_h_alignment="left", layout="rows")
        assert defaults.text_h_alignment is not None
        assert defaults.text_h_alignment.value == "left"
        assert defaults.layout is not None
        assert defaults.layout.value == "rows"


class TestLoadJobConfig:
    """Tests for load_job_config()."""

    def test_none_path_returns_none(self) -> None:
        """A None path opts out of the config layer entirely."""
        assert load_job_config(None) is None

    def test_missing_file_returns_empty_config(self, tmp_path: Path) -> None:
        """A missing file keeps the layer active with no configured defaults."""
        config = load_job_config(tmp_path / "absent.json")
        assert config is not None
        assert config.defaults == JobDefaults()

    def test_valid_file_loads(self, tmp_path: Path) -> None:
        """A full config round-trips into the model."""
        path = _write_config(tmp_path, _FULL_CONFIG)
        config = load_job_config(path)
        assert config is not None
        assert config.defaults.max_h_compress == 0.5
        assert config.defaults.hole_margin == 0.0625
        assert config.defaults.plate_width == 24.0

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        """Invalid JSON is a hard error (never silently ignored)."""
        path = tmp_path / "job-config.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(JobConfigError, match="Invalid JSON"):
            load_job_config(path)

    def test_non_object_json_raises(self, tmp_path: Path) -> None:
        """A JSON array/document is rejected."""
        path = tmp_path / "job-config.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(JobConfigError, match="JSON object"):
            load_job_config(path)

    def test_invalid_field_raises(self, tmp_path: Path) -> None:
        """Validation failures surface as JobConfigError with detail."""
        path = _write_config(tmp_path, {"max_h_compress": 2.0})
        with pytest.raises(JobConfigError, match="max_h_compress"):
            load_job_config(path)

    def test_unreadable_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError while reading is wrapped in JobConfigError."""
        path = _write_config(tmp_path, _FULL_CONFIG)

        def _boom(self: Path, *args: Any, **kwargs: Any) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _boom)
        with pytest.raises(JobConfigError, match="Could not read"):
            load_job_config(path)


class TestApplyJobConfigDefaults:
    """Tests for top-most-layer default injection."""

    def test_none_config_passthrough(self) -> None:
        """Without a config the mapping is returned unchanged."""
        job = _minimal_job()
        assert apply_job_config_defaults(job, None) is job

    def test_job_layer_fill(self, tmp_path: Path) -> None:
        """Configured cascade fields land as job-level values."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["max_h_compress"] == 0.5
        assert filled["hole_margin"] == 0.0625
        assert filled["min_hole_margin"] == 0.05
        assert filled["hole_text_collision_distance"] == 0.1

    def test_yaml_values_win(self, tmp_path: Path) -> None:
        """Spec-declared values are never overridden by the config."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        job = _minimal_job(hole_margin=0.25, max_h_compress=0.0)
        filled = apply_job_config_defaults(job, config)
        assert filled["hole_margin"] == 0.25
        assert filled["max_h_compress"] == 0.0

    def test_explicit_null_counts_as_unset(self, tmp_path: Path) -> None:
        """A YAML ``null`` is inherited-from-parent semantics, not a value."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(hole_margin=None), config)
        assert filled["hole_margin"] == 0.0625

    def test_input_not_mutated(self, tmp_path: Path) -> None:
        """Injection returns a copy; the caller's mapping stays pristine."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        job = _minimal_job()
        apply_job_config_defaults(job, config)
        assert "hole_margin" not in job

    def test_plate_defaults_fill_entries(self, tmp_path: Path) -> None:
        """Plate width/height fill plate entries that omit them.

        Clearances are no longer plate-entry injections: they land at the
        job layer (see :meth:`TestJobLayerClearanceInjection`) and cascade
        onto plates via ``JobSpec``.
        """
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        job = _minimal_job(plates=[{"id": "p1"}, {"id": "p2", "width": 12.0}])
        filled = apply_job_config_defaults(job, config)
        assert filled["plates"][0] == {
            "id": "p1",
            "width": 24.0,
            "height": 16.0,
        }
        # Explicit plate width survives; the rest still gets filled.
        assert filled["plates"][1]["width"] == 12.0
        assert filled["plates"][1]["height"] == 16.0
        assert "left_clearance" not in filled["plates"][1]
        assert "top_clearance" not in filled["plates"][1]

    def test_plate_defaults_do_not_touch_job_dims(self, tmp_path: Path) -> None:
        """plate_width/height never fill the job-level label width/height."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert "width" not in filled
        assert "height" not in filled

    def test_clearances_inject_at_job_layer(self, tmp_path: Path) -> None:
        """Config clearances land as job-level values, never on plates."""
        config_data = dict(_FULL_CONFIG)
        config_data["left_clearance"] = 0.5
        config_data["top_clearance"] = 0.75
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(plates=[{"id": "p1"}]), config)
        assert filled["left_clearance"] == 0.5
        assert filled["top_clearance"] == 0.75
        assert "left_clearance" not in filled["plates"][0]
        assert "top_clearance" not in filled["plates"][0]

    def test_job_level_clearance_wins_over_config(self, tmp_path: Path) -> None:
        """A job-spec clearance is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["left_clearance"] = 0.5
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(left_clearance=1.25), config)
        assert filled["left_clearance"] == 1.25

    def test_null_job_clearance_filled_by_config(self, tmp_path: Path) -> None:
        """An explicit ``null`` job clearance is unset semantics: config fills."""
        config_data = dict(_FULL_CONFIG)
        config_data["top_clearance"] = 0.75
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(top_clearance=None), config)
        assert filled["top_clearance"] == 0.75

    def test_non_dict_plate_passthrough(self, tmp_path: Path) -> None:
        """Non-mapping plate entries are left untouched (validation errors
        remain the schema's job)."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        job = _minimal_job(plates=["not-a-dict"])
        filled = apply_job_config_defaults(job, config)
        assert filled["plates"] == ["not-a-dict"]

    def test_non_list_plates_passthrough(self, tmp_path: Path) -> None:
        """A non-list ``plates`` value is passed through untouched."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(plates="weird"), config)
        assert filled["plates"] == "weird"

    def test_config_holes_injected(self, tmp_path: Path) -> None:
        """Config holes land at the job layer with the default diameter.

        Group locations (``corners``) are preserved in the raw mapping; the
        in-place expansion into atomic members is the schema validator's job
        at ``JobSpec`` construction time.
        """
        config_data = dict(_FULL_CONFIG)
        config_data["holes"] = [{"location": "corners"}]
        config_data["hole_diameter"] = 0.25
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["holes"] == [{"location": "corners", "diameter": 0.25}]

    def test_config_group_holes_expand_via_parse(self, tmp_path: Path) -> None:
        """A config ``corners`` group expands to four holes after validation."""
        config_data = dict(_FULL_CONFIG)
        config_data["holes"] = [{"location": "corners"}]
        config_data["hole_diameter"] = 0.25
        config_path = _write_config(tmp_path, config_data)
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Config Holes Job\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 12.0\n"
            "  labels:\n"
            "    - id: l1\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      content:\n"
            "        - text: Hi\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path, job_config_path=config_path)
        assert job.holes is not None
        assert {hole.location.value for hole in job.holes} == {
            "top-left",
            "top-right",
            "bottom-left",
            "bottom-right",
        }
        assert all(hole.diameter == 0.25 for hole in job.holes)

    def test_config_holes_respect_explicit_diameter(self, tmp_path: Path) -> None:
        """An explicit hole diameter is preserved over the default."""
        config_data = dict(_FULL_CONFIG)
        config_data["holes"] = [{"location": "left", "diameter": 0.125}]
        config_data["hole_diameter"] = 0.25
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["holes"] == [{"location": "left", "diameter": 0.125}]

    def test_hole_diameter_applied_to_spec_holes(self, tmp_path: Path) -> None:
        """The default diameter also fills spec-declared holes."""
        config_data = dict(_FULL_CONFIG)
        config_data["hole_diameter"] = 0.25
        config = load_job_config(_write_config(tmp_path, config_data))
        job = _minimal_job(holes=[{"location": "left"}, {"location": "right", "diameter": 0.125}])
        filled = apply_job_config_defaults(job, config)
        assert filled["holes"][0]["diameter"] == 0.25
        assert filled["holes"][1]["diameter"] == 0.125

    def test_hole_diameter_noop_without_holes(self, tmp_path: Path) -> None:
        """hole_diameter alone never creates a holes key."""
        config_data = dict(_FULL_CONFIG)
        config_data["hole_diameter"] = 0.25
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert "holes" not in filled

    def test_null_holes_filled_by_config(self, tmp_path: Path) -> None:
        """An explicit ``holes: null`` is unset semantics, so config fills."""
        config_data = dict(_FULL_CONFIG)
        config_data["holes"] = [{"location": "corners"}]
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(holes=None), config)
        assert filled["holes"] == [{"location": "corners"}]

    def test_empty_holes_suppression_preserved(self, tmp_path: Path) -> None:
        """``holes: []`` is a real value (hole suppression), never replaced."""
        config_data = dict(_FULL_CONFIG)
        config_data["holes"] = [{"location": "corners"}]
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(holes=[]), config)
        assert filled["holes"] == []


class TestAssertRequiredFields:
    """Tests for the required-when-unconfigured contract."""

    def test_no_config_skips_enforcement(self) -> None:
        """Without a config the historical all-optional contract holds."""
        assert_required_fields(_minimal_job(), None)

    def test_missing_required_raises(self, tmp_path: Path) -> None:
        """A config without the required fields forces them into the spec."""
        config = load_job_config(_write_config(tmp_path, {"plate_width": 24, "plate_height": 16}))
        with pytest.raises(JobConfigError, match="hole_margin") as excinfo:
            assert_required_fields(_minimal_job(), config)
        message = str(excinfo.value)
        for field_name in REQUIRED_WHEN_UNCONFIGURED:
            assert field_name in message

    def test_full_config_satisfies_contract(self, tmp_path: Path) -> None:
        """A complete config satisfies the contract for a bare spec."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        assert_required_fields(apply_job_config_defaults(_minimal_job(), config), config)

    def test_spec_declaration_satisfies_contract(self, tmp_path: Path) -> None:
        """Job-level spec values satisfy the contract without the config."""
        config = load_job_config(_write_config(tmp_path, {"plate_width": 24, "plate_height": 16}))
        job = _minimal_job(
            max_h_compress=0.5,
            hole_margin=0.1,
            min_hole_margin=0.05,
            hole_text_collision_distance=0.1,
        )
        assert_required_fields(job, config)

    def test_per_label_declaration_satisfies_contract(self, tmp_path: Path) -> None:
        """A field declared on every label also satisfies the contract."""
        config = load_job_config(_write_config(tmp_path, {"plate_width": 24, "plate_height": 16}))
        job = {
            "job_name": "Per label",
            "labels": [
                {
                    "id": "a",
                    "content": [{"text": "A"}],
                    "max_h_compress": 0.5,
                    "hole_margin": 0.1,
                    "min_hole_margin": 0.05,
                    "hole_text_collision_distance": 0.1,
                },
                {
                    "id": "b",
                    "content": [{"text": "B"}],
                    "max_h_compress": 0.0,
                    "hole_margin": 0.2,
                    "min_hole_margin": 0.0,
                    "hole_text_collision_distance": 0.2,
                },
            ],
        }
        assert_required_fields(job, config)

    def test_partial_label_declaration_still_raises(self, tmp_path: Path) -> None:
        """A field declared on only some labels does not satisfy the contract."""
        config = load_job_config(_write_config(tmp_path, {"plate_width": 24, "plate_height": 16}))
        job = {
            "job_name": "Partial",
            "labels": [
                {"id": "a", "content": [{"text": "A"}], "hole_margin": 0.1},
                {"id": "b", "content": [{"text": "B"}]},
            ],
        }
        with pytest.raises(JobConfigError, match="hole_margin"):
            assert_required_fields(job, config)

    def test_unbounded_without_plate_size_raises(self, tmp_path: Path) -> None:
        """No plates AND no configured plate size is a config error."""
        config = load_job_config(_write_config(tmp_path, dict(_FULL_CONFIG, plate_width=None)))
        filled = apply_job_config_defaults(_minimal_job(), config)
        with pytest.raises(JobConfigError, match="plate_width"):
            assert_required_fields(filled, config)

    def test_plates_skip_plate_size_check(self, tmp_path: Path) -> None:
        """Constrained jobs declare plate sizes structurally."""
        config = load_job_config(_write_config(tmp_path, dict(_FULL_CONFIG, plate_width=None)))
        job = _minimal_job(plates=[{"id": "p1", "width": 24.0, "height": 12.0}])
        assert_required_fields(apply_job_config_defaults(job, config), config)

    def test_root_content_form_checked(self, tmp_path: Path) -> None:
        """Root-level content jobs are checked at the job layer too."""
        config = load_job_config(_write_config(tmp_path, {"plate_width": 24, "plate_height": 16}))
        job = {"job_name": "Root", "content": [{"text": "Hello"}]}
        with pytest.raises(JobConfigError):
            assert_required_fields(job, config)


class TestParseYamlWithJobConfig:
    """Integration of parse_yaml() with the job-config layer."""

    def test_repo_config_fills_job_layer(self) -> None:
        """The shipped job-config.json supplies the required defaults."""
        job = parse_yaml("tests_deps/test123_spec.yaml", job_config_path=Path("job-config.json"))
        assert job.hole_margin == 0.125
        assert job.max_h_compress == 0.5
        assert job.min_hole_margin == 0.1
        assert job.hole_text_collision_distance == 0.1

    def test_yaml_overrides_config(self) -> None:
        """Spec-declared values still win over the shipped config."""
        job = parse_yaml("tests_deps/test123_spec.yaml", job_config_path=Path("job-config.json"))
        assert job.margin == 0.1
        assert job.text_height == 0.5

    def test_plate_defaults_applied(self, tmp_path: Path) -> None:
        """Plates omitting dimensions take the config plate size."""
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Plate Default Job\n"
            "  max_h_compress: 0.5\n"
            "  hole_margin: 0.1\n"
            "  min_hole_margin: 0.05\n"
            "  hole_text_collision_distance: 0.1\n"
            "  plates:\n"
            "    - id: p1\n"
            "  labels:\n"
            "    - id: l1\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      content:\n"
            "        - text: Hi\n",
            encoding="utf-8",
        )
        config_path = _write_config(tmp_path, _FULL_CONFIG)
        job = parse_yaml(spec_path, job_config_path=config_path)
        assert job.plates is not None
        assert job.plates[0].width == 24.0
        assert job.plates[0].height == 16.0

    def test_missing_required_aborts(self, tmp_path: Path) -> None:
        """A config missing required fields aborts parsing with a clear error."""
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Bare Job\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 12.0\n"
            "  labels:\n"
            "    - id: l1\n"
            "      content:\n"
            "        - text: Hi\n",
            encoding="utf-8",
        )
        config_path = _write_config(tmp_path, {})
        with pytest.raises(JobConfigError, match="required field"):
            parse_yaml(spec_path, job_config_path=config_path)

    def test_missing_config_file_still_enforces(self, tmp_path: Path) -> None:
        """An opted-in config path that does not exist enforces the contract."""
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Bare Job\n"
            "  labels:\n"
            "    - id: l1\n"
            "      content:\n"
            "        - text: Hi\n",
            encoding="utf-8",
        )
        with pytest.raises(JobConfigError):
            parse_yaml(spec_path, job_config_path=tmp_path / "absent.json")

    def test_no_config_keeps_optional_contract(self) -> None:
        """Without a config path, parsing keeps the historical contract."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        assert job.hole_margin is None
        assert job.max_h_compress is None


class TestJobConfigDataclass:
    """Small structural checks for the JobConfig container."""

    def test_path_provenance(self, tmp_path: Path) -> None:
        """The loaded config remembers where it came from."""
        path = _write_config(tmp_path, _FULL_CONFIG)
        config = load_job_config(path)
        assert isinstance(config, JobConfig)
        assert config.path == path
