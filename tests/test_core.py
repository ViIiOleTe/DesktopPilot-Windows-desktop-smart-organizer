"""Coordinate tests use fake shell state and a temporary application data dir."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from desktop_pilot.core import Engine
from desktop_pilot.models import DesktopState, Icon, LayoutPlan, Monitor, Point, Rect, Settings
from desktop_pilot.storage import JsonStore


class FakeBackend:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def read(self):
        return self.state

    def position(self, positions):
        self.calls.append(dict(positions))
        self.state = replace(self.state, icons=tuple(replace(icon, position=positions.get(icon.identity, icon.position)) for icon in self.state.icons))
        return self.state


@pytest.fixture
def prepared(tmp_path):
    state = DesktopState(
        icons=(
            Icon("folder-id", "学习资料", r"C:\Desktop\学习资料", Point(300, 200), is_folder=True),
            Icon("app-id", "编辑器", r"C:\Desktop\编辑器.lnk", Point(200, 100), is_shortcut=True),
            Icon("game-id", "Game", r"C:\Desktop\Game.url", Point(400, 100), is_shortcut=True, target="steam://rungameid/123"),
            Icon("file-id", "讲义.txt", r"C:\Desktop\讲义.txt", Point(100, 200)),
            Icon("recycle-id", "回收站", "::{recycle-bin}", Point(500, 200), is_filesystem=False),
        ),
        monitors=(Monitor("main", Rect(0, 0, 600, 400)),),
        spacing=Point(100, 100),
    )
    backend = FakeBackend(state)
    return Engine(backend=backend, data_dir=tmp_path / "state"), backend


def positions(state):
    return {icon.identity: icon.position for icon in state.icons}


def test_scan_classifies_without_positioning(prepared):
    engine, backend = prepared
    scan = engine.scan()
    assert [icon.category for icon in scan.icons] == ["folder", "software", "game", "file", "system"]
    assert not backend.calls
    assert positions(scan) == positions(backend.state)


def test_preview_never_changes_shell_or_writes_snapshot(prepared):
    engine, backend = prepared
    original = backend.state
    plan = engine.preview()
    assert len(plan.items) == len(original.icons)
    assert backend.state == original and not backend.calls
    assert engine.saved_layouts() == []


def test_organize_and_undo_roundtrip_coordinates_after_restart(prepared):
    engine, backend = prepared
    original = positions(backend.state)
    plan = engine.preview()
    result = engine.organize(plan)
    assert not result.errors and result.changed_count > 0
    assert positions(backend.state) == {item.identity: item.destination for item in plan.items}
    snapshot = engine.history()[0]
    assert snapshot["kind"] == "before_arrange" and snapshot["status"] == "applied" and snapshot["can_undo"]
    reopened = Engine(backend, engine.data_dir)
    undone = reopened.undo()
    assert not undone.errors and undone.changed_count > 0
    assert positions(backend.state) == original
    assert reopened.history()[1]["status"] == "undone"
    assert reopened.history()[0]["kind"] == "before_undo" and not reopened.history()[0]["can_undo"]
    assert reopened.undo().changed_count == 0


def test_save_named_layout_and_restore_via_preview(prepared):
    engine, backend = prepared
    original = positions(backend.state)
    saved = engine.save_layout("工作布局")
    assert saved["name"] == "工作布局" and not saved["can_undo"]
    assert not backend.calls
    assert not engine.organize(engine.preview()).errors
    restore = engine.restore_preview(saved["id"])
    assert restore.kind == "restore"
    assert not engine.organize(restore).errors
    assert positions(backend.state) == original


def test_snapshot_precedes_every_native_coordinate_write(prepared, monkeypatch):
    engine, backend = prepared
    original_position = backend.position
    def checked(positions):
        snapshots = json.loads((engine.data_dir / JsonStore.LAYOUTS_NAME).read_text(encoding="utf-8"))
        assert snapshots[-1]["status"] == "pending"
        assert {icon["identity"]: Point(icon["x"], icon["y"]) for icon in snapshots[-1]["icons"]} == globals()["positions"](backend.state)
        return original_position(positions)
    monkeypatch.setattr(backend, "position", checked)
    assert not engine.organize(engine.preview()).errors


def test_snapshot_failure_prevents_any_native_write(prepared, monkeypatch):
    engine, backend = prepared
    plan = engine.preview()
    def fail(_):
        raise OSError("disk full")
    monkeypatch.setattr(engine._store, "save_layouts", fail)
    result = engine.organize(plan)
    assert result.errors and result.changed_count == 0 and not backend.calls


def test_final_journal_write_failure_retains_recoverable_snapshot(prepared, monkeypatch):
    engine, backend = prepared
    original = positions(backend.state)
    save = engine._store.save_layouts
    calls = 0
    def fail_after_native(layouts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk became full")
        save(layouts)
    monkeypatch.setattr(engine._store, "save_layouts", fail_after_native)
    result = engine.organize(engine.preview())
    assert result.errors and result.changed_count > 0
    reopened = Engine(backend, engine.data_dir)
    assert reopened.history()[0]["status"] == "pending"
    assert not reopened.undo().errors
    assert positions(backend.state) == original


def test_native_partial_failure_reports_and_keeps_undo(prepared, monkeypatch):
    engine, backend = prepared
    original = positions(backend.state)
    position = backend.position
    def partial_then_fail(mapping):
        identity = next(iter(mapping))
        position({identity: mapping[identity]})
        raise OSError("Explorer stopped after one item")
    monkeypatch.setattr(backend, "position", partial_then_fail)
    result = engine.organize(engine.preview())
    assert result.errors and result.changed_count == 1
    assert engine.history()[0]["status"] == "partial"
    monkeypatch.setattr(backend, "position", position)
    assert not engine.undo().errors
    assert positions(backend.state) == original


def test_readback_must_confirm_actual_target_coordinates(prepared, monkeypatch):
    engine, backend = prepared
    monkeypatch.setattr(backend, "position", lambda mapping: backend.state)
    result = engine.organize(engine.preview())
    assert result.errors and result.changed_count == 0
    assert engine.history()[0]["status"] == "failed"


@pytest.mark.parametrize("change", ["icon", "monitor", "dpi", "spacing", "auto_arrange", "rename"])
def test_stale_preview_is_refused_without_native_writes(prepared, change):
    engine, backend = prepared
    plan = engine.preview()
    if change == "icon":
        backend.state = replace(backend.state, icons=backend.state.icons + (Icon("new-id", "new", "new", Point(0, 300)),))
    elif change == "monitor":
        backend.state = replace(backend.state, monitors=(Monitor("new", Rect(0, 0, 600, 400)),))
    elif change == "dpi":
        backend.state = replace(backend.state, monitors=(replace(backend.state.monitors[0], dpi=144),))
    elif change == "spacing":
        backend.state = replace(backend.state, spacing=Point(80, 80))
    elif change == "auto_arrange":
        backend.state = replace(backend.state, auto_arrange=True)
    else:
        backend.state = replace(backend.state, icons=(replace(backend.state.icons[0], name="Renamed"),) + backend.state.icons[1:])
    result = engine.organize(plan)
    assert result.errors and not backend.calls


def test_setting_change_after_preview_requires_new_confirmation(prepared):
    engine, backend = prepared
    plan = engine.preview()
    engine.save_settings(Settings(direction="row"))
    assert engine.organize(plan).errors and not backend.calls


def test_settings_changed_by_other_instance_invalidate_preview(prepared):
    engine, backend = prepared
    plan = engine.preview()
    second = Engine(backend, engine.data_dir)
    second.save_settings(Settings(category_gap=0))
    assert engine.organize(plan).errors and not backend.calls


def test_forged_and_tampered_plans_are_refused(prepared):
    engine, backend = prepared
    plan = engine.preview()
    forged = LayoutPlan(plan.state, plan.items, token=plan.token)
    assert engine.organize(forged).errors
    plan.items = (replace(plan.items[0], destination=Point(9999, 9999)),) + plan.items[1:]
    assert engine.organize(plan).errors and not backend.calls


def test_snapshot_io_change_is_rechecked_before_native_write(prepared, monkeypatch):
    engine, backend = prepared
    plan = engine.preview()
    save = engine._store.save_layouts
    changed = False
    def mutate_after_flush(layouts):
        nonlocal changed
        save(layouts)
        if not changed:
            backend.state = replace(backend.state, icons=(replace(backend.state.icons[0], position=Point(0, 300)),) + backend.state.icons[1:])
            changed = True
    monkeypatch.setattr(engine._store, "save_layouts", mutate_after_flush)
    assert engine.organize(plan).errors
    assert not backend.calls


def test_auto_arrange_allows_read_and_save_but_blocks_positioning(prepared):
    engine, backend = prepared
    backend.state = replace(backend.state, auto_arrange=True)
    plan = engine.preview()
    assert plan.warnings
    saved = engine.save_layout("自动排列时保存")
    assert saved["auto_arrange"]
    assert engine.organize(plan).errors and not backend.calls


def test_pinned_icons_and_ignore_patterns_stay_at_current_positions(prepared):
    engine, backend = prepared
    original = positions(backend.state)
    engine.save_settings(Settings(pinned_ids=["folder-id"], ignored_patterns=["讲义.*"]))
    plan = engine.preview()
    assert not engine.organize(plan).errors
    assert positions(backend.state)["folder-id"] == original["folder-id"]
    assert positions(backend.state)["file-id"] == original["file-id"]


def test_restore_matches_stable_id_after_rename_and_ignores_removed_icons(prepared):
    engine, backend = prepared
    original = positions(backend.state)
    snapshot = engine.save_layout()
    engine.organize(engine.preview())
    backend.state = replace(backend.state, icons=tuple(replace(icon, name="renamed", parsing_name="renamed") if icon.identity == "folder-id" else icon for icon in backend.state.icons if icon.identity != "recycle-id"))
    plan = engine.restore_preview(snapshot["id"])
    assert any("不存在" in warning for warning in plan.warnings)
    assert not engine.organize(plan).errors
    assert positions(backend.state)["folder-id"] == original["folder-id"]
    assert "recycle-id" not in positions(backend.state)


def test_restore_remaps_disconnected_monitor_and_keeps_new_icons(prepared):
    engine, backend = prepared
    snapshot = engine.save_layout()
    backend.state = replace(backend.state, monitors=(Monitor("replacement", Rect(-800, 0, 0, 600), 144),), icons=tuple(replace(icon, position=Point(-800 + index * 100, 200)) for index, icon in enumerate(backend.state.icons)) + (Icon("new-id", "new", "new", Point(-100, 400)),))
    plan = engine.restore_preview(snapshot["id"])
    assert any("显示器" in warning for warning in plan.warnings)
    assert not engine.organize(plan).errors
    assert positions(backend.state)["new-id"] == Point(-100, 400)
    assert all(backend.state.monitors[0].work_area.contains(icon.position, backend.state.spacing) for icon in backend.state.icons)


def test_operation_lock_blocks_second_instance(prepared):
    engine, backend = prepared
    second = Engine(backend, engine.data_dir)
    plan = second.preview()
    with engine._lock():
        result = second.organize(plan)
    assert result.errors and not backend.calls


def test_no_changes_is_no_native_call_or_automatic_snapshot(prepared):
    engine, backend = prepared
    assert not engine.organize(engine.preview()).errors
    count = len(engine.history())
    backend.calls.clear()
    result = engine.organize(engine.preview())
    assert not result.errors and result.changed_count == 0 and not backend.calls
    assert len(engine.history()) == count


def test_error_backend_cannot_trigger_any_file_operation(prepared, monkeypatch, tmp_path):
    engine, backend = prepared
    user_dir = tmp_path / "user-files"
    user_dir.mkdir()
    user_file = user_dir / "document.txt"
    user_file.write_bytes(b"must not change")
    backend.state = replace(backend.state, icons=tuple(replace(icon, parsing_name=str(user_file)) for icon in backend.state.icons))
    def fail(mapping):
        raise RuntimeError("native unavailable")
    monkeypatch.setattr(backend, "position", fail)
    result = engine.organize(engine.preview())
    assert result.errors
    assert list(user_dir.iterdir()) == [user_file]
    assert user_file.read_bytes() == b"must not change"


def test_backend_read_exception_is_friendly_operation_error(prepared, monkeypatch):
    engine, backend = prepared
    plan = engine.preview()
    monkeypatch.setattr(backend, "read", lambda: (_ for _ in ()).throw(RuntimeError("Explorer unavailable")))
    result = engine.organize(plan)
    assert result.errors and not backend.calls


def test_duplicate_identity_is_rejected_before_preview(prepared):
    engine, backend = prepared
    backend.state = replace(backend.state, icons=(backend.state.icons[0], backend.state.icons[0]))
    with pytest.raises(ValueError, match="重复"):
        engine.scan()
    assert not backend.calls


def test_invalid_saved_layout_id_has_clear_error(prepared):
    engine, backend = prepared
    with pytest.raises(ValueError, match="未找到"):
        engine.restore_preview("missing")
    assert engine.restore_layout("missing").errors and not backend.calls
