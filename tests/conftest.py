"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import os
import sys
from typing import Any, Optional
from unittest.mock import MagicMock

# Set matplotlib to non-interactive backend BEFORE importing pyplot
os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib.pyplot as plt
import pytest


# Mock tkinter before any tests run to avoid _tkinter not found errors
class MockStringVar:
    """Mock StringVar that stores and returns values properly."""

    def __init__(self, initial: str = "") -> None:
        self._value = initial

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value


class MockBooleanVar:
    """Mock BooleanVar that stores and returns boolean values properly."""

    def __init__(self, initial: bool = False) -> None:
        self._value = initial

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value = value


def _install_tkinter_mocks() -> None:
    """Install tkinter mocks at the earliest possible moment."""

    class FiledialogModule:
        """Mock tkinter.filedialog module."""

        @staticmethod
        def askdirectory(
            title: str = "",
            initialdir: str = "",
            parent: Optional[Any] = None,
        ) -> str:
            return ""

    class MessageboxModule:
        """Mock tkinter.messagebox module."""

        @staticmethod
        def showerror(title: str, message: str, **kwargs: Any) -> None:
            pass

        @staticmethod
        def showinfo(title: str, message: str, **kwargs: Any) -> None:
            pass

        @staticmethod
        def askyesno(title: str, message: str, **kwargs: Any) -> bool:
            return False

    class TtkModule:
        """Mock tkinter.ttk module."""

        Frame = MagicMock()
        Label = MagicMock()
        LabelFrame = MagicMock()
        Entry = MagicMock()
        Checkbutton = MagicMock()
        Button = MagicMock()
        Style = MagicMock()

        def __getattr__(self, name: str) -> Any:
            return MagicMock()

    class MockTkRoot:
        """Mock tk.Tk root window."""

        @property
        def title(self) -> MagicMock:
            return MagicMock()

        @title.setter  # type: ignore[attr-defined]
        def title(self, value: str) -> None:
            pass

        def resizable(self, x: bool, y: bool) -> None:
            pass

        def transient(self, parent: Any = None) -> None:
            pass

        def withdraw(self) -> None:
            pass

        def deiconify(self) -> None:
            pass

        def focus_force(self) -> None:
            pass

        # These are methods in real tkinter
        def winfo_screenwidth(self) -> int:
            return 1920

        def winfo_screenheight(self) -> int:
            return 1080

        def winfo_width(self) -> int:
            return 580

        def winfo_height(self) -> int:
            return 480

        def update_idletasks(self) -> None:
            pass

        def geometry(self, geom: str = "") -> None:
            pass

        def mainloop(self) -> None:
            pass

        def destroy(self) -> None:
            pass

        def protocol(self, name: str, func: Any) -> None:
            pass

    class MockToplevel:
        """Mock tk.Toplevel window."""

        def __init__(self, parent: Optional[Any] = None) -> None:
            self._parent = parent
            self.winfo_screenwidth = MagicMock(return_value=1920)
            self.winfo_screenheight = MagicMock(return_value=1080)
            self.winfo_width = MagicMock(return_value=580)
            self.winfo_height = MagicMock(return_value=480)

        @property
        def title(self) -> str:
            return ""

        @title.setter  # type: ignore[attr-defined]
        def title(self, value: str) -> None:
            pass

        def geometry(self, geom: str = "") -> None:
            pass

        def resizable(self, x: bool, y: bool) -> None:
            pass

        def transient(self, parent: Any) -> None:
            pass

        def withdraw(self) -> None:
            pass

        def deiconify(self) -> None:
            pass

        def focus_force(self) -> None:
            pass

        def mainloop(self) -> None:
            pass

        def protocol(self, name: str, func: Any) -> None:
            pass

        def destroy(self) -> None:
            pass

    class TkinterModule:
        """Mock tkinter module."""

        filedialog = FiledialogModule()
        messagebox = MessageboxModule()
        ttk = TtkModule()

        Tk = MockTkRoot
        Toplevel = MockToplevel
        StringVar = MockStringVar
        BooleanVar = MockBooleanVar

        BOTH = "both"
        LEFT = "left"
        RIGHT = "right"
        TOP = "top"
        X = "x"
        EW = "ew"
        W = "w"

    # Install mocks - must set submodules BEFORE the main tkinter module
    sys.modules["tkinter.filedialog"] = FiledialogModule()
    sys.modules["tkinter.messagebox"] = MessageboxModule()
    sys.modules["tkinter.ttk"] = TtkModule()

    # Now install the main tkinter mock with submodule references
    sys.modules["tkinter"] = TkinterModule()  # type: ignore[assignment]


# Install mocks immediately when this module is imported (before collection)
_install_tkinter_mocks()


@pytest.fixture(autouse=True)
def close_figures_after_test() -> None:
    """Close all matplotlib figures after each test to prevent memory warnings."""
    yield
    plt.close("all")
