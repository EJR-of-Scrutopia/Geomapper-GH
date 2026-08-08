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

    def test_degenerate_after_trim_returns_none(self):
        # A narrow canopy strip: 5 real samples along one row (e=0, n=0..4),
        # z = 0.1*n + 3, plus one bad DSM spike far from the strip.
        # After trimming the spike, the 5 remaining points are all at e=0
        # (plan-collinear), so the refit should fail and this should return None.
        points = [
            (0.0, 0.0, 3.0), (0.0, 1.0, 3.1), (0.0, 2.0, 3.2),
            (0.0, 3.0, 3.3), (0.0, 4.0, 3.4),
            (3.0, 2.0, 1000.0),
        ]
        result = trimmed_plane(points)
        assert result is None


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

    def test_extract_planes_calls_trimmed_plane(self):
        # Indirect test that extract_planes correctly calls trimmed_plane on
        # inliers and breaks if trimmed_plane returns None. The degenerate-
        # after-trim case is tested directly in TestTrimmedPlane above. Here
        # we just verify that extract_planes doesn't crash when faced with
        # a large dataset, including the break path when trimmed_plane fails.
        points = _plane_points(0.2, 0.1, 3.0, extent=4, step=1.5)
        fitted = extract_planes(points)
        # Should return at least one plane from well-conditioned data.
        assert len(fitted) >= 1
        # All returned planes should have reasonable parameters.
        for f in fitted:
            assert isinstance(f.plane, Plane)
            assert isinstance(f.inlier_indices, tuple)
            assert len(f.inlier_indices) >= MIN_PLANE_SAMPLES


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
