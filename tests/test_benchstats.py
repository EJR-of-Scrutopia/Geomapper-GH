from __future__ import annotations

import math

import pytest

from mapgen import benchstats
from mapgen.benchstats import (
    AREA_BUCKET_LABELS,
    LSQ_MIN_SAMPLES,
    MATCH_CELL_SIZE_M,
    MATCH_IOU_FLOOR,
    ContainmentResult,
    MatchResult,
    OffsetStats,
    area_bucket,
    area_histogram,
    classify_containment,
    distribution,
    match_footprints,
    polyline_offsets,
    ring_area,
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
# classify_containment
#
# The question underneath every test here: an unmatched footprint is either
# standing on ground the other dataset already covers (the two datasets
# drawing the same building differently, an NGD building PART against a
# whole terrace of ours) or standing on ground the other dataset holds
# nothing on (a real gap). A raw unmatched count cannot tell those apart.
# --------------------------------------------------------------------------


def _u_shape() -> list[tuple[float, float]]:
    """A U opening east: a 10 m wide spine at easting 0 to 10 running the
    full 0 to 30 m height, with two 20 m arms at the bottom and the top,
    and a notch (easting 10 to 30, northing 10 to 20) that is OUTSIDE the
    shape entirely.

    Its own VERTEX AVERAGE is (17.5, 15), squarely in that notch and so
    outside the shape; its scanline representative point is (5, 15), in
    the spine. Every ordinary building shape with a rear return, a
    courtyard or an L plan has this property, which is why
    `classify_containment` uses `buildings.representative_point` and not
    a centroid (that function's own docstring, code review task-6-review
    Critical C1).
    """
    return [
        (0.0, 0.0),
        (30.0, 0.0),
        (30.0, 10.0),
        (10.0, 10.0),
        (10.0, 20.0),
        (30.0, 20.0),
        (30.0, 30.0),
        (0.0, 30.0),
    ]


def test_containment_footprint_wholly_inside_another_is_contained():
    subject = _rect(4.0, 4.0, 6.0, 6.0)
    other = _rect(0.0, 0.0, 20.0, 20.0)

    result = classify_containment([subject], [other])

    assert isinstance(result, ContainmentResult)
    assert result.contained == [0]
    assert result.not_contained == []


def test_containment_footprint_wholly_outside_is_not_contained():
    subject = _rect(100.0, 100.0, 110.0, 110.0)
    other = _rect(0.0, 0.0, 20.0, 20.0)

    result = classify_containment([subject], [other])

    assert result.contained == []
    assert result.not_contained == [0]


def test_containment_straddling_an_edge_is_decided_by_the_point_not_the_bbox():
    # Both subjects overlap the other footprint's own bounding box, and
    # both genuinely straddle its eastern edge at easting 20. The first
    # has most of its own body inside, so its interior point (17.0, 10.0)
    # lands inside; the second has most of its body outside, so its
    # interior point (23.0, 10.0) does not. A bbox-overlap test alone
    # would call both of them contained, which is exactly the wrong
    # answer for the second: a footprint mostly on empty ground is not
    # evidence that the ground is covered.
    other = _rect(0.0, 0.0, 20.0, 20.0)
    mostly_inside = _rect(12.0, 8.0, 22.0, 12.0)
    mostly_outside = _rect(18.0, 8.0, 28.0, 12.0)

    result = classify_containment([mostly_inside, mostly_outside], [other])

    assert result.contained == [0]
    assert result.not_contained == [1]


def test_containment_uses_the_scanline_point_never_the_vertex_average():
    # The U's own vertex average (17.5, 15) sits in its notch, outside
    # both the U itself and the `other` rectangle below; its real interior
    # point (5, 15) sits inside both. `other` is drawn deliberately narrow
    # (easting 0 to 8) so the two candidate points give OPPOSITE answers:
    # a centroid-based implementation reports this U as standing on empty
    # ground, when it is standing squarely on covered ground.
    subject = _u_shape()
    other = _rect(0.0, 12.0, 8.0, 18.0)

    result = classify_containment([subject], [other])

    assert result.contained == [0]
    assert result.not_contained == []


def test_containment_finds_a_large_other_registered_across_many_cells():
    # MATCH_CELL_SIZE_M is 50 m, so a 200 m building spans five cell
    # columns and five cell rows. The subject sits deep inside it, four
    # cells away from the big ring's own corner: found only because every
    # ring is registered under EVERY cell its bbox touches, which is what
    # makes a single-cell point query exhaustive (`_cell_index`'s own
    # docstring). A corner-cell-only index would answer "not contained"
    # here, and would do it silently.
    assert MATCH_CELL_SIZE_M == 50.0
    subject = _rect(120.0, 130.0, 122.0, 132.0)
    other = _rect(0.0, 0.0, 200.0, 200.0)

    result = classify_containment([subject], [other])

    assert result.contained == [0]


def test_containment_partitions_every_subject_exactly_once():
    subjects = [
        _rect(4.0, 4.0, 6.0, 6.0),  # inside
        _rect(100.0, 100.0, 101.0, 101.0),  # outside
        _rect(1.0, 1.0, 2.0, 2.0),  # inside
        _rect(500.0, 500.0, 501.0, 501.0),  # outside
    ]
    others = [_rect(0.0, 0.0, 20.0, 20.0)]

    result = classify_containment(subjects, others)

    assert result.contained == [0, 2]
    assert result.not_contained == [1, 3]
    assert sorted(result.contained + result.not_contained) == list(range(len(subjects)))


def test_containment_against_an_empty_other_population_is_all_not_contained():
    subjects = [_rect(0.0, 0.0, 10.0, 10.0), _rect(50.0, 50.0, 60.0, 60.0)]

    result = classify_containment(subjects, [])

    assert result.contained == []
    assert result.not_contained == [0, 1]


def test_containment_of_no_subjects_is_two_empty_lists():
    result = classify_containment([], [_rect(0.0, 0.0, 10.0, 10.0)])

    assert result.contained == []
    assert result.not_contained == []


def test_containment_reads_the_subdivision_case_the_benchmark_exists_to_separate():
    # The real shape of the problem: one footprint of ours (a terrace held
    # whole, 30 m by 10 m) against three of theirs (the same terrace as
    # three building PARTS). Whichever part won the pairing is gone by the
    # time this function runs; the other two are what reach it, and both
    # stand inside our single footprint. Nothing is missing here, and this
    # is the classification that says so.
    ours_terrace = _rect(0.0, 0.0, 30.0, 10.0)
    unmatched_parts = [_rect(10.0, 0.0, 20.0, 10.0), _rect(20.0, 0.0, 30.0, 10.0)]

    result = classify_containment(unmatched_parts, [ours_terrace])

    assert result.contained == [0, 1]
    assert result.not_contained == []


# --------------------------------------------------------------------------
# ring_area, area_bucket, area_histogram
# --------------------------------------------------------------------------


def test_ring_area_of_known_squares():
    assert ring_area(_rect(0.0, 0.0, 10.0, 10.0)) == pytest.approx(100.0)
    assert ring_area(_rect(0.0, 0.0, 3.0, 7.0)) == pytest.approx(21.0)
    assert ring_area(_rect(1000.0, 2000.0, 1002.0, 2002.5)) == pytest.approx(5.0)


def test_ring_area_of_an_l_shape():
    # A 10x10 square with its top-right 5x5 corner removed: 100 - 25 = 75,
    # by hand. The same L this file's own sampled_iou test uses.
    l_shape = [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (5.0, 5.0), (5.0, 10.0), (0.0, 10.0)]
    assert ring_area(l_shape) == pytest.approx(75.0)


def test_ring_area_ignores_winding_direction():
    anticlockwise = _rect(0.0, 0.0, 10.0, 10.0)
    clockwise = list(reversed(anticlockwise))
    assert ring_area(clockwise) == pytest.approx(ring_area(anticlockwise))
    assert ring_area(clockwise) > 0.0


def test_ring_area_is_identical_closed_or_unclosed():
    # Every GeoJSON ring repeats its first vertex at the end and every
    # closed OSM way repeats its first node; the wraparound sum makes that
    # repeated edge contribute exactly zero, so both spellings answer the
    # same with no stripping step anywhere.
    unclosed = _rect(0.0, 0.0, 10.0, 10.0)
    closed = unclosed + [unclosed[0]]
    assert ring_area(closed) == pytest.approx(ring_area(unclosed))


def test_ring_area_of_a_degenerate_zero_width_ring_is_zero():
    assert ring_area([(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]) == pytest.approx(0.0)


def test_area_bucket_boundaries_are_half_open_upward():
    assert area_bucket(0.0) == "under_10"
    assert area_bucket(9.999) == "under_10"
    assert area_bucket(10.0) == "10_to_30"
    assert area_bucket(29.999) == "10_to_30"
    assert area_bucket(30.0) == "30_to_80"
    assert area_bucket(79.999) == "30_to_80"
    assert area_bucket(80.0) == "80_to_200"
    assert area_bucket(199.999) == "80_to_200"
    assert area_bucket(200.0) == "200_to_1000"
    assert area_bucket(999.999) == "200_to_1000"
    assert area_bucket(1000.0) == "over_1000"
    assert area_bucket(50_000.0) == "over_1000"


def test_area_histogram_holds_every_bucket_including_the_empty_ones():
    rings = [
        _rect(0.0, 0.0, 2.0, 2.0),  # 4 m2: a bin store
        _rect(0.0, 0.0, 5.0, 4.0),  # 20 m2: a garage
        _rect(0.0, 0.0, 10.0, 10.0),  # 100 m2: an ordinary house
        _rect(0.0, 0.0, 10.0, 12.0),  # 120 m2: another
        _rect(0.0, 0.0, 100.0, 50.0),  # 5000 m2: a supermarket
    ]

    histogram = area_histogram(rings)

    assert list(histogram) == list(AREA_BUCKET_LABELS)
    assert histogram == {
        "under_10": 1,
        "10_to_30": 1,
        "30_to_80": 0,
        "80_to_200": 2,
        "200_to_1000": 0,
        "over_1000": 1,
    }
    assert sum(histogram.values()) == len(rings)


def test_area_histogram_over_a_subset_of_positions_cross_tabulates():
    # The exact shape the report's own cross-tabulation needs:
    # ContainmentResult.contained / not_contained are position lists into
    # the same ring sequence, so one histogram per class comes straight
    # out of them with no re-derivation.
    rings = [
        _rect(0.0, 0.0, 2.0, 2.0),  # 4 m2
        _rect(0.0, 0.0, 10.0, 10.0),  # 100 m2
        _rect(0.0, 0.0, 10.0, 12.0),  # 120 m2
    ]

    assert area_histogram(rings, [0])["under_10"] == 1
    assert sum(area_histogram(rings, [0]).values()) == 1
    assert area_histogram(rings, [1, 2])["80_to_200"] == 2
    assert sum(area_histogram(rings, [1, 2]).values()) == 2
    assert area_histogram(rings, []) == {label: 0 for label in AREA_BUCKET_LABELS}


def test_area_histogram_subsets_from_a_containment_split_add_back_to_the_whole():
    subjects = [
        _rect(4.0, 4.0, 6.0, 6.0),  # 4 m2, inside
        _rect(100.0, 100.0, 110.0, 110.0),  # 100 m2, outside
        _rect(1.0, 1.0, 11.0, 11.0),  # 100 m2, inside
    ]
    others = [_rect(0.0, 0.0, 20.0, 20.0)]

    result = classify_containment(subjects, others)
    whole = area_histogram(subjects)
    contained = area_histogram(subjects, result.contained)
    not_contained = area_histogram(subjects, result.not_contained)

    for label in AREA_BUCKET_LABELS:
        assert whole[label] == contained[label] + not_contained[label]


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
# polyline_offsets: the least-squares bias correction (task-3-brief's
# controller addition, evidence in task-2-review.md). The naive mean above
# only ever recovers the component of a shift PERPENDICULAR to whatever
# segment a sample lands on; these three tests are the review's own two
# executed demonstrations (the two-orientation grid, and a single line)
# turned into pinned, hand-checkable assertions against the LSQ fields.
# --------------------------------------------------------------------------


def test_lsq_recovers_true_shift_on_two_orientation_grid_where_naive_mean_does_not():
    # task-2-review.md's own second demonstration: a purely horizontal and
    # a purely vertical line, both shifted (+0.9, +0.9), combined into one
    # population. The naive mean only recovers about half the true shift
    # (measured there as 0.4714 on each axis against a true 0.9) because a
    # horizontal segment's projection can only ever reveal the north
    # component and a vertical segment's only the east one; the
    # least-squares solve, built from each sample's own segment normal, is
    # designed to recover the full (0.9, 0.9) instead.
    #
    # `theirs` runs well past `ours` at both ends on each axis (-200 to
    # 300 against ours's own 0 to 100): every `ours` sample's nearest
    # point then lands strictly INSIDE its `theirs` segment, never
    # clamped to an endpoint, matching the interior-projection model the
    # least-squares derivation assumes exactly (see OffsetStats's own
    # docstring).
    #
    # The two lines sit far apart (the vertical one at easting 500, well
    # clear of the horizontal one's own 0-100 extent and shifted theirs
    # segment): sharing an origin point would make that one sample
    # genuinely equidistant from both shifted lines (both offsets have
    # the same 0.9 m magnitude there), a real tie this test has no
    # business depending on the implementation's tie-break to resolve.
    shift_e, shift_n = 0.9, 0.9
    horizontal_ours = [(0.0, 0.0), (100.0, 0.0)]
    horizontal_theirs = [(-200.0 + shift_e, shift_n), (300.0 + shift_e, shift_n)]
    vertical_ours = [(500.0, 0.0), (500.0, 100.0)]
    vertical_theirs = [(500.0 + shift_e, -200.0 + shift_n), (500.0 + shift_e, 300.0 + shift_n)]

    stats = polyline_offsets(
        [horizontal_ours, vertical_ours], [horizontal_theirs, vertical_theirs]
    )

    assert isinstance(stats, OffsetStats)
    assert stats.unmatched_samples == 0
    # The naive mean reproduces the review's own measured understatement:
    # exactly half the true shift here, since every clamping leak is
    # eliminated by construction (see above).
    assert stats.mean_de == pytest.approx(0.45, abs=0.001)
    assert stats.mean_dn == pytest.approx(0.45, abs=0.001)
    # The least-squares estimate recovers the true shift instead.
    assert stats.lsq_de == pytest.approx(shift_e, abs=0.01)
    assert stats.lsq_dn == pytest.approx(shift_n, abs=0.01)
    assert stats.lsq_magnitude == pytest.approx(math.hypot(shift_e, shift_n), abs=0.01)


def test_lsq_is_none_for_a_single_orientation_line_singular_system():
    # A single straight line gives every sample the same segment direction
    # (up to sign), so every per-sample unit normal `n` is the same
    # vector: N = sum(n n^T) is then a scalar multiple of one rank-1
    # matrix, determinant exactly (up to float noise) zero. The two-vector
    # shift `s` is genuinely underdetermined from one orientation alone,
    # so this is reported as undefined rather than guessed.
    unit = 1.0 / math.sqrt(2.0)
    length = 100.0
    end = (length * unit, -length * unit)
    ours = [[(0.0, 0.0), end]]
    theirs = [[(0.9, 0.9), (end[0] + 0.9, end[1] + 0.9)]]

    stats = polyline_offsets(ours, theirs)

    assert stats.count > 0
    assert stats.lsq_de is None
    assert stats.lsq_dn is None
    assert stats.lsq_magnitude is None


def test_lsq_is_none_even_when_the_single_lines_shift_is_purely_perpendicular():
    # A single horizontal line, shifted purely north (no east component at
    # all): every interior sample's own offset lies exactly along that
    # line's own normal, so in principle the "across" component of the
    # shift is fully determined by this one orientation alone. This
    # implementation does not special-case that: N is still a rank-1
    # matrix (one orientation is one orientation, regardless of which way
    # the true shift happens to point), so lsq is still reported as
    # undefined here, not as a partial answer. Documented explicitly
    # because the brief leaves this choice to the implementation: a
    # minimum-norm solve could recover the well-determined axis in this
    # special case, but a real road network is never genuinely
    # single-orientation, so this module does not carry that extra
    # machinery for a case its real caller never hits.
    ours = [[(0.0, 0.0), (50.0, 0.0)]]
    theirs = [[(0.0, 0.9), (50.0, 0.9)]]

    stats = polyline_offsets(ours, theirs)

    assert stats.count > 0
    assert stats.mean_dn == pytest.approx(0.9, abs=0.01)
    assert stats.lsq_de is None
    assert stats.lsq_dn is None
    assert stats.lsq_magnitude is None


def test_lsq_fields_are_none_when_there_are_no_matches_at_all():
    ours = [[(0.0, 0.0), (10.0, 0.0)]]
    theirs = [[(1000.0, 1000.0), (1010.0, 1000.0)]]

    stats = polyline_offsets(ours, theirs, search_radius=1.0)

    assert stats.count == 0
    assert stats.lsq_de is None
    assert stats.lsq_dn is None
    assert stats.lsq_magnitude is None


# --------------------------------------------------------------------------
# polyline_offsets: task-3-review.md's own Important finding 2, fixed here.
# A CLAMPED sample's offset is not purely along its matched segment's own
# normal (only an interior sample's is), so the naive `N`/`V` shortcut is
# only exact once clamped samples are excluded from it. These tests are
# the review's own executed demonstrations (a same-extent two-orientation
# grid, and a short 20 m version of it) turned into pinned assertions:
# before this fix both overshot the true shift (0.943 and a magnitude of
# 1.527 respectively, the second past the epoch report's own CONSISTENT
# upper band); after it, both answer honestly.
# --------------------------------------------------------------------------


def test_lsq_recovers_true_shift_on_a_same_extent_two_orientation_grid():
    # task-3-review.md's own Important finding 2, executed construction:
    # unlike this file's own two-orientation LSQ test above, `theirs` is
    # NOT extended past `ours` here. This is the realistic case (a real
    # OSM way and its matching NGD roadlink describe the same physical
    # road, split at the same junctions, so their extents are naturally
    # similar, never one artificially longer), and it is exactly the case
    # that used to overshoot: before the interior-only fix this construction
    # answered lsq=(0.943, 0.943) against a true (0.9, 0.9).
    shift_e, shift_n = 0.9, 0.9
    horizontal_ours = [(0.0, 0.0), (100.0, 0.0)]
    horizontal_theirs = [(shift_e, shift_n), (100.0 + shift_e, shift_n)]
    vertical_ours = [(500.0, 0.0), (500.0, 100.0)]
    vertical_theirs = [(500.0 + shift_e, shift_n), (500.0 + shift_e, 100.0 + shift_n)]

    stats = polyline_offsets(
        [horizontal_ours, vertical_ours], [horizontal_theirs, vertical_theirs]
    )

    assert stats.unmatched_samples == 0
    # The naive mean still understates it, as always (unaffected by this
    # fix, since the naive statistics never excluded clamped samples).
    assert stats.mean_de == pytest.approx(0.4714, abs=0.001)
    # The least-squares estimate now recovers the true shift cleanly,
    # rather than the 0.943 overshoot the review measured against the
    # pre-fix accumulation.
    assert stats.lsq_de == pytest.approx(shift_e, abs=0.02)
    assert stats.lsq_dn == pytest.approx(shift_n, abs=0.02)
    assert stats.lsq_magnitude == pytest.approx(math.hypot(shift_e, shift_n), abs=0.02)


def test_lsq_on_a_short_same_extent_grid_answers_honestly_never_an_overshoot():
    # task-3-review.md's own second executed construction: the same
    # same-extent shape as above, shortened to 20 m each way (short
    # residential spurs/cul-de-sacs are common in real UK street
    # networks). Before the interior-only fix this answered
    # lsq_magnitude=1.527, PAST the epoch report's own CONSISTENT upper
    # bound (1.5 m) for a road population whose true shift is exactly the
    # 0.9 m hypothesis: a plausible-looking, band-crossing, wrong number.
    #
    # With `sample_every=5.0` (the module's own default) over a 20 m line,
    # densify gives 5 samples per line (0, 5, 10, 15, 20); the one sample
    # at each line's own near end (e=0, shifted 0.9 m away from theirs'
    # own start) projects with a clamped or exactly-boundary parameter
    # and is excluded, leaving exactly 4 interior samples per line, 8
    # total: precisely `LSQ_MIN_SAMPLES`. This implementation therefore
    # answers a real, correct recovery here, not `None` and not an
    # overshoot: documented as the actual behaviour at this exact
    # boundary, per the review's own instruction to assert whichever this
    # implementation yields.
    shift_e, shift_n = 0.9, 0.9
    horizontal_ours = [(0.0, 0.0), (20.0, 0.0)]
    horizontal_theirs = [(shift_e, shift_n), (20.0 + shift_e, shift_n)]
    vertical_ours = [(500.0, 0.0), (500.0, 20.0)]
    vertical_theirs = [(500.0 + shift_e, shift_n), (500.0 + shift_e, 20.0 + shift_n)]

    stats = polyline_offsets(
        [horizontal_ours, vertical_ours], [horizontal_theirs, vertical_theirs]
    )

    assert stats.count == 10
    assert stats.lsq_de is not None
    assert stats.lsq_de == pytest.approx(shift_e, abs=0.02)
    assert stats.lsq_dn == pytest.approx(shift_n, abs=0.02)
    # Never the pre-fix overshoot, and never past the epoch report's own
    # CONSISTENT upper bound for a true 0.9 m shift.
    assert stats.lsq_magnitude == pytest.approx(math.hypot(shift_e, shift_n), abs=0.02)
    assert stats.lsq_magnitude < 1.5


def test_lsq_min_samples_floor_trips_even_on_a_well_conditioned_orientation_mix():
    # A genuinely two-orientation (perfectly orthogonal, well-separated,
    # no cross-contamination) population, but a THIN one: 2 samples per
    # line, 4 total, well under LSQ_MIN_SAMPLES. The determinant of N
    # would in fact be perfectly healthy here (an orthogonal pair is the
    # best-conditioned case there is); this test is what pins that a
    # well-conditioned but thin sample is still refused, not answered,
    # since 4 points is too few to trust regardless of geometry.
    shift_e, shift_n = 0.9, 0.9
    horizontal_ours = [(0.0, 0.0), (10.0, 0.0)]
    horizontal_theirs = [(-200.0 + shift_e, shift_n), (300.0 + shift_e, shift_n)]
    vertical_ours = [(500.0, 0.0), (500.0, 10.0)]
    vertical_theirs = [(500.0 + shift_e, -200.0 + shift_n), (500.0 + shift_e, 300.0 + shift_n)]

    stats = polyline_offsets(
        [horizontal_ours, vertical_ours],
        [horizontal_theirs, vertical_theirs],
        sample_every=10.0,
    )

    assert 0 < stats.count < LSQ_MIN_SAMPLES
    assert stats.lsq_de is None
    assert stats.lsq_dn is None
    assert stats.lsq_magnitude is None


def test_lsq_relative_determinant_floor_traps_the_window_a_fixed_floor_missed():
    # task-3-review.md's own Minor finding 1, executed construction: two
    # well-separated (non-contaminating), well-populated orientations a
    # tiny fraction of a degree apart. Under the OLD absolute
    # `_LSQ_DET_FLOOR = 1e-9`, the review measured a real window (roughly
    # 1e-4 to 3e-5 degrees of separation) where the system had not yet
    # been floored to None but was already numerically unstable enough to
    # answer more than double the true shift. The RELATIVE floor closes
    # that window: even 0.01 degrees of separation, with dozens of
    # samples on each line (comfortably more than the old floor's own
    # blind spot needed to misbehave), now reports None rather than a
    # wrong number.
    shift_e, shift_n = 0.9, 0.9

    def _line(angle_degrees: float, length: float, origin: tuple[float, float]):
        radius = math.radians(angle_degrees)
        ox, oy = origin
        return [(ox, oy), (ox + math.cos(radius) * length, oy + math.sin(radius) * length)]

    def _extended_theirs(ours_line, extend: float = 300.0):
        (x0, y0), (x1, y1) = ours_line
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        ux, uy = dx / length, dy / length
        return [
            (x0 - ux * extend + shift_e, y0 - uy * extend + shift_n),
            (x1 + ux * extend + shift_e, y1 + uy * extend + shift_n),
        ]

    a_ours = _line(0.0, 100.0, (0.0, 0.0))
    b_ours = _line(0.01, 100.0, (2_000.0, 2_000.0))  # far apart: no cross-contamination
    stats = polyline_offsets(
        [a_ours, b_ours], [_extended_theirs(a_ours), _extended_theirs(b_ours)]
    )
    assert stats.count >= LSQ_MIN_SAMPLES
    assert stats.lsq_de is None
    assert stats.lsq_dn is None
    assert stats.lsq_magnitude is None

    # The brief's own pinned 2-degree case, well clear of that window,
    # still answers cleanly: the relative floor does not overcorrect.
    c_ours = _line(0.0, 100.0, (0.0, 0.0))
    d_ours = _line(2.0, 100.0, (2_000.0, 2_000.0))
    clean = polyline_offsets(
        [c_ours, d_ours], [_extended_theirs(c_ours), _extended_theirs(d_ours)]
    )
    assert clean.lsq_de == pytest.approx(shift_e, abs=0.001)
    assert clean.lsq_dn == pytest.approx(shift_n, abs=0.001)


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
