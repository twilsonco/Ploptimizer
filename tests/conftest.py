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
        def showwarning(title: str, message: str, **kwargs: Any) -> None:
            pass

        @staticmethod
        def askyesno(title: str, message: str, **kwargs: Any) -> bool:
            return False

    # Instantiate the submodule mocks BEFORE defining TkinterModule so that
    # the same instances are referenced both as ``sys.modules`` entries and
    # as attributes on the ``tkinter`` package mock. This ensures that
    # ``from tkinter import messagebox`` (which reads the attribute) and
    # ``sys.modules["tkinter.messagebox"]`` (which tests patch) refer to
    # the same object, so ``patch("tkinter.messagebox.showwarning")`` is
    # visible to production code that imports ``messagebox`` from
    # ``tkinter``.
    filedialog_module = FiledialogModule()
    messagebox_module = MessageboxModule()

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

        filedialog = filedialog_module
        messagebox = messagebox_module
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

    # Install mocks - must set submodules BEFORE the main tkinter module.
    # Use the SAME instances that TkinterModule references so that
    # ``from tkinter import messagebox`` and ``sys.modules["tkinter.messagebox"]``
    # resolve to the same object (tests patch the latter).
    sys.modules["tkinter.filedialog"] = filedialog_module
    sys.modules["tkinter.messagebox"] = messagebox_module
    sys.modules["tkinter.ttk"] = TtkModule()

    # Now install the main tkinter mock with submodule references
    sys.modules["tkinter"] = TkinterModule()  # type: ignore[assignment]


def _install_pystray_mocks() -> None:
    """Install pystray mocks at the earliest possible moment.

    Pystray's real implementation imports Xlib transitively on Linux/macOS,
    which fails on headless CI runners where no ``DISPLAY`` environment
    variable is set::

        Xlib.error.DisplayNameError: Bad display name ""

    ``plt_optimizer/ui/tray.py`` imports pystray lazily inside ``_setup_pystray``
    and ``_create_pystray_menu``, but the tests use
    ``with patch("pystray.Menu")`` which forces Python to ``import pystray``
    before patching can take effect. That import chain is what triggers the
    Xlib failure on Ubuntu/macOS CI runners.

    Installing a stand-in mock module in ``sys.modules`` prevents the real
    ``pystray`` package (and its ``Xlib`` dependency) from being loaded
    during test collection. The mock exposes the same public attributes
    (``Icon``, ``Menu``, ``MenuItem`` and ``Menu.SEPARATOR``) that the
    production code references, so existing ``patch("pystray.X")`` calls
    continue to work without modification. This mirrors the tkinter-mock
    strategy above and avoids scattering platform-specific guards across
    individual tests.

    The mock is given a ``__spec__`` (a ``ModuleSpec`` with ``loader=None``)
    so ``importlib.util.find_spec("pystray")`` still resolves to a non-None
    spec. Without this, production code paths that probe for pystray via
    ``_safe_find_spec("pystray")`` (e.g. ``TrayIconManager.run`` -> ``_check_dependencies``)
    would see the mock as "missing" and raise ``ImportError`` even though
    ``pystray`` is technically present in ``sys.modules``.
    """

    import importlib.machinery

    class MockIcon:
        """Mock pystray.Icon class used by TrayIconManager._setup_pystray."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._init_args = args
            self._init_kwargs = kwargs

        def run(self) -> None:
            """Stand-in for pystray.Icon.run (blocking message loop)."""

        def run_detached(self) -> None:
            """Stand-in for pystray.Icon.run_detached."""

        def stop(self) -> None:
            """Stand-in for pystray.Icon.stop."""

    class MockMenu:
        """Mock pystray.Menu class used by TrayIconManager._create_pystray_menu.

        ``Menu.SEPARATOR`` is referenced as a class attribute in production
        code and must therefore be defined on the class itself rather than on
        instances.
        """

        SEPARATOR = "---"

        def __init__(self, *items: Any) -> None:
            self._items = items

    class MockMenuItem:
        """Mock pystray.MenuItem class used by TrayIconManager._create_pystray_menu."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._init_args = args
            self._init_kwargs = kwargs

    class PystrayModule:
        """Stand-in for the ``pystray`` package module."""

        Icon = MockIcon
        Menu = MockMenu
        MenuItem = MockMenuItem

        # Mimic a real package so ``importlib.util.find_spec`` returns a
        # non-None spec and ``_safe_find_spec`` reports pystray as present.
        __name__ = "pystray"
        __package__ = "pystray"
        __file__ = __file__
        __spec__ = importlib.machinery.ModuleSpec(name="pystray", loader=None)

    # Only install if pystray isn't already in sys.modules - if a prior test
    # or import successfully brought in real pystray we leave it alone so
    # behaviour matches what was loaded at interpreter startup.
    if "pystray" not in sys.modules:
        sys.modules["pystray"] = PystrayModule()  # type: ignore[assignment]


# Install mocks immediately when this module is imported (before collection)
_install_tkinter_mocks()
_install_pystray_mocks()


@pytest.fixture(autouse=True)
def close_figures_after_test() -> None:
    """Close all matplotlib figures after each test to prevent memory warnings."""
    yield
    plt.close("all")
