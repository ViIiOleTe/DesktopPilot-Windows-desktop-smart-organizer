"""Packaged diagnostics using simulated icon data, never a real desktop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import traceback


def run_smoke_test(report_path: str) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    report: dict = {"status": "failed", "real_desktop_tested": False}
    try:
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication
        from desktop_pilot.core import Engine
        from desktop_pilot.gui import MainWindow
        from desktop_pilot.simulation import SimulatedBackend

        native_loaded = False
        if sys.platform == "win32":
            from desktop_pilot.windows import WindowsShellBackend
            # Only inspect DLL exports. Do NOT invoke any Shell entry point.
            WindowsShellBackend().validate_library()
            native_loaded = True
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory(prefix="DesktopPilot-icon-diagnostic-") as directory:
            backend = SimulatedBackend()
            original = backend.read()
            engine = Engine(backend=backend, data_dir=Path(directory))
            saved = engine.save_layout("模拟原始布局")
            state = engine.scan()
            plan = engine.preview(state)
            result = engine.organize(plan)
            if result.errors or result.changed_count <= 0:
                raise RuntimeError(f"模拟排列失败：{result.errors}")
            undo = engine.undo()
            if undo.errors or {i.identity: i.position for i in backend.read().icons} != {i.identity: i.position for i in original.icons}:
                raise RuntimeError(f"模拟撤销失败：{undo.errors}")
            restore = engine.restore_preview(saved["id"])
            result_restore = engine.organize(restore)
            if result_restore.errors:
                raise RuntimeError(f"模拟快照恢复失败：{result_restore.errors}")
            window = MainWindow(engine=engine)
            window.show()
            loop = QEventLoop()
            QTimer.singleShot(1200, loop.quit)
            loop.exec()
            if window.busy:
                raise RuntimeError("模拟界面后台读取未完成")
            if not window._has_snapshot:
                raise RuntimeError("模拟界面未显示图标布局")
            window.close()
            app.processEvents()
            report = {
                "status": "ok", "backend": "simulated", "real_desktop_tested": False,
                "icons": len(state.icons), "arranged": result.changed_count,
                "restored": undo.changed_count, "qt_window": True,
                "native_dll_exports_loaded": native_loaded,
            }
    except Exception:
        report["error"] = traceback.format_exc()
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "ok" else 1
