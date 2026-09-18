import pytest

from mapgen.planar import (
    clip_polygon,
    clip_ring,
    grid_shape,
    open_ring,
    point_in_rect,
    polygon_area_in_rect,
    ring_area,
    share_of_rect,
)

SQUARE_2 = [(0, 0), (2, 0), (2, 2), (0, 2)]


def test_open_ring_drops_the_closing_point_and_any_third_ordinate():
    assert open_ring([[0, 0, 5], [1, 0, 5], [1, 1, 5], [0, 0, 5]]) == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]


def test_ring_area_is_the_same_open_or_closed():
    assert ring_area(SQUARE_2) == 4.0
    assert ring_area(SQUARE_2 + [(0, 0)]) == 4.0
    assert ring_area([(0, 0), (1, 1)]) == 0.0


def test_clip_ring_keeps_only_the_overlap():
    clipped = clip_ring(SQUARE_2, (1, 1, 3, 3))
    assert ring_area(clipped) == pytest.approx(1.0)


def test_clip_ring_wholly_outside_is_empty():
    assert clip_ring(SQUARE_2, (5, 5, 6, 6)) == []


def test_polygon_area_in_rect_subtracts_holes():
    outer = [(0, 0), (4, 0), (4, 4), (0, 4)]
    hole = [(1, 1), (3, 1), (3, 3), (1, 3)]
    assert polygon_area_in_rect([outer, hole], (0, 0, 4, 4)) == pytest.approx(12.0)


def test_a_rect_inside_a_hole_touches_nothing():
    outer = [(0, 0), (4, 0), (4, 4), (0, 4)]
    hole = [(1, 1), (3, 1), (3, 3), (1, 3)]
    assert polygon_area_in_rect([outer, hole], (1.5, 1.5, 2.5, 2.5)) == 0.0


def test_clip_polygon_drops_holes_that_leave_nothing():
    outer = [(0, 0), (4, 0), (4, 4), (0, 4)]
    hole = [(3.5, 3.5), (3.9, 3.5), (3.9, 3.9), (3.5, 3.9)]
    clipped = clip_polygon([outer, hole], (0, 0, 2, 2))
    assert len(clipped) == 1
    assert ring_area(clipped[0]) == pytest.approx(4.0)


def test_point_in_rect_includes_the_edges():
    assert point_in_rect((0, 0), (0, 0, 1, 1))
    assert point_in_rect((1, 1), (0, 0, 1, 1))
    assert not point_in_rect((1.0001, 0.5), (0, 0, 1, 1))


def test_grid_shape_follows_the_true_width():
    assert grid_shape((0, 0, 10, 5), 10) == (10, 5)
    # x_scale halves the true width, so a 2 x 1 rect is really square.
    assert grid_shape((0, 0, 2, 1), 400, x_scale=0.5) == (400, 400)


def test_share_of_rect_left_half():
    left_half = [[(0, 0), (5, 0), (5, 10), (0, 10)]]
    assert share_of_rect([left_half], (0, 0, 10, 10), 10) == pytest.approx(0.5)


def test_share_of_rect_counts_overlapping_polygons_once():
    left_half = [[(0, 0), (5, 0), (5, 10), (0, 10)]]
    assert share_of_rect([left_half, left_half], (0, 0, 10, 10), 10) == pytest.approx(0.5)


def test_share_of_rect_respects_holes():
    outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(2, 2), (8, 2), (8, 8), (2, 8)]
    # 100 cells, the 6 x 6 cell centres inside the hole are not covered.
    assert share_of_rect([[outer, hole]], (0, 0, 10, 10), 10) == pytest.approx(0.64)


def test_share_of_rect_whole_and_none():
    whole = [[(-1, -1), (11, -1), (11, 11), (-1, 11)]]
    assert share_of_rect([whole], (0, 0, 10, 10), 10) == 1.0
    assert share_of_rect([], (0, 0, 10, 10), 10) == 0.0
