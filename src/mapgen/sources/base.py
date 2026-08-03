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
from mapgen.jobs import CancelToken


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
    method; a source with nothing to check simply omits it. OsmSource's
    configure() and routing_note() (Task 19: request-scoped category
    selection) are the same convention again, for a different purpose.

    api_key_config_field is the same convention for a different job (Task
    19's settings panel): a source with requires_api_key = True names the
    mapgen.config.Config field its own key is saved under (ElevationSource:
    "opentopography_api_key"), so the panel is built from this registry
    rather than one hard-coded input per key. A source with no key omits
    it, the same as any other optional attribute here.

    possible_outputs(stem) -> list[str] is the same convention once more,
    for the stale-output sweep: every package-root-relative file this
    source could EVER merge for the given stem, across all selections, not
    only the ones the current request asked for. package.py deletes any of
    these names it finds on disk that the current, complete run did not
    itself produce, which is what stops a resumed run with a narrower
    selection leaving the previous attempt's wider output beside a
    survey.json that never mentions it. The closed list is the safety
    property: a file the user dropped into the package folder can never
    match it, so the sweep cannot touch anything mapgen did not write.

    fetch()'s own `cancel` parameter (Task 22) is different in kind from
    everything above: those are all optional EXTENSIONS, read defensively
    with getattr because a source that has no opinion on them can simply
    not define them at all. fetch() is not optional, every source has one,
    only its willingness to be interrupted mid-loop varies. Adding a
    required parameter to it would break every source, real or a test
    double, that predates this task in one stroke, which is exactly the
    "control that appears to work and does not" failure this codebase
    keeps naming as the thing to avoid. So `cancel` is optional in the
    other sense instead: a keyword argument, default None, threaded
    through package.py's own call site only when
    inspect.signature(source.fetch) actually names it (see package.py's
    _fetch_accepts_cancel). A source that accepts it should call
    cancel.raise_if_cancelled() between whole units of paid-for work, the
    tile loop for OsmSource, the (tile, type) loop for OvertureSource,
    so a tile already in flight is always allowed to finish and be kept,
    never interrupted mid-request. A source that omits the parameter
    entirely, including every stub in this project's own test suite, is
    still called exactly as before and still works: it is simply not
    interruptible mid-fetch, and the next checkpoint (between sources, or
    before the bridge step, both in package.py) is what actually stops
    the run for it.
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
        cancel: CancelToken | None = None,
    ) -> list[Path]: ...

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """merge()'s output filename(s) must embed stem (PackagePaths.stem: the
        site and date, for example Barry-Waterfront_2026-08-03), not a bare
        generic name. Task 20 finding 2: OsmSource.merge used to write a fixed
        out_dir / "all.osm" regardless of which survey it belonged to, which
        gave the owner no way to tell one package's reference file from
        another's once copied elsewhere, and did not identify the file Urbano
        needs to read. Every source's merge follows the same rule now, so a
        bare, unidentifiable output name does not reappear the next time a
        source is added.
        """
        ...


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
