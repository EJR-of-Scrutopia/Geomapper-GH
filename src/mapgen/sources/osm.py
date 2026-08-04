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

A tile that does hit the cap is answered here, one tile deep, rather
than anywhere further out (Task 26, replacing Task 19's whole-run retry
at a smaller tile size, by owner ruling): fetch() splits that tile into
a 2x2 grid of quarters, downloads those in its place, and merges them
back into the single <tile_id>.osm file the rest of the pipeline already
expects, so nothing outside this module needs to know a split happened.
The tiling itself never changes, which is the whole point: tile_size_m
is hashed into naming.tiling_fingerprint, so the old ladder's smaller
retry landed in a different _work/ directory and refetched every tile
and every Overture type that had already succeeded, while this leaves
the work directory, the plan and the resume state exactly where they
were and pays only for the tile that was actually too dense.

Because NodeCapExceededError is never raised on the Overpass path
(there is no cap there to except), a run that configure() routed to
Overpass for its category filter is never a candidate for subdivision
either, which is the coherence a coordinator review asked to have
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
from mapgen.geo import BBox, Tile, extent_metres, split_tile_into_quarters
from mapgen.jobs import CancelToken
from mapgen.merge import merge_osm_xml
from mapgen.sources.base import Estimate, ProgressSink

DEFAULT_OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
DEFAULT_OSM_API_URL = "https://api.openstreetmap.org/api/0.6/map"
USER_AGENT = "mapgen/1.0 (architectural survey tool)"

# Rough bytes per tile at 2000 m, from the South Wales reference package.
#
# Left where it was in Task 25, after measuring, because there is no
# single honest value to move it to. Bytes per tile depend on what is on
# the ground rather than on the tile: six tiles of Vale of Glamorgan
# farmland at 2000 m came back at 582,178 to 1,189,116 bytes (mean
# 900,558), while six tiles of central Barry at 1000 m, a quarter of the
# area each, came back at 398,979 to 6,857,032 (mean 4,016,711). That is
# eighteen times the density per sq km between one Welsh extent and
# another twenty minutes away. 3,500,000 sits inside that spread. Moving
# it to either end would be trading one wrong number for a differently
# wrong one, and dense tiles at 2000 m cannot be measured at all because
# they exceed the node cap and fail rather than arrive.
BYTES_PER_TILE_ESTIMATE = 3_500_000

# Seconds per tile, one constant per endpoint, because the two differ by
# an order of magnitude and this source picks between them per request
# (see configure()). Measured 2026-08-04 through OsmSource.fetch itself.
#
# The single SECONDS_PER_TILE_ESTIMATE = 14.0 these replace was not
# simply wrong. It was about right for Overpass and eight times too high
# for the map API, which is the default and therefore the path almost
# every run takes. On the owner's own Barry extent at the default 2000 m
# tiling, 72 tiles, that one constant contributed 1008s to an estimate
# panel whose real total is nearer 175s. It was the largest single error
# in the panel, larger than Overture's, which is what this task was
# called to fix.
#
# Map API: three runs of six tiles, two extents, two tile sizes, all
# giving 10.17s to 10.27s for six, which is 1.69s to 1.71s per tile. The
# governing cost is not the download at all, it is this module's own
# RateLimiter: the gaps between tiles land within a few hundredths of
# min_interval_seconds, and the transfer itself disappears inside them.
# Six tiles pay five gaps, so the per-tile figure approaches the interval
# as the tile count grows, which is why 2.0 rather than the 1.7 measured
# over six.
MAP_API_SECONDS_PER_TILE = 2.0

# Overpass: four tiles of central Barry filtered to buildings, at 19.25s
# per tile (3.84s, 49.93s, 13.38s, 9.86s), and that is the run that
# WORKED. An identical second run failed outright after 217s, on a tile
# that had exhausted all four attempts against both endpoints, having
# already spent 59s on the first tile. HTTP 504 from Overpass is
# ordinary, not exceptional.
#
# So this is a floor and is documented as one. An Overpass run can cost
# far more than this says, or fail; what it cannot do is cost less. The
# estimate panel is not the place to model a shared public service's bad
# day, but it should not tell the owner that a category-filtered run
# costs what an unfiltered one costs, because it costs about ten times
# as much per tile.
OVERPASS_SECONDS_PER_TILE = 19.0

# How many times a tile may be cut into quarters before mapgen gives up
# and says so (see OsmSource.fetch). Two, so a 2000 m tile becomes at
# most sixteen 500 m pieces, finer than the old whole-run ladder's 1000 m
# floor ever reached, and a 500 m tile (the interface's own minimum)
# becomes 125 m pieces.
#
# Not user-configurable, and two rather than three or more, because each
# level costs four times the requests for the same ground: a tile that
# needs both levels everywhere is 1 + 4 + 16 = 21 rate-limited requests
# where the estimate quoted one, about 42 seconds. A third level would be
# 85. Ground that is still over 50000 nodes in a 500 m square is over
# 200000 nodes per sq km, denser than anything in the reference material
# by an order of magnitude, and the honest answer there is to tell the
# owner rather than to keep quartering in silence: they can draw a
# smaller extent knowing why, which no amount of further splitting
# decides for them.
MAX_SUBDIVISION_DEPTH = 2

# Where the quarters live: a subdirectory of the source's own work
# directory, kept rather than deleted so an interrupted run resumes INTO
# a subdivision instead of restarting it.
#
# The leading underscore is load-bearing. package.py's
# _existing_output_files skips any subdirectory of a source's work
# directory whose name begins with one, so quarter files are never
# offered to merge() and never counted as tile output; see its own
# docstring for why that rule, and not a change to _TILE_ID_SHAPE, is
# what keeps them out.
#
# This name plus the deepest quarter's own suffix is the longest path
# OSM can produce: 15 characters past the raw/osm/rNN_cNN.osm that
# naming.check_path_length actually measures, so a job admitted at that
# guard's 240 character limit KEEPS a quarter file 255 characters long,
# inside Windows' 260.
#
# The path mapgen CREATES is longer than either, and not by a margin the
# guard covers. Every write goes through fsutil.atomic_writer, which
# writes to .{pid}.{thread ident}.{8 hex}.part first, 26 characters as
# measured here, so the same job creates 266 characters for an ordinary
# tile and 281 for the deepest quarter. That predates subdivision,
# applies to every OSM tile mapgen has ever written, and works only
# because long paths are enabled on this machine. See test_naming.py's
# own two tests, which say those two things separately because they have
# different answers.
SPLIT_DIR_NAME = "_split"


class OsmDownloadError(RuntimeError):
    """Raised when a tile could not be downloaded."""


class NodeCapExceededError(OsmDownloadError):
    """Raised specifically when a tile exceeded the OSM API's 50000-node
    limit, as distinct from any other download failure.

    A subclass, not a message-text convention: the handler needs to tell
    "this tile was too dense, a smaller piece of it might clear it" apart
    from "this tile failed for some other reason a smaller piece would
    not fix" (a timeout, a 500, an exhausted retry budget), and matching
    on isinstance() here is exactly the structural-over-textual discipline
    the API key redaction work in mapgen.sources.elevation had to learn
    the hard way: a string match on "50000" or "too many nodes" in some
    later, differently-worded message is a future regression waiting to
    happen, in a way a type check is not.

    The same argument applies to WHICH tile was too dense, which is why
    tile is a required attribute and not merely a name inside the
    sentence (Task 26): a handler that has to split the offending tile
    must not have to parse a sentence to learn what to split, and there
    is no such thing as a node-cap failure without a tile to attach it
    to, so there is no default to fall back on.
    """

    def __init__(self, message: str, tile: Tile) -> None:
        super().__init__(message)
        self.tile = tile


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
        # routing_note below.
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
        return type(self)(
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
        """Per tile, unlike OvertureSource: this source really is tiled,
        really does make one request per tile, and really does pay for
        each of them in turn.

        Which endpoint this run will use is already decided by the time
        this is called. package.py's estimate_survey estimates through
        _configured_sources, so self.use_overpass here is the same value
        the fetch would run under, exactly as routing_note() relies on.
        Reading it is what stops a category-filtered run, which costs
        about ten times as much per tile, being quoted the unfiltered
        price.

        Still NOT modelling what a tile over the node cap costs, but the
        size of what is unmodelled has changed, and it is now bounded.
        Task 25 recorded the old whole-run retry ladder here as a real and
        unquoted cost: a dense extent requested at 2000 m failed part way
        through, and package.py restarted the entire run at 1500 m and
        then at 1000 m, four times the tiles, having already paid for the
        tiles it got, so the true cost of one dense tile was another
        whole run and then another.

        Task 26 replaced that with subdivision inside fetch(), and a
        subdivided tile now costs at most its own quarters. The worst
        case is one tile needing both levels of splitting everywhere:
        1 failed request + 4 quarters + 16 sixteenths = 21 rate-limited
        requests, about 42 seconds at MAP_API_SECONDS_PER_TILE, in place
        of the 2 seconds quoted for that one tile. Nothing else in the run
        is affected, nothing already downloaded is refetched, and every
        other tile costs exactly what it says here.

        Still not added to the model, for the same reason as before: how
        many tiles subdivide depends on how dense the ground is, which is
        precisely what cannot be known before downloading it. Assuming
        none is right for almost every extent; assuming the worst would
        overstate a rural survey twentyfold. The difference is that the
        error is now at most 40 seconds per dense tile rather than an
        unbounded multiple of the whole run, which is why this is a
        footnote rather than the largest unquoted number in the panel.
        """
        seconds_per_tile = (
            OVERPASS_SECONDS_PER_TILE if self.use_overpass else MAP_API_SECONDS_PER_TILE
        )
        return Estimate(
            bytes_estimate=BYTES_PER_TILE_ESTIMATE * len(tiles),
            seconds_estimate=seconds_per_tile * len(tiles),
        )

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        paths: list[Path] = []
        for tile in tiles:
            # Checked at the top of the loop, before this tile's own
            # request starts, not after: a tile already in flight is paid
            # for and always allowed to finish (see the LayerSource
            # protocol's own docstring on this). Cancelled propagates
            # straight out of fetch(); package.py's own per-source loop is
            # what catches it and merges whatever tiles already landed.
            if cancel is not None:
                cancel.raise_if_cancelled()
            output_path = work_dir / f"{tile.tile_id}.osm"
            if output_path.exists() and output_path.stat().st_size > 0:
                progress.emit("tile_skipped", source=self.id, tile_id=tile.tile_id)
                paths.append(output_path)
                continue
            self._fetch_tile(tile, output_path, work_dir, progress, cancel, depth=0)
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            paths.append(output_path)
        return paths

    def _fetch_tile(
        self,
        tile: Tile,
        output_path: Path,
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None,
        depth: int,
    ) -> None:
        """Put this tile's data at output_path, splitting it into quarters
        if it turns out to be too dense to download in one request.

        Recursive, and the recursion is the whole mechanism: a quarter is
        just a smaller tile, so a quarter that is itself over the cap is
        handled by the same code that handled its parent, up to
        MAX_SUBDIVISION_DEPTH. Depth is counted from the tile of the plan,
        so depth 0 is the tile package.py asked for.

        The quarters are fetched one after another, deliberately not
        concurrently, even though Task 24 made Overture's types download
        together. The two situations are opposites: Overture is one
        request per type against a CDN with no rate limit, while this is
        one request per quarter against a free public API that this module
        already spaces out on purpose (see RateLimiter), and the moment to
        start making concurrent requests is not immediately after that
        service told us the last request was too big.

        Cancellation lands here the same way it lands in fetch(): checked
        before a quarter's own request starts, never during one. A stop
        that arrives mid-subdivision therefore propagates out of fetch()
        BEFORE the recombine below runs, so no <tile_id>.osm is written
        from a half-fetched set of quarters and package.py records the
        tile as pending, not ok and not failed. The quarters that did
        land stay on disk, and the next run resumes into the subdivision
        rather than starting it again.

        Resuming an unfinished subdivision does re-ask for the whole tile
        first, and gets told it is too dense a second time, because that
        is how this function learns the tile needs splitting at all. One
        request per ANCESTOR LEVEL of the piece that was interrupted, so
        up to two at MAX_SUBDIVISION_DEPTH = 2: the plan tile is re-asked
        and told it is too dense, and so is the quarter that was mid-split
        when the stop landed. About two seconds each (review finding N9;
        this said "one request, about two seconds, paid once per
        interrupted tile", which is the depth-1 case and not the cap).
        The alternative is a note on disk recording that a tile was split
        before, which is a second source of truth about the same fact and
        would go stale the moment the ground did. Everything expensive,
        the quarters themselves, is still skipped.
        """
        try:
            self._download_tile(tile, output_path)
            return
        except NodeCapExceededError as too_dense:
            if depth >= MAX_SUBDIVISION_DEPTH:
                width_m, height_m = extent_metres(tile.query_bbox)
                raise NodeCapExceededError(
                    f"Tile {tile.tile_id} is still over the OSM API's 50000-node "
                    f"limit after being split into quarters {depth} times. This "
                    f"piece is about {width_m:.0f} by {height_m:.0f} metres and "
                    f"mapgen does not split further. Draw a smaller extent, or "
                    f"survey this area in separate pieces.",
                    tile=tile,
                ) from too_dense

        quarters = split_tile_into_quarters(tile)
        # Emitted before the quarters are fetched, not after: the point of
        # the event is that a run which has just gone quiet on one tile
        # for four times as long says why while it is happening.
        progress.emit(
            "tile_subdivided",
            source=self.id,
            tile_id=tile.tile_id,
            pieces=len(quarters),
            depth=depth + 1,
        )

        split_dir = work_dir / SPLIT_DIR_NAME
        quarter_paths: list[Path] = []
        for quarter in quarters:
            quarter_path = split_dir / f"{quarter.tile_id}.osm"
            quarter_paths.append(quarter_path)
            if quarter_path.exists() and quarter_path.stat().st_size > 0:
                # Already downloaded by an earlier run, or by an earlier
                # pass of this one. A quarter's file is only ever written
                # whole, either by a successful download or by the
                # recombine below, so its presence means this quarter's
                # own ground is complete, however many levels down it was
                # actually fetched.
                progress.emit("tile_skipped", source=self.id, tile_id=quarter.tile_id)
                continue
            if cancel is not None:
                cancel.raise_if_cancelled()
            self._fetch_tile(
                quarter, quarter_path, work_dir, progress, cancel, depth=depth + 1
            )

        # The existing merge, not a second XML combiner: mapgen.merge is
        # what OsmSource.merge already uses to fold overlapping tiles into
        # one file, keyed on element type and id, and two ways to combine
        # .osm files is how they drift apart. It also means the duplicate
        # elements along the quarters' shared seams are removed by exactly
        # the code that already removes them along tile seams.
        merge_osm_xml(quarter_paths, output_path)

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
                # asked, which would have wrongly triggered a response to
                # a limit a filtered, Overpass-routed run cannot hit in
                # the first place. On Overpass, any 400 falls through to
                # the generic branch beneath this one instead, an ordinary
                # retried-then-reported failure, never a subdivision.
                #
                # A plain statement of fact, with no advice in it: this
                # message is a signal to _fetch_tile, which answers it by
                # splitting the tile, and it reaches a human only if that
                # is somehow raised outside fetch(). The sentence the
                # owner actually reads when subdivision has run out of
                # room is composed in _fetch_tile, where the number of
                # rounds tried and the size of the piece that still
                # failed are both known. Neither says "reduce the tile
                # size to N": a Tile carries a bbox and no tile_size_m at
                # all, so this module can measure a piece it holds (see
                # extent_metres) but cannot name a setting it has never
                # been told, and a coordinator review's Honesty finding 1
                # was exactly that kind of unverifiable advice.
                raise NodeCapExceededError(
                    f"Tile {tile.tile_id} exceeded the OSM API 50000-node limit.",
                    tile=tile,
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

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root file merge() could ever write for this stem. Read by
        package.py's stale-output sweep; see sources/base.py."""
        return [f"{stem}.osm"]
