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
        self._root.geometry("580x480")
        self._root.resizable(False, False)

        # Make window modal (but don't grab_set here - do it in show() when deiconified)
        if parent is not None:
            self._root.transient(parent)

        self._setup_ui()
        self._load_current_values()

    def _setup_ui(self) -> None:
        """Set up the user interface components."""
        main_frame = ttk.Frame(self._root, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        title_label = ttk.Label(
            main_frame,
            text="PLT-Optimizer Configuration",
            font=("Segoe UI", 12, "bold"),
        )
        title_label.grid(row=0, column=0, columnspan=4, pady=(0, 15))

        # Directory settings section - TWO COLUMN LAYOUT
        dir_section = ttk.LabelFrame(main_frame, text="Directories", padding="8")
        dir_section.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 10))

        # Left column (col 0-1)
        ttk.Label(dir_section, text="Watch:").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=3)
        self._watch_dir_var = tk.StringVar()
        watch_entry = ttk.Entry(dir_section, textvariable=self._watch_dir_var, width=50)
        watch_entry.grid(row=0, column=1, sticky="ew", padx=(0, 5), pady=3)

        # Right column (col 2-3) - Watch Browse
        ttk.Button(
            dir_section,
            text="Browse",
            command=lambda: self._browse_directory(self._watch_dir_var),
        ).grid(row=0, column=3, pady=3)

        # Left column
        ttk.Label(dir_section, text="Output:").grid(
            row=1, column=0, sticky="w", padx=(0, 5), pady=3
        )
        self._output_dir_var = tk.StringVar()
        output_entry = ttk.Entry(dir_section, textvariable=self._output_dir_var, width=50)
        output_entry.grid(row=1, column=1, sticky="ew", padx=(0, 5), pady=3)

        # Right column - Output Browse
        ttk.Button(
            dir_section,
            text="Browse",
            command=lambda: self._browse_directory(self._output_dir_var),
        ).grid(row=1, column=3, pady=3)

        # Left column
        ttk.Label(dir_section, text="Log:").grid(row=2, column=0, sticky="w", padx=(0, 5), pady=3)
        self._log_dir_var = tk.StringVar()
        log_entry = ttk.Entry(dir_section, textvariable=self._log_dir_var, width=50)
        log_entry.grid(row=2, column=1, sticky="ew", padx=(0, 5), pady=3)

        # Right column - Log Browse
        ttk.Button(
            dir_section, text="Browse", command=lambda: self._browse_directory(self._log_dir_var)
        ).grid(row=2, column=3, pady=3)

        # Left column
        ttk.Label(dir_section, text="Processed:").grid(
            row=3, column=0, sticky="w", padx=(0, 5), pady=3
        )
        self._processed_dir_var = tk.StringVar()
        processed_entry = ttk.Entry(dir_section, textvariable=self._processed_dir_var, width=50)
        processed_entry.grid(row=3, column=1, sticky="ew", padx=(0, 5), pady=3)

        # Right column - Processed Browse
        ttk.Button(
            dir_section,
            text="Browse",
            command=lambda: self._browse_directory(self._processed_dir_var),
        ).grid(row=3, column=3, pady=3)

        # Optimization settings section - TWO COLUMN LAYOUT
        opt_section = ttk.LabelFrame(main_frame, text="Optimization Options", padding="8")
        opt_section.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 10))

        self._fast_mode_var = tk.BooleanVar()
        fast_check = ttk.Checkbutton(
            opt_section,
            text="Fast Mode (NearestNeighbor2Opt only)",
            variable=self._fast_mode_var,
        )
        fast_check.grid(row=0, column=0, sticky="w", pady=3)

        self._debug_save_files_var = tk.BooleanVar()
        debug_check = ttk.Checkbutton(
            opt_section,
            text="Debug Mode (save before/after files)",
            variable=self._debug_save_files_var,
        )
        debug_check.grid(row=0, column=2, sticky="w", pady=3)

        # Startup and Maintenance sections - TWO COLUMN LAYOUT
        if _IS_WINDOWS:
            startup_section = ttk.LabelFrame(main_frame, text="Startup", padding="8")
            startup_section.grid(row=3, column=0, sticky="ew", pady=(10, 10), ipady=5)

            self._run_at_startup_var = tk.BooleanVar()
            startup_check = ttk.Checkbutton(
                startup_section,
                text="Run at Windows Startup",
                variable=self._run_at_startup_var,
            )
            startup_check.grid(row=0, column=0, sticky="w", pady=(5, 0), padx=5)

        # Maintenance section (side by side with Startup if Windows, otherwise full width)
        cleanup_section = ttk.LabelFrame(main_frame, text="Maintenance", padding="8")
        if _IS_WINDOWS:
            cleanup_section.grid(row=3, column=1, sticky="ew", pady=(10, 10), ipady=5, padx=(10, 0))
        else:
            cleanup_section.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 10))

        # Buttons frame
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=4, column=0, columnspan=4, pady=(15, 0))

        def on_cleanup() -> None:
            """Handle cleanup button click."""
            result = messagebox.askyesno(
                "Cleanup Files",
                "This will delete all files in the logs and processed directories.\n\nContinue?",
                parent=self._root,
            )
            if not result:
                return

            # Clean logs directory
            log_dir = self._log_dir_var.get().strip()
            cleaned_count = 0
            if log_dir and Path(log_dir).exists():
                try:
                    for f in Path(log_dir).iterdir():
                        if f.is_file():
                            f.unlink()
                            cleaned_count += 1
                except Exception as e:
                    _logger.error(f"Error cleaning log directory: {e}")

            # Clean processed directory if set
            processed_dir = self._processed_dir_var.get().strip()
            if processed_dir and Path(processed_dir).exists():
                try:
                    for f in Path(processed_dir).iterdir():
                        if f.is_file():
                            f.unlink()
                            cleaned_count += 1
                except Exception as e:
                    _logger.error(f"Error cleaning processed directory: {e}")

            messagebox.showinfo(
                "Cleanup Complete", f"Deleted {cleaned_count} files.", parent=self._root
            )

        ttk.Button(cleanup_section, text="Clean Logs & Processed Files", command=on_cleanup).grid(
            row=0, column=0, sticky="w", pady=(5, 0), padx=5
        )

        ttk.Button(button_frame, text="Save", command=self._on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=self._on_cancel).pack(side=tk.LEFT, padx=5)

    def _browse_directory(self, var: tk.StringVar) -> None:
        """Open a directory browser dialog and update the given variable.

        Args:
            var: StringVar to update with selected path.
        """
        initial = var.get()
        if not initial:
            initial = str(Path.home())
        else:
            # Check existence safely - network paths may raise OSError on stat()
            try:
                if not Path(initial).exists():
                    initial = str(Path.home())
            except OSError:
                # Network paths (e.g., \\server\share) can fail with OSError
                # when server is unreachable or doesn't support the operation
                _logger.debug(f"Could not access path, using home: {initial}")
                initial = str(Path.home())

        _logger.debug(f"Opening directory dialog, current value: {initial}")

        # Hide settings while dialog is open for cleaner UX on all platforms
        self._root.withdraw()

        try:
            selected = filedialog.askdirectory(
                title="Select Directory",
                initialdir=initial,
                parent=self._root,
            )
        except Exception as e:
            _logger.error(f"Filedialog error: {e}")
            # Ensure window is restored
            try:
                self._root.deiconify()
            except Exception:
                pass
            return

        # Restore window
        try:
            self._root.deiconify()
            self._root.focus_force()
        except Exception as e:
            _logger.error(f"Error restoring window: {e}")

        if selected:
            _logger.debug(f"Selected directory: {selected}")
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

        if _IS_WINDOWS and hasattr(self, "_run_at_startup_var"):
            # Reflect actual system state as the source of truth, falling back
            # to the stored config value if the system check fails for any reason.
            try:
                from plt_optimizer.utils.startup import is_startup_enabled

                self._run_at_startup_var.set(is_startup_enabled())
            except Exception:
                self._run_at_startup_var.set(bool(self._config.get("run_at_startup", False)))

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

        if _IS_WINDOWS and hasattr(self, "_run_at_startup_var"):
            self._config["run_at_startup"] = self._run_at_startup_var.get()
            # Apply the change immediately so the user sees the effect right
            # away, even if the caller doesn't reconcile the new config after
            # the dialog closes. ``create_shortcut``/``remove_shortcut`` are
            # idempotent so this is safe even when ``run_tray.py`` also
            # applies the delta after ``show()`` returns.
            try:
                from plt_optimizer.utils.startup import (
                    create_shortcut,
                    remove_shortcut,
                )

                if self._run_at_startup_var.get():
                    create_shortcut()
                    _logger.info("Enabled run at startup")
                else:
                    remove_shortcut()
                    _logger.info("Disabled run at startup")
            except Exception as e:
                _logger.error(f"Failed to update startup state: {e}")

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
