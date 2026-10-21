"""Application entry point for both Python and the Windows executable."""

from __future__ import annotations

import os
import sys
import argparse


def main() -> int:
    """Start Qt and display a useful error if startup cannot complete."""
    parser = argparse.ArgumentParser(description="DesktopPilot 桌面图标排列控制中心")
    parser.add_argument("--smoke-test", metavar="REPORT_JSON", help="使用模拟图标验证打包，不访问真实桌面")
    options = parser.parse_args()
    if options.smoke_test:
        # This path never discovers or touches the user's actual desktop.
        from desktop_pilot.smoke import run_smoke_test

        return run_smoke_test(options.smoke_test)
    if os.name == "nt":
        # A stable identity gives the app its own Windows taskbar grouping.
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "DesktopPilot.DesktopPilot.0.2"
        )

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("DesktopPilot")
    app.setApplicationVersion("0.2.0")
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
            f"无法初始化桌面图标控制中心或本地设置。\n\n{exc}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
