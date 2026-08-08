"""src/mapgen/roofs.py (Task 1 portion)

Roof plane fitting for mapgen packages: sequential RANSAC over the DSM
samples inside a building footprint, with every final fit refined by a
trimmed least squares (the owner's own requirement, spec item B: real
DSMs carry spikes, and a plain least squares hands a spike the whole
fit). Task 2 adds classification, Task 3 the .osm loop, Task 5 massing.
"""

from __future__ import annotations

import json
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

# Roof faces worth extracting at 1 m: flat and mono use one, gable two.
# The ceiling stays at four rather than dropping to two with `hip` (Task
# 4): a roof's extra faces still have to be FOUND to be counted, because
# it is the count of significant planes that sends a building to
# `complex`. Extracting only two would let a four-plane roof's first two
# planes pass as a gable.
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


from mapgen.heights import MIN_HEIGHT_METRES, _percentile

MIN_ROOF_SAMPLES = 24
MIN_QUALITY = 0.75
FLAT_MAX_SLOPE_DEG = 5.0
PITCH_MIN_SLOPE_DEG = 10.0
PITCH_MAX_SLOPE_DEG = 65.0
GABLE_ASPECT_TOLERANCE_DEG = 35.0
GABLE_SLOPE_DIFFERENCE_MAX_DEG = 15.0

# A `flat` roof has to be level in FACT, not merely built of level planes.
# Several level planes at different heights are a stepped roof, an
# extension beside a taller block, a plant deck: calling that "flat" and
# then reporting one median level as both eaves and ridge asserts a
# single roof plane the evidence does not show. Task 4's Cowbridge run
# measured the spread (p95 minus p10) of the samples the SIGNIFICANT
# planes explain (see classify_roof for why significance matters here):
# the median flat roof spans 0.38 m, but 39% of them spanned more than
# half a metre and the worst spanned 4.20 m. Half a metre is where a real
# flat roof's own noise and parapet stop and a second storey begins, so
# anything wider is routed to `complex`, which reports the true p10 and
# p95 instead of flattening the step away.
#
# Half a metre is also exactly 2 x INLIER_TOLERANCE_METRES, which is the
# widest band a single accepted plane's own inliers can occupy. That is
# the reason to prefer 0.5 over a value fitted to the sample: one level
# plane cannot breach it by construction, so the gate only ever fires on
# a genuine second level, not on a noisy single deck.
FLAT_MAX_SPREAD_METRES = 0.5

# A classified ridge under this is not a roof at all. Task 4's validation
# against real Cowbridge data found 89 classified buildings with ridge
# under 0.5 m, 64 of them tagged `flat` at quality 1.00: a perfectly flat
# patch of BARE GROUND is perfectly explained by one plane, so it scores
# 1.00 on a question quality was never measuring. This is the identical
# physical question heights.py already ruled on for `height` itself:
# "Heights under 2.0 m are refused, not rounded up, and recorded as
# `no_data` exactly like a tile with no coverage: a slab, a low wall or a
# misregistered footprint reads as a few tens of centimetres of
# DSM-minus-DTM, and writing that as a building height would put
# fabricated data into a package." A ridge under that floor is the same
# slab, the same low wall, the same misregistered footprint, read off the
# DSM's own peak instead of its median, so this is MIN_HEIGHT_METRES
# itself, not a new number picked for roofs.
MIN_RIDGE_METRES = MIN_HEIGHT_METRES

# `hip` was dropped after Task 4's validation. See classify_roof.
ROOF_SHAPES = ("flat", "mono", "gable", "complex")


@dataclass(frozen=True)
class RoofForm:
    shape: str
    direction_deg: float | None
    eaves_m: float
    ridge_m: float
    quality: float
    planes: tuple[FittedPlane, ...]


def _aspect_difference(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _is_opposite_pair(p1: FittedPlane, p2: FittedPlane) -> bool:
    aspects_opposite = (
        _aspect_difference(
            p1.plane.downslope_azimuth_deg(), p2.plane.downslope_azimuth_deg()
        )
        >= 180.0 - GABLE_ASPECT_TOLERANCE_DEG
    )
    slopes_alike = (
        abs(p1.plane.slope_deg() - p2.plane.slope_deg())
        <= GABLE_SLOPE_DIFFERENCE_MAX_DEG
    )
    return aspects_opposite and slopes_alike


def classify_roof(points, planes) -> RoofForm | None:
    """One building's roof form from its extracted planes, or None when
    the evidence is below the honesty floor (too few samples, the planes
    explain less than MIN_QUALITY of them, or the fitted ridge sits under
    MIN_RIDGE_METRES): None means NO tags, the spec's absence-over-
    fabrication rule.
    """
    if len(points) < MIN_ROOF_SAMPLES or not planes:
        return None
    assigned: set[int] = set()
    for fitted in planes:
        assigned.update(fitted.inlier_indices)
    quality = round(len(assigned) / len(points), 2)
    if quality < MIN_QUALITY:
        return None

    floor = max(MIN_PLANE_SAMPLES, math.ceil(0.15 * len(assigned)))
    significant = sorted(
        (f for f in planes if len(f.inlier_indices) >= floor),
        key=lambda f: len(f.inlier_indices),
        reverse=True,
    )
    if not significant:
        return None
    pitched = [
        f
        for f in significant
        if PITCH_MIN_SLOPE_DEG <= f.plane.slope_deg() <= PITCH_MAX_SLOPE_DEG
    ]
    flat = [f for f in significant if f.plane.slope_deg() < FLAT_MAX_SLOPE_DEG]

    z_values = [points[i][2] for i in sorted(assigned)]
    z_low = _percentile(z_values, 0.10)
    z_high = _percentile(z_values, 0.95)

    # The flatness gate below measures the SIGNIFICANT planes only, not
    # every plane `extract_planes` returned. `extract_planes` keeps any
    # plane with MIN_PLANE_SAMPLES (12) inliers, which is far under the
    # significance floor for any building over about 80 samples, so a
    # chimney, aerial mount or plant box with a dozen returns of its own
    # forms a plane that never becomes significant and never decides the
    # shape. Pooling it into the spread would demote a genuinely flat
    # deck to `complex` over roof furniture, which is precisely what the
    # significance floor exists to ignore. The stepped roof this gate is
    # for has two SIGNIFICANT decks, and is still caught.
    #
    # `z_low`/`z_high` above stay over `assigned` for the eaves and ridge
    # heights: those report the whole roof the planes explain, furniture
    # included, and a chimney genuinely is part of the built height.
    significant_z = sorted(
        points[i][2]
        for i in {i for f in significant for i in f.inlier_indices}
    )
    flat_spread = _percentile(significant_z, 0.95) - _percentile(significant_z, 0.10)

    shape = "complex"
    direction: float | None = None
    if len(flat) == len(significant):
        # Level planes, but level at ONE height or at several? See
        # FLAT_MAX_SPREAD_METRES. A stepped roof falls through to
        # `complex`, which reports the real range rather than a median
        # that hides the step.
        if flat_spread <= FLAT_MAX_SPREAD_METRES:
            shape = "flat"
    elif len(significant) == 1 and len(pitched) == 1:
        shape = "mono"
        direction = pitched[0].plane.downslope_azimuth_deg()
    elif len(significant) == 2 and len(pitched) == 2:
        if _is_opposite_pair(pitched[0], pitched[1]):
            shape = "gable"
            direction = ridge_azimuth_deg(pitched[0].plane, pitched[1].plane)
            if direction is None:
                shape, direction = "complex", None
    # Three or four pitched planes used to be tested for a hip here (an
    # opposite pair plus an end face). `hip` was DROPPED after Task 4's
    # validation against the real Cowbridge package, which is the outcome
    # the spec itself anticipated for a class the 1 m DSM cannot carry.
    #
    # The evidence, from synthetic hips swept across aspect ratio at 1 m
    # sampling, five azimuths and three noise seeds each, so 15 runs per
    # aspect and 9 aspects for 135 runs in total (noise at the stated
    # 0.08 m). All nine rows, so the arithmetic is checkable:
    #
    #   aspect 1.00  hip  0/15   (8 refused, 4 complex, 3 flat)
    #   aspect 1.12  hip  6/15   (4 complex, 3 flat, 2 refused)
    #   aspect 1.25  hip 13/15   (1 complex, 1 gable) the one band that works
    #   aspect 1.38  hip  6/15   (4 gable, 3 complex, 1 flat, 1 refused)
    #   aspect 1.50  hip  6/15   (9 gable)
    #   aspect 1.75  hip  2/15   (13 gable)
    #   aspect 2.00  hip  0/15   (15 gable)
    #   aspect 2.50  hip  0/15   (15 gable)
    #   aspect 3.00  hip  0/15   (15 gable)
    #
    # 0+6+13+6+6+2+0+0+0 = 33 of 135, and the same roof answers hip,
    # gable, complex, flat or nothing depending only on which way it faces
    # and which noise it drew. A tag nobody can rely on, whose absence
    # means nothing either, is worse than no tag. The control sweep over
    # gables in the same harness answered gable 15/15 at each of the five
    # aspects it covered (1.00, 1.25, 1.50, 2.00, 2.50), so this is hip's
    # failure and not the fitter's.
    #
    # Confirmed on real data twice over: only 53 of 1080 classified
    # Cowbridge buildings ever reached hip, and check 6's spike probe
    # flipped a `complex` INTO a `hip` on three injected spikes.
    #
    # Those cases now fall through to `complex`, which keeps the honest
    # eaves and ridge and simply declines to name a form. Routing them to
    # `gable` was rejected: it would assert a ridge direction that, at the
    # near-square aspects where hips actually occur, the fitter picks
    # arbitrarily.

    if shape == "flat":
        level = round(_percentile(z_values, 0.5), 1)
        eaves = ridge = level
    else:
        eaves = round(z_low, 1)
        ridge = round(z_high, 1)
    if ridge < MIN_RIDGE_METRES:
        # See MIN_RIDGE_METRES: below the floor, this is bare ground or a
        # slab fitting one plane perfectly, not a roof. None means NO
        # tags, the same absence-over-fabrication rule this function
        # already applies above for thin evidence; the caller counts this
        # `below_quality`, exactly where a fit the evidence cannot support
        # belongs.
        return None
    if direction is not None:
        direction = round(direction, 0) % (360.0 if shape == "mono" else 180.0)
    return RoofForm(
        shape=shape,
        direction_deg=direction,
        eaves_m=eaves,
        ridge_m=ridge,
        quality=quality,
        planes=tuple(significant),
    )


# --------------------------------------------------------------------------
# Task 3: the .osm loop, roof tags, and the record. Transposed from
# heights.fuse_building_heights: same parse, same node table, same
# building-ways-only/relations-skipped/atomic-rewrite shape, with a plane
# fit standing where the fusion put a plain DSM-minus-DTM subtraction.
# --------------------------------------------------------------------------

import xml.etree.ElementTree as ET
from pathlib import Path

from mapgen.bng import BngError, Ostn15Grid, from_bng, to_bng
from mapgen.cog import BngWindow
from mapgen.fsutil import atomic_write_bytes
from mapgen.heights import (
    HeightsError,
    _declaration_line,
    _footprint_ring,
    _grid_points,
    _point_in_polygon,
    _rewrite_osm,
)

ROOF_SHAPE_TAG_KEY = "roof:shape"
ROOF_DIRECTION_TAG_KEY = "roof:direction"
ROOF_EAVES_TAG_KEY = "roof:height:eaves"
ROOF_RIDGE_TAG_KEY = "roof:height:ridge"
SOURCE_ROOF_TAG_KEY = "source:roof"
SOURCE_ROOF_ATTRIBUTION = "Welsh Government LiDAR 2020 to 2023 (DSM plane fit)"

# Task 5's massing GeoJSON: the same attribution, and one note carried on
# every feature (polygon and ridge alike) because both are a plane fit's
# read of a roof, not a survey of it. "indicative" is load-bearing: a
# concave footprint can make the ridge span (see _ridge_segment) bridge a
# notch in the outline, and this is what covers that case honestly.
MASSING_NOTE = "derived from LiDAR plane fits, indicative"

_BUILDING_TAG_KEY = "building"


class RoofsError(RuntimeError):
    """Raised when a package's own `.osm` cannot be read or rewritten,
    matching `HeightsError`'s scope exactly: caught and recorded by the
    package step, never allowed to take a survey down.
    """


@dataclass(frozen=True)
class RoofsRecord:
    """What one pass over an `.osm` did, mirroring `HeightsRecord`'s shape
    with the two extra outcomes a roof fit can land on: `below_quality`
    (evidence gathered, `classify_roof` refused it) alongside `no_data`
    (no usable evidence at all). `buildings == classified + kept_existing
    + below_quality + no_data` always. `shapes` counts classified ways by
    `RoofForm.shape`.
    """

    buildings: int
    classified: int
    kept_existing: int
    below_quality: int
    no_data: int
    relations_skipped: int
    shapes: dict[str, int]


def _roof_samples(ring_bng, dtm: BngWindow, dsm: BngWindow):
    """(points, ground, centre) for one footprint, or None when the
    evidence is below the floor.

    `points` are (e, n, z above ground), centred on the footprint's own
    sample mean for conditioning (plane fitting is better behaved near
    the origin); `ground` is the DTM median (the heights.py convention);
    `centre` is the (mean easting, mean northing) the centring subtracted,
    kept so Task 5's massing can place geometry back in real BNG.
    """
    eastings = [e for e, _ in ring_bng]
    northings = [n for _, n in ring_bng]
    raw = []
    for e, n in _grid_points(min(eastings), min(northings), max(eastings), max(northings)):
        if not _point_in_polygon(e, n, ring_bng):
            continue
        dtm_v = dtm.sample_bng(e, n)
        dsm_v = dsm.sample_bng(e, n)
        if dtm_v is None or dsm_v is None:
            continue
        raw.append((e, n, dtm_v, dsm_v))
    if len(raw) < MIN_ROOF_SAMPLES:
        return None
    ground = _percentile([r[2] for r in raw], 0.5)
    e_mean = sum(r[0] for r in raw) / len(raw)
    n_mean = sum(r[1] for r in raw) / len(raw)
    points = [(e - e_mean, n - n_mean, dsm_v - ground) for e, n, _, dsm_v in raw]
    return points, ground, (e_mean, n_mean)


def _write_roof_tags(way: ET.Element, form: RoofForm) -> None:
    """Write the fitted tags onto `way`, skipping any key it already
    carries.

    `roof:shape` alone gates `kept_existing` in `fit_roof_forms` (per the
    brief), so a way can still reach here carrying a stale, independently
    upstream-sourced fragment of one of the other four keys (real OSM
    data does carry partial roof tagging). Collecting the existing keys
    once and skipping them matches heights.py's own never-overwrite-
    mapper-data rule for `height`/`source:height`: the mapper's own value
    wins, this only fills the gaps.
    """
    existing_keys = {t.get("k") for t in way.findall("tag")}
    for key, value in (
        (ROOF_SHAPE_TAG_KEY, form.shape),
        (
            ROOF_DIRECTION_TAG_KEY,
            None if form.direction_deg is None else f"{form.direction_deg:.0f}",
        ),
        (ROOF_EAVES_TAG_KEY, f"{form.eaves_m:.1f}"),
        (ROOF_RIDGE_TAG_KEY, f"{form.ridge_m:.1f}"),
        (SOURCE_ROOF_TAG_KEY, SOURCE_ROOF_ATTRIBUTION),
    ):
        if value is None or key in existing_keys:
            continue
        tag = ET.SubElement(way, "tag")
        tag.set("k", key)
        tag.set("v", value)


def _ridge_segment(pair, ring_bng):
    """Two (e, n, z) endpoints of a gable pair's intersection line,
    clipped to the footprint, in the CENTRED frame the planes live in
    (the caller adds the centre back before projecting to lon/lat), or
    None when there is no honest span to report.

    Parametrises the intersection line by solving one coordinate from
    `(a1-a2)e + (b1-b2)n = c2-c1` at the footprint centre (whichever of
    e or n has the larger coefficient, to avoid dividing by a near-zero
    one), with direction taken from the planes' gradient difference
    rotated 90 degrees (the same vector `ridge_azimuth_deg` reads).  It
    then intersects that infinite line with every ring edge in 2D and
    takes the span between the smallest and largest intersection
    parameters found. Fewer than two intersections, or a near-parallel
    pair (no line at all), returns None; the polygon still ships either
    way. On a concave footprint the span can bridge a notch outside the
    building; the massing note's "indicative" covers exactly this.
    """
    p1, p2 = pair[0].plane, pair[1].plane
    de = -(p1.b - p2.b)
    dn = p1.a - p2.a
    length = math.hypot(de, dn)
    if length < 1e-9:
        return None
    de, dn = de / length, dn / length
    rhs = p2.c - p1.c
    da, db = p1.a - p2.a, p1.b - p2.b
    if abs(da) >= abs(db):
        n0 = 0.0
        e0 = (rhs - db * n0) / da
    else:
        e0 = 0.0
        n0 = (rhs - da * e0) / db
    ts = []
    prev = ring_bng[-1]
    for vertex in ring_bng:
        (x1, y1), (x2, y2) = prev, vertex
        prev = vertex
        # Solve (e0 + t*de, n0 + t*dn) crossing segment (x1,y1)-(x2,y2).
        sx, sy = x2 - x1, y2 - y1
        denom = de * sy - dn * sx
        if abs(denom) < 1e-12:
            continue
        # Cramer's rule on [de -sx; dn -sy] u = [x1-e0; y1-n0] gives
        # u = (dn*(x1-e0) - de*(y1-n0)) / denom (determinant -denom over
        # a numerator that itself carries the matching sign; the two
        # negations cancel). Dividing by -denom, as an earlier version of
        # this function did, flips every crossing's u outside [0, 1] and
        # silently drops every real intersection: confirmed both
        # algebraically and by running this exact fixture, where it
        # produced zero crossings on any edge of a footprint the ridge
        # plainly cuts through.
        u = ((x1 - e0) * dn - (y1 - n0) * de) / denom
        if not (0.0 <= u <= 1.0):
            continue
        crossing_e, crossing_n = x1 + u * sx, y1 + u * sy
        ts.append((crossing_e - e0) * de + (crossing_n - n0) * dn)
    if len(ts) < 2:
        return None
    t_min, t_max = min(ts), max(ts)
    z = p1.z_at(e0 + t_min * de, n0 + t_min * dn)
    return (
        (e0 + t_min * de, n0 + t_min * dn, z),
        (e0 + t_max * de, n0 + t_max * dn, z),
    )


def fit_roof_forms(
    osm_path: Path,
    dtm: BngWindow,
    dsm: BngWindow,
    grid: Ostn15Grid,
    massing_path: Path | None = None,
) -> RoofsRecord:
    """Fit and tag a roof form onto every untagged building way in
    `osm_path`, atomically, idempotently, absence over fabrication
    throughout.

    Ways tagged `building=*` only; a multipolygon relation is counted in
    `relations_skipped` and never looked at. A way that already carries
    `roof:shape` is `kept_existing` and never re-touched, which is what
    makes a second run over the same package idempotent by construction
    and byte-identical.

    `massing_path`, when given, gets a GeoJSON `FeatureCollection` for
    direct Grasshopper import: one Polygon per classified building (the
    footprint ring at eaves height, metres above that building's own
    ground) plus one LineString ridge per classified `gable` (the only
    shape with both a direction and two planes to intersect; `mono` has
    a direction but no second plane, `flat` and `complex` have neither).
    Built in this same loop, from the same fit that wrote the tags, so
    the file and the tags can never disagree. Unclassified buildings
    contribute nothing, and the file itself is written atomically and
    only when at least one building classified: an empty classification
    writes no file at all, matching the module's absence-over-
    fabrication rule for the tags themselves.

    Raises `RoofsError` for an `.osm` that cannot be read as the shape
    `mapgen.merge.merge_osm_xml` produces at all; a footprint this module
    cannot make sense of on its own (unresolvable node, no OSTN15
    coverage, too little raster data) is never an exception, only
    `no_data`, and a footprint `classify_roof` cannot honestly call a
    shape is `below_quality`, per the module's absence-over-fabrication
    rule.
    """
    path = Path(osm_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoofsError(f"{path.name} could not be read: {exc}") from None
    try:
        declaration = _declaration_line(text)
    except HeightsError as exc:
        raise RoofsError(str(exc)) from None
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RoofsError(f"{path.name} is not valid XML: {exc}") from None

    nodes: dict[str, tuple[float, float]] = {}
    for element in root:
        if element.tag != "node":
            continue
        node_id, lat, lon = element.get("id"), element.get("lat"), element.get("lon")
        if node_id is None or lat is None or lon is None:
            continue
        try:
            nodes[node_id] = (float(lat), float(lon))
        except ValueError:
            continue

    buildings = classified = kept_existing = below_quality = no_data = 0
    relations_skipped = 0
    shapes: dict[str, int] = {}
    changed = False
    features: list[dict] = []

    for element in root:
        if element.tag == "relation":
            relations_skipped += 1
            continue
        if element.tag != "way":
            continue
        tags = element.findall("tag")
        if not any(t.get("k") == _BUILDING_TAG_KEY for t in tags):
            continue
        buildings += 1
        if any(t.get("k") == ROOF_SHAPE_TAG_KEY for t in tags):
            kept_existing += 1
            continue
        ring = _footprint_ring(element, nodes)
        if ring is None:
            no_data += 1
            continue
        try:
            projected = [to_bng(lat, lon, grid) for lat, lon in ring]
        except BngError:
            # No OSTN15 coverage for this footprint: the same practical
            # fact as a raster with nothing under it (see heights.py).
            no_data += 1
            continue
        sampled = _roof_samples(projected, dtm, dsm)
        if sampled is None:
            no_data += 1
            continue
        points, ground, centre = sampled
        form = classify_roof(points, extract_planes(points, rng=random.Random(0)))
        if form is None:
            below_quality += 1
            continue
        _write_roof_tags(element, form)
        if massing_path is not None:
            way_id = element.get("id")
            try:
                building_id: object = int(way_id)
            except (TypeError, ValueError):
                building_id = way_id
            ring_positions = []
            for e, n in projected:
                lat, lon = from_bng(e, n, grid)
                ring_positions.append([lon, lat, form.eaves_m])
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [ring_positions]},
                    "properties": {
                        "building": building_id,
                        "shape": form.shape,
                        "direction": form.direction_deg,
                        "eaves": form.eaves_m,
                        "ridge": form.ridge_m,
                        "quality": form.quality,
                        "ground_m": round(ground, 2),
                        "source": SOURCE_ROOF_ATTRIBUTION,
                        "note": MASSING_NOTE,
                    },
                }
            )
            # Gable only (see the docstring above): the only classified
            # shape with exactly two significant planes to intersect.
            if form.shape == "gable" and form.direction_deg is not None:
                ring_centred = [(e - centre[0], n - centre[1]) for e, n in projected]
                segment = _ridge_segment(form.planes[:2], ring_centred)
                if segment is not None:
                    (e1, n1, _), (e2, n2, _) = segment
                    lat1, lon1 = from_bng(e1 + centre[0], n1 + centre[1], grid)
                    lat2, lon2 = from_bng(e2 + centre[0], n2 + centre[1], grid)
                    features.append(
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [
                                    [lon1, lat1, form.ridge_m],
                                    [lon2, lat2, form.ridge_m],
                                ],
                            },
                            "properties": {
                                "building": building_id,
                                "feature": "ridge",
                                "source": SOURCE_ROOF_ATTRIBUTION,
                                "note": MASSING_NOTE,
                            },
                        }
                    )
        shapes[form.shape] = shapes.get(form.shape, 0) + 1
        classified += 1
        changed = True

    if changed:
        _rewrite_osm(path, declaration, root)

    if massing_path is not None and features:
        collection = {"type": "FeatureCollection", "features": features}
        atomic_write_bytes(
            Path(massing_path), json.dumps(collection, indent=2).encode("utf-8")
        )

    return RoofsRecord(
        buildings=buildings,
        classified=classified,
        kept_existing=kept_existing,
        below_quality=below_quality,
        no_data=no_data,
        relations_skipped=relations_skipped,
        shapes=shapes,
    )
