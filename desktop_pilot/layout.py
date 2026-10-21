"""Pure, deterministic desktop icon coordinate planning.

No filesystem, COM, window message or shell calls belong in this module. Grid
Positions, work areas and spacing share Explorer desktop-view client units.
Monitor DPI is metadata and is never applied again to those coordinates.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

from .classification import classify_icons
from .models import DesktopState, Icon, LayoutItem, LayoutPlan, Monitor, Point, Rect, Settings


def _validate(state: DesktopState, settings: Settings) -> None:
    settings.validate()
    if not state.monitors:
        raise ValueError("未检测到可用显示器，无法生成桌面布局。")
    if any(type(value) is not int or value <= 0 for value in (state.spacing.x, state.spacing.y)):
        raise ValueError("桌面图标网格间距无效。")
    if len({monitor.id for monitor in state.monitors}) != len(state.monitors):
        raise ValueError("显示器标识重复，无法生成安全布局。")
    for monitor in state.monitors:
        area = monitor.work_area
        if any(type(value) is not int for value in (area.left, area.top, area.right, area.bottom)) or area.width <= 0 or area.height <= 0:
            raise ValueError("显示器工作区无效。")
    identities: set[str] = set()
    for icon in state.icons:
        if not icon.identity or icon.identity in identities:
            raise ValueError("桌面图标标识为空或重复，请重新扫描。")
        identities.add(icon.identity)
        if any(type(value) is not int for value in (icon.position.x, icon.position.y)):
            raise ValueError("桌面图标坐标无效。")


def _fixed(icon: Icon, settings: Settings) -> bool:
    return icon.identity in settings.pinned_ids or any(
        fnmatchcase(icon.name.casefold(), pattern.casefold())
        for pattern in settings.ignored_patterns
    )


def _footprint(point: Point, spacing: Point) -> Rect:
    return Rect(point.x, point.y, point.x + spacing.x, point.y + spacing.y)


def _intersects(first: Rect, second: Rect) -> bool:
    return first.left < second.right and second.left < first.right and first.top < second.bottom and second.top < first.bottom


def _free(point: Point, spacing: Point, occupied: list[Rect]) -> bool:
    rectangle = _footprint(point, spacing)
    return not any(_intersects(rectangle, other) for other in occupied)


def _affiliation(point: Point, monitors: tuple[Monitor, ...]) -> Monitor:
    containing = next((monitor for monitor in monitors if monitor.work_area.contains(point)), None)
    if containing is not None:
        return containing
    # Distance to the closest work-area edge, rather than monitor centre, also
    # behaves predictably for a negative-origin or disconnected display.
    def distance(monitor: Monitor) -> tuple[int, str]:
        area = monitor.work_area
        dx = max(area.left - point.x, 0, point.x - (area.right - 1))
        dy = max(area.top - point.y, 0, point.y - (area.bottom - 1))
        return dx * dx + dy * dy, monitor.id
    return min(monitors, key=distance)


def _slots(monitor: Monitor, spacing: Point, direction: str) -> list[Point]:
    area = monitor.work_area
    columns, rows = area.width // spacing.x, area.height // spacing.y
    if direction == "column":
        return [Point(area.left + column * spacing.x, area.top + row * spacing.y) for column in range(columns) for row in range(rows)]
    return [Point(area.left + column * spacing.x, area.top + row * spacing.y) for row in range(rows) for column in range(columns)]


def _warnings(state: DesktopState, fixed_icons: list[Icon]) -> list[str]:
    warnings = []
    if state.auto_arrange:
        warnings.append("Windows 已启用自动排列图标；预览仍可查看，应用布局前请关闭自动排列。")
    for icon in fixed_icons:
        if not any(monitor.work_area.contains(icon.position, state.spacing) for monitor in state.monitors):
            warnings.append(f"固定图标“{icon.name}”位于工作区边缘或外部，已保留原坐标。")
    return warnings


def _pack(icons: list[Icon], slots: list[Point], gap: int) -> dict[str, Point] | None:
    result: dict[str, Point] = {}
    cursor = 0
    previous_category: str | None = None
    for icon in icons:
        if previous_category is not None and previous_category != icon.category:
            cursor += gap
        if cursor >= len(slots):
            return None
        result[icon.identity] = slots[cursor]
        cursor += 1
        previous_category = icon.category
    return result


def build_layout(state: DesktopState, settings: Settings) -> LayoutPlan:
    """Plan category groups on each icon's current (or closest) monitor.

    Pins and ignored filenames keep their coordinates and reserve a complete
    grid footprint, including when they sit between grid cells. Insufficient
    category gaps are removed once; insufficient actual capacity is an error.
    """
    _validate(state, settings)
    state = classify_icons(state, settings)
    fixed_icons = [icon for icon in state.icons if _fixed(icon, settings)]
    fixed_ids = {icon.identity for icon in fixed_icons}
    occupied = [_footprint(icon.position, state.spacing) for icon in fixed_icons]
    warnings = _warnings(state, fixed_icons)
    destinations = {icon.identity: icon.position for icon in fixed_icons}
    category_order = {category: index for index, category in enumerate(settings.category_order)}
    for monitor in state.monitors:
        movable = [icon for icon in state.icons if icon.identity not in fixed_ids and _affiliation(icon.position, state.monitors).id == monitor.id]
        movable.sort(key=lambda icon: (category_order[icon.category], icon.name.casefold(), icon.identity))
        slots = [point for point in _slots(monitor, state.spacing, settings.direction) if _free(point, state.spacing, occupied)]
        assigned = _pack(movable, slots, settings.category_gap)
        if assigned is None and settings.category_gap:
            assigned = _pack(movable, slots, 0)
            if assigned is not None:
                warnings.append(f"显示器“{monitor.id}”空间有限，已取消分类间隔以容纳图标。")
        if assigned is None:
            raise ValueError(f"显示器“{monitor.id}”没有足够空位；请减少固定图标、缩小图标间距或调整显示器。")
        destinations.update(assigned)
        occupied.extend(_footprint(point, state.spacing) for point in assigned.values())
    return LayoutPlan(
        state=state,
        items=tuple(LayoutItem(icon.identity, icon.name, icon.category, icon.position, destinations[icon.identity], icon.identity in fixed_ids) for icon in state.icons),
        warnings=warnings,
    )


def build_restore_layout(state: DesktopState, saved_positions: dict[str, Point], settings: Settings) -> LayoutPlan:
    """Restore matching identities without displacing new, ignored or pinned icons.

    Saved coordinates are used exactly when still visible and unoccupied.
    Changed-monitor or conflicting coordinates are assigned safe current grid
    slots; removed icons are ignored and no synthetic shell items are created.
    """
    _validate(state, settings)
    if not isinstance(saved_positions, dict) or any(
        not isinstance(identity, str) or not isinstance(point, Point)
        or any(type(value) is not int for value in (point.x, point.y))
        for identity, point in saved_positions.items()
    ):
        raise ValueError("保存的布局坐标无效。")
    state = classify_icons(state, settings)
    fixed_icons = [icon for icon in state.icons if _fixed(icon, settings) or icon.identity not in saved_positions]
    fixed_ids = {icon.identity for icon in fixed_icons}
    occupied = [_footprint(icon.position, state.spacing) for icon in fixed_icons]
    warnings = _warnings(state, fixed_icons)
    destinations = {icon.identity: icon.position for icon in fixed_icons}
    removed_count = len(set(saved_positions) - {icon.identity for icon in state.icons})
    if removed_count:
        warnings.append(f"保存布局中的 {removed_count} 个图标已不存在，已跳过。")
    pending: list[Icon] = []
    # Reserve all exact valid positions before filling any fallback gaps.
    for icon in sorted(state.icons, key=lambda value: value.identity):
        if icon.identity in fixed_ids:
            continue
        saved = saved_positions[icon.identity]
        if any(monitor.work_area.contains(saved, state.spacing) for monitor in state.monitors) and _free(saved, state.spacing, occupied):
            destinations[icon.identity] = saved
            occupied.append(_footprint(saved, state.spacing))
        else:
            pending.append(icon)
    for icon in pending:
        saved = saved_positions[icon.identity]
        preferred = _affiliation(saved, state.monitors)
        current = _affiliation(icon.position, state.monitors)
        ordered = [preferred] + ([current] if current.id != preferred.id else [])
        ordered.extend(monitor for monitor in state.monitors if monitor.id not in {value.id for value in ordered})
        target = next((point for monitor in ordered for point in _slots(monitor, state.spacing, settings.direction) if _free(point, state.spacing, occupied)), None)
        if target is None:
            raise ValueError("当前显示器没有足够空位恢复布局；请调整显示器或固定图标后重试。")
        destinations[icon.identity] = target
        occupied.append(_footprint(target, state.spacing))
        warnings.append(f"图标“{icon.name}”的原坐标已不可用，已安排到最近可用网格。")
    return LayoutPlan(
        state=state,
        items=tuple(LayoutItem(icon.identity, icon.name, icon.category, icon.position, destinations[icon.identity], icon.identity in fixed_ids) for icon in state.icons),
        warnings=warnings,
        kind="restore",
    )
