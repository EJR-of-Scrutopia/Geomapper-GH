import io
import math
import zipfile

import pytest
import requests

import mapgen.bng as bng
from mapgen.bng import (
    OSTN15_URL,
    BngError,
    OutsideOstn15Error,
    ensure_ostn15,
    from_bng,
    load_ostn15,
    tm_forward,
    tm_inverse,
    to_bng,
)
from tests.fixtures.ostn15 import make_fixture


def test_tm_forward_true_origin_lands_on_false_origin_scaled():
    # The true origin projects to the false origin exactly, by construction.
    easting, northing = tm_forward(49.0, -2.0)
    assert easting == pytest.approx(400_000.0, abs=1e-6)
    assert northing == pytest.approx(-100_000.0, abs=1e-6)


def test_tm_roundtrip_over_wales():
    # Forward then inverse must return to the input to well under a
    # millimetre in degrees (1e-9 deg is about 0.1 mm on the ground).
    for lat, lon in [(51.40, -3.27), (51.48, -3.18), (53.32, -4.63), (52.42, -4.08)]:
        easting, northing = tm_forward(lat, lon)
        back_lat, back_lon = tm_inverse(easting, northing)
        assert back_lat == pytest.approx(lat, abs=1e-9)
        assert back_lon == pytest.approx(lon, abs=1e-9)


def test_tm_forward_monotonic_in_the_right_directions():
    # North increases northing, east increases easting: the cheapest way to
    # catch a swapped sign or a swapped argument order.
    e1, n1 = tm_forward(51.40, -3.27)
    e2, n2 = tm_forward(51.41, -3.27)
    e3, n3 = tm_forward(51.40, -3.26)
    assert n2 > n1 and abs(e2 - e1) < 200.0
    assert e3 > e1 and abs(n3 - n1) < 200.0


# --------------------------------------------------------------------------
# Refusals. Never a NaN, never an infinity, never a silent wrong answer.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_tm_forward_refuses_a_non_finite_input(bad):
    with pytest.raises(BngError, match="real numbers"):
        tm_forward(bad, -3.0)
    with pytest.raises(BngError, match="real numbers"):
        tm_forward(51.5, bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_tm_inverse_refuses_a_non_finite_input(bad):
    with pytest.raises(BngError, match="real numbers"):
        tm_inverse(bad, 100_000.0)
    with pytest.raises(BngError, match="real numbers"):
        tm_inverse(400_000.0, bad)


# --------------------------------------------------------------------------
# OSTN15: the shift grid, the cache, and the public to_bng/from_bng pair.
#
# The station vectors are OS's own (see tests/fixtures/ostn15/make_fixture.py
# for how three of them were picked and reduced to a fixture a few kilobytes
# in size), not values invented for this test file.
# --------------------------------------------------------------------------


@pytest.fixture
def ostn15_fixture_grid():
    return make_fixture.read_slice()


def load_fixture_stations():
    return make_fixture.read_stations()


def test_station_vectors_match_os_expected_output(ostn15_fixture_grid):
    # The three OS Net stations from OS's own TestInput/TestOutput pair,
    # run through tm_forward plus the real shift values around them.
    for station in load_fixture_stations():
        easting, northing = to_bng(
            station.etrs_lat, station.etrs_lon, ostn15_fixture_grid
        )
        # OS publishes expected E/N to 3 decimal places.
        assert easting == pytest.approx(station.expected_e, abs=0.002)
        assert northing == pytest.approx(station.expected_n, abs=0.002)


def test_from_bng_inverts_to_bng(ostn15_fixture_grid):
    for station in load_fixture_stations():
        easting, northing = to_bng(
            station.etrs_lat, station.etrs_lon, ostn15_fixture_grid
        )
        lat, lon = from_bng(easting, northing, ostn15_fixture_grid)
        assert lat == pytest.approx(station.etrs_lat, abs=2e-9)
        assert lon == pytest.approx(station.etrs_lon, abs=2e-9)


def test_outside_grid_refuses(ostn15_fixture_grid):
    with pytest.raises(OutsideOstn15Error):
        ostn15_fixture_grid.shift_at(-50_000.0, -50_000.0)


# A tiny, hand-written excerpt in the data file's own confirmed column
# order: two nodes on the ground (0,0) and (1000,0), read with a flag that
# is not 16, plus two more closing the (0,1000)-(1000,1000) corner so
# shift_at has all four corners it needs for the unit square between them.
_TINY_EXCERPT = (
    "Point_ID,ETRS89_Easting,ETRS89_Northing,ETRS89_OSGB36_EShift,"
    "ETRS89_OSGB36_NShift,ETRS89_ODN_HeightShift,Height_Datum_Flag\n"
    "1,0,0,90.750,-82.020,55.127,15\n"
    "2,1000,0,90.764,-82.015,55.108,15\n"
    "3,0,1000,90.700,-82.100,55.000,15\n"
    "4,1000,1000,90.800,-82.200,55.200,15\n"
)


def test_cache_roundtrip(tmp_path):
    # Parse a tiny synthetic data-file excerpt, write the cache, reload it,
    # and get identical shifts back.
    grid = bng._parse_data_file(_TINY_EXCERPT.splitlines(keepends=True))
    cache_path = tmp_path / "ostn15_shifts.bin"
    bng._write_cache(cache_path, grid)
    reloaded = bng._read_cache(cache_path)

    for easting, northing in [(0.0, 0.0), (500.0, 500.0), (999.0, 1.0)]:
        assert reloaded.shift_at(easting, northing) == pytest.approx(
            grid.shift_at(easting, northing)
        )


def test_ensure_ostn15_never_downloads_when_cache_present(tmp_path):
    # A session whose get() raises AssertionError proves no network call.
    grid = bng._parse_data_file(_TINY_EXCERPT.splitlines(keepends=True))
    bng._write_cache(tmp_path / bng._CACHE_FILENAME, grid)

    class RefusesToConnect:
        def get(self, *args, **kwargs):
            raise AssertionError(
                "ensure_ostn15 must not touch the network when the cache "
                "is already present"
            )

    loaded = ensure_ostn15(cache_dir=tmp_path, session=RefusesToConnect())
    assert loaded.shift_at(500.0, 500.0) == pytest.approx(grid.shift_at(500.0, 500.0))


class _FakeDownloadResponse:
    """Just enough of a `requests.Response` for _download_and_parse: a
    context manager, a no-op raise_for_status, and iter_content over
    whatever bytes the test wants served.
    """

    def __init__(self, content: bytes):
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self._content


class _FakeDownloadSession:
    def __init__(self, content: bytes):
        self._content = content

    def get(self, *args, **kwargs):
        return _FakeDownloadResponse(self._content)


def test_ensure_ostn15_wraps_a_corrupt_zip_as_bngerror(tmp_path):
    # zipfile.BadZipFile must not escape _download_and_parse as itself:
    # every other failure path in this module raises BngError, and a
    # caller catching BngError to report one clean fetch failure should
    # not also have to know about zipfile's own exception type.
    session = _FakeDownloadSession(b"this is not a zip file at all")
    with pytest.raises(BngError):
        ensure_ostn15(cache_dir=tmp_path, session=session)
    # No half-written cache from a download that never finished parsing.
    assert not (tmp_path / bng._CACHE_FILENAME).exists()


def test_ensure_ostn15_wraps_a_missing_data_file_as_bngerror(tmp_path):
    # A real zip, but not carrying OSTN15_OSGM15_DataFile.txt: archive.open
    # raises KeyError, which must not escape as itself either.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("WRONG_NAME.txt", "not the data file")
    session = _FakeDownloadSession(buffer.getvalue())
    with pytest.raises(BngError):
        ensure_ostn15(cache_dir=tmp_path, session=session)
    assert not (tmp_path / bng._CACHE_FILENAME).exists()


def test_load_ostn15_returns_none_without_touching_the_network(tmp_path):
    assert load_ostn15(cache_dir=tmp_path) is None


# --------------------------------------------------------------------------
# _read_cache's own guards, exercised negatively (a deferred gap: until now
# nothing ever wrote a cache file that fails them on purpose). load_ostn15
# is the cache-only entry point, so these prove its contract for a corrupt
# cache directly: it raises BngError with the specific guard sentence,
# unchanged by ensure_ostn15's self-healing below, which catches this same
# exception one layer up rather than altering what raises it here.
# --------------------------------------------------------------------------


def test_load_ostn15_raises_the_too_short_sentence_for_a_truncated_header(tmp_path):
    cache_path = tmp_path / bng._CACHE_FILENAME
    cache_path.write_bytes(b"short")
    with pytest.raises(BngError, match="too short to be valid"):
        load_ostn15(cache_dir=tmp_path)


def test_load_ostn15_raises_the_wrong_format_sentence_for_a_bad_magic(tmp_path):
    grid = bng._parse_data_file(_TINY_EXCERPT.splitlines(keepends=True))
    cache_path = tmp_path / bng._CACHE_FILENAME
    bng._write_cache(cache_path, grid)
    data = bytearray(cache_path.read_bytes())
    data[0:8] = b"XXXXXXXX"  # not _CACHE_MAGIC
    cache_path.write_bytes(bytes(data))
    with pytest.raises(BngError, match="not in the expected format"):
        load_ostn15(cache_dir=tmp_path)


def test_load_ostn15_raises_the_wrong_format_sentence_for_a_truncated_body(tmp_path):
    grid = bng._parse_data_file(_TINY_EXCERPT.splitlines(keepends=True))
    cache_path = tmp_path / bng._CACHE_FILENAME
    bng._write_cache(cache_path, grid)
    data = cache_path.read_bytes()
    cache_path.write_bytes(data[: len(data) // 2])  # magic intact, body cut short
    with pytest.raises(BngError, match="not in the expected format"):
        load_ostn15(cache_dir=tmp_path)


# --------------------------------------------------------------------------
# ensure_ostn15 self-heals a corrupt cache rather than reporting a
# download problem that was never the actual failure. A fake session
# serving a valid pack (the same tiny excerpt test_cache_roundtrip uses,
# zipped under the real data file name) proves the refetch actually
# happens, and that the cache it rewrites is not itself corrupt.
# --------------------------------------------------------------------------


def _zip_with_tiny_excerpt() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(bng._DATA_FILE_NAME, _TINY_EXCERPT)
    return buffer.getvalue()


def test_ensure_ostn15_refetches_when_the_cache_has_the_wrong_magic(tmp_path):
    grid = bng._parse_data_file(_TINY_EXCERPT.splitlines(keepends=True))
    cache_path = tmp_path / bng._CACHE_FILENAME
    bng._write_cache(cache_path, grid)
    data = bytearray(cache_path.read_bytes())
    data[0:8] = b"XXXXXXXX"
    cache_path.write_bytes(bytes(data))

    session = _FakeDownloadSession(_zip_with_tiny_excerpt())
    healed = ensure_ostn15(cache_dir=tmp_path, session=session)
    assert healed.shift_at(500.0, 500.0) == pytest.approx(grid.shift_at(500.0, 500.0))

    # The rewritten cache is not itself corrupt: a later load succeeds and
    # agrees, so the heal is not a one-time in-memory patch over a cache
    # file that would fail the very next call.
    reloaded = load_ostn15(cache_dir=tmp_path)
    assert reloaded.shift_at(500.0, 500.0) == pytest.approx(grid.shift_at(500.0, 500.0))


def test_ensure_ostn15_refetches_when_the_cache_is_truncated(tmp_path):
    grid = bng._parse_data_file(_TINY_EXCERPT.splitlines(keepends=True))
    cache_path = tmp_path / bng._CACHE_FILENAME
    bng._write_cache(cache_path, grid)
    data = cache_path.read_bytes()
    cache_path.write_bytes(data[: len(data) // 2])

    session = _FakeDownloadSession(_zip_with_tiny_excerpt())
    healed = ensure_ostn15(cache_dir=tmp_path, session=session)
    assert healed.shift_at(500.0, 500.0) == pytest.approx(grid.shift_at(500.0, 500.0))

    reloaded = load_ostn15(cache_dir=tmp_path)
    assert reloaded.shift_at(500.0, 500.0) == pytest.approx(grid.shift_at(500.0, 500.0))


def test_flag_16_is_treated_as_outside_even_though_it_carries_a_number(tmp_path):
    # OS's own "outside transformation area" flag: the row still carries a
    # real east/north shift (see the module docstring), and this parser
    # must drop it exactly as if the node had never been mentioned, so
    # OutsideOstn15Error covers both the same way.
    excerpt = (
        "Point_ID,ETRS89_Easting,ETRS89_Northing,ETRS89_OSGB36_EShift,"
        "ETRS89_OSGB36_NShift,ETRS89_ODN_HeightShift,Height_Datum_Flag\n"
        "1,0,0,90.750,-82.020,55.127,16\n"
        "2,1000,0,90.764,-82.015,55.108,1\n"
        "3,0,1000,90.700,-82.100,55.000,1\n"
        "4,1000,1000,90.800,-82.200,55.200,1\n"
    )
    grid = bng._parse_data_file(excerpt.splitlines(keepends=True))
    with pytest.raises(OutsideOstn15Error):
        grid.shift_at(500.0, 500.0)


def test_parse_data_file_refuses_an_unexpected_column_order():
    # The parser's own docstring claims this fails loudly rather than
    # silently swapping east and north; this is what pins that claim real.
    bad_header = (
        "Point_ID,ETRS89_Northing,ETRS89_Easting,ETRS89_OSGB36_EShift,"
        "ETRS89_OSGB36_NShift,ETRS89_ODN_HeightShift,Height_Datum_Flag\n"
    )
    with pytest.raises(BngError):
        bng._parse_data_file([bad_header])


@pytest.mark.live
def test_to_bng_matches_every_os_published_station(tmp_path):
    # ensure_ostn15 against the real URL, into a temp dir, then every
    # station OS itself publishes an answer for (not just the three
    # committed in stations.txt), checked to OS's own 2 mm precision.
    grid = ensure_ostn15(cache_dir=tmp_path)

    session = requests.Session()
    response = session.get(OSTN15_URL, timeout=120.0)
    response.raise_for_status()
    zip_path = tmp_path / "pack_for_test_vectors.zip"
    zip_path.write_bytes(response.content)

    with zipfile.ZipFile(zip_path) as archive:
        input_text = archive.read(
            "OSTN15_OSGM15_TestInput_ETRStoOSGB.txt"
        ).decode("utf-8")
        output_text = archive.read(
            "OSTN15_OSGM15_TestOutput_ETRStoOSGB.txt"
        ).decode("utf-8")

    inputs = {}
    for line in input_text.splitlines()[1:]:
        point_id, lat, lon, _height = line.split(",")
        inputs[point_id] = (float(lat), float(lon))

    outputs = {}
    for line in output_text.splitlines()[1:]:
        fields = line.split(",")
        outputs[fields[0]] = (float(fields[1]), float(fields[2]))

    assert len(inputs) == 40
    for point_id, (lat, lon) in inputs.items():
        easting, northing = to_bng(lat, lon, grid)
        expected_e, expected_n = outputs[point_id]
        assert easting == pytest.approx(expected_e, abs=0.002), point_id
        assert northing == pytest.approx(expected_n, abs=0.002), point_id
