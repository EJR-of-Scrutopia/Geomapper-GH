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


def build_overpass_query(bbox: BBox, timeout_seconds: int) -> str:
    south, west = f"{bbox.south:.7f}", f"{bbox.west:.7f}"
    north, east = f"{bbox.north:.7f}", f"{bbox.east:.7f}"
    area = f"{south},{west},{north},{east}"
    return (
        f"[out:xml][timeout:{timeout_seconds}];\n"
        f"(\n"
        f"  node({area});\n"
        f"  way({area});\n"
        f"  relation({area});\n"
        f");\n"
        f"(._;>;);\n"
        f"out meta;"
    )


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
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.overpass_urls = list(overpass_urls or DEFAULT_OVERPASS_URLS)
        self.osm_api_url = osm_api_url
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.use_overpass = use_overpass
        self._sleeper = sleeper
        self._limiter = RateLimiter(min_interval_seconds, sleeper=sleeper, clock=clock)
        # Endpoints actually contacted this run, deduplicated, first-seen
        # order. See the class docstring: a skipped tile records nothing.
        self.endpoints_used: list[str] = []

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
        query = build_overpass_query(tile.query_bbox, self.timeout_seconds)
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
