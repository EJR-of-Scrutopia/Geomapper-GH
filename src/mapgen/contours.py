"""Marching squares over a BngWindow, into the GeoJSON contour files the
owner opens in Grasshopper.

Contours are generated here, not downloaded: `BngWindow` (cog.py) is a grid
of 1 m heights, and this module is the whole mechanism that turns it into
lines at an elevation. There is no shortcut through a WCS or a
pre-rendered contour service for a survey extent nobody has asked for
before, so the grid and this file are the only path.

## Why marching squares runs on pixel CENTRES, not pixel corners

A `BngWindow` samples the terrain at pixel centres (see cog.py's own
docstring), and treating that centre grid as the marching squares lattice
means every "cell" in the algorithm below is bounded by four real samples,
never an extrapolated corner. The trade is half a pixel of inset at the
window's own edge, which is nothing beside 0.25 m contour spacing.

## Why the per-cell min/max matters

A naive marching squares loops over every contour level for every cell,
which is fine at whole-metre spacing and not at 0.25 m: a 1000 x 1000
window has just under a million cells, and checking, say, sixty levels of
0.25 m spacing against each one is sixty million comparisons before a
single segment is built. Instead every cell computes its four corners'
min and max once, and only the levels strictly between them (see "the
nudge" below) are visited at all. A cell on a gentle slope crosses one or
two levels; the whole pass stays proportional to the number of levels the
TERRAIN actually crosses, not the product of levels and cells.

## The nudge: a corner exactly on a level

A corner's state (above a level or not) is always `value > level`, never
`>=`. This is the one place a level exactly equal to a corner's height is
decided, and deciding it consistently, the same way everywhere that corner
is read, is what stops a degenerate zero-length segment or a double-drawn
edge from appearing at that corner: two cells sharing a corner will always
agree on which side of the level it falls, because both ask the same
question of the same stored float.

## The saddle

Four crossings on one cell (both diagonals differ from each other, i.e.
opposite corners agree and adjacent corners do not) is the one
configuration marching squares cannot resolve from the four corners alone:
either pair of adjacent crossings is a valid, non-self-crossing choice, and
they describe two different contours. This module breaks the tie the
standard way, off the cell's own centre average: whichever diagonal's
state the average agrees with is read as the connected one.

## Joining, closing, simplifying

Cells contribute independent short segments, not polylines; segments are
stitched into polylines by matching endpoints rounded to one micron, which
is far tighter than any real distance between two independently
interpolated points meant to be the same crossing. A polyline whose two
ends match after rounding is a closed ring, and its first point is copied
onto its last so a consumer sees a literally closed loop, not two points a
micron apart. Collinearity is trimmed once, per triple of (last kept
point, candidate, next raw point) at 0.05 m perpendicular tolerance, which
is what collapses a perfectly straight run (a planar slope's contour, most
of a gentle one) down to its two ends without touching a genuine bend.

## Decimating for the 5 m interval

5 m contours exist to give the drawing context beyond the site itself, and
running them off the full 1 m grid is both needless work and a busier line
than the interval implies. The grid is decimated first (this module
resolves the stride from the window's own pixel_size, see
`_grid_view`) so the 5 m pass samples roughly every 4 m, matching the level
of generalisation a 5 m contour is supposed to show.

## Interval policy and property keys

`contour_intervals_for` is `area_m2 = width * pixel_size * height *
pixel_height`, thresholded exactly as the task brief states it: 5 m
always, 1 m under 6 sq km, 0.5 m and 0.25 m under 1.5 sq km. GeoJSON
property keys are exactly `elevation`, `interval_m`, `source`,
`source_resolution_m`, `interpolated`; `source_resolution_m` records the
window's `pixel_size` (the column spacing) even where `pixel_height`
differs, because on every real window that reaches this module the two
agree to within a millimetre (see cog.py's own docstring on why they can
differ at all) and one resolution figure is what the property is for.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from mapgen.bng import Ostn15Grid, from_bng
from mapgen.cog import BngWindow
from mapgen.fsutil import atomic_write_bytes

SOURCE_LABEL = "Welsh Government LiDAR 2020 to 2023"

# Square metres, from the task brief verbatim: "at most 6.0 square
# kilometres" and "at most 1.5 square kilometres".
_ONE_METRE_AREA_CEILING_M2 = 6.0 * 1_000_000.0
_FINE_AREA_CEILING_M2 = 1.5 * 1_000_000.0

# The 5 m pass targets roughly 4 m data, not the full 1 m grid; see the
# module docstring's "Decimating" section.
_FIVE_METRE_TARGET_SPACING_M = 4.0

# Endpoints within a micron of each other are the same crossing point,
# never two contours that happen to pass close together; a real gap
# between two genuinely different lines is always orders of magnitude
# larger than floating point noise from two independent interpolations.
_JOIN_ROUNDING_DECIMALS = 6

# Perpendicular distance, in metres, within which a vertex is read as
# collinear with its neighbours and dropped. The task brief's own figure.
_COLLINEAR_TOLERANCE_M = 0.05

Point = tuple[float, float]
Segment = tuple[Point, Point]
Polyline = list[Point]


def contour_intervals_for(window: BngWindow) -> list[float]:
    """Which contour intervals this window's extent earns.

    5 m always; 1 m added under 6 sq km; 0.5 m and 0.25 m added under
    1.5 sq km. A survey extent large enough to only want an overview gets
    only the interval cheap enough to draw one; a site-scale extent gets
    everything down to a quarter metre, because that is the resolution the
    owner reads plan detail off.
    """
    area_m2 = window.width * window.pixel_size * window.height * window.pixel_height
    intervals = [5.0]
    if area_m2 <= _ONE_METRE_AREA_CEILING_M2:
        intervals.append(1.0)
    if area_m2 <= _FINE_AREA_CEILING_M2:
        intervals.extend([0.5, 0.25])
    return intervals


def generate_contours(window: BngWindow, interval: float) -> list[Polyline]:
    """Every polyline this window contours at this interval, in BNG metres.

    Flattened across every level the interval crosses, ascending, because
    the level itself is not part of this function's return shape (see
    write_contour_files for the properties that carry it). Built from the
    same one-pass, per-level grouping write_contour_files uses, so this
    function's own cost is not a second, separate walk of the grid.
    """
    by_level = _contours_by_level(window, interval)
    polylines: list[Polyline] = []
    for level in sorted(by_level):
        polylines.extend(by_level[level])
    return polylines


def write_contour_files(
    window: BngWindow, out_dir: Path, stem: str, grid: Ostn15Grid
) -> list[Path]:
    """One GeoJSON FeatureCollection per interval this window's area grants.

    Only the granted files are written; a coarse, county-scale window never
    gets a 0.25 m file it was never going to have room to draw legibly.
    Every vertex is projected through from_bng once, here, so nothing
    downstream of this module ever has to know a survey coordinate started
    life in British National Grid metres.
    """
    written: list[Path] = []
    out_dir = Path(out_dir)
    for interval in contour_intervals_for(window):
        by_level = _contours_by_level(window, interval)
        features = []
        for level in sorted(by_level):
            for polyline in by_level[level]:
                coordinates = []
                for easting, northing in polyline:
                    latitude, longitude = from_bng(easting, northing, grid)
                    coordinates.append([longitude, latitude])
                features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "elevation": float(level),
                            "interval_m": float(interval),
                            "source": SOURCE_LABEL,
                            "source_resolution_m": float(window.pixel_size),
                            "interpolated": interval < 1.0,
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": coordinates,
                        },
                    }
                )
        payload = {"type": "FeatureCollection", "features": features}
        path = out_dir / f"{stem}_contours_{_interval_suffix(interval)}.geojson"
        atomic_write_bytes(
            path, json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        written.append(path)
    return written


def _interval_suffix(interval: float) -> str:
    if interval == int(interval):
        return f"{int(interval)}m"
    return f"{interval:g}m"


# --------------------------------------------------------------------------
# The grid view: pixel centres, optionally decimated.
# --------------------------------------------------------------------------


def _grid_view(window: BngWindow, interval: float) -> tuple[int, int, int]:
    """(stride, ncols, nrows): how this interval reads the window's grid.

    stride is 1 for every interval except exactly 5.0, which decimates to
    roughly 4 m spacing (see the module docstring). ncols/nrows are the
    number of pixel CENTRES the decimated grid has along each axis; a
    window narrower than 2 of them, in either direction, has no cell to
    contour at all.
    """
    stride = 1
    if interval == 5.0:
        stride = max(1, math.floor(_FIVE_METRE_TARGET_SPACING_M / window.pixel_size))
    ncols = (window.width + stride - 1) // stride
    nrows = (window.height + stride - 1) // stride
    return stride, ncols, nrows


def _contours_by_level(
    window: BngWindow, interval: float
) -> dict[float, list[Polyline]]:
    """Every finished polyline this window contours at this interval, keyed
    by its elevation level.

    One pass over the (possibly decimated) grid of cells, collecting raw
    segments per level, then one join-and-simplify pass per level. Never a
    separate grid pass per level: see the module docstring's "per-cell
    min/max" section for why that is the part that keeps 0.25 m spacing
    affordable.
    """
    stride, ncols, nrows = _grid_view(window, interval)
    if ncols < 2 or nrows < 2:
        return {}

    values = window.values
    src_width = window.width
    pixel_size = window.pixel_size
    pixel_height = window.pixel_height
    e_origin = window.e_origin
    n_top = window.n_top

    cols = [c * stride for c in range(ncols)]
    eastings = [e_origin + (c + 0.5) * pixel_size for c in cols]

    raw_segments: dict[float, list[Segment]] = defaultdict(list)

    prev_row_values: list[float] | None = None
    prev_northing = 0.0
    for rj in range(nrows):
        row = rj * stride
        northing = n_top - (row + 0.5) * pixel_height
        row_values = [values[row * src_width + c] for c in cols]
        if prev_row_values is not None:
            _scan_row_pair(
                prev_row_values, row_values, prev_northing, northing,
                eastings, interval, raw_segments,
            )
        prev_row_values = row_values
        prev_northing = northing

    return {
        level: _finish_polylines(segments)
        for level, segments in raw_segments.items()
    }


def _scan_row_pair(
    top_values: list[float],
    bottom_values: list[float],
    top_n: float,
    bottom_n: float,
    eastings: list[float],
    interval: float,
    raw_segments: dict[float, list[Segment]],
) -> None:
    """Every cell between two adjacent grid rows, corners' min/max first."""
    ncols = len(eastings)
    for i in range(ncols - 1):
        tl, tr = top_values[i], top_values[i + 1]
        bl, br = bottom_values[i], bottom_values[i + 1]
        if tl != tl or tr != tr or bl != bl or br != br:  # any NaN corner
            continue

        vmin = min(tl, tr, bl, br)
        vmax = max(tl, tr, bl, br)
        if vmin == vmax:
            continue

        pt_tl = (eastings[i], top_n)
        pt_tr = (eastings[i + 1], top_n)
        pt_bl = (eastings[i], bottom_n)
        pt_br = (eastings[i + 1], bottom_n)

        lo = math.floor(vmin / interval)
        hi = math.ceil(vmax / interval)
        for k in range(lo, hi + 1):
            level = k * interval
            if not (vmin <= level < vmax):
                continue
            for segment in _cell_segments(
                tl, tr, br, bl, pt_tl, pt_tr, pt_br, pt_bl, level
            ):
                raw_segments[level].append(segment)


def _cell_segments(
    tl: float, tr: float, br: float, bl: float,
    pt_tl: Point, pt_tr: Point, pt_br: Point, pt_bl: Point,
    level: float,
) -> list[Segment]:
    """0, 1 or 2 segments crossing this cell at this level.

    Edges are named for the cell's own sides (N: TL-TR, E: TR-BR, S:
    BR-BL, W: BL-TL), which is also their cyclic order around the cell.
    Walking that cycle, the number of state changes (see the nudge, in the
    module docstring) is always even: 0 (the level misses the cell,
    already excluded by the caller's min/max check), 2 (the ordinary
    case, wherever the two crossing edges fall) or 4 (both diagonals
    differ from each other, the saddle).
    """
    edges: dict[str, Point] = {}
    if (tl > level) != (tr > level):
        edges["N"] = _interp(pt_tl, pt_tr, tl, tr, level)
    if (tr > level) != (br > level):
        edges["E"] = _interp(pt_tr, pt_br, tr, br, level)
    if (br > level) != (bl > level):
        edges["S"] = _interp(pt_br, pt_bl, br, bl, level)
    if (bl > level) != (tl > level):
        edges["W"] = _interp(pt_bl, pt_tl, bl, tl, level)

    if not edges:
        return []
    if len(edges) == 4:
        # The saddle: TL and BR share one state, TR and BL the other (see
        # the module docstring's "the saddle" section for why 4 crossings
        # implies exactly this pattern). The cell-centre average decides
        # which diagonal's state the middle of the cell agrees with, and
        # that diagonal's two corners are read as connected through it.
        average = (tl + tr + br + bl) / 4.0
        if (average > level) == (tl > level):
            pairs = (("N", "E"), ("S", "W"))
        else:
            pairs = (("N", "W"), ("E", "S"))
        return [(edges[a], edges[b]) for a, b in pairs]

    names = list(edges)
    return [(edges[names[0]], edges[names[1]])]


def _interp(p1: Point, p2: Point, v1: float, v2: float, level: float) -> Point:
    t = (level - v1) / (v2 - v1)
    return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))


# --------------------------------------------------------------------------
# Joining segments into polylines, closing rings, dropping collinear
# vertices.
# --------------------------------------------------------------------------


def _round_point(point: Point) -> tuple[float, float]:
    return (round(point[0], _JOIN_ROUNDING_DECIMALS), round(point[1], _JOIN_ROUNDING_DECIMALS))


def _join_segments(segments: list[Segment]) -> list[Polyline]:
    """Segments stitched into polylines by their rounded endpoints.

    Every rounded point most contour data ever produces is touched by
    exactly one segment end (an open end, at a NaN boundary or the window
    edge) or exactly two (a plain interior join); this walks outward from
    an arbitrary unused segment in both directions until neither end has
    an unused neighbour left, which is what a chain looks like whichever
    of those two counts it meets.
    """
    endpoint_index: dict[tuple[float, float], list[int]] = defaultdict(list)
    for index, (p1, p2) in enumerate(segments):
        endpoint_index[_round_point(p1)].append(index)
        endpoint_index[_round_point(p2)].append(index)

    used = [False] * len(segments)

    def _unused_at(rounded_point: tuple[float, float]) -> int | None:
        for candidate in endpoint_index.get(rounded_point, ()):
            if not used[candidate]:
                return candidate
        return None

    polylines: list[Polyline] = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        chain: Polyline = list(segments[start])

        while True:
            candidate = _unused_at(_round_point(chain[-1]))
            if candidate is None:
                break
            used[candidate] = True
            p1, p2 = segments[candidate]
            chain.append(p2 if _round_point(p1) == _round_point(chain[-1]) else p1)

        while True:
            candidate = _unused_at(_round_point(chain[0]))
            if candidate is None:
                break
            used[candidate] = True
            p1, p2 = segments[candidate]
            chain.insert(0, p2 if _round_point(p1) == _round_point(chain[0]) else p1)

        if len(chain) > 2 and _round_point(chain[0]) == _round_point(chain[-1]):
            # A closed ring: the last point is a micron from the first by
            # construction, not identical to it, so it is replaced outright
            # rather than left merely close.
            chain[-1] = chain[0]

        polylines.append(chain)

    return polylines


def _perpendicular_distance(point: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length == 0.0:
        return math.hypot(px - ax, py - ay)
    return abs(dx * (ay - py) - (ax - px) * dy) / length


def _drop_collinear(points: Polyline) -> Polyline:
    """Vertices within 0.05 m of the line through their neighbours, gone.

    Compared against the last KEPT point rather than the raw previous one,
    so a long straight run collapses to its two ends in one pass instead
    of losing only every other point: each candidate is judged against
    where the line actually is now, not where the unsimplified input
    happened to put its immediate predecessor.
    """
    if len(points) < 3:
        return list(points)
    kept = [points[0]]
    for i in range(1, len(points) - 1):
        if _perpendicular_distance(points[i], kept[-1], points[i + 1]) > _COLLINEAR_TOLERANCE_M:
            kept.append(points[i])
    kept.append(points[-1])
    return kept


def _finish_polylines(segments: list[Segment]) -> list[Polyline]:
    return [_drop_collinear(chain) for chain in _join_segments(segments)]
