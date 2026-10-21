from __future__ import annotations

import json
import os

import pytest

from desktop_pilot.models import Settings
from desktop_pilot.storage import JsonStore


def snapshot():
    return {
        "id": "snapshot-id", "name": "工作布局", "created_at": "2026-10-21T10:00:00+00:00",
        "kind": "saved", "status": "saved", "can_undo": False,
        "icons": [{"identity": "file-id", "name": "讲义", "x": -100, "y": 50}],
        "monitors": [{"id": "main", "work_area": {"left": -1920, "top": 0, "right": 0, "bottom": 1080}, "dpi": 144}],
        "spacing": {"x": 100, "y": 100}, "icon_size": 32, "auto_arrange": False,
    }


def test_defaults_and_unicode_roundtrip(tmp_path):
    store = JsonStore(tmp_path / "state")
    assert store.load_settings() == Settings()
    assert store.load_layouts() == []
    settings = Settings(theme="light", pinned_ids=["文件-id"], category_overrides={"文件-id": "game"})
    store.save_settings(settings)
    store.save_layouts([snapshot()])
    reopened = JsonStore(store.data_dir)
    assert reopened.load_settings() == settings
    assert reopened.load_layouts() == [snapshot()]
    assert "工作布局" in (store.data_dir / store.LAYOUTS_NAME).read_text(encoding="utf-8")


def test_legacy_file_move_history_and_settings_are_never_loaded_or_modified(tmp_path):
    legacy_settings = tmp_path / "settings.json"
    legacy_history = tmp_path / "history.json"
    legacy_settings.write_bytes(b'{"keep_shortcuts":["app.lnk"]}')
    legacy_history.write_bytes(b'[{"source":"user-file","destination":"other"}]')
    before = {path: path.read_bytes() for path in (legacy_settings, legacy_history)}
    store = JsonStore(tmp_path)
    assert store.load_settings() == Settings() and store.load_layouts() == []
    store.save_settings(Settings())
    store.save_layouts([snapshot()])
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("filename", [JsonStore.SETTINGS_NAME, JsonStore.LAYOUTS_NAME])
def test_corrupt_json_is_reported_and_preserved(tmp_path, filename):
    store = JsonStore(tmp_path)
    path = tmp_path / filename
    path.write_bytes(b"corrupted json\xff")
    with pytest.raises(ValueError, match="原文件已保留"):
        (store.load_settings if filename == store.SETTINGS_NAME else store.load_layouts)()
    assert path.read_bytes() == b"corrupted json\xff"


def test_atomic_replace_failure_preserves_previous_settings(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    store.save_settings(Settings())
    path = tmp_path / store.SETTINGS_NAME
    original = path.read_bytes()
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.save_settings(Settings(theme="light"))
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("field,value", [
    ("theme", "other"), ("theme", []), ("direction", "diagonal"), ("direction", {}),
    ("category_order", ["folder"]), ("category_order", [[], "software", "game", "file", "system"]),
    ("category_gap", -1), ("category_gap", True), ("category_gap", 11),
    ("pinned_ids", "all"), ("ignored_patterns", [None]), ("game_rules", [""]),
    ("category_overrides", {"id": "unknown"}), ("category_overrides", {"id": []}),
])
def test_invalid_settings_fail_without_writing(tmp_path, field, value):
    store = JsonStore(tmp_path)
    settings = Settings()
    setattr(settings, field, value)
    with pytest.raises(ValueError):
        store.save_settings(settings)
    assert not (tmp_path / store.SETTINGS_NAME).exists()


def test_unknown_settings_fields_are_not_silently_accepted(tmp_path):
    store = JsonStore(tmp_path)
    (tmp_path / store.SETTINGS_NAME).write_text('{"keep_shortcuts":[]}', encoding="utf-8")
    with pytest.raises(ValueError):
        store.load_settings()


@pytest.mark.parametrize("mutator", [
    lambda item: item.update(id=""),
    lambda item: item.update(icons=[{"identity": "a", "x": True, "y": 0}]),
    lambda item: item.update(icons=[{"identity": "a", "x": 1, "y": 2}] * 2),
    lambda item: item.update(monitors=[]),
    lambda item: item.update(spacing={"x": 0, "y": 10}),
    lambda item: item.update(auto_arrange="false"),
    lambda item: item.update(can_undo=1),
])
def test_invalid_snapshot_schema_is_rejected(tmp_path, mutator):
    store = JsonStore(tmp_path)
    item = snapshot()
    mutator(item)
    with pytest.raises(ValueError):
        store.save_layouts([item])
    assert not (tmp_path / store.LAYOUTS_NAME).exists()


def test_duplicate_snapshot_ids_are_rejected(tmp_path):
    store = JsonStore(tmp_path)
    with pytest.raises(ValueError, match="重复编号"):
        store.save_layouts([snapshot(), snapshot()])


def test_symlinked_config_directory_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "redirected"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic-link privilege unavailable")
    with pytest.raises(ValueError, match="符号链接"):
        JsonStore(link)
    assert not list(outside.iterdir())


def test_symlinked_snapshot_file_is_never_followed(tmp_path):
    store = JsonStore(tmp_path / "state")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"user data")
    try:
        (store.data_dir / store.LAYOUTS_NAME).symlink_to(outside)
    except OSError:
        pytest.skip("symbolic-link privilege unavailable")
    with pytest.raises(ValueError, match="符号链接"):
        store.load_layouts()
    with pytest.raises(ValueError, match="符号链接"):
        store.save_layouts([])
    assert outside.read_bytes() == b"user data"
