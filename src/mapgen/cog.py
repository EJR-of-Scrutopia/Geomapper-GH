"""Site sized windows out of a Cloud Optimized GeoTIFF, local or 48 GB away.

`geotiff.py`'s sibling, and deliberately not its replacement. That module
reads a whole small DEM off disk in the one dialect Urbano's own reader
accepts, and its sampler is pinned to Urbano's arithmetic. This module does
the opposite job: it reaches into a mosaic far too large to download, over
HTTP range requests, and pulls out the few hundred metres a survey asked
for. It also reads the classic TIFF `geotiff_write.py` writes back into a
package, so one parser serves both directions of the new traffic and there
is no second TIFF dialect in this project to drift.

## What the real files are

Probed live 2026-08-05 against the two whole-Wales mosaics,
`wales_dtm_32bit_cog.tif` (48,611,310,928 bytes) and
`wales_dsm_32bit_cog.tif` (52,096,926,263 bytes):

    little endian BigTIFF        191,007 x 233,000 at exactly 1.0 m
    BitsPerSample 32             SampleFormat 3 (IEEE float)
    SamplesPerPixel 1            PlanarConfiguration 1
    Compression 8 (deflate)      Predictor 1 (none)
    tiled 256 x 256              747 x 911 = 680,517 tiles at full res
    TileOffsets type 16 (LONG8)  TileByteCounts type 4 (LONG)
    ModelPixelScale 1,1,0        ModelTiepoint 0,0,0 -> 164993, 397000
    GeoKeys: projected (1)       EPSG 27700, linear units 9001 (metre)
    RasterPixelIsArea (1)        GDAL_NODATA "-9999"
    seven IFD levels             full res plus six halving overviews

Two of those lines are why this module exists at all. BigTIFF is a
different header and a different directory layout, not a variant of the
classic one; and the tile index alone is 5.4 MB at full resolution, so the
reader range-reads the SLICES of TileOffsets and TileByteCounts its window
needs and never the arrays. A 500 m window costs about a megabyte.

The overviews carry no georeference of their own, which is normal: their
extent is the full resolution extent, so each level's pixel size is level
0's scaled by the dimension ratio. That is GDAL's own overview arithmetic
(`GDALOverviewDataset::GetGeoTransform` multiplies by exactly this ratio),
and it is not quite a power of two, because 191,007 pixels halve to 95,504
rather than to 95,503.5.

**Per axis, because the two ratios disagree.** Measured on the real DTM:

    level   pixels            east/west     north/south
    0       191007 x 233000    1.0000000     1.0000000
    1        95504 x 116500    1.9999895     2.0000000
    2        47752 x  58250    3.9999791     4.0000000
    3        23876 x  29125    7.9999581     8.0000000
    4        11938 x  14563   15.9999162    15.9994507
    5         5969 x   7282   31.9998325    31.9967042
    6         2985 x   3641   63.9889447    63.9934084

The columns differ in the seventh digit and the temptation is to carry one
number. Do not: the error a single scalar makes is the ROW INDEX times the
difference, and a Barry window sits 115,050 rows down the mosaic. Using
level 1's east/west size for its rows puts that window 1.2 m north of its
own pixels, and level 6 about 16 m. So `CogLevel` and `BngWindow` each
carry both, and every piece of arithmetic below uses the one belonging to
its axis. A non-square raster at level 0 is read honestly rather than
refused (`_read_placement` takes `scale[0]` and `scale[1]` as they come):
level 0's own anisotropy is rare on a source mosaic, but it is exactly
what a packaged raster this project writes from an overview-level window
looks like (`geotiff_write.py`), and refusing that file back would be
worse than reading it.

Which anchoring these overviews really use is worth naming as an open
question. The ratio convention above is what GDAL and rasterio report, and
it assumes the overview was stretched onto the full extent. The other
possibility is that the producer decimated by exactly two with the grid
anchored at the origin, in which case every level would be an exact power
of two and the ratio places overview pixels up to about a metre west of
where they are at Barry. An attempt to settle it by correlating level 1
against level 0 on real terrain was inconclusive (the control axis, where
both hypotheses agree, did not return a zero shift, so the method had no
power). The consequence is bounded and confined to overview levels: it
does not touch the 1 m path, which is what a site survey uses.

## PixelIsArea, which phase 1 refused and this must not

`geotiff.py` refuses RasterPixelIsArea because its files are all
RasterPixelIsPoint and reading one convention as the other shifts every
height half a pixel diagonally: terrain that renders rather than failing.
These files are the other convention, so both are honoured here, and both
are normalised to the raster's outer corner in `CogReader.open`, once.
Past that line there is one rule everywhere below and in everything built
on a window: the centre of pixel (col, row) is
`origin_e + (col + 0.5) * pixel_size`, `origin_n - (row + 0.5) *
pixel_size`, whichever convention the file was written in.

## Nodata, and why the sampler is not Urbano's

Nodata is whatever GDAL_NODATA declares (a NaN pixel is nodata because
IEEE says so), turned into NaN at decode time so that nothing downstream
carries a sentinel, and `sample_bng` answers None where the data is absent.
The Welsh mosaic is nodata over the sea, over England, and over anything
unflown, so this is the ordinary case here rather than the exception it was
in phase 1.

`sample_bng` is bilinear between pixel centres with absent corners given
zero weight, which is the same shape as `DemRaster.sample`, but it
deliberately does NOT round its result back to single precision.
`DemRaster.sample` does that because the grid mapgen writes from it has to
be the grid Urbano itself would have written from the same file, digit for
digit. Nothing on this path is matched against Urbano: these windows feed
contours, building heights and an elevation grid built from a sampler
chain, and a needless narrowing to float32 in the middle of that would be a
loss of precision imitating a compatibility requirement that does not
apply here.

## The caps

A COG's header is a set of instructions to allocate memory, and this one
arrives over the network from a service nobody here controls. Every count
that reaches an allocation is bounded: directory entries, IFD chain length,
tile dimensions, tiles per window, bytes per tile, and the window itself.
Deflate output is bounded at the decompressor rather than after it, so a
zip bomb in a tile is refused rather than decompressed and then measured.
"""

from __future__ import annotations

import math
import struct
import zlib
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import requests

from mapgen.bng import Ostn15Grid, OutsideOstn15Error, to_bng
from mapgen.sources.base import (
    RETRYABLE_FAILURE_KINDS,
    classify_status_failure,
    classify_transport_failure,
)

# Matching osm.py and elevation.py exactly. Written out rather than imported
# from either, because this module has no other reason to depend on a source
# and a shared string is not worth the coupling.
USER_AGENT = "mapgen/1.0 (architectural survey tool)"

BNG_EPSG = 27700

# TIFF tags this reader knows about. Anything else in a directory is
# skipped, which is safe for the same reason it is in geotiff.py: every tag
# that changes how the samples are laid out is in this list.
_IMAGE_WIDTH = 256
_IMAGE_LENGTH = 257
_BITS_PER_SAMPLE = 258
_COMPRESSION = 259
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
_MODEL_TRANSFORMATION = 34264
_GEO_KEY_DIRECTORY = 34735
_GDAL_NODATA = 42113

_COMPRESSION_NONE = 1
_COMPRESSION_DEFLATE = 8
_COMPRESSION_ADOBE_DEFLATE = 32946
_COMPRESSION_NAMES = {
    2: "CCITT Group 3", 3: "CCITT T.4", 4: "CCITT T.6", 5: "LZW",
    6: "old JPEG", 7: "JPEG", 32773: "PackBits", 34887: "LERC",
    34925: "LZMA", 50000: "Zstandard", 50001: "WebP",
}

_SAMPLE_FORMAT_IEEE_FLOAT = 3
_SAMPLE_FORMAT_SIGNED_INT = 2

_GT_MODEL_TYPE = 1024
_GT_RASTER_TYPE = 1025
_PROJECTED_CS_TYPE = 3072
_MODEL_TYPE_PROJECTED = 1
_RASTER_PIXEL_IS_AREA = 1
_RASTER_PIXEL_IS_POINT = 2

# TIFF field types, by size in bytes and by struct code where this reader
# can read them. RATIONAL and the rest are absent because no tag this
# module reads is ever written in them.
_TYPE_SIZES = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4,
    12: 8, 13: 4, 16: 8, 17: 8, 18: 8,
}
_TYPE_FORMATS = {
    1: "B", 3: "H", 4: "I", 6: "b", 7: "B", 8: "h", 9: "i", 11: "f", 12: "d",
    13: "I", 16: "Q", 17: "q", 18: "Q",
}

# The same 4096 by 4096 ceiling geotiff.py already uses, for the same
# reason: a corrupt or hostile header must not be able to ask for a
# gigabyte of memory before anything has looked at the data. A 500 m window
# at 1 m is 250,000 pixels, sixty seven times under this; the ceiling is
# what makes read_window fall to an overview instead of refusing.
MAX_WINDOW_PIXELS = 16_777_216

# A real directory here has about twenty entries and the mosaics have seven
# levels. These are the bounds on what a header can talk this reader into
# allocating or looping over.
MAX_IFD_ENTRIES = 512
MAX_LEVELS = 32
MAX_TILE_DIMENSION = 8192

# A window of MAX_WINDOW_PIXELS at the mosaic's 256 x 256 tiling needs
# about 320 tiles. This bound is set by the smallest tiling that could
# arrive instead: 32 x 32 tiles over the same window would be 16,384 of
# them, and anything smaller than that is a file asking for a request per
# few hundred pixels.
MAX_TILES_PER_WINDOW = 16_384

# One tile of 256 x 256 float32 is 262,144 bytes uncompressed. The bound is
# on the COMPRESSED length a header may claim for one, before it is read.
MAX_TILE_BYTES = 64 * 1024 * 1024

# Tag values that are read whole rather than sliced: geokey directories,
# pixel scales, tie points. The tile index arrays are never read this way,
# which is the whole point of TagArray.
MAX_TAG_ARRAY_BYTES = 1024 * 1024

# One request instead of twenty while walking the directory chain. A COG
# puts its header, all its IFDs and its geo tags at the front of the file
# by construction, so this covers the whole of open() on both mosaics.
# Never used for tile data: a window's pixels are orders of magnitude
# larger than this and caching them would be a memory leak wearing a
# performance argument.
_HEADER_PREFETCH_BYTES = 16 * 1024

# Adjacent tiles are fetched in one request; this bounds how much one
# request may therefore become, so that a stalled transfer costs at most
# this much work to repeat.
_COALESCE_LIMIT_BYTES = 32 * 1024 * 1024

_HTTP_CHUNK_BYTES = 1024 * 1024

_MACHINE_IS_LITTLE = struct.pack("=H", 1) == b"\x01\x00"


class CogError(ValueError):
    """Raised when a raster is not one this reader will read, or cannot be.

    A ValueError subclass for the same reason every other refusal in mapgen
    is one. Every message names the file and the specific value that was
    not readable, and never the URL it came from: a URL is where a key
    lives, and this project's failure sentences do not carry one.

    `status_code` is set by `HttpByteSource` on every raise site where it
    actually saw an HTTP status (a 200 answered to a range request, or any
    other non-206 status after its own retry), and left None everywhere
    else (a short body, a Content-Range mismatch, a transport exception).
    It exists so a caller that needs to know WHICH status this was, to
    decide whether the failure is retryable, does not have to parse that
    back out of the message this class already renders in English:
    `mapgen.sources.lidar_wales._classify_cog_error` is the reason it was
    added, having previously tried to recover the status by matching
    "(HTTP nnn)" in the rendered sentence, which missed `classify_status_
    failure`'s own >=500 branch (`"answered HTTP {code}"`, no parentheses,
    unlike its 429/401/403/else siblings) and silently misclassified every
    5xx as unrecognised. A structural attribute cannot go stale the way a
    regex against this class's own prose can.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class ByteSource(Protocol):
    """Random access to a file, wherever it is.

    Two implementations ship here, one for a path and one for a URL, and
    the reader cannot tell which it was handed. `read` returns exactly
    `length` bytes or raises; a short answer is a truncated file or a
    server that did not do as it was asked, and both are refusals rather
    than something to work around.
    """

    name: str

    def read(self, start: int, length: int) -> bytes: ...

    def size(self) -> int: ...


class FileByteSource:
    """A local raster, opened per read.

    Per read rather than holding a handle open: a reader may outlive the
    call that made it (Task 7 reads packaged rasters back long after the
    fetch), an open handle on Windows locks the file against the atomic
    rename every mapgen writer ends with, and a handle nothing closes is a
    ResourceWarning waiting to happen. The cost is a syscall per tile
    group, which is nothing beside the deflate.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.name = self.path.name

    def size(self) -> int:
        return self.path.stat().st_size

    def read(self, start: int, length: int) -> bytes:
        if length <= 0:
            return b""
        with self.path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(length)
        if len(data) < length:
            raise CogError(
                f"{self.name} ends after {start + len(data)} bytes, where this "
                f"reader needed byte {start + length}, so it is truncated."
            )
        return data


class HttpByteSource:
    """A raster read where it lies, one Range request at a time.

    The refusals in `read` are not defensive padding. A server that answers
    200 to a range request is offering to stream the entire mosaic, which
    is 48 GB down a domestic line; one that answers 206 without a
    Content-Range has not said which bytes it sent, so the tile boundaries
    computed from the header would be applied to the wrong data and the
    window would be silently, plausibly wrong. Neither is worth recovering
    from, so neither is retried.

    `bytes_fetched` and `requests_made` are measurement, not bookkeeping:
    the estimate constants in `sources/lidar_wales.py` are refitted from
    them, and the live test asserts against them that a 500 m window costs
    megabytes rather than gigabytes. `bytes_fetched` counts payload asked
    for, so it is a little under what crossed the wire (headers, and the
    tail of a chunk the reader stopped in the middle of).
    """

    def __init__(
        self,
        url: str,
        session: object | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.url = url
        self.session = session if session is not None else requests.Session()
        self.timeout_seconds = timeout_seconds
        # The last path segment, never the URL: this is what every refusal
        # below names, and a query string is where a key would be.
        self.name = urlsplit(url).path.rsplit("/", 1)[-1] or "the remote raster"
        self.bytes_fetched = 0
        self.requests_made = 0
        self._size: int | None = None

    def size(self) -> int:
        if self._size is None:
            # One byte, purely for the total in its Content-Range. A HEAD
            # would do the same job in the same round trip, and a range
            # request is the one thing this source already knows the
            # server handles correctly.
            self.read(0, 1)
        if self._size is None:
            raise CogError(
                f"The server holding {self.name} did not say how large it is, "
                f"so there is no way to tell a short read from the end of it."
            )
        return self._size

    def read(self, start: int, length: int) -> bytes:
        if length <= 0:
            return b""
        headers = {
            "User-Agent": USER_AGENT,
            "Range": f"bytes={start}-{start + length - 1}",
        }
        # One retry, and only for the failures a second attempt can
        # plausibly fix: the transport ones, and the statuses this
        # project's own classifier already calls retryable. A while loop
        # rather than `for attempt in (0, 1)` so that every path inside
        # returns or raises and there is no unreachable line after it.
        attempt = 0
        while True:
            retry_allowed = attempt == 0
            attempt += 1
            try:
                self.requests_made += 1
                with self.session.get(
                    self.url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    stream=True,
                ) as response:
                    status = response.status_code
                    if status == 200:
                        # Refused before a single chunk is read: with
                        # stream=True the body has not been transferred
                        # yet, and this is the branch where it would be
                        # the whole mosaic.
                        raise CogError(
                            f"The server holding {self.name} answered a range "
                            f"request with the whole file (HTTP 200). mapgen "
                            f"reads this raster in pieces and will not "
                            f"download all of it.",
                            status_code=status,
                        )
                    if status != 206:
                        kind, phrase = classify_status_failure(status)
                        if kind in RETRYABLE_FAILURE_KINDS and retry_allowed:
                            continue
                        raise CogError(
                            f"The server holding {self.name} {phrase} for the "
                            f"{length} byte range mapgen asked for.",
                            status_code=status,
                        )
                    answered = getattr(response, "headers", None) or {}
                    self._accept_range(
                        answered.get("Content-Range"), start, length
                    )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(_HTTP_CHUNK_BYTES):
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= length:
                            break
            except CogError:
                raise
            except Exception as exc:
                # Exception, not BaseException: a KeyboardInterrupt is the
                # owner stopping the run, not a transport failure to
                # describe and swallow.
                _kind, phrase = classify_transport_failure(exc)
                if retry_allowed:
                    continue
                raise CogError(
                    f"The server holding {self.name} {phrase}, twice. mapgen "
                    f"reads this raster by range request and cannot continue "
                    f"without that range."
                ) from None
            if total < length:
                raise CogError(
                    f"The server holding {self.name} sent {total} bytes where "
                    f"{length} were asked for, so the range came back short."
                )
            self.bytes_fetched += length
            return b"".join(chunks)[:length]

    def _accept_range(
        self, content_range: str | None, start: int, length: int
    ) -> None:
        """Check that the bytes offered are the bytes that were asked for.

        `bytes 0-15/48611310928`. Presence of this header is not agreement
        with it, and the difference matters: a proxy or a cache that
        answers 206 with some OTHER part of the file passes every other
        check in this method, and its bytes are then handed to the tile
        decoder as the slice the tile index computed. That is a window
        made of the wrong pixels, with no error anywhere, which is the
        exact failure this module is built to make impossible.

        The total after the slash is taken while it is here, because it is
        how `size` learns the file's length without a second request. A
        starred or malformed total leaves the size unknown rather than
        guessed, and `size` refuses on that; an unparseable range is
        refused outright, because a range that cannot be read cannot be
        checked either.
        """
        text = (content_range or "").strip()
        first = last = total = None
        if text.lower().startswith("bytes "):
            span, _, tail = text[6:].partition("/")
            begin, _, end = span.partition("-")
            if begin.strip().isdigit() and end.strip().isdigit():
                first, last = int(begin), int(end)
            if tail.strip().isdigit():
                total = int(tail)
        if first is None or last is None:
            raise CogError(
                f"The server holding {self.name} answered a range request "
                f"without saying which bytes it sent, so what came back "
                f"cannot be trusted to be the range that was asked for."
            )
        if first != start or last != start + length - 1:
            raise CogError(
                f"The server holding {self.name} answered with different "
                f"bytes ({first} to {last}) than the {start} to "
                f"{start + length - 1} range mapgen asked for."
            )
        if total is not None:
            self._size = total


@dataclass(frozen=True)
class TagArray:
    """Where a tag's values are, without having read them.

    This is what makes a 5.4 MB tile index affordable: the directory entry
    says the array's type, its length and where it starts, and the reader
    range-reads the handful of elements a window needs from that. `inline`
    holds the entry's own value field for the short arrays TIFF packs
    there instead.
    """

    tag: int
    kind: int
    count: int
    offset: int
    inline: bytes | None

    @property
    def item_size(self) -> int:
        return _TYPE_SIZES[self.kind]

    @property
    def byte_length(self) -> int:
        return self.count * self.item_size


@dataclass(frozen=True)
class CogLevel:
    """One image file directory: a whole resolution of the raster."""

    index: int
    width: int
    height: int
    # East to west and north to south separately. They usually agree at
    # level 0 (real source mosaics are square there) but are not assumed
    # to: _read_placement reads scale[0] and scale[1] as given, honestly,
    # because a packaged raster written from an overview-level window can
    # genuinely differ at level 0 too. They differ at an overview level
    # whenever the two dimension ratios do:
    # 191007/95504 is 1.9999895 while 233000/116500 is exactly 2. One
    # scalar used for both axes puts an overview window metres away from
    # its own pixels, growing with distance from the tie point.
    pixel_size: float
    pixel_height: float
    tile_width: int
    tile_height: int
    tiles_across: int
    tiles_down: int
    compression: int
    sample_code: str
    sample_stride: int
    tile_offsets: TagArray
    tile_byte_counts: TagArray

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class BngWindow:
    """A rectangle of heights in British National Grid metres.

    `e_origin` and `n_top` are the OUTER CORNER of pixel (0,0), not its
    centre, so a window can be written straight back out as a
    RasterPixelIsArea GeoTIFF and read back to the same place.
    `values` is row major, north first, float32, with every nodata pixel
    already NaN.

    `pixel_size` is the east to west size and is the number anything
    downstream means by "resolution". `pixel_height` is the north to
    south size, and it is a separate field because an overview level of
    the real mosaics genuinely has a different one (see `CogLevel`). It
    defaults to `pixel_size`, so a window built by hand for a square
    grid, which is every window this project makes outside `read_window`,
    reads exactly as it did before this field existed.
    """

    e_origin: float
    n_top: float
    pixel_size: float
    width: int
    height: int
    values: array
    # Last, and defaulted, only because a dataclass cannot put a
    # defaulted field before an undefaulted one. Never None after
    # construction.
    pixel_height: float | None = None

    def __post_init__(self) -> None:
        if self.pixel_height is None:
            object.__setattr__(self, "pixel_height", self.pixel_size)

    def bounds(self) -> tuple[float, float, float, float]:
        """(e_min, n_min, e_max, n_max) of the window's outer edges."""
        return (
            self.e_origin,
            self.n_top - self.height * self.pixel_height,
            self.e_origin + self.width * self.pixel_size,
            self.n_top,
        )

    def sample_bng(self, easting: float, northing: float) -> float | None:
        """The height at a BNG point, or None where the window cannot say.

        Bilinear between the four surrounding pixel centres, with any
        absent corner given zero weight rather than being allowed to drag
        the answer towards a sentinel, and None when all four are absent.
        None also for a point outside the centres' own rectangle: half a
        pixel in from each edge is where interpolation stops being
        interpolation. A window less than two pixels across or down has no
        pair of centres to interpolate between and answers None
        everywhere, rather than reading past the end of its own array.

        Unlike `DemRaster.sample` this does not round the result back to
        single precision. See the module docstring: nothing on this path
        is matched against Urbano's own reader, so the narrowing would be
        a loss of precision imitating a requirement that does not apply.
        """
        if self.width < 2 or self.height < 2:
            return None
        column = (easting - self.e_origin) / self.pixel_size - 0.5
        row = (self.n_top - northing) / self.pixel_height - 0.5
        if not (math.isfinite(column) and math.isfinite(row)):
            return None
        if column < 0.0 or row < 0.0:
            return None
        if column > self.width - 1 or row > self.height - 1:
            return None
        # min() rather than a strict bound on the far edge: a point
        # landing exactly on the last centre is inside the sampleable
        # area, and it reads as fraction 1.0 of the cell before it.
        c0 = min(int(math.floor(column)), self.width - 2)
        r0 = min(int(math.floor(row)), self.height - 2)
        fx = column - c0
        fy = row - r0
        weights = (
            (1.0 - fx) * (1.0 - fy),
            fx * (1.0 - fy),
            (1.0 - fx) * fy,
            fx * fy,
        )
        corners = (
            self.values[r0 * self.width + c0],
            self.values[r0 * self.width + c0 + 1],
            self.values[(r0 + 1) * self.width + c0],
            self.values[(r0 + 1) * self.width + c0 + 1],
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
        return total / weight

    def sample(
        self, latitude: float, longitude: float, grid: Ostn15Grid
    ) -> float | None:
        """The height under a WGS84 latitude and longitude.

        The one place this module knows a datum exists, and it knows it by
        delegation. A point OSTN15 does not cover answers None rather than
        raising: to a sampler chain that is the same statement as a point
        off the edge of the mosaic, and the fallback source is exactly
        what should answer it.
        """
        try:
            easting, northing = to_bng(latitude, longitude, grid)
        except OutsideOstn15Error:
            return None
        return self.sample_bng(easting, northing)


class _Head:
    """The front of the file, so walking seven directories is one request.

    Also the one place a read is checked against the file's own length, so
    a tag pointing past the end is a sentence rather than a short buffer
    surfacing three frames later.
    """

    def __init__(self, source: ByteSource, size: int) -> None:
        self._source = source
        self._size = size
        self._block = source.read(0, min(size, _HEADER_PREFETCH_BYTES))

    @property
    def size(self) -> int:
        return self._size

    def read(self, start: int, length: int, name: str) -> bytes:
        if start < 0 or length < 0:
            raise CogError(
                f"{name} points to a negative position, so it is corrupt."
            )
        if start + length > self._size:
            raise CogError(
                f"{name} points to byte {start + length} of a file that is "
                f"{self._size} bytes long, so it is truncated or corrupt."
            )
        if start + length <= len(self._block):
            return self._block[start:start + length]
        return self._source.read(start, length)


def _unpack(order: str, kind: int, count: int, blob: bytes) -> list:
    code = _TYPE_FORMATS.get(kind)
    if code is None:
        return []
    return list(struct.unpack_from(order + code * count, blob, 0))


class CogReader:
    """A Cloud Optimized GeoTIFF, open and ready to be asked for a window.

    Opening reads the header and every directory in the chain, and nothing
    else: no pixels, no tile index. `read_window` is what pays for data,
    and it pays only for the tiles the window touches.
    """

    def __init__(
        self,
        source: ByteSource,
        head: _Head,
        order: str,
        levels: list[CogLevel],
        pixel_is_area: bool,
        origin_e: float,
        origin_n: float,
        nodata: float | None,
        epsg: int,
    ) -> None:
        self.source = source
        self.name = source.name
        self.levels = levels
        self.pixel_is_area = pixel_is_area
        self.origin_e = origin_e
        self.origin_n = origin_n
        self.nodata = nodata
        self.epsg = epsg
        self._head = head
        self._order = order
        self._swap = (order == "<") != _MACHINE_IS_LITTLE

    # -- opening ---------------------------------------------------------

    @classmethod
    def open(cls, source: ByteSource) -> "CogReader":
        name = source.name
        size = source.size()
        if size < 8:
            raise CogError(f"{name} is too short to be a TIFF at all.")
        head = _Head(source, size)
        order, big, first_ifd = _read_header(head, name)

        directories = []
        offset = first_ifd
        seen: set[int] = set()
        while offset:
            if offset in seen:
                raise CogError(
                    f"{name}'s image directories point back at each other, so "
                    f"the file is corrupt."
                )
            if len(directories) >= MAX_LEVELS:
                raise CogError(
                    f"{name} declares more than {MAX_LEVELS} image directories. "
                    f"A raster with overviews has a handful; this one is "
                    f"corrupt or is not the kind of file mapgen reads."
                )
            seen.add(offset)
            tags, offset = _read_ifd(head, offset, order, big, name)
            directories.append(tags)
        if not directories:
            raise CogError(f"{name} has no image directory where it says it has.")

        pixel_is_area, epsg = _read_geokeys(head, directories[0], order, name)
        scale, tie = _read_placement(head, directories[0], order, name)
        # Normalised to the raster's outer corner, once, here. See the
        # module docstring: after this line the half pixel question does
        # not exist anywhere else in the file.
        if pixel_is_area:
            origin_e, origin_n = tie[3], tie[4]
        else:
            origin_e, origin_n = tie[3] - scale[0] / 2.0, tie[4] + scale[1] / 2.0
        nodata = _read_nodata(head, directories[0], order, name)

        levels = [
            _read_level(head, tags, order, index, name, directories[0], scale)
            for index, tags in enumerate(directories)
        ]
        # Held finest first, which is the order every writer of overviews
        # produces anyway. Sorted rather than assumed, because read_window
        # walks this list and stops at the first level that fits: a file
        # whose chain ran the other way would otherwise be read at the
        # wrong resolution silently, which is the one failure mode this
        # module has no way to notice afterwards. `index` still records
        # which directory each level came from.
        levels.sort(key=lambda level: level.pixel_size)
        return cls(
            source, head, order, levels, pixel_is_area, origin_e, origin_n,
            nodata, epsg,
        )

    # -- reading ---------------------------------------------------------

    def read_window(
        self,
        e_min: float,
        n_min: float,
        e_max: float,
        n_max: float,
        max_pixels: int = MAX_WINDOW_PIXELS,
    ) -> BngWindow:
        """The rectangle of BNG metres, at the finest level that fits.

        Levels are walked finest first and the first one whose window fits
        `max_pixels` wins, so a site extent comes back at 1 m and a whole
        county comes back at 8 m rather than not at all. The chosen
        level's pixel size travels on the window, because that is the
        resolution every downstream file is then entitled to claim.

        The window is exactly the rectangle asked for, never clamped to
        the raster. An extent straddling the mosaic's edge comes back the
        size it was asked for with NaN beyond the edge, which keeps the
        geometry of everything built from it (contours especially) where
        the caller put it.
        """
        if self.epsg != BNG_EPSG:
            raise CogError(
                f"{self.name} is in EPSG:{self.epsg}, not EPSG:{BNG_EPSG} "
                f"(British National Grid). mapgen asks this reader for windows "
                f"in national grid metres, so this is the wrong file for the "
                f"job."
            )
        for value in (e_min, n_min, e_max, n_max):
            if not math.isfinite(value):
                raise CogError(
                    f"{self.name} was asked for a window whose corners are not "
                    f"all real numbers."
                )
        if e_max <= e_min or n_max <= n_min:
            raise CogError(
                f"{self.name} was asked for an empty window "
                f"({e_min}, {n_min}) to ({e_max}, {n_max}): its eastings or "
                f"northings do not increase."
            )
        if max_pixels < 1:
            raise CogError(
                f"{self.name} was asked for a window of at most {max_pixels} "
                f"pixels, which no window can be."
            )

        for level in self.levels:
            col0, row0, width, height = self._geometry(
                level, e_min, n_min, e_max, n_max
            )
            if width * height <= max_pixels:
                break
        else:
            coarsest = self.levels[-1]
            _, _, width, height = self._geometry(
                coarsest, e_min, n_min, e_max, n_max
            )
            raise CogError(
                f"{self.name} cannot answer that extent within {max_pixels} "
                f"pixels: even its coarsest level ({coarsest.pixel_size:g} by "
                f"{coarsest.pixel_height:g} m) needs {width} by {height}. Ask "
                f"for a smaller extent."
            )

        values = array("f", [math.nan]) * (width * height)
        self._fill(level, col0, row0, width, height, values)
        return BngWindow(
            e_origin=self.origin_e + col0 * level.pixel_size,
            n_top=self.origin_n - row0 * level.pixel_height,
            pixel_size=level.pixel_size,
            pixel_height=level.pixel_height,
            width=width,
            height=height,
            values=values,
        )

    def _geometry(
        self,
        level: CogLevel,
        e_min: float,
        n_min: float,
        e_max: float,
        n_max: float,
    ) -> tuple[int, int, int, int]:
        """The pixels of `level` covering a rectangle: column, row, size.

        Every pixel the rectangle touches, so the window always covers at
        least what was asked for rather than cutting a partial pixel off
        an edge.
        """
        col0 = math.floor((e_min - self.origin_e) / level.pixel_size)
        col1 = math.ceil((e_max - self.origin_e) / level.pixel_size)
        row0 = math.floor((self.origin_n - n_max) / level.pixel_height)
        row1 = math.ceil((self.origin_n - n_min) / level.pixel_height)
        return col0, row0, max(1, col1 - col0), max(1, row1 - row0)

    def _fill(
        self,
        level: CogLevel,
        col0: int,
        row0: int,
        width: int,
        height: int,
        values: array,
    ) -> None:
        """Fetch and unpack every tile the window touches.

        Tiles outside the raster's own tile grid are not fetched and are
        left NaN, which is what makes a window over the mosaic's edge, or
        wholly off it, an ordinary answer rather than an error.
        """
        tile_w, tile_h = level.tile_width, level.tile_height
        tx0 = max(0, col0 // tile_w)
        tx1 = min(level.tiles_across - 1, (col0 + width - 1) // tile_w)
        ty0 = max(0, row0 // tile_h)
        ty1 = min(level.tiles_down - 1, (row0 + height - 1) // tile_h)
        if tx1 < tx0 or ty1 < ty0:
            return
        wanted = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        if wanted > MAX_TILES_PER_WINDOW:
            raise CogError(
                f"{self.name} would need {wanted} tiles for that window, over "
                f"mapgen's limit of {MAX_TILES_PER_WINDOW}. Ask for a smaller "
                f"extent."
            )

        plan: list[tuple[int, int, int, int]] = []
        across = tx1 - tx0 + 1
        for ty in range(ty0, ty1 + 1):
            # One contiguous slice per tile row, out of each index array,
            # because tiles of a row are consecutive in it. Never the
            # whole array: at full resolution that is 5.4 MB per raster.
            first = ty * level.tiles_across + tx0
            offsets = self._index_slice(level.tile_offsets, first, across)
            counts = self._index_slice(level.tile_byte_counts, first, across)
            for step in range(across):
                offset, count = offsets[step], counts[step]
                if offset <= 0 or count <= 0:
                    # A sparse tile: the COG says it holds nothing, and
                    # nothing is what the window keeps for it.
                    continue
                if count > MAX_TILE_BYTES:
                    raise CogError(
                        f"{self.name} declares a {count} byte tile, over "
                        f"mapgen's limit of {MAX_TILE_BYTES}, so its tile "
                        f"index is corrupt."
                    )
                plan.append((offset, count, tx0 + step, ty))
        plan.sort()

        for start, end, members in _coalesce(plan):
            if end > self._head.size:
                raise CogError(
                    f"{self.name}'s tile index points past the end of the "
                    f"file, so it is truncated or corrupt."
                )
            payload = self.source.read(start, end - start)
            for offset, count, tx, ty in members:
                block = self._decode(
                    payload[offset - start:offset - start + count], level
                )
                self._paste(block, level, tx, ty, col0, row0, width, height, values)

    def _index_slice(self, ref: TagArray, first: int, count: int) -> list[int]:
        if first < 0 or first + count > ref.count:
            raise CogError(
                f"{self.name} lists {ref.count} entries in tag {ref.tag} where "
                f"tile {first + count - 1} was needed, so its tile index does "
                f"not match its own tile grid."
            )
        item = ref.item_size
        if ref.inline is not None:
            blob = ref.inline[first * item:(first + count) * item]
        else:
            blob = self._head.read(
                ref.offset + first * item, count * item, self.name
            )
        return _unpack(self._order, ref.kind, count, blob)

    def _decode(self, payload: bytes, level: CogLevel) -> array:
        expected = level.tile_width * level.tile_height * level.sample_stride
        if level.compression == _COMPRESSION_NONE:
            raw = payload
        else:
            try:
                # Bounded at the decompressor rather than after it: a tile
                # that unpacks to a gigabyte is refused without ever
                # holding a gigabyte.
                raw = zlib.decompressobj().decompress(payload, expected)
            except zlib.error as exc:
                raise CogError(
                    f"{self.name}'s deflate tile data could not be unpacked: "
                    f"{exc}"
                ) from None
        if len(raw) < expected:
            raise CogError(
                f"{self.name} holds {len(raw)} bytes of tile data where "
                f"{expected} were needed, so it is truncated."
            )
        block = array(level.sample_code)
        block.frombytes(raw[:expected])
        if self._swap:
            block.byteswap()
        if level.sample_code != "f":
            block = array("f", block)
        if self.nodata is not None:
            _blank_nodata(block, self.nodata)
        return block

    def _paste(
        self,
        block: array,
        level: CogLevel,
        tx: int,
        ty: int,
        col0: int,
        row0: int,
        width: int,
        height: int,
        values: array,
    ) -> None:
        tile_w, tile_h = level.tile_width, level.tile_height
        left = tx * tile_w - col0
        top = ty * tile_h - row0
        x0 = max(0, left)
        # Bounded by the IMAGE, not only by the window and the tile. TIFF
        # pads the last tile of a row or column out to the full tile size
        # and says nothing about what goes in the padding: on the real
        # mosaic that is 105 columns east and 216 rows south of the
        # declared 191007 by 233000, and whatever the producer left there
        # is not terrain. Reading it as terrain is silent, plausible and
        # exactly wrong at the one place a mosaic is most likely to be
        # sampled, its edge.
        x1 = min(width, left + tile_w, level.width - col0)
        if x1 <= x0:
            return
        last_row = min(height, level.height - row0)
        for row in range(tile_h):
            y = top + row
            if y < 0:
                continue
            if y >= last_row:
                break
            source = row * tile_w + (x0 - left)
            target = y * width + x0
            values[target:target + (x1 - x0)] = block[source:source + (x1 - x0)]


def read_full_window(reader: CogReader) -> BngWindow:
    """The whole raster at its finest level, as one BngWindow.

    For reading back a small raster this project itself wrote (a packaged
    LiDAR tile, Task 6; a synthetic fixture in a test) without already
    holding the BngWindow it was written from. `levels[0]` is always the
    finest level (see `CogReader.open`'s own sort), which for anything
    `geotiff_write.py` produced is the only level there is, since that
    writer never emits overviews.

    Promoted out of `mapgen.sources.lidar_wales._full_bounds` (Task 7),
    which read a packaged DTM back this same way to build contours; Task
    7's own height fusion needed the identical read and would otherwise
    have been a second copy of it. Both call sites now share this one.

    The far corner is inset by half a pixel rather than queried at the
    raster's true outer edge, matching test_geotiff_write.py's own
    `_safe_bounds`: querying the exact edge risks `CogReader._geometry`'s
    own `ceil` rounding a division up by one pixel when `width *
    pixel_size` does not land back on an exact integer in floating point,
    which would ask `read_window` for one column or row more than the
    file actually has. The near corner needs no inset: it is exactly
    where `_geometry`'s `floor` already lands.
    """
    level = reader.levels[0]
    e_min = reader.origin_e
    e_max = reader.origin_e + (level.width - 0.5) * level.pixel_size
    n_max = reader.origin_n
    n_min = reader.origin_n - (level.height - 0.5) * level.pixel_height
    return reader.read_window(e_min, n_min, e_max, n_max)


def _blank_nodata(block: array, nodata: float) -> None:
    """Every nodata sample in a decoded tile, turned into NaN.

    `count` and `index` do their searching in C, which matters: a full
    window is sixteen million samples and a Python loop over them costs
    more than the download did. The comparison is exact rather than
    tolerant because the sentinel was written as a float32 and read back as
    the same float32, so there is nothing to round; a tolerance here would
    quietly delete real heights near the value instead.
    """
    sentinel = struct.unpack("<f", struct.pack("<f", nodata))[0]
    hits = block.count(sentinel)
    if hits == 0:
        return
    if hits == len(block):
        for index in range(len(block)):
            block[index] = math.nan
        return
    at = 0
    for _ in range(hits):
        at = block.index(sentinel, at)
        block[at] = math.nan
        at += 1


def _coalesce(
    plan: list[tuple[int, int, int, int]]
) -> list[tuple[int, int, list[tuple[int, int, int, int]]]]:
    """Tiles whose byte ranges touch, gathered into single requests.

    A COG writes the tiles of a row consecutively, so a window's tiles
    within one row are almost always one contiguous run: this turns nine
    round trips into three. Contiguous only, never bridging a gap, because
    a gap is data the window does not want and paying for it is the thing
    this whole module avoids.
    """
    groups: list[tuple[int, int, list[tuple[int, int, int, int]]]] = []
    for item in plan:
        offset, count = item[0], item[1]
        if groups:
            start, end, members = groups[-1]
            if offset == end and offset + count - start <= _COALESCE_LIMIT_BYTES:
                members.append(item)
                groups[-1] = (start, offset + count, members)
                continue
        groups.append((offset, offset + count, [item]))
    return groups


# --------------------------------------------------------------------------
# Header and directory parsing.
# --------------------------------------------------------------------------


def _read_header(head: _Head, name: str) -> tuple[str, bool, int]:
    """Byte order, dialect, and where the first directory is.

    BigTIFF is not a variant of classic TIFF here: it declares its own
    offset width, its entry count is eight bytes rather than two, and its
    entries are twenty rather than twelve. Both are read because the
    mosaics are BigTIFF and mapgen's own packaged rasters are classic.
    """
    mark = head.read(0, 2, name)
    if mark == b"II":
        order = "<"
    elif mark == b"MM":
        order = ">"
    else:
        raise CogError(
            f"{name} does not start with a TIFF byte order mark, so it is not "
            f"a TIFF."
        )
    version = struct.unpack(order + "H", head.read(2, 2, name))[0]
    if version == 42:
        return order, False, struct.unpack(order + "I", head.read(4, 4, name))[0]
    if version != 43:
        raise CogError(f"{name} is not a TIFF: its version word is {version}.")
    offset_size, padding = struct.unpack(order + "HH", head.read(4, 4, name))
    if offset_size != 8 or padding != 0:
        raise CogError(
            f"{name} is a BigTIFF declaring {offset_size} byte offsets. Only "
            f"the standard 8 byte form exists, so this file is corrupt."
        )
    return order, True, struct.unpack(order + "Q", head.read(8, 8, name))[0]


def _read_ifd(
    head: _Head, offset: int, order: str, big: bool, name: str
) -> tuple[dict[int, TagArray], int]:
    if big:
        count = struct.unpack(order + "Q", head.read(offset, 8, name))[0]
        entry_size, entries_at, capacity = 20, offset + 8, 8
    else:
        count = struct.unpack(order + "H", head.read(offset, 2, name))[0]
        entry_size, entries_at, capacity = 12, offset + 2, 4
    if count == 0:
        raise CogError(f"{name} has an image directory with no tags in it.")
    if count > MAX_IFD_ENTRIES:
        raise CogError(
            f"{name} declares an image directory of {count} tags, over "
            f"mapgen's limit of {MAX_IFD_ENTRIES}, so it is corrupt."
        )
    blob = head.read(entries_at, count * entry_size, name)
    tags: dict[int, TagArray] = {}
    for index in range(count):
        base = index * entry_size
        if big:
            tag, kind, length = struct.unpack_from(order + "HHQ", blob, base)
            field = blob[base + 12:base + 20]
        else:
            tag, kind, length = struct.unpack_from(order + "HHI", blob, base)
            field = blob[base + 8:base + 12]
        size = _TYPE_SIZES.get(kind)
        if size is None:
            continue
        if length * size <= capacity:
            tags[tag] = TagArray(tag, kind, length, 0, field)
        else:
            pointer = struct.unpack(
                order + ("Q" if big else "I"), field
            )[0]
            tags[tag] = TagArray(tag, kind, length, pointer, None)
    next_at = entries_at + count * entry_size
    next_ifd = struct.unpack(
        order + ("Q" if big else "I"), head.read(next_at, 8 if big else 4, name)
    )[0]
    return tags, next_ifd


def _values(
    head: _Head, ref: TagArray | None, order: str, name: str
) -> list:
    """A whole tag's values, for the short tags it is safe to read whole."""
    if ref is None:
        return []
    if ref.byte_length > MAX_TAG_ARRAY_BYTES:
        raise CogError(
            f"{name}'s tag {ref.tag} claims {ref.byte_length} bytes of values, "
            f"over mapgen's limit of {MAX_TAG_ARRAY_BYTES}, so it is corrupt."
        )
    if ref.inline is not None:
        blob = ref.inline[:ref.byte_length]
    else:
        blob = head.read(ref.offset, ref.byte_length, name)
    if ref.kind == 2:
        return [blob.split(b"\0", 1)[0]]
    return _unpack(order, ref.kind, ref.count, blob)


def _one(head: _Head, tags: dict[int, TagArray], tag: int, order: str,
         name: str, default: int | None = None) -> int | None:
    values = _values(head, tags.get(tag), order, name)
    return int(values[0]) if values else default


def _read_geokeys(
    head: _Head, tags: dict[int, TagArray], order: str, name: str
) -> tuple[bool, int]:
    """The pixel convention and the EPSG code, both required.

    Required rather than assumed, exactly as in geotiff.py and for the
    same reason: a raster read in the wrong coordinate system does not
    fail, it renders, several hundred kilometres from the site.
    """
    directory = _values(head, tags.get(_GEO_KEY_DIRECTORY), order, name)
    if len(directory) < 4:
        raise CogError(
            f"{name} carries no GeoTIFF key directory, so it does not say "
            f"which coordinate system it is in."
        )
    keys = {}
    for index in range(4, len(directory) - 3, 4):
        key, location, count, value = directory[index:index + 4]
        if location == 0 and count == 1:
            keys[key] = value
    model = keys.get(_GT_MODEL_TYPE)
    if model != _MODEL_TYPE_PROJECTED:
        raise CogError(
            f"{name} is not a projected raster (its GeoTIFF model type is "
            f"{model}, not {_MODEL_TYPE_PROJECTED}). This reader works in "
            f"projected metres."
        )
    epsg = keys.get(_PROJECTED_CS_TYPE)
    if epsg is None:
        raise CogError(
            f"{name} does not name its projected coordinate system (GeoTIFF "
            f"key {_PROJECTED_CS_TYPE}), so where it is cannot be established."
        )
    raster = keys.get(_GT_RASTER_TYPE, _RASTER_PIXEL_IS_AREA)
    if raster not in (_RASTER_PIXEL_IS_AREA, _RASTER_PIXEL_IS_POINT):
        raise CogError(
            f"{name} declares raster type {raster}, which is neither "
            f"PixelIsArea ({_RASTER_PIXEL_IS_AREA}) nor PixelIsPoint "
            f"({_RASTER_PIXEL_IS_POINT}), so its samples cannot be placed."
        )
    return raster == _RASTER_PIXEL_IS_AREA, int(epsg)


def _read_placement(
    head: _Head, tags: dict[int, TagArray], order: str, name: str
) -> tuple[list[float], list[float]]:
    scale = _values(head, tags.get(_MODEL_PIXEL_SCALE), order, name)
    tie = _values(head, tags.get(_MODEL_TIEPOINT), order, name)
    if not scale or not tie:
        if tags.get(_MODEL_TRANSFORMATION) is not None:
            raise CogError(
                f"{name} places itself with a ModelTransformation rather than "
                f"a pixel scale and a tie point. mapgen does not read those: "
                f"a rotated raster read as an unrotated one is wrong without "
                f"ever failing."
            )
        raise CogError(
            f"{name} carries no GeoTIFF pixel scale and tie point, so there is "
            f"no way to say where on the ground it is."
        )
    if len(scale) < 2 or len(tie) < 6:
        raise CogError(
            f"{name}'s GeoTIFF pixel scale or tie point is too short to place "
            f"it."
        )
    if not (
        math.isfinite(scale[0]) and math.isfinite(scale[1])
        and scale[0] > 0.0 and scale[1] > 0.0
    ):
        raise CogError(
            f"{name} declares a pixel scale of {scale[0]} by {scale[1]}, which "
            f"is not a size on the ground."
        )
    # Anisotropy at level 0 is read honestly, not refused. It used to be
    # refused here on the grounds that everything downstream of a window
    # quotes one resolution for it; that argument does not survive contact
    # with a packaged raster written from an overview-level window (Task
    # 5's geotiff_write.py), where the two axes genuinely differ by more
    # than a rounding error and refusing the file this project's own
    # writer produced is a worse failure than reading it. `CogLevel` and
    # `BngWindow` both carry pixel_size and pixel_height separately for
    # exactly this reason (see the module docstring), and every piece of
    # arithmetic downstream already uses the one belonging to its axis.
    return [float(value) for value in scale], [float(value) for value in tie]


def _read_nodata(
    head: _Head, tags: dict[int, TagArray], order: str, name: str
) -> float | None:
    """GDAL_NODATA, which is an ASCII string in the tag, or None.

    An unparseable value is read as no declaration rather than as a
    refusal, matching geotiff.py: a raster that says something odd here is
    still a readable raster, and the alternative is refusing a file over a
    tag nothing in it uses.
    """
    raw = _values(head, tags.get(_GDAL_NODATA), order, name)
    if not raw:
        return None
    try:
        value = float(raw[0].decode("latin-1").strip())
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return value if math.isfinite(value) else None


def _read_level(
    head: _Head,
    tags: dict[int, TagArray],
    order: str,
    index: int,
    name: str,
    first: dict[int, TagArray],
    scale: list[float],
) -> CogLevel:
    """One directory, checked and measured.

    Every level is checked, not just the full resolution one: an overview
    written with a different compression or sample format would otherwise
    be discovered by decoding it into noise at the moment a large extent
    was asked for.
    """
    where = "" if index == 0 else f" (overview level {index})"
    width = _one(head, tags, _IMAGE_WIDTH, order, name)
    height = _one(head, tags, _IMAGE_LENGTH, order, name)
    if not width or not height:
        raise CogError(f"{name}{where} does not say how big its image is.")

    samples = _one(head, tags, _SAMPLES_PER_PIXEL, order, name, 1)
    if samples != 1:
        raise CogError(
            f"{name}{where} has {samples} samples per pixel. A height raster "
            f"has one."
        )
    planar = _one(head, tags, _PLANAR_CONFIGURATION, order, name, 1)
    if planar != 1:
        raise CogError(
            f"{name}{where} stores its samples in separate planes, which "
            f"mapgen does not read."
        )
    compression = _one(head, tags, _COMPRESSION, order, name, 1)
    if compression not in (
        _COMPRESSION_NONE, _COMPRESSION_DEFLATE, _COMPRESSION_ADOBE_DEFLATE
    ):
        named = _COMPRESSION_NAMES.get(compression, "an unrecognised scheme")
        raise CogError(
            f"{name}{where} is compressed with {named} (tag value "
            f"{compression}). This reader reads uncompressed and deflate "
            f"rasters."
        )
    predictor = _one(head, tags, _PREDICTOR, order, name, 1)
    if predictor != 1:
        raise CogError(
            f"{name}{where} uses TIFF predictor {predictor}. This reader reads "
            f"only unpredicted samples; decoding one as the other produces "
            f"heights that look plausible and are wrong."
        )
    bits = _one(head, tags, _BITS_PER_SAMPLE, order, name)
    sample_format = _one(head, tags, _SAMPLE_FORMAT, order, name)
    if sample_format is None:
        raise CogError(
            f"{name}{where} does not declare a sample format, so what its "
            f"numbers mean cannot be established. mapgen will not assume."
        )
    if sample_format == _SAMPLE_FORMAT_IEEE_FLOAT and bits == 32:
        code, stride = "f", 4
    elif sample_format == _SAMPLE_FORMAT_SIGNED_INT and bits == 16:
        code, stride = "h", 2
    else:
        raise CogError(
            f"{name}{where} holds {bits} bit samples of format "
            f"{sample_format}. mapgen reads 32 bit IEEE floats and 16 bit "
            f"signed integers."
        )

    tile_width = _one(head, tags, _TILE_WIDTH, order, name)
    tile_height = _one(head, tags, _TILE_LENGTH, order, name)
    if not tile_width or not tile_height:
        raise CogError(
            f"{name}{where} is not tiled. mapgen reads windows out of tiled "
            f"rasters; a stripped one has to be read whole, which is what a "
            f"Cloud Optimized GeoTIFF exists to avoid."
        )
    if tile_width > MAX_TILE_DIMENSION or tile_height > MAX_TILE_DIMENSION:
        raise CogError(
            f"{name}{where} declares {tile_width} by {tile_height} tiles, over "
            f"mapgen's limit of {MAX_TILE_DIMENSION}, so it is corrupt."
        )
    offsets = tags.get(_TILE_OFFSETS)
    counts = tags.get(_TILE_BYTE_COUNTS)
    if offsets is None or counts is None:
        raise CogError(
            f"{name}{where} is tiled but does not list where its tiles are."
        )
    across = (width + tile_width - 1) // tile_width
    down = (height + tile_height - 1) // tile_height
    if offsets.count < across * down or counts.count < across * down:
        raise CogError(
            f"{name}{where} declares {across * down} tiles but lists "
            f"{min(offsets.count, counts.count)} of them."
        )

    if index == 0:
        pixel_size, pixel_height = float(scale[0]), float(scale[1])
    else:
        # No geo tags of their own, by construction: an overview covers
        # the full resolution extent, so its pixel size is level 0's
        # times the dimension ratio, which is what GDAL itself does.
        #
        # Per axis, and that is not pedantry. The two ratios disagree
        # whenever the dimensions did not halve evenly, which on the real
        # DTM is every level: 191007/95504 is 1.9999895 across while
        # 233000/116500 is exactly 2 down. Taking the width's answer for
        # both puts a Barry window on level 1 about 1.2 m north of its own
        # pixels, and roughly 16 m at level 6, because the error is the
        # row index times the difference.
        base_width = _one(head, first, _IMAGE_WIDTH, order, name) or width
        base_height = _one(head, first, _IMAGE_LENGTH, order, name) or height
        pixel_size = float(scale[0]) * base_width / width
        pixel_height = float(scale[1]) * base_height / height
    return CogLevel(
        index=index,
        width=width,
        height=height,
        pixel_size=pixel_size,
        pixel_height=pixel_height,
        tile_width=tile_width,
        tile_height=tile_height,
        tiles_across=across,
        tiles_down=down,
        compression=compression,
        sample_code=code,
        sample_stride=stride,
        tile_offsets=offsets,
        tile_byte_counts=counts,
    )
