"""`mapgen benchmark`: measuring a package's own open-stack outputs against
OS's survey-grade NGD data over the same extent, behind a binding
derived-data firewall.

## The firewall, made concrete here

`ngd.py` never writes to disk (see its own module docstring); this is the
one module that reads what it fetched, computes AGGREGATE statistics, and
writes them to a report. Nothing from the NGD pull itself, no coordinate,
no feature id, no attribute value beyond a class NAME, ever reaches
`report.json`/`report.md`, and nothing from either report ever reaches the
package directory: `run_benchmark` reads `package_dir` and writes only
under `out_root`, never back into `package_dir`. The plan this module
ships under (`docs/superpowers/plans/2026-08-09-mapgen-phase2b-f-os-
benchmark.md`) states the rule this module exists to make true by
construction: nothing from any OS premium product may enter a package, a
fusion, a shard cache, or any output a client could receive.

## The key is radioactive here too

`run_benchmark` takes the key as a plain string and hands it straight to
`client_factory` (ordinarily `NgdClient`, see `ngd.py`'s own "the key is
radioactive" section); nothing in this module ever writes the key
anywhere, logs it, or lets it reach an exception message. The CLI's own
`command_benchmark` (`cli.py`) resolves it from `--key` or the
`OS_NGD_KEY` environment variable ONLY, never from `~/.mapgen/config.json`.

## Reading the package

`survey.json` gives the stem (`urbano_stem`) and the bbox. Buildings are
every `building=*` way in `<stem>.osm`, the FUSED file: OSM's own trace
carries no `source` tag at all, while Overture and OS OpenMap Local
injections (`mapgen.buildings.fuse_missing_buildings`) each carry one, so
splitting building counts by that tag (missing counted as `"osm"`) is
exactly a split by which pipeline stage put a given footprint there.
Roads are every `highway=*` way in the same file (OSM-derived, so subject
to the epoch question) PLUS, when `<stem>_os_roads.geojson` exists (OS
OpenMap Local's own road layer, `mapgen.sources.os_open`), its LineStrings
as a SEPARATE population labelled `os_open`: that file is written in
WGS84 (`OsOpenSource.merge`'s own `_reproject_geometry`, `from_bng`) but
its underlying survey is BNG-native already, so its own offset against
NGD measures generalisation between two OS products, not an epoch gap,
and the report says so.

Every one of our own coordinates reaches BNG the same way `heights.py`'s
`_fuse_heights_step` does: `to_bng` per node, `load_ostn15()` cache-only
first, `ensure_ostn15()` only if that misses (this module's own only
network call apart from the NGD pull itself).

## Nothing fabricated

An empty NGD pull reports zeros honestly: `matched_fraction_ours`/
`matched_fraction_theirs` are 0.0 (not NaN, not omitted) when either side
is empty, `OffsetStats` is already built to the same standard
(`benchstats.py`'s own module docstring), and the epoch verdict reads
"INCONCLUSIVE" rather than guessing when the least-squares estimate is
undefined. Nothing here invents a class name, a count, or a verdict that
the numbers do not support.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

from mapgen.benchstats import (
    OffsetStats,
    distribution,
    match_footprints,
    polyline_offsets,
)
from mapgen.bng import BngError, Ostn15Grid, ensure_ostn15, load_ostn15, padded_bng_extent, to_bng
from mapgen.fsutil import atomic_write_bytes, atomic_write_text
from mapgen.geo import BBox
from mapgen.ngd import BUILDING_COLLECTION, ROAD_COLLECTION, NgdClient

_BUILDING_TAG_KEY = "building"
_HIGHWAY_TAG_KEY = "highway"
_SOURCE_TAG_KEY = "source"

# The bucket a building way with no `source` tag at all falls into:
# `mapgen.sources.osm`'s own merge never writes one, only
# `buildings.fuse_missing_buildings`'s Overture/OS-OpenMap-Local
# injections do (see the module docstring).
OSM_SOURCE_LABEL = "osm"

_NGD_DESCRIPTION_PROPERTY = "description"

# The epoch-shift note's own hypothesis (docs/superpowers/specs/
# 2026-08-06-epoch-shift-note.md): "roughly 0.9 m north-east". The three
# bands below are task-3-brief's own pinned wording, read against the
# LEAST-SQUARES offset vector (the controller addition born from
# task-2-review.md's bias finding, see benchstats.OffsetStats's own
# docstring): the naive mean understates a real shift on anything but a
# single-orientation street population, so the verdict cannot be read off
# it without reintroducing that exact understatement.
_EPOCH_CONSISTENT_MIN_M = 0.5
_EPOCH_CONSISTENT_MAX_M = 1.5
_EPOCH_NOT_DETECTED_MAX_M = 0.3
_NORTHEAST_BEARING_DEG = 45.0
_EPOCH_BEARING_TOLERANCE_DEG = 45.0

_IOU_PERCENTILE_LABELS = {0.1: "p10", 0.5: "p50", 0.9: "p90"}


class BenchmarkError(RuntimeError):
    """Raised when `package_dir` cannot be read well enough to benchmark.

    A plain, one-line message naming the file and the problem, matching
    every other package-reading refusal in this project
    (`UnbridgeablePackageError`, `HeightsError`); never a URL, never the
    NGD key, since this class shares the same "no radioactive string in
    any raised message" discipline as `NgdError` and `BngError`.
    """


# --------------------------------------------------------------------------
# Reading the package: survey.json, <stem>.osm, <stem>_os_roads.geojson.
# --------------------------------------------------------------------------


def _read_survey(package_dir: Path) -> tuple[str, BBox]:
    """`(stem, bbox)` from `package_dir`'s own `survey.json`.

    `stem` is the `urbano_stem` field, the same one every other package
    reader in this project (`package.py`, `heights.py`) uses to name a
    package's own merged files. `bbox` is rebuilt from the `west`/
    `south`/`east`/`north` floats `BBox.to_dict()` itself writes.
    """
    survey_path = package_dir / "survey.json"
    try:
        text = survey_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkError(f"{survey_path} could not be read: {exc}") from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"{survey_path} is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise BenchmarkError(f"{survey_path} is not a JSON object.")

    stem = payload.get("urbano_stem")
    if not isinstance(stem, str) or not stem:
        raise BenchmarkError(f"{survey_path} has no urbano_stem to benchmark against.")

    bbox_dict = payload.get("bbox")
    if not isinstance(bbox_dict, dict):
        raise BenchmarkError(f"{survey_path} has no bbox to pull NGD data over.")
    try:
        bbox = BBox(
            west=float(bbox_dict["west"]),
            south=float(bbox_dict["south"]),
            east=float(bbox_dict["east"]),
            north=float(bbox_dict["north"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BenchmarkError(f"{survey_path}'s bbox is not the expected shape: {exc}") from None
    return stem, bbox


def _read_osm(osm_path: Path) -> ET.Element:
    try:
        text = osm_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkError(f"{osm_path} could not be read: {exc}") from None
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise BenchmarkError(f"{osm_path} is not valid XML: {exc}") from None


def _osm_nodes(root: ET.Element) -> dict[str, tuple[float, float]]:
    """Every `<node>`'s own id mapped to `(lat, lon)`, `heights._fuse_
    building_heights`'s own node table exactly."""
    nodes: dict[str, tuple[float, float]] = {}
    for element in root:
        if element.tag != "node":
            continue
        node_id = element.get("id")
        lat = element.get("lat")
        lon = element.get("lon")
        if node_id is None or lat is None or lon is None:
            continue
        try:
            nodes[node_id] = (float(lat), float(lon))
        except ValueError:
            continue
    return nodes


def _way_tags(way: ET.Element) -> dict[str, str]:
    tags: dict[str, str] = {}
    for tag in way.findall("tag"):
        key, value = tag.get("k"), tag.get("v")
        if key is not None and value is not None:
            tags[key] = value
    return tags


def _way_latlon(
    way: ET.Element, nodes: dict[str, tuple[float, float]]
) -> list[tuple[float, float]] | None:
    """A way's own nodes as `(lat, lon)` pairs, in file order, or None if
    fewer than 2 `nd` refs are present or any of them does not resolve
    against `nodes`.

    Two points, not `heights._footprint_ring`'s own three-point floor for
    a polygon: a road is an open polyline here, never closed into a ring.
    """
    refs = [nd.get("ref") for nd in way.findall("nd")]
    if len(refs) < 2:
        return None
    coords: list[tuple[float, float]] = []
    for ref in refs:
        latlon = nodes.get(ref) if ref is not None else None
        if latlon is None:
            return None
        coords.append(latlon)
    return coords


def _project(
    latlon: Sequence[tuple[float, float]], grid: Ostn15Grid
) -> list[tuple[float, float]] | None:
    """`latlon`'s own points projected to BNG via `to_bng`, or None if any
    of them falls outside OSTN15's own coverage: the same "no OSTN15
    coverage reads the same as no data" rule `heights.fuse_building_
    heights` already applies to a footprint, applied here to a footprint
    or a road alike (nothing fabricated for an unprojectable feature; it
    is simply left out of the population it would have joined).
    """
    try:
        return [to_bng(lat, lon, grid) for lat, lon in latlon]
    except BngError:
        return None


def _read_buildings(
    root: ET.Element, nodes: dict[str, tuple[float, float]], grid: Ostn15Grid
) -> tuple[list[list[tuple[float, float]]], dict[str, int], dict[str, int]]:
    """Every `building=*` way in `root`, projected to BNG.

    Returns `(rings, counts_by_source, tag_value_counts)`: `rings` is the
    population `match_footprints` runs against; `counts_by_source` is how
    many buildings carry each `source` tag value (`OSM_SOURCE_LABEL` for
    none at all); `tag_value_counts` is how many carry each `building=
    <value>` tag value, the report's own comparison against NGD's
    `description` frequency table. The latter two are counted for every
    building=* way regardless of whether its own footprint could be
    projected, since they describe what this package's own tagging looks
    like, not what could be compared: an unprojectable footprint is a
    fact about that one way, not a reason to hide it from a tag count.
    """
    rings: list[list[tuple[float, float]]] = []
    counts_by_source: dict[str, int] = {}
    tag_value_counts: dict[str, int] = {}
    for element in root:
        if element.tag != "way":
            continue
        tags = _way_tags(element)
        if _BUILDING_TAG_KEY not in tags:
            continue

        source = tags.get(_SOURCE_TAG_KEY, OSM_SOURCE_LABEL)
        counts_by_source[source] = counts_by_source.get(source, 0) + 1
        tag_value = tags[_BUILDING_TAG_KEY]
        tag_value_counts[tag_value] = tag_value_counts.get(tag_value, 0) + 1

        latlon = _way_latlon(element, nodes)
        if latlon is None or len(latlon) < 3:
            continue
        ring = _project(latlon, grid)
        if ring is not None:
            rings.append(ring)
    return rings, counts_by_source, tag_value_counts


def _read_osm_roads(
    root: ET.Element, nodes: dict[str, tuple[float, float]], grid: Ostn15Grid
) -> list[list[tuple[float, float]]]:
    """Every `highway=*` way in `root`, projected to BNG: the OSM-derived
    road population the epoch section interprets (see the module
    docstring).
    """
    polylines: list[list[tuple[float, float]]] = []
    for element in root:
        if element.tag != "way":
            continue
        tags = _way_tags(element)
        if _HIGHWAY_TAG_KEY not in tags:
            continue
        latlon = _way_latlon(element, nodes)
        if latlon is None:
            continue
        polyline = _project(latlon, grid)
        if polyline is not None:
            polylines.append(polyline)
    return polylines


def _read_os_open_roads(geojson_path: Path, grid: Ostn15Grid) -> list[list[tuple[float, float]]]:
    """`<stem>_os_roads.geojson`'s own LineString features, projected to
    BNG: the BNG-native control population (see the module docstring).

    `[]`, not an error, when the file simply does not exist: this layer
    is optional (only present when the package selected `os_open`), the
    same "missing candidate file" tolerance `package._fuse_buildings_step`
    already applies to `<stem>_os_buildings.geojson`. A file that exists
    but is not the shape `OsOpenSource.merge` itself writes IS a
    `BenchmarkError`, matching `package._load_building_features`'s own
    line: this project's own writer producing a malformed file is this
    project's own bug, not upstream data to shrug off.
    """
    if not geojson_path.is_file():
        return []
    try:
        text = geojson_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkError(f"{geojson_path} could not be read: {exc}") from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"{geojson_path} is not valid JSON: {exc}") from None
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise BenchmarkError(f"{geojson_path} is not a GeoJSON FeatureCollection.")
    features = payload.get("features")
    if not isinstance(features, list):
        raise BenchmarkError(f"{geojson_path} has no features list.")

    polylines: list[list[tuple[float, float]]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            continue
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            # GeoJSON order is [lon, lat]; OsOpenSource.merge's own
            # _reproject_geometry writes exactly that order via from_bng.
            latlon = [(float(pair[1]), float(pair[0])) for pair in coordinates]
        except (TypeError, ValueError, IndexError):
            continue
        polyline = _project(latlon, grid)
        if polyline is not None:
            polylines.append(polyline)
    return polylines


# --------------------------------------------------------------------------
# Reading the NGD pull: geometry and the one attribute the report is
# allowed to name (class NAMES only, never a coordinate or an id).
# --------------------------------------------------------------------------


def _ngd_building_rings(features: Sequence[dict]) -> list[list[tuple[float, float]]]:
    """Every buildingpart feature's own exterior ring, already BNG (the
    `crs`/`bbox-crs` the client requested; see `ngd.py`'s own docstring):
    no projection here, unlike every one of our own populations above.
    """
    rings: list[list[tuple[float, float]]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        exterior = None
        if geometry_type == "Polygon" and isinstance(coordinates, list) and coordinates:
            exterior = coordinates[0]
        elif geometry_type == "MultiPolygon" and isinstance(coordinates, list) and coordinates:
            first_polygon = coordinates[0]
            if isinstance(first_polygon, list) and first_polygon:
                exterior = first_polygon[0]
        if not isinstance(exterior, list) or len(exterior) < 3:
            continue
        try:
            ring = [(float(pair[0]), float(pair[1])) for pair in exterior]
        except (TypeError, ValueError, IndexError):
            continue
        rings.append(ring)
    return rings


def _ngd_road_polylines(features: Sequence[dict]) -> list[list[tuple[float, float]]]:
    polylines: list[list[tuple[float, float]]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            continue
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            polyline = [(float(pair[0]), float(pair[1])) for pair in coordinates]
        except (TypeError, ValueError, IndexError):
            continue
        polylines.append(polyline)
    return polylines


def _ngd_description_counts(features: Sequence[dict]) -> dict[str, int]:
    """How many buildingpart features carry each `description` value:
    the ONLY NGD attribute this module ever reads past geometry, and
    only ever turned into a class-name frequency table, never into a
    per-feature value anywhere a report could echo it back to a
    coordinate or an id.
    """
    counts: dict[str, int] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue
        value = properties.get(_NGD_DESCRIPTION_PROPERTY)
        if value is None:
            continue
        name = str(value)
        counts[name] = counts.get(name, 0) + 1
    return counts


# --------------------------------------------------------------------------
# Statistics glue: OffsetStats -> plain dict, distribution() -> "p10" keys.
# --------------------------------------------------------------------------


def _offset_stats_dict(stats: OffsetStats) -> dict[str, object]:
    return {
        "count": stats.count,
        "unmatched_samples": stats.unmatched_samples,
        "mean_de": stats.mean_de,
        "mean_dn": stats.mean_dn,
        "magnitude_of_mean": stats.magnitude_of_mean,
        "std_de": stats.std_de,
        "std_dn": stats.std_dn,
        "p50_abs": stats.p50_abs,
        "p90_abs": stats.p90_abs,
        "lsq_de": stats.lsq_de,
        "lsq_dn": stats.lsq_dn,
        "lsq_magnitude": stats.lsq_magnitude,
    }


def _iou_percentiles(ious: Sequence[float]) -> dict[str, float]:
    """`{"p10": ..., "p50": ..., "p90": ...}`, converting `distribution`'s
    own fraction-keyed dict (`{0.5: ...}`) to the report's own string
    labels. 0.0 for all three when there are no matched pairs at all
    (nothing fabricated: `buildings.matched == 0` in the same report
    already says these three numbers describe nothing real).
    """
    if not ious:
        return {"p10": 0.0, "p50": 0.0, "p90": 0.0}
    raw = distribution(ious, fractions=(0.1, 0.5, 0.9))
    return {_IOU_PERCENTILE_LABELS[fraction]: value for fraction, value in raw.items()}


# --------------------------------------------------------------------------
# The epoch verdict: read off the least-squares vector (controller
# addition), never the naive mean.
# --------------------------------------------------------------------------


def _bearing_degrees(east: float, north: float) -> float:
    """Compass bearing of `(east, north)`, 0 = north, 90 = east, in
    `[0, 360)`."""
    return math.degrees(math.atan2(east, north)) % 360.0


def _angular_difference(a: float, b: float) -> float:
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


def _epoch_verdict(osm_offsets: OffsetStats) -> str:
    """One of three sentences, read against `osm_offsets.lsq_*`, never the
    naive mean (see the module docstring's citation of the controller
    addition). `std_de`/`std_dn` (the ordinary per-sample offset spread,
    unchanged by the least-squares addition) still stand in for "how
    consistent is this vector", combined here as `hypot(std_de, std_dn)`
    against twice the least-squares magnitude, since the brief's own
    "std under twice the magnitude" band was written before the
    least-squares field existed and names no lsq-specific standard
    deviation of its own to use instead.
    """
    if osm_offsets.count == 0:
        return "INCONCLUSIVE: no matched road samples to measure an offset from."
    if osm_offsets.lsq_magnitude is None:
        return (
            "INCONCLUSIVE: the least-squares offset estimate is undefined; "
            "the sampled road population is too close to one orientation "
            "to separate the two shift components."
        )

    magnitude = osm_offsets.lsq_magnitude
    if magnitude < _EPOCH_NOT_DETECTED_MAX_M:
        return f"NOT DETECTED: least-squares offset magnitude {magnitude:.2f} m is under 0.3 m."

    bearing = _bearing_degrees(osm_offsets.lsq_de, osm_offsets.lsq_dn)
    bearing_ok = (
        _angular_difference(bearing, _NORTHEAST_BEARING_DEG) <= _EPOCH_BEARING_TOLERANCE_DEG
    )
    std_combined = math.hypot(osm_offsets.std_de, osm_offsets.std_dn)
    consistent = (
        _EPOCH_CONSISTENT_MIN_M <= magnitude <= _EPOCH_CONSISTENT_MAX_M
        and bearing_ok
        and std_combined < 2.0 * magnitude
    )
    if consistent:
        return (
            f"CONSISTENT with the epoch-shift hypothesis: least-squares offset "
            f"{magnitude:.2f} m at bearing {bearing:.0f} degrees."
        )
    return (
        f"INCONCLUSIVE: least-squares offset {magnitude:.2f} m at bearing "
        f"{bearing:.0f} degrees does not clear every CONSISTENT band."
    )


# --------------------------------------------------------------------------
# The report itself.
# --------------------------------------------------------------------------


def _render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# OS NGD benchmark: {report['package']}")
    lines.append("")
    lines.append(
        f"Source: {report['source']}. Pulled {report['pulled']}. These numbers "
        f"exist for internal calibration only; they never reach a client or a "
        f"package."
    )
    lines.append("")
    west, south, east, north = report["bbox"]
    lines.append(f"Bbox (WGS84): west {west}, south {south}, east {east}, north {north}")
    lines.append("")

    lines.append("## Pages pulled")
    lines.append("")
    lines.append(f"- Buildings: {report['pages']['buildings']}")
    lines.append(f"- Roads: {report['pages']['roads']}")
    lines.append("")

    lines.append("## Counts")
    lines.append("")
    lines.append("Buildings, ours by source:")
    ours_by_source = report["counts"]["buildings"]["ours"]
    for source in sorted(ours_by_source):
        lines.append(f"- {source}: {ours_by_source[source]}")
    lines.append(f"- NGD buildingpart: {report['counts']['buildings']['ngd']}")
    lines.append("")
    road_counts = report["counts"]["roads"]
    lines.append("Roads:")
    lines.append(f"- ours, OSM highway=*: {road_counts['ours_osm']}")
    lines.append(f"- ours, OS Open roads: {road_counts['ours_os_open']}")
    lines.append(f"- NGD roadlink: {road_counts['ngd']}")
    lines.append("")

    lines.append("## Building footprint matching")
    lines.append("")
    buildings = report["buildings"]
    lines.append(f"- Matched: {buildings['matched']}")
    lines.append(f"- Matched fraction of ours: {buildings['matched_fraction_ours']:.3f}")
    lines.append(f"- Matched fraction of NGD: {buildings['matched_fraction_theirs']:.3f}")
    iou = buildings["iou"]
    lines.append(f"- IoU p10 / p50 / p90: {iou['p10']:.3f} / {iou['p50']:.3f} / {iou['p90']:.3f}")
    lines.append(f"- Unmatched ours: {buildings['unmatched_ours']}")
    lines.append(f"- Unmatched NGD: {buildings['unmatched_theirs']}")
    lines.append("")

    lines.append("## Road offsets and the epoch question")
    lines.append("")
    lines.append(
        "The OSM population carries the epoch question (OSM/Overture geometry "
        "sits in current-epoch WGS84 while our own LiDAR/contour/boundary "
        "geometry sits in ETRS89(1989.0); see "
        "docs/superpowers/specs/2026-08-06-epoch-shift-note.md). The OS Open "
        "population is BNG-native already, so its own offset against NGD "
        "measures generalisation between two OS products, not epoch, and is "
        "printed only as a control."
    )
    lines.append("")
    for label, key in (("OSM", "osm"), ("OS Open (control)", "os_open")):
        stats = report["roads"][key]
        lines.append(f"### {label}")
        lines.append("")
        lines.append(
            f"- Samples matched: {stats['count']} (unmatched: {stats['unmatched_samples']})"
        )
        lines.append(
            f"- Naive mean offset (the raw perpendicular-projection statistic; "
            f"it understates a true systematic shift on any street population "
            f"that is not all one orientation, since a straight segment's "
            f"nearest-point projection only ever reveals the shift component "
            f"perpendicular to it): east {stats['mean_de']:.3f} m, "
            f"north {stats['mean_dn']:.3f} m, magnitude {stats['magnitude_of_mean']:.3f} m"
        )
        if stats["lsq_de"] is None:
            lines.append(
                "- Least-squares offset (the primary estimate): undefined; the "
                "sampled street population is too close to one orientation to "
                "separate the two shift components"
            )
        else:
            lines.append(
                f"- Least-squares offset (the primary estimate, corrected for "
                f"the perpendicular-only bias above): east {stats['lsq_de']:.3f} m, "
                f"north {stats['lsq_dn']:.3f} m, magnitude {stats['lsq_magnitude']:.3f} m"
            )
        lines.append(f"- Std: east {stats['std_de']:.3f} m, north {stats['std_dn']:.3f} m")
        lines.append(
            f"- Per-sample offset magnitude p50 / p90: {stats['p50_abs']:.3f} / "
            f"{stats['p90_abs']:.3f} m"
        )
        lines.append("")
    lines.append(f"Epoch verdict: {report['epoch_verdict']}")
    lines.append("")

    lines.append("## Class names")
    lines.append("")
    lines.append("NGD `description` values over this pull (names and counts only):")
    ngd_classes = report["classes"]["ngd_only"]
    if ngd_classes:
        for name in sorted(ngd_classes):
            lines.append(f"- {name}: {ngd_classes[name]}")
    else:
        lines.append("- (none carried a description value)")
    lines.append("")
    lines.append("Our own `building=*` tag values:")
    our_classes = report["classes"]["counts"]
    if our_classes:
        for name in sorted(our_classes):
            lines.append(f"- {name}: {our_classes[name]}")
    else:
        lines.append("- (no buildings in this package)")
    lines.append("")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# run_benchmark
# --------------------------------------------------------------------------


def run_benchmark(
    package_dir: Path,
    key: str,
    out_root: Path = Path("benchmarks"),
    *,
    client_factory: Callable[[str], object] = NgdClient,
) -> tuple[Path, Path]:
    """Read `package_dir`, pull OS NGD data over its own bbox, compare,
    and write `report.md` + `report.json` under `out_root`, returning
    both paths.

    `client_factory(key)` builds the client this call uses, ordinarily
    `NgdClient` (a real client, a real key, a real network call); a test
    hands in something else shaped the same way (`verify_collections`,
    `items`) to prove the firewall and the report's own shape without
    ever touching the network. This is additional to the brief's own
    three positional parameters, not a replacement for any of them: the
    CLI never passes it, so `mapgen benchmark` always gets a real client.

    Writes happen ONLY at the very end, after every read and every NGD
    call has already succeeded, and only via the project's own atomic
    writers (`fsutil.atomic_write_text`/`atomic_write_bytes`): an
    `NgdError` raised by `client_factory(key).items(...)` propagates
    straight out of this function with no directory created and no file
    written under `out_root` at all, and `package_dir` is never written
    to under any outcome, success or failure alike (see the module
    docstring's "the firewall, made concrete here").
    """
    package_dir = Path(package_dir)
    stem, bbox = _read_survey(package_dir)

    osm_path = package_dir / f"{stem}.osm"
    if not osm_path.is_file():
        raise BenchmarkError(f"{osm_path} is not in this package; nothing to benchmark.")
    root = _read_osm(osm_path)
    nodes = _osm_nodes(root)

    grid = load_ostn15()
    if grid is None:
        grid = ensure_ostn15()

    ours_building_rings, building_counts_by_source, building_tag_counts = _read_buildings(
        root, nodes, grid
    )
    ours_osm_roads = _read_osm_roads(root, nodes, grid)
    ours_os_open_roads = _read_os_open_roads(package_dir / f"{stem}_os_roads.geojson", grid)

    bbox_bng = padded_bng_extent(bbox, grid, 0.0)

    client = client_factory(key)
    client.verify_collections([BUILDING_COLLECTION, ROAD_COLLECTION])
    ngd_building_features, building_pages = client.items(BUILDING_COLLECTION, bbox_bng)
    ngd_road_features, road_pages = client.items(ROAD_COLLECTION, bbox_bng)

    ngd_building_rings = _ngd_building_rings(ngd_building_features)
    ngd_road_polylines = _ngd_road_polylines(ngd_road_features)
    ngd_description_counts = _ngd_description_counts(ngd_building_features)

    match = match_footprints(ours_building_rings, ngd_building_rings)
    ious = [iou for _, _, iou in match.matched]

    offsets_osm = polyline_offsets(ours_osm_roads, ngd_road_polylines)
    offsets_os_open = polyline_offsets(ours_os_open_roads, ngd_road_polylines)

    epoch_verdict = _epoch_verdict(offsets_osm)
    pulled_date = date.today()

    report = {
        "source": "OS NGD (dev-mode evaluation key)",
        "pulled": pulled_date.isoformat(),
        "package": stem,
        "bbox": [bbox.west, bbox.south, bbox.east, bbox.north],
        "pages": {"buildings": building_pages, "roads": road_pages},
        "counts": {
            "buildings": {
                "ours": building_counts_by_source,
                "ngd": len(ngd_building_rings),
            },
            "roads": {
                "ours_osm": len(ours_osm_roads),
                "ours_os_open": len(ours_os_open_roads),
                "ngd": len(ngd_road_polylines),
            },
        },
        "buildings": {
            "matched": len(match.matched),
            "matched_fraction_ours": (
                len(match.matched) / len(ours_building_rings) if ours_building_rings else 0.0
            ),
            "matched_fraction_theirs": (
                len(match.matched) / len(ngd_building_rings) if ngd_building_rings else 0.0
            ),
            "iou": _iou_percentiles(ious),
            "unmatched_ours": len(match.unmatched_ours),
            "unmatched_theirs": len(match.unmatched_theirs),
        },
        "roads": {
            "osm": _offset_stats_dict(offsets_osm),
            "os_open": _offset_stats_dict(offsets_os_open),
        },
        "classes": {
            "ngd_only": ngd_description_counts,
            "counts": building_tag_counts,
        },
        "epoch_verdict": epoch_verdict,
    }

    md_text = _render_markdown(report)
    json_text = json.dumps(report, indent=2)

    out_dir = Path(out_root) / f"{stem}_{pulled_date.isoformat()}"
    md_path = out_dir / "report.md"
    json_path = out_dir / "report.json"
    atomic_write_text(md_path, md_text)
    atomic_write_bytes(json_path, json_text.encode("utf-8"))
    return md_path, json_path
