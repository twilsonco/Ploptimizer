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
import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from plt_optimizer.generate.job_config import (
    REQUIRED_WHEN_UNCONFIGURED,
    JobConfig,
    JobConfigError,
    JobDefaults,
    _spec_declares_margin,
    apply_job_config_defaults,
    assert_required_fields,
    load_job_config,
)
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml

# A complete config supplying every required field (plus a few optional
# ones), used as the baseline in injection tests.
_FULL_CONFIG: dict[str, Any] = {
    "max_h_compress": 0.5,
    "hole_margin": 0.0625,
    "min_hole_margin": 0.05,
    "hole_text_collision_distance": 0.1,
    "space_width_fraction": 0.35,
    "min_glyph_width_fraction": 0.02,
    "kerning_window_fraction": 0.15,
    "kerning_penetration_scale": 1.5,
    "kerning_recession_scale": 0.0,
    "kerning_min_gap_fraction": 0.05,
    "fallback_advance_fraction": 0.9,
    "sc_scale": 0.45,
    "supersubscript_downscale": 0.6,
    "html_brackets": "\u00ab\u00bb",
    "italic_angle": 20.0,
    "underline_offset_fraction": 0.08,
    "underline_break_clearance_fraction": 0.12,
    "underline_break_spaces": True,
    "strikethrough_offset_fraction": 0.22,
    "strikethrough_break_spaces": True,
    "v_center_chars": "-:",
    "target_boldness_factor": 0.18,
    "multistroke_overlap_ratio": 0.35,
    "bold_boldness_multiplier": 2.0,
    "rounded_rectangle_fraction": 0.35,
    "rounded_rectangle_proportional": True,
    "use_baseline_spacing": False,
    "v_center_by_content": True,
    "enforce_cap_line": False,
    "auto_descending_character_adjustment": False,
    "descending_character_v_scale": 0.3,
    "descending_character_raise": 0.25,
    "descending_character_clearance": 0.05,
    "descending_character_reciprocal_scale": 0.6,
    "hole_adjust_for_cutter": False,
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

    def test_text_color_is_not_a_config_field(self) -> None:
        """text_color never cascades, so it must not be a shop default."""
        with pytest.raises(ValidationError):
            JobDefaults(**{"text_color": "magenta"})  # type: ignore

    def test_material_is_not_a_config_field(self) -> None:
        """material is a per-job decision, not a shop-level default."""
        with pytest.raises(ValidationError):
            JobDefaults(**{"material": "wb"})  # type: ignore

    def test_cutter_size_is_not_a_config_field(self) -> None:
        """cutter_size is a per-job/per-line choice, not a shop default."""
        with pytest.raises(ValidationError):
            JobDefaults(**{"cutter_size": 0.03})  # type: ignore

    def test_new_line_is_not_a_config_field(self) -> None:
        """new_line is a per-line hard break, never a shop default."""
        with pytest.raises(ValidationError):
            JobDefaults(**{"new_line": True})  # type: ignore

    def test_cutter_downsize_config_fields(self) -> None:
        """cutter_downsize/max_cutter_downsizes are legal shop defaults."""
        defaults = JobDefaults(cutter_downsize=False, max_cutter_downsizes=2)
        assert defaults.cutter_downsize is False
        assert defaults.max_cutter_downsizes == 2
        assert JobDefaults().cutter_downsize is None
        assert JobDefaults().max_cutter_downsizes is None

    def test_max_cutter_downsizes_negative_rejected(self) -> None:
        """The downsizing budget cannot be negative."""
        with pytest.raises(ValidationError):
            JobDefaults(max_cutter_downsizes=-1)  # type: ignore

    def test_cutter_downsize_global_config_field(self) -> None:
        """cutter_downsize_global is a legal shop default (opt-out configurable)."""
        assert JobDefaults(cutter_downsize_global=False).cutter_downsize_global is False
        assert JobDefaults(cutter_downsize_global=True).cutter_downsize_global is True
        assert JobDefaults().cutter_downsize_global is None

    def test_h_compress_global_config_field(self) -> None:
        """h_compress_global is a legal shop default (opt-in configurable)."""
        assert JobDefaults(h_compress_global=True).h_compress_global is True
        assert JobDefaults(h_compress_global=False).h_compress_global is False
        assert JobDefaults().h_compress_global is None

    def test_optimize_line_content_config_field(self) -> None:
        """optimize_line_content is a legal shop default (opt-in configurable)."""
        assert JobDefaults(optimize_line_content=True).optimize_line_content is True
        assert JobDefaults(optimize_line_content=False).optimize_line_content is False
        assert JobDefaults().optimize_line_content is None

    def test_enforce_cap_line_config_field(self) -> None:
        """enforce_cap_line is a legal shop default (opt-out configurable)."""
        assert JobDefaults(enforce_cap_line=True).enforce_cap_line is True
        assert JobDefaults(enforce_cap_line=False).enforce_cap_line is False
        assert JobDefaults().enforce_cap_line is None

    def test_optimize_line_content_max_lines_config_field(self) -> None:
        """optimize_line_content_max_lines is a legal shop default (int, ge=1)."""
        defaults = JobDefaults(optimize_line_content_max_lines=3)
        assert defaults.optimize_line_content_max_lines == 3
        assert JobDefaults().optimize_line_content_max_lines is None
        assert (
            JobDefaults(optimize_line_content_max_lines=None).optimize_line_content_max_lines
            is None
        )
        with pytest.raises(ValidationError):
            JobDefaults(optimize_line_content_max_lines=0)  # type: ignore[arg-type]

    def test_description_key_allowed(self) -> None:
        """A free-form description (like tools.json) is accepted."""
        assert JobDefaults(**_FULL_CONFIG, description="shop A").description == "shop A"

    def test_descending_character_config_fields(self) -> None:
        """The relief fields are legal shop defaults with schema-mirrored bounds."""
        assert (
            JobDefaults(
                auto_descending_character_adjustment=True
            ).auto_descending_character_adjustment
            is True
        )
        assert (
            JobDefaults(
                auto_descending_character_adjustment=False
            ).auto_descending_character_adjustment
            is False
        )
        assert JobDefaults().auto_descending_character_adjustment is None
        assert JobDefaults(descending_character_v_scale=0.2).descending_character_v_scale == 0.2
        assert JobDefaults(descending_character_raise=1.0).descending_character_raise == 1.0
        assert JobDefaults(descending_character_clearance=0.0).descending_character_clearance == 0.0
        assert (
            JobDefaults(
                descending_character_reciprocal_scale=0.2
            ).descending_character_reciprocal_scale
            == 0.2
        )
        assert (
            JobDefaults(
                descending_character_reciprocal_scale=1.0
            ).descending_character_reciprocal_scale
            == 1.0
        )
        assert JobDefaults().descending_character_v_scale is None
        assert JobDefaults().descending_character_raise is None
        assert JobDefaults().descending_character_clearance is None
        assert JobDefaults().descending_character_reciprocal_scale is None

    def test_descending_character_range_validators(self) -> None:
        """The relief floats enforce the same bounds as the job spec."""
        with pytest.raises(ValidationError):
            JobDefaults(descending_character_v_scale=0.1)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            JobDefaults(descending_character_v_scale=1.1)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            JobDefaults(descending_character_raise=-0.1)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            JobDefaults(descending_character_raise=1.5)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            JobDefaults(descending_character_clearance=-0.01)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            JobDefaults(descending_character_reciprocal_scale=0.1)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            JobDefaults(descending_character_reciprocal_scale=1.1)  # type: ignore[arg-type]

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

    def test_font_field_optional(self) -> None:
        """font defaults to None and accepts a free-form name."""
        assert JobDefaults().font is None
        assert JobDefaults(font="dino").font == "dino"

    def test_font_not_required_when_unconfigured(self) -> None:
        """font has a schema-level fallback, so it is never required."""
        assert "font" not in REQUIRED_WHEN_UNCONFIGURED

    def test_plt_typesetting_fields_not_required(self) -> None:
        """space_width_fraction/min_glyph_width_fraction keep renderer-level fallbacks."""
        assert "space_width_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "min_glyph_width_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "kerning_window_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "kerning_penetration_scale" not in REQUIRED_WHEN_UNCONFIGURED
        assert "kerning_recession_scale" not in REQUIRED_WHEN_UNCONFIGURED
        assert "kerning_min_gap_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "fallback_advance_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "sc_scale" not in REQUIRED_WHEN_UNCONFIGURED
        assert "supersubscript_downscale" not in REQUIRED_WHEN_UNCONFIGURED
        assert "html_brackets" not in REQUIRED_WHEN_UNCONFIGURED
        assert "italic_angle" not in REQUIRED_WHEN_UNCONFIGURED
        assert "cutter_size" not in REQUIRED_WHEN_UNCONFIGURED
        assert "cutter_downsize" not in REQUIRED_WHEN_UNCONFIGURED
        assert "max_cutter_downsizes" not in REQUIRED_WHEN_UNCONFIGURED
        assert "cutter_downsize_global" not in REQUIRED_WHEN_UNCONFIGURED
        assert "h_compress_global" not in REQUIRED_WHEN_UNCONFIGURED
        assert "optimize_line_content" not in REQUIRED_WHEN_UNCONFIGURED
        assert "optimize_line_content_max_lines" not in REQUIRED_WHEN_UNCONFIGURED
        assert "enforce_cap_line" not in REQUIRED_WHEN_UNCONFIGURED
        assert "use_baseline_spacing" not in REQUIRED_WHEN_UNCONFIGURED
        assert "rounded_rectangle_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "rounded_rectangle_proportional" not in REQUIRED_WHEN_UNCONFIGURED
        assert "v_center_by_content" not in REQUIRED_WHEN_UNCONFIGURED
        assert "auto_descending_character_adjustment" not in REQUIRED_WHEN_UNCONFIGURED
        assert "descending_character_v_scale" not in REQUIRED_WHEN_UNCONFIGURED
        assert "descending_character_raise" not in REQUIRED_WHEN_UNCONFIGURED
        assert "hole_adjust_for_cutter" not in REQUIRED_WHEN_UNCONFIGURED
        assert "descending_character_clearance" not in REQUIRED_WHEN_UNCONFIGURED
        assert "descending_character_reciprocal_scale" not in REQUIRED_WHEN_UNCONFIGURED
        assert "underline_offset_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "underline_break_clearance_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "underline_break_spaces" not in REQUIRED_WHEN_UNCONFIGURED
        assert "strikethrough_offset_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert "strikethrough_break_spaces" not in REQUIRED_WHEN_UNCONFIGURED
        assert "v_center_chars" not in REQUIRED_WHEN_UNCONFIGURED
        assert JobDefaults().space_width_fraction is None
        assert JobDefaults().min_glyph_width_fraction is None
        assert JobDefaults().kerning_window_fraction is None
        assert JobDefaults().kerning_penetration_scale is None
        assert JobDefaults().kerning_recession_scale is None
        assert JobDefaults().kerning_min_gap_fraction is None
        assert JobDefaults().fallback_advance_fraction is None
        assert JobDefaults().sc_scale is None
        assert JobDefaults(sc_scale=0.4).sc_scale == pytest.approx(0.4)
        with pytest.raises(Exception):
            JobDefaults(sc_scale=0.1)
        with pytest.raises(Exception):
            JobDefaults(sc_scale=1.1)
        assert JobDefaults().supersubscript_downscale is None
        assert JobDefaults(supersubscript_downscale=0.6).supersubscript_downscale == pytest.approx(
            0.6
        )
        with pytest.raises(Exception):
            JobDefaults(supersubscript_downscale=0.1)
        with pytest.raises(Exception):
            JobDefaults(supersubscript_downscale=1.1)
        assert JobDefaults().html_brackets is None
        assert JobDefaults(html_brackets="<<>>").html_brackets == "<<>>"
        assert JobDefaults().italic_angle is None
        assert JobDefaults(italic_angle=20.0).italic_angle == pytest.approx(20.0)
        with pytest.raises(Exception):
            JobDefaults(italic_angle=-1.0)
        with pytest.raises(Exception):
            JobDefaults(italic_angle=46.0)
        assert JobDefaults().underline_offset_fraction is None
        assert JobDefaults(
            underline_offset_fraction=0.08
        ).underline_offset_fraction == pytest.approx(0.08)
        assert JobDefaults(underline_offset_fraction=0.0).underline_offset_fraction == 0.0
        with pytest.raises(Exception):
            JobDefaults(underline_offset_fraction=-0.1)
        assert JobDefaults().underline_break_clearance_fraction is None
        assert JobDefaults(
            underline_break_clearance_fraction=0.12
        ).underline_break_clearance_fraction == pytest.approx(0.12)
        assert (
            JobDefaults(underline_break_clearance_fraction=0.0).underline_break_clearance_fraction
            == 0.0
        )
        with pytest.raises(Exception):
            JobDefaults(underline_break_clearance_fraction=-0.1)
        assert JobDefaults().underline_break_spaces is None
        assert JobDefaults(underline_break_spaces=True).underline_break_spaces is True
        assert JobDefaults(underline_break_spaces=False).underline_break_spaces is False
        # Unset strikethrough_offset_fraction means the cap-band midline, so None is a
        # meaningful value: only the bound (>= 0) is enforced.
        assert JobDefaults().strikethrough_offset_fraction is None
        assert JobDefaults(
            strikethrough_offset_fraction=0.22
        ).strikethrough_offset_fraction == pytest.approx(0.22)
        assert JobDefaults(strikethrough_offset_fraction=0.0).strikethrough_offset_fraction == 0.0
        with pytest.raises(Exception):
            JobDefaults(strikethrough_offset_fraction=-0.1)
        assert JobDefaults().strikethrough_break_spaces is None
        assert JobDefaults(strikethrough_break_spaces=True).strikethrough_break_spaces is True
        assert JobDefaults(strikethrough_break_spaces=False).strikethrough_break_spaces is False
        # Unset v_center_chars means the shipped default set, so None is the
        # cascade's "unset"; "" is the meaningful auto-apply-off value.
        assert JobDefaults().v_center_chars is None
        assert JobDefaults(v_center_chars="-:").v_center_chars == "-:"
        assert JobDefaults(v_center_chars="").v_center_chars == ""

    def test_bold_boldness_multiplier_defaults_and_bounds(self) -> None:
        """The job layer defaults the multiplier to None and bounds it [1.0, 4.0]."""
        assert JobDefaults().bold_boldness_multiplier is None
        assert JobDefaults(bold_boldness_multiplier=1.0).bold_boldness_multiplier == 1.0
        assert JobDefaults(bold_boldness_multiplier=4.0).bold_boldness_multiplier == 4.0
        with pytest.raises(ValidationError):
            JobDefaults(bold_boldness_multiplier=0.5)
        with pytest.raises(ValidationError):
            JobDefaults(bold_boldness_multiplier=4.5)
        assert JobDefaults().target_boldness_factor is None
        assert JobDefaults(target_boldness_factor=0.18).target_boldness_factor == pytest.approx(
            0.18
        )
        assert JobDefaults(target_boldness_factor=0.05).target_boldness_factor == 0.05
        assert JobDefaults(target_boldness_factor=0.30).target_boldness_factor == 0.30
        with pytest.raises(Exception):
            JobDefaults(target_boldness_factor=0.04)
        with pytest.raises(Exception):
            JobDefaults(target_boldness_factor=0.31)
        assert JobDefaults().multistroke_overlap_ratio is None
        assert JobDefaults(
            multistroke_overlap_ratio=0.35
        ).multistroke_overlap_ratio == pytest.approx(0.35)
        assert JobDefaults(multistroke_overlap_ratio=0.1).multistroke_overlap_ratio == 0.1
        assert JobDefaults(multistroke_overlap_ratio=0.5).multistroke_overlap_ratio == 0.5
        with pytest.raises(Exception):
            JobDefaults(multistroke_overlap_ratio=0.05)
        with pytest.raises(Exception):
            JobDefaults(multistroke_overlap_ratio=0.6)
        assert JobDefaults().use_baseline_spacing is None
        assert JobDefaults(use_baseline_spacing=False).use_baseline_spacing is False
        assert JobDefaults().hole_adjust_for_cutter is None
        assert JobDefaults(hole_adjust_for_cutter=False).hole_adjust_for_cutter is False
        assert JobDefaults(hole_adjust_for_cutter=True).hole_adjust_for_cutter is True
        assert JobDefaults().rounded_rectangle_fraction is None
        assert JobDefaults(rounded_rectangle_fraction=0.5).rounded_rectangle_fraction == 0.5
        assert JobDefaults(rounded_rectangle_fraction=0.0).rounded_rectangle_fraction == 0.0
        assert JobDefaults(rounded_rectangle_fraction=1.0).rounded_rectangle_fraction == 1.0
        with pytest.raises(Exception):
            JobDefaults(rounded_rectangle_fraction=-0.1)
        with pytest.raises(Exception):
            JobDefaults(rounded_rectangle_fraction=1.1)
        assert JobDefaults().rounded_rectangle_proportional is None
        assert (
            JobDefaults(rounded_rectangle_proportional=True).rounded_rectangle_proportional is True
        )
        assert JobDefaults().v_center_by_content is None
        assert JobDefaults(v_center_by_content=True).v_center_by_content is True


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

    def test_unreadable_file_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert filled["space_width_fraction"] == 0.35
        assert filled["min_glyph_width_fraction"] == 0.02
        assert filled["kerning_window_fraction"] == 0.15
        assert filled["kerning_penetration_scale"] == 1.5
        assert filled["kerning_recession_scale"] == 0.0
        assert filled["kerning_min_gap_fraction"] == 0.05
        assert filled["fallback_advance_fraction"] == 0.9
        assert filled["sc_scale"] == 0.45
        assert filled["supersubscript_downscale"] == 0.6
        assert filled["html_brackets"] == "\u00ab\u00bb"
        assert filled["italic_angle"] == 20.0
        assert filled["underline_offset_fraction"] == 0.08
        assert filled["underline_break_clearance_fraction"] == 0.12
        assert filled["underline_break_spaces"] is True
        assert filled["strikethrough_offset_fraction"] == 0.22
        assert filled["strikethrough_break_spaces"] is True
        assert filled["v_center_chars"] == "-:"
        assert filled["target_boldness_factor"] == 0.18
        assert filled["multistroke_overlap_ratio"] == 0.35
        assert filled["bold_boldness_multiplier"] == 2.0
        assert filled["rounded_rectangle_fraction"] == 0.35
        assert filled["rounded_rectangle_proportional"] is True

    def test_cutter_downsize_job_layer_fill(self, tmp_path: Path) -> None:
        """Configured cutter-downsize defaults land as job-level values."""
        config_data = dict(_FULL_CONFIG)
        config_data["cutter_downsize"] = False
        config_data["max_cutter_downsizes"] = 3
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["cutter_downsize"] is False
        assert filled["max_cutter_downsizes"] == 3

    def test_cutter_downsize_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared cutter_downsize is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["cutter_downsize"] = False
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(cutter_downsize=True), config)
        assert filled["cutter_downsize"] is True

    def test_cutter_downsize_global_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured sharing default lands as a job-level value."""
        config_data = dict(_FULL_CONFIG)
        config_data["cutter_downsize_global"] = False
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["cutter_downsize_global"] is False

    def test_cutter_downsize_global_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared cutter_downsize_global is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["cutter_downsize_global"] = False
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(cutter_downsize_global=True), config)
        assert filled["cutter_downsize_global"] is True

    def test_h_compress_global_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured compression-sharing default lands as a job-level value."""
        config_data = dict(_FULL_CONFIG)
        config_data["h_compress_global"] = True
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["h_compress_global"] is True

    def test_h_compress_global_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared h_compress_global is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["h_compress_global"] = True
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(h_compress_global=False), config)
        assert filled["h_compress_global"] is False

    def test_hole_adjust_for_cutter_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured hole-compensation default lands as a job-level value."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["hole_adjust_for_cutter"] is False

    def test_hole_adjust_for_cutter_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared hole_adjust_for_cutter is never overridden by the config."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(hole_adjust_for_cutter=True), config)
        assert filled["hole_adjust_for_cutter"] is True

    def test_optimize_line_content_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured reflow default lands as a job-level value."""
        config_data = dict(_FULL_CONFIG)
        config_data["optimize_line_content"] = True
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["optimize_line_content"] is True

    def test_optimize_line_content_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared optimize_line_content is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["optimize_line_content"] = True
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(optimize_line_content=False), config)
        assert filled["optimize_line_content"] is False

    def test_enforce_cap_line_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured cap-line clamping default lands as a job-level value."""
        config_data = dict(_FULL_CONFIG)
        config_data["enforce_cap_line"] = True
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["enforce_cap_line"] is True

    def test_enforce_cap_line_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared enforce_cap_line is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["enforce_cap_line"] = True
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(enforce_cap_line=False), config)
        assert filled["enforce_cap_line"] is False

    def test_optimize_line_content_max_lines_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured reflow line cap lands as a job-level value."""
        config_data = dict(_FULL_CONFIG)
        config_data["optimize_line_content_max_lines"] = 4
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["optimize_line_content_max_lines"] == 4

    def test_optimize_line_content_max_lines_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared cap is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["optimize_line_content_max_lines"] = 4
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(optimize_line_content_max_lines=2), config)
        assert filled["optimize_line_content_max_lines"] == 2

    def test_optimize_line_content_max_lines_null_stays_unset(self, tmp_path: Path) -> None:
        """A null config cap leaves the job key unset (no growth)."""
        config_data = dict(_FULL_CONFIG)
        config_data["optimize_line_content_max_lines"] = None
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled.get("optimize_line_content_max_lines") is None

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

    def test_spec_margin_suppresses_config_axis_margins(self, tmp_path: Path) -> None:
        """A job-level margin wins over configured h_margin/v_margin.

        The margin cascade checks job.h_margin/job.v_margin before the
        universal margin, so injecting the axis values for a
        margin-declaring spec would silently outrank the spec's margin
        on that axis.
        """
        config_data = dict(_FULL_CONFIG, margin=0.2, h_margin=0.3, v_margin=0.4)
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(margin=2.0), config)
        assert "h_margin" not in filled
        assert "v_margin" not in filled
        assert filled["margin"] == 2.0

    def test_label_margin_suppresses_config_axis_margins(self, tmp_path: Path) -> None:
        """A label-declared margin suppresses the axis margins job-wide."""
        config_data = dict(_FULL_CONFIG, h_margin=0.3, v_margin=0.4)
        config = load_job_config(_write_config(tmp_path, config_data))
        job = _minimal_job(labels=[{"id": "l1", "margin": 0.5, "content": [{"text": "Hi"}]}])
        filled = apply_job_config_defaults(job, config)
        assert "h_margin" not in filled
        assert "v_margin" not in filled

    def test_null_margin_does_not_suppress_config_axis_margins(self, tmp_path: Path) -> None:
        """An explicit ``margin: null`` is unset, so the config fills the axes."""
        config_data = dict(_FULL_CONFIG, h_margin=0.3, v_margin=0.4)
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(margin=None), config)
        assert filled["h_margin"] == 0.3
        assert filled["v_margin"] == 0.4

    def test_yaml_axis_margin_wins_beside_spec_margin(self, tmp_path: Path) -> None:
        """A spec-declared h_margin keeps its own value next to a margin."""
        config_data = dict(_FULL_CONFIG, h_margin=0.3, v_margin=0.4)
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(margin=2.0, h_margin=0.25), config)
        assert filled["h_margin"] == 0.25
        assert "v_margin" not in filled

    def test_axis_margins_inject_without_spec_margin(self, tmp_path: Path) -> None:
        """A spec declaring no margin keeps the configured axis defaults."""
        config_data = dict(_FULL_CONFIG, h_margin=0.3, v_margin=0.4)
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["h_margin"] == 0.3
        assert filled["v_margin"] == 0.4

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

    def test_config_font_injected_at_job_layer(self, tmp_path: Path) -> None:
        """A configured font lands as a job-level value."""
        config_data = dict(_FULL_CONFIG)
        config_data["font"] = "dino"
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["font"] == "dino"

    def test_yaml_font_wins_over_config(self, tmp_path: Path) -> None:
        """A spec-declared font is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["font"] = "dino"
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(font="jhanuni"), config)
        assert filled["font"] == "jhanuni"

    def test_null_yaml_font_filled_by_config(self, tmp_path: Path) -> None:
        """An explicit ``font: null`` is unset semantics: config fills."""
        config_data = dict(_FULL_CONFIG)
        config_data["font"] = "dino"
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(font=None), config)
        assert filled["font"] == "dino"

    def test_config_font_canonicalizes_through_parse(self, tmp_path: Path) -> None:
        """A config font passes through JobSpec validation (canonical form)."""
        config_data = dict(_FULL_CONFIG)
        config_data["font"] = "dino"
        config_path = _write_config(tmp_path, config_data)
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Config Font Job\n"
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
        assert job.font == "Dino"

    def test_config_unknown_font_fails_parse(self, tmp_path: Path) -> None:
        """An unknown config font fails at JobSpec validation (strict)."""
        config_data = dict(_FULL_CONFIG)
        config_data["font"] = "NotARealFont"
        config_path = _write_config(tmp_path, config_data)
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Bad Config Font\n"
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
        with pytest.raises(ValidationError):
            parse_yaml(spec_path, job_config_path=config_path)


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


class TestSpecDeclaresMargin:
    """Direct checks for the margin-declaration probe."""

    def test_job_level_margin(self) -> None:
        """A job-level margin is a declaration."""
        assert _spec_declares_margin({"margin": 0.1}) is True

    def test_label_level_margin(self) -> None:
        """A margin on any label is a declaration too."""
        assert _spec_declares_margin({"labels": [{"margin": 0.1}]}) is True

    def test_null_everywhere_is_unset(self) -> None:
        """Explicit nulls are inherit semantics, not declarations."""
        assert _spec_declares_margin({"margin": None, "labels": [{"margin": None}]}) is False

    def test_non_list_labels_ignored(self) -> None:
        """A malformed labels value never declares a margin."""
        assert _spec_declares_margin({"labels": "not-a-list"}) is False

    def test_non_dict_label_entries_ignored(self) -> None:
        """Non-mapping label entries are skipped, not crashes."""
        assert _spec_declares_margin({"labels": ["plain string"]}) is False


class TestParseYamlWithJobConfig:
    """Integration of parse_yaml() with the job-config layer."""

    def test_repo_config_fills_job_layer(self) -> None:
        """The shipped job-config.json supplies the required defaults."""
        job = parse_yaml("tests_deps/test123_spec.yaml", job_config_path=Path("job-config.json"))
        assert job.hole_margin == 0.1875
        assert job.max_h_compress == 0.7
        assert job.min_hole_margin == 0.1875
        assert job.hole_text_collision_distance == 0.1
        assert job.space_width_fraction == 0.6
        # Shop-tuned kerning/width values, re-pinned after the 2026-10-22
        # job-config.json tuning. tests/AGENTS.md stale-pin pattern -
        # re-pin, keep config.
        assert job.min_glyph_width_fraction == 0.25
        assert job.kerning_window_fraction == 0.1
        assert job.kerning_penetration_scale == 1.0
        assert job.kerning_recession_scale == 1.0
        assert job.kerning_min_gap_fraction == 0.05
        assert job.fallback_advance_fraction == 1.0
        assert job.sc_scale == 0.8
        assert job.supersubscript_downscale == 0.6
        assert job.html_brackets == "<>"
        assert job.italic_angle == 12.0
        # Cap-height fractions (the unitless migration): the shop-tuned
        # values, re-pinned after commit 05889c6 ("chore: update underline
        # and strikethrough defaults"). tests/AGENTS.md stale-pin pattern -
        # re-pin, keep config.
        assert job.underline_offset_fraction == 0.15
        assert job.underline_break_clearance_fraction == 0.2
        assert job.underline_break_spaces is False
        # The shop ships the mid-cap strike (0.4 cap heights) and a continuous
        # stroke.
        assert job.strikethrough_offset_fraction == 0.4
        assert job.strikethrough_break_spaces is False
        # The shop ships the high-sitting characters (hyphen, colon, en/em dash,
        # '=', '<', '>') auto-centred between the baseline and the capline.
        assert job.v_center_chars == "-:–—=<>"
        assert job.target_boldness_factor == 0.14
        assert job.multistroke_overlap_ratio == 0.25
        assert job.bold_boldness_multiplier == 1.5
        assert job.cutter_downsize is True
        assert job.max_cutter_downsizes == 1
        assert job.cutter_downsize_global is True
        assert job.h_compress_global is False
        assert job.h_compress_global_same_height_only is True
        assert job.optimize_line_content is False
        assert job.optimize_line_content_max_lines is None
        assert job.enforce_cap_line is True
        assert job.use_baseline_spacing is True
        assert job.v_center_by_content is False
        # The shop ships cutter-compensated drill holes (the shipped default).
        assert job.hole_adjust_for_cutter is True
        # The shipped config keeps the sharp rectangle (the historical border).
        assert job.rounded_rectangle_fraction == 0.0
        assert job.rounded_rectangle_proportional is False
        # The descending-character relief knobs are shop-tuning values (the
        # repo config ships non-default numbers on purpose), so this test only
        # pins that the config layer resolves them -- never the exact numbers.
        assert isinstance(job.auto_descending_character_adjustment, bool)
        assert isinstance(job.descending_character_v_scale, float)
        assert isinstance(job.descending_character_raise, float)
        assert isinstance(job.descending_character_clearance, float)
        assert isinstance(job.descending_character_reciprocal_scale, float)

    def test_yaml_overrides_config(self) -> None:
        """Spec-declared values still win over the shipped config."""
        job = parse_yaml("tests_deps/test123_spec.yaml", job_config_path=Path("job-config.json"))
        assert job.margin == 0.1
        assert job.text_height == 0.5

    def test_repo_config_margin_suppresses_axis_margins(self) -> None:
        """The shipped h_margin default yields to the spec's margin.

        ``tests_deps/test123_spec.yaml`` declares ``margin: 0.1`` and no
        axis margins, so the repo config's ``h_margin`` must not be
        injected and resolution must land 0.1in on every edge.
        """
        job = parse_yaml("tests_deps/test123_spec.yaml", job_config_path=Path("job-config.json"))
        assert job.margin == 0.1
        assert job.h_margin is None
        assert job.v_margin is None
        for label in resolve_job_spec(job):
            assert math.isclose(label.h_margin, 0.1)
            assert math.isclose(label.v_margin, 0.1)

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
