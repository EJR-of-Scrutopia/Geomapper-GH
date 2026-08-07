"""Fusing Overture and OS OpenMap Local footprints into the OSM base.

The owner's own ground truth (Cowbridge, 2026-08-06): the OSM base for a
package renders 1,552 building ways; the SAME package's Overture
`_building.geojson` holds 2,629 features, 1,076 of them Microsoft ML
Buildings footprints OSM never had at all. `mapgen.sources.osm` merges
whatever OSM itself has traced; this module adds what it has not, from two
optional per-package files: `<stem>_building.geojson` (Overture, aerial-
traced, sometimes carrying a `height`) and `<stem>_os_buildings.geojson`
(OS OpenMap Local, generalised but nationally complete), in that priority
order, since Overture's own outlines are the better trace and OS is the
completeness backstop underneath them.

## The fusion rule, and why it is what it is

A candidate footprint is injected only if BOTH hold:

  (a) its own representative interior point lies inside no ACCEPTED
      footprint (every existing OSM `building=*` way, resolved through
      its own node refs, plus every candidate already accepted earlier
      in this same run), and
  (b) no accepted footprint's own representative interior point lies
      inside the candidate.

Two directions, not one, because a small candidate sitting entirely inside
a much larger existing footprint would pass (a) (its own interior point is
inside the big one) but a huge candidate that entirely SWALLOWS a small
existing footprint could otherwise slip through under (a) alone (the
candidate's own interior point might sit outside the small existing shape
diagonally across the ring): (b) catches that direction by testing the
existing footprint's own interior point against the candidate's own ring
instead. Point-in-polygon, not ring intersection: cheap, and enough to
tell "this is materially the same building" from "these two happen to
touch", which is the only distinction fusion needs to make.

The "representative interior point" is `_representative_point`
(a scanline label-point, guaranteed interior to any simple ring, convex
or concave), NOT a plain vertex-average centroid: a vertex average is
cheaper but is not guaranteed to lie inside its own ring once that ring
is concave (a U-shaped building, a courtyard block, a dormitory wing),
which let a genuine duplicate escape both rules entirely. See
`_representative_point`'s own docstring for the fix and the code review
finding (task-6-review.md, Critical C1) that caught it: a first version
of this module used the vertex average here and was wrong on 1.74% of
one real survey's own existing buildings.

Existing OSM building ways are read, never written: their own tags are
never touched, whatever a candidate at the same place might have called
the same building, matching the plan's own "fusion adds what is missing,
never overwrites" rule.

## No special-casing by dataset

An Overture feature whose own `sources` list names only `OpenStreetMap`
is, in practice, a re-trace of a way already in the `.osm`; nothing here
looks at `sources` to catch that case specially, because the geometry
test above already catches it: that Overture ring's own representative
point already sits inside the existing OSM way it duplicates, so rule
(a) rejects it on its own, for the identical reason it rejects a
genuinely new footprint that happens to overlap something real. The
dataset a footprint CAME from never enters the dedup decision, only
where it IS.

## Idempotency, by the same mechanism, not a second one

Reading `mapgen.package._fuse_boundaries_step`, the sibling this module's
own package.py wrapper is modelled on, its own idempotence guard is
COARSE: any way already tagged `source=hm_land_registry` means an earlier
run already injected these exact curves, so nothing is even parsed a
second time and the file is never opened for writing again. Boundaries
fusion needs that guard because it has no other way to tell "already
here" from "new": every LineString becomes fresh nodes unconditionally,
with no overlap test of its own.

This module already has a finer instrument for exactly that
question, so it is reused rather than duplicated: a way this module
itself injected on an earlier run is, by the next run, simply another
`building=*` way already in the file, which `_existing_building_footprints`
below folds into the very same accepted set every OTHER existing OSM
building already sits in. Re-offering the same candidates a second time
therefore fails rule (a) against the previous run's own output and is
skipped as an overlap, not written twice; nothing is stripped and
nothing is re-fused, matching the sibling's own "skip, do not re-fuse"
behaviour by outcome (a second run written 0, an unchanged file, no
duplicate ways) even though the mechanism is per-candidate geometry here
rather than one file wide tag check there. See this task's own report for
why a second, coarser gate on top of this one would only ever agree with
it and was judged not worth the extra bookkeeping.

## MultiPolygon candidates, and the geometry this module refuses to invent

A MultiPolygon candidate's exterior ring, per member polygon, becomes its
own injected way; any INTERIOR ring (a hole) on either a Polygon or a
MultiPolygon member is dropped without inspection. Holes in a real
building footprint are vanishingly rare at these scales (an inner
courtyard, mostly), and representing one properly would need a multi-
polygon relation this module does not build; the alternative, closing the
outer ring as if the hole were solid ground, is judged the lesser
simplification and is what every exterior-only reader downstream already
does with a plain `building=yes` way regardless. `written` and
`per_source` both count a two-exterior MultiPolygon as two, not one,
since two ways are what is actually added to the file; nothing in the
record shape (four keys, fixed by the brief) carries a separate tally of
"how many source features split into more than one way", so that count
lives only here, in this docstring and the module's own tests.

## Node ids, and one thing this module deliberately does NOT do

Every injected node and way id sits strictly below the file's own actual
minimum id (`heights._minimum_existing_id`, shared with
`package._fuse_boundaries_step` rather than reimplemented, since both
steps need the identical collision-safety reasoning spelled out on that
function's own docstring). Nodes are NOT deduplicated across two
different injected ways even where two footprints genuinely share a
corner: a shared corner becomes two separate node elements, one per way,
at the same coordinate. Cross-way node sharing is the ordinary OSM way of
representing adjoining buildings properly, but detecting a "shared
corner" robustly (coordinate equality is not enough once floating point
and two different source datasets are both in play) is real machinery
this task's brief explicitly judged not worth building for a fusion step
whose whole job is footprints Urbano did not have at all a moment ago.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from mapgen.heights import HeightsError, _declaration_line, _minimum_existing_id, _rewrite_osm

# The spatial hash's own cell size, in degrees: coarse enough that an
# ordinary building footprint (tens of metres) sits inside one or a
# handful of cells, fine enough that a whole survey extent (a few
# kilometres) is spread across many buckets rather than one. 0.0005
# degrees is a little under 50 m of latitude at UK latitudes, comfortably
# larger than any single building this module will ever see, per the
# brief's own value.
CELL_SIZE_DEGREES = 0.0005

_BUILDING_TAG_KEY = "building"
_SOURCE_TAG_KEY = "source"
_HEIGHT_TAG_KEY = "height"
_CLASS_TAG_KEY = "class"
_DEFAULT_BUILDING_TAG_VALUE = "yes"

# The minimum number of distinct exterior vertices a ring needs to be a
# polygon at all (a triangle), matching heights.py's own `_footprint_ring`
# floor of "at least 3 refs" for the identical reason.
_MIN_RING_POINTS = 3


@dataclass(frozen=True)
class BuildingsFusionRecord:
    """What one pass over an `.osm` plus its candidate footprints did, in
    the four numbers `package.py` needs.

    `per_source` carries one entry per source tag `fuse_missing_buildings`
    was handed, present even at 0, so a reader never has to guess whether
    a source ran and injected nothing apart from one that never ran at
    all: `package.py`'s own survey.json record flattens this into
    `from_overture`/`from_os` explicitly instead of nesting the dict, but
    the dataclass itself stays source-agnostic (a future third source
    needs no new field here, only a new entry in `candidates`).

    `skipped_overlap` is every candidate ring this run looked at and did
    NOT write, for any reason at all: it failed the dedup rule (a
    genuine duplicate of something already accepted) or it could not be
    trusted as a shape in the first place (see the module docstring's
    "nothing fabricated" section). The record has no separate bucket for
    the second case; see this task's own report for why one more field
    was judged not worth adding for a distinction survey.json's readers
    were never asked to make.

    `kept_existing` is a fact about the file BEFORE this run touched
    anything: every existing building already there, whatever happened to
    it next, whether tagged directly on a `<way>` or on a `<relation>`
    whose own member ways carry the geometry (code review task-6-review.md,
    Important I1), including, on a second run over the same candidates,
    every way THIS module itself injected the first time (per the module
    docstring's idempotency section).
    """

    written: int
    per_source: dict[str, int]
    skipped_overlap: int
    kept_existing: int


# --------------------------------------------------------------------------
# Ray casting and the spatial hash.
# --------------------------------------------------------------------------


def point_in_ring(x: float, y: float, ring: Sequence[tuple[float, float]]) -> bool:
    """The ordinary even-odd (ray casting, PNPOLY) test, identical
    convention to `heights._point_in_polygon` and to `contours.py`'s own
    crossing rule (that module's docstring: "a corner's state is always
    `value > level`, never `>=`"): every comparison here is a strict `>`,
    so a vertex sitting exactly on the horizontal test ray belongs to at
    most one of its two adjoining edges, never both or neither.

    `ring` need not be explicitly closed (last point equal to first): the
    edge from the last vertex back to the first is included by starting
    `prev` at index -1, so a ring this module has already stripped its
    own closing duplicate from (`_drop_closing_duplicate`) and a raw
    GeoJSON ring that never had one both test identically.

    Public (promoted from `_point_in_ring`) because `classify.py`'s
    sampled-majority parcel classifier needs the identical ray cast for
    its own overlay lookups; `_point_in_ring` is kept below as an alias
    so every existing call site and test in this module and
    `test_buildings.py` still resolves unchanged.
    """
    inside = False
    prev_x, prev_y = ring[-1]
    for cx, cy in ring:
        if (cy > y) != (prev_y > y):
            x_at = (prev_x - cx) * (y - cy) / (prev_y - cy) + cx
            if x < x_at:
                inside = not inside
        prev_x, prev_y = cx, cy
    return inside


# Old, module-private name kept as a plain alias: every call site in this
# module (and `test_buildings.py`) that already spells `_point_in_ring`
# keeps working unchanged.
_point_in_ring = point_in_ring


def _drop_closing_duplicate(
    ring: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    """`ring`'s own vertices with a repeated closing point removed, if it
    has one: both a GeoJSON polygon ring (always explicitly closed, per
    the spec) and an existing OSM way's own resolved ring (closed by
    repeating the first node's id in its `nd` list, the ordinary OSM
    convention) carry this same duplicate, and every geometry operation
    downstream (centroid, bbox, the ray cast, node injection) wants the
    distinct vertex set once, not twice.
    """
    points = list(ring)
    if len(points) > 1 and points[0] == points[-1]:
        return points[:-1]
    return points


def _horizontal_spans(
    ring: Sequence[tuple[float, float]], y: float
) -> list[tuple[float, float]]:
    """Every interior span a horizontal line at height `y` cuts through
    `ring`, as `(x_low, x_high)` pairs in ascending order, under the same
    even-odd rule `_point_in_ring` itself uses.

    Callers of this function only ever pass a `y` proven not to equal any
    of `ring`'s own vertex y-values (`_representative_point`'s own
    construction), so every edge either straddles `y` cleanly or misses
    it entirely; no edge can lie exactly along it, which is what keeps
    the crossing count even and the pairing below correct.
    """
    crossings: list[float] = []
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossings.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    crossings.sort()
    return [
        (crossings[index], crossings[index + 1])
        for index in range(0, len(crossings) - 1, 2)
    ]


def representative_point(ring: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """A point GUARANTEED interior to `ring`, replacing a plain vertex-
    average centroid, which is NOT guaranteed interior to a concave ring
    (code review task-6-review.md, Critical C1: a U-shaped footprint's
    own vertex average falls squarely in its own notch, outside the ring
    entirely, which let rules (a)/(b) below miss a genuine duplicate on
    the very first run and on every rerun).

    The standard label-point scanline: a horizontal line is cast through
    `ring` at a height strictly between two ADJACENT distinct vertex
    y-values, chosen as close to the ring's own vertical middle as
    possible (never through a bare bounding-box midpoint, which can land
    exactly on a vertex and graze it, the degenerate case
    `_horizontal_spans` is written to never be asked to handle); the
    widest of the resulting interior spans is a real, unbroken run of
    the ring's own interior, and ITS midpoint therefore sits strictly
    between two points of the ring's own boundary, which is what makes
    it interior by construction rather than by any property of the
    ring's own vertex arrangement (convex or not, symmetric or not).

    A ring whose vertices all share one y-value has no vertical extent
    for a scanline to cut through at all (zero area, not a real
    polygon); this is treated as the degenerate case it is, falling back
    to the bounding box's own centre, rather than raising over a shape
    that should never have passed the caller's own minimum-points check
    in the first place.

    Public (promoted from `_representative_point`) because `classify.py`'s
    sampled-majority parcel classifier reuses it both for the "too small
    for a grid" sampling fallback and as the always-interior point that
    guarantees that fallback never samples zero points; `_representative_point`
    is kept below as an alias so every existing call site and test in
    this module and `test_buildings.py` still resolves unchanged.
    """
    points = _drop_closing_duplicate(ring)
    ys = sorted(set(y for _, y in points))
    if len(ys) < 2:
        xs = [x for x, _ in points]
        return ((min(xs) + max(xs)) / 2.0, ys[0])

    midpoint = (ys[0] + ys[-1]) / 2.0
    lower, upper = ys[0], ys[-1]
    for a, b in zip(ys, ys[1:]):
        if a <= midpoint <= b:
            lower, upper = a, b
            break
    test_y = (lower + upper) / 2.0

    spans = _horizontal_spans(points, test_y)
    if not spans:
        # Never reached for a simple polygon (a scanline strictly between
        # two of its own adjacent vertex y-values always cuts at least one
        # interior span), but nothing here fabricates a point for
        # anything self-intersecting or otherwise malformed enough to
        # defeat that: the bounding box centre is the same honest
        # fallback the degenerate branch above already uses.
        xs = [x for x, _ in points]
        return ((min(xs) + max(xs)) / 2.0, test_y)
    widest = max(spans, key=lambda span: span[1] - span[0])
    return ((widest[0] + widest[1]) / 2.0, test_y)


# Old, module-private name kept as a plain alias: every call site in this
# module (and `test_buildings.py`) that already spells `_representative_point`
# keeps working unchanged.
_representative_point = representative_point


def _ring_bbox(ring: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [x for x, _ in ring]
    ys = [y for _, y in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _cell(x: float, y: float) -> tuple[int, int]:
    return (math.floor(x / CELL_SIZE_DEGREES), math.floor(y / CELL_SIZE_DEGREES))


def _cells_for_bbox(
    bbox: tuple[float, float, float, float]
) -> list[tuple[int, int]]:
    """Every grid cell `bbox` reaches into, inclusive of both corners: a
    footprint whose bounding box spans several cells is registered under
    ALL of them, which is what lets a query from any one of those cells
    find it, including one nowhere near the footprint's own centroid
    (the "across cell borders" case this module's own tests check for).
    """
    min_x, min_y, max_x, max_y = bbox
    col_min, row_min = _cell(min_x, min_y)
    col_max, row_max = _cell(max_x, max_y)
    return [
        (col, row)
        for row in range(row_min, row_max + 1)
        for col in range(col_min, col_max + 1)
    ]


@dataclass
class _Footprint:
    """One accepted footprint's own ring, bounding box and representative
    interior point (`_representative_point`, GUARANTEED to lie inside
    `ring` itself, not a plain vertex-average that a concave ring can
    place outside its own boundary; see that function's own docstring
    for the code-review finding this replaced), precomputed once at
    acceptance time rather than on every later query: an accepted
    footprint is tested against many later candidates over the life of
    one `fuse_missing_buildings` call, its own geometry never changes
    once accepted, so computing it twice would be pure waste.
    """

    ring: list[tuple[float, float]]
    bbox: tuple[float, float, float, float] = field(init=False)
    interior_point: tuple[float, float] = field(init=False)

    def __post_init__(self) -> None:
        self.bbox = _ring_bbox(self.ring)
        self.interior_point = _representative_point(self.ring)


class _SpatialIndex:
    """Every accepted footprint (every existing OSM building this run
    started with, plus every candidate this run has accepted so far),
    bucketed by `CELL_SIZE_DEGREES` grid cell, so a new candidate only
    ever tests footprints that could plausibly matter to it rather than
    every accepted footprint there is: linear in the number of
    footprints touched, not quadratic in how many exist.

    Two bucket maps, not one, because rule (a) and rule (b) ask opposite
    questions: (a) is answered from the CANDIDATE's own interior-point
    cell, against every footprint whose BOUNDING BOX reaches that cell
    (`_by_bbox_cell`); (b) is answered from every cell the CANDIDATE's own
    bounding box spans, against every footprint whose INTERIOR POINT falls
    in one of them (`_by_interior_point_cell`). A footprint registered
    under `_by_bbox_cell` for every cell its box spans is what lets (a)
    find a footprint whose own interior point sits in a different cell
    from the one a small candidate's interior point lands in but whose
    bulk still reaches it, which is the exact "across cell borders"
    property this module's own tests check for directly.
    """

    def __init__(self) -> None:
        self._by_bbox_cell: dict[tuple[int, int], list[_Footprint]] = defaultdict(list)
        self._by_interior_point_cell: dict[tuple[int, int], list[_Footprint]] = defaultdict(list)

    def add(self, footprint: _Footprint) -> None:
        for cell in _cells_for_bbox(footprint.bbox):
            self._by_bbox_cell[cell].append(footprint)
        self._by_interior_point_cell[_cell(*footprint.interior_point)].append(footprint)

    def candidate_centroid_is_covered(self, point: tuple[float, float]) -> bool:
        """Rule (a): does any accepted footprint's own ring already
        contain the candidate's own representative point `point`?

        Name kept from this module's first version (the brief's own
        "centroid" wording) even though `point` is no longer a vertex
        average: renaming every call site for a private, module-internal
        method was judged less valuable than keeping this diff reviewable
        against the finding it fixes.
        """
        cell = _cell(*point)
        return any(
            _point_in_ring(point[0], point[1], footprint.ring)
            for footprint in self._by_bbox_cell.get(cell, ())
        )

    def an_accepted_centroid_falls_inside(
        self, ring: Sequence[tuple[float, float]], bbox: tuple[float, float, float, float]
    ) -> bool:
        """Rule (b): does `ring` already contain some accepted
        footprint's own representative point?
        """
        for cell in _cells_for_bbox(bbox):
            for footprint in self._by_interior_point_cell.get(cell, ()):
                if _point_in_ring(footprint.interior_point[0], footprint.interior_point[1], ring):
                    return True
        return False


# --------------------------------------------------------------------------
# Reading the existing OSM building ways.
# --------------------------------------------------------------------------


def _resolve_way_ring(
    way: ET.Element, nodes: dict[str, tuple[float, float]]
) -> list[tuple[float, float]] | None:
    """`way`'s own `nd` refs resolved through `nodes` into a ring, its own
    closing duplicate dropped, or None if any ref does not resolve or
    fewer than 3 distinct vertices remain: the identical tolerance
    `heights._footprint_ring` applies to a malformed way, shared here
    between a plain tagged way and a building relation's own member way
    (`_existing_building_footprints` below), rather than kept as two
    copies of the same resolve-then-drop-then-floor logic.
    """
    refs = [nd.get("ref") for nd in way.findall("nd")]
    ring: list[tuple[float, float]] = []
    for ref in refs:
        point = nodes.get(ref) if ref is not None else None
        if point is None:
            return None
        ring.append(point)
    ring = _drop_closing_duplicate(ring)
    if len(ring) < _MIN_RING_POINTS:
        return None
    return ring


def _relation_building_member_way_ids(relation: ET.Element) -> list[str]:
    """Every `<member type="way">` ref a building relation names, for the
    OUTER ring(s) only when any member's `role` distinguishes outer from
    inner (an inner member is a hole and is dropped, matching this
    module's own "holes dropped" rule for a MultiPolygon candidate's
    interior rings); when no member's role distinguishes them at all
    (every role empty or absent, real data this module has seen), every
    member way is used instead, since there is then no signal here at
    all to tell an outer ring from an inner one.
    """
    way_members = [
        member
        for member in relation.findall("member")
        if member.get("type") == "way" and member.get("ref") is not None
    ]
    outer_refs = [member.get("ref") for member in way_members if member.get("role") == "outer"]
    if outer_refs:
        return outer_refs
    return [member.get("ref") for member in way_members]


def _existing_building_footprints(root: ET.Element) -> tuple[list[_Footprint], int]:
    """Every existing building already in `root`, as accepted footprints
    for rules (a)/(b): every `<way>` tagged `building=*` directly (any
    value, not only `yes`, matching `heights.py`'s own `_BUILDING_TAG_KEY`
    check), PLUS every outer member way of a `<relation>` tagged
    `building=*` (code review task-6-review.md, Important I1: a building
    represented as a multipolygon relation, tags on the relation and its
    member ways carrying none of their own, the ordinary real-OSM shape,
    was previously invisible here, so a candidate exactly duplicating one
    went undetected on real data).

    Returns `(footprints, total)`. `total` is `kept_existing`: literally
    how many buildings were already in the file, counting a tagged way
    and a tagged relation the same way, whether or not this function
    could resolve a usable ring for either. A way (plain or a relation's
    own member) whose refs do not all resolve, or whose ring has fewer
    than 3 distinct vertices once its own closing duplicate is dropped,
    contributes to `total` but never to `footprints`: it is still a
    building already in the file (counted), but not a shape the dedup
    rules below can test against (excluded from the index), the identical
    tolerance `heights._footprint_ring` applies to a malformed way it
    folds into `no_data` rather than raising over.

    Relation members are read only to grow the accepted set the dedup
    rules test against; nothing here writes to them, and `heights.py`'s
    own `relations_skipped` scope limit for the SEPARATE heights-fusion
    pass is unaffected (a member way still carries no `height` tag of its
    own to skip past, and this function is never called from there).
    """
    nodes: dict[str, tuple[float, float]] = {}
    ways_by_id: dict[str, ET.Element] = {}
    for element in root:
        if element.tag == "node":
            node_id = element.get("id")
            lat = element.get("lat")
            lon = element.get("lon")
            if node_id is None or lat is None or lon is None:
                continue
            try:
                nodes[node_id] = (float(lon), float(lat))
            except ValueError:
                continue
        elif element.tag == "way":
            way_id = element.get("id")
            if way_id is not None:
                ways_by_id[way_id] = element

    footprints: list[_Footprint] = []
    total = 0

    for element in root:
        if element.tag != "relation":
            continue
        tags = element.findall("tag")
        if not any(tag.get("k") == _BUILDING_TAG_KEY for tag in tags):
            continue
        total += 1
        for way_id in _relation_building_member_way_ids(element):
            member_way = ways_by_id.get(way_id)
            if member_way is None:
                continue
            ring = _resolve_way_ring(member_way, nodes)
            if ring is not None:
                footprints.append(_Footprint(ring=ring))

    for element in root:
        if element.tag != "way":
            continue
        tags = element.findall("tag")
        if not any(tag.get("k") == _BUILDING_TAG_KEY for tag in tags):
            continue
        total += 1
        ring = _resolve_way_ring(element, nodes)
        if ring is not None:
            footprints.append(_Footprint(ring=ring))

    return footprints, total


# --------------------------------------------------------------------------
# Reading a candidate feature's own geometry and tags.
# --------------------------------------------------------------------------


def _exterior_ring(coordinates: object) -> list[tuple[float, float]] | None:
    """A GeoJSON Polygon's own `coordinates[0]` (the exterior ring; any
    further ring is a hole, per the module docstring's "holes dropped"
    rule, and is never read here at all), as a plain list of `(lon, lat)`
    pairs with its own closing duplicate already dropped.

    None for anything this function cannot trust: not a list, no ring at
    all, a ring point that is not exactly two numbers, or (after the
    closing duplicate is dropped) fewer than 3 distinct vertices.
    Nothing here repairs a shape, only refuses it, per the plan's
    "nothing fabricated" rule.
    """
    if not isinstance(coordinates, list) or not coordinates:
        return None
    exterior = coordinates[0]
    if not isinstance(exterior, list):
        return None
    ring: list[tuple[float, float]] = []
    for pair in exterior:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            return None
        try:
            x, y = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            return None
        ring.append((x, y))
    ring = _drop_closing_duplicate(ring)
    if len(ring) < _MIN_RING_POINTS:
        return None
    return ring


def _candidate_rings(feature: object) -> tuple[list[list[tuple[float, float]]], int]:
    """Every usable exterior ring `feature` contributes, and how many of
    its own sub-shapes could not be trusted at all.

    A Polygon contributes at most one ring; a MultiPolygon contributes
    one ring per member polygon whose own exterior is usable (the module
    docstring's "MultiPolygon" section: each becomes its own injected
    way). The second return value is how many sub-shapes were refused
    (an unsupported geometry type, a missing or malformed `coordinates`
    value, or a ring `_exterior_ring` itself refused): the caller folds
    this into `skipped_overlap` (see that field's own docstring on
    `BuildingsFusionRecord` for why one shared bucket, not two).
    """
    if not isinstance(feature, dict):
        return [], 1
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return [], 1
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        ring = _exterior_ring(coordinates)
        return ([ring], 0) if ring is not None else ([], 1)
    if geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates:
            return [], 1
        rings: list[list[tuple[float, float]]] = []
        invalid = 0
        for polygon in coordinates:
            ring = _exterior_ring(polygon)
            if ring is None:
                invalid += 1
            else:
                rings.append(ring)
        return rings, invalid
    return [], 1


def _feature_properties(feature: object) -> dict:
    if not isinstance(feature, dict):
        return {}
    properties = feature.get("properties")
    return properties if isinstance(properties, dict) else {}


def _building_tag_value(properties: dict) -> str:
    """`building=yes` unless the candidate names its own type through a
    non-empty string `building` property. Deliberately NOT extended to
    OS's own `class`/`theme` properties (the brief's own ruling): a `class`
    value is carried through as its own separate tag instead (see
    `_class_tag_value`), never folded into `building=`.
    """
    value = properties.get("building")
    if isinstance(value, str) and value:
        return value
    return _DEFAULT_BUILDING_TAG_VALUE


def _class_tag_value(properties: dict) -> str | None:
    value = properties.get("class")
    if isinstance(value, str) and value:
        return value
    return None


def _height_tag_value(properties: dict) -> str | None:
    """Overture's own `height` property (metres), stringified to one
    decimal, OSM's own convention for the tag (matching `heights.py`'s
    `f"{height:.1f}"`). `bool` is excluded explicitly even though Python's
    `bool` is an `int` subclass: a stray `True`/`False` here is not a
    height no matter what `float()` would do with it.
    """
    value = properties.get("height")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return f"{float(value):.1f}"
    return None


# --------------------------------------------------------------------------
# The fusion itself.
# --------------------------------------------------------------------------


def fuse_missing_buildings(
    osm_path: Path, candidates: Sequence[tuple[str, list[dict]]]
) -> BuildingsFusionRecord:
    """Inject every candidate footprint `candidates` holds that the `.osm`
    at `osm_path` does not already have, tagged and id'd per the module
    docstring, and return what happened as a `BuildingsFusionRecord`.

    `candidates` is `(source_tag, features)` pairs in PRIORITY order: a
    feature accepted from an earlier pair is already in the dedup index
    by the time a later pair's own features are tested, so a later
    source's duplicate of an earlier source's newly accepted footprint is
    rejected by the same rule (a)/(b) test as a duplicate of the original
    OSM base, with no special casing between the two (see the module
    docstring's "no special-casing by dataset" section).

    Reads and rewrites `osm_path` exactly the way `fuse_building_heights`
    does (`heights._declaration_line`/`heights._rewrite_osm`, not a
    second copy of that shape): a `HeightsError` here means the exact
    same thing it means there, an `.osm` that is not the shape
    `mapgen.merge.merge_osm_xml` itself produces, and is raised rather
    than guessed past. Nothing is written back at all, not even the
    declaration and closing tag, when nothing was actually injected
    (`written == 0`), matching `fuse_building_heights`'s own `changed`
    guard.
    """
    path = Path(osm_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HeightsError(f"{path.name} could not be read: {exc}") from None
    declaration = _declaration_line(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise HeightsError(f"{path.name} is not valid XML: {exc}") from None

    footprints, kept_existing = _existing_building_footprints(root)
    index = _SpatialIndex()
    for footprint in footprints:
        index.add(footprint)

    next_id = _minimum_existing_id(root) - 1
    new_nodes: list[ET.Element] = []
    new_ways: list[ET.Element] = []
    per_source: dict[str, int] = {}
    skipped_overlap = 0

    for source_tag, features in candidates:
        per_source.setdefault(source_tag, 0)
        for feature in features:
            properties = _feature_properties(feature)
            rings, invalid_count = _candidate_rings(feature)
            skipped_overlap += invalid_count
            for ring in rings:
                interior_point = _representative_point(ring)
                bbox = _ring_bbox(ring)
                if index.candidate_centroid_is_covered(interior_point):
                    skipped_overlap += 1
                    continue
                if index.an_accepted_centroid_falls_inside(ring, bbox):
                    skipped_overlap += 1
                    continue

                way_id = next_id
                next_id -= 1
                way = ET.Element("way")
                way.set("id", str(way_id))

                # One fresh node per distinct vertex, never shared with
                # another injected way even at a corner two footprints
                # happen to share exactly (module docstring, "one thing
                # this module deliberately does NOT do").
                first_node_id: str | None = None
                for x, y in ring:
                    node_id = next_id
                    next_id -= 1
                    node = ET.Element("node")
                    node.set("id", str(node_id))
                    node.set("lat", f"{y:.7f}")
                    node.set("lon", f"{x:.7f}")
                    new_nodes.append(node)
                    nd = ET.SubElement(way, "nd")
                    nd.set("ref", str(node_id))
                    if first_node_id is None:
                        first_node_id = str(node_id)
                # Close the ring by repeating the first node's own id,
                # the ordinary OSM convention (and the one every existing
                # building way this module reads already uses): no new
                # node for the closing point, only a second `nd` ref.
                closing = ET.SubElement(way, "nd")
                closing.set("ref", first_node_id)

                for key, value in (
                    (_BUILDING_TAG_KEY, _building_tag_value(properties)),
                    (_SOURCE_TAG_KEY, source_tag),
                ):
                    tag = ET.SubElement(way, "tag")
                    tag.set("k", key)
                    tag.set("v", value)
                class_value = _class_tag_value(properties)
                if class_value is not None:
                    tag = ET.SubElement(way, "tag")
                    tag.set("k", _CLASS_TAG_KEY)
                    tag.set("v", class_value)
                height_value = _height_tag_value(properties)
                if height_value is not None:
                    tag = ET.SubElement(way, "tag")
                    tag.set("k", _HEIGHT_TAG_KEY)
                    tag.set("v", height_value)

                new_ways.append(way)
                index.add(_Footprint(ring=ring))
                per_source[source_tag] += 1

    written = len(new_ways)
    if written > 0:
        for node in new_nodes:
            root.append(node)
        for way in new_ways:
            root.append(way)
        _rewrite_osm(path, declaration, root)

    return BuildingsFusionRecord(
        written=written,
        per_source=per_source,
        skipped_overlap=skipped_overlap,
        kept_existing=kept_existing,
    )
