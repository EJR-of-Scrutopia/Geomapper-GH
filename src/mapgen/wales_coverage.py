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
