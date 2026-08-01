"""Filesystem primitives. Every write in mapgen goes through here.

Partial files are worse than absent ones, so writes land on a temporary path in
the destination directory and are renamed into place only once complete.
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _temp_path(path: Path) -> Path:
    unique = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    return path.with_name(f"{path.name}.{unique}.part")


@contextmanager
def atomic_writer(path: Path, encoding: str = "utf-8") -> Iterator[IO[str]]:
    ensure_dir(path.parent)
    temp = _temp_path(path)
    try:
        with temp.open("w", encoding=encoding, newline="\n") as handle:
            yield handle
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    with atomic_writer(path, encoding=encoding) as handle:
        handle.write(text)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_dir(path.parent)
    temp = _temp_path(path)
    try:
        temp.write_bytes(data)
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def best_effort_rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


@contextmanager
def work_dir_scope(work_dir: Path, keep: bool) -> Iterator[Path]:
    """Scratch directory for a job.

    Removed on success unless keep is set. Always retained on failure, because
    it is what makes resume possible.
    """
    ensure_dir(work_dir)
    yield work_dir
    if not keep:
        best_effort_rmtree(work_dir)
