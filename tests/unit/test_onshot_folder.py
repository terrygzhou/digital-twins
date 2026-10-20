"""Unit tests for the one-shot folder source (feature 016, FR-3).

A ``OneShotFolderSource`` conforms to the ``Source`` contract but takes
a caller-supplied ``Path`` instead of reading a config entry — the path
arrives at call time, not from config (FR-6).  Read semantics mirror
``FsSource.read`` exactly so dedup behaviour matches the ``fs`` source
(FR-7).
"""

from __future__ import annotations

import os

from digital_twins.ingest.onshot import OneShotFolderSource


def test_read_yields_nested_files(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("beta", encoding="utf-8")

    src = OneShotFolderSource(tmp_path, source_name="reports")
    items = list(src.read(since=None))

    keys = [i.key for i in items]
    assert keys == ["a.txt", "sub/b.txt"], keys


def test_metadata_carries_path_and_source(tmp_path):
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")

    src = OneShotFolderSource(tmp_path, source_name="notes")
    (item,) = list(src.read(since=None))

    assert item.key == "x.txt"
    assert item.content == "x"
    assert item.metadata["path"] == str(tmp_path / "x.txt")
    assert item.metadata["source_name"] == "notes"


def test_non_utf8_bytes_do_not_crash(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00garbled")

    src = OneShotFolderSource(tmp_path)
    (item,) = list(src.read(since=None))

    assert item.key == "bin.dat"
    assert "\ufffd" in item.content  # errors="replace"


def test_recursive_false_skips_subdirs(tmp_path):
    (tmp_path / "top.txt").write_text("top", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "child.txt").write_text("child", encoding="utf-8")

    src = OneShotFolderSource(tmp_path, recursive=False)
    keys = [i.key for i in src.read(since=None)]

    assert keys == ["top.txt"], keys


def test_prerequisites_missing_path(tmp_path):
    missing = tmp_path / "does-not-exist"
    src = OneShotFolderSource(missing)

    missing_prereqs = src.prerequisites()
    assert len(missing_prereqs) == 1
    assert "does-not-exist" in missing_prereqs[0]


def test_prerequisites_file_not_dir(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    src = OneShotFolderSource(tmp_path / "a.txt")

    assert len(src.prerequisites()) == 1


def test_source_name_defaults_to_folder(tmp_path):
    (tmp_path / "f.txt").write_text("f", encoding="utf-8")

    src = OneShotFolderSource(tmp_path)
    (item,) = list(src.read(since=None))

    assert item.metadata["source_name"] == "folder"
