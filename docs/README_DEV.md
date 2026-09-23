## Project Structure

```
PLT-Optimizer/
├── main.py                 # CLI entry point (plt-optimizer optimize|generate|watch)
├── plt_optimizer/           # Main package
│   ├── cli/                # Command-line interface
│   │   ├── __init__.py
│   │   ├── optimize.py     # Single-file optimization subcommand
│   │   ├── generate.py     # YAML spec -> per-cutter PLT generation subcommand
│   │   ├── benchmark.py    # Batch strategy benchmarking tool
│   │   └── watch.py        # Hot-watch daemon for automated processing
│   ├── core/               # Core optimization pipeline
│   │   ├── __init__.py
│   │   ├── models.py       # Data classes for PLT representation
│   │   ├── parser.py       # HPGL tokenization and parsing
│   │   ├── writer.py       # PLT file generation
│   │   ├── profiler.py     # Document classification + baseline extent (95th percentile)
│   │   ├── chunker.py      # Stroke grouping into MacroBlocks
│   │   ├── optimizer.py    # Block traversal optimization strategies
│   │   ├── intra_chunk_optimizer.py  # Within-block path optimization
│   │   └── reassembler.py  # Document reconstruction + travel-distance metrics
│   ├── generate/           # Label generation pipeline (YAML -> PLT)
│   │   ├── __init__.py
│   │   ├── schema.py       # Pydantic YAML job-spec data contract
│   │   ├── substitution.py # Replacement text files ("badges"/multiples)
│   │   ├── resolution.py   # Cascade resolution + cutter compensation
│   │   ├── layout.py       # Bounds-aware multi-heuristic bin packing
│   │   ├── label_renderer.py    # Per-label PLT rendering, collision avoidance
│   │   ├── ftext_renderer.py    # matplotlib TTF glyph vectorization
│   │   ├── geometry.py     # Circle-vs-AABB collision math
│   │   └── vectorize.py    # Per-cutter PLT/PDF export (lossless HPGL writer)
│   ├── ui/                 # GUI components (system tray, Windows-only)
│   │   ├── __init__.py
│   │   ├── tray.py         # System tray icon and notification handling
│   │   └── settings.py     # Tkinter-based configuration window
│   ├── utils/              # Utility modules
│   │   ├── __init__.py
│   │   ├── config.py       # JSON configuration file management
│   │   ├── geometry.py     # Distance calculations and geometry utilities
│   │   ├── logging.py      # Dual logging system (text + CSV metrics)
│   │   └── startup.py      # Windows Startup folder shortcut management
│   └── diagnostics/        # Visualization tools
│       ├── __init__.py
│       └── plotter.py      # Matplotlib-based path visualization
├── run_tray.py             # GUI entry point (system tray application)
├── run_integration_test.py # End-to-end generate-pipeline test runner
├── tools.json              # Cutter inventory + boundary/hole cutter size
├── tests/                  # Test suite (pytest)
├── tests_deps/             # Frozen fixtures used by unit tests (do not move/edit)
│   ├── test123_spec.yaml   # Default integration-test job spec
│   ├── complex_test_job.yaml  # Feature stress-test job spec
│   └── *.plt               # Identity/profiler/simplifier example PLTs
├── examples/               # Example PLT files, job specs, and scripts
│   ├── run_diagnostics.py  # Full workflow demonstration
│   └── job_specs/          # User-facing sample specs + replacement text data files
├── docs/                   # Developer documentation
│   ├── README_DEV.md
│   └── INTEGRATION_TESTING.md
├── logs/                   # Generated log files
├── pyproject.toml          # Project configuration (uv)
└── README.md
```

## Installation

### Prerequisites

- Python 3.8 through 3.13 (see [Windows 7 Notes](#windows-7-notes) if using Win7)
- [uv](https://github.com/astral-sh/uv) package manager

### Installing uv

**macOS/Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or via pip:
```bash
pip install uv
```

**Windows 10+/11:**
```powershell
powershell -Command "irm https://astral.sh/uv/install.ps1 | iex"
```

**Windows 7:** See [Windows 7 Notes](#windows-7-notes) below — requires manual installation of uv < 0.1.40.

### Setup (All Platforms)

1. Clone the repository:
   ```bash
   cd /path/to/PLT-Optimizer
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   uv sync
   ```

3. (Optional) Enable plotting/diagnostics (requires Python 3.9+):
   ```bash
   uv sync --extra plotting
   ```

4. (Optional) Install development dependencies for testing:
   ```bash
   uv sync --extra dev
   ```

5. (Optional) For system tray GUI mode on **Windows only**:
   ```powershell
   # On Windows only - installs winshell and pywin32 for startup shortcut management
   uv sync --extra tray
   ```

6. (Optional) To build standalone executable (requires Windows):
   ```powershell
   # Install build tools (maintainer/release builder only)
   uv sync --extra build

   # Build with PyInstaller
   uv run pyinstaller --noconsole --windowed --name PLT-Optimizer ^
       --icon=assets/icon.ico --add-data "assets/icon.ico;assets" ^
       run_tray.py
   ```

### Windows-Specific Setup

#### Standalone Installation (Windows 10+/11)

If you want to run `plt-optimizer` from any directory without using `uv run`:

```powershell
# Add the uv-managed Python scripts directory to your PATH
# The default user-level install path is:
$env:PATH += ";$env:APPDATA\Python\Scripts"

# Or for system-wide installation:
# Run PowerShell as Administrator
uv sync --system
```

After this, you can run `plt-optimizer` directly from any command prompt or PowerShell window.

#### Portable Installation (Windows 10+/11)

For portable deployments on Windows (e.g., running from a USB drive or network share):

```powershell
# Install to a specific directory
uv sync --python C:\Python311 --path ./plt-optimizer-portable

# Run the watch daemon
.\plt-optimizer-portable\Scripts\plt-optimizer.exe watch --watch-dir D:\PlotterFiles\Input --output-dir D:\PlotterFiles\Output
```

---

## Windows 7 Notes

**Important:** Python 3.8 and uv < 0.1.40 are required for Windows 7 compatibility.

> **Windows 7 is CLI-only.** The pre-built `Ploptimizer.exe` installer and the
> system tray GUI are **not supported** on Windows 7 — the CI-built executable
> requires Windows 10+. The supported way to run PLT-Optimizer on Windows 7 is
> the headless **CLI watch daemon** (`plt-optimizer watch`), which runs on
> Python 3.8 without matplotlib. Follow Steps 1–3 below, then
> [Step 4](#step-4-run-the-watch-daemon-cli) and
> [Step 5](#step-5-autostart-via-task-scheduler) to run it as a boot task.

Windows 7 cannot use:
- Python 3.9+ (unsupported by Microsoft)
- uv >= 0.1.40 (requires `bcryptprimitives.dll` from Windows 10+)
- matplotlib plotting (requires Python 3.9+)
- the system tray GUI / pre-built `Ploptimizer.exe` installer (Windows 10+ only)
- the `generate` subcommand (its text-rendering stack needs matplotlib; `optimize`
  and `watch` work fine)

### Step 1: Install uv 0.1.39 manually

Download uv 0.1.39 from GitHub releases:

1. Go to https://github.com/astral-sh/uv/releases/tag/0.1.39
2. Download `uv-x86_64-pc-windows-msvc.zip` (64-bit) or `uv-i686-pc-windows-msvc.zip` (32-bit)
3. Extract the zip file
4. Move `uv.exe` to a directory in your PATH (e.g., `C:\Windows\System32\`)

Or from Command Prompt:
```batch
:: For 64-bit Windows 7
powershell -Command "Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/download/0.1.39/uv-x86_64-pc-windows-msvc.zip' -OutFile '%USERPROFILE%\Downloads\uv.zip'"
powershell -Command "Expand-Archive -Path '%USERPROFILE%\Downloads\uv.zip' -DestinationPath '%USERPROFILE%\Downloads\uv'"
copy %USERPROFILE%\Downloads\uv\uv-x86_64-pc-windows-msvc\uv.exe C:\Windows\System32\
```

### Step 2: Install Python 3.8

Download and install Python 3.8.x from python.org:
- https://www.python.org/downloads/release/python-3816/
- Ensure "Add Python to PATH" is checked
- Windows 7 SP1 must have [KB2533623 update](https://support.microsoft.com/en-us/topic/update-to-the-api-ms-win-core-path-helpers Tlpb) installed

### Step 2b: Install Visual C++ Redistributable

Many Python packages (including watchdog) require the Visual C++ Redistributable.
If you see errors like "the program can't start because VCRUNTIME140.dll is missing":

Download and install from:
- https://microsoft.com/en-us/download/details.aspx?id=48145

Or from Command Prompt (run as Administrator):
```batch
:: For 64-bit Windows 7
powershell -Command "Invoke-WebRequest -Uri 'https://download.microsoft.com/download/9/3/F/93FBF6C0-B1B2-4EBA-A501-48D11A246762/vc_redist.x64.exe' -OutFile '%TEMP%\vc_redist.x64.exe'"
%TEMP%\vc_redist.x64.exe /quiet /norestart
```

### Step 3: Install PLT-Optimizer

```batch
cd C:\path\to\plt-optimizer
uv venv --python 3.8
uv pip install -e .
```

Note: Plotting/diagnostic features (matplotlib) are not available on Windows 7.
The `optimize` and `watch` commands work fully; the `generate` command requires
matplotlib and therefore Python 3.9+ (run it on another machine).

### Step 4: Run the Watch Daemon (CLI)

The watch daemon is the supported Windows 7 workflow. Verify it runs:

```batch
uv run plt-optimizer watch --watch-dir D:\PlotterFiles\Watch --output-dir D:\PlotterFiles\Optimized --log-dir D:\Logs
```

This installs a console script at `.venv\Scripts\plt-optimizer.exe` inside the
project's virtual environment — use that path directly in Step 5 (it does not
depend on `uv` being on the PATH of the scheduled-task account).

Behaviour reminders (full list: `plt-optimizer watch --help`):
- Files are processed after they have been quiet for `--debounce-seconds`
  (default 2.0) and their OS lock has been released.
- Without `--processed-dir`, successfully optimized originals are **deleted**
  from the watch directory; with it, they are moved there for archiving.
- Failed files are copied to the output directory as `<name>_unprocessed.plt`.

### Step 5: Autostart via Task Scheduler

Register a task that starts the daemon at boot (run Command Prompt **as
Administrator**):

```batch
schtasks /create /tn "PLT-Optimizer Watch" ^
    /tr "C:\path\to\plt-optimizer\.venv\Scripts\plt-optimizer.exe watch --watch-dir D:\PlotterFiles\Watch --output-dir D:\PlotterFiles\Optimized --log-dir D:\Logs" ^
    /sc onstart /ru SYSTEM /rl highest /f
```

Notes:
- `/sc onstart` runs the task whether or not a user is logged on; use
  `/ru "YourUser" /rp` instead of `/ru SYSTEM` if the watch/output directories
  live on a mapped network drive or need your user's credentials.
- The watch daemon is long-running: in `taskschd.msc`, uncheck **"Stop the task
  if it runs longer than"** on the task's Settings tab.

Or use the GUI (`taskschd.msc` → **Create Basic Task**):

1. Name: `PLT-Optimizer Watch`
2. Trigger: **When the computer starts**
3. Action → Start a program:
   - Program: `C:\path\to\plt-optimizer\.venv\Scripts\plt-optimizer.exe`
   - Arguments: `watch --watch-dir D:\PlotterFiles\Watch --output-dir D:\PlotterFiles\Optimized --log-dir D:\Logs`
   - Start in: `C:\path\to\plt-optimizer`
4. Check **Run whether user is logged on or not**, then on the Settings tab
   uncheck **Stop the task if it runs longer than**.

Verifying:

```batch
schtasks /run /tn "PLT-Optimizer Watch"
schtasks /query /tn "PLT-Optimizer Watch" /v /fo LIST
type D:\Logs\optimizer.log
```

The daemon writes `optimizer.log` + `job_metrics.csv` to the log directory and
logs a `Watch daemon stopped.` line on graceful shutdown. Stop it with
`schtasks /end /tn "PLT-Optimizer Watch"`.

> **Do not build the tray executable on Windows 7.** The PyInstaller build of
> `run_tray.py` (system tray GUI) is only supported on Windows 10+; see
> [System Tray Application (GUI)](#system-tray-application-gui).

---

### Verifying Installation

```powershell
# Check that plt-optimizer is accessible
uv run plt-optimizer --help

# Expected output:
# usage: plt-optimizer [-h] {optimize,generate,watch} ...
#
# PLT-Optimizer: HPGL processing and CAM generation suite.
#
# positional arguments:
#   {optimize,generate,watch}
#                         Available commands
```

## Usage

### Basic API Usage

```python
from pathlib import Path
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.writer import PLTWriter

# Parse a PLT file
parser = PLTParser()
document = parser.parse_file(Path("input.plt"))

# Access document properties
print(f"Total segments: {document.total_segments}")
print(f"Cutting distance: {document.cutting_distance():,.2f}")

# Write back to file
writer = PLTWriter()
writer.write_file(document, Path("output.plt"))
```

### Diagnostic Visualization

```python
from plt_optimizer.diagnostics.plotter import plot_plt_document

# Generate visualization
fig = plot_plt_document(
    document,
    output_path=Path("toolpath.png"),
    title="Toolpath Diagnostic"
)
```

### CLI Subcommands

The `plt-optimizer` entry point (`main.py`) routes three subcommands: `optimize`, `generate`, and `watch`. Each supports `--help`.

#### `optimize` — Single-File Optimization

```bash
uv run plt-optimizer optimize input.plt -o output.plt
uv run plt-optimizer optimize input.plt --fast-mode -v
```

**Options:**
- `input` (required): Input PLT/HPGL file
- `-o`, `--output` (default: `<input>_optimized.plt` beside the input): Output path
- `--fast-mode`: Use only `NearestNeighbor2OptStrategy` (default: ParallelEnsemble)
- `-v`, `--verbose`: DEBUG console output
- `--log-dir` (default: `./logs_optimize`): Where `optimizer.log` + `job_metrics.csv` are written

The pipeline profiles the document first (text vs. structural): structural files are fractured and de-duplicated before optimization; text files skip stroke simplification to preserve contiguous paths.

#### `generate` — YAML Specification to Per-Cutter PLT

```bash
uv run plt-optimizer generate spec.yaml -o out/ --no-plots
uv run plt-optimizer generate tests_deps/test123_spec.yaml
```

**Options:**
- `spec` (required): Path to the YAML job specification
- `-o`, `--output` (default: the spec's parent directory): Receives `plt/` and `pdf/` subdirectories
- `-v`, `--verbose`: DEBUG output
- `--no-plots`: Skip simple-outline PDF previews (PLT files only)
- `--default-plots`: Also write color-coded `*_default.pdf` diagnostic plots (off by default; slow)
- `--tools` (default: `tools.json`): Cutter inventory JSON; ideal cutters are used if the file is missing

Outputs are named `<plate>_<kind>_<cutter>_<job_id>.plt` (`kind` = `text` or `bh` for borders+holes). Text–hole collisions abort the job with a non-zero exit code — see [`INTEGRATION_TESTING.md`](INTEGRATION_TESTING.md).

**Job spec schema:** see the "YAML Job Specification" section of [`AGENTS.md`](../AGENTS.md) and the example specs in `examples/job_specs/` (`sample_spec`-style jobs, `replacement_job.yaml`) plus the frozen unit-test fixtures in `tests_deps/` (`test123_spec.yaml`, `complex_test_job.yaml`, `sample_spec.yaml`, `rotation_demo_job.yaml`).

#### `watch` — Hot-Watch Daemon

The watch daemon monitors a directory for new or modified PLT files and automatically optimizes them:

```bash
uv run plt-optimizer watch --watch-dir /input/plt \
                           --output-dir ./optimized \
                           --log-dir ./logs
```

**Options:**
- `--watch-dir` (required): Directory to monitor for PLT files
- `--output-dir` (default: `./optimized`): Where optimized files are saved
- `--log-dir` (default: `./logs`): Log file directory
- `--processed-dir` (optional): Move processed files here after optimization; without it, originals are **deleted** from the watch directory
- `--fast-mode`: Use only `NearestNeighbor2OptStrategy` for faster processing
- `--debug-save-files`: Save before/after PLT files and comparison plots to a `debug/` subdirectory of the log directory (only effective with `--log-dir`)
- `--debounce-seconds` (default: 2.0): Quiet period after the last modification before a file is processed; also waits for OS file locks to release

The daemon processes existing files on startup, then continues watching for new changes. Press Ctrl+C for graceful shutdown.

**Robustness details:**
- Optimized files are staged in `<output-dir>/.incomplete/` and moved into place atomically (`os.replace`, with a `shutil.move` fallback), so downstream consumers never observe partially-written files.
- Files that fail optimization are copied to the output directory as `<name>_unprocessed.plt` for manual review, and the original is removed from the watch directory.
- The original file is deleted (or archived) only after a successful write.

### System Tray Application (GUI)

**Note:** The system tray application is **Windows-only** and requires
**Windows 10+** (the pre-built `Ploptimizer.exe` is not supported on Windows 7 —
see [Windows 7 Notes](#windows-7-notes) for the CLI-only watch daemon there). It
also requires `winshell` and `pywin32`, which are Windows-specific packages.

For a graphical interface with system tray icon and notifications:

```powershell
# On Windows only:
python run_tray.py
```

**Features:**
- System tray icon with context menu (Open Settings, Exit)
- Background file watcher running in a separate thread
- Native Windows notifications on file processing completion
- Settings window for configuring directories and options
- "Run at Windows Startup" checkbox for auto-start

The tray application stores configuration in `%LOCALAPPDATA%\PLT-Optimizer\config.json`.

#### Windows-Specific Usage

**Using PowerShell:**
```powershell
# Navigate to project directory
cd C:\PLT-Optimizer

# Run the watch daemon
uv run plt-optimizer watch --watch-dir D:\PlotterFiles\Watch --output-dir D:\PlotterFiles\Optimized --log-dir D:\Logs
```

**Using Command Prompt:**
```cmd
cd /d C:\PLT-Optimizer
uv run plt-optimizer watch --watch-dir D:\PlotterFiles\Watch --output-dir D:\PlotterFiles\Optimized --log-dir D:\Logs
```

**UNC Network Paths:**
The daemon supports UNC paths for network drives:
```powershell
uv run plt-optimizer watch --watch-dir \\Server\Plotter\Input --output-dir \\Server\Plotter\Output --log-dir C:\Logs
```

#### Starting the Watch Daemon at Boot (Windows)

##### Method 1: System Tray Application (Recommended)

The simplest method is to use the **system tray application** which provides a "Run at Startup" checkbox in its settings:

1. Run `run_tray.py` or launch the compiled executable (`PLT-Optimizer.exe`)
2. Right-click the system tray icon → **Open Settings**
3. Configure your watch directory, output directory, and other options
4. Check **Run at Windows Startup**
5. Click Save

The application will now start automatically when you log in to Windows, with no console window visible.

**Requirements:**
- **Windows only** - the system tray app uses Windows-specific APIs
- Core dependencies install automatically with `uv sync`
- For startup shortcut management, also run:
  ```powershell
  uv sync --extra tray
  ```
  This installs: `winshell` and `pywin32`

##### Method 2: Standalone Executable (PyInstaller)

**Note:** This build step must be run on **Windows**. The system tray functionality requires Windows APIs.

1. On **Windows**, install all dependencies:
   ```powershell
   uv sync --extra dev --extra tray --extra build
   ```

2. Create an icon file at `assets/icon.ico` (64x64 or 128x128)

3. Build the executable:
   ```powershell
   uv run pyinstaller --onefile --noconsole `
      --name "Ploptimizer" `
      --add-data "assets;assets" `
      --icon "assets/icon.ico" `
      run_tray.py
   ```

4. The compiled executable will be in `dist/PLT-Optimizer.exe`

##### Method 3: Task Scheduler (Recommended)

1. Open **Task Scheduler** (`taskschd.msc`)

2. Click **Create Basic Task** → Name it "PLT-Optimizer Watch"

3. Set Trigger: **When the computer starts**

4. Set Action: **Start a program**
   - Program: `C:\PLT-Optimizer\.venv\Scripts\plt-optimizer.exe`
   - Arguments: `watch --watch-dir D:\PlotterFiles\Watch --output-dir D:\PlotterFiles\Optimized --log-dir D:\Logs`
   - Start in: `C:\PLT-Optimizer`

   (Using the venv console script avoids depending on `uv` being on the PATH of
   the scheduled-task account. A `cmd.exe /c cd /d C:\PLT-Optimizer && uv run
   plt-optimizer watch ...` action also works if you prefer `uv run`.)

5. Configure:
   - Check **Run whether user is logged on or not** (requires password)
   - Check **Run with highest privileges** if writing to protected directories
   - On the Settings tab, **uncheck "Stop the task if it runs longer than"** —
     the watch daemon is a long-running process

6. Click **OK** and enter your Windows password when prompted.

> On **Windows 7**, use the dedicated
> [Task Scheduler setup](#step-5-autostart-via-task-scheduler) in the Windows 7
> Notes (identical approach, `schtasks` one-liner included).

##### Method 4: Windows Service (Advanced)

For a persistent background service that survives user logoff, use NSSM (Non-Sucking Service Manager):

```powershell
# Install NSSM via Chocolatey
choco install nssm -y

# Or download from https://nssm.cc/download

# Create the service (run PowerShell as Administrator)
$nssm = "C:\Program Files\nssm\win64\nssm.exe"

& $nssm install PLT-Optimizer "C:\Users\<YourUser>\.local\bin\uv.exe" "run plt-optimizer watch --watch-dir D:\PlotterFiles\Watch --output-dir D:\PlotterFiles\Optimized --log-dir D:\Logs"
# Note: Use full path to uv.exe from your user directory

# Configure startup type
& $nssm set PLT-Optimizer Start SERVICE_AUTO_START

# Start the service
& $nssm start PLT-Optimizer

# Check status
& $nssm status PLT-Optimizer
```

##### Method 5: Startup Folder Shortcut

For a simple user-level auto-start:

1. Press `Win + R`, type `shell:startup`, press Enter

2. Create a shortcut:
   - Right-click → New → Shortcut
   - Location: `cmd.exe /k cd /d C:\PLT-Optimizer && uv run plt-optimizer watch --watch-dir D:\PlotterFiles\Watch --output-dir D:\PlotterFiles\Optimized --log-dir D:\Logs`
   - Name: "PLT-Optimizer Watch"

3. The daemon will start when you log in, running in a visible console window.

##### Verifying the Service

```powershell
# Check logs
Get-Content D:\Logs\optimizer.log -Tail 20 -Wait

# Or for Task Scheduler tasks:
Get-ScheduledTask | Where-Object {$_.TaskName -like "*PLT*"}
Get-ScheduledTaskInfo -TaskName "PLT-Optimizer Watch"
```

##### Stopping the Service

```powershell
# For Task Scheduler (if running)
Stop-ScheduledTask -TaskName "PLT-Optimizer Watch"

# For NSSM service
& $nssm stop PLT-Optimizer
```

### Running the Example Script

The diagnostics script processes all example PLT files and generates before/after comparison plots:

```bash
uv run python examples/run_diagnostics.py
```

On Windows:
```powershell
cd C:\PLT-Optimizer
uv run python examples/run_diagnostics.py
```

This will:
1. Process all `.plt` files in `examples/` directory
2. Generate three diagnostic plots for each file:
   - **Before plot** (color-coded toolpath with rapid travel shown)
   - **Simple outline** (clean black lines only, no rapid travel)
   - **After optimization** (optimized toolpath with improvements)
3. Save all plots and optimized PLT files to `examples_diag_output/`
4. Write logs to `logs/optimizer.log` and `logs/job_metrics.csv`
5. Display summary statistics showing optimization improvements

**Optional flags:**
- `--no-optimize`: Skip optimization pipeline (diagnostics only)
- `--input-dir /path/to/files`: Process a specific directory instead of `./examples`
- `--output-dir /path/to/output`: Save output to a custom directory instead of `<input-dir>_diag_output`
- `--same-row-preference 1.5`: Adjust the penalty for vertical movement (higher values prefer same-row blocks)

**Note:** The `logs/` directory is created automatically relative to the working directory where you run the command.

## Testing

Run the test suite with pytest:

```bash
uv run pytest tests/ -v
```

On Windows:
```powershell
cd C:\PLT-Optimizer
uv run pytest tests/ -v
```

Run with coverage reporting:

```bash
uv run pytest --cov=plt_optimizer --cov-report=term-missing tests/
```

**Windows-specific test notes:**
- The parser includes automatic handling of Windows (`\r\n`) and Unix (`\n`) line endings
- Tests verify CRLF compatibility in `test_parser.py::test_parse_windows_line_endings`
- All file paths use `pathlib.Path` for cross-platform compatibility

### Test Categories

The suite mirrors the package layout; highlights:

- **test_identity.py**: Round-trip identity validation ensuring parse→write→parse consistency
- **test_parser.py / test_writer.py**: Parser accuracy/error handling and writer output formatting
- **test_optimizer*.py / test_intra_chunk_optimizer.py / test_chunker.py / test_reassembler.py / test_profiler.py**: Optimization pipeline
- **test_schema.py / test_resolution.py / test_substitution.py**: YAML contract, cascade/cutter resolution, replacement text files
- **test_layout.py**: Bin packing, rotation, fit guards
- **test_label_renderer*.py / test_ftext_renderer.py / test_label3_centering.py**: Label rendering, compression/alignment, glyph vectorization
- **test_collision_detection.py / test_collision_resolution.py**: Text–hole collision detection and avoidance phases
- **test_vectorize_phase3.py / test_phase3_export.py**: Per-cutter export and file naming
- **test_cli.py**: `optimize` / `generate` / `watch` subcommand entry points
- **test_watch.py**: Watch daemon debounce, locking, archiving, and shutdown seams
- **test_tray.py / test_settings.py / test_startup.py / test_config.py**: Tray UI and configuration (Windows paths mocked)

### End-to-End Integration Test

`run_integration_test.py` drives the full generate pipeline (YAML → resolution →
bin packing → per-cutter PLT export → coordinate validation) with intermediate
dumps:

```bash
uv run python run_integration_test.py
uv run python run_integration_test.py tests_deps/complex_test_job.yaml
```

Artifacts land under `test_output/integration_test/`. See
[`INTEGRATION_TESTING.md`](INTEGRATION_TESTING.md) for phases, fixtures, and a
verification checklist.

## HPGL/PLT Format Reference

The parser handles standard HPGL commands from EngraveLab:

| Command | Description |
|---------|-------------|
| `IN;` | Initialize |
| `VS<n>;` | Velocity Select |
| `ZO<x>,<y>;` | Zoom |
| `PA;` | Plot Absolute (a single command may carry multiple `x,y` pairs) |
| `PU<x>,<y>;` | Pen Up (rapid move) |
| `PD<x>,<y>;` | Pen Down (cutting move) |
| `AA<x>,<y>,<start>,<sweep>;` | Arc (center + angles; emitted for drill holes) |
| `SP<n>;` | Select Pen (resets drawing position — a pen change starts a fresh context) |

### Coordinate System

- Coordinates are preserved to 3 decimal places
- Path data is represented as sequences of `PU`/`PD` commands:
  - `PU` = rapid air travel (pen up, no cutting)
  - `PD` = cutting stroke (pen down)

## Logging

PLT-Optimizer uses a dual logging system:

### Text Log (`logs/optimizer.log`)

Standard Python logging with hierarchical levels:
```
2024-01-15 10:30:45 | INFO     | plt_optimizer.core.parser | Parsing PLT file: input.plt
2024-01-15 10:30:45 | DEBUG    | plt_optimizer.core.writer | Writing 18288 characters of PLT output
```

### CSV Metrics (`logs/job_metrics.csv`)

Job-level tracking for optimization analysis:
```csv
timestamp,job_id,original_file,optimized_file,original_total_distance,optimized_total_distance,percent_improvement,status,method,notes
2024-01-15T10:30:45,job_001,input.plt,output.plt,18288.500,14200.300,22.35%,success,Insertion Heuristic,"..."
```

## Development

### Code Style

The project follows:
- **Ruff/Black** formatting standards
- Strict PEP 484 type hints
- Google-style docstrings

#### Pre-commit Hooks (Recommended)

Install pre-commit to run linting and type checks before every commit:

```bash
uv pip install pre-commit
pre-commit install
```

Now `ruff check`, `ruff format`, and `mypy` will run automatically on each `git commit`.

#### Manual Checks

Run the linter:
```bash
uv run ruff check plt_optimizer/
```

Format code:
```bash
uv run ruff format plt_optimizer/
```

Type checking:
```bash
uv run mypy plt_optimizer/
```

### Adding New Commands

To add support for additional HPGL commands:

1. Add the command mnemonic to `PLTParser._is_header_command()`
2. Update `HeaderCommand.from_token()` if special parsing is needed
3. Add corresponding test cases in `tests/`

## Troubleshooting (Windows)

### Common Issues

| Issue | Solution |
|-------|----------|
| `uv: command not found` | Restart your terminal or run `&$env:LOCALAPPDATA\Programs\Python\Python311\python.exe -m uv` |
| Service won't start | Verify paths use backslashes properly, or use forward slashes with `pathlib.Path` |
| Files not being processed | Check that the watch directory path exists and has appropriate permissions |
| High CPU usage | Use `--fast-mode` for simpler optimization; reduce polling by running as a scheduled task |

### Viewing Logs

```powershell
# Real-time log monitoring
Get-Content D:\Logs\optimizer.log -Tail 20 -Wait

# Search for errors
Select-String -Path "D:\Logs\optimizer.log" -Pattern "ERROR|CRITICAL"

# Check metrics CSV
Import-Csv D:\Logs\job_metrics.csv | Sort-Object timestamp -Descending | Select-Object -First 10
```

### Firewall Considerations

If running the watch daemon on a network path, ensure Windows Defender or your firewall allows Python/uv through:
```powershell
# Allow Python through firewall (if needed for network access)
New-NetFirewallRule -DisplayName "PLT-Optimizer Python" -Direction Inbound -Program "C:\Users\<User>\.local\Programs\Python\Python311\python.exe" -Action Allow
```

### Performance Tips

1. **Fast Mode**: Use `--fast-mode` flag when processing speed is more important than optimal routing
2. **Local Storage**: Place watch directories on local drives rather than network shares when possible
3. **Processed Directory**: Use `--processed-dir` to move completed files out of the watch directory, reducing scan time

## CI/CD

This project uses GitHub Actions for continuous integration and automated builds:

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| **CI** | Push to main / PRs | Lint, type check, run tests |
| **Build** | Push to main / Tags | Build Windows executable |

### Automated Build Process

1. On every push to `main`, the build workflow:
   - Creates a Windows executable via PyInstaller
   - Uploads it as an artifact (downloadable from Actions tab)

2. On every git tag, the build workflow:
   - Creates a proper release with the `.exe` attached
   - Users can download `PLT-Optimizer.exe` from GitHub Releases

## See Also

- [Simpler Java implementation of PLT optimization by Fugazza](https://github.com/fugazza/PLTtools)
- [HPGL reference by Paul Bourke](http://paulbourke.net/dataformats/hpgl/)
