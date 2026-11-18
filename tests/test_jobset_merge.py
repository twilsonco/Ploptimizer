"""Tests for the jobset merge: namespacing, the gate and the packing pool.

``tests/test_jobset_schema.py`` pins the file format; this module pins what
the merge guarantees — composite ids, plate pooling, provenance lookups, the
gate's errors and warnings, and determinism (the same input must produce the
same plan, because the plotter output has to be reproducible).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plt_optimizer.generate.job_prep import JobPreparationError, prepare_job
from plt_optimizer.generate.jobset import (
    JobProvenance,
    JobRecord,
    JobSetError,
    build_jobset_plan,
    evaluate_jobset_gate,
    namespace_job,
)
from plt_optimizer.generate.schema import LayoutMode

FIXTURES = Path("tests_deps")
BATCH = FIXTURES / "jobset_batch.yaml"


def _plan(name: str):  # type: ignore[no-untyped-def]
    """Build a plan from a jobset fixture with the ideal-cutter fallback."""
    return build_jobset_plan(
        FIXTURES / name,
        job_config=None,
        job_config_path=None,
        inventory=None,
        boundary_hole_cutter=None,
    )


def _context(name: str, key: str = "job"):  # type: ignore[no-untyped-def]
    """Prepare + namespace one member fixture for direct gate calls."""
    prepared = prepare_job(FIXTURES / name, job_config=None)
    return namespace_job(key, prepared)


class TestCompositeIds:
    """Every id the merged pipeline carries is namespaced by job key."""

    def test_label_ids_are_namespaced_in_file_order(self) -> None:
        plan = _plan("jobset_batch.yaml")

        assert [label.id for label in plan.merged_labels] == [
            "Jobset_Alpha::alpha-main",
            "Jobset_Alpha::alpha-pinned",
            "Jobset_Beta::alpha-main",
            "Jobset_Beta::beta-pinned",
        ]

    def test_plate_ids_are_namespaced_in_file_order(self) -> None:
        plan = _plan("jobset_batch.yaml")

        assert plan.merged_plates is not None
        assert [plate.id for plate in plan.merged_plates] == [
            "Jobset_Alpha::plate1",
            "Jobset_Beta::plate1",
        ]

    def test_plate_pin_follows_the_plate_into_the_namespace(self) -> None:
        """A member's `plate_id` cannot name a composite plate, so the merge
        rewrites the pin alongside the plate id."""
        plan = _plan("jobset_batch.yaml")

        pinned = [label for label in plan.merged_labels if label.id.endswith("alpha-pinned")]
        assert pinned[0].plate_id == "Jobset_Alpha::plate1"

    def test_member_spec_keeps_its_own_ids(self) -> None:
        """The member's report prints the ids its own file declares."""
        plan = _plan("jobset_batch.yaml")

        assert [label.id for label in plan.contexts[1].spec.labels or []] == [
            "alpha-main",
            "beta-pinned",
        ]

    def test_colliding_label_ids_stay_distinct(self) -> None:
        """`alpha-main` exists in both member files by design."""
        plan = _plan("jobset_batch.yaml")
        ids = [label.id for label in plan.merged_labels]

        assert len(ids) == len(set(ids))
        assert sum(1 for label_id in ids if label_id.endswith("::alpha-main")) == 2

    def test_provenance_resolves_label_and_plate_owners(self) -> None:
        provenance = _plan("jobset_batch.yaml").provenance

        assert provenance.job_of_label("Jobset_Beta::alpha-main") == "Jobset_Beta"
        assert provenance.job_of_plate("Jobset_Alpha::plate1") == "Jobset_Alpha"

    def test_provenance_infers_the_owner_from_a_composite_prefix(self) -> None:
        """A packed plate is keyed by its own id, so a lookup that arrives
        with only a composite id (no table entry) still resolves."""
        provenance = _plan("jobset_batch.yaml").provenance

        assert provenance.job_of_label("Jobset_Beta::brand-new-label") == "Jobset_Beta"
        assert provenance.job_of_plate("default_plate_1") is None

    def test_base_id_returns_the_member_declared_id(self) -> None:
        provenance = _plan("jobset_batch.yaml").provenance

        assert provenance.base_id("Jobset_Beta::alpha-main") == "alpha-main"
        assert provenance.base_id("alpha-main") == "alpha-main"

    def test_owners_of_is_in_jobset_file_order(self) -> None:
        provenance = _plan("jobset_batch.yaml").provenance

        owners = provenance.owners_of(["Jobset_Beta::beta-pinned", "Jobset_Alpha::alpha-main"])

        assert owners == ("Jobset_Alpha", "Jobset_Beta")
        assert provenance.is_mixed(["Jobset_Alpha::x", "Jobset_Beta::y"]) is True
        assert provenance.is_mixed(["Jobset_Alpha::x", "Jobset_Alpha::y"]) is False

    def test_stem_token_is_the_job_on_a_one_job_plate(self) -> None:
        """Single-job plates keep exactly the names a standalone run makes —
        the operator's existing plate files must not change shape."""
        provenance = _plan("jobset_batch.yaml").provenance

        assert provenance.stem_token_for_owners(["Jobset_Alpha"]) == "Jobset_Alpha"

    def test_stem_token_is_the_jobset_on_a_mixed_plate(self) -> None:
        provenance = _plan("jobset_batch.yaml").provenance

        assert provenance.stem_token_for_owners(["Jobset_Alpha", "Jobset_Beta"]) == "Jobset_Batch"


class TestPackingPool:
    """The union of the members' plates is the pool; layout is shared."""

    def test_plan_uses_the_shared_layout(self) -> None:
        plan = _plan("jobset_batch.yaml")

        assert plan.layout is LayoutMode.COLUMNS

    def test_unbounded_members_share_one_allocation_pool(self) -> None:
        plan = _plan("jobset_unbounded_batch.yaml")

        assert plan.merged_plates is None
        assert plan.provenance.plate_jobs == {}
        assert [label.id for label in plan.merged_labels] == [
            "Jobset_Unbounded_A::unbounded-a",
            "Jobset_Unbounded_B::unbounded-b",
        ]

    def test_pooled_geometry_is_reported_as_a_warning(self) -> None:
        """The two members declare the same 24x16 kw sheets: that IS the
        material-utilisation win, and it must be visible in the reports."""
        plan = _plan("jobset_batch.yaml")

        assert any("24x16in" in warning and "one pool" in warning for warning in plan.warnings)

    def test_build_is_deterministic(self) -> None:
        """Identical input must yield an identical plan (the plotter runs the
        same file twice and expects the same engraving)."""
        first = _plan("jobset_batch.yaml")
        second = _plan("jobset_batch.yaml")

        assert [label.id for label in first.merged_labels] == [
            label.id for label in second.merged_labels
        ]
        assert [plate.id for plate in first.merged_plates or []] == [
            plate.id for plate in second.merged_plates or []
        ]
        assert first.warnings == second.warnings


class TestJobKeyUniqueness:
    """Two members whose names sanitize alike must not share a key."""

    def test_second_member_gets_a_numeric_suffix(self) -> None:
        plan = _plan("jobset_key_collision.yaml")

        assert [ctx.job_key for ctx in plan.contexts] == ["Jobset_Alpha", "Jobset_Alpha_2"]

    def test_collision_warns(self) -> None:
        plan = _plan("jobset_key_collision.yaml")

        assert any("job key collision" in warning for warning in plan.warnings)

    def test_composite_ids_stay_unique_after_a_collision(self) -> None:
        plan = _plan("jobset_key_collision.yaml")
        ids = [label.id for label in plan.merged_labels]

        assert len(ids) == len(set(ids))


class TestGateErrors:
    """Hard failures: these change geometry if merged silently."""

    def test_layout_conflict(self) -> None:
        with pytest.raises(JobSetError, match="layout conflict"):
            _plan("jobset_layout_conflict.yaml")

    def test_nested_jobset(self) -> None:
        with pytest.raises(JobSetError, match="itself a jobset"):
            _plan("jobset_nested.yaml")

    def test_missing_member_file(self) -> None:
        with pytest.raises(JobSetError, match="job file not found"):
            _plan("jobset_missing_member.yaml")

    def test_duplicate_member_file(self) -> None:
        with pytest.raises(JobSetError, match="duplicate job"):
            _plan("jobset_duplicate_member.yaml")

    def test_duplicate_plate_id_inside_one_member(self) -> None:
        with pytest.raises(JobSetError, match="more than once"):
            _plan("jobset_dup_plate_batch.yaml")

    def test_file_errors_are_not_padded_with_a_gate_message(self) -> None:
        """Every unresolvable member is reported by name, once."""
        with pytest.raises(JobSetError) as excinfo:
            _plan("jobset_missing_member.yaml")

        assert len(excinfo.value.errors) == 1

    def test_all_errors_are_reported_together(self) -> None:
        """A batch with two broken members must name both, so the user fixes
        the file in one pass - and nothing else."""
        with pytest.raises(JobSetError) as excinfo:
            _plan("jobset_two_bad_members.yaml")

        errors = excinfo.value.errors
        assert len(errors) == 2
        assert any("jobset_does_not_exist.yaml" in error for error in errors)
        assert any("jobset_also_missing.yaml" in error for error in errors)

    def test_member_failure_names_the_member(self) -> None:
        """A member that cannot resolve tags the stage with its own key, so
        the root report says which job broke."""
        with pytest.raises(JobPreparationError) as excinfo:
            _plan("jobset_broken_member.yaml")

        assert excinfo.value.stage == "parse[jobset_bad_label]"


class TestGateWarnings:
    """Pool-wide settings: the first member wins, and the reports say so."""

    def test_divergent_allow_rotation(self) -> None:
        plan = _plan("jobset_rotation_split.yaml")

        assert plan.allow_rotation is True  # jobset_a.yaml, the first member
        assert any(
            "allow_rotation differs" in warning
            and "Jobset Alpha=True" in warning
            and "Jobset Delta=False" in warning
            and "'Jobset Alpha' wins" in warning
            for warning in plan.warnings
        )

    def test_gate_on_prepared_contexts_directly(self) -> None:
        """The gate is a pure function of the member contexts - the CLI's
        failure path and the validation script both call it this way."""
        contexts = [_context("jobset_a.yaml", "a"), _context("jobset_rows.yaml", "b")]

        errors, warnings = evaluate_jobset_gate(contexts)

        assert any("layout conflict" in error for error in errors)
        assert contexts[0].layout is LayoutMode.COLUMNS
        assert contexts[1].layout is LayoutMode.ROWS

    def test_empty_context_list_is_an_error(self) -> None:
        errors, _ = evaluate_jobset_gate([])

        assert errors == ["A jobset must contain at least one job"]

    def test_duplicate_plate_id_is_an_error(self) -> None:
        ctx = _context("jobset_dup_plate.yaml", "dup")

        errors, _ = evaluate_jobset_gate([ctx])

        assert any("plate id 'dup::plate1' more than once" in error for error in errors)


class TestProvenanceHelpers:
    """The side table the export and report layers read."""

    def test_record_lookup(self) -> None:
        provenance = _plan("jobset_batch.yaml").provenance

        record = provenance.record("Jobset_Beta")

        assert record is not None
        assert record.job_name == "Jobset Beta"
        assert record.display_name == "Jobset Beta"
        assert provenance.record("nope") is None

    def test_job_keys_follow_file_order(self) -> None:
        assert _plan("jobset_batch.yaml").provenance.job_keys == ("Jobset_Alpha", "Jobset_Beta")

    def test_display_name_falls_back_to_the_id(self) -> None:
        assert JobRecord("k", "job_id", "", Path("j.yaml")).display_name == "job_id"

    def test_owners_of_unknown_ids(self) -> None:
        provenance = JobProvenance(jobset_id="batch", jobs=(JobRecord("a", "a", "A", Path()),))

        assert provenance.owners_of(["plain-id"]) == ()
        assert provenance.job_of_label("plain-id") is None
