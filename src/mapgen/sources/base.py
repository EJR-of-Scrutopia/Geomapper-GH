"""The LayerSource contract.

Every data source implements this protocol, and package.py orchestrates without
knowing about any of them specifically. Adding a source is one new module plus
one register call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from mapgen.geo import BBox, Tile


@dataclass(frozen=True)
class Estimate:
    bytes_estimate: int
    seconds_estimate: float


class UnknownSourceError(KeyError):
    """Raised when a source id is not in the registry."""


@runtime_checkable
class ProgressSink(Protocol):
    def emit(self, event: str, **fields: object) -> None: ...


class NullProgress:
    """Discards every event. Used by tests and non-interactive CLI runs."""

    def emit(self, event: str, **fields: object) -> None:
        return None


@runtime_checkable
class LayerSource(Protocol):
    id: str
    display_name: str
    licence: str
    attribution: str
    requires_api_key: bool

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate: ...

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
    ) -> list[Path]: ...

    def merge(self, parts: Sequence[Path], out_dir: Path) -> list[Path]: ...


_REGISTRY: dict[str, LayerSource] = {}


def register(source: LayerSource) -> None:
    _REGISTRY[source.id] = source


def get_source(source_id: str) -> LayerSource:
    try:
        return _REGISTRY[source_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise UnknownSourceError(
            f"Unknown source {source_id!r}. Available sources: {known}."
        ) from None


def available_sources() -> list[LayerSource]:
    return list(_REGISTRY.values())


def clear_registry() -> None:
    _REGISTRY.clear()
