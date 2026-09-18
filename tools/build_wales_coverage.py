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
