import threading
from pathlib import Path

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


def test_atomic_write_text_thread_safety_regression(tmp_path):
    """Regression test for thread safety: multiple threads writing to the same target.

    Verifies that concurrent writes from different threads do not corrupt the file
    or produce partial/mixed content. Each trial should result in the file containing
    exactly one of the two payloads, or raise an exception.
    """
    target = tmp_path / "concurrent.txt"
    payload_a = "thread-a-content"
    payload_b = "thread-b-very-different-content"
    results = []

    def write_a():
        try:
            atomic_write_text(target, payload_a)
            results.append(("success_a", target.read_text(encoding="utf-8")))
        except Exception as e:
            results.append(("exception_a", type(e).__name__))

    def write_b():
        try:
            atomic_write_text(target, payload_b)
            results.append(("success_b", target.read_text(encoding="utf-8")))
        except Exception as e:
            results.append(("exception_b", type(e).__name__))

    # Run 50 trials of concurrent writes
    for trial in range(50):
        target.unlink(missing_ok=True)
        results.clear()

        thread_a = threading.Thread(target=write_a)
        thread_b = threading.Thread(target=write_b)
        thread_a.start()
        thread_b.start()
        thread_a.join()
        thread_b.join()

        # Verify no exceptions and file content is valid
        success_count = sum(1 for status, _ in results if status.startswith("success"))
        exception_count = sum(1 for status, _ in results if status.startswith("exception"))

        if exception_count == 0:
            # No exceptions: file should exist and contain exactly one payload
            assert target.exists(), f"Trial {trial}: file missing after successful write"
            content = target.read_text(encoding="utf-8")
            assert content == payload_a or content == payload_b, (
                f"Trial {trial}: file contains corrupted/mixed content: {repr(content)}"
            )
        else:
            # Some exceptions OK, but file should not be corrupted if it exists
            if target.exists():
                content = target.read_text(encoding="utf-8")
                assert content == payload_a or content == payload_b, (
                    f"Trial {trial}: file corrupted after exception: {repr(content)}"
                )


def test_atomic_write_text_preserves_file_when_replace_fails(tmp_path):
    """Test that atomic_write_text preserves the original file when replace() fails.

    On Windows, Path.replace() raises PermissionError if the destination is open.
    This test verifies that when replace() fails, the original file survives untouched
    and no .part debris is left behind.
    """
    target = tmp_path / "locked.txt"
    original_content = "original data"
    target.write_text(original_content, encoding="utf-8")

    # Open the target file, then try to write to it atomically
    try:
        with open(target, "r", encoding="utf-8") as locked_handle:
            # Attempt atomic write while file is open for reading
            try:
                atomic_write_text(target, "new content")
            except PermissionError:
                # Expected on Windows when replacing an open file
                pass
            except OSError as e:
                # Accept other OS errors that may occur on different platforms
                # but verify the file is still intact
                pass

        # Verify original content is unchanged
        assert target.read_text(encoding="utf-8") == original_content

        # Verify no .part debris
        part_files = [p for p in tmp_path.iterdir() if ".part" in p.name]
        assert len(part_files) == 0, f"Found .part debris: {part_files}"
    except Exception as e:
        pytest.fail(f"Unexpected error: {e}")
