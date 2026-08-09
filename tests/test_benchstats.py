from __future__ import annotations

import math

import pytest

from mapgen import benchstats
from mapgen.benchstats import (
    MATCH_IOU_FLOOR,
    MatchResult,
    OffsetStats,
    distribution,
    match_footprints,
    polyline_offsets,
    sampled_iou,
)

# --------------------------------------------------------------------------
# Shared synthetic geometry helpers. Plain BNG-style (easting, northing)
# numbers, small for hand-checking; rings are exterior-only and never
# explicitly closed (matching buildings.point_in_ring's own convention).
# --------------------------------------------------------------------------


def _rect(e0: float, n0: float, e1: float, n1: float) -> list[tuple[float, float]]:
    return [(e0, n0), (e1, n0), (e1, n1), (e0, n1)]


# --------------------------------------------------------------------------
# sampled_iou
# --------------------------------------------------------------------------


def test_identical_squares_have_iou_near_one():
    square = _rect(0.0, 0.0, 10.0, 10.0)
    iou = sampled_iou(square, square)
    assert iou == pytest.approx(1.0, abs=0.02)


def test_half_overlap_squares_have_iou_near_one_third():
    # Two 10x10 squares, the second shifted up by 5m: intersection is a
    # 10x5 strip (area 50), union is 10x15 minus the strip counted once
    # (area 150), IoU = 50 / 150 = 1/3.
    square_a = _rect(0.0, 0.0, 10.0, 10.0)
    square_b = _rect(0.0, 5.0, 10.0, 15.0)
    iou = sampled_iou(square_a, square_b)
    assert iou == pytest.approx(1.0 / 3.0, abs=0.02)


def test_disjoint_squares_have_iou_zero():
    square_a = _rect(0.0, 0.0, 10.0, 10.0)
    square_b = _rect(1000.0, 1000.0, 1010.0, 1010.0)
    assert sampled_iou(square_a, square_b) == 0.0


def test_concave_l_against_its_bounding_rectangle():
    # A 10x10 square with its top-right 5x5 corner removed: area 75. Its own
    # bounding rectangle is the full 10x10 square, area 100. The L is a
    # subset of the rectangle, so intersection = 75, union = 100,
    # IoU = 0.75 exactly, by hand.
    l_shape = [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (5.0, 5.0), (5.0, 10.0), (0.0, 10.0)]
    bounding_rectangle = _rect(0.0, 0.0, 10.0, 10.0)
    iou = sampled_iou(l_shape, bounding_rectangle)
    assert iou == pytest.approx(0.75, abs=0.03)


def test_sample_cap_engages_and_stays_within_it(monkeypatch):
    # Two 500m-scale rectangles, overlapping by half: intersection 500x250
    # (125,000 m2), union 375,000 m2, exact IoU = 1/3. At the default 0.5m
    # step, the union bbox (500 x 750) would need over 1.5 million grid
    # points, far past the default 20,000 cap, so the cap must engage.
    ring_a = _rect(0.0, 0.0, 500.0, 500.0)
    ring_b = _rect(0.0, 250.0, 500.0, 750.0)

    call_count = 0
    real_point_in_ring = benchstats.point_in_ring

    def counting_point_in_ring(x, y, ring):
        nonlocal call_count
        call_count += 1
        return real_point_in_ring(x, y, ring)

    monkeypatch.setattr(benchstats, "point_in_ring", counting_point_in_ring)

    iou = sampled_iou(ring_a, ring_b, sample_cap=20000)

    # Two point_in_ring calls per grid point (one per ring), so the grid
    # itself held at most sample_cap points.
    assert call_count <= 2 * 20000
    assert iou == pytest.approx(1.0 / 3.0, abs=0.05)


# --------------------------------------------------------------------------
# match_footprints
# --------------------------------------------------------------------------


def test_match_footprints_greedy_pairing_and_leftovers():
    ours = [
        _rect(0.0, 0.0, 10.0, 10.0),  # ours[0]: identical to theirs[0]
        _rect(100.0, 100.0, 110.0, 110.0),  # ours[1]: half-overlaps theirs[1]
    ]
    theirs = [
        _rect(0.0, 0.0, 10.0, 10.0),  # theirs[0]: identical to ours[0], iou=1.0
        _rect(100.0, 105.0, 110.0, 115.0),  # theirs[1]: iou ~1/3 with ours[1]
        _rect(109.0, 109.0, 120.0, 120.0),  # theirs[2]: sliver touching ours[1], iou < 0.1
    ]

    result = match_footprints(ours, theirs)

    assert isinstance(result, MatchResult)
    assert len(result.matched) == 2
    assert result.matched[0][0] == 0
    assert result.matched[0][1] == 0
    assert result.matched[0][2] == pytest.approx(1.0, abs=0.02)
    assert result.matched[1][0] == 1
    assert result.matched[1][1] == 1
    assert result.matched[1][2] == pytest.approx(1.0 / 3.0, abs=0.02)
    assert result.unmatched_ours == []
    assert result.unmatched_theirs == [2]


def test_sliver_overlap_under_the_floor_stays_unmatched():
    # A tiny sliver overlap (1 m2 shared between two much larger squares)
    # sits well under MATCH_IOU_FLOOR and must never be forced into a match.
    ours = [_rect(0.0, 0.0, 10.0, 10.0)]
    theirs = [_rect(9.0, 9.0, 30.0, 30.0)]

    iou = sampled_iou(ours[0], theirs[0])
    assert iou < MATCH_IOU_FLOOR

    result = match_footprints(ours, theirs)
    assert result.matched == []
    assert result.unmatched_ours == [0]
    assert result.unmatched_theirs == [0]


# --------------------------------------------------------------------------
# polyline_offsets
# --------------------------------------------------------------------------


def test_straight_line_shifted_diagonally_recovers_the_offset_vector():
    # A straight segment's own nearest-point projection can only reveal the
    # component of a shift PERPENDICULAR to the segment's own direction:
    # projecting onto a line by definition erases any offset ALONG that
    # line (sliding along a line does not change how far a point is from
    # it), which is exactly what the clamped point-to-segment projection
    # computes. To make a SINGLE straight line recover the FULL diagonal
    # shift by hand, this line runs along (1, -1), a right angle to the
    # (+0.9, +0.9) shift itself (which runs along (1, 1)): the whole shift
    # is then perpendicular to the line, and its full magnitude is what
    # every interior sample's projection reveals.
    unit = 1.0 / math.sqrt(2.0)
    length = 100.0
    end = (length * unit, -length * unit)
    ours = [[(0.0, 0.0), end]]
    theirs = [[(0.9, 0.9), (end[0] + 0.9, end[1] + 0.9)]]

    stats = polyline_offsets(ours, theirs)

    assert isinstance(stats, OffsetStats)
    assert stats.unmatched_samples == 0
    assert stats.mean_de == pytest.approx(0.9, abs=0.01)
    assert stats.mean_dn == pytest.approx(0.9, abs=0.01)
    assert stats.std_de < 0.01
    assert stats.std_dn < 0.01
    assert stats.magnitude_of_mean == pytest.approx(math.sqrt(0.9 ** 2 + 0.9 ** 2), abs=0.01)


def test_perpendicular_segments_each_recover_their_own_axis_no_bias():
    # A horizontal segment's projection can only ever reveal the NORTH
    # component of a shift (moving east along a horizontal line changes
    # nothing about its distance to another horizontal line); a vertical
    # segment can only ever reveal the EAST component, for the identical
    # reason turned sideways. This is the general rule
    # test_straight_line_shifted_diagonally_recovers_the_offset_vector
    # above sidesteps by choosing a diagonal orientation; here it is used
    # directly to prove the projection handles a VERTICAL segment (dx == 0,
    # the case a dx/dy-swapped implementation bug would get wrong) exactly
    # as correctly as a horizontal one, for the same diagonal shift.
    shift_e, shift_n = 0.9, 0.9

    horizontal_ours = [[(0.0, 0.0), (50.0, 0.0)]]
    horizontal_theirs = [[(shift_e, shift_n), (50.0 + shift_e, shift_n)]]
    horizontal_stats = polyline_offsets(horizontal_ours, horizontal_theirs)
    assert horizontal_stats.unmatched_samples == 0
    assert horizontal_stats.mean_dn == pytest.approx(shift_n, abs=0.01)

    vertical_ours = [[(0.0, 0.0), (0.0, 50.0)]]
    vertical_theirs = [[(shift_e, shift_n), (shift_e, 50.0 + shift_n)]]
    vertical_stats = polyline_offsets(vertical_ours, vertical_theirs)
    assert vertical_stats.unmatched_samples == 0
    assert vertical_stats.mean_de == pytest.approx(shift_e, abs=0.01)


def test_sample_beyond_search_radius_lands_in_unmatched_samples():
    # ours is a 100m line sampled every 5m (21 samples: 0, 5, ..., 100).
    # theirs only covers the first 21m, so samples past e=35 project onto
    # the clamped endpoint (21, 0) at a distance over 15m and go unmatched.
    ours = [[(0.0, 0.0), (100.0, 0.0)]]
    theirs = [[(0.0, 0.0), (21.0, 0.0)]]

    stats = polyline_offsets(ours, theirs, sample_every=5.0, search_radius=15.0)

    # e in {0, 5, ..., 35}: distance to the clamped nearest point is at most
    # 14m (e=35 -> distance 14), all within the 15m radius: 8 samples.
    # e in {40, ..., 100}: distance is at least 19m, all beyond the radius:
    # 13 samples.
    assert stats.count == 8
    assert stats.unmatched_samples == 13
    assert stats.count + stats.unmatched_samples == 21


def test_search_radius_wider_than_cell_size_still_finds_a_real_match():
    # task-2-review.md's own Important finding: a fixed 3x3 cell-block
    # search (span=1) only reaches one OFFSET_CELL_SIZE_M (25m) cell in
    # every direction, so it silently misses a real match once
    # search_radius is widened past that cell size. Sample at (24, 0) sits
    # in cell column 0 (floor(24/25)); a real segment at e=50 sits in cell
    # column 2 (floor(50/25)), two columns over, at a true distance of 26m.
    ours = [[(24.0, 0.0), (24.0, 0.0)]]
    theirs = [[(50.0, -5.0), (50.0, 5.0)]]

    # Control: at the default-sized 15m radius, 26m is correctly out of
    # range regardless of the cell search span, so this must be unchanged
    # by the fix.
    control = polyline_offsets(ours, theirs, search_radius=15.0)
    assert control.count == 0
    assert control.unmatched_samples == 2

    # At search_radius=30.0, 26m is well inside it: the neighbourhood span
    # must now be derived from the actual radius (ceil(30 / 25) == 2) to
    # reach cell column 2, two columns over from the sample's own column 0.
    widened = polyline_offsets(ours, theirs, search_radius=30.0)
    assert widened.count == 2
    assert widened.unmatched_samples == 0
    assert widened.mean_de == pytest.approx(26.0, abs=0.001)
    assert widened.mean_dn == pytest.approx(0.0, abs=0.001)


# --------------------------------------------------------------------------
# distribution
# --------------------------------------------------------------------------


def test_distribution_of_empty_values_is_empty_dict():
    assert distribution([]) == {}


def test_distribution_matches_heights_percentile_hand_check():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    result = distribution(values, fractions=(0.1, 0.5, 0.9))
    assert result[0.1] == pytest.approx(1.9)
    assert result[0.5] == pytest.approx(5.5)
    assert result[0.9] == pytest.approx(9.1)
