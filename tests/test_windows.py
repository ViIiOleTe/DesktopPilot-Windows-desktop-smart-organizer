"""Mock the C ABI; no test invokes the real Explorer desktop."""
from dataclasses import asdict
import ctypes
import json
import sys

import pytest

from desktop_pilot.models import Point
from desktop_pilot.simulation import sample_desktop
from desktop_pilot.windows import ShellAccessError, WindowsShellBackend, _NativePoint


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class Library:
    def __init__(self):
        self.payload = {"ok": True, "state": asdict(sample_desktop())}
        self.buffers = {}
        self.freed = []
        self.reads = 0
        self.positions = []
        self.DP_ReadDesktop = Function(self.read)
        self.DP_Position = Function(self.position)
        self.DP_Free = Function(self.free)

    def response(self):
        data = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        buffer = ctypes.create_string_buffer(data)
        pointer = ctypes.addressof(buffer)
        self.buffers[pointer] = buffer
        return pointer

    def read(self):
        self.reads += 1
        return self.response()

    def position(self, ids, positions, count):
        request = {ids[i]: Point(positions[i].x, positions[i].y) for i in range(count)}
        self.positions.append(request)
        for icon in self.payload["state"]["icons"]:
            if icon["identity"] in request:
                icon["position"] = asdict(request[icon["identity"]])
        return self.response()

    def free(self, pointer):
        self.freed.append(pointer)
        self.buffers.pop(pointer)


def test_library_validation_only_loads_exports():
    library = Library()
    backend = WindowsShellBackend(library=library)
    backend.validate_library()
    assert library.reads == 0 and not library.positions
    assert ctypes.sizeof(_NativePoint) == 8
    assert library.DP_ReadDesktop.restype is ctypes.c_void_p


def test_read_utf8_shell_metadata_and_free_native_buffer():
    library = Library()
    state = WindowsShellBackend(library=library).read()
    assert len(state.icons) == 7
    assert state.icons[0].name == "项目资料"
    assert state.icons[4].is_filesystem is False
    assert state.monitors[1].dpi == 144
    assert len(library.freed) == 1 and not library.buffers


def test_native_positions_use_identity_and_signed_long_coordinates():
    library = Library()
    backend = WindowsShellBackend(library=library)
    positions = {"file:fixture:1": Point(-1200, 20), "shell:fixture:recycle": Point(96, 104)}
    state = backend.position(positions)
    assert library.positions == [positions]
    assert state.icons[0].position == Point(-1200, 20)
    assert state.icons[4].position == Point(96, 104)
    assert not library.buffers


@pytest.mark.parametrize("payload", [b"not JSON", b"\xff", {"ok": True, "state": {}}, [], {"ok": True, "state": None}])
def test_bad_native_data_fails_closed_and_frees_memory(payload):
    library = Library()
    library.payload = payload
    with pytest.raises(ShellAccessError):
        WindowsShellBackend(library=library).read()
    assert len(library.freed) == 1 and not library.buffers
    assert not library.positions


@pytest.mark.parametrize("field,value", [("auto_arrange", "false"), ("spacing", {"x": 0, "y": 10}), ("icons", [])])
def test_bad_native_state(field, value):
    library = Library()
    library.payload["state"][field] = value
    if field == "icons":
        # Empty views are valid; duplicate stable IDs are not.
        duplicate = asdict(sample_desktop().icons[0])
        library.payload["state"][field] = [duplicate, duplicate]
    with pytest.raises(ShellAccessError):
        WindowsShellBackend(library=library).read()


def test_shell_denial_has_explicit_error_and_no_file_fallback():
    library = Library()
    library.payload = {"ok": False, "error": "Explorer has no interactive desktop Shell view"}
    with pytest.raises(ShellAccessError, match="不会移动任何实际文件"):
        WindowsShellBackend(library=library).read()
    assert not library.positions


def test_auto_arrange_denial_explains_how_to_disable():
    library = Library()
    library.payload = {"ok": False, "error": "AUTO_ARRANGE_ENABLED"}
    with pytest.raises(ShellAccessError, match="取消“自动排列图标”"):
        WindowsShellBackend(library=library).read()


@pytest.mark.parametrize("point", [Point(True, 0), Point(2**31, 0), Point(0, -(2**31) - 1)])
def test_invalid_native_coordinate_never_calls_position(point):
    library = Library()
    with pytest.raises(ValueError):
        WindowsShellBackend(library=library).position({"file:fixture:1": point})
    assert not library.positions


def test_non_windows_does_not_use_a_fake_or_directory_backend(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ShellAccessError, match="不会改用文件扫描"):
        WindowsShellBackend().read()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL exports require Windows; no desktop access")
def test_real_native_dll_loads_exports_without_calling_shell():
    WindowsShellBackend().validate_library()
