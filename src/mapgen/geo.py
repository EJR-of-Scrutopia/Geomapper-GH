"""Bounding boxes, tiling, and the local planar projection.

The projection is an equirectangular approximation referenced to the centre
latitude of the study area. It is accurate at the latitudes and areas this tool
is used for. It degenerates near the poles and does not handle a bbox crossing
the antimeridian. Both are out of scope, see the phase 1 spec.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6378137.0


class BBoxError(ValueError):
    """Raised when a bounding box is malformed or geographically impossible."""


class TilingError(ValueError):
    """Raised when tile_size_m cannot produce a real tiling, or would
    produce an unreasonable one.

    A ValueError subclass deliberately: server.py's _REQUEST_VALUE_ERRORS
    already catches ValueError generically for /api/estimate, /api/jobs
    and /api/extent, so this needs no separate wiring there to become a
    clean 400 instead of an unhandled exception.
    """


@dataclass(frozen=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    @classmethod
    def parse(cls, value: str) -> "BBox":
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            raise BBoxError(
                "BBox must contain 4 comma-separated numbers: west,south,east,north"
            )
        try:
            raw_west, raw_south, raw_east, raw_north = [float(part) for part in parts]
        except ValueError as exc:
            raise BBoxError(f"Invalid bbox: {value}") from exc

        west, east = sorted((raw_west, raw_east))
        south, north = sorted((raw_south, raw_north))
        return cls(west=west, south=south, east=east, north=north).validated()

    def validated(self) -> "BBox":
        if not (-180.0 <= self.west <= 180.0 and -180.0 <= self.east <= 180.0):
            raise BBoxError("Longitude values must be between -180 and 180.")
        if not (-90.0 <= self.south <= 90.0 and -90.0 <= self.north <= 90.0):
            raise BBoxError("Latitude values must be between -90 and 90.")
        if self.west == self.east or self.south == self.north:
            raise BBoxError("BBox has zero width or height.")
        return self

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.west, self.south, self.east, self.north

    def to_dict(self) -> dict[str, float]:
        return {
            "west": round(self.west, 7),
            "south": round(self.south, 7),
            "east": round(self.east, 7),
            "north": round(self.north, 7),
        }

    def to_query_string(self) -> str:
        return ",".join(f"{value:.7f}" for value in self.as_tuple())

    @property
    def centre(self) -> tuple[float, float]:
        return (self.west + self.east) / 2.0, (self.south + self.north) / 2.0


@dataclass(frozen=True)
class Tile:
    tile_id: str
    row: int
    col: int
    core_bbox: BBox
    query_bbox: BBox

    def to_dict(self) -> dict[str, object]:
        return {
            "tile_id": self.tile_id,
            "row": self.row,
            "col": self.col,
            "core_bbox": self.core_bbox.to_dict(),
            "query_bbox": self.query_bbox.to_dict(),
        }


def lonlat_to_local_metres(lon: float, lat: float, ref_lat: float) -> tuple[float, float]:
    x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def local_metres_to_lonlat(x: float, y: float, ref_lat: float) -> tuple[float, float]:
    lon = math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(ref_lat))))
    lat = math.degrees(y / EARTH_RADIUS_M)
    return lon, lat


def extent_metres(bbox: BBox) -> tuple[float, float]:
    ref_lat = (bbox.south + bbox.north) / 2.0
    x_min, y_min = lonlat_to_local_metres(bbox.west, bbox.south, ref_lat)
    x_max, y_max = lonlat_to_local_metres(bbox.east, bbox.north, ref_lat)
    return x_max - x_min, y_max - y_min


def _clamp(bbox: BBox, bounds: BBox) -> BBox:
    return BBox(
        west=max(bounds.west, bbox.west),
        south=max(bounds.south, bbox.south),
        east=min(bounds.east, bbox.east),
        north=min(bounds.north, bbox.north),
    )


# Real single-site surveys in this tool's own reference material top out
# around a few dozen tiles (the South Wales reference package is 32).
# 10,000 is generous enough that no legitimate use is ever near it, while
# still being small enough to build in a blink: it exists purely to
# reject an accidental drag over a whole country before build_tiles's own
# nested loop ever starts constructing Tile objects for it. Review round
# 1 measured a real drag over Western Europe building 4,699,380 tiles in
# 29.9 seconds; this is checked from row/col counts alone, before any of
# that work happens, so the cost of refusing it is one multiplication.
DEFAULT_MAX_TILES = 10_000


def build_tiles(
    bbox: BBox,
    tile_size_m: float,
    overlap_m: float,
    max_tiles: int = DEFAULT_MAX_TILES,
) -> list[Tile]:
    if tile_size_m <= 0:
        raise TilingError(f"tile_size_m must be greater than zero, got {tile_size_m}.")

    ref_lat = (bbox.south + bbox.north) / 2.0
    x_min, y_min = lonlat_to_local_metres(bbox.west, bbox.south, ref_lat)
    x_max, y_max = lonlat_to_local_metres(bbox.east, bbox.north, ref_lat)

    cols = math.ceil((x_max - x_min) / tile_size_m)
    rows = math.ceil((y_max - y_min) / tile_size_m)

    total = rows * cols
    if total > max_tiles:
        raise TilingError(
            f"This extent and tile size would produce {total} tiles, over the "
            f"{max_tiles} limit. Draw a smaller extent or choose a larger tile size."
        )

    tiles: list[Tile] = []

    for row in range(rows):
        core_y_min = y_min + row * tile_size_m
        core_y_max = min(core_y_min + tile_size_m, y_max)

        for col in range(cols):
            core_x_min = x_min + col * tile_size_m
            core_x_max = min(core_x_min + tile_size_m, x_max)

            core_west, core_south = local_metres_to_lonlat(core_x_min, core_y_min, ref_lat)
            core_east, core_north = local_metres_to_lonlat(core_x_max, core_y_max, ref_lat)
            core_bbox = BBox(core_west, core_south, core_east, core_north)

            query_bbox = _clamp(
                BBox(
                    *local_metres_to_lonlat(
                        max(x_min, core_x_min - overlap_m),
                        max(y_min, core_y_min - overlap_m),
                        ref_lat,
                    ),
                    *local_metres_to_lonlat(
                        min(x_max, core_x_max + overlap_m),
                        min(y_max, core_y_max + overlap_m),
                        ref_lat,
                    ),
                ),
                bbox,
            )

            tiles.append(
                Tile(
                    tile_id=f"r{row:02d}_c{col:02d}",
                    row=row,
                    col=col,
                    core_bbox=core_bbox,
                    query_bbox=query_bbox,
                )
            )

    return tiles
