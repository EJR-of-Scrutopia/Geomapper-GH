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
