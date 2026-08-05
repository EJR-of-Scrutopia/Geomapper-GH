"""The elevation grid, checked against Urbano's own builder and serialiser.

This is a reverse engineered binary format, so a writer tested only against
a reader written beside it proves exactly nothing. Everything load bearing
here is measured against Urbano 2.2.1.2 itself.

**Urbano's own grid.** `tests/data/urbano_grid_*.egrid` are
`ElevationExtensions.BuildElevationGridFromTiff` on the two real DEMs in this
folder, with the padding and options `ProjectSettingComponent` uses when it
writes a `.egrid`, serialised by `ProtoBuf.Serializer.Serialize`. Urbano's
bytes, from Urbano's own code, run out of process (task 39, Probe9). mapgen
builds the same grid from the same file and the two are compared node by
node, and byte by byte.

**Urbano's own wire format.** `URBANO_TINY_GRID` is what Urbano's serialiser
produced for a 2x2 grid with known values. It is the byte level fixture: a
change to the encoding that a value comparison would forgive fails here
instead, at the byte.

**Orientation.** Row zero of `Z1D` is the SOUTH edge and a GeoTIFF's row zero
is its NORTH edge, so the single most likely way to get this wrong is a grid
that renders, upside down. Two tests hold that: one on a hand built raster
with a deliberate north-south asymmetry, one on the real Welsh DEM. The
mirrored, transposed and east-west flipped versions of the real grid were
also put through Urbano's own `SampleGridBilinear` against Urbano's own
raster reader (task 39, Probe10), and where the true grid agreed at 952 of
952 points, the flipped one agreed at 48 of 920. The check discriminates.
"""

import math
import struct
from pathlib import Path

import pytest

from mapgen.bng import BngError
from mapgen.egrid import (
    ELEVATION_GRID_SUFFIX,
    ElevationGrid,
    ElevationGridError,
    _ChainSampler,
    build_grid,
    elevation_grid_path,
    encode,
    grid_geometry,
    write_elevation_grid,
    write_elevation_grid_from_sampler,
)
from mapgen.geo import BBox
from mapgen.geotiff import read_dem
from mapgen.urbano import project_zone
from tests.test_heights import _zero_shift_grid

DATA = Path(__file__).resolve().parent / "data"

# The bounds each reference grid was built for, which are the bounds of the
# survey that produced the DEM beside it.
BARRY = (
    DATA / "Barry-Full_2026-08-04.tif",
    DATA / "urbano_grid_barry.egrid",
    BBox(west=-3.276, south=51.393, east=-3.268, north=51.398),
)
PORTHCAWL = (
    DATA / "Porthcawl_2026-08-04.tif",
    DATA / "urbano_grid_porthcawl.egrid",
    BBox(
        west=-3.731231689453125,
        south=51.47005907949787,
        east=-3.6631679534912114,
        north=51.50043309550593,
    ),
)

# ProtoBuf.Serializer.Serialize of an ElevationGrid with NX=2, NY=2,
# X0=481000, Y0=5693500, DX=DY=10, Z1D=[1,2,3,4], read off Urbano's own
# serialiser (task 37 Probe6, rerun in task 39). Seventy six bytes, and every
# one of them is a claim about the wire format:
#   08 02        field 1 varint, NX
#   10 02        field 2 varint, NY
#   19 <8>       field 3 fixed64, X0
#   21 <8>       field 4 fixed64, Y0
#   29 <8>       field 5 fixed64, DX
#   31 <8>       field 6 fixed64, DY
#   39 <8> x4    field 7 fixed64, UNPACKED, one tag per height
URBANO_TINY_GRID = bytes.fromhex(
    "0802"
    "1002"
    "1900000000A05B1D41"
    "2100000000 0FB85541"
    "290000000000002440"
    "310000000000002440"
    "39000000000000F03F"
    "390000000000000040"
    "390000000000000840"
    "390000000000001040"
)


def decode(blob: bytes) -> ElevationGrid:
    """A protobuf reader, in the tests and never in src/.

    Deliberately not a public function. Its whole purpose is to disagree with
    `encode` if `encode` is wrong, and a shared implementation could not do
    that. It is also what lets Urbano's own `.egrid` files be compared with
    mapgen's field by field rather than only byte by byte.
    """
    fields: dict[int, object] = {}
    heights: list[float] = []
    at = 0
    while at < len(blob):
        tag = blob[at]
        at += 1
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value = 0
            shift = 0
            while True:
                byte = blob[at]
                at += 1
                value |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            fields[number] = value
        elif wire == 1:
            value = struct.unpack_from("<d", blob, at)[0]
            at += 8
            if number == 7:
                heights.append(value)
            else:
                fields[number] = value
        elif wire == 2:
            length = blob[at]
            at += 1
            if number != 7:
                raise AssertionError(f"unexpected length delimited field {number}")
            for offset in range(0, length, 8):
                heights.append(struct.unpack_from("<d", blob, at + offset)[0])
            at += length
        else:
            raise AssertionError(f"unexpected wire type {wire} for field {number}")
    return ElevationGrid(
        nx=fields[1],
        ny=fields[2],
        x0=fields[3],
        y0=fields[4],
        dx=fields[5],
        dy=fields[6],
        heights=heights,
    )


def _built(case):
    tif, _reference, bbox = case
    return build_grid(read_dem(tif), bbox, project_zone(bbox))


# --------------------------------------------------------------------------
# Against Urbano's own grid.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", [BARRY, PORTHCAWL], ids=["barry", "porthcawl"])
def test_the_grid_is_the_one_urbano_would_have_built(case):
    """Same dimensions, same origin, same cell size, same heights, same
    holes, from the same DEM. This is the whole claim the writer rests on:
    the file mapgen produces is the file Urbano itself would have produced
    for this site if the site had been in the United States.

    Dimensions have to be EXACT. They come out of an integer tier and cell
    count calculation, and one node either way would mean mapgen and Urbano
    had different opinions about the arithmetic rather than about the last
    digit of a double.
    """
    tif, reference, bbox = case
    mine = _built(case)
    theirs = decode(reference.read_bytes())
    assert (mine.nx, mine.ny) == (theirs.nx, theirs.ny)
    assert mine.x0 == pytest.approx(theirs.x0, abs=1e-6)
    assert mine.y0 == pytest.approx(theirs.y0, abs=1e-6)
    assert mine.dx == pytest.approx(theirs.dx, abs=1e-9)
    assert mine.dy == pytest.approx(theirs.dy, abs=1e-9)
    assert len(mine.heights) == len(theirs.heights)
    holes = 0
    worst = 0.0
    for got, want in zip(mine.heights, theirs.heights):
        if want != want or got != got:
            # A hole has to be a hole in both. One grid's NaN against the
            # other's height is a coverage disagreement, which is worse than
            # a height disagreement: it is a piece of terrain that exists in
            # one reading of the DEM and not in the other.
            assert (want != want) and (got != got)
            holes += 1
            continue
        worst = max(worst, abs(got - want))
    assert worst < 1e-6
    assert holes > 0  # both real DEMs are smaller than their padded extent


@pytest.mark.parametrize("case", [BARRY, PORTHCAWL], ids=["barry", "porthcawl"])
def test_the_bytes_are_the_bytes_urbano_writes(case):
    """The same file, to the byte, except for the last bits of a handful of
    the doubles the projection produces.

    A value comparison cannot catch a packed repeated field, a field written
    in the wrong order, or an int written as a fixed32; a byte comparison
    catches all three, which is why this exists alongside the value
    comparison rather than instead of it.

    The tolerance is a COUNT and nothing else, and it is worth being exact
    about what that count is, because it is easy to read this test as
    stricter than it is. `doubles` is the set of double-sized slots that
    differ ANYWHERE in the file, header and heights alike; the assertion
    bounds how many of them there are, not where they sit. It does not say
    the heights match exactly, and today they do not: measured against the
    checked-in fixtures, Barry differs in three doubles, all of them in the
    header, and Porthcawl in four, three in the header and one height at
    byte 136,342. Five is the bound, so there is a slot of slack in it.

    That is the intended strength. A wrong wire format moves thousands of
    bytes, not four, so this catches every structural error it was written
    for; the last-bit disagreements it tolerates are the projection's own
    nanometre of residual arriving at a pixel edge, and the value comparison
    below is what bounds those to something meaningful (1e-6 on every
    height, with NaN matched as NaN).
    """
    _tif, reference, _bbox = case
    theirs = reference.read_bytes()
    mine = encode(_built(case))
    assert len(mine) == len(theirs)
    # Byte for byte, then counted as doubles, because a differing byte is
    # only interesting as part of the number it belongs to.
    differing_bytes = [i for i in range(len(mine)) if mine[i] != theirs[i]]
    doubles = {(index - 5) // 9 for index in differing_bytes}
    assert len(doubles) <= 5, (
        f"{len(doubles)} of this grid's numbers differ from Urbano's, which "
        f"the projection's own nanometre of residual does not explain"
    )
    # And every one of those numbers still agrees. The projection is a
    # nanometre off Urbano's and the DEM is sampled bilinearly, so a node
    # within a nanometre of a pixel edge can take a hair different weight.
    ours, urbano = decode(mine), decode(theirs)
    assert ours.x0 == pytest.approx(urbano.x0, abs=1e-6)
    assert ours.dy == pytest.approx(urbano.dy, abs=1e-9)
    for got, want in zip(ours.heights, urbano.heights):
        assert (got != got) == (want != want)
        if got == got:
            assert abs(got - want) < 1e-6


def test_urbanos_own_grid_round_trips_through_mapgens_encoder():
    """Decode Urbano's file, re-encode it, and get Urbano's file back.

    This isolates the encoder from everything else: no DEM, no projection, no
    arithmetic of mapgen's at all. If these bytes differ, the wire format is
    wrong, full stop.
    """
    for _tif, reference, _bbox in (BARRY, PORTHCAWL):
        original = reference.read_bytes()
        assert encode(decode(original)) == original


# --------------------------------------------------------------------------
# The wire format, at the byte.
# --------------------------------------------------------------------------


def test_a_known_grid_encodes_to_urbanos_own_seventy_six_bytes():
    grid = ElevationGrid(
        nx=2, ny=2, x0=481000.0, y0=5693500.0, dx=10.0, dy=10.0,
        heights=[1.0, 2.0, 3.0, 4.0],
    )
    assert encode(grid) == URBANO_TINY_GRID
    assert len(URBANO_TINY_GRID) == 76


def test_the_heights_are_written_unpacked_one_tag_each():
    """protobuf-net writes a `double[]` that carries no IsPacked unpacked,
    and Urbano's own output proves it. A packed encoding would deserialise
    perfectly well and would not be what Urbano writes, which would quietly
    end the byte comparison above.
    """
    grid = ElevationGrid(
        nx=2, ny=2, x0=1.0, y0=2.0, dx=3.0, dy=4.0, heights=[0.0, 0.0, 0.0, 0.0]
    )
    blob = encode(grid)
    assert blob.count(b"\x39") >= 4
    assert blob[-36] == 0x39
    assert len(blob) == 2 + 2 + 4 * 9 + 4 * 9


def test_a_hole_is_written_as_dotnets_own_nan():
    """FFF8000000000000, which is .NET's `double.NaN`, not Python's
    7FF8000000000000. Both read back as NaN and only one of them makes a
    byte comparison against Urbano's output mean anything.
    """
    grid = ElevationGrid(
        nx=1, ny=1, x0=1.0, y0=2.0, dx=3.0, dy=4.0, heights=[math.nan]
    )
    assert encode(grid)[-8:] == bytes.fromhex("000000000000F8FF")


def test_a_height_of_zero_is_a_height_and_is_written(tmp_path):
    """Sea level is a height. A writer that skipped zeroes as protobuf's
    default would shorten the array and Urbano would rebuild a grid of the
    wrong shape from it, or none at all.
    """
    grid = ElevationGrid(
        nx=2, ny=1, x0=1.0, y0=2.0, dx=3.0, dy=4.0, heights=[0.0, 5.0]
    )
    blob = encode(grid)
    assert decode(blob).heights == [0.0, 5.0]


# --------------------------------------------------------------------------
# Orientation, which is the error that renders rather than failing.
# --------------------------------------------------------------------------


def _flat_raster(tmp_path, values, *, north=51.5, west=-3.0, step=0.001):
    from tests.test_geotiff import build_tiff

    path = tmp_path / "dem.tif"
    path.write_bytes(
        build_tiff(
            values=values,
            scale=(step, step),
            tiepoint=(0.0, 0.0, 0.0, west, north, 0.0),
        )
    )
    return read_dem(path)


def test_the_grid_is_not_upside_down(tmp_path):
    """A DEM whose north half is high and south half is low, converted, has
    to come out with the HIGH values at the top of the grid, which in Z1D is
    the END of the array.

    Z1D's row zero is the south edge; a GeoTIFF's row zero is the north edge.
    Copying raster rows into Z1D in order produces terrain mirrored north to
    south, which renders perfectly and puts every hill where a valley is.
    """
    rows = 12
    values = [[100.0 if row < rows // 2 else 0.0] * 12 for row in range(rows)]
    dem = _flat_raster(tmp_path, values)
    # Raster columns and rows 1 to 10, so both quarters sampled below sit
    # well clear of the step between the two halves.
    bbox = BBox(west=-2.999, south=51.490, east=-2.990, north=51.499)
    grid = build_grid(dem, bbox, project_zone(bbox), pad_metres=0.0)
    real = [
        (iy, grid.height_at(ix, iy))
        for iy in range(grid.ny)
        for ix in range(grid.nx)
        if grid.height_at(ix, iy) == grid.height_at(ix, iy)
    ]
    assert real
    northern = [height for iy, height in real if iy >= grid.ny * 3 // 4]
    southern = [height for iy, height in real if iy < grid.ny // 4]
    assert northern and southern
    assert min(northern) > 50.0
    assert max(southern) < 50.0


def test_the_grid_is_not_mirrored_east_to_west(tmp_path):
    """The other half of the same question, and it needs its own test: a
    writer that got the row order right by transposing would pass the one
    above and fail this.
    """
    values = [[100.0 if column < 6 else 0.0 for column in range(12)] for _ in range(12)]
    dem = _flat_raster(tmp_path, values)
    bbox = BBox(west=-2.999, south=51.490, east=-2.990, north=51.499)
    grid = build_grid(dem, bbox, project_zone(bbox), pad_metres=0.0)
    real = [
        (ix, grid.height_at(ix, iy))
        for iy in range(grid.ny)
        for ix in range(grid.nx)
        if grid.height_at(ix, iy) == grid.height_at(ix, iy)
    ]
    western = [height for ix, height in real if ix < grid.nx // 4]
    eastern = [height for ix, height in real if ix >= grid.nx * 3 // 4]
    assert western and eastern
    assert min(western) > 50.0
    assert max(eastern) < 50.0


def test_a_node_sits_where_urbanos_own_xy_says_it_does():
    """`ElevationGrid.XY(ix, iy)` is `(X0 + ix*DX, Y0 + iy*DY)`, and every
    Urbano sampler inverts exactly that to find a cell. Node zero is the
    SOUTH WEST corner, in absolute UTM metres, not the local world origin
    frame, and not the north west corner a raster would start at.
    """
    grid = _built(BARRY)
    assert grid.node(0, 0) == (grid.x0, grid.y0)
    assert grid.node(grid.nx - 1, grid.ny - 1) == (
        grid.x0 + (grid.nx - 1) * grid.dx,
        grid.y0 + (grid.ny - 1) * grid.dy,
    )
    # In UTM metres, in zone 30U, west of the central meridian.
    assert 400_000 < grid.x0 < 500_000
    assert 5_600_000 < grid.y0 < 5_800_000


def test_the_real_welsh_grid_runs_the_same_way_up_as_the_dem_it_came_from():
    """The orientation check on real, asymmetric ground, with the DEM itself
    as the standard rather than anybody's memory of Barry.

    This bbox runs from the flat around the docks in the north, at 3 to 8 m,
    up onto Barry Island in the south, at 25 to 30 m: the raster's own first
    row averages a third of what its last row does. Row zero of the grid is
    the SOUTH edge, so the grid has to come out the other way up from the
    raster, and by the same ratio.
    """
    tif, _reference, bbox = BARRY
    dem = read_dem(tif)
    raster_north = sum(dem.heights[: dem.width]) / dem.width
    raster_south = sum(dem.heights[-dem.width:]) / dem.width
    assert raster_south > raster_north + 5.0  # the asymmetry this rests on

    grid = _built(BARRY)
    rows = []
    for iy in range(grid.ny):
        real = [
            grid.height_at(ix, iy)
            for ix in range(grid.nx)
            if grid.height_at(ix, iy) == grid.height_at(ix, iy)
        ]
        if real:
            rows.append((iy, sum(real) / len(real)))
    assert len(rows) > 20
    half = len(rows) // 2
    grid_south = sum(height for _, height in rows[:half]) / half
    grid_north = sum(height for _, height in rows[half:]) / (len(rows) - half)
    assert grid_south > grid_north + 5.0


# --------------------------------------------------------------------------
# The geometry, which has to agree with Urbano before any height is sampled.
# --------------------------------------------------------------------------


def test_the_padded_extent_leaves_room_for_import_terrains_own_crop():
    """Import Terrain snaps the project's bounds to the grid's lines and
    crops, and `CropAligned` indexes the source array with NO bounds check.
    So the snapped rectangle has to land strictly inside the grid, and the
    200 m pad is what guarantees it. A grid sized exactly to the bounds puts
    `Math.Ceiling` one line past the end of the array and the owner gets an
    IndexOutOfRangeException instead of terrain.
    """
    for _tif, _reference, bbox in (BARRY, PORTHCAWL):
        from mapgen.utm import project

        zone = project_zone(bbox)
        nx, ny, x0, y0, dx, dy = grid_geometry(bbox, zone)
        left, bottom = project(bbox.south, bbox.west, zone)
        right, top = project(bbox.north, bbox.east, zone)
        # SnapBboxToGrid: floor the low corner, ceil the high one.
        assert math.floor((left - x0) / dx) >= 0
        assert math.floor((bottom - y0) / dy) >= 0
        assert math.ceil((right - x0) / dx) <= nx - 1
        assert math.ceil((top - y0) / dy) <= ny - 1


def test_the_cell_size_tier_follows_the_extent():
    """Urbano's three tiers, on extents that sit either side of each
    boundary. The tier decides how big a cell is, and getting it wrong makes
    a grid Urbano's own mesh generation was not sized for: four times the
    memory, or a quarter of the detail.
    """
    # A few hundred metres: fine, 5 to 15 m cells.
    small = BBox(west=-3.276, south=51.393, east=-3.268, north=51.398)
    # A few kilometres: medium, 15 to 50 m.
    medium = BBox(west=-3.35, south=51.39, east=-3.25, north=51.44)
    # Tens of kilometres: coarse, 50 to 200 m.
    large = BBox(west=-3.8, south=51.3, east=-3.0, north=51.6)
    for bbox, low, high in (
        (small, 5.0, 15.0), (medium, 15.0, 50.0), (large, 50.0, 200.0)
    ):
        _nx, _ny, _x0, _y0, dx, dy = grid_geometry(bbox, project_zone(bbox))
        assert low <= dx <= high
        assert low <= dy <= high


def test_the_grid_never_exceeds_urbanos_own_count_per_side():
    """8 to 4000 nodes a side, which is `AutoTierOptions`' own clamp. The
    upper bound is what stops an enormous extent asking for a gigabyte; the
    lower one is what stops a tiny one being a single cell.
    """
    tiny = BBox(west=-3.2701, south=51.3931, east=-3.2700, north=51.3932)
    nx, ny, _x0, _y0, _dx, _dy = grid_geometry(tiny, project_zone(tiny))
    assert nx >= 9 and ny >= 9
    huge = BBox(west=-6.0, south=50.0, east=-1.0, north=53.0)
    nx, ny, _x0, _y0, _dx, _dy = grid_geometry(huge, project_zone(huge))
    assert nx <= 4001 and ny <= 4001


def test_the_grid_is_built_in_the_same_zone_the_project_setting_names():
    """Urbano projects every piece of downstream geometry with
    `CoordinateReference.Utm` and samples the grid with the result, so a grid
    built in a neighbouring zone is sampled hundreds of kilometres from
    where it is. One function decides the zone, and this is the test that
    says so.
    """
    from mapgen.urbano import world_origin

    for _tif, _reference, bbox in (BARRY, PORTHCAWL):
        assert project_zone(bbox) == world_origin(bbox)["Utm"]


# --------------------------------------------------------------------------
# Refusals, and never a half written file.
# --------------------------------------------------------------------------


def test_writing_a_grid_produces_a_file_urbano_can_open(tmp_path):
    tif, _reference, bbox = BARRY
    target = tmp_path / "out.egrid"
    grid = write_elevation_grid(tif, bbox, project_zone(bbox), target)
    assert target.is_file()
    written = decode(target.read_bytes())
    assert (written.nx, written.ny) == (grid.nx, grid.ny)
    assert len(written.heights) == len(grid.heights)
    for got, want in zip(written.heights, grid.heights):
        # NaN is not equal to itself, and a hole has to survive the round
        # trip as a hole rather than as a zero.
        assert (got != got) == (want != want)
        if got == got:
            assert got == want


def test_a_dem_that_covers_none_of_the_extent_is_refused_not_written(tmp_path):
    """An .egrid of nothing but holes is worse than no .egrid: the project
    setting would name it, Urbano would read it, and Import Terrain would
    produce an empty mesh with no explanation anywhere.
    """
    tif, _reference, _bbox = BARRY
    elsewhere = BBox(west=1.0, south=52.0, east=1.008, north=52.005)
    target = tmp_path / "out.egrid"
    with pytest.raises(ElevationGridError, match="covers none of this survey"):
        write_elevation_grid(tif, elsewhere, project_zone(elsewhere), target)
    assert not target.exists()


def test_the_egrid_goes_through_the_atomic_writer_and_never_over_a_good_one(
    tmp_path, monkeypatch
):
    """A half written protobuf is a file Urbano opens and throws on, and the
    owner's next Grasshopper session is the wrong place to find that out.

    The division of labour: test_fsutil.py owns what atomic_write_bytes
    guarantees, and this owns that this write is the one making the
    guarantee. Replacing it here with a plain write_bytes passed the whole
    suite, so nothing was holding that half.

    Asserted through the consequence. A write that fails must leave whatever
    was on disk before it untouched, and the only way to make one fail on
    demand is to stand in front of the writer. A plain write_bytes both
    misses the stand-in and destroys the previous file, so it fails here
    twice over.
    """
    import mapgen.egrid as egrid_module

    tif, _reference, bbox = BARRY
    target = tmp_path / "out.egrid"
    previous = b"a previous run's perfectly good grid"
    target.write_bytes(previous)

    def refuse(_path, _payload):
        raise OSError("the disk is full")

    monkeypatch.setattr(egrid_module, "atomic_write_bytes", refuse)

    with pytest.raises(OSError, match="disk is full"):
        write_elevation_grid(tif, bbox, project_zone(bbox), target)

    assert target.read_bytes() == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.egrid"]


def test_a_file_that_is_not_a_dem_is_refused_with_the_readers_own_sentence(tmp_path):
    source = tmp_path / "notadem.tif"
    source.write_bytes(b"{\"type\": \"FeatureCollection\"}")
    bbox = BARRY[2]
    with pytest.raises(ElevationGridError, match="byte order mark"):
        write_elevation_grid(source, bbox, project_zone(bbox), tmp_path / "out.egrid")


def test_a_missing_dem_is_refused_rather_than_crashing(tmp_path):
    bbox = BARRY[2]
    with pytest.raises(ElevationGridError):
        write_elevation_grid(
            tmp_path / "nothere.tif", bbox, project_zone(bbox), tmp_path / "out.egrid"
        )


# --------------------------------------------------------------------------
# Task 8: the sampler refactor, and the LiDAR-first, DEM-fallback chain.
# --------------------------------------------------------------------------


def test_write_elevation_grid_from_sampler_equals_the_old_path_byte_for_byte(tmp_path):
    """The refactor's whole contract. `write_elevation_grid` is now a thin
    wrapper: read the tiff into a `DemRaster`, then delegate here. Feeding
    that same `DemRaster` in by hand, bypassing the wrapper, has to produce
    the exact bytes the tiff path always has, to the byte, or the two
    functions are not actually one code path any more.
    """
    tif, _reference, bbox = BARRY
    zone = project_zone(bbox)
    via_wrapper = tmp_path / "via_wrapper.egrid"
    via_sampler = tmp_path / "via_sampler.egrid"
    write_elevation_grid(tif, bbox, zone, via_wrapper)
    write_elevation_grid_from_sampler(read_dem(tif), bbox, zone, via_sampler)
    assert via_sampler.read_bytes() == via_wrapper.read_bytes()


def test_a_sampler_that_answers_nothing_is_refused_with_a_generic_sentence(tmp_path):
    """The tiff wrapper's own refusal names the file
    (test_a_dem_that_covers_none_of_the_extent_is_refused_not_written,
    above); the sampler path has no single file to blame, since a
    `_ChainSampler` speaks for a LiDAR DTM and a DEM at once, so it says so
    honestly instead of inventing one to name.
    """
    tif, _reference, _bbox = BARRY
    elsewhere = BBox(west=1.0, south=52.0, east=1.008, north=52.005)
    target = tmp_path / "out.egrid"
    with pytest.raises(ElevationGridError, match="Nothing under this survey"):
        write_elevation_grid_from_sampler(
            read_dem(tif), elsewhere, project_zone(elsewhere), target
        )
    assert not target.exists()


class _StubSampler:
    """A minimal `.sample(latitude, longitude, ...)` stand-in for
    `_ChainSampler`'s own precedence tests: it answers a fixed value, a
    fixed None, or raises a fixed exception, and it accepts (and ignores)
    the extra `grid` argument `_ChainSampler` passes to `lidar.sample` but
    not to `fallback.sample`, so the same stub serves as either half of
    the chain without needing a real `BngWindow` or `DemRaster` at all.
    """

    def __init__(self, value: float | None = None, raises: Exception | None = None):
        self.value = value
        self.raises = raises
        self.calls = 0

    def sample(self, latitude: float, longitude: float, *args) -> float | None:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.value


def test_chain_sampler_prefers_the_lidar_answer_when_it_has_one():
    lidar = _StubSampler(value=12.5)
    fallback = _StubSampler(value=99.0)
    chain = _ChainSampler(lidar=lidar, ostn15=_zero_shift_grid(), fallback=fallback)
    assert chain.sample(51.4, -3.27) == 12.5
    assert fallback.calls == 0, "the DEM must never be consulted once LiDAR answers"


def test_chain_sampler_falls_back_when_the_lidar_answers_none():
    chain = _ChainSampler(
        lidar=_StubSampler(value=None),
        ostn15=_zero_shift_grid(),
        fallback=_StubSampler(value=7.0),
    )
    assert chain.sample(51.4, -3.27) == 7.0


def test_chain_sampler_stays_a_hole_when_neither_side_answers():
    chain = _ChainSampler(
        lidar=_StubSampler(value=None),
        ostn15=_zero_shift_grid(),
        fallback=_StubSampler(value=None),
    )
    assert chain.sample(51.4, -3.27) is None


def test_chain_sampler_with_no_fallback_at_all_stays_a_hole_on_a_lidar_miss():
    """The "LiDAR alone" case `_write_elevation_grid_step` builds when a
    package has no OpenTopography DEM: `fallback` is `None` rather than a
    second sampler, and a node the LiDAR cannot answer stays a hole, the
    same as if this package had no elevation source at all.
    """
    chain = _ChainSampler(lidar=_StubSampler(value=None), ostn15=_zero_shift_grid())
    assert chain.sample(51.4, -3.27) is None


def test_chain_sampler_treats_a_bare_bngerror_as_no_lidar_here():
    """`BngWindow.sample` catches `OutsideOstn15Error` itself but not the
    plainer `BngError` its own `to_bng` can still raise for a non-finite
    input (`bng.tm_forward`'s own guard, verified by reading cog.py's
    `BngWindow.sample` and bng.py's `to_bng`/`tm_forward` directly: `sample`
    only wraps the `OutsideOstn15Error` case, so a bare `BngError` from
    `tm_forward` would otherwise propagate out of it uncaught). This chain
    catches that too, and treats it exactly like a None answer rather than
    letting one unsampleable node crash the whole grid.
    """
    chain = _ChainSampler(
        lidar=_StubSampler(raises=BngError("not a real coordinate")),
        ostn15=_zero_shift_grid(),
        fallback=_StubSampler(value=3.0),
    )
    assert chain.sample(51.4, -3.27) == 3.0


@pytest.mark.parametrize(
    "grid,message",
    [
        (
            ElevationGrid(nx=0, ny=2, x0=1, y0=2, dx=3, dy=4, heights=[]),
            "nothing in it",
        ),
        (
            ElevationGrid(nx=2, ny=2, x0=1, y0=2, dx=3, dy=4, heights=[1.0]),
            "short of describing itself",
        ),
        (
            ElevationGrid(
                nx=1, ny=1, x0=math.nan, y0=2, dx=3, dy=4, heights=[1.0]
            ),
            "X0 is nan",
        ),
        (
            ElevationGrid(nx=1, ny=1, x0=1, y0=2, dx=0.0, dy=4, heights=[1.0]),
            "zero cell size",
        ),
    ],
    ids=["no-nodes", "short-array", "nan-origin", "zero-cell"],
)
def test_a_grid_urbano_would_misread_is_refused_rather_than_encoded(grid, message):
    with pytest.raises(ElevationGridError, match=message):
        encode(grid)


def test_the_file_goes_beside_the_dem_under_the_same_stem(tmp_path):
    """`Folder + "\\" + FileNameStr + Key.ELEVATION_EXTENSION`, which is the
    name Urbano's own download route builds for itself. Getting it right is
    what makes the Project Setting component find a file already there and
    skip a download it cannot make outside the United States.
    """
    path = elevation_grid_path(tmp_path, "Barry-Full_2026-08-04")
    assert path == tmp_path / "Barry-Full_2026-08-04.egrid"
    assert ELEVATION_GRID_SUFFIX == ".egrid"


# --------------------------------------------------------------------------
# The cell size arithmetic, exercised where the real extents do not reach it.
#
# Every test below exists because a mutant survived without it. With the 200 m
# pad on, a real survey's first guess at a cell count always lands inside its
# tier and always well above the minimum, so three whole branches of Urbano's
# own helper are unreachable from a real bbox. They are still Urbano's
# behaviour, and a grid that disagreed with it on a small or a long thin
# extent would be as wrong as one that disagreed on Barry.
# --------------------------------------------------------------------------


def test_the_tier_options_are_urbanos_own_defaults():
    """`ElevationExtensions.AutoTierOptions`, constructed with no arguments,
    which is what `BuildElevationGridFromTiff` uses when it is passed none.
    Read off the decompiled class (task 39) and pinned here because they are
    the contract, not a preference: they decide how big a cell is, and a
    disagreement makes a grid Urbano's own mesh generation was not sized for.
    """
    from mapgen import egrid

    assert egrid.PAD_METRES == 200.0
    assert egrid.SMALL_MAX_METRES == 2000.0
    assert egrid.MEDIUM_MAX_METRES == 10000.0
    assert egrid.FINE_CELL_METRES == (5.0, 15.0)
    assert egrid.MEDIUM_CELL_METRES == (15.0, 50.0)
    assert egrid.COARSE_CELL_METRES == (50.0, 200.0)
    assert egrid.MIN_COUNT_PER_SIDE == 8
    assert egrid.MAX_COUNT_PER_SIDE == 4000
    assert egrid.PREFER_SQUARE_CELLS is True
    assert egrid.SAMPLE_AT_CENTRES is False


def test_a_side_too_short_for_the_tier_still_gets_urbanos_minimum_count():
    """The clamp at eight nodes a side. Unreachable with the pad on, since
    400 m of padding alone is forty six fine cells, so it is exercised with
    the pad off. Urbano clamps first and recomputes the cell size from the
    clamped count, which is what makes a tiny extent a usable grid rather
    than one cell.
    """
    tiny = BBox(west=-3.27010, south=51.39310, east=-3.27000, north=51.39320)
    nx, ny, _x0, _y0, dx, dy = grid_geometry(tiny, project_zone(tiny), pad_metres=0.0)
    assert nx == 9 and ny == 9
    assert dx < 5.0 and dy < 5.0  # below the tier's own minimum, as Urbano does


def test_the_two_recomputation_branches_are_transcribed_and_cannot_bite():
    """Urbano's helper redoes the count from the tier's floor when the first
    guess comes out too fine, and from its ceiling when it comes out too
    coarse. Both branches are here, and neither can change an answer, for
    any of the three tiers Urbano defines. That is worth saying rather than
    leaving as an untested pair of lines.

    The argument, for the floor branch: the first guess is the length over
    the geometric mean of the tier, so it only comes out below the tier's
    minimum when the count has been clamped up to eight, and the branch then
    clamps to eight again and divides by the same number. For the ceiling
    branch: it only comes out above the tier's maximum when the count has
    been clamped down to four thousand, and the branch clamps to four
    thousand again. The clamp wins either way, which is Urbano's behaviour
    and is why a tiny extent gets cells finer than its tier and an enormous
    one gets cells coarser.

    Both are exercised below, so the transcription is at least run.
    """
    from mapgen.egrid import (
        FINE_CELL_METRES, MAX_COUNT_PER_SIDE, MIN_COUNT_PER_SIDE, _cell_count
    )

    # Through the floor: thirty metres in fine tier.
    count, size = _cell_count(30.0, *FINE_CELL_METRES)
    assert count == MIN_COUNT_PER_SIDE
    assert size == pytest.approx(30.0 / MIN_COUNT_PER_SIDE)
    assert size < FINE_CELL_METRES[0]

    # Through the ceiling: a hundred kilometres in fine tier.
    count, size = _cell_count(100_000.0, *FINE_CELL_METRES)
    assert count == MAX_COUNT_PER_SIDE
    assert size == pytest.approx(100_000.0 / MAX_COUNT_PER_SIDE)
    assert size > FINE_CELL_METRES[1]

    # And in between, where neither branch fires, the cell is the geometric
    # mean of the tier to within the rounding of one count.
    count, size = _cell_count(1000.0, *FINE_CELL_METRES)
    assert FINE_CELL_METRES[0] <= size <= FINE_CELL_METRES[1]
    assert size == pytest.approx(math.sqrt(5.0 * 15.0), rel=0.01)


def test_the_tier_is_picked_off_the_longer_side_of_the_extent():
    """`Math.Max(num5, num6)` in Urbano's own code. A long thin extent takes
    the tier its LENGTH earns, not the one its width does, so the two sides
    are divided by the same rule and the cells stay square. Picking the
    shorter side would give a 12 km by 500 m survey fine 5 to 15 m cells
    along its length: 1600 nodes across, four times the file and four times
    the mesh.
    """
    thin = BBox(west=-3.35, south=51.3930, east=-3.18, north=51.3975)
    zone = project_zone(thin)
    nx, ny, _x0, _y0, dx, dy = grid_geometry(thin, zone)
    assert dx > 50.0, "an extent this long is coarse tier on its longer side"
    assert dy > 50.0, "and both sides take the same tier"
    assert nx < 400


def test_square_cells_are_preferred_the_way_urbano_prefers_them():
    """`PreferSquareCells`: once the easting step is fixed, the northing
    count is retried as the height divided by the EASTING step, and taken if
    the result is still inside the tier. Without it the two steps are picked
    independently and the cells come out visibly rectangular.

    On this extent the preference moves the northing count by one node and
    brings the two steps from 0.29 m apart to 0.01 m. Both real surveys land
    on the same answer either way, which is exactly why this needs an extent
    of its own: measured over 81 extents, 28 of them are changed by it.
    """
    from mapgen import egrid

    oblong = BBox(west=-3.30, south=51.3930, east=-3.295, north=51.4130)
    zone = project_zone(oblong)
    _nx, ny, _x0, _y0, dx, dy = grid_geometry(oblong, zone)

    original = egrid.PREFER_SQUARE_CELLS
    try:
        egrid.PREFER_SQUARE_CELLS = False
        _nx2, ny2, _x02, _y02, dx2, dy2 = grid_geometry(oblong, zone)
    finally:
        egrid.PREFER_SQUARE_CELLS = original

    assert dx == dx2, "the easting step is picked first and is not affected"
    assert ny != ny2, (
        "this extent has to be one the preference changes, or the test below "
        "is not testing anything"
    )
    assert abs(dx - dy) < abs(dx2 - dy2) / 10.0


def test_a_height_no_float_can_hold_is_written_at_full_precision():
    """`Z1D` is a `double[]`, and encode writes doubles. Heights that come
    out of the DEM have already been through single precision, so a writer
    that quietly rounded again would be invisible on a real grid; a caller
    handing this a full precision height would silently lose it.
    """
    exact = 0.1  # not representable in single precision
    grid = ElevationGrid(
        nx=1, ny=1, x0=1.0, y0=2.0, dx=3.0, dy=4.0, heights=[exact]
    )
    assert struct.unpack("<d", encode(grid)[-8:])[0] == exact
