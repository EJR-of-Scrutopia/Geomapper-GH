"""Comparing an OS OpenMap Local extract against this project's own output.

Pure comparison machinery for the OS benchmark (plan item F): no network, no
disk, no `random` anywhere in this module. Everything here answers one of
three questions about two independently produced datasets over the same
ground, both already loaded into memory as plain BNG `(easting, northing)`
sequences by whatever caller owns the disk and network side of the
benchmark:

  * How much do a footprint of ours and a footprint of theirs actually
    overlap, as a fraction (`sampled_iou`)?
  * Which of our footprints corresponds to which of theirs, if any
    (`match_footprints`)?
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
forcing a pairing below `MATCH_IOU_FLOOR`. `polyline_offsets` counts a
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
from typing import Sequence

from mapgen.buildings import point_in_ring
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

    theirs_index: dict[Cell, list[int]] = defaultdict(list)
    for j, bbox in enumerate(theirs_bboxes):
        for cell in _cells_for_bbox(bbox, MATCH_CELL_SIZE_M):
            theirs_index[cell].append(j)

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


@dataclass
class OffsetStats:
    """The aggregate offset between a densified sample of `ours` and the
    nearest point on `theirs`, from `polyline_offsets`.

    `mean_de`/`mean_dn` is the mean offset vector (theirs minus ours, so a
    positive value means theirs sits further east/north than ours);
    `magnitude_of_mean` is that vector's own length, the single number this
    benchmark's epoch-shift question is really asking for.
    `std_de`/`std_dn` are the population standard deviations of the
    per-sample offsets on each axis (how consistent the vector is, not how
    large it is). `p50_abs`/`p90_abs` are percentiles of each individual
    sample's own offset MAGNITUDE (not of the mean), via `distribution`.

    `count` is how many densified samples found a neighbour inside
    `search_radius` and contributed to every statistic above;
    `unmatched_samples` is how many did not and contributed to none of
    them. All eight numeric fields are 0.0 (not fabricated, just inert)
    when `count` is 0: a caller reading `count == 0` already knows none of
    the other seven numbers describes anything real.
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
) -> tuple[float, float]:
    """The exact nearest point to `point` on the segment `seg_start` to
    `seg_end`: the ordinary clamped-projection formula (project `point`
    onto the segment's own infinite line, then clamp the parameter to
    `[0, 1]` so the answer never lands past either endpoint), no
    approximation. A zero-length segment (`seg_start == seg_end`, a
    degenerate polyline vertex repeated) answers `seg_start` itself rather
    than dividing by zero.
    """
    px, py = point
    ax, ay = seg_start
    bx, by = seg_end
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return seg_start
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return (ax + dx * t, ay + dy * t)


def _nearest_theirs_point(
    sample: tuple[float, float],
    segments: Sequence[tuple[tuple[float, float], tuple[float, float]]],
    index: dict[Cell, list[int]],
    search_radius: float,
) -> tuple[float, float] | None:
    """The nearest point to `sample` among every segment in `segments`
    that `index` (a `OFFSET_CELL_SIZE_M` cell index over those same
    segments' own bounding boxes) places within `sample`'s own cell or one
    of its neighbours, if that nearest point is within `search_radius`;
    None otherwise.

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
    for seg_index in candidates:
        seg_start, seg_end = segments[seg_index]
        candidate_point = _nearest_point_on_segment(sample, seg_start, seg_end)
        distance = math.hypot(candidate_point[0] - sample[0], candidate_point[1] - sample[1])
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_point = candidate_point

    if best_distance is not None and best_distance <= search_radius:
        return best_point
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

    for polyline in ours:
        for sample in _densify_polyline(polyline, sample_every):
            nearest = _nearest_theirs_point(sample, segments, index, search_radius)
            if nearest is None:
                unmatched_samples += 1
                continue
            de = nearest[0] - sample[0]
            dn = nearest[1] - sample[1]
            offsets_de.append(de)
            offsets_dn.append(dn)
            magnitudes.append(math.hypot(de, dn))

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
        )

    mean_de = sum(offsets_de) / count
    mean_dn = sum(offsets_dn) / count
    std_de = math.sqrt(sum((value - mean_de) ** 2 for value in offsets_de) / count)
    std_dn = math.sqrt(sum((value - mean_dn) ** 2 for value in offsets_dn) / count)
    magnitude_dist = distribution(magnitudes, fractions=(0.5, 0.9))

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
