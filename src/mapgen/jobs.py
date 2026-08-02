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


class JobState:
    def __init__(
        self,
        state_path: Path,
        tiles: Sequence[str],
        source_ids: Sequence[str],
        tile_size_m: float,
        overlap_m: float,
    ) -> None:
        self.state_path = state_path
        self.source_ids = list(source_ids)
        self.tile_size_m = tile_size_m
        self.overlap_m = overlap_m
        self.tiles: dict[str, dict[str, str]] = {
            tile_id: {source_id: PENDING for source_id in source_ids}
            for tile_id in tiles
        }
        self._extra_tiles: dict[str, dict[str, str]] = {}

    @classmethod
    def load_or_create(
        cls,
        work_dir: Path,
        tiles: Sequence[str],
        source_ids: Sequence[str],
        tile_size_m: float,
        overlap_m: float,
    ) -> "JobState":
        state = cls(work_dir / STATE_FILENAME, tiles, source_ids, tile_size_m, overlap_m)
        state._merge_saved()
        return state

    def _merge_saved(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        # Validate shape: must be a dict with "tiles" as a dict.
        if not isinstance(payload, dict):
            return

        # A tile id is only (row, col): the string carries no memory of the
        # tiling it was computed under, so the same id can mean a different
        # patch of ground under a different tile_size_m or overlap_m. Saved
        # statuses from a different tiling must never be merged in, because
        # is_done would then look done for ground that was never actually
        # fetched under the current plan. A state.json saved before this
        # check existed has no "tiling" block at all, which is exactly as
        # untrustworthy as a mismatch, not a crash: both mean start fresh.
        saved_tiling = payload.get("tiling")
        if not isinstance(saved_tiling, dict):
            return
        if (
            saved_tiling.get("tile_size_m") != self.tile_size_m
            or saved_tiling.get("overlap_m") != self.overlap_m
        ):
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
            json.dumps(
                {
                    "tiling": {
                        "tile_size_m": self.tile_size_m,
                        "overlap_m": self.overlap_m,
                    },
                    "tiles": all_tiles,
                },
                indent=2,
            ),
        )
