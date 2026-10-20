"""Native Windows coverage; every path is inside pytest's temporary directory."""
import ctypes
import subprocess
import sys

import pytest

from desktop_pilot.core import Engine


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows APIs")


def test_native_hidden_and_system_files_are_untouched(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    hidden = desktop / "hidden.txt"
    system = desktop / "system.txt"
    visible = desktop / "visible.txt"
    for path in (hidden, system, visible):
        path.write_text("temporary fixture", encoding="utf-8")
    setter = ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW
    setter.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    setter.restype = ctypes.c_int
    try:
        assert setter(str(hidden), 0x2)
        assert setter(str(system), 0x4)
        engine = Engine(desktop, tmp_path / "state")
        scan = engine.scan()
        assert [item.source for item in scan.files] == [visible]
        result = engine.organize(engine.preview(scan))
        assert result.moved_count == 1 and not result.errors
        assert hidden.read_text(encoding="utf-8") == "temporary fixture"
        assert system.read_text(encoding="utf-8") == "temporary fixture"
        assert engine.undo().moved_count == 1
    finally:
        setter(str(hidden), 0x80)
        setter(str(system), 0x80)


def test_native_junction_output_cannot_redirect_moves(tmp_path):
    desktop = tmp_path / "desktop"
    outside = tmp_path / "outside"
    desktop.mkdir()
    outside.mkdir()
    source = desktop / "report.txt"
    source.write_text("must stay", encoding="utf-8")
    junction = desktop / "DesktopPilot整理"
    creation = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
    )
    assert creation.returncode == 0, creation.stderr
    try:
        engine = Engine(desktop, tmp_path / "state")
        with pytest.raises(ValueError, match="重解析点"):
            engine.preview()
        assert source.read_text(encoding="utf-8") == "must stay"
        assert list(outside.iterdir()) == []
    finally:
        junction.rmdir()
