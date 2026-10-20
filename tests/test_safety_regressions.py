"""Independent safety checks; every user file lives in pytest's temp tree."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from desktop_pilot.core import Engine, MoveItem


@pytest.fixture
def prepared(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    source = desktop / "讲义.txt"
    source.write_bytes(b"user document")
    engine = Engine(desktop=desktop, data_dir=tmp_path / "state")
    return engine, source


def symlink_or_skip(link: Path, target: Path, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Runner does not grant symlink creation privileges")


def test_forged_preview_cannot_move_a_user_file(prepared, tmp_path):
    engine, source = prepared
    preview = engine.preview()
    forged = MoveItem(source, tmp_path / "outside.txt", preview[0].category)
    result = engine.organize([forged])
    assert result.moved_count == 0
    assert result.errors
    assert source.read_bytes() == b"user document"
    assert not forged.destination.exists()


def test_source_replaced_with_symlink_after_preview_is_preserved(prepared, tmp_path):
    engine, source = prepared
    preview = engine.preview()
    untouched = tmp_path / "outside.txt"
    untouched.write_bytes(b"external user file")
    source.unlink()
    symlink_or_skip(source, untouched)
    result = engine.organize(preview)
    assert result.moved_count == 0
    assert result.errors
    assert source.is_symlink()
    assert untouched.read_bytes() == b"external user file"
    assert not preview[0].destination.exists()


def test_category_redirected_after_preview_never_moves_outside_desktop(prepared, tmp_path):
    engine, source = prepared
    preview = engine.preview()
    preview[0].destination.parent.parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(preview[0].destination.parent, outside, directory=True)
    result = engine.organize(preview)
    assert result.moved_count == 0
    assert result.errors
    assert source.read_bytes() == b"user document"
    assert list(outside.iterdir()) == []


def test_undo_refuses_same_bytes_replaced_by_a_different_file(prepared):
    engine, source = prepared
    preview = engine.preview()
    assert engine.organize(preview).moved_count == 1
    destination = preview[0].destination
    old_stat = destination.stat()
    replacement = destination.with_suffix(".replacement")
    replacement.write_bytes(destination.read_bytes())
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    replacement.replace(destination)
    result = engine.undo()
    assert result.moved_count == 0
    assert result.errors
    assert not source.exists()
    assert destination.read_bytes() == b"user document"


def test_concurrent_engine_cannot_move_while_first_engine_holds_lock(prepared):
    engine, source = prepared
    second = Engine(desktop=engine.desktop, data_dir=engine._store.data_dir)
    preview = second.preview()
    with engine._lock():
        result = second.organize(preview)
    assert result.moved_count == 0
    assert result.errors
    assert source.read_bytes() == b"user document"
    assert not preview[0].destination.exists()


def test_ui_progress_exception_does_not_lose_move_or_undo_journal(prepared):
    engine, source = prepared
    preview = engine.preview()

    def broken_progress(current, total):
        raise RuntimeError("Window callback failed")

    moved = engine.organize(preview, progress=broken_progress)
    assert moved.moved_count == 1
    assert not moved.errors
    reopened = Engine(desktop=engine.desktop, data_dir=engine._store.data_dir)
    undone = reopened.undo(progress=broken_progress)
    assert undone.moved_count == 1
    assert not undone.errors
    assert source.read_bytes() == b"user document"


def test_source_replaced_during_journal_write_is_not_moved(prepared, tmp_path, monkeypatch):
    engine, source = prepared
    preview = engine.preview()
    save = engine._store.save_history
    swapped = False

    def save_and_simulate_external_editor(history):
        nonlocal swapped
        save(history)
        if not swapped and history[-1]["entries"][0]["status"] == "moving":
            swapped = True
            source.rename(tmp_path / "original-document.txt")
            source.write_bytes(b"new document saved while journal was syncing")

    monkeypatch.setattr(engine._store, "save_history", save_and_simulate_external_editor)
    result = engine.organize(preview)
    assert swapped
    assert result.moved_count == 0
    assert result.errors
    assert source.read_bytes() == b"new document saved while journal was syncing"
    assert not preview[0].destination.exists()
    assert (tmp_path / "original-document.txt").read_bytes() == b"user document"


def test_undo_replacement_during_journal_write_is_not_restored(prepared, tmp_path, monkeypatch):
    engine, source = prepared
    preview = engine.preview()
    assert engine.organize(preview).moved_count == 1
    destination = preview[0].destination
    save = engine._store.save_history
    swapped = False

    def save_and_simulate_external_editor(history):
        nonlocal swapped
        save(history)
        if not swapped and history[-1]["entries"][0]["status"] == "undoing":
            swapped = True
            destination.rename(tmp_path / "original-organized-file.txt")
            destination.write_bytes(b"edited during undo journal sync")

    monkeypatch.setattr(engine._store, "save_history", save_and_simulate_external_editor)
    result = engine.undo()
    assert swapped
    assert result.moved_count == 0
    assert result.errors
    assert not source.exists()
    assert destination.read_bytes() == b"edited during undo journal sync"
    assert (tmp_path / "original-organized-file.txt").read_bytes() == b"user document"
