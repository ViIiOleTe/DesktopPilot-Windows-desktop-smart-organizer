"""Application entry point for both Python and the Windows executable."""

from __future__ import annotations

import os
import sys
import argparse


def main() -> int:
    """Start Qt and display a useful error if startup cannot complete."""
    parser = argparse.ArgumentParser(description="DesktopPilot 桌面整理助手")
    parser.add_argument("--smoke-test", metavar="REPORT_JSON", help="在临时目录运行打包自检并写入 JSON 报告")
    options = parser.parse_args()
    if options.smoke_test:
        # This path never discovers or touches the user's actual desktop.
        from desktop_pilot.smoke import run_smoke_test

        return run_smoke_test(options.smoke_test)
    if os.name == "nt":
        # A stable identity gives the app its own Windows taskbar grouping.
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "DesktopPilot.DesktopPilot.0.1"
        )

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("DesktopPilot")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("DesktopPilot")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI" if os.name == "nt" else "Noto Sans", 10))

    try:
        # Absolute imports also work when PyInstaller runs this as its script.
        from desktop_pilot.gui import MainWindow

        window = MainWindow()
    except Exception as exc:
        QMessageBox.critical(
            None,
            "DesktopPilot 无法启动",
            f"无法初始化桌面或本地设置。请确认桌面路径可访问。\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
