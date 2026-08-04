"""Orchestration: plan, download, merge, bridge, survey.json.

Knows about paths, job state and the source registry. Knows nothing about any
individual data source, which is what makes phase 2 additive.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from mapgen import __version__
from mapgen.bridge import BridgeError, BridgeRequest, run_bridge
from mapgen.categories import ALL_CATEGORY_IDS, overture_types_for_categories, validate_categories
from mapgen.elevation_models import DEFAULT_DEMTYPE, validate_demtype
from mapgen.fsutil import (
    atomic_write_text,
    best_effort_rmtree,
    ensure_dir,
    work_dir_scope,
)
from mapgen.geo import BBox, Tile, build_tiles, extent_metres
from mapgen.jobs import FAILED, OK, PENDING, CancelToken, Cancelled, JobState
from mapgen.merge import assert_inputs_present
from mapgen.naming import (
    PackagePaths,
    build_package_paths,
    check_path_length,
    slugify,
    tiling_fingerprint,
)
from mapgen.sources.base import (
    FAILURE_NO_OUTPUT,
    FAILURE_RATE_LIMITED,
    FAILURE_SERVICE_ERROR,
    FAILURE_TIMEOUT,
    FAILURE_UNKNOWN,
    FAILURE_UNREACHABLE,
    EmptySourceSelectionError,
    NullProgress,
    ProgressSink,
    TileFailure,
    UnknownSourceError,
    available_sources,
    get_source,
    register,
)
from mapgen.sources.elevation import ElevationSource
from mapgen.sources.osm import OsmSource
from mapgen.sources.overture import (
    DEFAULT_OVERTURE_TYPES,
    LAYER_FILENAMES,
    OvertureSource,
)

SCHEMA_VERSION = 1

# How many extra passes over the tiles that failed one run is allowed to
# make (Task 30). One.
#
# The number is small because it is the SECOND retry layer, not the first,
# and the first one is already generous. OsmSource._download_tile makes
# max_retries attempts, four by default, at every single tile, with
# exponential backoff between them and Retry-After honoured when the
# service sends one. By the time a tile reaches this constant it has
# already been asked for four times over roughly fourteen seconds. Another
# pass here is a fifth through eighth attempt, so the worst case for one
# tile is 8 requests rather than the 4 it was. Two passes would make it 12
# and three would make it 16, against a free public API this module
# already spaces out on purpose, for a tile that has by then said no
# eight times.
#
# What the extra pass buys that the inner four do not is TIME. The inner
# backoff is seconds; this pass runs after every other tile in the run has
# been fetched, which on the owner's own 72-tile Barry extent is about two
# and a half minutes later. That is the gap a rate-limit window or a
# service having a bad minute actually needs, and it is why this layer is
# worth having at all rather than simply raising max_retries, which would
# only ask the same question faster.
RETRY_PASS_BUDGET = 1

# Which causes a retry could plausibly fix. Everything else is asked once
# and reported, because asking again is either useless or harmful.
#
# Retried:
#   timeout        the service or the link was too slow this time; the
#                  next request is a different roll of the dice
#   unreachable    a dropped connection or a DNS blip, the same
#   rate_limited   the service asked for less traffic, and the whole
#                  point of this layer is that it comes back later
#   service_error  a 5xx is the service saying the fault is its own
#
# Not retried, and each for its own reason:
#   not_authorised a wrong, missing or expired key answers 401 every
#                  time. Retrying it wastes the owner's time and, on a
#                  keyed service, can count against them. This is
#                  elevation's most common failure by a wide margin.
#   refused        a 4xx that is not a rate limit means the request
#                  itself was rejected. The same request will be
#                  rejected again.
#   node_cap       the tile is too dense, and OsmSource has already
#                  split it as far as splitting goes (Task 26). The
#                  answer is a smaller extent, not another identical
#                  request. Retrying this would also quietly undo that
#                  whole mechanism by turning a bounded subdivision into
#                  an unbounded re-ask.
#   no_output      the layer finished and left nothing, with no reason
#                  given. mapgen does not know what to fix.
#   unknown        by construction the kind a source uses when it cannot
#                  say what happened. An unrecognised cause retried
#                  blind is how a rate-limited API gets hammered.
RETRYABLE_FAILURE_KINDS = frozenset(
    {
        FAILURE_TIMEOUT,
        FAILURE_UNREACHABLE,
        FAILURE_RATE_LIMITED,
        FAILURE_SERVICE_ERROR,
    }
)


class IncompleteSurveyError(RuntimeError):
    """Raised when a run ends with tiles that never arrived and --force
    was not given.

    Task 30 moved WHEN this happens, not whether. Before it, the first
    tile that failed raised out of the source's own fetch() and ended the
    run there; now every tile is attempted, the failures are collected,
    the recoverable ones are retried, and this is raised at the end if any
    are still missing. The outcome for the owner is the same in the one
    respect they were promised: without --force, a run that could not get
    everything does not pretend otherwise, and it exits non-zero.

    What changed is that survey.json has already been written by the time
    this is raised, so the package explains itself rather than being a
    folder of scratch files with no record. The message carries the same
    account, composed by describe_tile_failures, so the terminal, the
    progress log and the file all say the same thing.

    A RuntimeError rather than a ValueError, unlike every refusal in this
    module: those are all "your request cannot be honoured", decided
    before any work starts. This one is "the work was done and the world
    did not cooperate", which is not the owner's mistake to correct.
    """


def _fetch_accepts_cancel(source) -> bool:
    """Whether source.fetch() accepts the optional `cancel` keyword Task
    22 adds to the LayerSource protocol.

    fetch() is not an optional extension the way readiness_problem or
    possible_outputs are (every source has one), only its willingness to
    be interrupted mid-loop varies, so this cannot be read defensively
    with a plain getattr the way this module reads those. The three real
    sources (osm, overture, elevation) all accept `cancel` now; every
    stub LayerSource this project's own tests define, and any future
    source that has not been updated yet, does not, and calling
    fetch(..., cancel=token) against one of those would raise TypeError
    before a single tile is fetched. Checked once per source per run via
    inspect.signature, not a try/except TypeError around the real call,
    so a source that fails on its very first tile for some unrelated
    reason is never misdiagnosed as "does not accept cancel".
    """
    try:
        params = inspect.signature(source.fetch).parameters
    except (TypeError, ValueError):
        return False
    return "cancel" in params


@dataclass(frozen=True)
class SurveyRequest:
    bbox: BBox
    region: str
    site: str
    output_root: Path
    tile_size_m: float = 2000.0
    overlap_m: float = 100.0
    source_ids: Sequence[str] = ("osm", "overture")
    overture_types: Sequence[str] | None = None
    categories: Sequence[str] | None = None
    # Task 28. Source-prefixed the same way overture_types is, since it
    # belongs to one source rather than to the request as a whole. Not
    # Optional and not None-defaulted, unlike categories: there is no
    # useful difference here between "not asked about" and "asked for the
    # default", because a DEM is always fetched with exactly one model and
    # COP30 is what every run before this task used.
    elevation_demtype: str = DEFAULT_DEMTYPE
    keep_work: bool = False
    coordinate_stem: bool = False
    force: bool = False
    survey_date: date | None = None
    run_bridge_step: bool = True

    def __post_init__(self) -> None:
        # A coordinator review's Critical 1: an unrecognised category id,
        # most often a typo, used to reach osm_tag_clauses and
        # overture_types_for_categories unvalidated, where it silently
        # matched nothing rather than being rejected (see
        # UnknownCategoryError's own docstring for the live reproduction).
        # Checked here, once, at construction, so the CLI's --category and
        # the browser's checklist both get the same rejection through the
        # same code path: SurveyRequest is the one place both already meet.
        validate_categories(self.categories)
        # Task 28's elevation model, refused here for exactly the same
        # reason and in exactly the same place. The alternative was a
        # check inside ElevationSource, which would have been a second
        # validation site reached only once a download had already
        # started: OpenTopography answers an unknown demtype with an error
        # page, which this project turns into "OpenTopography did not
        # return a TIFF", a message that names neither the typo nor the
        # valid values. A saved config.json holding a bad model is caught
        # by this too, on the next estimate, because that is also a
        # SurveyRequest.
        validate_demtype(self.elevation_demtype)
        # C1, and the same ruling as validate_categories above: an
        # explicitly empty layer selection is an accident, not a request
        # for an empty package. Checked here, at the one place the CLI's
        # --source and the browser's checklist both already meet, rather
        # than in server.py alone, so neither entry point can grow a way
        # round it. len() rather than a truth test: source_ids is a
        # Sequence, and the point of this whole finding is that an empty
        # sequence must stay distinguishable from a missing one.
        if len(self.source_ids) == 0:
            raise EmptySourceSelectionError(
                "No layers are selected. At least one layer is needed."
            )

    @property
    def effective_date(self) -> date:
        return self.survey_date or date.today()

    @property
    def effective_categories(self) -> list[str]:
        """None (nothing selected, or not asked about at all) means every
        category: today's behaviour, so an existing workflow that has
        never heard of categories does not silently start returning less.
        """
        return list(self.categories) if self.categories is not None else list(ALL_CATEGORY_IDS)

    @property
    def effective_overture_types(self) -> list[str]:
        """Precedence: an explicit --overture-type wins outright (the
        CLI's own original, lower-level knob, unchanged since before
        categories existed), then a category selection maps to the
        types it implies, then the full 8-type default.

        overture_types is checked first, not merged with categories: the
        two are different ways of choosing the same thing, and letting
        both apply at once (say, --category water plus --overture-type
        building) would mean guessing whether the two are additive,
        exclusive, or something else, with no obvious right answer.
        Explicit and specific beats derived and general, matching how
        resolve_api_key's own precedence chain in mapgen.sources.elevation
        already favours a more specific, more direct source of truth.
        """
        if self.overture_types is not None:
            return list(self.overture_types)
        if self.categories is not None:
            return overture_types_for_categories(self.effective_categories)
        return list(DEFAULT_OVERTURE_TYPES)


@dataclass(frozen=True)
class SurveyResult:
    paths: PackagePaths
    complete: bool
    survey: dict[str, object]
    # Task 22: True only if a Stop request is why this run ends short of
    # complete, never for an ordinary failure. False (the default) covers
    # both a genuinely complete run and a force-tolerated failure, exactly
    # as `complete` alone always did before this field existed; every
    # existing caller that only reads `.complete` is unaffected.
    stopped: bool = False


def register_default_sources() -> None:
    """Register the three phase 1 sources, skipping only a repeat of itself.

    The CLI's main() calls this on every invocation, so a second call within
    the same process, for example a second main() call in one test session,
    must be a no-op rather than a DuplicateSourceError. That is safe only
    when the id already present is genuinely this same default source: an
    id match alone is not enough, because a foreign object of a different
    type squatting on, say, "osm" would then be silently kept in place and
    get_source("osm") would go on returning it forever. Comparing by exact
    type as well as id means a real newcomer-vs-newcomer repeat is skipped,
    while a stranger occupying the id still reaches register() and raises
    DuplicateSourceError, which is the loud failure the registry is meant to
    guarantee on a genuine id collision.
    """
    by_id = {source.id: source for source in available_sources()}
    for source in (OsmSource(), OvertureSource(), ElevationSource()):
        existing = by_id.get(source.id)
        if existing is not None and type(existing) is type(source):
            continue
        register(source)


def _coordinate_stem(bbox: BBox) -> str:
    def fmt(value: float) -> str:
        return f"{value:.7f}".rstrip("0").rstrip(".")

    return "_".join((fmt(bbox.north), fmt(bbox.south), fmt(bbox.east), fmt(bbox.west)))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan(request: SurveyRequest):
    tiles = build_tiles(request.bbox, request.tile_size_m, request.overlap_m)
    stem_override = _coordinate_stem(request.bbox) if request.coordinate_stem else None
    fingerprint = tiling_fingerprint(
        request.bbox.west,
        request.bbox.south,
        request.bbox.east,
        request.bbox.north,
        request.tile_size_m,
        request.overlap_m,
        request.effective_categories,
        request.effective_overture_types,
    )
    paths = build_package_paths(
        request.output_root,
        request.region,
        request.site,
        request.effective_date,
        fingerprint,
        stem_override=stem_override,
    )
    check_path_length(
        paths,
        request.effective_overture_types,
        source_ids=request.source_ids,
        elevation_demtype=request.elevation_demtype,
    )
    return tiles, paths


def _configured_sources(request: SurveyRequest) -> list:
    """The sources this request actually uses, each configured for this
    request's own selection rather than whatever a shared, registered
    instance happened to be constructed with.

    Task 20 found that --overture-type (and SurveyRequest.overture_types
    generally) had no effect on what was actually fetched: register_
    default_sources() builds one OvertureSource with the 8-type default
    and registers it once, and every request fetched through that same
    shared instance regardless of its own selection. The fix reads the
    registered instance back via get_source (so listing routes like
    GET /api/sources keep seeing the original, always-present instance),
    then, only for sources that expose the optional `configure` extension
    documented on LayerSource, asks for a fresh, request-scoped copy
    rather than mutating the registered one. Overture (types), osm
    (category tag filtering) and, since Task 28, elevation (which DEM
    model) each have such a per-request selection; every other source id
    is used exactly as registered, with no id-specific branch needed for
    it to keep working unchanged.
    """
    configured = []
    for source_id in request.source_ids:
        source = get_source(source_id)
        configure = getattr(source, "configure", None)
        if callable(configure):
            if source_id == "overture":
                source = configure(request.effective_overture_types)
            elif source_id == "osm":
                source = configure(request.effective_categories)
            elif source_id == "elevation":
                source = configure(request.elevation_demtype)
        configured.append(source)
    return configured


def _geometry_summary(bbox: BBox, tiles: Sequence[Tile]) -> dict[str, object]:
    """Tile count, rows, cols, extent and the tile grid itself: the
    numbers that need nothing about naming or where output will land,
    shared verbatim by estimate_survey and estimate_geometry so this
    tiling arithmetic is written exactly once.

    tile_grid (Task 22) is the actual rectangle build_tiles computed for
    each tile, keyed by the same tile_id progress events already carry,
    so the browser can draw the real tiling on the map instead of a
    number and shade a rectangle from a tile_id it never had to
    recompute itself. core_bbox, not query_bbox: core_bbox is the
    non-overlapping grid that actually tiles the extent edge to edge,
    which is what "the same grid the pipeline is using" reads as on a
    map; query_bbox tiles deliberately overlap their neighbours (see
    geo.build_tiles), and drawing those instead would show overlapping
    rectangles that do not visually tile anything.
    """
    width_m, height_m = extent_metres(bbox)
    return {
        "tiles": len(tiles),
        "rows": max((t.row for t in tiles), default=0) + 1,
        "cols": max((t.col for t in tiles), default=0) + 1,
        "extent_km": {"width": width_m / 1000.0, "height": height_m / 1000.0},
        "tile_grid": [
            {"tile_id": t.tile_id, **t.core_bbox.to_dict()} for t in tiles
        ],
    }


def estimate_geometry(bbox: BBox, tile_size_m: float, overlap_m: float) -> dict[str, object]:
    """The subset of estimate_survey's numbers that need no region, site
    or output_root at all: how many tiles a bbox and tiling produce, and
    its extent.

    Backs the web interface's live extent feedback (Task 18): a drawn or
    pasted rectangle has a real tile count and area before a region or
    site has ever been typed, and the naming/path machinery in _plan()
    would only get in the way of reporting it (it needs a region and site
    to slugify, and can raise PathTooLongError for reasons that have
    nothing to do with the geometry itself).
    """
    tiles = build_tiles(bbox, tile_size_m, overlap_m)
    return _geometry_summary(bbox, tiles)


def estimate_survey(request: SurveyRequest) -> dict[str, object]:
    tiles, paths = _plan(request)

    total_bytes = 0
    total_seconds = 0.0
    source_summaries: list[dict[str, object]] = []
    warnings: list[str] = []
    for source in _configured_sources(request):
        estimate = source.estimate(request.bbox, tiles)
        total_bytes += estimate.bytes_estimate
        total_seconds += estimate.seconds_estimate
        source_summaries.append(
            {
                "id": source.id,
                "display_name": source.display_name,
                "licence": source.licence,
                "bytes_estimate": estimate.bytes_estimate,
                "seconds_estimate": estimate.seconds_estimate,
            }
        )
        # readiness_problem is an optional LayerSource extension (see
        # sources/base.py's own docstring on this convention), read
        # defensively so a source without one, which is every source
        # except ElevationSource today, contributes nothing here. This is
        # deliberately generic rather than naming "elevation": any future
        # source with its own prerequisite gets the same pre-flight
        # warning for free.
        check_readiness = getattr(source, "readiness_problem", None)
        if callable(check_readiness):
            problem = check_readiness()
            if problem:
                warnings.append(problem)
        # routing_note is the same optional-extension convention, for a
        # different kind of thing worth telling the owner before they
        # click Download: not "cannot run at all" (readiness_problem's
        # job), but "which endpoint this run actually depends on, and
        # why" (OsmSource's own case: a category filter routes it through
        # Overpass instead of the default map API, see OsmSource.
        # configure/routing_note). Zero arguments, not passed
        # request.effective_categories: by the time a source reaches this
        # loop it has already been through _configured_sources(), so its
        # own state already reflects this request's actual routing
        # decision, not a second, separately-computed guess at it.
        check_routing_note = getattr(source, "routing_note", None)
        if callable(check_routing_note):
            note = check_routing_note()
            if note:
                warnings.append(note)

    result = dict(_geometry_summary(request.bbox, tiles))
    result["bytes_estimate"] = total_bytes
    result["seconds_estimate"] = total_seconds
    result["sources"] = source_summaries
    result["warnings"] = warnings
    # The real path build_package_paths composed for this exact request,
    # not a guess: the interface reads this straight into a folder-path
    # preview that Grasshopper depends on being right, so it must come
    # from the same call _plan() already made to plan the job itself,
    # never a second, separately-assembled path that could drift from it.
    result["folder"] = str(paths.root)
    return result


def run_survey(
    request: SurveyRequest,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
    bridge_runner=None,
) -> SurveyResult:
    """Runs a survey once, at the tiling it was asked for.

    Task 19 wrapped this in a retry loop that restarted the WHOLE run at
    the next smaller size in a fixed ladder whenever an OSM tile exceeded
    the map API's 50000-node cap. Task 26 removed it by owner ruling: a
    restart changed tile_size_m, tile_size_m is part of tiling_fingerprint
    (see _plan), and a different fingerprint is a different work_dir, so
    every tile that had already succeeded and every Overture type was
    refetched from scratch. A single dense tile in a 72-tile extent cost
    the run three times over. Density is now handled where it arises, one
    tile deep, inside OsmSource.fetch: the offending tile is split into
    quarters and recombined into its own file, so this function sees an
    ordinary tile that took longer, the fingerprint never changes, and
    nothing already downloaded is thrown away.

    One function again, not the run_survey/_run_survey_once pair Task 19
    split it into: the only reason for the inner function was that the
    outer one could call it more than once.
    """
    sink = progress if progress is not None else NullProgress()
    token = cancel if cancel is not None else CancelToken()

    tiles, paths = _plan(request)
    current_tile_ids = [t.tile_id for t in tiles]
    ensure_dir(paths.root)
    ensure_dir(paths.layers_dir)
    started_at = _now()

    sink.emit("job_started", tiles=len(tiles), root=str(paths.root))

    sources = _configured_sources(request)
    # work_dir is fingerprinted by tiling (see _plan), so the state.json
    # found here, if any, was written under this exact bbox, tile_size_m and
    # overlap_m. A different tiling gets an entirely separate work_dir and
    # can never be seen from here.
    state = JobState.load_or_create(
        paths.work_dir, current_tile_ids, [s.id for s in sources]
    )
    ensure_dir(paths.work_dir)

    outputs_by_source: dict[str, list[Path]] = {}
    # Task 30. Every source this run actually fetched, paired with the
    # work directory its tiles live in, in the order they were fetched.
    # The verify pass and the retry pass both walk this rather than
    # `sources`: a source a stop caught before its turn has nothing on
    # disk to reconcile against, and counting its tiles would report
    # pending work as though it had been checked.
    fetched: list[tuple[object, Path]] = []
    # Sources whose merge waits until after the retry pass, because they
    # came back with per-tile failures a retry might still clear. Merging
    # one of these in the loop would write a package output that is short
    # of a tile the run is about to recover, and would then have to be
    # written a second time. A source that fetched cleanly, or that failed
    # as a whole, merges in the loop exactly as it always has.
    deferred_merges: list[tuple[object, Path]] = []
    ledger = _FailureLedger()
    # Task 22: true once a Stop request has been noticed, at any of three
    # checkpoints (between sources, mid-fetch inside a source that accepts
    # `cancel`, or between the last source and the bridge). A stop is a
    # deliberate, honest partial, never treated like a failure from here
    # on: whatever was already fetched is still merged below (see the
    # Cancelled branch), no further source is attempted and the bridge is
    # skipped. See _build_survey_json for how this is told apart from an
    # ordinary failure in survey.json.
    #
    # This is the control-flow flag and not, since finding I1, what
    # survey.json reports: see reported_stopped after the loop.
    stopped = False

    # The scope always keeps the directory. Removal is decided after the job,
    # by completeness, so a partial run can always be resumed.
    with work_dir_scope(paths.work_dir, keep=True):
        for source in sources:
            if token.is_cancelled():
                # Noticed before this source ever started: nothing of
                # its own to merge, and every source after it is skipped
                # too. Earlier sources in this same run already merged
                # in their own iteration below and are untouched.
                stopped = True
                break
            source_work = paths.work_dir / "raw" / source.id
            ensure_dir(source_work)
            pending = [t for t in tiles if not state.is_done(t.tile_id, source.id)]
            # Review finding I6. A tile state.json already records as ok
            # is filtered out here, before fetch() is ever called, so the
            # source never sees it and never emits anything for it. That
            # is right (there is nothing to fetch), but it left the
            # browser with no way to know the work was already done: it
            # counts progress from events alone, and app.js's own comment
            # on the fetched/skipped split says "every tile already on
            # disk reports tile_skipped in the first second", which was
            # true when it was written and stopped being true the moment
            # a clean Stop started marking landed tiles ok (Task 22). A
            # 60-of-72 resume showed the bar climbing 0 to 17 percent and
            # then jumping to 100, with the countdown quoting the full
            # 144s for 24s of remaining work.
            #
            # Emitted here rather than inside each source because here is
            # where the skipping actually happens: a source is handed
            # `pending` and is told nothing about what was withheld from
            # it, so asking all three to re-derive that from state.json
            # would be three copies of one fact. It is the same event
            # OsmSource already emits for the other resume shape (a file
            # on disk that state.json does not record as ok, the
            # hard-kill resume), so the browser needs no new vocabulary
            # and the two resume paths finally look alike to it.
            for tile in tiles:
                if state.is_done(tile.tile_id, source.id):
                    sink.emit("tile_skipped", source=source.id, tile_id=tile.tile_id)
            fetch_succeeded = False
            per_tile_failures = False
            fetch_kwargs = {"cancel": token} if _fetch_accepts_cancel(source) else {}
            try:
                source.fetch(request.bbox, pending, source_work, sink, **fetch_kwargs)
                fetch_succeeded = True
            except Cancelled:
                # The same shape as the Urbano bridge fix (Task 20): real
                # work completed, and it must not be discarded because
                # one step was interrupted. Unlike the generic failure
                # branch below, this is never a reason to re-raise
                # regardless of request.force: whatever this source's own
                # fetch() already wrote to disk before noticing the stop
                # (every tile whose in-flight request was allowed to
                # finish; see the LayerSource protocol's own cancel
                # convention) is merged below exactly like a force-
                # tolerated partial fetch, and the loop stops after it,
                # never attempting a further source.
                stopped = True
            except Exception as exc:
                sink.emit("source_failed", source=source.id, error=str(exc))
                # No special case for a node-cap failure any more (Task
                # 26). It used to be caught here and turned into a
                # whole-run restart at a smaller tile size, ahead of and
                # regardless of request.force; OsmSource now subdivides
                # the offending tile itself, and a NodeCapExceededError
                # that still reaches this far has already been split as
                # far as splitting is allowed to go. That makes it an
                # ordinary failure of one tile, handled by force exactly
                # like any other.
                #
                # Task 30 splits this in two, on whether the source came
                # back with an ACCOUNT. A source that lists which tiles
                # failed and why (see the tile_failures convention in
                # sources/base.py) has given this run something it can act
                # on, so the decision is deferred: the failures go in the
                # ledger, the retry pass gets its turn, and only what is
                # still missing afterwards decides whether this run
                # raises. A source that failed as a whole and cannot say
                # which tiles is unchanged in every respect, including
                # re-raising immediately without --force: there is nothing
                # to retry and nothing finer to report, so waiting would
                # buy nothing and would only delay the news.
                collected = list(getattr(source, "tile_failures", None) or ())
                if collected:
                    per_tile_failures = True
                    ledger.add_tile_failures(collected)
                else:
                    ledger.set_source_error(source.id, str(exc))
                    if not request.force:
                        # Record whatever genuinely landed before re-raising: a
                        # batch call failing partway through must not blame tiles
                        # that already succeeded, or the next resume would redo
                        # work that was already done.
                        _record_tile_outcomes(
                            state, source.id, pending, source_work, fetch_succeeded,
                            current_tile_ids, sink, ledger,
                        )
                        raise

            fetched.append((source, source_work))

            if per_tile_failures:
                # Recorded now, so the verify pass and the retry pass both
                # start from a state.json that already reflects this
                # fetch. The merge waits: see deferred_merges above.
                _record_tile_outcomes(
                    state, source.id, pending, source_work, fetch_succeeded,
                    current_tile_ids, sink, ledger,
                )
                deferred_merges.append((source, source_work))
                continue

            # merge() must see every output this source has ever produced for
            # this package, not just what fetch() returned from this call.
            # pending is only the tiles that still needed work, and fetch()
            # only returns paths for the tiles it was asked for, so using its
            # return value directly would silently drop every tile that had
            # already succeeded on an earlier, resumed-from run.
            parts = _existing_output_files(source_work, current_tile_ids)

            # Refuse to merge a tile set with holes unless the caller forced
            # it, or the run was stopped: a stop tolerates a partial tile
            # set for the same reason force does (whatever is missing is
            # missing on purpose, not because anything is broken), so it
            # is treated the same way here rather than needing its own
            # separate leniency check.
            try:
                parts = assert_inputs_present(parts, force=request.force or stopped)
            finally:
                # stopped, not force, decides whether a tile lacking
                # output here reads FAILED or PENDING: this is the one
                # call site reached by a Cancelled-interrupted fetch (see
                # _record_tile_outcomes's own docstring for why that
                # implies every such tile was genuinely never attempted,
                # not attempted and found wanting).
                _record_tile_outcomes(
                    state,
                    source.id,
                    pending,
                    source_work,
                    fetch_succeeded,
                    current_tile_ids,
                    sink,
                    ledger,
                    stopped=stopped,
                )
            merged = source.merge(parts, paths.root, paths.stem)
            outputs_by_source[source.id] = merged
            sink.emit("source_done", source=source.id, outputs=[p.name for p in merged])

            if stopped:
                # This source's own partial output is merged above; no
                # further source is attempted once a stop has landed.
                break

        if not stopped and token.is_cancelled():
            # Cancelled between the last source finishing and this check:
            # the same checkpoint this always had before the bridge step,
            # resolved the same way as every other stop rather than
            # raising past this point.
            stopped = True

        # Task 30, sections 3 and 4, in the order the owner asked for
        # them: confirm every tile really is there, retry the ones a
        # retry could fix, confirm again.
        #
        # The retry is here, at the end of the run, rather than inline
        # after each source's own fetch, and the reason is time. A tile
        # that failed early is retried once every other tile has been
        # fetched, which on a real extent is minutes later, and minutes
        # are what a rate limit window or a service having a bad minute
        # actually need. Retrying it immediately would ask the same
        # question of the same unhappy service two seconds later. See
        # RETRY_PASS_BUDGET for how this layer relates to the four
        # attempts OsmSource already makes inside a single tile fetch.
        verified = _verify_tiles(
            state, fetched, tiles, current_tile_ids, ledger, sink, "after_fetch"
        )
        for retry_pass in range(1, RETRY_PASS_BUDGET + 1):
            if stopped or token.is_cancelled():
                # A stop skips the retry entirely. The tiles it leaves
                # failed stay failed and are reported; they are not
                # quietly downgraded to pending, because they were
                # genuinely attempted and genuinely did not arrive.
                stopped = True
                break
            retryable = [
                record
                for record in verified["failures"]
                if record.get("kind") in RETRYABLE_FAILURE_KINDS
            ]
            if not retryable:
                break
            stopped = _retry_failed_tiles(
                retryable, fetched, tiles, current_tile_ids, state, request,
                token, sink, ledger, retry_pass,
            ) or stopped
            verified = _verify_tiles(
                state, fetched, tiles, current_tile_ids, ledger, sink, "after_retry"
            )

        # Without --force, tiles still missing after all of that end the
        # run, exactly as the first failing tile used to. What changed is
        # everything before this line: every recoverable tile is now on
        # disk, and survey.json (written below, before this is raised)
        # carries the full account of what is not. The raise happens after
        # the record is written, so an unforced failure leaves a package
        # that explains itself rather than a folder of scratch files.
        #
        # Scoped to the sources that actually FAILED, deliberately, and
        # not to every tile the verify pass found short. Those are not the
        # same set, and the difference is a behaviour this task must not
        # change: a source whose fetch() returns cleanly while writing
        # nothing has always left its tiles recorded failed and the run
        # recorded incomplete, WITHOUT raising, because nothing raised.
        # (test_a_source_that_writes_nothing_is_marked_incomplete_not_ok
        # pins exactly that.) Raising there would be this task inventing a
        # new failure mode for an unforced run under cover of preserving
        # an old one. What raises is what raised before: a layer that
        # reported a failure, and is still short of tiles after the retry.
        failed_layer_ids = {source.id for source, _ in deferred_merges}
        unresolved = [
            record
            for record in verified["failures"]
            if record.get("source") in failed_layer_ids
        ]
        unrecoverable = bool(unresolved) and not request.force and not stopped

        for source, source_work in deferred_merges:
            if unrecoverable:
                # No merged output for a source whose tiles are still
                # missing on an unforced run, which is what happened
                # before this task too: the first failing tile raised out
                # of fetch() and nothing was merged for that layer.
                break
            parts = _existing_output_files(source_work, current_tile_ids)
            parts = assert_inputs_present(parts, force=request.force or stopped)
            merged = source.merge(parts, paths.root, paths.stem)
            outputs_by_source[source.id] = merged
            sink.emit("source_done", source=source.id, outputs=[p.name for p in merged])

        # Review finding I1, and the handling both survey.json's own
        # `stopped` comment and README's schema table already claimed was
        # here without it ever being written. `stopped` above is the
        # CONTROL FLOW flag: it is what breaks the source loop and skips
        # the bridge, and it is set by any stop at any of the three
        # checkpoints, including one that lands after every tile has
        # genuinely finished. What survey.json reports is narrower, and
        # is the published invariant: stopped means a stop is the reason
        # this run is short. A run that is not short has nothing to be
        # honest about here, which is exactly how JobManager's own worker
        # has always read the same race (it checks result.complete first
        # and reports "done"), so this is survey.json being made to agree
        # with the state the browser already shows for the same run
        # rather than a new rule.
        reported_stopped = stopped and not state.complete

        layer_files = _write_layer_files(outputs_by_source, paths)

        # Swept whenever the DATA is complete, stop or no stop, and the
        # "not stopped" half of this gate is what finding I1 removed. The
        # old reasoning was that a stop's stale outputs are "deferred to a
        # later, ordinary run rather than risking removing something a
        # resume might still want". There is no later run: naming.
        # _survey_reports_complete reads complete: true, so
        # build_package_paths refuses to reuse this folder and the next
        # survey of the same site and date lands on _02. Skipping the
        # sweep here left an earlier, wider attempt's <stem>_water.geojson
        # in a package that names no water source, permanently, in a
        # folder the owner reads straight into Grasshopper. Nothing about
        # a complete run's sweep is made riskier by a stop having landed:
        # every file this run produced is in `produced`, and the candidate
        # list is still only each source's own possible_outputs. The
        # work_dir cleanup twenty lines below has always been gated on
        # state.complete alone, for this same reason, spelled out in its
        # own comment.
        if state.complete:
            produced = {
                merged.resolve()
                for outputs in outputs_by_source.values()
                for merged in outputs
            }
            produced.update(layer.resolve() for layer in layer_files)
            _sweep_stale_outputs(produced, paths, sink)

        # Urbano is one product of a survey among several (the OSM/Overture/
        # elevation data on disk are the others). A missing or failing
        # Urbano install must not destroy those: the failure is recorded
        # below, in survey.json and through the progress sink, and the
        # package is still finished. See Task 20 finding 1: on the owner's
        # machine, with no Urbano installed, this branch fails on every
        # single run, and it used to take the whole survey down with it.
        #
        # Never attempted on a stopped run (Task 22): the owner asked this
        # run to stop, and starting another external process, on data that
        # may itself be partial, works against "stop must actually stop,
        # promptly" for no benefit a subsequent resume does not already
        # provide once the survey data itself is complete. Recorded the
        # same honest way a --skip-bridge run already is: attempted=False,
        # ok=None, not a fabricated failure.
        #
        # Gated on the control-flow `stopped`, deliberately NOT on
        # reported_stopped above: a Stop press means "do not start another
        # external process", and that is true whether or not the tiles
        # happened to have all landed by the time it arrived. The
        # consequence is worth naming, because it is the one thing finding
        # I1's sweep change does not also settle: a complete run that a
        # stop caught at the very end keeps its data and its sweep, and
        # never gets its Urbano files, since the folder is complete and so
        # is never revisited. survey.json says so in the only way that
        # matters, attempted=False, exactly as --skip-bridge does; running
        # the bridge anyway would be this function deciding a Stop means
        # something narrower than the owner pressed it to mean.
        #
        # Never attempted on an unforced run that is ending short either
        # (Task 30). That run is about to raise, exactly as it would have
        # raised from inside the source loop before this task, and it
        # never reached the bridge then. Starting an external process on
        # data mapgen is in the middle of refusing to stand behind would
        # be a new behaviour, not a preserved one.
        bridge_attempted = request.run_bridge_step and not stopped and not unrecoverable
        bridge_ok: bool | None = None
        bridge_error: str | None = None
        if bridge_attempted:
            osm_outputs = outputs_by_source.get("osm", [])
            elevation_outputs = outputs_by_source.get("elevation", [])
            bridge_ok, bridge_error = _run_bridge_step(
                bbox=request.bbox,
                root=paths.root,
                file_name_stem=None if request.coordinate_stem else paths.stem,
                osm_file=osm_outputs[0] if osm_outputs else None,
                elevation_file=elevation_outputs[0] if elevation_outputs else None,
                sink=sink,
                bridge_runner=bridge_runner,
            )

    survey = _build_survey_json(
        request,
        paths,
        tiles,
        sources,
        state,
        started_at,
        bridge_attempted,
        bridge_ok,
        bridge_error,
        reported_stopped,
        outputs_by_source,
        verified,
    )
    atomic_write_text(paths.survey_json, json.dumps(survey, indent=2))
    # reported_stopped here too, not the control-flow flag: the browser
    # reads this event and survey.json for the same run, and the one thing
    # they must never do is disagree about whether it was stopped.
    sink.emit(
        "job_finished",
        complete=state.complete,
        stopped=reported_stopped,
        root=str(paths.root),
    )

    # Removed only on a clean, complete run. A failed or partial job keeps its
    # tiles, because that is what makes the next run resume rather than restart.
    # The whole _work/ parent goes, not just this tiling's fingerprint
    # subdirectory: a complete root is never reused (a later request lands on
    # a fresh _02), so a sibling tiling's abandoned scratch tree left inside
    # _work/ would otherwise survive forever with nothing left to remove it.
    #
    # Gated on state.complete alone, not also on stopped: if every tile
    # genuinely did finish (state.complete is True) despite a stop landing
    # right at the end (only the bridge step was actually skipped), there
    # is nothing left to resume, and _work/ is dead weight exactly as it
    # would be on any other complete run.
    if state.complete and not request.keep_work:
        best_effort_rmtree(paths.work_dir.parent)

    if unrecoverable:
        # After survey.json and job_finished, never before: the record of
        # what happened is part of what this run produced, and a caller
        # that catches this needs the folder to already make sense. The
        # message is the same account describe_tile_failures composed for
        # the file and for the terminal.
        raise IncompleteSurveyError(
            "\n".join(
                describe_tile_failures(survey["tile_failures"])
                + [
                    "Nothing was merged for the layers that are short. Run the "
                    "survey again over the same extent to pick up where it "
                    "stopped, or pass --force to package what did arrive and "
                    "record the rest as missing."
                ]
            )
        )

    return SurveyResult(
        paths=paths, complete=state.complete, survey=survey, stopped=reported_stopped
    )


def _run_bridge_step(
    bbox: BBox,
    root: Path,
    file_name_stem: str | None,
    osm_file: Path | None,
    elevation_file: Path | None,
    sink: ProgressSink,
    bridge_runner=None,
) -> tuple[bool, str | None]:
    """One attempt at the Urbano bridge, reported as (ok, error).

    The ONLY place in mapgen that calls run_bridge. Task 29 hoisted it out
    of run_survey rather than letting `mapgen bridge` grow a second call
    site of its own: the two would then have had to be kept agreeing about
    which exceptions are survivable, which progress events are emitted, and
    which BridgeRequest fields are filled, forever, with nothing but
    vigilance holding them together. Every decision that used to be inline
    in run_survey is now made here, once, for both callers.

    Never raises for a bridge that ran and failed. BridgeError covers a
    missing project or a non-zero exit (today's case on the owner's
    machine: Urbano.Core.dll/ProjectSetup.dll absent, so the bridge process
    itself runs and fails); OSError also covers dotnet itself being missing
    from PATH, which raises from the subprocess call rather than from
    bridge.py. Both are already plain, one-line messages, never a
    traceback, and both are returned as `error` rather than propagated,
    which is Task 20's ruling: real work already on disk is never discarded
    because the bridge step failed.

    skip_elevation follows the file, not the selection: a package with no
    DEM on disk has nothing to hand Urbano whatever its survey.json asked
    for, and telling the bridge to process an elevation file that is not
    there is the one way this call could fail for a reason nobody wants
    explained.
    """
    sink.emit("bridge_started")
    request = BridgeRequest(
        bbox=bbox,
        output_dir=root,
        file_name_stem=file_name_stem,
        osm_file_path=osm_file,
        elevation_tiff_path=elevation_file,
        skip_elevation=elevation_file is None,
    )
    try:
        if bridge_runner is None:
            run_bridge(request)
        else:
            run_bridge(request, runner=bridge_runner)
    except (BridgeError, OSError) as exc:
        error = str(exc)
        sink.emit("bridge_failed", error=error)
        return False, error
    sink.emit("bridge_done")
    return True, None


class UnbridgeablePackageError(ValueError):
    """Raised when `mapgen bridge` is pointed at something it must refuse.

    A ValueError subclass for the same reason every other refusal in this
    project is one (see EmptySourceSelectionError): cli.py names its
    exceptions explicitly and prints str(exc) as one plain line, and
    server.py's _REQUEST_VALUE_ERRORS already turns a ValueError into a
    clean 400 if this ever reaches a route, which today it does not.

    Every message this carries names the folder it is talking about and
    what to do next, because the two situations it exists for are exactly
    the two an owner reaches by accident: pointing it at the region folder
    rather than the package inside it, and pointing it at a package a run
    never finished.
    """


# The two survey.json source ids whose merged output the bridge takes as a
# FILE input. Overture is deliberately not among them: the bridge fetches
# its own buildings geoparquet on its own account (see README, "Using the
# output in Grasshopper"), so a package's <stem>_building.geojson is not
# something it is ever handed, and requiring one would refuse packages the
# bridge can process perfectly well.
_BRIDGE_FILE_INPUT_IDS = ("osm", "elevation")


def _bridge_input_file(root: Path, stem: str, source_id: str) -> tuple[Path | None, list[str]]:
    """The merged file this source left in the package root, or, if none of
    them is there, the names it could have had.

    Names come from the source's own possible_outputs(stem), the same
    closed list the stale-output sweep uses, rather than from a second copy
    of "osm writes <stem>.osm" kept here. package.py already knows the
    source IDS it has to reason about; what it must not start knowing
    separately is their filenames, because two copies of that fact drift
    and the failure when they do is a bridge quietly handed nothing.
    """
    try:
        source = get_source(source_id)
    except UnknownSourceError:
        raise UnbridgeablePackageError(
            f"This package's survey.json names the {source_id} layer, which this "
            f"version of mapgen does not have, so the file the Urbano bridge needs "
            f"from it cannot be identified."
        ) from None
    possible = getattr(source, "possible_outputs", None)
    if not callable(possible):
        raise UnbridgeablePackageError(
            f"The {source_id} layer does not declare which files it writes, so the "
            f"file the Urbano bridge needs from it cannot be identified."
        )
    names = [str(name) for name in possible(stem)]
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate, []
    return None, names


def _survey_payload(survey_json: Path) -> dict:
    """survey.json as a dict, or a refusal saying which of the three ways
    it was unusable.

    A missing file and a corrupt one are told apart deliberately, unlike in
    naming._survey_reports_complete, which collapses both to "not
    complete": that function only has to decide whether a folder is safe to
    reuse, and both answers are the same there. Here they mean different
    things to the owner. Missing means the run never got far enough to
    write one, and a resume is the answer. Corrupt means the record of a
    package that may be entirely intact has been damaged, and no amount of
    resuming fixes that by itself.
    """
    if not survey_json.is_file():
        raise UnbridgeablePackageError(
            f"There is no survey.json in {survey_json.parent}, so no survey ever "
            f"finished there. Run the survey again over the same extent: an "
            f"unfinished package needs a resume, which picks up whatever is "
            f"already on disk, not this command."
        )
    try:
        payload = json.loads(survey_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnbridgeablePackageError(
            f"{survey_json} could not be read as a survey record: {exc}"
        ) from None
    if not isinstance(payload, dict):
        raise UnbridgeablePackageError(
            f"{survey_json} does not contain a survey record."
        )
    return payload


def _survey_bbox(payload: dict, survey_json: Path) -> BBox:
    """The extent this package was surveyed over, read from its own record.

    The bridge needs a bbox and nothing else in survey.json implies one:
    Urbano's world origin is built from it (see UrbanoBridge's Program.cs),
    so a wrong one here is a package whose Urbano geometry sits in the
    wrong place, which is the failure that would be hardest to notice.
    Never re-derived from the folder name, which carries a site and a date
    and no coordinates at all.
    """
    raw = payload.get("bbox")
    if not isinstance(raw, dict):
        raise UnbridgeablePackageError(
            f"{survey_json} records no bbox, so the extent the Urbano bridge "
            f"needs cannot be recovered from it."
        )
    try:
        return BBox(
            west=float(raw["west"]),
            south=float(raw["south"]),
            east=float(raw["east"]),
            north=float(raw["north"]),
        ).validated()
    except (KeyError, TypeError, ValueError) as exc:
        raise UnbridgeablePackageError(
            f"{survey_json} records an unusable bbox: {exc}"
        ) from None


def _survey_source_ids(payload: dict) -> list[str]:
    """The source ids this package's own record says it holds.

    Read from `sources` rather than from what is lying in the folder: the
    folder is what a stale file from an earlier, wider attempt also lives
    in, and survey.json is the record of what this package actually is.
    """
    entries = payload.get("sources")
    if not isinstance(entries, list):
        return []
    return [
        entry["id"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]


def _every_elevation_tile_failed(payload: dict) -> bool:
    """Whether this package's own record positively states that its
    elevation layer was attempted and did not arrive.

    Task 30, section 6. `mapgen bridge` refuses a package that is short of
    an input, which is right, and Task 29's implementer flagged that it is
    therefore stricter than run_survey in one case the owner will
    certainly hit: elevation is the only keyed source, so it fails
    whenever the key is missing, wrong, rate limited or the service is
    down, and run_survey bridges around that with skip_elevation while
    this command used to refuse outright. Refusing leaves the owner with a
    package they can never produce Urbano files for without downloading
    the whole thing again, which is the exact problem this command exists
    to solve.

    The narrowing is Task 29's own proposal and it is the safe one: skip
    elevation only when `tiles` records EVERY elevation tile as failed.
    That is the package stating, in its own record, that the layer was
    tried and did not arrive. A file the owner moved or deleted, or the
    wrong folder passed, produces nothing of the kind, and those are still
    refused. So is a mixture, where some tiles succeeded: a package whose
    DEM half arrived and then vanished from the root is not explained by
    its record, and guessing there is how a _project_setting.json that
    reads as complete goes into Grasshopper missing a layer.

    Deliberately requires at least one tile record. An empty or absent
    `tiles` list says nothing at all, and "no evidence against" is not the
    same as "positively stated", which is the whole standard this function
    exists to apply.
    """
    records = payload.get("tiles")
    if not isinstance(records, list) or not records:
        return False
    for record in records:
        if not isinstance(record, dict) or record.get("elevation") != FAILED:
            return False
    return True


def bridge_package(
    package_dir: Path | str,
    progress: ProgressSink | None = None,
    bridge_runner=None,
) -> dict:
    """Run the Urbano bridge, and only the bridge, over a package that
    already exists on disk. Returns that package's updated survey.json.

    Task 29. Two situations share this one command, and both are ordinary
    rather than exotic:

      * The owner has no Urbano install, so the bridge has failed on every
        run they have ever made and every package they own is missing its
        _project_setting.json. Without this, installing Urbano would mean
        downloading every one of those packages again.
      * A Stop that lands after the last tile has finished leaves a package
        with complete: true and bridge.attempted: false (Task 22's ruling
        that a stop starts no further external process, kept deliberately
        by review finding I1). A complete folder is never reused, so
        surveying the same site and date again produces an _02 and refetches
        everything. This is what makes that ruling affordable.

    What it will not do is decide anything about the download. `complete`,
    `stopped`, `tiles` and `sources` describe a run that happened at some
    point in the past and this command was not there; they are read and
    never written. The only key it changes is `bridge`.
    """
    sink = progress if progress is not None else NullProgress()
    root = Path(package_dir)
    if not root.is_dir():
        raise UnbridgeablePackageError(
            f"There is no folder at {root}. Point mapgen bridge at a survey "
            f"package folder, the one holding survey.json, not at the region "
            f"folder above it."
        )

    survey_json = root / "survey.json"
    payload = _survey_payload(survey_json)
    stem = payload.get("urbano_stem")
    if not isinstance(stem, str) or not stem:
        raise UnbridgeablePackageError(
            f"{survey_json} records no urbano_stem, so the names of the files in "
            f"this package cannot be recovered from it."
        )
    bbox = _survey_bbox(payload, survey_json)
    source_ids = _survey_source_ids(payload)

    # Each file input resolved before the bridge is started, never during,
    # so a package that is short of one is refused without a dotnet process
    # ever being launched at it.
    found: dict[str, Path] = {}
    missing: list[tuple[str, list[str]]] = []
    for source_id in _BRIDGE_FILE_INPUT_IDS:
        if source_id not in source_ids:
            continue
        path, names = _bridge_input_file(root, stem, source_id)
        if path is None:
            if source_id == "elevation" and _every_elevation_tile_failed(payload):
                # The package says this layer was attempted and did not
                # arrive, so its absence is explained rather than
                # suspicious. Bridged without it, exactly as run_survey
                # does for the run that produced it (skip_elevation
                # follows the file, in _run_bridge_step). Not extended to
                # osm: a missing <stem>.osm has no benign reading, and
                # nothing about a failed OSM layer makes the Urbano
                # geometry worth producing without it.
                sink.emit("bridge_skipping_elevation")
                continue
            missing.append((source_id, names))
        else:
            found[source_id] = path
    if missing:
        # Refused rather than quietly bridged without it. run_survey does
        # go ahead in this shape, and it is right to: it has just watched
        # that source fail in this same run and knows the file is absent
        # because it never arrived. This command knows nothing of the kind.
        # An absent file here is equally a package whose layer failed, a
        # file the owner moved, and a folder that is not the one they
        # meant, and the difference between them is not recoverable from
        # the folder. Guessing produces a _project_setting.json that reads
        # as complete, goes straight into Grasshopper, and is silently
        # missing a layer the package's own survey.json says it holds.
        layers = " and ".join(source_id for source_id, _ in missing)
        files = " and ".join(
            " or ".join(names) if names else "its merged output"
            for _, names in missing
        )
        layer_word = "layer" if len(missing) == 1 else "layers"
        file_word = "is" if len(missing) == 1 else "are"
        raise UnbridgeablePackageError(
            f"Cannot run the Urbano bridge over {root}: survey.json names the "
            f"{layers} {layer_word}, but {files} {file_word} not in the package. "
            f"Run the survey again over the same extent to fetch what is missing, "
            f"then run this again."
        )

    ok, error = _run_bridge_step(
        bbox=bbox,
        root=root,
        # The package's own recorded stem, always, so every file the bridge
        # writes carries the same name as everything already in the folder
        # and as naming.PackagePaths.project_setting predicts. run_survey
        # passes None instead for a --coordinate-stem run, letting
        # UrbanoBridge derive the same coordinate string itself; the two
        # agree for every package that flag can produce except a suffixed
        # one, where run_survey's own bridge output is already named
        # differently from the project_setting path mapgen goes on to
        # report (see the task 29 report). Reading the record is the
        # honest half of that disagreement.
        file_name_stem=stem,
        osm_file=found.get("osm"),
        elevation_file=found.get("elevation"),
        sink=sink,
        bridge_runner=bridge_runner,
    )

    payload["bridge"] = _rebridged_block(payload.get("bridge"), ok, error, _now())
    atomic_write_text(survey_json, json.dumps(payload, indent=2))
    return payload


def _rebridged_block(
    existing: object, ok: bool, error: str | None, ran_at: str
) -> dict[str, object]:
    """The `bridge` block after a `mapgen bridge` run, keeping the download's
    own record of the same field intact.

    attempted, ok and error stay exactly where a reader already looks for
    them and go on meaning the same thing: whether this package has Urbano
    files, and why not if it has none. That is the question survey.json is
    read for, and the freshest answer is the true one.

    What would have been a lie is leaving it there alone. Before this
    command existed the block could only ever describe the download itself,
    and every sentence written about it, in this file and in README, said
    so. A later success written over an earlier failure would have made the
    file claim the bridge succeeded during a run where it demonstrably did
    not, on a machine that at the time had no Urbano on it at all.

    So two fields carry what the three cannot:

      * ran_at, the UTC time of the attempt attempted/ok/error describe.
        Its ABSENCE is the signal, and it is why nothing was added to
        run_survey's own block: no ran_at means the block describes the
        download, which is what every package written before this task
        already means and what every ordinary run still means.
      * during_download, the untouched attempted/ok/error the download
        itself wrote. Copied verbatim, including a null, and never
        rewritten by a second `mapgen bridge` run: it is the ORIGINAL that
        must survive, not the previous one.

    None if the package's record had no usable bridge block at all, which
    is not the same as false: it says nothing was recorded, rather than
    claiming a run that nothing witnessed.
    """
    block = existing if isinstance(existing, dict) else {}
    if "during_download" in block:
        during_download = block["during_download"]
    elif block:
        during_download = {
            "attempted": block.get("attempted"),
            "ok": block.get("ok"),
            "error": block.get("error"),
        }
    else:
        during_download = None
    return {
        "attempted": True,
        "ok": ok,
        "error": error,
        "ran_at": ran_at,
        "during_download": during_download,
    }


_TILE_ID_SHAPE = re.compile(r"^r\d+_c\d+$")


def _existing_output_files(source_work: Path, current_tile_ids: Sequence[str]) -> list[Path]:
    """Every real file already on disk for this source that still belongs to
    the current plan, crash debris and stale tiling excluded.

    A hard kill can leave one of fsutil's atomic-write .part temp files
    behind, and OvertureSource downloads to the same kind of .part path
    before renaming it into place; neither is usable output, and both are
    excluded outright.

    A tile id is only (row, col): the string itself carries no memory of the
    tile_size_m or overlap_m it was computed under. A file named after a
    tile id from a different tiling can therefore share its name with a
    current tile without covering the same ground, so anything shaped like a
    tile id that is not in current_tile_ids is excluded too. A file that is
    not shaped like a tile id at all, such as elevation's single whole-area
    TIFF, carries no tiling assumption to begin with and always belongs.
    current_tile_ids is passed in by the caller, which already knows the
    current plan, rather than inferred here, so this stays correct
    regardless of what older runs left on disk.

    A subdirectory whose name starts with an underscore is a source's own
    private scratch, not output, and everything under it is excluded
    whatever it is named (Task 26). OsmSource keeps the quarters of a
    subdivided tile in one, and they are exactly the kind of file this
    function must not hand on: they are real .osm data, but they are
    PARTS of a tile whose own file may not exist yet, and merging them
    into the package would put a half-fetched tile's ground into the
    finished output with nothing recording that it is partial. They are
    also not shaped like a tile id (see geo.split_tile_into_quarters:
    r00_c00_q10, never r00_c00), so the tile-id rule above would have
    admitted them rather than rejected them, which is why this needs a
    rule of its own.

    That rule, and NOT a change to _TILE_ID_SHAPE, deliberately: the regex
    is a safety mechanism against stale tilings, and every change to it
    either lets more through, which is the failure it exists to prevent,
    or excludes more real output. The underscore rule can only ever
    exclude, it excludes nothing any source writes today (osm writes
    rNN_cNN.osm flat, overture <type>.geojson flat, elevation one TIFF),
    and it leaves the stale-tiling property exactly as it was: a file that
    would have been rejected for its name is still rejected for its name.
    """
    current_ids = set(current_tile_ids)
    kept: list[Path] = []
    for path in sorted(source_work.rglob("*")):
        if not path.is_file() or path.suffix == ".part":
            continue
        if any(part.startswith("_") for part in path.relative_to(source_work).parts[:-1]):
            continue
        if _TILE_ID_SHAPE.match(path.stem) and path.stem not in current_ids:
            continue
        kept.append(path)
    return kept


class _FailureLedger:
    """Every reason this run has for a tile not being here, kept in one
    place so the progress log, survey.json and the terminal cannot end up
    telling the owner three different stories (Task 30, section 5).

    Two kinds of reason go in, and they come from different places:

      * A per-tile reason, composed by the source that failed, which is
        the only thing that knows what the service actually said. These
        arrive through the source's own tile_failures list.
      * A source-level reason, for a layer that failed as a whole and
        cannot say which tiles: str(exc) from its fetch(). Elevation is
        the one that matters, because it is the only keyed source, so
        "Failed to download DEM: HTTP 401" is the sentence the owner will
        actually meet. It is already redacted by the time it gets here
        (mapgen.sources.elevation does that at its own boundary, which is
        the only place that has ever held the key), and nothing in this
        module ever composes a reason out of a URL.

    A tile with neither still gets a record. That is the point: the whole
    task exists because a run that quietly produces less than asked for is
    the failure mode to remove, so "no reason was given" is written down
    as a reason rather than left as a gap.
    """

    def __init__(self) -> None:
        self._tiles: dict[tuple[str, str], dict[str, object]] = {}
        self._source_errors: dict[str, str] = {}
        self._retries: dict[tuple[str, str], int] = {}

    def add_tile_failures(self, failures: Sequence[TileFailure]) -> None:
        for failure in failures:
            self._tiles[(failure.source, failure.tile_id)] = failure.to_record()

    def set_source_error(self, source_id: str, error: str) -> None:
        self._source_errors[source_id] = error

    def note_retry(self, source_id: str, tile_ids: Sequence[str]) -> None:
        for tile_id in tile_ids:
            key = (source_id, tile_id)
            self._retries[key] = self._retries.get(key, 0) + 1

    def forget(self, source_id: str, tile_id: str) -> None:
        """Drop a tile's reason once it is genuinely on disk.

        Called only by the verify pass, and only for a tile it has just
        reconciled to ok. A retried tile that succeeded must not keep the
        sentence explaining why it once did not, or a later reader would
        find an explanation attached to something that is not a problem.
        """
        self._tiles.pop((source_id, tile_id), None)

    def record_for(self, source_id: str, tile_id: str) -> dict[str, object]:
        record = self._tiles.get((source_id, tile_id))
        if record is None:
            source_error = self._source_errors.get(source_id)
            if source_error:
                record = {
                    "source": source_id,
                    "tile_id": tile_id,
                    "kind": FAILURE_UNKNOWN,
                    "reason": source_error,
                }
            else:
                record = {
                    "source": source_id,
                    "tile_id": tile_id,
                    "kind": FAILURE_NO_OUTPUT,
                    "reason": (
                        "The layer finished without leaving a file for this tile, "
                        "and gave no reason."
                    ),
                }
        return {**record, "retried": self._retries.get((source_id, tile_id), 0)}


def describe_tile_failures(records: Sequence[Mapping[str, object]]) -> list[str]:
    """The account of what did not arrive, as plain lines.

    One composer for all three places the owner can meet this (Task 30,
    section 5): the terminal summary of a forced run, the message
    IncompleteSurveyError carries out of an unforced one, and the browser,
    which reads the same records out of survey.json. Three separately
    written versions of this would drift, and the one thing they must
    never do is disagree about which tiles are missing.

    Sorted by source then tile id, so the same run always reads the same
    way, rather than in whatever order the failures happened to land.
    """
    if not records:
        return []
    count = len(records)
    noun = "tile" if count == 1 else "tiles"
    lines = [f"{count} {noun} did not download:"]
    for record in sorted(
        records, key=lambda r: (str(r.get("source", "")), str(r.get("tile_id", "")))
    ):
        retried = record.get("retried") or 0
        again = " Retried, and it failed again." if retried else ""
        lines.append(
            f"  {record.get('source')} {record.get('tile_id')}: "
            f"{record.get('reason')}{again}"
        )
    return lines


def _tile_has_output(
    files: Sequence[Path], tile_stamped_dirs: set[Path], tile_id: str
) -> bool | None:
    """What the DISK says about one tile, as True, False, or "cannot say".

    Extracted from _record_tile_outcomes (Task 30) so the verify pass
    reaches exactly the same verdict from exactly the same code. Two
    separately written answers to "is this tile actually here" is how a
    belt and a braces end up disagreeing, and the verify pass exists
    precisely to be a second opinion on the first one, which is worth
    nothing if it is a different opinion for a different reason.

    None, and not False, when a source keeps no tile-stamped files at all
    (elevation's single whole-area TIFF, and Overture's one file per type
    since Task 23): there is genuinely no per-tile fact on disk to read,
    and inventing one would be fabricating a denominator. The caller
    decides what to do with that; _record_tile_outcomes falls back on the
    batch outcome, and the verify pass leaves such a tile exactly as it
    found it.

    False when this source does keep tile-stamped files and this tile has
    none, or has one in only some of the directories it should be in.
    """
    if tile_stamped_dirs:
        return all(
            any(
                path.parent == directory
                and path.stem == tile_id
                and path.stat().st_size > 0
                for path in files
            )
            for directory in tile_stamped_dirs
        )
    if files:
        return None
    return False


def _verify_tiles(
    state: JobState,
    fetched: Sequence[tuple[object, Path]],
    tiles: Sequence[Tile],
    current_tile_ids: Sequence[str],
    ledger: _FailureLedger,
    sink: ProgressSink,
    phase: str,
) -> dict[str, object]:
    """Reconcile what this run RECORDED against what is genuinely on disk,
    for every planned tile of every source it fetched, and say so.

    Task 30, section 3, and the owner's own words: "we should be doing a
    verify at the end of the run to confirm all tiles are there". It is
    the belt to _record_tile_outcomes' braces. That function is called
    once per source, from inside the fetch loop, and reasons about the
    tiles that source was handed; this walks every planned tile of every
    source afterwards, from a standing start, and believes only the
    filesystem.

    It CORRECTS rather than merely reports, because survey.json is the
    package's record of itself and the owner is expected to trust it. A
    record that says ok for a tile with no file is the file lying, and
    leaving the lie in place while noting it elsewhere would be a worse
    outcome than either fixing it or not noticing.

    Three rules, and the third is the one that matters most:

      * A file is there and the record does not say ok: the record is
        raised to ok. This is how a tile whose retry succeeded gets its
        state, and how a file written by a run that died before saving
        state.json is recognised.
      * The record says ok and no file is there: the record is lowered to
        failed, and tile_failed is emitted for it. This is the belt.
      * No file, and the record does NOT say ok: left exactly as it is. A
        tile a stop never reached reads pending and stays pending; this
        pass never invents a failure for work that was never attempted.
        Task 22's ruling is a ruling about what "failed" means, and it
        holds here for the same reason it holds there: red must mean a
        real attempt came up short, or the owner stops believing it.

    A source with no tile-stamped output at all (elevation, Overture) is
    walked and left alone, per _tile_has_output's None: there is no
    per-tile fact on disk to check against. Said plainly rather than
    quietly skipped, because "verified" would otherwise imply more than
    was actually done.
    """
    current = set(current_tile_ids)
    counts = {OK: 0, FAILED: 0, PENDING: 0}
    corrections: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []

    for source, source_work in fetched:
        source_id = source.id
        files = _existing_output_files(source_work, current_tile_ids)
        tile_stamped_dirs = {path.parent for path in files if path.stem in current}
        for tile in tiles:
            recorded = state.status(tile.tile_id, source_id)
            present = _tile_has_output(files, tile_stamped_dirs, tile.tile_id)
            resolved = recorded
            if present is True and recorded != OK:
                resolved = OK
            elif present is False and recorded == OK:
                resolved = FAILED
            if resolved != recorded:
                state.mark(tile.tile_id, source_id, resolved)
                corrections.append(
                    {
                        "source": source_id,
                        "tile_id": tile.tile_id,
                        "was": recorded,
                        "now": resolved,
                    }
                )
                if resolved == FAILED:
                    ledger.add_tile_failures(
                        [
                            TileFailure(
                                source=source_id,
                                tile_id=tile.tile_id,
                                kind=FAILURE_NO_OUTPUT,
                                reason=(
                                    "This tile was recorded as downloaded, but no "
                                    "file for it is on disk."
                                ),
                            )
                        ]
                    )
                    sink.emit(
                        "tile_failed",
                        source=source_id,
                        tile_id=tile.tile_id,
                        **_failure_event_fields(ledger, source_id, tile.tile_id),
                    )
            if resolved == OK:
                ledger.forget(source_id, tile.tile_id)
            else:
                if resolved == FAILED:
                    failures.append(ledger.record_for(source_id, tile.tile_id))
            counts[resolved] = counts.get(resolved, 0) + 1

    report: dict[str, object] = {
        "checked": counts[OK] + counts[FAILED] + counts[PENDING],
        "ok": counts[OK],
        "failed": counts[FAILED],
        "pending": counts[PENDING],
        "corrections": corrections,
        "failures": failures,
    }
    sink.emit("verify_done", phase=phase, **report)
    return report


def _failure_event_fields(
    ledger: _FailureLedger, source_id: str, tile_id: str
) -> dict[str, object]:
    """The reason fields every tile_failed event carries.

    Always present, never conditional, so the browser can read
    event.reason without first checking whether this particular failure
    happened to have one. The ledger guarantees a record for any tile at
    all, which is what makes that safe.
    """
    record = ledger.record_for(source_id, tile_id)
    return {
        "kind": record["kind"],
        "reason": record["reason"],
        "retried": record["retried"],
    }


def _retry_failed_tiles(
    records: Sequence[Mapping[str, object]],
    fetched: Sequence[tuple[object, Path]],
    tiles: Sequence[Tile],
    current_tile_ids: Sequence[str],
    state: JobState,
    request: SurveyRequest,
    token: CancelToken,
    sink: ProgressSink,
    ledger: _FailureLedger,
    retry_pass: int,
) -> bool:
    """One pass over the tiles a retry could plausibly fix. Returns True
    if a stop landed during it.

    Each source is asked again for exactly its own failed tiles, through
    the same fetch() the first attempt used, so everything that call
    already does keeps happening: the rate limiter still spaces requests,
    _download_tile still makes its own four attempts with backoff, a tile
    that is already on disk is still skipped, and a tile that turns out to
    be over the node cap is still subdivided rather than retried. This
    layer adds no backoff of its own, deliberately, because a second
    competing delay on top of one that already honours Retry-After would
    be slower for no benefit and impossible to reason about.

    Cancellation interrupts this as promptly as it interrupts the first
    attempt: checked before each source is asked, and threaded into
    fetch() itself for the sources that accept it, so a stop lands between
    tiles rather than after all of them.

    A tile whose retry never happened because a stop landed first stays
    FAILED, not pending, and that is deliberate. Task 22's rule is that a
    tile a stop never REACHED is pending; this tile was reached, an entire
    pass ago, and it failed. Recording it pending because a later,
    optional second attempt did not happen would erase a failure the run
    genuinely observed.
    """
    sources_by_id = {source.id: (source, work) for source, work in fetched}
    tiles_by_id = {tile.tile_id: tile for tile in tiles}
    by_source: dict[str, list[str]] = {}
    for record in records:
        by_source.setdefault(str(record["source"]), []).append(str(record["tile_id"]))

    stopped = False
    for source_id, tile_ids in by_source.items():
        if token.is_cancelled():
            return True
        entry = sources_by_id.get(source_id)
        if entry is None:
            continue
        source, source_work = entry
        retry_tiles = [tiles_by_id[t] for t in tile_ids if t in tiles_by_id]
        if not retry_tiles:
            continue
        for tile in retry_tiles:
            record = ledger.record_for(source_id, tile.tile_id)
            sink.emit(
                "tile_retrying",
                source=source_id,
                tile_id=tile.tile_id,
                pass_number=retry_pass,
                of=RETRY_PASS_BUDGET,
                kind=record["kind"],
                reason=record["reason"],
            )
        ledger.note_retry(source_id, [tile.tile_id for tile in retry_tiles])

        fetch_kwargs = {"cancel": token} if _fetch_accepts_cancel(source) else {}
        fetch_succeeded = False
        try:
            source.fetch(request.bbox, retry_tiles, source_work, sink, **fetch_kwargs)
            fetch_succeeded = True
        except Cancelled:
            stopped = True
        except Exception as exc:
            sink.emit("source_failed", source=source_id, error=str(exc))
            collected = list(getattr(source, "tile_failures", None) or ())
            if collected:
                ledger.add_tile_failures(collected)
            else:
                ledger.set_source_error(source_id, str(exc))
        # stopped is deliberately NOT passed through here: see this
        # function's own docstring on why a tile the stop caught before
        # its second attempt stays failed rather than reverting to
        # pending.
        _record_tile_outcomes(
            state,
            source_id,
            retry_tiles,
            source_work,
            fetch_succeeded,
            current_tile_ids,
            sink,
            ledger,
        )
        if stopped:
            break
    return stopped


def _record_tile_outcomes(
    state: JobState,
    source_id: str,
    pending: Sequence[Tile],
    source_work: Path,
    fetch_succeeded: bool,
    current_tile_ids: Sequence[str],
    sink: ProgressSink,
    ledger: "_FailureLedger",
    stopped: bool = False,
) -> None:
    """Mark each pending tile from what is actually on disk, not from
    whether the batch fetch() call raised.

    Also emits a tile_failed progress event for every tile this call
    resolves to FAILED (Task 22), never for one it resolves to PENDING.
    Since Task 30 that event carries WHY, read from the ledger: kind for
    code, reason for the owner, and how many times the tile has been
    retried. The fields are always present, so a browser can read
    event.reason without checking first.

    This is where a fetch decides a tile has failed, since it is the place
    that already has to reason about what is genuinely on disk rather than
    trust a batch call's own return value. It used to be the only such
    place anywhere; _verify_tiles (Task 30) is now a second one, and the
    division between them is exact rather than approximate. This one
    resolves the tiles a fetch() call was just handed, from that call's
    outcome plus the disk. That one re-reads every planned tile of every
    source afterwards, from the disk alone, and only ever lowers a
    recorded ok that has no file behind it. Both go through
    _tile_has_output, so they cannot form different opinions about what
    "on disk" means; what differs is which tiles they look at and when.

    The browser's tile grid reads this event to show a failed tile as
    visibly distinct from one that is merely still pending, which matters
    more here than for the other three grid states, since it is the one
    the owner would want to know about before deciding they have enough.
    Precisely because it matters more, it must mean a real attempt came
    up short, never merely "not reached yet" (a coordinator review's
    finding: this call site marked a whole stopped source's untouched
    tail of tiles FAILED, which painted most of a 16-tile grid red the
    instant a clean Stop landed on tile 2, and made the owner distrust a
    package that was, in fact, fine).

    stopped tells this apart from an ordinary failure: it is true only
    when THIS call is the one running because this source's own fetch()
    raised Cancelled just now (see run_survey's own local `stopped`
    flag, passed straight through), never for a genuine per-tile error or
    a plain, complete success. Cancelled and any other exception cannot
    both come out of the same fetch() call (Python raises one exception
    at a time, and none of the three real sources catch and continue past
    an internal failure), so a tile lacking output here, on a call where
    stopped is true, was never attempted at all, not attempted and found
    wanting: it is marked PENDING, matching the status a source that
    never got a turn at all already carries, and no tile_failed event is
    emitted for it. A tile that already has real output (the one in
    flight when the stop landed, kept per the LayerSource protocol's own
    cancel convention) is unaffected: it is still marked OK from the
    checks below exactly as it always was.

    fetch() is one Python call for potentially many tiles: it either returns
    once for all of them or raises once for all of them, which says nothing
    about which individual tiles actually got a usable file. Sources that
    name output files after the tile id, which covers OSM and the test
    stubs, get true per-tile status here.

    A tile is ok only if it has a non-empty, tile-stamped file in EVERY
    directory where this source keeps tile-stamped files, not just any one
    of them. OSM writes flat into source_work, so that is one directory. No
    source ships today that writes tile-stamped files into more than one
    (Overture did, one type per subdirectory, until Task 23 stopped tiling
    it), so the multi-directory case is currently exercised only by this
    file's own stubs. It is kept rather than simplified away because the
    set of directories to check is derived from what is actually on disk and
    never hardcoded, which is what lets any future source shape work here
    without package.py knowing anything about it; collapsing it to "the one
    directory" would be a quiet assumption that no source ever splits again.

    A source with no per-tile naming at all, which is elevation's single
    whole-area file and, since Task 23, Overture's one file per type,
    contributes no tile-stamped directory at all. If it still produced its
    output, there is no finer signal than the batch outcome, so it applies
    uniformly to every tile, matching how such sources have always behaved.

    That used to be justified here as "one whole-extent download either
    lands, in which case every tile's ground is genuinely covered, or it
    does not, in which case none of them is". Review finding I2: that was
    true when Overture was one download, and Task 23 made it eight while
    Task 24 made them concurrent and independently failing. The partial
    case, one bad type name or one type 503ing, is now the ordinary one,
    and a single type failing marks all 72 tiles FAILED while seven real
    layers are merged into the package.

    The uniform application is still right, and the reason is now a
    different one. It is not that the outcome is all-or-nothing; it is
    that a type's download covers the whole extent, so whatever it did or
    did not deliver, it delivered the same thing to every tile. There is
    no per-tile fact to record here that would be finer than this, and
    inventing one would be fabricating a denominator.

    What it costs is worth stating plainly, because the alternative was
    weighed and refused. A tile that got seven of eight types is not
    wholly failed, and painting it red over-reports the damage. But the
    only other status available is one that does not emit tile_failed,
    and the grid then settles those tiles to "done" off Overture's own
    per-type tile_done events: measured on the real path, one type of
    eight failing turns a 16-tile grid from 16 failed to 16 DONE, on a
    package that is missing a layer. Over-reporting damage is recoverable
    by reading the log, which carries source_failed naming exactly which
    types failed; under-reporting it hands the owner a folder that looks
    finished and is not, into a workflow that reads these folders straight
    into Grasshopper. This module's rule everywhere else is never to claim
    more than it has, and red is the side of that line to be on.

    survey.json no longer contradicts the folder while it says so: the
    Overture entry's `types` reports what actually landed (see
    _source_provenance and OvertureSource.fetched_types), so a reader who
    finds seven layers is told about seven types.

    A fetch() that returns without raising while leaving nothing at all on
    disk is not evidence of anything: that is vacuous, not done, so it is
    always failed regardless of what the batch call reported (this can
    never coincide with stopped=True: Cancelled is what sets stopped, and
    Cancelled means fetch() never reached its own return at all, so
    fetch_succeeded is always False whenever stopped is True).
    """
    files = _existing_output_files(source_work, current_tile_ids)
    pending_ids = {tile.tile_id for tile in pending}
    tile_stamped_dirs = {path.parent for path in files if path.stem in pending_ids}
    not_attempted_status = PENDING if stopped else FAILED

    for tile in pending:
        present = _tile_has_output(files, tile_stamped_dirs, tile.tile_id)
        if present is None:
            # No per-tile fact on disk to read, so the batch outcome is
            # the finest signal there is. See _tile_has_output.
            status = OK if fetch_succeeded else not_attempted_status
        else:
            status = OK if present else not_attempted_status
        state.mark(tile.tile_id, source_id, status)
        if status == FAILED:
            sink.emit(
                "tile_failed",
                source=source_id,
                tile_id=tile.tile_id,
                **_failure_event_fields(ledger, source_id, tile.tile_id),
            )


def _write_layer_files(
    outputs_by_source: dict[str, list[Path]], paths: PackagePaths
) -> list[Path]:
    """Copy the three phase 1 Overture layers into layers/ under friendly names.

    OvertureSource.merge names its raw output f"{stem}_{overture_type}.geojson"
    (see Task 20 finding 2: a bare type.geojson gave no clue which survey it
    belonged to), so the friendly-name lookup matches on that exact composed
    name rather than on the merged file's own path stem.

    Returns the layer files it wrote, so the stale-output sweep can count
    them as this run's own products rather than a previous attempt's.
    """
    written: list[Path] = []
    for output in outputs_by_source.get("overture", []):
        for overture_type, friendly in LAYER_FILENAMES.items():
            if output.name == f"{paths.stem}_{overture_type}.geojson":
                target = paths.layers_dir / friendly
                atomic_write_text(target, output.read_text(encoding="utf-8"))
                written.append(target)
                break
    return written


def _sweep_stale_outputs(
    produced: set[Path], paths: PackagePaths, sink: ProgressSink
) -> None:
    """Remove merged outputs a previous attempt left in the package root.

    Resume reuses an incomplete root, and the fingerprinted work directory
    isolates each selection's raw tiles, but nothing isolated the MERGED
    files: an old <stem>_water.geojson from a failed building-plus-water
    attempt survived a completed buildings-only resume, so the folder
    listing contradicted the survey.json beside it (final review residual,
    HANDOFF item 4). The owner reads these folders straight into
    Grasshopper, so a stale layer is not clutter, it is wrong data.

    Candidates come only from each source's own possible_outputs(stem)
    declaration, a closed list of names mapgen itself could ever write for
    this exact stem. A file the user dropped into the folder can never
    match it, survey.json is not a source output, and the Urbano bridge's
    artifacts are deliberately out of scope: the bridge reruns every run
    unless skipped, and survey.json's bridge block already records
    honestly whether its outputs are this run's work.

    Only called on a complete run. An incomplete run keeps everything,
    because its survey.json already says complete: false and the next
    resume will finish the job and sweep then; deleting the only merged
    copy of anything mid-failure helps nobody.

    "Complete" is the only condition, and finding I1 is why that is worth
    stating: a stop that lands after every tile has already finished used
    to skip this as well, on the reasoning that a stopped run's sweep
    could be left to a later, ordinary run. That reasoning holds for an
    incomplete run and is false for a complete one. A complete package is
    never reused (naming._survey_reports_complete, read by
    build_package_paths, sends the next survey of the same site and date
    to _02), so "later" never arrives and the stale layer stays in the
    folder for good.
    """
    for source in available_sources():
        possible = getattr(source, "possible_outputs", None)
        if not callable(possible):
            continue
        for relative in possible(paths.stem):
            candidate = paths.root / relative
            if not candidate.exists() or candidate.resolve() in produced:
                continue
            candidate.unlink()
            sink.emit("stale_output_removed", name=relative)


def _source_provenance(source, merged_files: Sequence[Path] = ()) -> dict[str, object]:
    """One survey.json `sources` entry: the LayerSource protocol's own
    fields, plus endpoints_used, plus (Task 19) types and routing_note if
    this source exposes them, all read the same defensive way
    sources/base.py's own docstring documents for optional,
    source-specific attributes.

    types is Overture-specific today (the actual list of types this
    package's Overture data was fetched with, which the fix in
    _configured_sources means can genuinely differ from the 8-type
    default): a package's audit trail should say what it actually
    contains, not require cross-referencing category selection against a
    mapping table kept somewhere else to work that out.

    Read from fetched_types in preference to types, since review finding
    I2. Task 24 made the eight downloads independent and independently
    failing, so "the selection" and "what the package holds" stopped
    being the same list: one type failing left seven real layers merged
    into the folder beside a survey.json still naming all eight. The
    fallback matters as much as the preference does. fetched_types is
    None until fetch() has run, so a source a stop caught before its turn
    reports the selection rather than an empty list, which would read as
    "this package deliberately contains no Overture types".

    routing_note is OsmSource-specific today: endpoints_used already says
    WHICH endpoint a run actually contacted, but not WHY it was that one
    rather than the other, which matters here specifically because a
    category filter now silently changes it (map API vs Overpass). A
    coordinator review asked that survey.json say both.
    """
    entry: dict[str, object] = {
        "id": source.id,
        "licence": source.licence,
        "attribution": source.attribution,
        "endpoints_used": list(getattr(source, "endpoints_used", [])),
        # Task 30, and the half of the no-fabricated-empty-output ruling
        # that lives in the record rather than in merge(). A source that
        # merged nothing writes no file, so the folder is honest; without
        # these two fields the absence would be a silent gap, and the
        # ruling was explicit that it must be explained by the record.
        #
        # Three situations a reader has to be able to tell apart, and
        # these are how:
        #
        #   not selected        no entry in `sources` at all
        #   merged nothing      merged_files: [], features_merged: 0,
        #                       and no entry for this source in
        #                       tile_failures
        #   failed              merged_files may be empty too, but
        #                       tile_failures names the tiles and says
        #                       why, and `tiles` records them failed
        #
        # features_merged is omitted rather than zeroed for a source that
        # does not count features (elevation's DEM is a raster, not a
        # feature collection), because a 0 there would read as "this DEM
        # is empty" for a file that either exists or does not.
        "merged_files": [path.name for path in merged_files],
    }
    merged_features = getattr(source, "merged_features", None)
    if merged_features is not None:
        entry["features_merged"] = merged_features
    types = getattr(source, "fetched_types", None)
    if types is None:
        types = getattr(source, "types", None)
    if types is not None:
        entry["types"] = list(types)
    # demtype is ElevationSource-specific today (Task 28), read the same
    # defensive way. It records which DEM the package actually holds,
    # which nothing else in survey.json says: licence and attribution
    # above already follow the model, but neither of them names it, and a
    # 30 m surface model and a 30 m bare earth model are a different
    # ground plane in Rhino for the same extent, the same date and the
    # same file size.
    #
    # Truthful about what was downloaded rather than what was selected,
    # per the brief, because of a detail that is not obvious from here:
    # the source's own downloaded file carries the model in its NAME (see
    # elevation_models.work_file_name), so a resumed run cannot skip
    # another model's file and have this line describe it. Without that,
    # this would be a record of the setting, not of the package.
    demtype = getattr(source, "demtype", None)
    if demtype is not None:
        entry["demtype"] = demtype
    check_routing_note = getattr(source, "routing_note", None)
    if callable(check_routing_note):
        note = check_routing_note()
        if note:
            entry["routing_note"] = note
    return entry


def _build_survey_json(
    request,
    paths,
    tiles,
    sources,
    state,
    started_at,
    bridge_attempted,
    bridge_ok,
    bridge_error,
    stopped,
    outputs_by_source=None,
    verified=None,
) -> dict:
    width_m, height_m = extent_metres(request.bbox)
    outputs_by_source = outputs_by_source or {}
    verified = verified or {
        "checked": 0, "ok": 0, "failed": 0, "pending": 0,
        "corrections": [], "failures": [],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "site": request.site,
        "region": request.region,
        "slug": {
            "site": slugify(request.site, "site"),
            "region": slugify(request.region, "region"),
        },
        "date": request.effective_date.isoformat(),
        "urbano_stem": paths.stem,
        "bbox": request.bbox.to_dict(),
        "extent_km": {
            "width": round(width_m / 1000.0, 3),
            "height": round(height_m / 1000.0, 3),
        },
        "tiling": {
            "tile_size_m": request.tile_size_m,
            "overlap_m": request.overlap_m,
            "rows": max((t.row for t in tiles), default=0) + 1,
            "cols": max((t.col for t in tiles), default=0) + 1,
        },
        "sources": [
            _source_provenance(source, outputs_by_source.get(source.id, ()))
            for source in sources
        ],
        # The resolved selection (never the possibly-None raw field:
        # every other audit-trail value here is a concrete, resolved
        # fact, not "whatever was asked for or a default"), so a package
        # says what it contains without the reader having to separately
        # know that None means everything.
        "categories": request.effective_categories,
        "tiles": state.as_tile_records(),
        # Task 30, section 5: which tiles are missing, for which source,
        # and why, in the package's own record, because "if nothing then
        # it should say" was the owner's requirement and a folder that
        # quietly holds less than was asked for is the failure this whole
        # task exists to remove.
        #
        # A separate list rather than extra keys inside `tiles`, which
        # stays exactly the shape it has always been: `tiles` is read by
        # the browser, by `mapgen bridge` and by every package already on
        # this machine, and one record per failure is also the shape a
        # reader wants, since the ordinary run has none at all rather
        # than one per tile. Empty on a clean run.
        #
        # `retried` says whether the run tried again before giving up,
        # which is the difference between "the service was busy" and "the
        # service is still saying no". Only kinds a retry could plausibly
        # fix are ever retried; see RETRYABLE_FAILURE_KINDS.
        "tile_failures": verified["failures"],
        # What the end-of-run verify actually checked, and what it had to
        # correct. corrections is normally empty, and when it is not, it
        # is the interesting part: it means this run had recorded a tile
        # as downloaded that had no file behind it, and the record has
        # been put right rather than left to be believed.
        "verified": {
            "checked": verified["checked"],
            "ok": verified["ok"],
            "failed": verified["failed"],
            "pending": verified["pending"],
            "corrections": verified["corrections"],
        },
        # complete reports the survey DATA alone: every requested source's
        # every tile downloaded and merged successfully. It intentionally
        # says nothing about the Urbano bridge, decided and recorded
        # separately below. Folding a bridge failure into complete would
        # give it a second, unrelated meaning: complete already decides
        # whether a folder is safe to reuse or must be suffixed _02 (see
        # naming.build_package_paths) and whether _work/ gets cleaned up
        # below. An owner with no Urbano install at all, which is Task 20's
        # real, reproduced case, would then never see complete: true no
        # matter how many times every source downloaded cleanly, the folder
        # would never be considered finished, and _work/ would never be
        # swept. The data either downloaded completely or it did not; the
        # bridge either produced Urbano's files or it did not; those are two
        # different questions and an owner reading this file deserves a
        # straight answer to each.
        "complete": state.complete,
        # stopped (Task 22) is what tells "short because the owner said
        # so" apart from "short because something broke", the distinction
        # complete alone cannot make: it was always a bool covering both
        # "finished" and "something failed", and a deliberate stop is
        # neither. True only when a Stop request is the reason this run
        # ends with complete: false; never true for an ordinary tile
        # failure (with or without --force), and never true once complete
        # is true (a stop noticed only after every tile had already
        # genuinely finished has nothing left to be honest about here,
        # see run_survey's own reported_stopped for that exact race, which
        # this comment described for a whole task before finding I1 found
        # it was describing something nothing implemented). Read this
        # alongside `tiles`, which already carries the true per-tile
        # picture this field is a one-word summary of: complete=false,
        # stopped=true, the tiles the stop caught before they were
        # reached read "pending" there, not "failed" (see
        # _record_tile_outcomes's own docstring); "failed" on a stopped
        # run still means a real attempt came up short, exactly as it
        # does on any other run.
        "stopped": stopped,
        "bridge": {
            "attempted": bridge_attempted,
            "ok": bridge_ok,
            "error": bridge_error,
        },
        "started_at": started_at,
        "finished_at": _now(),
    }
