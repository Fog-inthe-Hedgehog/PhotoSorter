# -*- mode: python -*-
import shutil
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

hiddenimports = [
    "flet_desktop",
] + collect_submodules("flet") + collect_submodules("flet_desktop")

flet_datas = collect_data_files("flet")

ffprobe_path = shutil.which("ffprobe")
if not ffprobe_path and getattr(sys, "_MEIPASS", None):
    candidate = Path(sys._MEIPASS) / ("ffprobe.exe" if sys.platform.startswith("win") else "ffprobe")
    if candidate.exists():
        ffprobe_path = str(candidate)

binaries = []
if ffprobe_path:
    binaries.append((ffprobe_path, "."))

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=[("resources", "resources")] + flet_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PhotoSorter",
    icon="resources/icon.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    onefile=True,
)
