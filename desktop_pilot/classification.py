"""Classify shell metadata without opening, resolving or moving user files."""
from __future__ import annotations

from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import PureWindowsPath

from .models import CATEGORY_LABELS, CATEGORY_NAMES, DesktopState, Icon, Settings


def _normalized(value: str) -> str:
    return value.casefold().replace("/", "\\")


def classify(icon: Icon, settings: Settings) -> str:
    """Apply explicit overrides, shell identity, then conservative metadata rules.

    Shortcut targets and arguments are supplied by the shell adapter. They are
    treated as text only: no executable is launched and no target is followed.
    """
    override = settings.category_overrides.get(icon.identity)
    if override in CATEGORY_LABELS:
        return override
    if not icon.is_filesystem:
        return "system"
    if icon.is_folder:
        return "folder"
    suffix = PureWindowsPath(icon.parsing_name or icon.name).suffix.casefold()
    if not (icon.is_shortcut or suffix in {".lnk", ".url", ".exe", ".com", ".appref-ms"}):
        return "file"
    candidates = (icon.name, icon.parsing_name, icon.target, icon.arguments)
    patterns = settings.game_rules
    if any(
        fnmatchcase(_normalized(value), _normalized(pattern))
        for value in candidates if value
        for pattern in patterns
    ):
        return "game"
    return "software"


def classify_icons(state: DesktopState, settings: Settings) -> DesktopState:
    """Return a new state; preserve all icon identities and original positions."""
    return replace(state, icons=tuple(replace(icon, category=classify(icon, settings)) for icon in state.icons))
