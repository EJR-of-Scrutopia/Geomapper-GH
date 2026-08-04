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
from typing import Sequence

from mapgen import __version__
from mapgen.bridge import BridgeError, BridgeRequest, run_bridge
from mapgen.categories import ALL_CATEGORY_IDS, overture_types_for_categories, validate_categories
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
    NullProgress,
    ProgressSink,
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
    check_path_length(paths, request.effective_overture_types, source_ids=request.source_ids)
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
    rather than mutating the registered one. Only overture (types) and
    osm (category tag filtering) have any such per-request selection
    today; every other source id is used exactly as registered, with no
    id-specific branch needed for it to keep working unchanged.
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
    # Task 22: true once a Stop request has been noticed, at any of three
    # checkpoints (between sources, mid-fetch inside a source that accepts
    # `cancel`, or between the last source and the bridge). A stop is a
    # deliberate, honest partial, never treated like a failure from here
    # on: whatever was already fetched is still merged below (see the
    # Cancelled branch), no further source is attempted, the bridge is
    # skipped, and the stale-output sweep never runs against a package
    # this incomplete on purpose. See _build_survey_json for how this is
    # told apart from an ordinary failure in survey.json.
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
            fetch_succeeded = False
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
                if not request.force:
                    # Record whatever genuinely landed before re-raising: a
                    # batch call failing partway through must not blame tiles
                    # that already succeeded, or the next resume would redo
                    # work that was already done.
                    _record_tile_outcomes(
                        state, source.id, pending, source_work, fetch_succeeded, current_tile_ids, sink
                    )
                    raise

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

        layer_files = _write_layer_files(outputs_by_source, paths)

        # Never on a stopped run, whatever state.complete happens to say:
        # a stop is a deliberate, honest partial (see _sweep_stale_
        # outputs's own docstring on why an incomplete run is never swept),
        # and sweeping it risks removing a merged file a resume would
        # still want to find sitting in the root next time.
        if state.complete and not stopped:
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
        bridge_attempted = request.run_bridge_step and not stopped
        bridge_ok: bool | None = None
        bridge_error: str | None = None
        if bridge_attempted:
            sink.emit("bridge_started")
            osm_outputs = outputs_by_source.get("osm", [])
            elevation_outputs = outputs_by_source.get("elevation", [])
            bridge_request = BridgeRequest(
                bbox=request.bbox,
                output_dir=paths.root,
                file_name_stem=None if request.coordinate_stem else paths.stem,
                osm_file_path=osm_outputs[0] if osm_outputs else None,
                elevation_tiff_path=elevation_outputs[0] if elevation_outputs else None,
                skip_elevation=not elevation_outputs,
            )
            try:
                if bridge_runner is None:
                    run_bridge(bridge_request)
                else:
                    run_bridge(bridge_request, runner=bridge_runner)
            except (BridgeError, OSError) as exc:
                # BridgeError covers a missing project or a non-zero exit
                # (today's case: Urbano.Core.dll/ProjectSetup.dll absent, so
                # the bridge process itself runs and fails). OSError also
                # covers dotnet itself being missing from PATH, which raises
                # from the subprocess call rather than from bridge.py. Both
                # are already plain, one-line messages, never a traceback.
                bridge_ok = False
                bridge_error = str(exc)
                sink.emit("bridge_failed", error=bridge_error)
            else:
                bridge_ok = True
                sink.emit("bridge_done")

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
        stopped,
    )
    atomic_write_text(paths.survey_json, json.dumps(survey, indent=2))
    sink.emit("job_finished", complete=state.complete, stopped=stopped, root=str(paths.root))

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

    return SurveyResult(paths=paths, complete=state.complete, survey=survey, stopped=stopped)


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


def _record_tile_outcomes(
    state: JobState,
    source_id: str,
    pending: Sequence[Tile],
    source_work: Path,
    fetch_succeeded: bool,
    current_tile_ids: Sequence[str],
    sink: ProgressSink,
    stopped: bool = False,
) -> None:
    """Mark each pending tile from what is actually on disk, not from
    whether the batch fetch() call raised.

    Also emits a tile_failed progress event for every tile this call
    resolves to FAILED (Task 22), never for one it resolves to PENDING.
    This is deliberately the ONE place that decides a tile has failed,
    since it is the one place that already has to reason about what is
    genuinely on disk rather than trust a batch call's own return value;
    the browser's tile grid reads this event to show a failed tile as
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
    For Overture that is not a loss of resolution: one whole-extent download
    either lands, in which case every tile's ground is genuinely covered, or
    it does not, in which case none of them is. But a fetch() that returns without raising while leaving
    nothing at all on disk is not evidence of anything: that is vacuous, not
    done, so it is always failed regardless of what the batch call reported
    (this can never coincide with stopped=True: Cancelled is what sets
    stopped, and Cancelled means fetch() never reached its own return at
    all, so fetch_succeeded is always False whenever stopped is True).
    """
    files = _existing_output_files(source_work, current_tile_ids)
    pending_ids = {tile.tile_id for tile in pending}
    tile_stamped_dirs = {path.parent for path in files if path.stem in pending_ids}
    not_attempted_status = PENDING if stopped else FAILED

    for tile in pending:
        if tile_stamped_dirs:
            has_output = all(
                any(
                    path.parent == directory
                    and path.stem == tile.tile_id
                    and path.stat().st_size > 0
                    for path in files
                )
                for directory in tile_stamped_dirs
            )
            status = OK if has_output else not_attempted_status
        elif files:
            status = OK if fetch_succeeded else not_attempted_status
        else:
            status = not_attempted_status
        state.mark(tile.tile_id, source_id, status)
        if status == FAILED:
            sink.emit("tile_failed", source=source_id, tile_id=tile.tile_id)


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


def _source_provenance(source) -> dict[str, object]:
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
    }
    types = getattr(source, "types", None)
    if types is not None:
        entry["types"] = list(types)
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
) -> dict:
    width_m, height_m = extent_metres(request.bbox)
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
        "sources": [_source_provenance(source) for source in sources],
        # The resolved selection (never the possibly-None raw field:
        # every other audit-trail value here is a concrete, resolved
        # fact, not "whatever was asked for or a default"), so a package
        # says what it contains without the reader having to separately
        # know that None means everything.
        "categories": request.effective_categories,
        "tiles": state.as_tile_records(),
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
        # see run_survey's own handling of that exact race). Read this
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
