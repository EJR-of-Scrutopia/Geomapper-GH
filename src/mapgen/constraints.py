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

from mapgen.planar import Rect, clip_polygon, point_in_rect, polygon_area_in_rect, share_of_rect

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
    # type_name -> count of features whose geometry classify_feature never
    # reads (a line, a GeometryCollection): counted per layer as well as
    # in total, so summarise() can tell a layer that is truly empty from
    # one DataMapWales answered with nothing mapgen reads.
    skipped_geometry_by_layer: dict = field(default_factory=dict)

    @property
    def skipped_geometry(self) -> int:
        return sum(self.skipped_geometry_by_layer.values())


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
                by_layer = assessment.skipped_geometry_by_layer
                by_layer[result.layer.type_name] = by_layer.get(result.layer.type_name, 0) + 1
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


# The four attribution groups, verbatim from each layer's DataMapWales
# page (the spec's "Attribution groups"). C's {date} is the date the
# data was received from Cadw, as Cadw's own wording instructs; D is
# OGL v3's own default statement, for layers that publish none.
ATTRIBUTIONS: dict[str, str] = {
    "A": (
        "Contains Natural Resources Wales information © Natural Resources Wales and database right. "
        "All rights reserved. Some features of this information are based on digital spatial data licensed "
        "from the UK Centre for Ecology & Hydrology © UKCEH. Defra, Met Office and DARD Rivers Agency "
        "© Crown copyright. © Cranfield University. © James Hutton Institute. Contains OS data "
        "© Crown copyright and database right."
    ),
    "B": (
        "Contains Natural Resources Wales information © Natural Resources Wales and Database Right. "
        "All rights Reserved. Contains Ordnance Survey Data. Ordnance Survey Licence number AC0000849444. "
        "Crown Copyright and Database Right."
    ),
    "C": (
        "Designated Historic Asset GIS Data, The Welsh Historic Environment Service (Cadw), {date}, "
        "licensed under the Open Government Licence"
    ),
    "D": (
        "Contains public sector information licensed under the Open Government Licence v3.0. "
        "Datasets: World Heritage Sites (Site Boundary) and Conservation Area Boundaries, "
        "published on DataMapWales by the Welsh Government."
    ),
}

COAL_LINE = "Coal mining risk: no open data service. See the Mining Remediation Authority's interactive map."
TPO_LINE = "Tree Preservation Orders: no national dataset for Wales. Ask the council."
UNREADABLE_GEOMETRY_REASON = "DataMapWales answered with geometry mapgen does not read"
PARTIAL_LINE = "Part of the extent is outside Wales and was not checked."
CONSERVATION_CAVEAT = "Conservation areas are boundaries only; contact the council for full details."
GRADE_ORDER = ("I", "II*", "II")
LISTED_GROUP = "listed_buildings"
NATIONAL_PARK_TYPE = "inspire-nrw:NRW_NATIONAL_PARK"
CONSERVATION_TYPE = "geonode:conservation_areas_wales"


def attribution_statements(groups, fetched_on: str) -> list[str]:
    wanted = set(groups)
    return [
        ATTRIBUTIONS[group].format(date=fetched_on) if group == "C" else ATTRIBUTIONS[group]
        for group in ("A", "B", "C", "D")
        if group in wanted
    ]


def share_text(share: float) -> str:
    if share >= 0.995:
        return "the whole site"
    if share < 0.005:
        return "under 1% of the site"
    # Half up, not Python's round-half-to-even, so 0.5% reads 1%.
    return f"{int(share * 100 + 0.5)}% of the site"


@dataclass
class Summary:
    on_site: list
    margin: list
    none_found: list
    not_checked: list
    authority: str
    conservation_found: bool
    attribution_groups: list


def _label(item: Found) -> str | None:
    return item.name if item.layer.name_field else item.grade_or_zone


def _grade_counts(items: Sequence[Found]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        grade = item.grade_or_zone or "ungraded"
        counts[grade] = counts.get(grade, 0) + 1
    return counts


def _grades_text(counts: dict[str, int]) -> str:
    known = [grade for grade in GRADE_ORDER if grade in counts]
    others = sorted(grade for grade in counts if grade not in GRADE_ORDER)
    parts = [f"Grade {grade} {counts[grade]}" for grade in known]
    parts += [f"{grade} {counts[grade]}" for grade in others]
    return ", ".join(parts)


def _area_groups(items: Sequence[Found]) -> dict[tuple[str, str | None], list]:
    groups: dict[tuple[str, str | None], list] = {}
    for item in items:
        if item.layer.group == LISTED_GROUP:
            continue
        groups.setdefault((item.layer.type_name, _label(item)), []).append(item)
    return groups


def summarise(assessment: Assessment, councils: Sequence[str], coverage: str) -> Summary:
    order = {layer.type_name: index for index, layer in enumerate(LAYERS)}
    site_items = [item for item in assessment.found if item.where == "site"]
    margin_items = [item for item in assessment.found if item.where == "margin"]
    x_scale = math.cos(math.radians((assessment.site[1] + assessment.site[3]) / 2))

    ranked: list[tuple[int, str, dict]] = []
    site_groups = _area_groups(site_items)
    for (type_name, label), items in site_groups.items():
        layer = LAYER_BY_TYPE[type_name]
        polygons = [rings for item in items for rings in item.site_polygons]
        share = share_of_rect(polygons, assessment.site, SHARE_GRID_CELLS, x_scale)
        subject = f"{layer.designation}: {label}" if label else layer.designation
        ranked.append(
            (order[type_name], label or "", {
                "designation": layer.designation, "label": label, "share": round(share, 4),
                "count": len(items), "grades": None, "line": f"{subject} ({share_text(share)})",
            })
        )
    listed_site = [item for item in site_items if item.layer.group == LISTED_GROUP]
    if listed_site:
        counts = _grade_counts(listed_site)
        ranked.append(
            (order["inspire-wg:Cadw_ListedBuildings"], "", {
                "designation": "Listed building", "label": None, "share": None, "count": len(listed_site),
                "grades": counts, "line": f"Listed buildings: {len(listed_site)} ({_grades_text(counts)})",
            })
        )
    on_site = [entry for _, _, entry in sorted(ranked, key=lambda r: (r[0], r[1]))]

    ranked = []
    for (type_name, label), items in _area_groups(margin_items).items():
        if (type_name, label) in site_groups:
            continue
        layer = LAYER_BY_TYPE[type_name]
        ranked.append(
            (order[type_name], label or "", {
                "designation": layer.designation, "label": label, "share": None, "count": len(items),
                "grades": None, "line": f"{layer.designation}: {label}" if label else layer.designation,
            })
        )
    listed_margin = [item for item in margin_items if item.layer.group == LISTED_GROUP]
    if listed_margin:
        counts = _grade_counts(listed_margin)
        ranked.append(
            (order["inspire-wg:Cadw_ListedBuildings"], "", {
                "designation": "Listed building", "label": None, "share": None, "count": len(listed_margin),
                "grades": counts, "line": f"Listed buildings: {len(listed_margin)} ({_grades_text(counts)})",
            })
        )
    margin = [entry for _, _, entry in sorted(ranked, key=lambda r: (r[0], r[1]))]

    found_types = {item.layer.type_name for item in assessment.found}
    # A layer whose every feature is geometry classify_feature never reads
    # (a line, a GeometryCollection) is not "none found": it was never
    # truly checked, and reads as such below, not here.
    unreadable_types = {
        layer.type_name for layer in assessment.checked
        if layer.type_name not in found_types and assessment.skipped_geometry_by_layer.get(layer.type_name, 0) > 0
    }
    none_found = [
        layer.designation for layer in assessment.checked
        if layer.type_name not in found_types and layer.type_name not in unreadable_types
    ]
    none_found.sort(key=lambda designation: next(i for i, l in enumerate(LAYERS) if l.designation == designation))

    not_checked = [
        {"designation": layer.designation, "reason": reason, "line": f"{layer.designation}: {reason}"}
        for layer, reason in assessment.failed
    ]
    not_checked += [
        {
            "designation": layer.designation, "reason": UNREADABLE_GEOMETRY_REASON,
            "line": f"{layer.designation}: {UNREADABLE_GEOMETRY_REASON}",
        }
        for layer in assessment.checked
        if layer.type_name in unreadable_types
    ]
    not_checked.append({"designation": "Coal mining risk", "reason": "no open data service", "line": COAL_LINE})
    not_checked.append({"designation": "Tree Preservation Orders", "reason": "no national dataset for Wales", "line": TPO_LINE})
    if coverage == "partial":
        not_checked.append({"designation": "Outside Wales", "reason": "outside the Welsh layers", "line": PARTIAL_LINE})

    parks = sorted({item.name for item in site_items if item.layer.type_name == NATIONAL_PARK_TYPE and item.name})
    if parks:
        authority = f"the National Park authority ({' and '.join(parks)})"
    elif councils:
        authority = f"the council ({' and '.join(councils)})"
    else:
        authority = "the local planning authority"

    return Summary(
        on_site=on_site,
        margin=margin,
        none_found=none_found,
        not_checked=not_checked,
        authority=authority,
        conservation_found=any(item.layer.type_name == CONSERVATION_TYPE for item in assessment.found),
        attribution_groups=sorted({layer.attribution for layer in assessment.checked}),
    )


def render_text(summary: Summary, title: str, fetched_on: str, margin_m: float = MARGIN_METRES) -> str:
    lines = [
        f"Planning constraints: {title}",
        f"Indicative, from open data checked {fetched_on}. Confirm with {summary.authority}.",
        "",
        "On the site",
    ]
    lines += [f"- {entry['line']}" for entry in summary.on_site] or ["- nothing found"]
    lines += ["", f"Within {int(margin_m)} m"]
    lines += [f"- {entry['line']}" for entry in summary.margin] or ["- nothing found"]
    lines += ["", "Checked, none found"]
    lines.append(f"- {'; '.join(summary.none_found)}" if summary.none_found else "- none")
    lines += ["", "Not checked"]
    lines += [f"- {entry['line']}" for entry in summary.not_checked]
    if summary.conservation_found:
        lines += ["", CONSERVATION_CAVEAT]
    lines += ["", "Sources"]
    lines += [f"- {statement}" for statement in attribution_statements(summary.attribution_groups, fetched_on)]
    return "\n".join(lines) + "\n"


def to_record(
    summary: Summary,
    assessment: Assessment,
    *,
    councils: Sequence[str],
    coverage: str,
    coverage_share: float,
    margin_m: float,
    summary_file: str,
    fetched_on: str,
) -> dict:
    found_counts: dict[str, int] = {}
    for item in assessment.found:
        found_counts[item.layer.type_name] = found_counts.get(item.layer.type_name, 0) + 1
    failed = {layer.type_name: reason for layer, reason in assessment.failed}
    layers: dict[str, dict] = {}
    for layer in LAYERS:
        if layer.type_name in failed:
            layers[layer.type_name] = {
                "designation": layer.designation, "status": "failed", "features": None,
                "reason": failed[layer.type_name],
            }
        elif layer in assessment.checked:
            features = found_counts.get(layer.type_name, 0)
            skipped = assessment.skipped_geometry_by_layer.get(layer.type_name, 0)
            # The same test summarise() uses to put a layer under Not checked,
            # so the layers map never reads a layer as checked and empty
            # when the summary says it was not checked.
            unreadable = features == 0 and skipped > 0
            layers[layer.type_name] = {
                "designation": layer.designation, "status": "unreadable" if unreadable else "ok",
                "features": features, "reason": UNREADABLE_GEOMETRY_REASON if unreadable else None,
                "skipped_geometry": skipped,
            }
    return {
        "checked": True,
        "fetched_on": fetched_on,
        "margin_m": margin_m,
        "coverage": coverage,
        "coverage_share": round(coverage_share, 4),
        "councils": list(councils),
        "authority": summary.authority,
        "on_site": summary.on_site,
        "margin": summary.margin,
        "none_found": summary.none_found,
        "not_checked": summary.not_checked,
        "layers": layers,
        "skipped_geometry": assessment.skipped_geometry,
        "summary_file": summary_file,
    }
