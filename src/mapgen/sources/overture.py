"""Overture Maps as a LayerSource.

Downloads run through the overturemaps CLI, which handles the cloud-hosted
parquet release. Each type is fetched once, over the whole request bbox, and
merged into one GeoJSON per type. Up to MAX_CONCURRENT_TYPE_DOWNLOADS of
those fetches run at the same time (Task 24): they are independent network
reads that spend nearly all of their time waiting, and running the eight
default types together took the same 168,020,582 bytes from 59.98s and
61.88s down to 17.75s and 17.77s on this machine. See that constant for
the full measurement and for why eight is where it stops.

Not parallelised across sources, and specifically not for OSM. OSM tiles hit
a shared public API with its own rate limits, and pointing many concurrent
requests at it is a good way to get the owner throttled part way through a
survey. Overture is a read of cloud-hosted parquet with no such etiquette
problem, which is why it and not OSM got this.

Not tiled, unlike OsmSource (Task 23). OSM is tiled because the OSM map API
has a hard 50,000-node cap per request, and the subdivision machinery in
osm.py exists to service that cap when a tile hits it even so (Task 26,
which replaced package.py's whole-run retry at a smaller tile size).
Overture is a bbox-filtered read of
cloud-hosted parquet with no equivalent cap; it only ever inherited OSM's
constraint because it was added alongside it. Measured on this machine over
the same extent, same type, 16 tiles at 600 m with 20 m overlap, in two
independent samples: one whole-bbox call took 4.66s and 4.51s, the 16 tiled
calls took 68.78s and 93.38s, and both returned the identical 9,910 unique
features while the tiled version wrote 956,404 extra bytes of overlapping
duplicate data. The counts match exactly because geo.build_tiles clamps every
tile's query_bbox to the parent extent on all four sides, so the tiled union
covers exactly the parent bbox and never more. Per-call cost is dominated by
fixed overhead (process spawn, parquet metadata read, connection setup), not
data volume: a large bbox returning 89,722 features and 55 MB still
completed in one call in 43.73s, so a single call scales to real survey
extents comfortably.

That 55 MB is `building` over the owner's own Barry extent
(-3.3400,51.3600,-3.1000,51.5000), which is 16.66 x 15.58 km, not the
10 x 14 km this paragraph used to attribute it to. The two were
conflated when it was written. Re-measured on 2026-08-04 (Task 25):
55,420,672 bytes there, and 25,863,193 bytes in 5.41s for the same type
over the 10.41 x 13.91 km bbox. The conclusion is unaffected and if
anything understated. The 43.73s is left as recorded rather than
replaced, because it was a real reading on a real day and this source
has been seen to vary sixfold on the same query. It is not a cost
anchor, and none of the constants below were fitted from it.
"""

from __future__ import annotations

import concurrent.futures
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Sequence

from mapgen.fsutil import best_effort_rmtree, ensure_dir
from mapgen.geo import BBox, Tile, extent_metres
from mapgen.jobs import Cancelled, CancelToken
from mapgen.merge import merge_geojson
from mapgen.procutil import run_hidden
from mapgen.sources.base import (
    RETRYABLE_FAILURE_KINDS,
    FAILURE_REFUSED,
    FAILURE_UNKNOWN,
    FAILURE_UNREACHABLE,
    Estimate,
    ProgressSink,
    TileFailure,
)

DEFAULT_OVERTURE_TYPES = [
    "building",
    "place",
    "segment",
    "connector",
    "infrastructure",
    "land_use",
    "land_cover",
    "water",
]

# Overture type to the friendly filename written into the package layers folder.
LAYER_FILENAMES = {
    "water": "water.geojson",
    "land_cover": "vegetation.geojson",
    "land_use": "landuse.geojson",
}

# Real measurements on this machine against the live release, not a model.
#
# Refitted on 2026-08-04 (Task 25), against overturemaps 0.20.0 from the
# venv, driven through this module's own fetch() on the shipping
# concurrent code path with MAX_CONCURRENT_TYPE_DOWNLOADS at 8. What they
# replaced was fitted before Task 23 untiled this source and before Task
# 24 made it concurrent, and by 2026-08-04 it overstated a full 8-type run
# by 6.5x at 4 sq km and by 34x at 260 sq km. The old constants were not
# wrong when they were taken; the code underneath them changed and they
# were not retaken.
#
# 42 timed runs. The area sweep is all 8 default types over four Welsh
# extents, six samples each, interleaved (small, mid, big, large, and
# again) so no extent gets a systematically better network than another:
#
#     4.17 sq km   12.42s to 20.09s   mean 15.21s (12 samples)
#    38.63 sq km   15.03s to 20.71s   mean 17.43s
#   144.92 sq km   16.56s to 19.45s   mean 17.81s
#   259.60 sq km   17.43s to 20.67s   mean 18.77s
#
# Sixty-two times the area for 1.23x the time. Wall clock is set by the
# slowest single download in the pool now, not by the sum of eight and
# not by how much ground is covered.
#
# The batch is what costs, and how many downloads share it matters far
# more than the extent does. Same 4.17 sq km extent, three samples each:
#
#   1 type     3.52s to  3.70s   mean  3.59s
#   2 types    3.70s to  4.54s   mean  4.22s
#   4 types    7.06s to 13.97s   mean 10.15s
#   8 types   12.42s to 20.09s   mean 15.21s
#
# Eight at once take 4.2x one, not 8x and not 1x: they overlap, and they
# also slow each other down (see MAX_CONCURRENT_TYPE_DOWNLOADS for the
# bandwidth measurements behind that). So the model is per BATCH, with a
# term for how many downloads share the batch, and emphatically not a
# per-type cost multiplied out.
#
# Least squares over all 42 runs gives
#   1.623 + 1.789 * types_in_batch + 0.01177 * sq_km
# rounded below to the two significant figures this data can carry. Every
# fitted cell lands within 0.87x to 1.24x of its measured mean; the two
# worst are the 2-type and 4-type cells, where WHICH types are in the
# batch matters more than how many (`segment` is the slowest of the eight
# and `water` among the cheapest).
#
# Checked afterwards against a cell that was not fitted: one type over
# 144.92 sq km measured 5.41s against 5.11s predicted.
#
# Two significant figures is all that is claimed. Run-to-run variance on
# the same query has been seen at 4.66s against 28.48s, and the twelve
# samples of the SAME 8-type run at 4.17 sq km span 12.42s to 20.09s.
# That spread is wider than any refinement this data could justify.
#
# Both overturemaps on this machine were checked, because they perform
# differently often enough to be worth ruling out: 0.19.0 (what
# shutil.which finds, from C:\Python313\Scripts, which is what an
# unactivated shell gets) and 0.20.0 (what the activated venv gives, as
# the README instructs), alternated back to back over the same extent,
# three samples each. 15.81s to 18.10s against 12.42s to 16.82s, and byte
# totals differing by 10,498 in 23.6 million, which is 0.04%. They do not
# differ enough to change any constant here, so these fit both.
BASE_SECONDS_PER_BATCH = 1.6
SECONDS_PER_TYPE_IN_BATCH = 1.8
SECONDS_PER_BATCH_PER_SQ_KM = 0.012

# Bytes, unlike seconds, are deterministic. Every sample at a given extent
# returned a byte-identical total. Measured 2026-08-04, all 8 default
# types, same runs as above:
#
#     4.17 sq km    23,596,128
#    38.63 sq km    41,491,052
#   144.92 sq km    70,398,078
#   259.60 sq km   168,020,582
#
# The shape was already close and is kept: a fixed per-type base plus an
# area term, one unit per type. Only the slope moved, from 172,000 to
# 66,000, by least squares over all four extents with the base held where
# it was.
#
# The residuals say something the model cannot express: bytes depend on
# what is on the ground, not on how much ground there is. 144.92 sq km
# that is mostly the Bristol Channel returns 70 MB, while 259.60 sq km of
# Barry and Cardiff returns 168 MB. The fitted line reads 0.85x at the
# smallest extent, 0.92x at 38.63, 1.34x at 144.92 and 0.92x at 259.60.
# No area-only model does better, and a slope that nailed any single
# extent would be worse at every other. That is exactly how the old
# 172,000 went wrong: it was fitted to the smallest extent alone, where
# it was accurate to 1%, and it read 2.2x high at the owner's real one.
BASE_BYTES_PER_TYPE = 2_230_000
BYTES_PER_TYPE_PER_SQ_KM = 66_000

# How many type downloads run at once (Task 24).
#
# Threads rather than processes: every one of these is a subprocess.run
# blocked on a network read for essentially its whole life, so the GIL is
# released throughout and processes would buy nothing but their own spawn
# cost on top.
#
# Eight, from measurement rather than taste. Taken on this machine through
# this same fetch(), against the live release, using overturemaps 0.20.0
# from the venv, over the owner's real Barry extent
# (-3.3400,51.3600,-3.1000,51.5000, roughly 10 km x 14 km), all eight
# default types, A and B alternated back to back so neither gets a
# different network from the other:
#
#     one at a time     59.98s, 61.88s
#     eight at a time   17.75s, 17.77s
#
# 168,020,582 bytes on every single run at both settings, so nothing is
# being skipped to go faster. About 3.4x.
#
# Eight is where it stops, but not quite for the reason it looks like from
# the wall clock. Under eight-way every individual download gets SLOWER,
# not merely overlapped: `segment` takes 8.7s to 10.6s on its own and
# 16.2s to 17.7s alongside seven others, and `place` goes from 5.3s to
# 8.2s. Total throughput is what improved, from about 2.8 MB/s to about
# 9.4 MB/s, which is the shape of a link running out of bandwidth rather
# than of requests waiting on latency. More concurrency has nothing left
# to win here: it would divide the same bandwidth into more pieces.
#
# It is a CAP and not simply "however many types were asked for" because
# --overture-type takes any string and is repeatable, so a caller can name
# far more than the default eight. Without a ceiling, one request would
# launch an unbounded number of CLI processes at once.
MAX_CONCURRENT_TYPE_DOWNLOADS = 8


class OvertureError(RuntimeError):
    """Raised when the overturemaps CLI is missing or fails.

    `kind` is the shared failure vocabulary's term for what went wrong,
    added in Task 32 so package.py can decide whether a second attempt is
    worth making without reading this message in English. It defaults to
    `unknown`, which is the kind that is never retried, so an
    OvertureError raised anywhere that has not thought about the question
    is treated as unrecoverable rather than retried blind.

    `phrase` is the same fact written for a person, and it is what ends
    up in survey.json. Kept beside the kind rather than derived from the
    message, because the message carries the CLI's own output and that is
    exactly what a reason must not (see TileFailure's own docstring).
    """

    def __init__(self, message: str, kind: str = FAILURE_UNKNOWN, phrase: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.phrase = phrase or "did not download, and gave no reason"


def _temp_download_path(output_path: Path) -> Path:
    """The .part path the CLI is pointed at before a clean exit renames it.

    One function rather than the expression repeated at each site, because
    _state_sidecar below has to derive from exactly the same path, and two
    independently-written versions of "the temp name" that drift apart
    would leave a sidecar nobody deletes.
    """
    return output_path.with_suffix(".geojson.part")


def _state_sidecar(download_path: Path) -> Path:
    """The .state file overturemaps 0.20.0 writes beside its --output.

    Its own state.get_state_path is literally Path(f"{output_path}.state"),
    so pointing the CLI at "water.geojson.part" produces
    "water.geojson.part.state". It is written unconditionally whenever
    --output is given; there is no flag to turn it off, so it has to be
    cleaned up after the fact.

    Left alone it reaches the finished package. package.py's
    _existing_output_files skips a file whose suffix is ".part", and the
    sidecar's suffix is ".state", so it survives that filter and is handed
    to merge() with everything else. merge() groups by file stem, and this
    file's stem is "water.geojson.part", so it becomes a merged output
    called "<stem>_water.geojson.part.geojson" sitting in the package root
    beside the real layers: 43 bytes of empty FeatureCollection, named like
    something Grasshopper should read, and not declared by
    possible_outputs() so the stale-output sweep never removes it either.
    Reproduced against 0.20.0 before this was written.

    Deliberately solved here rather than by teaching package.py to filter
    ".state": the shape of this filename is a fact about a third-party CLI,
    and package.py is the one module in this project that knows nothing
    about any individual source. 0.19.0 writes no sidecar at all, which is
    why nothing caught this until 0.20.0 appeared in the venv.
    """
    return download_path.with_name(download_path.name + ".state")


def _combined_failure(failures: dict[str, BaseException]) -> BaseException:
    """One exception for however many types failed in the same pool.

    A single failure is re-raised exactly as it came out, so the message
    the owner reads for the ordinary case is character for character the
    one this source has always raised ("overturemaps failed for type
    segment: ...") and nothing downstream that matches on it changes.

    More than one is the case a pool creates and a sequential loop never
    could, and it is the reason this function exists rather than the
    caller simply re-raising whichever future it happened to inspect
    first. Six types failing and one type failing are very different
    situations, most likely a dead network against one bad type name, and
    a report that names only the first gives the owner no way to tell
    them apart. Every type is named, and every type's own detail is
    carried, since the detail is usually the CLI's stderr and that is what
    says which of the two it was.
    """
    if len(failures) == 1:
        return next(iter(failures.values()))
    names = ", ".join(failures)
    detail = "; ".join(f"{name}: {exc}" for name, exc in failures.items())
    return OvertureError(
        f"overturemaps failed for {len(failures)} types ({names}). {detail}",
        kind=_combined_kind(failures),
    )


def _failure_kind(exc: BaseException) -> str:
    """The shared vocabulary's term for one type's failure.

    An OvertureError classified itself when it was raised (see
    _download). Anything else that came out of a worker, an OSError from
    the filesystem being the realistic case, is unknown: mapgen has no
    account of it, and the vocabulary's rule is that an unrecognised
    cause is never retried blind.
    """
    return getattr(exc, "kind", FAILURE_UNKNOWN) or FAILURE_UNKNOWN


def _failure_phrase(overture_type: str, exc: BaseException) -> str:
    """One failed type, as a sentence for the owner.

    Composed from the phrase the failure carried, never from str(exc).
    The exception's own message quotes the CLI's output, which is where
    a path, an S3 URL and, for a source that one day needs a key, a key
    would live; TileFailure's own docstring is explicit that a reason is
    assembled from a closed vocabulary rather than from whatever a
    service said. The CLI's own words still reach the owner, through the
    raised exception and through package.py's source_failed event.
    """
    phrase = getattr(exc, "phrase", "") or f"failed with {type(exc).__name__}"
    return f"Overture {overture_type}: {phrase}."


def _combined_kind(failures: Mapping[str, BaseException]) -> str:
    """One kind for however many types failed in the same batch.

    Retryable if ANY of them is, and that direction is deliberate. The
    retry re-runs fetch(), which skips every type already on disk, so
    what it actually repeats is exactly the set that failed. A bad
    --overture-type sitting in the same batch as a genuine transient
    would, under the opposite rule, cost the owner the layer that could
    have been recovered, to save one second of a command that will
    refuse the typo again.

    Otherwise the first failure's kind, in the order the caller asked
    for the types, so the answer does not depend on which download the
    network happened to finish first.
    """
    kinds = [_failure_kind(exc) for exc in failures.values()]
    for kind in kinds:
        if kind in RETRYABLE_FAILURE_KINDS:
            return kind
    return kinds[0] if kinds else FAILURE_UNKNOWN


class OvertureSource:
    id = "overture"
    display_name = "Overture Maps"
    licence = "Overture Maps Foundation, mixed source licences (ODbL and CDLA-Permissive-2.0)"
    attribution = "(c) Overture Maps Foundation"
    requires_api_key = False

    def __init__(
        self,
        types: Sequence[str] | None = None,
        release: str | None = None,
        runner: Callable[..., object] = subprocess.run,
        executable_finder: Callable[[str], str | None] = shutil.which,
    ) -> None:
        # A coordinator review's Critical 2: `types or DEFAULT_OVERTURE_TYPES`
        # treats an EMPTY list the same as no list at all, because both are
        # falsy. types=[] is not "unspecified", it is a genuine, deliberate
        # "fetch nothing", the exact shape overture_types_for_categories
        # returns for a category selection like ["rail"] or ["boundaries"]
        # that maps to no Overture type at all (see that function's own
        # docstring). Under the old line, every one of those selections
        # silently fetched the full 8-type default instead, undetected by
        # any test because the mutation this review proposed, replacing the
        # line with the one below, left all 561 tests at the time green:
        # nothing exercised configure() with an empty (as opposed to
        # unspecified) type list and then checked what reached self.types.
        # None is the only value this treats as "use the default"; anything
        # else, including [], is taken exactly as given.
        self.types = list(types) if types is not None else list(DEFAULT_OVERTURE_TYPES)
        # Which of those types this instance's fetch() actually got onto
        # disk, set at the end of fetch() and read by package.py's
        # _source_provenance (review finding I2). None until fetch() has
        # run at all, which is what tells "not attempted" apart from
        # "attempted and got none", so a source that never got its turn
        # still has survey.json report the selection rather than an empty
        # list. self.types itself stays exactly as configured, because
        # possible_outputs and _remove_earlier_version_debris both read it
        # and both need the full closed list, not just what landed.
        self.fetched_types: list[str] | None = None
        # How many features the last merge() actually wrote, across every
        # type, so survey.json can explain a missing file as "there was
        # nothing here" rather than leaving it as a silent gap (Task 30).
        # None until merge() has run.
        self.merged_features: int | None = None
        # The per-tile account of the most recent fetch(), for package.py's
        # retry pass (Task 32). The optional LayerSource extension
        # sources/base.py documents: reset at the top of every fetch(), so
        # it always describes THAT call and never accumulates.
        #
        # This source has no tiles of its own either (Task 23 made every
        # download whole-extent), so a type that did not arrive did not
        # arrive for every tile equally, and the record says so against
        # each of them. Exactly the reasoning package.py's own
        # _record_tile_outcomes already applies here.
        self.tile_failures: list[TileFailure] = []
        self.release = release
        self._runner = runner
        self._find = executable_finder

    # category -> tier, this source's own row of mapgen.resolver's shared
    # table (the phase 2 spec's tier tables). A category absent here is
    # one Overture never serves, and tier() answers None for it.
    _TIERS = {
        "heights": 2,
        "buildings": 2,
        "land_use": 1,
        "water": 1,
        "places": 1,
        "greenspace": 2,
        "land": 2,
    }

    def covers(self, bbox: BBox) -> str:
        """Overture Maps has no coverage edge at all: its releases are
        global, so this is always "full". See sources/base.py's own
        docstring on this optional extension.
        """
        return "full"

    def tier(self, category: str) -> int | None:
        """This source's own tier for `category`, or None when Overture
        does not serve it. See _TIERS above.
        """
        return self._TIERS.get(category)

    # -- detail (mapgen.resolver) ---------------------------------------------

    def detail(self, bbox: BBox) -> str:
        """The spec's own resolution copy, verbatim.

        Fixed regardless of bbox: resolve() only ever calls this on a
        source whose covers(bbox) was not "none" for the same bbox (see
        sources/base.py's own docstring on this optional extension), and
        this figure is a property of how Overture's own conflated
        sources trace geometry, not of which extent a given survey
        happens to draw, so there is no bbox-dependent answer to give
        and no covers() gate needed here either (this source's own
        covers() is a fixed "full" regardless, in any case). Never
        touches the network, trivially: nothing here reads anything at
        all.
        """
        return "traced footprints and centrelines, typically 1 to 5 m positional accuracy"

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        """Bytes per type, seconds per BATCH of concurrent types.

        Neither number has anything to do with tiles (Task 23): fetch()
        makes exactly one overturemaps call per type over the whole bbox.
        `tiles` stays in the signature because the LayerSource protocol
        defines it and OsmSource genuinely needs it.

        Bytes are per type because every type really is downloaded and
        really does land on disk, so eight types cost eight types' worth
        of disk and of the owner's data allowance however they are
        scheduled.

        Seconds are not, and that is the whole point of this method since
        Task 24. Downloads run up to MAX_CONCURRENT_TYPE_DOWNLOADS at a
        time, so a selection at or under the cap costs ONE batch: its
        wall clock is the slowest download in the pool, not the sum of
        them. Multiplying a per-type cost by the type count, which is
        what this did until Task 25, is how the panel came to overstate a
        real run by up to 34x.

        The cap is read here, not assumed away, because --overture-type
        is repeatable and takes any string (see fetch()). A caller naming
        more types than the cap genuinely does serialise: the pool runs
        the first MAX_CONCURRENT_TYPE_DOWNLOADS, and the rest wait. The
        loop below charges for each batch, so twelve types read as two
        batches rather than as one impossibly fast one.

        Deliberately a slight overstatement for a type count that is not
        a multiple of the cap. ThreadPoolExecutor is a pool and not a
        barrier: with twelve types the ninth starts the moment any of the
        first eight finishes, rather than waiting for all eight. Charging
        two whole batches is the pessimistic reading, chosen because it
        cannot be measured today (the default selection is eight, exactly
        one batch, and nothing in the interface offers more) and because
        an estimate that runs slightly long is a better failure than one
        that runs out early. Said plainly here so the next person knows
        it is a decision and not an oversight.
        """
        width_m, height_m = extent_metres(bbox)
        area_sq_km = (width_m / 1000.0) * (height_m / 1000.0)
        # Deduplicated exactly as fetch() deduplicates, and for the same
        # reason: `--overture-type water --overture-type water` is a thing
        # the owner can type, and fetch() downloads it once. An estimate
        # that charged twice would describe a run that does not happen.
        type_count = len(dict.fromkeys(self.types))
        # Rounded to whole bytes once, per type, and only then multiplied.
        # Rounding the product instead would make the estimate for two
        # types differ from twice the estimate for one by a byte or two,
        # which is meaningless in itself but makes the "one call per type"
        # shape impossible to assert cleanly and invites a future reader to
        # go looking for a scaling subtlety that is not there.
        per_type_bytes = int(BASE_BYTES_PER_TYPE + BYTES_PER_TYPE_PER_SQ_KM * area_sq_km)

        seconds = 0.0
        remaining = type_count
        while remaining > 0:
            in_batch = min(remaining, MAX_CONCURRENT_TYPE_DOWNLOADS)
            seconds += (
                BASE_SECONDS_PER_BATCH
                + SECONDS_PER_TYPE_IN_BATCH * in_batch
                + SECONDS_PER_BATCH_PER_SQ_KM * area_sq_km
            )
            remaining -= in_batch

        return Estimate(
            bytes_estimate=per_type_bytes * type_count,
            seconds_estimate=seconds,
        )

    def configure(self, types: Sequence[str]) -> "OvertureSource":
        """Returns a fresh OvertureSource scoped to exactly these types,
        sharing this instance's release/runner/executable_finder, and
        never mutating self.

        This is the fix for the bug Task 20 found and deliberately left
        for this task: register_default_sources() builds ONE OvertureSource
        with the 8-type default and registers it into the process-wide
        registry once; every request, whatever its own --overture-type or
        category selection, was fetching through that same shared
        instance's fixed .types, so the owner downloaded all 8 datasets
        every time regardless of what they asked for. The fix is not to
        mutate the registered instance's .types in place: estimate_survey
        can be polled from a browser while a job using a DIFFERENT
        selection is already running (there is no busy-guard on
        /api/estimate, only on /api/jobs), and mutating shared state that
        an in-flight fetch() loop is actively reading from underneath it
        is exactly the kind of race that would corrupt a running job's
        output for a reason that would be very hard to reproduce. Handing
        back a new, independently-configured instance instead means the
        registered instance available_sources()/GET /api/sources lists
        keeps behaving identically no matter what any single request
        selects, while package.py uses the returned copy for the actual
        estimate/fetch/merge work.
        """
        return type(self)(
            types=types, release=self.release, runner=self._runner, executable_finder=self._find
        )

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        """One download per type, over the whole request bbox, up to
        MAX_CONCURRENT_TYPE_DOWNLOADS of them at once.

        bbox, not each tile's query_bbox: this source is not tiled (see the
        module docstring for the measurements). tiles is still used, for
        progress reporting only.

        Progress events stay per tile AND per type, exactly as they were
        when the download itself was per tile and per type. tile_done (or
        tile_skipped on a resume) is emitted once per tile for a type, after
        that type's single whole-extent download lands, which is a truthful
        claim: once the whole extent for a type is on disk, every tile's
        area genuinely does have that type's data.

        Deliberately NOT switched to ElevationSource's tile_id="whole-area"
        convention, which would look like the tidier match for a source
        that no longer tiles. The browser's classifyTiles (web/static/
        app.js) ignores any event whose tile_id is not in the grid it built
        from the plan, so "whole-area" would be silently dropped and an
        Overture-only run, which the owner can and does select, would show
        a dead grid from the first second to the last. classifyTiles also
        only ever promotes a tile pending -> active on tile_done/
        tile_skipped, and settles active -> done only once every selected
        source has emitted source_done, so emitting per tile per type does
        not make a tile look finished after the first of eight types.

        Those emissions now leave several worker threads at once, which is
        the part of Task 24 with the least visible failure mode: nothing
        crashes when two threads write a half line each, the owner simply
        reads nonsense. Every ProgressSink in production was checked
        against that (see download_one below) and ConsoleProgress was
        fixed in the same task.

        The returned list is in the order the caller asked for the types,
        which under a pool is emphatically not the order they finished in.
        merge() groups these by stem and package.py rebuilds the list from
        disk anyway, so nothing downstream currently depends on the order;
        that is exactly why it is worth pinning, because a return value
        whose order is decided by whichever download the network happened
        to finish first is a landmine for whoever next writes code that
        does depend on it.
        """
        # Debris from the superseded per-tile layout, swept once, up front,
        # before anything is downloaded or read (Task 23). Not migration:
        # nothing here reuses those files, and the fresh whole-extent
        # download below covers every one of them. They are removed because
        # leaving them in place actively corrupts this package. work_dir is
        # fingerprinted on the type selection (naming.tiling_fingerprint),
        # so a part-downloaded package from before this change resumes into
        # this same directory with <type>/<tile_id>.geojson files still
        # sitting in it, and package.py's own _existing_output_files hands
        # every file it finds under work_dir straight to merge() and to
        # _record_tile_outcomes. merge() would then read each stale tile id
        # as though it were a TYPE and write a <stem>_r00_c00.geojson into
        # the package root beside the real layers, and _record_tile_outcomes
        # would see a tile-stamped directory again and mark every tile the
        # old run never reached FAILED, leaving a package that is in fact
        # complete permanently reporting complete: false and painting a red
        # tile the owner has no way to clear. Both were reproduced before
        # this sweep was written. The closed list is the safety property, as
        # it is for package.py's own stale-output sweep: only a directory
        # named exactly after a type this source fetches, inside this
        # source's own fingerprinted scratch tree, is ever removed.
        #
        # The same sweep also removes a .state sidecar an earlier run left
        # behind (see _state_sidecar). _download cleans up the one IT
        # creates, but _download does not run at all for a type that is
        # already on disk, which is the whole point of a resume, so a
        # sidecar written by an unfixed version would survive into merge()
        # and put a junk <stem>_<type>.geojson.part.geojson in the package.
        # Reproduced on that exact resume path before this line was added.
        # Deliberately before the pool, on this one thread, and it has to
        # stay there. Each type's own sidecar has a distinct name, so the
        # per-download cleanup in _download cannot collide across threads;
        # this sweep does not, because it walks every type in the selection
        # and would race with a worker that had already started writing its
        # own .part and sidecar. Nothing here is worth parallelising in any
        # case: it is a handful of unlink calls against a directory that is
        # normally empty.
        # Reset before anything else, so this list always describes THIS
        # call. A retry that recovers a type must not leave the previous
        # attempt's reason attached to a layer that is now on disk.
        self.tile_failures = []

        self._remove_earlier_version_debris(work_dir)

        # Deduplicated, and in the order asked for. Duplicates are only
        # reachable through --overture-type, which is repeatable and takes
        # any string, so `--overture-type water --overture-type water` is a
        # thing the owner can type. Sequentially that was harmless: the
        # second pass found the first pass's file already on disk and
        # skipped it. Concurrently it is not, because both copies would
        # race for the same water.geojson.part, and the first thing
        # _download does with that path is unlink it, so one thread would
        # delete the other's half-written download and both would then
        # rename over the same output. Collapsed here rather than in
        # self.types, which stays exactly as the caller configured it,
        # since possible_outputs and _remove_earlier_version_debris both
        # read it and neither cares about duplicates.
        work_types = list(dict.fromkeys(self.types))
        if not work_types:
            # Not merely an optimisation: ThreadPoolExecutor(max_workers=0)
            # raises, and an empty selection is a real, deliberate request
            # (see __init__ on types=[]).
            return []

        def download_one(overture_type: str) -> Path:
            # Checked again here, at the start of the worker, and not only
            # before submission: a type still queued behind the cap when a
            # stop lands is skipped rather than run. A download already in
            # flight is never interrupted, per the LayerSource cancel
            # convention.
            if cancel is not None:
                cancel.raise_if_cancelled()
            output_path = work_dir / f"{overture_type}.geojson"
            if output_path.exists() and output_path.stat().st_size > 0:
                event = "tile_skipped"
            else:
                self._download(bbox, overture_type, output_path)
                event = "tile_done"
            # Emitted from this worker thread, so every ProgressSink in
            # production has to be safe to call concurrently. Checked, not
            # assumed: EventLog appends under its own lock, NullProgress
            # holds no state, and ConsoleProgress was given a lock in this
            # same task because print() writes the text and the newline
            # separately and two threads could split them apart.
            for tile in tiles:
                progress.emit(
                    event,
                    source=self.id,
                    tile_id=tile.tile_id,
                    overture_type=overture_type,
                )
            return output_path

        downloaded: dict[str, Path] = {}
        failures: dict[str, BaseException] = {}
        cancelled: Cancelled | None = None
        futures: dict[str, concurrent.futures.Future] = {}

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MAX_CONCURRENT_TYPE_DOWNLOADS, len(work_types)),
            thread_name_prefix="mapgen-overture",
        ) as pool:
            for overture_type in work_types:
                if cancel is not None:
                    try:
                        cancel.raise_if_cancelled()
                    except Cancelled as exc:
                        cancelled = exc
                        break
                futures[overture_type] = pool.submit(download_one, overture_type)

            # Drained in full before anything is raised, which is the whole
            # point: seven types that succeeded have real files on disk and
            # a hundred and sixty megabytes of paid-for network behind
            # them, and an eighth that failed is not a reason to abandon
            # them. Same principle as the Urbano bridge fix in Task 20 and
            # the stop path in Task 22. Their files stay where they are
            # either way, and package.py rebuilds the part list from disk
            # rather than from this return value (see its own comment at
            # the fetch call site), so a raise below costs nothing that was
            # already earned.
            for overture_type, future in futures.items():
                try:
                    downloaded[overture_type] = future.result()
                except Cancelled as exc:
                    if cancelled is None:
                        cancelled = exc
                except Exception as exc:
                    # Broad on purpose: whatever a worker raised, an
                    # OvertureError or an OSError from the filesystem, this
                    # loop's job is to finish inspecting the other seven
                    # futures before any of it comes back out. Narrowing it
                    # would let an unexpected type escape mid-drain and
                    # abandon the futures after it. BaseException is
                    # deliberately NOT caught: a KeyboardInterrupt should
                    # end the run, not be filed as a type that failed.
                    failures[overture_type] = exc

        # Recorded before anything is raised, and that ordering is the
        # point (review finding I2): the partial case is the one this
        # exists for. Seven types landing and an eighth failing leaves
        # seven real layers merged into the package, and survey.json used
        # to go on listing all eight under `types`, which README documents
        # as "the actual Overture types fetched". The folder and the
        # record disagreed, in the same shape as finding I8's licence
        # line. `downloaded` covers a type that was freshly downloaded and
        # one that was already on disk and skipped equally, because both
        # mean the package holds it.
        self.fetched_types = [t for t in work_types if t in downloaded]

        if failures:
            # Recorded before the raise, and against every tile, because
            # package.py's retry pass is written in tiles and this source
            # has none of its own. What the record buys is the whole of
            # Task 32's first section: a failed TYPE is one download to
            # repeat, and repeating it is exactly what calling fetch()
            # again does, since download_one skips every type that is
            # already on disk. No type that succeeded is fetched twice,
            # and the concurrent batch is not restarted; what the pool
            # re-runs is the failed types and a handful of instant skips.
            self.tile_failures = self._tile_failures_for(tiles, failures)
            raise _combined_failure(failures)
        if cancelled is not None:
            # A genuine download failure is reported ahead of a stop, on
            # purpose. A stop is the owner's own decision and they already
            # know about it; a download that broke is news, and swallowing
            # it inside a Cancelled would leave package.py marking the
            # affected tiles pending and the owner with no idea a type is
            # missing for a reason that will still be there next time.
            raise cancelled

        # In the order the caller asked for the types, never completion
        # order, which under a pool is whatever the network decided. See
        # the module docstring.
        return [downloaded[t] for t in work_types if t in downloaded]

    def _tile_failures_for(
        self, tiles: Sequence[Tile], failures: Mapping[str, BaseException]
    ) -> list[TileFailure]:
        """The per-tile record for however many types failed in one batch.

        One reason, naming every failed type and what happened to each,
        repeated against every tile. Not one record per (tile, type):
        package.py's ledger is keyed by (source, tile) and always has
        been, and a second record for the same tile would silently
        replace the first, leaving a reason that named one of five failed
        types and gave no clue there were four more.

        No retry_after_seconds. The overturemaps CLI reports nothing of
        the kind: it has no HTTP response to carry a header, and its own
        error text is printed and discarded. Attaching a made-up period
        would be exactly the fabricated distinction this task was told
        not to invent.
        """
        reason = " ".join(
            _failure_phrase(overture_type, exc)
            for overture_type, exc in failures.items()
        )
        kind = _combined_kind(failures)
        return [
            TileFailure(source=self.id, tile_id=tile.tile_id, kind=kind, reason=reason)
            for tile in tiles
        ]

    def _remove_earlier_version_debris(self, work_dir: Path) -> None:
        """Remove what an earlier version of mapgen, or of the overturemaps
        CLI, can leave in this work directory for a resume to trip over.

        Two things, both of which reach merge() otherwise. See the comment
        at the call site in fetch() for why this is a defect fix rather
        than a migration, and _state_sidecar for what the sidecar is.

        Both are driven off self.types, a closed list, rather than globbing:
        nothing is removed whose name this source could not itself have
        produced. self.types alone is enough rather than a union with
        DEFAULT_OVERTURE_TYPES the way possible_outputs does it, because
        naming.tiling_fingerprint hashes the resolved overture_types, so a
        narrowed selection lands in its own work directory and can never
        meet a wider selection's leftovers here. possible_outputs faces the
        opposite situation and genuinely does need the union: it names
        files in the PACKAGE ROOT, which is shared across selections.
        """
        for overture_type in self.types:
            legacy_dir = work_dir / overture_type
            if legacy_dir.is_dir():
                best_effort_rmtree(legacy_dir)
            _state_sidecar(
                _temp_download_path(work_dir / f"{overture_type}.geojson")
            ).unlink(missing_ok=True)

    def _download(self, bbox: BBox, overture_type: str, output_path: Path) -> None:
        executable = self._find("overturemaps")
        if not executable:
            raise OvertureError(
                "Could not find the overturemaps CLI on PATH. Run bootstrap.ps1, "
                "or install it with: pip install overturemaps",
                kind=FAILURE_REFUSED,
                phrase="the overturemaps command is not installed on this machine",
            )

        ensure_dir(output_path.parent)
        # The CLI writes wherever we point it, and a killed process leaves a
        # truncated file that resume would later mistake for a finished
        # download. Point it at a .part path and rename only once it exits
        # cleanly. Untouched by Task 23: one whole-extent file is a much
        # bigger thing to half-write than one tile was, so the risk this
        # guards against is if anything larger now, not smaller.
        temp_path = _temp_download_path(output_path)
        state_sidecar = _state_sidecar(temp_path)
        temp_path.unlink(missing_ok=True)
        # Cleared before as well as after: a sidecar left by a previous
        # attempt that was killed outright, between the CLI writing it and
        # this function returning, would otherwise still be sitting there.
        state_sidecar.unlink(missing_ok=True)
        command = [
            executable,
            "download",
            f"--bbox={bbox.to_query_string()}",
            "-f",
            "geojson",
            "--type",
            overture_type,
            "--output",
            str(temp_path),
        ]
        if self.release:
            command.extend(["--release", self.release])

        # run_hidden, not a bare runner call: on Windows this is a console
        # executable, and without CREATE_NO_WINDOW it opens a real window
        # that the owner can close, killing the download inside it. See
        # mapgen.procutil. Far fewer windows than before Task 23 (one per
        # type rather than one per tile per type), which is a reason the
        # owner meets this less often, not a reason to stop hiding them.
        # try/finally, not a tidy-up after the happy path: 0.20.0 writes the
        # sidecar before this function decides whether the download counts,
        # so every way out of here has to remove it. A non-zero exit and a
        # clean exit that wrote nothing both raise below, and either would
        # otherwise leave the sidecar behind for merge() to find on the next
        # resume, having left no real output to go with it.
        try:
            result = run_hidden(command, runner=self._runner)
            if result.returncode != 0:
                temp_path.unlink(missing_ok=True)
                detail = (result.stderr or result.stdout or "").strip()
                # Never retried, and this is the conservative half of Task
                # 32's Overture classification rather than a claim to know
                # what happened. A non-zero exit from this CLI is USUALLY
                # click rejecting the invocation (an --overture-type it
                # does not recognise exits 2), which no number of
                # attempts fixes. It can also be an exception escaping the
                # release or STAC lookup, which would be transient. The
                # two are not distinguishable from here without reading
                # click's own exit-code convention as though it were an
                # API, so the reported kind is the one that does not
                # invent a distinction.
                raise OvertureError(
                    f"overturemaps failed for type {overture_type}: {detail}",
                    kind=FAILURE_REFUSED,
                    phrase=(
                        f"the overturemaps command refused the request "
                        f"(exit code {result.returncode})"
                    ),
                )

            if not temp_path.exists():
                # Retried, and this is the one Overture failure that is
                # genuinely worth another attempt. It looks like the
                # weakest signal here and it is in fact the strongest,
                # because of what overturemaps 0.20.0 actually does:
                # _create_s3_record_batch_reader catches EVERY exception,
                # prints "Error reading data from path ...: ..." to its
                # own stdout, and returns None, and the download command
                # answers a None reader with a bare `return`. So a failed
                # read of the Overture store exits ZERO and writes no
                # file. That is what this branch is.
                #
                # It is not the empty case. A bbox with nothing in it
                # still builds a reader (an empty STAC result falls back
                # to the full dataset path) and still writes an empty
                # FeatureCollection, so a legitimately empty type leaves
                # a real file and never reaches here.
                #
                # What mapgen cannot tell is WHICH read failure it was: a
                # timeout, S3 asking for less traffic, or a genuine
                # error. All three are printed as English on the CLI's
                # stdout and are distinguishable only by matching that
                # text, which is the mistake this codebase keeps naming.
                # unreachable is the retryable kind that claims least:
                # the store was not read. The reason below says plainly
                # that the cause is not known.
                detail = (result.stdout or result.stderr or "").strip()
                raise OvertureError(
                    f"overturemaps exited cleanly but wrote nothing for type "
                    f"{overture_type}. It said: {detail}"
                    if detail
                    else (
                        f"overturemaps exited cleanly but wrote nothing for type "
                        f"{overture_type}."
                    ),
                    kind=FAILURE_UNREACHABLE,
                    phrase=(
                        "the overturemaps command finished without downloading "
                        "anything, which is how it reports a failed read of the "
                        "Overture data store. It does not say whether that was a "
                        "timeout, a rate limit or a genuine error"
                    ),
                )
            temp_path.replace(output_path)
        finally:
            state_sidecar.unlink(missing_ok=True)

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        # Grouped by the file's own stem, which IS the type now that fetch()
        # writes one flat <type>.geojson per type (Task 23). It used to be
        # part.parent.name, because the type was a directory holding one file
        # per tile; that directory no longer exists, so that key would group
        # every part under the single work directory's name and merge all
        # eight types into one output. Still a grouping rather than a plain
        # one-part-per-type walk, because merge() is handed whatever
        # package.py's _existing_output_files found on disk, and a source
        # that later regains a second file per type should not need this
        # rewritten again.
        by_type: dict[str, list[Path]] = {}
        for part in parts:
            by_type.setdefault(part.stem, []).append(part)

        # Named after the package stem plus the type, not a bare
        # "<type>.geojson": Task 20 finding 2 found the same unidentified
        # merged-output problem here as in OsmSource.merge and asked for a
        # consistent fix. package.py's _write_layer_files matches on this
        # exact composed name to copy the three phase 1 layers into layers/.
        #
        # A type that returned no features leaves no file, per Task 30's
        # ruling, and a previous attempt's file for that type is removed
        # rather than left to contradict the record. Applied per TYPE and
        # not only per source, because each type is its own merged file
        # and an empty <stem>_water.geojson is the same fabrication as an
        # empty <stem>.osm: a layer the folder says is present and
        # Grasshopper reads as containing nothing, rather than a layer
        # that is honestly absent. See OsmSource.merge for the full
        # reasoning, which is one ruling applied in two places.
        outputs: list[Path] = []
        merged_features = 0
        for overture_type, type_parts in sorted(by_type.items()):
            output = out_dir / f"{stem}_{overture_type}.geojson"
            # write_when_empty=False AND the unlink, for the reason
            # OsmSource.merge spells out: the pair is not redundant, it
            # closes the window in which the fabricated file exists.
            count = merge_geojson(type_parts, output, write_when_empty=False)
            merged_features += count
            if count == 0:
                output.unlink(missing_ok=True)
                continue
            outputs.append(output)
        self.merged_features = merged_features
        return outputs

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root-relative file this source could ever produce for this
        stem, across ALL selections, which is why this unions the full
        default type list with the instance's own (possibly narrowed, or
        exotically widened via --overture-type) selection rather than
        reading either alone. Includes the layers/ copies package.py's
        _write_layer_files derives from the merged output, so a stale
        layers/water.geojson is swept together with the stale
        <stem>_water.geojson it was copied from. Read by package.py's
        stale-output sweep; see sources/base.py.
        """
        every_type = sorted(set(DEFAULT_OVERTURE_TYPES) | set(self.types))
        names = [f"{stem}_{overture_type}.geojson" for overture_type in every_type]
        names += [f"layers/{filename}" for filename in LAYER_FILENAMES.values()]
        return names
