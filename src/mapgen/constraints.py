"""Planning constraints for Wales: the layer table, and every decision
made about a fetched feature, with no network at all.

The source (mapgen.sources.constraints_wales) fetches; this module
decides. Everything here works in the drawn site's own lon/lat frame
(see mapgen.planar's docstring and the spec's "Architecture"). A
feature the server returned is re-tested here for true intersection
with the site and with the 250 m margin, because GeoServer's bbox
filter can match on a feature's envelope rather than its shape (the
spec's "loose bbox" note): one that touches neither is dropped before
it reaches any file, count or summary line.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from mapgen.planar import Rect, clip_polygon, point_in_rect, polygon_area_in_rect

METRES_PER_DEGREE_LAT = 111_320.0
MARGIN_METRES = 250.0
SHARE_GRID_CELLS = 400
OUTPUT_GROUPS = ("flood", "nature", "landscape", "heritage", "listed_buildings")


@dataclass(frozen=True)
class Layer:
    type_name: str
    designation: str
    group: str
    attribution: str
    name_field: str | None
    grade_field: str | None
    reference_field: str | None


# The spec's table, in the spec's order; field names read off each live
# layer on 2026-09-18.
LAYERS: tuple[Layer, ...] = (
    Layer("inspire-nrw:NRW_FLOODZONE_RIVERS", "Flood zone, rivers", "flood", "A", None, "risk", "mm_id"),
    Layer("inspire-nrw:NRW_FLOODZONE_SEAS", "Flood zone, sea", "flood", "A", None, "risk", "mm_id"),
    Layer(
        "inspire-nrw:NRW_FLOODZONE_SURFACE_WATER_AND_SMALL_WATERCOURSES",
        "Flood zone, surface water and small watercourses", "flood", "A", None, "risk", "mm_id",
    ),
    Layer("inspire-nrw:NRW_TAN15_DEFENDED_ZONES", "TAN15 defended zone", "flood", "A", "name", "defends_against", None),
    Layer("inspire-nrw:NRW_SSSI", "SSSI", "nature", "B", "sssi_name", None, "sssi_code"),
    Layer("inspire-nrw:NRW_SAC", "Special Area of Conservation", "nature", "B", "SAC_name", None, "Site_code"),
    Layer("inspire-nrw:NRW_SPA", "Special Protection Area", "nature", "B", "SPA_Name", None, "SPA_code"),
    Layer("inspire-nrw:NRW_RAMSAR", "Ramsar site", "nature", "B", "RAM_name", None, "RAM_code"),
    Layer("inspire-nrw:NRW_NNR", "National Nature Reserve", "nature", "B", "NNR_Name", None, "NNR_Code"),
    Layer("inspire-nrw:NRW_LNR", "Local Nature Reserve", "nature", "B", "lnr_name", None, "isis_id"),
    Layer(
        "inspire-nrw:NRW_ANCIENT_WOODLAND_INVENTORY_2021", "Ancient woodland", "nature", "B",
        None, "category_name", "unique_id",
    ),
    Layer("inspire-nrw:NRW_NATIONAL_PARK", "National Park", "landscape", "B", "np_name", None, "isis_id"),
    Layer("inspire-nrw:NRW_AONB", "AONB (National Landscape)", "landscape", "B", "AONB_NAME", None, "ISIS_ID"),
    Layer("inspire-nrw:NRW_HERITAGE_COAST", "Heritage Coast", "landscape", "B", "Coast_name", None, "ISIS_id"),
    Layer("inspire-wg:Cadw_ListedBuildings", "Listed building", "listed_buildings", "C", "Name", "Grade", "RecordNumber"),
    Layer("inspire-wg:Cadw_SAM", "Scheduled monument", "heritage", "C", "Name", None, "SAMNumber"),
    Layer(
        "geonode:cadw_rhpg_registeredareas", "Registered historic park or garden", "heritage", "C",
        "site_name_en", None, "reference_number",
    ),
    Layer(
        "inspire-wg:Cadw_HistoricLandscapes", "Registered historic landscape", "heritage", "C",
        "Name", "RegisterType", "RecordNumber",
    ),
    Layer(
        "inspire-wg:vGeoServer_WorldHeritageSites_Public", "World Heritage Site", "heritage", "D",
        "Name", None, "UNESCO_Ref",
    ),
    Layer("geonode:conservation_areas_wales", "Conservation area", "heritage", "D", "sitename", None, "inspireid"),
)
LAYER_BY_TYPE: dict[str, Layer] = {layer.type_name: layer for layer in LAYERS}


@dataclass(frozen=True)
class LayerResult:
    """One layer's answer: its raw GeoJSON features when ok, else the
    failure kind (sources/base.py's vocabulary) and a plain reason."""

    layer: Layer
    ok: bool
    features: tuple = ()
    kind: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Found:
    """One feature that truly touches the site or the margin."""

    layer: Layer
    name: str | None
    grade_or_zone: str | None
    reference: str | None
    where: str
    geometry: dict
    site_polygons: tuple


@dataclass
class Assessment:
    site: Rect
    padded: Rect
    found: list = field(default_factory=list)
    checked: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped_geometry: int = 0


def padded_rect(site: Rect, margin_m: float = MARGIN_METRES) -> Rect:
    west, south, east, north = site
    lat0 = math.radians((south + north) / 2)
    dlat = margin_m / METRES_PER_DEGREE_LAT
    dlon = margin_m / (METRES_PER_DEGREE_LAT * math.cos(lat0))
    return (west - dlon, south - dlat, east + dlon, north + dlat)


def _text(value: object) -> str | None:
    """Trimmed text, or None for a missing, blank or literal "null"
    value (the NRW layers pad names with spaces and write "null")."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text


def _polygons(geometry: dict) -> list:
    kind = geometry.get("type")
    if kind == "Polygon":
        return [geometry["coordinates"]]
    if kind == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def _points(geometry: dict) -> list:
    kind = geometry.get("type")
    if kind == "Point":
        return [geometry["coordinates"]]
    if kind == "MultiPoint":
        return list(geometry["coordinates"])
    return []


def _closed(ring: list) -> list:
    return [list(point) for point in ring] + [list(ring[0])]


def classify_feature(layer: Layer, feature: dict, site: Rect, padded: Rect) -> Found | None:
    """The feature as found, or None when its shape reaches neither the
    site nor the margin (or it has no polygon or point geometry)."""
    geometry = feature.get("geometry") or {}
    properties = feature.get("properties") or {}
    polygons = _polygons(geometry)
    points = _points(geometry)
    if polygons:
        on_site = any(polygon_area_in_rect(rings, site) > 0.0 for rings in polygons)
        if not on_site and not any(polygon_area_in_rect(rings, padded) > 0.0 for rings in polygons):
            return None
        clipped = [c for c in (clip_polygon(rings, padded) for rings in polygons) if c]
        out_geometry = {
            "type": "MultiPolygon",
            "coordinates": [[_closed(ring) for ring in rings] for rings in clipped],
        }
        site_polygons = (
            tuple(tuple(c) for c in (clip_polygon(rings, site) for rings in polygons) if c) if on_site else ()
        )
    elif points:
        coordinates = [(float(p[0]), float(p[1])) for p in points]
        inside = [p for p in coordinates if point_in_rect(p, padded)]
        if not inside:
            return None
        on_site = any(point_in_rect(p, site) for p in coordinates)
        out_geometry = {"type": "MultiPoint", "coordinates": [list(p) for p in inside]}
        site_polygons = ()
    else:
        return None
    return Found(
        layer=layer,
        name=_text(properties.get(layer.name_field)) if layer.name_field else None,
        grade_or_zone=_text(properties.get(layer.grade_field)) if layer.grade_field else None,
        reference=_text(properties.get(layer.reference_field)) if layer.reference_field else None,
        where="site" if on_site else "margin",
        geometry=out_geometry,
        site_polygons=site_polygons,
    )


def assess(site: Rect, results: Sequence[LayerResult], margin_m: float = MARGIN_METRES) -> Assessment:
    padded = padded_rect(site, margin_m)
    assessment = Assessment(site=tuple(site), padded=padded)
    for result in results:
        if not result.ok:
            assessment.failed.append((result.layer, result.reason or "did not arrive"))
            continue
        assessment.checked.append(result.layer)
        for feature in result.features:
            geometry = feature.get("geometry") or {}
            if not _polygons(geometry) and not _points(geometry):
                assessment.skipped_geometry += 1
                continue
            item = classify_feature(result.layer, feature, assessment.site, padded)
            if item is not None:
                assessment.found.append(item)
    return assessment


def group_collections(assessment: Assessment) -> dict[str, dict]:
    """One FeatureCollection per output group that has features, in
    OUTPUT_GROUPS order."""
    by_group: dict[str, list] = {}
    for item in assessment.found:
        by_group.setdefault(item.layer.group, []).append(
            {
                "type": "Feature",
                "properties": {
                    "designation": item.layer.designation,
                    "name": item.name,
                    "grade_or_zone": item.grade_or_zone,
                    "reference": item.reference,
                    "where": item.where,
                    "source": item.layer.type_name,
                },
                "geometry": item.geometry,
            }
        )
    return {
        group: {"type": "FeatureCollection", "features": by_group[group]}
        for group in OUTPUT_GROUPS
        if group in by_group
    }
