"""Strict ctypes adapter to the supported Windows Shell COM bridge.

Never scan Desktop directories or move user files. The native library owns
short-lived COM apartments and returns Explorer desktop-view coordinates.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys

from .models import DesktopState, Icon, Monitor, Point, Rect


class ShellAccessError(RuntimeError):
    """The Explorer Shell view could not be read or positioned safely."""


class _NativePoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


def default_data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "DesktopPilot"


def _integer(value) -> int:
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise ValueError("Shell 坐标或尺寸不是有效的 32 位整数。")
    return value


def _point(value) -> Point:
    return Point(_integer(value["x"]), _integer(value["y"]))


def _string(value) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("Shell 返回了无效的项目标识或名称。")
    return value


def _boolean(value) -> bool:
    if type(value) is not bool:
        raise ValueError("Shell 返回了无效的状态值。")
    return value


def _decode_state(value: dict) -> DesktopState:
    try:
        icons = tuple(
            Icon(
                identity=_string(item["identity"]), name=_string(item["name"]),
                parsing_name=_string(item["parsing_name"]), position=_point(item["position"]),
                is_folder=_boolean(item["is_folder"]),
                is_shortcut=_boolean(item["is_shortcut"]),
                is_filesystem=_boolean(item["is_filesystem"]),
                target=_string(item.get("target", "")),
                arguments=_string(item.get("arguments", "")),
            ) for item in value["icons"]
        )
        monitors = tuple(
            Monitor(
                id=_string(item["id"]),
                work_area=Rect(*(_integer(item["work_area"][key]) for key in ("left", "top", "right", "bottom"))),
                dpi=_integer(item["dpi"]),
            ) for item in value["monitors"]
        )
        spacing = _point(value["spacing"])
        if spacing.x <= 0 or spacing.y <= 0 or not monitors:
            raise ValueError("Shell 未返回可用工作区或有效图标间距。")
        if len({icon.identity for icon in icons}) != len(icons) or any(not icon.identity for icon in icons):
            raise ValueError("Shell 返回了空或重复的图标身份，未生成排列。")
        if any(monitor.work_area.width <= 0 or monitor.work_area.height <= 0 or monitor.dpi <= 0 for monitor in monitors):
            raise ValueError("Shell 显示器工作区或 DPI 无效。")
        return DesktopState(
            icons=icons, monitors=monitors, spacing=spacing,
            icon_size=_integer(value["icon_size"]), auto_arrange=_boolean(value["auto_arrange"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ShellAccessError(f"Windows Shell 数据不完整，未应用任何新排列：{error}") from error


class WindowsShellBackend:
    """Use IFolderView through the native DLL, with no fallback.

    Injecting a library is for ABI unit tests. Production loads the bridge
    beside this package, including inside PyInstaller's bundle.
    """

    def __init__(self, library_path: Path | None = None, *, library=None):
        self.library_path = library_path or Path(__file__).parent / "native" / "desktop_shell.dll"
        self._dll = library
        self._configured = False

    def _load(self):
        if self._dll is None:
            if sys.platform != "win32":
                raise ShellAccessError(
                    "桌面图标坐标控制仅支持 Windows 10/11 的交互式 Explorer 桌面。"
                    "当前平台不会改用文件扫描或文件搬运。"
                )
            if not self.library_path.is_file():
                raise ShellAccessError(
                    "缺少 Windows Shell 原生桥接模块 desktop_shell.dll。"
                    "请重新下载完整 exe，或先运行 tools\\build_native.cmd 再从源码启动。"
                )
            try:
                # Search only this DLL's directory and Windows System32.
                self._dll = ctypes.CDLL(str(self.library_path.resolve()), winmode=0x900)
            except OSError as error:
                raise ShellAccessError(f"Windows Shell 桥接模块无法加载：{error}") from error
        if not self._configured:
            try:
                self._dll.DP_ReadDesktop.argtypes = []
                self._dll.DP_ReadDesktop.restype = ctypes.c_void_p
                self._dll.DP_Position.argtypes = [
                    ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(_NativePoint), ctypes.c_uint32,
                ]
                self._dll.DP_Position.restype = ctypes.c_void_p
                self._dll.DP_Free.argtypes = [ctypes.c_void_p]
                self._dll.DP_Free.restype = None
            except AttributeError as error:
                raise ShellAccessError("Windows Shell 桥接模块版本不正确，缺少所需接口。") from error
            self._configured = True
        return self._dll

    def validate_library(self) -> None:
        """Load and inspect exports only; never access the actual desktop."""
        self._load()

    def _invoke(self, name: str, *arguments) -> DesktopState:
        dll = self._load()
        pointer = getattr(dll, name)(*arguments)
        if not pointer:
            raise ShellAccessError("Windows Shell 桥接模块未返回结果，未确认桌面排列。")
        try:
            payload = json.loads(ctypes.string_at(pointer).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ShellAccessError("Windows Shell 返回了无效数据，无法确认桌面排列。") from error
        finally:
            dll.DP_Free(pointer)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            detail = payload.get("error", "未知 Shell 错误") if isinstance(payload, dict) else "无效返回值"
            if "AUTO_ARRANGE_ENABLED" in str(detail):
                raise ShellAccessError("Windows 已开启自动排列图标。请右键桌面 → 查看 → 取消“自动排列图标”，然后重新预览。")
            raise ShellAccessError(
                f"无法访问 Windows Explorer 桌面 Shell 接口：{detail}\n"
                "请确认在 Windows 10/11 的交互式用户桌面运行。不会移动任何实际文件。"
            )
        return _decode_state(payload.get("state"))

    def read(self) -> DesktopState:
        return self._invoke("DP_ReadDesktop")

    def position(self, positions: dict[str, Point]) -> DesktopState:
        if not isinstance(positions, dict) or any(
            not isinstance(identity, str) or not identity or "\0" in identity or not isinstance(point, Point)
            for identity, point in positions.items()
        ):
            raise ValueError("图标定位请求必须使用稳定的 Shell 身份和坐标。")
        ids = (ctypes.c_wchar_p * len(positions))(*positions)
        points = (_NativePoint * len(positions))(
            *(_NativePoint(_integer(point.x), _integer(point.y)) for point in positions.values())
        )
        return self._invoke("DP_Position", ids, points, len(positions))
