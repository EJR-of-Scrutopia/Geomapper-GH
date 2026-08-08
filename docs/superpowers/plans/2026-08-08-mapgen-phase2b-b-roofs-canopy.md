# Phase 2b Item B: Roof Forms and Canopy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fit roof forms (flat, mono, gable, hip, complex) to every fused building footprint from the packaged 1 m LiDAR, write them as OSM tags plus a massing GeoJSON, and emit canopy points from the nDSM outside footprints, with a validation task over the owner's real Cowbridge package gating everything before any tag ships.

**Architecture:** Two new stdlib-only modules, `src/mapgen/roofs.py` (footprint DSM sampling, sequential RANSAC plane extraction with trimmed-residual refits, shape classification, tag writing, massing features) and `src/mapgen/canopy.py` (nDSM thresholding, footprint masking, connected components, point features). Two new package steps in `package.py`, wired immediately after `_fuse_heights_step` in BOTH pipelines (`run_survey` and `bridge_package`), following that step's exact one-shape-whatever-happened record convention. The roof step is gated on the packaged rasters actually being at the 1 m level, read from the tif itself, never assumed.

**Tech Stack:** Python stdlib only (`math`, `random`, `xml.etree`, `collections.deque`, `json`). Reuses `heights.py` (percentiles, `.osm` rewrite plumbing), `buildings.py` (`point_in_ring`, `_existing_building_footprints`), `cog.py` (`BngWindow`, `read_full_window`), `bng.py` (`to_bng`, `from_bng`), `fsutil` (atomic writes), `geotiff_write` (test fixtures).

## Global Constraints

Copied from the phase 2 spec and the phase 2b addendum (spec:
`docs/superpowers/specs/2026-08-07-mapgen-phase2b-addendum-design.md`,
Item B), binding on every task:

- **Resolution gate (owner requirement, 2026-08-07):** roof fitting runs
  ONLY when the package's LiDAR rasters are at the 1 m level. The gate is
  read from the packaged tif's own pixel scale (`BngWindow.pixel_size` and
  `pixel_height`, both rasters), threshold `ROOF_MAX_PIXEL_METRES = 1.5`
  (level 0 is 1.0 m exactly; level 1 is about 2.0000004 m, never under
  1.5). A gated skip records a reason and emits it through the progress
  sink so the UI log shows it, with this exact string:
  `roof fitting needs the 1 m LiDAR level and this package's rasters are
  {pixel:g} m; extents under about 4 x 4 km come back at 1 m`.
- **Trimmed residuals, never a plain least squares (owner requirement):**
  every plane fit that ships a number runs least squares, drops the worst
  `TRIM_FRACTION` of residuals (at least one point), and refits. The
  untrimmed fit is never the final fit.
- **Per-building fit quality recorded in the output:** `quality` is the
  fraction of roof samples within `INLIER_TOLERANCE_METRES` of their
  assigned plane, rounded to two decimals, carried on every massing
  feature. Below `MIN_QUALITY`, NO roof tags are written and the building
  is counted `below_quality`.
- **Validation before tags ship (owner requirement):** Task 4 runs the
  fitter over a COPY of the owner's real Cowbridge package and produces a
  report. A roof class the 1 m DSM cannot support reliably is DROPPED from
  the vocabulary there, never guessed. If validation fails systemically,
  the plan STOPS before Task 7 wires anything into packages.
- **Nothing fabricated:** no samples means no tags (`no_data`); a tile with
  no data has no data. Canopy copy says
  `vegetation and other above-ground features`, never species or "tree".
- **Owner's originals are read-only:** anything under `C:\Users\Param\Surveys`
  is copied to the session scratchpad before any run; originals are never
  opened for writing.
- No network in any package step. No new third-party dependencies, stdlib
  only. No URL ever enters an exception message. Atomic writes
  (`fsutil.atomic_write_bytes`/`atomic_write_text`) for every package file.
- No em dashes anywhere (code, comments, docs, commit messages). No AI
  attribution anywhere, no Co-Authored-By. Explicit-path `git add`, never
  `-A`.
- Live-network tests carry the `live` marker. Suites:
  `python -m pytest tests/ -m "not live"` (baseline 1795 passed / 17
  deselected) and `node tests/js/test_app.js` (285 checks).
- OSM roof tag names verbatim from the spec: `roof:shape`,
  `roof:direction`, `roof:height:eaves`, `roof:height:ridge`,
  `source:roof`. Heights are metres above the footprint's DTM-median
  ground (the `heights.py` convention), one decimal.

## File Structure

- Create: `src/mapgen/roofs.py` (Tasks 1, 2, 3, 5): plane math,
  extraction, classification, `.osm` tag loop, massing features.
- Create: `src/mapgen/canopy.py` (Task 6): nDSM clustering and features.
- Modify: `src/mapgen/package.py` (Task 7): `_fit_roofs_step`,
  `_canopy_step`, wiring in `run_survey` and `bridge_package`, record
  helpers.
- Create: `tests/test_roofs.py`, `tests/test_canopy.py`.
- Modify: `tests/test_package.py` (Task 7).
- Modify: `README.md`, `docs/urbano/README.md`,
  `docs/superpowers/HANDOFF.md` (Task 8).
- Validation artifacts (Task 4, NOT package files, NOT committed):
  `.superpowers/sdd/2026-08-08-mapgen-phase2b-b-roofs-canopy/roof-validation.md`
  and `roof-validation.geojson`.

---

### Task 1: Plane primitives and sequential extraction (`roofs.py` core)

**Files:**
- Create: `src/mapgen/roofs.py`
- Test: `tests/test_roofs.py`

**Interfaces:**
- Consumes: nothing from other tasks; `math`, `random` stdlib.
- Produces (Tasks 2, 3, 5 rely on these exact names):
  - `Plane(a: float, b: float, c: float)` frozen dataclass with
    `z_at(e, n) -> float`, `slope_deg() -> float`,
    `downslope_azimuth_deg() -> float`
  - `FittedPlane(plane: Plane, inlier_indices: tuple[int, ...])` frozen
    dataclass
  - `trimmed_plane(points: list[tuple[float, float, float]]) -> Plane | None`
  - `extract_planes(points, rng=None) -> list[FittedPlane]`
  - `ridge_azimuth_deg(p1: Plane, p2: Plane) -> float | None`
  - Module constants: `INLIER_TOLERANCE_METRES = 0.25`,
    `RANSAC_ITERATIONS = 120`, `MAX_PLANES = 4`,
    `MIN_PLANE_SAMPLES = 12`, `TRIM_FRACTION = 0.1`

Points are `(e, n, z)` triples in metres, CENTRED by the caller (mean
easting and northing subtracted) for numerical conditioning; every plane
lives in that centred frame.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_roofs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.roofs'`

- [ ] **Step 3: Write the implementation**

```python
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
    spare one and take the single fit as-is.
    """
    fit = _least_squares_plane(points)
    if fit is None or len(points) < 5:
        return fit
    by_residual = sorted(points, key=lambda p: abs(fit.z_at(p[0], p[1]) - p[2]))
    keep = max(4, len(points) - max(1, int(len(points) * TRIM_FRACTION)))
    refit = _least_squares_plane(by_residual[:keep])
    return refit if refit is not None else fit


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
            refined = best_plane
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_roofs.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full suite, then commit**

Run: `python -m pytest tests/ -m "not live" -q`

```bash
git add src/mapgen/roofs.py tests/test_roofs.py
git commit -m "feat(roofs): plane primitives and trimmed sequential RANSAC"
```

---

### Task 2: Roof classification and quality

**Files:**
- Modify: `src/mapgen/roofs.py`
- Test: `tests/test_roofs.py`

**Interfaces:**
- Consumes: Task 1's `Plane`, `FittedPlane`, `extract_planes`,
  `ridge_azimuth_deg`; `heights._percentile` (the project's one
  percentile, imported as `from mapgen.heights import _percentile`,
  matching `package.py`'s existing use of `heights` privates).
- Produces (Tasks 3, 4, 5 rely on these exact names):
  - `RoofForm(shape: str, direction_deg: float | None, eaves_m: float,
    ridge_m: float, quality: float, planes: tuple[FittedPlane, ...])`
    frozen dataclass
  - `classify_roof(points, planes) -> RoofForm | None` (None means below
    quality or too few samples: NO tags)
  - Constants: `MIN_ROOF_SAMPLES = 24`, `MIN_QUALITY = 0.75`,
    `FLAT_MAX_SLOPE_DEG = 5.0`, `PITCH_MIN_SLOPE_DEG = 10.0`,
    `PITCH_MAX_SLOPE_DEG = 65.0`, `GABLE_ASPECT_TOLERANCE_DEG = 35.0`,
    `GABLE_SLOPE_DIFFERENCE_MAX_DEG = 15.0`,
    `ROOF_SHAPES = ("flat", "mono", "gable", "hip", "complex")`

Classification rules, pinned (Task 4 may tune constants or DROP a class
with documented evidence, never add one):

- `quality` = assigned samples / all samples, where assigned is the union
  of the planes' inliers. Below `MIN_QUALITY`, or fewer than
  `MIN_ROOF_SAMPLES` samples in total, the answer is None.
- A plane is *significant* when its inliers number at least
  `max(MIN_PLANE_SAMPLES, ceil(0.15 * assigned))`.
- All significant planes flatter than `FLAT_MAX_SLOPE_DEG` -> `flat`
  (no direction).
- Exactly one significant plane, pitched within
  [`PITCH_MIN_SLOPE_DEG`, `PITCH_MAX_SLOPE_DEG`] -> `mono`; direction is
  that plane's `downslope_azimuth_deg()` in [0, 360).
- Exactly two significant pitched planes, aspects opposite within
  `GABLE_ASPECT_TOLERANCE_DEG` of 180 apart, slopes within
  `GABLE_SLOPE_DIFFERENCE_MAX_DEG` -> `gable`; direction is
  `ridge_azimuth_deg` of the pair in [0, 180).
- Three or four significant pitched planes containing such an opposite
  pair, plus at least one further significant pitched plane whose aspect
  sits 90 within 45 degrees off that pair's aspects -> `hip`; direction
  from the dominant opposite pair. (A cross-gable can satisfy this too;
  that ambiguity is real at 1 m and Task 4 decides whether `hip`
  survives validation.)
- Anything else with quality above the floor -> `complex` (no direction).
- Heights over the ASSIGNED samples' z values: pitched shapes take
  `eaves_m` = percentile 0.10 and `ridge_m` = percentile 0.95; `flat`
  takes the median for both. One decimal. `eaves_m <= ridge_m` holds by
  construction (order statistics of one set).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_roofs.py`. The synthetic roof builder is the test
file's own helper and every geometric expectation carries a tolerance,
never exact equality:

```python
from mapgen.roofs import MIN_QUALITY, MIN_ROOF_SAMPLES, RoofForm, classify_roof
from mapgen.roofs import extract_planes as _extract


def _roof_points(shape, ridge_azimuth_deg=0.0, pitch_deg=35.0,
                 half_width=4.0, half_length=6.0, eaves=5.0, noise=0.0,
                 seed=1):
    """1 m grid samples of a synthetic roof, rotated so its ridge lies
    along `ridge_azimuth_deg`. Local frame: u along the ridge, v across
    it; z falls from the ridge with tan(pitch)."""
    rng = random.Random(seed)
    tan_pitch = math.tan(math.radians(pitch_deg))
    ridge_height = eaves + half_width * tan_pitch
    azimuth = math.radians(ridge_azimuth_deg)
    ue, un = math.sin(azimuth), math.cos(azimuth)     # along ridge
    ve, vn = math.cos(azimuth), -math.sin(azimuth)    # across ridge
    points = []
    for row in range(-int(half_length), int(half_length) + 1):
        for col in range(-int(half_width), int(half_width) + 1):
            u, v = float(row), float(col)
            if shape == "flat":
                z = eaves
            elif shape == "mono":
                z = eaves + (v + half_width) * tan_pitch * 0.5
            elif shape == "gable":
                z = ridge_height - abs(v) * tan_pitch
            elif shape == "hip":
                inset = abs(u) - (half_length - half_width)
                z = ridge_height - max(abs(v), max(inset, 0.0)) * tan_pitch
            else:
                raise ValueError(shape)
            z += rng.gauss(0.0, noise) if noise else 0.0
            points.append((u * ue + v * ve, u * un + v * vn, z))
    return points


def _classified(points):
    return classify_roof(points, _extract(points))


class TestClassifyRoof:
    def test_flat_roof(self):
        form = _classified(_roof_points("flat", noise=0.03))
        assert form.shape == "flat"
        assert form.direction_deg is None
        assert abs(form.eaves_m - 5.0) < 0.2
        assert abs(form.ridge_m - 5.0) < 0.2
        assert form.quality >= 0.9

    def test_gable_at_30_degrees(self):
        form = _classified(_roof_points("gable", ridge_azimuth_deg=30.0, noise=0.03))
        assert form.shape == "gable"
        assert abs(form.direction_deg - 30.0) < 5.0
        assert form.eaves_m < form.ridge_m
        assert abs(form.ridge_m - (5.0 + 4.0 * math.tan(math.radians(35.0)))) < 0.4

    def test_mono_pitch(self):
        form = _classified(_roof_points("mono", ridge_azimuth_deg=0.0, noise=0.03))
        assert form.shape == "mono"
        assert form.direction_deg is not None

    def test_hip_roof(self):
        form = _classified(
            _roof_points("hip", ridge_azimuth_deg=90.0, half_width=4.0,
                         half_length=8.0, noise=0.03)
        )
        assert form.shape in ("hip", "complex")
        if form.shape == "hip":
            assert abs(form.direction_deg - 90.0) < 10.0

    def test_spiked_gable_still_classifies_gable(self):
        # Three +8 m spikes (an aerial, a chimney, a bad return) on a
        # 30-degree gable: the owner's own failure case. The trimmed,
        # RANSAC-seeded fit must shrug them off.
        points = _roof_points("gable", ridge_azimuth_deg=30.0, noise=0.05)
        for i in (5, 60, 100):
            e, n, z = points[i]
            points[i] = (e, n, z + 8.0)
        form = _classified(points)
        assert form.shape == "gable"
        assert abs(form.direction_deg - 30.0) < 5.0
        # The ridge height must not be dragged up by the spikes.
        assert form.ridge_m < 5.0 + 4.0 * math.tan(math.radians(35.0)) + 0.5

    def test_pure_noise_is_refused_not_guessed(self):
        rng = random.Random(3)
        points = [
            (float(col), float(row), rng.uniform(0.0, 6.0))
            for row in range(-6, 7)
            for col in range(-6, 7)
        ]
        assert _classified(points) is None

    def test_too_few_samples_refused(self):
        assert _classified(_roof_points("flat")[: MIN_ROOF_SAMPLES - 1]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_roofs.py -k Classify -v`
Expected: FAIL with `ImportError` (no `classify_roof`)

- [ ] **Step 3: Write the implementation**

Append to `src/mapgen/roofs.py`:

```python
from mapgen.heights import _percentile

MIN_ROOF_SAMPLES = 24
MIN_QUALITY = 0.75
FLAT_MAX_SLOPE_DEG = 5.0
PITCH_MIN_SLOPE_DEG = 10.0
PITCH_MAX_SLOPE_DEG = 65.0
GABLE_ASPECT_TOLERANCE_DEG = 35.0
GABLE_SLOPE_DIFFERENCE_MAX_DEG = 15.0
ROOF_SHAPES = ("flat", "mono", "gable", "hip", "complex")


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
    the evidence is below the honesty floor (too few samples, or the
    planes explain less than MIN_QUALITY of them): None means NO tags,
    the spec's absence-over-fabrication rule.
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

    shape = "complex"
    direction: float | None = None
    if len(flat) == len(significant):
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
    elif 3 <= len(significant) <= 4 and len(pitched) == len(significant):
        pair = None
        for i in range(len(pitched)):
            for j in range(i + 1, len(pitched)):
                if _is_opposite_pair(pitched[i], pitched[j]):
                    pair = (pitched[i], pitched[j])
                    break
            if pair:
                break
        if pair is not None:
            pair_aspect = pair[0].plane.downslope_azimuth_deg()
            others = [f for f in pitched if f is not pair[0] and f is not pair[1]]
            end_face = any(
                abs(
                    _aspect_difference(
                        f.plane.downslope_azimuth_deg(), pair_aspect
                    )
                    - 90.0
                )
                <= 45.0
                for f in others
            )
            if end_face:
                shape = "hip"
                direction = ridge_azimuth_deg(pair[0].plane, pair[1].plane)
                if direction is None:
                    shape = "complex"

    z_values = [points[i][2] for i in sorted(assigned)]
    if shape == "flat":
        level = round(_percentile(z_values, 0.5), 1)
        eaves = ridge = level
    else:
        eaves = round(_percentile(z_values, 0.10), 1)
        ridge = round(_percentile(z_values, 0.95), 1)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_roofs.py -v`
Expected: all PASS. If the hip construction lands `complex`, that is the
test's own allowance; record which way it went in the task report, Task 4
weighs it.

- [ ] **Step 5: Run the full suite, then commit**

Run: `python -m pytest tests/ -m "not live" -q`

```bash
git add src/mapgen/roofs.py tests/test_roofs.py
git commit -m "feat(roofs): shape classification with quality floor"
```

---

### Task 3: The `.osm` loop, roof tags, and the record

**Files:**
- Modify: `src/mapgen/roofs.py`
- Test: `tests/test_roofs.py`

**Interfaces:**
- Consumes: Task 1 and 2 names; `heights._declaration_line`,
  `heights._rewrite_osm`, `heights._footprint_ring`, `heights._median`
  via `_percentile`; `cog.BngWindow`; `bng.to_bng`, `bng.Ostn15Grid`.
- Produces (Task 7 relies on these exact names):
  - `RoofsError(RuntimeError)`
  - `RoofsRecord(buildings: int, classified: int, kept_existing: int,
    below_quality: int, no_data: int, relations_skipped: int,
    shapes: dict[str, int])` frozen dataclass
  - `fit_roof_forms(osm_path: Path, dtm: BngWindow, dsm: BngWindow,
    grid: Ostn15Grid, massing_path: Path | None = None) -> RoofsRecord`
    (`massing_path` is Task 5's seam; until then it is accepted and
    ignored with a one-line comment saying Task 5 fills it in)
  - Tag constants: `ROOF_SHAPE_TAG_KEY = "roof:shape"`,
    `ROOF_DIRECTION_TAG_KEY = "roof:direction"`,
    `ROOF_EAVES_TAG_KEY = "roof:height:eaves"`,
    `ROOF_RIDGE_TAG_KEY = "roof:height:ridge"`,
    `SOURCE_ROOF_TAG_KEY = "source:roof"`,
    `SOURCE_ROOF_ATTRIBUTION = "Welsh Government LiDAR 2020 to 2023 (DSM plane fit)"`

Mechanics, pinned:

- The loop mirrors `heights.fuse_building_heights` exactly: parse with
  the declaration check, node table, ways tagged `building=*` only,
  relations counted `relations_skipped` and never touched, atomic
  rewrite ONLY when something changed.
- A way already carrying `roof:shape` is `kept_existing`, untouched:
  idempotent by tag detection, byte-identical second run.
- Sampling: the footprint's 1 m interior grid; a point contributes only
  where BOTH rasters answer. Ground is the median of the DTM samples
  (the heights.py convention); each roof sample's z is its DSM value
  minus that ground, so eaves and ridge are metres above ground.
  Points are centred (mean easting and northing subtracted) before
  fitting; the sampler returns `(points, ground, centre)` with `centre`
  kept for Task 5's massing.
- Fewer than `MIN_ROOF_SAMPLES` samples, an unresolvable ring, or no
  OSTN15 coverage -> `no_data`. `classify_roof` returning None ->
  `below_quality`. Both write NOTHING.
- Tags written on a classified way: `roof:shape` always;
  `roof:direction` only when `direction_deg` is not None, formatted
  `f"{direction:.0f}"`; `roof:height:eaves` and `roof:height:ridge` as
  `f"{v:.1f}"`; `source:roof` = `SOURCE_ROOF_ATTRIBUTION`. Counted in
  `shapes[form.shape]`.
- `buildings == classified + kept_existing + below_quality + no_data`
  always.
- Per-building rng is `random.Random(0)` (fresh per footprint):
  deterministic and independent of processing order.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_roofs.py`. Build the `.osm` fixture and windows the
way `tests/test_heights.py` builds its own (read that file first and
reuse its helper shapes; the plan repeats the essential fixture here so
this task stands alone):

```python
import xml.etree.ElementTree as ET
from array import array
from pathlib import Path

from mapgen.bng import from_bng, load_ostn15
from mapgen.cog import BngWindow
from mapgen.roofs import (
    ROOF_SHAPE_TAG_KEY,
    SOURCE_ROOF_ATTRIBUTION,
    RoofsRecord,
    fit_roof_forms,
)

# One OSTN15 grid for the class, exactly as test_heights.py loads its own.
# If load_ostn15() answers None on this machine the tests are skipped
# with the same marker/reason test_heights.py uses (read it first).


def _window(e0, n0, size, values, pixel=1.0):
    return BngWindow(
        e_origin=e0, n_top=n0 + size * pixel, pixel_size=pixel,
        width=size, height=size, values=array("f", values),
    )


def _gable_windows(e0, n0, size=20, eaves=5.0, ridge=8.0):
    """DTM flat at 100.0; DSM carries a west-east gable over the middle
    of the window (ridge running north-south at the window's centre
    column), background at ground."""
    ground = [100.0] * (size * size)
    dsm = []
    centre = size / 2.0
    for row in range(size):
        for col in range(size):
            offset = abs((col + 0.5) - centre)
            if 4 <= row < size - 4 and offset <= 6.0:
                dsm.append(100.0 + max(eaves, ridge - (ridge - eaves) * offset / 6.0))
            else:
                dsm.append(100.0)
    return _window(e0, n0, size, ground), _window(e0, n0, size, dsm)


def _osm_with_building(path, ring_bng, grid, extra_tags=()):
    """Write a minimal mapgen-shaped .osm holding one closed building way
    whose nodes are `ring_bng` projected to lat/lon via from_bng."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="test">']
    refs = []
    for i, (e, n) in enumerate(ring_bng, start=1):
        lat, lon = from_bng(e, n, grid)
        lines.append(f'  <node id="{i}" lat="{lat:.7f}" lon="{lon:.7f}" />')
        refs.append(i)
    nds = "".join(f'<nd ref="{r}" />' for r in refs + [refs[0]])
    tags = '<tag k="building" v="yes" />' + "".join(
        f'<tag k="{k}" v="{v}" />' for k, v in extra_tags
    )
    lines.append(f'  <way id="100">{nds}{tags}</way>')
    lines.append("</osm>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestFitRoofForms:
    def test_gable_building_gains_roof_tags(self, tmp_path):
        grid = load_ostn15()
        e0, n0 = 318000.0, 176000.0
        dtm, dsm = _gable_windows(e0, n0)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid)
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.buildings == 1
        assert record.classified == 1
        assert record.shapes.get("gable") == 1
        root = ET.fromstring(osm.read_text(encoding="utf-8"))
        way = root.find("way")
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        assert tags["roof:shape"] == "gable"
        assert float(tags["roof:height:eaves"]) < float(tags["roof:height:ridge"])
        assert tags["source:roof"] == SOURCE_ROOF_ATTRIBUTION
        # Ridge runs north-south in the fixture: direction near 0 or 180.
        direction = float(tags["roof:direction"])
        assert min(direction, 180.0 - direction) < 10.0

    def test_second_run_is_byte_identical(self, tmp_path):
        grid = load_ostn15()
        e0, n0 = 318000.0, 176000.0
        dtm, dsm = _gable_windows(e0, n0)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid)
        fit_roof_forms(osm, dtm, dsm, grid)
        first = osm.read_bytes()
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.kept_existing == 1
        assert record.classified == 0
        assert osm.read_bytes() == first

    def test_no_raster_data_writes_nothing(self, tmp_path):
        grid = load_ostn15()
        e0, n0 = 318000.0, 176000.0
        nan = float("nan")
        dtm = _window(e0, n0, 20, [nan] * 400)
        dsm = _window(e0, n0, 20, [nan] * 400)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid)
        before = osm.read_bytes()
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.no_data == 1
        assert osm.read_bytes() == before

    def test_counts_always_reconcile(self, tmp_path):
        # buildings == classified + kept_existing + below_quality + no_data
        grid = load_ostn15()
        e0, n0 = 318000.0, 176000.0
        dtm, dsm = _gable_windows(e0, n0)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid, extra_tags=(("roof:shape", "gable"),))
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.buildings == (
            record.classified + record.kept_existing
            + record.below_quality + record.no_data
        )
        assert record.kept_existing == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_roofs.py -k FitRoofForms -v`
Expected: FAIL with `ImportError` (no `fit_roof_forms`)

- [ ] **Step 3: Write the implementation**

Append to `src/mapgen/roofs.py`. The loop is `fuse_building_heights`
transposed, with the sampler keeping positions:

```python
import xml.etree.ElementTree as ET
from pathlib import Path

from mapgen.bng import BngError, Ostn15Grid, to_bng
from mapgen.cog import BngWindow
from mapgen.heights import (
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

_BUILDING_TAG_KEY = "building"


class RoofsError(RuntimeError):
    """Raised when a package's own `.osm` cannot be read or rewritten,
    matching `HeightsError`'s scope exactly: caught and recorded by the
    package step, never allowed to take a survey down."""


@dataclass(frozen=True)
class RoofsRecord:
    buildings: int
    classified: int
    kept_existing: int
    below_quality: int
    no_data: int
    relations_skipped: int
    shapes: dict


def _roof_samples(ring_bng, dtm: BngWindow, dsm: BngWindow):
    """(points, ground, centre) for one footprint, or None when the
    evidence is below the floor. `points` are (e, n, z above ground),
    centred on the footprint's own sample mean for conditioning; `ground`
    is the DTM median (the heights.py convention); `centre` is the
    (mean easting, mean northing) the centring subtracted, kept so Task
    5's massing can place geometry back in real BNG."""
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
    for key, value in (
        (ROOF_SHAPE_TAG_KEY, form.shape),
        (ROOF_DIRECTION_TAG_KEY, None if form.direction_deg is None else f"{form.direction_deg:.0f}"),
        (ROOF_EAVES_TAG_KEY, f"{form.eaves_m:.1f}"),
        (ROOF_RIDGE_TAG_KEY, f"{form.ridge_m:.1f}"),
        (SOURCE_ROOF_TAG_KEY, SOURCE_ROOF_ATTRIBUTION),
    ):
        if value is None:
            continue
        tag = ET.SubElement(way, "tag")
        tag.set("k", key)
        tag.set("v", value)


def fit_roof_forms(
    osm_path: Path,
    dtm: BngWindow,
    dsm: BngWindow,
    grid: Ostn15Grid,
    massing_path: Path | None = None,
) -> RoofsRecord:
    """Fit and tag a roof form onto every untagged building way in
    `osm_path`, atomically, idempotently, absence over fabrication
    throughout. Task 5 teaches `massing_path` to write the massing
    GeoJSON; until then it is accepted and ignored."""
    path = Path(osm_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoofsError(f"{path.name} could not be read: {exc}") from None
    try:
        declaration = _declaration_line(text)
    except Exception as exc:
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
        shapes[form.shape] = shapes.get(form.shape, 0) + 1
        classified += 1
        changed = True

    if changed:
        _rewrite_osm(path, declaration, root)

    return RoofsRecord(
        buildings=buildings,
        classified=classified,
        kept_existing=kept_existing,
        below_quality=below_quality,
        no_data=no_data,
        relations_skipped=relations_skipped,
        shapes=shapes,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_roofs.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full suite, then commit**

Run: `python -m pytest tests/ -m "not live" -q`

```bash
git add src/mapgen/roofs.py tests/test_roofs.py
git commit -m "feat(roofs): fit and tag roof forms on fused building ways"
```

---

### Task 4: VALIDATION over the owner's real Cowbridge package (gates everything after it)

This is the owner-required validation task ("we have to test to see if
its any good", 2026-08-07). NO package wiring exists yet, so nothing has
shipped; this task decides what MAY ship. Its artifacts live in the plan
workspace, never in a package and never committed.

**Files:**
- Read (copies only): the owner's real packages under
  `C:\Users\Param\Surveys\Vale-of-Glamorgan\` (Cowbridge 2026-08-06 for
  the run, Port-Talbot 2026-08-07 for the gate check). COPY the needed
  files to the session scratchpad first; the originals are read-only and
  never opened for writing.
- Create (workspace, not committed):
  `.superpowers/sdd/2026-08-08-mapgen-phase2b-b-roofs-canopy/roof-validation.md`
  and `roof-validation.geojson`.
- Possibly modify: `src/mapgen/roofs.py` constants and `tests/test_roofs.py`
  expectations, with every change evidenced in the report.

**Interfaces:**
- Consumes: `fit_roof_forms`, `read_full_window`, `CogReader`,
  `FileByteSource` (from `mapgen.cog`), `load_ostn15`.
- Produces: the validation report and verdicts; possibly retuned
  constants; possibly a REDUCED `ROOF_SHAPES` vocabulary.

- [ ] **Step 1: Copy the packages and confirm the resolution facts**

Copy `Cowbridge-with-Llanblethian_2026-08-06_lidar_dtm.tif`, `..._dsm.tif`
and `Cowbridge-with-Llanblethian_2026-08-06.osm` to the scratchpad, and
the Port Talbot pair's HEADERS only need reading in place (open with
`CogReader`, no write). Record in the report:
- Cowbridge `pixel_size`/`pixel_height` for both rasters (expected 1.0:
  the gate admits it)
- Port Talbot `pixel_size` (expected about 2.0: the gate must refuse it;
  assert `> 1.5`)

- [ ] **Step 2: Run the fitter over the Cowbridge copy and time it**

A scratch script (workspace, not committed) that loads the copied
windows and `.osm`, runs `fit_roof_forms`, prints the `RoofsRecord`, and
writes `roof-validation.geojson`: one Point feature per classified
building at the footprint's sample centre, properties
`{"shape", "direction", "eaves", "ridge", "quality", "way": id}`, plus
one per `below_quality`/`no_data` building with `{"shape": null,
"reason": ...}`. Wall time recorded; if the whole run exceeds 180
seconds, that is a finding for the controller (the categorised
boundaries step runs in about 7 s on this same package; the roof step
may honestly cost more, but the number must be on the table).

- [ ] **Step 3: Execute the pinned checks, verdict each in the report**

1. **Reconciliation (hard):** `buildings == classified + kept_existing +
   below_quality + no_data` and every pitched classification has
   `eaves < ridge`.
2. **Flat honesty:** among `flat` classifications, at least 95% have
   `ridge - eaves <= 0.5`.
3. **Gable ridge alignment:** for at least 75% of `gable` buildings, the
   ridge direction sits within 15 degrees of the footprint's longest
   edge azimuth (computed from the way's own ring; fold both to
   [0, 180)). Gable housing overwhelmingly ridges along its long axis;
   systematic misalignment means the fitter is reading noise.
4. **Distribution sanity:** Cowbridge is a Welsh market town of pitched
   housing. `gable` plus `hip` together must exceed 40% of classified
   buildings, and `flat` must not be the single largest class. (If it
   is, the fitter is flattening pitches it cannot resolve.)
5. **Classified fraction:** at least 50% of buildings with a usable
   sample count end classified (not `below_quality`). Lower means the
   quality floor or tolerance wants tuning, with evidence.
6. **Spike robustness on real data:** take 20 classified buildings,
   re-run each with 3 synthetic +10 m single-sample spikes injected into
   its own real samples (script-level, by wrapping `_roof_samples`
   output); at least 90% keep their class and their ridge within 0.5 m.
7. **Low-quality eyeball list:** the 10 lowest-quality classified
   buildings, listed with their numbers so the controller (and the
   owner, via the geojson) can judge whether marginal fits look
   defensible.

- [ ] **Step 4: Act on the verdicts**

- All pass: record PASS per check and proceed.
- A check fails on a tunable (tolerance, floors, aspect windows): tune
  the constant in `roofs.py`, update any test expectation that pinned
  the old value, re-run the suite AND the Cowbridge run, document
  before/after numbers in the report. At most two tuning rounds; more
  means the approach is wrong, STOP and report BLOCKED.
- A CLASS fails specifically (the spec's own example: hip and cross
  gable indistinguishable at 1 m, or gables misreading): DROP the class
  from `ROOF_SHAPES` and route its cases to `complex` (or `gable` where
  the evidence says so), update `classify_roof` and tests, document the
  drop and the evidence. Dropping is the spec's prescribed outcome, not
  a failure.
- `gable` AND `hip` both fail after tuning: the 1 m DSM cannot support
  roof forms at all. STOP, report BLOCKED to the controller; Tasks 5 to
  7's roof portions do not proceed (canopy is unaffected).

- [ ] **Step 5: Commit only what changed in code**

If constants or vocabulary changed:

```bash
git add src/mapgen/roofs.py tests/test_roofs.py
git commit -m "feat(roofs): validation-tuned thresholds from the real Cowbridge run"
```

If nothing changed, no commit; the report is the deliverable either way.

---

### Task 5: Massing GeoJSON (`<stem>_roof_massing.geojson`)

**Files:**
- Modify: `src/mapgen/roofs.py`
- Test: `tests/test_roofs.py`

**Interfaces:**
- Consumes: Task 3's loop (the `massing_path` parameter goes live),
  `bng.from_bng`, `fsutil.atomic_write_bytes`, `json`.
- Produces: `fit_roof_forms(..., massing_path=...)` writing a
  FeatureCollection; helper `_ridge_segment(pair, ring_bng) ->
  tuple[tuple[float, float, float], tuple[float, float, float]] | None`
  (two (e, n, z) endpoints in real BNG).

Feature shapes, pinned (simple by design: GH extrudes the eaves polygon
from the ground and lofts to the ridge line):

- Per CLASSIFIED building, one Polygon feature: the footprint ring in
  WGS84 with a third coordinate, `[lon, lat, eaves_m]` at every vertex
  (z is metres above the building's own ground). Properties:
  `{"building": <way id>, "shape", "direction", "eaves", "ridge",
  "quality", "ground_m": <DTM median, 2 dp>, "source":
  SOURCE_ROOF_ATTRIBUTION, "note": "derived from LiDAR plane fits,
  indicative"}`. `direction` null when the form has none.
- Per `gable`/`hip` building additionally one LineString feature, the
  two dominant planes' intersection line clipped to the footprint:
  `[[lon, lat, ridge_m], [lon, lat, ridge_m]]`, properties
  `{"building": <way id>, "feature": "ridge"}` plus the same source and
  note. `_ridge_segment` parametrises the intersection line (solve one
  coordinate from `(a1-a2)e + (b1-b2)n = c2-c1` at the footprint
  centroid, direction from `ridge_azimuth_deg`'s vector), intersects it
  with every ring edge in 2D, and takes the span between the smallest
  and largest intersection parameters; fewer than two intersections, or
  a near-parallel pair, means no ridge feature (the polygon still
  ships). On a concave footprint the span can bridge a notch; the note
  field's "indicative" covers exactly this and the docstring says so.
- Unclassified buildings contribute NOTHING (absence over fabrication).
- The file is written atomically, only when at least one building
  classified; an empty classification writes no file at all.
- Written in `fit_roof_forms` itself (single fitting pass, no refit for
  massing): the loop accumulates features when `massing_path` is given.

- [ ] **Step 1: Write the failing tests**

```python
import json

class TestMassing:
    def test_classified_building_emits_polygon_and_ridge(self, tmp_path):
        grid = load_ostn15()
        e0, n0 = 318000.0, 176000.0
        dtm, dsm = _gable_windows(e0, n0)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid)
        massing = tmp_path / "site_roof_massing.geojson"
        fit_roof_forms(osm, dtm, dsm, grid, massing_path=massing)
        collection = json.loads(massing.read_text(encoding="utf-8"))
        kinds = [f["geometry"]["type"] for f in collection["features"]]
        assert kinds.count("Polygon") == 1
        assert kinds.count("LineString") == 1
        polygon = next(f for f in collection["features"]
                       if f["geometry"]["type"] == "Polygon")
        ridge = next(f for f in collection["features"]
                     if f["geometry"]["type"] == "LineString")
        eaves_z = polygon["geometry"]["coordinates"][0][0][2]
        ridge_z = ridge["geometry"]["coordinates"][0][2]
        assert eaves_z < ridge_z
        assert polygon["properties"]["quality"] >= 0.75
        assert polygon["properties"]["ground_m"] == 100.0
        assert "indicative" in polygon["properties"]["note"]

    def test_no_classification_writes_no_file(self, tmp_path):
        grid = load_ostn15()
        e0, n0 = 318000.0, 176000.0
        nan = float("nan")
        dtm = _window(e0, n0, 20, [nan] * 400)
        dsm = _window(e0, n0, 20, [nan] * 400)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid)
        massing = tmp_path / "site_roof_massing.geojson"
        fit_roof_forms(osm, dtm, dsm, grid, massing_path=massing)
        assert not massing.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_roofs.py -k Massing -v`
Expected: FAIL (massing_path currently ignored)

- [ ] **Step 3: Implement**

In `fit_roof_forms`, accumulate features next to `_write_roof_tags`
(the fitted `form`, `ground`, `centre` and `projected` ring are all in
scope), and after the rewrite block:

```python
def _ridge_segment(pair, ring_bng):
    """Two (e, n, z) endpoints of the planes' intersection clipped to the
    footprint, in the CENTRED frame the planes live in (the caller adds
    the centre back), or None. See the plan for the parametrisation."""
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
        u = ((x1 - e0) * dn - (y1 - n0) * de) / -denom
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
```

The polygon feature projects each ring vertex through `from_bng` and
appends `form.eaves_m`; the ridge feature adds `centre` back to the
segment endpoints before `from_bng`, and carries `form.ridge_m` as its
z (the fitted plane height AT the ridge is the same number within fit
noise; the tagged ridge value is the one the file must agree with).
The ring passed to `_ridge_segment` is the projected footprint MINUS
the centre (the planes' own frame). Gable and hip only, and only when
`form.direction_deg is not None`. Write with `atomic_write_bytes`,
`json.dumps(collection, indent=2)`, only when features exist.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_roofs.py -v`

- [ ] **Step 5: Full suite, commit**

Run: `python -m pytest tests/ -m "not live" -q`

```bash
git add src/mapgen/roofs.py tests/test_roofs.py
git commit -m "feat(roofs): massing geojson with eaves polygons and ridge lines"
```

---

### Task 6: Canopy points (`canopy.py`)

**Files:**
- Create: `src/mapgen/canopy.py`
- Test: `tests/test_canopy.py`

**Interfaces:**
- Consumes: `cog.BngWindow`, `bng.from_bng`, `bng.Ostn15Grid`,
  `buildings.point_in_ring`, `heights._percentile`, `collections.deque`.
- Produces (Task 7 relies on these exact names):
  - `CanopyError(RuntimeError)`
  - `CanopyRecord(points: int, skipped_small: int, resolution_m: float)`
    frozen dataclass
  - `build_canopy(dtm: BngWindow, dsm: BngWindow,
    footprints_bng: list[list[tuple[float, float]]], grid: Ostn15Grid)
    -> tuple[list[dict], CanopyRecord]` (GeoJSON Point feature dicts)
  - Constants: `CANOPY_MIN_HEIGHT_METRES = 3.0`,
    `MIN_CLUSTER_AREA_M2 = 8.0`, `MIN_CLUSTER_PIXELS = 2`,
    `CANOPY_SOURCE = "Welsh Government LiDAR 2020 to 2023 (DSM minus DTM)"`,
    `CANOPY_NOTE = "vegetation and other above-ground features, derived from LiDAR, indicative"`

Mechanics, pinned:

- The two windows must agree on `e_origin`, `n_top`, `pixel_size`,
  `pixel_height`, `width`, `height`; a mismatch raises `CanopyError`
  (the package step records it). They always agree for rasters one
  merge wrote; the check is for the file someone replaced by hand.
- nDSM per pixel: `dsm - dtm`; NaN (either side) is no-data, excluded.
- Footprint mask: for each footprint ring (BNG), only the pixels of its
  own bounding box are tested, pixel centre against `point_in_ring`;
  masked pixels are excluded BEFORE thresholding. Cost is proportional
  to footprint area, not raster area.
- Threshold: nDSM `>= CANOPY_MIN_HEIGHT_METRES` (3.0, spec verbatim).
- Connected components: 4-connectivity, BFS with `deque`, over the
  surviving pixels.
- A cluster below `MIN_CLUSTER_AREA_M2` OR `MIN_CLUSTER_PIXELS` is
  counted `skipped_small`, not emitted: a single 1 m spike pixel (a
  pylon top, a bad return) never becomes a tree. Area is
  `pixels * pixel_size * pixel_height`.
- Per emitted cluster, one Point feature: position is the mean of member
  pixel CENTRES (BNG) through `from_bng` as `[lon, lat]`; properties
  `{"height": p90 of the cluster's nDSM, 1 dp, "crown_radius":
  sqrt(area/pi), 1 dp, "resolution": f"{pixel_size:g} m", "source":
  CANOPY_SOURCE, "note": CANOPY_NOTE}`. p90, not max: the owner's spike
  rule applied to trees.
- Canopy is NOT gated to the 1 m level (the spec's gate binds roof
  fitting only); the `resolution` property says honestly what it ran at.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_canopy.py"""
import math
from array import array

import pytest

from mapgen.bng import load_ostn15
from mapgen.canopy import (
    CANOPY_MIN_HEIGHT_METRES,
    CanopyError,
    CanopyRecord,
    build_canopy,
)
from mapgen.cog import BngWindow


def _window(values, size=12, e0=318000.0, n0=176000.0, pixel=1.0):
    return BngWindow(
        e_origin=e0, n_top=n0 + size * pixel, pixel_size=pixel,
        width=size, height=size, values=array("f", values),
    )


def _flat(level, size=12):
    return [level] * (size * size)


class TestBuildCanopy:
    def test_a_tree_cluster_becomes_one_point(self):
        grid = load_ostn15()
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
        grid = load_ostn15()
        dsm = _flat(100.0)
        dsm[5 * 12 + 5] = 140.0
        features, record = build_canopy(
            _window(_flat(100.0)), _window(dsm), [], grid
        )
        assert features == []
        assert record.skipped_small == 1

    def test_canopy_inside_a_footprint_is_masked(self):
        grid = load_ostn15()
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
        grid = load_ostn15()
        dsm = _flat(100.0 + CANOPY_MIN_HEIGHT_METRES - 0.1)
        features, record = build_canopy(
            _window(_flat(100.0)), _window(dsm), [], grid
        )
        assert features == []

    def test_mismatched_windows_refused(self):
        grid = load_ostn15()
        with pytest.raises(CanopyError):
            build_canopy(
                _window(_flat(100.0), size=12),
                _window(_flat(100.0), size=12, e0=999000.0),
                [],
                grid,
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_canopy.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/mapgen/canopy.py`**

```python
"""Canopy points from a package's own LiDAR: DSM minus DTM outside every
building footprint, thresholded at 3 m, clustered, one point per
cluster. Honest by construction: the note on every feature says
"vegetation and other above-ground features" because a pylon and a crane
clear 3 m too, and this module cannot tell species from a raster.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from mapgen.bng import Ostn15Grid, from_bng
from mapgen.buildings import point_in_ring
from mapgen.cog import BngWindow
from mapgen.heights import _percentile

CANOPY_MIN_HEIGHT_METRES = 3.0
MIN_CLUSTER_AREA_M2 = 8.0
MIN_CLUSTER_PIXELS = 2
CANOPY_SOURCE = "Welsh Government LiDAR 2020 to 2023 (DSM minus DTM)"
CANOPY_NOTE = (
    "vegetation and other above-ground features, derived from LiDAR, indicative"
)


class CanopyError(RuntimeError):
    """Raised for windows this module cannot honestly difference (grid
    mismatch). Caught and recorded by the package step, never fatal to a
    survey."""


@dataclass(frozen=True)
class CanopyRecord:
    points: int
    skipped_small: int
    resolution_m: float


def _pixel_centre(window: BngWindow, col: int, row: int) -> tuple[float, float]:
    return (
        window.e_origin + (col + 0.5) * window.pixel_size,
        window.n_top - (row + 0.5) * window.pixel_height,
    )


def build_canopy(dtm, dsm, footprints_bng, grid):
    for attr in ("e_origin", "n_top", "pixel_size", "pixel_height", "width", "height"):
        if getattr(dtm, attr) != getattr(dsm, attr):
            raise CanopyError(
                "The DTM and DSM do not share one pixel grid, so their "
                "difference would not mean anything; this package's rasters "
                "were not written together."
            )
    width, height = dsm.width, dsm.height
    ndsm: dict[int, float] = {}
    for index in range(width * height):
        d = dsm.values[index]
        t = dtm.values[index]
        if math.isnan(d) or math.isnan(t):
            continue
        value = d - t
        if value >= CANOPY_MIN_HEIGHT_METRES:
            ndsm[index] = value

    # Mask footprint interiors, bbox-bounded per ring.
    for ring in footprints_bng:
        eastings = [e for e, _ in ring]
        northings = [n for _, n in ring]
        col0 = max(0, int((min(eastings) - dsm.e_origin) / dsm.pixel_size))
        col1 = min(width - 1, int((max(eastings) - dsm.e_origin) / dsm.pixel_size))
        row0 = max(0, int((dsm.n_top - max(northings)) / dsm.pixel_height))
        row1 = min(height - 1, int((dsm.n_top - min(northings)) / dsm.pixel_height))
        for row in range(row0, row1 + 1):
            for col in range(col0, col1 + 1):
                index = row * width + col
                if index not in ndsm:
                    continue
                e, n = _pixel_centre(dsm, col, row)
                if point_in_ring(e, n, ring):
                    del ndsm[index]

    pixel_area = dsm.pixel_size * dsm.pixel_height
    seen: set[int] = set()
    features: list[dict] = []
    skipped_small = 0
    for start in sorted(ndsm):
        if start in seen:
            continue
        cluster = []
        queue = deque([start])
        seen.add(start)
        while queue:
            index = queue.popleft()
            cluster.append(index)
            row, col = divmod(index, width)
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                r, c = row + dr, col + dc
                if not (0 <= r < height and 0 <= c < width):
                    continue
                neighbour = r * width + c
                if neighbour in ndsm and neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        area = len(cluster) * pixel_area
        if len(cluster) < MIN_CLUSTER_PIXELS or area < MIN_CLUSTER_AREA_M2:
            skipped_small += 1
            continue
        centres = [_pixel_centre(dsm, index % width, index // width) for index in cluster]
        e_mean = sum(e for e, _ in centres) / len(centres)
        n_mean = sum(n for _, n in centres) / len(centres)
        lat, lon = from_bng(e_mean, n_mean, grid)
        heights = [ndsm[index] for index in cluster]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "height": round(_percentile(heights, 0.9), 1),
                    "crown_radius": round(math.sqrt(area / math.pi), 1),
                    "resolution": f"{dsm.pixel_size:g} m",
                    "source": CANOPY_SOURCE,
                    "note": CANOPY_NOTE,
                },
            }
        )
    return features, CanopyRecord(
        points=len(features),
        skipped_small=skipped_small,
        resolution_m=dsm.pixel_size,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_canopy.py -v`

- [ ] **Step 5: Full suite, commit**

Run: `python -m pytest tests/ -m "not live" -q`

```bash
git add src/mapgen/canopy.py tests/test_canopy.py
git commit -m "feat(canopy): clustered canopy points from the nDSM"
```

---

### Task 7: Package wiring, the resolution gate, records and events

**Files:**
- Modify: `src/mapgen/package.py`
- Test: `tests/test_package.py`

**Interfaces:**
- Consumes: `fit_roof_forms`, `RoofsError`, `RoofsRecord`,
  `build_canopy`, `CanopyError`, `buildings._existing_building_footprints`
  (relation members included), plus the same window/OSTN15 loading
  `_fuse_heights_step` already does.
- Produces: `_fit_roofs_step(root, stem, sink)` and
  `_canopy_step(root, stem, sink)`; survey.json keys `roof_forms` and
  `canopy` in BOTH pipelines; events `roof_forms_started`,
  `roof_forms_skipped`, `roof_forms_written`, `roof_forms_failed`,
  `canopy_started`, `canopy_skipped`, `canopy_written`, `canopy_failed`;
  constant `ROOF_MAX_PIXEL_METRES = 1.5`.

Mechanics, pinned:

- Both steps follow `_fuse_heights_step`'s one-shape-whatever-happened
  pattern to the letter: file gates first (`<stem>.osm` +
  `<stem>_lidar_dtm.tif` + `<stem>_lidar_dsm.tif`; missing means the
  skipped event and the all-None record), every failure caught
  (`RoofsError`/`CanopyError`/`CogError`/`BngError`/`OSError`) and
  recorded, never raised.
- **The resolution gate (`_fit_roofs_step` only, the spec's own scope):**
  after reading both windows, if
  `max(pixel_size, pixel_height)` of EITHER window exceeds
  `ROOF_MAX_PIXEL_METRES`, emit
  `sink.emit("roof_forms_skipped", reason=reason)` and record
  `{"skipped_reason": reason}` with the all-None counts, where `reason`
  is the Global Constraints' pinned string with the coarser pixel size
  substituted. The UI's log prints every event with its fields by name
  (app.js's own documented contract), so this is the spec's UI-visible
  reason with no JS change; assert the reason string in the Python test,
  and no Node test changes.
- `_fit_roofs_step` passes
  `massing_path=Path(root) / f"{stem}_roof_massing.geojson"`.
- `_canopy_step` builds its footprint list from the fused `.osm` via
  `buildings._existing_building_footprints` (ways AND relation outer
  members), projecting each ring to BNG with the loaded OSTN15 grid,
  skipping rings that fail projection (they simply do not mask). It
  writes `<stem>_canopy.geojson` (FeatureCollection, atomic,
  `indent=2`) even when empty of features ONLY if features exist; zero
  features writes no file, records `points: 0`.
- Wiring order in BOTH `run_survey` (immediately after the
  `_fuse_heights_step` call, before `_fuse_boundaries_step`) and
  `bridge_package` (same position): roofs first, then canopy. Neither
  is gated on `stopped`/`unrecoverable`, matching the neighbouring
  steps' reasoning verbatim.
- survey.json records:
  - `roof_forms`: `{"buildings", "classified", "kept_existing",
    "below_quality", "no_data", "relations_skipped", "shapes",
    "skipped_reason", "error"}` (all None except what happened; helper
    `_roofs_record(**overrides)` in the style of `_heights_record`).
  - `canopy`: `{"points", "skipped_small", "resolution_m", "error"}`
    (helper `_canopy_record(**overrides)`).
- Bridge parity gives OLD packages roofs and canopy from a plain
  `mapgen bridge`: unlike item A's parcels (which need a fresh merge),
  every input here (`.osm`, both tifs) is already in every Welsh
  package. This asymmetry is deliberate and Task 8 documents it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_package.py`, following its existing heights-fusion
test fixtures (`write_bng_geotiff`, `_constant_window`; read those
first). Cases, each its own test:

1. A survey-shaped folder with a 1 m gable fixture (reuse Task 3's
   window builder via `write_bng_geotiff`) and one building way: after
   `_fit_roofs_step`, the way carries `roof:shape`, the massing file
   exists, the record's `classified == 1`, and the sink saw
   `roof_forms_started` then `roof_forms_written`.
2. The same folder with rasters written at `pixel_size=2.0000004`:
   `_fit_roofs_step` records `skipped_reason` containing
   `"needs the 1 m LiDAR level"` and `"4 x 4 km"`, the `.osm` is
   byte-unchanged, no massing file, and the sink saw
   `roof_forms_skipped` with a `reason` field.
3. Missing lidar tifs: the all-None record, `roof_forms_skipped`
   without a reason field, no exception.
4. `_canopy_step` over a fixture with one 3x3 canopy block outside the
   building and canopy pixels inside the footprint: `<stem>_canopy.geojson`
   holds exactly one Point, the in-footprint block is absent, record
   `points == 1`.
5. `bridge_package` over a completed fixture package re-runs both steps:
   `roof_forms.kept_existing == 1` on the second pass (idempotent), and
   survey.json carries both keys with the run's real values.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_package.py -k "roof or canopy" -v`
Expected: FAIL (`_fit_roofs_step` undefined)

- [ ] **Step 3: Implement the two steps and the wiring**

`_fit_roofs_step` skeleton (the docstring follows `_fuse_heights_step`'s
in substance; write it fresh, not copied):

```python
ROOF_MAX_PIXEL_METRES = 1.5


def _roofs_record(**overrides) -> dict[str, object]:
    record: dict[str, object] = {
        "buildings": None, "classified": None, "kept_existing": None,
        "below_quality": None, "no_data": None, "relations_skipped": None,
        "shapes": None, "skipped_reason": None, "error": None,
    }
    record.update(overrides)
    return record


def _fit_roofs_step(root: Path, stem: str, sink: ProgressSink) -> dict[str, object]:
    osm_path = Path(root) / f"{stem}.osm"
    dtm_path = Path(root) / f"{stem}_lidar_dtm.tif"
    dsm_path = Path(root) / f"{stem}_lidar_dsm.tif"
    if not (osm_path.is_file() and dtm_path.is_file() and dsm_path.is_file()):
        sink.emit("roof_forms_skipped")
        return _roofs_record()
    try:
        grid = load_ostn15()
        if grid is None:
            grid = ensure_ostn15()
        dtm_window = read_full_window(CogReader.open(FileByteSource(dtm_path)))
        dsm_window = read_full_window(CogReader.open(FileByteSource(dsm_path)))
        coarsest = max(
            dtm_window.pixel_size, dtm_window.pixel_height,
            dsm_window.pixel_size, dsm_window.pixel_height,
        )
        if coarsest > ROOF_MAX_PIXEL_METRES:
            reason = (
                f"roof fitting needs the 1 m LiDAR level and this package's "
                f"rasters are {coarsest:g} m; extents under about 4 x 4 km "
                f"come back at 1 m"
            )
            sink.emit("roof_forms_skipped", reason=reason)
            return _roofs_record(skipped_reason=reason)
        sink.emit("roof_forms_started")
        record = fit_roof_forms(
            osm_path, dtm_window, dsm_window, grid,
            massing_path=Path(root) / f"{stem}_roof_massing.geojson",
        )
    except (RoofsError, CogError, BngError, OSError) as exc:
        error = str(exc)
        sink.emit("roof_forms_failed", error=error)
        return _roofs_record(error=error)
    sink.emit(
        "roof_forms_written",
        classified=record.classified,
        buildings=record.buildings,
        below_quality=record.below_quality,
        no_data=record.no_data,
    )
    return _roofs_record(
        buildings=record.buildings,
        classified=record.classified,
        kept_existing=record.kept_existing,
        below_quality=record.below_quality,
        no_data=record.no_data,
        relations_skipped=record.relations_skipped,
        shapes=dict(record.shapes),
    )
```

`_canopy_step` mirrors it: same file gate (events `canopy_skipped`),
loads windows and grid the same way, parses the `.osm` once with
`ET.fromstring`, takes `buildings._existing_building_footprints(root_element)`
and projects each footprint's lat/lon ring through `to_bng` (a
`BngError` ring is dropped from the mask, nothing else changes), calls
`build_canopy`, writes `<stem>_canopy.geojson` atomically when features
exist, emits `canopy_written` with `points=` and `skipped_small=`, and
returns `_canopy_record(points=..., skipped_small=..., resolution_m=...)`.
Catches `(CanopyError, CogError, BngError, HeightsError, OSError,
ET.ParseError)` into `canopy_failed` + `error`.

Wire both into `run_survey` after the `lidar_heights` line
(`roof_forms = _fit_roofs_step(...)`, `canopy = _canopy_step(...)`,
result keys `"roof_forms"`, `"canopy"` beside the existing record
assignments) and into `bridge_package` after its
`payload["lidar_heights"]` line, with a comment in each place naming
the ordering reason (after heights fusion so the rewrite preserves the
height tags already written; before the boundary injection because
neither touches the other's elements).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_package.py -k "roof or canopy" -v`

- [ ] **Step 5: Full suites, commit**

Run: `python -m pytest tests/ -m "not live" -q` AND
`node tests/js/test_app.js` (no JS changed; the run proves it).

```bash
git add src/mapgen/package.py tests/test_package.py
git commit -m "feat(package): roof forms and canopy steps with the 1 m gate"
```

---

### Task 8: Documentation and the real-package proof

**Files:**
- Modify: `README.md` (package contents rows, survey.json field table,
  honesty copy)
- Modify: `docs/urbano/README.md` (GH workflows)
- Modify: `docs/superpowers/HANDOFF.md` (item B shipped)

**Interfaces:**
- Consumes: everything shipped in Tasks 1 to 7 plus Task 4's validation
  report numbers.

Content, pinned:

- `README.md`: two package-contents rows (`<stem>_roof_massing.geojson`,
  `<stem>_canopy.geojson`, each one line saying what it holds and that
  it only exists when the LiDAR supported it); survey.json field-table
  rows for `roof_forms` and `canopy` with their key lists; a short roofs
  paragraph carrying the spec's own honesty lines: fitting runs only on
  1 m rasters (coarser extents record why they were skipped), a fitted
  plane is sharper than the pixels it came from but a conservatory will
  not be resolved, buildings below the quality floor get NO roof tags,
  and the canopy file is `vegetation and other above-ground features`,
  not a species survey. State the tag vocabulary that actually SHIPPED
  (Task 4 may have dropped a class; the doc lists the survivors, never
  the plan's original five as a promise).
- `docs/urbano/README.md`: a "Roof massing in Grasshopper" workflow
  (import `_roof_massing.geojson`, extrude each eaves polygon from the
  ground, loft to its building's ridge line, `quality` and `shape`
  filterable as keys) and a "Canopy points" workflow (import
  `_canopy.geojson`, place circles of `crown_radius` at each point,
  `height` drives extrusion or tree assets). Plus the bridge note: OLD
  Welsh packages GAIN roof tags, massing and canopy from a plain
  `mapgen bridge`, because every input is already on disk; this is the
  opposite of the categorised-boundaries rule and the sentence says so
  explicitly to head off the confusion.
- `docs/superpowers/HANDOFF.md`: item B shipped, one paragraph, with
  the validation report's headline numbers and a pointer at the spec
  for the gate and vocabulary rules.

- [ ] **Step 1: Write the doc changes**
- [ ] **Step 2: Re-read each changed section against the shipped code
  (tag names, file names, record keys, the gate string) and against
  Task 4's final vocabulary**
- [ ] **Step 3: Run both suites**

Run: `python -m pytest tests/ -m "not live" -q` and
`node tests/js/test_app.js`

- [ ] **Step 4: Commit**

```bash
git add README.md docs/urbano/README.md docs/superpowers/HANDOFF.md
git commit -m "docs(roofs): roof forms, massing and canopy documented"
```

---

## Self-Review

- **Spec coverage:** roofs (sample DSM inside footprints, subtract DTM
  median, RANSAC-style seeded fits, five classes, ridge direction, eave
  and ridge heights, tags verbatim, optional massing file, below-threshold
  buildings counted with no tags) -> Tasks 1, 2, 3, 5. Resolution gate with
  UI-visible reason -> Task 7. Spike validation with trimmed residuals,
  quality score, real-buildings check, drop-the-class rule, no-tags failure
  mode -> Tasks 1 (trim), 2 (quality), 4 (validation). Canopy (nDSM outside
  footprints, >= 3 m, connected components, minimum area, position, canopy
  height, crown radius, honesty copy) -> Task 6. Ordering after buildings
  and heights fusion in both pipelines, Wales-covered only (file gate) ->
  Task 7. Docs -> Task 8. No gaps found.
- **Placeholder scan:** clean; every step carries its code or its exact
  content list. Task 3's `massing_path` seam is declared, not a TODO: it is
  accepted-and-ignored with a comment naming Task 5, and Task 5's tests
  fail against exactly that.
- **Type consistency:** `RoofForm.direction_deg` is `float | None` at
  every consumer; `RoofsRecord.shapes` is `dict` in the dataclass and
  `dict(record.shapes)` at the record boundary; `build_canopy` returns
  `(list[dict], CanopyRecord)` everywhere it is named; `_ridge_segment`
  works in the centred frame and Task 5's prose says the caller adds the
  centre back. Checked.
