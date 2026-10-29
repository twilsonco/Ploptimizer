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
    _JOB_LAYER_FIELDS,
    _MARGIN_SUPPRESSED_FIELDS,
    _REPLACEMENT_SUPPRESSED_FIELDS,
    REQUIRED_WHEN_UNCONFIGURED,
    JobConfig,
    JobConfigError,
    JobDefaults,
    _spec_declares_margin,
    _spec_declares_replacement_file,
    apply_job_config_defaults,
    assert_required_fields,
    load_job_config,
)
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import CounterStyle, JobSpec, parse_yaml

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
    "kern_pos_string": "<+>",
    "kern_neg_string": "<->",
    "kern_width_fraction": 0.2,
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
    "align_margin_adjust_global": True,
    "cutter_buffer_text": 0.007,
    "cutter_buffer_routing": 0.033,
    "plate_width": 24.0,
    "plate_height": 16.0,
    "left_clearance": 0.0,
    "top_clearance": 0.0,
    "replacement_text_delimiter": ",",
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

    def test_preview_background_color_hex_accepted(self) -> None:
        """#RGB/#RRGGBB hex strings validate and pass through trimmed."""
        assert JobDefaults(preview_background_color="#000080").preview_background_color == "#000080"
        assert JobDefaults(preview_background_color=" #abc ").preview_background_color == "#abc"
        assert JobDefaults().preview_background_color is None

    @pytest.mark.parametrize("bad", ["", "  ", "D3D3D3", "#GG0000", "white"])
    def test_preview_background_color_rejects_non_hex(self, bad: str) -> None:
        """A malformed preview background color fails at config load."""
        with pytest.raises(ValidationError, match="preview_background_color"):
            JobDefaults(preview_background_color=bad)

    def test_preview_background_color_is_not_injected_into_the_job(self) -> None:
        """Display-only: the key never reaches the (extra-forbidding) JobSpec."""
        from plt_optimizer.generate.job_config import _JOB_LAYER_FIELDS

        assert "preview_background_color" not in _JOB_LAYER_FIELDS

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
        """font defaults to None; a known name is canonicalized against the registry."""
        assert JobDefaults().font is None
        assert JobDefaults(font="dino").font == "Dino"

    def test_font_unknown_name_rejected_at_load(self) -> None:
        """An unknown config font fails at config load, not at spec parse."""
        with pytest.raises(ValidationError, match="Unknown font 'NotARealFont'"):
            JobDefaults(font="NotARealFont")

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
        assert "align_margin_adjust_global" not in REQUIRED_WHEN_UNCONFIGURED
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
        assert "kern_pos_string" not in REQUIRED_WHEN_UNCONFIGURED
        assert "kern_neg_string" not in REQUIRED_WHEN_UNCONFIGURED
        assert "kern_width_fraction" not in REQUIRED_WHEN_UNCONFIGURED
        assert JobDefaults().kern_pos_string is None
        assert JobDefaults().kern_neg_string is None
        assert JobDefaults().kern_width_fraction is None
        assert JobDefaults(kern_pos_string="<->").kern_pos_string == "<->"
        with pytest.raises(Exception):
            JobDefaults(kern_width_fraction=-0.1)
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
        assert JobDefaults().align_margin_adjust_global is None
        assert JobDefaults(align_margin_adjust_global=False).align_margin_adjust_global is False
        assert JobDefaults(align_margin_adjust_global=True).align_margin_adjust_global is True
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
        assert filled["kern_pos_string"] == "<+>"
        assert filled["kern_neg_string"] == "<->"
        assert filled["kern_width_fraction"] == 0.2
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

    def test_cutter_buffer_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured kerf buffers land as job-level values."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["cutter_buffer_text"] == 0.007
        assert filled["cutter_buffer_routing"] == 0.033

    def test_cutter_buffer_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared kerf buffer is never overridden by the config."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(
            _minimal_job(cutter_buffer_text=0.0, cutter_buffer_routing=0.02), config
        )
        # An explicit 0.0 is a value (legacy engraved = nominal), not "unset".
        assert filled["cutter_buffer_text"] == 0.0
        assert filled["cutter_buffer_routing"] == 0.02

    def test_cutter_buffer_absent_stays_unset(self, tmp_path: Path) -> None:
        """A config without the keys leaves them unset (resolution -> 0.0)."""
        config_data = {k: v for k, v in _FULL_CONFIG.items() if not k.startswith("cutter_buffer")}
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert "cutter_buffer_text" not in filled
        assert "cutter_buffer_routing" not in filled

    def test_cutter_buffer_negative_rejected_at_load(self, tmp_path: Path) -> None:
        """A negative kerf buffer fails at config load, not on the first job."""
        for name in ("cutter_buffer_text", "cutter_buffer_routing"):
            with pytest.raises(JobConfigError, match=name):
                load_job_config(_write_config(tmp_path, {**_FULL_CONFIG, name: -0.001}))

    def test_repo_config_ships_cutter_buffers(self) -> None:
        """The shipped shop config configures both kerf buffers.

        Adaptive: the values are the config's own, never pinned here (the
        repo config is hand-tuned for the real shop).
        """
        config = load_job_config(Path("job-config.json"))
        assert config is not None
        for name in ("cutter_buffer_text", "cutter_buffer_routing"):
            value = getattr(config.defaults, name)
            assert value is not None, f"job-config.json must ship {name}"
            assert value >= 0.0

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

    def test_cutter_downsize_global_job_wide_config_field(self) -> None:
        """cutter_downsize_global_job_wide is a legal shop default (opt-in configurable)."""
        assert (
            JobDefaults(cutter_downsize_global_job_wide=True).cutter_downsize_global_job_wide
            is True
        )
        assert (
            JobDefaults(cutter_downsize_global_job_wide=False).cutter_downsize_global_job_wide
            is False
        )
        assert JobDefaults().cutter_downsize_global_job_wide is None

    def test_cutter_downsize_global_job_wide_not_required(self) -> None:
        """The sharing-scope field keeps its in-code fallback when unconfigured."""
        assert "cutter_downsize_global_job_wide" not in REQUIRED_WHEN_UNCONFIGURED

    def test_cutter_downsize_global_job_wide_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured sharing-scope default lands as a job-level value."""
        config_data = dict(_FULL_CONFIG)
        config_data["cutter_downsize_global_job_wide"] = True
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["cutter_downsize_global_job_wide"] is True

    def test_cutter_downsize_global_job_wide_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared value is never overridden by the config."""
        config_data = dict(_FULL_CONFIG)
        config_data["cutter_downsize_global_job_wide"] = True
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(
            _minimal_job(cutter_downsize_global_job_wide=False), config
        )
        assert filled["cutter_downsize_global_job_wide"] is False

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

    def test_align_margin_adjust_global_job_layer_fill(self, tmp_path: Path) -> None:
        """The configured edge-margin sharing default lands as a job-level value."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["align_margin_adjust_global"] is True

    def test_align_margin_adjust_global_yaml_wins(self, tmp_path: Path) -> None:
        """A spec-declared align_margin_adjust_global is never overridden by the config."""
        config = load_job_config(_write_config(tmp_path, _FULL_CONFIG))
        filled = apply_job_config_defaults(_minimal_job(align_margin_adjust_global=False), config)
        assert filled["align_margin_adjust_global"] is False

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
        """A configured font lands as a job-level value (canonicalized at load)."""
        config_data = dict(_FULL_CONFIG)
        config_data["font"] = "dino"
        config = load_job_config(_write_config(tmp_path, config_data))
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert filled["font"] == "Dino"

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
        assert filled["font"] == "Dino"

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

    def test_config_unknown_font_fails_config_load(self, tmp_path: Path) -> None:
        """An unknown config font fails at config load (attributed to the config).

        The gate moved earlier: the shop config is rejected by
        ``load_job_config`` before any spec is parsed, so the error names the
        config file instead of surfacing as a ``JobSpec`` field error.
        """
        config_data = dict(_FULL_CONFIG)
        config_data["font"] = "NotARealFont"
        config_path = _write_config(tmp_path, config_data)
        with pytest.raises(JobConfigError, match="Unknown font 'NotARealFont'"):
            load_job_config(config_path)


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


class TestSpecDeclaresReplacementFile:
    """Direct checks for the replacement-file-declaration probe."""

    def test_job_level_file(self) -> None:
        """A job-level file is a declaration."""
        assert _spec_declares_replacement_file({"replacement_text_file": "d.txt"}) is True

    def test_plate_level_file(self) -> None:
        """A plate-level file counts too (the cascade reaches plates)."""
        assert (
            _spec_declares_replacement_file({"plates": [{"replacement_text_file": "d.txt"}]})
            is True
        )

    def test_label_level_file(self) -> None:
        """A label-level file counts too (the cascade reaches labels)."""
        assert (
            _spec_declares_replacement_file({"labels": [{"replacement_text_file": "d.txt"}]})
            is True
        )

    def test_no_file_anywhere(self) -> None:
        """A plain labels job declares no replacement file."""
        assert _spec_declares_replacement_file(_minimal_job()) is False

    def test_null_everywhere_is_unset(self) -> None:
        """Explicit nulls are inherit semantics, not declarations."""
        assert (
            _spec_declares_replacement_file(
                {"replacement_text_file": None, "labels": [{"replacement_text_file": None}]}
            )
            is False
        )

    def test_non_list_entries_ignored(self) -> None:
        """Malformed plates/labels values never declare a file, not crashes."""
        assert _spec_declares_replacement_file({"plates": "not-a-list"}) is False
        assert _spec_declares_replacement_file({"labels": ["plain string"]}) is False


class TestConfigCommentMarkerInjection:
    """The configured replacement comment marker honors the file requirement."""

    def test_injected_with_job_file(self, tmp_path: Path) -> None:
        """A spec with a job-level file receives the configured marker."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_file_comment_string": "//"})
        )
        filled = apply_job_config_defaults(_minimal_job(replacement_text_file="d.txt"), config)
        assert filled["replacement_text_file_comment_string"] == "//"

    def test_injected_with_label_file(self, tmp_path: Path) -> None:
        """A label-level file also unlocks the injection (cascade target)."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_file_comment_string": "//"})
        )
        job = {
            "job_name": "Config Test",
            "labels": [{"id": "l1", "replacement_text_file": "d.txt"}],
        }
        filled = apply_job_config_defaults(job, config)
        assert filled["replacement_text_file_comment_string"] == "//"

    def test_suppressed_without_file(self, tmp_path: Path) -> None:
        """A spec with no replacement file anywhere never gets the marker."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_file_comment_string": "//"})
        )
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert "replacement_text_file_comment_string" not in filled

    def test_yaml_wins_over_config(self, tmp_path: Path) -> None:
        """A spec-declared marker is never overridden by the config."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_file_comment_string": "//"})
        )
        filled = apply_job_config_defaults(
            _minimal_job(replacement_text_file="d.txt", replacement_text_file_comment_string="#"),
            config,
        )
        assert filled["replacement_text_file_comment_string"] == "#"

    def test_repo_config_ships_comment_marker(self) -> None:
        """The shipped shop config supplies the '# ' default (adaptive)."""
        config = load_job_config(Path("job-config.json"))
        assert config is not None
        assert config.defaults.replacement_text_file_comment_string is not None

    def test_repo_config_parses_file_less_job(self) -> None:
        """The shipped marker cannot break a job with no replacement file."""
        job = parse_yaml("tests_deps/test123_spec.yaml", job_config_path=Path("job-config.json"))
        assert job.replacement_text_file_comment_string is None


class TestConfigDelimiterInjection:
    """The configured replacement delimiter honors the file requirement."""

    def test_injected_with_job_file(self, tmp_path: Path) -> None:
        """A spec with a job-level file receives the configured delimiter."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_delimiter": ","})
        )
        filled = apply_job_config_defaults(_minimal_job(replacement_text_file="d.txt"), config)
        assert filled["replacement_text_delimiter"] == ","

    def test_injected_with_label_file(self, tmp_path: Path) -> None:
        """A label-level file also unlocks the injection (cascade target)."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_delimiter": ","})
        )
        job = {
            "job_name": "Config Test",
            "labels": [{"id": "l1", "replacement_text_file": "d.txt"}],
        }
        filled = apply_job_config_defaults(job, config)
        assert filled["replacement_text_delimiter"] == ","

    def test_injected_with_plate_file(self, tmp_path: Path) -> None:
        """A plate-level file unlocks the injection too."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_delimiter": ","})
        )
        job = {
            "job_name": "Config Test",
            "plates": [
                {"id": "p1", "width": 24.0, "height": 16.0, "replacement_text_file": "d.txt"}
            ],
        }
        filled = apply_job_config_defaults(job, config)
        assert filled["replacement_text_delimiter"] == ","

    def test_suppressed_without_file(self, tmp_path: Path) -> None:
        """A spec with no replacement file anywhere never gets the delimiter."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_delimiter": ","})
        )
        filled = apply_job_config_defaults(_minimal_job(), config)
        assert "replacement_text_delimiter" not in filled

    def test_yaml_wins_over_config(self, tmp_path: Path) -> None:
        """A spec-declared delimiter is never overridden by the config."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_delimiter": ","})
        )
        filled = apply_job_config_defaults(
            _minimal_job(replacement_text_file="d.txt", replacement_text_delimiter="|"),
            config,
        )
        assert filled["replacement_text_delimiter"] == "|"

    def test_repo_config_ships_delimiter(self) -> None:
        """The shipped shop config supplies the delimiter default (adaptive)."""
        config = load_job_config(Path("job-config.json"))
        assert config is not None
        assert config.defaults.replacement_text_delimiter is not None

    def test_repo_config_parses_file_less_job(self) -> None:
        """The shipped delimiter cannot break a job with no replacement file."""
        job = parse_yaml("tests_deps/test123_spec.yaml", job_config_path=Path("job-config.json"))
        assert job.replacement_text_delimiter is None

    def test_config_delimiter_cascades_to_label_file(self, tmp_path: Path) -> None:
        """The configured job-level delimiter lands on a label-level file."""
        config_path = _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_delimiter": ","})
        spec = tmp_path / "job.yaml"
        spec.write_text(
            "job:\n"
            "  job_name: J\n"
            "  width: 3.0\n"
            "  height: 1.0\n"
            "  text_height: 0.75\n"
            "  labels:\n"
            "    - id: l\n"
            "      replacement_text_file: data.txt\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec, job_config_path=config_path)
        assert job.replacement_text_delimiter == ","
        assert job.labels is not None
        assert job.labels[0].replacement_text_delimiter == ","

    @pytest.mark.parametrize("delimiter", ["ab", "\n", "\r", "a", "Z", "9"])
    def test_invalid_config_delimiter_rejected(self, tmp_path: Path, delimiter: str) -> None:
        """A bad shop delimiter fails at config load, not on the first job."""
        with pytest.raises(JobConfigError, match="replacement_text_delimiter"):
            load_job_config(
                _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_delimiter": delimiter})
            )

    def test_whitespace_config_delimiter_accepted(self, tmp_path: Path) -> None:
        """The shared rule allows whitespace delimiters."""
        config = load_job_config(
            _write_config(tmp_path, {**_FULL_CONFIG, "replacement_text_delimiter": " "})
        )
        assert config is not None
        assert config.defaults.replacement_text_delimiter == " "


class TestParseYamlWithJobConfig:
    """Integration of parse_yaml() with the job-config layer."""

    def test_repo_config_fills_job_layer(self) -> None:
        """Every value the shipped job-config.json sets lands on the job layer.

        The repo config is hand-tuned for the real shop, so this test is
        **adaptive**: it asserts the injection *mechanism* -- each configured
        (non-``None``) job-layer field reaches the parsed ``JobSpec`` with the
        config's own value, and the required-when-unconfigured set is
        satisfied -- never the shop's exact numbers. Retuning
        ``job-config.json`` can therefore never break this test.
        """
        config = load_job_config(Path("job-config.json"))
        assert config is not None
        # ``parse_yaml`` enforces the required-when-unconfigured contract, so
        # reaching a parsed job proves the shipped config satisfies it.
        job = parse_yaml("tests_deps/test123_spec.yaml", job_config_path=Path("job-config.json"))
        # The spec declares ``margin``, so the axis-margin injection is
        # suppressed by contract, and it declares no replacement text file,
        # so the replacement delimiter/comment injection is suppressed too --
        # those keys are the documented exceptions.
        checked = 0
        suppressed = _MARGIN_SUPPRESSED_FIELDS | _REPLACEMENT_SUPPRESSED_FIELDS
        for field_name in _JOB_LAYER_FIELDS:
            if field_name in suppressed:
                continue
            configured = getattr(config.defaults, field_name)
            if configured is None:
                continue
            checked += 1
            assert getattr(job, field_name) == configured, field_name
        # The shipped config is a real default set, not an empty one.
        assert checked > 20

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


class TestListDefaults:
    """The six list-settings fields are legal shop defaults."""

    def test_fields_are_accepted(self) -> None:
        """Every list field loads from the config."""
        defaults = JobDefaults(
            bullet_string="-",
            counter_start_index=3,
            counter_style="upper-roman",
            counter_prefix="Step ",
            counter_suffix=")",
            list_marker_gap_fraction=0.25,
        )
        assert defaults.bullet_string == "-"
        assert defaults.counter_start_index == 3
        assert defaults.counter_style is CounterStyle.UPPER_ROMAN
        assert defaults.counter_prefix == "Step "
        assert defaults.counter_suffix == ")"
        assert defaults.list_marker_gap_fraction == 0.25

    def test_defaults_are_unset(self) -> None:
        """An empty config carries no list defaults (the cascade falls through
        to the code fallbacks)."""
        defaults = JobDefaults()
        assert defaults.bullet_string is None
        assert defaults.counter_start_index is None
        assert defaults.counter_style is None
        assert defaults.counter_prefix is None
        assert defaults.counter_suffix is None
        assert defaults.list_marker_gap_fraction is None

    def test_counter_start_index_bound(self) -> None:
        """The configured starting counter is >= 1."""
        with pytest.raises(ValidationError):
            JobDefaults(counter_start_index=0)

    def test_gap_fraction_bound(self) -> None:
        """The configured marker gap is >= 0."""
        with pytest.raises(ValidationError):
            JobDefaults(list_marker_gap_fraction=-0.1)

    def test_fields_inject_at_the_job_layer(self, tmp_path: Path) -> None:
        """The config's list settings land on the job spec (job layer)."""
        path = _write_config(
            tmp_path,
            {
                "bullet_string": "*",
                "counter_style": "lower-alpha",
                "list_marker_gap_fraction": 0.75,
            },
        )
        config = load_job_config(path)
        assert config is not None
        job_data: dict = {"job_name": "J", "width": 2.0, "height": 1.0}
        filled = apply_job_config_defaults(job_data, config)
        assert filled["bullet_string"] == "*"
        assert filled["counter_style"] == "lower-alpha"
        assert filled["list_marker_gap_fraction"] == 0.75
        # The input mapping is never mutated (the documented contract).
        assert "bullet_string" not in job_data

    def test_marker_strings_reject_newlines(self, tmp_path: Path) -> None:
        """A newline-carrying configured marker aborts the injected job.

        ``JobDefaults`` carries no string validator (the ``html_brackets``
        precedent): the rule runs on the injected ``JobSpec`` at validation
        time, so the config value is checked by the same cascade-tier rule.
        """
        path = _write_config(tmp_path, {"bullet_string": "a\nb"})
        config = load_job_config(path)
        assert config is not None
        job_data: dict = {"job_name": "J", "width": 2.0, "height": 1.0}
        filled = apply_job_config_defaults(job_data, config)
        with pytest.raises(ValidationError, match="newline"):
            JobSpec(**filled)


#: A single sound numeric option, used as the baseline for the mutation tests.
_GOOD_FLOAT_OPTION: dict[str, Any] = {
    "command": "VS",
    "type": "float",
    "units": "in/sec",
    "default": {"text": 1.5, "borders_holes": 0.8},
    "min": 0.05,
    "max": 3.0,
}


def _tool_options_config(entry: Any, key: str = "cutting_velocity") -> dict[str, Any]:
    """Build a config carrying one ``tool_options`` entry.

    Args:
        entry: The metadata value under test.
        key: The option key.

    Returns:
        A config mapping with only ``tool_options`` set.
    """
    return {"tool_options": {key: entry}}


class TestToolOptionsMetadataValidation:
    """The config ``tool_options`` gate (dict[str, Any] needs semantic checks).

    Every case below is a shape that used to load cleanly and then fail deep
    in export, emit a corrupt HPGL header, or silently drop a cutter option.
    """

    def test_unset_allows(self) -> None:
        """``None`` (missing/null) keeps the historical no-options contract."""
        assert JobDefaults().tool_options is None
        assert JobDefaults(tool_options=None).tool_options is None

    def test_shop_config_loads_clean(self) -> None:
        """The real hand-tuned shop config passes the gate (not too strict)."""
        config = load_job_config(Path(__file__).resolve().parents[1] / "job-config.json")
        assert config is not None
        metadata = config.defaults.tool_options
        assert metadata, "shop config is expected to define tool options"
        assert "cutting_velocity" in metadata

    def test_sound_entry_loads_unchanged(self, tmp_path: Path) -> None:
        """A valid entry round-trips byte-for-byte (no coercion)."""
        config = load_job_config(_write_config(tmp_path, _tool_options_config(_GOOD_FLOAT_OPTION)))
        assert config is not None
        assert config.defaults.tool_options == {"cutting_velocity": _GOOD_FLOAT_OPTION}

    def test_empty_mapping_allowed(self, tmp_path: Path) -> None:
        """``tool_options: {}`` is a legitimate "no options" config."""
        config = load_job_config(_write_config(tmp_path, {"tool_options": {}}))
        assert config is not None
        assert config.defaults.tool_options == {}

    def test_non_object_mapping_rejected(self, tmp_path: Path) -> None:
        """The mapping itself must be an object."""
        path = _write_config(tmp_path, {"tool_options": [1, 2]})
        with pytest.raises(JobConfigError, match="must be an object"):
            load_job_config(path)

    @pytest.mark.parametrize("entry", [42, "VS1.5;", None, True, [1], 3.5])
    def test_entry_must_be_object(self, tmp_path: Path, entry: Any) -> None:
        """A non-object entry is the crash-deep-in-export shape (AttributeError)."""
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match="must be an object, got"):
            load_job_config(path)

    @pytest.mark.parametrize("command", [None, "", "   ", 5, ["VS"]])
    def test_command_required_string(self, tmp_path: Path, command: Any) -> None:
        """A missing command emits a header with no command (corrupt PLT)."""
        entry = dict(_GOOD_FLOAT_OPTION)
        if command is None:
            entry.pop("command")
        else:
            entry["command"] = command
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match=r"\.command: required non-empty string"):
            load_job_config(path)

    def test_command_must_not_contain_semicolon(self, tmp_path: Path) -> None:
        """A ';' in the command would terminate the HPGL statement early."""
        entry = dict(_GOOD_FLOAT_OPTION, command="VS;ZO123,")
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match="must not contain ';'"):
            load_job_config(path)

    @pytest.mark.parametrize("param_type", [None, "string", "String", "double", "", 7])
    def test_type_must_be_known(self, tmp_path: Path, param_type: Any) -> None:
        """An unknown type silently drops the option from the header."""
        entry = dict(_GOOD_FLOAT_OPTION)
        if param_type is None:
            entry.pop("type")
        else:
            entry["type"] = param_type
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match=r"\.type: must be one of bool, int, float"):
            load_job_config(path)

    def test_default_required(self, tmp_path: Path) -> None:
        """Without a default the option is inert for every job."""
        entry = dict(_GOOD_FLOAT_OPTION)
        entry.pop("default")
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match=r"\.default: required"):
            load_job_config(path)

    def test_dual_default_must_cover_both_layers(self, tmp_path: Path) -> None:
        """A dual default missing a layer leaves that PLT type with no header."""
        entry = dict(_GOOD_FLOAT_OPTION, default={"text": 1.5})
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match="missing layer\\(s\\) borders_holes"):
            load_job_config(path)

    def test_dual_default_rejects_unknown_layer(self, tmp_path: Path) -> None:
        """A typo'd layer name is an unusable default (silently None)."""
        entry = dict(_GOOD_FLOAT_OPTION, default={"text": 1.5, "border_hole": 0.8})
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match="unknown layer\\(s\\) border_hole"):
            load_job_config(path)

    @pytest.mark.parametrize("value", ["fast", True, [1.5], None])
    def test_numeric_dual_default_type(self, tmp_path: Path, value: Any) -> None:
        """A numeric option's per-layer default must be numeric."""
        entry = dict(_GOOD_FLOAT_OPTION, default={"text": value, "borders_holes": 0.8})
        path = _write_config(tmp_path, _tool_options_config(entry))
        if value is None:
            # An explicit null layer means "omit this header" (documented).
            config = load_job_config(path)
            assert config is not None
            return
        with pytest.raises(JobConfigError, match=r"\.default\.text: must be a number"):
            load_job_config(path)

    def test_legacy_scalar_default_allowed(self, tmp_path: Path) -> None:
        """A scalar default stays legal (backwards-compatible single default)."""
        entry = dict(_GOOD_FLOAT_OPTION, default=1.5)
        config = load_job_config(_write_config(tmp_path, _tool_options_config(entry)))
        assert config is not None
        assert config.defaults.tool_options == {"cutting_velocity": entry}

    def test_legacy_scalar_default_must_be_numeric(self, tmp_path: Path) -> None:
        """A scalar default for a numeric option must be a number."""
        entry = dict(_GOOD_FLOAT_OPTION, default="fast")
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match=r"\.default: must be a number"):
            load_job_config(path)

    def test_legacy_scalar_bool_default(self, tmp_path: Path) -> None:
        """A scalar boolean default is legal for a bool option; a number is not."""
        good = {"command": "ZO123,", "type": "bool", "default": True}
        assert load_job_config(_write_config(tmp_path, _tool_options_config(good, "spindle")))
        bad = {"command": "ZO123,", "type": "bool", "default": 1}
        path = _write_config(tmp_path, _tool_options_config(bad, "spindle"))
        with pytest.raises(JobConfigError, match=r"\.default: must be a boolean"):
            load_job_config(path)

    @pytest.mark.parametrize("value", ["on", 1, [True]])
    def test_bool_default_must_be_boolean(self, tmp_path: Path, value: Any) -> None:
        """A bool option's default must be a real boolean (1 is not True here)."""
        entry = {
            "command": "ZO123,",
            "type": "bool",
            "default": {"text": value, "borders_holes": True},
        }
        path = _write_config(tmp_path, _tool_options_config(entry, key="spindle"))
        with pytest.raises(JobConfigError, match=r"\.default\.text: must be a boolean"):
            load_job_config(path)

    @pytest.mark.parametrize("bound", ["min", "max"])
    def test_numeric_bounds_required(self, tmp_path: Path, bound: str) -> None:
        """A numeric option without its clamp range has undefined bounds."""
        entry = dict(_GOOD_FLOAT_OPTION)
        entry.pop(bound)
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match=rf"\.{bound}: required for type 'float'"):
            load_job_config(path)

    @pytest.mark.parametrize("bound", ["min", "max"])
    def test_numeric_bounds_must_be_numbers(self, tmp_path: Path, bound: str) -> None:
        """Non-numeric bounds would raise inside the clamp comparison."""
        entry = dict(_GOOD_FLOAT_OPTION, **{bound: "0.05"})
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match=rf"\.{bound}: must be a number"):
            load_job_config(path)

    def test_min_must_not_exceed_max(self, tmp_path: Path) -> None:
        """An inverted range clamps every value to min (nonsense output)."""
        entry = dict(_GOOD_FLOAT_OPTION, min=5.0, max=1.0)
        path = _write_config(tmp_path, _tool_options_config(entry))
        with pytest.raises(JobConfigError, match="is greater than max"):
            load_job_config(path)

    def test_min_equal_max_allowed(self, tmp_path: Path) -> None:
        """A degenerate range is a legitimate "fixed value" option."""
        entry = dict(_GOOD_FLOAT_OPTION, min=2.0, max=2.0)
        config = load_job_config(_write_config(tmp_path, _tool_options_config(entry)))
        assert config is not None

    def test_bool_options_need_no_bounds(self, tmp_path: Path) -> None:
        """The formatters ignore min/max for bools, so they stay optional."""
        entry = {
            "command": "ZO104,",
            "type": "bool",
            "default": {"text": True, "borders_holes": True},
        }
        config = load_job_config(_write_config(tmp_path, _tool_options_config(entry)))
        assert config is not None

    @pytest.mark.parametrize(
        "key",
        ["Cutting Velocity", "cuttingVelocity", "cutting-velocity", "_x", "1x", "x_", "VS"],
    )
    def test_option_key_must_be_snake_case(self, tmp_path: Path, key: str) -> None:
        """Keys are the job-spec tool_options lookup names, so they must match."""
        path = _write_config(tmp_path, _tool_options_config(_GOOD_FLOAT_OPTION, key=key))
        with pytest.raises(JobConfigError, match="key must be a snake_case name"):
            load_job_config(path)

    def test_single_snake_word_key_allowed(self, tmp_path: Path) -> None:
        """A one-word snake_case key is valid (the regex is not underscore-forced)."""
        entry = {"command": "ZO104,", "type": "bool", "default": True}
        config = load_job_config(_write_config(tmp_path, _tool_options_config(entry, key="vacuum")))
        assert config is not None

    def test_all_problems_reported_at_once(self, tmp_path: Path) -> None:
        """The gate aggregates: one load reports every problem in the config."""
        bad_min = dict(_GOOD_FLOAT_OPTION, min=9.0, max=1.0)
        bad_min.pop("command")
        config_data = {
            "tool_options": {
                "cutting_velocity": bad_min,
                "dwell_time": {"type": "string", "default": "50"},
                "not_an_object": 42,
                "BadKey": dict(_GOOD_FLOAT_OPTION),
            }
        }
        path = _write_config(tmp_path, config_data)
        with pytest.raises(JobConfigError) as excinfo:
            load_job_config(path)
        message = str(excinfo.value)
        # BadKey: key; cutting_velocity: missing command + inverted range;
        # dwell_time: missing command + bad type (short-circuits defaults);
        # not_an_object: not an object.
        assert "6 problem(s)" in message
        for fragment in (
            "tool_options.BadKey: key must be a snake_case name",
            "tool_options.cutting_velocity.command: required non-empty string",
            "tool_options.cutting_velocity.min: 9.0 is greater than max 1.0",
            "tool_options.dwell_time.type: must be one of bool, int, float",
            "tool_options.not_an_object: must be an object, got int",
        ):
            assert fragment in message, fragment

    def test_problems_sorted_by_key(self, tmp_path: Path) -> None:
        """Report order is deterministic (stable for scripts and tests)."""
        config_data = {
            "tool_options": {
                "z_option": 1,
                "a_option": 2,
                "m_option": 3,
            }
        }
        path = _write_config(tmp_path, config_data)
        with pytest.raises(JobConfigError) as excinfo:
            load_job_config(path)
        message = str(excinfo.value)
        assert message.index("a_option") < message.index("m_option") < message.index("z_option")

    def test_validator_rejects_direct_construction(self) -> None:
        """The gate lives on the model, so direct construction is covered too."""
        with pytest.raises(ValidationError, match="must be an object, got int"):
            JobDefaults(tool_options={"cutting_velocity": 42})


class TestToolOptionsGateRuntimeEffect:
    """The validated metadata is what makes the header generator total."""

    def test_validated_entry_generates_header(self, tmp_path: Path) -> None:
        """A config that passes the gate produces the expected HPGL header."""
        from types import SimpleNamespace

        from plt_optimizer.generate.tool_options import generate_tool_option_headers

        config = load_job_config(_write_config(tmp_path, _tool_options_config(_GOOD_FLOAT_OPTION)))
        assert config is not None
        headers = generate_tool_option_headers(SimpleNamespace(tool_options=None), config, "text")
        assert headers == ["VS1.50;"]

    def test_malformed_entry_never_reaches_generator(self, tmp_path: Path) -> None:
        """The crash shape is unreachable: config load rejects it first."""
        path = _write_config(tmp_path, _tool_options_config(42))
        with pytest.raises(JobConfigError):
            load_job_config(path)
