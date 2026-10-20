"""Validated, atomically replaced local settings and operation journals."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


@dataclass
class Settings:
    theme: str = "dark"
    ignored_patterns: list[str] = field(default_factory=list)
    keep_shortcuts: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.theme not in ("dark", "light"):
            raise ValueError("主题必须为 dark 或 light。")
        for label, values in (
            ("忽略列表", self.ignored_patterns), ("保留快捷方式", self.keep_shortcuts)
        ):
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() or "\x00" in value
                for value in values
            ):
                raise ValueError(f"{label}必须是非空字符串列表。")


def _safe_existing(path: Path, *, directory: bool = False) -> None:
    """Refuse redirected/reparse state paths rather than following them."""
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


def _validate_history(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("整理历史必须是 JSON 数组。")
    ids: set[str] = set()
    for record in value:
        if not isinstance(record, dict):
            raise ValueError("整理历史记录格式错误。")
        for key in ("id", "created_at", "status"):
            if not isinstance(record.get(key), str) or not record[key]:
                raise ValueError(f"整理历史缺少有效的 {key}。")
        if record["id"] in ids:
            raise ValueError("整理历史包含重复操作编号。")
        ids.add(record["id"])
        if not isinstance(record.get("entries"), list):
            raise ValueError("整理历史缺少文件记录。")
        for entry in record["entries"]:
            if not isinstance(entry, dict) or any(
                not isinstance(entry.get(key), str) or not entry[key]
                for key in ("source", "destination", "status")
            ):
                raise ValueError("整理历史中的文件记录格式错误。")
            if "error" in entry and not isinstance(entry["error"], str):
                raise ValueError("整理历史中的错误信息格式错误。")
    return value


class JsonStore:
    """State files use UTF-8 and a same-directory fsync + atomic replace.

    Missing files mean defaults. Corrupt files are never silently discarded or
    overwritten during loading, so the user can recover their undo journal.
    Operation serialization is the caller's responsibility.
    """

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
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{name}.", suffix=".tmp", dir=self.data_dir
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _safe_directories(self.data_dir)
            _safe_existing(path)
            os.replace(temporary, path)
            # Windows cannot open directories with os.open; replacement itself
            # remains atomic there. POSIX also flushes the directory entry.
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
        value = self._load("settings.json", {})
        if not isinstance(value, dict) or any(
            key not in {"theme", "ignored_patterns", "keep_shortcuts"} for key in value
        ):
            raise ValueError("设置文件格式错误。")
        settings = Settings(**value)
        settings.validate()
        return settings

    def save_settings(self, settings: Settings) -> None:
        if not isinstance(settings, Settings):
            raise ValueError("设置必须为 Settings 对象。")
        settings.validate()
        self._save("settings.json", asdict(settings))

    def load_history(self) -> list[dict]:
        return _validate_history(self._load("history.json", []))

    def save_history(self, history: list[dict]) -> None:
        self._save("history.json", _validate_history(history))
