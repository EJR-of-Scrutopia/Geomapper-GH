"""Orchestration: plan, download, merge, bridge, survey.json.

Knows about paths, job state and the source registry. Knows nothing about any
individual data source, which is what makes phase 2 additive.
"""

from __future__ import annotations

import json
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
from mapgen.geo import BBox, build_tiles, extent_metres
from mapgen.jobs import FAILED, OK, CancelToken, JobState
from mapgen.merge import assert_inputs_present
from mapgen.naming import PackagePaths, build_package_paths, check_path_length, slugify
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
    paths = build_package_paths(
        request.output_root,
        request.region,
        request.site,
        request.effective_date,
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
    ensure_dir(paths.root)
    ensure_dir(paths.layers_dir)
    started_at = _now()

    sink.emit("job_started", tiles=len(tiles), root=str(paths.root))

    sources = [get_source(source_id) for source_id in request.source_ids]
    state = JobState.load_or_create(
        paths.work_dir, [t.tile_id for t in tiles], [s.id for s in sources]
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
            try:
                parts = source.fetch(request.bbox, pending, source_work, sink)
                for tile in pending:
                    state.mark(tile.tile_id, source.id, OK)
            except Exception as exc:
                for tile in pending:
                    state.mark(tile.tile_id, source.id, FAILED)
                sink.emit("source_failed", source=source.id, error=str(exc))
                if not request.force:
                    raise
                parts = [p for p in sorted(source_work.rglob("*")) if p.is_file()]

            # Refuse to merge a tile set with holes unless the caller forced it.
            parts = assert_inputs_present(parts, force=request.force)
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
    if state.complete and not request.keep_work:
        best_effort_rmtree(paths.work_dir)

    return SurveyResult(paths=paths, complete=state.complete, survey=survey)


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
