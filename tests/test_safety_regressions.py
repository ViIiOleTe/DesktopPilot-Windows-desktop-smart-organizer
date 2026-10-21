"""Independent coordinate safety regressions; no real desktop or user files."""

from __future__ import annotations

from dataclasses import replace

import pytest

from desktop_pilot.layout import build_layout, build_restore_layout
from desktop_pilot.models import DesktopState, Icon, Monitor, Point, Rect, Settings


def icon(identity, x=500, y=300, *, name=None):
    return Icon(identity, name or identity, f"fake:{identity}", Point(x, y))


def state(*icons, area=Rect(0, 0, 640, 480), spacing=Point(80, 80)):
    return DesktopState(tuple(icons), (Monitor("display-A", area),), spacing)


def intersects(first, second, spacing):
    return (
        first.x < second.x + spacing.x
        and second.x < first.x + spacing.x
        and first.y < second.y + spacing.y
        and second.y < first.y + spacing.y
    )


def assert_visible_and_disjoint(plan):
    moving = [item for item in plan.items if not item.pinned]
    for item in moving:
        assert any(monitor.work_area.contains(item.destination, plan.state.spacing) for monitor in plan.state.monitors)
        for other in plan.items:
            if other.identity != item.identity:
                assert not intersects(item.destination, other.destination, plan.state.spacing)


def test_duplicate_stable_identity_fails_closed():
    with pytest.raises(ValueError, match="标识"):
        build_layout(state(icon("same"), icon("same", 200, 200)), Settings())


def test_between_grid_pinned_icon_reserves_full_footprint():
    current = state(icon("pinned", 35, 25), icon("a"), icon("b"), icon("c"))
    plan = build_layout(current, Settings(pinned_ids=["pinned"]))
    pinned = next(item for item in plan.items if item.identity == "pinned")
    assert pinned.destination == Point(35, 25)
    assert_visible_and_disjoint(plan)


def test_restore_never_displaces_new_icon_occupying_saved_coordinate():
    current = state(icon("original", 400, 320), icon("new", 0, 0))
    plan = build_restore_layout(current, {"original": Point(0, 0)}, Settings())
    by_id = {item.identity: item for item in plan.items}
    assert by_id["new"].destination == Point(0, 0)
    assert by_id["original"].destination != Point(0, 0)
    assert plan.warnings
    assert_visible_and_disjoint(plan)


def test_restore_does_not_match_replaced_icon_by_display_name_or_order():
    current = state(icon("new-identity", 320, 240, name="游戏"))
    plan = build_restore_layout(current, {"deleted-identity": Point(0, 0)}, Settings())
    assert len(plan.items) == 1
    assert plan.items[0].destination == Point(320, 240)
    assert plan.items[0].pinned


def test_disconnected_negative_origin_monitor_restores_into_current_workarea():
    current = state(icon("a"), icon("b"), area=Rect(40, 0, 840, 640))
    plan = build_restore_layout(current, {"a": Point(-1200, 100), "b": Point(-1120, 100)}, Settings())
    assert len(plan.warnings) >= 2
    assert_visible_and_disjoint(plan)


def test_taskbar_workarea_offset_and_bottom_are_hard_bounds():
    current = state(*(icon(str(number)) for number in range(10)), area=Rect(56, 16, 536, 416))
    plan = build_layout(current, Settings(direction="row"))
    assert min(item.destination.x for item in plan.items) == 56
    assert min(item.destination.y for item in plan.items) == 16
    assert_visible_and_disjoint(plan)


def test_insufficient_space_is_error_instead_of_clipped_icon():
    with pytest.raises(ValueError, match="空位"):
        build_layout(state(icon("one"), area=Rect(0, 0, 70, 70)), Settings())


def test_mixed_dpi_does_not_scale_shell_coordinate_grid_again():
    current = DesktopState(
        (icon("left", -400, 0), icon("right", 900, 0)),
        (
            Monitor("left-150", Rect(-800, 0, 0, 640), 144),
            Monitor("right-200", Rect(0, 0, 1280, 960), 192),
        ),
        Point(96, 96),
    )
    plan = build_layout(current, Settings())
    by_id = {item.identity: item for item in plan.items}
    assert by_id["left"].destination == Point(-800, 0)
    assert by_id["right"].destination == Point(0, 0)
    assert_visible_and_disjoint(plan)


def test_offscreen_fixed_icon_keeps_coordinate_without_displacing_other_icons():
    current = state(icon("pinned", -1000, -1000), icon("a"), icon("b"))
    plan = build_layout(current, Settings(pinned_ids=["pinned"]))
    assert plan.items[0].destination == Point(-1000, -1000)
    assert plan.warnings
    assert_visible_and_disjoint(plan)


def test_virtual_system_folder_classified_as_system_before_folder():
    virtual = replace(icon("::{computer}"), is_folder=True, is_filesystem=False)
    plan = build_layout(state(virtual), Settings())
    assert plan.items[0].category == "system"


class FakeShell:
    def __init__(self, current):
        self.state = current
        self.calls = []
        self.after_position = None

    def read(self):
        return self.state

    def position(self, positions):
        self.calls.append(dict(positions))
        self.state = replace(
            self.state,
            icons=tuple(replace(item, position=positions.get(item.identity, item.position)) for item in self.state.icons),
        )
        if self.after_position:
            self.state = self.after_position(self.state)
        return self.state


@pytest.fixture

def fake_engine(tmp_path):
    from desktop_pilot.core import Engine

    backend = FakeShell(state(icon("first", 400, 240), icon("second", 480, 240), icon("fixed", 400, 0)))
    return Engine(backend=backend, data_dir=tmp_path / "state"), backend


def test_snapshot_save_failure_blocks_every_native_position_call(fake_engine, monkeypatch):
    engine, backend = fake_engine
    plan = engine.preview()
    original = backend.state

    def fail_save(layouts):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(engine._store, "save_layouts", fail_save)
    result = engine.organize(plan)
    assert result.errors
    assert result.changed_count == 0
    assert backend.calls == []
    assert backend.state == original


def test_readback_detects_collateral_change_of_pinned_icon(fake_engine):
    engine, backend = fake_engine
    engine.save_settings(Settings(pinned_ids=["fixed"]))
    plan = engine.preview()
    backend.after_position = lambda current: replace(
        current,
        icons=tuple(replace(item, position=Point(480, 0)) if item.identity == "fixed" else item for item in current.icons),
    )
    result = engine.organize(plan)
    assert len(backend.calls) == 1
    assert result.errors, "An unexpected change to a pinned icon must not be reported as successful"
    assert engine.history()[0]["can_undo"]


def test_state_change_during_snapshot_sync_is_not_reported_as_app_change(fake_engine, monkeypatch):
    engine, backend = fake_engine
    plan = engine.preview()
    save = engine._store.save_layouts
    external_change_done = False

    def save_then_user_drags_icon(layouts):
        nonlocal external_change_done
        save(layouts)
        if not external_change_done:
            external_change_done = True
            backend.state = replace(
                backend.state,
                icons=tuple(replace(item, position=Point(320, 160)) if item.identity == "first" else item for item in backend.state.icons),
            )

    monkeypatch.setattr(engine._store, "save_layouts", save_then_user_drags_icon)
    result = engine.organize(plan)
    assert external_change_done
    assert result.errors
    assert backend.calls == []
    assert result.changed_count == 0
    assert not engine.history()[0]["can_undo"]
