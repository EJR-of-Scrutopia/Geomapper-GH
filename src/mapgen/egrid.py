"""Writing Urbano's `.egrid`, so a UK survey gets terrain.

`ElevationFilePath` in the project setting is passed straight to
`ProtoBuf.Serializer.Deserialize<ElevationGrid>` by every one of the seven
Urbano 2.2.1.2 components that reads it, Import Terrain included (task 37).
So terrain reaches Grasshopper as a protobuf elevation grid or not at all.
Urbano's own way of getting one is to download a USGS 3DEP raster, which is
United States only; mapgen already has a DEM for anywhere in the world, and
this turns it into the same file.

## The contract, from the assembly

`Urbano.Core.Data.ElevationGrid`, decompiled with ilspycmd (task 39):

    [ProtoContract(SkipConstructor = true)] sealed class ElevationGrid
      [ProtoMember(1)] int      NX      nodes across, easting
      [ProtoMember(2)] int      NY      nodes up, northing
      [ProtoMember(3)] double   X0      easting of node 0, UTM metres
      [ProtoMember(4)] double   Y0      northing of node 0, UTM metres
      [ProtoMember(5)] double   DX      easting step, metres
      [ProtoMember(6)] double   DY      northing step, metres
      [ProtoMember(7)] double[] Z1D     NX*NY heights, metres
      [ProtoIgnore]    double[,] Z      rebuilt from Z1D after reading

Three things about it are not in the field list and are what make the file
right or wrong.

**The frame is absolute UTM metres**, in the zone the project setting's
`CoordinateReference.Utm` names, not the local world origin frame. Every
consumer proves it the same way: `UrbanoGhHelpers.ImportGeometriesFromFeatures`
projects a feature to UTM, calls `SampleGridBilinear(grid, x, y)` with that,
and only afterwards applies the transform to the world origin. So `X0` and
`Y0` are a full easting and northing, in the millions.

**Row zero is the SOUTH edge.** `[ProtoAfterDeserialization]` fills
`Z[NY, NX]` from `Z1D` with the northing index outer and the easting index
inner, and `ElevationGrid.XY(ix, iy)` returns `(X0 + ix*DX, Y0 + iy*DY)`.
`ToRhinoMesh` puts vertex `[i, j]` at `XY(j, i)` with height `Z[i, j]`. DY is
positive, so increasing `i` goes north. A GeoTIFF's row zero is its NORTH
edge, so a writer that copies raster rows straight into `Z1D` produces
terrain mirrored north to south. That is the single most likely way to get
this wrong, and it renders rather than failing.

**Nodata is `double.NaN`.** `ToRhinoMesh` gives a NaN vertex a height of zero
and then omits every face touching it, so a NaN is a hole in the mesh rather
than a spike. `SampleGridBilinear` returns NaN when any corner is NaN and
falls back to a nearest-real-neighbour search within three cells, and every
caller checks `IsFinite` before using the result. Nothing else in the file
means "no data", and a sentinel height would be read as ground.

## Why this reproduces Urbano's own builder rather than improving on it

`ProjectSettingComponent`, on the route that writes a `.egrid`, calls
`ElevationExtensions.BuildElevationGridFromTiff(bounds, 200.0, utm, tiffPath)`
and serialises the result. `build_grid` below is that method, transcribed:
the same 200 m pad, the same three cell size tiers, the same square cell
preference, the same node-aligned corner, and the same
`HasCoverage`/`TryGetElevation` sampling through mapgen.geotiff.

Reproducing it rather than devising something better buys three things:

  * The file mapgen writes is the file Urbano itself would have written for
    this site if the site had been in the United States. That is a claim
    that can be checked, and it was: `tests/test_egrid.py` compares against
    Urbano's own builder run out of process on the same two real DEMs.
  * `Import Terrain` does not read the grid whole. It snaps the project's
    bounds to the grid's own lines and crops, and `CropAligned` indexes the
    source array with no bounds check at all. The 200 m pad is what
    guarantees the crop lands inside. A grid sized exactly to the survey
    bounds would put `Math.Ceiling` one line past the end of the array on a
    rounding, and the owner would get an IndexOutOfRangeException instead of
    terrain.
  * The cell sizes stay in the range Urbano's own mesh generation was built
    for, between 5 and 200 m depending on extent, rather than at the DEM's
    own 30 m everywhere.

## What the wire format is

protobuf-net writes standard proto2. Varints for the two ints, little endian
fixed64 for the doubles, and `Z1D` UNPACKED, one `0x39` tag per element,
which is what protobuf-net does with a `double[]` that carries no
`IsPacked` (verified by round tripping a grid through Urbano's own
serialiser: a 2x2 grid comes out as 76 bytes, `08 02 10 02 19 .. 21 .. 29 ..
31 .. 39 .. 39 .. 39 .. 39 ..`).

NaN is written as `FFF8000000000000`, which is .NET's own `double.NaN` and
not Python's, so that the bytes mapgen writes are the bytes Urbano writes.
Any NaN would deserialise the same; this one makes a byte comparison mean
something.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path

from mapgen.bng import BngError, Ostn15Grid
from mapgen.cog import BngWindow
from mapgen.fsutil import atomic_write_bytes
from mapgen.geo import BBox
from mapgen.geotiff import DemRaster, GeoTiffError, read_dem
from mapgen.utm import ProjectionError, project, unproject

ELEVATION_GRID_SUFFIX = ".egrid"

# Urbano's `ElevationExtensions.TerrainDefaults.PadMeters`, and the value
# ProjectSettingComponent passes on the one route that writes a .egrid. Not a
# margin of comfort: see the module docstring on CropAligned.
PAD_METRES = 200.0

# `ElevationExtensions.AutoTierOptions`, constructed with no arguments, which
# is what BuildElevationGridFromTiff does when it is passed no options.
# SmallMaxMeters and MediumMaxMeters pick the tier off the longer side of the
# padded extent; each tier is a (minimum, maximum) cell size in metres.
SMALL_MAX_METRES = 2000.0
MEDIUM_MAX_METRES = 10000.0
FINE_CELL_METRES = (5.0, 15.0)
MEDIUM_CELL_METRES = (15.0, 50.0)
COARSE_CELL_METRES = (50.0, 200.0)
MIN_COUNT_PER_SIDE = 8
MAX_COUNT_PER_SIDE = 4000
# AutoTierOptions leaves SampleAtCenters at C#'s default of false, so the
# nodes sit on the padded rectangle's own corner rather than half a cell in.
SAMPLE_AT_CENTRES = False
PREFER_SQUARE_CELLS = True

# .NET's `double.NaN`, which is the negative quiet NaN, where Python's
# float("nan") packs as the positive one. Both read back as NaN; this one
# makes mapgen's bytes comparable with Urbano's.
_DOTNET_NAN = struct.pack("<Q", 0xFFF8000000000000)

_TAG_NX = b"\x08"
_TAG_NY = b"\x10"
_TAG_X0 = b"\x19"
_TAG_Y0 = b"\x21"
_TAG_DX = b"\x29"
_TAG_DY = b"\x31"
_TAG_Z = b"\x39"


class ElevationGridError(RuntimeError):
    """Raised when a usable elevation grid cannot be produced.

    A RuntimeError rather than a ValueError, matching ProjectSettingError:
    the caller records it and finishes the package, because a survey without
    terrain is the package the owner has had all along and is worth far more
    than no package at all.
    """


@dataclass(frozen=True)
class ElevationGrid:
    """One `ElevationGrid`, in Urbano's own field names and units.

    `heights` is `Z1D`: NX*NY values, northing index outer and easting index
    inner, so `heights[iy * nx + ix]` is the height at
    `(x0 + ix*dx, y0 + iy*dy)`, and index zero is the SOUTH WEST corner.
    NaN means no data.
    """

    nx: int
    ny: int
    x0: float
    y0: float
    dx: float
    dy: float
    heights: list[float]

    def height_at(self, ix: int, iy: int) -> float:
        """The height at a node, by Urbano's own indexing. For tests and for
        anyone reading this file who has to check an orientation by hand."""
        return self.heights[iy * self.nx + ix]

    def node(self, ix: int, iy: int) -> tuple[float, float]:
        """`ElevationGrid.XY(ix, iy)`: where a node is, in UTM metres."""
        return (self.x0 + ix * self.dx, self.y0 + iy * self.dy)

    @property
    def real_count(self) -> int:
        return sum(1 for height in self.heights if height == height)

    @property
    def height_bounds(self) -> tuple[float, float] | None:
        """The (min, max) of every real (non-NaN) height on the grid, or
        None when every node is NaN (which `write_elevation_grid_from_
        sampler` already refuses before it ever writes anything, via
        `_NoCoverageError` below; this stays honest about the empty case
        anyway rather than assuming that guard is the only caller).

        Unrounded, matching `real_count`'s own plain count with no
        formatting applied at this layer: survey.json's own record is
        where the 2-decimal rounding for Grasshopper's benefit happens,
        not here.
        """
        finite = [height for height in self.heights if height == height]
        if not finite:
            return None
        return min(finite), max(finite)


def _cell_count(
    length: float, smallest: float, largest: float
) -> tuple[int, float]:
    """How many cells to divide a side into, and how big they come out.

    Urbano's private helper inside `ElevationExtensions`, transcribed. The
    first guess is the geometric mean of the tier's two cell sizes; if that
    lands outside the tier the count is redone from whichever end it fell
    off. `round` is Python's banker's rounding, which is what C#'s
    `Math.Round(double)` is too, so the two agree on a half.
    """
    target = math.sqrt(smallest * largest)
    count = int(round(length / max(1e-9, target)))
    count = min(max(max(1, count), MIN_COUNT_PER_SIDE), MAX_COUNT_PER_SIDE)
    size = length / count
    if size < smallest:
        count = int(math.floor(length / smallest))
        count = min(max(max(1, count), MIN_COUNT_PER_SIDE), MAX_COUNT_PER_SIDE)
        size = length / max(1, count)
    elif size > largest:
        count = int(math.ceil(length / largest))
        count = min(max(max(1, count), MIN_COUNT_PER_SIDE), MAX_COUNT_PER_SIDE)
        size = length / max(1, count)
    return count, size


def _tier(span: float) -> tuple[tuple[float, float], tuple[float, float]]:
    if span <= SMALL_MAX_METRES:
        return FINE_CELL_METRES, FINE_CELL_METRES
    if span <= MEDIUM_MAX_METRES:
        return MEDIUM_CELL_METRES, MEDIUM_CELL_METRES
    return COARSE_CELL_METRES, COARSE_CELL_METRES


def grid_geometry(
    bbox: BBox, zone: str, pad_metres: float = PAD_METRES
) -> tuple[int, int, float, float, float, float]:
    """Where the grid's nodes go: nx, ny, x0, y0, dx, dy.

    Split out from `build_grid` because it is the half that has to agree with
    Urbano exactly and can be checked without a DEM in the room.

    The two corners are `LatLongToUTM(bottom, left)` and
    `LatLongToUTM(top, right)`, which is what both `BuildElevationGridFromTiff`
    and `DownloadImportTiffComponent` use, so the grid mapgen writes and the
    rectangle Import Terrain crops out of it are measured from the same two
    points. Note that these are not the same two corners
    `mapgen.urbano.world_origin` uses: the world origin takes both BOTTOM
    corners, because it is defining an axis, and this takes the diagonal,
    because it is defining a rectangle. Both are Urbano's own choices.
    """
    left_easting, bottom_northing = project(bbox.south, bbox.west, zone)
    right_easting, top_northing = project(bbox.north, bbox.east, zone)
    min_x = left_easting - pad_metres
    min_y = bottom_northing - pad_metres
    max_x = right_easting + pad_metres
    max_y = top_northing + pad_metres
    width = max(0.0, max_x - min_x)
    height = max(0.0, max_y - min_y)
    if width <= 0.0 or height <= 0.0:
        raise ElevationGridError(
            f"This survey's extent projects to a rectangle {width:.1f} m by "
            f"{height:.1f} m in zone {zone}, which is not a surface anything "
            f"can be sampled onto."
        )
    x_tier, y_tier = _tier(max(width, height))
    x_count, dx = _cell_count(width, *x_tier)
    y_count, dy = _cell_count(height, *y_tier)
    if PREFER_SQUARE_CELLS and dx > 0.0 and height > 0.0:
        candidate = int(round(height / dx))
        candidate = min(
            max(max(1, candidate), MIN_COUNT_PER_SIDE), MAX_COUNT_PER_SIDE
        )
        square = height / candidate
        if y_tier[0] <= square <= y_tier[1]:
            y_count, dy = candidate, square
    nx = x_count if SAMPLE_AT_CENTRES else x_count + 1
    ny = y_count if SAMPLE_AT_CENTRES else y_count + 1
    x0 = min_x + 0.5 * dx if SAMPLE_AT_CENTRES else min_x
    y0 = min_y + 0.5 * dy if SAMPLE_AT_CENTRES else min_y
    return nx, ny, x0, y0, dx, dy


def build_grid(
    dem: DemRaster, bbox: BBox, zone: str, pad_metres: float = PAD_METRES
) -> ElevationGrid:
    """The grid Urbano's own `BuildElevationGridFromTiff` would have built.

    Each node is projected out of UTM back to latitude and longitude and the
    DEM is asked what is under it, which is the direction Urbano goes too. It
    is the only direction that works: the grid is regular in UTM and the
    raster is regular in degrees, and the two are not the same rectangle.

    A node the DEM cannot answer for becomes a NaN, never a zero. On a
    coastal Welsh survey a good third of the padded extent is outside the
    DEM's own coverage, and every one of those nodes has to read as absent
    rather than as sea level.
    """
    nx, ny, x0, y0, dx, dy = grid_geometry(bbox, zone, pad_metres)
    heights: list[float] = []
    for iy in range(ny):
        northing = y0 + iy * dy
        for ix in range(nx):
            easting = x0 + ix * dx
            latitude, longitude = unproject(easting, northing, zone)
            height = dem.sample(latitude, longitude)
            heights.append(math.nan if height is None else height)
    return ElevationGrid(nx=nx, ny=ny, x0=x0, y0=y0, dx=dx, dy=dy, heights=heights)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def encode(grid: ElevationGrid) -> bytes:
    """One `ElevationGrid` as protobuf-net writes it.

    Field order and packing are protobuf-net's, not a choice: ascending field
    number, and `Z1D` unpacked with its own tag per element. A packed
    encoding would deserialise too, and would not be what Urbano produces, so
    a byte comparison against Urbano's own output would stop meaning
    anything.

    Refuses a grid that cannot be encoded honestly rather than writing one
    Urbano would misread: a negative or zero dimension, a heights array that
    is not exactly NX*NY long, and a non-finite corner or step. A NaN HEIGHT
    is fine and expected; a NaN in the geometry is not, because it makes
    every index Urbano computes from it a NaN.
    """
    if grid.nx <= 0 or grid.ny <= 0:
        raise ElevationGridError(
            f"An elevation grid of {grid.nx} by {grid.ny} nodes has nothing in "
            f"it to write."
        )
    if len(grid.heights) != grid.nx * grid.ny:
        raise ElevationGridError(
            f"This elevation grid says it is {grid.nx} by {grid.ny} nodes and "
            f"carries {len(grid.heights)} heights, which is "
            f"{grid.nx * grid.ny - len(grid.heights)} short of describing "
            f"itself."
        )
    for name, value in (
        ("X0", grid.x0), ("Y0", grid.y0), ("DX", grid.dx), ("DY", grid.dy)
    ):
        if not math.isfinite(value):
            raise ElevationGridError(
                f"This elevation grid's {name} is {value}, so every index "
                f"Urbano computes from it would be one too."
            )
    if grid.dx == 0.0 or grid.dy == 0.0:
        raise ElevationGridError(
            "This elevation grid has a zero cell size, which Urbano's own "
            "samplers answer with a NaN for every point on it."
        )
    out = bytearray()
    out += _TAG_NX + _varint(grid.nx)
    out += _TAG_NY + _varint(grid.ny)
    out += _TAG_X0 + struct.pack("<d", grid.x0)
    out += _TAG_Y0 + struct.pack("<d", grid.y0)
    out += _TAG_DX + struct.pack("<d", grid.dx)
    out += _TAG_DY + struct.pack("<d", grid.dy)
    for height in grid.heights:
        out += _TAG_Z
        out += _DOTNET_NAN if height != height else struct.pack("<d", height)
    return bytes(out)


def elevation_grid_path(root: Path, stem: str) -> Path:
    """Where the `.egrid` goes: beside the DEM, under the same stem.

    The same name Urbano's own download route builds for itself,
    `Folder + "\\" + FileNameStr + Key.ELEVATION_EXTENSION`, so the Project
    Setting component finds a file already there and skips a download it
    cannot make outside the United States.
    """
    return Path(root) / f"{stem}{ELEVATION_GRID_SUFFIX}"


class _NoCoverageError(ElevationGridError):
    """The `real_count == 0` refusal, tagged so `write_elevation_grid`'s
    wrapper can restate it with the tiff's own name.

    Task 8 split the one-tiff writer into a sampler-generic builder
    (`write_elevation_grid_from_sampler`) and a thin tiff wrapper around
    it (`write_elevation_grid`). Both refuse an all-holes grid, and both
    have to keep the sentence every test since task 39 already pins:
    `write_elevation_grid`'s names the tiff, and a sampler has no single
    file to name (a `_ChainSampler` speaks for a LiDAR DTM and a DEM at
    once). Rather than two independent copies of the refusal drifting
    apart, there is one check, in `write_elevation_grid_from_sampler`,
    and this subclass is how its caller tells "the grid came back empty"
    apart from any other `ElevationGridError` well enough to restate it
    honestly, without every other caller of `write_elevation_grid_from_
    sampler` having to know this type exists: it is still, and is caught
    as, a plain `ElevationGridError` everywhere else.
    """

    def __init__(self, node_count: int) -> None:
        self.node_count = node_count
        super().__init__(
            f"Nothing under this survey's {node_count} node elevation grid "
            f"could be sampled, so writing it would put a hole in "
            f"Grasshopper where the terrain should be."
        )


def write_elevation_grid_from_sampler(
    sampler, bbox: BBox, zone: str, target: Path
) -> ElevationGrid:
    """Build a grid from anything with `.sample(latitude, longitude) ->
    float | None` and write it beside `target`. Returns the grid.

    `build_grid`'s own loop already only ever calls `.sample()` (see its
    docstring), so nothing about the arithmetic changes for a sampler that
    is not a `DemRaster`: today that is `_ChainSampler`, below, which
    tries a Welsh LiDAR DTM before falling back to a DEM, but any other
    object honouring the same one method works identically, including in
    a test.

    `write_elevation_grid` is this function with one extra step in front,
    turning a tiff into a `DemRaster` first, which is what makes it a
    THIN wrapper rather than a second copy: every refusal below this line
    is written once and read by both callers.

    Written atomically, like everything else mapgen puts in a package: a
    half written protobuf is a file Urbano opens and throws on, and the
    owner's next Grasshopper session is the wrong place to find that out.

    Refuses an all-holes grid (`real_count == 0`) exactly as the tiff path
    always has, via `_NoCoverageError`: an `.egrid` of nothing but holes is
    worse than none, and Import Terrain would build an empty mesh with no
    explanation anywhere.
    """
    try:
        grid = build_grid(sampler, bbox, zone)
    except ProjectionError as exc:
        raise ElevationGridError(str(exc)) from None
    if grid.real_count == 0:
        raise _NoCoverageError(len(grid.heights))
    atomic_write_bytes(Path(target), encode(grid))
    return grid


def write_elevation_grid(tiff: Path, bbox: BBox, zone: str, target: Path) -> ElevationGrid:
    """Convert a DEM GeoTIFF into a `.egrid` beside it. Returns the grid.

    A thin wrapper: read the tiff into a `DemRaster`, then delegate to
    `write_elevation_grid_from_sampler`, which is where the geometry, the
    refusal and the atomic write all actually live now (Task 8). The one
    thing this layer still owns is the tiff-specific wording of the
    all-holes refusal, which every test since task 39 pins to the exact
    sentence below: `_NoCoverageError` carries the node count back up so
    this can restate it naming the file, rather than the generic sentence
    a sampler with no single file to blame gets instead.

    Every refusal below the covers arrives here as an ElevationGridError with
    the original sentence in it, so the caller records one kind of failure
    rather than three.
    """
    try:
        dem = read_dem(tiff)
    except GeoTiffError as exc:
        raise ElevationGridError(str(exc)) from None
    except OSError as exc:
        raise ElevationGridError(f"{Path(tiff).name} could not be read: {exc}") from None
    try:
        return write_elevation_grid_from_sampler(dem, bbox, zone, target)
    except _NoCoverageError as exc:
        raise ElevationGridError(
            f"{Path(tiff).name} covers none of this survey's extent, so every "
            f"one of the {exc.node_count} nodes of the elevation grid would "
            f"be empty. Writing it would put a hole in Grasshopper where the "
            f"terrain should be."
        ) from None


@dataclass(frozen=True)
class _ChainSampler:
    """Samples a Welsh LiDAR DTM first, and falls back to an
    OpenTopography DEM, or to a hole, where the LiDAR cannot answer.

    Built by `package.py`'s `_write_elevation_grid_step`, which decides
    which of a package's rasters exist and hands them here; this class
    only knows how to combine two things that can each answer `.sample`,
    not how a package is laid out. `fallback` is `None` for a package
    that has the LiDAR DTM and no OpenTopography DEM at all (a Welsh
    survey that never selected `elevation`, or one whose key or quota
    failed): the chain then has one link, and a node the LiDAR cannot
    answer stays a hole exactly as it would with no elevation source in
    the package at all.
    """

    lidar: BngWindow
    ostn15: Ostn15Grid
    fallback: DemRaster | None = None

    def sample(self, latitude: float, longitude: float) -> float | None:
        """LiDAR first, the DEM second, and never both for the same node.

        `BngWindow.sample` catches `OutsideOstn15Error` itself, for a
        point beyond OSTN15's own 701 by 1251 node rectangle, but not the
        plainer `BngError` its own `to_bng` can still raise for a
        non-finite latitude or longitude (`bng.tm_forward`'s own guard).
        Every latitude and longitude this chain is ever asked for comes
        out of `unproject`, which never produces one, but the catch sits
        here anyway rather than being assumed away: a point this chain
        cannot place is a point to fall back on, on the same footing as
        the OSTN15-coverage None right beside it, never a crash.
        """
        try:
            height = self.lidar.sample(latitude, longitude, self.ostn15)
        except BngError:
            height = None
        if height is not None:
            return height
        if self.fallback is None:
            return None
        # The one place this chain accepts being wrong, and the whole of
        # what it accepts: COP30 fills water with a flat plane on its own
        # EGM-family vertical datum rather than voiding it (see
        # geotiff.py's own docstring on nodata), and the LiDAR DTM is
        # Ordnance Datum Newlyn; the two do not agree to the millimetre at
        # the seam between them. This fallback only ever fires where the
        # LiDAR has nothing to say, over the sea and beyond the mosaic's
        # own edge, and there COP30's filled water plane is exactly the
        # value phase 1 already shipped, so nothing a real survey has been
        # reading for months changes because of it.
        return self.fallback.sample(latitude, longitude)
