"""tests/test_canopy.py"""
import math
from array import array

import pytest

from mapgen.bng import _NODE_COUNT, Ostn15Grid
from mapgen.canopy import (
    CANOPY_MIN_HEIGHT_METRES,
    CanopyError,
    CanopyRecord,
    build_canopy,
)
from mapgen.cog import BngWindow


def _zero_shift_grid() -> Ostn15Grid:
    """An OSTN15 grid every node of which carries a (0, 0) shift.

    to_bng then equals tm_forward exactly, and tm_inverse is its
    exact inverse, which is what lets a test choose BNG coordinates
    directly and get them back out of the fusion almost unchanged. This is
    not a real OSTN15 grid (the real one never shifts by exactly zero
    anywhere), and it does not need to be: nothing under test cares what
    the shift IS, only that projecting a node succeeds.
    """
    shifts = array("f", [0.0]) * (_NODE_COUNT * 2)
    return Ostn15Grid(shifts)


def _window(values, size=12, e0=318000.0, n0=176000.0, pixel=1.0):
    return BngWindow(
        e_origin=e0, n_top=n0 + size * pixel, pixel_size=pixel,
        width=size, height=size, values=array("f", values),
    )


def _flat(level, size=12):
    return [level] * (size * size)


class TestBuildCanopy:
    def test_a_tree_cluster_becomes_one_point(self):
        grid = _zero_shift_grid()
        dsm = _flat(100.0)
        # A 3x3 block of 8 m canopy: rows 4-6, cols 4-6.
        for row in range(4, 7):
            for col in range(4, 7):
                dsm[row * 12 + col] = 108.0
        features, record = build_canopy(
            _window(_flat(100.0)), _window(dsm), [], grid
        )
        assert record == CanopyRecord(points=1, skipped_small=0, resolution_m=1.0)
        (feature,) = features
        assert feature["geometry"]["type"] == "Point"
        assert feature["properties"]["height"] == 8.0
        assert abs(feature["properties"]["crown_radius"] - math.sqrt(9.0 / math.pi)) < 0.06
        assert "vegetation and other above-ground features" in feature["properties"]["note"]

    def test_single_spike_pixel_is_skipped(self):
        grid = _zero_shift_grid()
        dsm = _flat(100.0)
        dsm[5 * 12 + 5] = 140.0
        features, record = build_canopy(
            _window(_flat(100.0)), _window(dsm), [], grid
        )
        assert features == []
        assert record.skipped_small == 1

    def test_canopy_inside_a_footprint_is_masked(self):
        grid = _zero_shift_grid()
        dsm = _flat(100.0)
        for row in range(4, 7):
            for col in range(4, 7):
                dsm[row * 12 + col] = 108.0
        # A footprint ring covering exactly that block.
        e0, n0 = 318000.0, 176000.0
        ring = [(e0 + 3.5, n0 + 12.0 - 7.5), (e0 + 7.5, n0 + 12.0 - 7.5),
                (e0 + 7.5, n0 + 12.0 - 3.5), (e0 + 3.5, n0 + 12.0 - 3.5)]
        features, record = build_canopy(
            _window(_flat(100.0)), _window(dsm), [ring], grid
        )
        assert features == []
        assert record.points == 0

    def test_below_threshold_is_ground_not_canopy(self):
        grid = _zero_shift_grid()
        dsm = _flat(100.0 + CANOPY_MIN_HEIGHT_METRES - 0.1)
        features, record = build_canopy(
            _window(_flat(100.0)), _window(dsm), [], grid
        )
        assert features == []

    def test_mismatched_windows_refused(self):
        grid = _zero_shift_grid()
        with pytest.raises(CanopyError):
            build_canopy(
                _window(_flat(100.0), size=12),
                _window(_flat(100.0), size=12, e0=999000.0),
                [],
                grid,
            )
