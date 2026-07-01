# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for PLT-Optimizer tray application (Windows).

This spec builds the system tray version of PLT-Optimizer using infi.systray
instead of pystray to avoid Windows message loop conflicts with tkinter.

Usage:
    pyinstaller plt_optimizer.spec
"""

import sys
from pathlib import Path

block_cipher = None

# Determine paths.
# NOTE: PyInstaller executes this spec file via ``exec(code, spec_namespace)``
# and does NOT inject ``__file__`` into the namespace. We must rely on
# ``SPECPATH`` (the directory containing this spec file), which PyInstaller
# does inject, instead of ``Path(__file__).parent``.
ROOT_DIR = Path(SPECPATH)
ASSETS_DIR = ROOT_DIR / "assets"

# Collect all necessary data files and dependencies
datas = [
    (str(ASSETS_DIR / "icon.ico"), "assets"),
]

hiddenimports = [
    # Core application imports
    "plt_optimizer",
    "plt_optimizer.cli.watch",
    "plt_optimizer.core.parser",
    "plt_optimizer.core.writer",
    "plt_optimizer.core.chunker",
    "plt_optimizer.core.optimizer",
    "plt_optimizer.core.intra_chunk_optimizer",
    "plt_optimizer.ui.tray",
    "plt_optimizer.utils.config",
    # Windows-specific systray (infi.systray uses pywin32 directly)
    "infi.systray",
    "win32api",
    "win32con",
    "win32gui",
    "win32gui_struct",
    "win32service",
]

# For onefile, uncomment below and remove 'onedir' from command
# exe_options = [('u', None, 'OPTION')] + datas

a = Analysis(
    ["run_tray.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude pystray since we're using infi.systray on Windows
        "pystray",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PLT-Optimizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # No console window (windowed app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ASSETS_DIR / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PLT-Optimizer",
)
