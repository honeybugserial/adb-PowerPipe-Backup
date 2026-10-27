#!/usr/bin/env python
# MIT License
#
# adb-PowerPipe.py -- a PySide6 GUI for fast Android backups over adb.
#
# Streams device directories with `adb exec-out "tar -c -C <parent> <name>"` and
# writes them straight to .tar / .tar.gz files, counting bytes as they flow so
# you get real throughput, a percentage bar, and an ETA -- the thing that plain
# `tar > file` can't give you (tar emits a byte stream with no total, so the
# only way to show progress is to measure the stream as it passes through).
#
# Why exec-out + a Python-side binary write:
#   * exec-out (not `adb shell`) skips the pty layer that mangles binary data.
#   * writing the archive from Python in "wb" avoids the Windows `>` corruption.
#
# Requires : PySide6            ->  pip install PySide6
#            (Python < 3.11 also needs `pip install tomli` for TOML reading.)
# Run      : python adb-PowerPipe.py

import os
import sys
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import atexit
import urllib.request
import zipfile
from datetime import datetime

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # pip install tomli

from PySide6.QtGui import (
    QAction, QFontDatabase, QPixmap, QPainter, QColor, QPen, QBrush, QIcon,
)
from PySide6.QtCore import QObject, QThread, Signal, QTimer, Qt, QRectF, QPointF
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QComboBox,
    QCheckBox, QProgressBar, QPlainTextEdit, QFileDialog, QMessageBox,
    QGroupBox, QVBoxLayout, QHBoxLayout, QFormLayout, QSizePolicy,
    QMenuBar, QTreeWidget, QTreeWidgetItem, QStyle, QSplitter,
)

# Optional startup splash (see splash_screen.py). Best-effort: if the module
# or its image is missing, the app just starts without a splash.
try:
    from splash_screen import show_splash
except Exception:
    show_splash = None


# ---------------------------------------------------------------------------
#  Script identity
# ---------------------------------------------------------------------------
SCRIPT_TITLE   = "adb-PowerPipe: BACKUP"
SCRIPT_VERSION = "v1.26"
SCRIPT_TAG     = "a daily source of fibre for your datas"

# ---------------------------------------------------------------------------
#  Paths (PyInstaller-aware)
#
#  BASE_DIR is where the app lives -- next to the frozen exe when bundled,
#  next to this source file otherwise. CONFIG_PATH sits beside the app so the
#  TOML travels with it and survives freezing.
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "config.toml")

# Default place for backups: a "backups" directory next to the app. Each run gets
# its own timestamped subdirectory underneath (backup_<datetime>).
DEFAULT_BACKUPS_DIR = os.path.join(BASE_DIR, "backups")

# ---------------------------------------------------------------------------
#  UI layout / tunables -- tweak these to resize things or change the log
#  scroll speed without hunting through the widget code below.
# ---------------------------------------------------------------------------
WINDOW_MIN_WIDTH       = 860   # main window minimum width  (px) -- two panes
WINDOW_MIN_HEIGHT      = 620   # main window minimum height (px)
SPLIT_LEFT_WIDTH       = 320   # initial width of the file-browser pane (px)
SPLIT_RIGHT_WIDTH      = 460   # initial width of the controls pane     (px)
DIRECTORY_TREE_MIN_HEIGHT = 240   # minimum height of the live browser tree (px)
LOG_MIN_HEIGHT         = 140   # minimum height of the log box (px)
CHECKBOX_PX            = 18    # size of the tree's tri-state checkbox marks (px)
LOG_STREAM_INTERVAL_MS = 140   # delay between streamed log lines (ms); lower = faster
LS_TIMEOUT_SEC         = 20    # per-directory `ls` timeout when browsing (s)

# Roles for stashing per-item data on tree nodes (the live directory browser).
ROLE_PATH   = int(Qt.ItemDataRole.UserRole)
ROLE_ISDIR  = int(Qt.ItemDataRole.UserRole) + 1
ROLE_LOADED = int(Qt.ItemDataRole.UserRole) + 2

# ---------------------------------------------------------------------------
#  Google platform-tools -- direct "latest" zips, per OS. Offered when adb
#  can't be found at startup.
# ---------------------------------------------------------------------------
PLATFORM_TOOLS_URLS = {
    "win32":  "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    "linux":  "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
}

# ---------------------------------------------------------------------------
#  Config -- defaults live here; config.toml overrides them and is written
#  back out from these values if it's missing.
# ---------------------------------------------------------------------------
CHUNK_MIB_FLOOR = 1  # smallest allowed pipe read size, in MiB

DEFAULTS = {
    "backup":  {"output_dir": "", "compress": False, "verify": True, "chunk_mib": 1},
    "directory": {"root": "/sdcard"},   # top of the live browser tree
    "ui":      {"theme": "Fusion"},
}

DEFAULT_CONFIG_TEXT = """\
# adb-PowerPipe configuration.
# Edit this file to change defaults without touching the code.
# It's re-created with these defaults if you delete it.

[backup]
output_dir = ""      # where backups are saved. "" = a "backups" directory next to the app.
compress = false     # gzip the stream. Off is right for photos/video.
verify = true        # re-open each archive afterward and count entries.
chunk_mib = 1        # pipe read size in MiB. 1 is a fine default.

[directories]
# Where the live device browser starts. The tree loads this directory and
# lets you expand into it; tick whatever you want backed up.
root = "/sdcard"

[ui]
theme = "Fusion"     # Qt style. "Fusion" is consistent across platforms.
"""


# ===========================================================================
#  Config load / save
# ===========================================================================

def _merge(base, override):
    """Recursively overlay override onto base, returning a new dict."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path=CONFIG_PATH):
    """Load config.toml, creating it from defaults if absent.

    Always returns a fully-populated dict -- missing keys fall back to DEFAULTS,
    so a partial or hand-edited file can never leave the app short a setting.
    """
    if not os.path.exists(path):
        write_default_config(path)

    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        data = {}

    return _merge({k: dict(v) for k, v in DEFAULTS.items()}, data)


def write_default_config(path=CONFIG_PATH):
    """Write the stock config.toml. Returns True on success."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_CONFIG_TEXT)
        return True
    except OSError:
        return False


def _toml_str(val):
    """Quote a scalar as a TOML basic string."""
    return '"' + str(val).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(cfg, path=CONFIG_PATH):
    """Write the config back out. Minimal hand-rolled writer for our known shape."""
    bak = cfg["backup"]
    fld = cfg["directories"]
    text = (
        "# adb-PowerPipe configuration (saved from the app).\n\n"
        "[backup]\n"
        f"output_dir = {_toml_str(bak['output_dir'])}\n"
        f"compress = {'true' if bak['compress'] else 'false'}\n"
        f"verify = {'true' if bak['verify'] else 'false'}\n"
        f"chunk_mib = {int(bak['chunk_mib'])}\n\n"
        "[directories]\n"
        f"root = {_toml_str(fld['root'])}\n\n"
        "[ui]\n"
        f"theme = {_toml_str(cfg['ui']['theme'])}\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ===========================================================================
#  adb / platform / formatting helpers
# ===========================================================================

def _subprocess_kwargs():
    """Extra kwargs so console apps (adb) don't flash a black console window
    when the host is a windowed / frozen build on Windows.

    CREATE_NO_WINDOW (0x08000000) is the modern flag; getattr keeps us safe on
    older Python or non-Windows platforms where the name isn't defined.
    """
    if sys.platform.startswith("win"):
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def resource_path(name):
    """Absolute path to a bundled, read-only resource (icons, splash, etc.).

    PyInstaller unpacks files added with --add-data into sys._MEIPASS -- a
    temp dir for a --onefile build, or the app dir for --onedir -- which is
    NOT the directory the exe sits in. So a bundled resource must be looked up
    there first. We then fall back to BASE_DIR (a copy shipped loose next to
    the exe) and, for source runs, the script's own directory. Returns None
    if the name isn't found in any of them.

    (BASE_DIR stays next-to-exe on purpose for config.toml / backups, which
    must remain writable beside the app -- that's a different concern from
    read-only bundled assets, which is why they get their own lookup.)
    """
    for base in (getattr(sys, "_MEIPASS", None), BASE_DIR):
        if base:
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return path
    return None


def load_app_icon():
    """Load a window / taskbar icon for the app, if present.

    Looks for common names as bundled resources (works for source runs, a
    --onefile exe, and a --onedir exe). Returns a null QIcon when nothing is
    found -- Qt then falls back to its default. Note: PyInstaller's --icon
    only stamps the .exe file itself; the live window still needs
    setWindowIcon() for the title bar, which is what this feeds.
    """
    for name in ("icon.ico", "icon.png", "adb-PowerPipe.ico", "adb-PowerPipe.png"):
        path = resource_path(name)
        if path:
            icon = QIcon(path)
            if not icon.isNull():
                return icon
    return QIcon()


def adb_base(adb_path, serial):
    """Leading part of an adb invocation for a given device serial."""
    cmd = [adb_path]
    if serial:
        cmd += ["-s", serial]
    return cmd


def adb_available(adb_path):
    """True if adb resolves on PATH or points at an existing binary."""
    return bool(shutil.which(adb_path)) or os.path.isfile(adb_path)


def local_adb():
    """Return a script-dir adb binary if one is present, else None.

    Searches BASE_DIR itself and a BASE_DIR/platform-tools subdirectory (where the
    downloaded zip extracts to). A copy found here takes precedence over PATH,
    so a portable install 'just works' and overrides any system adb without the
    user touching config.
    """
    for sub in ("", "platform-tools"):
        base = os.path.join(BASE_DIR, sub) if sub else BASE_DIR
        for name in ("adb.exe", "adb"):
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _safe_remove(path):
    """Delete a file, swallowing any error."""
    try:
        os.remove(path)
    except OSError:
        pass


def platform_tools_url():
    """Pick the platform-tools zip for the current OS."""
    if sys.platform.startswith("win"):
        return PLATFORM_TOOLS_URLS["win32"]
    if sys.platform == "darwin":
        return PLATFORM_TOOLS_URLS["darwin"]
    return PLATFORM_TOOLS_URLS["linux"]


def open_in_editor(path):
    """Open a file with the OS default handler. Returns True on success."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: only exists on Windows; guarded above
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
        return True
    except Exception:
        return False


def list_devices(adb_path):
    """Return serials in the 'device' state, or None if adb can't be run."""
    try:
        # Quietly ensure the server is up first. Without this, the first
        # `adb devices` can spawn a visible console while the daemon starts.
        subprocess.run(
            [adb_path, "start-server"],
            capture_output=True, timeout=15, **_subprocess_kwargs(),
        )
        out = subprocess.run(
            [adb_path, "devices"], capture_output=True, text=True, timeout=10,
            **_subprocess_kwargs(),
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        return None

    devices = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def parse_ls(text):
    """Parse `ls -1aF` output into (name, is_dir) tuples.

    -F appends a type suffix: '/' for dirs, one of '*@=|' for other special
    kinds. We only care about '/', everything else counts as a file for now.
    Dirs sort first, then case-insensitive by name.
    """
    entries = []
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if not line:
            continue
        is_dir = line.endswith("/")
        name = line[:-1] if line[-1:] in "/*@=|" else line
        if name in (".", ".."):
            continue
        entries.append((name, is_dir))
    entries.sort(key=lambda e: (not e[1], e[0].lower()))
    return entries


def estimate_bytes(adb_path, serial, path):
    remote = f"du -sk {shlex.quote(path)}"
    cmd = adb_base(adb_path, serial) + ["exec-out", "sh", "-c", remote]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, **_subprocess_kwargs()
        )
        for token in out.stdout.split():
            if token.lstrip("-").isdigit():
                size_kb = int(token)
                if size_kb >= 0:
                    return size_kb * 1024
        return 0
    except Exception:
        return 0


def human(num):
    """Human-readable byte count."""
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def fmt_eta(seconds):
    """Format an ETA as m:ss or h:mm:ss; '--:--' when unknown."""
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"


# ===========================================================================
#  Worker -- runs the transfer off the UI thread, talks back via signals
# ===========================================================================

class BackupWorker(QObject):
    total     = Signal(int)                # total estimated bytes
    progress  = Signal(int, float, float)  # done_bytes, bytes/s, eta_seconds
    status    = Signal(str)
    log       = Signal(str)
    done      = Signal(int)                # total bytes written
    cancelled = Signal()
    fatal     = Signal(str)

    def __init__(self, adb_path, serial, paths, outdir, compress, verify, chunk):
        super().__init__()
        self.adb_path = adb_path
        self.serial   = serial
        self.paths    = paths
        self.outdir   = outdir
        self.compress = compress
        self.verify   = verify
        self.chunk    = chunk
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        # --- size pass: gives the progress bar a real denominator ---
        self.status.emit("Estimating sizes...")
        sizes, grand_total = {}, 0
        for path in self.paths:
            size = estimate_bytes(self.adb_path, self.serial, path)
            sizes[path] = size
            grand_total += size

        grand_total = max(0, grand_total)   # never allow a negative total
        self.total.emit(grand_total)

        done = 0
        speed_ema = 0.0  # smoothed rate, so the ETA doesn't jitter per file

        for path in self.paths:
            if self.cancel_event.is_set():
                self.cancelled.emit()
                return

            clean  = path.rstrip("/")
            name   = clean.split("/")[-1] or "root"
            parent = clean.rsplit("/", 1)[0] or "/"
            ext    = ".tar.gz" if self.compress else ".tar"
            # self.outdir is the per-run directory (backup_<datetime>), so the
            # timestamp is already in the path -- files are just <name>.tar here.
            out_file = os.path.join(self.outdir, f"{name}{ext}")

            remote = f"tar -c -C {shlex.quote(parent)} {shlex.quote(name)}"
            if self.compress:
                remote += " | gzip"
            cmd = adb_base(self.adb_path, self.serial) + ["exec-out", "sh", "-c", remote]

            self.status.emit(f"{name} -> {os.path.basename(out_file)}  ({human(sizes[path])})")
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, **_subprocess_kwargs()
                )
            except FileNotFoundError:
                self.fatal.emit("adb not found -- check the adb path.")
                return

            file_bytes = 0
            last_t     = time.time()
            last_done  = done
            try:
                with open(out_file, "wb") as fh:
                    while True:
                        if self.cancel_event.is_set():
                            proc.terminate()
                            self.cancelled.emit()
                            return

                        buf = proc.stdout.read(self.chunk)
                        if not buf:
                            break

                        fh.write(buf)
                        file_bytes += len(buf)
                        done       += len(buf)

                        now = time.time()
                        if now - last_t >= 0.3:
                            inst = (done - last_done) / (now - last_t)
                            speed_ema = inst if speed_ema == 0 else 0.3 * inst + 0.7 * speed_ema
                            remaining = max(grand_total - done, 0)
                            eta = remaining / speed_ema if speed_ema > 0 else -1
                            self.progress.emit(done, speed_ema, eta)
                            last_t, last_done = now, done
                proc.wait()
            except Exception as exc:
                self.log.emit(f"  ! {name}: {exc}")
                continue

            if proc.returncode not in (0, None):
                self.log.emit(f"  ! {name}: adb exited {proc.returncode}")

            if self.verify:
                try:
                    with tarfile.open(out_file) as archive:
                        count = sum(1 for _ in archive)
                    self.log.emit(f"  \u2713 {name}: {human(file_bytes)}, {count} entries")
                except Exception as exc:
                    self.log.emit(f"  ! {name}: verify failed -- {exc}")
            else:
                self.log.emit(f"  \u2713 {name}: {human(file_bytes)}")

        self.done.emit(done)


# ===========================================================================
#  Download worker -- fetches + extracts platform-tools off the UI thread
# ===========================================================================

class DownloadWorker(QObject):
    progress  = Signal(int, int)  # done_bytes, total_bytes (0 if unknown)
    status    = Signal(str)
    done      = Signal(str)       # path to the extracted adb binary
    failed    = Signal(str)
    cancelled = Signal()

    def __init__(self, url, dest_dir):
        super().__init__()
        self.url = url
        self.dest_dir = dest_dir
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        # --- download the zip straight into the script dir ---
        zip_name = os.path.basename(self.url)  # e.g. platform-tools-latest-windows.zip
        zip_path = os.path.join(self.dest_dir, zip_name)
        try:
            self.status.emit("Connecting to Google...")
            req = urllib.request.Request(self.url, headers={"User-Agent": "adb-PowerPipe"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                done = 0
                with open(zip_path, "wb") as fh:
                    while True:
                        if self.cancel_event.is_set():
                            _safe_remove(zip_path)  # drop the partial download
                            self.cancelled.emit()
                            return
                        buf = resp.read(65536)
                        if not buf:
                            break
                        fh.write(buf)
                        done += len(buf)
                        self.progress.emit(done, total)
        except Exception as exc:
            _safe_remove(zip_path)  # drop the partial download
            self.failed.emit(f"Download failed: {exc}")
            return

        # --- extract into the script dir (zip is kept alongside) ---
        try:
            self.status.emit("Extracting platform-tools...")
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(self.dest_dir)
        except Exception as exc:
            self.failed.emit(f"Extract failed: {exc}")
            return

        # --- locate + arm the extracted adb ---
        adb = local_adb()
        if not adb:
            self.failed.emit("Extracted, but adb wasn't found afterward.")
            return
        if not sys.platform.startswith("win"):
            try:
                mode = os.stat(adb).st_mode
                os.chmod(adb, mode | 0o111)  # ensure it's executable
            except OSError:
                pass

        self.done.emit(adb)


# ===========================================================================
#  Main window
# ===========================================================================

class MainWindow(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.total_bytes = 0
        self.thread = None
        self.worker = None
        self._updating = False  # guards programmatic tree check-state changes
        self.root_path = (cfg["directories"].get("root") or "/sdcard").rstrip("/") or "/"

        # adb is resolved once, up front: a script-dir copy wins, else PATH.
        # There's no editable field anymore -- the choice is shown in the log.
        self.adb_path = local_adb() or "adb"

        self.setWindowTitle(f"{SCRIPT_TITLE} {SCRIPT_VERSION}")
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        # Title-bar / taskbar icon (separate from PyInstaller's --icon, which
        # only stamps the .exe file itself).
        icon = load_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        # Standard file/directory icons for the browser tree.
        self.icon_dir  = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.icon_file = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

        # Two panes side by side: the file browser on the left, all the
        # controls (device, output, backup, log) stacked on the right. A
        # splitter lets the divider be dragged. Laying the tree beside the
        # controls -- instead of above them -- keeps the window from having to
        # be tall enough to stack everything, which is what used to crush the
        # bottom rows together.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setMenuBar(self._build_menu())

        left = QWidget()
        left_pane = QVBoxLayout(left)
        left_pane.setContentsMargins(0, 0, 0, 0)
        self._build_directories_group(left_pane)

        right = QWidget()
        right_pane = QVBoxLayout(right)
        right_pane.setContentsMargins(0, 0, 0, 0)
        self._build_device_group(right_pane)
        self._build_output_group(right_pane)
        self._build_run_group(right_pane)
        self._build_log_group(right_pane)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)   # tree pane gets the extra width
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([SPLIT_LEFT_WIDTH, SPLIT_RIGHT_WIDTH])
        outer.addWidget(splitter)

        self._startup_log()

    def _startup_log(self):
        """Build a formatted startup banner and stream it into the log so it
        scrolls in rather than appearing all at once."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rule = "-" * 65
        lines = [
            rule,
            f"  {SCRIPT_TITLE} {SCRIPT_VERSION}",
            f"  Started {now}",
            rule,
            "",
            "Detecting adb...",
        ]

        bundled = local_adb()
        on_path = shutil.which("adb")
        if bundled:
            lines += ["  \u2713 using bundled adb", f"      {bundled}"]
        elif on_path:
            lines += ["  \u2713 using adb on PATH", f"      {on_path}"]
        else:
            lines += ["  \u2717 adb not found",
                      "      use File > Download platform-tools"]

        lines += ["", "Scanning for devices..."]
        devices = self.refresh_devices()          # populate combo + status now
        lines += self._device_lines(devices)
        lines += ["", "Ready."]

        self._stream_log(lines)

    def _device_lines(self, devices):
        """Formatted log lines describing a device-scan result."""
        if devices is None:
            return ["  \u2717 adb could not be run"]
        if devices:
            out = [f"  \u2713 {len(devices)} device(s) found"]
            out += [f"      \u2022 {d}" for d in devices]
            return out
        return ["  \u2717 no devices",
                "      unplugged, locked, or USB debugging off"]

    def _stream_log(self, lines, interval=LOG_STREAM_INTERVAL_MS):
        """Append lines to the log one at a time, `interval` ms apart. Uses a
        chained single-shot timer so the UI stays responsive (no sleep)."""
        pending = list(lines)

        def emit_next():
            if not pending:
                return
            self.logln(pending.pop(0))
            if pending:
                QTimer.singleShot(interval, emit_next)

        emit_next()

    # --- construction helpers -------------------------------------------

    def _build_menu(self):
        menubar = QMenuBar(self)

        # Keep Python-side refs to the menus/actions. Without this, shiboken can
        # garbage-collect the underlying QMenu objects out from under the bar.
        self._menus = {}
        self._actions = {}

        file_menu = menubar.addMenu("&File")
        self._menus["file"] = file_menu
        act_edit = QAction("Edit config file", self)
        act_edit.triggered.connect(self.open_config)
        file_menu.addAction(act_edit)
        act_dl = QAction("Download platform-tools", self)
        act_dl.triggered.connect(self.confirm_download)
        file_menu.addAction(act_dl)
        act_save = QAction("Save settings as defaults", self)
        act_save.triggered.connect(self.save_defaults)
        file_menu.addAction(act_save)
        file_menu.addSeparator()
        act_exit = QAction("Exit", self)
        act_exit.setShortcut("Ctrl+Q")
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)
        self._actions["edit"] = act_edit
        self._actions["download"] = act_dl
        self._actions["save"] = act_save
        self._actions["exit"] = act_exit

        help_menu = menubar.addMenu("&Help")
        self._menus["help"] = help_menu
        act_about = QAction("About", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)
        self._actions["about"] = act_about

        return menubar

    def _build_device_group(self, root):
        group = QGroupBox("Device")
        form = QFormLayout(group)

        row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.device_combo.activated.connect(self.load_root)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.on_refresh)
        row.addWidget(self.device_combo)
        row.addWidget(refresh_btn)
        form.addRow("Device:", row)

        root.addWidget(group)

    def _build_directories_group(self, root):
        group = QGroupBox(f"Tree [ {self.root_path} ]")
        layout = QVBoxLayout(group)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name"])
        self.tree.setMinimumHeight(DIRECTORY_TREE_MIN_HEIGHT)
        self.tree.itemExpanded.connect(self.on_item_expanded)
        self.tree.itemClicked.connect(self.on_item_clicked)
        self.tree.itemChanged.connect(self.on_item_changed)
        self._install_tree_checkboxes()
        layout.addWidget(self.tree)

        sel_row = QHBoxLayout()
        self.sel_lbl = QLabel("0 selected")
        sel_row.addWidget(self.sel_lbl)
        sel_row.addStretch(1)
        layout.addLayout(sel_row)

        root.addWidget(group, 1)  # the browser takes the extra vertical space

    def _install_tree_checkboxes(self):
        """Draw high-contrast tri-state checkbox images and wire them into the
        tree via a stylesheet.

        The native Fusion dark-theme indicator is nearly invisible when
        unchecked -- an empty box on a dark row -- so it's hard to tell the
        items are even checkable. We render our own for all three states:
        a clearly-bordered empty box (unchecked), an accent box with a white
        tick (checked), and an accent box with a dash (partially checked).

        QSS renders indicators from image files, so the three PNGs are written
        to a temp dir that's cleaned up at exit.
        """
        px      = CHECKBOX_PX
        accent  = self.palette().highlight().color()
        border  = QColor("#9aa0a6")   # light grey outline, reads on dark rows
        empty   = QColor("#20242b")   # subtle fill so the box has presence
        mark    = QColor("#ffffff")

        def render(kind):
            pm = QPixmap(px, px)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            box = QRectF(1.5, 1.5, px - 3, px - 3)
            if kind == "unchecked":
                p.setPen(QPen(border, 1.5))
                p.setBrush(QBrush(empty))
                p.drawRoundedRect(box, 3.0, 3.0)
            else:
                p.setPen(QPen(accent.darker(120), 1.5))
                p.setBrush(QBrush(accent))
                p.drawRoundedRect(box, 3.0, 3.0)
                pen = QPen(mark, 2.0)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                if kind == "checked":
                    p.drawPolyline([QPointF(px * 0.26, px * 0.52),
                                    QPointF(px * 0.44, px * 0.70),
                                    QPointF(px * 0.74, px * 0.32)])
                else:  # indeterminate / partially checked
                    p.drawLine(QPointF(px * 0.30, px * 0.50),
                               QPointF(px * 0.70, px * 0.50))
            p.end()
            return pm

        self._cb_dir = tempfile.mkdtemp(prefix="adbbackup_cb_")
        atexit.register(shutil.rmtree, self._cb_dir, ignore_errors=True)

        paths = {}
        for kind in ("unchecked", "checked", "indeterminate"):
            fp = os.path.join(self._cb_dir, f"{kind}.png")
            render(kind).save(fp, "PNG")
            paths[kind] = fp.replace("\\", "/")   # QSS url() wants forward slashes

        self.tree.setStyleSheet(
            f"QTreeWidget::indicator {{ width: {px}px; height: {px}px; }}\n"
            f"QTreeWidget::indicator:unchecked {{ image: url({paths['unchecked']}); }}\n"
            f"QTreeWidget::indicator:checked {{ image: url({paths['checked']}); }}\n"
            f"QTreeWidget::indicator:indeterminate {{ image: url({paths['indeterminate']}); }}\n"
        )

    def _build_output_group(self, root):
        group = QGroupBox("Output")
        form = QFormLayout(group)

        row = QHBoxLayout()
        self.outdir_edit = QLineEdit(self.cfg["backup"]["output_dir"] or DEFAULT_BACKUPS_DIR)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.pick_outdir)
        row.addWidget(self.outdir_edit)
        row.addWidget(browse_btn)
        form.addRow("Save to:", row)

        self.compress_chk = QCheckBox("gzip (slower; little gain on photos/video)")
        self.compress_chk.setChecked(bool(self.cfg["backup"]["compress"]))
        self.verify_chk = QCheckBox("Verify archive after each directory")
        self.verify_chk.setChecked(bool(self.cfg["backup"]["verify"]))
        form.addRow(self.compress_chk)
        form.addRow(self.verify_chk)

        root.addWidget(group)

    def _build_run_group(self, root):
        # The run action, its progress, and its status live together in one
        # bordered box -- so they read as a single unit instead of a loose stack.
        group = QGroupBox("Backup")
        layout = QVBoxLayout(group)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start backup")
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 1000)  # per-mille, so >2GB totals don't overflow int
        self.pbar.setFormat("%p%")
        layout.addWidget(self.pbar)

        self.status_lbl = QLabel("Idle.")
        layout.addWidget(self.status_lbl)

        root.addWidget(group)

    def _build_log_group(self, root):
        group = QGroupBox("Log")
        layout = QVBoxLayout(group)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(LOG_MIN_HEIGHT)   # keep a few lines visible
        # Fixed-width font so the header rule, check marks and bullets line up.
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.log)
        root.addWidget(group, 1)  # log fills the leftover height in the right pane

    # --- menu actions ----------------------------------------------------

    def open_config(self):
        """File > Edit config file -- open config.toml in the OS default editor."""
        if not os.path.exists(CONFIG_PATH) and not write_default_config(CONFIG_PATH):
            QMessageBox.warning(self, "Config", "Couldn't create the config file.")
            return
        if open_in_editor(CONFIG_PATH):
            self.status_lbl.setText("Opened config.toml -- restart to apply changes.")
        else:
            QMessageBox.warning(self, "Config", f"Couldn't open:\n{CONFIG_PATH}")

    def show_about(self):
        """Help > About."""
        QMessageBox.about(
            self,
            f"About {SCRIPT_TITLE}",
            f"<b>{SCRIPT_TITLE} {SCRIPT_VERSION}</b><br> {SCRIPT_TAG}<br><br>"
            "Fast Android backups over adb.<br>"
            "--whalelinguini"
        )

    # --- startup checks --------------------------------------------------

    def check_adb_startup(self):
        """Warn if adb is missing and offer the platform-tools download."""
        if adb_available(self.adb_path):
            return

        resp = QMessageBox.question(
            self,
            "adb not found",
            "Android Debug Bridge (adb) wasn't found on your PATH.\n\n"
            "Download Google's platform-tools and install it into the app "
            f"directory now?\n({BASE_DIR})",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if resp == QMessageBox.StandardButton.Yes:
            self.download_platform_tools()

    def confirm_download(self):
        """File > Download platform-tools: confirm, then fetch + extract.

        The startup prompt has its own confirmation, so it calls
        download_platform_tools() directly and skips this.
        """
        if self.thread is not None:
            return  # a task is already running
        resp = QMessageBox.question(
            self,
            "Download platform-tools",
            "This will fetch the latest platform-tools package from Google and "
            f"extract it into the app directory:\n\n{BASE_DIR}\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if resp == QMessageBox.StandardButton.Yes:
            self.download_platform_tools()

    def download_platform_tools(self):
        """Download Google's platform-tools zip and extract it into the script
        dir, then point the app at the bundled adb.

        Reachable from File > Download platform-tools, and from the startup
        prompt when adb can't be found. Runs off the UI thread.
        """
        if self.thread is not None:
            return  # a task is already running

        url = platform_tools_url()
        self._begin_task()
        self.pbar.setValue(0)
        self.logln("--- downloading platform-tools ---")
        self.logln(url)

        self.thread = QThread()
        self.worker = DownloadWorker(url, BASE_DIR)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_dl_progress)
        self.worker.status.connect(self.status_lbl.setText)
        self.worker.done.connect(self.on_dl_done)
        self.worker.failed.connect(self.on_dl_failed)
        self.worker.cancelled.connect(self.on_cancelled)

        for sig in (self.worker.done, self.worker.failed, self.worker.cancelled):
            sig.connect(self.thread.quit)
        # Clean up only after the thread has actually stopped -- clearing the
        # refs any earlier lets the running QThread get GC'd and crashes Qt.
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self._clear_refs)

        self.thread.start()

    # --- live directory browser --------------------------------------------

    def load_root(self):
        """(Re)load the top level of the browse root for the selected device."""
        serial = self.device_combo.currentText().strip()
        self.tree.clear()
        self._update_selection_count()
        if not serial:
            return
        entries = self.list_dir(self.root_path)
        if entries is None:
            self.status_lbl.setText(f"Couldn't read {self.root_path}")
            return
        self._updating = True
        for name, is_dir in entries:
            self._make_item(self.tree, name,
                            self._child_path(self.root_path, name), is_dir)
        self._updating = False
        self.status_lbl.setText(f"{self.root_path}  --  {len(entries)} item(s)")
        self._update_selection_count()

    @staticmethod
    def _child_path(parent, name):
        """Join a device path + child name without doubling the slash."""
        return parent.rstrip("/") + "/" + name

    def _make_item(self, parent, name, path, is_dir, check=Qt.CheckState.Unchecked):
        item = QTreeWidgetItem(parent, [name])
        item.setData(0, ROLE_PATH, path)
        item.setData(0, ROLE_ISDIR, is_dir)
        item.setData(0, ROLE_LOADED, False)
        item.setIcon(0, self.icon_dir if is_dir else self.icon_file)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, check)
        if is_dir:
            # Placeholder child so the expand arrow shows before we load; it has
            # no ROLE_PATH and no checkbox, so it's ignored everywhere.
            ph = QTreeWidgetItem(item, ["(loading...)"])
            ph.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return item

    def on_item_expanded(self, item):
        if not item.data(0, ROLE_ISDIR) or item.data(0, ROLE_LOADED):
            return
        item.setData(0, ROLE_LOADED, True)
        item.takeChildren()  # drop the placeholder
        path = item.data(0, ROLE_PATH)
        entries = self.list_dir(path)
        if entries is None:
            QTreeWidgetItem(item, ["(could not read)"])
            return
        if not entries:
            QTreeWidgetItem(item, ["(empty)"])
            return
        # New children inherit the parent's checked state, so expanding a
        # ticked directory shows its contents already ticked.
        inherit = (Qt.CheckState.Checked
                   if item.checkState(0) == Qt.CheckState.Checked
                   else Qt.CheckState.Unchecked)
        self._updating = True
        for name, is_dir in entries:
            self._make_item(item, name, self._child_path(path, name),
                            is_dir, check=inherit)
        self._updating = False
        self._update_selection_count()

    def on_item_clicked(self, item, _col):
        path = item.data(0, ROLE_PATH)
        if path:
            kind = "dir" if item.data(0, ROLE_ISDIR) else "file"
            self.status_lbl.setText(f"{kind}: {path}")

    # --- check-state propagation ----------------------------------------

    def on_item_changed(self, item, col):
        if col != 0 or self._updating or item.data(0, ROLE_PATH) is None:
            return
        self._updating = True
        state = item.checkState(0)
        if state in (Qt.CheckState.Checked, Qt.CheckState.Unchecked):
            self._cascade_down(item, state)   # push to loaded descendants
        self._update_ancestors(item)          # pull parents to combined state
        self._updating = False
        self._update_selection_count()

    def _cascade_down(self, item, state):
        for i in range(item.childCount()):
            child = item.child(i)
            if child.data(0, ROLE_PATH) is None:
                continue  # skip placeholder
            child.setCheckState(0, state)
            self._cascade_down(child, state)

    def _update_ancestors(self, item):
        parent = item.parent()
        while parent is not None and parent.data(0, ROLE_PATH) is not None:
            states = [parent.child(i).checkState(0)
                      for i in range(parent.childCount())
                      if parent.child(i).data(0, ROLE_PATH) is not None]
            if states and all(s == Qt.CheckState.Checked for s in states):
                new = Qt.CheckState.Checked
            elif states and all(s == Qt.CheckState.Unchecked for s in states):
                new = Qt.CheckState.Unchecked
            else:
                new = Qt.CheckState.PartiallyChecked
            parent.setCheckState(0, new)
            parent = parent.parent()

    # --- selection ------------------------------------------------------

    def resolve_selection(self):
        """Top-most checked paths: a fully-checked directory is returned whole; a
        partially-checked directory is descended into. This is what gets tarred."""
        selected = []

        def walk(item):
            count = item.childCount() if item else self.tree.topLevelItemCount()
            for i in range(count):
                child = item.child(i) if item else self.tree.topLevelItem(i)
                if child.data(0, ROLE_PATH) is None:
                    continue
                st = child.checkState(0)
                if st == Qt.CheckState.Checked:
                    selected.append(child.data(0, ROLE_PATH))
                elif st == Qt.CheckState.PartiallyChecked:
                    walk(child)

        walk(None)
        return selected

    def _update_selection_count(self):
        n = len(self.resolve_selection())
        self.sel_lbl.setText(f"{n} selected")

    def list_dir(self, path):
        """Return [(name, is_dir), ...] for a device path, or None on error.

        Note the -L and the trailing slash: /sdcard (and some subdirs) are
        symlinks, and `ls -F` on a symlink *argument* prints the link's own
        name instead of listing its contents. -L dereferences symlinks and the
        trailing slash forces directory-content listing, so we get what's
        inside rather than the link itself.
        """
        serial = self.device_combo.currentText().strip()
        dir_arg = path.rstrip("/") + "/"
        remote = f"ls -1aFL {shlex.quote(dir_arg)}"
        cmd = adb_base(self.adb_path, serial) + ["exec-out", "sh", "-c", remote]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=LS_TIMEOUT_SEC, **_subprocess_kwargs(),
            )
        except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode not in (0, None) and not out.stdout.strip():
            return None
        return parse_ls(out.stdout)

    def pick_outdir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose output directory", self.outdir_edit.text()
        )
        if chosen:
            self.outdir_edit.setText(chosen)

    def logln(self, msg):
        self.log.appendPlainText(msg)

    def on_refresh(self):
        """Refresh button: rescan and stream the result into the log."""
        devices = self.refresh_devices()
        self._stream_log(["", "Refreshing devices..."] + self._device_lines(devices))

    def refresh_devices(self):
        """Populate the device dropdown + status. Returns the device list
        (or None if adb couldn't run) so callers can log it."""
        devices = list_devices(self.adb_path)
        self.device_combo.clear()
        if devices is None:
            self.status_lbl.setText("adb not found.")
        elif devices:
            self.device_combo.addItems(devices)
            self.status_lbl.setText(f"{len(devices)} device(s) connected.")
        else:
            self.status_lbl.setText("No devices. Plug in, unlock, allow USB debugging.")
        # Reflect the (new) current device in the browser: load its root, or
        # clear the tree if there's nothing to show. addItems() doesn't fire
        # `activated`, so the load is driven from here.
        if hasattr(self, "tree"):
            if devices:
                self.load_root()
            else:
                self.tree.clear()
                self._update_selection_count()
        return devices

    # --- config persistence ---------------------------------------------

    def save_defaults(self):
        self.cfg["backup"]["output_dir"] = self.outdir_edit.text().strip()
        self.cfg["backup"]["compress"]   = self.compress_chk.isChecked()
        self.cfg["backup"]["verify"]     = self.verify_chk.isChecked()
        self.cfg["directories"]["root"]      = self.root_path
        try:
            save_config(self.cfg)
            self.status_lbl.setText(f"Saved config to {CONFIG_PATH}")
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    # --- run control -----------------------------------------------------

    def start(self):
        adb_path = self.adb_path
        serial   = self.device_combo.currentText().strip()
        paths    = self.resolve_selection()
        outdir   = self.outdir_edit.text().strip() or DEFAULT_BACKUPS_DIR

        if not serial:
            QMessageBox.warning(self, "Device", "No device selected.")
            return
        if not paths:
            QMessageBox.warning(self, "Directories", "Tick at least one file or directory.")
            return

        # Each run goes in its own timestamped subdirectory under the output dir.
        stamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(outdir, f"backup_{stamp}")

        resp = QMessageBox.question(
            self,
            "Start backup",
            f"This backup will be saved to:\n\n{run_dir}\n\n"
            f"{len(paths)} directory(ies) from device {serial}.\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        # Create the run directory (and the backups/ parent) if needed.
        try:
            os.makedirs(run_dir, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Output", f"Couldn't create directory:\n{run_dir}\n\n{exc}")
            return

        chunk = max(CHUNK_MIB_FLOOR, int(self.cfg["backup"]["chunk_mib"])) * 1024 * 1024

        self._begin_task()
        self.pbar.setValue(0)
        self.logln(f"--- backup started {datetime.now():%Y-%m-%d %H:%M:%S} ---")
        self.logln(f"  saving to: {run_dir}")

        self.thread = QThread()
        self.worker = BackupWorker(
            adb_path, serial, paths, run_dir,
            self.compress_chk.isChecked(), self.verify_chk.isChecked(), chunk,
        )
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.total.connect(self.on_total)
        self.worker.progress.connect(self.on_progress)
        self.worker.status.connect(self.status_lbl.setText)
        self.worker.log.connect(self.logln)
        self.worker.done.connect(self.on_done)
        self.worker.cancelled.connect(self.on_cancelled)
        self.worker.fatal.connect(self.on_fatal)

        # teardown once any terminal signal fires
        for sig in (self.worker.done, self.worker.cancelled, self.worker.fatal):
            sig.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self._clear_refs)

        self.thread.start()

    def cancel(self):
        if self.worker:
            self.worker.cancel()
            self.status_lbl.setText("Cancelling...")

    # --- worker signal handlers -----------------------------------------

    # --- works badly -------
    def on_total(self, total):
        self.total_bytes = max(0, total)
        if self.total_bytes:
            self.logln(f"Estimated total: {human(self.total_bytes)}")
            self.pbar.setRange(0, 1000)
        else:
            self.logln("Estimated total: unknown")
            self.pbar.setRange(0, 0)  # indeterminate / busy bar

    def on_progress(self, done, speed, eta):
        # If the estimate is clearly wrong, switch to busy bar (It will)
        if self.total_bytes and done > self.total_bytes * 1.2:
            self.pbar.setRange(0, 0)          # indeterminate (striped) bar
            self.total_bytes = 0              # stop using the bad total for % #-- I suggest a rand num instead of 0...

        if self.total_bytes > 0:
            pct = done / self.total_bytes * 100
            self.pbar.setValue(min(1000, int(pct * 10)))
            self.status_lbl.setText(
                f"{human(done)} / {human(self.total_bytes)} ({pct:.0f}%)  --  "
                f"{human(speed)}/s  --  ETA {fmt_eta(eta)}"
            )
        else:
            self.status_lbl.setText(
                f"{human(done)} transferred  --  {human(speed)}/s"
            )
            # close enough, send it (_))_)===D
            
    def on_done(self, total):
        self.pbar.setValue(1000)
        self.status_lbl.setText(f"Done. {human(total)} written.")
        self.logln("--- FINISHED ---")
        self._end_task()

    def on_cancelled(self):
        self.status_lbl.setText("Cancelled.")
        self.logln("--- cancelled ---")
        self._end_task()

    def on_fatal(self, msg):
        QMessageBox.critical(self, "adb", msg)
        self.status_lbl.setText(msg)
        self._end_task()

    # --- download signal handlers ---------------------------------------

    def on_dl_progress(self, done, total):
        if total:
            self.pbar.setValue(int(done / total * 1000))
            pct = done / total * 100
            self.status_lbl.setText(
                f"Downloading platform-tools... {human(done)} / {human(total)} ({pct:.0f}%)"
            )
        else:
            self.status_lbl.setText(f"Downloading platform-tools... {human(done)}")

    def on_dl_done(self, adb_path):
        self.pbar.setValue(1000)
        self.adb_path = adb_path
        self.status_lbl.setText("platform-tools installed.")
        self.logln(f"platform-tools ready -- adb: {adb_path}")
        self._end_task()
        devices = self.refresh_devices()
        self._stream_log(["", "Scanning for devices..."] + self._device_lines(devices))

    def on_dl_failed(self, msg):
        QMessageBox.warning(self, "Download", msg)
        self.status_lbl.setText(msg)
        self.logln(f"  ! {msg}")
        self._end_task()

    # --- task button state ----------------------------------------------

    def _begin_task(self):
        """Enter the running state: only Cancel is live."""
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._actions["download"].setEnabled(False)

    def _end_task(self):
        """Leave the running state. Buttons only -- the thread/worker refs are
        cleared separately by _clear_refs once the thread has fully stopped."""
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._actions["download"].setEnabled(True)

    def _clear_refs(self):
        """Drop thread/worker refs, but only after QThread.finished -- i.e. once
        the thread has really stopped, so nothing GCs a live QThread."""
        self.thread = None
        self.worker = None


# ===========================================================================
#  Entry point
# ===========================================================================

def main():
    cfg = load_config()
    app = QApplication(sys.argv)

    # Window / taskbar icon for the process as a whole. MainWindow also sets
    # it on itself; both matter depending on the platform / Qt style.
    icon = load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    # Splash is fully optional: the import at the top becomes None if
    # splash_screen.py (or its dependency) is missing, so this is a no-op.
    if show_splash is not None:
        try:
            show_splash(app, "splash.png", seconds=3.0)
        except Exception:
            pass  # never let a splash failure block startup

    try:
        app.setStyle(cfg["ui"]["theme"])
    except Exception:
        pass
    win = MainWindow(cfg)
    win.show()
    win.check_adb_startup()  # after show() so the prompt sits over the window
    sys.exit(app.exec())


if __name__ == "__main__":
    main()