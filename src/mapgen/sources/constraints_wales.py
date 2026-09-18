"""Planning constraints for Wales: twenty DataMapWales designation
layers over the drawn site plus a 250 m margin.

The spec is docs/superpowers/specs/2026-09-18-mapgen-planning-
constraints-wales-design.md. This module fetches and writes; every
decision about a feature is mapgen.constraints'.

One WFS 2.0 GetFeature per layer, one at a time, in table order. Three
traps verified live: without srsName GeoServer answers in EPSG:27700,
the bbox must be lon,lat order (lat,lon returns nothing, silently), and
startIndex on a table with no primary key answers HTTP 400, and only a
request carrying startIndex is sorted at all (GeoServer's own natural
order, by primary key), so a bare page one and a startIndex page two
need not agree. Every layer's first request is bare; if that page is
already complete (short, or numberMatched says there is no more) it
stands, otherwise it is discarded and the whole layer is re-fetched from
zero with startIndex on every page, so every page it keeps was served in
the same sorted order (see _fetch_layer's own docstring). A layer that fails
again it is reported as not checked, never as none found, and the run
carries on. Only when every layer fails does this source record
tile_failures and raise, so the package is marked incomplete and the
retry pass gets its turn (the spec's "Failures are never absences").

DataMapWales's robots.txt disallows /geoserver/ for crawlers. This is
not a crawler: about twenty requests per survey the owner starts, over
one small area, identified by USER_AGENT (the spec's "Licence and usage
position").
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Sequence

import requests

from mapgen.cog import USER_AGENT
from mapgen.constraints import (
    LAYER_BY_TYPE,
    LAYERS,
    MARGIN_METRES,
    OUTPUT_GROUPS,
    Layer,
    LayerResult,
    assess,
    attribution_statements,
    group_collections,
    padded_rect,
    render_text,
    summarise,
    to_record,
)
from mapgen.fsutil import atomic_write_text
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken
from mapgen.sources.base import (
    FAILURE_SERVICE_ERROR,
    Estimate,
    ProgressSink,
    TileFailure,
    classify_status_failure,
    classify_transport_failure,
)
from mapgen.wales_coverage import COVERAGE_PATH, councils_touching, coverage_share, verdict

WFS_URL = "https://datamap.gov.wales/geoserver/wfs"
PAGE_SIZE = 1000
FEATURE_CEILING = 20_000
REQUEST_TIMEOUT_SECONDS = 60.0
BYTES_PER_LAYER_ESTIMATE = 50_000
SECONDS_PER_LAYER_ESTIMATE = 1.0
WORK_FEATURES = "constraints_layers.jsonl"
WORK_META = "constraints_meta.json"
WHOLE_AREA = "whole-area"


class ConstraintsWalesError(RuntimeError):
    """Raised when no layer at all arrived. Never carries a URL."""


class _LayerFetchError(Exception):
    def __init__(self, kind: str, phrase: str) -> None:
        super().__init__(phrase)
        self.kind = kind
        self.phrase = phrase


def _site_rect(bbox: BBox) -> tuple[float, float, float, float]:
    return (bbox.west, bbox.south, bbox.east, bbox.north)


class ConstraintsWalesSource:
    id = "constraints_wales"
    display_name = "Planning constraints (Wales)"
    licence = "Open Government Licence v3.0"
    # Until merge() knows which layers answered and on what date, every
    # statement, with Cadw's date described rather than invented.
    attribution = "\n".join(attribution_statements("ABCD", "the date of the survey"))
    requires_api_key = False

    def __init__(self, session: object | None = None, coverage_path: Path | None = None) -> None:
        self.session = session if session is not None else requests.Session()
        self._coverage_path = Path(coverage_path) if coverage_path is not None else COVERAGE_PATH
        self.tile_failures: list[TileFailure] = []
        self.endpoints_used: list[str] = []
        self.constraints_record: dict | None = None

    def covers(self, bbox: BBox) -> str:
        return verdict(coverage_share(padded_rect(_site_rect(bbox)), self._coverage_path))

    def tier(self, category: str) -> int | None:
        return 1 if category == "constraints" else None

    def detail(self, bbox: BBox) -> str:
        return "Welsh designations as published, indicative"

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        return Estimate(
            bytes_estimate=len(LAYERS) * BYTES_PER_LAYER_ESTIMATE,
            seconds_estimate=len(LAYERS) * SECONDS_PER_LAYER_ESTIMATE,
        )

    def _get_page(self, layer: Layer, padded, start: int, send_start_index: bool) -> dict:
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": layer.type_name,
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
            "bbox": f"{padded[0]},{padded[1]},{padded[2]},{padded[3]},EPSG:4326",
            "count": str(PAGE_SIZE),
        }
        # GeoServer answers HTTP 400 to startIndex on a table with no primary key,
        # so the bare first request never sends one.
        if send_start_index:
            params["startIndex"] = str(start)
        try:
            response = self.session.get(
                WFS_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            kind, phrase = classify_transport_failure(exc)
            raise _LayerFetchError(kind, phrase) from None
        if response.status_code != 200:
            kind, phrase = classify_status_failure(response.status_code)
            raise _LayerFetchError(kind, phrase)
        try:
            body = response.json()
        except ValueError:
            raise _LayerFetchError(FAILURE_SERVICE_ERROR, "sent an answer that is not GeoJSON") from None
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise _LayerFetchError(FAILURE_SERVICE_ERROR, "sent an answer that is not GeoJSON")
        return body

    def _fetch_layer(self, layer: Layer, padded) -> list:
        """One page, bare, first: GeoServer only sorts a keyless table once
        startIndex is on the request, so a bare page one and a startIndex
        page two need not agree, and the union would carry duplicates and
        omissions with no error. If that first page is already complete
        (short, or numberMatched says there is no more) it stands; a
        complete page from one bare request was, trivially, served in one
        order. Otherwise it is discarded and every page, starting again
        from zero, carries startIndex, so the whole layer was served in
        the same sorted order throughout. A keyless table that needs a
        second page then answers 400 on that restart, same as any other
        service failure."""
        too_many = f"holds more than {FEATURE_CEILING:,} features here, too many to check"

        def _checked(features: list, matched: object) -> list:
            if isinstance(matched, int) and matched > FEATURE_CEILING:
                raise _LayerFetchError(FAILURE_SERVICE_ERROR, too_many)
            if len(features) > FEATURE_CEILING:
                raise _LayerFetchError(FAILURE_SERVICE_ERROR, too_many)
            return features

        def _complete(page: list, matched: object) -> bool:
            return len(page) < PAGE_SIZE or (isinstance(matched, int) and matched <= len(page))

        first_body = self._get_page(layer, padded, 0, send_start_index=False)
        first_matched = first_body.get("numberMatched")
        first_page = first_body["features"]
        if _complete(first_page, first_matched):
            return _checked(first_page, first_matched)

        features: list = []
        start = 0
        while True:
            body = self._get_page(layer, padded, start, send_start_index=True)
            matched = body.get("numberMatched")
            page = body["features"]
            features.extend(page)
            _checked(features, matched)
            if len(page) < PAGE_SIZE or (isinstance(matched, int) and len(features) >= matched):
                return features
            start += PAGE_SIZE

    def _attempt(self, layer: Layer, padded) -> LayerResult:
        try:
            return LayerResult(layer=layer, ok=True, features=tuple(self._fetch_layer(layer, padded)))
        except _LayerFetchError as exc:
            return LayerResult(layer=layer, ok=False, kind=exc.kind, reason=f"DataMapWales {exc.phrase}")

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        self.tile_failures = []
        self.constraints_record = None
        site = _site_rect(bbox)
        padded = padded_rect(site)
        features_path = Path(work_dir) / WORK_FEATURES
        meta_path = Path(work_dir) / WORK_META
        if features_path.exists() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
            if meta.get("site") == list(site) and meta.get("margin_m") == MARGIN_METRES:
                progress.emit("tile_skipped", source=self.id, tile_id=WHOLE_AREA)
                return [features_path, meta_path]

        self.endpoints_used = [WFS_URL]
        results: dict[str, LayerResult] = {}
        for layer in LAYERS:
            if cancel is not None:
                cancel.raise_if_cancelled()
            results[layer.type_name] = self._attempt(layer, padded)
        for layer in LAYERS:
            if results[layer.type_name].ok:
                continue
            if cancel is not None:
                cancel.raise_if_cancelled()
            results[layer.type_name] = self._attempt(layer, padded)
        ordered = [results[layer.type_name] for layer in LAYERS]

        if not any(result.ok for result in ordered):
            first = ordered[0]
            reason = f"No planning constraint layer arrived: {first.reason}."
            self.tile_failures = [
                TileFailure(source=self.id, tile_id=tile.tile_id, kind=first.kind or FAILURE_SERVICE_ERROR, reason=reason)
                for tile in tiles
            ]
            raise ConstraintsWalesError("No planning constraint layer arrived from DataMapWales.")

        for result in ordered:
            if not result.ok:
                progress.emit("constraint_layer_not_checked", layer=result.layer.designation, reason=result.reason)

        share = coverage_share(padded, self._coverage_path)
        councils = councils_touching(site, self._coverage_path)
        meta = {
            "site": list(site),
            "margin_m": MARGIN_METRES,
            "fetched_on": datetime.date.today().isoformat(),
            "coverage": verdict(share),
            "coverage_share": share,
            "councils": councils,
        }
        rows = [
            json.dumps(
                {"type_name": r.layer.type_name, "ok": r.ok, "kind": r.kind, "reason": r.reason, "features": list(r.features)},
                separators=(",", ":"),
            )
            for r in ordered
        ]
        atomic_write_text(features_path, "\n".join(rows) + "\n")
        atomic_write_text(meta_path, json.dumps(meta, separators=(",", ":")))

        summary = summarise(assess(site, ordered), councils, meta["coverage"])
        for entry in summary.on_site:
            progress.emit("constraint_on_site", line=entry["line"])
        progress.emit("tile_done", source=self.id, tile_id=WHOLE_AREA)
        return [features_path, meta_path]

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Every package output, from the two work files: one GeoJSON per
        group that has features, and the summary text, always. Sets
        constraints_record (read by package.py into survey.json) and
        attribution (only the groups whose layers answered, Cadw's date
        substituted)."""
        self.constraints_record = None
        self.attribution = type(self).attribution
        features_path = next((Path(p) for p in parts if Path(p).name == WORK_FEATURES), None)
        meta_path = next((Path(p) for p in parts if Path(p).name == WORK_META), None)
        if features_path is None or meta_path is None:
            return []
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        results = []
        for line in features_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            layer = LAYER_BY_TYPE.get(row["type_name"])
            if layer is None:
                continue
            results.append(
                LayerResult(layer=layer, ok=row["ok"], features=tuple(row["features"]), kind=row["kind"], reason=row["reason"])
            )
        site = tuple(meta["site"])
        assessment = assess(site, results, meta["margin_m"])
        summary = summarise(assessment, meta["councils"], meta["coverage"])
        out_dir = Path(out_dir)
        written: list[Path] = []
        for group, collection in group_collections(assessment).items():
            path = out_dir / f"{stem}_constraints_{group}.geojson"
            atomic_write_text(path, json.dumps(collection, separators=(",", ":")))
            written.append(path)
        summary_path = out_dir / f"{stem}_constraints.txt"
        atomic_write_text(summary_path, render_text(summary, stem, meta["fetched_on"], meta["margin_m"]))
        written.append(summary_path)
        self.constraints_record = to_record(
            summary,
            assessment,
            councils=meta["councils"],
            coverage=meta["coverage"],
            coverage_share=meta["coverage_share"],
            margin_m=meta["margin_m"],
            summary_file=summary_path.name,
            fetched_on=meta["fetched_on"],
        )
        self.attribution = "\n".join(attribution_statements(summary.attribution_groups, meta["fetched_on"]))
        return written

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root file merge() could ever write for this stem. Read by
        package.py's stale-output sweep; see sources/base.py."""
        return [f"{stem}_constraints_{group}.geojson" for group in OUTPUT_GROUPS] + [f"{stem}_constraints.txt"]
