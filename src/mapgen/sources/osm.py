"""OpenStreetMap as a LayerSource.

Standard .osm XML comes from the OSM map API by default, which caps a
single request at 50000 nodes and cannot filter by tag at all. Overpass
(use_overpass=True) is the alternative: no node cap, and it can filter,
at the cost of being a shared public service with its own rate limits and
availability, distinct from the map API's.

configure() (below) is what actually chooses between them for a given
request, automatically: a genuine category restriction routes through
Overpass, since the map API has no way to honour one; no restriction
keeps the map API, today's default and the faster choice for a whole-area
pull. This is a WHOLE-RUN choice made once per request, never a per-tile
fallback: a tile that exceeds the map API's node cap fails outright (see
_download_tile's "too many nodes" branch below, which raises
NodeCapExceededError, and is scoped to the map API path specifically,
since Overpass has no such cap to hit) rather than retrying against
Overpass on its own mid-run. Task 18's interface help text was corrected
to say this plainly after review found a false "automatic fallback" claim
here, in the one file that should have been the source of truth for it;
that correction still holds; it is a different claim from configure()'s
own whole-request routing decision above it. Both endpoints are free
public services, so requests are spaced out and back off on failure.

Task 19 restored a different kind of retry the superseded script had and
mapgen initially dropped: on a NodeCapExceededError, mapgen.package's
run_survey retries the WHOLE run at the next smaller size in a fixed
ladder (2000, then 1500, then 1000 metres) before giving up. That is
orchestration, not this module's concern: this module only ever raises
the one exception type that makes the retry decision possible elsewhere,
it does not loop or know about tile sizes other than the one it was
asked to fetch. Because NodeCapExceededError is never raised on the
Overpass path (there is no cap there to except), a run that configure()
routed to Overpass for its category filter is never a candidate for this
retry either, which is the coherence a coordinator review asked to have
confirmed rather than assumed: a limit that does not apply on a given
path cannot trigger a response written for the path where it does.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

import requests

from mapgen.categories import osm_tag_clauses
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


class NodeCapExceededError(OsmDownloadError):
    """Raised specifically when a tile exceeded the OSM API's 50000-node
    limit, as distinct from any other download failure.

    A subclass, not a message-text convention: mapgen.package's whole-run
    retry-at-a-smaller-tile-size logic (Task 19) needs to tell "this tile
    was too dense, a smaller tile size might clear it" apart from "this
    tile failed for some other reason a smaller tile size would not fix"
    (a timeout, a 500, an exhausted retry budget), and matching on
    isinstance() here is exactly the structural-over-textual discipline
    the API key redaction work in mapgen.sources.elevation had to learn
    the hard way: a string match on "50000" or "too many nodes" in some
    later, differently-worded message is a future regression waiting to
    happen, in a way a type check is not.
    """


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
    """OpenStreetMap LayerSource: the OSM map API by default, Overpass when
    a category filter needs one (see configure()) or when a Python caller
    asks for it directly (use_overpass=True).

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

        Routes through Overpass automatically whenever categories is a
        genuine restriction (osm_tag_clauses(categories) is not None: a
        proper subset of every known id, including "nothing selected"),
        because the OSM map API this source uses by default has no
        server-side filtering at all, so a restricted selection can only
        ever be honoured by asking Overpass instead. A coordinator review
        caught this exact gap: earlier, configure() only ever set
        .categories, leaving .use_overpass at whatever the REGISTERED
        instance was built with, which register_default_sources() always
        constructs as False. Every category clause the Overpass query
        builder could produce was consequently unreachable through the
        CLI or browser, precisely the "control that appears to work and
        does not" failure this whole feature exists to avoid, one layer
        further in than the Overture bug this task already fixed once.

        self.use_overpass=True already set on THIS instance (a Python
        caller's own whole-run choice, unrelated to category selection,
        still available per the README) always wins and is never
        downgraded back to the map API just because this particular
        request did not restrict anything.
        """
        use_overpass = self.use_overpass or (osm_tag_clauses(categories) is not None)
        return OsmSource(
            session=self.session,
            overpass_urls=self.overpass_urls,
            osm_api_url=self.osm_api_url,
            max_retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            min_interval_seconds=self.min_interval_seconds,
            sleeper=self._sleeper,
            use_overpass=use_overpass,
            clock=self._clock,
            categories=categories,
        )

    def routing_note(self) -> str | None:
        """A plain-English statement of which OSM endpoint THIS
        configured instance will use and why, for the estimate panel and
        survey.json, or None when there is nothing notable to say (the
        default map API, doing exactly what it always has).

        Was filtering_caveat, and said the opposite: "your category
        selection does nothing here", because at the time nothing ever
        routed a real request through Overpass regardless of what was
        asked for. Once configure() (above) actually makes that routing
        decision for real, the honest thing to tell the owner is not a
        caveat about a broken control, it is a fact about which shared
        public service this run depends on: Overpass has its own rate
        limits and availability, distinct from the OSM map API's, and
        that is exactly the kind of thing "decide and state" means
        surfacing rather than leaving to be discovered as an unexplained
        slowdown or a 429.

        Read the same defensive, optional-extension way as
        readiness_problem (documented in sources/base.py): called only if
        present, zero arguments, since by the time this is checked (on an
        instance configure() already produced) self.use_overpass and
        self.categories already reflect this run's actual routing.
        """
        if not self.use_overpass:
            return None
        if self.categories is not None and osm_tag_clauses(self.categories) is not None:
            return (
                "OpenStreetMap is being fetched via Overpass instead of the "
                "default map API, to apply the selected category filter (the "
                "map API cannot filter by category). Overpass is a shared "
                "public service with its own rate limits."
            )
        return (
            "OpenStreetMap is being fetched via Overpass (set directly, not "
            "through category selection), a shared public service with its "
            "own rate limits."
        )

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

            if (
                not self.use_overpass
                and response.status_code == 400
                and "too many nodes" in response.text.lower()
            ):
                # not self.use_overpass matters: the 50000-node cap is the
                # OSM map API's own limit, and Overpass is not subject to
                # it at all. A coordinator review caught that this branch
                # used to fire regardless of which endpoint was actually
                # asked, which would have wrongly triggered mapgen.
                # package's whole-run smaller-tile retry (below) for a
                # limit a filtered, Overpass-routed run cannot hit in the
                # first place. On Overpass, any 400 falls through to the
                # generic branch beneath this one instead, an ordinary
                # retried-then-reported failure, never a node-cap retry.
                #
                # mapgen.package's run_survey catches NodeCapExceededError
                # specifically and retries the whole run at the next
                # smaller size in its own fixed ladder before this message
                # ever reaches a human; it is the message actually shown
                # only once that ladder is exhausted, so it still needs to
                # read correctly on its own at that point, not assume the
                # reader already knows a retry was attempted.
                raise NodeCapExceededError(
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
