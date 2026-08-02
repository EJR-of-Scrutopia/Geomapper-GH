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


class DuplicateSourceError(ValueError):
    """Raised when two sources try to register the same id."""


@runtime_checkable
class ProgressSink(Protocol):
    def emit(self, event: str, **fields: object) -> None: ...


class NullProgress:
    """Discards every event. Used by tests and non-interactive CLI runs."""

    def emit(self, event: str, **fields: object) -> None:
        return None


@runtime_checkable
class LayerSource(Protocol):
    """A survey data source.

    Sources may expose additional read-only attributes beyond this protocol for
    survey.json provenance tracking. These are deliberately not part of the
    protocol because they are source-specific: for example, OsmSource exposes
    endpoints_used (WMS server addresses), but ElevationSource has no equivalent.
    Consumers must read such attributes with getattr(source, 'attribute_name',
    default_value) to handle sources that do not expose them.

    The same convention covers optional zero-argument methods, not only
    attributes: ElevationSource defines readiness_problem() -> str | None,
    read the same defensive way (getattr(source, 'readiness_problem', None),
    called only if present), which package.py's estimate_survey uses to
    surface a missing API key as a warning on the estimate without knowing
    "elevation" by name. Any future source with its own pre-flight
    prerequisite gets the same treatment for free by defining the same
    method; a source with nothing to check simply omits it.
    """

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
    if source.id in _REGISTRY:
        raise DuplicateSourceError(
            f"id {source.id!r} is already registered. Each source needs a unique "
            f"id; if you copied an existing source module, change its id attribute."
        )
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
