# -*- mode: python ; coding: utf-8 -*-
"""One-folder console distribution, with an explicit data-file allowlist."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], []
for package in ("textual", "yt_dlp", "yt_dlp_ejs"):
    package_data, package_binaries, package_imports = collect_all(package, include_py_files=False)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports

analysis = Analysis(
    [str(root / "yamd.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "IPython", "ytdlp_plugins"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="yamd",
    console=True,
    debug=False,
    strip=False,
    upx=False,
    codesign_identity=None,
)
COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="yamd")
