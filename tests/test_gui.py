"""Exercise the complete preview/confirm/move/undo UI on temporary desktops."""
import threading
import time

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from desktop_pilot.core import Engine
from desktop_pilot.gui import MainWindow


def wait_until(qapp, condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return
        QTest.qWait(5)
    raise AssertionError("Qt operation did not finish before timeout")


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    (desktop / "report.pdf").write_bytes(b"report contents")
    (desktop / "Browser.lnk").write_bytes(b"shortcut contents")
    (desktop / "application.exe").write_bytes(b"never move application")
    monkeypatch.setattr(QMessageBox, "information", lambda *args: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.Ok)
    widget = MainWindow(Engine(desktop, tmp_path / "state"))
    widget.show()
    wait_until(qapp, lambda: widget._has_snapshot and not widget.busy)
    yield widget
    wait_until(qapp, lambda: not widget.busy)
    widget.close()
    qapp.processEvents()


def test_scan_dashboard_and_preview_do_not_move_files(window):
    assert window.stat_values["files"].text() == "2"
    assert window.stat_values["shortcuts"].text() == "1"
    assert window.stat_values["ignored"].text() == "1"
    assert window.preview_table.rowCount() == 2
    window.organize_button.click()
    assert window.pages.currentIndex() == 1
    assert (window.engine.desktop / "report.pdf").exists()
    assert not (window.engine.desktop / "DesktopPilot整理").exists()


def test_confirmation_cancel_preserves_all_files(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    window.confirm_button.click()
    assert not window.busy
    assert (window.engine.desktop / "report.pdf").exists()
    assert window.engine.history() == []


def test_checkbox_selection_organize_and_undo(window, qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    for row in range(window.preview_table.rowCount()):
        if window.preview_table.item(row, 1).text() == "Browser.lnk":
            window.preview_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
    window.confirm_button.click()
    wait_until(qapp, lambda: not window.busy)
    desktop = window.engine.desktop
    assert not (desktop / "report.pdf").exists()
    assert (desktop / "Browser.lnk").read_bytes() == b"shortcut contents"
    assert (desktop / "application.exe").exists()
    assert window.history_table.rowCount() == 1
    assert window.undo_button.isEnabled()
    window.undo_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert (desktop / "report.pdf").read_bytes() == b"report contents"
    assert not window.undo_button.isEnabled()


def test_settings_persist_and_refresh_rules(window, qapp):
    window.theme_combo.setCurrentIndex(window.theme_combo.findData("light"))
    window.ignore_edit.setPlainText("*.pdf")
    window.keep_edit.setPlainText("Browser.lnk")
    window.save_settings_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert window.engine.settings.theme == "light"
    assert window.preview_table.rowCount() == 0
    reloaded = Engine(window.engine.desktop, window.engine._store.data_dir)
    assert reloaded.settings.ignored_patterns == ["*.pdf"]
    assert reloaded.settings.keep_shortcuts == ["Browser.lnk"]
    assert reloaded.settings.theme == "light"


def test_rescan_updates_file_statistics(window, qapp):
    (window.engine.desktop / "photo.png").write_bytes(b"photo")
    window.scan_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert window.stat_values["files"].text() == "3"
    assert window.preview_table.rowCount() == 3


def test_failed_undo_can_be_retried_without_overwriting(window, qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.confirm_button.click()
    wait_until(qapp, lambda: not window.busy)
    conflict = window.engine.desktop / "report.pdf"
    conflict.write_bytes(b"new desktop file")
    window.undo_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert conflict.read_bytes() == b"new desktop file"
    assert window.undo_button.isEnabled(), "Partial undo must remain retryable in the UI"
    # Removing only this test-created conflict allows a safe retry.
    conflict.unlink()
    window.undo_button.click()
    wait_until(qapp, lambda: not window.busy)
    assert conflict.read_bytes() == b"report contents"


def test_scan_is_background_work_and_controls_lock(window, qapp, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original_scan = window.engine.scan
    worker_threads = []

    def slow_scan():
        worker_threads.append(QThread.currentThread())
        started.set()
        assert release.wait(5), "Test did not release worker"
        return original_scan()

    monkeypatch.setattr(window.engine, "scan", slow_scan)
    window.scan_button.click()
    try:
        wait_until(qapp, started.is_set)
        assert window.busy
        assert not window.scan_button.isEnabled()
        assert not window.preview_table.isEnabled()
        assert not window.close(), "Busy window must keep its journal worker alive"
    finally:
        release.set()
    wait_until(qapp, lambda: not window.busy)
    assert worker_threads and worker_threads[0] != qapp.thread()
