"""Orchestration: plan, download, merge, bridge, survey.json.

Knows about paths, job state and the source registry. Knows nothing about any
individual data source, which is what makes phase 2 additive.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from mapgen import __version__
from mapgen.bridge import BridgeRequest, run_bridge
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
from mapgen.sources.base import NullProgress, ProgressSink, get_source, register
from mapgen.sources.elevation import ElevationSource
from mapgen.sources.osm import OsmSource
from mapgen.sources.overture import (
    DEFAULT_OVERTURE_TYPES,
    LAYER_FILENAMES,
    OvertureSource,
)

SCHEMA_VERSION = 1


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
    keep_work: bool = False
    coordinate_stem: bool = False
    force: bool = False
    survey_date: date | None = None
    run_bridge_step: bool = True

    @property
    def effective_date(self) -> date:
        return self.survey_date or date.today()

    @property
    def effective_overture_types(self) -> list[str]:
        return list(self.overture_types or DEFAULT_OVERTURE_TYPES)


@dataclass(frozen=True)
class SurveyResult:
    paths: PackagePaths
    complete: bool
    survey: dict[str, object]


def register_default_sources() -> None:
    register(OsmSource())
    register(OvertureSource())
    register(ElevationSource())


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
    )
    paths = build_package_paths(
        request.output_root,
        request.region,
        request.site,
        request.effective_date,
        fingerprint,
        stem_override=stem_override,
    )
    check_path_length(paths, request.effective_overture_types)
    return tiles, paths


def estimate_survey(request: SurveyRequest) -> dict[str, object]:
    tiles, _paths = _plan(request)
    width_m, height_m = extent_metres(request.bbox)

    total_bytes = 0
    total_seconds = 0.0
    source_summaries: list[dict[str, object]] = []
    for source_id in request.source_ids:
        source = get_source(source_id)
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

    return {
        "tiles": len(tiles),
        "rows": max((t.row for t in tiles), default=0) + 1,
        "cols": max((t.col for t in tiles), default=0) + 1,
        "extent_km": {"width": width_m / 1000.0, "height": height_m / 1000.0},
        "bytes_estimate": total_bytes,
        "seconds_estimate": total_seconds,
        "sources": source_summaries,
    }


def run_survey(
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

    sources = [get_source(source_id) for source_id in request.source_ids]
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
            merged = source.merge(parts, paths.root)
            outputs_by_source[source.id] = merged
            sink.emit("source_done", source=source.id, outputs=[p.name for p in merged])

        _write_layer_files(outputs_by_source, paths)

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
            if bridge_runner is None:
                run_bridge(bridge_request)
            else:
                run_bridge(bridge_request, runner=bridge_runner)
            sink.emit("bridge_done")

    survey = _build_survey_json(request, paths, tiles, sources, state, started_at)
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
) -> None:
    """Copy the three phase 1 Overture layers into layers/ under friendly names."""
    for output in outputs_by_source.get("overture", []):
        friendly = LAYER_FILENAMES.get(output.stem)
        if friendly:
            atomic_write_text(
                paths.layers_dir / friendly, output.read_text(encoding="utf-8")
            )


def _build_survey_json(request, paths, tiles, sources, state, started_at) -> dict:
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
        "sources": [
            {
                "id": source.id,
                "licence": source.licence,
                "attribution": source.attribution,
                "endpoints_used": list(getattr(source, "endpoints_used", [])),
            }
            for source in sources
        ],
        "tiles": state.as_tile_records(),
        "complete": state.complete,
        "started_at": started_at,
        "finished_at": _now(),
    }
