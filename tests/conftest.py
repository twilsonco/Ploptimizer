"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import matplotlib.pyplot as plt
import pytest


@pytest.fixture(autouse=True)
def close_figures_after_test() -> None:
    """Close all matplotlib figures after each test to prevent memory warnings.

    This fixture runs automatically for every test, closing any figures that
    were created during the test execution.
    """
    yield
    plt.close("all")
