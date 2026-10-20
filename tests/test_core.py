from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_pilot import core
from desktop_pilot.core import CATEGORY_NAMES, Engine, MoveItem, OUTPUT_FOLDER, Settings, classify


@pytest.fixture
def engine(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    return Engine(desktop, tmp_path / "state")


def write(engine, name, data=b"user content"):
    path = engine.desktop / name
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(("filename", "category"), [
    ("lesson.PDF", 0), ("notes.docx", 0), ("sheet.xlsx", 0), ("slides.pptx", 0),
    ("screenshot.PNG", 1), ("photo.heic", 1), ("music.flac", 2), ("clip.mp4", 2),
    ("module.py", 3), ("script.ps1", 3), ("archive.tar.gz", 4), ("data.7z", 4),
    ("Browser.LNK", 5), ("Website.URL", 5), ("unknown.custom", 6), ("LICENSE", 6),
])
def test_categories(filename, category):
    assert classify(Path(filename)) == CATEGORY_NAMES[category]


def test_scan_statistics_and_root_only(engine):
    for name in ["report.pdf", "other.pdf", "photo.png", "app.LNK", "link.url", "no-extension"]:
        write(engine, name)
    folder = engine.desktop / "existing-folder"
    folder.mkdir()
    (folder / "nested.pdf").write_bytes(b"nested")
    scan = engine.scan()
    assert len(scan.files) == 6
    assert scan.shortcut_count == 2
    assert scan.ignored_count == 1
    assert scan.extension_counts[".pdf"] == 2
    assert scan.category_counts[CATEGORY_NAMES[5]] == 2


def test_protected_files_directories_custom_ignore_and_symlinks(engine):
    for name in ["desktop.ini", "Desktop.INI", ".hidden", "application.EXE", "installer.msi", "library.dll", "keep-private.TXT", "good.txt"]:
        write(engine, name)
    (engine.desktop / OUTPUT_FOLDER).mkdir()
    (engine.desktop / OUTPUT_FOLDER / "never.pdf").write_bytes(b"untouched")
    target = write(engine, "target.txt")
    try:
        (engine.desktop / "shortcut-link").symlink_to(target)
    except OSError:
        pass  # Windows runners can lack symbolic-link privilege.
    engine.save_settings(Settings(ignored_patterns=["KEEP-*.txt"]))
    assert {item.source.name for item in engine.scan().files} == {"good.txt", "target.txt"}
    assert (engine.desktop / OUTPUT_FOLDER / "never.pdf").read_bytes() == b"untouched"


@pytest.mark.parametrize("attribute", [0x2, 0x4, 0x400])
def test_hidden_system_reparse_stat_flags(engine, attribute):
    path = write(engine, "system.txt")
    info = path.lstat()
    fake = SimpleNamespace(st_mode=info.st_mode, st_file_attributes=attribute)
    assert engine._ignored(path, fake)


def test_trusted_onedrive_root_can_have_cloud_reparse_metadata(engine, monkeypatch):
    source = write(engine, "local-note.txt")
    original_lstat = Path.lstat
    def with_cloud_attributes(path):
        info = original_lstat(path)
        if path == engine.desktop:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", with_cloud_attributes)
    assert [file.source for file in engine.scan().files] == [source]


def test_preview_is_non_mutating_and_respects_kept_shortcuts(engine):
    keep = write(engine, "Browser.LNK")
    move = write(engine, "Website.url")
    engine.save_settings(Settings(keep_shortcuts=["browser.lnk"]))
    scan = engine.scan()
    assert scan.shortcut_count == 2
    plan = engine.preview(scan)
    assert [item.source for item in plan] == [move]
    assert keep.exists() and move.exists()
    assert not (engine.desktop / OUTPUT_FOLDER).exists()


def test_organize_undo_and_reload_preserve_bytes(engine):
    originals = {name: bytes(range(256)) * 8 for name in ["document.pdf", "image.png", "Editor.lnk", "site.url"]}
    for name, content in originals.items():
        write(engine, name, content)
    plan = engine.preview()
    notifications = []
    result = engine.organize(plan, lambda current, total: notifications.append((current, total)))
    assert result.moved_count == 4 and not result.errors
    assert notifications == [(1, 4), (2, 4), (3, 4), (4, 4)]
    for item in plan:
        assert not item.source.exists()
        assert item.destination.read_bytes() == originals[item.source.name]
    reloaded = Engine(engine.desktop, engine._store.data_dir)
    assert reloaded.history()[0]["status"] == "completed"
    undone = reloaded.undo()
    assert undone.moved_count == 4 and not undone.errors
    assert reloaded.history()[0]["status"] == "undone"
    for name, content in originals.items():
        assert (engine.desktop / name).read_bytes() == content
    assert all(not item.destination.exists() for item in plan)


def test_deselected_files_and_empty_selection(engine):
    first = write(engine, "a.txt")
    second = write(engine, "b.txt")
    plan = engine.preview()
    plan[0].selected = False
    result = engine.organize(plan)
    assert result.moved_count == 1 and not result.errors
    assert first.exists() and not second.exists()
    empty = engine.organize([])
    assert empty.moved_count == 0 and not empty.errors


def test_destination_collision_suffix_and_race_never_overwrite(engine):
    source = write(engine, "report.pdf", b"new")
    directory = engine.desktop / OUTPUT_FOLDER / CATEGORY_NAMES[0]
    directory.mkdir(parents=True)
    existing = directory / "REPORT.PDF"
    existing.write_bytes(b"old")
    plan = engine.preview()
    assert plan[0].destination.name == "report (2).pdf"
    plan[0].destination.write_bytes(b"arrived after preview")
    result = engine.organize(plan)
    assert result.moved_count == 0 and result.errors
    assert source.read_bytes() == b"new"
    assert existing.read_bytes() == b"old"
    assert plan[0].destination.read_bytes() == b"arrived after preview"


def test_source_changed_after_preview_is_not_moved(engine):
    source = write(engine, "report.txt", b"before")
    plan = engine.preview()
    source.write_bytes(b"modified since preview")
    result = engine.organize(plan)
    assert result.moved_count == 0 and result.errors
    assert source.read_bytes() == b"modified since preview"


def test_removed_source_partial_failure_does_not_lose_other_moves(engine):
    missing = write(engine, "a.txt")
    kept = write(engine, "b.txt", b"other file")
    plan = engine.preview()
    missing.unlink()
    result = engine.organize(plan)
    assert result.moved_count == 1 and len(result.errors) == 1
    assert engine.history()[0]["status"] == "partial"
    assert engine.undo().moved_count == 1
    assert kept.read_bytes() == b"other file"


def test_reject_forged_or_modified_preview_paths(engine, tmp_path):
    source = write(engine, "a.txt")
    forged = MoveItem(source, tmp_path / "stolen.txt", CATEGORY_NAMES[0])
    assert engine.organize([forged]).errors
    plan = engine.preview()
    plan[0].destination = tmp_path / "outside.txt"
    assert engine.organize(plan).errors
    assert source.exists()
    assert not (tmp_path / "outside.txt").exists()


def test_new_keep_setting_invalidates_shortcut_move(engine):
    source = write(engine, "Browser.lnk")
    plan = engine.preview()
    engine.save_settings(Settings(keep_shortcuts=["BROWSER.LNK"]))
    result = engine.organize(plan)
    assert result.errors and result.moved_count == 0
    assert source.exists()


def test_undo_does_not_overwrite_new_desktop_file(engine):
    original = write(engine, "report.txt", b"original")
    plan = engine.preview()
    assert not engine.organize(plan).errors
    original.write_bytes(b"new desktop file")
    result = engine.undo()
    assert result.moved_count == 0 and result.errors
    assert original.read_bytes() == b"new desktop file"
    assert plan[0].destination.read_bytes() == b"original"
    original.rename(engine.desktop / "new desktop file.txt")
    assert engine.undo().moved_count == 1
    assert original.read_bytes() == b"original"


def test_undo_refuses_modified_or_replaced_destination(engine):
    write(engine, "report.txt", b"original")
    plan = engine.preview()
    engine.organize(plan)
    plan[0].destination.write_bytes(b"changed")
    result = engine.undo()
    assert result.errors and result.moved_count == 0
    assert plan[0].destination.read_bytes() == b"changed"
    assert not plan[0].source.exists()


def test_undo_missing_destination_reports_error(engine):
    write(engine, "report.txt")
    plan = engine.preview()
    engine.organize(plan)
    # Simulate the user moving their file elsewhere; DesktopPilot must not guess.
    elsewhere = engine.desktop.parent / "elsewhere.txt"
    plan[0].destination.rename(elsewhere)
    result = engine.undo()
    assert result.errors and result.moved_count == 0
    assert elsewhere.read_bytes() == b"user content"


def test_multiple_batches_undo_latest_first(engine):
    a = write(engine, "a.txt")
    engine.organize(engine.preview())
    b = write(engine, "b.txt")
    engine.organize(engine.preview())
    assert engine.undo().moved_count == 1
    assert b.exists() and not a.exists()
    assert engine.undo().moved_count == 1
    assert a.exists()
    assert engine.undo().moved_count == 0


def test_failed_journal_before_move_leaves_source(engine, monkeypatch):
    source = write(engine, "a.txt")
    plan = engine.preview()
    def fail(_):
        raise OSError("disk full")
    monkeypatch.setattr(engine._store, "save_history", fail)
    result = engine.organize(plan)
    assert result.errors and result.moved_count == 0
    assert source.exists() and not plan[0].destination.exists()


def test_journal_write_failure_after_move_can_be_undone_after_restart(engine, monkeypatch):
    source = write(engine, "a.txt", b"recoverable")
    plan = engine.preview()
    original_save = engine._store.save_history
    calls = 0
    def fail_after_move(history):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("disk became full after physical move")
        original_save(history)
    monkeypatch.setattr(engine._store, "save_history", fail_after_move)
    result = engine.organize(plan)
    assert result.errors and result.moved_count == 1
    assert not source.exists()
    reloaded = Engine(engine.desktop, engine._store.data_dir)
    assert reloaded.history()[0]["entries"][0]["status"] == "moving"
    assert reloaded.undo().moved_count == 1
    assert source.read_bytes() == b"recoverable"


def test_journal_intent_precedes_filesystem_move(engine, monkeypatch):
    write(engine, "a.txt")
    original_move = core._move_no_replace
    def checked_move(source, destination, expected_identity=None):
        record = json.loads((engine._store.data_dir / "history.json").read_text(encoding="utf-8"))[-1]
        assert record["entries"][0]["status"] == "moving"
        assert len(record["entries"][0]["sha256"]) == 64
        original_move(source, destination, expected_identity)
    monkeypatch.setattr(core, "_move_no_replace", checked_move)
    assert engine.organize(engine.preview()).moved_count == 1


def test_source_replaced_during_journal_write_is_not_moved(engine, monkeypatch):
    source = write(engine, "a.txt", b"original")
    plan = engine.preview()
    original_save = engine._store.save_history
    replaced = False
    def replace_after_fsync(history):
        nonlocal replaced
        original_save(history)
        if history[-1]["entries"][0]["status"] == "moving" and not replaced:
            source.rename(engine.desktop / "original-preserved.txt")
            source.write_bytes(b"new file")
            replaced = True
    monkeypatch.setattr(engine._store, "save_history", replace_after_fsync)
    result = engine.organize(plan)
    assert result.errors and result.moved_count == 0
    assert source.read_bytes() == b"new file"
    assert not plan[0].destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-replace branch")
def test_exclusive_link_collision_race_preserves_both_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"source bytes")
    original_link = os.link
    def race_link(src, dst, **kwargs):
        destination.write_bytes(b"racing writer")
        original_link(src, dst, **kwargs)
    monkeypatch.setattr(core.os, "link", race_link)
    with pytest.raises(FileExistsError):
        core._move_no_replace(source, destination)
    assert source.read_bytes() == b"source bytes"
    assert destination.read_bytes() == b"racing writer"


def test_operation_lock_prevents_second_engine(engine):
    write(engine, "a.txt")
    second = Engine(engine.desktop, engine._store.data_dir)
    with engine._lock():
        result = second.organize(second.preview())
    assert result.moved_count == 0 and result.errors


def test_reject_output_symlink(engine, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (engine.desktop / OUTPUT_FOLDER).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links unavailable")
    source = write(engine, "a.txt")
    with pytest.raises(ValueError, match="目录不安全"):
        engine.preview()
    assert source.exists() and not list(outside.iterdir())


def test_history_path_tampering_cannot_move_outside_desktop(engine, tmp_path):
    write(engine, "a.txt")
    plan = engine.preview()
    engine.organize(plan)
    history = engine._store.load_history()
    history[-1]["entries"][0]["source"] = str(tmp_path / "outside.txt")
    engine._store.save_history(history)
    result = engine.undo()
    assert result.errors and result.moved_count == 0
    assert not (tmp_path / "outside.txt").exists()
    assert plan[0].destination.exists()


def test_missing_desktop_does_not_get_created_implicitly(tmp_path):
    missing = tmp_path / "does-not-exist"
    engine = Engine(missing, tmp_path / "state")
    with pytest.raises(FileNotFoundError):
        engine.scan()
    assert not missing.exists()
