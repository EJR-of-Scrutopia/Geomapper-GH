"""Orchestration: plan, download, merge, bridge, survey.json.

Knows about paths, job state and the source registry. Knows nothing about any
individual data source, which is what makes phase 2 additive.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
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
from mapgen.jobs import FAILED, OK, CancelToken, JobState
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
from mapgen.sources.osm import NodeCapExceededError, OsmSource
from mapgen.sources.overture import (
    DEFAULT_OVERTURE_TYPES,
    LAYER_FILENAMES,
    OvertureSource,
)

SCHEMA_VERSION = 1

# The old, superseded script's own fixed ladder (Task 17's audit found it
# in build_tiled_osm_fallback: 2000, then 1500, then 1000 metres), restored
# by owner ruling after Task 17 recorded it as a dropped capability: dense
# city centres are exactly where the owner surveys, and a run that stops an
# hour in to wait for a manual --tile-size-m retry has to be babysat. Only
# candidates SMALLER than whatever size just failed are ever tried (see
# _next_smaller_node_cap_tile_size), so a job already at or below 1000 m
# has nowhere smaller to fall back to and fails immediately, exactly as it
# does today.
NODE_CAP_RETRY_TILE_SIZES_M: tuple[float, ...] = (2000.0, 1500.0, 1000.0)


def _next_smaller_node_cap_tile_size(current_tile_size_m: float) -> float | None:
    smaller = [size for size in NODE_CAP_RETRY_TILE_SIZES_M if size < current_tile_size_m]
    return max(smaller) if smaller else None


class _RetryAtSmallerTileSize(RuntimeError):
    """Internal signal only: raised by _run_survey_once when an OSM tile
    exceeded the node cap AND a smaller size remains in the retry ladder,
    caught by run_survey's own loop to actually perform the retry. Never
    raised when no smaller size remains, which is what lets _run_survey_
    once fall through to its ordinary (non-retryable) failure handling,
    respecting request.force exactly as it always has, at the final rung
    and for every other kind of failure at every rung.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


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
    """Tile count, rows, cols and extent: the numbers that need nothing
    about naming or where output will land, shared verbatim by
    estimate_survey and estimate_geometry so this tiling arithmetic is
    written exactly once.
    """
    width_m, height_m = extent_metres(bbox)
    return {
        "tiles": len(tiles),
        "rows": max((t.row for t in tiles), default=0) + 1,
        "cols": max((t.col for t in tiles), default=0) + 1,
        "extent_km": {"width": width_m / 1000.0, "height": height_m / 1000.0},
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
    """Runs a survey, automatically retrying the WHOLE run at the next
    smaller tile size in NODE_CAP_RETRY_TILE_SIZES_M whenever an OSM tile
    exceeds the 50000-node cap and a smaller size remains untried.

    The whole run, not just the failing tile: the owner considered and
    rejected subdividing only the offending tile, in favour of this
    simpler, uniform retry. This is safe by construction, not merely by
    convention: a different tile_size_m hashes to a different
    tiling_fingerprint (see _plan), so a retry gets its own, entirely
    separate work_dir and cannot read or mix in anything the failed
    attempt at the larger size wrote. Confirmed directly, not merely
    assumed, by test_package.py's own retry tests.

    Stops once NODE_CAP_RETRY_TILE_SIZES_M is exhausted (nothing smaller
    remains to try) and fails with the same message _run_survey_once has
    always raised, honouring request.force at that final attempt exactly
    as it always has: this function never overrides force itself, it only
    decides whether another attempt happens at all, at a smaller size,
    before force's own "continue past a failure" or "stop and raise"
    behaviour gets to run at that attempt.
    """
    sink = progress if progress is not None else NullProgress()
    current_request = request
    while True:
        try:
            return _run_survey_once(current_request, sink, cancel, bridge_runner)
        except _RetryAtSmallerTileSize as retry_signal:
            next_size = _next_smaller_node_cap_tile_size(current_request.tile_size_m)
            # _run_survey_once only ever raises this when a smaller size
            # genuinely exists (see its own comment at the raise site), so
            # next_size is never None here; asserted rather than silently
            # trusted, since a future change to that condition breaking
            # this invariant should fail loudly, not retry into a
            # TypeError from max() on an empty sequence three lines away.
            assert next_size is not None
            next_overlap = min(current_request.overlap_m, max(50.0, next_size / 10.0))
            sink.emit(
                "tile_size_retry",
                previous_tile_size_m=current_request.tile_size_m,
                next_tile_size_m=next_size,
                reason=str(retry_signal),
            )
            current_request = replace(
                current_request, tile_size_m=next_size, overlap_m=next_overlap
            )


def _run_survey_once(
    request: SurveyRequest,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
    bridge_runner=None,
) -> SurveyResult:
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

    # The scope always keeps the directory. Removal is decided after the job,
    # by completeness, so a partial run can always be resumed.
    with work_dir_scope(paths.work_dir, keep=True):
        for source in sources:
            token.raise_if_cancelled()
            source_work = paths.work_dir / "raw" / source.id
            ensure_dir(source_work)
            pending = [t for t in tiles if not state.is_done(t.tile_id, source.id)]
            fetch_succeeded = False
            try:
                source.fetch(request.bbox, pending, source_work, sink)
                fetch_succeeded = True
            except Exception as exc:
                sink.emit("source_failed", source=source.id, error=str(exc))
                # A node-cap failure with a smaller size still to try is
                # ALWAYS retried by run_survey's own wrapper, regardless of
                # request.force: force's job is "tolerate a failure and
                # mark the package incomplete rather than stop", which is
                # not the same decision as "this specific, structural
                # failure has a smaller-tile-size fix available, try it
                # first". Checked before the request.force branch below,
                # not folded into it, so force's existing behaviour for
                # every OTHER kind of failure, and for a node-cap failure
                # once this ladder is exhausted, is completely unchanged:
                # this raises only in the one case _run_survey_once itself
                # would not otherwise have handled differently.
                if (
                    isinstance(exc, NodeCapExceededError)
                    and _next_smaller_node_cap_tile_size(request.tile_size_m) is not None
                ):
                    _record_tile_outcomes(
                        state, source.id, pending, source_work, fetch_succeeded, current_tile_ids
                    )
                    raise _RetryAtSmallerTileSize(exc) from exc
                if not request.force:
                    # Record whatever genuinely landed before re-raising: a
                    # batch call failing partway through must not blame tiles
                    # that already succeeded, or the next resume would redo
                    # work that was already done.
                    _record_tile_outcomes(
                        state, source.id, pending, source_work, fetch_succeeded, current_tile_ids
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
            # it. Per-tile status is recorded only once this has run, or been
            # attempted, so a file fetch() claimed but never materialised is
            # never marked ok on the strength of fetch() alone.
            try:
                parts = assert_inputs_present(parts, force=request.force)
            finally:
                _record_tile_outcomes(
                    state, source.id, pending, source_work, fetch_succeeded, current_tile_ids
                )
            merged = source.merge(parts, paths.root, paths.stem)
            outputs_by_source[source.id] = merged
            sink.emit("source_done", source=source.id, outputs=[p.name for p in merged])

        layer_files = _write_layer_files(outputs_by_source, paths)

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
        bridge_attempted = request.run_bridge_step
        bridge_ok: bool | None = None
        bridge_error: str | None = None
        if request.run_bridge_step:
            token.raise_if_cancelled()
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
        request, paths, tiles, sources, state, started_at, bridge_attempted, bridge_ok, bridge_error
    )
    atomic_write_text(paths.survey_json, json.dumps(survey, indent=2))
    sink.emit("job_finished", complete=state.complete, root=str(paths.root))

    # Removed only on a clean, complete run. A failed or partial job keeps its
    # tiles, because that is what makes the next run resume rather than restart.
    # The whole _work/ parent goes, not just this tiling's fingerprint
    # subdirectory: a complete root is never reused (a later request lands on
    # a fresh _02), so a sibling tiling's abandoned scratch tree left inside
    # _work/ would otherwise survive forever with nothing left to remove it.
    if state.complete and not request.keep_work:
        best_effort_rmtree(paths.work_dir.parent)

    return SurveyResult(paths=paths, complete=state.complete, survey=survey)


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
    """
    current_ids = set(current_tile_ids)
    kept: list[Path] = []
    for path in sorted(source_work.rglob("*")):
        if not path.is_file() or path.suffix == ".part":
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
) -> None:
    """Mark each pending tile from what is actually on disk, not from
    whether the batch fetch() call raised.

    fetch() is one Python call for potentially many tiles: it either returns
    once for all of them or raises once for all of them, which says nothing
    about which individual tiles actually got a usable file. Sources that
    name output files after the tile id, which covers OSM, Overture and the
    test stub, get true per-tile status here.

    A tile is ok only if it has a non-empty, tile-stamped file in EVERY
    directory where this source keeps tile-stamped files, not just any one
    of them. OSM writes flat into source_work, so that is one directory.
    Overture writes one type per subdirectory, so a tile with water but not
    building for the same id must not be marked ok on the strength of water
    alone: the set of directories to check is derived from what is actually
    on disk, never hardcoded, so this generalises to any future source shape
    without package.py knowing anything about it.

    A source with no per-tile naming at all, such as elevation's single
    whole-area file, contributes no tile-stamped directory at all. If it
    still produced that whole-area file, there is no finer signal than the
    batch outcome, so it applies uniformly, matching how such sources have
    always behaved. But a fetch() that returns without raising while leaving
    nothing at all on disk is not evidence of anything: that is vacuous, not
    done, so it is always failed regardless of what the batch call reported.
    """
    files = _existing_output_files(source_work, current_tile_ids)
    pending_ids = {tile.tile_id for tile in pending}
    tile_stamped_dirs = {path.parent for path in files if path.stem in pending_ids}

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
            status = OK if has_output else FAILED
        elif files:
            status = OK if fetch_succeeded else FAILED
        else:
            status = FAILED
        state.mark(tile.tile_id, source_id, status)


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
        "bridge": {
            "attempted": bridge_attempted,
            "ok": bridge_ok,
            "error": bridge_error,
        },
        "started_at": started_at,
        "finished_at": _now(),
    }
