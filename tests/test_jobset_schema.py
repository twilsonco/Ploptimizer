"""Tests for the jobset spec: parsing, validation and identity tokens.

A jobset is the composite YAML that points at multiple job files
(docs/TODO.md). These tests pin the parsing contract in
:mod:`plt_optimizer.generate.jobset` and the models in
:mod:`plt_optimizer.generate.schema`; the merge/gate contract is pinned by
``tests/test_jobset_merge.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plt_optimizer.generate.job_prep import sanitize_job_id
from plt_optimizer.generate.jobset import (
    ID_SEPARATOR,
    JOB_LEGEND_PALETTE,
    JOB_LEGEND_STYLES,
    is_jobset_file,
    jobset_id_of,
    legend_style_for,
    make_composite_id,
    parse_jobset_yaml,
    split_composite_id,
)
from plt_optimizer.generate.schema import (
    JobSetEntry,
    JobSetSpec,
    parse_yaml,
)

FIXTURES = Path("tests_deps")


class TestParseJobsetYaml:
    """The jobset file's two shapes, path resolution and root-key rules."""

    def test_parses_the_documented_list_form(self) -> None:
        """A bare list under `jobset:` is the shape from the TODO item."""
        spec = parse_jobset_yaml(FIXTURES / "jobset_list_form.yaml")

        assert spec.name is None
        assert [entry.job.name for entry in spec.jobs] == ["jobset_a.yaml", "jobset_b.yaml"]

    def test_parses_the_mapping_form_with_a_name(self) -> None:
        """The extended form names the batch (the filename token)."""
        spec = parse_jobset_yaml(FIXTURES / "jobset_batch.yaml")

        assert spec.name == "Jobset Batch"
        assert len(spec.jobs) == 2

    def test_relative_job_paths_resolve_against_the_jobset_file(self) -> None:
        """Moving the whole directory must not break the members."""
        spec = parse_jobset_yaml(FIXTURES / "jobset_batch.yaml")

        for entry in spec.jobs:
            assert entry.job.is_absolute()
            assert entry.job.parent == (FIXTURES / "jobset_batch.yaml").resolve().parent
            assert entry.job.exists()

    def test_missing_file_raises_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError, match="Jobset file not found"):
            parse_jobset_yaml(FIXTURES / "jobset_nope.yaml")

    def test_empty_jobs_list_is_rejected(self) -> None:
        """`min_length=1`: a jobset with no members has nothing to pack."""
        with pytest.raises(ValueError, match="at least 1 item"):
            parse_jobset_yaml(FIXTURES / "jobset_empty.yaml")

    def test_unknown_entry_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            parse_jobset_yaml(FIXTURES / "jobset_bad_entry.yaml")

    def test_unknown_jobset_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            parse_jobset_yaml(FIXTURES / "jobset_bad_root.yaml")

    def test_single_job_root_is_rejected_with_the_right_command(self) -> None:
        """A job file fed to the jobset parser points at `generate`."""
        with pytest.raises(ValueError, match="single-job spec"):
            parse_jobset_yaml(FIXTURES / "jobset_job_root.yaml")

    def test_jobset_root_is_rejected_by_the_job_parser(self) -> None:
        """The reverse direction: a jobset fed to `parse_yaml`."""
        with pytest.raises(ValueError, match="--jobset"):
            parse_yaml(FIXTURES / "jobset_batch.yaml")

    def test_non_mapping_document_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="mapping"):
            parse_jobset_yaml(FIXTURES / "jobset_scalar_root.yaml")


class TestIsJobsetFile:
    """The cheap root-key probe used by the gate and the CLI."""

    def test_true_for_a_jobset(self) -> None:
        assert is_jobset_file(FIXTURES / "jobset_batch.yaml") is True

    def test_false_for_a_job(self) -> None:
        assert is_jobset_file(FIXTURES / "jobset_a.yaml") is False

    def test_false_for_a_missing_file(self) -> None:
        assert is_jobset_file(FIXTURES / "jobset_nope.yaml") is False

    def test_false_for_malformed_yaml(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.yaml"
        broken.write_text("jobset:\n  - job: [unclosed\n", encoding="utf-8")

        assert is_jobset_file(broken) is False


class TestJobsetId:
    """The jobset id: batch name, else the file stem."""

    def test_name_wins(self) -> None:
        spec = JobSetSpec(name="2026-11-17 CES batch", jobs=[JobSetEntry(job=Path("a.yaml"))])

        assert jobset_id_of(spec, Path("specs/batch.yaml")) == "2026_11_17_CES_batch"

    def test_file_stem_is_the_fallback(self) -> None:
        spec = JobSetSpec(jobs=[JobSetEntry(job=Path("a.yaml"))])

        assert jobset_id_of(spec, Path("specs/my batch.yaml")) == "my_batch"

    def test_sanitizes_windows_unsafe_characters(self) -> None:
        spec = JobSetSpec(name="a/b:c*d", jobs=[JobSetEntry(job=Path("a.yaml"))])

        assert jobset_id_of(spec, Path("batch.yaml")) == "a_b_c_d"

    def test_blank_name_falls_back_to_the_fallback_token(self) -> None:
        """A name that sanitizes to nothing cannot be a filename, and it is
        truthy, so the explicit fallback is what lands in the file names."""
        spec = JobSetSpec(name="   ", jobs=[JobSetEntry(job=Path("a.yaml"))])

        assert jobset_id_of(spec, Path("batch.yaml")) == "jobset"

    def test_empty_string_name_uses_the_stem(self) -> None:
        spec = JobSetSpec(name="", jobs=[JobSetEntry(job=Path("a.yaml"))])

        assert jobset_id_of(spec, Path("batch.yaml")) == "batch"


class TestCompositeIds:
    """The `{job_key}::{base_id}` id that carries provenance."""

    def test_round_trip(self) -> None:
        composite = make_composite_id("seer_2963", "case tags")

        assert split_composite_id(composite) == ("seer_2963", "case tags")

    def test_plain_id_has_no_owner(self) -> None:
        assert split_composite_id("plate1") == (None, "plate1")

    def test_first_separator_wins(self) -> None:
        """A base id containing `::` survives namespacing."""
        composite = make_composite_id("job_a", "weird::id")

        assert split_composite_id(composite) == ("job_a", "weird::id")

    def test_job_keys_can_never_contain_the_separator(self) -> None:
        """Job keys are sanitized names, so `{key}::id` is never ambiguous."""
        key = sanitize_job_id("a::b::c")

        assert ID_SEPARATOR not in key
        assert split_composite_id(make_composite_id(key, "label")) == (key, "label")


class TestLegendStyle:
    """Deterministic colour/dash assignment over the jobset's file order."""

    def test_first_ten_jobs_get_distinct_colors(self) -> None:
        colors = {legend_style_for(i)[0] for i in range(len(JOB_LEGEND_PALETTE))}

        assert len(colors) == len(JOB_LEGEND_PALETTE)

    def test_eleven_jobs_reuse_a_color_with_a_new_dash(self) -> None:
        color_a, style_a = legend_style_for(0)
        color_k, style_k = legend_style_for(len(JOB_LEGEND_PALETTE))

        assert (color_a, style_a) != (color_k, style_k)
        assert color_a == color_k
        assert style_k == JOB_LEGEND_STYLES[1]

    def test_is_periodic(self) -> None:
        period = len(JOB_LEGEND_PALETTE) * len(JOB_LEGEND_STYLES)

        assert legend_style_for(3) == legend_style_for(3 + period)
