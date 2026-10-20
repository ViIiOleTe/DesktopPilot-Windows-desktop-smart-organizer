"""Desktop scanning and conservative, journalled file moves.

Only files directly inside the selected desktop are considered. Directory trees,
application binaries and Windows reparse points are deliberately left alone.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fnmatch
import hashlib
import os
from pathlib import Path
import stat
from typing import Callable, Iterator
import uuid

from .storage import JsonStore, Settings
from .windows import default_data_dir, discover_desktop

CATEGORY_NAMES = (
    "文档与课件", "图片与截图", "视频与音频", "代码与开发", "压缩文件", "软件快捷方式", "其他文件",
)
OUTPUT_FOLDER = "DesktopPilot整理"
_EXTENSIONS = {
    CATEGORY_NAMES[0]: {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt", ".ods", ".odp", ".csv", ".epub", ".md", ".pages", ".key", ".numbers"},
    CATEGORY_NAMES[1]: {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tif", ".tiff", ".heic", ".avif", ".raw"},
    CATEGORY_NAMES[2]: {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"},
    CATEGORY_NAMES[3]: {".py", ".pyw", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".yaml", ".yml", ".xml", ".toml", ".ini", ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".go", ".rs", ".sql", ".sh", ".ps1", ".bat", ".ipynb", ".vue", ".svelte"},
    CATEGORY_NAMES[4]: {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".zst", ".tgz"},
    CATEGORY_NAMES[5]: {".lnk", ".url"},
}
_BINARIES = {".exe", ".com", ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle", ".dll", ".sys", ".scr"}
_UNSAFE_ATTRIBUTES = 0x2 | 0x4 | 0x400  # hidden, system, reparse point
_Fingerprint = tuple[int, int, int, int]
Progress = Callable[[int, int], None]


def classify(path: Path) -> str:
    suffix = path.suffix.casefold()
    return next((category for category, extensions in _EXTENSIONS.items() if suffix in extensions), CATEGORY_NAMES[-1])


def _fingerprint(info: os.stat_result) -> _Fingerprint:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _safe_regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & _UNSAFE_ATTRIBUTES:
        raise ValueError(f"不是可安全移动的普通文件：{path.name}")
    return info


def _safe_directory(path: Path, *, allow_reparse: bool = False) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (not allow_reparse and getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError(f"目录不安全（可能是符号链接或重解析点）：{path}")


def _digest(path: Path) -> str:
    before = _fingerprint(_safe_regular(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if _fingerprint(os.fstat(stream.fileno())) != before:
            raise ValueError(f"文件在读取前发生变化：{path.name}")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if _fingerprint(_safe_regular(path)) != before:
        raise ValueError(f"文件在读取期间发生变化：{path.name}")
    return digest.hexdigest()


def _occupied(path: Path) -> bool:
    """Include dangling symlinks and case variants when planning on Linux."""
    if os.path.lexists(path):
        return True
    return path.parent.exists() and any(child.name.casefold() == path.name.casefold() for child in path.parent.iterdir())


def _move_no_replace(source: Path, destination: Path, expected_identity: _Fingerprint | None = None) -> None:
    """Move within one filesystem without ever replacing an existing path.

    Windows rename has no-replace semantics. POSIX rename does not, so use an
    exclusive hard link followed by unlink. No unsafe shutil.move fallback is
    used: unsupported filesystems fail without removing the source.
    """
    before = _fingerprint(_safe_regular(source))
    if expected_identity is not None and before != expected_identity:
        raise ValueError(f"文件在日志写入期间发生变化，未移动：{source.name}")
    if _occupied(destination):
        raise FileExistsError(f"目标已存在，未覆盖：{destination}")
    if os.name == "nt":
        os.rename(source, destination)
        return
    os.link(source, destination, follow_symlinks=False)
    # If another process changed the source, keep both paths for manual recovery.
    if _fingerprint(_safe_regular(source)) != before or _fingerprint(_safe_regular(destination)) != before:
        raise ValueError(f"移动期间文件变化，已保留两处文件：{source.name}")
    source.unlink()


@dataclass(frozen=True)
class FileItem:
    source: Path
    category: str
    is_shortcut: bool
    _identity: _Fingerprint = field(repr=False)


@dataclass(frozen=True)
class ScanResult:
    files: tuple[FileItem, ...]
    ignored_count: int
    extension_counts: dict[str, int]
    category_counts: dict[str, int]
    shortcut_count: int


@dataclass
class MoveItem:
    source: Path
    destination: Path
    category: str
    selected: bool = True


@dataclass
class OperationResult:
    moved_count: int = 0
    errors: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, desktop: Path | None = None, data_dir: Path | None = None):
        # The configured desktop itself is trusted; child reparse points never are.
        self.desktop = Path(desktop if desktop is not None else discover_desktop()).expanduser().resolve()
        self._store = JsonStore(Path(data_dir) if data_dir is not None else default_data_dir())
        self.settings = self._store.load_settings()
        self._issued: dict[int, tuple[Path, Path, str, _Fingerprint]] = {}

    def save_settings(self, settings: Settings) -> None:
        self._store.save_settings(settings)
        self.settings = settings

    def _check_desktop(self) -> None:
        # Known Folder output is the trusted scan root. OneDrive may attach cloud
        # reparse metadata to that root; only children are categorically rejected.
        _safe_directory(self.desktop, allow_reparse=True)

    def _ignored(self, path: Path, info: os.stat_result) -> bool:
        return (
            path.name.startswith(".")
            or path.name.casefold() == "desktop.ini"
            or path.name.casefold() == OUTPUT_FOLDER.casefold()
            or path.suffix.casefold() in _BINARIES
            or not stat.S_ISREG(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & _UNSAFE_ATTRIBUTES)
            or any(fnmatch.fnmatchcase(path.name.casefold(), pattern.casefold()) for pattern in self.settings.ignored_patterns)
        )

    def scan(self) -> ScanResult:
        self._check_desktop()
        files: list[FileItem] = []
        ignored = 0
        for path in sorted(self.desktop.iterdir(), key=lambda child: child.name.casefold()):
            try:
                info = path.lstat()
                if self._ignored(path, info):
                    ignored += 1
                    continue
                category = classify(path)
                files.append(FileItem(path, category, category == CATEGORY_NAMES[5], _fingerprint(info)))
            except OSError:
                ignored += 1  # A disappearing/unreadable file is never moved.
        return ScanResult(tuple(files), ignored, dict(Counter(item.source.suffix.casefold() or "无扩展名" for item in files)), dict(Counter(item.category for item in files)), sum(item.is_shortcut for item in files))

    def _output_directory(self, category: str, create: bool = False) -> Path:
        self._check_desktop()
        if category not in CATEGORY_NAMES:
            raise ValueError("未知分类")
        output = self.desktop / OUTPUT_FOLDER
        folder = output / category
        for directory in (output, folder):
            if create:
                directory.mkdir(exist_ok=True)
            if os.path.lexists(directory):
                _safe_directory(directory)
        return folder

    def preview(self, scan: ScanResult | None = None) -> list[MoveItem]:
        scan = self.scan() if scan is None else scan
        self._issued = {}
        planned: dict[str, set[str]] = {}
        items: list[MoveItem] = []
        keep = {name.casefold() for name in self.settings.keep_shortcuts}
        for file in scan.files:
            if file.is_shortcut and file.source.name.casefold() in keep:
                continue
            folder = self._output_directory(file.category)
            used = planned.setdefault(file.category, set())
            destination = folder / file.source.name
            number = 2
            while destination.name.casefold() in used or _occupied(destination):
                destination = folder / f"{file.source.stem} ({number}){file.source.suffix}"
                number += 1
            used.add(destination.name.casefold())
            item = MoveItem(file.source, destination, file.category)
            self._issued[id(item)] = (item.source, item.destination, item.category, file._identity)
            items.append(item)
        return items

    @contextmanager
    def _lock(self) -> Iterator[None]:
        # OS locks are released after crashes, unlike create-only lockfiles.
        self._store.data_dir.mkdir(parents=True, exist_ok=True)
        _safe_directory(self._store.data_dir)
        lock_path = self._store.data_dir / "operation.lock"
        if os.path.lexists(lock_path):
            _safe_regular(lock_path)
        with lock_path.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise OSError("另一个 DesktopPilot 操作正在运行") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise OSError("另一个 DesktopPilot 操作正在运行") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _validate_plan(self, item: MoveItem) -> _Fingerprint:
        issued = self._issued.get(id(item))
        if issued is None or issued[:3] != (item.source, item.destination, item.category):
            raise ValueError("预览已失效或移动路径被修改，请重新扫描")
        if item.source.parent != self.desktop or item.destination.parent != self._output_directory(item.category):
            raise ValueError("文件必须来自当前桌面并移到指定分类目录")
        info = _safe_regular(item.source)
        if self._ignored(item.source, info) or _fingerprint(info) != issued[3]:
            raise ValueError(f"文件在预览后发生变化，请重新扫描：{item.source.name}")
        if item.source.name.casefold() in {name.casefold() for name in self.settings.keep_shortcuts} and item.source.suffix.casefold() in _EXTENSIONS[CATEGORY_NAMES[5]]:
            raise ValueError(f"该快捷方式已设置为保留：{item.source.name}")
        return issued[3]

    @staticmethod
    def _progress(callback: Progress | None, current: int, total: int) -> None:
        if callback is not None:
            try:
                callback(current, total)
            except Exception:
                pass  # A UI callback must never interrupt durable file bookkeeping.

    def organize(self, items: list[MoveItem], progress: Progress | None = None) -> OperationResult:
        result = OperationResult()
        selected = [item for item in items if item.selected]
        if not selected:
            return result
        try:
            with self._lock():
                history = self._store.load_history()
                record = {"id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(), "status": "running", "entries": []}
                # A complete intent record is durable before the first move.
                for item in selected:
                    record["entries"].append({"source": str(item.source), "destination": str(item.destination), "category": item.category, "status": "pending"})
                history.append(record)
                self._store.save_history(history)
                for index, (item, entry) in enumerate(zip(selected, record["entries"], strict=True), 1):
                    moving = False
                    try:
                        identity = self._validate_plan(item)
                        self._output_directory(item.category, create=True)
                        if _occupied(item.destination):
                            raise FileExistsError(f"目标已存在，未覆盖：{item.destination.name}")
                        checksum = _digest(item.source)
                        if _fingerprint(_safe_regular(item.source)) != identity:
                            raise ValueError(f"文件在确认后发生变化：{item.source.name}")
                        entry.update(status="moving", sha256=checksum, identity=list(identity))
                        self._store.save_history(history)
                        moving = True
                        # Journal fsync can take time. Revalidate both the parent
                        # directory chain and the exact previewed file afterwards.
                        self._output_directory(item.category)
                        _move_no_replace(item.source, item.destination, identity)
                        entry.update(status="moved", identity=list(_fingerprint(_safe_regular(item.destination))))
                        result.moved_count += 1
                    except (OSError, ValueError) as exc:
                        entry["error"] = str(exc)
                        # A crash/error between filesystem and journal writes is
                        # recoverable: keep 'moving' if a destination was created.
                        entry["status"] = "moving" if moving and os.path.lexists(item.destination) else "failed"
                        result.errors.append(f"{item.source.name}：{exc}")
                    self._store.save_history(history)
                    self._progress(progress, index, len(selected))
                record["status"] = "completed" if not result.errors else ("partial" if result.moved_count else "failed")
                self._store.save_history(history)
        except (OSError, ValueError) as exc:
            result.errors.append(f"整理停止，文件和恢复记录已保留：{exc}")
        return result

    def history(self) -> list[dict]:
        return list(reversed(self._store.load_history()))

    def _validate_entry_paths(self, entry: dict) -> tuple[Path, Path]:
        source, destination = Path(entry["source"]), Path(entry["destination"])
        category = entry.get("category", destination.parent.name)
        if source.parent != self.desktop or destination.parent != self._output_directory(category):
            raise ValueError("历史记录路径不属于当前桌面，未移动任何文件")
        if source.name.startswith(".") or source.name.casefold() == "desktop.ini" or source.suffix.casefold() in _BINARIES:
            raise ValueError("历史记录包含受保护文件")
        return source, destination

    def undo(self, progress: Progress | None = None) -> OperationResult:
        result = OperationResult()
        recoverable = {"moved", "moving", "undo_failed", "undoing"}
        try:
            with self._lock():
                history = self._store.load_history()
                record = next((batch for batch in reversed(history) if any(entry["status"] in recoverable for entry in batch["entries"])), None)
                if record is None:
                    return result
                entries = [entry for entry in reversed(record["entries"]) if entry["status"] in recoverable]
                for index, entry in enumerate(entries, 1):
                    try:
                        source, destination = self._validate_entry_paths(entry)
                        checksum = entry.get("sha256")
                        if not isinstance(checksum, str) or len(checksum) != 64:
                            raise ValueError("恢复记录缺少校验信息，请手动检查文件")
                        # Resume a completed undo whose final journal write failed.
                        if entry["status"] == "undoing" and not os.path.lexists(destination) and os.path.lexists(source) and _digest(source) == checksum:
                            entry["status"] = "undone"
                            continue
                        if _occupied(source):
                            raise FileExistsError(f"桌面已有同名文件，未覆盖：{source.name}")
                        if _digest(destination) != checksum:
                            raise ValueError(f"整理后的文件已被修改，未移动：{destination.name}")
                        identity = entry.get("identity")
                        if identity is None or list(_fingerprint(_safe_regular(destination))) != identity:
                            raise ValueError(f"整理后的文件已被替换或修改，未移动：{destination.name}")
                        entry["status"] = "undoing"
                        self._store.save_history(history)
                        self._output_directory(destination.parent.name)
                        _move_no_replace(destination, source, tuple(identity))
                        entry.pop("error", None)
                        entry["status"] = "undone"
                        result.moved_count += 1
                    except (OSError, ValueError) as exc:
                        entry["error"] = str(exc)
                        entry["status"] = "undo_failed"
                        result.errors.append(f"{Path(entry['source']).name}：{exc}")
                    self._store.save_history(history)
                    self._progress(progress, index, len(entries))
                record["status"] = "partially_undone" if result.errors else "undone"
                self._store.save_history(history)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result.errors.append(f"撤销停止，文件和恢复记录已保留：{exc}")
        return result
