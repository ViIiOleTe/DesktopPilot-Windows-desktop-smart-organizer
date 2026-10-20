from __future__ import annotations

import ctypes
import json
from pathlib import Path
from unittest.mock import Mock
import uuid

import pytest

from desktop_pilot.storage import JsonStore, Settings
from desktop_pilot import windows


def record():
    return {
        "id": "test-operation",
        "created_at": "2026-10-21T12:00:00+00:00",
        "status": "completed",
        "entries": [{"source": "/Desktop/a.txt", "destination": "/Desktop/organized/a.txt", "status": "moved"}],
    }


def test_defaults_and_settings_round_trip(tmp_path):
    store = JsonStore(tmp_path / "state")
    assert store.load_settings() == Settings()
    assert store.load_history() == []
    settings = Settings(theme="light", ignored_patterns=["*.tmp", "私人*"], keep_shortcuts=["浏览器.lnk"])
    store.save_settings(settings)
    assert JsonStore(store.data_dir).load_settings() == settings
    assert "浏览器" in (store.data_dir / "settings.json").read_text(encoding="utf-8")
    assert list(store.data_dir.glob("*.tmp")) == []


def test_mutable_settings_defaults_are_independent():
    first, second = Settings(), Settings()
    first.ignored_patterns.append("private*")
    assert second.ignored_patterns == []


def test_history_round_trip_preserves_extra_metadata(tmp_path):
    history = [record()]
    history[0]["entries"][0]["fingerprint"] = {"sha256": "abcdef"}
    store = JsonStore(tmp_path)
    store.save_history(history)
    assert JsonStore(tmp_path).load_history() == history


@pytest.mark.parametrize("contents", ["{invalid", "\ufffd not JSON", "null", "[]", '{"theme":"unknown"}', '{"ignored_patterns":"*.tmp"}', '{"unknown":true}'])
def test_corrupt_settings_are_preserved(tmp_path, contents):
    path = tmp_path / "settings.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        JsonStore(tmp_path).load_settings()
    assert path.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("history", [None, {}, [{}], [{"id": "x", "created_at": "now", "status": "done", "entries": [None]}]])
def test_invalid_history_rejected(tmp_path, history):
    path = tmp_path / "history.json"
    path.write_text(json.dumps(history), encoding="utf-8")
    with pytest.raises(ValueError):
        JsonStore(tmp_path).load_history()
    assert json.loads(path.read_text(encoding="utf-8")) == history


def test_duplicate_operation_ids_rejected(tmp_path):
    with pytest.raises(ValueError, match="重复"):
        JsonStore(tmp_path).save_history([record(), record()])
    assert not (tmp_path / "history.json").exists()


def test_failed_replace_preserves_original_and_cleans_temporary(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    store.save_settings(Settings(theme="light"))
    original = (tmp_path / "settings.json").read_bytes()
    def fail(*_):
        raise OSError("simulated disk error")
    monkeypatch.setattr("desktop_pilot.storage.os.replace", fail)
    with pytest.raises(OSError, match="disk error"):
        store.save_settings(Settings(theme="dark"))
    assert (tmp_path / "settings.json").read_bytes() == original
    assert {path.name for path in tmp_path.iterdir()} == {"settings.json"}


def test_invalid_settings_never_replace_saved_value(tmp_path):
    store = JsonStore(tmp_path)
    store.save_settings(Settings())
    with pytest.raises(ValueError):
        store.save_settings(Settings(ignored_patterns=[""]))
    assert store.load_settings() == Settings()


def make_symlink_or_skip(link: Path, target: Path, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("This runner cannot create symbolic links.")


def test_state_directory_symlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    make_symlink_or_skip(link, target, directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        JsonStore(link)
    assert list(target.iterdir()) == []


def test_state_file_symlink_rejected_without_changing_target(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    make_symlink_or_skip(state / "settings.json", target)
    store = JsonStore(state)
    with pytest.raises(ValueError, match="符号链接"):
        store.load_settings()
    with pytest.raises(ValueError, match="符号链接"):
        store.save_settings(Settings())
    assert target.read_text(encoding="utf-8") == "{}"


def test_state_parent_symlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    make_symlink_or_skip(link, target, directory=True)
    with pytest.raises(ValueError):
        JsonStore(link / "nested")
    assert list(target.iterdir()) == []


def test_non_windows_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(windows.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert windows.discover_desktop() == tmp_path / "home" / "Desktop"
    assert windows.default_data_dir() == tmp_path / "config" / "DesktopPilot"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert windows.default_data_dir() == tmp_path / "home" / ".config" / "DesktopPilot"


def test_windows_data_path(tmp_path, monkeypatch):
    monkeypatch.setattr(windows.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert windows.default_data_dir() == tmp_path / "local" / "DesktopPilot"


def mock_shell(monkeypatch, *, status=0, value=r"C:\Users\test\OneDrive\Desktop"):
    buffer = ctypes.create_unicode_buffer(value)
    shell, ole = Mock(), Mock()
    def read_path(folder_id, flags, token, output):
        assert bytes(folder_id._obj) == uuid.UUID("b4bfcc3a-db2c-424c-b029-7fe99a87c641").bytes_le
        assert flags == 0 and token is None
        output._obj.value = ctypes.addressof(buffer)
        return status
    shell.SHGetKnownFolderPath.side_effect = read_path
    def dll(name, **kwargs):
        assert kwargs == {"use_last_error": True}
        return {"shell32": shell, "ole32": ole}[name]
    monkeypatch.setattr(ctypes, "WinDLL", dll, raising=False)
    return buffer, shell, ole


def test_windows_discovery_uses_known_folder_and_frees_memory(monkeypatch):
    buffer, shell, ole = mock_shell(monkeypatch)
    monkeypatch.setattr(windows.sys, "platform", "win32")
    assert windows.discover_desktop() == Path(r"C:\Users\test\OneDrive\Desktop")
    shell.SHGetKnownFolderPath.assert_called_once()
    ole.CoTaskMemFree.assert_called_once()
    assert ole.CoTaskMemFree.call_args.args[0].value == ctypes.addressof(buffer)


def test_windows_discovery_failure_never_guesses_and_frees_memory(monkeypatch):
    _, _, ole = mock_shell(monkeypatch, status=-2147024891)
    monkeypatch.setattr(windows.sys, "platform", "win32")
    with pytest.raises(OSError, match="80070005"):
        windows.discover_desktop()
    ole.CoTaskMemFree.assert_called_once()


def test_windows_discovery_empty_result_is_error(monkeypatch):
    _, _, ole = mock_shell(monkeypatch, value="")
    with pytest.raises(OSError, match="空"):
        windows._known_folder_desktop()
    ole.CoTaskMemFree.assert_called_once()
