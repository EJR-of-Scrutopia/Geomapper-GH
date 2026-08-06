"""boundary_curves, checked against hand-derived edge and chain counts.

Every geometric test here builds a small, explicit set of rings (unit
squares, triangles) and works out by hand what the three binding stages
(ring dedup, edge dedup, chaining) must produce, rather than trusting the
implementation to grade its own homework: a shared wall between two
squares collapses to one edge and, because both of that edge's endpoints
now touch three ways (the two squares' own outlines, plus the wall
itself), splits the outline into three curves, not one; that arithmetic
is worked through by hand in each test's own comments. The two tests that
are not hand-built play the real fixture from Task 2 (`parcels_sample.gml`)
through `parcels_in`, and check the shared-edge pair that fixture's own
provenance notes name explicitly, at the edge-dedup stage directly rather
than in the finished, collinear-simplified curves (see that test's own
docstring for why).
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

from mapgen.boundary_curves import (
    BoundaryCurvesResult,
    _canonical_direction,
    _drop_collinear,
    _edge_key,
    _extract_unique_edges,
    _normalise_ring,
    _ring_key,
    boundary_curves,
    boundary_curves_with_counts,
)
from mapgen.sources.inspire import GML_MEMBER_NAME, parcels_in

_FIXTURE_GML_PATH = Path(__file__).resolve().parent / "fixtures" / "inspire" / "parcels_sample.gml"

# Covers the whole committed fixture's own extent (see test_inspire.py's
# own FIXTURE_BBOX_ALL, which this mirrors: E 313526..313587, N
# 168911..168987).
_FIXTURE_BBOX_ALL = (313_500.0, 168_900.0, 313_600.0, 169_000.0)


def _fixture_zip(tmp_path: Path) -> Path:
    """The committed fixture GML, zipped under the real member name:
    `parcels_in` opens a zip's own GML member, never a bare GML file, and
    test_inspire.py's own `_fixture_zip` already established this exact
    build-fresh-per-test pattern (the fixture stays a plain, diffable
    text file; only this throwaway zip wraps it).
    """
    zip_path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(_FIXTURE_GML_PATH, arcname=GML_MEMBER_NAME)
    return zip_path


# --------------------------------------------------------------------------
# Two squares sharing one edge: 7 unique edges (4 + 4 - 1 shared), and the
# shared edge's own two endpoints each touch 3 ways (their own square's
# other side, plus the wall), so both are junctions and the outline splits
# into 3 curves: the wall itself, and the two outer arcs either side of it.
# --------------------------------------------------------------------------


def test_two_squares_sharing_an_edge_yield_seven_edges_and_three_curves():
    square_a = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    square_b = [(1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (1.0, 1.0)]  # shares (1,0)-(1,1)

    result = boundary_curves_with_counts([[square_a], [square_b]])

    assert isinstance(result, BoundaryCurvesResult)
    assert result.rings_in == 2
    assert result.rings_deduped == 2  # different shapes, neither is a duplicate
    assert result.edges_in == 8  # 4 + 4, nothing zero-length
    assert result.edges_deduped == 7  # the shared wall counted once, not twice
    assert result.edges_pruned == 0  # no degree-1 vertex anywhere in this graph
    assert result.curves_out == 3


def test_two_squares_sharing_an_edge_produces_the_hand_computed_outline():
    square_a = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    square_b = [(1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (1.0, 1.0)]

    curves = boundary_curves([[square_a], [square_b]])

    # Hand-derived: both (1,0) and (1,1) are junctions (degree 3), so the
    # graph splits into exactly these three curves. Each curve's own
    # direction is fixed to put its smaller endpoint first (both endpoints
    # here are (1,0) and (1,1), so every curve starts at (1,0)); the list
    # itself is sorted by full point sequence, which breaks the resulting
    # three-way tie on the second point.
    assert curves == [
        [(1.0, 0.0), (0.0, 0.0), (0.0, 1.0), (1.0, 1.0)],  # A's own outer arc
        [(1.0, 0.0), (1.0, 1.0)],  # the party wall, kept once
        [(1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (1.0, 1.0)],  # B's own outer arc
    ]


# --------------------------------------------------------------------------
# Duplicate rings: the same square twice must collapse to one, whether the
# second copy is written identically or merely rotated and reversed.
# --------------------------------------------------------------------------

# Both variants below collapse to this same canonical closed square: rotated
# to start at its own smallest vertex (0,0), wound whichever direction makes
# the second vertex smaller ((0,1) beats (1,0)), first point repeated last.
_CANONICAL_UNIT_SQUARE = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]


def test_duplicate_ring_identical_order_collapses_to_one():
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

    result = boundary_curves_with_counts([[square], [list(square)]])

    assert result.rings_in == 2
    assert result.rings_deduped == 1
    assert result.edges_in == 4
    assert result.edges_deduped == 4
    assert result.curves_out == 1
    assert result.curves == [_CANONICAL_UNIT_SQUARE]


def test_duplicate_ring_reversed_and_rotated_collapses_to_one():
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    # Reverse square's winding, then rotate to start at (1,1): this is the
    # identical shape (same 4 undirected edges), never written the same way
    # twice, which is exactly what two different authority exports of the
    # same boundary-crossing parcel would look like.
    reversed_and_rotated = [(1.0, 1.0), (1.0, 0.0), (0.0, 0.0), (0.0, 1.0)]

    result = boundary_curves_with_counts([[square], [reversed_and_rotated]])

    assert result.rings_deduped == 1
    assert result.curves == [_CANONICAL_UNIT_SQUARE]


def test_rounding_collapses_near_duplicate_vertices_within_the_entry_tolerance():
    """Rounding happens once, at entry: a second copy whose own vertices
    are each within half a centimetre of the first must round to the
    exact same points and dedupe as an identical ring, not survive as a
    near-miss that only edge dedup, or nothing at all, catches.
    """
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    perturbed = [
        (0.004, -0.003),
        (0.997, 0.001),
        (1.003, 0.998),
        (-0.002, 1.004),
    ]

    result = boundary_curves_with_counts([[square], [perturbed]])

    assert result.rings_deduped == 1
    assert result.curves == [_CANONICAL_UNIT_SQUARE]


# --------------------------------------------------------------------------
# Zero-length edges and degenerate rings, both a rounding consequence.
# --------------------------------------------------------------------------


def test_zero_length_edge_after_rounding_is_dropped_at_extraction():
    """A ring with an extra vertex that rounds down onto its own neighbour
    must extract the same 4 edges a plain unit square would, not 5 with
    one of them zero-length.
    """
    ring_with_rounding_artifact = [
        (0.0, 0.0),
        (0.003, 0.001),  # rounds to (0.0, 0.0), the same as the point before it
        (1.0, 0.0),
        (1.0, 1.0),
        (0.0, 1.0),
    ]

    result = boundary_curves_with_counts([[ring_with_rounding_artifact]])

    assert result.rings_in == 1
    assert result.rings_deduped == 1
    assert result.edges_in == 4  # not 5: the zero-length edge never counted in
    assert result.edges_deduped == 4
    assert result.curves == [_CANONICAL_UNIT_SQUARE]


def test_ring_with_fewer_than_three_distinct_vertices_after_rounding_is_dropped():
    degenerate = [(0.001, 0.001), (0.002, -0.001), (0.003, 0.002)]  # all round to (0.0, 0.0)
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

    result = boundary_curves_with_counts([[degenerate], [square]])

    assert result.rings_in == 2
    assert result.rings_deduped == 1  # the degenerate ring never reaches stage 2
    assert result.curves == [_CANONICAL_UNIT_SQUARE]


# --------------------------------------------------------------------------
# Ring input shape: closed (first == last, per GML) or unclosed must be
# treated identically once normalised.
# --------------------------------------------------------------------------


def test_closed_and_unclosed_ring_input_produce_the_same_curve():
    unclosed = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    closed = unclosed + [unclosed[0]]

    assert boundary_curves([[unclosed]]) == boundary_curves([[closed]])
    assert boundary_curves([[closed]]) == [_CANONICAL_UNIT_SQUARE]


def test_closed_and_unclosed_copies_of_the_same_ring_dedupe_against_each_other():
    unclosed = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    closed = unclosed + [unclosed[0]]

    result = boundary_curves_with_counts([[unclosed], [closed]])

    assert result.rings_in == 2
    assert result.rings_deduped == 1


# --------------------------------------------------------------------------
# T-junction: three arms meeting at one point end every chain that touches
# it, none of them running through it.
# --------------------------------------------------------------------------


def test_t_junction_ends_chains_at_the_junction_vertex():
    # Two triangles sharing the base edge (0,0)-(2,0): both its endpoints
    # end up touching 3 ways (their own triangle's other side, plus the
    # shared base), so the graph splits into the base itself plus each
    # triangle's own remaining two-edge arm, 3 curves in total, and (0,0)
    # and (2,0) never appear except as a first or last point.
    triangle_up = [(0.0, 0.0), (2.0, 0.0), (1.0, 2.0)]
    triangle_down = [(0.0, 0.0), (2.0, 0.0), (1.0, -2.0)]

    result = boundary_curves_with_counts([[triangle_up], [triangle_down]])

    assert result.edges_in == 6
    assert result.edges_deduped == 5  # the shared base counted once
    assert result.curves_out == 3
    assert result.curves == [
        [(0.0, 0.0), (1.0, -2.0), (2.0, 0.0)],
        [(0.0, 0.0), (1.0, 2.0), (2.0, 0.0)],
        [(0.0, 0.0), (2.0, 0.0)],
    ]

    junction_points = {(0.0, 0.0), (2.0, 0.0)}
    for curve in result.curves:
        interior_points = set(curve[1:-1])
        assert not (interior_points & junction_points), (
            f"a junction point appeared inside a chain, not just at its ends: {curve}"
        )


# --------------------------------------------------------------------------
# Interior ring: a courtyard inside its own parcel, sharing no edge with
# the exterior, survives as its own closed curve.
# --------------------------------------------------------------------------


def test_interior_ring_survives_as_its_own_closed_curve():
    exterior = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    interior = [(1.0, 1.0), (1.0, 2.0), (2.0, 2.0), (2.0, 1.0)]

    result = boundary_curves_with_counts([[exterior, interior]])

    assert result.rings_in == 2
    assert result.rings_deduped == 2
    assert result.curves_out == 2
    # Each ring is unrelated to the other (no shared edges), so both come
    # back as plain closed rings, canonicalised the same way the unit
    # square test above works out by hand.
    assert result.curves == [
        [(0.0, 0.0), (0.0, 4.0), (4.0, 4.0), (4.0, 0.0), (0.0, 0.0)],
        [(1.0, 1.0), (1.0, 2.0), (2.0, 2.0), (2.0, 1.0), (1.0, 1.0)],
    ]


# --------------------------------------------------------------------------
# Repeated minimum vertex: a ring that touches the same point twice (a
# pinch point) has more than one rotation that starts at its own smallest
# vertex, and `_ring_key`/`_canonical_direction` must try every one of
# them, not just whichever `list.index` finds first, or two physically
# identical rings recorded starting at different pinch occurrences hash
# unequal. Code review counterexample.
# --------------------------------------------------------------------------


def test_ring_key_is_canonical_when_the_minimum_vertex_repeats():
    # ring4 is ring3 phase-shifted to start at the SECOND occurrence of
    # (0,0) rather than the first: the identical cyclic sequence.
    ring3 = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0), (2.0, 0.0), (2.0, 1.0)]
    ring4 = ring3[3:] + ring3[:3]
    assert ring3 != ring4  # different as plain lists...
    assert _ring_key(ring3) == _ring_key(ring4)  # ...but the same ring


def test_duplicated_pinch_point_ring_dedupes_to_one():
    ring3 = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0), (2.0, 0.0), (2.0, 1.0)]
    ring4 = ring3[3:] + ring3[:3]

    result = boundary_curves_with_counts([[ring3], [ring4]])

    assert result.rings_in == 2
    assert result.rings_deduped == 1  # the two authority files' copies of one ring


def test_canonical_direction_is_order_independent_when_minimum_vertex_repeats():
    # The same closed curve (first == last), recorded starting the seam at
    # two different occurrences of its own repeated minimum vertex.
    variant_1 = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 0.0)]
    variant_2 = variant_1[3:-1] + variant_1[:3] + [variant_1[3]]
    assert variant_1 != variant_2
    assert _canonical_direction(variant_1) == _canonical_direction(variant_2)


# --------------------------------------------------------------------------
# The closed-ring seam: a redundant, exactly-collinear vertex must be
# dropped whether it lands in the middle of the raw chain or happens to be
# the vertex chaining started its walk from (the seam). Code review
# counterexample: a rectangle with one extra midpoint on its bottom edge,
# recorded starting at a real corner in one ordering and at that redundant
# midpoint in the other.
#
# `contours.py`'s own `_drop_collinear` does not check its seam this way,
# and is deliberately left as-is (see this task's report): its rings come
# from marching squares, one join pass over segments built in a single,
# fixed scan order, so the same physical ring is never handed to it twice
# starting at two different vertices the way stage 1's own duplicate-ring
# dedup can hand this module two differently-rotated copies of one ring.
# Order-independence is a correctness property HERE specifically because
# stage 1 keeps whichever duplicate ring arrived first, and that arbitrary
# choice must never leak into a different-shaped output curve.
# --------------------------------------------------------------------------


def test_collinear_seam_dropped_regardless_of_which_vertex_the_chain_starts_at():
    # Real corners: (0,0), (2,0), (2,1), (0,1). (1,0) sits exactly on the
    # bottom edge between (0,0) and (2,0), a redundant midpoint.
    starts_at_a_real_corner = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)]
    starts_at_the_redundant_midpoint = [(1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0), (0.0, 0.0)]

    curves_a = boundary_curves([[starts_at_a_real_corner]])
    curves_b = boundary_curves([[starts_at_the_redundant_midpoint]])

    expected = [[(0.0, 0.0), (0.0, 1.0), (2.0, 1.0), (2.0, 0.0), (0.0, 0.0)]]
    assert curves_a == expected
    assert curves_b == expected


# --------------------------------------------------------------------------
# Within-ring spikes: an out-and-back traversal along the same physical
# edge is digitising noise, not a boundary, and must not survive as a
# dangling line fused onto whatever real edge happens to be nearby. Code
# review counterexample plus a two-segment cascade.
# --------------------------------------------------------------------------


def test_within_ring_spike_leaves_no_dangling_line():
    # Walks (0,0) -> (1,0) -> (0,0) -> (0,1) -> back to (0,0): the whole
    # ring is two out-and-back spikes sharing a pivot, with no real
    # enclosed area anywhere, so nothing genuine survives.
    spike_ring = [(0.0, 0.0), (1.0, 0.0), (0.0, 0.0), (0.0, 1.0)]

    result = boundary_curves_with_counts([[spike_ring]])

    assert result.edges_in == 4
    assert result.edges_deduped == 2  # (0,0)-(1,0) and (0,0)-(0,1), each walked twice
    assert result.edges_pruned == 2  # both are spikes: (1,0) and (0,1) are degree-1 dead ends
    assert result.curves == []
    assert result.curves_out == 0


def test_two_segment_spike_cascades_away_leaving_the_real_square():
    # A real 4x4 square, with a 2-segment out-and-back spike
    # ((0,0) -> (1,0) -> (2,0) -> (1,0) -> (0,0)) grafted onto one corner
    # before the ring continues around the square's own real edges.
    ring_with_spike = [
        (0.0, 0.0),
        (1.0, 0.0),
        (2.0, 0.0),
        (1.0, 0.0),
        (0.0, 0.0),
        (4.0, 0.0),
        (4.0, 4.0),
        (0.0, 4.0),
    ]

    result = boundary_curves_with_counts([[ring_with_spike]])

    assert result.edges_in == 8
    # Unique edges before pruning: (0,0)-(1,0), (1,0)-(2,0), (0,0)-(4,0),
    # (4,0)-(4,4), (4,4)-(0,4), (0,4)-(0,0) = 6 (the spike's own return
    # trip collapses onto its outbound trip, same as any shared edge).
    assert result.edges_deduped == 6
    # Pruning cascades: (2,0) is degree 1, removing (1,0)-(2,0) exposes
    # (1,0) as newly degree 1, removing (0,0)-(1,0) in the next pass;
    # the real square's own 4 edges are untouched (2 pruned, 4 remain).
    assert result.edges_pruned == 2
    assert result.curves_out == 1
    assert result.curves == [[(0.0, 0.0), (0.0, 4.0), (4.0, 4.0), (4.0, 0.0), (0.0, 0.0)]]


# --------------------------------------------------------------------------
# Interface shape: boundary_curves is a thin wrapper over the counts
# companion, empty input is handled cleanly, and the two never disagree.
# --------------------------------------------------------------------------


def test_boundary_curves_returns_exactly_the_counts_companions_curves_field():
    parcels = [
        [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]],
        [[(1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (1.0, 1.0)]],
    ]
    assert boundary_curves(parcels) == boundary_curves_with_counts(parcels).curves


def test_empty_input_returns_no_curves():
    result = boundary_curves_with_counts([])
    assert result.curves == []
    assert (
        result.rings_in,
        result.rings_deduped,
        result.edges_in,
        result.edges_deduped,
        result.edges_pruned,
        result.curves_out,
    ) == (0, 0, 0, 0, 0, 0)
    assert boundary_curves([]) == []


# --------------------------------------------------------------------------
# The real fixture: Task 2's own parcels_sample.gml, parsed through
# parcels_in, must show the documented shared-edge pair collapsing.
# --------------------------------------------------------------------------


def test_real_fixture_ring_counts_match_the_known_fixture_shape(tmp_path):
    zip_path = _fixture_zip(tmp_path)
    parcels = list(parcels_in(zip_path, _FIXTURE_BBOX_ALL))

    result = boundary_curves_with_counts(parcels)

    # 10 members, 9 with only an exterior ring and 1 (INSPIREID 16722605)
    # with an exterior plus one interior ring: 11 rings in, and none of
    # them duplicate another (they are 10 distinct real parcels), so all
    # 11 survive into stage 2.
    assert result.rings_in == 11
    assert result.rings_deduped == 11


def test_real_fixture_shared_edge_collapses_at_the_edge_dedup_stage(tmp_path):
    """The shared-edge pair Task 2's report documents by name (members 1
    and 2, INSPIREID 16721537 and 16721605, sharing the edge between
    (313548.47, 168920.19) and (313538.26, 168925.58), the second ring
    carrying it reversed immediately before it closes) must collapse to
    one copy at stage 2.

    Checked here directly against the stage 1 and 2 helpers, rather than
    against `boundary_curves`'s own finished curves: that shared edge's
    own middle point sits within 4 mm of the straight line between its
    two neighbours on either side (both rings agree on those neighbours
    too, since the whole 3-point run is shared), well inside the 0.05 m
    collinearity tolerance, so stage 3 legitimately simplifies straight
    through it in the finished output. That is real, correct
    simplification, not a sign the edge dedup failed to fire; isolating
    stages 1 and 2 here proves the dedup itself, independently of
    whatever stage 3 goes on to do with the result.
    """
    zip_path = _fixture_zip(tmp_path)
    parcels = list(parcels_in(zip_path, _FIXTURE_BBOX_ALL))

    member_1_ring = _normalise_ring(parcels[0][0])
    member_2_ring = _normalise_ring(parcels[1][0])
    assert member_1_ring is not None and member_2_ring is not None

    edges_in, unique_edges = _extract_unique_edges([member_1_ring, member_2_ring])

    shared_a = (313548.47, 168920.19)
    shared_b = (313538.26, 168925.58)
    shared_key = _edge_key(shared_a, shared_b)

    # Hand-counted from the fixture's own posList tokens (see
    # make_gml_fixture.py's committed output): member 1's ring has 10
    # distinct vertices (11 posList pairs, the last repeating the first
    # per GML), member 2's has 9 (10 pairs), 19 real, non-zero-length
    # edges between them. The two parcels' shared property line is not
    # just the one documented edge: member 1 walks
    # (548.47,920.19) -> (538.26,925.58) -> (533.65,928.01) -> (531.15,929.25)
    # and member 2 walks the same 4 points in reverse, so all 3 of those
    # edges are shared, each collapsing from 2 copies to 1 (19 - 3 = 16).
    assert edges_in == 19
    assert len(unique_edges) == 16
    assert unique_edges.count(shared_key) == 1


# --------------------------------------------------------------------------
# Performance sanity: a 50 x 50 grid of adjacent unit squares.
# --------------------------------------------------------------------------


def test_grid_of_adjacent_squares_dedupes_edges_and_completes_promptly():
    size = 50
    parcels = []
    for row in range(size):
        for col in range(size):
            square = [
                (float(col), float(row)),
                (float(col + 1), float(row)),
                (float(col + 1), float(row + 1)),
                (float(col), float(row + 1)),
            ]
            parcels.append([square])

    started = time.perf_counter()
    result = boundary_curves_with_counts(parcels)
    elapsed = time.perf_counter() - started

    assert result.rings_in == size * size
    assert result.rings_deduped == size * size
    assert result.edges_in == size * size * 4

    # A full size x size grid graph has size*(size+1) horizontal edges and
    # size*(size+1) vertical edges: 2 * 50 * 51 = 5100, the plan's own
    # figure for this exact grid.
    assert result.edges_deduped == 2 * size * (size + 1)
    assert result.edges_pruned == 0  # every vertex in a filled grid keeps degree >= 2
    assert result.curves_out > 0

    # Generous on purpose (see the task brief): a guardrail against
    # minutes, not a tight performance pin that would be flaky on a
    # loaded machine.
    assert elapsed < 60.0
