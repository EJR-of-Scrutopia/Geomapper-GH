"""OpenStreetMap as a LayerSource.

Standard .osm XML comes from the OSM map API, which caps a request at 50000
nodes. Overpass is available as an alternative (use_overpass=True), but
only as a whole-run constructor choice, not an automatic per-tile
fallback: a tile that exceeds the OSM API's limit fails outright (see
_download_tile's "too many nodes" branch below) rather than retrying
against Overpass on its own. Task 18's interface help text was corrected
to say this plainly after review found the same false "automatic
fallback" claim here, in the one file that should have been the source
of truth for it. Both are free public services, so requests are spaced
out and back off on failure.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

import requests

from mapgen.categories import ALL_CATEGORY_IDS, osm_tag_clauses
from mapgen.fsutil import atomic_write_text
from mapgen.geo import BBox, Tile
from mapgen.merge import merge_osm_xml
from mapgen.sources.base import Estimate, ProgressSink

DEFAULT_OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
DEFAULT_OSM_API_URL = "https://api.openstreetmap.org/api/0.6/map"
USER_AGENT = "mapgen/1.0 (architectural survey tool)"

# Rough bytes per tile at 2000 m, from the South Wales reference package.
BYTES_PER_TILE_ESTIMATE = 3_500_000
SECONDS_PER_TILE_ESTIMATE = 14.0


class OsmDownloadError(RuntimeError):
    """Raised when a tile could not be downloaded."""


def build_overpass_query(
    bbox: BBox, timeout_seconds: int, categories: Sequence[str] | None = None
) -> str:
    """categories is None by default, reproducing the original,
    unfiltered node/way/relation query byte for byte: every existing
    caller of the old 2-argument signature keeps working unchanged.

    A real, narrowed selection switches to a tag-filtered form instead,
    one `nwr[...]` clause per matched category (see
    mapgen.categories.osm_tag_clauses for exactly which tags each
    category maps to, and why footpath means highway=footway rather than
    the literal id). An empty (but not None) selection produces a filter
    on a tag no real OSM data will ever carry, rather than either an
    empty union (invalid Overpass QL) or silently falling back to
    unfiltered, which would turn "select nothing" into "select
    everything", exactly the "control that appears to work and does not"
    failure this whole feature exists to avoid.
    """
    south, west = f"{bbox.south:.7f}", f"{bbox.west:.7f}"
    north, east = f"{bbox.north:.7f}", f"{bbox.east:.7f}"
    area = f"{south},{west},{north},{east}"
    clauses = osm_tag_clauses(categories)
    if clauses is None:
        body = f"  node({area});\n  way({area});\n  relation({area});\n"
    elif not clauses:
        body = f'  nwr["mapgen:none"="true"]({area});\n'
    else:
        body = "".join(f"  nwr{clause}({area});\n" for clause in clauses)
    return f"[out:xml][timeout:{timeout_seconds}];\n(\n{body});\n(._;>;);\nout meta;"


def retry_delay_seconds(
    response_headers: Mapping[str, str] | None, attempt: int
) -> float:
    if response_headers:
        raw = response_headers.get("Retry-After")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return float(min(60, 2**attempt))


class RateLimiter:
    """Spaces calls so a free public API is not hammered."""

    def __init__(
        self,
        min_interval_seconds: float,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._sleeper = sleeper
        self._clock = clock
        self._last_call: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self.min_interval_seconds - elapsed
            if remaining > 0:
                self._sleeper(remaining)
        self._last_call = self._clock()


class OsmSource:
    """OpenStreetMap LayerSource: the OSM map API by default, Overpass on request.

    endpoints_used lists the distinct endpoints contacted DURING THIS RUN, in
    first-contacted order. A tile skipped because it was already downloaded in
    an earlier run contributes nothing to this list: there is no record of
    which endpoint served it, and guessing would misinform survey.json rather
    than inform it. A fully resumed fetch, where every tile is already on
    disk, therefore leaves endpoints_used empty by design, not by omission.
    """

    id = "osm"
    display_name = "OpenStreetMap"
    licence = "Open Database License (ODbL) 1.0"
    attribution = "(c) OpenStreetMap contributors"
    requires_api_key = False

    def __init__(
        self,
        session: object | None = None,
        overpass_urls: Sequence[str] | None = None,
        osm_api_url: str = DEFAULT_OSM_API_URL,
        max_retries: int = 4,
        timeout_seconds: int = 180,
        min_interval_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
        use_overpass: bool = False,
        clock: Callable[[], float] = time.monotonic,
        categories: Sequence[str] | None = None,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.overpass_urls = list(overpass_urls or DEFAULT_OVERPASS_URLS)
        self.osm_api_url = osm_api_url
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.use_overpass = use_overpass
        self.min_interval_seconds = min_interval_seconds
        self._sleeper = sleeper
        self._clock = clock
        self._limiter = RateLimiter(min_interval_seconds, sleeper=sleeper, clock=clock)
        # None means every category: see build_overpass_query/osm_tag_
        # clauses for how this becomes a real Overpass filter, or stays
        # the original unfiltered query when nothing has been narrowed.
        # Read only by the Overpass path (self.use_overpass); the OSM map
        # API path has no equivalent filtering ability at all, see
        # filtering_caveat below.
        self.categories = list(categories) if categories is not None else None
        # Endpoints actually contacted this run, deduplicated, first-seen
        # order. See the class docstring: a skipped tile records nothing.
        self.endpoints_used: list[str] = []

    def configure(self, categories: Sequence[str] | None) -> "OsmSource":
        """Returns a fresh OsmSource sharing this instance's transport
        configuration but scoped to the given category selection, never
        mutating self. See OvertureSource.configure's own docstring (the
        same optional LayerSource extension) for why request-scoped
        reconfiguration must never mutate a registered singleton.
        """
        return OsmSource(
            session=self.session,
            overpass_urls=self.overpass_urls,
            osm_api_url=self.osm_api_url,
            max_retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            min_interval_seconds=self.min_interval_seconds,
            sleeper=self._sleeper,
            use_overpass=self.use_overpass,
            clock=self._clock,
            categories=categories,
        )

    def filtering_caveat(self, categories: Sequence[str] | None) -> str | None:
        """A plain-English caveat if the given category selection cannot
        actually be applied to what this source will fetch, else None.

        The default OSM map API path (use_overpass=False) has no
        server-side tag filtering at all: every node, way and relation in
        the extent comes back regardless of category selection. Only the
        Overpass path can filter by tag, and it is not wired to the CLI
        or web interface as a whole-run choice (see the module
        docstring). Read the same defensive way as readiness_problem:
        this is the same optional LayerSource extension, documented in
        sources/base.py, applied to a different kind of pre-flight
        problem (not "cannot run at all", but "will run, and will ignore
        part of what was asked for").
        """
        if self.use_overpass:
            return None
        if categories is not None and set(categories) < set(ALL_CATEGORY_IDS):
            return (
                "Category selection has no effect on OpenStreetMap data: the "
                "default OSM download has no server-side filtering and always "
                "returns everything in the extent, regardless of which "
                "categories are selected."
            )
        return None

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        return Estimate(
            bytes_estimate=BYTES_PER_TILE_ESTIMATE * len(tiles),
            seconds_estimate=SECONDS_PER_TILE_ESTIMATE * len(tiles),
        )

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
    ) -> list[Path]:
        paths: list[Path] = []
        for tile in tiles:
            output_path = work_dir / f"{tile.tile_id}.osm"
            if output_path.exists() and output_path.stat().st_size > 0:
                progress.emit("tile_skipped", source=self.id, tile_id=tile.tile_id)
                paths.append(output_path)
                continue
            self._download_tile(tile, output_path)
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            paths.append(output_path)
        return paths

    def _download_tile(self, tile: Tile, output_path: Path) -> None:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            overpass_candidate = self.overpass_urls[(attempt - 1) % len(self.overpass_urls)]
            endpoint = overpass_candidate if self.use_overpass else self.osm_api_url
            self._limiter.wait()
            try:
                response = self._request(tile, overpass_candidate)
            except Exception as exc:
                last_error = exc
                self._sleeper(retry_delay_seconds(None, attempt))
                continue

            if response.status_code == 200:
                atomic_write_text(output_path, response.text)
                self._record_endpoint(endpoint)
                return

            if response.status_code == 400 and "too many nodes" in response.text.lower():
                raise OsmDownloadError(
                    f"Tile {tile.tile_id} exceeded the OSM API 50000-node limit. "
                    f"Reduce the tile size, for example to 1500 or 2000 metres in "
                    f"dense urban areas."
                )

            last_error = OsmDownloadError(
                f"HTTP {response.status_code} for tile {tile.tile_id} via {endpoint}"
            )
            self._sleeper(retry_delay_seconds(response.headers, attempt))

        raise OsmDownloadError(
            f"Failed to download OSM tile {tile.tile_id} after "
            f"{self.max_retries} attempts."
        ) from last_error

    def _record_endpoint(self, endpoint: str) -> None:
        if endpoint not in self.endpoints_used:
            self.endpoints_used.append(endpoint)

    def _request(self, tile: Tile, endpoint: str):
        headers = {"User-Agent": USER_AGENT}
        if not self.use_overpass:
            return self.session.get(
                self.osm_api_url,
                params={"bbox": tile.query_bbox.to_query_string()},
                headers=headers,
                timeout=(30, self.timeout_seconds + 60),
            )
        query = build_overpass_query(tile.query_bbox, self.timeout_seconds, self.categories)
        return self.session.post(
            endpoint,
            data=query.encode("utf-8"),
            headers={**headers, "Content-Type": "text/plain; charset=utf-8"},
            timeout=(30, self.timeout_seconds + 60),
        )

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        # Named after the package stem, not a bare "all.osm": this is the
        # file Urbano needs to read, and the owner's original brief asked
        # that its name say which survey it belongs to (Task 20 finding 2).
        output = out_dir / f"{stem}.osm"
        merge_osm_xml(parts, output)
        return [output]
