"""Atomic JSON for icon settings and layout snapshots only.

Legacy file-moving settings/history are intentionally neither read nor modified.
Nothing in this module opens an icon's parsing name or shortcut target.
"""
from __future__ import annotations

from dataclasses import asdict, fields
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from .models import Settings


def _safe_existing(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"配置路径不能是符号链接或重解析点：{path}")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ValueError(f"配置路径类型不正确：{path}")


def _safe_directories(path: Path) -> None:
    for parent in reversed((path, *path.parents)):
        _safe_existing(parent, directory=True)


def _integer(value: object) -> bool:
    return type(value) is int


def _validate_snapshots(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("图标布局历史必须是 JSON 数组。")
    ids = set()
    for record in value:
        if not isinstance(record, dict) or any(not isinstance(record.get(key), str) or not record[key] for key in ("id", "created_at", "kind", "status")):
            raise ValueError("图标布局记录格式错误。")
        if record["id"] in ids:
            raise ValueError("布局记录包含重复编号。")
        ids.add(record["id"])
        if not isinstance(record.get("name", ""), str) or type(record.get("can_undo", False)) is not bool:
            raise ValueError("布局名称或恢复状态无效。")
        icons = record.get("icons")
        if not isinstance(icons, list):
            raise ValueError("布局记录缺少图标坐标。")
        identities = set()
        for icon in icons:
            if not isinstance(icon, dict) or not isinstance(icon.get("identity"), str) or not icon["identity"] or not isinstance(icon.get("name", ""), str) or not all(_integer(icon.get(axis)) for axis in ("x", "y")):
                raise ValueError("布局记录中的图标标识或坐标无效。")
            if icon["identity"] in identities:
                raise ValueError("布局记录包含重复图标标识。")
            identities.add(icon["identity"])
        monitors = record.get("monitors")
        if not isinstance(monitors, list) or not monitors:
            raise ValueError("布局记录缺少显示器信息。")
        monitor_ids = set()
        for monitor in monitors:
            if not isinstance(monitor, dict) or not isinstance(monitor.get("id"), str) or not monitor["id"] or monitor["id"] in monitor_ids:
                raise ValueError("布局记录的显示器标识无效。")
            monitor_ids.add(monitor["id"])
            area = monitor.get("work_area")
            if not isinstance(area, dict) or not all(_integer(area.get(key)) for key in ("left", "top", "right", "bottom")) or area["right"] <= area["left"] or area["bottom"] <= area["top"]:
                raise ValueError("布局记录的显示器工作区域无效。")
            if type(monitor.get("dpi", 96)) is not int or monitor.get("dpi", 96) <= 0:
                raise ValueError("布局记录的显示器 DPI 无效。")
        spacing = record.get("spacing")
        if not isinstance(spacing, dict) or not all(_integer(spacing.get(axis)) and spacing[axis] > 0 for axis in ("x", "y")):
            raise ValueError("布局记录的图标间距无效。")
        if type(record.get("icon_size", 32)) is not int or record.get("icon_size", 32) <= 0:
            raise ValueError("布局记录的图标尺寸无效。")
        if type(record.get("auto_arrange", False)) is not bool:
            raise ValueError("布局记录的自动排列状态无效。")
    return value


class JsonStore:
    SETTINGS_NAME = "icon-settings.json"
    LAYOUTS_NAME = "icon-layouts.json"

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).absolute()
        _safe_directories(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _safe_directories(self.data_dir)

    def _load(self, name: str, default: Any) -> Any:
        path = self.data_dir / name
        _safe_directories(self.data_dir)
        _safe_existing(path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"无法读取 {name}；原文件已保留，请先修复或备份该文件。") from exc

    def _save(self, name: str, value: Any) -> None:
        path = self.data_dir / name
        _safe_directories(self.data_dir)
        _safe_existing(path)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _safe_directories(self.data_dir)
            _safe_existing(path)
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(self.data_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load_settings(self) -> Settings:
        value = self._load(self.SETTINGS_NAME, {})
        if not isinstance(value, dict) or any(key not in {item.name for item in fields(Settings)} for key in value):
            raise ValueError("图标设置文件格式错误。")
        settings = Settings(**value)
        settings.validate()
        return settings

    def save_settings(self, settings: Settings) -> None:
        if not isinstance(settings, Settings):
            raise ValueError("设置必须为 Settings 对象。")
        settings.validate()
        self._save(self.SETTINGS_NAME, asdict(settings))

    def load_layouts(self) -> list[dict]:
        return _validate_snapshots(self._load(self.LAYOUTS_NAME, []))

    def save_layouts(self, layouts: list[dict]) -> None:
        self._save(self.LAYOUTS_NAME, _validate_snapshots(layouts))
