"""DesktopPilot's Qt interface. File operations are delegated to the safe core."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
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
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .core import CATEGORY_NAMES, Engine, Settings


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


class MainWindow(QMainWindow):
    """The main application window; injected engines make GUI tests safe."""

    operation_finished = Signal()

    def __init__(self, engine: Engine | None = None):
        super().__init__()
        self.engine = engine if engine is not None else Engine()
        self._thread: _JobThread | None = None
        self._plan: list[Any] = []
        self._scan: Any = None
        self._history: list[dict[str, Any]] = []
        self._has_snapshot = False
        self._job_callback: Callable[[Any], None] | None = None
        self._busy_controls: list[QWidget] = []
        self.setWindowTitle("DesktopPilot · 智能桌面整理助手")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "icon.png")))
        # Fit the screen in logical pixels, including high-DPI Windows displays.
        available = self.screen().availableGeometry()
        self.setMinimumSize(min(900, available.width()), min(600, available.height()))
        self.resize(min(1280, available.width()), min(850, available.height()))
        self.setFont(QFont("Segoe UI", 10))
        self._build_ui()
        self._load_settings_fields()
        self._apply_theme(self.engine.settings.theme)
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
        side.addWidget(_label("让桌面，回归井然有序", "muted"))
        side.addSpacing(34)
        side.addWidget(_label("工作空间", "eyebrow"))
        side.addSpacing(4)
        self.nav_buttons: list[QPushButton] = []
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, (icon, title) in enumerate([
            ("▦", "桌面概览"), ("≡", "整理预览"), ("↶", "整理历史"), ("⚙", "软件设置"),
        ]):
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
        safety_layout.addWidget(_label("✓  文件安全优先", "smallTitle"))
        safety_layout.addWidget(_label("先预览，再整理。\n不覆盖文件，支持撤销。", "muted", True))
        side.addWidget(safety)
        side.addSpacing(8)
        side.addWidget(_label(f"DESKTOPPILOT  /  {__version__}", "eyebrow"))
        outer.addWidget(sidebar)

        workspace = QWidget()
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(30, 24, 30, 20)
        layout.setSpacing(18)
        top = QHBoxLayout()
        self.page_title = _label("桌面概览", "pageTitle")
        top.addWidget(self.page_title)
        top.addStretch()
        self.status_chip = _label("●  正在准备", "chip")
        top.addWidget(self.status_chip)
        layout.addLayout(top)
        self.desktop_label = _label(f"桌面位置  {self.engine.desktop}", "muted", True)
        self.desktop_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.desktop_label)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._dashboard_page())
        self.pages.addWidget(self._preview_page())
        self.pages.addWidget(self._history_page())
        self.pages.addWidget(self._settings_page())
        layout.addWidget(self.pages, 1)
        bottom = QHBoxLayout()
        self.status_label = _label("正在读取桌面…", "muted")
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(180)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        bottom.addWidget(self.progress_bar)
        layout.addLayout(bottom)
        outer.addWidget(workspace, 1)

    def _dashboard_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page.setMinimumHeight(660)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setMinimumHeight(172)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(28, 20, 20, 20)
        copy = QVBoxLayout()
        copy.addWidget(_label("A CLEAR DESK. A CLEAR MIND.", "heroEyebrow"))
        copy.addWidget(_label("桌面清爽一点，\n专注多一点。", "heroTitle"))
        copy.addWidget(_label("智能分类散落的文件与快捷方式，整理从一次预览开始。", "heroDescription", True))
        hero_layout.addLayout(copy, 1)
        hero_layout.addWidget(_Orbit())
        layout.addWidget(hero)

        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.stat_values: dict[str, QLabel] = {}
        for key, title, footer, accent in [
            ("files", "桌面文件", "本次扫描识别", "blue"),
            ("shortcuts", "应用快捷方式", "只整理链接文件", "purple"),
            ("categories", "文件分类", "按格式自动归类", "teal"),
            ("ignored", "已安全忽略", "文件夹 / 系统对象等", "amber"),
        ]:
            frame = _card()
            frame.setMinimumHeight(126)
            box = QVBoxLayout(frame)
            box.setContentsMargins(18, 17, 18, 17)
            box.setSpacing(8)
            box.addWidget(_label(title, "muted"))
            value = _label("—", "stat")
            value.setProperty("accent", accent)
            self.stat_values[key] = value
            box.addWidget(value)
            box.addWidget(_label(footer, "tiny"))
            stats.addWidget(frame, 1)
        layout.addLayout(stats)

        frame = _card()
        frame.setMinimumHeight(254)
        categories = QVBoxLayout(frame)
        categories.setContentsMargins(22, 18, 22, 18)
        heading = QHBoxLayout()
        heading.addWidget(_label("分类一览", "sectionTitle"))
        heading.addStretch()
        heading.addWidget(_label("DESKTOP INSIGHTS", "eyebrow"))
        categories.addLayout(heading)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)
        self.category_labels: dict[str, QLabel] = {}
        self.category_bars: dict[str, QProgressBar] = {}
        for index, category in enumerate(CATEGORY_NAMES):
            row = QVBoxLayout()
            row.setSpacing(6)
            text_row = QHBoxLayout()
            text_row.addWidget(_label(category))
            text_row.addStretch()
            count = _label("0", "muted")
            self.category_labels[category] = count
            text_row.addWidget(count)
            row.addLayout(text_row)
            bar = QProgressBar()
            bar.setTextVisible(False)
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(5)
            self.category_bars[category] = bar
            row.addWidget(bar)
            grid.addLayout(row, index // 2, index % 2)
        categories.addSpacing(8)
        categories.addLayout(grid)
        categories.addSpacing(8)
        self.file_type_summary = _label("文件类型统计将在扫描完成后显示", "tiny", True)
        categories.addWidget(self.file_type_summary)
        layout.addWidget(frame, 1)
        actions = QHBoxLayout()
        self.scan_button = _button("↻  重新扫描")
        self.scan_button.clicked.connect(self.rescan)
        self.undo_button = _button("↶  撤销上次整理")
        self.undo_button.clicked.connect(self.request_undo)
        self.organize_button = _button("一键整理  →", "primary")
        self.organize_button.setMinimumHeight(48)
        self.organize_button.setMinimumWidth(205)
        self.organize_button.clicked.connect(lambda: self._show_page(1))
        actions.addWidget(self.scan_button)
        actions.addWidget(self.undo_button)
        actions.addStretch()
        actions.addWidget(self.organize_button)
        layout.addLayout(actions)
        self._busy_controls.extend([self.scan_button, self.undo_button, self.organize_button])
        scroll.setWidget(page)
        return scroll

    def _preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        hint = _card()
        hint_layout = QVBoxLayout(hint)
        hint_layout.setContentsMargins(20, 16, 20, 16)
        hint_layout.addWidget(_label("确认每一次移动", "sectionTitle"))
        hint_layout.addWidget(_label(
            "文件将分类到桌面的“DesktopPilot整理”文件夹。取消勾选可保留文件；同名文件会使用新名称。确认后才开始移动。",
            "muted", True))
        layout.addWidget(hint)
        tools = QHBoxLayout()
        self.preview_summary = _label("正在生成预览…", "smallTitle")
        tools.addWidget(self.preview_summary, 1)
        self.select_all_button = _button("全选", "quiet")
        self.clear_selection_button = _button("取消全选", "quiet")
        self.select_all_button.clicked.connect(lambda: self._select_all(True))
        self.clear_selection_button.clicked.connect(lambda: self._select_all(False))
        tools.addWidget(self.select_all_button)
        tools.addWidget(self.clear_selection_button)
        layout.addLayout(tools)
        self.preview_table = self._table(["移动", "文件名", "分类", "原位置", "目标位置"])
        self.preview_table.setColumnWidth(0, 52)
        self.preview_table.setColumnWidth(1, 170)
        self.preview_table.setColumnWidth(2, 110)
        self.preview_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.preview_table.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.preview_table, 1)
        self.preview_empty = _label("桌面已经很整洁，暂无待整理文件。", "muted")
        self.preview_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.preview_empty)
        footer = QHBoxLayout()
        footer.addWidget(_label("✓  不删除文件   ·   不覆盖文件   ·   支持撤销", "muted"), 1)
        self.confirm_button = _button("确认并开始整理", "primary")
        self.confirm_button.setMinimumHeight(46)
        self.confirm_button.setMinimumWidth(190)
        self.confirm_button.clicked.connect(self.request_organize)
        footer.addWidget(self.confirm_button)
        layout.addLayout(footer)
        self._busy_controls.extend([
            self.confirm_button, self.select_all_button, self.clear_selection_button,
            self.preview_table,
        ])
        return page

    def _history_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        intro = QHBoxLayout()
        copy = QVBoxLayout()
        copy.addWidget(_label("每一次整理，都有迹可循", "sectionTitle"))
        copy.addWidget(_label("保留文件移动记录；撤销时若原位置有同名文件，会保留现有文件并提示。", "muted", True))
        intro.addLayout(copy, 1)
        self.history_undo_button = _button("↶  撤销上次整理")
        self.history_undo_button.clicked.connect(self.request_undo)
        intro.addWidget(self.history_undo_button)
        layout.addLayout(intro)
        self.history_table = self._table(["整理时间", "状态", "文件", "原位置", "整理后位置"])
        self.history_table.setColumnWidth(0, 150)
        self.history_table.setColumnWidth(1, 90)
        self.history_table.setColumnWidth(2, 150)
        self.history_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.history_table, 1)
        self.history_empty = _label("还没有整理记录。完成第一次整理后，会在这里记录每个文件的去向。", "muted", True)
        self.history_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.history_empty)
        self._busy_controls.append(self.history_undo_button)
        return page

    def _settings_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(16)
        appearance = _card()
        box = QVBoxLayout(appearance)
        box.setContentsMargins(22, 20, 22, 20)
        box.addWidget(_label("外观", "sectionTitle"))
        row = QHBoxLayout()
        row.addWidget(_label("选择你喜欢的工作空间主题", "muted"), 1)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("深色 · Midnight", "dark")
        self.theme_combo.addItem("浅色 · Daylight", "light")
        self.theme_combo.setMinimumWidth(180)
        row.addWidget(self.theme_combo)
        box.addLayout(row)
        layout.addWidget(appearance)

        exclusions = _card()
        box = QVBoxLayout(exclusions)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(9)
        box.addWidget(_label("自定义忽略列表", "sectionTitle"))
        box.addWidget(_label("每行一个文件名或通配符。例如 *.tmp、工作计划.docx。匹配忽略大小写。", "muted", True))
        self.ignore_edit = QPlainTextEdit()
        self.ignore_edit.setPlaceholderText("*.tmp\n保留的文件.txt")
        self.ignore_edit.setMinimumHeight(100)
        self.ignore_edit.setMaximumHeight(135)
        box.addWidget(self.ignore_edit)
        layout.addWidget(exclusions)

        shortcuts = _card()
        box = QVBoxLayout(shortcuts)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(9)
        box.addWidget(_label("保留常用快捷方式", "sectionTitle"))
        box.addWidget(_label("每行一个快捷方式文件名，包含 .lnk 或 .url 后缀。例如 微信.lnk。这些快捷方式会留在桌面。", "muted", True))
        self.keep_edit = QPlainTextEdit()
        self.keep_edit.setPlaceholderText("微信.lnk\n浏览器.lnk")
        self.keep_edit.setMinimumHeight(85)
        self.keep_edit.setMaximumHeight(115)
        box.addWidget(self.keep_edit)
        layout.addWidget(shortcuts)
        layout.addWidget(_label(
            "安全规则始终启用：跳过文件夹、隐藏及系统对象、符号链接、程序本体和本程序的整理目录。快捷方式只移动链接本身。",
            "muted", True))
        row = QHBoxLayout()
        row.addStretch()
        self.save_settings_button = _button("保存设置并重新扫描", "primary")
        self.save_settings_button.clicked.connect(self.save_settings)
        row.addWidget(self.save_settings_button)
        layout.addLayout(row)
        layout.addStretch()
        scroll.setWidget(page)
        self._busy_controls.extend([
            self.theme_combo, self.ignore_edit, self.keep_edit, self.save_settings_button,
        ])
        return scroll

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(48)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setMinimumSectionSize(48)
        return table

    @Slot(int)
    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        self.page_title.setText(["桌面概览", "整理预览", "整理历史", "软件设置"][index])

    def _snapshot(self) -> dict[str, Any]:
        scan = self.engine.scan()
        return {"scan": scan, "plan": self.engine.preview(scan), "history": self.engine.history()}

    @Slot()
    def rescan(self) -> None:
        self._start_job(lambda progress: self._snapshot(), self._render_snapshot, "正在扫描桌面并生成安全预览…")

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
        self.status_label.setText("操作未完成，请查看提示后重试。")
        self.status_chip.setText("●  需要关注")
        QMessageBox.warning(self, "操作未完成", f"DesktopPilot 无法完成此次操作：\n\n{error}\n\n请检查文件权限及桌面位置后重新扫描。")

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
        self.status_label.setText(f"正在处理文件 {current} / {total}…")

    def _render_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._scan = snapshot["scan"]
        self._plan = snapshot["plan"]
        self._history = snapshot["history"]
        self._has_snapshot = True
        counts = self._scan.category_counts
        self.stat_values["files"].setText(str(len(self._scan.files)))
        self.stat_values["shortcuts"].setText(str(self._scan.shortcut_count))
        self.stat_values["categories"].setText(str(sum(value > 0 for value in counts.values())))
        self.stat_values["ignored"].setText(str(self._scan.ignored_count))
        maximum = max(1, max(counts.values(), default=0))
        for category in CATEGORY_NAMES:
            count = counts.get(category, 0)
            self.category_labels[category].setText(str(count))
            self.category_bars[category].setValue(round(count / maximum * 100))
        extensions = sorted(self._scan.extension_counts.items(), key=lambda item: (-item[1], item[0]))
        self.file_type_summary.setText("常见文件类型  " + "  ·  ".join(
            f"{extension.upper().lstrip('.')} {count}" for extension, count in extensions[:8]
        ) if extensions else "暂无可统计的文件类型")
        self._render_preview()
        self._render_history()
        self.status_chip.setText("●  扫描已完成")
        self.status_label.setText(f"扫描完成 · {len(self._plan)} 个文件可整理 · {self._scan.ignored_count} 个对象已忽略")
        self._update_action_states()

    def _render_preview(self) -> None:
        self.preview_table.blockSignals(True)
        self.preview_table.setRowCount(len(self._plan))
        for row, item in enumerate(self._plan):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked if item.selected else Qt.CheckState.Unchecked)
            check.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.preview_table.setItem(row, 0, check)
            for column, text in enumerate([
                item.source.name, item.category, str(item.source), str(item.destination),
            ], start=1):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                self.preview_table.setItem(row, column, cell)
        self.preview_table.blockSignals(False)
        self.preview_empty.setVisible(not self._plan)
        self._selection_changed()

    @Slot()
    def _selection_changed(self, *args: Any) -> None:
        for row, item in enumerate(self._plan):
            cell = self.preview_table.item(row, 0)
            if cell is not None:
                item.selected = cell.checkState() == Qt.CheckState.Checked
        selected = sum(bool(item.selected) for item in self._plan)
        self.preview_summary.setText(f"待整理 {len(self._plan)} 个文件 · 已选择 {selected} 个")
        self._update_action_states()

    def _select_all(self, selected: bool) -> None:
        self.preview_table.blockSignals(True)
        for row in range(self.preview_table.rowCount()):
            self.preview_table.item(row, 0).setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
        self.preview_table.blockSignals(False)
        self._selection_changed()

    def _render_history(self) -> None:
        rows = [(record, entry) for record in self._history for entry in record.get("entries", [])]
        self.history_table.setRowCount(len(rows))
        statuses = {
            "moved": "已整理", "undone": "已恢复", "restored": "已恢复", "failed": "失败",
            "error": "需要关注", "pending": "待处理", "skipped": "已跳过", "conflict": "存在冲突",
            "completed": "已完成", "partial": "部分完成", "undo_failed": "恢复失败",
            "moving": "移动待核验", "undoing": "恢复待核验", "running": "进行中",
            "partially_undone": "部分已恢复",
        }
        for row, (record, entry) in enumerate(rows):
            source = str(entry.get("source", ""))
            destination = str(entry.get("destination", ""))
            raw_timestamp = str(record.get("created_at", ""))
            try:
                timestamp = datetime.fromisoformat(raw_timestamp).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                timestamp = raw_timestamp.replace("T", " ")[:19]
            status = str(entry.get("status", record.get("status", "")))
            filename = source.replace("\\", "/").rsplit("/", 1)[-1]
            values = [timestamp, statuses.get(status, status), filename, source, destination]
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setToolTip(str(entry.get("error", "")) or text)
                self.history_table.setItem(row, column, cell)
        self.history_empty.setVisible(not rows)

    def _update_action_states(self) -> None:
        available = not self.busy
        self.organize_button.setEnabled(available and self._has_snapshot and bool(self._plan))
        self.confirm_button.setEnabled(available and any(item.selected for item in self._plan))
        recoverable = {"moved", "moving", "undo_failed", "undoing"}
        can_undo = any(entry.get("status") in recoverable for record in self._history for entry in record.get("entries", []))
        self.undo_button.setEnabled(available and can_undo)
        self.history_undo_button.setEnabled(available and can_undo)

    @Slot()
    def request_organize(self) -> None:
        selected = sum(bool(item.selected) for item in self._plan)
        if self.busy or not selected:
            return
        answer = QMessageBox.question(
            self, "确认整理桌面", f"将移动已勾选的 {selected} 个文件。\n\n"
            f"目标文件夹：\n{self.engine.desktop / 'DesktopPilot整理'}\n\n"
            "请确认预览中的原位置与目标位置。不会覆盖已有文件，完成后可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        plan = list(self._plan)

        def operation(progress: Callable[[int, int], None]) -> dict[str, Any]:
            result = self.engine.organize(plan, progress=progress)
            return {"result": result, "snapshot": self._snapshot()}

        self._start_job(operation, lambda value: self._operation_result(value, "整理"), "正在整理文件，请勿关闭窗口…")

    @Slot()
    def request_undo(self) -> None:
        if self.busy:
            return
        answer = QMessageBox.question(
            self, "确认撤销上次整理", "将恢复上一次尚未撤销的整理记录中的文件。\n\n"
            "若原位置已存在同名文件，或整理后的文件已被修改，将跳过并提示。现有文件不会被覆盖。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def operation(progress: Callable[[int, int], None]) -> dict[str, Any]:
            result = self.engine.undo(progress=progress)
            return {"result": result, "snapshot": self._snapshot()}

        self._start_job(operation, lambda value: self._operation_result(value, "撤销"), "正在恢复文件，请勿关闭窗口…")

    def _operation_result(self, value: dict[str, Any], action: str) -> None:
        self._render_snapshot(value["snapshot"])
        result = value["result"]
        self.status_label.setText(f"{action}完成 · 成功移动 {result.moved_count} 个文件 · {len(result.errors)} 个问题")
        self.status_chip.setText("●  需要关注" if result.errors else "●  已安全完成")
        if result.errors:
            detail = "\n".join(result.errors[:15])
            if len(result.errors) > 15:
                detail += f"\n…另有 {len(result.errors) - 15} 个问题，请查看历史记录。"
            QMessageBox.warning(self, f"{action}部分完成", f"成功移动 {result.moved_count} 个文件。\n\n以下文件未完成：\n{detail}")
        else:
            QMessageBox.information(self, f"{action}完成", f"已安全移动 {result.moved_count} 个文件。\n可在“整理历史”查看文件位置。")

    def _load_settings_fields(self) -> None:
        settings = self.engine.settings
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(settings.theme)))
        self.ignore_edit.setPlainText("\n".join(settings.ignored_patterns))
        self.keep_edit.setPlainText("\n".join(settings.keep_shortcuts))

    @Slot()
    def save_settings(self) -> None:
        if self.busy:
            return
        theme = str(self.theme_combo.currentData())
        settings = Settings(
            theme=theme,
            ignored_patterns=[line.strip() for line in self.ignore_edit.toPlainText().splitlines() if line.strip()],
            keep_shortcuts=[line.strip() for line in self.keep_edit.toPlainText().splitlines() if line.strip()],
        )

        def operation(progress: Callable[[int, int], None]) -> dict[str, Any]:
            self.engine.save_settings(settings)
            return self._snapshot()

        def completed(snapshot: dict[str, Any]) -> None:
            self._apply_theme(theme)
            self._render_snapshot(snapshot)
            self.status_label.setText("设置已保存，已按新规则重新扫描桌面。")

        self._start_job(operation, completed, "正在保存设置并更新预览…")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy:
            event.ignore()
            QMessageBox.information(self, "正在安全处理文件", "请等待当前操作完成后再关闭 DesktopPilot，以便完整保存整理记录。")
        else:
            event.accept()

    def _apply_theme(self, theme: str) -> None:
        dark = theme != "light"
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
            QPlainTextEdit, QComboBox {{ background: {c['field']}; border: 1px solid {c['line']}; border-radius: 7px; padding: 9px; selection-background-color: #3b72be; }}
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
