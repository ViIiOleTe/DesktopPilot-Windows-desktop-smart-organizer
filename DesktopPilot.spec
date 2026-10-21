# Build on Windows with: python -m PyInstaller --noconfirm --clean DesktopPilot.spec
from pathlib import Path
import sys

project_root = Path(SPECPATH)
native_binaries = []
if sys.platform == "win32":
    bridge = project_root / "desktop_pilot" / "native" / "desktop_shell.dll"
    if not bridge.is_file():
        raise FileNotFoundError(
            "Missing native Explorer bridge. Run tools\\build_native.cmd "
            "in an x64 MSVC developer command prompt before packaging."
        )
    native_binaries.append((str(bridge), "desktop_pilot/native"))

a = Analysis(
    [str(project_root / "desktop_pilot" / "__main__.py")],
    pathex=[str(project_root)],
    binaries=native_binaries,
    datas=[(str(project_root / "desktop_pilot" / "assets" / "icon.png"), "desktop_pilot/assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DesktopPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project_root / "desktop_pilot" / "assets" / "icon.ico"),
    disable_windowed_traceback=False,
)
