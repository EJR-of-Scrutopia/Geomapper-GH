"""Job state, progress events, and cancellation.

State is written after every tile so a job that dies at hour two resumes rather
than restarts. A listener that raises must never take the job down with it,
because the most likely listener is a browser tab that got closed.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Sequence

from mapgen.fsutil import atomic_write_text

STATE_FILENAME = "state.json"
PENDING = "pending"
OK = "ok"
FAILED = "failed"


class Cancelled(Exception):
    """Raised inside a job when the user has asked it to stop."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("Job cancelled at the user's request.")

    def wait(self, seconds: float) -> bool:
        """Sleep for up to `seconds`, waking the instant a stop lands.
        Returns True if it was a stop that ended the wait.

        Task 32, and the only reason this exists: a rate-limited service
        can name the period it wants to be left alone for, and package.py
        honours it before retrying. time.sleep would honour it too, and
        would also mean a Stop press did nothing at all for up to a
        minute, on the one control this project's own brief calls
        load-bearing. Waiting on the same Event the token already holds
        makes the pause exactly as interruptible as everything else here.

        A non-positive wait returns immediately, and reports whether a
        stop is already standing, which is the same answer
        is_cancelled() would give. Callers therefore need no special case
        for "no wait was asked for".
        """
        if seconds <= 0:
            return self._event.is_set()
        return self._event.wait(seconds)


class EventLog:
    """A ProgressSink that keeps history and optionally forwards live."""

    def __init__(self, listener: Callable[[dict], None] | None = None) -> None:
        self.events: list[dict] = []
        self._listener = listener
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: object) -> None:
        payload = {"event": event, **fields}
        with self._lock:
            self.events.append(payload)
        if self._listener is not None:
            try:
                self._listener(payload)
            except Exception:
                # A dead listener is a closed browser tab, not a job failure.
                pass

    def snapshot(self) -> list[dict]:
        """A copy of the history, taken under the lock.

        The HTTP handler thread reads a running job's events while the worker
        thread appends to them. Each payload dict is built fresh in emit and
        never mutated afterwards, so copying the list is enough.
        """
        with self._lock:
            return list(self.events)


class JobState:
    def __init__(
        self, state_path: Path, tiles: Sequence[str], source_ids: Sequence[str]
    ) -> None:
        self.state_path = state_path
        self.source_ids = list(source_ids)
        self.tiles: dict[str, dict[str, str]] = {
            tile_id: {source_id: PENDING for source_id in source_ids}
            for tile_id in tiles
        }
        self._extra_tiles: dict[str, dict[str, str]] = {}

    @classmethod
    def load_or_create(
        cls, work_dir: Path, tiles: Sequence[str], source_ids: Sequence[str]
    ) -> "JobState":
        state = cls(work_dir / STATE_FILENAME, tiles, source_ids)
        state._merge_saved()
        return state

    def _merge_saved(self) -> None:
        # work_dir is keyed by a fingerprint of the tiling (bbox, tile_size_m,
        # overlap_m), computed by the caller before this state.json's path is
        # ever formed. Two different tilings therefore never share a work_dir
        # to begin with, so there is nothing to compare here: any state.json
        # found at this exact path was, by construction, written under this
        # exact tiling.
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        # Validate shape: must be a dict with "tiles" as a dict.
        if not isinstance(payload, dict):
            return
        saved = payload.get("tiles", {})
        if not isinstance(saved, dict):
            return
        for tile_id, sources in self.tiles.items():
            saved_sources = saved.get(tile_id, {})
            if not isinstance(saved_sources, dict):
                continue
            for source_id in sources:
                if saved_sources.get(source_id) in (OK, FAILED):
                    sources[source_id] = saved_sources[source_id]
        # Preserve saved entries not in the current tile list to avoid data loss
        # when a job is reloaded with a narrower tile set.
        for tile_id, saved_sources in saved.items():
            if tile_id not in self.tiles and isinstance(saved_sources, dict):
                self._extra_tiles[tile_id] = saved_sources

    def mark(self, tile_id: str, source_id: str, status: str) -> None:
        self.tiles.setdefault(tile_id, {})[source_id] = status
        self.save()

    def status(self, tile_id: str, source_id: str) -> str:
        return self.tiles.get(tile_id, {}).get(source_id, PENDING)

    def is_done(self, tile_id: str, source_id: str) -> bool:
        return self.status(tile_id, source_id) == OK

    @property
    def complete(self) -> bool:
        return all(
            status == OK
            for sources in self.tiles.values()
            for status in sources.values()
        )

    def as_tile_records(self) -> list[dict[str, object]]:
        return [
            {"tile_id": tile_id, **sources}
            for tile_id, sources in sorted(self.tiles.items())
        ]

    def save(self) -> None:
        all_tiles = {**self._extra_tiles, **self.tiles}
        atomic_write_text(
            self.state_path,
            json.dumps({"tiles": all_tiles}, indent=2),
        )
