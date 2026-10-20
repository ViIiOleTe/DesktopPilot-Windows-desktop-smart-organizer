"""An executable diagnostic that operates exclusively on a temporary desktop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import traceback


def run_smoke_test(report_path: str) -> int:
    """Exercise bundled Qt, the core workflow and a real Qt event loop.

    The report is required because the windowed Windows executable has no stdout.
    All application data and files used here belong to TemporaryDirectory.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    report: dict = {"status": "failed"}
    window = None
    try:
        from PySide6.QtCore import QEventLoop, QThread, QTimer
        from PySide6.QtWidgets import QApplication
        from desktop_pilot.core import Engine
        from desktop_pilot.gui import MainWindow

        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory(prefix="DesktopPilot-smoke-") as directory:
            base = Path(directory)
            desktop = base / "desktop"
            desktop.mkdir()
            original = {"课件.pdf": b"diagnostic pdf", "Browser.lnk": b"shortcut fixture"}
            for name, content in original.items():
                (desktop / name).write_bytes(content)
            engine = Engine(desktop=desktop, data_dir=base / "state")
            scan = engine.scan()
            if len(scan.files) != 2 or scan.shortcut_count != 1:
                raise RuntimeError("扫描自检失败")
            plan = engine.preview(scan)
            organized = engine.organize(plan)
            if organized.errors or organized.moved_count != 2:
                raise RuntimeError(f"整理自检失败：{organized.errors}")
            undone = engine.undo()
            if undone.errors or undone.moved_count != 2:
                raise RuntimeError(f"撤销自检失败：{undone.errors}")
            if any((desktop / name).read_bytes() != data for name, data in original.items()):
                raise RuntimeError("撤销后的文件内容不一致")
            window = MainWindow(engine=engine)
            window.show()
            loop = QEventLoop()
            QTimer.singleShot(1500, loop.quit)
            loop.exec()
            # Wait for the initial worker before Qt destroys the window.
            for thread in window.findChildren(QThread):
                if thread.isRunning() and not thread.wait(10000):
                    raise RuntimeError("界面后台扫描超时")
            app.processEvents()
            if not window.scan_button.isEnabled():
                raise RuntimeError("界面初次扫描未完成")
            window.close()
            app.processEvents()
            report = {"status": "ok", "scanned": 2, "organized": 2, "restored": 2, "qt_window": True}
    except Exception:
        report["error"] = traceback.format_exc()
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "ok" else 1
