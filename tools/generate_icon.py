"""Regenerate the application's PNG and Windows ICO using Qt (no extra tools)."""
import os
from pathlib import Path
import struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication


def generate():
    app = QApplication.instance() or QApplication([])
    directory = Path(__file__).resolve().parents[1] / "desktop_pilot" / "assets"
    directory.mkdir(exist_ok=True)
    images = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 256, size / 256)
        painter.setPen(Qt.PenStyle.NoPen)
        gradient = QLinearGradient(0, 0, 256, 256)
        gradient.setColorAt(0, QColor("#4e91fc"))
        gradient.setColorAt(1, QColor("#2959b6"))
        painter.setBrush(gradient)
        painter.drawRoundedRect(QRectF(4, 4, 248, 248), 56, 56)
        for x, y, color in ((48, 48, "#ffffff"), (140, 48, "#c9beff"),
                            (48, 140, "#96eddb"), (140, 140, "#ffdb9e")):
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(x, y, 68, 68), 14, 14)
            painter.setBrush(QColor("#3672d1"))
            painter.drawRoundedRect(QRectF(x + 13, y + 21, 42, 5), 2, 2)
            painter.drawRoundedRect(QRectF(x + 13, y + 34, 28, 5), 2, 2)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise RuntimeError("Unable to generate icon")
        images.append((size, bytes(data)))
    (directory / "icon.png").write_bytes(images[-1][1])
    offset = 6 + 16 * len(images)
    headers = []
    for size, data in images:
        headers.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    (directory / "icon.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(images)) + b"".join(headers) + b"".join(data for _, data in images)
    )


if __name__ == "__main__":
    generate()
