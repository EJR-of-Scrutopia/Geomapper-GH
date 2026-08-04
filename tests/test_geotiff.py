"""The DEM reader, checked against Urbano's own reader and against files
built here to be wrong in one specific way each.

Two standards, because a reader tested only against files it also wrote
proves nothing:

**Urbano's own numbers.** `tests/data/urbano_samples_*.txt` is
`TiffExtensions.ReadTiffFile.CreateDemSourceFromTiff` run out of process on
the two real OpenTopography DEMs on this machine, at 169 points each, by
Probe9 in a scratch directory (task 39). Nothing in those tables was computed
by anything in this repository, and the two real files are checked into
`tests/data/` unchanged so the comparison can be rerun.

**Files built to be refused.** Everything else here is a TIFF assembled by
`build_tiff` with one thing about it changed: a compression mapgen does not
implement, a predictor, a datum that is not WGS84, an area sampled raster
rather than a point sampled one. Each has to be refused with a sentence,
because a DEM read with the wrong convention reaches the owner's canvas as
terrain rather than as an error.
"""

import math
import struct
import zlib
from pathlib import Path

import pytest

from mapgen.geotiff import GeoTiffError, read_dem

DATA = Path(__file__).resolve().parent / "data"
BARRY = DATA / "Barry-Full_2026-08-04.tif"
PORTHCAWL = DATA / "Porthcawl_2026-08-04.tif"


# --------------------------------------------------------------------------
# Building a TIFF to be wrong in one way.
# --------------------------------------------------------------------------


def build_tiff(
    values,
    *,
    width=None,
    scale=(0.001, 0.001),
    tiepoint=(0.0, 0.0, 0.0, -3.0, 51.5, 0.0),
    compression=1,
    predictor=None,
    sample_format=3,
    bits=32,
    samples_per_pixel=1,
    planar=None,
    model_type=2,
    raster_type=2,
    datum=4326,
    nodata=None,
    geokeys=True,
    tile=None,
    rows_per_strip=None,
    big_endian=False,
    magic=42,
) -> bytes:
    """A minimal single strip (or single tile) GeoTIFF, little endian.

    Every keyword is here so a test can change exactly one thing and assert
    the reader notices, which is the only way to know a check is doing work
    rather than being carried by its neighbours.
    """
    end = ">" if big_endian else "<"
    height = len(values)
    width = width if width is not None else len(values[0])
    flat = [v for row in values for v in row]
    if bits == 32:
        raw = b"".join(struct.pack(end + "f", v) for v in flat)
    else:
        raw = b"".join(struct.pack(end + "h", int(v)) for v in flat)
    if tile is not None:
        tile_w, tile_h = tile
        padded = []
        for row in range(tile_h):
            for column in range(tile_w):
                inside = row < height and column < width
                padded.append(values[row][column] if inside else 0.0)
        if bits == 32:
            raw = b"".join(struct.pack(end + "f", v) for v in padded)
        else:
            raw = b"".join(struct.pack(end + "h", int(v)) for v in padded)
    if compression in (8, 32946):
        raw = zlib.compress(raw)

    entries = [
        (256, 3, [width]),
        (257, 3, [height]),
        (258, 3, [bits]),
        (259, 3, [compression]),
        (277, 3, [samples_per_pixel]),
    ]
    if planar is not None:
        entries.append((284, 3, [planar]))
    if predictor is not None:
        entries.append((317, 3, [predictor]))
    if sample_format is not None:
        entries.append((339, 3, [sample_format]))
    entries.append((33550, 12, [scale[0], scale[1], 0.0]))
    entries.append((33922, 12, list(tiepoint)))
    if geokeys:
        keys = [1, 1, 0, 3]
        keys += [1024, 0, 1, model_type]
        keys += [1025, 0, 1, raster_type]
        keys += [2048, 0, 1, datum]
        entries.append((34735, 3, keys))
    if nodata is not None:
        entries.append((42113, 2, str(nodata).encode("ascii") + b"\0"))

    # Two passes: the offsets of the out-of-line values depend on how many
    # entries there are, and the strip or tile data goes after all of them.
    count = len(entries) + (4 if tile is not None else 3)
    directory_at = 8
    directory_size = 2 + count * 12 + 4
    pool_at = directory_at + directory_size

    pool = bytearray()

    def stow(kind, payload):
        nonlocal pool
        if len(payload) <= 4:
            return payload.ljust(4, b"\0"), None
        at = pool_at + len(pool)
        pool += payload
        if len(pool) % 2:
            pool += b"\0"
        return struct.pack(end + "I", at), at

    packed = []
    codes = {3: "H", 4: "I", 12: "d"}
    for tag, kind, value in entries:
        if kind == 2:
            payload = value
            length = len(value)
        else:
            payload = b"".join(struct.pack(end + codes[kind], v) for v in value)
            length = len(value)
        field, _ = stow(kind, payload)
        packed.append((tag, kind, length, field))

    data_at = pool_at + len(pool)
    if tile is not None:
        packed.append((322, 3, 1, struct.pack(end + "H", tile[0]).ljust(4, b"\0")))
        packed.append((323, 3, 1, struct.pack(end + "H", tile[1]).ljust(4, b"\0")))
        packed.append((324, 4, 1, struct.pack(end + "I", data_at)))
        packed.append((325, 4, 1, struct.pack(end + "I", len(raw))))
    elif rows_per_strip is not None:
        # Two arrays of `strips` longs each, which do not fit in the tag's own
        # four bytes, so they go in the pool. Their room is claimed before
        # data_at is fixed and the real offsets are patched in afterwards.
        stride = bits // 8 * width
        strips = (height + rows_per_strip - 1) // rows_per_strip
        offsets_field, offsets_at = stow(4, b"\0" * 4 * strips)
        counts_field, counts_at = stow(4, b"\0" * 4 * strips)
        data_at = pool_at + len(pool)
        at = data_at
        for index in range(strips):
            rows = min(rows_per_strip, height - index * rows_per_strip)
            struct.pack_into(end + "I", pool, offsets_at - pool_at + index * 4, at)
            struct.pack_into(
                end + "I", pool, counts_at - pool_at + index * 4, rows * stride
            )
            at += rows * stride
        packed.append((273, 4, strips, offsets_field))
        packed.append(
            (278, 3, 1, struct.pack(end + "H", rows_per_strip).ljust(4, b"\0"))
        )
        packed.append((279, 4, strips, counts_field))
    else:
        packed.append((273, 4, 1, struct.pack(end + "I", data_at)))
        packed.append((278, 3, 1, struct.pack(end + "H", height).ljust(4, b"\0")))
        packed.append((279, 4, 1, struct.pack(end + "I", len(raw))))
    packed.sort(key=lambda row: row[0])

    out = bytearray()
    out += b"MM" if big_endian else b"II"
    out += struct.pack(end + "H", magic)
    out += struct.pack(end + "I", directory_at)
    out += struct.pack(end + "H", len(packed))
    for tag, kind, length, field in packed:
        out += struct.pack(end + "HHI", tag, kind, length) + field
    out += struct.pack(end + "I", 0)
    assert len(out) == pool_at, (len(out), pool_at)
    out += pool
    assert len(out) == data_at, (len(out), data_at)
    out += raw
    return bytes(out)


def write(tmp_path, name="dem.tif", **kwargs) -> Path:
    path = tmp_path / name
    path.write_bytes(build_tiff(**kwargs))
    return path


RAMP = [[float(row * 10 + column) for column in range(4)] for row in range(3)]


# --------------------------------------------------------------------------
# The real files, against Urbano's own reader.
# --------------------------------------------------------------------------


def _urbano_samples(name):
    """The recorded output of Urbano's own DEM sampler on one real file.

    lat, lon, has_coverage, sampled, height. `None` where Urbano said it
    could not sample there.
    """
    rows = []
    for line in (DATA / name).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        latitude, longitude, coverage, ok, height = line.split()
        rows.append(
            (
                float(latitude),
                float(longitude),
                coverage == "True",
                ok == "True",
                None if height == "-" else struct.unpack(
                    "<f", struct.pack("<f", float(height))
                )[0],
            )
        )
    return rows


@pytest.mark.parametrize(
    "tif,samples",
    [(BARRY, "urbano_samples_barry.txt"), (PORTHCAWL, "urbano_samples_porthcawl.txt")],
    ids=["barry", "porthcawl"],
)
def test_every_sample_agrees_with_urbanos_own_reader(tif, samples):
    """The check that matters. Urbano's `CreateDemSourceFromTiff` was run on
    these exact files, out of process, at 169 points each, and every answer
    it gave is reproduced here exactly: the same coverage decision, the same
    "cannot sample here", and the same height to the last bit of a single
    precision float.

    Exactly, not approximately. Urbano's sampler returns a `float` and
    mapgen rounds its own bilinear result back to single precision for that
    reason, so any disagreement at all is a disagreement about the data
    rather than about arithmetic.
    """
    dem = read_dem(tif)
    rows = _urbano_samples(samples)
    assert len(rows) == 169
    for latitude, longitude, coverage, ok, height in rows:
        assert dem.has_coverage(latitude, longitude) is coverage
        got = dem.sample(latitude, longitude)
        assert (got is not None) is ok
        if ok:
            assert got == height


def test_the_real_dems_are_what_this_reader_was_written_for():
    """The measurement the reader's narrowness rests on. If a future
    OpenTopography download is a different shape this fails here, in a test
    that says what changed, rather than in a refusal on the owner's desk.
    """
    barry = read_dem(BARRY)
    assert (barry.width, barry.height) == (29, 18)
    assert barry.nodata is None
    assert barry.transform[1] == pytest.approx(1.0 / 3600.0)
    assert barry.transform[5] == pytest.approx(-1.0 / 3600.0)
    porthcawl = read_dem(PORTHCAWL)
    assert (porthcawl.width, porthcawl.height) == (245, 109)
    assert porthcawl.nodata is None


def test_the_sea_is_a_height_and_not_a_hole():
    """COP30 fills water rather than voiding it, and the whole of a coastal
    survey turns on mapgen agreeing. Porthcawl's Bristol Channel is 7,839
    pixels of exactly 0.0 m; if any of them were read as nodata the terrain
    under the shoreline would vanish and every building on it would drop to
    the model's zero plane.
    """
    dem = read_dem(PORTHCAWL)
    zeroes = sum(1 for height in dem.heights if height == 0.0)
    holes = sum(1 for height in dem.heights if height != height)
    assert zeroes == 7839
    assert holes == 0


# --------------------------------------------------------------------------
# Reading, on files built here.
# --------------------------------------------------------------------------


def test_an_uncompressed_strip_dem_reads_its_own_values(tmp_path):
    dem = read_dem(write(tmp_path, values=RAMP))
    assert (dem.width, dem.height) == (4, 3)
    assert list(dem.heights) == [0, 1, 2, 3, 10, 11, 12, 13, 20, 21, 22, 23]


def test_a_deflate_dem_reads_the_same_values_as_an_uncompressed_one(tmp_path):
    plain = read_dem(write(tmp_path, "a.tif", values=RAMP))
    packed = read_dem(write(tmp_path, "b.tif", values=RAMP, compression=8))
    assert list(plain.heights) == list(packed.heights)


def test_the_lzw_in_the_real_files_decodes_to_the_full_raster():
    """LZW is the one compression that had to be written out, and a decoder
    that is subtly wrong produces plausible noise rather than an error. Both
    real files unpack to exactly their pixel count with no NaN in them,
    which a wrong early-change rule does not survive.
    """
    for tif, size in ((BARRY, 29 * 18), (PORTHCAWL, 245 * 109)):
        dem = read_dem(tif)
        assert len(dem.heights) == size
        assert not any(height != height for height in dem.heights)
        assert min(dem.heights) >= -500.0
        assert max(dem.heights) < 9000.0


def test_a_tiled_dem_reads_the_image_out_of_the_padded_tile(tmp_path):
    """A tile is padded out to its full size, so reading it as if it were
    scanlines gives the right first row and nonsense after it. Both real
    files are one 256 by 256 tile holding a 29 by 18 and a 245 by 109 image.
    """
    dem = read_dem(write(tmp_path, values=RAMP, tile=(8, 8)))
    assert list(dem.heights) == [0, 1, 2, 3, 10, 11, 12, 13, 20, 21, 22, 23]


def test_a_multi_strip_dem_reads_every_strip(tmp_path):
    """Four rows in two strips of two. A reader that took the first strip
    for the whole image would return two rows of heights and two of zeroes,
    which on a real DEM is half a survey at sea level.
    """
    values = [[float(row * 10 + column) for column in range(3)] for row in range(4)]
    one = read_dem(write(tmp_path, "one.tif", values=values))
    two = read_dem(write(tmp_path, "two.tif", values=values, rows_per_strip=2))
    assert list(two.heights) == list(one.heights)
    assert list(two.heights)[-3:] == [30.0, 31.0, 32.0]


def test_an_uneven_last_strip_is_read_at_its_real_length(tmp_path):
    """Five rows in strips of two leaves a last strip of one. A reader that
    assumed every strip was full would read past the end of it.
    """
    values = [[float(row * 10 + column) for column in range(3)] for row in range(5)]
    dem = read_dem(write(tmp_path, values=values, rows_per_strip=2))
    assert list(dem.heights)[-3:] == [40.0, 41.0, 42.0]


def test_a_big_endian_dem_is_read_forwards(tmp_path):
    dem = read_dem(write(tmp_path, values=RAMP, big_endian=True))
    assert list(dem.heights) == [0, 1, 2, 3, 10, 11, 12, 13, 20, 21, 22, 23]


def test_signed_sixteen_bit_samples_read_as_heights(tmp_path):
    dem = read_dem(
        write(tmp_path, values=[[1.0, 2.0], [3.0, 4.0]], bits=16, sample_format=2)
    )
    assert list(dem.heights) == [1.0, 2.0, 3.0, 4.0]


# --------------------------------------------------------------------------
# Placing, sampling and nodata.
# --------------------------------------------------------------------------


def test_the_tie_point_is_the_centre_of_the_first_sample(tmp_path):
    """These rasters declare RasterPixelIsPoint, so pixel 0,0 IS the tie
    point, not half a pixel south east of it. Half a pixel of COP30 is 15 m,
    which is a whole building.
    """
    dem = read_dem(write(tmp_path, values=RAMP, tiepoint=(0, 0, 0, -3.0, 51.5, 0)))
    assert dem.pixel_at(51.5, -3.0) == (0.0, 0.0)
    assert dem.sample(51.5, -3.0) == 0.0


def test_sampling_interpolates_between_the_four_surrounding_samples(tmp_path):
    dem = read_dem(write(tmp_path, values=[[0.0, 10.0], [20.0, 30.0]]))
    # Halfway along the first row, in longitude only.
    assert dem.sample(51.5, -3.0 + 0.0005) == pytest.approx(5.0)
    # The centre of the cell.
    assert dem.sample(51.5 - 0.0005, -3.0 + 0.0005) == pytest.approx(15.0)


def test_a_point_outside_the_raster_is_none_and_not_a_number(tmp_path):
    """None, never a zero. "No data here" and "sea level" are different
    statements and a grid that confuses them drops the coastline.
    """
    dem = read_dem(write(tmp_path, values=RAMP))
    assert dem.sample(51.5 + 0.01, -3.0) is None
    assert dem.sample(51.5, -3.0 - 0.01) is None
    assert dem.has_coverage(51.5 + 0.01, -3.0) is False


def test_the_last_row_and_column_are_outside_coverage(tmp_path):
    """A bilinear sample needs the pixel to the right and the row below, so
    the sampleable area stops one short of the raster. Urbano's own reader
    does the same, and a grid built on the other assumption reads past the
    end of the array.
    """
    dem = read_dem(write(tmp_path, values=RAMP))
    assert dem.has_coverage(51.5 - 0.002, -3.0) is False
    assert dem.has_coverage(51.5, -3.0 + 0.003) is False
    assert dem.has_coverage(51.5 - 0.0019, -3.0 + 0.0029) is True


def test_a_declared_nodata_value_becomes_a_hole_and_never_a_height(tmp_path):
    dem = read_dem(
        write(tmp_path, values=[[1.0, -9999.0], [3.0, 4.0]], nodata=-9999.0)
    )
    assert dem.nodata == -9999.0
    assert math.isnan(dem.heights[1])


def test_a_nodata_corner_is_dropped_from_the_interpolation_not_averaged_in(tmp_path):
    """Urbano gives a nodata corner zero weight and renormalises, so the
    height at a point near three real corners is a blend of those three. A
    reader that let the sentinel through would produce a spike of thousands
    of metres; one that treated the corner as zero would produce a pit.
    """
    dem = read_dem(
        write(tmp_path, values=[[10.0, -9999.0], [10.0, 10.0]], nodata=-9999.0)
    )
    height = dem.sample(51.5 - 0.0005, -3.0 + 0.0005)
    assert height == pytest.approx(10.0)


def test_a_point_whose_four_corners_are_all_nodata_cannot_be_sampled(tmp_path):
    dem = read_dem(
        write(
            tmp_path,
            values=[[-9999.0, -9999.0], [-9999.0, -9999.0]],
            nodata=-9999.0,
        )
    )
    assert dem.sample(51.5 - 0.0005, -3.0 + 0.0005) is None


def test_a_nodata_tag_that_is_not_a_number_is_ignored_rather_than_fatal(tmp_path):
    dem = read_dem(write(tmp_path, values=RAMP, nodata="nan"))
    assert dem.nodata is None
    assert list(dem.heights)[0] == 0.0


# --------------------------------------------------------------------------
# Refusals. Every one of these is a file that would otherwise be misread.
# --------------------------------------------------------------------------


def test_a_compression_this_does_not_implement_is_refused_by_name(tmp_path):
    path = write(tmp_path, values=RAMP, compression=32773)
    with pytest.raises(GeoTiffError, match="PackBits"):
        read_dem(path)


def test_a_predictor_is_refused_rather_than_ignored(tmp_path):
    """Decoding predicted samples as unpredicted gives heights that look
    like a landscape and are not one, which is the exact failure mode this
    whole module exists to avoid.
    """
    path = write(tmp_path, values=RAMP, compression=8, predictor=2)
    with pytest.raises(GeoTiffError, match="predictor 2"):
        read_dem(path)


def test_a_raster_that_is_not_in_wgs84_is_refused(tmp_path):
    path = write(tmp_path, values=RAMP, datum=4277)
    with pytest.raises(GeoTiffError, match="not WGS84"):
        read_dem(path)


def test_a_projected_raster_is_refused_rather_than_read_as_degrees(tmp_path):
    """Urbano's own reader falls back to assuming WGS84 when it finds no WKT
    in the GDAL metadata, so a projected DEM would have its eastings read as
    degrees and land in the Gulf of Guinea. mapgen refuses instead.
    """
    path = write(tmp_path, values=RAMP, model_type=1)
    with pytest.raises(GeoTiffError, match="not a geographic raster"):
        read_dem(path)


def test_an_area_sampled_raster_is_refused_rather_than_read_half_a_pixel_out(tmp_path):
    path = write(tmp_path, values=RAMP, raster_type=1)
    with pytest.raises(GeoTiffError, match="RasterPixelIsArea"):
        read_dem(path)


def test_a_raster_with_no_geo_keys_at_all_is_refused(tmp_path):
    path = write(tmp_path, values=RAMP, geokeys=False)
    with pytest.raises(GeoTiffError, match="no GeoTIFF key directory"):
        read_dem(path)


def test_a_raster_with_no_placement_is_refused(tmp_path):
    body = bytearray(build_tiff(values=RAMP))
    # Blank the ModelTiepoint tag number so the reader cannot find it.
    body = bytes(body).replace(struct.pack("<H", 33922), struct.pack("<H", 65000), 1)
    path = tmp_path / "dem.tif"
    path.write_bytes(body)
    with pytest.raises(GeoTiffError, match="pixel scale and tie point"):
        read_dem(path)


def test_a_sample_format_this_cannot_read_is_refused(tmp_path):
    path = write(tmp_path, values=RAMP, sample_format=1)
    with pytest.raises(GeoTiffError, match="format 1"):
        read_dem(path)


def test_a_file_that_does_not_declare_its_sample_format_is_refused(tmp_path):
    """TIFF's own default here is unsigned integer and Urbano's is IEEE
    float, so a file that leaves it out means different things to the two
    readers. mapgen will not choose.
    """
    path = write(tmp_path, values=RAMP, sample_format=None)
    with pytest.raises(GeoTiffError, match="does not declare a sample format"):
        read_dem(path)


def test_a_multi_band_raster_is_refused(tmp_path):
    path = write(tmp_path, values=RAMP, samples_per_pixel=3)
    with pytest.raises(GeoTiffError, match="samples per pixel"):
        read_dem(path)


def test_a_planar_raster_is_refused(tmp_path):
    path = write(tmp_path, values=RAMP, planar=2)
    with pytest.raises(GeoTiffError, match="separate planes"):
        read_dem(path)


def test_a_bigtiff_is_refused_by_name(tmp_path):
    path = write(tmp_path, values=RAMP, magic=43)
    with pytest.raises(GeoTiffError, match="BigTIFF"):
        read_dem(path)


def test_something_that_is_not_a_tiff_at_all_is_refused(tmp_path):
    path = tmp_path / "not.tif"
    path.write_bytes(b"{\"type\": \"FeatureCollection\"}")
    with pytest.raises(GeoTiffError, match="byte order mark"):
        read_dem(path)


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "empty.tif"
    path.write_bytes(b"")
    with pytest.raises(GeoTiffError, match="too short"):
        read_dem(path)


def test_truncated_sample_data_is_refused_rather_than_padded(tmp_path):
    body = bytearray(build_tiff(values=RAMP))
    path = tmp_path / "dem.tif"
    path.write_bytes(bytes(body[:-8]))
    with pytest.raises(GeoTiffError):
        read_dem(path)


def test_lzw_that_never_starts_with_a_clear_code_is_refused(tmp_path):
    """A corrupt LZW stream must not decode to something. The first code in
    a TIFF LZW strip is always 256.
    """
    from mapgen.geotiff import _lzw_decode

    with pytest.raises(GeoTiffError, match="clear code"):
        _lzw_decode(b"\x00\x00\x00\x00", 16)


def test_a_raster_larger_than_the_limit_is_refused_before_it_is_read(tmp_path):
    body = bytearray(build_tiff(values=RAMP))
    # Claim 30000 by 30000 pixels without carrying them.
    body = bytes(body).replace(
        struct.pack("<HHI", 256, 3, 1) + struct.pack("<H", 4) + b"\0\0",
        struct.pack("<HHI", 256, 3, 1) + struct.pack("<H", 30000) + b"\0\0",
        1,
    ).replace(
        struct.pack("<HHI", 257, 3, 1) + struct.pack("<H", 3) + b"\0\0",
        struct.pack("<HHI", 257, 3, 1) + struct.pack("<H", 30000) + b"\0\0",
        1,
    )
    path = tmp_path / "dem.tif"
    path.write_bytes(body)
    with pytest.raises(GeoTiffError, match="pixel limit"):
        read_dem(path)
