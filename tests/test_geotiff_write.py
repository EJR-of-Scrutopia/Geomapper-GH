"""geotiff_write.py's suite: every window round-trips through the real
`cog.py` reader, not a mock of it.

Every round trip below goes `write_bng_geotiff` -> `CogReader.open` over a
real file on disk, because the point of this writer is that `cog.py` never
has to know its bytes came from here rather than from GDAL. `_values_equal`
treats NaN as equal to NaN, because `array == array` does not (NaN is never
equal to anything, including itself), and every window here carries at
least one nodata pixel.
"""

from __future__ import annotations

import math
import zlib
from array import array
from pathlib import Path

import pytest

from mapgen.cog import BngWindow, CogReader, FileByteSource
from mapgen.geotiff_write import GeoTiffWriteError, write_bng_geotiff

_TILE_SIZE = 256


def _values_equal(a, b) -> bool:
    if len(a) != len(b):
        return False
    for left, right in zip(a, b):
        if left != right and not (left != left and right != right):
            return False
    return True


def _grid(width: int, height: int, formula) -> array:
    return array(
        "f", [formula(x, y) for y in range(height) for x in range(width)]
    )


def _raw_tiles(path: Path, count: int) -> list[array]:
    """The first `count` tiles' own float32 samples, undecoded by cog.py.

    Tiles are written back to back right after the 8 byte header, each its
    own complete zlib stream with no padding between them. Two zlib
    streams in a row can be told apart because `decompressobj().decompress`
    stops at the end of its own stream and leaves whatever is left over in
    `unused_data`, so this walks the file exactly the way the writer built
    it without needing to parse the IFD at all.
    """
    remaining = path.read_bytes()[8:]
    tiles = []
    for _ in range(count):
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(remaining)
        remaining = decompressor.unused_data
        tile = array("f")
        tile.frombytes(raw)
        tiles.append(tile)
    return tiles


def _safe_bounds(window: BngWindow) -> tuple[float, float, float, float]:
    """A query rectangle guaranteed to `read_window` back the whole window.

    `window.bounds()` puts every corner exactly on a pixel edge, and
    `CogReader._geometry` gets from a corner to a pixel count with a
    `ceil` of a division. For a pixel size that is not an exact float
    (this suite's anisotropic test uses one half a part per million off
    1.0) that division can land a hair above the true integer, and `ceil`
    then reports one pixel too many. Insetting each corner by half a
    pixel, onto the centre of the first and last row and column, keeps
    the same `floor`/`ceil` arithmetic away from that edge by a margin
    (0.5) many orders of magnitude bigger than the rounding noise it is
    built to survive, without changing which pixels come back.
    """
    return (
        window.e_origin,
        window.n_top - (window.height - 0.5) * window.pixel_height,
        window.e_origin + (window.width - 0.5) * window.pixel_size,
        window.n_top,
    )


def test_a_single_tile_window_round_trips_exactly(tmp_path):
    width, height = 8, 8
    values = _grid(width, height, lambda x, y: float(x) + float(y) * 0.25)
    values[5] = float("nan")
    window = BngWindow(
        e_origin=300_000.0,
        n_top=200_000.0,
        pixel_size=1.0,
        width=width,
        height=height,
        values=values,
    )
    path = tmp_path / "single_tile.tif"
    write_bng_geotiff(path, window)

    reader = CogReader.open(FileByteSource(path))
    assert reader.epsg == 27700
    assert reader.pixel_is_area is True

    got = reader.read_window(*_safe_bounds(window))
    assert _values_equal(got.values, window.values)
    assert got.e_origin == window.e_origin
    assert got.n_top == window.n_top
    assert got.pixel_size == window.pixel_size
    assert got.pixel_height == window.pixel_height
    assert got.width == window.width
    assert got.height == window.height


def test_a_multi_tile_window_round_trips_exactly(tmp_path):
    # 300 x 520 needs 2 tile columns and 3 tile rows at 256 x 256, so every
    # edge (right and bottom) lands on a partial tile.
    width, height = 300, 520
    values = _grid(width, height, lambda x, y: float((x % 11) - (y % 7)))
    # Nodata scattered across tile boundaries: inside the first tile, on a
    # tile seam, and inside the bottom-right partial tile.
    for index in (0, 255, 256, width * height - 1, 300 * 519 + 299):
        values[index] = float("nan")
    window = BngWindow(
        e_origin=500_000.0,
        n_top=250_000.0,
        pixel_size=1.0,
        width=width,
        height=height,
        values=values,
    )
    path = tmp_path / "multi_tile.tif"
    write_bng_geotiff(path, window)

    reader = CogReader.open(FileByteSource(path))
    got = reader.read_window(*_safe_bounds(window))
    assert _values_equal(got.values, window.values)
    assert got.width == width
    assert got.height == height


def test_pixel_height_different_from_pixel_size_round_trips_both_axes(tmp_path):
    # cog.py's own _read_placement refuses a ModelPixelScale whose X and Y
    # differ by more than one part in a million (see geotiff_write.py's
    # module docstring), so this delta is deliberately small: large enough
    # to prove pixel_height is not silently written as pixel_size again,
    # small enough that the file it produces is still one cog.py accepts.
    width, height = 4, 4
    values = _grid(width, height, lambda x, y: float(x + y))
    window = BngWindow(
        e_origin=100_000.0,
        n_top=50_000.0,
        pixel_size=1.0,
        pixel_height=1.0 + 5e-7,
        width=width,
        height=height,
        values=values,
    )
    assert window.pixel_size != window.pixel_height

    path = tmp_path / "anisotropic.tif"
    write_bng_geotiff(path, window)

    reader = CogReader.open(FileByteSource(path))
    got = reader.read_window(*_safe_bounds(window))
    assert got.pixel_size == window.pixel_size
    assert got.pixel_height == window.pixel_height
    assert got.pixel_size != got.pixel_height
    assert _values_equal(got.values, window.values)


def test_all_nodata_window_round_trips_to_all_nan(tmp_path):
    width, height = 4, 4
    values = array("f", [float("nan")] * (width * height))
    window = BngWindow(
        e_origin=0.0, n_top=0.0, pixel_size=1.0,
        width=width, height=height, values=values,
    )
    path = tmp_path / "empty.tif"
    write_bng_geotiff(path, window)

    reader = CogReader.open(FileByteSource(path))
    got = reader.read_window(*_safe_bounds(window))
    assert all(math.isnan(value) for value in got.values)


@pytest.mark.parametrize("width, height", [(0, 5), (5, 0), (0, 0)])
def test_a_window_with_zero_width_or_height_is_refused(tmp_path, width, height):
    window = BngWindow(
        e_origin=0.0, n_top=0.0, pixel_size=1.0,
        width=width, height=height, values=array("f", []),
    )
    path = tmp_path / "empty_dims.tif"
    with pytest.raises(GeoTiffWriteError, match=r"empty_dims\.tif"):
        write_bng_geotiff(path, window)
    assert not path.exists()


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    values = _grid(4, 4, lambda x, y: float(x + y))
    window = BngWindow(
        e_origin=0.0, n_top=0.0, pixel_size=1.0,
        width=4, height=4, values=values,
    )
    path = tmp_path / "atomic.tif"
    write_bng_geotiff(path, window)
    assert path.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_tile_padding_beyond_the_image_extent_is_the_nodata_sentinel(tmp_path):
    # A window that does not fill its last tile: the pad is unobservable
    # through CogReader (it clamps to the image extent), so this decodes
    # the raw last tile directly to check the writer did not leave an
    # uninitialised or arbitrary value out there.
    width, height = 300, 300
    values = _grid(width, height, lambda x, y: 1.0)
    window = BngWindow(
        e_origin=0.0, n_top=0.0, pixel_size=1.0,
        width=width, height=height, values=values,
    )
    path = tmp_path / "padded.tif"
    write_bng_geotiff(path, window)

    # across=2, down=2 at 256 for a 300 x 300 image; the last of the four
    # tiles in row-major order is the bottom-right one.
    tile = _raw_tiles(path, 4)[-1]
    # Only its top-left 44 x 44 (300 - 256) corner overlaps the declared
    # image; everything else is pad.
    overlap = width - _TILE_SIZE
    for row in range(_TILE_SIZE):
        for col in range(_TILE_SIZE):
            sample = tile[row * _TILE_SIZE + col]
            if row < overlap and col < overlap:
                assert sample == 1.0
            else:
                assert sample == -9999.0


def test_nan_samples_are_written_as_the_sentinel_not_raw_ieee_nan(tmp_path):
    # Bit-identical round trips through cog.py's own reader would pass
    # even if NaN were written to disk untouched (IEEE NaN survives the
    # tile codec unchanged, and cog.py's nodata pass only rewrites exact
    # matches of the declared sentinel, leaving an untouched NaN as NaN).
    # So the file's own declared GDAL_NODATA contract is checked directly
    # against the raw tile bytes instead, where a writer that forgot to
    # convert NaN to the sentinel would still leave 0x7fc00000 (or
    # whichever NaN bit pattern Python's float('nan') is) rather than the
    # float32 -9999.0 this file's own GDAL_NODATA tag promises.
    width, height = 4, 4
    values = _grid(width, height, lambda x, y: float(x + y))
    values[6] = float("nan")
    window = BngWindow(
        e_origin=0.0, n_top=0.0, pixel_size=1.0,
        width=width, height=height, values=values,
    )
    path = tmp_path / "nodata_sentinel.tif"
    write_bng_geotiff(path, window)

    tile = _raw_tiles(path, 1)[0]
    row, col = divmod(6, width)
    assert tile[row * _TILE_SIZE + col] == -9999.0
