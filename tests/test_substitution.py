"""Tests for replacement text file expansion (EngraveLab/Vision Pro style).

Covers:
- Schema validation for the new ``replacement_text_file`` /
  ``replacement_text_delimiter`` fields on ``LabelSpec``.
- ``load_replacement_file`` parsing (delimiters, line endings, blank lines).
- ``_synthesize_content`` merge semantics (fewer/equal/more items, template
  attribute inheritance).
- ``expand_label_with_replacements`` / ``expand_job_spec`` orchestration.
- End-to-end resolution of an expanded job (counts + per-line attributes).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import JobSpec, LabelSpec, TextLine, parse_yaml
from plt_optimizer.generate.substitution import (
    DEFAULT_REPLACEMENT_DELIMITER,
    SubstitutionError,
    expand_job_spec,
    expand_label_with_replacements,
    load_replacement_file,
)


def _write(path: Path, text: str) -> Path:
    """Write ``text`` to ``path`` and return the path."""
    path.write_text(text, encoding="utf-8")
    return path


class TestLabelSpecReplacementValidation:
    """Schema-level validation of the new replacement fields."""

    def test_replacement_file_without_content_is_valid(self) -> None:
        """A template label may omit ``content`` when using a replacement file."""
        label = LabelSpec(id="tmpl", replacement_text_file="data.txt")
        assert label.content is None
        assert label.replacement_text_file == "data.txt"
        assert label.replacement_text_delimiter is None

    def test_replacement_file_with_content_is_valid(self) -> None:
        """A template label may declare ``content`` as attribute placeholders."""
        label = LabelSpec(
            id="tmpl",
            replacement_text_file="data.txt",
            content=[TextLine(text="PLACEHOLDER", text_height=0.5)],
        )
        assert label.content is not None
        assert len(label.content) == 1

    def test_count_and_replacement_file_mutually_exclusive(self) -> None:
        """Explicit ``count`` plus a replacement file must be rejected."""
        with pytest.raises(ValidationError, match="count.*cannot be combined"):
            LabelSpec(id="tmpl", count=5, replacement_text_file="data.txt")

    def test_explicit_count_one_with_file_still_rejected(self) -> None:
        """Even an explicit ``count: 1`` conflicts with a replacement file."""
        with pytest.raises(ValidationError, match="count.*cannot be combined"):
            LabelSpec(id="tmpl", count=1, replacement_text_file="data.txt")

    def test_implicit_count_one_with_file_is_allowed(self) -> None:
        """An omitted ``count`` (defaulting to 1) does not conflict."""
        label = LabelSpec(id="tmpl", replacement_text_file="data.txt")
        assert label.count == 1

    def test_neither_content_nor_file_rejected(self) -> None:
        """A label must define content or a replacement file."""
        with pytest.raises(ValidationError, match="must define either"):
            LabelSpec(id="tmpl")

    def test_empty_content_still_rejected(self) -> None:
        """An empty ``content`` list remains invalid."""
        with pytest.raises(ValidationError, match="at least one TextLine"):
            LabelSpec(id="tmpl", content=[])

    def test_delimiter_without_file_rejected(self) -> None:
        """A delimiter is meaningless without a replacement file."""
        with pytest.raises(ValidationError, match="requires 'replacement_text_file'"):
            LabelSpec(
                id="tmpl",
                content=[TextLine(text="X")],
                replacement_text_delimiter=",",
            )

    @pytest.mark.parametrize("delimiter", [";", ",", " ", "\t", "$", "|", "*", "#"])
    def test_valid_single_char_delimiters(self, delimiter: str) -> None:
        """Single special/whitespace delimiters are accepted."""
        label = LabelSpec(
            id="tmpl",
            replacement_text_file="data.txt",
            replacement_text_delimiter=delimiter,
        )
        assert label.replacement_text_delimiter == delimiter

    @pytest.mark.parametrize("delimiter", ["", "ab", "\n", "\r", "a", "Z", "9"])
    def test_invalid_delimiters_rejected(self, delimiter: str) -> None:
        """Empty, multi-char, newline, and alphanumeric delimiters are rejected."""
        with pytest.raises(ValidationError):
            LabelSpec(
                id="tmpl",
                replacement_text_file="data.txt",
                replacement_text_delimiter=delimiter,
            )


class TestLoadReplacementFile:
    """Parsing of replacement text files."""

    def test_default_delimiter(self, tmp_path: Path) -> None:
        """Semicolon is the default delimiter."""
        path = _write(tmp_path / "r.txt", "A;B;C\nD;E\n")
        assert load_replacement_file(path, DEFAULT_REPLACEMENT_DELIMITER) == [
            ["A", "B", "C"],
            ["D", "E"],
        ]

    def test_custom_delimiter(self, tmp_path: Path) -> None:
        """A custom delimiter splits correctly."""
        path = _write(tmp_path / "r.txt", "A,B\nC,D,E\n")
        assert load_replacement_file(path, ",") == [["A", "B"], ["C", "D", "E"]]

    def test_trailing_newline_no_phantom_instance(self, tmp_path: Path) -> None:
        """A single trailing newline does not add an empty instance."""
        path = _write(tmp_path / "r.txt", "A\nB\n")
        assert load_replacement_file(path, ";") == [["A"], ["B"]]

    def test_crlf_line_endings(self, tmp_path: Path) -> None:
        """Windows CRLF endings are normalized."""
        path = _write(tmp_path / "r.txt", "A;B\r\nC;D\r\n")
        assert load_replacement_file(path, ";") == [["A", "B"], ["C", "D"]]

    def test_cr_only_line_endings(self, tmp_path: Path) -> None:
        """Classic-Mac CR-only endings are normalized."""
        path = _write(tmp_path / "r.txt", "A;B\rC;D")
        assert load_replacement_file(path, ";") == [["A", "B"], ["C", "D"]]

    def test_items_preserved_verbatim(self, tmp_path: Path) -> None:
        """Whitespace inside items is preserved (no stripping)."""
        path = _write(tmp_path / "r.txt", "  SPACED  ;END\n")
        assert load_replacement_file(path, ";") == [["  SPACED  ", "END"]]

    def test_blank_line_becomes_empty_item(self, tmp_path: Path) -> None:
        """A blank line yields a single empty item (renders blank)."""
        path = _write(tmp_path / "r.txt", "A\n\nB\n")
        assert load_replacement_file(path, ";") == [["A"], [""], ["B"]]

    def test_space_delimiter(self, tmp_path: Path) -> None:
        """Space is a valid delimiter splitting on every space."""
        path = _write(tmp_path / "r.txt", "HELLO WORLD\n")
        assert load_replacement_file(path, " ") == [["HELLO", "WORLD"]]

    def test_utf8_bom_stripped(self, tmp_path: Path) -> None:
        """A leading UTF-8 BOM is stripped from the first item."""
        path = tmp_path / "r.txt"
        path.write_bytes("\ufeffA;B\n".encode("utf-8"))
        assert load_replacement_file(path, ";") == [["A", "B"]]

    def test_unicode_content(self, tmp_path: Path) -> None:
        """Non-ASCII text is preserved."""
        path = _write(tmp_path / "r.txt", "CAFÉ;NAÏVE\n")
        assert load_replacement_file(path, ";") == [["CAFÉ", "NAÏVE"]]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A nonexistent file raises SubstitutionError."""
        with pytest.raises(SubstitutionError, match="not found"):
            load_replacement_file(tmp_path / "nope.txt", ";")

    def test_directory_raises(self, tmp_path: Path) -> None:
        """A directory path raises SubstitutionError."""
        (tmp_path / "adir").mkdir()
        with pytest.raises(SubstitutionError, match="not found"):
            load_replacement_file(tmp_path / "adir", ";")

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        """An empty file raises SubstitutionError."""
        path = _write(tmp_path / "r.txt", "")
        with pytest.raises(SubstitutionError, match="empty"):
            load_replacement_file(path, ";")

    def test_only_newline_raises(self, tmp_path: Path) -> None:
        """A file with only a trailing newline is treated as empty."""
        path = _write(tmp_path / "r.txt", "\n")
        with pytest.raises(SubstitutionError, match="empty"):
            load_replacement_file(path, ";")

    def test_invalid_utf8_raises(self, tmp_path: Path) -> None:
        """A file with invalid UTF-8 bytes raises SubstitutionError."""
        path = tmp_path / "r.txt"
        path.write_bytes(b"A;B\n\xff\xfe\xfd\n")
        with pytest.raises(SubstitutionError, match="not valid UTF-8"):
            load_replacement_file(path, ";")

    def test_unreadable_file_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An OS-level read failure raises SubstitutionError."""
        path = _write(tmp_path / "r.txt", "A\n")

        def boom(self: Path, *args: object, **kwargs: object) -> str:
            raise OSError("simulated I/O failure")

        monkeypatch.setattr(Path, "read_text", boom)
        with pytest.raises(SubstitutionError, match="Could not read"):
            load_replacement_file(path, ";")

    def test_substitution_error_is_value_error(self) -> None:
        """SubstitutionError remains a ValueError for existing guards."""
        assert issubclass(SubstitutionError, ValueError)


class TestExpandLabelWithReplacements:
    """Per-label expansion semantics."""

    def test_no_replacement_file_returns_same_label(self, tmp_path: Path) -> None:
        """Static labels pass through unchanged (identity)."""
        label = LabelSpec(id="s", count=3, content=[TextLine(text="X")])
        result = expand_label_with_replacements(label, tmp_path)
        assert result == [label]

    def test_one_label_per_file_line(self, tmp_path: Path) -> None:
        """Each file line becomes one label with count=1 and a suffixed id."""
        _write(tmp_path / "r.txt", "A\nB\nC\n")
        label = LabelSpec(id="tmpl", replacement_text_file="r.txt")
        result = expand_label_with_replacements(label, tmp_path)
        assert [lbl.id for lbl in result] == ["tmpl_0000", "tmpl_0001", "tmpl_0002"]
        assert all(lbl.count == 1 for lbl in result)

    def test_ids_are_zero_padded_beyond_nine(self, tmp_path: Path) -> None:
        """Suffixes are zero-padded to four digits past nine instances."""
        _write(tmp_path / "r.txt", "\n".join(f"L{i}" for i in range(12)) + "\n")
        label = LabelSpec(id="t", replacement_text_file="r.txt")
        result = expand_label_with_replacements(label, tmp_path)
        assert result[9].id == "t_0009"
        assert result[10].id == "t_0010"

    def test_replacement_fields_cleared_on_expansion(self, tmp_path: Path) -> None:
        """Expanded labels no longer reference the replacement file."""
        _write(tmp_path / "r.txt", "A\n")
        label = LabelSpec(id="t", replacement_text_file="r.txt", replacement_text_delimiter=",")
        result = expand_label_with_replacements(label, tmp_path)
        assert result[0].replacement_text_file is None
        assert result[0].replacement_text_delimiter is None

    def test_no_content_all_lines_inherit_label_attrs(self, tmp_path: Path) -> None:
        """Without template content, lines carry no per-line overrides (label cascade)."""
        _write(tmp_path / "r.txt", "A;B;C\n")
        label = LabelSpec(id="t", text_height=0.5, replacement_text_file="r.txt")
        (expanded,) = expand_label_with_replacements(label, tmp_path)
        assert expanded.content is not None
        assert [line.text for line in expanded.content] == ["A", "B", "C"]
        # No per-line override -> resolution cascades label text_height.
        assert all(line.text_height is None for line in expanded.content)

    def test_fewer_items_renders_fewer_lines(self, tmp_path: Path) -> None:
        """Fewer items than template lines drops the extra template lines."""
        _write(tmp_path / "r.txt", "A\n")
        label = LabelSpec(
            id="t",
            replacement_text_file="r.txt",
            content=[TextLine(text="P1"), TextLine(text="P2"), TextLine(text="P3")],
        )
        (expanded,) = expand_label_with_replacements(label, tmp_path)
        assert expanded.content is not None
        assert [line.text for line in expanded.content] == ["A"]

    def test_more_items_inherit_label_attrs(self, tmp_path: Path) -> None:
        """Extra items beyond template lines get label-level attributes."""
        _write(tmp_path / "r.txt", "A;B;C\n")
        label = LabelSpec(
            id="t",
            replacement_text_file="r.txt",
            content=[TextLine(text="P1", text_height=0.7)],
        )
        (expanded,) = expand_label_with_replacements(label, tmp_path)
        assert expanded.content is not None
        assert [line.text for line in expanded.content] == ["A", "B", "C"]
        # First line keeps the template override; extras have none (label cascade).
        assert expanded.content[0].text_height == 0.7
        assert expanded.content[1].text_height is None
        assert expanded.content[2].text_height is None

    def test_template_attributes_preserved_per_line(self, tmp_path: Path) -> None:
        """Placeholder lines keep every attribute except the replaced text."""
        _write(tmp_path / "r.txt", "REAL\n")
        label = LabelSpec(
            id="t",
            replacement_text_file="r.txt",
            content=[TextLine(text="PLACEHOLDER", text_height=0.6, text_h_alignment="left")],
        )
        (expanded,) = expand_label_with_replacements(label, tmp_path)
        assert expanded.content is not None
        line = expanded.content[0]
        assert line.text == "REAL"
        assert line.text_height == 0.6
        assert line.text_h_alignment is not None
        assert line.text_h_alignment.value == "left"

    def test_custom_delimiter_used(self, tmp_path: Path) -> None:
        """A custom delimiter splits the file lines."""
        _write(tmp_path / "r.txt", "A|B\n")
        label = LabelSpec(id="t", replacement_text_file="r.txt", replacement_text_delimiter="|")
        (expanded,) = expand_label_with_replacements(label, tmp_path)
        assert expanded.content is not None
        assert [line.text for line in expanded.content] == ["A", "B"]

    def test_relative_path_resolved_against_base_dir(self, tmp_path: Path) -> None:
        """A relative replacement path resolves against the job YAML directory."""
        sub = tmp_path / "data"
        sub.mkdir()
        _write(sub / "r.txt", "A\n")
        label = LabelSpec(id="t", replacement_text_file="data/r.txt")
        (expanded,) = expand_label_with_replacements(label, tmp_path)
        assert expanded.content is not None and expanded.content[0].text == "A"

    def test_absolute_path_accepted(self, tmp_path: Path) -> None:
        """An absolute replacement path is used as-is."""
        path = _write(tmp_path / "r.txt", "A\n")
        label = LabelSpec(id="t", replacement_text_file=str(path))
        (expanded,) = expand_label_with_replacements(label, Path("/nonexistent"))
        assert expanded.content is not None and expanded.content[0].text == "A"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A missing replacement file raises SubstitutionError."""
        label = LabelSpec(id="t", replacement_text_file="nope.txt")
        with pytest.raises(SubstitutionError, match="not found"):
            expand_label_with_replacements(label, tmp_path)

    def test_other_fields_preserved(self, tmp_path: Path) -> None:
        """Dimensions, holes, and margins carry over to every instance."""
        _write(tmp_path / "r.txt", "A\nB\n")
        label = LabelSpec(
            id="t",
            replacement_text_file="r.txt",
            width=3.0,
            height=1.0,
            margin=0.2,
            text_height=0.4,
        )
        result = expand_label_with_replacements(label, tmp_path)
        assert all(lbl.width == 3.0 for lbl in result)
        assert all(lbl.height == 1.0 for lbl in result)
        assert all(lbl.margin == 0.2 for lbl in result)
        assert all(lbl.text_height == 0.4 for lbl in result)


class TestExpandJobSpec:
    """Job-level orchestration."""

    def _job_yaml(self, tmp_path: Path, replacement_body: str) -> Path:
        """Write a job YAML with one static and one template label."""
        _write(tmp_path / "r.txt", "A\nB\n")
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text(
            "job:\n"
            "  job_name: Test Job\n"
            "  text_height: 0.3\n"
            "  labels:\n"
            "    - id: static\n"
            "      count: 2\n"
            "      content:\n"
            "        - text: HELLO\n"
            f"{replacement_body}",
            encoding="utf-8",
        )
        return yaml_path

    def test_expands_template_and_keeps_static(self, tmp_path: Path) -> None:
        """Static labels are untouched; template labels expand in place."""
        yaml_path = self._job_yaml(
            tmp_path,
            "    - id: tmpl\n"
            "      replacement_text_file: r.txt\n"
            "      content:\n"
            "        - text: P1\n",
        )
        job = expand_job_spec(parse_yaml(yaml_path), yaml_path)
        assert job.labels is not None
        ids = [lbl.id for lbl in job.labels]
        assert ids == ["static", "tmpl_0000", "tmpl_0001"]
        static = job.labels[0]
        assert static.count == 2

    def test_no_replacement_returns_same_job(self, tmp_path: Path) -> None:
        """A job with no replacement labels is returned unchanged (identity)."""
        yaml_path = self._job_yaml(tmp_path, "")
        job = parse_yaml(yaml_path)
        assert expand_job_spec(job, yaml_path) is job

    def test_root_level_job_returned_unchanged(self, tmp_path: Path) -> None:
        """Root-level single-label jobs skip expansion entirely."""
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text(
            "job:\n  job_name: Root\n  count: 3\n  content:\n    - text: X\n",
            encoding="utf-8",
        )
        job = parse_yaml(yaml_path)
        assert expand_job_spec(job, yaml_path) is job

    def test_expansion_preserves_job_level_fields(self, tmp_path: Path) -> None:
        """Non-label job fields (name, plates, defaults) survive expansion."""
        yaml_path = self._job_yaml(
            tmp_path,
            "    - id: tmpl\n      replacement_text_file: r.txt\n",
        )
        job = expand_job_spec(parse_yaml(yaml_path), yaml_path)
        assert job.job_name == "Test Job"
        assert job.text_height == 0.3

    def test_missing_file_propagates_error(self, tmp_path: Path) -> None:
        """A template pointing at a missing file aborts job expansion."""
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text(
            "job:\n  job_name: J\n  labels:\n"
            "    - id: t\n      replacement_text_file: missing.txt\n",
            encoding="utf-8",
        )
        with pytest.raises(SubstitutionError, match="not found"):
            expand_job_spec(parse_yaml(yaml_path), yaml_path)


class TestResolutionAfterExpansion:
    """End-to-end resolution of an expanded job."""

    def test_resolved_counts_and_texts(self, tmp_path: Path) -> None:
        """Expanded labels resolve to one instance each with correct text."""
        _write(tmp_path / "r.txt", "ALPHA;ONE\nBETA\n")
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text(
            "job:\n"
            "  job_name: R\n"
            "  text_height: 0.3\n"
            "  labels:\n"
            "    - id: t\n"
            "      width: 4.0\n"
            "      height: 1.0\n"
            "      replacement_text_file: r.txt\n"
            "      content:\n"
            "        - text: P1\n"
            "          text_height: 0.6\n"
            "        - text: P2\n",
            encoding="utf-8",
        )
        job = expand_job_spec(parse_yaml(yaml_path), yaml_path)
        resolved = resolve_job_spec(job)
        assert [lbl.id for lbl in resolved] == ["t_0000", "t_0001"]
        assert [lbl.count for lbl in resolved] == [1, 1]

        # First instance: two lines, first uses the 0.6 override.
        first = resolved[0]
        assert [line.text for line in first.content] == ["ALPHA", "ONE"]
        assert first.content[0].nominal_text_height == 0.6
        assert first.content[1].nominal_text_height == 0.3  # job default

        # Second instance: one item -> one line.
        second = resolved[1]
        assert [line.text for line in second.content] == ["BETA"]
        assert second.content[0].nominal_text_height == 0.6

    def test_no_content_resolves_with_label_attrs(self, tmp_path: Path) -> None:
        """Without template content, all lines resolve to the label text_height."""
        _write(tmp_path / "r.txt", "X;Y\n")
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text(
            "job:\n"
            "  job_name: R\n"
            "  labels:\n"
            "    - id: t\n"
            "      width: 4.0\n"
            "      height: 1.0\n"
            "      text_height: 0.5\n"
            "      replacement_text_file: r.txt\n",
            encoding="utf-8",
        )
        job = expand_job_spec(parse_yaml(yaml_path), yaml_path)
        (resolved,) = resolve_job_spec(job)
        assert [line.text for line in resolved.content] == ["X", "Y"]
        assert all(line.nominal_text_height == 0.5 for line in resolved.content)


class TestJobSpecModelCopy:
    """The JobSpec copy returned by expansion is a valid model."""

    def test_expanded_job_is_valid_jobspec(self, tmp_path: Path) -> None:
        """The returned object re-validates as a proper JobSpec."""
        _write(tmp_path / "r.txt", "A\n")
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text(
            "job:\n  job_name: J\n  labels:\n    - id: t\n      replacement_text_file: r.txt\n",
            encoding="utf-8",
        )
        job = expand_job_spec(parse_yaml(yaml_path), yaml_path)
        assert isinstance(job, JobSpec)
        # Re-validating the model dump must succeed.
        JobSpec(**job.model_dump())


class TestEndToEndPipeline:
    """Full parse -> expand -> resolve -> export pipeline."""

    def test_export_replacement_driven_job(self, tmp_path: Path) -> None:
        """A replacement-driven job exports PLT files with per-instance text."""
        from plt_optimizer.generate.vectorize import export_per_cutter_plts

        _write(tmp_path / "r.txt", "ALPHA;ONE\nBETA;TWO;EXTRA\nGAMMA\n")
        yaml_path = tmp_path / "job.yaml"
        yaml_path.write_text(
            "job:\n"
            "  job_name: E2E\n"
            "  text_height: 0.3\n"
            "  margin: 0.15\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 16.0\n"
            "      clearance_padding: 0.125\n"
            "  labels:\n"
            "    - id: badge\n"
            "      width: 3.0\n"
            "      height: 1.0\n"
            "      replacement_text_file: r.txt\n",
            encoding="utf-8",
        )
        job = expand_job_spec(parse_yaml(yaml_path), yaml_path)
        resolved = resolve_job_spec(job)
        assert len(resolved) == 3
        assert [len(lbl.content) for lbl in resolved] == [2, 3, 1]

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        result = export_per_cutter_plts(
            resolved,
            job.plates,
            output_dir=output_dir,
            optimize=False,
            job_id="e2e",
            plots=False,
        )
        exported = result.plt_paths
        # Borders + one text cutter file for the single 0.3in text height.
        assert len(exported) == 2
        assert all(p.parent == output_dir / "plt" for p in exported)
        content = exported[0].read_text(encoding="utf-8")
        assert content.startswith("IN;") or "IN;" in content[:200]
        # Every instance must contribute geometry (3 badges packed).
        assert "PU" in content and "PD" in content
