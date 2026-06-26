"""Settings window for PLT-Optimizer.

This module provides a tkinter-based configuration dialog that allows users to:
- Set watch directory, output directory, log directory, and processed directory
- Toggle fast mode and debug save files options
- Enable/disable run at startup
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

# Module-level logger
_logger = logging.getLogger(__name__)

# Platform detection
_IS_WINDOWS = sys.platform == "win32"


class SettingsWindow:
    """Settings dialog window using tkinter.

    This class creates a modal settings window that allows users to configure
    all PLT-Optimizer options including directory paths, optimization mode,
    and startup behavior.

    Attributes:
        on_settings_saved: Callback when settings are saved.
            Receives the updated config dict as argument.
    """

    def __init__(
        self,
        current_config: dict[str, Any],
        save_callback: Callable[[dict[str, Any]], None],
        parent: tk.Tk | None = None,
    ) -> None:
        """Initialize the settings window.

        Args:
            current_config: Current configuration dictionary.
            save_callback: Function to call with updated config on save.
            parent: Optional parent Tk window for modal behavior.
        """
        self._config = current_config.copy()
        self._save_callback = save_callback
        self._parent = parent

        # Create the main window
        self._root = tk.Toplevel(parent) if parent else tk.Tk()
        self._root.title("PLT-Optimizer Settings")
        self._root.geometry("600x550")  # Increased height to fit all content
        self._root.resizable(False, False)

        # Make window modal (but don't grab_set here - do it in show() when deiconified)
        if parent is not None:
            self._root.transient(parent)

        self._setup_ui()
        self._load_current_values()

    def _setup_ui(self) -> None:
        """Set up the user interface components."""
        main_frame = ttk.Frame(self._root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        title_label = ttk.Label(
            main_frame,
            text="PLT-Optimizer Configuration",
            font=("Segoe UI", 14, "bold"),
        )
        title_label.grid(row=0, column=0, columnspan=3, pady=(0, 20))

        # Directory settings section
        dir_section = ttk.LabelFrame(main_frame, text="Directories", padding="10")
        dir_section.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        row = 2

        # Watch Directory
        ttk.Label(dir_section, text="Watch Directory:").grid(row=row, column=0, sticky="w", pady=5)
        self._watch_dir_var = tk.StringVar()
        watch_entry = ttk.Entry(dir_section, textvariable=self._watch_dir_var, width=40)
        watch_entry.grid(row=row, column=1, sticky="ew", padx=(5, 0), pady=5)
        ttk.Button(
            dir_section,
            text="Browse...",
            command=lambda: self._browse_directory(self._watch_dir_var),
        ).grid(row=row, column=2, pady=5)

        row += 1

        # Output Directory
        ttk.Label(dir_section, text="Output Directory:").grid(row=row, column=0, sticky="w", pady=5)
        self._output_dir_var = tk.StringVar()
        output_entry = ttk.Entry(dir_section, textvariable=self._output_dir_var, width=40)
        output_entry.grid(row=row, column=1, sticky="ew", padx=(5, 0), pady=5)
        ttk.Button(
            dir_section,
            text="Browse...",
            command=lambda: self._browse_directory(self._output_dir_var),
        ).grid(row=row, column=2, pady=5)

        row += 1

        # Log Directory
        ttk.Label(dir_section, text="Log Directory:").grid(row=row, column=0, sticky="w", pady=5)
        self._log_dir_var = tk.StringVar()
        log_entry = ttk.Entry(dir_section, textvariable=self._log_dir_var, width=40)
        log_entry.grid(row=row, column=1, sticky="ew", padx=(5, 0), pady=5)
        ttk.Button(
            dir_section, text="Browse...", command=lambda: self._browse_directory(self._log_dir_var)
        ).grid(row=row, column=2, pady=5)

        row += 1

        # Processed Directory (optional)
        ttk.Label(dir_section, text="Processed Directory:").grid(
            row=row, column=0, sticky="w", pady=5
        )
        self._processed_dir_var = tk.StringVar()
        processed_entry = ttk.Entry(dir_section, textvariable=self._processed_dir_var, width=40)
        processed_entry.grid(row=row, column=1, sticky="ew", padx=(5, 0), pady=5)

        def clear_processed() -> None:
            self._processed_dir_var.set("")

        ttk.Button(dir_section, text="Clear", command=clear_processed).grid(
            row=row, column=2, pady=5
        )

        # Optimization settings section
        opt_section = ttk.LabelFrame(main_frame, text="Optimization Options", padding="10")
        opt_section.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 10))

        self._fast_mode_var = tk.BooleanVar()
        fast_check = ttk.Checkbutton(
            opt_section,
            text="Fast Mode (uses NearestNeighbor2Opt only)",
            variable=self._fast_mode_var,
        )
        fast_check.grid(row=0, column=0, sticky="w", pady=5)

        self._debug_save_files_var = tk.BooleanVar()
        debug_check = ttk.Checkbutton(
            opt_section,
            text="Debug Mode (save before/after files)",
            variable=self._debug_save_files_var,
        )
        debug_check.grid(row=1, column=0, sticky="w", pady=5)

        # Startup settings section
        if _IS_WINDOWS:
            startup_section = ttk.LabelFrame(main_frame, text="Startup", padding="10")
            startup_section.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 10))

            self._run_at_startup_var = tk.BooleanVar()
            startup_check = ttk.Checkbutton(
                startup_section,
                text="Run at Windows Startup",
                variable=self._run_at_startup_var,
            )
            startup_check.grid(row=0, column=0, sticky="w", pady=5)

        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=5, column=0, columnspan=3, pady=(20, 0))

        ttk.Button(button_frame, text="Save", command=self._on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=self._on_cancel).pack(side=tk.LEFT, padx=5)

    def _browse_directory(self, var: tk.StringVar) -> None:
        """Open a directory browser dialog and update the given variable.

        Args:
            var: StringVar to update with selected path.
        """
        # Hide window during dialog to avoid focus issues
        self._root.withdraw()

        initial = var.get()
        if not initial or not Path(initial).exists():
            initial = str(Path.home())

        selected = filedialog.askdirectory(
            title="Select Directory",
            initialdir=initial,
            parent=self._root,  # Explicitly set parent for proper binding
        )

        # Restore window after dialog closes
        self._root.deiconify()
        self._root.focus_force()

        if selected:
            var.set(selected)

    def _load_current_values(self) -> None:
        """Load current configuration values into UI fields."""
        self._watch_dir_var.set(self._config.get("watch_dir", ""))
        self._output_dir_var.set(self._config.get("output_dir", "./optimized"))
        self._log_dir_var.set(self._config.get("log_dir", "./logs"))

        processed = self._config.get("processed_dir")
        if processed:
            self._processed_dir_var.set(str(processed))

        self._fast_mode_var.set(bool(self._config.get("fast_mode", False)))
        self._debug_save_files_var.set(bool(self._config.get("debug_save_files", False)))

    def _validate_inputs(self) -> bool:
        """Validate user inputs before saving.

        Returns:
            True if all inputs are valid.
        """
        watch_dir = self._watch_dir_var.get().strip()
        if not watch_dir:
            messagebox.showerror(
                "Validation Error",
                "Watch Directory is required.",
                parent=self._root,
            )
            return False

        output_dir = self._output_dir_var.get().strip()
        if not output_dir:
            messagebox.showerror(
                "Validation Error",
                "Output Directory is required.",
                parent=self._root,
            )
            return False

        log_dir = self._log_dir_var.get().strip()
        if not log_dir:
            messagebox.showerror(
                "Validation Error",
                "Log Directory is required.",
                parent=self._root,
            )
            return False

        # Check watch directory exists
        if not Path(watch_dir).exists():
            result = messagebox.askyesno(
                "Directory Not Found",
                f"Watch directory '{watch_dir}' does not exist.\n\nCreate it now?",
                parent=self._root,
            )
            if result:
                try:
                    Path(watch_dir).mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    messagebox.showerror(
                        "Error",
                        f"Could not create directory: {e}",
                        parent=self._root,
                    )
                    return False
            else:
                return False

        return True

    def _on_save(self) -> None:
        """Handle save button click."""
        if not self._validate_inputs():
            return

        # Update config dict
        self._config["watch_dir"] = self._watch_dir_var.get().strip()
        self._config["output_dir"] = self._output_dir_var.get().strip() or "./optimized"
        self._config["log_dir"] = self._log_dir_var.get().strip() or "./logs"

        processed = self._processed_dir_var.get().strip()
        self._config["processed_dir"] = processed if processed else None

        self._config["fast_mode"] = self._fast_mode_var.get()
        self._config["debug_save_files"] = self._debug_save_files_var.get()

        # Call save callback
        try:
            self._save_callback(self._config)
            _logger.info("Settings saved successfully")
            self._root.destroy()
        except Exception as e:
            messagebox.showerror(
                "Error",
                f"Failed to save settings: {e}",
                parent=self._root,
            )
            _logger.error(f"Failed to save settings: {e}")

    def _on_cancel(self) -> None:
        """Handle cancel button click."""
        self._root.destroy()

    def show(self) -> None:
        """Show the settings window (blocking)."""
        # Center and display the window
        self._center_window()

        if not sys.platform == "win32":
            self._root.deiconify()
            return

        # On Windows: deiconify, set focus, use transient for modal behavior
        # Note: grab_set() on a child of a hidden/withdrawn root causes issues
        self._root.protocol("WM_DELETE_WINDOW", self._on_cancel)
        try:
            self._root.deiconify()
            self._root.focus_force()
            self._root.mainloop()
        except KeyboardInterrupt:
            self._on_cancel()

    def destroy(self) -> None:
        """Destroy the settings window."""
        if self._root is not None:
            self._root.destroy()

    def _center_window(self) -> None:
        """Center the window on the screen."""
        self._root.update_idletasks()
        x = (self._root.winfo_screenwidth() // 2) - (self._root.winfo_width() // 2)
        y = (self._root.winfo_screenheight() // 2) - (self._root.winfo_height() // 2)
        self._root.geometry(f"+{x}+{y}")


__all__ = [
    "SettingsWindow",
]
