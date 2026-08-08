"""src/mapgen/roofs.py (Task 1 portion)

Roof plane fitting for mapgen packages: sequential RANSAC over the DSM
samples inside a building footprint, with every final fit refined by a
trimmed least squares (the owner's own requirement, spec item B: real
DSMs carry spikes, and a plain least squares hands a spike the whole
fit). Task 2 adds classification, Task 3 the .osm loop, Task 5 massing.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# A DSM sample within this of its plane counts as explained by it. The
# Welsh 1 m LiDAR's stated vertical RMSE is under 0.15 m; 0.25 gives one
# noise-width of slack without letting a second roof face bleed in.
INLIER_TOLERANCE_METRES = 0.25

# Seeded trials per extracted plane. With the 0.9 early exit below, a
# simple roof stops long before this bound.
RANSAC_ITERATIONS = 120

# Roof faces worth telling apart at 1 m: flat and mono use one, gable
# two, hip four. Anything needing more is `complex` by vocabulary.
MAX_PLANES = 4

# A face explained by fewer samples than this is one bad seed away from
# noise and is refused.
MIN_PLANE_SAMPLES = 12

# The worst tenth of residuals is dropped before the refit (at least one
# point always). See the module docstring: never a plain least squares.
TRIM_FRACTION = 0.1


@dataclass(frozen=True)
class Plane:
    """z = a*e + b*n + c, in a footprint's own centred BNG frame."""

    a: float
    b: float
    c: float

    def z_at(self, e: float, n: float) -> float:
        return self.a * e + self.b * n + self.c

    def slope_deg(self) -> float:
        return math.degrees(math.atan(math.hypot(self.a, self.b)))

    def downslope_azimuth_deg(self) -> float:
        """Compass direction the plane falls towards, degrees clockwise
        from north. The gradient (a, b) points uphill; downhill is its
        negation, and azimuth reads atan2(east, north)."""
        return math.degrees(math.atan2(-self.a, -self.b)) % 360.0


@dataclass(frozen=True)
class FittedPlane:
    plane: Plane
    inlier_indices: tuple[int, ...]


def _plane_through(p1, p2, p3) -> Plane | None:
    """The exact plane through three points, or None when they are too
    close to collinear to define one."""
    (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = p1, p2, p3
    det = (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)
    if abs(det) < 1e-9:
        return None
    a = ((z2 - z1) * (y3 - y1) - (z3 - z1) * (y2 - y1)) / det
    b = ((x2 - x1) * (z3 - z1) - (x3 - x1) * (z2 - z1)) / det
    return Plane(a=a, b=b, c=z1 - a * x1 - b * y1)


def _least_squares_plane(points) -> Plane | None:
    """Ordinary least squares for z = a*e + b*n + c via the 3x3 normal
    equations, solved by Cramer's rule. None for a degenerate spread
    (all points collinear in plan view)."""
    n = float(len(points))
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sz = sum(p[2] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    syy = sum(p[1] * p[1] for p in points)
    sxz = sum(p[0] * p[2] for p in points)
    syz = sum(p[1] * p[2] for p in points)
    det = (
        sxx * (syy * n - sy * sy)
        - sxy * (sxy * n - sy * sx)
        + sx * (sxy * sy - syy * sx)
    )
    if abs(det) < 1e-9:
        return None
    a = (
        sxz * (syy * n - sy * sy)
        - sxy * (syz * n - sy * sz)
        + sx * (syz * sy - syy * sz)
    ) / det
    b = (
        sxx * (syz * n - sy * sz)
        - sxz * (sxy * n - sx * sy)
        + sx * (sxy * sz - syz * sx)
    ) / det
    c = (
        sxx * (syy * sz - syz * sy)
        - sxy * (sxy * sz - syz * sx)
        + sxz * (sxy * sy - syy * sx)
    ) / det
    return Plane(a=a, b=b, c=c)


def trimmed_plane(points) -> Plane | None:
    """The final fit for a set of inliers: least squares, drop the worst
    TRIM_FRACTION of residuals (never fewer than one point), refit.

    The untrimmed fit is never returned when a trim is possible: this is
    the owner's spike rule made structural. Fewer than 5 points cannot
    spare one and take the single fit as-is. When a trim is possible but
    the trimmed subset is plan-collinear (e.g. canopy strips confined to
    one row), the refit will fail and this function returns None: a point
    set whose trimmed core cannot support an honest plane rejects the fit
    entirely, following absence-over-fabrication rather than shipping a
    spike-corrupted untrimmed fit.
    """
    fit = _least_squares_plane(points)
    if fit is None or len(points) < 5:
        return fit
    by_residual = sorted(points, key=lambda p: abs(fit.z_at(p[0], p[1]) - p[2]))
    keep = max(4, len(points) - max(1, int(len(points) * TRIM_FRACTION)))
    refit = _least_squares_plane(by_residual[:keep])
    return refit


def extract_planes(points, rng: random.Random | None = None) -> list[FittedPlane]:
    """Sequential RANSAC: find the best-supported plane among the
    unclaimed points, refine it with `trimmed_plane`, claim its inliers,
    repeat. Deterministic: the default rng is seeded with 0 per call, so
    a rerun over the same package answers the same planes.
    """
    if rng is None:
        rng = random.Random(0)
    remaining = list(range(len(points)))
    fitted: list[FittedPlane] = []
    while len(remaining) >= MIN_PLANE_SAMPLES and len(fitted) < MAX_PLANES:
        best_plane: Plane | None = None
        best_inliers: list[int] = []
        for _ in range(RANSAC_ITERATIONS):
            trio = rng.sample(remaining, 3)
            candidate = _plane_through(points[trio[0]], points[trio[1]], points[trio[2]])
            if candidate is None:
                continue
            inliers = [
                i
                for i in remaining
                if abs(candidate.z_at(points[i][0], points[i][1]) - points[i][2])
                <= INLIER_TOLERANCE_METRES
            ]
            if len(inliers) > len(best_inliers):
                best_plane, best_inliers = candidate, inliers
                if len(best_inliers) >= 0.9 * len(remaining):
                    break
        if best_plane is None or len(best_inliers) < MIN_PLANE_SAMPLES:
            break
        refined = trimmed_plane([points[i] for i in best_inliers])
        if refined is None:
            break
        inliers = [
            i
            for i in remaining
            if abs(refined.z_at(points[i][0], points[i][1]) - points[i][2])
            <= INLIER_TOLERANCE_METRES
        ]
        if len(inliers) < MIN_PLANE_SAMPLES:
            break
        fitted.append(FittedPlane(plane=refined, inlier_indices=tuple(inliers)))
        claimed = set(inliers)
        remaining = [i for i in remaining if i not in claimed]
    return fitted


def ridge_azimuth_deg(p1: Plane, p2: Plane) -> float | None:
    """The compass direction of two planes' intersection line, folded to
    [0, 180) because a ridge has no head or tail. None when the planes
    are parallel in plan and never meet in one line."""
    de = -(p1.b - p2.b)
    dn = p1.a - p2.a
    if math.hypot(de, dn) < 1e-9:
        return None
    return math.degrees(math.atan2(de, dn)) % 180.0
