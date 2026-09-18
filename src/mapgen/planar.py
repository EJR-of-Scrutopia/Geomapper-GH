"""Planar geometry against axis-aligned rectangles.

Pure functions over (x, y) points, with no projection and no network.
The planning constraints work in the drawn site's own lon/lat frame (see
the spec, docs/superpowers/specs/2026-09-18-mapgen-planning-constraints-
wales-design.md, "Architecture"): the site is a lon/lat rectangle, so
clipping and intersection against it are exact there, and a SHARE of it
needs no projection either, only grid cells laid over it in proportion
to its true width.

A ring is a sequence of positions, open or closed; a position may carry
a third ordinate, which is ignored. A polygon is a sequence of rings,
the outer boundary first and any holes after. A rect is (x_min, y_min,
x_max, y_max).
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

Point = tuple[float, float]
Rect = tuple[float, float, float, float]


def open_ring(ring: Sequence[Sequence[float]]) -> list[Point]:
    points = [(float(position[0]), float(position[1])) for position in ring]
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    return points


def ring_area(ring: Sequence[Sequence[float]]) -> float:
    points = open_ring(ring)
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _clip_pass(
    points: list[Point], inside: Callable[[Point], bool], cross: Callable[[Point, Point], Point]
) -> list[Point]:
    if not points:
        return []
    kept: list[Point] = []
    previous = points[-1]
    for current in points:
        if inside(current):
            if not inside(previous):
                kept.append(cross(previous, current))
            kept.append(current)
        elif inside(previous):
            kept.append(cross(previous, current))
        previous = current
    return kept


def _cross_x(x: float) -> Callable[[Point, Point], Point]:
    # Only ever called for an edge with one end on each side of x, so the
    # two x values differ and the division is safe.
    def cross(a: Point, b: Point) -> Point:
        t = (x - a[0]) / (b[0] - a[0])
        return (x, a[1] + t * (b[1] - a[1]))

    return cross


def _cross_y(y: float) -> Callable[[Point, Point], Point]:
    def cross(a: Point, b: Point) -> Point:
        t = (y - a[1]) / (b[1] - a[1])
        return (a[0] + t * (b[0] - a[0]), y)

    return cross


def clip_ring(ring: Sequence[Sequence[float]], rect: Rect) -> list[Point]:
    """Sutherland-Hodgman against rect, one pass per side: an open ring,
    or [] when fewer than three points survive. A concave ring clipped
    this way can keep zero-width seams along rect's edges; they add no
    area and draw as a line on the boundary."""
    x_min, y_min, x_max, y_max = rect
    points = open_ring(ring)
    points = _clip_pass(points, lambda p: p[0] >= x_min, _cross_x(x_min))
    points = _clip_pass(points, lambda p: p[0] <= x_max, _cross_x(x_max))
    points = _clip_pass(points, lambda p: p[1] >= y_min, _cross_y(y_min))
    points = _clip_pass(points, lambda p: p[1] <= y_max, _cross_y(y_max))
    return points if len(points) >= 3 else []


def clip_polygon(rings: Sequence[Sequence[Sequence[float]]], rect: Rect) -> list[list[Point]]:
    """The polygon clipped to rect: [] when its outer ring leaves nothing,
    and any hole that leaves nothing is dropped."""
    if not rings:
        return []
    outer = clip_ring(rings[0], rect)
    if not outer:
        return []
    holes = [hole for hole in (clip_ring(ring, rect) for ring in rings[1:]) if hole]
    return [outer] + holes


def polygon_area_in_rect(rings: Sequence[Sequence[Sequence[float]]], rect: Rect) -> float:
    """The polygon's area inside rect, holes subtracted. Zero means the
    polygon's shape does not reach rect, whatever its envelope does."""
    clipped = clip_polygon(rings, rect)
    if not clipped:
        return 0.0
    return max(ring_area(clipped[0]) - sum(ring_area(hole) for hole in clipped[1:]), 0.0)


def point_in_rect(point: Sequence[float], rect: Rect) -> bool:
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def grid_shape(rect: Rect, cells_long_side: int = 400, x_scale: float = 1.0) -> tuple[int, int]:
    """(columns, rows) for a grid over rect with cells_long_side cells
    along its longer TRUE side. x_scale converts one x unit to the y
    unit's length: cos(latitude) for a lon/lat rect."""
    width = (rect[2] - rect[0]) * x_scale
    height = rect[3] - rect[1]
    if width <= 0 or height <= 0:
        return (1, 1)
    if width >= height:
        return cells_long_side, max(1, round(cells_long_side * height / width))
    return max(1, round(cells_long_side * width / height)), cells_long_side


def share_of_rect(
    polygons: Sequence[Sequence[Sequence[Sequence[float]]]],
    rect: Rect,
    cells_long_side: int = 400,
    x_scale: float = 1.0,
) -> float:
    """The fraction of rect's grid cells whose centre lies inside any of
    polygons. Each polygon is filled even-odd across its rings, so a
    hole is a hole; overlapping polygons count once, which summing their
    clipped areas would not."""
    columns, rows = grid_shape(rect, cells_long_side, x_scale)
    x0, y0, x1, y1 = rect
    dx = (x1 - x0) / columns
    dy = (y1 - y0) / rows
    covered = bytearray(columns * rows)
    for rings in polygons:
        clipped = clip_polygon(rings, rect)
        if not clipped:
            continue
        edges = []
        for ring in clipped:
            for index, a in enumerate(ring):
                b = ring[(index + 1) % len(ring)]
                if a[1] != b[1]:
                    edges.append((a, b))
        ys = [point[1] for ring in clipped for point in ring]
        first_row = max(0, math.floor((min(ys) - y0) / dy - 0.5))
        last_row = min(rows - 1, math.ceil((max(ys) - y0) / dy - 0.5))
        for row in range(first_row, last_row + 1):
            yc = y0 + (row + 0.5) * dy
            xs = sorted(
                a[0] + (yc - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                for a, b in edges
                if (a[1] <= yc) != (b[1] <= yc)
            )
            for left, right in zip(xs[0::2], xs[1::2]):
                first_col = max(0, math.ceil((left - x0) / dx - 0.5))
                last_col = min(columns - 1, math.floor((right - x0) / dx - 0.5))
                if last_col >= first_col:
                    start = row * columns
                    covered[start + first_col:start + last_col + 1] = b"\x01" * (last_col - first_col + 1)
    return sum(covered) / len(covered)
