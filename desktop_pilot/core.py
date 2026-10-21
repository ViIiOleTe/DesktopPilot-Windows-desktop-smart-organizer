"""Preview-first desktop icon positioning with durable coordinate snapshots.

The backend can read shell metadata and change icon coordinates. It receives no
file operation instructions; parsing names and shortcut targets remain opaque.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import stat
from typing import Callable, Iterator, Protocol
import uuid

from .classification import classify_icons
from .layout import build_layout, build_restore_layout
from .models import (
    CATEGORY_LABELS, CATEGORY_NAMES, DesktopState, Icon, LayoutItem, LayoutPlan,
    Monitor, OperationResult, Point, Rect, Settings,
)
from .storage import JsonStore
from .windows import default_data_dir

Progress = Callable[[int, int], None]


class ShellBackend(Protocol):
    def read(self) -> DesktopState: ...
    def position(self, positions: dict[str, Point]) -> DesktopState: ...


def _validate_state(state: DesktopState) -> None:
    if not isinstance(state, DesktopState):
        raise ValueError("桌面服务返回的状态无效。")
    if not isinstance(state.spacing, Point) or any(type(value) is not int or value <= 0 for value in (state.spacing.x, state.spacing.y)):
        raise ValueError("桌面图标间距无效。")
    if type(state.icon_size) is not int or state.icon_size <= 0 or type(state.auto_arrange) is not bool:
        raise ValueError("桌面图标尺寸或自动排列状态无效。")
    if not state.monitors:
        raise ValueError("没有可用的显示器工作区域。")
    monitor_ids = set()
    for monitor in state.monitors:
        if not isinstance(monitor, Monitor) or not isinstance(monitor.id, str) or not monitor.id or monitor.id in monitor_ids:
            raise ValueError("显示器标识缺失或重复。")
        monitor_ids.add(monitor.id)
        area = monitor.work_area
        if not isinstance(area, Rect) or any(type(value) is not int for value in (area.left, area.top, area.right, area.bottom)) or area.width <= 0 or area.height <= 0 or type(monitor.dpi) is not int or monitor.dpi <= 0:
            raise ValueError("显示器工作区域或 DPI 无效。")
    identities = set()
    for icon in state.icons:
        if not isinstance(icon, Icon) or not isinstance(icon.identity, str) or not icon.identity or icon.identity in identities:
            raise ValueError("图标标识为空或重复，无法安全定位。")
        identities.add(icon.identity)
        if not isinstance(icon.position, Point) or any(type(value) is not int for value in (icon.position.x, icon.position.y)):
            raise ValueError("图标坐标无效。")
        if any(not isinstance(value, str) for value in (icon.name, icon.parsing_name, icon.target, icon.arguments, icon.category)) or any(type(value) is not bool for value in (icon.is_folder, icon.is_shortcut, icon.is_filesystem)):
            raise ValueError("图标元数据无效。")


def _state_signature(state: DesktopState) -> str:
    value = asdict(state)
    value.pop("warnings", None)
    value["icons"] = sorted(value["icons"], key=lambda icon: icon["identity"])
    value["monitors"] = sorted(value["monitors"], key=lambda monitor: monitor["id"])
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _settings_signature(settings: Settings) -> str:
    return json.dumps(asdict(settings), sort_keys=True, ensure_ascii=False)


def _plan_signature(plan: LayoutPlan) -> str:
    return json.dumps({"state": _state_signature(plan.state), "items": [asdict(item) for item in plan.items], "kind": plan.kind, "token": plan.token}, sort_keys=True, ensure_ascii=False)


def _intersects(first: Point, second: Point, spacing: Point) -> bool:
    return first.x < second.x + spacing.x and second.x < first.x + spacing.x and first.y < second.y + spacing.y and second.y < first.y + spacing.y


class Engine:
    def __init__(self, backend: ShellBackend | None = None, data_dir: Path | None = None):
        if backend is None:
            from .windows import WindowsShellBackend
            backend = WindowsShellBackend()
        self.backend = backend
        self._store = JsonStore(Path(data_dir) if data_dir is not None else default_data_dir())
        self.settings = self._store.load_settings()
        self._issued: dict[int, dict] = {}

    @property
    def data_dir(self) -> Path:
        return self._store.data_dir

    @contextmanager
    def _lock(self) -> Iterator[None]:
        # OS locks disappear on process exit, including unclean termination.
        lock_path = self.data_dir / "icon-operation.lock"
        if os.path.lexists(lock_path):
            info = lock_path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("操作锁路径不安全。")
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
                    raise OSError("另一个 DesktopPilot 操作正在运行。") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise OSError("另一个 DesktopPilot 操作正在运行。") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def save_settings(self, settings: Settings) -> None:
        settings.validate()
        with self._lock():
            self._store.save_settings(settings)
            # Keep a private copy so callers cannot alter already-saved lists.
            self.settings = Settings(**asdict(settings))

    def scan(self) -> DesktopState:
        self.settings.validate()
        state = self.backend.read()
        _validate_state(state)
        return classify_icons(state, self.settings)

    def _issue(self, plan: LayoutPlan, source_snapshot_id: str | None = None) -> LayoutPlan:
        plan.token = uuid.uuid4().hex
        self._issued = {id(plan): {"signature": _plan_signature(plan), "settings": _settings_signature(self.settings), "source_snapshot_id": source_snapshot_id}}
        return plan

    def preview(self, state: DesktopState | None = None) -> LayoutPlan:
        state = self.scan() if state is None else state
        _validate_state(state)
        return self._issue(build_layout(state, self.settings))

    @staticmethod
    def _progress(callback: Progress | None, current: int, total: int) -> None:
        if callback is not None:
            try:
                callback(current, total)
            except Exception:
                pass  # GUI failures cannot interrupt coordinate bookkeeping.

    def _validate_plan(self, plan: LayoutPlan) -> dict:
        if not isinstance(plan, LayoutPlan):
            raise ValueError("请先生成并确认布局预览。")
        issued = self._issued.get(id(plan))
        if issued is None or issued["signature"] != _plan_signature(plan):
            raise ValueError("布局预览已失效或被修改，请重新扫描。")
        self.settings.validate()
        saved_settings = self._store.load_settings()
        if issued["settings"] != _settings_signature(self.settings) or issued["settings"] != _settings_signature(saved_settings):
            raise ValueError("设置已改变，请重新扫描并确认布局。")
        current = self.scan()
        if _state_signature(current) != _state_signature(plan.state):
            raise ValueError("图标、显示器或网格在预览后发生变化，请重新扫描。")
        if current.auto_arrange:
            raise ValueError("Windows 已启用“自动排列图标”。请在桌面右键 → 查看中关闭该选项后重新扫描；程序不会更改此设置。")
        self._validate_destinations(plan)
        return issued

    def _validate_destinations(self, plan: LayoutPlan) -> None:
        icons = {icon.identity: icon for icon in plan.state.icons}
        if len(plan.items) != len(icons) or {item.identity for item in plan.items} != set(icons):
            raise ValueError("布局必须包含且仅包含当前桌面的图标。")
        final: list[tuple[str, Point]] = []
        for item in plan.items:
            icon = icons[item.identity]
            if item.source != icon.position or item.category not in CATEGORY_LABELS or not isinstance(item.destination, Point) or any(type(value) is not int for value in (item.destination.x, item.destination.y)):
                raise ValueError("布局包含无效的图标坐标。")
            fixed = item.pinned or icon.identity in self.settings.pinned_ids or any(fnmatchcase(icon.name.casefold(), pattern.casefold()) for pattern in self.settings.ignored_patterns)
            if fixed and item.destination != item.source:
                raise ValueError(f"固定图标“{icon.name}”不能改变位置。")
            if not any(monitor.work_area.contains(item.destination, plan.state.spacing) for monitor in plan.state.monitors):
                raise ValueError(f"图标“{icon.name}”超出显示器工作区域，请调整固定图标或显示器后重试。")
            if any(_intersects(item.destination, point, plan.state.spacing) for _, point in final):
                raise ValueError(f"图标“{icon.name}”与其他图标重叠，请调整固定图标后重试。")
            final.append((item.identity, item.destination))

    @staticmethod
    def _snapshot(state: DesktopState, name: str, kind: str, *, can_undo: bool = False) -> dict:
        return {
            "id": uuid.uuid4().hex,
            "name": name.strip()[:120] or "桌面布局",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "status": "pending" if can_undo else "saved",
            "can_undo": can_undo,
            "icons": [{"identity": icon.identity, "name": icon.name, "x": icon.position.x, "y": icon.position.y} for icon in state.icons],
            "monitors": [{"id": monitor.id, "work_area": asdict(monitor.work_area), "dpi": monitor.dpi} for monitor in state.monitors],
            "spacing": asdict(state.spacing),
            "icon_size": state.icon_size,
            "auto_arrange": state.auto_arrange,
        }

    def organize(self, plan: LayoutPlan, progress: Progress | None = None) -> OperationResult:
        result = OperationResult(warnings=list(plan.warnings) if isinstance(plan, LayoutPlan) else [])
        try:
            with self._lock():
                issued = self._validate_plan(plan)
                positions = {item.identity: item.destination for item in plan.items if item.source != item.destination}
                if not positions:
                    result.warnings.append("当前图标已处于目标位置，无需调整。")
                    if plan.kind == "undo":
                        layouts = self._store.load_layouts()
                        for layout in layouts:
                            if layout["id"] == issued["source_snapshot_id"]:
                                layout["status"] = "undone"
                        self._store.save_layouts(layouts)
                    self._issued.clear()
                    return result
                layouts = self._store.load_layouts()
                before = self._snapshot(plan.state, {"arrange": "整理前自动备份", "restore": "恢复前自动备份", "undo": "撤销前自动备份"}.get(plan.kind, "操作前自动备份"), f"before_{plan.kind}", can_undo=plan.kind != "undo")
                before["status"] = "pending"
                before["restored_from"] = issued["source_snapshot_id"]
                layouts.append(before)
                # No native write may happen until the entire coordinate snapshot
                # has been atomically persisted and fsynced.
                self._store.save_layouts(layouts)
                self._progress(progress, 0, len(positions))
                write_attempted = False
                try:
                    # Snapshot IO can be slow; validate again before the only
                    # coordinate-changing backend call.
                    self._validate_plan(plan)
                    write_attempted = True
                    after = self.backend.position(positions)
                    _validate_state(after)
                    actual = {icon.identity: icon.position for icon in after.icons}
                    result.changed_count = sum(actual.get(item.identity) != item.source for item in plan.items if item.identity in positions and item.identity in actual)
                    # Verify stationary and pinned icons too: Explorer or another
                    # desktop extension must not silently displace other items.
                    mismatches = [item.name for item in plan.items if actual.get(item.identity) != item.destination]
                    if mismatches:
                        result.errors.append(f"Windows 未确认 {len(mismatches)} 个图标到达目标位置：{'、'.join(mismatches[:5])}。操作前布局已保留，可撤销或重新预览。")
                    result.warnings.extend(warning for warning in after.warnings if warning not in result.warnings)
                except Exception as exc:
                    result.errors.append(f"调整图标位置失败，操作前布局已保留：{exc}")
                    # A shell call can fail after positioning only some icons.
                    # Count observed changes, while keeping the snapshot undoable.
                    if write_attempted:
                        try:
                            after = self.backend.read()
                            _validate_state(after)
                            actual = {icon.identity: icon.position for icon in after.icons}
                            result.changed_count = sum(item.identity in actual and actual[item.identity] != item.source for item in plan.items if item.identity in positions)
                        except Exception:
                            result.warnings.append("暂时无法重新读取桌面；请重新扫描后通过历史记录恢复布局。")
                    else:
                        # A stale preflight after fsync means this application
                        # made no change; do not label an external edit undoable.
                        before["can_undo"] = False
                before["status"] = "applied" if not result.errors else ("partial" if result.changed_count else "failed")
                if plan.kind == "undo" and not result.errors:
                    for layout in layouts:
                        if layout["id"] == issued["source_snapshot_id"]:
                            layout["status"] = "undone"
                self._store.save_layouts(layouts)
                self._issued.clear()
                self._progress(progress, len(positions), len(positions))
        except Exception as exc:
            result.errors.append(f"未能完成布局操作：{exc}")
        return result

    def save_layout(self, name: str = "") -> dict:
        if not isinstance(name, str):
            raise ValueError("布局名称必须为文本。")
        with self._lock():
            state = self.scan()
            snapshot = self._snapshot(state, name or "手动保存的布局", "saved")
            layouts = self._store.load_layouts()
            layouts.append(snapshot)
            self._store.save_layouts(layouts)
            return snapshot

    def saved_layouts(self) -> list[dict]:
        return list(reversed(self._store.load_layouts()))

    def history(self) -> list[dict]:
        return self.saved_layouts()

    def restore_preview(self, snapshot_id: str) -> LayoutPlan:
        snapshot = next((layout for layout in self._store.load_layouts() if layout["id"] == snapshot_id), None)
        if snapshot is None:
            raise ValueError("未找到保存的图标布局。")
        state = self.scan()
        positions = {icon["identity"]: Point(icon["x"], icon["y"]) for icon in snapshot["icons"]}
        plan = build_restore_layout(state, positions, self.settings)
        old_monitors = snapshot["monitors"]
        new_monitors = [{"id": monitor.id, "work_area": asdict(monitor.work_area), "dpi": monitor.dpi} for monitor in state.monitors]
        if old_monitors != new_monitors or snapshot["spacing"] != asdict(state.spacing):
            plan.warnings.insert(0, "显示器或网格配置已改变；已按当前工作区域校验并重新安排不可用坐标，请确认预览。")
        return self._issue(plan, snapshot_id)

    def restore_layout(self, snapshot_id: str, progress: Progress | None = None) -> OperationResult:
        # Explicit callers can treat this method as their confirmed command.
        # The GUI uses restore_preview + organize so users inspect remapping first.
        try:
            return self.organize(self.restore_preview(snapshot_id), progress)
        except Exception as exc:
            return OperationResult(errors=[str(exc)])

    def undo(self, progress: Progress | None = None) -> OperationResult:
        try:
            snapshot = next((layout for layout in self.saved_layouts() if layout.get("can_undo") and layout["status"] != "undone"), None)
            if snapshot is None:
                return OperationResult(warnings=["没有可撤销的图标布局。"])
            plan = self.restore_preview(snapshot["id"])
            plan.kind = "undo"
            self._issue(plan, snapshot["id"])
            return self.organize(plan, progress)
        except Exception as exc:
            return OperationResult(errors=[str(exc)])
