"""Sampled-majority classification of an INSPIRE parcel against the
package's own overlay layers (fused .osm buildings, Overture land_use,
Overture/`.osm` water, OS Open Greenspace, OS OpenMapLocal Woodland).

## What a parcel gets classified as, and why

A parcel is never given a category no overlay actually supports: an
uncovered parcel is `unclassified`, never guessed (the plan's own
"nothing fabricated" rule). Where evidence exists, it is read off a grid
of interior sample points rather than any exact-geometry intersection or
area computation, because the overlays this classifier reads (Overture's
own land_use extraction, OS Open Greenspace, OS OpenMapLocal Woodland)
are themselves generalised, aerial-derived products, not survey-grade
boundaries; a sampled majority is an honest match to that precision, and
is what the plan's own header spec pins.

## The single majority race, and why buildings sit outside it

Every sample point is tested against every overlay ring it could
plausibly fall inside, and each hit is tagged with an EVIDENCE BUCKET:
one of the nine land_use groups the mapping table below collapses raw
Overture `land_use` classes into (`garden`, `field`, `recreation`,
`retail`, `industrial`, `education`, `religious`, `allotments`, or the
land_use side of `greenspace` via `grass`), plus `water`, the OS Open
Greenspace side of `greenspace`, and `woodland`. The bucket with the
most sample hits is the parcel's overall majority, ties broken by the
PRIORITY_ORDER below (the plan header's own "ties... the priority order
breaks them"), and a raw land_use class the mapping table does not
recognise contributes to no bucket at all, exactly as pinned ("a land-use
class mapping to 'no evidence' contributes nothing to any majority").

Fused .osm building footprints are read differently: `housing` needs
"at least one sample hit" AND "the parcel's land-use majority is
residential" (the plan header's own rule 1), a compound gate layered on
top of the majority race rather than a bucket competing inside it. A
single barn's footprint sitting on farmland must never out-vote a whole
field of farmland samples merely by existing (the plan header's own
worked example, pinned as `test_farmland_majority_with_a_barn_footprint_is_field_not_housing`
in this module's test file); giving `building` its own bucket in the
count-based race would let exactly that happen on a small enough parcel.
So buildings are tested for a same-point hit alongside everything else
(one shared candidate lookup, one shared spatial hash, see
`_OverlayIndex` below) but are folded into the final decision as a
boolean gate, never a vote.

`greenspace` can be reached two different ways (Overture `land_use`
class `grass`, or an OS Open Greenspace site polygon) at two different
priority ranks (the plan header's rule 2 vs its rule 4). Both are kept
as SEPARATE evidence buckets here (`_LANDUSE_GREENSPACE_BUCKET` and
`_OS_GREENSPACE_BUCKET`), each with its own slot in PRIORITY_ORDER,
rather than merged into one `greenspace` bucket before the count race:
the plan header lists them as genuinely different rules at different
priority ranks, and merging their counts before comparing against, say,
`water`'s count would silently let two different, weaker forms of
evidence combine into a majority neither held on its own. They only
become the same thing at the very end, when a winning bucket's name is
looked up in `_FINAL_CATEGORY_BY_BUCKET`.

## Sampling: the grid, its spacing, and the small-parcel fallback

Sample points sit on a square grid (in real, local metres) over the
parcel's own bounding box, spacing `clamp(sqrt(parcel_area_m2) / 8, 2.0,
20.0)` metres, converted to a lon/lat step with the local cos-latitude
factor (metres per degree latitude is a constant 111,320; metres per
degree longitude shrinks by `cos(latitude)` moving away from the
equator): this is a sampling density choice for an inherently
approximate classification, not a survey-grade projection, so the
single constant and a plain `cos()` are enough; a full geodesic
transform would buy nothing a `clamp()`-bounded grid spacing does not
already tolerate. `parcel_area_m2` itself comes from the shoelace
formula run over the same local-metre projection (see
`_parcel_area_m2`), for the identical reason: it only has to size a
sampling grid, not certify a legal area.

A parcel too small to catch at least 4 grid points this way (a sliver,
a needle-thin remnant) falls back to `representative_point` (imported
from `buildings.py`, GUARANTEED interior to any simple ring, convex or
concave) plus the 4 midpoints of the parcel's own bounding-box edges,
each kept only if it also passes the same interior ray-cast test. The
representative point is unconditional (never tested, always kept),
which is what makes the minimum sample count exactly 1 rather than 0:
nothing here ever classifies a parcel from zero samples, and nothing
here divides by a sample count that could be zero.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from mapgen.buildings import CELL_SIZE_DEGREES, point_in_ring, representative_point

Ring = list[tuple[float, float]]

# --------------------------------------------------------------------------
# Metric conversion. A sampling density choice, not a survey-grade
# transform: see the module docstring's "Sampling" section.
# --------------------------------------------------------------------------

_METRES_PER_DEGREE_LAT = 111_320.0


def _metres_per_degree_lon(latitude_deg: float) -> float:
    return _METRES_PER_DEGREE_LAT * math.cos(math.radians(latitude_deg))


# --------------------------------------------------------------------------
# The overlay container.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OverlaySets:
    """Every overlay layer a parcel is classified against, WGS84 [lon, lat]
    exterior rings only: a hole on any source polygon (an Overture
    land_use MultiPolygon's own interior ring, an OS Open Greenspace site
    with a courtyard) is ignored entirely, never represented, matching
    `buildings.py`'s own "holes dropped" rule for a MultiPolygon
    candidate (see that module's docstring). Building this container from
    the package's own overlay files (loading each optional GeoJSON,
    falling back to an empty list when a file is absent) is Task 3's job,
    not this module's: `classify.py` only ever consumes an already-built
    `OverlaySets`.

    `landuse` is a list of `(class_name, ring)` pairs, `class_name` being
    the raw Overture `land_use` class value (`"residential"`,
    `"farmland"`, and so on) exactly as the source data spells it, not
    yet mapped to a final category: that mapping is this module's own job
    (`_LANDUSE_BUCKET_BY_CLASS` below), done once the majority race has a
    winning raw class to look up, not before.
    """

    buildings: list[Ring]
    landuse: list[tuple[str, Ring]]
    water: list[Ring]
    greenspace: list[Ring]
    woodland: list[Ring]


# --------------------------------------------------------------------------
# The category vocabulary, the mapping table, and the priority order.
# Spec-exact per task-2-rules.md; see that file for the pinned wording
# this module implements rather than paraphrases.
# --------------------------------------------------------------------------

# Every evidence bucket a sample point's hits can be tagged with, EXCEPT
# `building` (the housing gate, not a race participant: see the module
# docstring). Order is the tie-break priority: task-2-rules.md's rule 2
# mapping table, in its own listed order, then rule 3 (water), rule 4
# (the OS Open Greenspace side of `greenspace`), then rule 5 (woodland).
PRIORITY_ORDER: tuple[str, ...] = (
    "garden",
    "field",
    "recreation",
    "retail",
    "industrial",
    "education",
    "religious",
    "allotments",
    "greenspace_landuse",
    "water",
    "greenspace_os",
    "woodland",
)

# The bucket a `garden` majority becomes when at least one sample also
# hits a fused building footprint (task-2-rules.md rule 1). Not itself a
# member of PRIORITY_ORDER: `housing` is never the winner of the count
# race, only a re-labelling of a `garden` win.
_HOUSING_BUCKET = "garden"
_HOUSING_CATEGORY = "housing"

_BUILDING_TAG = "building"

# task-2-rules.md rule 2's own mapping table, spelled out one raw
# Overture land_use class per entry (never grouped by list, so a typo in
# one class name cannot silently swallow its neighbours). Any class not
# a key here is, per the rules file, "treated as no land-use evidence":
# `_flat_candidates` below simply never emits a bucket for it, which is
# what keeps it out of the majority race entirely rather than losing a
# race it was silently entered into.
_LANDUSE_BUCKET_BY_CLASS: dict[str, str] = {
    "residential": "garden",
    "farmland": "field",
    "farmyard": "field",
    "meadow": "field",
    "pitch": "recreation",
    "park": "recreation",
    "playground": "recreation",
    "recreation_ground": "recreation",
    "recreation": "recreation",
    "retail": "retail",
    "industrial": "industrial",
    "quarry": "industrial",
    "school": "education",
    "education": "education",
    "religious": "religious",
    "cemetery": "religious",
    "allotments": "allotments",
    "grass": "greenspace_landuse",
}

_FINAL_CATEGORY_BY_BUCKET: dict[str, str] = {
    "garden": "garden",
    "field": "field",
    "recreation": "recreation",
    "retail": "retail",
    "industrial": "industrial",
    "education": "education",
    "religious": "religious",
    "allotments": "allotments",
    "greenspace_landuse": "greenspace",
    "water": "water",
    "greenspace_os": "greenspace",
    "woodland": "woodland",
}

UNCLASSIFIED = "unclassified"


# --------------------------------------------------------------------------
# Parcel area (for the sampling spacing formula only) and the sampling
# grid itself.
# --------------------------------------------------------------------------


def _open_ring(ring: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """`ring`'s own vertices with a repeated closing point dropped, if it
    has one, so a Task 1 parcel ring (explicitly closed, first == last)
    and a synthetic test ring (never closed) both feed the area formula
    and the grid construction below with the same distinct-vertex count.
    Small, local copy of `buildings._drop_closing_duplicate`'s own logic:
    not imported, since the brief promotes only `representative_point`
    and `point_in_ring` out of that module for this one.
    """
    points = list(ring)
    if len(points) > 1 and points[0] == points[-1]:
        return points[:-1]
    return points


def _bbox(ring: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [x for x, _y in ring]
    ys = [y for _x, y in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _parcel_area_m2(points: Sequence[tuple[float, float]], lat0: float) -> float:
    """The shoelace formula over `points`, projected to local metres with
    the same cos-latitude factor the sampling spacing itself uses (see
    the module docstring): only ever needed to size a sampling grid, not
    to certify a legal area, so this deliberately reuses the sampling
    grid's own approximation rather than a second, more careful one.

    `points` must already be the OPEN vertex list (`_open_ring`); fewer
    than 3 distinct vertices is not a real polygon and reads as zero
    area, which the caller's own `clamp(..., 2.0, 20.0)` floors to the
    minimum spacing rather than needing a special case here.
    """
    if len(points) < 3:
        return 0.0
    m_per_deg_lon = _metres_per_degree_lon(lat0)
    metres = [(x * m_per_deg_lon, y * _METRES_PER_DEGREE_LAT) for x, y in points]
    total = 0.0
    count = len(metres)
    for index in range(count):
        x1, y1 = metres[index]
        x2, y2 = metres[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


_SPACING_MIN_M = 2.0
_SPACING_MAX_M = 20.0
_SPACING_AREA_DIVISOR = 8.0
_MIN_GRID_SAMPLES = 4

# task-2-review.md's own Important finding: `clamp(..., 2.0, 20.0)` bounds
# sample DENSITY, not sample COUNT. Past ~2.56 ha the spacing ceiling (20m)
# stops scaling with area, so sample count grows unbounded with area: the
# review measured 19,119,210 samples in ~10s for a 1deg x 1deg ring (this
# module's own test reproduces that exact shape). A realistic large single
# INSPIRE title (10 km2, a genuinely large agricultural/estate parcel)
# needs only ~25,000 samples; 40,000 is comfortably above that, and still
# small enough to classify in well under a second, which is what this
# constant documents rather than any survey-grade sample-density claim.
# `_grid_spacing_m`'s caller widens spacing toward this cap when a bbox is
# large enough to need it (see `_sample_points`); `_grid_samples` itself
# also stops outright at this many interior points, which is what actually
# guarantees the bound (the widened spacing alone approximates it well for
# a roughly square bbox, but does not itself bound an extreme, elongated
# aspect ratio a malformed ring could produce).
SAMPLE_CAP = 40_000


def _grid_spacing_m(area_m2: float) -> float:
    """task-2-rules.md's own `clamp(sqrt(parcel_area_m2) / 8, 2.0, 20.0)`,
    verbatim: `math.sqrt(0.0)` is `0.0`, not an error, so a degenerate
    (zero-area) ring floors to the minimum spacing here rather than
    raising; it will fail the grid's own `_MIN_GRID_SAMPLES` floor a
    moment later and fall to the representative-point sampling instead.
    """
    return min(_SPACING_MAX_M, max(_SPACING_MIN_M, math.sqrt(area_m2) / _SPACING_AREA_DIVISOR))


def _bbox_area_m2(bbox: tuple[float, float, float, float], lat0: float) -> float:
    """`bbox`'s own area in local square metres, the same cos-latitude
    projection everywhere else in this module uses: not the parcel's own
    (possibly much smaller) area `_parcel_area_m2` computes, but the
    bounding box the grid actually scans over, which is what the sample
    COUNT (rather than density) is bounded against in `_sample_points`.
    """
    min_x, min_y, max_x, max_y = bbox
    width_m = (max_x - min_x) * _metres_per_degree_lon(lat0)
    height_m = (max_y - min_y) * _METRES_PER_DEGREE_LAT
    return width_m * height_m


def _grid_samples(
    ring: Sequence[tuple[float, float]],
    bbox: tuple[float, float, float, float],
    spacing_m: float,
    lat0: float,
) -> list[tuple[float, float]]:
    """Every point of a square grid (in local metres, converted to a
    lon/lat step by the same cos-latitude factor everywhere else in this
    module uses it) over `bbox` that also lies inside `ring` itself
    (`point_in_ring`, the ray cast `buildings.py` already promoted for
    this): the "square grid... keeping only points inside the parcel
    ring" the rules file specifies, in that order.

    Stops outright once `SAMPLE_CAP` interior points have been collected,
    the hard guarantee behind that constant's own docstring: the caller
    widens `spacing_m` first so this stop is rarely the acting mechanism
    for an ordinary oversized-but-proportioned ring, but it is what
    actually bounds both the returned count and the wall time for a
    ring whose bbox is large in one dimension and tiny in the other (an
    antimeridian wraparound, a coordinate-order bug), where the area-based
    widening alone would under-shrink one axis while over-shrinking
    nothing about the other.
    """
    min_x, min_y, max_x, max_y = bbox
    dx = spacing_m / _metres_per_degree_lon(lat0)
    dy = spacing_m / _METRES_PER_DEGREE_LAT
    cols = int(math.floor((max_x - min_x) / dx)) + 1 if dx > 0 else 1
    rows = int(math.floor((max_y - min_y) / dy)) + 1 if dy > 0 else 1
    points: list[tuple[float, float]] = []
    for row in range(rows):
        y = min_y + row * dy
        for col in range(cols):
            x = min_x + col * dx
            if point_in_ring(x, y, ring):
                points.append((x, y))
                if len(points) >= SAMPLE_CAP:
                    return points
    return points


def _fallback_samples(
    ring: Sequence[tuple[float, float]], bbox: tuple[float, float, float, float]
) -> list[tuple[float, float]]:
    """The "too small for the grid" fallback: `representative_point`
    (always kept, always interior, so this list is never empty) plus the
    4 midpoints of `ring`'s own bounding-box edges, each kept only if it
    also passes the interior ray cast (the rules file's own "clipped to
    inside"). Minimum length 1, maximum 5.
    """
    min_x, min_y, max_x, max_y = bbox
    mid_x = (min_x + max_x) / 2.0
    mid_y = (min_y + max_y) / 2.0
    edge_midpoints = (
        (mid_x, min_y),
        (mid_x, max_y),
        (min_x, mid_y),
        (max_x, mid_y),
    )
    points = [representative_point(ring)]
    for x, y in edge_midpoints:
        if point_in_ring(x, y, ring):
            points.append((x, y))
    return points


def _sample_points(ring: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parcel's own interior sample points: the grid path when it
    catches at least `_MIN_GRID_SAMPLES`, the representative-point
    fallback otherwise. Never returns an empty list for any real
    geometry (see `_fallback_samples`'s own docstring), which is what
    keeps every later majority computation free of a division by a zero
    sample count.

    A genuinely empty `ring` (`[]`) is the one input that is not real
    geometry at all: `_bbox` would otherwise crash on `min()`/`max()` of
    an empty sequence (task-2-review.md's own Minor finding). Returning
    `[]` here, before `_bbox` is ever called, is the only way this
    function returns zero samples; `classify_parcel`/`_classify_samples`
    read a zero-sample list as `unclassified` (no evidence, honestly,
    since there was no geometry to sample in the first place), the same
    zero-hits path an empty `OverlaySets` already takes. Not reachable
    from a real Task 1 parcel ring (an INSPIRE polygon always has real
    area and at least 3 vertices); defensive only.
    """
    open_points = _open_ring(ring)
    if not open_points:
        return []
    bbox = _bbox(open_points)
    lat0 = (bbox[1] + bbox[3]) / 2.0
    area_m2 = _parcel_area_m2(open_points, lat0)
    spacing_m = _grid_spacing_m(area_m2)
    # Widen spacing toward the point where the FULL bbox grid (not just
    # the parcel's own smaller area) would land near SAMPLE_CAP: this is
    # the density-based approximation of the cap; `_grid_samples`'s own
    # hard stop at SAMPLE_CAP is what actually guarantees it (see that
    # constant's own docstring for why the two are both needed).
    bbox_area_m2 = _bbox_area_m2(bbox, lat0)
    if bbox_area_m2 > 0:
        spacing_m = max(spacing_m, math.sqrt(bbox_area_m2 / SAMPLE_CAP))
    grid_points = _grid_samples(ring, bbox, spacing_m, lat0)
    if len(grid_points) >= _MIN_GRID_SAMPLES:
        return grid_points
    return _fallback_samples(ring, bbox)


# --------------------------------------------------------------------------
# Evidence buckets: turning OverlaySets into (bucket, ring) pairs, and
# turning a sample point into the set of buckets it hits.
# --------------------------------------------------------------------------

_Candidate = tuple[str, Ring]


def _flat_candidates(overlays: OverlaySets) -> list[_Candidate]:
    """Every `(bucket, ring)` pair this classifier will ever ray-cast a
    sample point against, flattened out of `overlays`. A raw `landuse`
    class absent from `_LANDUSE_BUCKET_BY_CLASS` contributes no entry at
    all here, which is what keeps it out of the majority race (see the
    module docstring).
    """
    candidates: list[_Candidate] = [(_BUILDING_TAG, ring) for ring in overlays.buildings]
    for class_name, ring in overlays.landuse:
        bucket = _LANDUSE_BUCKET_BY_CLASS.get(class_name)
        if bucket is not None:
            candidates.append((bucket, ring))
    candidates.extend(("water", ring) for ring in overlays.water)
    candidates.extend(("greenspace_os", ring) for ring in overlays.greenspace)
    candidates.extend(("woodland", ring) for ring in overlays.woodland)
    return candidates


def _buckets_hit_at_point(point: tuple[float, float], candidates: Iterable[_Candidate]) -> set[str]:
    """Every distinct bucket among `candidates` whose own ring contains
    `point` (the ray cast `buildings.py` promoted for this), deduplicated
    per bucket so two overlapping rings of the SAME class hitting one
    point never count as two hits for it.
    """
    x, y = point
    hits: set[str] = set()
    for bucket, ring in candidates:
        if bucket in hits:
            continue
        if point_in_ring(x, y, ring):
            hits.add(bucket)
    return hits


def _majority_bucket(counts: dict[str, int]) -> str | None:
    """The bucket in PRIORITY_ORDER holding the most samples, ties
    (equal counts) broken by walking PRIORITY_ORDER itself and only ever
    replacing the current leader on a STRICTLY greater count: the
    earliest-listed bucket among however many share the top count is
    what survives, which is the rules file's "ties... the priority order
    breaks them" for every majority this module computes. None if every
    bucket has zero hits (no evidence at all).
    """
    winner: str | None = None
    best = 0
    for bucket in PRIORITY_ORDER:
        count = counts.get(bucket, 0)
        if count > best:
            winner = bucket
            best = count
    return winner


def _classify_samples(
    samples: Sequence[tuple[float, float]],
    candidates_for_point: Callable[[tuple[float, float]], Iterable[_Candidate]],
) -> tuple[str, int]:
    """The one decision procedure both `classify_parcel` and
    `classify_parcels` run: tally evidence-bucket hits over `samples`,
    find the majority (ties broken by PRIORITY_ORDER), and apply the
    housing gate. `candidates_for_point` is the only thing that differs
    between the two public functions (the full flat list for the
    singular reference implementation, a shared spatial hash's per-cell
    lookup for the plural batch path); everything else, including this
    function, is shared code, which is what makes "plural agrees with
    singular" a property of the code rather than a hope.
    """
    counts: dict[str, int] = {}
    building_hit = False
    for point in samples:
        hits = _buckets_hit_at_point(point, candidates_for_point(point))
        if _BUILDING_TAG in hits:
            building_hit = True
        for bucket in hits:
            if bucket == _BUILDING_TAG:
                continue
            counts[bucket] = counts.get(bucket, 0) + 1

    winner = _majority_bucket(counts)
    if winner == _HOUSING_BUCKET and building_hit:
        category = _HOUSING_CATEGORY
    elif winner is not None:
        category = _FINAL_CATEGORY_BY_BUCKET[winner]
    else:
        category = UNCLASSIFIED
    return category, len(samples)


def classify_parcel(ring: Ring, overlays: OverlaySets) -> tuple[str, int]:
    """`(category, sample_count)` for one parcel `ring` against
    `overlays`: the readable reference implementation, ray-casting every
    sample point against every overlay ring directly (no spatial hash).
    `classify_parcels` (plural) must agree with this exactly over any
    input; see `test_classify_parcels_agrees_with_classify_parcel_over_a_mixed_fixture`.
    """
    samples = _sample_points(ring)
    candidates = _flat_candidates(overlays)
    return _classify_samples(samples, lambda _point: candidates)


# --------------------------------------------------------------------------
# classify_parcels: one shared spatial hash over the overlay rings.
# --------------------------------------------------------------------------


def _cell(x: float, y: float) -> tuple[int, int]:
    return (math.floor(x / CELL_SIZE_DEGREES), math.floor(y / CELL_SIZE_DEGREES))


def _cells_for_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Every grid cell `bbox` reaches into, inclusive of both corners:
    the identical convention `buildings._cells_for_bbox` uses (a small,
    local copy, not an import, since the brief names only
    `representative_point` and `point_in_ring` for reuse out of that
    module), so a ring registered here is found by a query point
    anywhere inside its bounding box, not only near its own centroid.

    Never called on a ring `_cell_span` has already flagged as GIANT (see
    `_OverlayIndex.__init__`): building this list at all is the cost that
    needed avoiding, not merely iterating it once built.
    """
    min_x, min_y, max_x, max_y = bbox
    col_min, row_min = _cell(min_x, min_y)
    col_max, row_max = _cell(max_x, max_y)
    return [(col, row) for row in range(row_min, row_max + 1) for col in range(col_min, col_max + 1)]


# A package-review finding against the owner's own real Cowbridge data:
# Overture's water layer is not clipped to the query bbox, so a
# `class: "sea"` feature arrives as its own real-world polygon merely
# intersecting the survey extent, not cut down to it. One such feature,
# 19,902 vertices, had a bounding box of 12.63 deg x 10.05 deg;
# `_cells_for_bbox` alone enumerated 507,817,242 `CELL_SIZE_DEGREES`
# cells for that ONE ring (measured directly, ~35s and on the order of
# 40 GB just for the returned list), and feeding it through
# `_OverlayIndex.__init__` unmodified never completed at all (killed
# after ~6 minutes of climbing memory).
#
# `GIANT_RING_CELLS` is the line a ring's own cell SPAN (`_cell_span`,
# columns times rows, computed from its two corner cells without ever
# enumerating the cells themselves) must cross before this module
# refuses to bucket it at all: past this many cells, the ring joins
# `_OverlayIndex`'s own overflow list instead (see that class), tested
# against every sample point directly rather than through the grid.
# 10,000 cells is 100 x 100 at `CELL_SIZE_DEGREES` (0.0005 deg, ~55 m),
# an approximately 5.5 km square: comfortably larger than any real
# overlay feature this classifier is meant to bucket finely (a field, an
# estate, a park), so no ordinary land_use, greenspace or woodland
# polygon this project has seen in real data crosses it, while a
# continent- or sea-scale feature crosses it by many orders of
# magnitude. The real overflow SET this produces in practice is tiny by
# ring COUNT (the real Cowbridge package's own `_water.geojson` has 4 of
# 96 features cross this line, all coastline/sea-scale), which is what
# this fix's own correctness and MEMORY bound rest on: `_by_cell` never
# grows past what ordinary, small overlay features put there, whatever a
# giant ring's own bbox is. Ring count alone does not bound WALL TIME,
# though, and this is measured, not assumed: those same 4 real rings
# carry 121 + 12,833 + 102 + 19,902 = 32,958 vertices between them, each
# tested by a full `point_in_ring` ray cast against every sample point
# whose OWN bbox pre-check (`_OverlayIndex.near`) cannot reject it, which
# it rarely can for a feature whose bbox already covers most of Great
# Britain. Run end to end over the real, un-defanged Cowbridge package
# (204,023 samples, task-3-report.md's own addendum), this step measured
# 283.27 seconds, against 6.25 seconds for the same package with those 4
# features removed: correct and BOUNDED (it finishes, in bounded memory,
# where the unfixed code never did), but not fast, when the overflow set
# is this vertex-heavy. A further optimisation (simplifying an overflow
# ring's own vertex count before it is ever ray-cast, which this
# classifier's own "indicative, not survey-grade" standard would tolerate
# readily) is a real option for a later task; it is not implemented here,
# since fixing the correctness/memory defect is this fix's own scope and
# the wall time, while worse than the defanged baseline, is bounded and
# was the number this fix was asked to report, not to optimise further.
GIANT_RING_CELLS = 10_000


def _cell_span(bbox: tuple[float, float, float, float]) -> int:
    """How many `CELL_SIZE_DEGREES` cells `bbox` reaches into, WITHOUT
    ever enumerating them: the same two corner cells `_cells_for_bbox`
    itself computes, multiplied rather than expanded into a list. This is
    what lets a ring's own bbox be recognised as GIANT (see
    `GIANT_RING_CELLS`) before a single cell entry is built, which is the
    actual cost that needed avoiding: `_cells_for_bbox`'s own list is the
    507-million-entry object this function exists so nothing ever
    constructs.
    """
    min_x, min_y, max_x, max_y = bbox
    col_min, row_min = _cell(min_x, min_y)
    col_max, row_max = _cell(max_x, max_y)
    return (col_max - col_min + 1) * (row_max - row_min + 1)


class _OverlayIndex:
    """Every `(bucket, ring)` candidate `_flat_candidates` can produce,
    bucketed by `CELL_SIZE_DEGREES` grid cell (imported from
    `buildings.py` rather than restated, so the two modules' idea of
    "how coarse is a cell" can never drift apart): built ONCE per
    `classify_parcels` call and shared across every parcel it classifies,
    which is the whole point of the plural entry point (N parcels do not
    each rebuild this).

    A ring whose own bbox spans more than `GIANT_RING_CELLS` cells (see
    that constant's own docstring for the real, executed Cowbridge
    finding this responds to) is never bucketed at all: it joins
    `_overflow` instead, a plain list every query tests directly, which
    is what keeps this class's own memory bounded regardless of how
    large a single overlay ring's bbox is. Correctness is unaffected
    either way: every sample point is still ray-cast against every
    overlay ring that could plausibly contain it, whether that ring was
    found through a cell lookup or through this fallback list.
    """

    def __init__(self, candidates: Iterable[_Candidate]) -> None:
        self._by_cell: dict[tuple[int, int], list[_Candidate]] = defaultdict(list)
        # (bucket, ring, bbox) triples, not bare (bucket, ring) pairs:
        # `near`'s own bbox pre-check (see its docstring) needs the bbox
        # too, and every candidate here is rare enough in practice that
        # recomputing it per query, rather than storing it once here,
        # would be the wrong place to economise.
        self._overflow: list[tuple[str, Ring, tuple[float, float, float, float]]] = []
        for bucket, ring in candidates:
            bbox = _bbox(ring)
            if _cell_span(bbox) > GIANT_RING_CELLS:
                self._overflow.append((bucket, ring, bbox))
                continue
            for cell in _cells_for_bbox(bbox):
                self._by_cell[cell].append((bucket, ring))

    def near(self, point: tuple[float, float]) -> list[_Candidate]:
        """Every candidate `point` could plausibly hit: its own grid
        cell's own bucketed candidates, plus every overflow candidate
        whose own bbox actually contains `point` (a plain, four-
        comparison rectangle test, cheap enough to run unconditionally
        and skip the full ray cast for any overflow ring `point` cannot
        possibly be inside, which is every ordinary query against a
        real overlay's own overflow set: a giant ring's bbox usually
        covers far more ground than any one survey extent, so this
        rarely rejects anything for THAT specific shape, but it is a
        correct, cheap filter for any smaller overflow candidate whose
        bbox point genuinely falls outside, and it costs nothing when
        `_overflow` is empty, the ordinary case).
        """
        local = self._by_cell.get(_cell(point[0], point[1]))
        candidates = list(local) if local is not None else []
        if not self._overflow:
            return candidates
        x, y = point
        for bucket, ring, bbox in self._overflow:
            min_x, min_y, max_x, max_y = bbox
            if min_x <= x <= max_x and min_y <= y <= max_y:
                candidates.append((bucket, ring))
        return candidates


# --------------------------------------------------------------------------
# Round 2 of the giant-ring fix (task-3-review.md): clipping every overlay
# candidate to the parcels' own padded extent BEFORE it ever reaches
# `_OverlayIndex`, so a real, un-clipped continent-scale ring (Overture's
# own "sea" feature, the review's own reproduction) shrinks to the small
# sliver of it that could ever matter to THIS run, rather than depending
# on `GIANT_RING_CELLS`'s own overflow list (still kept, as a second line
# of defence: see that constant's own docstring) to survive it cheaply.
# Measured on the real, un-defanged Cowbridge package: round 1's fix
# completed in 283.27s; this round's own clip, over the identical
# package, completes in single-digit seconds (see this module's own test
# suite and task-3-report.md's own fix-round addendum for the exact
# number), because the four real giant rings each shrink from tens of
# thousands of vertices to a short coastline fragment, or to nothing at
# all, before a single sample is ever tested against them.
# --------------------------------------------------------------------------

# The maximum sampling spacing this module's own grid ever uses
# (`_SPACING_MAX_M`) plus a small, fixed safety margin: the padding
# `_parcels_clip_window` adds around the parcels' own bbox, in metres,
# before converting to degrees. See that function's own docstring for
# why padding is a SAFETY margin, not a correctness requirement.
_CLIP_PADDING_EPSILON_M = 1.0


def _parcels_clip_window(rings: Sequence[Ring]) -> tuple[float, float, float, float] | None:
    """The axis-aligned bbox `classify_parcels` clips every overlay
    candidate ring to before indexing them: the union of every ring in
    `rings`, padded by `_SPACING_MAX_M + _CLIP_PADDING_EPSILON_M` metres
    (21 m) on every side, converted to degrees with the same cos-latitude
    factor `_sample_points`'s own grid spacing already uses, evaluated
    ONCE at the collective bbox's own mid-latitude (never per-parcel: a
    single shared window is what lets one clipped candidate list serve
    every parcel `classify_parcels` classifies in one call).

    `None` for an empty `rings`: `classify_parcels` is never actually
    called this way in practice (there would be nothing to classify),
    but this gives that case a total, honest answer rather than crashing
    on `min()`/`max()` of an empty sequence, the same defensive standard
    `_sample_points`'s own empty-ring branch already holds.

    Padding is a SAFETY margin, not what makes the clip correct: for any
    point p strictly inside the (unpadded) union bbox, "p in ring" and "p
    in ring INTERSECT bbox" already agree (see `_clip_overlay_candidates`'s
    own docstring for the full argument, which needs no padding at all).
    What padding buys is keeping every real sample point comfortably
    inside the window's own INTERIOR, never within a hair's breadth of
    its boundary, which is where a Sutherland-Hodgman seam artifact (see
    `_clip_overlay_candidates`'s own "Honest caveat") could in principle
    live. `_SPACING_MAX_M` is the largest distance `_sample_points` ever
    puts between a parcel's own edge and its nearest interior grid
    sample, so padding by at least that much guarantees no real sample
    point is ever born within the margin; the extra fixed metre is
    headroom against the cos-latitude conversion's own rounding, nothing
    more.
    """
    all_points = [point for ring in rings for point in ring]
    if not all_points:
        return None
    xs = [point[0] for point in all_points]
    ys = [point[1] for point in all_points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    lat0 = (min_y + max_y) / 2.0
    pad_m = _SPACING_MAX_M + _CLIP_PADDING_EPSILON_M
    pad_lon = pad_m / _metres_per_degree_lon(lat0)
    pad_lat = pad_m / _METRES_PER_DEGREE_LAT
    return (min_x - pad_lon, min_y - pad_lat, max_x + pad_lon, max_y + pad_lat)


def _sutherland_hodgman_pass(
    polygon: list[tuple[float, float]],
    inside: Callable[[tuple[float, float]], bool],
    intersect: Callable[[tuple[float, float], tuple[float, float]], tuple[float, float]],
) -> list[tuple[float, float]]:
    """One Sutherland-Hodgman clip pass: `polygon`'s own vertices, kept
    only on the `inside` side of ONE half-plane, with a new vertex
    spliced in at every edge that crosses it (`intersect`). Walking
    `polygon`'s own edges (including the wraparound from its last vertex
    back to its first, `prev` starting at `polygon[-1]`) and classifying
    each edge by its two endpoints' own `inside` status is the whole
    algorithm for one half-plane; `_clip_ring_to_bbox` runs this four
    times, once per side of an axis-aligned rectangle, which is what
    makes the full clip exact against ANY simple polygon, convex or
    concave (each pass alone is exact against a single half-plane
    regardless of the subject's own shape; only the CLIP window's own
    convexity matters for Sutherland-Hodgman, and a rectangle is always
    convex).
    """
    if not polygon:
        return []
    output: list[tuple[float, float]] = []
    prev = polygon[-1]
    prev_inside = inside(prev)
    for point in polygon:
        point_inside = inside(point)
        if point_inside:
            if not prev_inside:
                output.append(intersect(prev, point))
            output.append(point)
        elif prev_inside:
            output.append(intersect(prev, point))
        prev, prev_inside = point, point_inside
    return output


def _clip_ring_to_bbox(ring: Ring, bbox: tuple[float, float, float, float]) -> Ring | None:
    """`ring` clipped to `bbox` (`min_x, min_y, max_x, max_y`) by four
    Sutherland-Hodgman passes (`_sutherland_hodgman_pass`), one per
    rectangle side, in `left, right, bottom, top` order (the order does
    not affect the result, only which intermediate polygon each pass
    sees).

    Returns None for a ring that clips away to fewer than 3 points (lies
    entirely outside `bbox`, or is reduced to a degenerate sliver at its
    boundary): the caller drops such a ring entirely, the identical
    "not a real polygon any more" standard `_normalise_ring`
    (`boundary_curves.py`) and `_exterior_ring` (`buildings.py`) already
    hold a ring to elsewhere in this project.
    """
    min_x, min_y, max_x, max_y = bbox
    polygon: list[tuple[float, float]] = list(ring)

    polygon = _sutherland_hodgman_pass(
        polygon,
        inside=lambda p: p[0] >= min_x,
        intersect=lambda a, b: (
            min_x,
            a[1] + (b[1] - a[1]) * (min_x - a[0]) / (b[0] - a[0]),
        ),
    )
    polygon = _sutherland_hodgman_pass(
        polygon,
        inside=lambda p: p[0] <= max_x,
        intersect=lambda a, b: (
            max_x,
            a[1] + (b[1] - a[1]) * (max_x - a[0]) / (b[0] - a[0]),
        ),
    )
    polygon = _sutherland_hodgman_pass(
        polygon,
        inside=lambda p: p[1] >= min_y,
        intersect=lambda a, b: (
            a[0] + (b[0] - a[0]) * (min_y - a[1]) / (b[1] - a[1]),
            min_y,
        ),
    )
    polygon = _sutherland_hodgman_pass(
        polygon,
        inside=lambda p: p[1] <= max_y,
        intersect=lambda a, b: (
            a[0] + (b[0] - a[0]) * (max_y - a[1]) / (b[1] - a[1]),
            max_y,
        ),
    )
    if len(polygon) < 3:
        return None
    return polygon


def _clip_overlay_candidates(
    candidates: Iterable[_Candidate], window: tuple[float, float, float, float]
) -> list[_Candidate]:
    """Every `(bucket, ring)` candidate `_flat_candidates` produced, each
    ring clipped to `window` (`_parcels_clip_window`) via
    `_clip_ring_to_bbox`; a ring that clips away to nothing is dropped
    outright (contributes no evidence, matching every other "not a real
    polygon" ring this project already refuses rather than mishandles).

    **Correctness, by construction.** `window` is built to contain every
    sample point `classify_parcels` will ever test against the result
    (`_parcels_clip_window`'s own docstring): each sample is generated by
    `_sample_points` strictly inside some parcel ring, and every parcel
    ring `classify_parcels` is ever called with is one `window` was built
    from. Take any point p strictly inside `window`, and any ring R this
    function clips to R' = R INTERSECT window:
      * If p is in R, then p is in R (given) and p is in window (given),
        so p is in R INTERSECT window, i.e. p is in R'.
      * If p is in R', then p is in R INTERSECT window, which means p is
        in R (one of the two things being intersected).
    So "p in R" and "p in R'" are the same fact for every p this module
    will ever ask about, which is exactly the property `_OverlayIndex`'s
    own ray casts depend on: classifying against the clipped candidates
    gives IDENTICAL answers to classifying against the originals, for
    every real query, regardless of whether R itself was convex, huge, or
    partly outside `window` to begin with.

    A giant, real-world ring (an Overture "sea" polygon spanning most of
    the planet) clips down to whatever small sliver of it actually
    crosses `window`, typically a short coastline fragment for a real
    survey extent, or nothing at all for an inland one: this is what
    keeps `GIANT_RING_CELLS`'s own overflow path (`_OverlayIndex`) from
    ever engaging on real data once this clip runs; it stays in place as
    a second line of defence for a ring this clip cannot shrink enough
    (one whose own bbox already sits inside `window`) or a case this
    project has not seen in real data yet.

    **Honest caveat on non-convex subjects.** Sutherland-Hodgman clips
    any subject polygon against a CONVEX window correctly in the sense
    the argument above needs (every point's own in/out answer is
    preserved), but a concave subject can produce a result with a
    degenerate "seam" edge running along `window`'s own boundary, where
    the algorithm stitches two separate visible pieces of the original
    ring back into one output loop (a real, well-known property of the
    algorithm, not a bug in this implementation: see this module's own
    test suite for a worked concave case). This never changes an
    INTERIOR point's own classification: the even-odd ray cast
    (`point_in_ring`) still answers correctly for any point not exactly
    ON that seam, and a real grid sample sits exactly on a seam only if
    it sits exactly on `window`'s own boundary, which `_parcels_clip_
    window`'s own padding margin makes unreachable by any real sample
    point at all.
    """
    clipped: list[_Candidate] = []
    for bucket, ring in candidates:
        result = _clip_ring_to_bbox(ring, window)
        if result is not None:
            clipped.append((bucket, result))
    return clipped


def _build_overlay_index(rings: Sequence[Ring], overlays: OverlaySets) -> _OverlayIndex:
    """The one shared spatial hash `classify_parcels` builds and uses,
    factored out on its own: every overlay candidate `overlays` produces
    (`_flat_candidates`), clipped to the parcels' own padded extent
    (`_parcels_clip_window`, `_clip_overlay_candidates`) before it is
    ever bucketed, then handed to `_OverlayIndex`.

    A caller (this module's own test suite, in particular) can inspect
    the returned index directly, which is what makes `_OverlayIndex.
    _overflow`'s own length after this call the read-only signal for
    "did the clip do its job": a real overlay ring should clip down to
    something small enough that it never needs `GIANT_RING_CELLS`'s own
    overflow path at all once this function is what builds the index,
    which task-3-review.md's own round 2 asks to see proven rather than
    assumed.
    """
    window = _parcels_clip_window(rings)
    candidates = _flat_candidates(overlays)
    if window is not None:
        candidates = _clip_overlay_candidates(candidates, window)
    return _OverlayIndex(candidates)


def classify_parcels(rings: list[Ring], overlays: OverlaySets) -> list[tuple[str, int]]:
    """`(category, sample_count)` per ring in `rings`, in order, against
    ONE shared spatial hash built over `overlays`' own rings, clipped to
    the parcels' own padded extent first (`_build_overlay_index`; the
    plan header's "one shared spatial hash... so N parcels do not rebuild
    it N times"). Agrees with `classify_parcel` exactly for every ring:
    see `_classify_samples`'s own docstring for why that is a property of
    the shared decision code, not of this function's own lookup source,
    and `_clip_overlay_candidates`'s own docstring for why clipping the
    index's own candidates first changes no classification answer either.
    """
    index = _build_overlay_index(rings, overlays)
    results: list[tuple[str, int]] = []
    for ring in rings:
        samples = _sample_points(ring)
        results.append(_classify_samples(samples, index.near))
    return results
