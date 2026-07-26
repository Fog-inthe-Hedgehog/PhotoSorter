# -*- mode: python -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

hiddenimports = [
    "flet_dropzone",
    "flet_desktop",
] + collect_submodules("flet") + collect_submodules("flet_desktop")

flet_datas = collect_data_files("flet")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
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
