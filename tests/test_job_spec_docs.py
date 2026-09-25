"""Drift tests for the generated AI-friendly job-spec docs (docs/schema/).

Guards the contract between the Pydantic models and the committed
artifacts so an AI consumer of ``docs/schema/`` can never be handed a
stale reference:

- ``job_spec.schema.json`` / ``job_config.schema.json`` / ``JOB_SPEC.md``
  must match a fresh render of the current models.
- Every schema field must carry a ``description`` (the generator renders
  field docs straight from ``Field(description=...)``).
- Every example job spec under ``examples/job_specs/`` must still parse.

Regenerate with::

    uv run python docs/schema/generate_ai_docs.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs" / "schema"


def _load_generator() -> Any:
    """Import ``docs/schema/generate_ai_docs.py`` as a module by path.

    The script is not part of the installed package, so it is loaded via
    ``importlib`` (its own ``sys.path`` bootstrap makes the
    ``plt_optimizer`` imports inside it work).

    Returns:
        The loaded generator module.
    """
    spec = importlib.util.spec_from_file_location(
        "generate_ai_docs", DOCS_DIR / "generate_ai_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = _load_generator()


def _assert_current(artifact: Path, rendered: str) -> None:
    """Fail with a regeneration hint when a committed artifact is stale.

    Args:
        artifact: Path to the committed artifact file.
        rendered: Freshly rendered expected content.

    Raises:
        AssertionError: If the file is missing or differs from ``rendered``.
    """
    assert artifact.exists(), (
        f"{artifact.name} is missing; run: uv run python docs/schema/generate_ai_docs.py"
    )
    assert artifact.read_text(encoding="utf-8") == rendered, (
        f"{artifact.name} is stale; run: uv run python docs/schema/generate_ai_docs.py"
    )


class TestGeneratedArtifacts:
    """Committed artifacts must byte-match a fresh render of the models."""

    def test_job_spec_schema_json_is_up_to_date(self) -> None:
        """job_spec.schema.json matches JobSpec.model_json_schema()."""
        _assert_current(DOCS_DIR / "job_spec.schema.json", generator.build_job_spec_schema())

    def test_job_config_schema_json_is_up_to_date(self) -> None:
        """job_config.schema.json matches JobDefaults.model_json_schema()."""
        _assert_current(DOCS_DIR / "job_config.schema.json", generator.build_job_config_schema())

    def test_job_spec_markdown_is_up_to_date(self) -> None:
        """JOB_SPEC.md matches the generated markdown render."""
        _assert_current(DOCS_DIR / "JOB_SPEC.md", generator.build_job_spec_markdown())

    def test_fallback_constants_are_scraped_live(self) -> None:
        """The fallback table carries the real current constant values.

        Guards the ``layout.py`` regex scrape specifically: a renamed or
        retyped constant must surface here rather than silently dropping
        out of the docs.
        """
        from plt_optimizer.generate import layout, resolution

        constants = generator._scrape_constants()
        for name in dir(resolution):
            if name.startswith("DEFAULT_"):
                assert constants[name] == repr(getattr(resolution, name))
        assert constants["DEFAULT_PLATE_WIDTH"] == repr(layout.DEFAULT_PLATE_WIDTH)
        assert constants["DEFAULT_PLATE_HEIGHT"] == repr(layout.DEFAULT_PLATE_HEIGHT)


class TestSchemaSelfDescribing:
    """The JSON schema must stay self-describing for AI consumers."""

    @pytest.mark.parametrize(
        ("schema_text", "model_name"),
        [
            (generator.build_job_spec_schema(), "JobSpec"),
            (generator.build_job_config_schema(), "JobDefaults"),
        ],
    )
    def test_every_field_has_a_description(self, schema_text: str, model_name: str) -> None:
        """Every property in every model/enum node carries a description.

        Args:
            schema_text: The generated JSON Schema text.
            model_name: Model name, for assertion messages.
        """
        schema = json.loads(schema_text)
        nodes = {model_name: schema, **schema.get("$defs", {})}
        missing = [
            f"{model}.{field_name}"
            for model, node in nodes.items()
            for field_name, field_schema in node.get("properties", {}).items()
            if not field_schema.get("description")
        ]
        assert not missing, f"fields missing Field(description=...): {missing}"


class TestExampleSpecsParse:
    """Every checked-in example job spec must parse against the schema."""

    @pytest.mark.parametrize(
        "spec_path",
        sorted((REPO_ROOT / "examples" / "job_specs").glob("*.yaml")),
        ids=lambda path: path.name,
    )
    def test_example_parses(self, spec_path: Path) -> None:
        """examples/job_specs/*.yaml validate through parse_yaml().

        Args:
            spec_path: The example YAML file (parametrized).
        """
        from plt_optimizer.generate.schema import parse_yaml

        # No job-config path: examples keep the historical all-optional
        # contract (the required-when-unconfigured gate is exercised in
        # test_job_config.py).
        job = parse_yaml(spec_path)
        assert job.job_name

    @pytest.mark.parametrize(
        "spec_path",
        sorted((REPO_ROOT / "examples" / "job_specs").glob("*.yaml")),
        ids=lambda path: path.name,
    )
    def test_example_validates_against_committed_json_schema(self, spec_path: Path) -> None:
        """examples/job_specs/*.yaml conform to the committed JSON Schema.

        Exercises the artifact as a *machine-usable* contract (the workflow
        documented in docs/schema/README.md), catching e.g. broken ``$ref``
        pointers that ``parse_yaml`` alone would not surface.

        Args:
            spec_path: The example YAML file (parametrized).
        """
        import jsonschema
        import yaml

        schema = json.loads((DOCS_DIR / "job_spec.schema.json").read_text(encoding="utf-8"))
        raw = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        jsonschema.validate(raw["job"], schema)
