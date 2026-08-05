"""Reading mapgen's own DEM GeoTIFF, narrowly, with nothing but the standard
library.

This exists to feed `mapgen.egrid`, which turns the DEM into the elevation
grid Urbano reads. It is not a TIFF library and is not trying to be one: it
reads exactly the shape the files mapgen downloads actually have, and refuses
everything else with a sentence rather than guessing.

## What the real files are

Measured (task 39) on the two real OpenTopography COP30 downloads on this
machine, `Barry-Full_2026-08-04.tif` and the owner's own
`Porthcawl_2026-08-04.tif`, with a hand written IFD dump. They agree in every
respect:

    little endian classic TIFF   29x18 and 245x109 pixels
    BitsPerSample 32            SampleFormat 3 (IEEE float)
    SamplesPerPixel 1           PlanarConfiguration 1
    Compression 5 (LZW)         Predictor 1 (none)
    tiled, 256x256              one tile, padded
    ModelPixelScale 1/3600 deg  ModelTiepoint at pixel 0,0
    GeoKeys: model type 2 (geographic), datum 4326, raster type 2 (point)
    no GDAL_NODATA tag at all

So LZW is the one compression that had to be implemented; deflate is
supported too because it is three lines of `zlib` and is the other thing
GDAL commonly writes. Everything else is refused.

## Nodata, which is not what it was expected to be

Neither real file carries a GDAL_NODATA tag, and neither contains a
sentinel. The sea is a real height: Porthcawl's Bristol Channel is 7,839
pixels of exactly 0.0 m, and Barry's enclosed docks are 80 pixels of exactly
3.0 m, which is a flattened water surface, not a hole. COP30 fills water
rather than voiding it.

The danger here is therefore the opposite of the expected one. Inventing a
sentinel, or treating 0.0 as "no data", would punch a hole through the whole
of a coastal survey's sea and drop every building on the shoreline to the
model's zero plane. So: nodata is honoured only when the file itself declares
it, in GDAL_NODATA, and a NaN pixel is nodata because IEEE says so. Nothing
else is ever treated as absent.

## Why the sampler is Urbano's and not a better one

`sample` reproduces `TiffExtensions.ReadTiffFile`'s bilinear interpolation
exactly, down to rounding the result back to single precision, because the
grid mapgen writes has to be the grid Urbano itself would have written from
the same file. See mapgen.egrid's own docstring for what that buys. The one
place this deliberately differs is spelled out at `_sample_pixel`.

The pixel indexing looks half a pixel off and is not: these rasters declare
`RasterPixelIsPoint`, so the tie point IS the centre of pixel 0,0 and an
integer pixel coordinate lands on a sample rather than on a corner.
`RasterPixelIsArea` is refused rather than read with the wrong convention.
"""

from __future__ import annotations

import math
import struct
import zlib
from array import array
from dataclasses import dataclass
from pathlib import Path

# Tags this reader knows about. Anything else in the IFD is skipped, which is
# safe: a tag that changes how the samples are laid out is in this list, so an
# unknown one cannot silently change the meaning of the data.
_IMAGE_WIDTH = 256
_IMAGE_LENGTH = 257
_BITS_PER_SAMPLE = 258
_COMPRESSION = 259
_STRIP_OFFSETS = 273
_SAMPLES_PER_PIXEL = 277
_ROWS_PER_STRIP = 278
_STRIP_BYTE_COUNTS = 279
_PLANAR_CONFIGURATION = 284
_PREDICTOR = 317
_TILE_WIDTH = 322
_TILE_LENGTH = 323
_TILE_OFFSETS = 324
_TILE_BYTE_COUNTS = 325
_SAMPLE_FORMAT = 339
_MODEL_PIXEL_SCALE = 33550
_MODEL_TIEPOINT = 33922
_MODEL_TRANSFORMATION = 34264
_GEO_KEY_DIRECTORY = 34735
_GDAL_NODATA = 42113

# Not a TIFF tag. The key `_read_ifd` files the file's byte order under, kept
# in the same dict so that one parse produces everything the rest needs.
_BYTE_ORDER = -1

_COMPRESSION_NONE = 1
_COMPRESSION_LZW = 5
_COMPRESSION_DEFLATE = 8
_COMPRESSION_ADOBE_DEFLATE = 32946
_COMPRESSION_NAMES = {
    2: "CCITT Group 3", 3: "CCITT T.4", 4: "CCITT T.6", 6: "old JPEG",
    7: "JPEG", 32773: "PackBits", 34887: "LERC", 50000: "Zstandard",
    50001: "WebP", 34925: "LZMA",
}

# (SampleFormat, BitsPerSample) pairs. Exactly the two Urbano's own reader
# accepts, so a file mapgen reads is a file Urbano could have read.
_SAMPLE_FORMAT_IEEE_FLOAT = 3
_SAMPLE_FORMAT_SIGNED_INT = 2

# GeoTIFF keys, read to establish that the raster really is in WGS84 latitude
# and longitude. Urbano's own reader does not check this and would silently
# misplace a projected raster; mapgen refuses one instead.
_GT_MODEL_TYPE = 1024
_GT_RASTER_TYPE = 1025
_GEOGRAPHIC_TYPE = 2048
_MODEL_TYPE_GEOGRAPHIC = 2
_RASTER_PIXEL_IS_POINT = 2
_WGS84 = 4326

# 4096 by 4096. Far beyond anything OpenTopography returns for a survey
# extent: the largest DEM ever measured for this tool is 245x109, and a 260
# square kilometre run came back at 288,384 pixels, fifty eight times under
# this. It is here so that a corrupt or hostile header cannot ask for a
# gigabyte of memory before anything has looked at the file.
MAX_PIXELS = 16_777_216


class GeoTiffError(ValueError):
    """Raised when a file is not a DEM GeoTIFF this reader will read.

    A ValueError subclass for the same reason every other refusal in mapgen
    is one, and every message names the file and says which specific thing
    about it was not readable, because the caller's only useful response is
    to look at the file.
    """


def _lzw_decode(data: bytes, expected: int) -> bytes:
    """TIFF LZW, the variable width variant with the early code change.

    Two things make this the TIFF flavour rather than the GIF one: the codes
    are packed most significant bit first, and the width goes up one code
    early, when the table reaches 511, 1023 and 2047 rather than 512, 1024
    and 2048. Getting the second wrong decodes the first few hundred bytes
    correctly and then produces noise, which is exactly the kind of failure
    that reaches a Grasshopper canvas as terrain rather than as an error, so
    the decoded length is checked against what the tile or strip must hold.
    """
    out = bytearray()
    table: list[bytes] = []
    previous: int | None = None
    bit = 0
    width = 9
    limit = len(data) * 8
    while True:
        if bit + width > limit:
            break
        start = bit >> 3
        chunk = int.from_bytes(data[start:start + 3].ljust(3, b"\0"), "big")
        code = (chunk >> (24 - (bit - (start << 3)) - width)) & ((1 << width) - 1)
        bit += width
        if code == 257:
            break
        if code == 256:
            table = [bytes([i]) for i in range(256)] + [b"", b""]
            width = 9
            previous = None
            continue
        if not table:
            raise GeoTiffError(
                "This DEM's LZW data does not begin with a clear code, so it "
                "is not TIFF LZW."
            )
        if previous is None:
            if code >= 256:
                raise GeoTiffError(
                    "This DEM's LZW data starts with a code that has no "
                    "meaning yet, so it is corrupt."
                )
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(table[previous] + entry[:1])
        elif code == len(table):
            entry = table[previous] + table[previous][:1]
            table.append(entry)
        else:
            raise GeoTiffError(
                "This DEM's LZW data refers to a code that was never defined, "
                "so it is corrupt."
            )
        out += entry
        previous = code
        if len(table) + 1 >= (1 << width) and width < 12:
            width += 1
    if len(out) < expected:
        raise GeoTiffError(
            f"This DEM's LZW data unpacked to {len(out)} bytes where "
            f"{expected} were needed, so it is truncated or corrupt."
        )
    return bytes(out)


@dataclass(frozen=True)
class DemRaster:
    """A DEM read off disk: its samples, and where on the earth they are.

    `heights` is row major, north first, exactly as the TIFF stores it, with
    every nodata pixel already turned into a NaN so that nothing downstream
    has to remember the sentinel. `transform` is the GDAL six number affine
    from pixel coordinates to longitude and latitude, and `inverse` is its
    inverse, which is the direction every sample goes.
    """

    path: Path
    width: int
    height: int
    transform: tuple[float, float, float, float, float, float]
    inverse: tuple[float, float, float, float, float, float]
    nodata: float | None
    heights: array

    def pixel_at(self, latitude: float, longitude: float) -> tuple[float, float]:
        """The continuous pixel coordinate a latitude and longitude falls on.

        Column first, matching the affine's own order. Integer values land on
        sample centres, not on pixel corners: see the module docstring on
        `RasterPixelIsPoint`.
        """
        inv = self.inverse
        return (
            inv[0] + inv[1] * longitude + inv[2] * latitude,
            inv[3] + inv[4] * longitude + inv[5] * latitude,
        )

    def has_coverage(self, latitude: float, longitude: float) -> bool:
        """Whether a point is inside the raster's sampleable area.

        `HasCoverage` in Urbano's reader, and the bound is `width - 1` rather
        than `width` because the sampler needs the pixel to the right and the
        row below it to interpolate with.
        """
        column, row = self.pixel_at(latitude, longitude)
        return (
            column >= 0.0
            and row >= 0.0
            and column < float(self.width - 1)
            and row < float(self.height - 1)
        )

    def sample(self, latitude: float, longitude: float) -> float | None:
        """The height at a point, or None where the raster cannot say.

        None, never a sentinel and never a zero, because "no data here" is a
        different statement from "sea level" and the whole of a coastal
        survey turns on the difference.

        This is `TiffExtensions.ReadTiffFile.TryGetElevation`: bilinear over
        the four surrounding samples, with any nodata corner given zero
        weight rather than being allowed to drag the result down, and None
        when all four are nodata. The result is rounded back to single
        precision because Urbano's own sampler returns a `float` and the
        grid it builds is the double widening of that; a `double` here would
        differ from Urbano in the last few digits of every value.

        has_coverage is the ONLY bounds check here, and it is a complete
        one: it establishes 0 <= column < width - 1 and 0 <= row < height -
        1, so the floors below are at most width - 2 and height - 2 and all
        four corners are inside the array. There used to be a second check
        restating that, and it could not fire. A guard that cannot fire is
        worse than no guard, because the next person to loosen has_coverage
        reads it as independent cover when it is only an echo. Loosen
        has_coverage and this reads past the end of the array, so the
        boundary is asserted through sample() in test_geotiff.py and fails
        there rather than in Grasshopper.
        """
        if not self.has_coverage(latitude, longitude):
            return None
        column, row = self.pixel_at(latitude, longitude)
        c0 = math.floor(column)
        r0 = math.floor(row)
        fx = column - c0
        fy = row - r0
        weights = ((1.0 - fx) * (1.0 - fy), fx * (1.0 - fy), (1.0 - fx) * fy, fx * fy)
        corners = (
            self.heights[r0 * self.width + c0],
            self.heights[r0 * self.width + c0 + 1],
            self.heights[(r0 + 1) * self.width + c0],
            self.heights[(r0 + 1) * self.width + c0 + 1],
        )
        total = 0.0
        weight = 0.0
        for height, share in zip(corners, weights):
            if height != height:  # NaN: this corner has no data.
                continue
            total += height * share
            weight += share
        if weight == 0.0:
            return None
        return struct.unpack("<f", struct.pack("<f", total / weight))[0]


def _read_ifd(data: bytes, path: Path) -> dict[int, list]:
    """Every tag in the first IFD, as plain Python values.

    The first directory only. A DEM GeoTIFF's later directories are reduced
    resolution overviews, and reading one instead of the full raster would
    be a silent loss of half the detail per level.
    """
    if len(data) < 8:
        raise GeoTiffError(f"{path.name} is too short to be a TIFF at all.")
    order = data[:2]
    if order == b"II":
        end = "<"
    elif order == b"MM":
        end = ">"
    else:
        raise GeoTiffError(
            f"{path.name} does not start with a TIFF byte order mark, so it is "
            f"not a TIFF."
        )
    magic = struct.unpack_from(end + "H", data, 2)[0]
    if magic == 43:
        raise GeoTiffError(
            f"{path.name} is a BigTIFF. mapgen reads classic TIFF only; no DEM "
            f"OpenTopography has returned for a survey extent has ever been one."
        )
    if magic != 42:
        raise GeoTiffError(f"{path.name} is not a TIFF: its version word is {magic}.")
    offset = struct.unpack_from(end + "I", data, 4)[0]
    if offset == 0 or offset + 2 > len(data):
        raise GeoTiffError(f"{path.name} has no image directory where it says it has.")
    count = struct.unpack_from(end + "H", data, offset)[0]
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
    formats = {1: "B", 3: "H", 4: "I", 6: "b", 7: "B", 8: "h", 9: "i", 11: "f", 12: "d"}
    tags: dict[int, list] = {}
    for index in range(count):
        base = offset + 2 + index * 12
        if base + 12 > len(data):
            raise GeoTiffError(f"{path.name}'s image directory runs off the end of it.")
        tag, kind, length = struct.unpack_from(end + "HHI", data, base)
        size = sizes.get(kind)
        if size is None:
            continue
        total = size * length
        if total <= 4:
            value_at = base + 8
        else:
            value_at = struct.unpack_from(end + "I", data, base + 8)[0]
        if value_at + total > len(data):
            raise GeoTiffError(
                f"{path.name}'s tag {tag} points past the end of the file, so it "
                f"is truncated or corrupt."
            )
        if kind == 2:
            # ASCII, which is how GDAL_NODATA carries a number.
            tags[tag] = [data[value_at:value_at + length].split(b"\0", 1)[0]]
        elif kind in formats:
            tags[tag] = list(
                struct.unpack_from(end + formats[kind] * length, data, value_at)
            )
    tags[_BYTE_ORDER] = [end]
    return tags


def _geotransform(
    tags: dict[int, list], path: Path
) -> tuple[float, float, float, float, float, float]:
    """The GDAL affine, built the way Urbano's own reader builds it.

    Scale and tie point only. `ModelTransformation` is refused rather than
    read: no OpenTopography DEM has ever carried one, Urbano's reader throws
    on a file that has one instead of a tie point, and a rotated DEM read as
    an unrotated one is precisely the silent, geometry shifting error this
    whole module is careful about.
    """
    scale = tags.get(_MODEL_PIXEL_SCALE)
    tie = tags.get(_MODEL_TIEPOINT)
    if scale is None or tie is None:
        if tags.get(_MODEL_TRANSFORMATION) is not None:
            raise GeoTiffError(
                f"{path.name} places itself with a ModelTransformation rather "
                f"than a pixel scale and a tie point. mapgen does not read "
                f"those, and neither does Urbano."
            )
        raise GeoTiffError(
            f"{path.name} carries no GeoTIFF pixel scale and tie point, so "
            f"there is no way to say where on the earth it is."
        )
    if len(scale) < 2 or len(tie) < 6:
        raise GeoTiffError(
            f"{path.name}'s GeoTIFF pixel scale or tie point is too short to "
            f"place it."
        )
    sx = float(scale[0])
    sy = float(scale[1]) if len(scale) > 1 else sx
    return (
        float(tie[3]) - float(tie[0]) * sx,
        sx,
        0.0,
        float(tie[4]) + float(tie[1]) * sy,
        0.0,
        -sy,
    )


def _invert(
    transform: tuple[float, float, float, float, float, float], path: Path
) -> tuple[float, float, float, float, float, float]:
    determinant = transform[1] * transform[5] - transform[2] * transform[4]
    if abs(determinant) < 1e-20:
        raise GeoTiffError(
            f"{path.name}'s GeoTIFF placement cannot be inverted, so no point "
            f"on the earth can be turned into a pixel of it."
        )
    scale = 1.0 / determinant
    a = transform[5] * scale
    b = -transform[2] * scale
    d = -transform[4] * scale
    e = transform[1] * scale
    return (
        -(transform[0] * a + transform[3] * b),
        a,
        b,
        -(transform[0] * d + transform[3] * e),
        d,
        e,
    )


def _check_geokeys(tags: dict[int, list], path: Path) -> None:
    """Refuse anything that is not an unrotated WGS84 point sampled raster.

    Urbano's reader looks for a WKT in the GDAL metadata and falls back to
    assuming WGS84 when it finds none, so a projected DEM would be read as
    if its eastings were degrees and land in the Gulf of Guinea. mapgen
    reads the GeoTIFF keys instead and refuses, because the point of writing
    an elevation grid at all is that the owner can trust where it is.
    """
    directory = tags.get(_GEO_KEY_DIRECTORY)
    if not directory or len(directory) < 4:
        raise GeoTiffError(
            f"{path.name} carries no GeoTIFF key directory, so it does not say "
            f"which coordinate system it is in."
        )
    keys = {}
    for index in range(4, len(directory) - 3, 4):
        key, location, count, value = directory[index:index + 4]
        if location == 0 and count == 1:
            keys[key] = value
    model = keys.get(_GT_MODEL_TYPE)
    if model != _MODEL_TYPE_GEOGRAPHIC:
        raise GeoTiffError(
            f"{path.name} is not a geographic raster (its GeoTIFF model type is "
            f"{model}, not {_MODEL_TYPE_GEOGRAPHIC}). mapgen reads DEMs in "
            f"WGS84 latitude and longitude only."
        )
    datum = keys.get(_GEOGRAPHIC_TYPE)
    if datum != _WGS84:
        raise GeoTiffError(
            f"{path.name} is in geographic system {datum}, not WGS84 ({_WGS84}), "
            f"so its coordinates are not the ones mapgen and Urbano project from."
        )
    raster = keys.get(_GT_RASTER_TYPE)
    if raster != _RASTER_PIXEL_IS_POINT:
        raise GeoTiffError(
            f"{path.name} declares RasterPixelIsArea rather than "
            f"RasterPixelIsPoint. Urbano's own reader would sample it half a "
            f"pixel out and mapgen will not write terrain it knows is shifted."
        )


def _decompress(block: bytes, compression: int, expected: int, path: Path) -> bytes:
    if compression == _COMPRESSION_NONE:
        raw = block
    elif compression == _COMPRESSION_LZW:
        raw = _lzw_decode(block, expected)
    else:
        try:
            raw = zlib.decompress(block)
        except zlib.error as exc:
            raise GeoTiffError(
                f"{path.name}'s deflate data could not be unpacked: {exc}"
            ) from None
    if len(raw) < expected:
        raise GeoTiffError(
            f"{path.name} holds {len(raw)} bytes of sample data where {expected} "
            f"were needed, so it is truncated."
        )
    return raw


def read_dem(path: Path | str) -> DemRaster:
    """Read a DEM GeoTIFF, or refuse it and say which part was unreadable.

    The refusals are the point. Every one of them is a file this cannot read
    correctly, and returning heights that are half a pixel out, or in the
    wrong datum, or decoded with the wrong predictor, would reach the owner's
    canvas as terrain rather than as an error.
    """
    path = Path(path)
    data = path.read_bytes()
    tags = _read_ifd(data, path)
    # array.frombytes reads in the machine's own byte order, so a file whose
    # order differs has to be swapped after the copy. Both real DEMs are
    # little endian on a little endian machine, which is the branch that does
    # nothing; the other one is here so a big endian DEM is read rather than
    # read backwards.
    swap = (tags[_BYTE_ORDER][0] == "<") != (struct.pack("=H", 1) == b"\x01\x00")

    def one(tag: int, default: int | None = None) -> int | None:
        value = tags.get(tag)
        return int(value[0]) if value else default

    width = one(_IMAGE_WIDTH)
    height = one(_IMAGE_LENGTH)
    if not width or not height:
        raise GeoTiffError(f"{path.name} does not say how big its image is.")
    if width * height > MAX_PIXELS:
        raise GeoTiffError(
            f"{path.name} is {width} by {height} pixels, over mapgen's "
            f"{MAX_PIXELS} pixel limit for a DEM."
        )
    samples_per_pixel = one(_SAMPLES_PER_PIXEL, 1)
    if samples_per_pixel != 1:
        raise GeoTiffError(
            f"{path.name} has {samples_per_pixel} samples per pixel. A DEM has "
            f"one, and Urbano's own reader refuses anything else too."
        )
    planar = one(_PLANAR_CONFIGURATION, 1)
    if planar != 1:
        raise GeoTiffError(
            f"{path.name} stores its samples in separate planes, which mapgen "
            f"does not read."
        )
    bits = one(_BITS_PER_SAMPLE)
    sample_format = one(_SAMPLE_FORMAT)
    if sample_format is None:
        raise GeoTiffError(
            f"{path.name} does not declare a sample format, so what its numbers "
            f"mean cannot be established. mapgen will not assume."
        )
    if sample_format == _SAMPLE_FORMAT_IEEE_FLOAT and bits == 32:
        code, stride = "f", 4
    elif sample_format == _SAMPLE_FORMAT_SIGNED_INT and bits == 16:
        code, stride = "h", 2
    else:
        raise GeoTiffError(
            f"{path.name} holds {bits} bit samples of format {sample_format}. "
            f"mapgen reads 32 bit IEEE floats and 16 bit signed integers, which "
            f"are the two Urbano's own reader accepts."
        )
    compression = one(_COMPRESSION, 1)
    if compression not in (
        _COMPRESSION_NONE,
        _COMPRESSION_LZW,
        _COMPRESSION_DEFLATE,
        _COMPRESSION_ADOBE_DEFLATE,
    ):
        named = _COMPRESSION_NAMES.get(compression, "an unrecognised scheme")
        raise GeoTiffError(
            f"{path.name} is compressed with {named} (tag value {compression}). "
            f"mapgen reads uncompressed, LZW and deflate DEMs."
        )
    predictor = one(_PREDICTOR, 1)
    if predictor != 1:
        raise GeoTiffError(
            f"{path.name} uses TIFF predictor {predictor}. mapgen reads only "
            f"unpredicted samples; decoding one as the other produces heights "
            f"that look plausible and are wrong."
        )
    _check_geokeys(tags, path)
    transform = _geotransform(tags, path)
    inverse = _invert(transform, path)

    heights = array("d", [0.0]) * (width * height)
    tile_width = one(_TILE_WIDTH)
    tile_height = one(_TILE_LENGTH)
    if tile_width and tile_height:
        offsets = tags.get(_TILE_OFFSETS) or []
        counts = tags.get(_TILE_BYTE_COUNTS) or []
        across = (width + tile_width - 1) // tile_width
        down = (height + tile_height - 1) // tile_height
        if len(offsets) < across * down or len(counts) < across * down:
            raise GeoTiffError(
                f"{path.name} declares {across * down} tiles but lists "
                f"{min(len(offsets), len(counts))} of them."
            )
        block_bytes = tile_width * tile_height * stride
        for index in range(across * down):
            start = int(offsets[index])
            raw = _decompress(
                data[start:start + int(counts[index])], compression, block_bytes, path
            )
            block = array(code)
            block.frombytes(raw[:block_bytes])
            if swap:
                block.byteswap()
            left = (index % across) * tile_width
            top = (index // across) * tile_height
            for row in range(min(tile_height, height - top)):
                source = row * tile_width
                target = (top + row) * width + left
                span = min(tile_width, width - left)
                heights[target:target + span] = array(
                    "d", block[source:source + span]
                )
    else:
        rows_per_strip = one(_ROWS_PER_STRIP, height) or height
        offsets = tags.get(_STRIP_OFFSETS) or []
        counts = tags.get(_STRIP_BYTE_COUNTS) or []
        strips = (height + rows_per_strip - 1) // rows_per_strip
        if len(offsets) < strips or len(counts) < strips:
            raise GeoTiffError(
                f"{path.name} declares {strips} strips but lists "
                f"{min(len(offsets), len(counts))} of them."
            )
        for index in range(strips):
            top = index * rows_per_strip
            rows = min(rows_per_strip, height - top)
            start = int(offsets[index])
            raw = _decompress(
                data[start:start + int(counts[index])], compression, rows * width * stride,
                path,
            )
            block = array(code)
            block.frombytes(raw[: rows * width * stride])
            if swap:
                block.byteswap()
            heights[top * width:(top + rows) * width] = array("d", block[: rows * width])

    nodata = _nodata(tags)
    if nodata is not None:
        for index, value in enumerate(heights):
            if abs(value - nodata) < 1e-6:
                heights[index] = math.nan
    return DemRaster(
        path=path,
        width=width,
        height=height,
        transform=transform,
        inverse=inverse,
        nodata=nodata,
        heights=heights,
    )


def _nodata(tags: dict[int, list]) -> float | None:
    """The GDAL_NODATA value, which is an ASCII string in the tag, or None.

    None is the ordinary case: neither real OpenTopography DEM carries this
    tag, because COP30 fills water rather than voiding it. An unparseable
    value is treated as no declaration rather than as a refusal, because a
    DEM that says something odd here is still a readable DEM and the
    alternative would be refusing a file over a tag nothing in it uses. This
    is what Urbano's own reader does with it too.
    """
    raw = tags.get(_GDAL_NODATA)
    if not raw:
        return None
    try:
        value = float(raw[0].decode("latin-1").strip())
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return value if math.isfinite(value) else None
