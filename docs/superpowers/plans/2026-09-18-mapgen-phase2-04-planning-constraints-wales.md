# Planning constraints for Wales Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new `constraints_wales` source that checks twenty Welsh planning designation layers over the drawn site plus a 250 m margin and writes five GeoJSON layers and a plain-English constraints summary into every Welsh survey package.

**Architecture:** Pure logic (planar geometry, coverage, feature assessment, summary) lives in three network-free modules; one source module does the WFS requests and writes work files, and its merge composes the package outputs. Coverage comes from a committed, simplified GeoJSON of the 22 Welsh councils plus the Welsh inshore waters. package.py gains one registration line and one survey.json block.

**Tech Stack:** Python 3.13 standard library plus the project's existing `requests`; node for the one JS test. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-18-mapgen-planning-constraints-wales-design.md` (read it before starting any task; it holds the verbatim licence texts and the reasoning behind every rule below).

## Global Constraints

- No new third-party dependency and no build step. `requests` is already a dependency.
- No em dashes anywhere: code, comments, docstrings, docs, commit messages.
- Never add a `Co-Authored-By` trailer or any AI attribution to a commit. The owner owns the work.
- Commit after every task with explicit paths (`git add <path> ...`), never `git add -A` or `git add .`.
- No URL and no key in any exception message, `TileFailure.reason`, progress event or summary line. Failure sentences are composed from `sources.base.classify_transport_failure` / `classify_status_failure` phrases, never from `str(exc)`.
- Nothing fabricated: a layer that failed or was not checked is never reported under "Checked, none found".
- `covers()`, `tier()`, `detail()` and `estimate()` never touch the network. Live-network tests carry `@pytest.mark.live`.
- Run Python tests as `PYTHONPATH=src /c/Python313/python.exe -m pytest <paths> -q -p no:cacheprovider` from `C:\Users\Param\mapgen-phase1` (the miniforge base Python on this machine is broken). JS tests: `node tests/js/test_app.js`.
- Keep the machine light: run the targeted test files while iterating, and the full suite (about 2 minutes) once, at the end of the task. One agent at a time in this worktree.
- Never touch anything under `C:\Users\Param\Surveys`.
- Exact values from the spec: margin 250 m; coverage "full" at a share of 0.98 and above; share text "under 1% of the site" below 0.5%, "the whole site" at 99.5% and above, otherwise a whole percent rounded half up; feature ceiling 20,000 per layer; share grid 400 cells along the site's longer side.

## File structure

| File | Responsibility |
|---|---|
| `src/mapgen/planar.py` (create) | Pure planar geometry against axis-aligned rectangles: clipping, areas, point tests, the scanline share grid |
| `tools/build_wales_coverage.py` (create) | One-off generator for the committed coverage file, from DataMapWales |
| `src/mapgen/data/wales_coverage.geojson` (create, generated) | 22 Welsh councils plus the Welsh inshore waters, simplified to 20 m |
| `src/mapgen/wales_coverage.py` (create) | Loads the coverage file; coverage share, verdict, councils touching a rectangle |
| `src/mapgen/constraints.py` (create) | The layer table, feature assessment against site and margin, output collections, summary text, survey.json record |
| `src/mapgen/sources/constraints_wales.py` (create) | The LayerSource: WFS requests, paging, retry, work files, merge into package outputs |
| `src/mapgen/package.py` (modify) | Register the source; add survey.json's `constraints` block |
| `src/mapgen/resolver.py` (modify) | New category `constraints` |
| `src/mapgen/web/static/app.js` (modify) | Tier-list label for the new category |
| `pyproject.toml` (modify) | Ship `src/mapgen/data/*` as package data |
| `README.md`, `docs/urbano/README.md` (modify) | What the files are and how to filter them |
| `tests/test_planar.py`, `tests/test_wales_coverage.py`, `tests/test_constraints.py`, `tests/test_sources_constraints_wales.py` (create) | Tests |
| `tests/fixtures/constraints/*.json` (create, recorded) | Two real DataMapWales responses over Cowbridge, trimmed |

---

### Task 1: Planar geometry

**Files:**
- Create: `src/mapgen/planar.py`
- Test: `tests/test_planar.py`

**Interfaces:**
- Produces: `Point = tuple[float, float]`, `Rect = tuple[float, float, float, float]` (x_min, y_min, x_max, y_max); `open_ring(ring) -> list[Point]`; `ring_area(ring) -> float`; `clip_ring(ring, rect) -> list[Point]`; `clip_polygon(rings, rect) -> list[list[Point]]`; `polygon_area_in_rect(rings, rect) -> float`; `point_in_rect(point, rect) -> bool`; `grid_shape(rect, cells_long_side=400, x_scale=1.0) -> tuple[int, int]`; `share_of_rect(polygons, rect, cells_long_side=400, x_scale=1.0) -> float`. A ring is a sequence of positions (a position may carry a third ordinate, which is ignored), open or closed. A polygon is a sequence of rings, outer first, holes after.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_planar.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_planar.py -q -p no:cacheprovider`
Expected: collection error, `ModuleNotFoundError: No module named 'mapgen.planar'`.

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/planar.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_planar.py -q -p no:cacheprovider`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/planar.py tests/test_planar.py
git commit -m "feat(planar): rectangle clipping, areas and a scanline share grid for constraints"
```

---

### Task 2: Welsh coverage data and module

**Files:**
- Create: `tools/build_wales_coverage.py`
- Create (generated by the tool): `src/mapgen/data/wales_coverage.geojson`
- Create: `src/mapgen/wales_coverage.py`
- Modify: `pyproject.toml` (package data)
- Test: `tests/test_wales_coverage.py`

**Interfaces:**
- Consumes: `mapgen.planar.polygon_area_in_rect`, `Rect` (Task 1); `mapgen.bng.tm_forward(latitude, longitude) -> (easting, northing)`; `mapgen.cog.USER_AGENT`.
- Produces: `COVERAGE_PATH: Path`; `FULL_SHARE = 0.98`; `load_areas(path=COVERAGE_PATH) -> tuple[Area, ...]` where `Area(kind: str, name: str, polygons: tuple, bbox: Rect)` and `kind` is `"council"` or `"inshore"`; `coverage_share(rect, path=COVERAGE_PATH) -> float` (capped at 1.0); `verdict(share) -> str` (`"full"`, `"partial"` or `"none"`); `coverage(rect, path=COVERAGE_PATH) -> str`; `councils_touching(rect, path=COVERAGE_PATH) -> list[str]` (sorted names). Every `rect` is a lon/lat `(west, south, east, north)`.

- [ ] **Step 1: Write the generator**

Create `tools/build_wales_coverage.py`:

```python
"""Regenerate src/mapgen/data/wales_coverage.geojson from DataMapWales.

Run from the repository root, with network access:

    PYTHONPATH=src /c/Python313/python.exe tools/build_wales_coverage.py

Two layers, both published under the Open Government Licence v3 on their
DataMapWales pages: inspire-wg:localauthorities (the 22 Welsh unitary
authorities to the high-water mark) and geonode:wnmp_areas (the Welsh
National Marine Plan areas, of which only "Inshore Region" is kept:
Welsh waters out to 12 nautical miles). Every ring is simplified by
Douglas-Peucker at TOLERANCE_METRES, measured in bng.tm_forward metres,
and rounded to six decimal places. See the spec's "Coverage" for why the
inshore waters are included and why the coverage threshold is 98%.
"""

from __future__ import annotations

import datetime
import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from mapgen.bng import tm_forward  # noqa: E402
from mapgen.cog import USER_AGENT  # noqa: E402

WFS_URL = "https://datamap.gov.wales/geoserver/wfs"
TOLERANCE_METRES = 20.0
OUT_PATH = REPO / "src" / "mapgen" / "data" / "wales_coverage.geojson"


def fetch(type_name: str) -> dict:
    query = urllib.parse.urlencode(
        {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": type_name,
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
        }
    )
    request = urllib.request.Request(f"{WFS_URL}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def simplify(ring: list, tolerance: float) -> list:
    """Douglas-Peucker over one closed lon/lat ring, measured in metres.

    Iterative rather than recursive, so a coastline of 100,000 points
    cannot exhaust the recursion limit. The ring is split at the point
    farthest from its first point, so the two halves are open chains.
    Returns a closed ring, or [] when fewer than three points survive.
    """
    points = [(position[0], position[1]) for position in ring]
    if points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 4:
        return []
    metres = [tm_forward(lat, lon) for lon, lat in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    far = max(
        range(len(points)),
        key=lambda i: (metres[i][0] - metres[0][0]) ** 2 + (metres[i][1] - metres[0][1]) ** 2,
    )
    keep[far] = True
    stack = [(0, far), (far, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        (ax, ay), (bx, by) = metres[first], metres[last]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        worst, worst_distance = None, tolerance
        for index in range(first + 1, last):
            px, py = metres[index]
            if length == 0:
                distance = math.hypot(px - ax, py - ay)
            else:
                distance = abs(dy * (px - ax) - dx * (py - ay)) / length
            if distance > worst_distance:
                worst, worst_distance = index, distance
        if worst is not None:
            keep[worst] = True
            stack.append((first, worst))
            stack.append((worst, last))
    kept = [(round(points[i][0], 6), round(points[i][1], 6)) for i in range(len(points)) if keep[i]]
    if len(kept) < 3:
        return []
    return [list(point) for point in kept] + [list(kept[0])]


def simplified_multipolygon(geometry: dict) -> dict:
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    kept = []
    for rings in polygons:
        outer = simplify(rings[0], TOLERANCE_METRES)
        if not outer:
            continue
        holes = [hole for hole in (simplify(ring, TOLERANCE_METRES) for ring in rings[1:]) if hole]
        kept.append([outer] + holes)
    return {"type": "MultiPolygon", "coordinates": kept}


def main() -> None:
    councils = fetch("inspire-wg:localauthorities")
    marine = fetch("geonode:wnmp_areas")
    features = []
    for feature in sorted(councils["features"], key=lambda f: f["properties"]["name_en"]):
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": "council",
                    "name": feature["properties"]["name_en"],
                    "code": feature["properties"]["code"],
                },
                "geometry": simplified_multipolygon(feature["geometry"]),
            }
        )
    if len(features) != 22:
        raise SystemExit(f"expected 22 Welsh councils, found {len(features)}")
    inshore = [f for f in marine["features"] if f["properties"].get("name") == "Inshore Region"]
    if len(inshore) != 1:
        raise SystemExit(f"expected one Inshore Region, found {len(inshore)}")
    features.append(
        {
            "type": "Feature",
            "properties": {"kind": "inshore", "name": "Welsh inshore waters", "code": None},
            "geometry": simplified_multipolygon(inshore[0]["geometry"]),
        }
    )
    collection = {
        "type": "FeatureCollection",
        "source": "DataMapWales WFS: inspire-wg:localauthorities, and geonode:wnmp_areas (Inshore Region)",
        "licence": "Open Government Licence v3.0",
        "retrieved": datetime.date.today().isoformat(),
        "simplified_metres": TOLERANCE_METRES,
        "features": features,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(collection, separators=(",", ":")), encoding="utf-8")
    vertices = sum(
        len(ring) for f in features for polygon in f["geometry"]["coordinates"] for ring in polygon
    )
    print(f"{OUT_PATH}: {len(features)} features, {vertices} vertices, {OUT_PATH.stat().st_size} bytes")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate the data file**

Run: `PYTHONPATH=src /c/Python313/python.exe tools/build_wales_coverage.py`
Expected: one line reporting 23 features, roughly 54,000 vertices, and a size around 1.3 MB. If it reports anything other than 23 features, stop and report: the source layers have changed.

- [ ] **Step 3: Ship the data as package data**

In `pyproject.toml`, change:

```toml
[tool.setuptools.package-data]
mapgen = ["web/static/*", "web/static/**/*"]
```

to:

```toml
[tool.setuptools.package-data]
mapgen = ["web/static/*", "web/static/**/*", "data/*"]
```

- [ ] **Step 4: Write the failing tests**

Create `tests/test_wales_coverage.py`:

```python
import json

import pytest

from mapgen.wales_coverage import (
    COVERAGE_PATH,
    FULL_SHARE,
    councils_touching,
    coverage,
    coverage_share,
    load_areas,
    verdict,
)


def _square(west, south, east, north):
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


@pytest.fixture
def synthetic(tmp_path):
    """One council square west of lon 1, one inshore-waters square east of it."""
    path = tmp_path / "coverage.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"kind": "council", "name": "Testshire", "code": "T1"},
                        "geometry": {"type": "MultiPolygon", "coordinates": [_square(0, 50, 1, 51)]},
                    },
                    {
                        "type": "Feature",
                        "properties": {"kind": "inshore", "name": "Welsh inshore waters", "code": None},
                        "geometry": {"type": "MultiPolygon", "coordinates": [_square(1, 50, 2, 51)]},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_threshold_is_the_measured_98_percent():
    assert FULL_SHARE == 0.98


def test_verdict_bands():
    assert verdict(1.0) == "full"
    assert verdict(0.98) == "full"
    assert verdict(0.979) == "partial"
    assert verdict(0.0001) == "partial"
    assert verdict(0.0) == "none"


def test_a_rect_inside_a_council_is_full(synthetic):
    assert coverage((0.2, 50.2, 0.8, 50.8), synthetic) == "full"
    assert councils_touching((0.2, 50.2, 0.8, 50.8), synthetic) == ["Testshire"]


def test_a_coastal_rect_counts_the_inshore_waters(synthetic):
    rect = (0.8, 50.2, 1.2, 50.8)
    assert coverage_share(rect, synthetic) == pytest.approx(1.0)
    assert coverage(rect, synthetic) == "full"
    assert councils_touching(rect, synthetic) == ["Testshire"]


def test_a_rect_half_beyond_the_waters_is_partial(synthetic):
    assert coverage((1.8, 50.2, 2.2, 50.8), synthetic) == "partial"
    assert councils_touching((1.8, 50.2, 2.2, 50.8), synthetic) == []


def test_a_rect_wholly_elsewhere_is_none(synthetic):
    assert coverage((3.0, 50.0, 4.0, 51.0), synthetic) == "none"


def test_the_committed_file_holds_22_councils_and_the_inshore_waters():
    areas = load_areas(COVERAGE_PATH)
    assert sorted(area.kind for area in areas).count("council") == 22
    assert [area.name for area in areas if area.kind == "inshore"] == ["Welsh inshore waters"]


# Real extents, against the committed file. The spike that set the 98%
# threshold measured these (see the spec's "Coverage").

def test_cowbridge_is_full_and_in_the_vale():
    rect = (-3.4637, 51.4527, -3.4343, 51.4703)
    assert coverage(rect) == "full"
    assert councils_touching(rect) == ["Vale of Glamorgan"]


def test_a_coastal_extent_at_aberystwyth_is_full():
    assert coverage((-4.0950, 52.4080, -4.0750, 52.4200)) == "full"


def test_an_extent_across_the_english_border_at_chepstow_is_partial():
    assert coverage((-2.7000, 51.6300, -2.6400, 51.6500)) == "partial"


def test_bristol_is_none():
    assert coverage((-2.62, 51.44, -2.56, 51.47)) == "none"
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_wales_coverage.py -q -p no:cacheprovider`
Expected: collection error, `ModuleNotFoundError: No module named 'mapgen.wales_coverage'`.

- [ ] **Step 6: Write the implementation**

Create `src/mapgen/wales_coverage.py`:

```python
"""Where the Welsh planning constraints layers answer, from a committed
file and never the network.

src/mapgen/data/wales_coverage.geojson (written by
tools/build_wales_coverage.py) holds the 22 Welsh councils to the
high-water mark and the Welsh National Marine Plan's inshore region,
both simplified to about 20 m. Together they are Wales including its
waters out to 12 nautical miles, so "outside Wales" here means England,
or open sea beyond that limit.

The share is summed polygon area over the rectangle's area, capped at 1:
the councils' high-water line and the marine plan's landward edge are
separate lines, so a coastal extent sums to a little over or under 1
(99.0% at Aberystwyth, 100.2% at Penarth, measured), while an extent
across the English border sums to about a half. FULL_SHARE sits between
those, from that measurement; see the spec's "Coverage".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from mapgen.planar import Rect, polygon_area_in_rect

COVERAGE_PATH = Path(__file__).resolve().parent / "data" / "wales_coverage.geojson"
FULL_SHARE = 0.98


@dataclass(frozen=True)
class Area:
    kind: str
    name: str
    polygons: tuple
    bbox: Rect


def _bbox(polygons) -> Rect:
    xs = [position[0] for rings in polygons for position in rings[0]]
    ys = [position[1] for rings in polygons for position in rings[0]]
    return (min(xs), min(ys), max(xs), max(ys))


@lru_cache(maxsize=4)
def load_areas(path: Path = COVERAGE_PATH) -> tuple[Area, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    areas = []
    for feature in data["features"]:
        geometry = feature["geometry"]
        if geometry["type"] == "MultiPolygon":
            polygons = tuple(geometry["coordinates"])
        else:
            polygons = (geometry["coordinates"],)
        if not polygons:
            continue
        properties = feature["properties"]
        areas.append(Area(properties["kind"], properties["name"], polygons, _bbox(polygons)))
    return tuple(areas)


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def coverage_share(rect: Rect, path: Path = COVERAGE_PATH) -> float:
    rect_area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    if rect_area <= 0:
        return 0.0
    covered = 0.0
    for area in load_areas(Path(path)):
        if not _overlaps(area.bbox, rect):
            continue
        for rings in area.polygons:
            covered += polygon_area_in_rect(rings, rect)
    return min(covered / rect_area, 1.0)


def verdict(share: float) -> str:
    if share >= FULL_SHARE:
        return "full"
    if share > 0.0:
        return "partial"
    return "none"


def coverage(rect: Rect, path: Path = COVERAGE_PATH) -> str:
    return verdict(coverage_share(rect, path))


def councils_touching(rect: Rect, path: Path = COVERAGE_PATH) -> list[str]:
    names = set()
    for area in load_areas(Path(path)):
        if area.kind != "council" or not _overlaps(area.bbox, rect):
            continue
        if any(polygon_area_in_rect(rings, rect) > 0.0 for rings in area.polygons):
            names.add(area.name)
    return sorted(names)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_wales_coverage.py -q -p no:cacheprovider`
Expected: 11 passed. Then time one real call and report it:
`PYTHONPATH=src /c/Python313/python.exe -c "import time; from mapgen.wales_coverage import coverage, load_areas; load_areas(); t=time.time(); coverage((-4.0950, 52.4080, -4.0750, 52.4200)); print(round(time.time()-t, 3))"`
Report the seconds in your report (the picker calls this on every extent change; above 0.3 s is worth flagging).

- [ ] **Step 8: Commit**

```bash
git add tools/build_wales_coverage.py src/mapgen/data/wales_coverage.geojson src/mapgen/wales_coverage.py pyproject.toml tests/test_wales_coverage.py
git commit -m "feat(wales_coverage): Welsh councils plus inshore waters, committed, for constraints coverage"
```

---

### Task 3: The layer table and feature assessment

**Files:**
- Create: `src/mapgen/constraints.py`
- Test: `tests/test_constraints.py`

**Interfaces:**
- Consumes: `mapgen.planar` (Task 1): `Rect`, `clip_polygon`, `point_in_rect`, `polygon_area_in_rect`.
- Produces (later tasks rely on these exact names): `METRES_PER_DEGREE_LAT = 111_320.0`; `MARGIN_METRES = 250.0`; `SHARE_GRID_CELLS = 400`; `OUTPUT_GROUPS = ("flood", "nature", "landscape", "heritage", "listed_buildings")`; `Layer(type_name, designation, group, attribution, name_field, grade_field, reference_field)`; `LAYERS: tuple[Layer, ...]` (twenty, in the spec's order); `LAYER_BY_TYPE: dict[str, Layer]`; `LayerResult(layer, ok, features=(), kind=None, reason=None)`; `Found(layer, name, grade_or_zone, reference, where, geometry, site_polygons)`; `Assessment(site, padded, found, checked, failed, skipped_geometry)`; `padded_rect(site, margin_m=MARGIN_METRES) -> Rect`; `classify_feature(layer, feature, site, padded) -> Found | None`; `assess(site, results, margin_m=MARGIN_METRES) -> Assessment`; `group_collections(assessment) -> dict[str, dict]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_constraints.py`:

```python
import math

import pytest

from mapgen.constraints import (
    LAYER_BY_TYPE,
    LAYERS,
    MARGIN_METRES,
    OUTPUT_GROUPS,
    Layer,
    LayerResult,
    assess,
    classify_feature,
    group_collections,
    padded_rect,
)

SITE = (-3.46, 51.455, -3.438, 51.468)  # Cowbridge
CONSERVATION = LAYER_BY_TYPE["geonode:conservation_areas_wales"]
LISTED = LAYER_BY_TYPE["inspire-wg:Cadw_ListedBuildings"]
FLOOD_RIVERS = LAYER_BY_TYPE["inspire-nrw:NRW_FLOODZONE_RIVERS"]
SAC = LAYER_BY_TYPE["inspire-nrw:NRW_SAC"]


def _polygon(west, south, east, north):
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}


def _feature(geometry, **properties):
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def test_the_table_has_the_spec_s_twenty_layers_in_order():
    assert len(LAYERS) == 20
    assert LAYERS[0].type_name == "inspire-nrw:NRW_FLOODZONE_RIVERS"
    assert LAYERS[-1].type_name == "geonode:conservation_areas_wales"
    assert {layer.group for layer in LAYERS} == set(OUTPUT_GROUPS)
    assert {layer.attribution for layer in LAYERS} == {"A", "B", "C", "D"}
    assert LISTED.group == "listed_buildings" and LISTED.grade_field == "Grade"


def test_padded_rect_adds_250_m_each_side():
    west, south, east, north = padded_rect(SITE)
    assert MARGIN_METRES == 250.0
    assert south == pytest.approx(SITE[1] - 250 / 111_320)
    lat0 = math.radians((SITE[1] + SITE[3]) / 2)
    assert west == pytest.approx(SITE[0] - 250 / (111_320 * math.cos(lat0)))


def test_a_polygon_on_the_site_is_on_the_site():
    padded = padded_rect(SITE)
    found = classify_feature(CONSERVATION, _feature(_polygon(-3.455, 51.458, -3.445, 51.465), sitename=" Cowbridge "), SITE, padded)
    assert found.where == "site"
    assert found.name == "Cowbridge"
    assert found.site_polygons


def test_a_polygon_only_in_the_margin_is_in_the_margin():
    padded = padded_rect(SITE)
    # South of the site, inside the 250 m margin (0.00224 degrees of latitude).
    found = classify_feature(CONSERVATION, _feature(_polygon(-3.455, 51.4535, -3.445, 51.4545), sitename="Llanblethian"), SITE, padded)
    assert found.where == "margin"
    assert found.site_polygons == ()


def test_the_loose_bbox_trap_is_dropped():
    """A triangle whose envelope overlaps the padded extent but whose
    shape stays south-east of it: GeoServer's loose bounding box returns
    such features (a Heritage Coast 7 km from Cowbridge, in the research
    query), and they must reach no file or line."""
    padded = padded_rect(SITE)
    triangle = {"type": "Polygon", "coordinates": [[[-3.50, 51.40], [-3.30, 51.40], [-3.30, 51.50], [-3.50, 51.40]]]}
    assert classify_feature(SAC, _feature(triangle, SAC_name="Far away"), SITE, padded) is None


def test_points_are_tested_one_by_one():
    padded = padded_rect(SITE)
    on_site = _feature({"type": "MultiPoint", "coordinates": [[-3.45, 51.46]]}, Name="House", Grade="II", RecordNumber=7)
    found = classify_feature(LISTED, on_site, SITE, padded)
    assert found.where == "site" and found.grade_or_zone == "II" and found.reference == "7"
    outside = _feature({"type": "Point", "coordinates": [-3.30, 51.30]}, Name="Far", Grade="II")
    assert classify_feature(LISTED, outside, SITE, padded) is None


def test_null_and_blank_text_reads_as_none():
    padded = padded_rect(SITE)
    found = classify_feature(CONSERVATION, _feature(_polygon(-3.455, 51.458, -3.445, 51.465), sitename="null", inspireid="  "), SITE, padded)
    assert found.name is None and found.reference is None


def test_output_geometry_is_clipped_to_the_padded_extent_and_closed():
    padded = padded_rect(SITE)
    huge = _polygon(-4.0, 51.0, -3.0, 52.0)
    found = classify_feature(CONSERVATION, _feature(huge, sitename="Big"), SITE, padded)
    ring = found.geometry["coordinates"][0][0]
    assert ring[0] == ring[-1]
    xs = [p[0] for p in ring]
    assert min(xs) == pytest.approx(padded[0]) and max(xs) == pytest.approx(padded[2])


def test_assess_keeps_failures_apart_and_counts_unsupported_geometry():
    line = _feature({"type": "LineString", "coordinates": [[-3.45, 51.46], [-3.44, 51.46]]})
    results = [
        LayerResult(FLOOD_RIVERS, ok=True, features=(_feature(_polygon(-3.455, 51.458, -3.445, 51.465), risk="Flood Zone 3"), line)),
        LayerResult(SAC, ok=False, kind="timeout", reason="DataMapWales did not answer in time"),
    ]
    assessment = assess(SITE, results)
    assert [item.layer for item in assessment.found] == [FLOOD_RIVERS]
    assert assessment.checked == [FLOOD_RIVERS]
    assert assessment.failed == [(SAC, "DataMapWales did not answer in time")]
    assert assessment.skipped_geometry == 1


def test_group_collections_carry_the_filterable_properties():
    results = [
        LayerResult(CONSERVATION, ok=True, features=(_feature(_polygon(-3.455, 51.458, -3.445, 51.465), sitename="Cowbridge", inspireid="W1"),)),
        LayerResult(LISTED, ok=True, features=(_feature({"type": "MultiPoint", "coordinates": [[-3.45, 51.46]]}, Name="House", Grade="II*", RecordNumber=9),)),
    ]
    collections = group_collections(assess(SITE, results))
    assert set(collections) == {"heritage", "listed_buildings"}
    properties = collections["heritage"]["features"][0]["properties"]
    assert properties == {
        "designation": "Conservation area",
        "name": "Cowbridge",
        "grade_or_zone": None,
        "reference": "W1",
        "where": "site",
        "source": "geonode:conservation_areas_wales",
    }
    assert collections["listed_buildings"]["features"][0]["properties"]["grade_or_zone"] == "II*"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_constraints.py -q -p no:cacheprovider`
Expected: collection error, `ModuleNotFoundError: No module named 'mapgen.constraints'`.

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/constraints.py`:

```python
"""Planning constraints for Wales: the layer table, and every decision
made about a fetched feature, with no network at all.

The source (mapgen.sources.constraints_wales) fetches; this module
decides. Everything here works in the drawn site's own lon/lat frame
(see mapgen.planar's docstring and the spec's "Architecture"). A
feature the server returned is re-tested here for true intersection
with the site and with the 250 m margin, because GeoServer's bbox
filter can match on a feature's envelope rather than its shape (the
spec's "loose bbox" note): one that touches neither is dropped before
it reaches any file, count or summary line.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from mapgen.planar import Rect, clip_polygon, point_in_rect, polygon_area_in_rect

METRES_PER_DEGREE_LAT = 111_320.0
MARGIN_METRES = 250.0
SHARE_GRID_CELLS = 400
OUTPUT_GROUPS = ("flood", "nature", "landscape", "heritage", "listed_buildings")


@dataclass(frozen=True)
class Layer:
    type_name: str
    designation: str
    group: str
    attribution: str
    name_field: str | None
    grade_field: str | None
    reference_field: str | None


# The spec's table, in the spec's order; field names read off each live
# layer on 2026-09-18.
LAYERS: tuple[Layer, ...] = (
    Layer("inspire-nrw:NRW_FLOODZONE_RIVERS", "Flood zone, rivers", "flood", "A", None, "risk", "mm_id"),
    Layer("inspire-nrw:NRW_FLOODZONE_SEAS", "Flood zone, sea", "flood", "A", None, "risk", "mm_id"),
    Layer(
        "inspire-nrw:NRW_FLOODZONE_SURFACE_WATER_AND_SMALL_WATERCOURSES",
        "Flood zone, surface water and small watercourses", "flood", "A", None, "risk", "mm_id",
    ),
    Layer("inspire-nrw:NRW_TAN15_DEFENDED_ZONES", "TAN15 defended zone", "flood", "A", "name", "defends_against", None),
    Layer("inspire-nrw:NRW_SSSI", "SSSI", "nature", "B", "sssi_name", None, "sssi_code"),
    Layer("inspire-nrw:NRW_SAC", "Special Area of Conservation", "nature", "B", "SAC_name", None, "Site_code"),
    Layer("inspire-nrw:NRW_SPA", "Special Protection Area", "nature", "B", "SPA_Name", None, "SPA_code"),
    Layer("inspire-nrw:NRW_RAMSAR", "Ramsar site", "nature", "B", "RAM_name", None, "RAM_code"),
    Layer("inspire-nrw:NRW_NNR", "National Nature Reserve", "nature", "B", "NNR_Name", None, "NNR_Code"),
    Layer("inspire-nrw:NRW_LNR", "Local Nature Reserve", "nature", "B", "lnr_name", None, "isis_id"),
    Layer(
        "inspire-nrw:NRW_ANCIENT_WOODLAND_INVENTORY_2021", "Ancient woodland", "nature", "B",
        None, "category_name", "unique_id",
    ),
    Layer("inspire-nrw:NRW_NATIONAL_PARK", "National Park", "landscape", "B", "np_name", None, "isis_id"),
    Layer("inspire-nrw:NRW_AONB", "AONB (National Landscape)", "landscape", "B", "AONB_NAME", None, "ISIS_ID"),
    Layer("inspire-nrw:NRW_HERITAGE_COAST", "Heritage Coast", "landscape", "B", "Coast_name", None, "ISIS_id"),
    Layer("inspire-wg:Cadw_ListedBuildings", "Listed building", "listed_buildings", "C", "Name", "Grade", "RecordNumber"),
    Layer("inspire-wg:Cadw_SAM", "Scheduled monument", "heritage", "C", "Name", None, "SAMNumber"),
    Layer(
        "geonode:cadw_rhpg_registeredareas", "Registered historic park or garden", "heritage", "C",
        "site_name_en", None, "reference_number",
    ),
    Layer(
        "inspire-wg:Cadw_HistoricLandscapes", "Registered historic landscape", "heritage", "C",
        "Name", "RegisterType", "RecordNumber",
    ),
    Layer(
        "inspire-wg:vGeoServer_WorldHeritageSites_Public", "World Heritage Site", "heritage", "D",
        "Name", None, "UNESCO_Ref",
    ),
    Layer("geonode:conservation_areas_wales", "Conservation area", "heritage", "D", "sitename", None, "inspireid"),
)
LAYER_BY_TYPE: dict[str, Layer] = {layer.type_name: layer for layer in LAYERS}


@dataclass(frozen=True)
class LayerResult:
    """One layer's answer: its raw GeoJSON features when ok, else the
    failure kind (sources/base.py's vocabulary) and a plain reason."""

    layer: Layer
    ok: bool
    features: tuple = ()
    kind: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Found:
    """One feature that truly touches the site or the margin."""

    layer: Layer
    name: str | None
    grade_or_zone: str | None
    reference: str | None
    where: str
    geometry: dict
    site_polygons: tuple


@dataclass
class Assessment:
    site: Rect
    padded: Rect
    found: list = field(default_factory=list)
    checked: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped_geometry: int = 0


def padded_rect(site: Rect, margin_m: float = MARGIN_METRES) -> Rect:
    west, south, east, north = site
    lat0 = math.radians((south + north) / 2)
    dlat = margin_m / METRES_PER_DEGREE_LAT
    dlon = margin_m / (METRES_PER_DEGREE_LAT * math.cos(lat0))
    return (west - dlon, south - dlat, east + dlon, north + dlat)


def _text(value: object) -> str | None:
    """Trimmed text, or None for a missing, blank or literal "null"
    value (the NRW layers pad names with spaces and write "null")."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text


def _polygons(geometry: dict) -> list:
    kind = geometry.get("type")
    if kind == "Polygon":
        return [geometry["coordinates"]]
    if kind == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def _points(geometry: dict) -> list:
    kind = geometry.get("type")
    if kind == "Point":
        return [geometry["coordinates"]]
    if kind == "MultiPoint":
        return list(geometry["coordinates"])
    return []


def _closed(ring: list) -> list:
    return [list(point) for point in ring] + [list(ring[0])]


def classify_feature(layer: Layer, feature: dict, site: Rect, padded: Rect) -> Found | None:
    """The feature as found, or None when its shape reaches neither the
    site nor the margin (or it has no polygon or point geometry)."""
    geometry = feature.get("geometry") or {}
    properties = feature.get("properties") or {}
    polygons = _polygons(geometry)
    points = _points(geometry)
    if polygons:
        on_site = any(polygon_area_in_rect(rings, site) > 0.0 for rings in polygons)
        if not on_site and not any(polygon_area_in_rect(rings, padded) > 0.0 for rings in polygons):
            return None
        clipped = [c for c in (clip_polygon(rings, padded) for rings in polygons) if c]
        out_geometry = {
            "type": "MultiPolygon",
            "coordinates": [[_closed(ring) for ring in rings] for rings in clipped],
        }
        site_polygons = (
            tuple(tuple(c) for c in (clip_polygon(rings, site) for rings in polygons) if c) if on_site else ()
        )
    elif points:
        coordinates = [(float(p[0]), float(p[1])) for p in points]
        inside = [p for p in coordinates if point_in_rect(p, padded)]
        if not inside:
            return None
        on_site = any(point_in_rect(p, site) for p in coordinates)
        out_geometry = {"type": "MultiPoint", "coordinates": [list(p) for p in inside]}
        site_polygons = ()
    else:
        return None
    return Found(
        layer=layer,
        name=_text(properties.get(layer.name_field)) if layer.name_field else None,
        grade_or_zone=_text(properties.get(layer.grade_field)) if layer.grade_field else None,
        reference=_text(properties.get(layer.reference_field)) if layer.reference_field else None,
        where="site" if on_site else "margin",
        geometry=out_geometry,
        site_polygons=site_polygons,
    )


def assess(site: Rect, results: Sequence[LayerResult], margin_m: float = MARGIN_METRES) -> Assessment:
    padded = padded_rect(site, margin_m)
    assessment = Assessment(site=tuple(site), padded=padded)
    for result in results:
        if not result.ok:
            assessment.failed.append((result.layer, result.reason or "did not arrive"))
            continue
        assessment.checked.append(result.layer)
        for feature in result.features:
            geometry = feature.get("geometry") or {}
            if not _polygons(geometry) and not _points(geometry):
                assessment.skipped_geometry += 1
                continue
            item = classify_feature(result.layer, feature, assessment.site, padded)
            if item is not None:
                assessment.found.append(item)
    return assessment


def group_collections(assessment: Assessment) -> dict[str, dict]:
    """One FeatureCollection per output group that has features, in
    OUTPUT_GROUPS order."""
    by_group: dict[str, list] = {}
    for item in assessment.found:
        by_group.setdefault(item.layer.group, []).append(
            {
                "type": "Feature",
                "properties": {
                    "designation": item.layer.designation,
                    "name": item.name,
                    "grade_or_zone": item.grade_or_zone,
                    "reference": item.reference,
                    "where": item.where,
                    "source": item.layer.type_name,
                },
                "geometry": item.geometry,
            }
        )
    return {
        group: {"type": "FeatureCollection", "features": by_group[group]}
        for group in OUTPUT_GROUPS
        if group in by_group
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_constraints.py -q -p no:cacheprovider`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/constraints.py tests/test_constraints.py
git commit -m "feat(constraints): the Welsh layer table and a local intersection re-test of every feature"
```

---

### Task 4: The summary text and the survey.json record

**Files:**
- Modify: `src/mapgen/constraints.py` (append)
- Test: `tests/test_constraints.py` (append)

**Interfaces:**
- Consumes: everything Task 3 produced; `mapgen.planar.share_of_rect`.
- Produces: `ATTRIBUTIONS: dict[str, str]`; `COAL_LINE`, `TPO_LINE`, `PARTIAL_LINE`, `CONSERVATION_CAVEAT` (str); `attribution_statements(groups, fetched_on) -> list[str]`; `share_text(share) -> str`; `Summary(on_site, margin, none_found, not_checked, authority, conservation_found, attribution_groups)`; `summarise(assessment, councils, coverage) -> Summary`; `render_text(summary, title, fetched_on, margin_m=MARGIN_METRES) -> str`; `to_record(summary, assessment, *, councils, coverage, coverage_share, margin_m, summary_file, fetched_on) -> dict`. Each `on_site` / `margin` entry is `{"designation", "label", "share", "count", "grades", "line"}`; each `not_checked` entry is `{"designation", "reason", "line"}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_constraints.py`:

```python
from mapgen.constraints import (
    ATTRIBUTIONS,
    COAL_LINE,
    CONSERVATION_CAVEAT,
    PARTIAL_LINE,
    TPO_LINE,
    attribution_statements,
    render_text,
    share_text,
    summarise,
    to_record,
)

NATIONAL_PARK = LAYER_BY_TYPE["inspire-nrw:NRW_NATIONAL_PARK"]


def _site_left_half():
    """A polygon covering the west half of SITE exactly."""
    middle = (SITE[0] + SITE[2]) / 2
    return _polygon(SITE[0] - 0.01, SITE[1] - 0.01, middle, SITE[3] + 0.01)


def _all_checked_but(*results):
    """Every layer checked and empty, except the given results."""
    given = {result.layer for result in results}
    return list(results) + [LayerResult(layer, ok=True) for layer in LAYERS if layer not in given]


@pytest.mark.parametrize(
    "share,text",
    [
        (0.0, "under 1% of the site"),
        (0.0049, "under 1% of the site"),
        (0.005, "1% of the site"),
        (0.5, "50% of the site"),
        (0.125, "13% of the site"),
        (0.994, "99% of the site"),
        (0.995, "the whole site"),
        (1.0, "the whole site"),
    ],
)
def test_share_text_rules(share, text):
    assert share_text(share) == text


def test_an_area_on_the_site_gets_its_share():
    results = _all_checked_but(LayerResult(CONSERVATION, ok=True, features=(_feature(_site_left_half(), sitename="Cowbridge"),)))
    summary = summarise(assess(SITE, results), ["Vale of Glamorgan"], "full")
    assert [entry["line"] for entry in summary.on_site] == ["Conservation area: Cowbridge (50% of the site)"]
    assert summary.on_site[0]["share"] == pytest.approx(0.5, abs=0.01)


def test_a_nameless_flood_zone_is_labelled_by_its_zone():
    results = _all_checked_but(LayerResult(FLOOD_RIVERS, ok=True, features=(_feature(_site_left_half(), risk="Flood Zone 3"),)))
    summary = summarise(assess(SITE, results), [], "full")
    assert summary.on_site[0]["line"] == "Flood zone, rivers: Flood Zone 3 (50% of the site)"


def test_listed_buildings_are_counted_by_grade_in_order():
    points = [
        _feature({"type": "Point", "coordinates": [-3.45, 51.46]}, Name="A", Grade="II"),
        _feature({"type": "Point", "coordinates": [-3.449, 51.46]}, Name="B", Grade="II*"),
        _feature({"type": "Point", "coordinates": [-3.448, 51.46]}, Name="C", Grade="II"),
        _feature({"type": "Point", "coordinates": [-3.447, 51.46]}, Name="D", Grade="I"),
    ]
    summary = summarise(assess(SITE, _all_checked_but(LayerResult(LISTED, ok=True, features=tuple(points)))), [], "full")
    assert summary.on_site[0]["line"] == "Listed buildings: 4 (Grade I 1, Grade II* 1, Grade II 2)"
    assert summary.on_site[0]["grades"] == {"II": 2, "II*": 1, "I": 1}


def test_the_margin_lists_only_what_is_not_on_the_site():
    on_site = _feature(_polygon(-3.455, 51.458, -3.445, 51.465), sitename="Cowbridge")
    margin_same = _feature(_polygon(-3.455, 51.4535, -3.445, 51.4545), sitename="Cowbridge")
    margin_other = _feature(_polygon(-3.455, 51.4535, -3.445, 51.4545), sitename="Llanblethian")
    results = _all_checked_but(LayerResult(CONSERVATION, ok=True, features=(on_site, margin_same, margin_other)))
    summary = summarise(assess(SITE, results), [], "full")
    assert [entry["line"] for entry in summary.margin] == ["Conservation area: Llanblethian"]


def test_a_failed_layer_is_not_checked_and_never_none_found():
    results = _all_checked_but(LayerResult(SAC, ok=False, kind="timeout", reason="DataMapWales did not answer in time"))
    summary = summarise(assess(SITE, results), [], "full")
    assert "Special Area of Conservation" not in summary.none_found
    lines = [entry["line"] for entry in summary.not_checked]
    assert lines[0] == "Special Area of Conservation: DataMapWales did not answer in time"
    assert COAL_LINE in lines and TPO_LINE in lines


def test_everything_checked_and_absent_is_none_found_in_table_order():
    summary = summarise(assess(SITE, _all_checked_but()), [], "full")
    assert summary.none_found == [layer.designation for layer in LAYERS]
    assert [entry["line"] for entry in summary.not_checked] == [COAL_LINE, TPO_LINE]


def test_partial_coverage_adds_the_outside_wales_line():
    summary = summarise(assess(SITE, _all_checked_but()), [], "partial")
    assert summary.not_checked[-1]["line"] == PARTIAL_LINE


def test_the_authority_is_the_park_when_a_national_park_covers_the_site():
    park = LayerResult(NATIONAL_PARK, ok=True, features=(_feature(_site_left_half(), np_name="Bannau Brycheiniog"),))
    assert summarise(assess(SITE, _all_checked_but(park)), ["Powys"], "full").authority == (
        "the National Park authority (Bannau Brycheiniog)"
    )
    assert summarise(assess(SITE, _all_checked_but()), ["Vale of Glamorgan"], "full").authority == (
        "the council (Vale of Glamorgan)"
    )
    assert summarise(assess(SITE, _all_checked_but()), [], "full").authority == "the local planning authority"


def test_cadw_attribution_carries_the_fetch_date():
    statements = attribution_statements(["C"], "2026-09-18")
    assert statements == [ATTRIBUTIONS["C"].format(date="2026-09-18")]
    assert "2026-09-18" in statements[0]


def test_render_text_layout():
    results = _all_checked_but(LayerResult(CONSERVATION, ok=True, features=(_feature(_site_left_half(), sitename="Cowbridge"),)))
    summary = summarise(assess(SITE, results), ["Vale of Glamorgan"], "full")
    text = render_text(summary, "Cowbridge_2026-09-18", "2026-09-18")
    lines = text.splitlines()
    assert lines[0] == "Planning constraints: Cowbridge_2026-09-18"
    assert lines[1] == "Indicative, from open data checked 2026-09-18. Confirm with the council (Vale of Glamorgan)."
    assert "On the site" in lines and "Within 250 m" in lines
    assert "Checked, none found" in lines and "Not checked" in lines and "Sources" in lines
    assert "- Conservation area: Cowbridge (50% of the site)" in lines
    assert lines[lines.index("Within 250 m") + 1] == "- nothing found"
    assert CONSERVATION_CAVEAT in lines
    assert text.endswith("\n")
    assert "\u2014" not in text


def test_to_record_shape():
    results = _all_checked_but(LayerResult(SAC, ok=False, kind="timeout", reason="DataMapWales did not answer in time"))
    assessment = assess(SITE, results)
    summary = summarise(assessment, ["Vale of Glamorgan"], "full")
    record = to_record(
        summary, assessment, councils=["Vale of Glamorgan"], coverage="full", coverage_share=0.9991,
        margin_m=250.0, summary_file="x_constraints.txt", fetched_on="2026-09-18",
    )
    assert record["checked"] is True
    assert record["coverage"] == "full" and record["coverage_share"] == 0.9991
    assert record["layers"]["inspire-nrw:NRW_SAC"] == {
        "designation": "Special Area of Conservation", "status": "failed", "features": None,
        "reason": "DataMapWales did not answer in time",
    }
    assert record["layers"]["inspire-nrw:NRW_SSSI"]["status"] == "ok"
    assert record["layers"]["inspire-nrw:NRW_SSSI"]["features"] == 0
    assert record["summary_file"] == "x_constraints.txt"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_constraints.py -q -p no:cacheprovider`
Expected: collection error, `ImportError: cannot import name 'ATTRIBUTIONS' from 'mapgen.constraints'`.

- [ ] **Step 3: Write the implementation**

Append to `src/mapgen/constraints.py`, and add `share_of_rect` to its `mapgen.planar` import line:

```python
# The four attribution groups, verbatim from each layer's DataMapWales
# page (the spec's "Attribution groups"). C's {date} is the date the
# data was received from Cadw, as Cadw's own wording instructs; D is
# OGL v3's own default statement, for layers that publish none.
ATTRIBUTIONS: dict[str, str] = {
    "A": (
        "Contains Natural Resources Wales information \u00a9 Natural Resources Wales and database right. "
        "All rights reserved. Some features of this information are based on digital spatial data licensed "
        "from the UK Centre for Ecology & Hydrology \u00a9 UKCEH. Defra, Met Office and DARD Rivers Agency "
        "\u00a9 Crown copyright. \u00a9 Cranfield University. \u00a9 James Hutton Institute. Contains OS data "
        "\u00a9 Crown copyright and database right."
    ),
    "B": (
        "Contains Natural Resources Wales information \u00a9 Natural Resources Wales and Database Right. "
        "All rights Reserved. Contains Ordnance Survey Data. Ordnance Survey Licence number AC0000849444. "
        "Crown Copyright and Database Right."
    ),
    "C": (
        "Designated Historic Asset GIS Data, The Welsh Historic Environment Service (Cadw), {date}, "
        "licensed under the Open Government Licence"
    ),
    "D": "Contains public sector information licensed under the Open Government Licence v3.0.",
}

COAL_LINE = "Coal mining risk: no open data service. See the Mining Remediation Authority's interactive map."
TPO_LINE = "Tree Preservation Orders: no national dataset for Wales. Ask the council."
PARTIAL_LINE = "Part of the extent is outside Wales and was not checked."
CONSERVATION_CAVEAT = "Conservation areas are boundaries only; contact the council for full details."
GRADE_ORDER = ("I", "II*", "II")
LISTED_GROUP = "listed_buildings"
NATIONAL_PARK_TYPE = "inspire-nrw:NRW_NATIONAL_PARK"
CONSERVATION_TYPE = "geonode:conservation_areas_wales"


def attribution_statements(groups, fetched_on: str) -> list[str]:
    wanted = set(groups)
    return [
        ATTRIBUTIONS[group].format(date=fetched_on) if group == "C" else ATTRIBUTIONS[group]
        for group in ("A", "B", "C", "D")
        if group in wanted
    ]


def share_text(share: float) -> str:
    if share >= 0.995:
        return "the whole site"
    if share < 0.005:
        return "under 1% of the site"
    # Half up, not Python's round-half-to-even, so 0.5% reads 1%.
    return f"{int(share * 100 + 0.5)}% of the site"


@dataclass
class Summary:
    on_site: list
    margin: list
    none_found: list
    not_checked: list
    authority: str
    conservation_found: bool
    attribution_groups: list


def _label(item: Found) -> str | None:
    return item.name if item.layer.name_field else item.grade_or_zone


def _grade_counts(items: Sequence[Found]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        grade = item.grade_or_zone or "ungraded"
        counts[grade] = counts.get(grade, 0) + 1
    return counts


def _grades_text(counts: dict[str, int]) -> str:
    known = [grade for grade in GRADE_ORDER if grade in counts]
    others = sorted(grade for grade in counts if grade not in GRADE_ORDER)
    parts = [f"Grade {grade} {counts[grade]}" for grade in known]
    parts += [f"{grade} {counts[grade]}" for grade in others]
    return ", ".join(parts)


def _area_groups(items: Sequence[Found]) -> dict[tuple[str, str | None], list]:
    groups: dict[tuple[str, str | None], list] = {}
    for item in items:
        if item.layer.group == LISTED_GROUP:
            continue
        groups.setdefault((item.layer.type_name, _label(item)), []).append(item)
    return groups


def summarise(assessment: Assessment, councils: Sequence[str], coverage: str) -> Summary:
    order = {layer.type_name: index for index, layer in enumerate(LAYERS)}
    site_items = [item for item in assessment.found if item.where == "site"]
    margin_items = [item for item in assessment.found if item.where == "margin"]
    x_scale = math.cos(math.radians((assessment.site[1] + assessment.site[3]) / 2))

    ranked: list[tuple[int, str, dict]] = []
    site_groups = _area_groups(site_items)
    for (type_name, label), items in site_groups.items():
        layer = LAYER_BY_TYPE[type_name]
        polygons = [rings for item in items for rings in item.site_polygons]
        share = share_of_rect(polygons, assessment.site, SHARE_GRID_CELLS, x_scale)
        subject = f"{layer.designation}: {label}" if label else layer.designation
        ranked.append(
            (order[type_name], label or "", {
                "designation": layer.designation, "label": label, "share": round(share, 4),
                "count": len(items), "grades": None, "line": f"{subject} ({share_text(share)})",
            })
        )
    listed_site = [item for item in site_items if item.layer.group == LISTED_GROUP]
    if listed_site:
        counts = _grade_counts(listed_site)
        ranked.append(
            (order["inspire-wg:Cadw_ListedBuildings"], "", {
                "designation": "Listed building", "label": None, "share": None, "count": len(listed_site),
                "grades": counts, "line": f"Listed buildings: {len(listed_site)} ({_grades_text(counts)})",
            })
        )
    on_site = [entry for _, _, entry in sorted(ranked, key=lambda r: (r[0], r[1]))]

    ranked = []
    for (type_name, label), items in _area_groups(margin_items).items():
        if (type_name, label) in site_groups:
            continue
        layer = LAYER_BY_TYPE[type_name]
        ranked.append(
            (order[type_name], label or "", {
                "designation": layer.designation, "label": label, "share": None, "count": len(items),
                "grades": None, "line": f"{layer.designation}: {label}" if label else layer.designation,
            })
        )
    listed_margin = [item for item in margin_items if item.layer.group == LISTED_GROUP]
    if listed_margin:
        counts = _grade_counts(listed_margin)
        ranked.append(
            (order["inspire-wg:Cadw_ListedBuildings"], "", {
                "designation": "Listed building", "label": None, "share": None, "count": len(listed_margin),
                "grades": counts, "line": f"Listed buildings: {len(listed_margin)} ({_grades_text(counts)})",
            })
        )
    margin = [entry for _, _, entry in sorted(ranked, key=lambda r: (r[0], r[1]))]

    found_types = {item.layer.type_name for item in assessment.found}
    none_found = [layer.designation for layer in assessment.checked if layer.type_name not in found_types]
    none_found.sort(key=lambda designation: next(i for i, l in enumerate(LAYERS) if l.designation == designation))

    not_checked = [
        {"designation": layer.designation, "reason": reason, "line": f"{layer.designation}: {reason}"}
        for layer, reason in assessment.failed
    ]
    not_checked.append({"designation": "Coal mining risk", "reason": "no open data service", "line": COAL_LINE})
    not_checked.append({"designation": "Tree Preservation Orders", "reason": "no national dataset for Wales", "line": TPO_LINE})
    if coverage == "partial":
        not_checked.append({"designation": "Outside Wales", "reason": "outside the Welsh layers", "line": PARTIAL_LINE})

    parks = sorted({item.name for item in site_items if item.layer.type_name == NATIONAL_PARK_TYPE and item.name})
    if parks:
        authority = f"the National Park authority ({' and '.join(parks)})"
    elif councils:
        authority = f"the council ({' and '.join(councils)})"
    else:
        authority = "the local planning authority"

    return Summary(
        on_site=on_site,
        margin=margin,
        none_found=none_found,
        not_checked=not_checked,
        authority=authority,
        conservation_found=any(item.layer.type_name == CONSERVATION_TYPE for item in assessment.found),
        attribution_groups=sorted({layer.attribution for layer in assessment.checked}),
    )


def render_text(summary: Summary, title: str, fetched_on: str, margin_m: float = MARGIN_METRES) -> str:
    lines = [
        f"Planning constraints: {title}",
        f"Indicative, from open data checked {fetched_on}. Confirm with {summary.authority}.",
        "",
        "On the site",
    ]
    lines += [f"- {entry['line']}" for entry in summary.on_site] or ["- nothing found"]
    lines += ["", f"Within {int(margin_m)} m"]
    lines += [f"- {entry['line']}" for entry in summary.margin] or ["- nothing found"]
    lines += ["", "Checked, none found"]
    lines.append(f"- {'; '.join(summary.none_found)}" if summary.none_found else "- none")
    lines += ["", "Not checked"]
    lines += [f"- {entry['line']}" for entry in summary.not_checked]
    if summary.conservation_found:
        lines += ["", CONSERVATION_CAVEAT]
    lines += ["", "Sources"]
    lines += [f"- {statement}" for statement in attribution_statements(summary.attribution_groups, fetched_on)]
    return "\n".join(lines) + "\n"


def to_record(
    summary: Summary,
    assessment: Assessment,
    *,
    councils: Sequence[str],
    coverage: str,
    coverage_share: float,
    margin_m: float,
    summary_file: str,
    fetched_on: str,
) -> dict:
    found_counts: dict[str, int] = {}
    for item in assessment.found:
        found_counts[item.layer.type_name] = found_counts.get(item.layer.type_name, 0) + 1
    failed = {layer.type_name: reason for layer, reason in assessment.failed}
    layers: dict[str, dict] = {}
    for layer in LAYERS:
        if layer.type_name in failed:
            layers[layer.type_name] = {
                "designation": layer.designation, "status": "failed", "features": None,
                "reason": failed[layer.type_name],
            }
        elif layer in assessment.checked:
            layers[layer.type_name] = {
                "designation": layer.designation, "status": "ok",
                "features": found_counts.get(layer.type_name, 0), "reason": None,
            }
    return {
        "checked": True,
        "fetched_on": fetched_on,
        "margin_m": margin_m,
        "coverage": coverage,
        "coverage_share": round(coverage_share, 4),
        "councils": list(councils),
        "authority": summary.authority,
        "on_site": summary.on_site,
        "margin": summary.margin,
        "none_found": summary.none_found,
        "not_checked": summary.not_checked,
        "layers": layers,
        "skipped_geometry": assessment.skipped_geometry,
        "summary_file": summary_file,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_constraints.py -q -p no:cacheprovider`
Expected: 29 passed (10 from Task 3, 19 here counting each of the 8 parametrised share cases).

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/constraints.py tests/test_constraints.py
git commit -m "feat(constraints): the plain-English summary and the survey.json record"
```

---

### Task 5: The source: fetching the layers

**Files:**
- Create: `src/mapgen/sources/constraints_wales.py`
- Create (recorded): `tests/fixtures/constraints/cowbridge_conservation_areas.json`, `tests/fixtures/constraints/cowbridge_listed_buildings.json`
- Test: `tests/test_sources_constraints_wales.py`

**Interfaces:**
- Consumes: `mapgen.constraints` (Tasks 3 and 4): `LAYERS`, `LAYER_BY_TYPE`, `MARGIN_METRES`, `LayerResult`, `assess`, `padded_rect`, `summarise`, `attribution_statements`; `mapgen.wales_coverage` (Task 2): `COVERAGE_PATH`, `coverage_share`, `verdict`, `councils_touching`; `mapgen.sources.base`: `Estimate`, `FAILURE_SERVICE_ERROR`, `TileFailure`, `classify_status_failure`, `classify_transport_failure`; `mapgen.cog.USER_AGENT`; `mapgen.fsutil.atomic_write_text`; `mapgen.jobs.CancelToken`.
- Produces: `WFS_URL`, `PAGE_SIZE = 1000`, `FEATURE_CEILING = 20_000`, `REQUEST_TIMEOUT_SECONDS = 60.0`, `WORK_FEATURES = "constraints_layers.jsonl"`, `WORK_META = "constraints_meta.json"`, `WHOLE_AREA = "whole-area"`, `ConstraintsWalesError`, and `ConstraintsWalesSource(session=None, coverage_path=None)` with `id = "constraints_wales"`, `covers`, `tier`, `detail`, `estimate`, `fetch`. Task 6 adds `merge` and `possible_outputs` to the same class.

- [ ] **Step 1: Record the two real fixtures**

Run from the repo root (this is the task's only network step):

```bash
PYTHONPATH=src /c/Python313/python.exe - <<'EOF'
import json, urllib.parse, urllib.request
from pathlib import Path
from mapgen.cog import USER_AGENT
out = Path("tests/fixtures/constraints")
out.mkdir(parents=True, exist_ok=True)
bbox = "-3.4637,51.4527,-3.4343,51.4703,EPSG:4326"  # Cowbridge plus the 250 m margin
keep_fields = {"sitename", "localplann", "inspireid", "Name", "Grade", "RecordNumber"}
for type_name, name, keep in (
    ("geonode:conservation_areas_wales", "cowbridge_conservation_areas.json", None),
    ("inspire-wg:Cadw_ListedBuildings", "cowbridge_listed_buildings.json", 12),
):
    query = urllib.parse.urlencode({
        "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": type_name,
        "outputFormat": "application/json", "srsName": "EPSG:4326", "bbox": bbox,
        "count": "1000", "startIndex": "0",
    })
    request = urllib.request.Request("https://datamap.gov.wales/geoserver/wfs?" + query, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.load(response)
    if keep:
        body["features"] = body["features"][:keep]
    body["numberMatched"] = len(body["features"])
    body["numberReturned"] = len(body["features"])
    for feature in body["features"]:
        feature["properties"] = {k: v for k, v in feature["properties"].items() if k in keep_fields}
    (out / name).write_text(json.dumps(body, indent=1), encoding="utf-8")
    print(name, len(body["features"]), [f["properties"].get("sitename") or f["properties"].get("Name") for f in body["features"]][:4])
EOF
```

Expected: `cowbridge_conservation_areas.json 2 ['Cowbridge', 'Llanblethian']` (order may differ) and `cowbridge_listed_buildings.json 12 [...]`. If the conservation-area names differ, stop and report.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_sources_constraints_wales.py`:

```python
import json
from pathlib import Path

import pytest
import requests

from mapgen.cog import USER_AGENT
from mapgen.constraints import LAYERS, padded_rect
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken, Cancelled
from mapgen.sources import constraints_wales
from mapgen.sources.constraints_wales import (
    FEATURE_CEILING,
    REQUEST_TIMEOUT_SECONDS,
    WFS_URL,
    WORK_FEATURES,
    WORK_META,
    ConstraintsWalesError,
    ConstraintsWalesSource,
)

FIXTURES = Path(__file__).parent / "fixtures" / "constraints"
COWBRIDGE = BBox.parse("-3.46,51.455,-3.438,51.468")
BRISTOL = BBox.parse("-2.62,51.44,-2.56,51.47")


def _tiles(bbox=COWBRIDGE):
    return [Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox)]


class _Response:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return json.loads(self.text)


def _collection(features=(), matched=None):
    features = list(features)
    return _Response(payload={
        "type": "FeatureCollection", "features": features,
        "numberMatched": len(features) if matched is None else matched, "numberReturned": len(features),
    })


class _RoutedSession:
    """Answers each GetFeature by its typeNames; an answer may be a
    response, an exception to raise, or a list consumed one per call."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout})
        answer = self.answers.get(params["typeNames"], _collection())
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def _fixture(name):
    return _Response(text=(FIXTURES / name).read_text(encoding="utf-8"))


def test_one_request_per_layer_in_table_order_with_the_verified_shape(tmp_path):
    session = _RoutedSession()
    source = ConstraintsWalesSource(session=session)
    source.fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    assert [call["params"]["typeNames"] for call in session.calls] == [layer.type_name for layer in LAYERS]
    call = session.calls[0]
    west, south, east, north = padded_rect((COWBRIDGE.west, COWBRIDGE.south, COWBRIDGE.east, COWBRIDGE.north))
    assert call["url"] == WFS_URL
    assert call["params"]["srsName"] == "EPSG:4326"
    assert call["params"]["outputFormat"] == "application/json"
    # lon,lat order: the verified trap is that lat,lon silently returns nothing.
    assert call["params"]["bbox"] == f"{west},{south},{east},{north},EPSG:4326"
    assert call["params"]["startIndex"] == "0"
    assert call["headers"]["User-Agent"] == USER_AGENT
    assert call["timeout"] == REQUEST_TIMEOUT_SECONDS


def test_a_full_page_asks_for_the_next(tmp_path, monkeypatch):
    monkeypatch.setattr(constraints_wales, "PAGE_SIZE", 2)
    point = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-3.45, 51.46]}, "properties": {"Name": "A", "Grade": "II"}}
    session = _RoutedSession({"inspire-wg:Cadw_ListedBuildings": [_collection([point, point], matched=3), _collection([point], matched=3)]})
    ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    listed = [c for c in session.calls if c["params"]["typeNames"] == "inspire-wg:Cadw_ListedBuildings"]
    assert [c["params"]["startIndex"] for c in listed] == ["0", "2"]
    rows = [json.loads(line) for line in (tmp_path / WORK_FEATURES).read_text(encoding="utf-8").splitlines()]
    assert len(next(r for r in rows if r["type_name"] == "inspire-wg:Cadw_ListedBuildings")["features"]) == 3


def test_a_layer_over_the_ceiling_is_not_checked(tmp_path):
    session = _RoutedSession({"inspire-nrw:NRW_SSSI": _collection([], matched=FEATURE_CEILING + 1)})
    ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    rows = {r["type_name"]: r for r in (json.loads(l) for l in (tmp_path / WORK_FEATURES).read_text(encoding="utf-8").splitlines())}
    assert rows["inspire-nrw:NRW_SSSI"]["ok"] is False
    assert "too many" in rows["inspire-nrw:NRW_SSSI"]["reason"]


def test_a_layer_that_fails_once_is_retried_after_the_others(tmp_path):
    session = _RoutedSession({"inspire-nrw:NRW_SAC": [requests.ConnectionError("down"), _collection()]})
    ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    names = [c["params"]["typeNames"] for c in session.calls]
    assert names.count("inspire-nrw:NRW_SAC") == 2
    assert names[-1] == "inspire-nrw:NRW_SAC"
    rows = {r["type_name"]: r for r in (json.loads(l) for l in (tmp_path / WORK_FEATURES).read_text(encoding="utf-8").splitlines())}
    assert rows["inspire-nrw:NRW_SAC"]["ok"] is True


def test_a_layer_that_fails_twice_is_reported_without_a_url(tmp_path):
    failure = requests.ConnectionError("failed reaching https://example.invalid/secret")
    session = _RoutedSession({"inspire-nrw:NRW_SAC": failure})
    recorder = _Recorder()
    ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, recorder)
    rows = {r["type_name"]: r for r in (json.loads(l) for l in (tmp_path / WORK_FEATURES).read_text(encoding="utf-8").splitlines())}
    assert rows["inspire-nrw:NRW_SAC"]["ok"] is False
    assert rows["inspire-nrw:NRW_SAC"]["reason"] == "DataMapWales could not be reached"
    assert "example.invalid" not in json.dumps(recorder.events)
    assert ("constraint_layer_not_checked", {"layer": "Special Area of Conservation", "reason": "DataMapWales could not be reached"}) in recorder.events


def test_every_layer_failing_is_a_real_failure(tmp_path):
    session = _RoutedSession({layer.type_name: _Response(status_code=503, text="") for layer in LAYERS})
    source = ConstraintsWalesSource(session=session)
    with pytest.raises(ConstraintsWalesError):
        source.fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    assert [f.tile_id for f in source.tile_failures] == ["r00_c00"]
    assert source.tile_failures[0].source == "constraints_wales"
    assert "503" in source.tile_failures[0].reason
    assert not (tmp_path / WORK_FEATURES).exists()


def test_cancel_stops_before_the_next_layer(tmp_path):
    token = CancelToken()
    token.cancel()
    session = _RoutedSession()
    with pytest.raises(Cancelled):
        ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder(), cancel=token)
    assert session.calls == []


def test_work_files_and_a_resume_that_touches_nothing(tmp_path):
    session = _RoutedSession({"geonode:conservation_areas_wales": _fixture("cowbridge_conservation_areas.json")})
    recorder = _Recorder()
    parts = ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, recorder)
    assert [p.name for p in parts] == [WORK_FEATURES, WORK_META]
    meta = json.loads((tmp_path / WORK_META).read_text(encoding="utf-8"))
    assert meta["councils"] == ["Vale of Glamorgan"]
    assert meta["coverage"] == "full"
    assert meta["margin_m"] == 250.0
    assert any(event == "constraint_on_site" and fields["line"].startswith("Conservation area: Cowbridge") for event, fields in recorder.events)
    assert ("tile_done", {"source": "constraints_wales", "tile_id": "whole-area"}) in recorder.events

    class _Poisoned:
        def get(self, *args, **kwargs):
            raise AssertionError("a resume over the same extent must not fetch")

    again = ConstraintsWalesSource(session=_Poisoned()).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    assert [p.name for p in again] == [WORK_FEATURES, WORK_META]


def test_covers_tier_detail_estimate_never_touch_the_network():
    class _Poisoned:
        def get(self, *args, **kwargs):
            raise AssertionError("no network here")

    source = ConstraintsWalesSource(session=_Poisoned())
    assert source.covers(COWBRIDGE) == "full"
    assert source.covers(BRISTOL) == "none"
    assert source.tier("constraints") == 1
    assert source.tier("buildings") is None
    assert source.detail(COWBRIDGE) == "Welsh designations as published, indicative"
    estimate = source.estimate(COWBRIDGE, _tiles())
    assert estimate.bytes_estimate == len(LAYERS) * constraints_wales.BYTES_PER_LAYER_ESTIMATE
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_sources_constraints_wales.py -q -p no:cacheprovider`
Expected: collection error, `ModuleNotFoundError: No module named 'mapgen.sources.constraints_wales'`.

- [ ] **Step 4: Write the implementation**

Create `src/mapgen/sources/constraints_wales.py`:

```python
"""Planning constraints for Wales: twenty DataMapWales designation
layers over the drawn site plus a 250 m margin.

The spec is docs/superpowers/specs/2026-09-18-mapgen-planning-
constraints-wales-design.md. This module fetches and writes; every
decision about a feature is mapgen.constraints'.

One WFS 2.0 GetFeature per layer, one at a time, in table order. Two
traps verified live: without srsName GeoServer answers in EPSG:27700,
and the bbox must be lon,lat order (lat,lon returns nothing, silently).
A layer that fails is tried once more after the others; if it fails
again it is reported as not checked, never as none found, and the run
carries on. Only when every layer fails does this source record
tile_failures and raise, so the package is marked incomplete and the
retry pass gets its turn (the spec's "Failures are never absences").

DataMapWales's robots.txt disallows /geoserver/ for crawlers. This is
not a crawler: about twenty requests per survey the owner starts, over
one small area, identified by USER_AGENT (the spec's "Licence and usage
position").
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Sequence

import requests

from mapgen.cog import USER_AGENT
from mapgen.constraints import (
    LAYERS,
    MARGIN_METRES,
    Layer,
    LayerResult,
    assess,
    attribution_statements,
    padded_rect,
    summarise,
)
from mapgen.fsutil import atomic_write_text
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken
from mapgen.sources.base import (
    FAILURE_SERVICE_ERROR,
    Estimate,
    ProgressSink,
    TileFailure,
    classify_status_failure,
    classify_transport_failure,
)
from mapgen.wales_coverage import COVERAGE_PATH, councils_touching, coverage_share, verdict

WFS_URL = "https://datamap.gov.wales/geoserver/wfs"
PAGE_SIZE = 1000
FEATURE_CEILING = 20_000
REQUEST_TIMEOUT_SECONDS = 60.0
BYTES_PER_LAYER_ESTIMATE = 50_000
SECONDS_PER_LAYER_ESTIMATE = 1.0
WORK_FEATURES = "constraints_layers.jsonl"
WORK_META = "constraints_meta.json"
WHOLE_AREA = "whole-area"


class ConstraintsWalesError(RuntimeError):
    """Raised when no layer at all arrived. Never carries a URL."""


class _LayerFetchError(Exception):
    def __init__(self, kind: str, phrase: str) -> None:
        super().__init__(phrase)
        self.kind = kind
        self.phrase = phrase


def _site_rect(bbox: BBox) -> tuple[float, float, float, float]:
    return (bbox.west, bbox.south, bbox.east, bbox.north)


class ConstraintsWalesSource:
    id = "constraints_wales"
    display_name = "Planning constraints (Wales)"
    licence = "Open Government Licence v3.0"
    # Until merge() knows which layers answered and on what date, every
    # statement, with Cadw's date described rather than invented.
    attribution = "\n".join(attribution_statements("ABCD", "the date of the survey"))
    requires_api_key = False

    def __init__(self, session: object | None = None, coverage_path: Path | None = None) -> None:
        self.session = session if session is not None else requests.Session()
        self._coverage_path = Path(coverage_path) if coverage_path is not None else COVERAGE_PATH
        self.tile_failures: list[TileFailure] = []
        self.endpoints_used: list[str] = []
        self.constraints_record: dict | None = None

    def covers(self, bbox: BBox) -> str:
        return verdict(coverage_share(padded_rect(_site_rect(bbox)), self._coverage_path))

    def tier(self, category: str) -> int | None:
        return 1 if category == "constraints" else None

    def detail(self, bbox: BBox) -> str:
        return "Welsh designations as published, indicative"

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        return Estimate(
            bytes_estimate=len(LAYERS) * BYTES_PER_LAYER_ESTIMATE,
            seconds_estimate=len(LAYERS) * SECONDS_PER_LAYER_ESTIMATE,
        )

    def _get_page(self, layer: Layer, padded, start: int) -> dict:
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": layer.type_name,
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
            "bbox": f"{padded[0]},{padded[1]},{padded[2]},{padded[3]},EPSG:4326",
            "count": str(PAGE_SIZE),
            "startIndex": str(start),
        }
        try:
            response = self.session.get(
                WFS_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            kind, phrase = classify_transport_failure(exc)
            raise _LayerFetchError(kind, phrase) from None
        if response.status_code != 200:
            kind, phrase = classify_status_failure(response.status_code)
            raise _LayerFetchError(kind, phrase)
        try:
            body = response.json()
        except ValueError:
            raise _LayerFetchError(FAILURE_SERVICE_ERROR, "sent an answer that is not GeoJSON") from None
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise _LayerFetchError(FAILURE_SERVICE_ERROR, "sent an answer that is not GeoJSON")
        return body

    def _fetch_layer(self, layer: Layer, padded) -> list:
        too_many = f"holds more than {FEATURE_CEILING:,} features here, too many to check"
        features: list = []
        start = 0
        while True:
            body = self._get_page(layer, padded, start)
            matched = body.get("numberMatched")
            if isinstance(matched, int) and matched > FEATURE_CEILING:
                raise _LayerFetchError(FAILURE_SERVICE_ERROR, too_many)
            page = body["features"]
            features.extend(page)
            if len(features) > FEATURE_CEILING:
                raise _LayerFetchError(FAILURE_SERVICE_ERROR, too_many)
            if len(page) < PAGE_SIZE:
                return features
            start += PAGE_SIZE

    def _attempt(self, layer: Layer, padded) -> LayerResult:
        try:
            return LayerResult(layer=layer, ok=True, features=tuple(self._fetch_layer(layer, padded)))
        except _LayerFetchError as exc:
            return LayerResult(layer=layer, ok=False, kind=exc.kind, reason=f"DataMapWales {exc.phrase}")

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        self.tile_failures = []
        self.constraints_record = None
        site = _site_rect(bbox)
        padded = padded_rect(site)
        features_path = Path(work_dir) / WORK_FEATURES
        meta_path = Path(work_dir) / WORK_META
        if features_path.exists() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
            if meta.get("site") == list(site) and meta.get("margin_m") == MARGIN_METRES:
                progress.emit("tile_skipped", source=self.id, tile_id=WHOLE_AREA)
                return [features_path, meta_path]

        self.endpoints_used = [WFS_URL]
        results: dict[str, LayerResult] = {}
        for layer in LAYERS:
            if cancel is not None:
                cancel.raise_if_cancelled()
            results[layer.type_name] = self._attempt(layer, padded)
        for layer in LAYERS:
            if results[layer.type_name].ok:
                continue
            if cancel is not None:
                cancel.raise_if_cancelled()
            results[layer.type_name] = self._attempt(layer, padded)
        ordered = [results[layer.type_name] for layer in LAYERS]

        if not any(result.ok for result in ordered):
            first = ordered[0]
            reason = f"No planning constraint layer arrived: {first.reason}."
            self.tile_failures = [
                TileFailure(source=self.id, tile_id=tile.tile_id, kind=first.kind or FAILURE_SERVICE_ERROR, reason=reason)
                for tile in tiles
            ]
            raise ConstraintsWalesError("No planning constraint layer arrived from DataMapWales.")

        for result in ordered:
            if not result.ok:
                progress.emit("constraint_layer_not_checked", layer=result.layer.designation, reason=result.reason)

        share = coverage_share(padded, self._coverage_path)
        councils = councils_touching(site, self._coverage_path)
        meta = {
            "site": list(site),
            "margin_m": MARGIN_METRES,
            "fetched_on": datetime.date.today().isoformat(),
            "coverage": verdict(share),
            "coverage_share": share,
            "councils": councils,
        }
        rows = [
            json.dumps(
                {"type_name": r.layer.type_name, "ok": r.ok, "kind": r.kind, "reason": r.reason, "features": list(r.features)},
                separators=(",", ":"),
            )
            for r in ordered
        ]
        atomic_write_text(features_path, "\n".join(rows) + "\n")
        atomic_write_text(meta_path, json.dumps(meta, separators=(",", ":")))

        summary = summarise(assess(site, ordered), councils, meta["coverage"])
        for entry in summary.on_site:
            progress.emit("constraint_on_site", line=entry["line"])
        progress.emit("tile_done", source=self.id, tile_id=WHOLE_AREA)
        return [features_path, meta_path]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_sources_constraints_wales.py -q -p no:cacheprovider`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add src/mapgen/sources/constraints_wales.py tests/test_sources_constraints_wales.py tests/fixtures/constraints/cowbridge_conservation_areas.json tests/fixtures/constraints/cowbridge_listed_buildings.json
git commit -m "feat(constraints_wales): fetch twenty DataMapWales layers, retry once, never a failure as absence"
```

---

### Task 6: The source: merging into the package

**Files:**
- Modify: `src/mapgen/sources/constraints_wales.py` (add `merge`, `possible_outputs`, imports)
- Test: `tests/test_sources_constraints_wales.py` (append)

**Interfaces:**
- Consumes: Task 5's source and work files; `mapgen.constraints`: `LAYER_BY_TYPE`, `OUTPUT_GROUPS`, `group_collections`, `render_text`, `to_record`.
- Produces: `ConstraintsWalesSource.merge(parts, out_dir, stem) -> list[Path]` writing `<stem>_constraints_<group>.geojson` for each group with features and always `<stem>_constraints.txt`, then setting `self.constraints_record` (Task 4's `to_record` shape) and `self.attribution` (the used groups' statements joined by newlines, Cadw's date substituted); `possible_outputs(stem) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sources_constraints_wales.py`:

```python
def _fetched(tmp_path, answers):
    work = tmp_path / "work"
    work.mkdir()
    source = ConstraintsWalesSource(session=_RoutedSession(answers))
    parts = source.fetch(COWBRIDGE, _tiles(), work, _Recorder())
    out = tmp_path / "package"
    out.mkdir()
    return source, parts, out


def test_merge_writes_only_groups_with_features_and_always_the_summary(tmp_path):
    source, parts, out = _fetched(tmp_path, {
        "geonode:conservation_areas_wales": _fixture("cowbridge_conservation_areas.json"),
        "inspire-wg:Cadw_ListedBuildings": _fixture("cowbridge_listed_buildings.json"),
    })
    written = source.merge(parts, out, "Cowbridge_2026-09-18")
    names = sorted(p.name for p in written)
    assert names == [
        "Cowbridge_2026-09-18_constraints.txt",
        "Cowbridge_2026-09-18_constraints_heritage.geojson",
        "Cowbridge_2026-09-18_constraints_listed_buildings.geojson",
    ]
    heritage = json.loads((out / "Cowbridge_2026-09-18_constraints_heritage.geojson").read_text(encoding="utf-8"))
    assert {f["properties"]["name"] for f in heritage["features"]} >= {"Cowbridge"}
    assert {f["properties"]["where"] for f in heritage["features"]} <= {"site", "margin"}


def test_merge_summary_and_record(tmp_path):
    source, parts, out = _fetched(tmp_path, {
        "geonode:conservation_areas_wales": _fixture("cowbridge_conservation_areas.json"),
        "inspire-nrw:NRW_SAC": requests.ConnectionError("down"),
    })
    source.merge(parts, out, "Cowbridge_2026-09-18")
    text = (out / "Cowbridge_2026-09-18_constraints.txt").read_text(encoding="utf-8")
    lines = text.splitlines()
    today = json.loads(parts[1].read_text(encoding="utf-8"))["fetched_on"]
    assert "Confirm with the council (Vale of Glamorgan)." in text
    assert "- Special Area of Conservation: DataMapWales could not be reached" in lines
    # Never under "Checked, none found": the line after that heading.
    assert "Special Area of Conservation" not in lines[lines.index("Checked, none found") + 1]
    # Cadw's layers answered (found or not), so Cadw's statement is in
    # Sources, carrying the fetch date.
    assert f"The Welsh Historic Environment Service (Cadw), {today}," in text
    record = source.constraints_record
    assert record["checked"] is True
    assert record["summary_file"] == "Cowbridge_2026-09-18_constraints.txt"
    assert record["layers"]["inspire-nrw:NRW_SAC"]["status"] == "failed"
    assert "Natural Resources Wales" in source.attribution
    assert f"(Cadw), {today}," in source.attribution


def test_merge_with_no_work_files_writes_nothing(tmp_path):
    source = ConstraintsWalesSource(session=_RoutedSession())
    assert source.merge([], tmp_path, "X_2026-09-18") == []
    assert source.constraints_record is None


def test_possible_outputs_is_the_closed_list():
    assert ConstraintsWalesSource(session=_RoutedSession()).possible_outputs("S") == [
        "S_constraints_flood.geojson",
        "S_constraints_nature.geojson",
        "S_constraints_landscape.geojson",
        "S_constraints_heritage.geojson",
        "S_constraints_listed_buildings.geojson",
        "S_constraints.txt",
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_sources_constraints_wales.py -q -p no:cacheprovider`
Expected: the four new tests fail with `AttributeError: 'ConstraintsWalesSource' object has no attribute 'merge'`.

- [ ] **Step 3: Write the implementation**

In `src/mapgen/sources/constraints_wales.py`, extend the `mapgen.constraints` import to include `LAYER_BY_TYPE`, `OUTPUT_GROUPS`, `group_collections`, `render_text` and `to_record`, then add these methods to `ConstraintsWalesSource`:

```python
    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Every package output, from the two work files: one GeoJSON per
        group that has features, and the summary text, always. Sets
        constraints_record (read by package.py into survey.json) and
        attribution (only the groups whose layers answered, Cadw's date
        substituted)."""
        features_path = next((Path(p) for p in parts if Path(p).name == WORK_FEATURES), None)
        meta_path = next((Path(p) for p in parts if Path(p).name == WORK_META), None)
        if features_path is None or meta_path is None:
            return []
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        results = []
        for line in features_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            layer = LAYER_BY_TYPE.get(row["type_name"])
            if layer is None:
                continue
            results.append(
                LayerResult(layer=layer, ok=row["ok"], features=tuple(row["features"]), kind=row["kind"], reason=row["reason"])
            )
        site = tuple(meta["site"])
        assessment = assess(site, results, meta["margin_m"])
        summary = summarise(assessment, meta["councils"], meta["coverage"])
        out_dir = Path(out_dir)
        written: list[Path] = []
        for group, collection in group_collections(assessment).items():
            path = out_dir / f"{stem}_constraints_{group}.geojson"
            atomic_write_text(path, json.dumps(collection, separators=(",", ":")))
            written.append(path)
        summary_path = out_dir / f"{stem}_constraints.txt"
        atomic_write_text(summary_path, render_text(summary, stem, meta["fetched_on"], meta["margin_m"]))
        written.append(summary_path)
        self.constraints_record = to_record(
            summary,
            assessment,
            councils=meta["councils"],
            coverage=meta["coverage"],
            coverage_share=meta["coverage_share"],
            margin_m=meta["margin_m"],
            summary_file=summary_path.name,
            fetched_on=meta["fetched_on"],
        )
        self.attribution = "\n".join(attribution_statements(summary.attribution_groups, meta["fetched_on"]))
        return written

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root file merge() could ever write for this stem. Read by
        package.py's stale-output sweep; see sources/base.py."""
        return [f"{stem}_constraints_{group}.geojson" for group in OUTPUT_GROUPS] + [f"{stem}_constraints.txt"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_sources_constraints_wales.py tests/test_constraints.py -q -p no:cacheprovider`
Expected: all pass (13 in the source file, 29 in constraints).

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/sources/constraints_wales.py tests/test_sources_constraints_wales.py
git commit -m "feat(constraints_wales): merge into five filterable GeoJSON layers and a constraints summary"
```

---

### Task 7: Integration: registry, survey.json, resolver, picker label, docs

**Files:**
- Modify: `src/mapgen/package.py` (import; `register_default_sources` tuple near line 399; new `_constraints_record` beside `_canopy_record` near line 2094; `_build_survey_json` near line 4873 and its returned dict near line 5202)
- Modify: `src/mapgen/resolver.py:143-158` (CATEGORIES)
- Modify: `tests/test_resolver.py` (the CATEGORIES assertion near line 228)
- Modify: `src/mapgen/web/static/app.js:2848-2854` (TIER_CATEGORY_LABELS)
- Modify: `tests/js/test_app.js` (one test)
- Modify: `README.md` (a subsection under "## What a survey folder contains", before "### survey.json"; a paragraph under "## Data sources, licences and attribution")
- Modify: `docs/urbano/README.md` (a section after "## The categorised boundaries file: filtering by category in Import Geojson File")
- Test: `tests/test_package.py` (append)

**Interfaces:**
- Consumes: `ConstraintsWalesSource` (Tasks 5 and 6) and its `constraints_record` attribute.
- Produces: survey.json key `"constraints"`, always present: the source's record when it merged, else `_constraints_record()` (`checked: False`, lists empty, everything else None); resolver category `"constraints"`; picker label "planning constraints".

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_package.py`:

```python
from mapgen.package import register_default_sources  # noqa: E402
from mapgen.sources.base import available_sources  # noqa: E402
from mapgen.sources.constraints_wales import ConstraintsWalesSource  # noqa: E402


class _EmptyWfsSession:
    """Every DataMapWales layer answers an empty FeatureCollection."""

    def get(self, url, params=None, headers=None, timeout=None):
        class _Response:
            status_code = 200

            def json(self):
                return {"type": "FeatureCollection", "features": [], "numberMatched": 0}

        return _Response()


def test_constraints_wales_is_a_default_source():
    register_default_sources()
    assert "constraints_wales" in {source.id for source in available_sources()}


def test_a_survey_with_constraints_carries_the_record_and_the_summary(tmp_path):
    register(ConstraintsWalesSource(session=_EmptyWfsSession()))
    result = run_survey(_request(tmp_path, source_ids=("constraints_wales",)))
    survey = json.loads((result.paths.root / "survey.json").read_text(encoding="utf-8"))
    record = survey["constraints"]
    assert record["checked"] is True
    assert len(record["none_found"]) == 20
    assert (result.paths.root / record["summary_file"]).is_file()
    provenance = next(entry for entry in survey["sources"] if entry["id"] == "constraints_wales")
    assert "Natural Resources Wales" in provenance["attribution"]


def test_a_survey_without_constraints_says_not_checked(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    survey = json.loads((result.paths.root / "survey.json").read_text(encoding="utf-8"))
    assert survey["constraints"]["checked"] is False
    assert survey["constraints"]["summary_file"] is None
```

If `run_survey` or `register` are not already imported at the top of `tests/test_package.py`, they are: `run_survey` comes from `mapgen.package` and `register` from `mapgen.sources.base` in the existing import blocks; add either name to those blocks only if missing.

In `tests/test_resolver.py`, change the CATEGORIES assertion (near line 228) so its tuple ends with `"places",` followed by `"constraints",` (the exact existing tuple with one element appended).

Append to `tests/js/test_app.js`, before the final `console.log(`:

```javascript
  await test("the tier list names the constraints category in words", async () => {
    const { sandbox } = await bootedSandbox();
    ok(sandbox.tierCategoryLabel("constraints") === "planning constraints", sandbox.tierCategoryLabel("constraints"));
  });
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_package.py -q -p no:cacheprovider -k "constraints" ; PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_resolver.py -q -p no:cacheprovider ; node tests/js/test_app.js | tail -3`
Expected: the three package tests fail (`constraints_wales` not registered; `KeyError: 'constraints'`), the resolver CATEGORIES test fails, and the JS test fails with `constraints`.

- [ ] **Step 3: Implement**

`src/mapgen/package.py`:

1. Beside the other source imports (near line 101), add:

```python
from mapgen.sources.constraints_wales import ConstraintsWalesSource
```

2. In `register_default_sources`, extend the tuple:

```python
    for source in (
        OsmSource(), OvertureSource(), ElevationSource(), LidarWalesSource(), InspireSource(),
        OsOpenSource(), OsUprnSource(), LidarCardiffSource(), ConstraintsWalesSource(),
    ):
```

3. Directly after `_canopy_record` (near line 2102), add:

```python
def _constraints_record(**overrides) -> dict[str, object]:
    """survey.json's `constraints` block for a package whose constraints
    source never merged: `checked` False, every list empty, everything
    else None, so a reader can tell "not checked" from "checked, nothing
    found" (which has `checked` True and a none_found list)."""
    record: dict[str, object] = {
        "checked": False, "fetched_on": None, "margin_m": None, "coverage": None,
        "coverage_share": None, "councils": [], "authority": None, "on_site": [],
        "margin": [], "none_found": [], "not_checked": [], "layers": {},
        "skipped_geometry": None, "summary_file": None,
    }
    record.update(overrides)
    return record
```

4. In `_build_survey_json`, directly after the line `canopy = canopy or _canopy_record()`, add:

```python
    constraints_source = next(
        (source for source in sources if getattr(source, "id", None) == "constraints_wales"), None
    )
    constraints = getattr(constraints_source, "constraints_record", None) or _constraints_record()
```

5. In the dict `_build_survey_json` returns, directly after the `"canopy": canopy,` entry, add:

```python
        # Planning constraints (phase 2 item 4): the constraints source's
        # own record when it merged, else `checked: False`; see
        # `_constraints_record` and mapgen.constraints.to_record.
        "constraints": constraints,
```

`src/mapgen/resolver.py`: append `"constraints",` after `"places",` in `CATEGORIES`.

`src/mapgen/web/static/app.js`: add `constraints: "planning constraints",` to `TIER_CATEGORY_LABELS`.

`README.md`, directly before `### survey.json`, add:

```markdown
### Planning constraints (Wales)

A survey whose extent is in Wales also checks twenty Welsh planning
designation layers from DataMapWales over the site and a 250 m margin
around it, and writes:

- `<stem>_constraints_flood.geojson`, `_nature`, `_landscape`,
  `_heritage` (areas and scheduled monuments) and `_listed_buildings`
  (points), each only when it has something in it. Every feature carries
  `designation`, `name`, `grade_or_zone`, `reference`, `where` (`site` or
  `margin`) and `source`.
- `<stem>_constraints.txt`, a short summary under four headings: On the
  site, Within 250 m, Checked and none found, Not checked. Areas are
  given as a share of the site, listed buildings are counted by grade,
  and every source's attribution statement follows word for word.

It is indicative, from open data, and says so: confirm with the council.
Coal mining risk and Tree Preservation Orders are not in any open Welsh
service and are listed as not checked. A layer that did not answer is
listed as not checked, never as none found. The same facts are in
survey.json under `constraints`.
```

`README.md`, at the end of the "## Data sources, licences and attribution" section, add:

```markdown
The planning constraints layers are DataMapWales services from Natural
Resources Wales, Cadw and the Welsh Government, all under the Open
Government Licence v3.0. Their attribution statements differ by
publisher; the package's `<stem>_constraints.txt` reproduces each one
that applies, word for word, with Cadw's date filled in as the date the
data was fetched.
```

`docs/urbano/README.md`, directly after the categorised boundaries section, add:

```markdown
## The planning constraints files: filtering by where and designation

`<stem>_constraints_*.geojson` import through Import Geojson File like
the other GeoJSON layers. Filter on key `where`, value `site` for what
the drawn site itself touches, or `margin` for the 250 m around it; or
on key `designation` (for example `Conservation area`, `Flood zone,
rivers`, `Scheduled monument`). Listed buildings are points in their own
file. Geometry is clipped to the site plus its margin, so a designation
that runs far beyond the site stops at the margin's edge.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_package.py tests/test_resolver.py tests/test_sources_constraints_wales.py tests/test_constraints.py tests/test_wales_coverage.py tests/test_planar.py -q -p no:cacheprovider ; node tests/js/test_app.js | tail -1`
Expected: all pass; JS `ALL ... CHECKS PASSED`.

Then the full suite once: `PYTHONPATH=src /c/Python313/python.exe -m pytest -q -p no:cacheprovider` (about 2 minutes). Expected: all pass. Report the count.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/package.py src/mapgen/resolver.py tests/test_resolver.py src/mapgen/web/static/app.js tests/js/test_app.js tests/test_package.py README.md docs/urbano/README.md
git commit -m "feat(package): planning constraints for Wales in every package, survey.json and the tier list"
```

---

### Task 8: Live proof over Cowbridge

**Files:**
- Test: `tests/test_sources_constraints_wales.py` (append one live test)

**Interfaces:**
- Consumes: the finished source.

- [ ] **Step 1: Write the live test**

Append to `tests/test_sources_constraints_wales.py`:

```python
@pytest.mark.live
def test_live_cowbridge(tmp_path):
    """The real service over the real town: both conservation areas, at
    least 30 listed buildings, and no Heritage Coast, which the loose
    bbox returns and the local re-test must drop (the spec's "Testing")."""
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "package"
    out.mkdir()
    source = ConstraintsWalesSource()
    parts = source.fetch(COWBRIDGE, _tiles(), work, _Recorder())
    source.merge(parts, out, "Cowbridge_live")
    heritage = json.loads((out / "Cowbridge_live_constraints_heritage.geojson").read_text(encoding="utf-8"))
    names = {f["properties"]["name"] for f in heritage["features"] if f["properties"]["designation"] == "Conservation area"}
    assert {"Cowbridge", "Llanblethian"} <= names
    listed = json.loads((out / "Cowbridge_live_constraints_listed_buildings.geojson").read_text(encoding="utf-8"))
    assert len(listed["features"]) >= 30
    landscape_path = out / "Cowbridge_live_constraints_landscape.geojson"
    if landscape_path.exists():
        landscape = json.loads(landscape_path.read_text(encoding="utf-8"))
        assert all(f["properties"]["designation"] != "Heritage Coast" for f in landscape["features"])
    assert source.constraints_record["coverage"] == "full"
    assert all(entry["status"] == "ok" for entry in source.constraints_record["layers"].values())
```

- [ ] **Step 2: Run it**

Run: `PYTHONPATH=src /c/Python313/python.exe -m pytest tests/test_sources_constraints_wales.py -q -p no:cacheprovider -m live`
Expected: 1 passed, in roughly 20 to 60 seconds. If a layer fails, report which and the reason; do not weaken the test.

- [ ] **Step 3: Print the real summary for the report**

Run:

```bash
PYTHONPATH=src /c/Python313/python.exe - <<'EOF'
import tempfile
from pathlib import Path
from mapgen.geo import BBox, Tile
from mapgen.sources.base import NullProgress
from mapgen.sources.constraints_wales import ConstraintsWalesSource
bbox = BBox.parse("-3.46,51.455,-3.438,51.468")
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "w").mkdir(); (tmp / "o").mkdir()
    source = ConstraintsWalesSource()
    parts = source.fetch(bbox, [Tile("r00_c00", 0, 0, bbox, bbox)], tmp / "w", NullProgress())
    source.merge(parts, tmp / "o", "Cowbridge_live")
    print((tmp / "o" / "Cowbridge_live_constraints.txt").read_text(encoding="utf-8"))
EOF
```

Paste the printed summary into your report verbatim.

- [ ] **Step 4: Commit**

```bash
git add tests/test_sources_constraints_wales.py
git commit -m "test(constraints_wales): live proof over Cowbridge, including the loose-bbox Heritage Coast"
```
