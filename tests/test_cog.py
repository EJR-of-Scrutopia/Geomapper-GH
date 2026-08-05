"""cog.py's suite, run against COGs this file builds byte by byte.

`_make_cog` writes a real TIFF: header, tile data, tag values, and a chain
of image file directories, in either the classic or the BigTIFF dialect.
Nothing here mocks the parser or hands it a dictionary of tags it was
supposed to have read; every test below drives the same bytes a GDAL
written file would present, through the same ByteSource protocol the
remote reader uses, so what is under test is the parsing and the range
arithmetic rather than a description of them.

The builder writes tile payloads back to back and with no padding between
them, which is what GDAL does and what makes the coalescing test's
expected single range computable by hand.
"""

import math
import struct
import zlib
from array import array
from dataclasses import dataclass

import pytest
import requests

from mapgen import cog
from mapgen.cog import (
    CogError,
    CogReader,
    FileByteSource,
    HttpByteSource,
)

# The real mosaic's own tie point, so the numbers in these tests read like
# the numbers in a live run.
_TIE_E = 164_993.0
_TIE_N = 397_000.0

_TYPE_FORMATS = {3: "H", 4: "I", 12: "d", 16: "Q"}


@dataclass(frozen=True)
class _SyntheticCog:
    """The bytes, plus where the builder put every tile.

    The offsets and byte counts are returned rather than re-parsed out of
    the file, so a test asserting which ranges were requested is comparing
    against what was written, not against what the reader thinks was
    written.
    """

    data: bytes
    tile_offsets: list[list[int]]
    tile_counts: list[list[int]]


def _grid(width: int, height: int, value) -> list:
    return [value(x, y) for y in range(height) for x in range(width)]


def _make_cog(
    levels,
    *,
    tile_size: int = 16,
    pixel_size: float = 1.0,
    tiepoint: tuple[float, float] = (_TIE_E, _TIE_N),
    pixel_is_area: bool = True,
    nodata: float | None = -9999.0,
    epsg: int = 27700,
    model_type: int = 1,
    compression: int = 8,
    predictor: int = 1,
    sample_format: int = 3,
    bits: int = 32,
    bigtiff: bool = False,
    sparse: tuple = (),
) -> _SyntheticCog:
    """A tiled TIFF holding `levels`, each a (width, height, values) triple.

    Level 0 carries the georeference; the rest are overviews with no geo
    tags of their own, exactly as GDAL writes them and as the real Welsh
    mosaics are. `sparse` names (level, tile index) pairs to write with
    offset 0 and byte count 0, which is how a COG records a tile that is
    entirely nodata.
    """
    code = "f" if bits == 32 else "h"
    fill = 0.0 if nodata is None else nodata
    fill = int(fill) if code == "h" else float(fill)

    out = bytearray()
    if bigtiff:
        out += b"II" + struct.pack("<HHH", 43, 8, 0) + struct.pack("<Q", 0)
        chain_at, capacity, offset_format = 8, 8, "Q"
    else:
        out += b"II" + struct.pack("<HI", 42, 0)
        chain_at, capacity, offset_format = 4, 4, "I"

    all_offsets: list[list[int]] = []
    all_counts: list[list[int]] = []
    for level_index, (width, height, values) in enumerate(levels):
        across = (width + tile_size - 1) // tile_size
        down = (height + tile_size - 1) // tile_size
        offsets: list[int] = []
        counts: list[int] = []
        for index in range(across * down):
            if (level_index, index) in sparse:
                offsets.append(0)
                counts.append(0)
                continue
            tile_x = (index % across) * tile_size
            tile_y = (index // across) * tile_size
            block = array(code, [fill]) * (tile_size * tile_size)
            for row in range(tile_size):
                y = tile_y + row
                if y >= height:
                    break
                span = min(tile_size, width - tile_x)
                source = y * width + tile_x
                block[row * tile_size:row * tile_size + span] = array(
                    code, values[source:source + span]
                )
            raw = block.tobytes()
            payload = zlib.compress(raw) if compression in (8, 32946) else raw
            offsets.append(len(out))
            counts.append(len(payload))
            out += payload
        all_offsets.append(offsets)
        all_counts.append(counts)

    geo_keys = [
        1, 1, 0, 4,
        1024, 0, 1, model_type,
        1025, 0, 1, 1 if pixel_is_area else 2,
        3072, 0, 1, epsg,
        3076, 0, 1, 9001,
    ]
    for level_index, (width, height, _values) in enumerate(levels):
        entries = [
            (256, 4, [width]),
            (257, 4, [height]),
            (258, 3, [bits]),
            (259, 3, [compression]),
            (262, 3, [1]),
            (277, 3, [1]),
            (284, 3, [1]),
            (317, 3, [predictor]),
            (322, 4, [tile_size]),
            (323, 4, [tile_size]),
            (324, 16 if bigtiff else 4, all_offsets[level_index]),
            (325, 4, all_counts[level_index]),
            (339, 3, [sample_format]),
        ]
        if level_index == 0:
            entries.append((33550, 12, [pixel_size, pixel_size, 0.0]))
            entries.append(
                (33922, 12, [0.0, 0.0, 0.0, tiepoint[0], tiepoint[1], 0.0])
            )
            entries.append((34735, 3, geo_keys))
        if nodata is not None:
            entries.append((42113, 2, f"{nodata:g}".encode("ascii") + b"\0"))
        entries.sort()

        packed = []
        for tag, kind, values in entries:
            if kind == 2:
                blob = values
                count = len(values)
            else:
                blob = struct.pack(
                    "<" + _TYPE_FORMATS[kind] * len(values), *values
                )
                count = len(values)
            if len(blob) <= capacity:
                field = blob.ljust(capacity, b"\0")
            else:
                if len(out) % 2:
                    out += b"\0"
                field = struct.pack("<" + offset_format, len(out))
                out += blob
            packed.append((tag, kind, count, field))

        if len(out) % 2:
            out += b"\0"
        struct.pack_into("<" + offset_format, out, chain_at, len(out))
        out += (
            struct.pack("<Q", len(packed))
            if bigtiff
            else struct.pack("<H", len(packed))
        )
        for tag, kind, count, field in packed:
            head = (
                struct.pack("<HHQ", tag, kind, count)
                if bigtiff
                else struct.pack("<HHI", tag, kind, count)
            )
            out += head + field
        chain_at = len(out)
        out += struct.pack("<" + offset_format, 0)

    return _SyntheticCog(bytes(out), all_offsets, all_counts)


def _reader(tmp_path, built: _SyntheticCog, name: str = "synthetic.tif"):
    path = tmp_path / name
    path.write_bytes(built.data)
    return CogReader.open(FileByteSource(path))


class _CountingSource:
    """A ByteSource that remembers every range it was asked for."""

    def __init__(self, data: bytes, name: str = "counted.tif") -> None:
        self.data = data
        self.name = name
        self.reads: list[tuple[int, int]] = []

    def size(self) -> int:
        return len(self.data)

    def read(self, start: int, length: int) -> bytes:
        self.reads.append((start, length))
        chunk = self.data[start:start + length]
        if len(chunk) < length:
            raise CogError(f"{self.name} ends before byte {start + length}.")
        return chunk


@pytest.fixture
def counting_source():
    def build(data: bytes) -> _CountingSource:
        return _CountingSource(data)

    return build


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict, body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.body_read = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_content(self, chunk_size: int):
        self.body_read = True
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]


class _RangeSession:
    """A requests.Session stand-in that answers, or mishandles, ranges.

    `mode` picks which server this is: an honest one, one that ignores
    Range and streams the whole file, one that answers 206 without saying
    which bytes it sent, one that sends fewer bytes than it was asked
    for, one that times out, and one that times out only on the first
    attempt.
    """

    def __init__(self, data: bytes, mode: str = "honest") -> None:
        self.data = data
        self.mode = mode
        self.calls: list[str] = []
        self.responses: list[_FakeResponse] = []

    def get(self, url, headers=None, timeout=None, stream=False):
        response = self._answer(headers)
        self.responses.append(response)
        return response

    def _answer(self, headers):
        header = (headers or {}).get("Range", "")
        self.calls.append(header)
        if self.mode == "timeout" or (
            self.mode == "flaky" and len(self.calls) == 1
        ):
            raise requests.exceptions.Timeout("the fake server stalled")
        if self.mode == "server_error" and len(self.calls) == 1:
            return _FakeResponse(503, {}, b"")
        start, end = header.removeprefix("bytes=").split("-")
        first, last = int(start), int(end)
        body = self.data[first:last + 1]
        if self.mode == "ignores_range":
            return _FakeResponse(200, {}, self.data)
        if self.mode == "no_content_range":
            return _FakeResponse(206, {}, body)
        if self.mode == "short_body":
            body = body[: max(0, len(body) - 1)]
        return _FakeResponse(
            206,
            {"Content-Range": f"bytes {first}-{last}/{len(self.data)}"},
            body,
        )


@pytest.fixture
def fake_200_source():
    built = _make_cog([(8, 8, _grid(8, 8, lambda x, y: float(x)))])
    return HttpByteSource(
        "https://example.invalid/cogs/wales_dtm_32bit_cog.tif?token=secret",
        session=_RangeSession(built.data, mode="ignores_range"),
    )


# --------------------------------------------------------------------------
# Reading windows.
# --------------------------------------------------------------------------


def test_reads_window_values_exactly_where_they_were_written(tmp_path):
    values = _grid(40, 40, lambda x, y: float(y * 100 + x))
    reader = _reader(tmp_path, _make_cog([(40, 40, values)]))

    window = reader.read_window(
        _TIE_E + 10.0, _TIE_N - 20.0, _TIE_E + 15.0, _TIE_N - 15.0
    )

    assert (window.width, window.height) == (5, 5)
    assert window.pixel_size == pytest.approx(1.0)
    assert window.e_origin == pytest.approx(_TIE_E + 10.0)
    assert window.n_top == pytest.approx(_TIE_N - 15.0)
    assert window.bounds() == pytest.approx(
        (_TIE_E + 10.0, _TIE_N - 20.0, _TIE_E + 15.0, _TIE_N - 15.0)
    )
    for row in range(5):
        for column in range(5):
            assert window.values[row * 5 + column] == pytest.approx(
                (15 + row) * 100 + (10 + column)
            )


def test_pixel_is_area_centres_offset_by_half_a_pixel(tmp_path):
    values = _grid(8, 8, lambda x, y: float(y * 10 + x))
    area = _reader(tmp_path, _make_cog([(8, 8, values)]), "area.tif")
    point = _reader(
        tmp_path, _make_cog([(8, 8, values)], pixel_is_area=False), "point.tif"
    )

    assert area.pixel_is_area is True
    assert point.pixel_is_area is False
    # The tie point is the outer corner of pixel (0,0) for PixelIsArea and
    # its centre for PixelIsPoint, so the two rasters' corners differ by
    # half a pixel in each direction.
    assert area.origin_e == pytest.approx(_TIE_E)
    assert area.origin_n == pytest.approx(_TIE_N)
    assert point.origin_e == pytest.approx(_TIE_E - 0.5)
    assert point.origin_n == pytest.approx(_TIE_N + 0.5)

    area_window = area.read_window(
        area.origin_e, area.origin_n - 8.0, area.origin_e + 8.0, area.origin_n
    )
    point_window = point.read_window(
        point.origin_e,
        point.origin_n - 8.0,
        point.origin_e + 8.0,
        point.origin_n,
    )
    assert area_window.sample_bng(_TIE_E + 0.5, _TIE_N - 0.5) == pytest.approx(
        values[0]
    )
    assert point_window.sample_bng(_TIE_E, _TIE_N) == pytest.approx(values[0])
    # And the half pixel really is a half pixel: the area raster cannot
    # answer at its own tie point, which lies on a corner, not a centre.
    assert area_window.sample_bng(_TIE_E, _TIE_N) is None


def test_nodata_pixels_become_nan_and_sample_returns_none(tmp_path):
    values = _grid(8, 8, lambda x, y: 5.0)
    for y in range(4):
        for x in range(4):
            values[y * 8 + x] = -9999.0
    reader = _reader(tmp_path, _make_cog([(8, 8, values)]))
    assert reader.nodata == pytest.approx(-9999.0)

    window = reader.read_window(_TIE_E, _TIE_N - 8.0, _TIE_E + 8.0, _TIE_N)

    assert math.isnan(window.values[0])
    assert window.values[7] == pytest.approx(5.0)
    # All four corners absent: no answer, not a zero and not a sentinel.
    assert window.sample_bng(_TIE_E + 1.5, _TIE_N - 1.5) is None
    assert window.sample_bng(_TIE_E + 6.5, _TIE_N - 6.5) == pytest.approx(5.0)
    # One absent corner out of four takes zero weight rather than dragging
    # the answer towards the sentinel.
    assert window.sample_bng(_TIE_E + 4.0, _TIE_N - 4.0) == pytest.approx(5.0)


def test_bilinear_matches_hand_computed_value(tmp_path):
    reader = _reader(tmp_path, _make_cog([(2, 2, [10.0, 20.0, 30.0, 50.0])]))
    window = reader.read_window(_TIE_E, _TIE_N - 2.0, _TIE_E + 2.0, _TIE_N)

    # Column 0.25, row 0.5 between the pixel centres:
    #   0.75 * 0.5 * 10 + 0.25 * 0.5 * 20 + 0.75 * 0.5 * 30 + 0.25 * 0.5 * 50
    #   = 3.75 + 2.5 + 11.25 + 6.25 = 23.75
    assert window.sample_bng(_TIE_E + 0.75, _TIE_N - 1.0) == pytest.approx(
        23.75, abs=1e-9
    )


def test_level_selection_prefers_finest_that_fits_max_pixels(tmp_path):
    built = _make_cog(
        [
            (64, 64, _grid(64, 64, lambda x, y: 1.0)),
            (32, 32, _grid(32, 32, lambda x, y: 2.0)),
            (16, 16, _grid(16, 16, lambda x, y: 3.0)),
        ]
    )
    reader = _reader(tmp_path, built)
    assert [level.pixel_size for level in reader.levels] == [1.0, 2.0, 4.0]

    corners = (_TIE_E, _TIE_N - 40.0, _TIE_E + 40.0, _TIE_N)
    finest = reader.read_window(*corners, max_pixels=4096)
    assert finest.pixel_size == pytest.approx(1.0)
    assert (finest.width, finest.height) == (40, 40)
    assert finest.values[0] == pytest.approx(1.0)

    middle = reader.read_window(*corners, max_pixels=1000)
    assert middle.pixel_size == pytest.approx(2.0)
    assert (middle.width, middle.height) == (20, 20)
    assert middle.values[0] == pytest.approx(2.0)

    coarsest = reader.read_window(*corners, max_pixels=200)
    assert coarsest.pixel_size == pytest.approx(4.0)
    assert coarsest.values[0] == pytest.approx(3.0)

    with pytest.raises(CogError, match="coarsest"):
        reader.read_window(*corners, max_pixels=4)


def test_a_window_beyond_the_raster_edge_is_nan_not_a_refusal(tmp_path):
    values = _grid(8, 8, lambda x, y: 5.0)
    reader = _reader(tmp_path, _make_cog([(8, 8, values)]))

    # Half on the raster, half off its eastern edge.
    window = reader.read_window(
        _TIE_E + 4.0, _TIE_N - 4.0, _TIE_E + 12.0, _TIE_N
    )

    assert (window.width, window.height) == (8, 4)
    assert window.values[0] == pytest.approx(5.0)
    assert all(math.isnan(window.values[row * 8 + 4]) for row in range(4))
    assert window.sample_bng(_TIE_E + 10.0, _TIE_N - 2.0) is None


def test_sparse_tiles_come_back_as_nodata(tmp_path):
    # A tile written with offset 0 and byte count 0 is how a COG records
    # one that is entirely nodata; the mosaic is full of them over England
    # and the sea, and reading one as a zero length deflate stream would
    # either raise or fabricate a plane of zeros.
    values = _grid(32, 16, lambda x, y: 7.0)
    reader = _reader(tmp_path, _make_cog([(32, 16, values)], sparse=((0, 0),)))

    window = reader.read_window(_TIE_E, _TIE_N - 16.0, _TIE_E + 32.0, _TIE_N)

    assert math.isnan(window.values[0])
    assert math.isnan(window.values[15])
    assert window.values[16] == pytest.approx(7.0)
    assert window.sample_bng(_TIE_E + 4.0, _TIE_N - 4.0) is None
    assert window.sample_bng(_TIE_E + 24.0, _TIE_N - 4.0) == pytest.approx(7.0)


def test_bigtiff_and_classic_read_the_same_window(tmp_path):
    values = _grid(40, 40, lambda x, y: float(y * 100 + x))
    classic = _reader(tmp_path, _make_cog([(40, 40, values)]), "classic.tif")
    big = _reader(
        tmp_path, _make_cog([(40, 40, values)], bigtiff=True), "big.tif"
    )

    corners = (_TIE_E + 10.0, _TIE_N - 20.0, _TIE_E + 15.0, _TIE_N - 15.0)
    from_classic = classic.read_window(*corners)
    from_big = big.read_window(*corners)

    # The BigTIFF fixture carries its tile offsets as LONG8, which is what
    # the real mosaics do and what a classic-only parser reads as garbage.
    assert big.levels[0].tile_offsets.kind == 16
    assert classic.levels[0].tile_offsets.kind == 4
    assert list(from_big.values) == list(from_classic.values)
    assert from_big.e_origin == pytest.approx(from_classic.e_origin)
    assert from_big.n_top == pytest.approx(from_classic.n_top)


def test_int16_samples_are_read_as_floats(tmp_path):
    values = _grid(8, 8, lambda x, y: y * 10 + x)
    built = _make_cog(
        [(8, 8, values)], sample_format=2, bits=16, nodata=-9999.0
    )
    reader = _reader(tmp_path, built)

    window = reader.read_window(_TIE_E, _TIE_N - 8.0, _TIE_E + 8.0, _TIE_N)

    assert window.values.typecode == "f"
    assert window.values[0] == pytest.approx(0.0)
    assert window.values[63] == pytest.approx(77.0)


# --------------------------------------------------------------------------
# What is actually fetched.
# --------------------------------------------------------------------------


def test_only_needed_tile_index_slices_are_fetched(counting_source):
    values = _grid(320, 320, lambda x, y: float(x) + float(y) / 2.0)
    built = _make_cog([(320, 320, values)])
    source = counting_source(built.data)
    reader = CogReader.open(source)
    level = reader.levels[0]

    # 20 by 20 tiles, so the tile index is 1,600 bytes of LONG, written
    # after the tile data and therefore well past the header prefetch:
    # if the reader read it whole, the reads below would show it.
    assert level.tile_offsets.byte_length == 400 * 4
    assert level.tile_offsets.offset > cog._HEADER_PREFETCH_BYTES

    source.reads.clear()
    window = reader.read_window(
        _TIE_E + 100.0, _TIE_N - 120.0, _TIE_E + 110.0, _TIE_N - 110.0
    )

    start = level.tile_offsets.offset
    end = start + level.tile_offsets.byte_length
    index_reads = [
        (at, length)
        for at, length in source.reads
        if at < end and at + length > start
    ]
    assert index_reads, "the reader must read the tile index at all"
    assert max(length for _at, length in index_reads) <= 64
    assert sum(length for _at, length in index_reads) < 200
    # And the slices it read were the right ones: the window's values are
    # the ramp, not some other tile's.
    assert window.values[0] == pytest.approx(100.0 + 110.0 / 2.0)
    assert window.values[9] == pytest.approx(109.0 + 110.0 / 2.0)
    assert window.values[90] == pytest.approx(100.0 + 119.0 / 2.0)


def test_adjacent_tiles_coalesce_into_one_range_request(counting_source):
    values = _grid(64, 16, lambda x, y: float(x))
    built = _make_cog([(64, 16, values)])
    offsets, counts = built.tile_offsets[0], built.tile_counts[0]
    # The builder's own promise, without which "adjacent" means nothing.
    assert offsets[1] == offsets[0] + counts[0]
    assert offsets[2] == offsets[1] + counts[1]

    source = counting_source(built.data)
    reader = CogReader.open(source)
    source.reads.clear()
    window = reader.read_window(
        _TIE_E + 10.0, _TIE_N - 16.0, _TIE_E + 40.0, _TIE_N
    )

    expected = (offsets[0], counts[0] + counts[1] + counts[2])
    payload_reads = [
        (at, length)
        for at, length in source.reads
        if offsets[0] <= at < offsets[3]
    ]
    assert payload_reads == [expected]
    assert window.values[0] == pytest.approx(10.0)
    assert window.values[29] == pytest.approx(39.0)


def test_http_byte_source_reads_through_range_requests():
    # Big enough for the claim below to mean something: a raster of 400
    # tiles, of which a 10 by 10 m window touches one.
    values = _grid(320, 320, lambda x, y: float(y * 1000 + x))
    built = _make_cog([(320, 320, values)])
    session = _RangeSession(built.data)
    source = HttpByteSource(
        "https://example.invalid/cogs/wales_dtm_32bit_cog.tif", session=session
    )

    reader = CogReader.open(source)
    window = reader.read_window(
        _TIE_E + 100.0, _TIE_N - 110.0, _TIE_E + 110.0, _TIE_N - 100.0
    )

    assert source.name == "wales_dtm_32bit_cog.tif"
    assert window.values[0] == pytest.approx(100.0 * 1000 + 100.0)
    assert all(call.startswith("bytes=") for call in session.calls)
    # Nothing near the whole file: this is the entire point of the module.
    assert source.bytes_fetched < len(built.data) // 4
    assert source.requests_made == len(session.calls)


# --------------------------------------------------------------------------
# Refusals.
# --------------------------------------------------------------------------


def test_refuses_wrong_epsg_lzw_and_predictor(tmp_path):
    values = _grid(8, 8, lambda x, y: 1.0)

    with pytest.raises(CogError, match="LZW"):
        _reader(tmp_path, _make_cog([(8, 8, values)], compression=5), "lzw.tif")

    with pytest.raises(CogError, match="predictor"):
        _reader(
            tmp_path, _make_cog([(8, 8, values)], predictor=2), "predictor.tif"
        )

    with pytest.raises(CogError, match="projected"):
        _reader(
            tmp_path, _make_cog([(8, 8, values)], model_type=2), "geographic.tif"
        )

    wrong_epsg = _reader(
        tmp_path, _make_cog([(8, 8, values)], epsg=3857), "webmercator.tif"
    )
    assert wrong_epsg.epsg == 3857
    with pytest.raises(CogError, match="27700"):
        wrong_epsg.read_window(_TIE_E, _TIE_N - 8.0, _TIE_E + 8.0, _TIE_N)


def test_range_ignoring_server_is_refused(fake_200_source):
    with pytest.raises(CogError, match="whole file") as refusal:
        fake_200_source.read(0, 16)

    # The refusal has to land before a byte of the body is touched: on the
    # real mosaic that body is 48 GB, and a reader that refuses only after
    # reading it has not refused anything.
    served = fake_200_source.session.responses[-1]
    assert served.body_read is False

    message = str(refusal.value)
    assert "wales_dtm_32bit_cog.tif" in message
    # The name of the file, never the address it came from: a URL is where
    # a key lives, and this project's failure sentences never carry one.
    assert "://" not in message
    assert "secret" not in message


def test_a_range_answered_without_content_range_or_short_is_refused():
    built = _make_cog([(8, 8, _grid(8, 8, lambda x, y: 1.0))])

    silent = HttpByteSource(
        "https://example.invalid/wales.tif",
        session=_RangeSession(built.data, mode="no_content_range"),
    )
    with pytest.raises(CogError, match="which bytes"):
        silent.read(0, 16)

    short = HttpByteSource(
        "https://example.invalid/wales.tif",
        session=_RangeSession(built.data, mode="short_body"),
    )
    with pytest.raises(CogError, match="15 bytes"):
        short.read(0, 16)


def test_a_failed_range_request_is_retried_exactly_once():
    built = _make_cog([(8, 8, _grid(8, 8, lambda x, y: 1.0))])

    flaky_session = _RangeSession(built.data, mode="flaky")
    flaky = HttpByteSource("https://example.invalid/wales.tif", session=flaky_session)
    assert len(flaky.read(0, 16)) == 16
    assert len(flaky_session.calls) == 2

    stalled_session = _RangeSession(built.data, mode="timeout")
    stalled = HttpByteSource(
        "https://example.invalid/wales.tif", session=stalled_session
    )
    with pytest.raises(CogError, match="did not answer in time"):
        stalled.read(0, 16)
    assert len(stalled_session.calls) == 2

    # A 5xx is the service saying the fault is its own, so it earns the
    # same single retry a dropped connection does.
    flaky_status_session = _RangeSession(built.data, mode="server_error")
    flaky_status = HttpByteSource(
        "https://example.invalid/wales.tif", session=flaky_status_session
    )
    assert len(flaky_status.read(0, 16)) == 16
    assert len(flaky_status_session.calls) == 2


def test_refuses_a_file_that_is_not_a_tiff(tmp_path):
    path = tmp_path / "not-a-tiff.tif"
    path.write_bytes(b"PK\x03\x04this is a zip file, not a raster at all")

    with pytest.raises(CogError, match="byte order"):
        CogReader.open(FileByteSource(path))


def test_refuses_an_upside_down_or_empty_window(tmp_path):
    reader = _reader(tmp_path, _make_cog([(8, 8, _grid(8, 8, lambda x, y: 1.0))]))

    with pytest.raises(CogError, match="empty"):
        reader.read_window(_TIE_E + 8.0, _TIE_N - 8.0, _TIE_E, _TIE_N)
    with pytest.raises(CogError, match="real numbers"):
        reader.read_window(float("nan"), _TIE_N - 8.0, _TIE_E + 8.0, _TIE_N)


# --------------------------------------------------------------------------
# The real mosaic. Excluded from the default run; invoke with pytest -m live.
# --------------------------------------------------------------------------

_WALES_DTM_URL = (
    "https://dmwproductionblob.blob.core.windows.net/cogs/lidar/"
    "wales_dtm_32bit_cog.tif"
)


@pytest.mark.live
def test_live_barry_island_window_comes_back_at_one_metre():
    source = HttpByteSource(_WALES_DTM_URL)
    reader = CogReader.open(source)

    assert reader.epsg == 27700
    assert reader.pixel_is_area is True
    assert reader.nodata == pytest.approx(-9999.0)
    assert len(reader.levels) == 7
    assert (reader.levels[0].width, reader.levels[0].height) == (191_007, 233_000)

    window = reader.read_window(311_000.0, 166_400.0, 311_500.0, 166_900.0)

    assert window.pixel_size == pytest.approx(1.0)
    assert (window.width, window.height) == (500, 500)
    heights = [
        window.sample_bng(311_000.0 + 25.0 * i, 166_400.0 + 25.0 * j)
        for i in range(1, 20)
        for j in range(1, 20)
    ]
    answered = [height for height in heights if height is not None]
    assert len(answered) > 200
    assert all(-5.0 <= height <= 150.0 for height in answered)
    # A 500 by 500 m window out of a 48 GB mosaic: what is paid for is the
    # tiles it touches, nothing else.
    assert source.bytes_fetched < 20_000_000


@pytest.mark.live
def test_live_bristol_channel_window_answers_nothing():
    source = HttpByteSource(_WALES_DTM_URL)
    reader = CogReader.open(source)

    window = reader.read_window(320_000.0, 155_000.0, 320_500.0, 155_500.0)

    samples = [
        window.sample_bng(320_000.0 + 25.0 * i, 155_000.0 + 25.0 * j)
        for i in range(1, 20)
        for j in range(1, 20)
    ]
    assert all(sample is None for sample in samples)
