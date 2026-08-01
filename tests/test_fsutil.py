import pytest

from mapgen.fsutil import (
    atomic_write_bytes,
    atomic_write_text,
    atomic_writer,
    best_effort_rmtree,
    ensure_dir,
    work_dir_scope,
)


def test_atomic_write_text_creates_the_file(tmp_path):
    target = tmp_path / "nested" / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_text_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello")
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_text_replaces_existing_content(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_bytes_round_trips(tmp_path):
    target = tmp_path / "out.bin"
    atomic_write_bytes(target, b"\x00\x01\x02")
    assert target.read_bytes() == b"\x00\x01\x02"


def test_atomic_writer_writes_on_clean_exit(tmp_path):
    target = tmp_path / "out.txt"
    with atomic_writer(target) as handle:
        handle.write("streamed")
    assert target.read_text(encoding="utf-8") == "streamed"


def test_atomic_writer_leaves_no_file_on_exception(tmp_path):
    target = tmp_path / "out.txt"
    with pytest.raises(RuntimeError):
        with atomic_writer(target) as handle:
            handle.write("partial")
            raise RuntimeError("boom")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_writer_preserves_existing_file_on_exception(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(RuntimeError):
        with atomic_writer(target) as handle:
            handle.write("partial")
            raise RuntimeError("boom")
    assert target.read_text(encoding="utf-8") == "original"


def test_ensure_dir_is_idempotent(tmp_path):
    target = tmp_path / "a" / "b"
    ensure_dir(target)
    ensure_dir(target)
    assert target.is_dir()


def test_best_effort_rmtree_removes_a_tree(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
    best_effort_rmtree(tmp_path / "a")
    assert not (tmp_path / "a").exists()


def test_best_effort_rmtree_ignores_a_missing_path(tmp_path):
    best_effort_rmtree(tmp_path / "absent")


def test_work_dir_scope_removes_on_success(tmp_path):
    work = tmp_path / "_work"
    with work_dir_scope(work, keep=False):
        assert work.is_dir()
        (work / "tile.osm").write_text("x", encoding="utf-8")
    assert not work.exists()


def test_work_dir_scope_keeps_when_asked(tmp_path):
    work = tmp_path / "_work"
    with work_dir_scope(work, keep=True):
        (work / "tile.osm").write_text("x", encoding="utf-8")
    assert (work / "tile.osm").exists()


def test_work_dir_scope_always_keeps_on_failure(tmp_path):
    work = tmp_path / "_work"
    with pytest.raises(RuntimeError):
        with work_dir_scope(work, keep=False):
            (work / "tile.osm").write_text("x", encoding="utf-8")
            raise RuntimeError("download failed")
    assert (work / "tile.osm").exists()
