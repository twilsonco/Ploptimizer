"""Regression tests for pyproject.toml Windows 7 compatibility invariants.

These tests guard against inadvertent regressions of the Windows 7 support
guarantees documented in AGENTS.md §5. They are intentionally cheap (no I/O,
no subprocesses) so they run on every CI matrix cell.

The invariants enforced here:

1. ``requires-python`` floor must be ``<=3.8`` (Win7 cannot run Python 3.9+).
2. The ``Programming Language :: Python :: 3.8`` classifier must be present.
3. The ``Operating System :: Microsoft :: Windows :: Windows 7`` classifier
   must be present.
4. ``tool.mypy.python_version`` must not be raised above ``py38`` without
   verifying dependency resolution on the floor.
5. ``tool.ruff.target-version`` must not be raised above ``py38`` without
   verifying dependency resolution on the floor.
6. The CI build job must build with Python 3.8 (not 3.11) so the resulting
   PyInstaller bundle ships ``python38.dll`` instead of ``python311.dll`` —
   the latter depends on Universal C Runtime components
   (``api-ms-win-core-path-l1-1-0.dll``) that do not exist on Windows 7.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.8/3.9 fallback
    import tomli as tomllib  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
CI_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def _load_pyproject() -> dict[str, object]:
    """Load and return the parsed ``pyproject.toml`` contents.

    Returns:
        The parsed TOML document as a dictionary.

    Raises:
        pytest.skip: If ``pyproject.toml`` is missing (should never happen in CI).
    """
    if not PYPROJECT_PATH.exists():
        pytest.skip(f"pyproject.toml not found at {PYPROJECT_PATH}")
    with PYPROJECT_PATH.open("rb") as fh:
        return tomllib.load(fh)


def _load_ci_workflow() -> str:
    """Load and return the raw CI workflow YAML as text.

    Returns:
        The raw contents of ``.github/workflows/ci.yml``.

    Raises:
        pytest.skip: If the workflow file is missing.
    """
    if not CI_WORKFLOW_PATH.exists():
        pytest.skip(f"CI workflow not found at {CI_WORKFLOW_PATH}")
    return CI_WORKFLOW_PATH.read_text(encoding="utf-8")


class TestRequiresPythonFloor:
    """Guard the ``requires-python`` floor against accidental bumps."""

    def test_requires_python_field_present(self) -> None:
        """``requires-python`` must be declared in ``[project]``."""
        project = _load_pyproject().get("project", {})
        assert isinstance(project, dict)
        assert "requires-python" in project, (
            "pyproject.toml [project] is missing 'requires-python' — "
            "AGENTS.md §5.1 requires an explicit floor."
        )

    def test_requires_python_floor_is_py38_or_lower(self) -> None:
        """The floor of ``requires-python`` must be ``<=3.8``.

        Windows 7 cannot run Python 3.9+. Raising the floor above 3.8 silently
        breaks Win7 support and is a regression per AGENTS.md §5.1.
        """
        project = _load_pyproject().get("project", {})
        assert isinstance(project, dict)
        raw = project.get("requires-python")
        assert isinstance(raw, str), "requires-python must be a string"

        # Parse the lower bound of the version specifier.
        match = re.match(r">=\s*(\d+)\.(\d+)", raw)
        assert match, (
            f"Could not parse lower bound from requires-python={raw!r}. "
            "Expected a PEP 440 specifier like '>=3.8,<3.14'."
        )
        major, minor = int(match.group(1)), int(match.group(2))
        assert (major, minor) <= (3, 8), (
            f"requires-python floor is {major}.{minor}, but AGENTS.md §5.1 "
            f"requires the floor to be <=3.8 for Windows 7 support. "
            f"Current value: {raw!r}"
        )


class TestClassifiers:
    """Guard the Win7 and Python 3.8 classifiers."""

    @pytest.fixture
    def classifiers(self) -> list[str]:
        """Return the ``classifiers`` list from ``[project]``."""
        project = _load_pyproject().get("project", {})
        assert isinstance(project, dict)
        classifiers = project.get("classifiers", [])
        assert isinstance(classifiers, list)
        return [str(c) for c in classifiers]

    def test_python_38_classifier_present(self, classifiers: list[str]) -> None:
        """The ``Programming Language :: Python :: 3.8`` classifier must exist."""
        assert "Programming Language :: Python :: 3.8" in classifiers, (
            "Missing 'Programming Language :: Python :: 3.8' classifier. "
            "AGENTS.md §5.1 requires it for Windows 7 support."
        )

    def test_windows_7_classifier_present(self, classifiers: list[str]) -> None:
        """The ``Windows 7`` OS classifier must exist."""
        assert "Operating System :: Microsoft :: Windows :: Windows 7" in classifiers, (
            "Missing 'Operating System :: Microsoft :: Windows :: Windows 7' "
            "classifier. AGENTS.md §5.1 requires it to advertise Win7 support."
        )


class TestToolConfigFloor:
    """Guard ``tool.mypy.python_version`` and ``tool.ruff.target-version``."""

    def test_mypy_python_version_not_above_py38(self) -> None:
        """``tool.mypy.python_version`` must not be raised above ``py38``."""
        data = _load_pyproject()
        mypy = data.get("tool", {}).get("mypy", {})
        assert isinstance(mypy, dict)
        version = mypy.get("python_version")
        if version is None:
            pytest.skip("tool.mypy.python_version not set")
        assert isinstance(version, str)
        # Acceptable values: "3.8", "3.7", "3.6" — anything <=3.8.
        match = re.match(r"^(\d+)\.(\d+)$", version)
        assert match, f"Unexpected tool.mypy.python_version format: {version!r}"
        major, minor = int(match.group(1)), int(match.group(2))
        assert (major, minor) <= (3, 8), (
            f"tool.mypy.python_version is {version!r}, but AGENTS.md §5.1 "
            f"requires it to remain <=3.8 unless dependency resolution on "
            f"Python 3.8 has been verified."
        )

    def test_ruff_target_version_not_above_py38(self) -> None:
        """``tool.ruff.target-version`` must not be raised above ``py38``."""
        data = _load_pyproject()
        ruff = data.get("tool", {}).get("ruff", {})
        assert isinstance(ruff, dict)
        target = ruff.get("target-version")
        if target is None:
            pytest.skip("tool.ruff.target-version not set")
        assert isinstance(target, str)
        # Acceptable values: "py38", "py37", "py36" — anything <=py38.
        match = re.match(r"^py(\d+)(\d+)$", target)
        assert match, f"Unexpected tool.ruff.target-version format: {target!r}"
        major, minor = int(match.group(1)), int(match.group(2))
        assert (major, minor) <= (3, 8), (
            f"tool.ruff.target-version is {target!r}, but AGENTS.md §5.1 "
            f"requires it to remain <=py38 unless dependency resolution on "
            f"Python 3.8 has been verified."
        )


class TestCIBuildJob:
    """Guard the CI ``build-windows-exe`` job against Python 3.9+ builds.

    These tests are the regression net for the bug where the CI build job
    produced a PyInstaller bundle containing ``python311.dll`` — which depends
    on Universal C Runtime components (``api-ms-win-core-path-l1-1-0.dll``)
    that do not exist on Windows 7. The fix is to build with Python 3.8.
    """

    @pytest.fixture
    def workflow_text(self) -> str:
        """Return the raw CI workflow text."""
        return _load_ci_workflow()

    def test_build_job_pins_python_38(self, workflow_text: str) -> None:
        """The ``build-windows-exe`` job must explicitly use Python 3.8.

        Without this pin, ``setup-uv`` reads ``.python-version`` (currently
        ``3.11``) and produces a Win7-incompatible bundle.
        """
        # Isolate the build-windows-exe job block.
        match = re.search(
            r"build-windows-exe:.*?(?=\n  [a-z][\w-]*:|\Z)",
            workflow_text,
            re.DOTALL,
        )
        assert match, "Could not locate 'build-windows-exe' job in ci.yml"
        job_block = match.group(0)

        # The job must reference python-version: '3.8' (or 3.8.x) somewhere
        # in its setup-uv step.
        py_pin = re.search(r"python-version:\s*['\"]?3\.8(?:\.\d+)?['\"]?", job_block)
        assert py_pin, (
            "build-windows-exe job does not pin Python 3.8. "
            "Building with Python 3.9+ ships python3XX.dll which depends on "
            "api-ms-win-core-path-l1-1-0.dll and other Universal CRT "
            "components that do not exist on Windows 7. "
            "Add `python-version: '3.8'` to the setup-uv step."
        )

        # And it must NOT pin 3.9 or higher.
        bad_pin = re.search(r"python-version:\s*['\"]?(?:3\.(9|[1-9]\d+)|[4-9]\d?)", job_block)
        assert not bad_pin, (
            f"build-windows-exe job pins Python {bad_pin.group(0)!r} which "
            "is not supported on Windows 7. Use Python 3.8."
        )

    def test_build_job_does_not_sync_with_unconditional_all_extras(
        self, workflow_text: str
    ) -> None:
        """The build job must not sync ``--all-extras`` (which pulls in
        ``plotting`` extras gated to ``python_version >= '3.9'`` and would
        fail on a Python 3.8 build).

        The build job only needs ``tray`` and ``build`` extras.
        """
        match = re.search(
            r"build-windows-exe:.*?(?=\n  [a-z][\w-]*:|\Z)",
            workflow_text,
            re.DOTALL,
        )
        assert match, "Could not locate 'build-windows-exe' job in ci.yml"
        job_block = match.group(0)

        # Find the uv sync line within the build job.
        sync_line = re.search(r"uv sync[^\n]*", job_block)
        assert sync_line, "build-windows-exe job has no 'uv sync' step"
        cmd = sync_line.group(0)

        assert "--all-extras" not in cmd, (
            f"build-windows-exe job uses 'uv sync --all-extras' ({cmd!r}). "
            "This pulls in the 'plotting' extras which are gated to "
            "python_version >= '3.9' and will fail on a Python 3.8 build. "
            "Use 'uv sync --python 3.8 --extra tray --extra build' instead."
        )

    def test_test_matrix_includes_python_38(self, workflow_text: str) -> None:
        """The test matrix must include Python 3.8 to exercise the floor.

        AGENTS.md §5.6 requires at least one Python 3.8 job so dependency
        resolution is verified on the floor version.
        """
        match = re.search(
            r"^\s*test:.*?(?=\n  [a-z][\w-]*:|\Z)",
            workflow_text,
            re.DOTALL | re.MULTILINE,
        )
        assert match, "Could not locate 'test' job in ci.yml"
        job_block = match.group(0)

        # The test job must declare a python-version matrix containing 3.8.
        py_matrix = re.search(
            r"python-version:\s*\[[^\]]*3\.8[^\]]*\]",
            job_block,
        )
        assert py_matrix, (
            "Test job does not include Python 3.8 in its matrix. "
            "AGENTS.md §5.6 requires at least one Python 3.8 job so "
            "dependency resolution is exercised on the floor version."
        )