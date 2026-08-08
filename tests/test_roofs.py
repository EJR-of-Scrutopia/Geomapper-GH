"""tests/test_roofs.py"""
import math
import random

from mapgen.roofs import (
    INLIER_TOLERANCE_METRES,
    MIN_PLANE_SAMPLES,
    Plane,
    extract_planes,
    ridge_azimuth_deg,
    trimmed_plane,
)


def _plane_points(a, b, c, extent=6, step=1.0):
    """A grid of exact samples of z = a*e + b*n + c over a square."""
    points = []
    for row in range(-extent, extent + 1):
        for col in range(-extent, extent + 1):
            e, n = col * step, row * step
            points.append((e, n, a * e + b * n + c))
    return points


class TestPlane:
    def test_z_at_evaluates_the_plane(self):
        plane = Plane(a=0.5, b=-0.25, c=2.0)
        assert plane.z_at(2.0, 4.0) == 0.5 * 2.0 - 0.25 * 4.0 + 2.0

    def test_slope_of_a_horizontal_plane_is_zero(self):
        assert Plane(a=0.0, b=0.0, c=5.0).slope_deg() == 0.0

    def test_slope_of_a_45_degree_plane(self):
        assert abs(Plane(a=1.0, b=0.0, c=0.0).slope_deg() - 45.0) < 1e-9

    def test_downslope_azimuth_points_downhill(self):
        # z rises to the east (a > 0), so downhill faces west (270).
        assert abs(Plane(a=1.0, b=0.0, c=0.0).downslope_azimuth_deg() - 270.0) < 1e-9
        # z rises to the north (b > 0), so downhill faces south (180).
        assert abs(Plane(a=0.0, b=1.0, c=0.0).downslope_azimuth_deg() - 180.0) < 1e-9


class TestTrimmedPlane:
    def test_recovers_an_exact_plane(self):
        fit = trimmed_plane(_plane_points(0.3, -0.1, 4.0))
        assert abs(fit.a - 0.3) < 1e-6
        assert abs(fit.b + 0.1) < 1e-6
        assert abs(fit.c - 4.0) < 1e-6

    def test_trim_discards_a_spike_a_plain_fit_cannot(self):
        # One +8 m spike on a flat roof of 169 samples. The trimmed refit
        # must land within a centimetre of flat; a plain least squares
        # over the same points is pulled visibly off (documented here as
        # the reason the spec forbids it).
        points = _plane_points(0.0, 0.0, 3.0)
        points[0] = (points[0][0], points[0][1], 11.0)
        fit = trimmed_plane(points)
        assert abs(fit.z_at(0.0, 0.0) - 3.0) < 0.01

    def test_collinear_points_return_none(self):
        points = [(float(i), 0.0, 1.0) for i in range(10)]
        assert trimmed_plane(points) is None


class TestExtractPlanes:
    def test_single_plane_claims_everything(self):
        points = _plane_points(0.2, 0.0, 5.0)
        fitted = extract_planes(points)
        assert len(fitted) == 1
        assert len(fitted[0].inlier_indices) == len(points)

    def test_two_gable_planes_found_separately(self):
        # A gable along the n axis: z falls away from e = 0 both ways.
        points = []
        for row in range(-6, 7):
            for col in range(-6, 7):
                e, n = float(col), float(row)
                points.append((e, n, 8.0 - 0.7 * abs(e)))
        fitted = extract_planes(points)
        assert len(fitted) == 2
        claimed = set(fitted[0].inlier_indices) | set(fitted[1].inlier_indices)
        # The two planes together explain nearly every sample (the ridge
        # row itself belongs to whichever plane finds it first).
        assert len(claimed) >= 0.9 * len(points)

    def test_deterministic_across_runs(self):
        points = _plane_points(0.1, 0.2, 2.0)
        first = extract_planes(points, rng=random.Random(0))
        second = extract_planes(points, rng=random.Random(0))
        assert [f.inlier_indices for f in first] == [f.inlier_indices for f in second]

    def test_too_few_points_yields_nothing(self):
        assert extract_planes(_plane_points(0.0, 0.0, 3.0)[: MIN_PLANE_SAMPLES - 1]) == []


class TestRidgeAzimuth:
    def test_gable_ridge_at_30_degrees(self):
        # Ridge along azimuth 30: downslope azimuths 120 and 300, both at
        # gradient magnitude g. Gradients point uphill.
        g = 0.7
        up1 = math.radians(300.0)
        up2 = math.radians(120.0)
        p1 = Plane(a=g * math.sin(up1), b=g * math.cos(up1), c=0.0)
        p2 = Plane(a=g * math.sin(up2), b=g * math.cos(up2), c=0.0)
        azimuth = ridge_azimuth_deg(p1, p2)
        assert abs(azimuth - 30.0) < 1e-6

    def test_parallel_planes_have_no_ridge(self):
        p = Plane(a=0.5, b=0.0, c=0.0)
        assert ridge_azimuth_deg(p, Plane(a=0.5, b=0.0, c=2.0)) is None
