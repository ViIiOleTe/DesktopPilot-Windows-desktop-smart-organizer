"""Plain data shared by the shell adapter, layout engine and user interface.

Positions use Explorer desktop-view client coordinates. Monitor work areas are
mapped into that same space; no Qt logical-pixel conversion is applied. Icon
parsing names are opaque identity metadata, never paths to open or mutate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CATEGORY_LABELS = {
    "folder": "文件夹",
    "software": "普通软件",
    "game": "游戏",
    "file": "普通文件",
    "system": "系统图标",
}
CATEGORY_NAMES = tuple(CATEGORY_LABELS.values())


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, point: Point, spacing: Point | None = None) -> bool:
        if spacing is None:
            return self.left <= point.x < self.right and self.top <= point.y < self.bottom
        return (
            self.left <= point.x and self.top <= point.y
            and point.x + spacing.x <= self.right
            and point.y + spacing.y <= self.bottom
        )


@dataclass(frozen=True)
class Monitor:
    id: str
    work_area: Rect
    dpi: int = 96


@dataclass(frozen=True)
class Icon:
    identity: str
    name: str
    parsing_name: str
    position: Point
    is_folder: bool = False
    is_shortcut: bool = False
    is_filesystem: bool = True
    target: str = ""
    arguments: str = ""
    category: str = ""


@dataclass(frozen=True)
class DesktopState:
    icons: tuple[Icon, ...]
    monitors: tuple[Monitor, ...]
    spacing: Point
    icon_size: int = 32
    auto_arrange: bool = False
    warnings: tuple[str, ...] = ()


@dataclass
class Settings:
    theme: str = "dark"
    category_order: list[str] = field(default_factory=lambda: list(CATEGORY_LABELS))
    direction: str = "column"
    category_gap: int = 1
    pinned_ids: list[str] = field(default_factory=list)
    ignored_patterns: list[str] = field(default_factory=list)
    game_rules: list[str] = field(default_factory=lambda: ["steam://*", "*\\steamapps\\*", "com.epicgames.launcher://*"])
    category_overrides: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not isinstance(self.theme, str) or self.theme not in {"dark", "light"}:
            raise ValueError("主题必须为 dark 或 light。")
        if not isinstance(self.category_order, list) or any(not isinstance(category, str) for category in self.category_order) or len(self.category_order) != len(CATEGORY_LABELS) or set(self.category_order) != set(CATEGORY_LABELS):
            raise ValueError("分类顺序必须包含且仅包含五种分类。")
        if not isinstance(self.direction, str) or self.direction not in {"column", "row"}:
            raise ValueError("排列方向必须为 column 或 row。")
        if type(self.category_gap) is not int or not 0 <= self.category_gap <= 10:
            raise ValueError("分类间隔必须为 0 到 10 个网格。")
        for label, values in (("固定图标", self.pinned_ids), ("忽略列表", self.ignored_patterns), ("游戏规则", self.game_rules)):
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() or "\x00" in value for value in values):
                raise ValueError(f"{label}必须是非空字符串列表。")
        if not isinstance(self.category_overrides, dict) or any(not isinstance(identity, str) or not identity or "\x00" in identity or not isinstance(category, str) or category not in CATEGORY_LABELS for identity, category in self.category_overrides.items()):
            raise ValueError("分类覆盖必须使用有效图标标识和分类。")


@dataclass(frozen=True)
class LayoutItem:
    identity: str
    name: str
    category: str
    source: Point
    destination: Point
    pinned: bool = False


@dataclass
class LayoutPlan:
    state: DesktopState
    items: tuple[LayoutItem, ...]
    warnings: list[str] = field(default_factory=list)
    kind: str = "arrange"
    token: str = ""


@dataclass
class OperationResult:
    changed_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
