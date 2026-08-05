"""Writing a `BngWindow` out as the one classic TIFF dialect `cog.py` reads.

`cog.py` reads two dialects: BigTIFF, for the 48 GB Welsh mosaics, and
classic, for everything mapgen itself produces. This module writes the
second half of that sentence. A packaged survey keeps `<stem>_lidar_dtm.tif`
and `<stem>_lidar_dsm.tif` after `work_dir` is gone, so `mapgen bridge` can
rebuild the elevation grid and re-fuse building heights on a finished
package with nothing but the package itself, and so the owner can sample
the 1 m surfaces directly in Grasshopper. `cog.py` is the one parser that
has to accept what comes out of here; there is no second TIFF dialect in
this project to drift, so every tag below is exactly what `cog.py`'s own
directory walk expects, in the order it expects to find it.

## Why there is exactly one image directory

A packaged raster is a window mapgen already cut to the survey's own
extent; it has no owner for the reduced-resolution levels a mosaic needs to
answer a much larger query cheaply, and writing them would be a second
resampling step nobody asked for. So this writer emits ONE directory, at
`window.pixel_size` by `window.pixel_height`, and nothing downstream of it
in the chain (the next-IFD offset is 0).

## The one thing that constrains a window with pixel_size != pixel_height

`BngWindow.pixel_height` exists because an overview level of the real
mosaic is not quite square: a window read from one carries slightly
different east/west and north/south pixel sizes (see `cog.py`'s own
docstring). Both are written into `ModelPixelScale` here, honestly, exactly
as the window carries them. But `cog.py`'s own `_read_placement` refuses
ANY file whose `ModelPixelScale` X and Y differ by more than one part in a
million, on the grounds that a single-resolution file should quote one
number for everything built from it. That check runs once, against
whichever directory is first in the file, which for everything this module
writes is the ONLY directory. A window whose two axes differ by more than
that tolerance (the real overview ratios in `cog.py`'s docstring run to
about five parts in a million at the finest overview and much more at
coarser ones) would therefore be written correctly and then refused on the
very next read. This is a real, narrow gap between what `BngWindow` can
carry and what a single classic directory can get back through `cog.py`
unmodified; it is not something this task can close without touching
`cog.py`, and every survey extent mapgen actually downloads stays at full
resolution (see `cog.py`'s `MAX_WINDOW_PIXELS`), so it is not hit in
practice. Recorded here rather than silently, so the next person who sees
a `CogError` on a very large packaged raster knows where to look.

## Nodata and the pad

`GDAL_NODATA` is always written as the literal string `-9999`, and every
NaN sample is written as the float32 `-9999.0`, matching `cog.py`'s own
`_blank_nodata`, which turns that exact float32 value back into NaN on
read. TIFF pads the last tile of a row or column out to the full tile size
and says nothing about what belongs in the pad; this writer fills it with
`-9999.0` rather than leaving whatever was already in a freshly allocated
buffer, so a tool that does not clamp to the declared image extent (this
project's own reader does, since Task 3's fix) still finds a sentinel out
there rather than noise that looks like terrain.
"""

from __future__ import annotations

import struct
import zlib
from array import array
from pathlib import Path

from mapgen.cog import BNG_EPSG, BngWindow
from mapgen.fsutil import atomic_write_bytes

# Tile geometry and the nodata sentinel. Fixed rather than configurable:
# nothing calling this writer has ever asked for a different tile size or
# a different way of marking absent data, and a second convention here is
# exactly the "second TIFF dialect" the module docstring says not to grow.
_TILE_SIZE = 256
_NODATA = -9999.0

# TIFF field types this writer uses, by their TIFF 6.0 codes.
_SHORT = 3
_LONG = 4
_ASCII = 2
_DOUBLE = 12
_TYPE_FORMATS = {_SHORT: "H", _LONG: "I", _DOUBLE: "d"}

# The tags cog.py's directory walk reads, written under the same numbers.
_IMAGE_WIDTH = 256
_IMAGE_LENGTH = 257
_BITS_PER_SAMPLE = 258
_COMPRESSION = 259
_PHOTOMETRIC_INTERPRETATION = 262
_SAMPLES_PER_PIXEL = 277
_PLANAR_CONFIGURATION = 284
_PREDICTOR = 317
_TILE_WIDTH = 322
_TILE_LENGTH = 323
_TILE_OFFSETS = 324
_TILE_BYTE_COUNTS = 325
_SAMPLE_FORMAT = 339
_MODEL_PIXEL_SCALE = 33550
_MODEL_TIEPOINT = 33922
_GEO_KEY_DIRECTORY = 34735
_GDAL_NODATA = 42113

_COMPRESSION_DEFLATE = 8
_SAMPLE_FORMAT_IEEE_FLOAT = 3

# GeoKey IDs and the two values this writer ever puts in them: projected,
# PixelIsArea, British National Grid, metres. cog.py's own _GT_MODEL_TYPE
# etc. are private to that module, so the numbers are repeated here rather
# than imported; they are GeoTIFF's own constants, not this project's.
_GT_MODEL_TYPE_KEY = 1024
_GT_RASTER_TYPE_KEY = 1025
_PROJECTED_CS_TYPE_KEY = 3072
_PROJ_LINEAR_UNITS_KEY = 3076
_MODEL_TYPE_PROJECTED = 1
_RASTER_PIXEL_IS_AREA = 1
_LINEAR_UNIT_METRE = 9001

# array('f').tobytes() uses the machine's own byte order; the header this
# writer emits always claims "II" (little endian), so a big endian machine
# has to swap before the bytes go on disk. Matches cog.py's own probe.
_MACHINE_IS_LITTLE = struct.pack("=H", 1) == b"\x01\x00"


class GeoTiffWriteError(ValueError):
    """Raised when a window is not one this writer can turn into a raster.

    A ValueError subclass for the same reason every other refusal in
    mapgen is one, naming the file and the specific value that made it
    unwritable.
    """


def write_bng_geotiff(path: Path, window: BngWindow) -> None:
    """Write `window` to `path` as a classic TIFF `cog.py` can read back.

    Tiled 256 by 256, deflate, float32, EPSG:27700 with PixelIsArea. The
    write is atomic (`fsutil.atomic_write_bytes`): `path` either does not
    exist, or exists complete, never half written.
    """
    if window.width <= 0 or window.height <= 0:
        raise GeoTiffWriteError(
            f"{path.name} was asked to hold a window {window.width} by "
            f"{window.height} pixels, which is not a raster: a raster "
            f"needs at least one pixel across and one down."
        )

    out = bytearray(b"II" + struct.pack("<HI", 42, 0))
    tile_offsets, tile_counts = _write_tiles(out, window)
    packed = _pack_entries(out, _entries(window, tile_offsets, tile_counts))
    _write_ifd(out, packed)
    atomic_write_bytes(path, bytes(out))


def _write_tiles(
    out: bytearray, window: BngWindow
) -> tuple[list[int], list[int]]:
    """Every tile's compressed bytes, appended to `out` in row-major order.

    Back to back, with no padding between tiles: nothing reads this file
    except `cog.py`'s own tile index, which needs exact offsets and byte
    counts, not alignment.
    """
    across = (window.width + _TILE_SIZE - 1) // _TILE_SIZE
    down = (window.height + _TILE_SIZE - 1) // _TILE_SIZE
    tile_offsets: list[int] = []
    tile_counts: list[int] = []
    for tile_y in range(down):
        for tile_x in range(across):
            payload = zlib.compress(_tile_bytes(window, tile_x, tile_y))
            tile_offsets.append(len(out))
            tile_counts.append(len(payload))
            out += payload
    return tile_offsets, tile_counts


def _tile_bytes(window: BngWindow, tile_x: int, tile_y: int) -> bytes:
    """One 256 by 256 float32 tile's raw bytes, little endian.

    NaN samples and the pad beyond the window's own width and height are
    both `_NODATA`, never a leftover or an uninitialised value: see the
    module docstring on why a well-formed file does not leave the pad to
    chance even though this project's own reader clamps it away.
    """
    block = array("f", [_NODATA]) * (_TILE_SIZE * _TILE_SIZE)
    left = tile_x * _TILE_SIZE
    top = tile_y * _TILE_SIZE
    rows = min(_TILE_SIZE, window.height - top)
    span = min(_TILE_SIZE, window.width - left)
    for row in range(rows):
        start = (top + row) * window.width + left
        segment = window.values[start:start + span]
        cleaned = array(
            "f", (_NODATA if value != value else value for value in segment)
        )
        block[row * _TILE_SIZE:row * _TILE_SIZE + span] = cleaned
    if not _MACHINE_IS_LITTLE:
        block.byteswap()
    return block.tobytes()


def _entries(
    window: BngWindow, tile_offsets: list[int], tile_counts: list[int]
) -> list[tuple[int, int, int, bytes]]:
    """Every IFD entry this file carries, as (tag, type, count, value bytes).

    Sorted by tag ascending before being returned, because that is a TIFF
    requirement `cog.py`'s own directory reader assumes without checking
    (as every reader is entitled to).
    """
    geo_keys = [
        1, 1, 0, 4,
        _GT_MODEL_TYPE_KEY, 0, 1, _MODEL_TYPE_PROJECTED,
        _GT_RASTER_TYPE_KEY, 0, 1, _RASTER_PIXEL_IS_AREA,
        _PROJECTED_CS_TYPE_KEY, 0, 1, BNG_EPSG,
        _PROJ_LINEAR_UNITS_KEY, 0, 1, _LINEAR_UNIT_METRE,
    ]
    nodata_bytes = f"{_NODATA:g}".encode("ascii") + b"\0"
    tile_count = len(tile_offsets)
    entries = [
        (_IMAGE_WIDTH, _LONG, 1, _packed(_LONG, [window.width])),
        (_IMAGE_LENGTH, _LONG, 1, _packed(_LONG, [window.height])),
        (_BITS_PER_SAMPLE, _SHORT, 1, _packed(_SHORT, [32])),
        (_COMPRESSION, _SHORT, 1, _packed(_SHORT, [_COMPRESSION_DEFLATE])),
        (_PHOTOMETRIC_INTERPRETATION, _SHORT, 1, _packed(_SHORT, [1])),
        (_SAMPLES_PER_PIXEL, _SHORT, 1, _packed(_SHORT, [1])),
        (_PLANAR_CONFIGURATION, _SHORT, 1, _packed(_SHORT, [1])),
        (_PREDICTOR, _SHORT, 1, _packed(_SHORT, [1])),
        (_TILE_WIDTH, _LONG, 1, _packed(_LONG, [_TILE_SIZE])),
        (_TILE_LENGTH, _LONG, 1, _packed(_LONG, [_TILE_SIZE])),
        (_TILE_OFFSETS, _LONG, tile_count, _packed(_LONG, tile_offsets)),
        (_TILE_BYTE_COUNTS, _LONG, tile_count, _packed(_LONG, tile_counts)),
        (_SAMPLE_FORMAT, _SHORT, 1, _packed(_SHORT, [_SAMPLE_FORMAT_IEEE_FLOAT])),
        (
            _MODEL_PIXEL_SCALE, _DOUBLE, 3,
            _packed(_DOUBLE, [window.pixel_size, window.pixel_height, 0.0]),
        ),
        (
            _MODEL_TIEPOINT, _DOUBLE, 6,
            _packed(
                _DOUBLE,
                [0.0, 0.0, 0.0, window.e_origin, window.n_top, 0.0],
            ),
        ),
        (_GEO_KEY_DIRECTORY, _SHORT, len(geo_keys), _packed(_SHORT, geo_keys)),
        (_GDAL_NODATA, _ASCII, len(nodata_bytes), nodata_bytes),
    ]
    entries.sort(key=lambda entry: entry[0])
    return entries


def _packed(kind: int, values: list) -> bytes:
    code = _TYPE_FORMATS[kind]
    return struct.pack("<" + code * len(values), *values)


def _pack_entries(
    out: bytearray, entries: list[tuple[int, int, int, bytes]]
) -> list[tuple[int, int, int, bytes]]:
    """Place every entry too big for its own 4 byte field, return the fields.

    A value of 4 bytes or less is left-justified into the field itself, per
    TIFF 6.0; anything larger is appended to `out` at a 2 byte aligned
    offset and the field becomes a pointer to it.
    """
    packed = []
    for tag, kind, count, blob in entries:
        if len(blob) <= 4:
            field = blob.ljust(4, b"\0")
        else:
            if len(out) % 2:
                out += b"\0"
            field = struct.pack("<I", len(out))
            out += blob
        packed.append((tag, kind, count, field))
    return packed


def _write_ifd(out: bytearray, packed: list[tuple[int, int, int, bytes]]) -> None:
    """The directory itself, plus patching the header's pointer to it."""
    if len(out) % 2:
        out += b"\0"
    struct.pack_into("<I", out, 4, len(out))
    out += struct.pack("<H", len(packed))
    for tag, kind, count, field in packed:
        out += struct.pack("<HHI", tag, kind, count) + field
    # No next directory: this writer never produces overviews (see the
    # module docstring on why there is exactly one).
    out += struct.pack("<I", 0)
