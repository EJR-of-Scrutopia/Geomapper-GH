"""Marching squares over a BngWindow, checked against hand-derived geometry.

Every geometric test here builds a synthetic BngWindow from a known height
function (a cone, a plane, a saddle) rather than real LiDAR, because the
point of these tests is to pin the algorithm against an answer that can be
computed independently of it: a plane's contours are straight lines at
e = level / slope, a cone's are circles at radius = R - level, and a saddle
has exactly one correct disambiguation. Only the WGS84 and GeoJSON tests
touch real geodesy, using the same OSTN15 fixture slice test_bng.py uses
(see tests/fixtures/ostn15/make_fixture.py for how it was built), and stay
within the fixture's own TP06 block so from_bng never has to guess.
"""

from __future__ import annotations

import json
import math
import time
from array import array

import pytest

from mapgen.bng import from_bng
from mapgen.cog import BngWindow
from mapgen.contours import (
    contour_intervals_for,
    generate_contours,
    write_contour_files,
)
from tests.fixtures.ostn15 import make_fixture

# TP06 (Bridgend), the Welsh station the OSTN15 fixture block was built
# around. Every geodesy-touching test below sits its window near this point
# so the fixture's 3x3 km block (see make_fixture.py) always covers it.
_TP06_E = 292184.87
_TP06_N = 168003.465


@pytest.fixture
def ostn15_fixture_grid():
    return make_fixture.read_slice()


def _make_window(width, height, pixel_size, e_origin, n_top, height_fn, pixel_height=None):
    """A BngWindow whose values are height_fn(easting, northing) at every
    pixel centre, using the exact centre formula BngWindow.sample_bng uses.
    """
    step_n = pixel_height if pixel_height is not None else pixel_size
    values = array("f")
    for row in range(height):
        n = n_top - (row + 0.5) * step_n
        for col in range(width):
            e = e_origin + (col + 0.5) * pixel_size
            values.append(height_fn(e, n))
    return BngWindow(
        e_origin=e_origin,
        n_top=n_top,
        pixel_size=pixel_size,
        width=width,
        height=height,
        values=values,
        pixel_height=pixel_height,
    )


# --------------------------------------------------------------------------
# Segment intersection, for the saddle test. A minimal, exact-enough
# implementation: it is only ever asked whether two short line segments
# built by this same test file cross, never anything adversarial.
# --------------------------------------------------------------------------


def _orientation(a, b, c):
    value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if abs(value) < 1e-9:
        return 0
    return 1 if value > 0 else -1


def _segments_properly_intersect(p1, p2, p3, p4):
    """True only for a transversal crossing, never for a shared endpoint.

    Adjacent segments of a joined polyline share an endpoint by
    construction; that is connectivity, not a self-cross, so any zero
    orientation (collinear or touching) reads as "does not properly
    intersect" here.
    """
    o1 = _orientation(p1, p2, p3)
    o2 = _orientation(p1, p2, p4)
    o3 = _orientation(p3, p4, p1)
    o4 = _orientation(p3, p4, p2)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


# --------------------------------------------------------------------------
# Geometry: a cone, a plane, a gap, a saddle.
# --------------------------------------------------------------------------


def test_single_cone_produces_concentric_closed_rings():
    # height = R - distance from the origin: a cone with its peak at the
    # window's centre, tall enough that levels well inside R never reach
    # the window's own edge (radius 10) and so close cleanly.
    radius = 8.0
    window = _make_window(
        21, 21, 1.0, -10.5, 10.5,
        height_fn=lambda e, n: radius - math.hypot(e, n),
    )
    polylines = generate_contours(window, 2.0)

    for level in (2.0, 4.0, 6.0):
        expected_radius = radius - level
        rings = [
            polyline
            for polyline in polylines
            if polyline[0] == polyline[-1]
            and all(
                abs(math.hypot(e, n) - expected_radius) < 0.5
                for e, n in polyline
            )
        ]
        assert rings, (
            f"no closed ring near radius {expected_radius} for level {level}"
        )


def test_planar_ramp_produces_straight_parallel_lines_at_exact_levels():
    # height depends only on easting, so every contour is a perfectly
    # straight north-south line at e = level / slope: the collinearity
    # simplifier should collapse each one to just its two endpoints.
    slope = 0.5
    window = _make_window(
        11, 11, 1.0, -5.5, 5.5,
        height_fn=lambda e, n: slope * e,
    )
    polylines = generate_contours(window, 1.0)

    expected_levels = [-2.0, -1.0, 0.0, 1.0, 2.0]
    assert len(polylines) == len(expected_levels)

    found = sorted(polyline[0][0] for polyline in polylines)
    expected = sorted(level / slope for level in expected_levels)
    for found_e, expected_e in zip(found, expected):
        assert found_e == pytest.approx(expected_e, abs=1e-6)

    for polyline in polylines:
        assert len(polyline) == 2
        (e1, n1), (e2, n2) = polyline
        assert e1 == pytest.approx(e2, abs=1e-6)
        assert {round(n1, 3), round(n2, 3)} == {5.0, -5.0}


def test_nan_region_breaks_lines_rather_than_bridging_it():
    slope = 0.5
    width = height = 11
    e_origin, n_top = -5.5, 5.5
    nan_rows = {4, 5, 6}

    def build(with_gap):
        values = array("f")
        for row in range(height):
            for col in range(width):
                if with_gap and row in nan_rows:
                    values.append(float("nan"))
                else:
                    e = e_origin + (col + 0.5) * 1.0
                    values.append(slope * e)
        return BngWindow(
            e_origin=e_origin, n_top=n_top, pixel_size=1.0,
            width=width, height=height, values=values,
        )

    # interval=10.0 (never the special 5.0 that triggers decimation) isolates
    # a single level, 0.0, the only multiple of 10 the ramp's -2.5..2.5
    # range crosses.
    whole = generate_contours(build(with_gap=False), 10.0)
    assert len(whole) == 1

    split = generate_contours(build(with_gap=True), 10.0)
    assert len(split) == 2
    spans = sorted((min(n for _, n in pl), max(n for _, n in pl)) for pl in split)
    (_, top_of_bottom_piece), (bottom_of_top_piece, _) = spans
    assert top_of_bottom_piece < bottom_of_top_piece


def test_saddle_does_not_self_cross():
    # A single ambiguous cell: TL and BR share one state (above 0), TR and
    # BL the other (below 0), and the cell's own average (1.0) is clearly
    # above the tested level. The standard disambiguation reads the
    # average's state as the connected diagonal, so TL and BR join through
    # the cell's middle (the N and E crossings pair together) while TR and
    # BL are excised as two separate below-level pockets (S pairs with W),
    # never the opposite, self-crossing-adjacent pairing (N-W, E-S). Values
    # chosen so the corner spread is [-4, 6), which interval=10.0 crosses at
    # exactly one level, 0.0, isolating this cell's own two segments with
    # nothing else mixed into the output.
    tl, tr, br, bl = 6.0, -2.0, 4.0, -4.0
    values = array("f", [tl, tr, bl, br])  # row0=[TL,TR], row1=[BL,BR]
    window = BngWindow(
        e_origin=-1.0, n_top=1.0, pixel_size=1.0, width=2, height=2, values=values,
    )
    pt_tl, pt_tr, pt_br, pt_bl = (-0.5, 0.5), (0.5, 0.5), (0.5, -0.5), (-0.5, -0.5)

    def crossing(p1, v1, p2, v2, level=0.0):
        t = (level - v1) / (v2 - v1)
        return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))

    n_point = crossing(pt_tl, tl, pt_tr, tr)
    e_point = crossing(pt_tr, tr, pt_br, br)
    s_point = crossing(pt_br, br, pt_bl, bl)
    w_point = crossing(pt_bl, bl, pt_tl, tl)

    polylines = generate_contours(window, 10.0)
    assert len(polylines) == 2

    def close(p, q):
        return math.isclose(p[0], q[0], abs_tol=1e-9) and math.isclose(p[1], q[1], abs_tol=1e-9)

    def has_pair(a, b):
        return any(
            (close(pl[0], a) and close(pl[-1], b)) or (close(pl[0], b) and close(pl[-1], a))
            for pl in polylines
        )

    assert has_pair(n_point, e_point)
    assert has_pair(s_point, w_point)

    a, b = polylines
    assert not _segments_properly_intersect(a[0], a[-1], b[0], b[-1])


# --------------------------------------------------------------------------
# Interval policy.
# --------------------------------------------------------------------------


def _bare_window(width, height, pixel_size):
    return BngWindow(
        e_origin=0.0, n_top=0.0, pixel_size=pixel_size,
        width=width, height=height, values=array("f", [0.0]) * (width * height),
    )


def test_interval_policy_thresholds():
    # 100 m pixels rather than 1 m: contour_intervals_for only reads the
    # window's total area, so this reaches the same km-scale extents as a
    # real 1 m survey window without allocating a multi-megapixel array.
    two_km = _bare_window(20, 20, 100.0)
    assert contour_intervals_for(two_km) == [5.0, 1.0]

    one_km = _bare_window(10, 10, 100.0)
    assert contour_intervals_for(one_km) == [5.0, 1.0, 0.5, 0.25]

    four_km = _bare_window(40, 40, 100.0)
    assert contour_intervals_for(four_km) == [5.0]


# --------------------------------------------------------------------------
# GeoJSON output.
# --------------------------------------------------------------------------


def test_geojson_properties_and_interpolated_flag(tmp_path, ostn15_fixture_grid):
    width = height = 60
    e_origin = _TP06_E - width / 2.0
    n_top = _TP06_N + height / 2.0
    window = _make_window(
        width, height, 1.0, e_origin, n_top,
        height_fn=lambda e, n: 10.0 + 0.2 * (e - _TP06_E) + 0.1 * (n - _TP06_N),
    )

    written = write_contour_files(window, tmp_path, "test", ostn15_fixture_grid)

    names = sorted(path.name for path in written)
    assert names == [
        "test_contours_0.25m.geojson",
        "test_contours_0.5m.geojson",
        "test_contours_1m.geojson",
        "test_contours_5m.geojson",
    ]

    for path in written:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["type"] == "FeatureCollection"
        if "_0.25m" in path.name:
            interval, interpolated = 0.25, True
        elif "_0.5m" in path.name:
            interval, interpolated = 0.5, True
        elif "_1m" in path.name:
            interval, interpolated = 1.0, False
        else:
            interval, interpolated = 5.0, False

        assert payload["features"], f"{path.name} has no features to check"
        for feature in payload["features"]:
            assert feature["type"] == "Feature"
            assert feature["geometry"]["type"] == "LineString"
            props = feature["properties"]
            assert set(props) == {
                "elevation", "interval_m", "source", "source_resolution_m",
                "interpolated",
            }
            assert props["interval_m"] == interval
            assert props["source"] == "Welsh Government LiDAR 2020 to 2023"
            assert props["source_resolution_m"] == 1.0
            assert props["interpolated"] is interpolated
            assert isinstance(props["elevation"], float)
            for lon, lat in feature["geometry"]["coordinates"]:
                # Comfortably inside Wales: a wrong axis order or a wrong
                # transform would land this far outside either range.
                assert -6.0 < lon < 0.0
                assert 51.0 < lat < 52.5


def test_vertices_are_wgs84_via_from_bng(tmp_path, ostn15_fixture_grid):
    width = height = 10
    e_origin = _TP06_E - 5.0
    n_top = _TP06_N + 5.0
    window = _make_window(
        width, height, 1.0, e_origin, n_top,
        height_fn=lambda e, n: e - _TP06_E,
    )

    bng_polylines = generate_contours(window, 1.0)
    assert bng_polylines
    sample_easting, sample_northing = bng_polylines[0][0]
    expected_lat, expected_lon = from_bng(
        sample_easting, sample_northing, ostn15_fixture_grid
    )

    written = write_contour_files(window, tmp_path, "vertex-check", ostn15_fixture_grid)
    target = next(path for path in written if path.name.endswith("_contours_1m.geojson"))
    payload = json.loads(target.read_text(encoding="utf-8"))

    found = any(
        lon == pytest.approx(expected_lon, abs=1e-9)
        and lat == pytest.approx(expected_lat, abs=1e-9)
        for feature in payload["features"]
        for lon, lat in feature["geometry"]["coordinates"]
    )
    assert found, "no GeoJSON vertex matched from_bng applied to the same BNG point"


# --------------------------------------------------------------------------
# Performance guardrail: a 1000 x 1000 window, every interval granted.
# --------------------------------------------------------------------------


def test_thousand_by_thousand_hillside_completes_promptly():
    width = height = 1000

    def height_fn(e, n):
        return (
            50.0
            + 0.01 * e
            + 3.0 * math.sin(e / 40.0)
            + 2.0 * math.sin(n / 65.0)
            + 1.0 * math.sin((e + n) / 23.0)
        )

    window = _make_window(width, height, 1.0, 0.0, float(height), height_fn)

    # A 1000 x 1000 m window at 1 m is exactly 1 sq km, so every interval
    # the policy can grant is granted, and this exercises all four passes.
    intervals = contour_intervals_for(window)
    assert intervals == [5.0, 1.0, 0.5, 0.25]

    started = time.perf_counter()
    for interval in intervals:
        generate_contours(window, interval)
    elapsed = time.perf_counter() - started

    # Generous on purpose (see the task brief): this is a guardrail against
    # minutes, not a tight performance pin that would be flaky on a loaded
    # machine.
    assert elapsed < 60.0
