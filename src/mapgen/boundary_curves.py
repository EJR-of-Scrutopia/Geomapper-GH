"""Parcel rings into the single-line boundary drawing the owner asked for.

INSPIRE Index Polygons carry every parcel as its own closed ring, and a
shared wall between two neighbouring parcels is drawn twice: once from
each parcel's own file, byte-for-byte identical or merely re-started at a
different vertex and wound the other way. A boundary-crossing parcel (the
plan's own documented case) goes further and arrives as the exact same
ring twice, once from each authority's own export. Handed straight to a
drawing, both of those double every line they touch. This module is the
whole mechanism that turns parcel-by-parcel geometry into one line per
real boundary: whole-ring duplicates collapse first, the survivors' edges
collapse a second time (an edge and its reversal, or two parcels' copies
of the same wall, are the same edge), and what remains is chained into the
longest polylines the graph allows, stopping wherever three or more edges
meet.

Pure geometry: no imports from `sources/`, no network, no file paths.
`boundary_curves` takes exactly the ring lists `sources/inspire.py`'s
`parcels_in` already yields (exterior ring first, then any interior
rings, each a list of BNG (easting, northing) float tuples) and returns
curves in the same frame, so it is tested, and trusted, on its own.

## Internal ring form: unclosed, rounded once

A ring may arrive closed (`gml:LinearRing`'s own convention, first vertex
repeated last) or unclosed; every ring is normalised to UNCLOSED on entry
(the repeated last point dropped, if present), because a ring's edges are
its consecutive vertex pairs including the wraparound from its last
vertex back to its first, and an unclosed list's own wraparound
(`ring[(index + 1) % len(ring)]`) already gives exactly that without a
special case for the one closing edge.

Rounding to 0.01 m happens exactly once, right here at entry
(`_round_point`, `_ROUND_DECIMALS`), and every identity check downstream,
a ring's own duplicate hash, an edge's endpoints, a chain's join point,
reads that rounded coordinate and nothing else. Nothing in this module
ever computes a NEW coordinate (no interpolation, unlike contours.py), so
a plain `==` on rounded tuples is identity everywhere, with none of the
float-drift risk a derived coordinate would carry. Two rings that were
never meant to be the same parcel do not collide at 1 cm; two copies of
the same parcel from two different authority exports, whose vertices were
never going to agree to the sub-millimetre, do.

## Why three separate collapses, not one

A parcel that arrives twice is a WHOLE-RING duplicate: the same vertices,
in some rotation and either direction. Two neighbouring, DIFFERENT
parcels sharing one wall are not duplicates of each other at the ring
level at all; only their shared EDGE is. Collapsing rings first, before
edges are even extracted, is what stops a repeated ring from doubling its
own edges into the second collapse's count, and the two stages catch
genuinely different things: neither can stand in for the other.

## Chaining: contours.py's own approach, mirrored rather than imported

Stage 3's join-by-endpoint and drop-collinear-vertices are the same
mechanism `contours.py`'s `_join_segments`/`_drop_collinear` already use,
including the 0.05 m collinearity tolerance, because a chain of edges
joined at shared vertices is the same problem whether the edges came from
marching squares or from parcel rings. `contours.py`'s own version
additionally rounds every point to a micron before comparing endpoints,
because ITS points are freshly interpolated floats that need that
tolerance to agree on what "the same point" means; this module's points
are already rounded once at entry and never recomputed, so that second
rounding step would be a no-op here and is left out, not overlooked.
Mirrored rather than imported (see this task's own report for the case
that a shared home would now be worth building, not made in this pass):
the one behavioural difference this module adds on top is stopping every
chain at a vertex three or more edges touch, which `contours.py` has
never needed, because marching-squares segments essentially never meet
three-deep.

## Deterministic output

Each finished curve is wound whichever direction makes it compare
smallest (`_canonical_direction`): an open chain keeps its smaller
endpoint first, a closed ring is rotated to its own smallest vertex and
wound the same way `_ring_key` already breaks that tie. The finished list
is then sorted outright (`list.sort` on lists of point tuples, which
compares element by element): first by each curve's own first point,
then, wherever two curves happen to share one, by their next point, and
so on. Two runs over the same input, regardless of which edge happened to
start a chain's walk or which parcel arrived first, always produce the
same list in the same order.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

Point = tuple[float, float]
Ring = list[Point]
Curve = list[Point]

# Metres, the brief's own figure: rounded once here, and read everywhere
# downstream (ring hashing, edge identity, chaining) as the same value.
_ROUND_DECIMALS = 2

# Metres, perpendicular distance: the same constant contours.py's own
# _COLLINEAR_TOLERANCE_M uses, mirrored rather than imported (see the
# module docstring's "Chaining" section).
_COLLINEAR_TOLERANCE_M = 0.05


@dataclass(frozen=True)
class BoundaryCurvesResult:
    """`boundary_curves`'s own curves, plus the counts its brief asks for.

    A companion record, not `boundary_curves`'s own return value: the
    brief's interface is `boundary_curves(...) -> list[list[tuple[float,
    float]]]`, and this dataclass exists so a caller who wants the
    pipeline's counts can ask `boundary_curves_with_counts` for them
    without changing what the primary function returns or stashing
    mutable state anywhere between calls. Frozen: every field is set once,
    from one pipeline run, and never touched again.
    """

    curves: list[Curve]
    rings_in: int
    rings_deduped: int
    edges_in: int
    edges_deduped: int
    curves_out: int


def boundary_curves(
    parcels: Iterable[list[list[tuple[float, float]]]]
) -> list[list[tuple[float, float]]]:
    """Every parcel's rings, deduplicated and chained into the single-line
    boundary drawing the owner asked for (see the module docstring).

    `parcels` is exactly `sources/inspire.py`'s `parcels_in` own yield
    shape: one list of rings per parcel, exterior ring first, coordinates
    as (easting, northing) float tuples in BNG metres. Returns curves in
    the same frame, deterministically ordered (see the module docstring's
    "Deterministic output" section). A thin wrapper over
    `boundary_curves_with_counts`, which runs the whole pipeline; this
    function exists to keep the interface exactly the callable the brief
    names, with no counts attached to it.
    """
    return boundary_curves_with_counts(parcels).curves


def boundary_curves_with_counts(
    parcels: Iterable[list[list[tuple[float, float]]]]
) -> BoundaryCurvesResult:
    """`boundary_curves`'s own result, plus the pipeline's counts.

    `rings_in` counts every ring handed in, exterior and interior alike,
    before anything is dropped. `rings_deduped` counts what stage 1
    keeps: a ring that rounds down to fewer than 3 distinct vertices
    (`_normalise_ring`) and a ring that is a rotated or reversed repeat of
    one already kept (`_dedupe_rings`) are both excluded from it by the
    same "never reached stage 2" logic, so the two kinds of drop are not
    told apart here; nothing in the brief asks for that finer split.
    `edges_in` counts the edges stage 2 actually considers, which is
    AFTER zero-length edges (two rounded vertices that coincide) are
    dropped: a zero-length edge was never a line the surviving rings
    intended to draw, so it is excluded from the "in" count the same way
    a degenerate ring is, rather than inflating it before being
    subtracted back out. `edges_deduped` is the unique-edge count stage 3
    chains. `curves_out` is `len(curves)`.
    """
    rings_in = 0
    normalised_rings: list[Ring] = []
    for parcel in parcels:
        for ring in parcel:
            rings_in += 1
            normalised = _normalise_ring(ring)
            if normalised is not None:
                normalised_rings.append(normalised)

    deduped_rings = _dedupe_rings(normalised_rings)
    edges_in, unique_edges = _extract_unique_edges(deduped_rings)
    curves = _chain_edges(unique_edges)

    return BoundaryCurvesResult(
        curves=curves,
        rings_in=rings_in,
        rings_deduped=len(deduped_rings),
        edges_in=edges_in,
        edges_deduped=len(unique_edges),
        curves_out=len(curves),
    )


# --------------------------------------------------------------------------
# Stage 0 + 1: entry normalisation and duplicate-ring removal.
# --------------------------------------------------------------------------


def _round_point(point: tuple[float, float]) -> Point:
    return (round(point[0], _ROUND_DECIMALS), round(point[1], _ROUND_DECIMALS))


def _normalise_ring(ring: list[tuple[float, float]]) -> Ring | None:
    """`ring`, rounded once and unclosed, or None if it is degenerate.

    A closed ring (first vertex repeated last, per GML) is unclosed here
    by dropping that repeat; see the module docstring for why this,
    rather than a closed form, is what every later stage works on. A ring
    whose rounded vertices collapse to fewer than 3 DISTINCT points
    cannot bound an area at all (a point or a line, not a boundary) and is
    dropped here, wholesale; its own possibly-repeated interior vertices
    (two adjacent points that merely round to the same value) are left in
    place, for stage 2's zero-length-edge check to drop as an edge, not
    thinned out here as a point, so a ring is only ever rejected outright
    for being too small, never silently altered first.
    """
    if not ring:
        return None
    rounded = [_round_point(point) for point in ring]
    if len(rounded) > 1 and rounded[0] == rounded[-1]:
        rounded = rounded[:-1]
    if len(set(rounded)) < 3:
        return None
    return rounded


def _ring_key(ring: Ring) -> tuple[Point, ...]:
    """`ring`'s own shape identity: invariant to which vertex it starts at
    and which direction it was wound.

    Rotated to start at its own smallest vertex (lexicographic on
    (easting, northing)), then compared against that same rotation's
    reversal (wound the other way, but still starting at the same
    vertex): the lexicographically smaller of the two is the key. Two
    rings that are the same shape, however each source file happened to
    start or wind it, always produce this same key.
    """
    start = ring.index(min(ring))
    rotated = ring[start:] + ring[:start]
    reversed_rotated = [rotated[0]] + list(reversed(rotated[1:]))
    return tuple(min(rotated, reversed_rotated))


def _dedupe_rings(rings: list[Ring]) -> list[Ring]:
    seen: set[tuple[Point, ...]] = set()
    kept: list[Ring] = []
    for ring in rings:
        key = _ring_key(ring)
        if key in seen:
            continue
        seen.add(key)
        kept.append(ring)
    return kept


# --------------------------------------------------------------------------
# Stage 2: edge extraction and shared-edge dedup.
# --------------------------------------------------------------------------


def _edge_key(a: Point, b: Point) -> tuple[Point, Point]:
    """`(a, b)` and `(b, a)` are the same undirected edge; this is
    whichever ordering is smaller, the one identity both ever hash to.
    """
    return (a, b) if a <= b else (b, a)


def _extract_unique_edges(rings: list[Ring]) -> tuple[int, list[tuple[Point, Point]]]:
    """Every ring's consecutive vertex pairs (including the wraparound
    from its last vertex back to its first), direction-normalised and
    deduplicated, plus how many survived the zero-length check to be
    counted as "in" (see `boundary_curves_with_counts`'s own docstring).

    A dict, not a set, for `unique`: Python dicts keep insertion order,
    which makes `_chain_edges`'s own walk, and this whole pipeline's
    output, depend only on the order rings themselves arrived in, never
    on set-iteration order, which carries no such guarantee. The values
    are never read; only the keys, and their order, are.
    """
    edges_in = 0
    unique: dict[tuple[Point, Point], None] = {}
    for ring in rings:
        count = len(ring)
        for index in range(count):
            a, b = ring[index], ring[(index + 1) % count]
            if a == b:
                continue  # zero-length after rounding; never a real wall.
            edges_in += 1
            unique[_edge_key(a, b)] = None
    return edges_in, list(unique.keys())


# --------------------------------------------------------------------------
# Stage 3: chaining, mirroring contours.py's own approach (see the module
# docstring's "Chaining" section for what is deliberately different).
# --------------------------------------------------------------------------


def _chain_edges(edges: list[tuple[Point, Point]]) -> list[Curve]:
    """Every unique edge, joined into maximal polylines through vertices
    exactly two edges touch, stopping at every vertex three or more touch.
    """
    degree: dict[Point, int] = defaultdict(int)
    endpoint_index: dict[Point, list[int]] = defaultdict(list)
    for index, (a, b) in enumerate(edges):
        degree[a] += 1
        degree[b] += 1
        endpoint_index[a].append(index)
        endpoint_index[b].append(index)

    junctions = {point for point, count in degree.items() if count >= 3}
    used = [False] * len(edges)

    def _unused_at(point: Point) -> int | None:
        for candidate in endpoint_index.get(point, ()):
            if not used[candidate]:
                return candidate
        return None

    def _other_end(edge_index: int, known_end: Point) -> Point:
        a, b = edges[edge_index]
        return b if a == known_end else a

    raw_chains: list[Curve] = []
    for start in range(len(edges)):
        if used[start]:
            continue
        used[start] = True
        chain: Curve = list(edges[start])

        # A junction ends every chain touching it, even one with an unused
        # edge still waiting there: that edge starts its OWN chain, in a
        # later pass of this same loop, rather than being fused into this
        # one (see the module docstring: this is the one place this
        # module's chaining differs from contours.py's own).
        while chain[-1] not in junctions:
            candidate = _unused_at(chain[-1])
            if candidate is None:
                break
            used[candidate] = True
            chain.append(_other_end(candidate, chain[-1]))

        while chain[0] not in junctions:
            candidate = _unused_at(chain[0])
            if candidate is None:
                break
            used[candidate] = True
            chain.insert(0, _other_end(candidate, chain[0]))

        if len(chain) > 2 and chain[0] == chain[-1]:
            # A closed ring: see contours.py's own _join_segments for the
            # same convention (there, replacing a near-miss with an exact
            # copy; here the two ends are already the identical rounded
            # tuple, so this is a no-op kept for the same documented shape).
            chain[-1] = chain[0]

        raw_chains.append(chain)

    finished = [_canonical_direction(_drop_collinear(chain)) for chain in raw_chains]
    finished.sort()
    return finished


def _perpendicular_distance(point: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length == 0.0:
        return math.hypot(px - ax, py - ay)
    return abs(dx * (ay - py) - (ax - px) * dy) / length


def _drop_collinear(points: Curve) -> Curve:
    """The same simplification as contours.py's own `_drop_collinear`, at
    the same 0.05 m tolerance: a vertex within tolerance of the line
    through the last KEPT point and its own next raw neighbour is dropped,
    which collapses a straight run to its two ends in one pass. The first
    and last points are always kept, whichever curve, closed or not.
    """
    if len(points) < 3:
        return list(points)
    kept = [points[0]]
    for index in range(1, len(points) - 1):
        if _perpendicular_distance(points[index], kept[-1], points[index + 1]) > _COLLINEAR_TOLERANCE_M:
            kept.append(points[index])
    kept.append(points[-1])
    return kept


def _canonical_direction(curve: Curve) -> Curve:
    """`curve`, wound whichever way makes it compare smallest, so two runs
    that happened to walk the same chain from opposite ends (an accident
    of which edge started the walk, not a property of the boundary
    itself) produce identical output.

    An open chain keeps whichever direction puts the smaller endpoint
    first, reversing outright if it does not already. A closed ring is
    rotated to start at its own smallest vertex and wound whichever way
    makes the second vertex smaller, exactly `_ring_key`'s own rule,
    because the two problems, which of a ring's own equivalent
    descriptions is canonical, are the same problem.
    """
    if len(curve) > 2 and curve[0] == curve[-1]:
        body = curve[:-1]
        start = body.index(min(body))
        rotated = body[start:] + body[:start]
        reversed_rotated = [rotated[0]] + list(reversed(rotated[1:]))
        chosen = min(rotated, reversed_rotated)
        return chosen + [chosen[0]]
    if curve[0] <= curve[-1]:
        return curve
    return list(reversed(curve))
