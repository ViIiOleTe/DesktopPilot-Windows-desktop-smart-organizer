"""Platform locations, including Windows' redirected Desktop known folder."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import uuid


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _known_folder_desktop() -> Path:
    """Ask the Windows shell, never guess from USERPROFILE or OneDrive names."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    get_path = shell32.SHGetKnownFolderPath
    get_path.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_path.restype = ctypes.c_int32
    free = ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    folder_id = _GUID.from_buffer_copy(
        uuid.UUID("b4bfcc3a-db2c-424c-b029-7fe99a87c641").bytes_le
    )
    result = ctypes.c_void_p()
    try:
        status = get_path(ctypes.byref(folder_id), 0, None, ctypes.byref(result))
        if status != 0 or not result.value:
            raise OSError(
                f"无法读取 Windows 桌面目录（SHGetKnownFolderPath: 0x{status & 0xFFFFFFFF:08X}）。"
            )
        value = ctypes.wstring_at(result.value)
        if not value:
            raise OSError("Windows 返回了空的桌面目录。")
        return Path(value)
    finally:
        if result.value:
            free(result)


def discover_desktop() -> Path:
    """Return the actual per-user Windows Desktop, including OneDrive redirection.

    The non-Windows fallback supports development; it is not a Windows emulation.
    Callers must check the directory before scanning it.
    """
    if sys.platform == "win32":
        return _known_folder_desktop()
    return Path.home() / "Desktop"


def default_data_dir() -> Path:
    """Return local per-user application state, away from the organized Desktop."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "DesktopPilot"
