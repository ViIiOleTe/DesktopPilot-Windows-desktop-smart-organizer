"""Desktop icon control center; only the Shell backend changes icon positions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .core import Engine
from .models import CATEGORY_LABELS, LayoutItem, Settings


class _JobThread(QThread):
    """One background job, with no QObject moved between Python-owned threads.

    Keeping execution on the QThread subclass also avoids deleting a Python
    worker QObject while its queued result handlers are still being delivered.
    All widget access remains in MainWindow's queued slots.
    """

    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, operation: Callable[[Callable[[int, int], None]], Any], parent: QWidget):
        super().__init__(parent)
        self.operation = operation

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation(self.progress.emit)
            self.succeeded.emit(result)
        except Exception as exc:  # Report background failures on the GUI thread.
            logging.exception("DesktopPilot background operation failed")
            self.failed.emit(str(exc) or type(exc).__name__)


class _Orbit(QWidget):
    """Small scalable illustration, without platform-specific icon fonts."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(146, 136)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#517bc5"), 1))
        painter.setBrush(QColor("#1d3659"))
        painter.drawEllipse(15, 10, 116, 116)
        painter.setPen(Qt.PenStyle.NoPen)
        for x, y, color in [(32, 36, "#7ba7ff"), (83, 36, "#a391ff"),
                             (32, 83, "#60ceba"), (83, 83, "#ffce83")]:
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(x, y, 32, 27, 7, 7)
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(x + 7, y + 8, 18, 3, 1, 1)
            painter.drawRoundedRect(x + 7, y + 15, 12, 3, 1, 1)


def _label(text: str, kind: str = "", wrap: bool = False) -> QLabel:
    label = QLabel(text)
    if kind:
        label.setProperty("kind", kind)
    label.setWordWrap(wrap)
    return label


def _button(text: str, kind: str = "secondary") -> QPushButton:
    button = QPushButton(text)
    button.setProperty("kind", kind)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumHeight(38)
    return button


def _card() -> QFrame:
    frame = QFrame()
    frame.setProperty("kind", "card")
    return frame


class DesktopCanvas(QWidget):
    """Scaled virtual-monitor map. Rendering never opens an icon's target."""

    COLORS = {"folder": "#e8bd7a", "software": "#8db8ff", "game": "#b5a1fa", "file": "#6ed5bc", "system": "#c0ccdc"}

    def __init__(self, proposed: bool = False):
        super().__init__()
        self.state: Any = None
        self.plan: Any = None
        self.proposed = proposed
        self.dark = True
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self._hits: list[tuple[QRectF, str]] = []

    def set_layout(self, state: Any, plan: Any = None) -> None:
        self.state, self.plan = state, plan
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#97a6bd" if self.dark else "#63738b"))
        if self.state is None or not self.state.monitors:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "扫描 Windows 桌面视图后，在这里查看图标布局")
            return
        areas = [monitor.work_area for monitor in self.state.monitors]
        left, top = min(a.left for a in areas), min(a.top for a in areas)
        right, bottom = max(a.right for a in areas), max(a.bottom for a in areas)
        scale = min((self.width() - 48) / max(1, right - left), (self.height() - 48) / max(1, bottom - top))
        offset_x = (self.width() - (right - left) * scale) / 2
        offset_y = (self.height() - (bottom - top) * scale) / 2

        def point(position: Any) -> QPointF:
            return QPointF(offset_x + (position.x - left) * scale, offset_y + (position.y - top) * scale)

        for monitor in self.state.monitors:
            area = monitor.work_area
            rect = QRectF(offset_x + (area.left - left) * scale, offset_y + (area.top - top) * scale, area.width * scale, area.height * scale)
            painter.setPen(QPen(QColor("#496288" if self.dark else "#9db5d8"), 1))
            painter.setBrush(QColor("#131d2d" if self.dark else "#edf3fc"))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QColor("#7890b2" if self.dark else "#607697"))
            painter.setFont(QFont(self.font().family(), 8))
            painter.drawText(rect.adjusted(9, 5, -6, -5), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight, f"{monitor.id} · {area.width}×{area.height} · {monitor.dpi} DPI")
        plan_items = {item.identity: item for item in self.plan.items} if self.plan is not None else {}
        self._hits = []
        for icon in self.state.icons:
            item = plan_items.get(icon.identity)
            current = point(icon.position)
            proposed = point(item.destination) if self.proposed and item is not None else current
            color = QColor(self.COLORS.get(item.category if item else icon.category, "#8db8ff"))
            if self.proposed and item is not None and item.source != item.destination:
                painter.setPen(QPen(QColor("#6b83a5"), 1, Qt.PenStyle.DashLine))
                painter.drawLine(current, proposed)
                delta_x, delta_y = proposed.x() - current.x(), proposed.y() - current.y()
                length = math.hypot(delta_x, delta_y)
                if length > 12:
                    ux, uy = delta_x / length, delta_y / length
                    painter.drawLine(proposed, QPointF(proposed.x() - 7 * ux + 3 * uy, proposed.y() - 7 * uy - 3 * ux))
                    painter.drawLine(proposed, QPointF(proposed.x() - 7 * ux - 3 * uy, proposed.y() - 7 * uy + 3 * ux))
                painter.setPen(QPen(QColor("#6b83a5"), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(current, 3, 3)
            size = max(7.0, min(16.0, self.state.icon_size * scale))
            icon_rect = QRectF(proposed.x() - size / 2, proposed.y() - size / 2, size, size)
            painter.setPen(QPen(QColor("#ffd17e"), 2) if item is not None and item.pinned else QPen(color, 1))
            painter.setBrush(color)
            painter.drawRoundedRect(icon_rect, 3, 3)
            painter.setPen(QColor("#dbe6f7" if self.dark else "#31425f"))
            painter.setFont(QFont(self.font().family(), 7))
            label_rect = QRectF(proposed.x() - 35, proposed.y() + size / 2 + 2, 70, 14)
            label = icon.name if len(icon.name) <= 10 else icon.name[:9] + "…"
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter, label)
            tooltip = f"{icon.name}\n当前坐标：({icon.position.x}, {icon.position.y})"
            if item is not None:
                tooltip += f"\n预览坐标：({item.destination.x}, {item.destination.y})"
                if item.pinned:
                    tooltip += "\n已固定位置"
            self._hits.append((icon_rect.adjusted(-6, -6, 6, 20), tooltip))

    def mouseMoveEvent(self, event: Any) -> None:
        self.setToolTip(next((text for rect, text in reversed(self._hits) if rect.contains(event.position())), ""))
        super().mouseMoveEvent(event)


class MainWindow(QMainWindow):
    """Modern UI for scanning and arranging Windows Shell icon coordinates."""

    operation_finished = Signal()

    def __init__(self, engine: Engine | None = None):
        super().__init__()
        self.engine = engine if engine is not None else Engine()
        self._thread: _JobThread | None = None
        self._plan: Any = None
        self._plan_error = ""
        self._state: Any = None
        self._scan: Any = None
        self._history: list[dict[str, Any]] = []
        self._saved_layouts: list[dict[str, Any]] = []
        self._has_snapshot = False
        self._job_callback: Callable[[Any], None] | None = None
        self._busy_controls: list[QWidget] = []
        self.setWindowTitle("DesktopPilot · 桌面图标控制中心")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "icon.png")))
        available = self.screen().availableGeometry()
        self.setMinimumSize(min(900, available.width()), min(600, available.height()))
        self.resize(min(1280, available.width()), min(880, available.height()))
        self.setFont(QFont("Segoe UI", 10))
        self._build_ui()
        self._load_settings_fields()
        self._apply_theme(self.engine.settings.theme)
        self._update_action_states()
        QTimer.singleShot(0, self.rescan)

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(216)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 30, 20, 24)
        side.setSpacing(8)
        brand = QHBoxLayout()
        mark = _label("DP", "logo")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(40, 40)
        brand.addWidget(mark)
        brand.addWidget(_label("DesktopPilot", "brand"))
        side.addLayout(brand)
        side.addWidget(_label("桌面图标，井然有序", "muted"))
        side.addSpacing(30)
        side.addWidget(_label("图标控制中心", "eyebrow"))
        self.nav_buttons: list[QPushButton] = []
        group = QButtonGroup(self)
        for index, (icon, title) in enumerate([("▦", "桌面概览"), ("≡", "排列预览"), ("↶", "布局与历史"), ("⚙", "排列设置")]):
            button = _button(f"{icon}    {title}", "nav")
            button.setCheckable(True)
            button.setMinimumHeight(46)
            group.addButton(button, index)
            self.nav_buttons.append(button)
            side.addWidget(button)
        group.idClicked.connect(self._show_page)
        self.nav_buttons[0].setChecked(True)
        side.addStretch()
        safety = QFrame()
        safety.setProperty("kind", "safety")
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(14, 14, 14, 14)
        safety_layout.addWidget(_label("✓  只调整图标坐标", "smallTitle"))
        safety_layout.addWidget(_label("先预览，再排列。\n保留文件位置与内容。", "muted", True))
        side.addWidget(safety)
        side.addSpacing(8)
        side.addWidget(_label(f"DESKTOPPILOT  /  {__version__}", "eyebrow"))
        outer.addWidget(sidebar)
        workspace = QWidget()
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(30, 24, 30, 20)
        layout.setSpacing(14)
        top = QHBoxLayout()
        self.page_title = _label("桌面概览", "pageTitle")
        top.addWidget(self.page_title)
        top.addStretch()
        self.status_chip = _label("●  正在准备", "chip")
        top.addWidget(self.status_chip)
        layout.addLayout(top)
        self.desktop_label = _label("Windows Shell 桌面视图 · 多显示器坐标布局", "muted", True)
        layout.addWidget(self.desktop_label)
        self.auto_arrange_warning = _label("", "muted", True)
        self.auto_arrange_warning.setStyleSheet("color: #d79b40; padding: 10px; background: rgba(200,140,40,0.1); border-radius: 7px;")
        self.auto_arrange_warning.hide()
        layout.addWidget(self.auto_arrange_warning)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._dashboard_page())
        self.pages.addWidget(self._preview_page())
        self.pages.addWidget(self._history_page())
        self.pages.addWidget(self._settings_page())
        layout.addWidget(self.pages, 1)
        footer = QHBoxLayout()
        self.status_label = _label("正在连接 Windows 桌面视图…", "muted", True)
        footer.addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(160)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        footer.addWidget(self.progress_bar)
        layout.addLayout(footer)
        outer.addWidget(workspace, 1)

    def _dashboard_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page.setMinimumHeight(710)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(16)
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setMinimumHeight(132)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(24, 14, 18, 14)
        copy = QVBoxLayout()
        copy.addWidget(_label("YOUR DESKTOP. PERFECTLY ALIGNED.", "heroEyebrow"))
        copy.addWidget(_label("让图标各就其位。", "heroTitle"))
        copy.addWidget(_label("分类排列桌面图标，保存熟悉的布局，一键找回秩序。", "heroDescription", True))
        hero_layout.addLayout(copy, 1)
        illustration = _Orbit()
        illustration.setFixedHeight(110)
        hero_layout.addWidget(illustration)
        layout.addWidget(hero)
        stats = QGridLayout()
        stats.setSpacing(12)
        self.stat_values: dict[str, QLabel] = {}
        for index, (key, title) in enumerate([("icons", "桌面图标总数"), *CATEGORY_LABELS.items()]):
            frame = _card()
            frame.setMinimumHeight(80)
            box = QHBoxLayout(frame)
            box.setContentsMargins(16, 12, 16, 12)
            box.addWidget(_label(title, "muted"), 1)
            value = _label("—", "stat")
            value.setProperty("accent", {"folder": "amber", "game": "purple", "file": "teal"}.get(key, "blue"))
            self.stat_values[key] = value
            box.addWidget(value)
            stats.addWidget(frame, index // 3, index % 3)
        layout.addLayout(stats)
        frame = _card()
        frame.setMinimumHeight(280)
        box = QVBoxLayout(frame)
        box.setContentsMargins(18, 14, 18, 12)
        heading = QHBoxLayout()
        heading.addWidget(_label("当前桌面布局", "sectionTitle"))
        heading.addStretch()
        self.monitor_summary = _label("等待扫描", "eyebrow")
        heading.addWidget(self.monitor_summary)
        box.addLayout(heading)
        self.current_canvas = DesktopCanvas()
        box.addWidget(self.current_canvas, 1)
        box.addWidget(_label("悬停图标查看名称与坐标 · 显示器使用实际工作区比例", "tiny"))
        layout.addWidget(frame, 1)
        actions = QHBoxLayout()
        self.scan_button = _button("↻  重新扫描")
        self.scan_button.clicked.connect(self.rescan)
        self.save_layout_button = _button("◇  保存当前布局")
        self.save_layout_button.clicked.connect(self.save_current_layout)
        self.undo_button = _button("↶  一键撤销")
        self.undo_button.clicked.connect(self.request_undo)
        self.organize_button = _button("智能排列  →", "primary")
        self.organize_button.setMinimumHeight(46)
        self.organize_button.clicked.connect(self.show_arrange_preview)
        for button in [self.scan_button, self.save_layout_button, self.undo_button]:
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.organize_button)
        layout.addLayout(actions)
        self._busy_controls.extend([self.scan_button, self.save_layout_button, self.undo_button, self.organize_button])
        scroll.setWidget(page)
        return scroll

    def _preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.preview_summary = _label("扫描后可预览图标排列", "sectionTitle")
        layout.addWidget(self.preview_summary)
        self.plan_warnings = _label("", "muted", True)
        self.plan_warnings.hide()
        layout.addWidget(self.plan_warnings)
        self.preview_canvas = DesktopCanvas(proposed=True)
        self.preview_canvas.setMinimumHeight(160)
        layout.addWidget(self.preview_canvas, 1)
        layout.addWidget(_label("虚线箭头：当前坐标 → 预览坐标 · 金色边框：保持当前位置", "tiny"))
        self.preview_table = self._table(["固定位置", "图标名称", "分类", "当前坐标", "预览坐标"])
        self.preview_table.setMinimumHeight(140)
        self.preview_table.setColumnWidth(0, 75)
        self.preview_table.setColumnWidth(1, 190)
        self.preview_table.setColumnWidth(2, 130)
        self.preview_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.preview_table.itemChanged.connect(self._icon_rules_edited)
        layout.addWidget(self.preview_table, 1)
        tools = QHBoxLayout()
        self.save_icon_rules_button = _button("保存固定 / 分类规则并重新预览")
        self.save_icon_rules_button.clicked.connect(self.save_icon_rules)
        tools.addWidget(self.save_icon_rules_button)
        tools.addStretch()
        self.confirm_button = _button("确认并应用排列", "primary")
        self.confirm_button.setMinimumHeight(44)
        self.confirm_button.clicked.connect(self.request_organize)
        tools.addWidget(self.confirm_button)
        layout.addLayout(tools)
        self._busy_controls.extend([self.preview_table, self.save_icon_rules_button, self.confirm_button])
        return page

    def _history_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(_label("保存布局，随时回到熟悉的位置", "sectionTitle"))
        layout.addWidget(_label("自动保留操作前的图标坐标；手动布局可重复恢复。已消失的图标不会重新创建。", "muted", True))
        tools = QHBoxLayout()
        self.saved_layouts_combo = QComboBox()
        self.saved_layouts_combo.setMinimumWidth(200)
        self.saved_layouts_combo.currentIndexChanged.connect(self._update_action_states)
        tools.addWidget(self.saved_layouts_combo, 1)
        self.restore_layout_button = _button("预览恢复布局", "primary")
        self.restore_layout_button.clicked.connect(self.restore_layout_preview)
        tools.addWidget(self.restore_layout_button)
        self.history_save_button = _button("保存当前布局")
        self.history_save_button.clicked.connect(self.save_current_layout)
        tools.addWidget(self.history_save_button)
        layout.addLayout(tools)
        self.history_table = self._table(["布局名称", "保存时间", "类型", "图标数", "状态"])
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history_table.setColumnWidth(1, 160)
        self.history_table.setColumnWidth(2, 125)
        self.history_table.setColumnWidth(3, 70)
        self.history_table.setColumnWidth(4, 100)
        self.history_table.itemSelectionChanged.connect(self._select_history_layout)
        layout.addWidget(self.history_table, 1)
        self.history_empty = _label("暂无已保存的布局。点击“保存当前布局”记录桌面图标位置。", "muted", True)
        self.history_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.history_empty)
        self.history_undo_button = _button("↶  撤销上次坐标调整")
        self.history_undo_button.clicked.connect(self.request_undo)
        layout.addWidget(self.history_undo_button, 0, Qt.AlignmentFlag.AlignRight)
        self._busy_controls.extend([self.saved_layouts_combo, self.restore_layout_button, self.history_save_button, self.history_undo_button])
        return page

    def _settings_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        appearance = _card()
        box = QVBoxLayout(appearance)
        box.setContentsMargins(22, 18, 22, 18)
        box.addWidget(_label("工作空间外观", "sectionTitle"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("深色 · Midnight", "dark")
        self.theme_combo.addItem("浅色 · Daylight", "light")
        box.addWidget(self.theme_combo)
        layout.addWidget(appearance)
        arrangement = _card()
        box = QVBoxLayout(arrangement)
        box.setContentsMargins(22, 18, 22, 18)
        box.addWidget(_label("分类顺序与排列方式", "sectionTitle"))
        box.addWidget(_label("每行一个分类，调整行的顺序即可改变排列次序。须包含全部五个分类。", "muted", True))
        self.category_order_edit = QPlainTextEdit()
        self.category_order_edit.setFixedHeight(115)
        box.addWidget(self.category_order_edit)
        row = QHBoxLayout()
        row.addWidget(_label("排列方向", "muted"))
        self.direction_combo = QComboBox()
        self.direction_combo.addItem("按列 · 从上到下", "column")
        self.direction_combo.addItem("按行 · 从左到右", "row")
        row.addWidget(self.direction_combo, 1)
        row.addWidget(_label("分类间隔（网格）", "muted"))
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 10)
        row.addWidget(self.gap_spin)
        box.addLayout(row)
        layout.addWidget(arrangement)
        games = _card()
        box = QVBoxLayout(games)
        box.setContentsMargins(22, 18, 22, 18)
        box.addWidget(_label("游戏识别规则", "sectionTitle"))
        box.addWidget(_label("每行一个名称、目标或启动参数的通配符。例如 steam://*、*\\steamapps\\*。可在排列预览中手动覆盖分类。", "muted", True))
        self.game_rules_edit = QPlainTextEdit()
        self.game_rules_edit.setMinimumHeight(90)
        self.game_rules_edit.setMaximumHeight(125)
        box.addWidget(self.game_rules_edit)
        layout.addWidget(games)
        layout.addWidget(_label("在“排列预览”勾选固定位置可保留常用图标；修改图标分类后，需保存规则并重新预览。排列前请关闭 Windows 的“自动排列图标”。", "muted", True))
        self.save_settings_button = _button("保存设置并重新预览", "primary")
        self.save_settings_button.clicked.connect(self.save_settings)
        layout.addWidget(self.save_settings_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addStretch()
        scroll.setWidget(page)
        self._busy_controls.extend([self.theme_combo, self.category_order_edit, self.direction_combo, self.gap_spin, self.game_rules_edit, self.save_settings_button])
        return scroll

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(44)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        table.horizontalHeader().setHighlightSections(False)
        return table

    @Slot(int)
    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        self.page_title.setText(["桌面概览", "排列预览", "布局与历史", "排列设置"][index])

    def _snapshot(self) -> dict[str, Any]:
        state = self.engine.scan()
        try:
            plan, plan_error = self.engine.preview(state), ""
        except ValueError as exc:
            # A full grid prevents arranging, but scanning and saving the
            # existing layout remain useful and safe. Pins can still be edited.
            plan, plan_error = None, str(exc)
        return {"state": state, "plan": plan, "plan_error": plan_error, "layouts": self.engine.saved_layouts(), "history": self.engine.history()}

    @Slot()
    def rescan(self) -> None:
        self._start_job(lambda progress: self._snapshot(), self._render_snapshot, "正在读取 Windows Shell 桌面图标与显示器坐标…")

    def _start_job(self, operation: Callable[..., Any], on_success: Callable[[Any], None], message: str) -> None:
        if self.busy:
            return
        self._job_callback = on_success
        self._thread = _JobThread(operation, self)
        self._thread.succeeded.connect(self._job_succeeded)
        self._thread.failed.connect(self._job_failed)
        self._thread.progress.connect(self._progress)
        self._thread.finished.connect(self._job_ended)
        self._thread.finished.connect(self._thread.deleteLater)
        self._set_busy(True)
        self.status_label.setText(message)
        self._thread.start()

    @Slot(object)
    def _job_succeeded(self, value: Any) -> None:
        if self._job_callback is not None:
            try:
                self._job_callback(value)
            except Exception as exc:
                logging.exception("Failed to display operation result")
                self._job_failed(str(exc))

    @Slot(str)
    def _job_failed(self, error: str) -> None:
        self._plan = None
        self.status_label.setText("Windows 桌面操作未完成，请查看提示。")
        self.status_chip.setText("●  需要关注")
        QMessageBox.warning(self, "桌面视图操作未完成", f"{error}\n\nDesktopPilot 需要 Windows 10/11 的 Explorer 桌面视图。请确认 Explorer 正常运行；本程序不会改用文件目录扫描或文件移动。")

    @Slot()
    def _job_ended(self) -> None:
        self._thread = None
        self._job_callback = None
        self._set_busy(False)
        self.operation_finished.emit()

    def _set_busy(self, busy: bool) -> None:
        for widget in self._busy_controls:
            widget.setEnabled(not busy)
        self.progress_bar.setVisible(busy)
        self.progress_bar.setRange(0, 0 if busy else 100)
        if busy:
            self.status_chip.setText("●  正在处理")
        else:
            self._update_action_states()

    @Slot(int, int)
    def _progress(self, current: int, total: int) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(current)
        self.status_label.setText(f"正在调整图标坐标 {current} / {total}…")

    def _render_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._state = snapshot["state"]
        self._scan = self._state
        self._plan = snapshot["plan"]
        self._plan_error = snapshot.get("plan_error", "")
        self._saved_layouts = snapshot["layouts"]
        self._history = snapshot["history"]
        self._has_snapshot = True
        self._rules_dirty = False
        self.stat_values["icons"].setText(str(len(self._state.icons)))
        for category in CATEGORY_LABELS:
            self.stat_values[category].setText(str(sum(icon.category == category for icon in self._state.icons)))
        self.monitor_summary.setText(f"{len(self._state.monitors)} 个显示器 · 图标间距 {self._state.spacing.x}×{self._state.spacing.y}")
        self.current_canvas.set_layout(self._state)
        self._render_preview()
        self._render_history()
        auto = self._state.auto_arrange
        self.auto_arrange_warning.setVisible(auto)
        self.auto_arrange_warning.setText("Windows 已开启自动排列，应用排列与恢复暂不可用。请右键桌面 → 查看 → 关闭“自动排列图标”，然后重新扫描。仍可扫描及保存当前布局。")
        self.status_chip.setText("●  请关闭自动排列" if auto else ("●  需要调整布局规则" if self._plan_error else "●  桌面视图已连接"))
        self.status_label.setText(self._plan_error or f"已读取 {len(self._state.icons)} 个图标 · 仅调整图标坐标 · 排列前自动保存布局")
        self._update_action_states()

    def _render_preview(self) -> None:
        plan = self._plan
        items = plan.items if plan is not None else tuple(
            LayoutItem(icon.identity, icon.name, icon.category, icon.position, icon.position, icon.identity in self.engine.settings.pinned_ids)
            for icon in self._state.icons
        ) if self._state is not None else ()
        self.preview_table.blockSignals(True)
        self.preview_table.setRowCount(len(items))
        for row, item in enumerate(items):
            pin = QTableWidgetItem()
            pin.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            pin.setCheckState(Qt.CheckState.Checked if item.pinned else Qt.CheckState.Unchecked)
            pin.setData(Qt.ItemDataRole.UserRole, item.identity)
            self.preview_table.setItem(row, 0, pin)
            name = QTableWidgetItem(item.name)
            name.setToolTip(item.name)
            self.preview_table.setItem(row, 1, name)
            combo = QComboBox()
            combo.setStyleSheet("QComboBox { padding: 3px 6px; min-height: 20px; }")
            combo.addItem(f"自动识别（{CATEGORY_LABELS.get(item.category, item.category)}）", "")
            for category, label in CATEGORY_LABELS.items():
                combo.addItem(label, category)
            combo.setCurrentIndex(max(0, combo.findData(self.engine.settings.category_overrides.get(item.identity, ""))))
            combo.currentIndexChanged.connect(self._icon_rules_edited)
            self.preview_table.setCellWidget(row, 2, combo)
            for column, position in [(3, item.source), (4, item.destination)]:
                self.preview_table.setItem(row, column, QTableWidgetItem(f"({position.x}, {position.y})"))
        self.preview_table.blockSignals(False)
        self._rules_dirty = False
        self.preview_canvas.set_layout(self._state, plan)
        changed = sum(item.source != item.destination for item in items)
        kind = "恢复布局" if plan is not None and plan.kind == "restore" else "智能排列"
        self.preview_summary.setText(f"{kind}预览 · {len(items)} 个图标 · {changed} 个位置将调整")
        warnings = list(plan.warnings) if plan is not None else [self._plan_error] if self._plan_error else []
        if self._state is not None:
            warnings.extend(warning for warning in getattr(self._state, "warnings", []) if warning not in warnings)
        self.plan_warnings.setText("\n".join(warnings))
        self.plan_warnings.setVisible(bool(warnings))
        self.confirm_button.setText("确认并恢复布局" if kind == "恢复布局" else "确认并应用排列")

    @Slot()
    def _icon_rules_edited(self, *args: Any) -> None:
        self._rules_dirty = True
        self.status_label.setText("固定或分类规则已修改。请先保存规则并重新预览，再应用排列。")
        self._update_action_states()

    def _render_history(self) -> None:
        previous = self.saved_layouts_combo.currentData()
        self.saved_layouts_combo.blockSignals(True)
        self.saved_layouts_combo.clear()
        for layout in self._saved_layouts:
            name = str(layout.get("name", "未命名布局"))
            self.saved_layouts_combo.addItem(f"{name} · {len(layout.get('icons', []))} 个图标", layout["id"])
        index = self.saved_layouts_combo.findData(previous)
        if index >= 0:
            self.saved_layouts_combo.setCurrentIndex(index)
        self.saved_layouts_combo.blockSignals(False)
        self.history_table.blockSignals(True)
        self.history_table.setRowCount(len(self._history))
        kinds = {"saved": "手动保存", "manual": "手动保存", "before_arrange": "排列前备份", "before_restore": "恢复前备份", "before_undo": "撤销前备份"}
        statuses = {"saved": "已保存", "completed": "已完成", "applied": "已应用", "pending": "待核验", "partial": "部分完成", "undone": "已撤销", "undo_failed": "撤销需重试", "failed": "未完成", "running": "进行中"}
        for row, record in enumerate(self._history):
            raw_time = str(record.get("created_at", ""))
            try:
                timestamp = datetime.fromisoformat(raw_time).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                timestamp = raw_time
            kind = str(record.get("kind", "saved"))
            status = str(record.get("status", "saved"))
            values = [str(record.get("name", "自动保存布局")), timestamp, kinds.get(kind, kind), str(len(record.get("icons", []))), statuses.get(status, status)]
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                cell.setData(Qt.ItemDataRole.UserRole, record["id"])
                self.history_table.setItem(row, column, cell)
        self.history_table.blockSignals(False)
        self.history_empty.setVisible(not self._history)

    @Slot()
    def _select_history_layout(self) -> None:
        selected = self.history_table.selectedItems()
        if selected:
            index = self.saved_layouts_combo.findData(selected[0].data(Qt.ItemDataRole.UserRole))
            if index >= 0:
                self.saved_layouts_combo.setCurrentIndex(index)

    @Slot()
    def _update_action_states(self, *args: Any) -> None:
        available = not self.busy
        state_ready = self._has_snapshot and self._state is not None
        arrange_ready = available and state_ready and not self._state.auto_arrange
        self.organize_button.setEnabled(arrange_ready)
        self.save_layout_button.setEnabled(available and state_ready)
        self.history_save_button.setEnabled(available and state_ready)
        self.restore_layout_button.setEnabled(arrange_ready and self.saved_layouts_combo.currentData() is not None)
        self.confirm_button.setEnabled(arrange_ready and self._plan is not None and not getattr(self, "_rules_dirty", False) and any(item.source != item.destination for item in self._plan.items))
        can_undo = any(record.get("can_undo") and record.get("status") != "undone" for record in self._history)
        self.undo_button.setEnabled(arrange_ready and can_undo)
        self.history_undo_button.setEnabled(arrange_ready and can_undo)
        self.save_icon_rules_button.setEnabled(available and state_ready and getattr(self, "_rules_dirty", False))

    @Slot()
    def show_arrange_preview(self) -> None:
        if self._state is None:
            return
        def completed(snapshot: dict[str, Any]) -> None:
            self._render_snapshot(snapshot)
            self._show_page(1)
        self._start_job(lambda progress: self._snapshot(), completed, "正在计算分类网格与固定图标…")

    def _show_plan(self, plan: Any) -> None:
        self._render_snapshot({"state": plan.state, "plan": plan, "layouts": self._saved_layouts, "history": self._history})
        self._show_page(1)
        self.status_label.setText("预览已生成。请核对图标坐标，确认后应用。")

    def _result_with_snapshot(self, result: Any) -> dict[str, Any]:
        try:
            return {"result": result, "snapshot": self._snapshot(), "refresh_error": ""}
        except Exception as exc:
            return {"result": result, "snapshot": None, "refresh_error": str(exc)}

    @Slot()
    def request_organize(self) -> None:
        plan = self._plan
        if self.busy or plan is None or getattr(self, "_rules_dirty", False):
            return
        if self._state.auto_arrange:
            QMessageBox.warning(self, "请先关闭自动排列", self.auto_arrange_warning.text())
            return
        count = sum(item.source != item.destination for item in plan.items)
        if not count:
            return
        action = "恢复布局" if plan.kind == "restore" else "排列图标"
        answer = QMessageBox.question(self, f"确认{action}", f"将调整 {count} 个桌面图标的位置。\n\n请核对预览中的当前坐标与目标坐标。操作前会自动保存当前布局，文件名称、内容与所在目录保持不变。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_job(lambda progress: self._result_with_snapshot(self.engine.organize(plan, progress=progress)), lambda value: self._operation_result(value, action), "正在调整桌面图标坐标，请勿关闭窗口…")

    @Slot()
    def request_undo(self) -> None:
        if self.busy or self._state is None:
            return
        if self._state.auto_arrange:
            QMessageBox.warning(self, "请先关闭自动排列", self.auto_arrange_warning.text())
            return
        answer = QMessageBox.question(self, "确认撤销图标排列", "将尝试恢复上一次坐标调整前的桌面布局。\n\n已消失的图标会跳过；新出现的图标保留。请确认当前显示器布局正确。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self._start_job(lambda progress: self._result_with_snapshot(self.engine.undo(progress=progress)), lambda value: self._operation_result(value, "撤销"), "正在恢复上次图标坐标…")

    def _operation_result(self, value: dict[str, Any], action: str) -> None:
        if value["snapshot"] is not None:
            self._render_snapshot(value["snapshot"])
        else:
            self._plan = None
        result = value["result"]
        errors = list(result.errors)
        warnings = list(getattr(result, "warnings", []))
        if value["refresh_error"]:
            warnings.append("坐标操作后重新扫描失败，请手动重新扫描：" + value["refresh_error"])
        self.status_label.setText(f"{action}完成 · 已调整 {result.changed_count} 个图标 · {len(errors)} 个错误")
        self.status_chip.setText("●  需要关注" if errors or warnings else "●  布局已更新")
        details = "\n".join((errors + warnings)[:20])
        if errors or warnings:
            QMessageBox.warning(self, f"{action}结果", f"已调整 {result.changed_count} 个图标的位置。\n\n{details}")
        else:
            QMessageBox.information(self, f"{action}完成", f"已调整 {result.changed_count} 个图标的位置。\n操作前布局已保存在“布局与历史”。")

    @Slot()
    def save_current_layout(self) -> None:
        if self.busy:
            return
        name = "手动布局 " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def operation(progress: Callable[[int, int], None]) -> dict[str, Any]:
            self.engine.save_layout(name)
            return self._snapshot()

        def completed(snapshot: dict[str, Any]) -> None:
            self._render_snapshot(snapshot)
            self.status_label.setText("当前图标坐标已保存，可在“布局与历史”选择恢复。")

        self._start_job(operation, completed, "正在保存当前图标布局…")

    @Slot()
    def restore_layout_preview(self) -> None:
        identity = self.saved_layouts_combo.currentData()
        if identity is None or self.busy:
            return
        self._start_job(lambda progress: self.engine.restore_preview(str(identity)), self._show_plan, "正在读取布局并生成恢复预览…")

    def _load_settings_fields(self) -> None:
        settings = self.engine.settings
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(settings.theme)))
        self.category_order_edit.setPlainText("\n".join(CATEGORY_LABELS[category] for category in settings.category_order))
        self.direction_combo.setCurrentIndex(max(0, self.direction_combo.findData(settings.direction)))
        self.gap_spin.setValue(settings.category_gap)
        self.game_rules_edit.setPlainText("\n".join(settings.game_rules))

    def _save_settings_job(self, settings: Settings) -> None:
        def operation(progress: Callable[[int, int], None]) -> dict[str, Any]:
            self.engine.save_settings(settings)
            return self._snapshot()

        def completed(snapshot: dict[str, Any]) -> None:
            self._apply_theme(settings.theme)
            self._render_snapshot(snapshot)
            self.status_label.setText("设置已保存，已重新计算图标排列预览。")

        self._start_job(operation, completed, "正在保存设置并重新计算布局…")

    @Slot()
    def save_settings(self) -> None:
        if self.busy:
            return
        inverse = {label: category for category, label in CATEGORY_LABELS.items()}
        order = [inverse.get(line.strip(), line.strip()) for line in self.category_order_edit.toPlainText().splitlines() if line.strip()]
        settings = replace(self.engine.settings, theme=str(self.theme_combo.currentData()), category_order=order, direction=str(self.direction_combo.currentData()), category_gap=self.gap_spin.value(), game_rules=[line.strip() for line in self.game_rules_edit.toPlainText().splitlines() if line.strip()])
        try:
            settings.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "请检查排列设置", str(exc))
            return
        self._save_settings_job(settings)

    @Slot()
    def save_icon_rules(self) -> None:
        if self.busy or self._state is None:
            return
        visible = {icon.identity for icon in self._state.icons}
        pinned = [identity for identity in self.engine.settings.pinned_ids if identity not in visible]
        overrides = dict(self.engine.settings.category_overrides)
        for row in range(self.preview_table.rowCount()):
            cell = self.preview_table.item(row, 0)
            identity = str(cell.data(Qt.ItemDataRole.UserRole))
            if cell.checkState() == Qt.CheckState.Checked:
                pinned.append(identity)
            combo = self.preview_table.cellWidget(row, 2)
            category = str(combo.currentData())
            if category:
                overrides[identity] = category
            else:
                overrides.pop(identity, None)
        settings = replace(self.engine.settings, pinned_ids=pinned, category_overrides=overrides)
        self._save_settings_job(settings)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy:
            event.ignore()
            QMessageBox.information(self, "正在处理桌面布局", "请等待当前操作完成后再关闭 DesktopPilot，以便完整保存图标布局记录。")
        else:
            event.accept()

    def _apply_theme(self, theme: str) -> None:
        dark = theme != "light"
        self.current_canvas.dark = dark
        self.preview_canvas.dark = dark
        self.current_canvas.update()
        self.preview_canvas.update()
        colors = {
            "bg": "#10141d" if dark else "#f3f6fb",
            "side": "#151b27" if dark else "#ffffff",
            "card": "#1a2230" if dark else "#ffffff",
            "cardAlt": "#1d2737" if dark else "#f7f9fd",
            "line": "#2b3546" if dark else "#e1e7f1",
            "text": "#eef3fc" if dark else "#19283f",
            "muted": "#97a6bd" if dark else "#63738b",
            "blue": "#8db8ff" if dark else "#326cce",
            "selected": "#253d61" if dark else "#e9f1ff",
            "hover": "#253146" if dark else "#edf2fa",
            "field": "#131c29" if dark else "#f8faff",
        }
        c = colors
        self.setStyleSheet(f"""
            QWidget {{ color: {c['text']}; font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Noto Sans CJK SC', sans-serif; font-size: 13px; }}
            QWidget#root, QStackedWidget, QScrollArea {{ background: {c['bg']}; }}
            QFrame#sidebar {{ background: {c['side']}; border-right: 1px solid {c['line']}; }}
            QLabel {{ background: transparent; border: none; }}
            QLabel[kind='logo'] {{ background: #578bea; border-radius: 12px; color: white; font-size: 17px; font-weight: 800; }}
            QLabel[kind='brand'] {{ font-size: 18px; font-weight: 700; }}
            QLabel[kind='pageTitle'] {{ font-size: 24px; font-weight: 700; }}
            QLabel[kind='muted'] {{ color: {c['muted']}; font-size: 12px; }}
            QLabel[kind='tiny'] {{ color: {c['muted']}; font-size: 11px; }}
            QLabel[kind='eyebrow'] {{ color: {c['muted']}; font-size: 10px; font-weight: 600; letter-spacing: 1px; }}
            QLabel[kind='sectionTitle'] {{ font-size: 16px; font-weight: 650; }}
            QLabel[kind='smallTitle'] {{ font-size: 13px; font-weight: 600; }}
            QLabel[kind='chip'] {{ color: {c['blue']}; background: {c['selected']}; padding: 7px 12px; border-radius: 13px; font-size: 11px; }}
            QLabel[kind='stat'] {{ font-size: 31px; font-weight: 700; }}
            QLabel[accent='blue'] {{ color: {c['blue']}; }}
            QLabel[accent='purple'] {{ color: {'#b5a1fa' if dark else '#7960c7'}; }}
            QLabel[accent='teal'] {{ color: {'#6ed5bc' if dark else '#218f78'}; }}
            QLabel[accent='amber'] {{ color: {'#e8bd7a' if dark else '#b57820'}; }}
            QFrame[kind='card'] {{ background: {c['card']}; border: 1px solid {c['line']}; border-radius: 12px; }}
            QFrame[kind='safety'] {{ background: {c['cardAlt']}; border: 1px solid {c['line']}; border-radius: 10px; }}
            QFrame#hero {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #203857,stop:1 #25304d); border: 1px solid #354a6c; border-radius: 16px; }}
            QLabel[kind='heroEyebrow'] {{ color: #8eb7f4; font-size: 10px; letter-spacing: 1.5px; font-weight: 650; }}
            QLabel[kind='heroTitle'] {{ color: #f0f6ff; font-size: 29px; font-weight: 700; }}
            QLabel[kind='heroDescription'] {{ color: #b5c6df; font-size: 12px; }}
            QPushButton {{ border: 1px solid {c['line']}; border-radius: 8px; background: {c['card']}; padding: 8px 15px; font-weight: 550; }}
            QPushButton:hover {{ background: {c['hover']}; border-color: #5c7eaf; }}
            QPushButton:pressed {{ background: {c['selected']}; }}
            QPushButton:disabled {{ color: {c['muted']}; background: {c['bg']}; border-color: {c['line']}; }}
            QPushButton[kind='primary'] {{ color: #ffffff; background: #397bea; border: 1px solid #5a93f2; padding: 10px 22px; font-weight: 650; }}
            QPushButton[kind='primary']:hover {{ background: #508cf1; }}
            QPushButton[kind='primary']:disabled {{ color: {c['muted']}; background: {c['cardAlt']}; border-color: {c['line']}; }}
            QPushButton[kind='quiet'] {{ background: transparent; border: none; color: {c['blue']}; padding: 4px 9px; }}
            QPushButton[kind='nav'] {{ text-align: left; background: transparent; border: none; padding: 11px 14px; color: {c['muted']}; }}
            QPushButton[kind='nav']:hover {{ background: {c['hover']}; color: {c['text']}; }}
            QPushButton[kind='nav']:checked {{ color: {c['blue']}; background: {c['selected']}; font-weight: 650; border-left: 3px solid #6da0fa; }}
            QProgressBar {{ background: {c['line']}; border: none; border-radius: 2px; min-height: 5px; max-height: 6px; }}
            QProgressBar::chunk {{ background: #649af1; border-radius: 2px; }}
            QTableWidget {{ background: {c['card']}; alternate-background-color: {c['cardAlt']}; border: 1px solid {c['line']}; border-radius: 10px; selection-background-color: {c['selected']}; selection-color: {c['text']}; outline: 0; }}
            QTableWidget::item {{ padding: 7px; border-bottom: 1px solid {c['line']}; }}
            QHeaderView {{ background: {c['card']}; }}
            QHeaderView::section {{ color: {c['muted']}; background: {c['cardAlt']}; padding: 12px 8px; border: none; border-bottom: 1px solid {c['line']}; font-size: 11px; font-weight: 600; }}
            QPlainTextEdit, QComboBox, QSpinBox {{ background: {c['field']}; border: 1px solid {c['line']}; border-radius: 7px; padding: 9px; selection-background-color: #3b72be; }}
            QPlainTextEdit:focus, QComboBox:focus {{ border-color: #639af2; }}
            QComboBox::drop-down {{ border: none; width: 23px; }}
            QComboBox QAbstractItemView {{ background: {c['card']}; color: {c['text']}; selection-background-color: {c['selected']}; }}
            QScrollArea {{ border: none; }}
            QScrollArea > QWidget > QWidget {{ background: {c['bg']}; }}
            QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: {c['line']}; min-height: 30px; border-radius: 4px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QToolTip {{ background: {c['card']}; color: {c['text']}; border: 1px solid {c['line']}; padding: 5px; }}
            QMessageBox {{ background: {c['card']}; }}
        """)


def run() -> int:
    """Launch the desktop application and return its Qt exit status."""
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("DesktopPilot")
    app.setOrganizationName("DesktopPilot")
    window = MainWindow()
    window.show()
    return app.exec()
