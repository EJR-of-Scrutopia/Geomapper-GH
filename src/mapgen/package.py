"""Orchestration: plan, download, merge, bridge, survey.json.

Knows about paths, job state and the source registry. Knows nothing about any
individual data source, which is what makes phase 2 additive.
"""

from __future__ import annotations

import inspect
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from mapgen import __version__
from mapgen.bng import BngError, ensure_ostn15, load_ostn15
from mapgen.boundary_curves import boundary_curves_with_counts
from mapgen.bridge import BridgeError, BridgeRequest, run_bridge
from mapgen.buildings import (
    _candidate_rings,
    _exterior_ring,
    _resolve_way_ring,
    fuse_missing_buildings,
)
from mapgen.categories import ALL_CATEGORY_IDS, overture_types_for_categories, validate_categories
from mapgen.classify import (
    OverlaySets,
    Ring,
    SAMPLE_CAP,
    _METRES_PER_DEGREE_LAT,
    _metres_per_degree_lon,
    classify_parcels,
)
from mapgen.cog import CogError, CogReader, FileByteSource, read_full_window
from mapgen.egrid import (
    ElevationGridError,
    _ChainSampler,
    elevation_grid_path,
    write_elevation_grid,
    write_elevation_grid_from_sampler,
)
from mapgen.elevation_models import DEFAULT_DEMTYPE, validate_demtype
from mapgen.fsutil import (
    atomic_write_bytes,
    atomic_write_text,
    best_effort_rmtree,
    ensure_dir,
    work_dir_scope,
)
from mapgen.geo import BBox, Tile, build_tiles, extent_metres
from mapgen.geotiff import GeoTiffError, read_dem
from mapgen.heights import (
    HeightsError,
    _declaration_line,
    _minimum_existing_id,
    _rewrite_osm,
    fuse_building_heights,
)
from mapgen.jobs import FAILED, OK, PENDING, CancelToken, Cancelled, JobState
from mapgen.merge import assert_inputs_present
from mapgen.naming import (
    PackagePaths,
    build_package_paths,
    check_path_length,
    slugify,
    tiling_fingerprint,
)
from mapgen.resolver import resolve
from mapgen.sources.base import (
    FAILURE_NO_OUTPUT,
    FAILURE_UNKNOWN,
    # Task 32 moved this to base.py, beside the vocabulary it is a subset
    # of, because OvertureSource now has to read it (see its own comment
    # there). Imported rather than redefined, and re-exported under this
    # module's own name so mapgen.package.RETRYABLE_FAILURE_KINDS still
    # resolves: there is one policy, and this is still the only module
    # that acts on it.
    RETRYABLE_FAILURE_KINDS,
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
from mapgen.sources.inspire import BOUNDARY_INDICATIVE_NOTE, InspireSource
from mapgen.sources.lidar_wales import LidarWalesSource
from mapgen.sources.os_open import OsOpenSource
from mapgen.sources.os_uprn import OsUprnSource
from mapgen.sources.osm import OsmSource
from mapgen.urbano import (
    LAYER_ORDER,
    ProjectSettingError,
    project_zone,
    resolve_data_files,
    write_project_setting,
)
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

# The longest a retry will wait because a service asked it to, in
# seconds (Task 32).
#
# ElevationSource attaches a Retry-After to a rate-limited failure when
# OpenTopography sends one, and the retry pass honours it rather than
# coming straight back at a service that has just said, in writing, when
# to return. That is the whole point of respecting the header: a retry
# that ignores it is worse than no retry at all, because it costs the
# owner's own quota to be told the same thing again.
#
# A ceiling exists because the header is not always a small number. A
# daily quota that is genuinely exhausted can answer with an hour, and a
# survey the owner is watching must not silently stop for an hour inside
# a step that presents itself as automatic. Past this line the retry is
# not made at all, which is the honest outcome: if the service will not
# serve this run for another hour, this run cannot have the layer, and
# saying so now is better than saying so in an hour.
#
# Sixty seconds, matching the ceiling osm.py's own retry_delay_seconds
# already puts on its exponential backoff. One number for "the longest
# this project ever pauses", rather than a second one chosen separately.
MAX_RETRY_AFTER_WAIT_SECONDS = 60.0


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
    """Register the phase 1 and phase 2 default sources, skipping only a
    repeat of itself.

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

    LidarWalesSource (Task 6) joins the tuple the same way ElevationSource
    did: it is not part of SurveyRequest.source_ids' own default selection
    (opt-in, like elevation, since it is a large, Wales-only download the
    owner chooses rather than gets by default), but registering it here is
    what makes it appear in the web UI's layer checklist at all, since that
    checklist is registry-driven rather than hard-coded per source.

    InspireSource (Task 4 of the INSPIRE curves plan) joins the same way,
    for the same reason: opt-in (an England/Wales-only property-boundary
    download the owner chooses, not a default), and registering it here is
    what makes "Property boundaries (INSPIRE)" appear in the layer
    checklist at all.

    OsOpenSource (Task 4 of the OS Open pack plan) joins the same way too:
    opt-in (a Great-Britain-wide, but still large, three-product download
    the owner chooses rather than gets by default), and registering it
    here is what makes "OS Open map data (GB)" appear in the layer
    checklist at all.

    OsUprnSource (Task 5 of the OS Open pack plan) joins the same way
    again: opt-in (a Great-Britain-wide, one-time 619 MB national address
    download the owner chooses, not a default), and registering it here
    is what makes "Addresses (OS Open UPRN, GB)" appear in the layer
    checklist at all.
    """
    by_id = {source.id: source for source in available_sources()}
    for source in (
        OsmSource(), OvertureSource(), ElevationSource(), LidarWalesSource(), InspireSource(),
        OsOpenSource(), OsUprnSource(),
    ):
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

    inspire (the INSPIRE curves review's own fix), os_open and os_uprn
    (final review of the OS Open pack plan, Important I1) all join the
    dispatch for the same reason, distinct from the three above: none of
    the three has a per-request selection to pass through at all, so each
    one's own `configure()` takes no argument beyond self. What each needs
    a fresh copy FOR is a per-run attribute's own lifetime (InspireSource's
    `endpoints_used`; os_open's and os_uprn's own `versions_used`, added
    when Important I1's provenance enrichment gave it a real consumer):
    that attribute accumulates across every fetch() call one instance ever
    serves, which package.py's retry pass depends on within a single
    survey, but would otherwise go on accumulating across every survey
    ever run through the one instance register_default_sources() builds
    for the whole process. Calling `configure()` with no arguments at all
    is what a source needs when "give me a clean, request-scoped copy" is
    the whole ask.
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
            elif source_id in ("inspire", "os_open", "os_uprn"):
                source = configure()
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
    configured_sources = _configured_sources(request)
    for source in configured_sources:
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
    # Task 7 of the tier resolver plan: which source resolves each
    # category for this exact bbox and selection, computed once here over
    # the same configured_sources the loop above already estimated
    # against, never a second, separately-configured list that could
    # disagree with it. run_survey/_build_survey_json compute the
    # identical structure from resolve() the same way, over its own
    # request-scoped `sources`, for survey.json's own "resolution" key.
    result["resolution"] = resolve(request.bbox, configured_sources)
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
            # Task 32, section 4. A run that completed only because a
            # retry worked is not the same run as one that never
            # stumbled, and by this point nothing else says so: a tile
            # that recovered has had its reason forgotten by the verify
            # pass, correctly, so tile_failures is empty and the package
            # reads as an ordinary success. This is the one event that
            # says the run was fragile, and survey.json's `retries`
            # below is its saved form.
            still_failed = {
                (str(record.get("source")), str(record.get("tile_id")))
                for record in verified["failures"]
            }
            attempted = ledger.retry_records(still_failed)
            recovered = [record for record in attempted if record["recovered"]]
            sink.emit(
                "retry_done",
                pass_number=retry_pass,
                of=RETRY_PASS_BUDGET,
                attempted=len(attempted),
                recovered=len(recovered),
                still_failing=len(attempted) - len(recovered),
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
        # Which layers are STILL short, as opposed to which ones reported
        # a failure at some point during the run. Since Task 32 those are
        # routinely different sets: elevation and Overture now report
        # per-tile failures too, so an unforced run can reach here with
        # one layer permanently short beside another whose retry worked.
        short_layer_ids = {str(record.get("source")) for record in unresolved}

        for source, source_work in deferred_merges:
            if unrecoverable and source.id in short_layer_ids:
                # No merged output for a source whose tiles are still
                # missing on an unforced run, which is what happened
                # before Task 30 too: the first failing tile raised out
                # of fetch() and nothing was merged for that layer.
                #
                # Scoped to that source, and not to every deferred merge,
                # which is what this did while OSM was the only layer
                # that could get here. A layer whose retry recovered it
                # has all of its tiles and is merged; withholding it
                # because a DIFFERENT layer failed would throw away work
                # that is complete and paid for, and the run is about to
                # raise and say what is missing in any case.
                continue
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

        # Task 6 of the OS Open pack plan. BEFORE heights fusion and,
        # transitively, BEFORE the bridge: a footprint this step injects
        # with no height of its own is exactly the shape heights fusion
        # already knows how to fill in, so it has to already be in the
        # `.osm` by the time that pass runs. Gated only on `<stem>.osm`
        # being in the package at all (see `_fuse_buildings_step`'s own
        # docstring for why neither candidate file being there is a
        # normal outcome, not a skip), not on `stopped` or `unrecoverable`,
        # for the identical reason heights fusion below is not gated on
        # them either.
        buildings_fusion = _fuse_buildings_step(root=paths.root, stem=paths.stem, sink=sink)

        # Task 3 of the categorised boundaries plan. Immediately after
        # buildings fusion, unconditionally, for the identical reason
        # heights fusion below is not gated on `stopped` or
        # `unrecoverable` either: gated only on `<stem>_parcels.geojson`
        # being in the package at all (see this step's own docstring).
        # Runs BEFORE heights fusion and the .osm boundary injection below
        # because it reads the FUSED `.osm` for its own building-way
        # evidence, which buildings fusion has already written by this
        # point, and neither of the two steps after it touches anything
        # this one reads.
        boundaries_categories = _categorise_boundaries_step(
            root=paths.root, stem=paths.stem, sink=sink
        )

        # Task 7. BEFORE the bridge, unconditionally, exactly like the
        # elevation grid and project setting steps below: the bridge
        # converts <stem>.osm into Urbano's own formats, so the heights
        # this writes into it have to already be there when that happens,
        # never after. Gated only on the three files it needs already
        # being in the package (see _fuse_heights_step's own docstring),
        # not on `stopped` or `unrecoverable`: a run that stopped after
        # both the osm and lidar_wales sources had genuinely finished has
        # a real .osm and real rasters sitting in the folder, and there is
        # no reason to leave that building data flat just because a later
        # source in the same run never got its turn.
        lidar_heights = _fuse_heights_step(root=paths.root, stem=paths.stem, sink=sink)

        # Task 5 of the INSPIRE curves plan. Immediately after the heights
        # fusion above and, like it, unconditionally BEFORE the bridge
        # step below: gated only on the two files _fuse_boundaries_step
        # itself needs already being in the package, not on `stopped` or
        # `unrecoverable`, for the identical reason heights fusion is not
        # gated on them either (see that step's own comment, two lines
        # up).
        inspire_boundaries = _fuse_boundaries_step(
            root=paths.root, stem=paths.stem, sink=sink
        )

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

        # Task 35, and the whole point of it: this happens on EVERY run, in
        # every outcome, whether the bridge ran, failed, was skipped by
        # --skip-bridge or was never reached because the run was stopped or
        # is about to raise.
        #
        # Unconditional, deliberately, because the failure this replaces is
        # "the folder does not have the file and you have to know why". The
        # owner met that once already, in Grasshopper, with a real survey.
        # Every condition that could be put on this brings back a version of
        # it.
        #
        # It cannot claim more than the folder holds: every path and every
        # layer name in the file is resolved off the disk (see
        # mapgen.urbano.resolve_data_files), so a partial package gets a
        # project setting describing exactly the partial package, and one
        # with nothing in it at all gets none and says so.
        #
        # AFTER the bridge step, never before, so that when both write the
        # file mapgen's is the one that survives. See _write_project_setting_
        # step for why that is the right way round.
        # Task 39. Before the project setting, never after: the setting's
        # layer list is resolved off the disk, so the .egrid has to be there
        # for `elevation` to be in it. After the sweep, so that a previous
        # attempt's .egrid is gone before this one writes its own.
        elevation_grid = _write_elevation_grid_step(
            bbox=request.bbox, root=paths.root, stem=paths.stem, sink=sink
        )
        project_setting = _write_project_setting_step(
            bbox=request.bbox, root=paths.root, stem=paths.stem, sink=sink
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
        ledger.retry_records(
            {
                (str(record.get("source")), str(record.get("tile_id")))
                for record in verified["failures"]
            }
        ),
        project_setting,
        elevation_grid,
        lidar_heights,
        inspire_boundaries,
        buildings_fusion,
        boundaries_categories,
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
                describe_tile_failures(survey["tile_failures"], planned_tiles=len(tiles))
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


def _elevation_grid_record(
    written: bool = False,
    file: str | None = None,
    nodes: int | None = None,
    covered: int | None = None,
    error: str | None = None,
    source: str | None = None,
    min_height: float | None = None,
    max_height: float | None = None,
) -> dict[str, object]:
    """survey.json's `elevation_grid` block, in ONE shape whatever happened.

    All eight keys, always. README's schema table has always described
    the block this way (five keys before Task 8, six since, eight since
    the 2026-08-06 post-release fix below), but `covered` used to be added
    only on the success branch, so a package with no DEM and a package
    whose DEM could not be read each carried four. Nothing in mapgen
    noticed, because cli.py reads the block with .get; a reader following
    the README and indexing the key got a KeyError on exactly the packages
    where they most wanted to know.

    A record whose KEYS depend on the outcome is the kind of shape that is
    only ever found by the reader who trips over it, so the outcome now
    lives entirely in the values.

    `source` (Task 8) is one of `"lidar_wales+opentopography"`,
    `"lidar_wales"`, `"opentopography"` or `None`: which raster (or both,
    LiDAR first) actually answered the grid that was written, `None` on
    every branch that wrote nothing at all, written or not. It is not
    derivable from `covered` alone, which counts nodes and says nothing
    about which sampler answered them.

    `min_height`/`max_height` (2026-08-06): the min and max over the
    grid's own covered (non-NaN) nodes, rounded to 2 decimals, so
    Grasshopper labels can anchor to the terrain's real values instead of
    the flattened elevations Urbano's GeoJSON import leaves contours with.
    Both are None on exactly the branches `covered` is None:
    `write_elevation_grid_from_sampler` already refuses an all-NaN grid
    (`_NoCoverageError`, egrid.py) before it can ever reach here, so a
    `written: True` record can never carry a `covered` of zero and never
    carries a null `min_height`/`max_height` either.
    """
    return {
        "written": written,
        "file": file,
        "nodes": nodes,
        "covered": covered,
        "error": error,
        "source": source,
        "min_height": min_height,
        "max_height": max_height,
    }


def _write_elevation_grid_step(
    bbox: BBox, root: Path, stem: str, sink: ProgressSink
) -> dict[str, object]:
    """Convert this package's DEM into `<stem>.egrid`, reported as a record.

    Task 39, and the last data gap between a mapgen package and Urbano.
    Urbano's elevation field is a protobuf `ElevationGrid` and every one of
    the seven components that reads it deserialises it as one, so a GeoTIFF
    beside it is a file nothing in Urbano can open. This writes the file
    those components want, out of the DEM mapgen already has.

    Task 8 adds a second, better DEM: `<stem>_lidar_dtm.tif`, LidarWalesSource's
    own 1 m Ordnance Survey terrain model, which answers far more of the
    grid than COP30 ever will inside its own coverage. Both rasters are
    read into memory BEFORE either is committed to, in their own separate
    failure boundaries, so a problem reading one never costs the answer
    the other could still give. The degradation ladder this produces,
    checked in order:

      * Both the LiDAR DTM and the tiff are on disk, an OSTN15 shift grid
        can be obtained (`load_ostn15`, then `ensure_ostn15` if the cache
        is empty), and both rasters read cleanly: sample the DTM first and
        the tiff second, through `egrid._ChainSampler`
        (`source: "lidar_wales+opentopography"`).
      * OSTN15 cannot be obtained, or the DTM is on disk but cannot be
        read (a review finding: an earlier version of this step read the
        DTM and the tiff inside the SAME failure boundary, so a corrupt or
        truncated DTM discarded a perfectly good tiff along with it):
        fall back to the tiff alone, exactly the phase 1 path, IF the tiff
        is there and readable (`source: "opentopography"`), announced
        through `elevation_grid_degraded` when the DTM was genuinely on
        disk and unreadable rather than simply absent or OSTN15-blocked
        (the OSTN15 case already existed before this task and is not a
        new degradation to announce).
      * The tiff is on disk but cannot be read, and the DTM read cleanly:
        the symmetric fallback, LiDAR alone (`source: "lidar_wales"`),
        also announced through `elevation_grid_degraded`.
      * Nothing left that can answer, whether because neither raster could
        be read, or the only raster present could not: a real DEM this
        step cannot honestly convert, recorded as a failure rather than
        silently read as "no DEM".
      * Neither raster present at all removes any `.egrid` a previous
        attempt left, exactly as phase 1 did.

    `elevation_grid_degraded` is a sink event, not a sixth record key,
    deliberately: the record describes what WAS written (see
    `_elevation_grid_record`'s own docstring on why its keys never move),
    and `written: True` with a degraded source is already a complete,
    honest answer to that question. The event is a different question,
    what happened on the way to it, and belongs where every other
    mid-step happening in this file already lives.

    OSTN15 is only ever asked for when the LiDAR DTM is on disk: a survey
    that never selected `lidar_wales` touches neither the cache nor the
    network here, same as `_fuse_heights_step`'s own reasoning for the
    same lookup.

    The ONLY place in mapgen that calls write_elevation_grid (or, since
    Task 8, write_elevation_grid_from_sampler), for the same reason
    _run_bridge_step and _write_project_setting_step are each the only
    caller of theirs: two call sites would have to be kept agreeing forever
    about which failures are survivable and which events are emitted.

    Runs BEFORE _write_project_setting_step in both of its callers, and that
    ordering is load bearing rather than incidental. mapgen.urbano.
    resolve_data_files reads the layer list off the disk, so the `.egrid`
    has to already be there for `elevation` to appear in the project setting
    at all; reversed, every package would get a setting that says it has no
    terrain and a `.egrid` sitting next to it unused.

    **Staleness is this step's own job, not the stale-output sweep's.** The
    sweep only ever deletes names a source's `possible_outputs` declares,
    and `<stem>.egrid` is deliberately not one of them: `possible_outputs` is
    documented as what `merge()` writes, ElevationSource.merge writes only
    the `.tif`, and package.py's `_bridge_input_file` reads the same list to
    pick the file it hands the C# bridge as `--elevation-tiff-path`. Naming
    the `.egrid` there would eventually hand a protobuf to a flag that wants
    a raster. So instead: no DEM of either kind in the package means any
    `.egrid` beside it is removed here, which covers more ground than the
    sweep does anyway, since the sweep never runs on an incomplete package
    or on `mapgen bridge` and this runs on both.

    A failure costs the package nothing. It is recorded here, in survey.json
    and through the sink, and the run finishes: a package without terrain is
    exactly the package the owner has had all along, and losing a real
    survey's data because a DEM could not be converted would be the mistake
    task 20 already fixed once.
    """
    target = elevation_grid_path(root, stem)
    tiff = Path(root) / f"{stem}.tif"
    dtm_path = Path(root) / f"{stem}_lidar_dtm.tif"
    has_tiff = tiff.is_file()
    has_lidar_dtm = dtm_path.is_file()

    if not has_tiff and not has_lidar_dtm:
        # No DEM of either kind, so no terrain, and no leftover from a
        # previous attempt either: an .egrid describing a DEM this package
        # no longer holds is a file the owner would read straight into
        # Grasshopper.
        removed = target.exists()
        target.unlink(missing_ok=True)
        if removed:
            sink.emit("elevation_grid_removed", file=target.name)
        return _elevation_grid_record()

    ostn15_grid = None
    if has_lidar_dtm:
        try:
            ostn15_grid = load_ostn15()
            if ostn15_grid is None:
                ostn15_grid = ensure_ostn15()
        except BngError:
            # No cached grid, and the network fetch failed (or this
            # machine has never had a network to fetch one with). Falls
            # through to the tiff path below when there is a readable tiff
            # to fall back to, and is recorded as a failure, honestly,
            # when there is not: never a crash either way.
            ostn15_grid = None

    # Each raster is read into memory in its OWN failure boundary, before
    # either is committed to. A review finding on this task: an earlier
    # version read the DTM and the tiff inside one shared try block, so a
    # corrupt or truncated raster on either side discarded whatever the
    # OTHER side could still have answered with, unannounced. `window`/
    # `dem` stay `None` on a read failure rather than raising past this
    # point; `window_error`/`dem_error` carry the reason for the "nothing
    # left that can answer" message and the degradation events below.
    window = None
    window_error: str | None = None
    if has_lidar_dtm and ostn15_grid is not None:
        try:
            window = read_full_window(CogReader.open(FileByteSource(dtm_path)))
        except (CogError, OSError) as exc:
            window_error = str(exc)

    dem = None
    dem_error: str | None = None
    if has_tiff:
        try:
            dem = read_dem(tiff)
        except (GeoTiffError, OSError) as exc:
            dem_error = str(exc)

    zone = project_zone(bbox)
    sink.emit("elevation_grid_started")
    source: str | None = None
    try:
        if window is not None and dem is not None:
            grid = write_elevation_grid_from_sampler(
                _ChainSampler(lidar=window, ostn15=ostn15_grid, fallback=dem),
                bbox, zone, target,
            )
            source = "lidar_wales+opentopography"
        elif window is not None:
            # No tiff at all, OR a tiff that could not be read. Only the
            # second is a DEGRADATION worth announcing: the first is the
            # ordinary "LiDAR alone" shape this task always had.
            if dem_error is not None:
                sink.emit(
                    "elevation_grid_degraded",
                    reason=(
                        f"{tiff.name} could not be read ({dem_error}), so this "
                        f"elevation grid uses only the Welsh LiDAR DTM."
                    ),
                )
            grid = write_elevation_grid_from_sampler(
                _ChainSampler(lidar=window, ostn15=ostn15_grid, fallback=None),
                bbox, zone, target,
            )
            source = "lidar_wales"
        elif dem is not None:
            # No usable LiDAR: no DTM at all, OSTN15 unobtainable, or a DTM
            # that could not be read. Only the last of those three is a
            # degradation; the other two are the phase 1 path this task
            # never changes, and OSTN15-unobtainable already existed as a
            # silent fallback before this fix, so it stays silent here.
            if window_error is not None:
                sink.emit(
                    "elevation_grid_degraded",
                    reason=(
                        f"{dtm_path.name} could not be read ({window_error}), "
                        f"so this elevation grid uses only the OpenTopography "
                        f"DEM."
                    ),
                )
            # write_elevation_grid, not write_elevation_grid_from_sampler(dem,
            # ...): this is the one path phase 1 already had before either
            # LiDAR branch existed, and re-reading the tiff (already proven
            # readable, a moment ago, above) through the same wrapper every
            # pre-Task-8 package went through keeps its own refusal wording
            # (the tiff-named "covers none of this survey's extent" sentence)
            # identical rather than switching it to the sampler path's
            # generic one the instant a DTM happens to sit beside it.
            grid = write_elevation_grid(tiff, bbox, zone, target)
            source = "opentopography"
        else:
            # Nothing left that can answer. Named honestly, per whichever
            # of the two rasters was actually on disk and why it could not
            # be used, rather than one sentence pretending both were tried
            # the same way.
            raise ElevationGridError(
                _no_usable_raster_message(
                    tiff=tiff,
                    dtm_path=dtm_path,
                    has_tiff=has_tiff,
                    has_lidar_dtm=has_lidar_dtm,
                    ostn15_obtained=ostn15_grid is not None,
                    dem_error=dem_error,
                    window_error=window_error,
                )
            )
    except (ElevationGridError, OSError) as exc:
        error = str(exc)
        # Never left half converted. A previous run's .egrid describing a
        # different DEM would be worse than none, and a partially written one
        # is a component that throws on the owner's canvas.
        target.unlink(missing_ok=True)
        sink.emit("elevation_grid_failed", error=error)
        return _elevation_grid_record(error=error)
    covered = grid.real_count
    # write_elevation_grid_from_sampler already refused an all-NaN grid
    # (_NoCoverageError) before this line, so height_bounds is never None
    # here; the `is not None` guard is for anyone reading this file who
    # has not read that guarantee, not a case this branch actually meets.
    bounds = grid.height_bounds
    min_height = round(bounds[0], 2) if bounds is not None else None
    max_height = round(bounds[1], 2) if bounds is not None else None
    sink.emit(
        "elevation_grid_written",
        file=target.name,
        nodes=len(grid.heights),
        covered=covered,
    )
    return _elevation_grid_record(
        written=True,
        file=target.name,
        nodes=len(grid.heights),
        covered=covered,
        source=source,
        min_height=min_height,
        max_height=max_height,
    )


def _no_usable_raster_message(
    *,
    tiff: Path,
    dtm_path: Path,
    has_tiff: bool,
    has_lidar_dtm: bool,
    ostn15_obtained: bool,
    dem_error: str | None,
    window_error: str | None,
) -> str:
    """The sentence for `_write_elevation_grid_step`'s "nothing left that
    can answer" branch: reached only when every raster actually on disk
    either could not be read, or (the DTM's case) had no usable OSTN15
    grid to be sampled through.

    Composed rather than a single fixed sentence, because which raster
    was even attempted differs by combination, and a package with only a
    tiff on disk that could not be read deserves that tiff's own
    `GeoTiffError` text verbatim (matching this step's behaviour before
    this task existed) rather than a sentence about a DTM that was never
    there to fail.
    """
    if has_lidar_dtm and not has_tiff:
        if not ostn15_obtained:
            reason = "the OSTN15 shift grid it needs could not be obtained"
        else:
            reason = f"it could not be read ({window_error})"
        return (
            f"{dtm_path.name} is on disk, but {reason}, and this package has "
            f"no OpenTopography DEM to fall back to, so no elevation grid "
            f"could be built."
        )
    if has_tiff and not has_lidar_dtm:
        # The pre-Task-8 shape, verbatim: a tiff, and only a tiff, that
        # could not be read. `dem_error` IS `read_dem`'s own message, so
        # this is not paraphrased into a second sentence about it.
        return dem_error or f"{tiff.name} could not be read."
    # Both are on disk and neither could be used.
    dtm_reason = (
        "the OSTN15 shift grid it needs could not be obtained"
        if not ostn15_obtained
        else f"it could not be read ({window_error})"
    )
    return (
        f"Neither raster in this package could be used to build an "
        f"elevation grid: {dtm_path.name} is on disk, but {dtm_reason}; and "
        f"{tiff.name} could not be read ({dem_error})."
    )


# --------------------------------------------------------------------------
# Task 6 of the OS Open pack plan: fusing Overture and OS OpenMap Local
# building footprints into <stem>.osm, BEFORE _fuse_heights_step so an
# injected footprint with no height of its own still gets a DSM-minus-DTM
# one when lidar_wales ran (see both call sites below).
# --------------------------------------------------------------------------

BUILDINGS_SOURCE_OVERTURE = "overture"
BUILDINGS_SOURCE_OS_OPEN = "os_openmap_local"


class BuildingsFusionError(RuntimeError):
    """Raised when `<stem>_building.geojson` or `<stem>_os_buildings.geojson`
    is not the shape `OvertureSource.merge`/`OsOpenSource.merge` themselves
    write: not a FeatureCollection, or a `features` value that is not a
    list.

    Matches `BoundariesFusionError`'s own reasoning exactly: this class
    exists only for the shape this step's OWN new inputs can get wrong.
    `<stem>.osm` reading and rewriting failures raise `heights.HeightsError`
    instead, because `buildings.fuse_missing_buildings` already raises
    that type for them (it does its own full read/parse of the `.osm`,
    the same delegation `_fuse_heights_step` already relies on for
    `fuse_building_heights`, rather than `_fuse_boundaries_step`'s own
    inline `ET.fromstring` call).
    """


def _buildings_record(
    written: int = 0,
    from_overture: int = 0,
    from_os: int = 0,
    kept_existing: int = 0,
    skipped_overlap: int = 0,
    error: str | None = None,
) -> dict[str, object]:
    """survey.json's `buildings_fusion` block, in ONE shape whatever
    happened: six keys, always.

    Deliberately NOT the `int | None` convention `_heights_record`/
    `_boundaries_record` both use for "this step never ran at all": the
    brief's own ruling for this step is that a package with neither
    `<stem>_building.geojson` nor `<stem>_os_buildings.geojson` is an
    ordinary, uneventful outcome (the owner's own default today, with
    `os_open` unselected), not a reader-visible "unknown" the way no
    Welsh LiDAR or no INSPIRE boundaries is. `written: 0` here always
    means "zero footprints were missing, or there was nothing to check
    at all"; only a non-null `error` means the step actually tried and
    could not finish.
    """
    return {
        "written": written,
        "from_overture": from_overture,
        "from_os": from_os,
        "kept_existing": kept_existing,
        "skipped_overlap": skipped_overlap,
        "error": error,
    }


def _load_building_features(geojson_path: Path) -> list:
    """Every feature `<stem>_building.geojson` or
    `<stem>_os_buildings.geojson` holds, in file order, following
    `_load_boundary_features`'s own shape check exactly: both are
    mapgen's own merge output (`OvertureSource.merge`, `OsOpenSource.merge`),
    never hand edited, so a wrong top level shape here is this project's
    own bug and is raised rather than silently read as zero features.

    Each feature's own geometry is validated later, per feature, inside
    `buildings.fuse_missing_buildings` itself: an invalid or empty
    footprint is skipped and counted there (the plan's own "nothing
    fabricated" rule), never raised here, since one malformed feature
    among thousands is real upstream data, not a shape this project's own
    writer got wrong.
    """
    try:
        text = geojson_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildingsFusionError(
            f"{geojson_path.name} could not be read: {exc}"
        ) from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BuildingsFusionError(
            f"{geojson_path.name} is not valid JSON: {exc}"
        ) from None
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise BuildingsFusionError(
            f"{geojson_path.name} is not a GeoJSON FeatureCollection."
        )
    features = payload.get("features")
    if not isinstance(features, list):
        raise BuildingsFusionError(f"{geojson_path.name} has no features list.")
    return features


def _fuse_buildings_step(root: Path, stem: str, sink: ProgressSink) -> dict[str, object]:
    """Inject `<stem>_building.geojson` (Overture) and
    `<stem>_os_buildings.geojson` (OS OpenMap Local) footprints into
    `<stem>.osm`, in that priority order, reported as a record, in the
    same one-shape-whatever-happened style as `_fuse_heights_step` and
    `_fuse_boundaries_step`.

    Unlike those two, this step is never "skipped" as a distinct outcome:
    `<stem>.osm` missing, neither candidate file present, or both present
    with nothing left for either to add are all the same ordinary,
    all-zero record (see `_buildings_record`'s own docstring for why),
    and every one of those still emits `buildings_fusion_started` then
    `buildings_fusion_finished`, never a third event name. Only a genuine
    failure partway through (a malformed `.osm`, a malformed candidate
    GeoJSON, a disk write failure) emits `buildings_fusion_failed`
    instead.

    Runs BEFORE `_fuse_heights_step` in both of its own callers
    (`run_survey` and `bridge_package`): a footprint this step injects
    with no `height` property of its own is exactly the shape
    `fuse_building_heights` already knows how to fill in (any
    `building=*` way with no `height` tag yet, whatever `source=` it
    carries), so the injection has to land first for the heights pass to
    see it in the same run.

    The geometry dedup itself (`buildings.fuse_missing_buildings`) is
    this step's own idempotence guard: a way this step injected on an
    earlier run is, by the next run, just another existing OSM building,
    so re-offering the same candidates fails the dedup rule against it
    and is skipped rather than duplicated. See that function's own module
    docstring for why this is judged equivalent to, rather than a
    departure from, `_fuse_boundaries_step`'s coarser "already tagged,
    skip the whole step" guard.
    """
    osm_path = Path(root) / f"{stem}.osm"
    overture_path = Path(root) / f"{stem}_building.geojson"
    os_path = Path(root) / f"{stem}_os_buildings.geojson"

    sink.emit("buildings_fusion_started")
    if not osm_path.is_file():
        result = _buildings_record()
        sink.emit("buildings_fusion_finished", **result)
        return result

    try:
        candidates: list[tuple[str, list]] = []
        if overture_path.is_file():
            candidates.append(
                (BUILDINGS_SOURCE_OVERTURE, _load_building_features(overture_path))
            )
        if os_path.is_file():
            candidates.append(
                (BUILDINGS_SOURCE_OS_OPEN, _load_building_features(os_path))
            )
        if not candidates:
            result = _buildings_record()
            sink.emit("buildings_fusion_finished", **result)
            return result
        record = fuse_missing_buildings(osm_path, candidates)
    except (HeightsError, BuildingsFusionError, OSError) as exc:
        error = str(exc)
        sink.emit("buildings_fusion_failed", error=error)
        return _buildings_record(error=error)

    result = _buildings_record(
        written=record.written,
        from_overture=record.per_source.get(BUILDINGS_SOURCE_OVERTURE, 0),
        from_os=record.per_source.get(BUILDINGS_SOURCE_OS_OPEN, 0),
        kept_existing=record.kept_existing,
        skipped_overlap=record.skipped_overlap,
    )
    sink.emit("buildings_fusion_finished", **result)
    return result


def _heights_record(
    written: int | None = None,
    buildings: int | None = None,
    kept_existing: int | None = None,
    no_data: int | None = None,
    error: str | None = None,
) -> dict[str, object]:
    """survey.json's `lidar_heights` block, in ONE shape whatever happened,
    following `_elevation_grid_record`'s own ruling exactly: five keys,
    always, so a reader can index any of them on a package this step never
    ran on (no LiDAR, no error) the same way it does on one it fused.

    `relations_skipped` is deliberately not one of the five: it is
    `HeightsRecord`'s own bookkeeping for `heights.py`'s tests, not
    something a reader of survey.json was ever asked to see, and the
    brief's own record shape names exactly these five keys.
    """
    return {
        "written": written,
        "buildings": buildings,
        "kept_existing": kept_existing,
        "no_data": no_data,
        "error": error,
    }


def _fuse_heights_step(root: Path, stem: str, sink: ProgressSink) -> dict[str, object]:
    """Write DSM-minus-DTM building heights into `<stem>.osm`, reported as
    a record, in the same one-shape-whatever-happened style as
    `_write_elevation_grid_step`.

    Runs only when all three files it needs are already in the package:
    `<stem>.osm` (OsmSource.merge), `<stem>_lidar_dtm.tif` and
    `<stem>_lidar_dsm.tif` (LidarWalesSource.merge). Any other combination,
    including the ordinary case of a survey that never selected the
    `lidar_wales` source at all, records the all-None shape and emits
    `heights_fusion_skipped` rather than being treated as a failure: a
    package with no Welsh LiDAR selected is not a package that tried to
    fuse heights and could not.

    The ONLY place in mapgen that calls `fuse_building_heights`, for the
    same reason `_run_bridge_step` is the only caller of `run_bridge`: two
    call sites would have to be kept agreeing forever about which
    failures are survivable and which events are emitted. Runs BEFORE
    `_run_bridge_step` in both of its own callers (`run_survey` and
    `bridge_package`): the bridge converts `<stem>.osm` into Urbano's own
    formats, so the heights have to already be written into it, exactly
    the ordering `_write_elevation_grid_step` already keeps ahead of
    `_write_project_setting_step` for the same kind of reason.

    OSTN15 is read cache-only first (`load_ostn15`) and only fetched
    (`ensure_ostn15`) if that misses: `lidar_wales`'s own fetch() already
    downloaded and cached one for this package, on every ordinary run, so
    the fetch path exists only for `mapgen bridge` on a package moved to a
    machine that has never run a survey. A failure anywhere on this path,
    an unreadable raster, a network failure fetching OSTN15, a malformed
    `.osm`, is caught here and recorded rather than raised: a building
    left flat is exactly the package the owner already had, and losing the
    survey's real data over this step would be the mistake Task 20 already
    fixed once for the bridge.
    """
    osm_path = Path(root) / f"{stem}.osm"
    dtm_path = Path(root) / f"{stem}_lidar_dtm.tif"
    dsm_path = Path(root) / f"{stem}_lidar_dsm.tif"
    if not (osm_path.is_file() and dtm_path.is_file() and dsm_path.is_file()):
        sink.emit("heights_fusion_skipped")
        return _heights_record()

    sink.emit("heights_fusion_started")
    try:
        grid = load_ostn15()
        if grid is None:
            grid = ensure_ostn15()
        dtm_window = read_full_window(CogReader.open(FileByteSource(dtm_path)))
        dsm_window = read_full_window(CogReader.open(FileByteSource(dsm_path)))
        record = fuse_building_heights(osm_path, dtm_window, dsm_window, grid)
    except (HeightsError, CogError, BngError, OSError) as exc:
        error = str(exc)
        sink.emit("heights_fusion_failed", error=error)
        return _heights_record(error=error)

    sink.emit(
        "heights_fusion_written",
        written=record.written,
        buildings=record.buildings,
        kept_existing=record.kept_existing,
        no_data=record.no_data,
    )
    return _heights_record(
        written=record.written,
        buildings=record.buildings,
        kept_existing=record.kept_existing,
        no_data=record.no_data,
    )


# --------------------------------------------------------------------------
# Task 5 of the INSPIRE curves plan: fusing HM Land Registry property
# boundary curves into <stem>.osm, following _fuse_heights_step's own
# pattern exactly (see that function's own docstring, and this task's own
# report for the negative-id verification).
# --------------------------------------------------------------------------

# The exact tag keys and values the plan specifies, verbatim, named rather
# than repeated as literals at each call site: the same convention
# heights.py's own HEIGHT_TAG_KEY/SOURCE_HEIGHT_TAG_KEY establish for its
# tags.
BOUNDARY_TAG_KEY = "boundary"
BOUNDARY_TAG_VALUE = "property"
BOUNDARY_SOURCE_TAG_KEY = "source"
BOUNDARY_SOURCE_TAG_VALUE = "hm_land_registry"
BOUNDARY_NOTE_TAG_KEY = "note"


class BoundariesFusionError(RuntimeError):
    """Raised when `<stem>_boundaries.geojson` is not the shape
    `InspireSource.merge` itself writes: not a FeatureCollection, a
    feature with no LineString geometry, or a LineString with fewer than
    two coordinate pairs.

    A RuntimeError, matching `HeightsError`, `ElevationGridError` and
    every other refusal a post-step in this module records rather than
    propagates (see `_fuse_boundaries_step`'s own docstring). `<stem>.osm`
    reading and rewriting failures raise `heights.HeightsError` instead,
    since `_declaration_line`/`_rewrite_osm` (imported from there rather
    than duplicated) already raise that type; this class exists only for
    the shape this step's OWN new input, the boundaries GeoJSON, can get
    wrong.
    """


def _boundaries_record(
    written: int | None = None,
    curves: int | None = None,
    kept_existing: int | None = None,
    error: str | None = None,
) -> dict[str, object]:
    """survey.json's `inspire_boundaries` block, in ONE shape whatever
    happened, following `_heights_record`'s own ruling exactly: four keys,
    always, so a reader can index any of them on a package this step
    never ran on (no `.osm`, no boundaries GeoJSON, no error) the same way
    it does on one it fused.

    `curves` is how many LineString features `<stem>_boundaries.geojson`
    itself holds, read whether or not this run went on to inject any of
    them (see `_fuse_boundaries_step`'s own idempotence branch): it is a
    fact about the package's own data, not a count of this run's actions,
    which `written` already is.
    """
    return {
        "written": written,
        "curves": curves,
        "kept_existing": kept_existing,
        "error": error,
    }


def _load_boundary_features(geojson_path: Path) -> list:
    """Every feature `<stem>_boundaries.geojson` holds, in file order.

    Only the top level shape is checked here (a JSON object naming
    `"FeatureCollection"` and holding a `features` list): this file is
    mapgen's own output (`InspireSource.merge`), never hand edited, the
    same standing `heights.py` gives `<stem>.osm` itself, so a wrong shape
    here is this project's own bug and is raised rather than silently
    read as zero features. Each feature's own geometry is checked later
    (`_boundary_coordinates`), only for the features this step actually
    goes on to inject: a run whose `kept_existing` branch fires never
    looks at a single coordinate, and has no reason to validate ones it
    will not use.
    """
    try:
        text = geojson_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BoundariesFusionError(
            f"{geojson_path.name} could not be read: {exc}"
        ) from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BoundariesFusionError(
            f"{geojson_path.name} is not valid JSON: {exc}"
        ) from None
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise BoundariesFusionError(
            f"{geojson_path.name} is not a GeoJSON FeatureCollection."
        )
    features = payload.get("features")
    if not isinstance(features, list):
        raise BoundariesFusionError(f"{geojson_path.name} has no features list.")
    return features


def _boundary_note(feature: object) -> str:
    """A feature's own `note` property, or, absent one, the constant
    `InspireSource.merge` itself always writes there today
    (`BOUNDARY_INDICATIVE_NOTE`, imported rather than restated): one
    source of truth for HMLR's own indicative sentence, per the plan.
    """
    properties = feature.get("properties") if isinstance(feature, dict) else None
    if isinstance(properties, dict):
        note = properties.get("note")
        if isinstance(note, str) and note:
            return note
    return BOUNDARY_INDICATIVE_NOTE


def _boundary_coordinates(feature: object, index: int) -> list[tuple[float, float]]:
    """One LineString feature's own vertices, as `(longitude, latitude)`
    pairs in file order: the injected `<node>` shape wants `lat`/`lon`
    directly, and `InspireSource.merge` already writes GeoJSON's own
    `[longitude, latitude]` order, so this is a read, not a projection.

    Raises `BoundariesFusionError`, naming the feature's own index rather
    than any coordinate value, for anything about this one feature this
    function cannot trust: not a LineString, fewer than two coordinate
    pairs (not a line), or a pair that is not exactly two numbers.
    """
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        raise BoundariesFusionError(
            f"Feature {index} in the boundaries GeoJSON is not a LineString."
        )
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise BoundariesFusionError(
            f"Feature {index} in the boundaries GeoJSON has fewer than two "
            f"coordinates."
        )
    points: list[tuple[float, float]] = []
    for pair in coordinates:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise BoundariesFusionError(
                f"Feature {index} in the boundaries GeoJSON has a malformed "
                f"coordinate pair."
            )
        try:
            longitude, latitude = float(pair[0]), float(pair[1])
        except (TypeError, ValueError) as exc:
            raise BoundariesFusionError(
                f"Feature {index} in the boundaries GeoJSON has a "
                f"non-numeric coordinate."
            ) from exc
        points.append((longitude, latitude))
    return points


def _existing_boundary_way_count(root: ET.Element) -> int:
    """Every `<way>` in `root` already tagged `source=hm_land_registry`:
    the idempotence signal the plan asks for. A prior fusion's own ways
    are identifiable by that one tag alone, whether the download itself
    wrote them or an earlier `mapgen bridge` did.
    """
    count = 0
    for element in root:
        if element.tag != "way":
            continue
        for tag in element.findall("tag"):
            if (
                tag.get("k") == BOUNDARY_SOURCE_TAG_KEY
                and tag.get("v") == BOUNDARY_SOURCE_TAG_VALUE
            ):
                count += 1
                break
    return count


def _fuse_boundaries_step(root: Path, stem: str, sink: ProgressSink) -> dict[str, object]:
    """Inject `<stem>_boundaries.geojson`'s own curves into `<stem>.osm`
    as tagged ways, reported as a record, in the same
    one-shape-whatever-happened style as `_fuse_heights_step`.

    Runs only when both files it needs are already in the package:
    `<stem>.osm` (OsmSource.merge, or an earlier bridge's own output) and
    `<stem>_boundaries.geojson` (InspireSource.merge). Any other
    combination, including the ordinary case of a survey that never
    selected the `inspire` source at all, records the all-None shape and
    emits `boundaries_fusion_skipped` rather than being treated as a
    failure, matching `_fuse_heights_step`'s own reasoning for the
    identical gate.

    Each LineString feature becomes fresh `<node>` elements for its own
    vertices (never shared with another feature's nodes, even where two
    curves happen to touch at the same point: the brief's own mechanics
    say a LineString becomes node elements for ITS vertices, and
    de-duplicating shared endpoints across features is a refinement
    nothing here asks for) plus one `<way>` tagged `boundary=property`,
    `source=hm_land_registry` and `note=` (see `_boundary_note`). Ids are
    negative, descending from -1, through one counter shared by nodes and
    ways.

    **Negative ids, verified rather than assumed.** `docs/urbano/README.md`
    names Urbano's OSM reader (`OsmExtension.ImportOsmGeometries`) but says
    nothing about id sign; its own report,
    `.superpowers/sdd/2026-08-01-mapgen-phase1/task-34-report.md`, is what
    identifies that reader as `OsmSharp.Streams.XmlOsmStreamSource`, an
    unmodified, unobfuscated copy of the real OsmSharp library merged into
    `Urbano.SiteAnalysis.gha`. Decompiling that exact type (ilspycmd, this
    task's own report) settles the rest directly: `OsmGeo.Id` is `long?`;
    `Node.ReadXml`, `Way.ReadXml` and the `nd ref` list all read it through
    `XmlExtensions.GetAttributeInt64`, which is
    `long.TryParse(text, NumberStyles.Any, CultureInfo.InvariantCulture,
    out result)`, and `NumberStyles.Any` allows a leading sign; and
    `OsmSharp.Streams.Collections.OsmIdIndex.Add(long number)` branches
    explicitly on `number >= 0` with a SEPARATE path for negative numbers,
    which only makes sense if negative ids are a deliberately supported
    case, not an accident of the field being a signed integer. Negative,
    descending ids are therefore safe, and no positive-id fallback is
    implemented.

    **Collision safety, not merely readability.** Reading right is not
    the same question as landing somewhere unused. The idempotence guard
    below only recognises a PRIOR RUN OF THIS FUNCTION, because only the
    ways it writes carry the `source=hm_land_registry` tag; a negative id
    introduced by any OTHER route (a hand-edited `.osm`, a foreign tool
    using the same negative-descending convention, a future sibling
    feature doing the same) is invisible to that guard. Starting a fresh
    counter at a bare -1 regardless of what is already in the file would
    collide with such an id silently: two elements sharing one id is
    legal-looking XML that `OsmSharp.Streams.Collections.OsmIdIndex`
    (see above) then indexes ambiguously, since it is built to answer
    "does this id exist" and "resolve this id to an element", not
    "which of the two elements that both claim it did the caller mean".
    `_minimum_existing_id` closes this: the counter starts one below the
    file's own actual minimum id, across every element type and both
    signs, so an injected id always sits strictly below everything
    already present, no matter who put it there or why.

    Idempotent by tag detection, exactly like `_fuse_heights_step`'s own
    `height` tag: any existing way already tagged
    `source=hm_land_registry` means a previous run (the download itself,
    or an earlier `mapgen bridge`) already injected these curves, so
    EVERY such way is counted as `kept_existing`, nothing is injected, and
    the file is never even opened for writing, which is what makes a
    second run byte-identical to the first rather than merely equivalent
    to it.

    Reuses `heights._declaration_line` and `heights._rewrite_osm` rather
    than a second copy of them: both are small, module-private helpers,
    but the atomic-write shape they implement
    (`fsutil.atomic_write_bytes`, one line per top level element, in file
    order) is delicate enough that an independently maintained second copy
    risks drifting from the first. One consequence of that reuse, carried
    over unchanged rather than fixed by this task: `ET.tostring` on a
    parsed element includes that element's own trailing whitespace
    (`.tail`, the newline-plus-indent between two sibling elements in the
    ORIGINAL file), so every EXISTING top level element gains a
    whitespace-only blank line after it once `_rewrite_osm` runs at all,
    while the NEW node/way elements this step appends carry no `.tail` of
    their own and stay on one clean line each. The result is cosmetically
    uneven and functionally identical: OSM XML's own whitespace between
    elements is insignificant, and `OsmSharp.Streams.XmlOsmStreamSource`
    parses it as such. `_fuse_heights_step`'s own runs already produce
    this same quirk in production; matching it here rather than diverging
    is the point.

    A failure anywhere on this path (an unreadable or malformed `.osm`, a
    malformed boundaries GeoJSON, a disk write failure) is caught here and
    recorded rather than raised: a package with no injected boundaries is
    exactly the package the owner already had, and losing the survey's
    real data over this step would be the same mistake Task 20 already
    fixed once for the bridge.
    """
    osm_path = Path(root) / f"{stem}.osm"
    geojson_path = Path(root) / f"{stem}_boundaries.geojson"
    if not (osm_path.is_file() and geojson_path.is_file()):
        sink.emit("boundaries_fusion_skipped")
        return _boundaries_record()

    sink.emit("boundaries_fusion_started")
    try:
        text = osm_path.read_text(encoding="utf-8")
        declaration = _declaration_line(text)
        osm_root = ET.fromstring(text)
        features = _load_boundary_features(geojson_path)
        curves = len(features)
        kept_existing = _existing_boundary_way_count(osm_root)

        written = 0
        if kept_existing == 0:
            # Strictly below every id already in the file, positive or
            # negative, not merely below -1 (see _minimum_existing_id's
            # own docstring and this function's "collision safety"
            # paragraph above): a bare -1 would silently collide with a
            # foreign negative id this run has no way to know about.
            next_id = _minimum_existing_id(osm_root) - 1
            new_nodes: list[ET.Element] = []
            new_ways: list[ET.Element] = []
            for index, feature in enumerate(features):
                points = _boundary_coordinates(feature, index)
                note = _boundary_note(feature)

                way_id = next_id
                next_id -= 1
                way = ET.Element("way")
                way.set("id", str(way_id))
                for longitude, latitude in points:
                    node_id = next_id
                    next_id -= 1
                    node = ET.Element("node")
                    node.set("id", str(node_id))
                    node.set("lat", f"{latitude:.7f}")
                    node.set("lon", f"{longitude:.7f}")
                    new_nodes.append(node)
                    nd = ET.SubElement(way, "nd")
                    nd.set("ref", str(node_id))
                for key, value in (
                    (BOUNDARY_TAG_KEY, BOUNDARY_TAG_VALUE),
                    (BOUNDARY_SOURCE_TAG_KEY, BOUNDARY_SOURCE_TAG_VALUE),
                    (BOUNDARY_NOTE_TAG_KEY, note),
                ):
                    tag = ET.SubElement(way, "tag")
                    tag.set("k", key)
                    tag.set("v", value)
                new_ways.append(way)
                written += 1

            if written > 0:
                for node in new_nodes:
                    osm_root.append(node)
                for way in new_ways:
                    osm_root.append(way)
                _rewrite_osm(osm_path, declaration, osm_root)
    except (HeightsError, BoundariesFusionError, ET.ParseError, OSError) as exc:
        error = str(exc)
        sink.emit("boundaries_fusion_failed", error=error)
        return _boundaries_record(error=error)

    sink.emit(
        "boundaries_fusion_written",
        written=written,
        curves=curves,
        kept_existing=kept_existing,
    )
    return _boundaries_record(written=written, curves=curves, kept_existing=kept_existing)


def _inspire_boundaries_year(root: Path, stem: str) -> int | None:
    """The publication year recorded in `<stem>_boundaries.geojson`'s own
    feature properties, or None if the file is absent, unreadable, or (an
    authority set with zero kept parcels in this padded extent) carries a
    real, empty FeatureCollection with no feature anywhere to read a year
    from.

    Read from the package's own merged output, not from work_dir's
    meta.json: the latter is swept once a complete run's `_work/` is
    removed (see `run_survey`'s own cleanup, near its end) and never
    exists at all for `mapgen bridge` against a package downloaded
    elsewhere, so it is not a fact this function could rely on being
    there. The GeoJSON is a package ROOT file and is what both of this
    function's callers (`_build_survey_json`, `bridge_package`) already
    have on disk.
    """
    path = Path(root) / f"{stem}_boundaries.geojson"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return None
    for feature in features:
        if isinstance(feature, dict):
            properties = feature.get("properties")
            if isinstance(properties, dict):
                year = properties.get("year")
                if isinstance(year, int):
                    return year
    return None


def _enrich_inspire_provenance(
    sources_entries: list, root: Path, stem: str, conditions_url: str | None
) -> None:
    """Substitutes `[year]` in the "inspire" entry of survey.json's own
    `sources` list and adds its `conditions_url`, in place, if that entry
    is there at all.

    Deliberately its own function rather than an extension of
    `_source_provenance` (see `InspireSource.conditions_url`'s own
    docstring in `mapgen/sources/inspire.py`, which names this exact
    seam): every OTHER optional attribute `_source_provenance` reads
    (`endpoints_used`, `demtype`, `routing_note`, ...) is read off the
    source object alone and applies however many sources choose to expose
    it. A `[year]` substitution needs THIS package's own merged
    boundaries GeoJSON, not merely an attribute on the source object, so
    folding it into that generic function would mean `_source_provenance`
    reading a file off disk on every source's behalf, for a fact only one
    source has. `conditions_url` genuinely could have gone through the
    generic path (it is a plain attribute, read defensively); it lives
    here instead so the two facts the plan asks be delivered together
    (the substituted attribution and the conditions link) are written by
    one piece of code rather than two that could drift apart.

    Called from both `_build_survey_json` (a fresh run) and
    `bridge_package` (re-run over an existing survey.json), which is why
    it takes plain data (a list of dicts, a root and stem, an
    already-resolved `conditions_url`) rather than a `sources` list of
    LayerSource objects: `bridge_package` never has the latter, only the
    JSON payload a previous run already wrote.
    """
    entry = next(
        (
            item
            for item in sources_entries
            if isinstance(item, dict) and item.get("id") == "inspire"
        ),
        None,
    )
    if entry is None:
        return
    attribution = entry.get("attribution")
    if isinstance(attribution, str) and "[year]" in attribution:
        year = _inspire_boundaries_year(root, stem)
        if year is not None:
            entry["attribution"] = attribution.replace("[year]", str(year))
    if conditions_url is not None:
        entry["conditions_url"] = conditions_url


_VERSION_YEAR_RE = re.compile(r"^(\d{4})-")


def _version_year(version: str) -> int | None:
    """The 4-digit year an OS Open `"YYYY-MM"` version string starts
    with, or `None` for anything else: defensive, since this reads a
    string a source object handed over, not a value this module itself
    validated when it was set.
    """
    match = _VERSION_YEAR_RE.match(version)
    return int(match.group(1)) if match else None


def _enrich_os_product_provenance(
    sources_entries: list, source_id: str, versions_by_product: Mapping[str, str] | None
) -> None:
    """Substitutes `[year]` in the `source_id` entry of survey.json's own
    `sources` list (`"os_open"` or `"os_uprn"`) using the LATEST product
    version year this run actually used, and records the exact per-product
    versions alongside as `"products"`, in place, when that entry is
    there and `versions_by_product` is non-empty.

    Final review, Important I1: Task 4's own brief deferred this exact
    substitution to "Task 6 wires the record", and Task 6 (the buildings
    fusion) never carried it; no `_enrich_os_open_provenance` existed
    anywhere on the branch, and every survey.json selecting os_open or
    os_uprn shipped the literal, unsubstituted `"[year]"`.

    Mirrors `_enrich_inspire_provenance`'s own shape (a small,
    source-specific enrichment pass over already-built dict entries, not
    a generic `_source_provenance` extension: see that function's own
    docstring for why) at a coarser grain, and covers BOTH os_open and
    os_uprn with one function rather than two near-identical copies,
    since the shape of what each needs is identical: a mapping of product
    name to the version string that product was actually sharded under.

    Unlike `_enrich_inspire_provenance`, which reads its year back off
    the merged `<stem>_boundaries.geojson` file itself (a fact that file
    happens to carry as a feature property), os_open's and os_uprn's own
    merged GeoJSON outputs never carry a "year" property at all (the
    brief's own closed, per-file property lists never name one; see
    os_open.py's module docstring, "Attribution"), so there is no file on
    disk this function could read a year back out of. The SOURCE
    OBJECT's own `versions_used` attribute (set at fetch() time, the
    moment each product's needed squares all reach `shards_complete`
    under one resolved version) is the one thing that knows which real OS
    Open publication this survey's data actually came from, and that
    fact exists only on the LIVE source object a fresh run holds.
    Consequently, a `bridge_package` re-run over an existing survey.json
    (no live source object at all, only its own prior JSON payload)
    cannot recover it either, and this function is not called from there:
    it leaves the placeholder exactly as it found it, the same as the
    "versions_by_product empty or None" branch below does for a run where
    the source was selected but never actually produced a versioned
    product (every unit failed, or the source was never selected at all
    and has no entry to find).

    Version skew (final review, seam trace 2): `versions_by_product` can
    legitimately name more than one distinct year when a survey straddles
    an OS monthly re-publication (OpenMapLocal resolved under N,
    OpenRoads under N+1, say). The LATEST year among them is substituted,
    since Crown copyright and database right attribution cares about the
    most recent publication date this package's data falls under, not the
    oldest; every individual product's own version is still recorded
    verbatim in `"products"`, so a skewed run is never hidden, only
    resolved to the one year an attribution STATEMENT can actually hold.
    """
    entry = next(
        (
            item
            for item in sources_entries
            if isinstance(item, dict) and item.get("id") == source_id
        ),
        None,
    )
    if entry is None or not versions_by_product:
        return
    entry["products"] = dict(versions_by_product)
    attribution = entry.get("attribution")
    if isinstance(attribution, str) and "[year]" in attribution:
        years = [
            year for year in (_version_year(version) for version in versions_by_product.values())
            if year is not None
        ]
        if years:
            entry["attribution"] = attribution.replace("[year]", str(max(years)))


def _write_project_setting_step(
    bbox: BBox, root: Path, stem: str, sink: ProgressSink
) -> dict[str, object]:
    """Write `<stem>_project_setting.json`, reported the way the bridge step
    is reported: as a record, never as an exception that costs the package.

    The ONLY place in mapgen that calls write_project_setting, for the same
    reason _run_bridge_step is the only place that calls run_bridge: two call
    sites would have to be kept agreeing about which failures are survivable
    and which events are emitted, forever, with nothing but vigilance holding
    them together.

    **Which writer wins.** Both this and the C# bridge write the same file
    name. This one runs second, so it wins, and that is the intended
    ordering rather than an accident of layout:

      * The bridge has never once succeeded on the owner's machine, so on
        every run to date there is nothing of its to overwrite.
      * When it does succeed, it names the files IT produced. This names
        whatever is actually in the folder, preferring the bridge's own
        native formats where they are there (see mapgen.urbano.DATA_FILES),
        so the result is a superset rather than a downgrade: a successful
        bridge run's `.osm.pbf`, `.egrid` and `.parquet` are all still
        named, and mapgen's own `.osm` and `.geojson` are named too where
        the bridge produced nothing. The DEM is the one output never named:
        Urbano's elevation field is a protobuf `ElevationGrid` and a
        GeoTIFF in it crashes every component that reads it (task 37).
      * A failure here is recorded and the package is finished anyway,
        exactly as a bridge failure is. Losing a survey's real data because
        a JSON file could not be written would be the same mistake task 20
        already fixed once.

    OSError is caught alongside ProjectSettingError because this writes to
    the owner's own output root, where a full disk, a permission and a file
    held open by something else are all ordinary rather than exotic.
    """
    sink.emit("project_setting_started")
    try:
        written = write_project_setting(bbox, root, stem)
    except (ProjectSettingError, OSError) as exc:
        error = str(exc)
        sink.emit("project_setting_failed", error=error)
        return {"written": False, "file": None, "layers": [], "error": error}
    # Re-read off the disk rather than returned by the writer, so this record
    # is derived by the same function the file itself was, and the two cannot
    # form different opinions about which layers a package holds.
    layers = [layer for layer in LAYER_ORDER if layer in resolve_data_files(Path(root), stem)]
    sink.emit("project_setting_written", file=written.name, layers=layers)
    return {"written": True, "file": written.name, "layers": layers, "error": None}


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


# --------------------------------------------------------------------------
# Task 3 of the 2026-08-07 categorised boundaries plan: classifying every
# parcel `<stem>_parcels.geojson` persisted (Task 1) against the package's
# own overlay layers (`classify.py`, Task 2), and writing
# `<stem>_boundaries_categorised.geojson`: the same pinch-point-safe curve
# machinery `InspireSource.merge` already uses (`boundary_curves.
# boundary_curves_with_counts`), run once PER CATEGORY, so a shared wall
# between a garden parcel and a field parcel survives once under EACH
# category rather than being deduplicated away between them (the plan's
# own point: filtering Grasshopper's import to one category stays a
# complete, closed set of outlines).
# --------------------------------------------------------------------------

BOUNDARY_CATEGORIES_NOTE = "derived from map overlay, indicative"


class BoundaryCategoriesError(RuntimeError):
    """Raised when `<stem>_parcels.geojson`, or one of the optional overlay
    files this step reads (`<stem>_land_use.geojson`, `<stem>_water.geojson`,
    `<stem>_os_greenspace.geojson`, `<stem>_os_land.geojson`), is not the
    shape its own writer produces: not a FeatureCollection, a `features`
    value that is not a list, or (parcels only) a feature whose geometry is
    not a Polygon.

    Matches `BoundariesFusionError`'s and `BuildingsFusionError`'s own
    reasoning exactly: every one of these files is mapgen's own merged
    output, never hand edited, so a wrong shape here is this project's own
    bug and is raised rather than silently read as empty. `<stem>.osm`
    reading failures raise `xml.etree.ElementTree.ParseError` instead,
    caught alongside this one in `_categorise_boundaries_step`'s own try
    block, the same split `_fuse_boundaries_step` already keeps between its
    own new-input errors and its inline `ET.fromstring` call.
    """


def _categories_record(
    parcels: int = 0,
    counts: dict[str, int] | None = None,
    samples: int = 0,
    capped: int = 0,
    error: str | None = None,
) -> dict[str, object]:
    """survey.json's `boundaries_categories` block, in ONE shape whatever
    happened, following `_buildings_record`'s own "always the same keys"
    ruling: six keys, always, so a reader can index any of them on a
    package with no `inspire` layer at all the same way it does on one this
    step actually classified.

    `note` breaks from every other value here on purpose: it is the fixed
    sentence the plan's own "Not promised" clause requires wherever a
    category appears (`BOUNDARY_CATEGORIES_NOTE`), never `None`, because the
    caveat is true of this record's own existence, not of whatever this run
    happened to find. `parcels`, `counts`, `samples` and `capped` all read
    as zero, not `None`, when this step never ran at all (no
    `<stem>_parcels.geojson`): a package without `inspire` selected simply
    has no parcels, a fact rather than a gap, matching the brief's own
    ruling for that exact case.
    """
    return {
        "parcels": parcels,
        "counts": dict(counts) if counts else {},
        "samples": samples,
        "capped": capped,
        "note": BOUNDARY_CATEGORIES_NOTE,
        "error": error,
    }


def _read_categorised_feature_collection(path: Path) -> list:
    """Every feature `path` holds, in file order: the one shape check every
    GeoJSON `_categorise_boundaries_step` reads shares (parcels, land_use,
    water, os_greenspace, os_land), following `_load_boundary_features`'s
    and `_load_building_features`'s own identical ruling: each of these
    files is mapgen's own merged output, never hand edited, so a wrong top
    level shape here is this project's own bug and is raised rather than
    silently read as zero features.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BoundaryCategoriesError(f"{path.name} could not be read: {exc}") from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BoundaryCategoriesError(f"{path.name} is not valid JSON: {exc}") from None
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise BoundaryCategoriesError(f"{path.name} is not a GeoJSON FeatureCollection.")
    features = payload.get("features")
    if not isinstance(features, list):
        raise BoundaryCategoriesError(f"{path.name} has no features list.")
    return features


def _read_optional_categorised_feature_collection(path: Path) -> list:
    """`_read_categorised_feature_collection(path)`, or `[]` if `path` is
    not there at all: every overlay file this step reads is optional (the
    controller's own ruling: "every file optional; absence contributes an
    empty set"), unlike `<stem>_parcels.geojson` itself, whose presence is
    what gates this step running at all (see `_categorise_boundaries_step`).
    """
    return _read_categorised_feature_collection(path) if path.is_file() else []


@dataclass(frozen=True)
class _ParcelPolygon:
    """One `<stem>_parcels.geojson` feature: its own exterior ring (WGS84
    lon/lat, unclosed) plus the three properties `_boundaries_categorised.
    geojson`'s own features carry through verbatim (`source`, `note`,
    `year`): read once here rather than retyped as a literal string
    anywhere in this step, so a future change to `InspireSource`'s own
    wording, or a different survey's own `year`, never needs a second edit
    here to match.
    """

    ring: Ring
    source: object
    note: object
    year: object


def _load_parcel_polygons(path: Path) -> list[_ParcelPolygon]:
    """Every parcel `<stem>_parcels.geojson` holds, in file order.

    Every feature's geometry is a Polygon: Task 1 writes exterior-only
    Polygons, a MultiPolygon parcel does not exist in this file at all, so
    this makes no attempt to accept one, and raises on anything else (the
    same strict, per-feature standard `_boundary_coordinates` already holds
    `<stem>_boundaries.geojson`'s own LineString features to, since both
    files are mapgen's own single-authorial output, not third-party
    geometry merely copied through). Read via `buildings._exterior_ring`,
    the identical helper `_fuse_buildings_step`'s own candidate loader
    relies on, rather than a second copy of "read a GeoJSON Polygon's own
    exterior ring, drop its closing duplicate, refuse fewer than 3 distinct
    vertices".
    """
    parcels: list[_ParcelPolygon] = []
    for index, feature in enumerate(_read_categorised_feature_collection(path)):
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
            raise BoundaryCategoriesError(f"Feature {index} in {path.name} is not a Polygon.")
        ring = _exterior_ring(geometry.get("coordinates"))
        if ring is None:
            raise BoundaryCategoriesError(
                f"Feature {index} in {path.name} has no usable exterior ring."
            )
        properties = feature.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        parcels.append(
            _ParcelPolygon(
                ring=ring,
                source=properties.get("source"),
                note=properties.get("note"),
                year=properties.get("year"),
            )
        )
    return parcels


def _load_polygon_overlay_rings(path: Path) -> list[Ring]:
    """Every exterior ring an optional Polygon/MultiPolygon overlay file
    holds (`<stem>_water.geojson`, `<stem>_os_greenspace.geojson`), via
    `buildings._candidate_rings`, the identical Polygon-or-MultiPolygon
    reader `fuse_missing_buildings` already uses for a candidate footprint:
    a Point feature (`_os_greenspace.geojson`'s own `AccessPoint` rows)
    contributes nothing at all, since that function only ever recognises
    `"Polygon"`/`"MultiPolygon"` geometry, which is what keeps an access
    point out of the greenspace overlay without a second, geometry-type
    check of its own here (the controller's own "only Polygon features
    (skip AccessPoint points)" ruling, satisfied by reuse rather than a
    restated check).
    """
    rings: list[Ring] = []
    for feature in _read_optional_categorised_feature_collection(path):
        feature_rings, _invalid = _candidate_rings(feature)
        rings.extend(feature_rings)
    return rings


def _load_landuse_pairs(path: Path) -> list[tuple[str, Ring]]:
    """`<stem>_land_use.geojson`'s own features as `(class_name, ring)`
    pairs, `class_name` read from each feature's own `class` property,
    falling back to `subtype` when `class` is absent (the controller's own
    ruling for this file): a feature with neither contributes no pair at
    all, the identical "no entry, not a bucket that loses" treatment
    `classify.py`'s own `_flat_candidates` already gives an unrecognised
    raw class string.
    """
    pairs: list[tuple[str, Ring]] = []
    for feature in _read_optional_categorised_feature_collection(path):
        properties = feature.get("properties") if isinstance(feature, dict) else None
        class_name = None
        if isinstance(properties, dict):
            class_name = properties.get("class") or properties.get("subtype")
        if not isinstance(class_name, str) or not class_name:
            continue
        feature_rings, _invalid = _candidate_rings(feature)
        pairs.extend((class_name, ring) for ring in feature_rings)
    return pairs


def _load_woodland_rings(path: Path) -> list[Ring]:
    """`<stem>_os_land.geojson`'s own `kind == "woodland"` features, as
    exterior rings: every other `kind` this file can hold (`water_area`,
    `water_line`, `tidal_water`, `foreshore`; see `os_open.
    _LAND_KIND_BY_TYPE`) is OS OpenMapLocal Land theme data this plan's own
    priority order never asks for, and is left unread.
    """
    rings: list[Ring] = []
    for feature in _read_optional_categorised_feature_collection(path):
        properties = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(properties, dict) or properties.get("kind") != "woodland":
            continue
        feature_rings, _invalid = _candidate_rings(feature)
        rings.extend(feature_rings)
    return rings


def _osm_node_positions(osm_root: ET.Element) -> dict[str, tuple[float, float]]:
    """`osm_root`'s own `<node>` elements as `{id: (lon, lat)}`, the exact
    small collection loop `buildings._existing_building_footprints` builds
    inline, for the identical reason: `_resolve_way_ring` needs it to turn
    a way's `nd ref` list into real coordinates, and this step needs it for
    TWO different tag filters (`building=*` and `natural=water`) over the
    SAME file, so it is built once here rather than twice, or pulled out of
    a function whose own return value is scoped to buildings alone.
    """
    nodes: dict[str, tuple[float, float]] = {}
    for element in osm_root:
        if element.tag != "node":
            continue
        node_id = element.get("id")
        lat = element.get("lat")
        lon = element.get("lon")
        if node_id is None or lat is None or lon is None:
            continue
        try:
            nodes[node_id] = (float(lon), float(lat))
        except ValueError:
            continue
    return nodes


def _osm_building_and_water_rings(osm_root: ET.Element) -> tuple[list[Ring], list[Ring]]:
    """Every `building=*` way and every CLOSED `natural=water` way already
    in `osm_root`, resolved to rings via `_resolve_way_ring`
    (`buildings.py`'s own node-ref resolver, shared with
    `_existing_building_footprints` rather than re-walked here): building
    ways regardless of their own `source=` tag, an owner's real OSM way or
    one `_fuse_buildings_step` already injected from Overture or OS
    OpenMapLocal read identically (the controller's own "regardless of
    source tag" ruling). Plain ways only, no relations: the controller's
    own wording names "ways", not "ways or relations", and every building
    this step needs is one already, since a footprint `_fuse_buildings_step`
    injects is always a plain way.

    A `natural=water` way is only read when its own `nd` list is closed
    (first ref equal to last): an open way is a river centreline rather
    than a lake outline, real OSM data this classifier has no use for and
    no way to sample a ring from. `_resolve_way_ring`'s own closing-
    duplicate drop only removes a repeat that IS there, so this checks the
    way was closed to begin with rather than trusting an accidental
    coincidence between an open way's first and last node ids.
    """
    nodes = _osm_node_positions(osm_root)
    buildings: list[Ring] = []
    water: list[Ring] = []
    for element in osm_root:
        if element.tag != "way":
            continue
        tags = {tag.get("k"): tag.get("v") for tag in element.findall("tag")}
        if "building" in tags:
            ring = _resolve_way_ring(element, nodes)
            if ring is not None:
                buildings.append(ring)
        elif tags.get("natural") == "water":
            refs = [nd.get("ref") for nd in element.findall("nd")]
            if len(refs) > 1 and refs[0] == refs[-1]:
                ring = _resolve_way_ring(element, nodes)
                if ring is not None:
                    water.append(ring)
    return buildings, water


def _load_categorise_overlays(root: Path, stem: str) -> OverlaySets:
    """This package's own `OverlaySets`, read off whatever the package
    actually holds: every file named here is optional (the controller's
    own ruling, "every file optional; absence contributes an empty set"),
    so a survey that never selected `overture` or `os_open` classifies
    every parcel `unclassified` rather than failing (`classify_parcels`
    itself already reads "nothing to compare against" as "no evidence",
    never a guess).
    """
    root = Path(root)
    building_rings: list[Ring] = []
    water_rings: list[Ring] = []
    osm_path = root / f"{stem}.osm"
    if osm_path.is_file():
        osm_root = ET.fromstring(osm_path.read_text(encoding="utf-8"))
        building_rings, water_rings = _osm_building_and_water_rings(osm_root)

    water_rings = water_rings + _load_polygon_overlay_rings(root / f"{stem}_water.geojson")

    return OverlaySets(
        buildings=building_rings,
        landuse=_load_landuse_pairs(root / f"{stem}_land_use.geojson"),
        water=water_rings,
        greenspace=_load_polygon_overlay_rings(root / f"{stem}_os_greenspace.geojson"),
        woodland=_load_woodland_rings(root / f"{stem}_os_land.geojson"),
    )


def _rings_lat0(rings: Sequence[Ring]) -> float:
    """The one reference latitude every ring `_categorise_boundaries_step`
    projects to local metres against (see that function's own docstring
    for why a SHARED value, not one computed per ring, is what makes a
    vertex two neighbouring parcels both carry project to the identical
    point): the mean of each ring's own bounding-box mid-latitude. `0.0`
    for empty `rings`, never actually reached in practice (this step
    already refuses to run at all with zero parcels; see its own "no
    parcels" branch), kept only so this function has a total, honest
    answer for an input it is never asked about for real.
    """
    midpoints = [
        (min(lat for _lon, lat in ring) + max(lat for _lon, lat in ring)) / 2.0
        for ring in rings
        if ring
    ]
    return sum(midpoints) / len(midpoints) if midpoints else 0.0


def _to_local_metres(point: tuple[float, float], lat0: float) -> tuple[float, float]:
    """`point` (lon, lat) projected to an approximate local metric plane:
    the identical cos-latitude approximation `classify.py`'s own sampling
    grid already uses (`classify._METRES_PER_DEGREE_LAT`, `classify.
    _metres_per_degree_lon`, imported rather than restated, so the two
    modules' idea of "how many metres in a degree here" can never drift
    apart), reused here for a different reason: `boundary_curves.py`'s own
    rounding (1 cm) and collinear tolerance (5 cm) are metre-scale
    constants, and handing `boundary_curves_with_counts` raw WGS84 degrees
    directly would round every parcel's own vertices to the nearest
    HUNDREDTH OF A DEGREE, on the order of a kilometre at this latitude,
    collapsing every real parcel to fewer than 3 distinct points before
    stage 1 of that pipeline ever runs. Projecting first, and projecting
    every parcel this step ever hands to that pipeline through the SAME
    `lat0` (never a per-parcel one), is what lets that unmodified,
    metre-tuned function be reused at all here rather than reimplemented at
    a different scale: Task 3's own brief calls for exactly the one
    function, not a second copy of it at a different tolerance. Not a
    survey-grade transform, matching every other approximation this plan's
    own header already accepts (see classify.py's module docstring,
    "Sampling"): this whole file is explicitly labelled `derived from map
    overlay, indicative` wherever it appears.
    """
    longitude, latitude = point
    return (longitude * _metres_per_degree_lon(lat0), latitude * _METRES_PER_DEGREE_LAT)


def _from_local_metres(point: tuple[float, float], lat0: float) -> tuple[float, float]:
    """The exact algebraic inverse of `_to_local_metres`, at the same
    `lat0`: unlike `bng.from_bng`'s own OSTN15 fixed-point iteration, this
    projection is a plain linear scale with no grid lookup, so its inverse
    is exact (to floating-point precision) rather than approximate, and
    needs no network access or cached grid file at all, which is what keeps
    this whole step inside the plan's own "no network in any package step"
    global constraint.
    """
    x, y = point
    return (x / _metres_per_degree_lon(lat0), y / _METRES_PER_DEGREE_LAT)


def _categorise_boundaries_step(root: Path, stem: str, sink: ProgressSink) -> dict[str, object]:
    """Classify every `<stem>_parcels.geojson` parcel (Task 1) against this
    package's own overlay layers (`classify.classify_parcels`, Task 2),
    group the parcels by the category that comes back, and write
    `<stem>_boundaries_categorised.geojson`: EACH category's own group run
    once through `boundary_curves.boundary_curves_with_counts` (item 2),
    separately, so a shared wall between two DIFFERENT categories' parcels
    survives once under EACH of them rather than being deduplicated away
    between them (the plan's own point: filtering Grasshopper's import to
    one category alone stays a complete, closed set of outlines).

    Runs only when `<stem>_parcels.geojson` is in the package at all (Task
    1 never writes it for a survey that never selected `inspire`, or that
    selected it and kept zero parcels in this extent): the all-zero record
    and no output file, matching `_fuse_buildings_step`'s own "started then
    finished, never a third event name" convention exactly, is this step's
    ordinary, uneventful outcome for that case, not a failure.

    Every overlay file this step reads (`<stem>_land_use.geojson`,
    `<stem>_water.geojson`, `<stem>_os_greenspace.geojson`,
    `<stem>_os_land.geojson`) is optional, and its own absence contributes
    an empty set to `OverlaySets` rather than a refusal.

    Building and water rings are read out of the FUSED `.osm`, deliberately
    not `<stem>_building.geojson`: `_fuse_buildings_step` runs immediately
    before this step in both `run_survey` and `bridge_package` (see both
    call sites), and its whole job is folding Overture's and OS
    OpenMapLocal's own footprints into `.osm` as plain `building=*` ways,
    so reading `.osm` after it has run sees every building this package
    knows about, sourced from OSM itself or injected, with no need for this
    step to separately open two more candidate files and reconcile them
    against what buildings fusion already decided to keep.

    Idempotent by full recomputation, not by tag detection: unlike
    `_fuse_boundaries_step`/`_fuse_buildings_step`, which mutate `.osm` in
    place and must recognise their own earlier work to avoid re-injecting
    it, this step's own output is a fresh, standalone file, deterministic
    in every input it reads (`boundary_curves`'s own "Deterministic output"
    guarantee, `classify_parcels`'s agreement with `classify_parcel`, and
    this step's own single shared `lat0`), so simply overwriting it every
    run already IS byte-identical for byte-identical inputs, with no
    "already done" guard needed at all.

    `source`/`note`/`year` on every written feature come from the FIRST
    parcel `<stem>_parcels.geojson` itself carries, never retyped as a
    literal string here: every parcel in one package's own file shares the
    identical triple (Task 1 writes it from one INSPIRE download's own
    single `year` and the one constant HMLR attribution/caveat), so any one
    of them is the whole file's own answer.

    A failure anywhere on this path (a malformed `<stem>_parcels.geojson`
    or overlay file, a malformed `.osm`, a disk write failure) is caught
    here and recorded rather than raised, matching every other post-step in
    this module: a package without a categorised file is exactly the
    package the owner already had, and losing the survey's real data over
    this step would be the same mistake Task 20 already fixed once for the
    bridge. The rest of the package, including a PRIOR run's own
    `<stem>_boundaries_categorised.geojson`, is left untouched on a failure
    here, the same "untouched" guarantee `_fuse_boundaries_step`'s own
    tests already pin for `.osm`.
    """
    root = Path(root)
    parcels_path = root / f"{stem}_parcels.geojson"
    output_path = root / f"{stem}_boundaries_categorised.geojson"

    sink.emit("boundaries_categories_started")
    if not parcels_path.is_file():
        # No inspire parcels in this package: an ordinary outcome (the
        # owner never selected `inspire`, or selected it and kept none in
        # this extent), not a failure, matching Task 1's own "zero parcels
        # -> no file" rule. A stale file a PRIOR run of this same package
        # left behind no longer describes data this package still has, so
        # it goes, the identical stale-output discipline `InspireSource.
        # merge` itself already applies to its own two files when its own
        # inputs go missing.
        output_path.unlink(missing_ok=True)
        result = _categories_record()
        sink.emit("boundaries_categories_finished", **result)
        return result

    try:
        parcels = _load_parcel_polygons(parcels_path)
        if not parcels:
            # Defensive only: Task 1 never writes this file with zero
            # features (it unlinks it instead, the same convention this
            # branch itself follows), so a real package never reaches this,
            # but nothing downstream (`parcels[0]`, `_rings_lat0`) is safe
            # to call over an empty list, and the honest reading of "zero
            # parcels" is the same either way.
            output_path.unlink(missing_ok=True)
            result = _categories_record()
            sink.emit("boundaries_categories_finished", **result)
            return result

        overlays = _load_categorise_overlays(root, stem)
        rings = [parcel.ring for parcel in parcels]
        classifications = classify_parcels(rings, overlays)

        groups: dict[str, list[int]] = defaultdict(list)
        samples_total = 0
        capped = 0
        for index, (category, sample_count) in enumerate(classifications):
            groups[category].append(index)
            samples_total += sample_count
            if sample_count == SAMPLE_CAP:
                capped += 1

        lat0 = _rings_lat0(rings)
        first = parcels[0]
        features: list[dict] = []
        for category in sorted(groups):
            parcel_groups = [
                [[_to_local_metres(point, lat0) for point in rings[index]]]
                for index in groups[category]
            ]
            for curve in boundary_curves_with_counts(parcel_groups).curves:
                coordinates = [list(_from_local_metres(point, lat0)) for point in curve]
                features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "category": category,
                            "source": first.source,
                            "note": first.note,
                            "year": first.year,
                        },
                        "geometry": {"type": "LineString", "coordinates": coordinates},
                    }
                )

        payload = {"type": "FeatureCollection", "features": features}
        atomic_write_bytes(
            output_path, json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
    except (BoundaryCategoriesError, ET.ParseError, OSError) as exc:
        error = str(exc)
        sink.emit("boundaries_categories_failed", error=error)
        return _categories_record(error=error)

    counts = {category: len(groups[category]) for category in sorted(groups)}
    result = _categories_record(
        parcels=len(parcels), counts=counts, samples=samples_total, capped=capped
    )
    sink.emit("boundaries_categories_finished", **result)
    return result


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

    # Task 6 of the OS Open pack plan. Same ordering as run_survey's own
    # call site: BEFORE heights fusion, unconditionally before the bridge
    # attempt below, so an injected footprint with no height of its own
    # is already in the `.osm` by the time heights fusion runs immediately
    # after it.
    payload["buildings_fusion"] = _fuse_buildings_step(root=root, stem=stem, sink=sink)

    # Task 3 of the categorised boundaries plan. Same ordering as
    # run_survey's own call site: immediately after buildings fusion,
    # unconditionally, so this reads the FUSED `.osm`'s own building
    # evidence (see `_categorise_boundaries_step`'s own docstring). Fully
    # recomputed every call, so a re-run over a package this already
    # classified once overwrites `<stem>_boundaries_categorised.geojson`
    # with an identical file rather than needing its own idempotence
    # guard (see that step's own docstring, "Idempotent by full
    # recomputation").
    payload["boundaries_categories"] = _categorise_boundaries_step(
        root=root, stem=stem, sink=sink
    )

    # Task 7. BEFORE the bridge, unconditionally, same as run_survey's own
    # ordering and for the same reason: the bridge converts <stem>.osm into
    # Urbano's own formats, so any height this can still add has to be in
    # the file before that conversion runs. This is also what proves
    # idempotence on a package the download already fused: a second run
    # here finds every touched way already carrying `height` and reports
    # `written: 0`, `kept_existing` at least as large as the download's own
    # `written` count, never re-adding a tag that is already there.
    payload["lidar_heights"] = _fuse_heights_step(root=root, stem=stem, sink=sink)

    # Task 5 of the INSPIRE curves plan. Same ordering as run_survey's own
    # call site: immediately after heights fusion, unconditionally before
    # the bridge attempt below. The provenance enrichment runs regardless
    # of whether this package's own survey.json has ever seen it before
    # (a package downloaded before this task shipped carries an
    # unsubstituted "[year]" and no conditions_url in its "inspire" entry
    # until the first `mapgen bridge` run after upgrading fixes it, the
    # same way this run is what first fuses its boundaries into the
    # `.osm`).
    payload["inspire_boundaries"] = _fuse_boundaries_step(root=root, stem=stem, sink=sink)
    try:
        inspire_source = get_source("inspire")
    except UnknownSourceError:
        inspire_source = None
    existing_sources = payload.get("sources")
    _enrich_inspire_provenance(
        existing_sources if isinstance(existing_sources, list) else [],
        root,
        stem,
        getattr(inspire_source, "conditions_url", None),
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

    # Task 35. The reason this command exists, restated: every package the
    # owner already has was downloaded by a run whose bridge failed, so
    # every one of them is missing its project setting. That is now fixed
    # here by mapgen itself rather than by whether the bridge can be made to
    # work, which is why this runs unconditionally after the bridge attempt
    # rather than only when it succeeded.
    # Task 39, and the same reasoning as the project setting one line down:
    # every package the owner already has was downloaded before mapgen could
    # write a .egrid, so every one of them has a DEM Urbano cannot open. This
    # converts it in place, with no download, and it runs FIRST so that the
    # project setting written next can see the file and name the layer.
    payload["elevation_grid"] = _write_elevation_grid_step(
        bbox=bbox, root=root, stem=stem, sink=sink
    )
    payload["project_setting"] = _write_project_setting_step(
        bbox=bbox, root=root, stem=stem, sink=sink
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
        # How long each failure's own service asked to be left alone for,
        # when it said (Task 32). Kept here rather than inside the record
        # to_record() produces, because it is a fact about what this run
        # should do next rather than about what happened, and survey.json
        # would otherwise carry a mostly-null field on every failure in
        # every package to serve one decision made seconds later in the
        # same process.
        self._retry_after: dict[tuple[str, str], float] = {}
        # One entry per tile this run actually asked for a second time,
        # kept whatever the answer was. tile_failures cannot carry this:
        # a tile that recovered is forgotten from it, deliberately and
        # correctly, so without a separate list the run has no way to say
        # "this completed, but it was fragile" (Task 32, section 4).
        self._retry_records: list[dict[str, object]] = []

    def add_tile_failures(self, failures: Sequence[TileFailure]) -> None:
        for failure in failures:
            self._tiles[(failure.source, failure.tile_id)] = failure.to_record()
            key = (failure.source, failure.tile_id)
            if failure.retry_after_seconds is None:
                self._retry_after.pop(key, None)
            else:
                self._retry_after[key] = failure.retry_after_seconds

    def set_source_error(self, source_id: str, error: str) -> None:
        self._source_errors[source_id] = error

    def retry_after_for(self, source_id: str, tile_ids: Sequence[str]) -> float | None:
        """The longest period any of these tiles' services asked for, or
        None if none of them asked.

        The longest rather than the first: the tiles are about to be
        fetched in one call, so one wait covers all of them, and coming
        back before the latest of the deadlines the service set would be
        ignoring it for the tiles it applied to.
        """
        asked = [
            self._retry_after[(source_id, tile_id)]
            for tile_id in tile_ids
            if (source_id, tile_id) in self._retry_after
        ]
        return max(asked) if asked else None

    def note_retry(
        self,
        source_id: str,
        tile_ids: Sequence[str],
        pass_number: int,
        whole_layer: bool = False,
    ) -> None:
        """One record per tile asked again, each saying whether it was asked
        on its own account or as one share of a single whole-extent request.

        whole_layer is what stops a reader of survey.json counting seventy
        two stumbles where one thing stumbled once. Elevation and Overture
        download the whole extent in one call, so a failure of theirs is
        recorded against every planned tile and the retry asks for every
        planned tile back in one request; without this flag the file's only
        account of that is seventy two entries reading exactly like seventy
        two separate tiles going wrong.

        The entries stay per tile rather than collapsing to one, because
        `recovered` is decided per (source, tile_id) against the run's final
        verdict, and that verdict is the only thing in a position to say
        which of the tiles one request covered actually arrived. The flag
        says the entries are one event; it does not throw away which tiles
        the event was about.
        """
        for tile_id in tile_ids:
            key = (source_id, tile_id)
            self._retries[key] = self._retries.get(key, 0) + 1
            record = self.record_for(source_id, tile_id)
            self._retry_records.append(
                {
                    "source": source_id,
                    "tile_id": tile_id,
                    "pass_number": pass_number,
                    "kind": record["kind"],
                    "reason": record["reason"],
                    "whole_layer": whole_layer,
                }
            )

    def retry_records(self, still_failed: set[tuple[str, str]]) -> list[dict[str, object]]:
        """What this run retried, and whether it worked.

        `recovered` is decided from the run's FINAL verdict rather than
        from anything observed during the retry itself, because the final
        verdict is the one that read the disk. A retry whose fetch()
        returned cleanly while writing nothing has not recovered
        anything, and only the verify pass is in a position to say so.
        """
        return [
            {
                **record,
                "recovered": (record["source"], record["tile_id"]) not in still_failed,
            }
            for record in self._retry_records
        ]

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


def describe_tile_failures(
    records: Sequence[Mapping[str, object]], planned_tiles: int | None = None
) -> list[str]:
    """The account of what did not arrive, as plain lines.

    One composer for all three places the owner can meet this (Task 30,
    section 5): the terminal summary of a forced run, the message
    IncompleteSurveyError carries out of an unforced one, and the browser,
    which reads the same records out of survey.json. Three separately
    written versions of this would drift, and the one thing they must
    never do is disagree about which tiles are missing.

    Sorted by source then tile id, so the same run always reads the same
    way, rather than in whatever order the failures happened to land.

    planned_tiles is Task 32's, and it is what stops a whole-layer
    failure being reported one tile at a time. Elevation and Overture
    have no tiles of their own: they make one whole-extent request, so a
    failure is recorded against every planned tile with one identical
    reason. On the owner's own Barry extent that is seventy-two copies of
    the same sentence, for one thing that went wrong once, and it is now
    the ordinary shape of an unforced elevation failure rather than an
    exotic one. When a layer's every planned tile carries the same
    reason, that is a LAYER failure and is said once, naming the layer
    and the count.

    A partial failure is untouched and must be: two OSM tiles of
    seventy-two timing out is two tiles, is named as two tiles, and
    collapsing it would hide which ones. The rule is exactly "all of
    them, identically", never "several of them", which is why this needs
    to be told how many were planned rather than inferring anything from
    the records it holds.

    planned_tiles defaults to None, which never collapses, so a caller
    that does not know the plan gets the same output this has always
    produced.
    """
    if not records:
        return []
    count = len(records)
    noun = "tile" if count == 1 else "tiles"
    lines = [f"{count} {noun} did not download:"]

    grouped: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for record in records:
        key = (str(record.get("source", "")), str(record.get("reason", "")))
        grouped.setdefault(key, []).append(record)

    said: set[tuple[str, str]] = set()
    for record in sorted(
        records, key=lambda r: (str(r.get("source", "")), str(r.get("tile_id", "")))
    ):
        retried = record.get("retried") or 0
        again = " Retried, and it failed again." if retried else ""
        key = (str(record.get("source", "")), str(record.get("reason", "")))
        whole_layer = (
            planned_tiles is not None
            and len(grouped[key]) == planned_tiles
            and planned_tiles > 1
        )
        if whole_layer:
            if key in said:
                continue
            said.add(key)
            lines.append(
                f"  {record.get('source')}, all {planned_tiles} tiles: "
                f"{record.get('reason')}{again}"
            )
            continue
        lines.append(
            f"  {record.get('source')} {record.get('tile_id')}: "
            f"{record.get('reason')}{again}"
        )
    return lines


def describe_tile_recoveries(records: Sequence[Mapping[str, object]]) -> list[str]:
    """The account of what arrived only on a second attempt, as plain lines.

    The companion to describe_tile_failures, and it exists for the same
    reason that one does: a run that completed only because a retry worked
    is not the same run as one that never stumbled, and this is the only
    line that says so.

    Task 32 collapsed a whole-layer FAILURE to one sentence naming the
    layer. This is the same collapse on the recovery side of the same run,
    which review I2 found had been left uncollapsed: elevation and Overture
    ask for the whole extent in one request, so one DEM timing out and
    arriving on the retry was reported as the whole planned tile count
    recovering. On the owner's own Barry extent that is "72 tiles arrived
    only on a retry" for one download that stumbled once, which is exactly
    the reading the failure side had already been fixed to avoid.

    Collapsed on the records' own `whole_layer` rather than on a tile count
    passed in beside them, unlike describe_tile_failures. The retry pass
    already made that judgement, on the plan it actually had in front of it,
    and a second opinion composed here from a number this function was
    handed is a second opinion that can differ. A record without the field,
    which is every package written before this, reads as per tile and gets
    the sentence it has always got.

    A partial recovery is untouched and must be: two OSM tiles of seventy
    two coming back is two tiles, and saying "the OSM layer" of it would
    claim seventy more than arrived.
    """
    recovered = [record for record in records if record.get("recovered")]
    if not recovered:
        return []

    lines: list[str] = []
    layers: dict[str, int] = {}
    per_tile: list[Mapping[str, object]] = []
    for record in recovered:
        if record.get("whole_layer"):
            source = str(record.get("source"))
            layers[source] = layers.get(source, 0) + 1
        else:
            per_tile.append(record)

    for source in sorted(layers):
        tiles = layers[source]
        noun = "tile" if tiles == 1 else "tiles"
        lines.append(
            f"The {source} layer arrived only on a retry, one request "
            f"covering {tiles} {noun}. See survey.json for what was retried."
        )
    if per_tile:
        noun = "tile" if len(per_tile) == 1 else "tiles"
        named = ", ".join(sorted({str(record.get("source")) for record in per_tile}))
        lines.append(
            f"{len(per_tile)} {noun} arrived only on a retry ({named}). "
            f"See survey.json for which."
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

    Since Task 32 every source reaches this, not only OSM. Elevation and
    Overture both keep whole-extent output rather than tile-stamped
    files, so their failures are recorded against every planned tile and
    arrive here as one group per source. Asking them again is one fetch()
    call each, and each of them already skips what is on disk: elevation
    returns immediately if its DEM is there, and Overture re-downloads
    only the types that are missing. So "retry the layer that failed, for
    the reason it failed, and nothing else" needs no per-source knowledge
    in this function, which has none.

    A service that named a period to wait is waited for, once per source,
    before its retry. See MAX_RETRY_AFTER_WAIT_SECONDS for the ceiling
    and for what happens past it. This is the only place in the outer
    layer that pauses at all, and it pauses only when a service asked in
    writing: there is deliberately no generic backoff out here, because
    the sources that have one have already served it by the time a tile
    gets this far.

    Cancellation interrupts this as promptly as it interrupts the first
    attempt: checked before each source is asked, waited on rather than
    slept through, and threaded into fetch() itself for the sources that
    accept it, so a stop lands between tiles rather than after all of
    them.

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

        asked_for = ledger.retry_after_for(
            source_id, [tile.tile_id for tile in retry_tiles]
        )
        if asked_for is not None and asked_for > MAX_RETRY_AFTER_WAIT_SECONDS:
            # The service named a period longer than this run will pause
            # for, so it is not retried at all. Coming back early would
            # spend the owner's own quota to be told the same thing
            # again, and waiting it out would stop a survey they are
            # watching for as long as the service felt like naming.
            #
            # Announced rather than silent. survey.json records this tile
            # as retried: 0, which is true and which describes a
            # non-retryable kind identically, so the live stream is where
            # the difference is said out loud.
            sink.emit(
                "retry_postponed",
                source=source_id,
                seconds_requested=asked_for,
                seconds_ceiling=MAX_RETRY_AFTER_WAIT_SECONDS,
                tiles=len(retry_tiles),
            )
            continue
        if asked_for:
            sink.emit("retry_waiting", source=source_id, seconds=asked_for)
            if token.wait(asked_for):
                # The stop landed during the wait. Nothing has been
                # asked for a second time, so nothing changes state:
                # these tiles keep the failure they already had.
                return True

        # Task 32's whole-layer rule, on the recovery side of the same run
        # (review I2). The failure side already says a layer that failed
        # identically on every planned tile once, naming the layer, because
        # seventy two copies of one sentence for one thing that went wrong
        # once is a misreading rather than a detail. Asking that same layer
        # again is the same shape and gets the same treatment: one request
        # is announced once.
        #
        # The predicate is describe_tile_failures' predicate, deliberately,
        # so the two halves of a run cannot disagree about what counts as a
        # whole layer: every planned tile, all carrying the same reason, and
        # more than one of them. A partial group is untouched and must be,
        # since two OSM tiles of seventy two is two tiles and collapsing it
        # would hide which ones.
        records = [ledger.record_for(source_id, tile.tile_id) for tile in retry_tiles]
        whole_layer = (
            len(retry_tiles) == len(tiles)
            and len(tiles) > 1
            and len({(r["kind"], r["reason"]) for r in records}) == 1
        )
        if whole_layer:
            # No tile_id, the way retry_postponed carries none: this event
            # is about a layer, and stamping it with one of the tiles it
            # covers would put a single square's name on work spanning the
            # whole extent. The browser's status line already has the
            # sentence for an event with no tile on it.
            sink.emit(
                "tile_retrying",
                source=source_id,
                tiles=len(retry_tiles),
                pass_number=retry_pass,
                of=RETRY_PASS_BUDGET,
                kind=records[0]["kind"],
                reason=records[0]["reason"],
            )
        else:
            for tile, record in zip(retry_tiles, records):
                sink.emit(
                    "tile_retrying",
                    source=source_id,
                    tile_id=tile.tile_id,
                    pass_number=retry_pass,
                    of=RETRY_PASS_BUDGET,
                    kind=record["kind"],
                    reason=record["reason"],
                )
        ledger.note_retry(
            source_id,
            [tile.tile_id for tile in retry_tiles],
            retry_pass,
            whole_layer=whole_layer,
        )

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
    retries=None,
    project_setting=None,
    elevation_grid=None,
    lidar_heights=None,
    inspire_boundaries=None,
    buildings_fusion=None,
    boundaries_categories=None,
) -> dict:
    width_m, height_m = extent_metres(request.bbox)
    outputs_by_source = outputs_by_source or {}
    project_setting = project_setting or {
        "written": False, "file": None, "layers": [], "error": None,
    }
    elevation_grid = elevation_grid or {
        "written": False, "file": None, "nodes": None, "covered": None,
        "error": None, "source": None,
    }
    lidar_heights = lidar_heights or {
        "written": None, "buildings": None, "kept_existing": None,
        "no_data": None, "error": None,
    }
    inspire_boundaries = inspire_boundaries or {
        "written": None, "curves": None, "kept_existing": None, "error": None,
    }
    buildings_fusion = buildings_fusion or _buildings_record()
    boundaries_categories = boundaries_categories or _categories_record()
    verified = verified or {
        "checked": 0, "ok": 0, "failed": 0, "pending": 0,
        "corrections": [], "failures": [],
    }
    retries = retries or []
    # Task 5 of the INSPIRE curves plan. Built here, before the return
    # dict below, rather than inline as a list comprehension the way it
    # was before this task, so _enrich_inspire_provenance can mutate the
    # "inspire" entry in place afterward: the [year] substitution and the
    # conditions link both need this package's own root and stem, which
    # _source_provenance itself is deliberately not extended to know
    # about (see that function's own enrichment helper for why).
    sources_provenance = [
        _source_provenance(source, outputs_by_source.get(source.id, ()))
        for source in sources
    ]
    inspire_source = next(
        (source for source in sources if getattr(source, "id", None) == "inspire"), None
    )
    _enrich_inspire_provenance(
        sources_provenance,
        paths.root,
        paths.stem,
        getattr(inspire_source, "conditions_url", None),
    )
    # Final review, Important I1. Same shape as the inspire enrichment
    # immediately above (a live source object's own per-run fact, folded
    # into the already-built dict entry), but reading `versions_used`
    # rather than a merged file back off disk; see
    # `_enrich_os_product_provenance`'s own docstring for why the two
    # differ there and why this is not also called from `bridge_package`.
    for source_id in ("os_open", "os_uprn"):
        matching_source = next(
            (source for source in sources if getattr(source, "id", None) == source_id), None
        )
        _enrich_os_product_provenance(
            sources_provenance, source_id, getattr(matching_source, "versions_used", None)
        )
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
        "sources": sources_provenance,
        # The resolved selection (never the possibly-None raw field:
        # every other audit-trail value here is a concrete, resolved
        # fact, not "whatever was asked for or a default"), so a package
        # says what it contains without the reader having to separately
        # know that None means everything.
        "categories": request.effective_categories,
        # Task 7 of the tier resolver plan: which source resolves each
        # category for this exact bbox, computed from the same
        # configured, selected `sources` this run actually fetched from
        # (never the registry), so a source the owner did not select
        # never appears here either. estimate_survey computes the
        # identical structure the same way, over its own request-scoped
        # configured sources, for the estimate HTTP response's own
        # "resolution" key; both call sites share resolve() rather than
        # each assembling the list by hand.
        "resolution": resolve(request.bbox, sources),
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
        # Task 32, section 4: what this run had to ask for twice, and
        # whether asking again worked. Empty on a run that never
        # stumbled, which is the ordinary one.
        #
        # A separate list from tile_failures, and it has to be, because
        # the two answer opposite questions. tile_failures is what is
        # still missing, so a tile the retry recovered is deliberately
        # NOT in it: the verify pass forgets a reason once the file is
        # genuinely on disk, or a reader would find an explanation
        # attached to something that is not a problem. That is right,
        # and it leaves a complete-but-fragile run indistinguishable
        # from a clean one. This is the field that tells them apart.
        #
        # `recovered: true` is the interesting value, not the alarming
        # one. It says the package is complete and that it was not
        # complete on the first attempt, which is what an owner wanting
        # to know whether their link or the service is deteriorating
        # actually needs. `recovered: false` duplicates a tile_failures
        # entry on purpose: the same fact is worth having in both the
        # "what is missing" and the "what was fragile" reading.
        "retries": retries,
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
        # Task 35. Whether this package has the one file Urbano 2 is pointed
        # at, which is a different question from whether the bridge ran and
        # is now answered separately from it. mapgen writes this file itself,
        # so a run whose bridge failed, was skipped or was never reached
        # still has one.
        #
        # `layers` is what the file actually names, so a reader can tell
        # without opening it whether the elevation or Overture data reached
        # Urbano's side of the package. `error` is one plain sentence when it
        # could not be written at all, which for a package with none of
        # Urbano's four data files in it is the honest outcome rather than a
        # file describing nothing.
        "project_setting": project_setting,
        # Task 39. The DEM in Urbano's own format, which is the only format
        # any Urbano component will read elevation in. `written` false with a
        # null `error` means this package simply has no DEM to convert;
        # `written` false WITH an error means it had one and the conversion
        # refused, and the sentence says which part of the file it refused
        # over. `covered` is how many of the grid's nodes the DEM could
        # actually answer for, which on a coastal survey is well under all of
        # them and is worth being able to see without opening Grasshopper.
        # `source` (Task 8) says which raster actually answered: the Welsh
        # LiDAR DTM, OpenTopography's own DEM, or both with the LiDAR
        # preferred, so a reader can tell a Welsh survey's better terrain
        # apart from the coarser 30 m fallback without diffing `covered`
        # against a run from before `lidar_wales` was selected.
        "elevation_grid": elevation_grid,
        # Task 6 of the OS Open pack plan. `written` is 0, never None, on
        # a package with neither candidate footprint file at all (see
        # `_buildings_record`'s own docstring for why this one step does
        # not follow `lidar_heights`/`inspire_boundaries`'s None-means-
        # never-ran convention): a survey with `os_open` unselected and no
        # Overture layer either is an ordinary, complete package, not one
        # missing something it tried for.
        "buildings_fusion": buildings_fusion,
        # Task 3 of the categorised boundaries plan. `parcels`, `counts`,
        # `samples` and `capped` all read as zero, never `None`, on a
        # package with no `<stem>_parcels.geojson` at all (no `inspire`
        # layer, or zero parcels kept in this extent): the identical
        # always-zero-not-unknown discipline `buildings_fusion` above
        # already follows, and for the same reason (see `_categories_
        # record`'s own docstring). `note` is the one key that is never
        # `None` on any branch, since the caveat is true of this record's
        # own existence rather than of what this run happened to find.
        "boundaries_categories": boundaries_categories,
        # Task 7. `written` is None, not 0 or False, on a package this step
        # never ran on at all (no `.osm`, or no Welsh LiDAR selected):
        # `int | None` throughout, matching the brief's own record shape, so
        # a reader can tell "nothing to fuse" apart from "fused, and found
        # zero buildings to touch" (`written: 0`). `error` is one plain
        # sentence when the step ran and could not finish; every OTHER
        # layer's own data is untouched either way, per Task 20's ruling.
        "lidar_heights": lidar_heights,
        # Task 5 of the INSPIRE curves plan. Same None-vs-0 discipline as
        # `lidar_heights` immediately above: `written` is None, not 0,
        # on a package this step never ran on at all (no `.osm`, or no
        # boundaries GeoJSON), so a reader can tell "nothing to fuse"
        # apart from "fused, and found zero curves to inject"
        # (`written: 0`).
        "inspire_boundaries": inspire_boundaries,
        "started_at": started_at,
        "finished_at": _now(),
    }
