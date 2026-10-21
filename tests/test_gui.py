"""End-to-end Qt control-center checks with fictional Shell icon metadata only."""
from dataclasses import replace
import threading
import time

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from desktop_pilot.core import Engine
from desktop_pilot.gui import MainWindow
from desktop_pilot.models import CATEGORY_LABELS, DesktopState, Icon, Monitor, Point, Rect
from desktop_pilot.simulation import SimulatedBackend, sample_desktop


def wait_until(qapp, condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return
        QTest.qWait(5)
    raise AssertionError("Qt operation did not finish before timeout")


def positions(state):
    return {icon.identity: icon.position for icon in state.icons}


def metadata(state):
    return {
        icon.identity: (icon.name, icon.parsing_name, icon.target, icon.arguments)
        for icon in state.icons
    }


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *args: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    widget = MainWindow(Engine(backend=SimulatedBackend(), data_dir=tmp_path / "state"))
    widget.show()
    wait_until(qapp, lambda: widget._has_snapshot and not widget.busy)
    yield widget
    wait_until(qapp, lambda: not widget.busy)
    widget.close()
    qapp.processEvents()


def test_shell_scan_six_statistics_and_preview_do_not_change_coordinates(window, qapp):
    backend = window.engine.backend
    original = backend.state
    expected = {"icons": "7", "folder": "1", "software": "2", "game": "1", "file": "1", "system": "2"}
    assert {key: value.text() for key, value in window.stat_values.items()} == expected
    assert window.preview_table.rowCount() == 7
    window.organize_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert window.pages.currentIndex() == 1
    assert backend.position_calls == []
    assert backend.state == original
    assert window.current_canvas.state.monitors == sample_desktop().monitors
    assert window.preview_canvas.plan is window._plan


def test_confirmation_cancel_does_not_change_layout_or_create_history(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    before = window.engine.backend.state
    window.confirm_button.click()
    assert not window.busy
    assert window.engine.backend.state == before
    assert window.engine.backend.position_calls == []
    assert window.engine.history() == []


def test_confirm_apply_then_undo_restores_coordinates_and_never_changes_paths(window, qapp):
    backend = window.engine.backend
    before = backend.state
    window.confirm_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert backend.position_calls
    assert positions(backend.state) != positions(before)
    assert metadata(backend.state) == metadata(before)
    assert window.undo_button.isEnabled()
    assert window.history_table.rowCount() == 1
    window.undo_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert positions(backend.state) == positions(before)
    assert metadata(backend.state) == metadata(before)
    assert not window.undo_button.isEnabled()


def test_save_layout_restore_preview_requires_confirmation(window, qapp, monkeypatch):
    backend = window.engine.backend
    original = positions(backend.state)
    window.save_layout_button.click()
    wait_until(qapp, lambda: not window.busy)
    manual = next(record for record in window.engine.saved_layouts() if record["kind"] == "saved")
    assert not backend.position_calls
    window.confirm_button.click()
    wait_until(qapp, lambda: not window.busy)
    arranged = positions(backend.state)
    assert arranged != original
    index = window.saved_layouts_combo.findData(manual["id"])
    assert index >= 0
    window.saved_layouts_combo.setCurrentIndex(index)
    writes = len(backend.position_calls)
    window.restore_layout_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert window._plan.kind == "restore"
    assert window.pages.currentIndex() == 1
    assert len(backend.position_calls) == writes
    assert positions(backend.state) == arranged
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    window.confirm_button.click()
    assert len(backend.position_calls) == writes
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.confirm_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert positions(backend.state) == original


def test_pinned_icons_keep_coordinates_and_category_override_is_persisted(window, qapp):
    backend = window.engine.backend
    pinned = window._plan.items[0]
    overridden = window._plan.items[1]
    original_position = pinned.source
    window.preview_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    category = window.preview_table.cellWidget(1, 2)
    category.setCurrentIndex(category.findData("game"))
    assert not window.confirm_button.isEnabled(), "Unsaved rules must invalidate confirmation"
    window.save_icon_rules_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert pinned.identity in window.engine.settings.pinned_ids
    assert window.engine.settings.category_overrides[overridden.identity] == "game"
    pinned_plan = next(item for item in window._plan.items if item.identity == pinned.identity)
    assert pinned_plan.pinned and pinned_plan.destination == original_position
    assert next(icon for icon in window._state.icons if icon.identity == overridden.identity).category == "game"
    window.confirm_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert positions(backend.state)[pinned.identity] == original_position
    reloaded = Engine(backend=backend, data_dir=window.engine.data_dir)
    assert reloaded.settings.pinned_ids == window.engine.settings.pinned_ids
    assert reloaded.settings.category_overrides == window.engine.settings.category_overrides


def test_category_override_can_be_reset_to_automatic(window, qapp):
    identity = window._plan.items[1].identity
    category = window.preview_table.cellWidget(1, 2)
    category.setCurrentIndex(category.findData("game"))
    window.save_icon_rules_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert window.engine.settings.category_overrides[identity] == "game"
    row = next(row for row in range(window.preview_table.rowCount()) if window.preview_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == identity)
    window.preview_table.cellWidget(row, 2).setCurrentIndex(0)
    window.save_icon_rules_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert identity not in window.engine.settings.category_overrides
    assert next(icon for icon in window._state.icons if icon.identity == identity).category == "software"


def test_category_order_direction_gap_game_rules_and_theme_survive_reload(window, qapp):
    order = list(reversed(CATEGORY_LABELS))
    window.category_order_edit.setPlainText("\n".join(CATEGORY_LABELS[category] for category in order))
    window.direction_combo.setCurrentIndex(window.direction_combo.findData("row"))
    window.gap_spin.setValue(3)
    window.game_rules_edit.setPlainText("steam://*\n*Editor*")
    window.theme_combo.setCurrentIndex(window.theme_combo.findData("light"))
    window.save_settings_button.click()
    wait_until(qapp, lambda: not window.busy)
    settings = Engine(backend=window.engine.backend, data_dir=window.engine.data_dir).settings
    assert settings.category_order == order
    assert settings.direction == "row"
    assert settings.category_gap == 3
    assert settings.game_rules == ["steam://*", "*Editor*"]
    assert settings.theme == "light"
    assert window.stat_values["game"].text() == "2"
    assert not window.engine.backend.position_calls


def test_invalid_category_order_is_rejected_without_persisting(window, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    original = list(window.engine.settings.category_order)
    window.category_order_edit.setPlainText("文件夹\n文件夹")
    window.save_settings_button.click()
    assert warnings and "五种分类" in warnings[0]
    assert not window.busy
    assert window.engine.settings.category_order == original


def test_auto_arrange_blocks_apply_restore_undo_but_allows_scan_and_save(window, qapp):
    backend = window.engine.backend
    window.confirm_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert window.undo_button.isEnabled()
    writes = len(backend.position_calls)
    backend.state = replace(backend.state, auto_arrange=True)
    window.scan_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert window.auto_arrange_warning.isVisible()
    assert "右键桌面" in window.auto_arrange_warning.text()
    for button in [window.confirm_button, window.organize_button, window.restore_layout_button, window.undo_button, window.history_undo_button]:
        assert not button.isEnabled()
        button.click()
    assert window.scan_button.isEnabled() and window.save_layout_button.isEnabled()
    previous = len(window.engine.saved_layouts())
    window.save_layout_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert len(window.engine.saved_layouts()) == previous + 1
    assert len(backend.position_calls) == writes


def test_shell_com_failure_is_explicit_and_never_uses_a_file_fallback(window, qapp, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
    backend = window.engine.backend
    original = backend.state

    def failed_read():
        raise OSError("IFolderView2 COM 连接失败 (0x80004005)")

    monkeypatch.setattr(backend, "read", failed_read)
    window.scan_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert errors and "IFolderView2 COM 连接失败" in errors[0]
    assert "不会改用文件目录扫描或文件移动" in errors[0]
    assert window.engine.backend is backend
    assert backend.state == original and backend.position_calls == []
    assert window.scan_button.isEnabled()
    assert not window.confirm_button.isEnabled()


def test_full_grid_keeps_scan_save_and_pin_editor_available(window, qapp):
    backend = window.engine.backend
    # Two fictional icons in one grid cell: arranging cannot safely succeed.
    backend.state = DesktopState(
        icons=(Icon("one", "一个文件", "C:\\Fiction\\one.txt", Point(0, 0)), Icon("two", "另一个文件", "C:\\Fiction\\two.txt", Point(0, 0))),
        monitors=(Monitor("small", Rect(0, 0, 100, 100)),),
        spacing=Point(100, 100),
    )
    window.scan_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert window.stat_values["icons"].text() == "2"
    assert window._plan is None
    assert window.preview_table.rowCount() == 2
    assert not window.confirm_button.isEnabled()
    assert window.scan_button.isEnabled() and window.save_layout_button.isEnabled()
    assert "足够空位" in window.plan_warnings.text()
    window.save_layout_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert any(len(record["icons"]) == 2 for record in window.engine.saved_layouts())
    assert backend.position_calls == []


def test_scan_runs_in_background_and_close_waits_for_safe_completion(window, qapp, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original_read = window.engine.backend.read
    worker_threads = []

    def slow_read():
        worker_threads.append(QThread.currentThread())
        started.set()
        assert release.wait(5), "Test did not release worker"
        return original_read()

    monkeypatch.setattr(window.engine.backend, "read", slow_read)
    window.scan_button.click()
    try:
        wait_until(qapp, started.is_set)
        assert window.busy
        assert not window.scan_button.isEnabled()
        assert not window.preview_table.isEnabled()
        assert not window.close()
    finally:
        release.set()
    wait_until(qapp, lambda: not window.busy)
    assert worker_threads and worker_threads[0] != qapp.thread()
