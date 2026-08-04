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


def split_tile_into_quarters(tile: Tile) -> list[Tile]:
    """One tile cut into a 2x2 grid of quarters, standing in the same
    relationship to their parent that build_tiles' own tiles stand in to
    the whole extent: the four core_bboxes tile the parent's core exactly,
    and each query_bbox is its core widened by the parent's own overlap and
    clamped, never reaching past the ground the parent itself would have
    asked for.

    Used by OsmSource when a tile exceeds the map API's node cap: the
    quarters are fetched in its place and merged back into the parent's own
    file, so nothing outside that module has to know a split happened.

    Two properties this arithmetic exists to guarantee, both pinned by
    tests in test_geo.py:

    - The four query_bboxes UNION to exactly the parent's query_bbox, so
      the recombined tile covers the same ground as an unsplit fetch of it,
      with no hairline gap at the outer edge. Guaranteed by construction
      rather than by arithmetic that happens to come out even: each outer
      side is copied straight off the parent, which is the same value
      widen-then-clamp would have produced, without the float round trip.
    - No quarter ever reaches OUTSIDE the parent's query_bbox, so a split
      cannot quietly pull in ground the plan never asked for.

    The interior seams then overlap by the parent's own overlap on both
    sides. Worth being exact about what that does and does not buy, since
    it is easy to assume the seams depend on it. They do not: it is the
    exact cover above that leaves no gap, and it is mapgen.merge.
    merge_osm_xml, which keys elements on type and id exactly as the
    whole-tile merge already does, that leaves no duplicate. Measured
    directly by removing the overlap here and rerunning
    test_the_recombined_tile_holds_exactly_what_an_unsplit_fetch_would_
    have in test_sources_osm.py, which still passes: the OSM map API
    returns every way whole, including its nodes outside the requested
    box, so a way lying across a seam comes back complete from whichever
    piece holds one of its nodes.

    The overlap is kept anyway, for the same reason build_tiles has one:
    it is this project's one convention about seams, and it costs a ring
    of ground that is deduplicated away rather than a correctness
    argument that has to hold for a service to be relied on to return
    features whole.

    Every value is a plain degree, no projection: build_tiles works in
    local metres, but both of its conversions (see lonlat_to_local_metres)
    are linear per axis, so the midpoint of a side in degrees IS its
    midpoint in metres and an overlap measured in degrees is the same
    overlap measured in metres. Doing it in degrees keeps the outer edges
    bit-for-bit identical to the parent's rather than a round trip away
    from them, and needs no reference latitude, which a tile does not carry
    and could only guess at.
    """
    core, query = tile.core_bbox, tile.query_bbox
    mid_lon = (core.west + core.east) / 2.0
    mid_lat = (core.south + core.north) / 2.0

    # The parent's overlap, read back off the tile rather than passed in:
    # a Tile carries no overlap_m, only the two boxes it produced. An edge
    # tile's query_bbox is clamped to the extent on the outward side (see
    # build_tiles), so that side reads 0 and the inward side carries the
    # real figure; max() recovers it. A tile with no margin at all on
    # either side (a single-tile extent, or a hand-built Tile whose two
    # boxes are the same object) genuinely has no overlap to inherit, and
    # the quarters then meet edge to edge, which is still an exact cover.
    # The 0.0 floor matters only for a malformed tile whose query does not
    # contain its core, which build_tiles cannot produce: no overlap is a
    # safe reading of it, an inverted seam is not.
    overlap_lon = max(core.west - query.west, query.east - core.east, 0.0)
    overlap_lat = max(core.south - query.south, query.north - core.north, 0.0)

    quarters: list[Tile] = []
    for row_offset in (0, 1):
        for col_offset in (0, 1):
            core_bbox = BBox(
                west=core.west if col_offset == 0 else mid_lon,
                south=core.south if row_offset == 0 else mid_lat,
                east=mid_lon if col_offset == 0 else core.east,
                north=mid_lat if row_offset == 0 else core.north,
            )
            query_bbox = BBox(
                # An outer side is the parent's own value, verbatim. An
                # inner side is the seam widened by the overlap, held
                # inside the parent in case the overlap is wider than the
                # half tile it is being added to.
                west=(
                    query.west
                    if col_offset == 0
                    else max(query.west, mid_lon - overlap_lon)
                ),
                south=(
                    query.south
                    if row_offset == 0
                    else max(query.south, mid_lat - overlap_lat)
                ),
                east=(
                    query.east
                    if col_offset == 1
                    else min(query.east, mid_lon + overlap_lon)
                ),
                north=(
                    query.north
                    if row_offset == 1
                    else min(query.north, mid_lat + overlap_lat)
                ),
            )
            quarters.append(
                Tile(
                    # Suffixed, never a bare rNN_cNN: a quarter is not a
                    # tile of the plan, and package.py's _existing_output_
                    # files must be able to tell the difference. The suffix
                    # chain also names the ancestry, so r00_c00_q10_q01
                    # says which tile of the plan a piece two levels down
                    # came from and where in it, in the file name alone.
                    tile_id=f"{tile.tile_id}_q{row_offset}{col_offset}",
                    # The row and column this quarter would have in a grid
                    # of twice the resolution. Nothing reads these today
                    # (a quarter never reaches survey.json or the map's
                    # tile grid); they are consistent rather than zero so
                    # that anything which does read them later gets a
                    # truthful answer.
                    row=tile.row * 2 + row_offset,
                    col=tile.col * 2 + col_offset,
                    core_bbox=core_bbox,
                    query_bbox=query_bbox,
                )
            )
    return quarters


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
