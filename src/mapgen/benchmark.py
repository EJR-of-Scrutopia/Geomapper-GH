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
every `building=*` way in `<stem>.osm`, the FUSED file: Overture and OS
OpenMap Local injections (`mapgen.buildings.fuse_missing_buildings`) each
carry a `source` tag of one of two EXACT, known values
(`package.BUILDINGS_SOURCE_OVERTURE`/`BUILDINGS_SOURCE_OS_OPEN`); every
other building way, tagged or not, is counted `"osm"`. This is NOT the
same as "carries no `source` tag at all" (task-3-review.md's own
Important finding 1, an executed demonstration): ordinary upstream OSM
data commonly carries its own unrelated `source=*` provenance tag
(`source=Bing`, `source=survey`, bulk-import tags), which
`merge_osm_xml` preserves verbatim with no stripping step anywhere in the
OSM source pipeline, so bucketing on presence/absence alone would
misreport a perfectly ordinary OSM-native building as an Overture or
OS-OpenMap-Local injection that never happened. Bucketing on the two
known values instead is correct regardless of what else a real OSM
extract's own `source` tag ever says.

Roads are every `highway=*` way in the same file (OSM-derived, so subject
to the epoch question), SPLIT into carriageways and paths (see
"carriageways are not paths" below), PLUS, when
`<stem>_os_roads.geojson` exists (OS OpenMap Local's own road layer,
`mapgen.sources.os_open`), its LineStrings as a SEPARATE population
labelled `os_open`: that file is written in WGS84
(`OsOpenSource.merge`'s own `_reproject_geometry`, `from_bng`) but its
underlying survey is BNG-native already, so its own offset against NGD
measures generalisation between two OS products, not an epoch gap, and
the report says so.

Every one of our own coordinates reaches BNG the same way `heights.py`'s
`_fuse_heights_step` does: `to_bng` per node, `load_ostn15()` cache-only
first, `ensure_ostn15()` only if that misses (this module's own only
network call apart from the NGD pull itself).

## Comparing over the same ground

A package covers MORE ground than the rectangle the NGD pull is made
over: the survey pads its own extent, and the OS OpenMap Local
footprints `buildings.fuse_missing_buildings` injects arrive over a
wider footprint again. Our own building population is therefore CLIPPED
to the exact bbox sent to the API (`_split_by_extent`) before anything
is matched, using the same guaranteed-interior representative point the
containment test already reduces a footprint to. A footprint outside
that rectangle can neither pair with an NGD feature nor stand inside
one, so leaving it in put it in "absent from OS" by construction, which
reads as a finding about OS's coverage when it is really a statement
about which ground was asked about. Measured on the Cowbridge benchmark
package: 367 of 1755 footprints of ours fell outside, 346 of them from
the single OS OpenMap Local injection layer, and both the "absent from
OS" count and the matched fraction of ours were wrong because of it.
The clipping is never silent: the report prints the excluded count per
source layer beside the kept one. Their side needs no equivalent, since
it arrived from the query itself.

## Carriageways are not paths

NGD's roadlink collection holds carriageway centrelines and nothing
else. A pavement, a field path, a bridleway or a flight of steps has no
counterpart in it at all, so an offset measured from one is the width of
the street, not a disagreement between two surveys, and its sign flips
with which side of the street the pavement runs down. Taking every
`highway=*` way as one population therefore fed the least-squares
estimate a large block of samples that cancel each other and pull it
toward zero: on the Cowbridge package the path family was 34.6 percent
of the sampled length (12,886.0 m of 37,198.9 m), measured as a share of
summed way length in metres, NOT as a share of the sample COUNT (a
different measure that answers a different question).
`_read_osm_roads` splits the population by
`categories.road_family` (that module's own vocabulary, read rather than
re-typed), both legs are reported, and THE EPOCH VERDICT IS DERIVED FROM
THE CARRIAGEWAY LEG ALONE. This deliberately breaks continuity with the
single-population OSM offset earlier runs published.

The identical disease survives inside the carriageway leg itself, tagged
`highway=service` rather than `highway=footway`: a parking aisle
(`service=parking_aisle`) or a private driveway (`service=driveway`) has
no NGD roadlink counterpart either, so it too finds the nearest real
carriageway and contributes a width-of-the-car-park vector rather than a
survey disagreement. Measured on the Cowbridge package: 106 of 230
carriageway-family ways are `highway=service`, and 49 of those (31
`parking_aisle`, 18 `driveway`) carry one of these two values.
`_read_osm_roads` excludes both before `road_family` is even consulted,
counting them separately (`OsmRoadPopulations.excluded_service_counts`)
rather than dropping them in silence; an ordinary `highway=service` way
with no such tag, or any other `service` value, is a real carriageway and
is unaffected.

## Explaining the unmatched, not just counting them

A raw unmatched count is the one number in this report that cannot be
read at face value. NGD counts building PARTS, so a terrace this project
holds as one footprint arrives from OS as several parts, and every part
after the one that wins the greedy pairing lands in "unmatched theirs"
even though the ground itself is covered on both sides. Two additions
separate a difference about where the LINES fall from a real gap in
coverage, both aggregate, both firewall-safe:

  * CONTAINMENT (`benchstats.classify_containment`). Every unmatched
    footprint on one side is reduced to a single guaranteed-interior
    point (`buildings.representative_point`, the scanline label point,
    never a vertex average that a concave footprint puts outside itself)
    and tested against the other side's footprints. One of theirs
    standing inside one of ours is a SUBDIVISION; one standing on ground
    we hold nothing on is ABSENT, the genuine gap. Mirrored the other
    way for ours: `SPURIOUS_OR_NEWER` and `ABSENT_FROM_OS` (see the four
    constants' own comment for what each does and does not prove). One
    further aggregate integer earns the SUBDIVISION label rather than
    assuming it: how many of those cases have their part LARGER than the
    footprint of ours containing it, which is the reversed reading (our
    polygon drawn oversized) the point test alone cannot separate out.
  * SIZE (`benchstats.ring_area`, `area_histogram`). Every footprint,
    matched and unmatched, is bucketed by shoelace area in square metres,
    and the unmatched buckets are cross-tabulated against the
    containment classes above. The architectural question a bare count
    cannot answer is whether a gap is bin stores and sheds or dwellings.

Only bucket COUNTS and class COUNTS reach the report from either, never a
per-feature area, never which feature fell where.

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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

from mapgen.benchstats import (
    AREA_BUCKET_LABELS,
    OffsetStats,
    area_histogram,
    classify_containment,
    distribution,
    match_footprints,
    polyline_offsets,
    ring_area,
)
from mapgen.bng import BngError, Ostn15Grid, ensure_ostn15, load_ostn15, padded_bng_extent, to_bng
from mapgen.buildings import representative_point
from mapgen.categories import ROAD_FAMILY_CARRIAGEWAY, ROAD_FAMILY_PATH, road_family
from mapgen.fsutil import atomic_write_bytes, atomic_write_text
from mapgen.geo import BBox
from mapgen.ngd import BUILDING_COLLECTION, ROAD_COLLECTION, NgdClient
from mapgen.package import BUILDINGS_SOURCE_OS_OPEN, BUILDINGS_SOURCE_OVERTURE

_BUILDING_TAG_KEY = "building"
_HIGHWAY_TAG_KEY = "highway"
_SOURCE_TAG_KEY = "source"
_SERVICE_TAG_KEY = "service"

# The two `service=*` sub-values that mark a `highway=service` way as NOT
# a real carriageway at all (see `_read_osm_roads`'s own docstring): a
# parking aisle or a private driveway has no NGD roadlink counterpart any
# more than a pavement does, so left in the carriageway population it
# contributes a vector pointing at the nearest real road rather than a
# survey disagreement, exactly the contamination the carriageway/path
# split already removes for footways. Every other `service` value (an
# ordinary access road, an alley, no tag at all) is a real carriageway
# and stays.
_EXCLUDED_SERVICE_VALUES = frozenset(["parking_aisle", "driveway"])

# The bucket every building way falls into UNLESS its own `source` tag is
# one of the two EXACT values `buildings.fuse_missing_buildings` actually
# writes (`_KNOWN_INJECTION_SOURCES` below), imported from `package.py`
# rather than re-typed here so the two can never drift apart
# (task-3-review.md's own Important finding 1: presence/absence of ANY
# `source` tag is not the same test, since real upstream OSM data can and
# does carry its own unrelated `source=*` provenance tag; see the module
# docstring).
OSM_SOURCE_LABEL = "osm"

_KNOWN_INJECTION_SOURCES = {BUILDINGS_SOURCE_OVERTURE, BUILDINGS_SOURCE_OS_OPEN}

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

# The four names this report gives `benchstats.classify_containment`'s own
# deliberately neutral `contained`/`not_contained` split (that dataclass's
# own docstring: the reading belongs to whoever knows which dataset is
# which). Read them as questions already answered:
#
#   SUBDIVISION      one of theirs that matched nothing of ours, but whose
#                    own interior point stands inside a footprint we DO
#                    hold. The ground is covered on both sides; what
#                    differs is where the LINES fall. NGD counts building
#                    PARTS, so a terrace we carry as one footprint arrives
#                    from them as several, and every part after the one
#                    that won the greedy pairing lands in the unmatched
#                    pile. Not a gap in coverage, and not merely a
#                    difference in counting either: each one is a
#                    boundary OS records and we do not, a party wall, a
#                    house-to-garage join, a terrace division, every one
#                    of them a line an architect draws at 1:500. The
#                    subdivision count is reported beside a second count
#                    (how many of them are LARGER than the footprint of
#                    ours containing them) because the point test on its
#                    own cannot tell this reading from its reverse, our
#                    own polygon drawn oversized.
#   ABSENT           one of theirs that matched nothing of ours and stands
#                    on ground where we hold nothing at all. This is the
#                    real gap, the only one of the four worth acting on.
#   SPURIOUS_OR_NEWER one of ours that matched nothing of theirs but stands
#                    inside a footprint they DO hold: we are splitting or
#                    duplicating something they carry whole, or our own
#                    footprint sits far enough off theirs for the sampled
#                    IoU to refuse the pair while the point still lands
#                    inside. Three readings, and this test cannot separate
#                    them, so the label says so rather than picking one.
#   ABSENT_FROM_OS   one of ours on ground THEY hold nothing on: new
#                    construction since their survey, a demolition they
#                    have not caught, or an error of ours. Again three
#                    readings and no way to choose between them from
#                    geometry alone.
CONTAINMENT_SUBDIVISION = "subdivision"
CONTAINMENT_ABSENT = "absent"
CONTAINMENT_SPURIOUS_OR_NEWER = "spurious_or_newer"
CONTAINMENT_ABSENT_FROM_OS = "absent_from_os"


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
) -> tuple[list[list[tuple[float, float]]], list[str], dict[str, int], dict[str, int]]:
    """Every `building=*` way in `root`, projected to BNG.

    Returns `(rings, ring_sources, counts_by_source, tag_value_counts)`.
    `rings` is the population that (once clipped to the NGD query
    rectangle, `_split_by_extent`) `match_footprints` runs against;
    `ring_sources` is the source label of each of those rings, at the
    same position, so a count of what the clipping excluded can be broken
    down by layer rather than reported as one opaque total.
    `counts_by_source` is how many buildings carry each of the two KNOWN
    injection `source` values (`_KNOWN_INJECTION_SOURCES`), with
    everything else, tagged or not, counted `OSM_SOURCE_LABEL`
    (task-3-review.md's own Important finding 1: an unrelated upstream
    `source=*` tag, `source=Bing` for instance, must never be counted as
    an Overture/OS-OpenMap-Local injection that never happened);
    `tag_value_counts` is how many carry each `building=<value>` tag
    value, the report's own comparison against NGD's `description`
    frequency table. The latter two are counted for every building=* way
    regardless of whether its own footprint could be projected, since
    they describe what this package's own tagging looks like, not what
    could be compared: an unprojectable footprint is a fact about that
    one way, not a reason to hide it from a tag count.
    """
    rings: list[list[tuple[float, float]]] = []
    ring_sources: list[str] = []
    counts_by_source: dict[str, int] = {}
    tag_value_counts: dict[str, int] = {}
    for element in root:
        if element.tag != "way":
            continue
        tags = _way_tags(element)
        if _BUILDING_TAG_KEY not in tags:
            continue

        source_tag = tags.get(_SOURCE_TAG_KEY)
        source = source_tag if source_tag in _KNOWN_INJECTION_SOURCES else OSM_SOURCE_LABEL
        counts_by_source[source] = counts_by_source.get(source, 0) + 1
        tag_value = tags[_BUILDING_TAG_KEY]
        tag_value_counts[tag_value] = tag_value_counts.get(tag_value, 0) + 1

        latlon = _way_latlon(element, nodes)
        if latlon is None or len(latlon) < 3:
            continue
        ring = _project(latlon, grid)
        if ring is not None:
            rings.append(ring)
            ring_sources.append(source)
    return rings, ring_sources, counts_by_source, tag_value_counts


# --------------------------------------------------------------------------
# Clipping our own population to the rectangle the NGD pull actually
# covered (see the module docstring's "comparing over the same ground").
# --------------------------------------------------------------------------


def _in_extent(ring: Sequence[tuple[float, float]], extent: tuple[float, float, float, float]) -> bool:
    """Whether `ring`'s own guaranteed-interior representative point falls
    inside the BNG rectangle `extent` (`e_min, n_min, e_max, n_max`).

    The interior point, not a bbox overlap and not "every vertex inside":
    it is the same point `benchstats.classify_containment` already reduces
    a footprint to, so a footprint counted in the compared population and
    a footprint tested for containment are the same footprint by the same
    rule, and the two sections of this report cannot disagree about which
    footprints exist. It is also already computed for the unmatched ones,
    so this costs nothing new for them.

    Inclusive on all four edges: a footprint whose point lands exactly on
    the boundary is in, which is the same closed-interval convention
    `benchstats._bbox_holds` uses.
    """
    e_min, n_min, e_max, n_max = extent
    point_e, point_n = representative_point(ring)
    return e_min <= point_e <= e_max and n_min <= point_n <= n_max


def _split_by_extent(
    rings: Sequence[Sequence[tuple[float, float]]],
    sources: Sequence[str],
    extent: tuple[float, float, float, float],
) -> tuple[list[list[tuple[float, float]]], dict[str, int], dict[str, int]]:
    """`rings` split into the ones inside `extent` and a count of the ones
    outside it, both broken down by source layer.

    Returns `(in_extent_rings, in_extent_by_source, out_of_extent_by_source)`.
    Nothing is thrown away silently: the second and third dicts add back
    to the population handed in, per layer, and the report prints both.
    """
    kept: list[list[tuple[float, float]]] = []
    inside: dict[str, int] = {}
    outside: dict[str, int] = {}
    for ring, source in zip(rings, sources):
        if _in_extent(ring, extent):
            kept.append(list(ring))
            inside[source] = inside.get(source, 0) + 1
        else:
            outside[source] = outside.get(source, 0) + 1
    return kept, inside, outside


@dataclass
class OsmRoadPopulations:
    """`_read_osm_roads`'s own answer: the package's `highway=*` ways
    projected to BNG and split by what they physically are.

    `carriageway` is every way a vehicle drives on
    (`categories.CARRIAGEWAY_HIGHWAY_VALUES`) MINUS the parking-aisle and
    driveway exclusion below; `path` is every way a person walks, climbs
    or cycles (`categories.PATH_HIGHWAY_VALUES`); `other_value_counts` is
    how many ways carried a `highway` value in neither family, by value,
    so the ones this split leaves out of both legs are named and counted
    rather than dropped in silence. `excluded_service_counts` is how many
    `highway=service` ways were pulled back OUT of `carriageway` because
    their own `service` tag named one of `_EXCLUDED_SERVICE_VALUES`, keyed
    by that value, for the identical reason (see `_read_osm_roads`'s own
    docstring): a plain `highway=service` way with no such tag, or one
    carrying any other `service` value, is a real carriageway and stays.
    """

    carriageway: list[list[tuple[float, float]]]
    path: list[list[tuple[float, float]]]
    other_value_counts: dict[str, int]
    excluded_service_counts: dict[str, int]


def _read_osm_roads(
    root: ET.Element, nodes: dict[str, tuple[float, float]], grid: Ostn15Grid
) -> OsmRoadPopulations:
    """Every `highway=*` way in `root`, projected to BNG and SPLIT by
    `categories.road_family` into carriageways and paths.

    One population was wrong here, and wrong in a direction that
    flattered the answer (see the module docstring's "carriageways are
    not paths"): NGD's roadlink collection holds carriageway centrelines
    only, so a pavement or a field path has no counterpart in it at all,
    yet each one still finds the nearest carriageway inside the offset
    sampler's own search radius and contributes an offset vector pointing
    at it. Those vectors are the width of the street, not a survey
    disagreement, and their sign flips with which side of the street the
    pavement is on, so they cancel and pull the least-squares estimate
    toward zero.

    A second population carries the identical disease while still being
    tagged `highway=service`, which `road_family` reads as an ordinary
    carriageway value: a parking aisle (`service=parking_aisle`) or a
    private driveway (`service=driveway`) has no NGD roadlink counterpart
    any more than a pavement does, so it too finds the nearest real
    carriageway inside the search radius and contributes a vector
    pointing at it rather than a survey disagreement. Measured on the
    Cowbridge benchmark package: 106 of 230 carriageway-family ways are
    `highway=service`, and 49 of those (31 `parking_aisle`, 18
    `driveway`) carry one of these two values. Both are excluded from
    `carriageway` here, before `road_family` is even consulted, and
    counted separately in `excluded_service_counts` rather than dropped
    in silence; every other `highway=service` way (no `service` tag at
    all, or any value other than these two) is a real carriageway and is
    unaffected.

    A way whose `highway` value is in neither family (`road_family`
    answering None: highway=construction, highway=pedestrian and the
    rest) joins neither leg and is counted in `other_value_counts`.
    """
    carriageway: list[list[tuple[float, float]]] = []
    path: list[list[tuple[float, float]]] = []
    other_value_counts: dict[str, int] = {}
    excluded_service_counts: dict[str, int] = {}
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
        if polyline is None:
            continue
        value = tags[_HIGHWAY_TAG_KEY]

        service_value = tags.get(_SERVICE_TAG_KEY)
        if value == "service" and service_value in _EXCLUDED_SERVICE_VALUES:
            excluded_service_counts[service_value] = (
                excluded_service_counts.get(service_value, 0) + 1
            )
            continue

        family = road_family(value)
        if family == ROAD_FAMILY_CARRIAGEWAY:
            carriageway.append(polyline)
        elif family == ROAD_FAMILY_PATH:
            path.append(polyline)
        else:
            other_value_counts[value] = other_value_counts.get(value, 0) + 1
    return OsmRoadPopulations(
        carriageway=carriageway,
        path=path,
        other_value_counts=other_value_counts,
        excluded_service_counts=excluded_service_counts,
    )


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


def _exterior_rings(geometry: dict) -> list[list]:
    """Every constituent polygon's own OUTER ring in `geometry`'s own raw
    coordinate arrays: one for a `Polygon`, one PER constituent polygon
    for a `MultiPolygon` (task-3-review.md's own Minor finding 2: reading
    only `coordinates[0]`'s own first polygon silently dropped every
    other part of a genuine multi-part NGD building, with no count or
    signal that anything was left out). Interior (hole) rings are never
    read here, matching `match_footprints`'s own exterior-only ring
    convention throughout this module.

    `[]`, not a guess, for any shape this cannot make sense of: an
    unrecognised geometry type, or a `coordinates` array that is not
    shaped the way GeoJSON's own spec says `Polygon`/`MultiPolygon`
    should be.
    """
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        return []
    if geometry_type == "Polygon":
        return [coordinates[0]]
    if geometry_type == "MultiPolygon":
        return [
            polygon[0]
            for polygon in coordinates
            if isinstance(polygon, list) and polygon
        ]
    return []


def _ngd_building_rings(features: Sequence[dict]) -> list[list[tuple[float, float]]]:
    """Every buildingpart feature's own exterior ring(s), already BNG (the
    `crs`/`bbox-crs` the client requested; see `ngd.py`'s own docstring):
    no projection here, unlike every one of our own populations above.

    A `MultiPolygon` feature contributes one ring per constituent
    polygon, not only its first (`_exterior_rings`): every part of a
    genuine multi-part building enters `match_footprints`, none silently
    dropped.
    """
    rings: list[list[tuple[float, float]]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        for exterior in _exterior_rings(geometry):
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

    `osm_offsets` is the CARRIAGEWAY leg alone, never the whole
    `highway=*` population (see `_read_osm_roads` and the module
    docstring): a path measured against NGD's carriageway-only roadlink
    collection contributes the width of the street, not a survey
    disagreement, and enough of them cancel the estimate toward zero.
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

# The size table's own column headings, in `AREA_BUCKET_LABELS` order:
# the label a machine reads (`under_10`) is not the label a person
# reads over a column of a markdown table, and the report is written for
# a person first.
_BUCKET_HEADINGS = {
    "under_10": "<10",
    "10_to_30": "10-30",
    "30_to_80": "30-80",
    "80_to_200": "80-200",
    "200_to_1000": "200-1000",
    "over_1000": ">1000",
}


def _containment_lines(report: dict) -> list[str]:
    """The "what the unmatched footprints are standing on" section: the
    one part of this report that turns an unmatched COUNT into an
    answerable question (see the module docstring's own "explaining the
    unmatched" section).
    """
    theirs = report["containment"]["theirs_unmatched"]
    ours = report["containment"]["ours_unmatched"]
    larger = report["containment"]["theirs_subdivision_larger_than_ours"]
    lines: list[str] = []
    lines.append("## What the unmatched footprints are standing on")
    lines.append("")
    lines.append(
        "NGD counts building PARTS, so a terrace held here as one footprint "
        "arrives from OS as several, and every part after the one that won "
        "the pairing lands in the unmatched column while the ground itself "
        "is still covered. Each unmatched footprint below is reduced to one "
        "guaranteed-interior point and tested against the other dataset's "
        "own footprints: standing inside one of them means the two datasets "
        "disagree about where the lines fall, standing on ground the other "
        "dataset holds nothing on means a real gap."
    )
    lines.append("")
    lines.append(f"NGD footprints that matched nothing of ours ({sum(theirs.values())}):")
    lines.append(
        f"- Subdivision (stands inside a footprint we hold): "
        f"{theirs[CONTAINMENT_SUBDIVISION]}"
    )
    lines.append(
        f"- of those, NGD part LARGER than the footprint of ours containing it: {larger}"
    )
    lines.append(
        "  The containment test asks only whether their interior point lands "
        "inside one of our rings; it never compares areas, so on its own it "
        "cannot tell \"OS split a building we hold whole\" from \"our polygon "
        "is drawn oversized and swallowed part of theirs\". Where their part "
        "is the larger of the two, the reversed reading is the live one and "
        "the fault is ours, not a difference in counting. That is what this "
        "one number bounds."
    )
    lines.append(
        f"- Absent (we hold nothing on that ground): {theirs[CONTAINMENT_ABSENT]}"
    )
    lines.append("")
    lines.append(f"Our footprints that matched nothing of theirs ({sum(ours.values())}):")
    lines.append(
        f"- Spurious or newer (stands inside a footprint they hold, so we are "
        f"splitting, duplicating or offsetting something they carry): "
        f"{ours[CONTAINMENT_SPURIOUS_OR_NEWER]}"
    )
    lines.append(
        f"- Absent from OS (they hold nothing on that ground: new build, a "
        f"demolition they have not caught, or an error of ours): "
        f"{ours[CONTAINMENT_ABSENT_FROM_OS]}"
    )
    lines.append("")
    return lines


def _size_table(rows: Sequence[tuple[str, dict]], buckets: Sequence[str]) -> list[str]:
    """A markdown table of `rows` (each a label and its own
    `area_histogram` dict) against `buckets`, with a total column so a
    reader can check any row against the counts above it rather than
    adding six numbers by hand.
    """
    header = " | ".join(_BUCKET_HEADINGS.get(bucket, bucket) for bucket in buckets)
    lines = [f"| Population | {header} | total |", "| --- |" + " --- |" * (len(buckets) + 1)]
    for label, histogram in rows:
        cells = " | ".join(str(histogram[bucket]) for bucket in buckets)
        lines.append(f"| {label} | {cells} | {sum(histogram.values())} |")
    return lines


def _size_lines(report: dict) -> list[str]:
    """The footprint-area section: every population bucketed by square
    metres, and the unmatched ones cross-tabulated against the
    containment classes above.
    """
    size = report["size"]
    buckets = size["buckets"]
    lines: list[str] = []
    lines.append("## Footprint size, square metres")
    lines.append("")
    lines.append(
        "Planar shoelace area on the BNG rings, bucketed. Under 10 m2 is a "
        "bin store or a garden shed; 10 to 30 m2 a garage; 30 to 80 m2 a "
        "small dwelling or a terrace part; 80 to 200 m2 an ordinary house; "
        "above that, large houses and commercial or institutional buildings. "
        "The question this answers is whether a gap is sheds, which do not "
        "matter to a site survey, or dwellings, which do."
    )
    lines.append("")
    lines.extend(
        _size_table(
            [
                ("ours, matched", size["ours_matched"]),
                ("ours, unmatched", size["ours_unmatched"]),
                ("NGD, matched", size["theirs_matched"]),
                ("NGD, unmatched", size["theirs_unmatched"]),
            ],
            buckets,
        )
    )
    lines.append("")
    lines.append("Unmatched, split by what each one is standing on:")
    lines.append("")
    lines.extend(
        _size_table(
            [
                (
                    "NGD, subdivision",
                    size["theirs_unmatched_by_class"][CONTAINMENT_SUBDIVISION],
                ),
                ("NGD, absent", size["theirs_unmatched_by_class"][CONTAINMENT_ABSENT]),
                (
                    "ours, spurious or newer",
                    size["ours_unmatched_by_class"][CONTAINMENT_SPURIOUS_OR_NEWER],
                ),
                (
                    "ours, absent from OS",
                    size["ours_unmatched_by_class"][CONTAINMENT_ABSENT_FROM_OS],
                ),
            ],
            buckets,
        )
    )
    lines.append("")
    return lines


def _epoch_evidence_lines(report: dict) -> list[str]:
    """The evidence the epoch verdict is read from, stated as numbers
    rather than left implicit in the verdict sentence alone: the
    carriageway vector, the OS Open control vector beside it, how closely
    the two agree, what is left of the carriageway vector once that
    shared component is subtracted out, and how far the measured bearing
    sits from the epoch-shift hypothesis's own north-east bearing.

    The control population (`os_open`, see the module docstring's own "OS
    Open" section) is generalisation between two OS products already
    reprojected through the same OSTN15 grid this project's own
    coordinates travel through; it is BNG-native at survey time and
    cannot carry an OSM/WGS84 epoch component AT ALL, by construction.
    So whatever least-squares vector it shows is necessarily an artefact
    shared by both populations alike (of generalisation, of the road
    sampler, of OSTN15 itself), not an epoch signal: subtracting the
    control vector from the carriageway vector isolates whatever is left
    over that is specific to OSM's own coordinates rather than common to
    both.

    Nothing here is fabricated for a run where either leg's least-squares
    estimate is undefined: the comparison is simply not drawn, and this
    function says so rather than dividing by an absent vector.
    """
    carriageway = report["roads"]["osm_carriageway"]
    control = report["roads"]["os_open"]
    lines: list[str] = []
    lines.append("## Epoch evidence: the carriageway vector against its own control")
    lines.append("")
    if carriageway["lsq_de"] is None or control["lsq_de"] is None:
        lines.append(
            "One of the two least-squares vectors above is undefined this "
            "run (too few interior samples, or a population too close to "
            "one orientation to separate the two shift components), so the "
            "agreement-and-residual comparison below cannot be drawn."
        )
        lines.append("")
        return lines

    c_de, c_dn, c_mag = carriageway["lsq_de"], carriageway["lsq_dn"], carriageway["lsq_magnitude"]
    o_de, o_dn, o_mag = control["lsq_de"], control["lsq_dn"], control["lsq_magnitude"]
    c_bearing = _bearing_degrees(c_de, c_dn)
    o_bearing = _bearing_degrees(o_de, o_dn)
    magnitude_agreement = abs(c_mag - o_mag)
    bearing_agreement = _angular_difference(c_bearing, o_bearing)

    residual_de, residual_dn = c_de - o_de, c_dn - o_dn
    residual_magnitude = math.hypot(residual_de, residual_dn)
    hypothesis_gap = _angular_difference(c_bearing, _NORTHEAST_BEARING_DEG)

    lines.append(
        f"- OSM carriageway vector: {c_mag:.3f} m at bearing {c_bearing:.1f} degrees."
    )
    lines.append(
        f"- OS Open control vector (BNG-native; cannot carry an epoch "
        f"shift by construction): {o_mag:.3f} m at bearing {o_bearing:.1f} "
        f"degrees."
    )
    lines.append(
        f"- The two agree to {magnitude_agreement:.3f} m in magnitude and "
        f"{bearing_agreement:.0f} degrees in bearing. A vector this close "
        f"to the datum-shift-immune control is an OS product artefact "
        f"common to both populations, not an OSM-specific signal."
    )
    if residual_magnitude > 0.0:
        residual_bearing = _bearing_degrees(residual_de, residual_dn)
        lines.append(
            f"- OSM-specific residual, the carriageway vector with the "
            f"control vector subtracted out: {residual_magnitude:.3f} m at "
            f"bearing {residual_bearing:.1f} degrees."
        )
    else:
        lines.append(
            "- OSM-specific residual, the carriageway vector with the "
            "control vector subtracted out: 0.000 m (the two vectors are "
            "identical this run)."
        )
    lines.append(
        f"- The epoch-shift hypothesis calls for roughly 0.9 m at a "
        f"north-east bearing (around {_NORTHEAST_BEARING_DEG:.0f} degrees); "
        f"the measured carriageway bearing sits {hypothesis_gap:.0f} "
        f"degrees away from that, on top of whatever gap already exists in "
        f"magnitude."
    )
    lines.append("")
    return lines


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
    building_counts = report["counts"]["buildings"]
    ours_by_source = building_counts["ours"]
    out_by_source = building_counts["ours_out_of_extent"]
    all_by_source = building_counts["ours_all"]
    lines.append(
        "The BUILDING counts below are CLIPPED to the rectangle the NGD pull "
        "was made over; the ROAD counts further down are NOT. The package "
        "covers more ground than the survey bbox: the survey pads its own "
        "extent, and the OS OpenMap Local footprints injected into it arrive "
        "over a wider footprint again. A footprint outside that rectangle "
        "cannot match an NGD feature and cannot stand inside one, so leaving "
        "it in the compared population would count it as a building OS does "
        "not hold when OS was never asked about that ground. A footprint is "
        "in when its own guaranteed-interior point falls inside the "
        "rectangle. Roads carry no equivalent clip: a road is not reduced to "
        "one representative point the way a footprint is, and an OSM way "
        "that runs outside the pulled rectangle simply finds no NGD segment "
        "within the offset sampler's own search radius, landing in that "
        "measurement's own unmatched-sample count rather than reading as a "
        "false gap the way an unclipped BUILDING would."
    )
    lines.append("")
    lines.append("Buildings, ours by source (in extent, the compared population):")
    for source in sorted(ours_by_source):
        lines.append(f"- {source}: {ours_by_source[source]}")
    lines.append(f"- total: {sum(ours_by_source.values())}")
    lines.append("")
    lines.append("Buildings, ours EXCLUDED as out of extent, by source:")
    if out_by_source:
        for source in sorted(out_by_source):
            lines.append(f"- {source}: {out_by_source[source]}")
        lines.append(f"- total: {sum(out_by_source.values())}")
    else:
        lines.append("- (none: every footprint of ours falls inside the pulled rectangle)")
    lines.append("")
    lines.append(
        f"Buildings, ours before clipping (every building=* way in the "
        f"package): {sum(all_by_source.values())}"
    )
    lines.append(f"- NGD buildingpart: {building_counts['ngd']}")
    lines.append("")
    road_counts = report["counts"]["roads"]
    lines.append("Roads (NOT clipped to the pulled rectangle; see above):")
    lines.append(f"- ours, OSM carriageway: {road_counts['ours_osm_carriageway']}")
    excluded_service = road_counts["ours_osm_excluded_service"]
    if excluded_service:
        named = ", ".join(
            f"{name} {excluded_service[name]}" for name in sorted(excluded_service)
        )
        lines.append(
            f"- ours, OSM highway=service EXCLUDED from carriageway (no NGD "
            f"roadlink counterpart, the same reason a pavement is excluded): "
            f"{sum(excluded_service.values())} ({named})"
        )
    else:
        lines.append("- ours, OSM highway=service excluded from carriageway: 0")
    lines.append(f"- ours, OSM path: {road_counts['ours_osm_path']}")
    other_values = road_counts["ours_osm_other_values"]
    if other_values:
        named = ", ".join(f"{name} {other_values[name]}" for name in sorted(other_values))
        lines.append(
            f"- ours, OSM highway=* in neither family (measured against "
            f"nothing): {sum(other_values.values())} ({named})"
        )
    else:
        lines.append("- ours, OSM highway=* in neither family: 0")
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

    lines.extend(_containment_lines(report))
    lines.extend(_size_lines(report))

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
    lines.append(
        "The OSM population is SPLIT into carriageways and paths, and the "
        "epoch verdict is read off the carriageway leg alone. NGD's roadlink "
        "collection holds carriageway centrelines and nothing else, so a "
        "pavement, a field path or a flight of steps has no counterpart in it "
        "to be offset from: each one still finds the nearest carriageway "
        "inside the sampler's search radius and contributes a vector that is "
        "the width of the street rather than a survey disagreement, and those "
        "vectors flip sign with which side of the street the pavement is on, "
        "so enough of them cancel and drag the estimate toward zero. This "
        "DELIBERATELY breaks continuity with the single-population OSM offset "
        "earlier runs of this benchmark published: that number was measured "
        "over both legs at once and read low because of it."
    )
    lines.append("")
    for label, key in (
        ("OSM carriageway (the epoch measurement)", "osm_carriageway"),
        ("OSM path (pavement-to-carriageway distance, NOT survey disagreement)", "osm_path"),
        ("OS Open (control)", "os_open"),
    ):
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
    lines.append(
        f"Epoch verdict (carriageway leg alone): {report['epoch_verdict']}"
    )
    lines.append("")

    lines.extend(_epoch_evidence_lines(report))

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
    lines.append(
        "Our own `building=*` tag values, over the WHOLE package and not "
        "clipped to the pulled rectangle (this table describes how this "
        "package is tagged, which is a fact about the package rather than "
        "about the comparison above):"
    )
    our_classes = report["classes"]["counts"]
    if our_classes:
        for name in sorted(our_classes):
            lines.append(f"- {name}: {our_classes[name]}")
    else:
        lines.append("- (no buildings in this package)")
    lines.append("")

    lines.extend(_reading_lines(report))

    return "\n".join(lines) + "\n"


def _reading_lines(report: dict) -> list[str]:
    """The closing paragraph: what the numbers above actually say.

    Written against the numbers in `report` rather than around them,
    because the easy closing line here is a false one. An earlier reading
    of this benchmark called the unmatched NGD footprints "not anything
    you would draw", which the subdivision half of the same table
    contradicts: a subdivision is a place where OS records a building
    boundary and this package records none, and those boundaries are
    party walls, house-to-garage joins and terrace divisions. Every one
    of them is a line an architect draws at 1:500. This section says what
    the two datasets agree and disagree about, and treats both halves of
    the unmatched table as real lines on a real drawing, never as a
    difference in counting alone.
    """
    theirs = report["containment"]["theirs_unmatched"]
    subdivision = theirs[CONTAINMENT_SUBDIVISION]
    absent = theirs[CONTAINMENT_ABSENT]
    larger = report["containment"]["theirs_subdivision_larger_than_ours"]
    ours_absent = report["containment"]["ours_unmatched"][CONTAINMENT_ABSENT_FROM_OS]
    matched_fraction = report["buildings"]["matched_fraction_ours"]

    lines: list[str] = []
    lines.append("## What this says")
    lines.append("")
    lines.append(
        f"Over the ground both datasets actually cover, the package and OS "
        f"agree about where the buildings are: {matched_fraction:.3f} of our "
        f"in-extent footprints pair with an NGD building part, at the IoU "
        f"quoted above. What they disagree about is outbuildings, and where "
        f"one building stops and the next begins."
    )
    lines.append("")
    lines.append(
        f"- {subdivision} NGD parts stand inside a footprint we hold. These "
        f"are boundaries OS records and this package does not: party walls, "
        f"house-to-garage joins, terrace divisions. They are lines an "
        f"architect draws at 1:500, so this half is a real difference in "
        f"what is drawn, not a difference in how the two datasets count. In "
        f"{larger} of them the NGD part is LARGER than the footprint of ours "
        f"containing it, which reads the other way round: our polygon is the "
        f"oversized one."
    )
    lines.append(
        f"- {absent} NGD parts stand on ground we hold nothing on. The size "
        f"table above says which of those matter: the small buckets are bin "
        f"stores, meter cabinets and garden sheds, the 80 to 200 m2 bucket is "
        f"houses."
    )
    lines.append(
        f"- {ours_absent} footprints of ours stand on ground OS holds nothing "
        f"on. Read against the same size table, and against the count of "
        f"footprints the clipping excluded: this is the column an injected "
        f"open-data layer inflates fastest."
    )
    lines.append("")
    return lines


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

    (
        all_building_rings,
        all_building_sources,
        building_counts_by_source,
        building_tag_counts,
    ) = _read_buildings(root, nodes, grid)
    ours_osm_roads = _read_osm_roads(root, nodes, grid)
    ours_os_open_roads = _read_os_open_roads(package_dir / f"{stem}_os_roads.geojson", grid)

    bbox_bng = padded_bng_extent(bbox, grid, 0.0)

    # Clip OUR OWN building population to the exact rectangle the NGD pull
    # is about to be made over, BEFORE anything is matched or classified.
    # The package covers roughly 1.8 times this ground (the survey pads
    # its extent, and injected OS OpenMap Local footprints arrive over a
    # wider footprint again), and a footprint outside the rectangle can
    # neither match an NGD feature nor stand inside one: it lands in
    # "absent from OS" by construction, which is not a finding about OS,
    # it is a finding about which ground was asked about. Measured on the
    # Cowbridge benchmark package, 367 of 1755 footprints of ours had
    # their interior point outside the queried rectangle, 346 of them
    # from the single OS OpenMap Local injection layer, and the two
    # headline numbers that flowed from leaving them in ("absent from
    # OS", and the matched fraction of ours) were both wrong because of
    # it. Their side needs no equivalent clipping: it came from the query.
    ours_building_rings, building_in_extent_by_source, building_out_of_extent_by_source = (
        _split_by_extent(all_building_rings, all_building_sources, bbox_bng)
    )

    client = client_factory(key)
    client.verify_collections([BUILDING_COLLECTION, ROAD_COLLECTION])
    ngd_building_features, building_pages = client.items(BUILDING_COLLECTION, bbox_bng)
    ngd_road_features, road_pages = client.items(ROAD_COLLECTION, bbox_bng)

    ngd_building_rings = _ngd_building_rings(ngd_building_features)
    ngd_road_polylines = _ngd_road_polylines(ngd_road_features)
    ngd_description_counts = _ngd_description_counts(ngd_building_features)

    match = match_footprints(ours_building_rings, ngd_building_rings)
    ious = [iou for _, _, iou in match.matched]

    # The four populations the containment and size sections describe,
    # gathered once here as plain ring lists so every index below is an
    # index into ITS OWN list: `classify_containment` and `area_histogram`
    # both speak positions into the sequence handed to them, which is
    # exactly what lets the cross-tabulation reuse one without
    # re-deriving the other.
    ours_matched_rings = [ours_building_rings[i] for i, _, _ in match.matched]
    theirs_matched_rings = [ngd_building_rings[j] for _, j, _ in match.matched]
    ours_unmatched_rings = [ours_building_rings[i] for i in match.unmatched_ours]
    theirs_unmatched_rings = [ngd_building_rings[j] for j in match.unmatched_theirs]

    # Each side's unmatched footprints tested against the OTHER side's
    # FULL population, not against its unmatched remainder: the whole
    # question is whether the ground is already covered, and a footprint
    # of ours that matched some other part of the same terrace covers
    # that ground just as much as an unmatched one does.
    theirs_containment = classify_containment(theirs_unmatched_rings, ours_building_rings)
    ours_containment = classify_containment(ours_unmatched_rings, ngd_building_rings)

    # How many of the SUBDIVISION cases are the reversed reading: their
    # part is bigger than the footprint of ours it stands inside, so what
    # the containment test found is not OS splitting a building we hold
    # whole, it is our polygon drawn oversized around a corner of theirs.
    # One aggregate integer, computed from two areas neither of which
    # leaves this function: a count, not a coordinate and not a
    # per-feature area, so the firewall is untouched.
    subdivision_larger_than_ours = sum(
        1
        for position in theirs_containment.contained
        if ring_area(theirs_unmatched_rings[position])
        > ring_area(ours_building_rings[theirs_containment.containers[position]])
    )

    offsets_osm_carriageway = polyline_offsets(ours_osm_roads.carriageway, ngd_road_polylines)
    offsets_osm_path = polyline_offsets(ours_osm_roads.path, ngd_road_polylines)
    offsets_os_open = polyline_offsets(ours_os_open_roads, ngd_road_polylines)

    # The carriageway leg ALONE (see `_read_osm_roads`): the path leg is
    # printed beside it as the pavement-to-carriageway distance it
    # actually measures, and never feeds the verdict.
    epoch_verdict = _epoch_verdict(offsets_osm_carriageway)
    pulled_date = date.today()

    report = {
        "source": "OS NGD (dev-mode evaluation key)",
        "pulled": pulled_date.isoformat(),
        "package": stem,
        "bbox": [bbox.west, bbox.south, bbox.east, bbox.north],
        "pages": {"buildings": building_pages, "roads": road_pages},
        "extent_bng": list(bbox_bng),
        "counts": {
            "buildings": {
                # The headline: our own footprints INSIDE the pulled
                # rectangle, the only ones the comparison below is about.
                "ours": building_in_extent_by_source,
                # What the clipping excluded, so it is visible rather
                # than silent.
                "ours_out_of_extent": building_out_of_extent_by_source,
                # Every building=* way in the package, clipped or not:
                # the pre-clip population, kept so the three numbers can
                # be read against each other.
                "ours_all": building_counts_by_source,
                "ngd": len(ngd_building_rings),
            },
            "roads": {
                "ours_osm_carriageway": len(ours_osm_roads.carriageway),
                "ours_osm_excluded_service": ours_osm_roads.excluded_service_counts,
                "ours_osm_path": len(ours_osm_roads.path),
                "ours_osm_other_values": ours_osm_roads.other_value_counts,
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
        "containment": {
            "theirs_unmatched": {
                CONTAINMENT_SUBDIVISION: len(theirs_containment.contained),
                CONTAINMENT_ABSENT: len(theirs_containment.not_contained),
            },
            "theirs_subdivision_larger_than_ours": subdivision_larger_than_ours,
            "ours_unmatched": {
                CONTAINMENT_SPURIOUS_OR_NEWER: len(ours_containment.contained),
                CONTAINMENT_ABSENT_FROM_OS: len(ours_containment.not_contained),
            },
        },
        "size": {
            "buckets": list(AREA_BUCKET_LABELS),
            "ours_matched": area_histogram(ours_matched_rings),
            "ours_unmatched": area_histogram(ours_unmatched_rings),
            "theirs_matched": area_histogram(theirs_matched_rings),
            "theirs_unmatched": area_histogram(theirs_unmatched_rings),
            "theirs_unmatched_by_class": {
                CONTAINMENT_SUBDIVISION: area_histogram(
                    theirs_unmatched_rings, theirs_containment.contained
                ),
                CONTAINMENT_ABSENT: area_histogram(
                    theirs_unmatched_rings, theirs_containment.not_contained
                ),
            },
            "ours_unmatched_by_class": {
                CONTAINMENT_SPURIOUS_OR_NEWER: area_histogram(
                    ours_unmatched_rings, ours_containment.contained
                ),
                CONTAINMENT_ABSENT_FROM_OS: area_histogram(
                    ours_unmatched_rings, ours_containment.not_contained
                ),
            },
        },
        "roads": {
            "osm_carriageway": _offset_stats_dict(offsets_osm_carriageway),
            "osm_path": _offset_stats_dict(offsets_osm_path),
            "os_open": _offset_stats_dict(offsets_os_open),
        },
        "classes": {
            "ngd_only": ngd_description_counts,
            "counts": building_tag_counts,
        },
        "epoch_verdict": epoch_verdict,
        # Which road population the verdict above was read off, named in
        # the machine-readable report as well as in the prose, so a later
        # reader comparing two runs can see that the basis changed rather
        # than reading a moved number as a moved measurement.
        "epoch_basis": "osm_carriageway",
    }

    md_text = _render_markdown(report)
    json_text = json.dumps(report, indent=2)

    out_dir = Path(out_root) / f"{stem}_{pulled_date.isoformat()}"
    md_path = out_dir / "report.md"
    json_path = out_dir / "report.json"
    atomic_write_text(md_path, md_text)
    atomic_write_bytes(json_path, json_text.encode("utf-8"))
    return md_path, json_path
