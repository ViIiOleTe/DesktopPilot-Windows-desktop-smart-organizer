from dataclasses import replace
import random

import pytest

from desktop_pilot.layout import build_layout, build_restore_layout
from desktop_pilot.models import DesktopState, Icon, Monitor, Point, Rect, Settings


def icon(identity, name=None, x=0, y=0, **kwargs):
    return Icon(identity, name or f"{identity}.txt", name or f"{identity}.txt", Point(x, y), **kwargs)


def desktop(*icons, width=400, height=300, spacing=Point(100, 100), monitors=None, **kwargs):
    return DesktopState(tuple(icons), tuple(monitors or [Monitor("main", Rect(0, 0, width, height))]), spacing, **kwargs)


def settings(**kwargs):
    return Settings(category_gap=kwargs.pop("category_gap", 0), **kwargs)


def targets(plan):
    return {item.identity: item.destination for item in plan.items}


def assert_safe(plan):
    spacing = plan.state.spacing
    occupied = []
    for item in plan.items:
        assert any(monitor.work_area.contains(item.destination, spacing) for monitor in plan.state.monitors)
        rectangle = (item.destination.x, item.destination.y, item.destination.x + spacing.x, item.destination.y + spacing.y)
        for previous in occupied:
            assert not (rectangle[0] < previous[2] and previous[0] < rectangle[2] and rectangle[1] < previous[3] and previous[1] < rectangle[3])
        occupied.append(rectangle)


def test_default_column_direction_top_bottom_then_left_right():
    plan = build_layout(desktop(icon("c"), icon("b"), icon("a"), icon("d")), settings())
    assert targets(plan) == {"a": Point(0, 0), "b": Point(0, 100), "c": Point(0, 200), "d": Point(100, 0)}
    assert_safe(plan)


def test_row_direction_left_right_then_top_bottom():
    plan = build_layout(desktop(*(icon(str(index)) for index in range(5))), settings(direction="row"))
    assert targets(plan)["3"] == Point(300, 0)
    assert targets(plan)["4"] == Point(0, 100)
    assert_safe(plan)


def test_category_order_and_gap_are_applied():
    state = desktop(icon("file"), icon("folder", "folder", is_folder=True), icon("software", "app.lnk", is_shortcut=True))
    plan = build_layout(state, settings(category_order=["file", "folder", "software", "game", "system"], category_gap=1))
    assert targets(plan) == {"file": Point(0, 0), "folder": Point(0, 200), "software": Point(100, 100)}
    assert_safe(plan)


def test_gap_removed_with_warning_when_actual_icon_capacity_is_sufficient():
    state = desktop(icon("folder", "folder", is_folder=True), icon("file"), width=100, height=200)
    plan = build_layout(state, settings(category_gap=1))
    assert targets(plan) == {"folder": Point(0, 0), "file": Point(0, 100)}
    assert any("取消分类间隔" in warning for warning in plan.warnings)


def test_insufficient_capacity_raises_without_mutating_state():
    state = desktop(icon("a"), icon("b"), width=100, height=100)
    with pytest.raises(ValueError, match="足够空位"):
        build_layout(state, settings())
    assert all(item.position == Point(0, 0) for item in state.icons)


def test_pin_keeps_off_grid_position_and_reserves_full_footprint():
    state = desktop(icon("pin", x=50, y=50), icon("a"), icon("b"))
    plan = build_layout(state, settings(pinned_ids=["pin"]))
    assert targets(plan)["pin"] == Point(50, 50)
    assert next(item for item in plan.items if item.identity == "pin").pinned
    assert targets(plan)["a"] == Point(0, 200)
    assert targets(plan)["b"] == Point(100, 200)
    assert_safe(plan)


def test_ignored_filename_patterns_case_insensitive_pin_icons():
    state = desktop(icon("ignored", "PRIVATE Notes.TXT", x=100, y=0), icon("other"))
    plan = build_layout(state, settings(ignored_patterns=["private*"]))
    assert targets(plan)["ignored"] == Point(100, 0)
    assert next(item for item in plan.items if item.identity == "ignored").pinned
    assert_safe(plan)


def test_negative_origin_monitor_and_affiliation_are_preserved():
    monitors = [Monitor("left", Rect(-400, 0, 0, 300), dpi=144), Monitor("right", Rect(0, 50, 400, 350), dpi=192)]
    state = desktop(icon("left-file", x=-250, y=100), icon("right-file", x=200, y=200), monitors=monitors)
    plan = build_layout(state, settings())
    assert targets(plan)["left-file"] == Point(-400, 0)
    assert targets(plan)["right-file"] == Point(0, 50)
    assert_safe(plan)


def test_disconnected_monitor_position_uses_nearest_monitor():
    state = desktop(icon("old", x=2000, y=10), monitors=[Monitor("left", Rect(-400, 0, 0, 300)), Monitor("right", Rect(0, 0, 400, 300))])
    plan = build_layout(state, settings())
    assert targets(plan)["old"] == Point(0, 0)
    assert_safe(plan)


def test_layout_is_deterministic_independent_of_scan_order():
    state = desktop(icon("z", "Same.txt"), icon("a", "same.TXT"), icon("b", "B.txt"))
    first = targets(build_layout(state, settings()))
    second = targets(build_layout(replace(state, icons=tuple(reversed(state.icons))), settings()))
    assert first == second


def test_auto_arrange_is_preview_warning_not_preview_failure():
    plan = build_layout(desktop(icon("a"), auto_arrange=True), settings())
    assert any("自动排列" in warning for warning in plan.warnings)
    assert targets(plan)["a"] == Point(0, 0)


def test_empty_desktop_returns_empty_plan():
    assert build_layout(desktop(), settings()).items == ()


@pytest.mark.parametrize("spacing", [Point(0, 100), Point(100, -1), Point(True, 100)])
def test_invalid_spacing_rejected(spacing):
    with pytest.raises(ValueError, match="网格间距"):
        build_layout(desktop(icon("a"), spacing=spacing), settings())


def test_invalid_monitor_or_duplicate_identity_rejected():
    with pytest.raises(ValueError, match="标识"):
        build_layout(desktop(icon("same"), icon("same")), settings())
    with pytest.raises(ValueError, match="工作区"):
        build_layout(desktop(icon("a"), monitors=[Monitor("bad", Rect(0, 0, 0, 100))]), settings())
    with pytest.raises(ValueError, match="显示器"):
        build_layout(DesktopState((), (), Point(100, 100)), settings())


def test_pinned_outside_current_monitor_is_preserved_with_warning():
    plan = build_layout(desktop(icon("pin", x=5000, y=0), icon("a")), settings(pinned_ids=["pin"]))
    assert targets(plan)["pin"] == Point(5000, 0)
    assert any("工作区" in warning for warning in plan.warnings)


def test_restore_exact_coordinates_supports_swapping_positions():
    state = desktop(icon("a", x=0, y=0), icon("b", x=100, y=0))
    plan = build_restore_layout(state, {"a": Point(100, 0), "b": Point(0, 0)}, settings())
    assert plan.kind == "restore"
    assert targets(plan) == {"a": Point(100, 0), "b": Point(0, 0)}
    assert_safe(plan)


def test_restore_unknown_current_icons_keep_positions_and_removed_ids_skipped():
    state = desktop(icon("saved", x=300, y=0), icon("new", x=0, y=0))
    plan = build_restore_layout(state, {"saved": Point(0, 0), "removed": Point(100, 0)}, settings())
    assert targets(plan)["new"] == Point(0, 0)
    assert next(item for item in plan.items if item.identity == "new").pinned
    assert targets(plan)["saved"] == Point(0, 100)
    assert any("已不存在" in warning for warning in plan.warnings)
    assert any("不可用" in warning for warning in plan.warnings)
    assert_safe(plan)


def test_restore_current_pin_takes_priority_over_saved_position():
    state = desktop(icon("a", x=100, y=100), icon("b", x=200, y=100))
    plan = build_restore_layout(state, {"a": Point(0, 0), "b": Point(0, 100)}, settings(pinned_ids=["a"]))
    assert targets(plan)["a"] == Point(100, 100)
    assert_safe(plan)


def test_restore_saved_positions_outside_changed_monitor_remapped():
    state = desktop(icon("a", x=200, y=100), monitors=[Monitor("new", Rect(-300, -200, 100, 100), dpi=144)])
    plan = build_restore_layout(state, {"a": Point(1920, 0)}, settings())
    assert targets(plan)["a"] == Point(-300, -200)
    assert plan.warnings
    assert_safe(plan)


def test_restore_reserves_exact_positions_before_remapping_conflicts():
    state = desktop(icon("a"), icon("z"))
    plan = build_restore_layout(state, {"a": Point(5000, 0), "z": Point(0, 0)}, settings())
    assert targets(plan)["z"] == Point(0, 0)
    assert targets(plan)["a"] == Point(0, 100)
    assert_safe(plan)


def test_restore_overlapping_saved_positions_remapped_without_collision():
    plan = build_restore_layout(desktop(icon("a"), icon("b")), {"a": Point(20, 20), "b": Point(30, 30)}, settings())
    assert targets(plan)["a"] == Point(20, 20)
    assert targets(plan)["b"] == Point(0, 200)
    assert_safe(plan)


def test_restore_uses_another_monitor_if_preferred_is_full():
    state = desktop(icon("saved", x=0, y=0), icon("new", x=0, y=0), monitors=[Monitor("first", Rect(0, 0, 100, 100)), Monitor("second", Rect(100, 0, 200, 100))])
    plan = build_restore_layout(state, {"saved": Point(0, 0)}, settings())
    assert targets(plan)["saved"] == Point(100, 0)
    assert_safe(plan)


def test_restore_no_capacity_is_safe_failure():
    state = desktop(icon("saved"), icon("new"), width=100, height=100)
    with pytest.raises(ValueError, match="足够空位"):
        build_restore_layout(state, {"saved": Point(0, 0)}, settings())


@pytest.mark.parametrize("positions", [[], {"a": [0, 0]}, {"a": Point(True, 0)}, {1: Point(0, 0)}])
def test_restore_rejects_invalid_saved_coordinate_shapes(positions):
    with pytest.raises(ValueError, match="坐标无效"):
        build_restore_layout(desktop(icon("a")), positions, settings())


def test_seeded_grid_layouts_have_no_overlap_or_clipping():
    rng = random.Random(3701)
    for _ in range(30):
        count = rng.randrange(1, 40)
        sample = [icon(str(index), f"{rng.randrange(999):03}.txt", x=rng.randrange(800), y=rng.randrange(600)) for index in range(count)]
        state = desktop(*sample, width=800, height=600)
        plan = build_layout(state, settings(direction=rng.choice(["row", "column"])))
        assert_safe(plan)
