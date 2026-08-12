"""Comparing an OS OpenMap Local extract against this project's own output.

Pure comparison machinery for the OS benchmark (plan item F): no network, no
disk, no `random` anywhere in this module. Everything here answers one of
five questions about two independently produced datasets over the same
ground, both already loaded into memory as plain BNG `(easting, northing)`
sequences by whatever caller owns the disk and network side of the
benchmark:

  * How much do a footprint of ours and a footprint of theirs actually
    overlap, as a fraction (`sampled_iou`)?
  * Which of our footprints corresponds to which of theirs, if any
    (`match_footprints`)?
  * For a footprint that matched NOTHING, is it standing on ground the
    other dataset already covers with some other footprint, or on ground
    the other dataset holds nothing at all (`classify_containment`)? This
    is the difference between two datasets DISAGREEING about where the
    building lines fall and one of them being genuinely EMPTY there, and
    a raw unmatched count cannot tell them apart: OS NGD counts building
    PARTS, so a terrace held here as one footprint arrives from them as
    several, every part after the first landing in the unmatched pile
    even though nothing at all is missing from either side.
  * How big are those footprints, bucketed (`ring_area`,
    `area_histogram`)? A gap made of bin stores and garden sheds and a
    gap made of dwellings are the same number and a different problem.
  * Averaged over many matched samples, which direction and how far is our
    survey offset from theirs (`polyline_offsets`)? This is the module's
    own answer to the project's epoch-shift question: OSTN15 (`bng.py`)
    reconciles the two national mapping epochs analytically, and this
    module's `mean_de`/`mean_dn` is the independent, empirical check on
    that reconciliation, computed the opposite way, from real geometry
    rather than a grid transform.

## Sampling, not exact geometry, and why

Every comparison here is a grid of point-in-ring tests, the same standard
this project already applies to parcel classification (`classify.py`'s own
module docstring: "a sampled majority is an honest match to that
precision"): OS OpenMap Local is itself a generalised product, so an exact
polygon-clipping intersection would buy precision neither dataset actually
carries. `sampled_iou` reuses `buildings.point_in_ring` (the ray cast this
project already promoted out of `buildings.py` for exactly this kind of
reuse) rather than reimplementing it.

## Nothing fabricated

`sampled_iou` returns 0.0, not a guess, when its grid never lands inside
either ring. `match_footprints` leaves a feature unmatched rather than
forcing a pairing below `MATCH_IOU_FLOOR`. `classify_containment` answers
only the question it can actually test, whether ONE guaranteed-interior
point of a footprint lands inside another footprint, and never dresses
that up as a claim about overlapping area. `polyline_offsets` counts a
sample with no neighbour inside `search_radius` as `unmatched_samples` and
folds it into no average. `distribution` returns `{}` for no values rather
than inventing a percentile of nothing. Every one of these is the same
"nothing fabricated" rule this project already holds everywhere else
(`heights.py`, `buildings.py`), applied to a comparison instead of a fusion.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from mapgen.buildings import point_in_ring, representative_point
from mapgen.heights import _percentile

Cell = tuple[int, int]
Bbox = tuple[float, float, float, float]

# --------------------------------------------------------------------------
# Shared bbox/cell-index plumbing. A small, local copy of the pattern
# `buildings._SpatialIndex`/`classify._OverlayIndex` both already use (bucket
# by a fixed cell size, register a shape under every cell its bbox touches),
# not an import: both of those operate in WGS84 degrees at their own fixed
# `CELL_SIZE_DEGREES`, while everything in this module is already in real
# BNG metres, so the cell size itself differs by call site
# (`MATCH_CELL_SIZE_M` for footprints, `OFFSET_CELL_SIZE_M` for polyline
# segments) rather than being one shared constant.
# --------------------------------------------------------------------------


def _ring_bbox(ring: Sequence[tuple[float, float]]) -> Bbox:
    xs = [x for x, _ in ring]
    ys = [y for _, y in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _cell(x: float, y: float, cell_size: float) -> Cell:
    return (math.floor(x / cell_size), math.floor(y / cell_size))


def _cells_for_bbox(bbox: Bbox, cell_size: float) -> list[Cell]:
    """Every cell `bbox` reaches into, inclusive of both corners: the same
    "register under every cell the bbox touches" convention
    `buildings._cells_for_bbox` and `classify._cells_for_bbox` both already
    use, so a shape near a cell boundary is found by a query from either
    side of it, not only from the cell its own corner happens to hash to.
    """
    min_x, min_y, max_x, max_y = bbox
    col_min, row_min = _cell(min_x, min_y, cell_size)
    col_max, row_max = _cell(max_x, max_y, cell_size)
    return [
        (col, row)
        for row in range(row_min, row_max + 1)
        for col in range(col_min, col_max + 1)
    ]


def _bboxes_overlap(a: Bbox, b: Bbox) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _bbox_holds(bbox: Bbox, x: float, y: float) -> bool:
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _cell_index(bboxes: Sequence[Bbox], cell_size: float) -> dict[Cell, list[int]]:
    """Every bbox's own index, bucketed under every cell it touches: the
    one shared construction `match_footprints` and `classify_containment`
    both build their candidate lookups on, written once here rather than
    twice at each call site.

    The "register under every cell the bbox touches" convention
    (`_cells_for_bbox`) is what makes a POINT query exhaustive from one
    cell alone: any shape whose bbox holds a given point necessarily
    touches the cell that point falls in, so it is necessarily registered
    there. `classify_containment` relies on exactly that; a bbox
    registered only under its own corner cell would not support it.
    """
    index: dict[Cell, list[int]] = defaultdict(list)
    for position, bbox in enumerate(bboxes):
        for cell in _cells_for_bbox(bbox, cell_size):
            index[cell].append(position)
    return index


# --------------------------------------------------------------------------
# sampled_iou
# --------------------------------------------------------------------------


def _grid_points(bbox: Bbox, step: float):
    """Every point of a `step`-spaced grid covering `bbox`, from its low
    corner up to and including a point at or past the high one: the same
    shape as `heights._grid_points`, in real metres rather than a raster's
    own pixel step, not imported since that function is private to a
    module this one has no other reason to depend on.
    """
    min_e, min_n, max_e, max_n = bbox
    e_count = int(math.floor((max_e - min_e) / step)) + 1
    n_count = int(math.floor((max_n - min_n) / step)) + 1
    for row in range(n_count):
        n = min_n + row * step
        for col in range(e_count):
            e = min_e + col * step
            yield e, n


def _widened_step(bbox: Bbox, step: float, sample_cap: int) -> float:
    """`step`, widened just enough that a grid over `bbox` at that spacing
    never holds more than `sample_cap` points.

    The initial widening is `classify.py`'s own `SAMPLE_CAP` precedent,
    cited rather than re-derived: `_sample_points`'s own
    `spacing_m = max(spacing_m, sqrt(bbox_area_m2 / SAMPLE_CAP))` widens a
    grid spacing by the square root of how far its area-based point count
    sits over a cap. Applied here to a fixed metre step rather than a
    density clamp, since this grid already lives in real BNG metres and has
    no minimum-density floor of its own to respect.

    The area-based estimate alone (`area / step**2`) can still leave the
    true grid a little over `sample_cap`: the two "+1" terms
    `_grid_points`'s own `e_count`/`n_count` add for a partial row or
    column at each edge are invisible to the continuous approximation, and
    for a bbox whose aspect ratio is far from square they can add up. So
    once the sqrt-scaled step is applied, the ACTUAL grid size
    (`_grid_points`'s own counting formula, mirrored in `count_for` below)
    is checked directly, and the step is widened again, by sqrt(2) each
    pass (doubling one grid cell's own area every time), until it truly
    fits: this is what turns the cap into a guarantee ("never sample
    unbounded") rather than merely a close approximation. Only ever a
    couple of passes are needed in practice, since the sqrt scaling above
    already does most of the work.
    """
    min_e, min_n, max_e, max_n = bbox
    width = max(max_e - min_e, 0.0)
    height = max(max_n - min_n, 0.0)

    def count_for(candidate_step: float) -> int:
        cols = int(math.floor(width / candidate_step)) + 1
        rows = int(math.floor(height / candidate_step)) + 1
        return cols * rows

    if count_for(step) <= sample_cap:
        return step

    area = width * height
    if area > 0:
        step = max(step, math.sqrt(area / sample_cap))
    while count_for(step) > sample_cap:
        step *= math.sqrt(2.0)
    return step


def sampled_iou(
    ring_a: Sequence[tuple[float, float]],
    ring_b: Sequence[tuple[float, float]],
    step: float = 0.5,
    sample_cap: int = 20000,
) -> float:
    """The sampled intersection-over-union of `ring_a` and `ring_b`: a grid
    at `step` metres over the union of their two bounding boxes, each point
    tested against both rings with `buildings.point_in_ring`, IoU = (points
    inside both) / (points inside either).

    0.0, not a guess, when the grid never lands inside either ring (an
    empty union bbox, or a step too coarse for either ring's own extent to
    catch a single interior point): "nothing fabricated" applies to a
    comparison exactly as it does to a fusion elsewhere in this project.

    `step` is widened (see `_widened_step`, the `classify.py` `SAMPLE_CAP`
    precedent) rather than honoured past `sample_cap` points: a footprint
    pair whose combined bbox is unexpectedly large (a digitising error, a
    unit mismatch between the two sources) degrades to a coarser sample
    rather than running unbounded.
    """
    bbox_a = _ring_bbox(ring_a)
    bbox_b = _ring_bbox(ring_b)
    union_bbox = (
        min(bbox_a[0], bbox_b[0]),
        min(bbox_a[1], bbox_b[1]),
        max(bbox_a[2], bbox_b[2]),
        max(bbox_a[3], bbox_b[3]),
    )
    step = _widened_step(union_bbox, step, sample_cap)

    in_both = 0
    in_either = 0
    for e, n in _grid_points(union_bbox, step):
        hit_a = point_in_ring(e, n, ring_a)
        hit_b = point_in_ring(e, n, ring_b)
        if hit_a or hit_b:
            in_either += 1
            if hit_a and hit_b:
                in_both += 1

    if in_either == 0:
        return 0.0
    return in_both / in_either


# --------------------------------------------------------------------------
# match_footprints
# --------------------------------------------------------------------------

# Below this sampled IoU, a candidate pair is not trusted as the same
# building at all (a sliver of accidental overlap between two unrelated
# footprints), matching this benchmark's own tolerance for "close enough to
# call it a match" without ever pairing two shapes that merely touch.
MATCH_IOU_FLOOR = 0.1

# The candidate-pair cell index's own cell size: coarse enough that an
# ordinary building footprint (a few to a few dozen metres) sits in one or a
# handful of cells, fine enough that a whole survey extent is spread across
# many buckets rather than one, matching the reasoning
# `buildings.CELL_SIZE_DEGREES`'s own docstring gives for its (degree-based)
# equivalent.
MATCH_CELL_SIZE_M = 50.0


@dataclass
class MatchResult:
    """The outcome of greedily pairing `ours` against `theirs` in
    `match_footprints`.

    `matched` is `(ours_index, theirs_index, iou)` triples, sorted by
    `ours_index` for a deterministic, readable order; `unmatched_ours` and
    `unmatched_theirs` are the indices of every feature on each side that
    found no partner at or above `MATCH_IOU_FLOOR`, in ascending order.
    """

    matched: list[tuple[int, int, float]]
    unmatched_ours: list[int]
    unmatched_theirs: list[int]


def match_footprints(
    ours: Sequence[Sequence[tuple[float, float]]],
    theirs: Sequence[Sequence[tuple[float, float]]],
) -> MatchResult:
    """Pair each of `ours` against at most one of `theirs`, greedily, by
    descending sampled IoU.

    Candidate pairs are found through a `MATCH_CELL_SIZE_M` cell index over
    `theirs`' own bounding boxes (each ring registered under every cell its
    bbox touches, `_cells_for_bbox`'s own convention): only a pair whose
    bounding boxes actually overlap is ever scored with `sampled_iou`, which
    is what keeps this function from running an O(n*m) IoU over every
    possible pair on a survey with thousands of footprints on each side.

    Greedy, best-IoU-first: every candidate pair at or above
    `MATCH_IOU_FLOOR` is sorted by `(-iou, ours_index, theirs_index)` (the
    tie-break is the second and third keys, so the result is identical on
    every run, never dependent on set/dict iteration order), then
    consumed in that order, each side matched at most once. This is a
    standard greedy approximation to maximum-weight bipartite matching, not
    an exact optimum, chosen because an exact assignment (Hungarian
    algorithm) is stdlib-absent machinery this benchmark's own "roughly how
    well do the two datasets agree" purpose does not need: a false-optimal
    pairing at the margin would not change the headline agreement number
    this benchmark reports.
    """
    ours_bboxes = [_ring_bbox(ring) for ring in ours]
    theirs_bboxes = [_ring_bbox(ring) for ring in theirs]

    theirs_index = _cell_index(theirs_bboxes, MATCH_CELL_SIZE_M)

    candidate_pairs: set[tuple[int, int]] = set()
    for i, bbox in enumerate(ours_bboxes):
        seen_j: set[int] = set()
        for cell in _cells_for_bbox(bbox, MATCH_CELL_SIZE_M):
            for j in theirs_index.get(cell, ()):
                if j in seen_j:
                    continue
                seen_j.add(j)
                if _bboxes_overlap(bbox, theirs_bboxes[j]):
                    candidate_pairs.add((i, j))

    scored: list[tuple[int, int, float]] = []
    for i, j in candidate_pairs:
        iou = sampled_iou(ours[i], theirs[j])
        if iou >= MATCH_IOU_FLOOR:
            scored.append((i, j, iou))
    scored.sort(key=lambda pair: (-pair[2], pair[0], pair[1]))

    matched: list[tuple[int, int, float]] = []
    matched_ours: set[int] = set()
    matched_theirs: set[int] = set()
    for i, j, iou in scored:
        if i in matched_ours or j in matched_theirs:
            continue
        matched.append((i, j, iou))
        matched_ours.add(i)
        matched_theirs.add(j)
    matched.sort(key=lambda triple: triple[0])

    unmatched_ours = [i for i in range(len(ours)) if i not in matched_ours]
    unmatched_theirs = [j for j in range(len(theirs)) if j not in matched_theirs]
    return MatchResult(
        matched=matched, unmatched_ours=unmatched_ours, unmatched_theirs=unmatched_theirs
    )


# --------------------------------------------------------------------------
# classify_containment: what an unmatched footprint is actually standing on
# --------------------------------------------------------------------------


@dataclass
class ContainmentResult:
    """Which of `subjects` stand on ground `others` already covers, from
    `classify_containment`.

    `contained` is the indices INTO `subjects` (ascending) whose own
    guaranteed-interior representative point falls inside at least one
    ring of `others`; `not_contained` is every other index. The two lists
    partition `range(len(subjects))` exactly: every subject lands in one
    of them and no subject lands in both, so a caller can add the two
    lengths and get its own input count back, with nothing quietly
    dropped in between.

    Deliberately NEUTRAL names. What "contained" MEANS depends entirely
    on which way round the caller asked the question (one dataset
    subdividing a footprint the other holds whole, or duplicating one,
    or simply drawing it in a different place), and that reading belongs
    to the caller that knows which dataset is which, not to a geometry
    module that only knows two bags of rings.
    """

    contained: list[int]
    not_contained: list[int]


def classify_containment(
    subjects: Sequence[Sequence[tuple[float, float]]],
    others: Sequence[Sequence[tuple[float, float]]],
    cell_size: float = MATCH_CELL_SIZE_M,
) -> ContainmentResult:
    """Split `subjects` by whether each one's own interior point lands
    inside any ring of `others`.

    ## Why a representative point, not a centroid

    The test point per subject is `buildings.representative_point`, the
    scanline label point that module already carries, NOT a vertex
    average: a vertex average is not guaranteed interior to a concave
    ring at all (that function's own docstring, code review task-6-review
    Critical C1: a U-shaped footprint's vertex average falls squarely in
    its own notch, outside the ring entirely). A courtyard block, an
    L-plan house and a terrace with a rear return are all ordinary
    building shapes, so a centroid here would answer a question about a
    point that is not in the building at all, and would do it silently.

    ## Why one point, and what that does and does not prove

    A single interior point inside another footprint is strong evidence
    of the same physical building being described twice with different
    lines, and it is exactly the evidence needed to tell "the other
    dataset splits what we hold whole" from "the other dataset holds
    something here that we do not". It is not, and is not reported as, a
    measurement of overlapping area: `sampled_iou` already exists for
    that, and `match_footprints` has already run it over every candidate
    pair before anything reaches here, which is precisely why the
    subjects that reach here are the ones a sampled IoU could NOT pair
    (see `MATCH_IOU_FLOOR`).

    ## Not quadratic

    `others` is bucketed into a `cell_size` metre cell index over its own
    bounding boxes (`_cell_index`, the same construction
    `match_footprints` uses). A point query then needs exactly ONE cell,
    the one the point itself falls in: any ring whose bbox holds the
    point touches that cell and is therefore registered under it, so
    checking that single bucket misses nothing, and a ring whose bbox
    does NOT hold the point cannot possibly contain it. The bbox is
    re-checked per candidate before the ray cast, so a large ring
    registered under many cells is rejected cheaply rather than ray cast
    against.

    Every ring in `subjects` needs at least 3 points, the same floor both
    of this benchmark's own readers already apply before a ring reaches
    any comparison at all (`benchmark._read_buildings`,
    `benchmark._ngd_building_rings`).
    """
    others_bboxes = [_ring_bbox(ring) for ring in others]
    index = _cell_index(others_bboxes, cell_size)

    contained: list[int] = []
    not_contained: list[int] = []
    for position, ring in enumerate(subjects):
        point_e, point_n = representative_point(ring)
        hit = False
        for other in index.get(_cell(point_e, point_n, cell_size), ()):
            if not _bbox_holds(others_bboxes[other], point_e, point_n):
                continue
            if point_in_ring(point_e, point_n, others[other]):
                hit = True
                break
        if hit:
            contained.append(position)
        else:
            not_contained.append(position)
    return ContainmentResult(contained=contained, not_contained=not_contained)


# --------------------------------------------------------------------------
# ring_area and area_histogram: how big are the footprints in question
# --------------------------------------------------------------------------

# The bucket boundaries, in square metres, and the labels either side of
# them. Chosen as building sizes rather than round numbers: under 10 m2 is
# a bin store, a meter cabinet or a garden shed; 10 to 30 m2 a garage or a
# large outbuilding; 30 to 80 m2 a small dwelling, a terrace part or a flat;
# 80 to 200 m2 an ordinary house; 200 to 1000 m2 a large house, a shop
# terrace read as one, a small commercial unit; over 1000 m2 a school, a
# supermarket, an industrial shed. The point of bucketing at all is that a
# count of unmatched footprints says nothing about whether the gap matters
# and a count PER SIZE says most of it.
AREA_BUCKET_BOUNDS: tuple[float, ...] = (10.0, 30.0, 80.0, 200.0, 1000.0)
AREA_BUCKET_LABELS: tuple[str, ...] = (
    "under_10",
    "10_to_30",
    "30_to_80",
    "80_to_200",
    "200_to_1000",
    "over_1000",
)


def ring_area(ring: Sequence[tuple[float, float]]) -> float:
    """`ring`'s own planar area in square metres, by the shoelace formula
    over its BNG `(easting, northing)` vertices.

    Absolute, so a ring wound clockwise and the same ring wound
    anticlockwise report the same area rather than one of them reporting
    a negative one: neither this module nor either dataset it reads
    guarantees a winding direction, and a signed area would turn that
    into a silently wrong number.

    Closed or unclosed alike, exactly like `point_in_ring`: the sum wraps
    from the last vertex back to the first, and an explicitly repeated
    closing vertex (every GeoJSON ring has one, every OSM closed way has
    one) contributes a term of exactly zero, so both spellings of the
    same ring answer identically with no stripping step needed.

    PLANAR, on the projected grid, not on the ellipsoid: BNG is a
    transverse Mercator projection whose scale factor departs from true
    by under a part in 2500 anywhere in Great Britain, which on a 100 m2
    house is under 0.1 m2. That is far inside the difference between the
    two datasets being compared, and the buckets this feeds are metres
    wide.
    """
    total = 0.0
    prev_e, prev_n = ring[-1]
    for east, north in ring:
        total += prev_e * north - east * prev_n
        prev_e, prev_n = east, north
    return abs(total) / 2.0


def area_bucket(area: float) -> str:
    """Which of `AREA_BUCKET_LABELS` `area` falls in, half open on each
    boundary (`10.0` reads as `10_to_30`, never as `under_10`), so every
    real number lands in exactly one bucket and no area is ever counted
    twice or dropped between two of them.
    """
    for bound, label in zip(AREA_BUCKET_BOUNDS, AREA_BUCKET_LABELS):
        if area < bound:
            return label
    return AREA_BUCKET_LABELS[-1]


def area_histogram(
    rings: Sequence[Sequence[tuple[float, float]]],
    positions: Iterable[int] | None = None,
) -> dict[str, int]:
    """`{bucket label: count}` over `rings`, or over just the subset
    `positions` names (`ContainmentResult.contained` and
    `not_contained` are exactly that shape, which is what lets a caller
    cross-tabulate size against containment without re-deriving either).

    EVERY label in `AREA_BUCKET_LABELS` is present, zeros included, in
    that fixed order: an explicit zero is a fact ("nothing of ours in
    this size band went unmatched"), while an absent key would leave a
    reader guessing whether the bucket was empty or was never counted.
    """
    counts = {label: 0 for label in AREA_BUCKET_LABELS}
    chosen = range(len(rings)) if positions is None else positions
    for position in chosen:
        counts[area_bucket(ring_area(rings[position]))] += 1
    return counts


# --------------------------------------------------------------------------
# polyline_offsets
# --------------------------------------------------------------------------

# The nearest-segment cell index's own cell size. The default `search_radius`
# (15 m) fits inside one cell, so the common case only ever needs a sample's
# own cell plus its 8 immediate neighbours; `_nearest_theirs_point` derives
# however many cells actually need checking from the ACTUAL `search_radius`
# it is called with, never assuming it is fixed at this module's own
# default (task-2-review.md's own Important finding: a search span fixed at
# 3x3 regardless of the argument silently drops a real match once
# `search_radius` exceeds this constant).
OFFSET_CELL_SIZE_M = 25.0


# The determinant floor below which the least-squares normal matrix `N`
# (see `polyline_offsets`'s own docstring, "the least-squares bias
# correction") is treated as singular. A population sampled from segments
# that are all (or nearly all) the same orientation makes every
# per-sample unit normal `n` point the same way (up to sign), so
# `N = sum(n n^T)` degenerates toward a rank-1 matrix and its determinant
# toward exactly 0.
#
# RELATIVE, not absolute (task-3-review.md's own Minor finding 1): a fixed
# absolute floor does not scale with how many samples fed `N`, since every
# accumulated sample adds exactly 1.0 to `N`'s own trace (each `n` is a
# unit vector, so `n n^T` always has trace 1). A well-conditioned
# two-orientation `N` built from many samples has a determinant that
# grows with the SQUARE of the sample count, while a genuinely
# near-singular `N` (one orientation, or two nearly parallel ones) stays
# near zero regardless of how many samples fed it: a fixed 1e-9 floor
# tuned against a small synthetic fixture is already far too permissive
# once real sample counts (hundreds to thousands, an ordinary road
# network) are plugged in, so a configuration that is genuinely too close
# to singular to trust can clear the fixed floor by many orders of
# magnitude and return a plausible-looking but badly wrong answer instead
# of `None` (task-3-review.md's own executed demonstration: an absolute
# floor left a real "explodes without tripping" window between roughly
# 1e-4 and 3e-5 degrees of angular separation). `trace/2` is the mean of
# `N`'s own two eigenvalues (`trace = lambda_1 + lambda_2`, always exactly
# `interior_count` for a unit-normal accumulation); `(trace/2)**2` is the
# determinant a WELL-CONDITIONED `N` of the same sample count would have
# if both eigenvalues sat at that mean (a perfectly balanced
# two-orthogonal-orientation population), so comparing the actual
# determinant against a small fraction of that reference scales the test
# to the sample count itself rather than to an arbitrary absolute number.
# 1e-6 is conservative enough that the brief's own pinned 2-degree
# near-singular case (task-3-review.md's own "Executed: nearly-singular
# case") stays comfortably a real answer, not `None`, while trapping the
# demonstrated bad-answer window well before it can return a wrong number
# (see the module's own tests for both boundaries, hand-verified).
_LSQ_DET_RELATIVE_FLOOR = 1e-6

# The floor below which too few INTERIOR (never endpoint-clamped) samples
# fed the least-squares accumulation for its answer to be trusted at all
# (task-3-review.md's own Important finding 2, see polyline_offsets's own
# docstring for why only interior samples are accumulated in the first
# place): 8 is small enough that the module's own two-orientation test
# fixtures (dozens of interior samples per line) clear it easily, and
# large enough that a determinant computed from a literal handful of
# samples, which the relative floor above alone cannot distinguish from a
# well-conditioned small population, is refused outright rather than
# reported as a number nobody should trust.
LSQ_MIN_SAMPLES = 8


@dataclass
class OffsetStats:
    """The aggregate offset between a densified sample of `ours` and the
    nearest point on `theirs`, from `polyline_offsets`.

    `mean_de`/`mean_dn` is the mean offset vector (theirs minus ours, so a
    positive value means theirs sits further east/north than ours);
    `magnitude_of_mean` is that vector's own length. `std_de`/`std_dn` are
    the population standard deviations of the per-sample offsets on each
    axis (how consistent the vector is, not how large it is). `p50_abs`/
    `p90_abs` are percentiles of each individual sample's own offset
    MAGNITUDE (not of the mean), via `distribution`.

    `lsq_de`/`lsq_dn`/`lsq_magnitude` are the LEAST-SQUARES estimate of the
    same systematic shift, the benchmark's own primary number for the
    epoch question (task-3-brief's controller addition, evidence in
    task-2-review.md): a straight segment's nearest-point projection only
    ever recovers the component of a shift PERPENDICULAR to that segment
    (task-2-review.md's own derivation, `offset = (n.s)*n` for an interior
    sample), so the naive mean vector above UNDERSTATES a true systematic
    shift `s` whenever the sampled population is not all one orientation:
    the review measured this exactly, invisible (0.043 of the true value)
    on a single line parallel to the shift, and exactly half (0.4714
    against a true 0.9) on a two-orientation grid. The least-squares
    solve corrects this: `polyline_offsets` accumulates, over every
    matched sample whose projection onto its winning `theirs` segment is
    STRICTLY INTERIOR (`0 < t < 1`, never clamped to either endpoint), the
    normal-equation terms `N += n n^T` (2x2) and `V += v` (the observed
    offset vector, which the same derivation shows equals `n n^T . s`
    exactly for an interior sample, so `N s = V` exactly under the
    noiseless model), then solves that system by Cramer's rule.

    The interior-only restriction is itself a correctness fix, not
    polish (task-3-review.md's own Important finding 2): the identity
    `v = n(n.v)` that makes the shortcut `V = sum(v)` exact holds ONLY for
    an interior sample. A CLAMPED sample's own offset vector points
    somewhere between the segment's normal and its own endpoint, not
    purely along the normal, so folding a clamped sample into the same
    accumulation biases `N s = V` toward a wrong answer that looks just as
    plausible as a right one: the review's own executed demonstration
    found a same-extent two-orientation grid (the realistic case, since a
    genuine OSM way and its matching NGD roadlink describe the same
    physical road, split at the same real-world junctions, so their
    extents are naturally similar) overshoot to `(0.943, 0.943)` against a
    true `(0.9, 0.9)`, and a shorter 20 m same-extent case overshoot past
    the epoch report's own CONSISTENT upper band entirely. Excluding
    clamped samples from `N`/`V` removes that bias source outright: they
    still count fully in `count`, the naive mean, `std_de`/`std_dn` and
    the percentile fields above, exactly as before, since none of those
    statistics assumes interior-only projection.

    `lsq_magnitude` is None whenever fewer than `LSQ_MIN_SAMPLES` interior
    samples fed the accumulation, or `N`'s determinant is under the
    RELATIVE floor `_LSQ_DET_RELATIVE_FLOOR * (trace/2)**2` (a population
    too close to one orientation, or too thin, to separate the two
    components of `s` with any confidence; see both constants' own
    docstrings for why relative and why 8): this implementation reports
    that case as undefined outright, even in the special case where the
    true shift happens to lie entirely along the one well-determined axis
    (a minimum-norm pseudo-inverse could recover that special case, but a
    real road network's line population is never genuinely
    single-orientation, so the extra machinery buys nothing this
    benchmark needs; see the module's own tests for this exact scenario).

    `count` is how many densified samples found a neighbour inside
    `search_radius` and contributed to every statistic above;
    `unmatched_samples` is how many did not and contributed to none of
    them. The eight non-lsq numeric fields are 0.0 (not fabricated, just
    inert) when `count` is 0, and all three lsq fields are None in that
    case too: a caller reading `count == 0` already knows none of the
    other numbers describes anything real.
    """

    count: int
    unmatched_samples: int
    mean_de: float
    mean_dn: float
    magnitude_of_mean: float
    std_de: float
    std_dn: float
    p50_abs: float
    p90_abs: float
    lsq_de: float | None
    lsq_dn: float | None
    lsq_magnitude: float | None


def _densify_polyline(
    polyline: Sequence[tuple[float, float]], sample_every: float
) -> list[tuple[float, float]]:
    """`polyline`'s own vertices, plus interpolated points every
    `sample_every` metres along each of its segments, in order.

    Every original vertex is included exactly once (the brief's own
    "include vertices"): each segment contributes its own start vertex,
    then interior points at `sample_every`, `2 * sample_every`, ... up to
    but never including the segment's own end (which is either the next
    segment's start, already covered, or the polyline's own last vertex,
    appended once at the very end) so no point is ever duplicated at a
    segment boundary.
    """
    points: list[tuple[float, float]] = []
    if not polyline:
        return points
    if len(polyline) == 1:
        return [polyline[0]]

    for index in range(len(polyline) - 1):
        start = polyline[index]
        end = polyline[index + 1]
        points.append(start)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= 0.0:
            continue
        step_count = int(math.floor(length / sample_every))
        for step_index in range(1, step_count + 1):
            distance = step_index * sample_every
            if distance >= length:
                break
            t = distance / length
            points.append((start[0] + dx * t, start[1] + dy * t))
    points.append(polyline[-1])
    return points


def _segments(polylines: Sequence[Sequence[tuple[float, float]]]):
    """Every consecutive vertex pair of every polyline in `polylines`, as
    `(start, end)` tuples, flattened across all of them: one polyline
    contributes `len(polyline) - 1` segments, a polyline of fewer than 2
    points contributes none.
    """
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for polyline in polylines:
        for index in range(len(polyline) - 1):
            segments.append((polyline[index], polyline[index + 1]))
    return segments


def _nearest_point_on_segment(
    point: tuple[float, float],
    seg_start: tuple[float, float],
    seg_end: tuple[float, float],
) -> tuple[tuple[float, float], float]:
    """The exact nearest point to `point` on the segment `seg_start` to
    `seg_end`, paired with the CLAMPED projection parameter `t` that
    produced it: the ordinary clamped-projection formula (project `point`
    onto the segment's own infinite line, then clamp the parameter to
    `[0, 1]` so the answer never lands past either endpoint), no
    approximation. A zero-length segment (`seg_start == seg_end`, a
    degenerate polyline vertex repeated) answers `seg_start` itself, `t`
    `0.0`, rather than dividing by zero.

    `t` travels back with the point (task-3-review.md's own Important
    finding 2) because `polyline_offsets`'s least-squares accumulation
    needs to tell an interior projection (`0 < t < 1`, unclamped, the only
    case its normal-equation shortcut is exact for) apart from one that
    was clamped to an endpoint: a pre-clamp `t` outside `[0, 1]` comes
    back here as exactly `0.0` or `1.0`, so a caller checking
    `0.0 < t < 1.0` on this return value alone correctly identifies every
    genuinely interior sample, with no separate before/after comparison
    needed.
    """
    px, py = point
    ax, ay = seg_start
    bx, by = seg_end
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return seg_start, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return (ax + dx * t, ay + dy * t), t


def _nearest_theirs_point(
    sample: tuple[float, float],
    segments: Sequence[tuple[tuple[float, float], tuple[float, float]]],
    index: dict[Cell, list[int]],
    search_radius: float,
) -> tuple[tuple[float, float], int, float] | None:
    """The nearest point to `sample` among every segment in `segments`
    that `index` (a `OFFSET_CELL_SIZE_M` cell index over those same
    segments' own bounding boxes) places within `sample`'s own cell or one
    of its neighbours, paired with that winning segment's own index into
    `segments` and the CLAMPED projection parameter `t` that produced the
    point (`_nearest_point_on_segment`'s own return shape), if the nearest
    point is within `search_radius`; None otherwise.

    The segment index and `t` travel back with the point (not just the
    point alone, which is all the caller needed before) because
    `polyline_offsets` now needs the winning segment's own direction, and
    whether the projection onto it was clamped, to build the
    least-squares bias correction's per-sample unit normal and decide
    whether this sample may contribute to it at all (see that function's
    own docstring): the nearest-point-on-segment distance alone carries
    neither.

    The neighbourhood span is derived from `search_radius` itself
    (`ceil(search_radius / OFFSET_CELL_SIZE_M)` cells in every direction
    from the sample's own cell, floored at 1), not a fixed 3x3 block: a
    fixed block is only correct while `search_radius <= OFFSET_CELL_SIZE_M`
    (task-2-review.md's own Important finding, an executed construction
    that this module's own test suite now reproduces: a sample at
    `(24, 0)`, cell column 0, with a real segment 26 m away registered in
    cell column 2, was silently dropped to `unmatched` under a fixed 3x3
    search once `search_radius=30.0` was passed, even though 26 m sits
    well inside that radius). Deriving the span from the actual argument
    makes the search correct for any `search_radius`, not only the
    default: at the default 15 m, `ceil(15 / 25) == 1`, so the common case
    still checks exactly the same 3x3 block as before.
    """
    span = max(1, math.ceil(search_radius / OFFSET_CELL_SIZE_M))
    col, row = _cell(sample[0], sample[1], OFFSET_CELL_SIZE_M)
    candidates: set[int] = set()
    for d_col in range(-span, span + 1):
        for d_row in range(-span, span + 1):
            candidates.update(index.get((col + d_col, row + d_row), ()))

    best_point: tuple[float, float] | None = None
    best_distance: float | None = None
    best_seg_index: int | None = None
    best_t: float | None = None
    for seg_index in candidates:
        seg_start, seg_end = segments[seg_index]
        candidate_point, t = _nearest_point_on_segment(sample, seg_start, seg_end)
        distance = math.hypot(candidate_point[0] - sample[0], candidate_point[1] - sample[1])
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_point = candidate_point
            best_seg_index = seg_index
            best_t = t

    if best_distance is not None and best_distance <= search_radius and best_point is not None:
        return best_point, best_seg_index, best_t
    return None


def polyline_offsets(
    ours: Sequence[Sequence[tuple[float, float]]],
    theirs: Sequence[Sequence[tuple[float, float]]],
    sample_every: float = 5.0,
    search_radius: float = 15.0,
) -> OffsetStats:
    """Densify every polyline in `ours` at `sample_every` metre intervals
    (`_densify_polyline`), find each sample's nearest point on any segment
    of `theirs` within `search_radius` (`_nearest_theirs_point`, backed by
    a `OFFSET_CELL_SIZE_M` cell index over `theirs`' own segments), and
    aggregate the resulting offset vectors (theirs minus ours) into an
    `OffsetStats`.

    A sample with no `theirs` segment inside `search_radius` contributes
    to `unmatched_samples` and to nothing else: no offset is guessed for
    it, matching this module's own "nothing fabricated" standard.

    ## The least-squares bias correction

    Alongside the naive mean this function has always computed, it also
    solves for the systematic shift `s` a differently-oriented sample
    population actually supports (`OffsetStats.lsq_de`/`lsq_dn`/
    `lsq_magnitude`; see that dataclass's own docstring for the full
    derivation, its interior-only restriction, and citation). For every
    matched sample this loop already has the winning `theirs` segment and
    the CLAMPED projection parameter that produced the point
    (`_nearest_theirs_point` now returns both alongside the point): that
    segment's own unit direction `(te, tn)` gives a unit normal
    `n = (-tn, te)` (the task-3-brief controller addition's own pinned
    convention; the sign choice is immaterial, since both `n n^T` and
    `n (n.s)` are invariant under `n -> -n`). ONLY a STRICTLY INTERIOR
    sample (`0.0 < t < 1.0`, never clamped to either of the winning
    segment's own endpoints) accumulates `N += n n^T` (a running 2x2
    symmetric matrix, kept as its three distinct entries `n_xx`, `n_xy`,
    `n_yy`) and `V += v` (the observed offset vector itself, `(de, dn)`);
    a clamped sample still contributes fully to `count`, the naive mean,
    `std_de`/`std_dn` and the percentile fields, exactly as before, only
    never to `N`/`V`. After the loop, `N s = V` is solved for `s` by
    Cramer's rule, gated on `LSQ_MIN_SAMPLES` interior samples and a
    determinant clear of the relative floor (see both constants' own
    docstrings).

    A zero-length winning segment (a degenerate repeated-vertex polyline
    entry) has no direction to contribute either: it is excluded from
    `N`/`V` the same way a clamped sample is, still counted normally in
    every other statistic.
    """
    segments = _segments(theirs)
    index: dict[Cell, list[int]] = defaultdict(list)
    for seg_index, (seg_start, seg_end) in enumerate(segments):
        bbox = (
            min(seg_start[0], seg_end[0]),
            min(seg_start[1], seg_end[1]),
            max(seg_start[0], seg_end[0]),
            max(seg_start[1], seg_end[1]),
        )
        for cell in _cells_for_bbox(bbox, OFFSET_CELL_SIZE_M):
            index[cell].append(seg_index)

    offsets_de: list[float] = []
    offsets_dn: list[float] = []
    magnitudes: list[float] = []
    unmatched_samples = 0

    # The least-squares normal-equation accumulators: N as its three
    # distinct symmetric entries, V as its two components. See the
    # docstring above and OffsetStats's own for the full derivation.
    # interior_count is the number of samples that actually fed them
    # (LSQ_MIN_SAMPLES gates the solve on this, not on `count` overall,
    # since a clamped sample counts toward `count` but never toward this).
    n_xx = n_xy = n_yy = 0.0
    v_e = v_n = 0.0
    interior_count = 0

    for polyline in ours:
        for sample in _densify_polyline(polyline, sample_every):
            nearest = _nearest_theirs_point(sample, segments, index, search_radius)
            if nearest is None:
                unmatched_samples += 1
                continue
            nearest_point, seg_index, t = nearest
            de = nearest_point[0] - sample[0]
            dn = nearest_point[1] - sample[1]
            offsets_de.append(de)
            offsets_dn.append(dn)
            magnitudes.append(math.hypot(de, dn))

            # Strictly interior only (task-3-review.md's own Important
            # finding 2): a clamped projection's offset is not purely
            # along the segment's own normal, so folding it in here would
            # bias N s = V toward a wrong answer (see this function's own
            # docstring). A clamped sample has already contributed to
            # every statistic above; it simply never reaches this block.
            if not (0.0 < t < 1.0):
                continue

            seg_start, seg_end = segments[seg_index]
            tx, ty = seg_end[0] - seg_start[0], seg_end[1] - seg_start[1]
            t_length = math.hypot(tx, ty)
            if t_length > 0.0:
                te, tn = tx / t_length, ty / t_length
                n_east, n_north = -tn, te
                n_xx += n_east * n_east
                n_xy += n_east * n_north
                n_yy += n_north * n_north
                v_e += de
                v_n += dn
                interior_count += 1

    count = len(offsets_de)
    if count == 0:
        return OffsetStats(
            count=0,
            unmatched_samples=unmatched_samples,
            mean_de=0.0,
            mean_dn=0.0,
            magnitude_of_mean=0.0,
            std_de=0.0,
            std_dn=0.0,
            p50_abs=0.0,
            p90_abs=0.0,
            lsq_de=None,
            lsq_dn=None,
            lsq_magnitude=None,
        )

    mean_de = sum(offsets_de) / count
    mean_dn = sum(offsets_dn) / count
    std_de = math.sqrt(sum((value - mean_de) ** 2 for value in offsets_de) / count)
    std_dn = math.sqrt(sum((value - mean_dn) ** 2 for value in offsets_dn) / count)
    magnitude_dist = distribution(magnitudes, fractions=(0.5, 0.9))

    if interior_count < LSQ_MIN_SAMPLES:
        lsq_de = lsq_dn = lsq_magnitude = None
    else:
        determinant = n_xx * n_yy - n_xy * n_xy
        trace_half = (n_xx + n_yy) / 2.0
        if determinant < _LSQ_DET_RELATIVE_FLOOR * (trace_half ** 2):
            lsq_de = lsq_dn = lsq_magnitude = None
        else:
            lsq_de = (v_e * n_yy - n_xy * v_n) / determinant
            lsq_dn = (n_xx * v_n - n_xy * v_e) / determinant
            lsq_magnitude = math.hypot(lsq_de, lsq_dn)

    return OffsetStats(
        count=count,
        unmatched_samples=unmatched_samples,
        mean_de=mean_de,
        mean_dn=mean_dn,
        magnitude_of_mean=math.hypot(mean_de, mean_dn),
        std_de=std_de,
        std_dn=std_dn,
        p50_abs=magnitude_dist[0.5],
        p90_abs=magnitude_dist[0.9],
        lsq_de=lsq_de,
        lsq_dn=lsq_dn,
        lsq_magnitude=lsq_magnitude,
    )


# --------------------------------------------------------------------------
# distribution
# --------------------------------------------------------------------------


def distribution(
    values: Sequence[float], fractions: tuple[float, ...] = (0.1, 0.5, 0.9)
) -> dict[float, float]:
    """`{fraction: percentile}` for every fraction in `fractions`, via
    `heights._percentile` (the same linear-interpolation-between-order-
    statistics convention that module's own docstring hand-checks, reused
    rather than re-derived).

    `{}` for an empty `values`, never a fabricated percentile of nothing:
    the same standard this module holds everywhere else.
    """
    if not values:
        return {}
    return {fraction: _percentile(values, fraction) for fraction in fractions}
