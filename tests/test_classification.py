from dataclasses import replace

import pytest

from desktop_pilot.classification import CATEGORY_LABELS, classify, classify_icons
from desktop_pilot.models import DesktopState, Icon, Monitor, Point, Rect, Settings


def icon(name="example.txt", **kwargs):
    return Icon("identity", name, name, Point(10, 20), **kwargs)


@pytest.mark.parametrize("item, expected", [
    (icon("Documents", is_folder=True), "folder"),
    (icon("Recycle Bin", is_filesystem=False), "system"),
    (icon("Browser.LNK", is_shortcut=True, target=r"C:\Program Files\Browser\browser.exe"), "software"),
    (icon("Browser.url", target="https://example.com"), "software"),
    (icon("Application.exe"), "software"),
    (icon("notes.pdf"), "file"),
    (icon("photo.png"), "file"),
    (icon("archive.zip"), "file"),
    (icon("script.py"), "file"),
    (icon("Game.url", target="STEAM://rungameid/1234"), "game"),
    (icon("Game.url", target="com.epicgames.launcher://apps/example?action=launch"), "game"),
    (icon("Game.lnk", is_shortcut=True, target=r"D:\SteamLibrary\steamapps\common\Game\game.exe"), "game"),
    (icon("Game.lnk", is_shortcut=True, target="D:/SteamLibrary/steamapps/common/Game/game.exe"), "game"),
])
def test_classification_metadata_only(item, expected):
    assert classify(item, Settings()) == expected


def test_system_and_folder_rules_take_priority_over_game_patterns():
    settings = Settings(game_rules=["*"])
    assert classify(icon("Folder", is_folder=True), settings) == "folder"
    assert classify(icon("System", is_filesystem=False, is_folder=True), settings) == "system"


def test_valid_identity_override_has_highest_priority():
    settings = Settings(category_overrides={"identity": "game"})
    assert classify(icon("ordinary.txt"), settings) == "game"
    settings.category_overrides["identity"] = "nonexistent"
    assert classify(icon("ordinary.txt"), settings) == "file"


def test_custom_game_rules_case_insensitive_targets_and_arguments():
    settings = Settings(game_rules=["*MyGames*", "*--GAME-ID=*"])
    assert classify(icon("Link.lnk", target=r"C:\MyGames\game.exe"), settings) == "game"
    assert classify(icon("Link.lnk", arguments="--game-id=123"), settings) == "game"
    assert classify(icon("Link.lnk", target=r"C:\Other\program.exe"), settings) == "software"


def test_removing_game_rules_disables_metadata_game_matches():
    assert classify(icon("Game.url", target="steam://rungameid/1234"), Settings(game_rules=[])) == "software"


def test_classify_state_returns_new_objects_without_mutating_coordinates():
    original = DesktopState((icon("test.lnk"),), (Monitor("main", Rect(0, 0, 800, 600)),), Point(100, 100))
    result = classify_icons(original, Settings())
    assert result is not original
    assert result.icons[0] is not original.icons[0]
    assert original.icons[0].category == ""
    assert result.icons[0].category == "software"
    assert result.icons[0].position == original.icons[0].position
    assert result.monitors == original.monitors
    assert set(CATEGORY_LABELS) == {"folder", "software", "game", "file", "system"}
