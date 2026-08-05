"""LidarWalesSource's suite: fake COGs built from test_cog.py's own
`_make_cog`, and the real OSTN15 fixture slice test_bng.py and
test_contours.py already build tests/fixtures/ostn15 around. No test here
touches the network; the one that does (`test_live_fetch_and_merge_over_a_
real_barry_extent`) is marked `live` and deselected by default.

Every fetch()-shaped test sits its bbox near TP06 (Bridgend), the Welsh
station the fixture's 3x3 km block was built around (see
tests/fixtures/ostn15/make_fixture.py), so `to_bng`/`from_bng` never have to
guess outside the fixture's own coverage. The "outside Wales" test uses TP03
(Cornwall) instead: OSTN15 places it fine (the fixture covers it too), but
it is nowhere near MOSAIC_BOUNDS, which is exactly the scenario that
refusal exists for.

Sessions here are keyed by the exact URL requested (`_UrlRoutedSession`),
refusing anything else outright: a session that answered any URL, the way
test_cog.py's own `_RangeSession` does, would hide a typo in DTM_URL or
DSM_URL, since the wrong URL would still get real bytes back and every test
would still pass.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from mapgen import bng
from mapgen.bng import to_bng
from mapgen.cog import BngWindow, CogReader, FileByteSource
from mapgen.egrid import PAD_METRES
from mapgen.geo import BBox, Tile
from mapgen.geotiff_write import write_bng_geotiff
from mapgen.jobs import CancelToken, Cancelled
from mapgen.package import get_source, register_default_sources
from mapgen.sources.base import (
    FAILURE_NO_OUTPUT,
    FAILURE_RATE_LIMITED,
    FAILURE_SERVICE_ERROR,
    FAILURE_TIMEOUT,
    FAILURE_UNKNOWN,
    RETRYABLE_FAILURE_KINDS,
    NullProgress,
)
from mapgen.sources.lidar_wales import (
    DSM_WORK_NAME,
    DTM_WORK_NAME,
    EMPTY_EXTENT_MESSAGE,
    WALES_ONLY_MESSAGE,
    LidarWalesError,
    LidarWalesSource,
)
from tests.fixtures.ostn15 import make_fixture
from tests.test_cog import _FakeResponse, _grid, _make_cog

# TP06 (Bridgend, South Wales): inside MOSAIC_BOUNDS and inside the OSTN15
# fixture's own 3x3 km block.
_TP06_LAT = 51.4007822014
_TP06_LON = -3.5512834924
_TP06_E = 292184.87
_TP06_N = 168003.465

# TP03 (Cornwall): the fixture covers it too (it is one of the three chosen
# stations), but it is nowhere near the Welsh mosaic.
_TP03_LAT = 50.4388582561
_TP03_LON = -4.10864563561

NEAR_TP06_BBOX = BBox.parse(
    f"{_TP06_LON - 0.0005},{_TP06_LAT - 0.0005},{_TP06_LON + 0.0005},{_TP06_LAT + 0.0005}"
)
NEAR_TP03_BBOX = BBox.parse(
    f"{_TP03_LON - 0.0005},{_TP03_LAT - 0.0005},{_TP03_LON + 0.0005},{_TP03_LAT + 0.0005}"
)


@pytest.fixture
def ostn15_fixture_grid():
    return make_fixture.read_slice()


def _seed_cache(cache_dir: Path, grid) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    bng._write_cache(cache_dir / bng._CACHE_FILENAME, grid)


def _tiles(*tile_ids: str) -> list[Tile]:
    bbox = NEAR_TP06_BBOX
    return [Tile(tile_id=tid, row=0, col=index, core_bbox=bbox, query_bbox=bbox) for index, tid in enumerate(tile_ids)]


def _padded_extent(bbox: BBox, grid) -> tuple[float, float, float, float]:
    e1, n1 = to_bng(bbox.south, bbox.west, grid)
    e2, n2 = to_bng(bbox.north, bbox.east, grid)
    return (
        min(e1, e2) - PAD_METRES,
        min(n1, n2) - PAD_METRES,
        max(e1, e2) + PAD_METRES,
        max(n1, n2) + PAD_METRES,
    )


def _fake_mosaic_bytes(
    e_min: float, n_min: float, e_max: float, n_max: float, value_fn, *, margin: float = 50.0
) -> bytes:
    """A synthetic COG (`_make_cog`'s bytes) sized to cover [e_min, e_max] x
    [n_min, n_max] with margin, real EPSG:27700/PixelIsArea georeference,
    1 m pixels, matching the real mosaics' own convention.
    """
    origin_e = math.floor(e_min - margin)
    origin_n = math.ceil(n_max + margin)
    width = int(math.ceil(e_max + margin - origin_e))
    height = int(math.ceil(origin_n - (n_min - margin)))
    values = _grid(width, height, value_fn)
    built = _make_cog(
        [(width, height, values)], tile_size=32, tiepoint=(origin_e, origin_n), nodata=-9999.0
    )
    return built.data


class _UrlRoutedSession:
    """Serves range requests only for the exact URLs it was told about."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = dict(blobs)
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append(url)
        if url not in self.blobs:
            raise AssertionError(f"no fake mosaic registered for {url!r}")
        data = self.blobs[url]
        header = (headers or {}).get("Range", "")
        start, end = header.removeprefix("bytes=").split("-")
        first, last = int(start), int(end)
        body = data[first : last + 1]
        return _FakeResponse(
            206, {"Content-Range": f"bytes {first}-{last}/{len(data)}"}, body
        )


class _RefusesToConnect:
    def get(self, *args, **kwargs):
        raise AssertionError("no network call was expected")


class _StatusThenRoutedSession(_UrlRoutedSession):
    """Answers a fixed status for every request to `target_url`, and
    behaves like an ordinary `_UrlRoutedSession` for anything else.

    Persistent, not one-shot: `HttpByteSource.read` already retries once
    for a retryable status (429 among them, per classify_status_failure),
    so a single bad answer would be silently absorbed by that retry and
    never reach lidar_wales.py's own classification at all. Failing every
    attempt is what proves the CogError this source has to classify
    actually happens.
    """

    def __init__(self, blobs: dict[str, bytes], target_url: str, status_code: int) -> None:
        super().__init__(blobs)
        self._target_url = target_url
        self._status_code = status_code

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append(url)
        if url == self._target_url:
            return _FakeResponse(self._status_code, {}, b"")
        data = self.blobs[url]
        header = (headers or {}).get("Range", "")
        start, end = header.removeprefix("bytes=").split("-")
        first, last = int(start), int(end)
        body = data[first : last + 1]
        return _FakeResponse(
            206, {"Content-Range": f"bytes {first}-{last}/{len(data)}"}, body
        )


class _TimesOutOnUrlSession(_UrlRoutedSession):
    """Raises a timeout on every request to `target_url`, persistently
    for the same reason `_StatusThenRoutedSession` must be persistent:
    `HttpByteSource.read` retries once for a transport failure too, so a
    single timeout would be silently absorbed and never reach this
    source's own classification.
    """

    def __init__(self, blobs: dict[str, bytes], target_url: str) -> None:
        super().__init__(blobs)
        self._target_url = target_url

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append(url)
        if url == self._target_url:
            import requests

            raise requests.exceptions.Timeout("the fake server stalled")
        data = self.blobs[url]
        header = (headers or {}).get("Range", "")
        start, end = header.removeprefix("bytes=").split("-")
        first, last = int(start), int(end)
        body = data[first : last + 1]
        return _FakeResponse(
            206, {"Content-Range": f"bytes {first}-{last}/{len(data)}"}, body
        )


class _ShortBodyOnUrlSession(_UrlRoutedSession):
    """Answers every request to `target_url` one byte short of what was
    asked for, persistently: `HttpByteSource.read` has no retry for this
    failure at all (it is a content-length mismatch, not a transport or
    status failure), so a single short answer is already enough to raise,
    but persistent for the same defensive reason every other fake session
    in this file is. This is the fallthrough case `_classify_cog_error`
    has no status_code and no recognised transport phrase for: the
    CogError it produces carries `status_code is None` and a message
    shaped nothing like "did not answer in time" or "could not be
    reached", which is exactly the shape FAILURE_UNKNOWN exists for.
    """

    def __init__(self, blobs: dict[str, bytes], target_url: str) -> None:
        super().__init__(blobs)
        self._target_url = target_url

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append(url)
        data = self.blobs[url]
        header = (headers or {}).get("Range", "")
        start, end = header.removeprefix("bytes=").split("-")
        first, last = int(start), int(end)
        body = data[first : last + 1]
        if url == self._target_url:
            body = body[: max(0, len(body) - 1)]
        return _FakeResponse(
            206, {"Content-Range": f"bytes {first}-{last}/{len(data)}"}, body
        )


class _CancelAfterFirstUrlSession(_UrlRoutedSession):
    """Cancels `token` as a side effect of serving the first request to
    `trigger_url`, so a test can prove a cancel CHECKPOINT fires between
    two rasters without the second raster ever being requested.
    """

    def __init__(self, blobs: dict[str, bytes], token: CancelToken, trigger_url: str) -> None:
        super().__init__(blobs)
        self._token = token
        self._trigger_url = trigger_url

    def get(self, url, headers=None, timeout=None, stream=False):
        response = super().get(url, headers=headers, timeout=timeout, stream=stream)
        if url == self._trigger_url:
            self._token.cancel()
        return response


def _flat_mosaics(bbox: BBox, grid, dtm_value: float, dsm_value: float) -> tuple[bytes, bytes]:
    e_min, n_min, e_max, n_max = _padded_extent(bbox, grid)
    dtm = _fake_mosaic_bytes(e_min, n_min, e_max, n_max, lambda x, y: dtm_value)
    dsm = _fake_mosaic_bytes(e_min, n_min, e_max, n_max, lambda x, y: dsm_value)
    return dtm, dsm


# --------------------------------------------------------------------------
# estimate(): no network, ever.
# --------------------------------------------------------------------------


def test_estimate_touches_no_network_with_no_cache(tmp_path):
    source = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    estimate = source.estimate(NEAR_TP06_BBOX, [])
    assert estimate.bytes_estimate > 0
    assert estimate.seconds_estimate > 0


def test_estimate_touches_no_network_with_a_cached_grid(tmp_path, ostn15_fixture_grid):
    _seed_cache(tmp_path, ostn15_fixture_grid)
    source = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    estimate = source.estimate(NEAR_TP06_BBOX, [])
    assert estimate.bytes_estimate > 0
    assert estimate.seconds_estimate > 0


def test_estimate_scales_with_bbox_area(tmp_path):
    source = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    small = source.estimate(NEAR_TP06_BBOX, [])
    large_bbox = BBox.parse(
        f"{_TP06_LON - 0.05},{_TP06_LAT - 0.05},{_TP06_LON + 0.05},{_TP06_LAT + 0.05}"
    )
    large = source.estimate(large_bbox, [])
    assert large.bytes_estimate > small.bytes_estimate
    assert large.seconds_estimate >= small.seconds_estimate


def test_estimate_seconds_floor_binds_for_a_small_extent(tmp_path, ostn15_fixture_grid):
    _seed_cache(tmp_path, ostn15_fixture_grid)
    source = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    estimate = source.estimate(NEAR_TP06_BBOX, [])
    from mapgen.sources.lidar_wales import SECONDS_FLOOR

    assert estimate.seconds_estimate == pytest.approx(SECONDS_FLOOR)


def test_seconds_floor_covers_the_measured_whole_fetch_not_just_its_parts():
    # Task 9's refit. Task 6's live test measured the WHOLE fetch() (both
    # mosaics opened, both windows read, OSTN15 already cached) at 2.16 s
    # over the real 400 x 400 m Barry extent (task-6-report.md); this is
    # a real end-to-end number, not a sum of separately measured parts.
    #
    # The 1.3 s floor this replaces was never such a measurement: it was
    # Task 3's own component arithmetic (two ~0.15 s mosaic opens plus a
    # 500 x 500 m UNPADDED window's 0.50 s and 0.42 s reads), and
    # fetch() actually reads a window padded by 2 * egrid.PAD_METRES on
    # every side, which that arithmetic never accounted for. Pinned as a
    # plain numeric floor, deliberately not a re-import-and-compare of
    # SECONDS_FLOOR against itself (the existing
    # test_estimate_seconds_floor_binds_for_a_small_extent already does
    # that and would pass unchanged whatever this constant is set to):
    # this is the one test that fails against the OLD 1.3 s value and
    # only that value, which is what makes it a real RED/GREEN pin on the
    # refit rather than a tautology.
    from mapgen.sources.lidar_wales import SECONDS_FLOOR

    assert SECONDS_FLOOR >= 2.16


def test_estimate_with_and_without_a_cached_grid_agree_closely(tmp_path, ostn15_fixture_grid):
    # The BNG-projected estimate and the equirectangular-approximation
    # fallback should not disagree wildly for a small extent well clear of
    # the poles: this is the property estimate()'s own docstring claims.
    with_cache = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    no_cache_dir = tmp_path / "no-cache-here"
    without_cache = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=no_cache_dir)

    fallback = without_cache.estimate(NEAR_TP06_BBOX, [])
    _seed_cache(tmp_path, ostn15_fixture_grid)
    projected = with_cache.estimate(NEAR_TP06_BBOX, [])

    assert projected.bytes_estimate == pytest.approx(fallback.bytes_estimate, rel=0.05)


# --------------------------------------------------------------------------
# fetch(): happy path, skip-on-resume, refusals, cancel, failure kinds.
# --------------------------------------------------------------------------


def test_fetch_happy_path_writes_both_work_files(tmp_path, ostn15_fixture_grid):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_bytes, dsm_bytes = _flat_mosaics(NEAR_TP06_BBOX, ostn15_fixture_grid, 12.5, 18.5)

    source = LidarWalesSource(
        session=_UrlRoutedSession({LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes}),
        ostn15_cache_dir=cache_dir,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    progress = _ProgressLog()

    paths = source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, progress)

    assert [p.name for p in paths] == [DTM_WORK_NAME, DSM_WORK_NAME]
    for path in paths:
        assert path.exists() and path.stat().st_size > 0
    assert source.tile_failures == []
    assert ("tile_done", {"source": "lidar_wales", "tile_id": "whole-area"}) in progress.events

    dtm_reader = CogReader.open(FileByteSource(work_dir / DTM_WORK_NAME))
    assert dtm_reader.epsg == 27700
    dsm_reader = CogReader.open(FileByteSource(work_dir / DSM_WORK_NAME))
    assert dsm_reader.epsg == 27700


def test_fetch_skips_when_both_work_files_already_exist(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / DTM_WORK_NAME).write_bytes(b"already-here-dtm")
    (work_dir / DSM_WORK_NAME).write_bytes(b"already-here-dsm")

    source = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path / "cache")
    progress = _ProgressLog()

    paths = source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, progress)

    assert [p.name for p in paths] == [DTM_WORK_NAME, DSM_WORK_NAME]
    assert ("tile_skipped", {"source": "lidar_wales", "tile_id": "whole-area"}) in progress.events
    assert source.tile_failures == []


def test_fetch_does_not_skip_when_one_work_file_is_empty(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / DTM_WORK_NAME).write_bytes(b"real-bytes")
    (work_dir / DSM_WORK_NAME).write_bytes(b"")  # empty: not a valid resume

    source = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path / "cache")
    with pytest.raises(AssertionError):
        # Falls through to ensure_ostn15, which _RefusesToConnect refuses;
        # proves the skip check requires BOTH files non-empty, not just present.
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress())


def test_fetch_refuses_an_extent_wholly_outside_the_mosaic(tmp_path, ostn15_fixture_grid):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    source = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=cache_dir)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(LidarWalesError) as excinfo:
        source.fetch(NEAR_TP03_BBOX, _tiles("t1", "t2"), work_dir, NullProgress())

    assert str(excinfo.value) == WALES_ONLY_MESSAGE
    assert len(source.tile_failures) == 2
    for failure in source.tile_failures:
        assert failure.kind == FAILURE_NO_OUTPUT
        assert failure.reason == WALES_ONLY_MESSAGE
    # Refused before either mosaic was ever opened: no HTTP call at all,
    # which _RefusesToConnect enforces by raising if one is attempted.
    assert not list(work_dir.iterdir())


def test_fetch_refuses_an_all_nodata_pair(tmp_path, ostn15_fixture_grid):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    e_min, n_min, e_max, n_max = _padded_extent(NEAR_TP06_BBOX, ostn15_fixture_grid)
    dtm_bytes = _fake_mosaic_bytes(e_min, n_min, e_max, n_max, lambda x, y: -9999.0)
    dsm_bytes = _fake_mosaic_bytes(e_min, n_min, e_max, n_max, lambda x, y: -9999.0)

    source = LidarWalesSource(
        session=_UrlRoutedSession({LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes}),
        ostn15_cache_dir=cache_dir,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(LidarWalesError) as excinfo:
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert str(excinfo.value) == EMPTY_EXTENT_MESSAGE
    assert len(source.tile_failures) == 1
    assert source.tile_failures[0].kind == FAILURE_NO_OUTPUT
    assert source.tile_failures[0].reason == EMPTY_EXTENT_MESSAGE
    # Both windows really were read (unlike the outside-mosaic refusal):
    # no work file is written either way, since the refusal happens before
    # write_bng_geotiff.
    assert not list(work_dir.iterdir())


def test_fetch_proceeds_when_only_one_raster_of_the_pair_has_data(tmp_path, ostn15_fixture_grid):
    # "an all-nodata PAIR" (the brief's own wording): one real raster beside
    # an empty one must not be refused, since there is real terrain to keep.
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    e_min, n_min, e_max, n_max = _padded_extent(NEAR_TP06_BBOX, ostn15_fixture_grid)
    dtm_bytes = _fake_mosaic_bytes(e_min, n_min, e_max, n_max, lambda x, y: 7.0)
    dsm_bytes = _fake_mosaic_bytes(e_min, n_min, e_max, n_max, lambda x, y: -9999.0)

    source = LidarWalesSource(
        session=_UrlRoutedSession({LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes}),
        ostn15_cache_dir=cache_dir,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    paths = source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress())
    assert [p.name for p in paths] == [DTM_WORK_NAME, DSM_WORK_NAME]
    assert source.tile_failures == []


def test_fetch_cancel_between_rasters_leaves_no_failure_records(tmp_path, ostn15_fixture_grid):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_bytes, dsm_bytes = _flat_mosaics(NEAR_TP06_BBOX, ostn15_fixture_grid, 5.0, 9.0)

    token = CancelToken()
    session = _CancelAfterFirstUrlSession(
        {LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes},
        token,
        LidarWalesSource.DTM_URL,
    )
    source = LidarWalesSource(session=session, ostn15_cache_dir=cache_dir)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Cancelled):
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress(), cancel=token)

    assert source.tile_failures == []
    assert LidarWalesSource.DSM_URL not in session.calls
    assert not list(work_dir.iterdir())


def test_fetch_stops_before_any_request_when_already_cancelled(tmp_path):
    token = CancelToken()
    token.cancel()
    source = LidarWalesSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path / "cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Cancelled):
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress(), cancel=token)


def test_fetch_classifies_a_rate_limited_dtm_response_and_never_leaks_a_url(tmp_path, ostn15_fixture_grid):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_bytes, dsm_bytes = _flat_mosaics(NEAR_TP06_BBOX, ostn15_fixture_grid, 1.0, 2.0)

    session = _StatusThenRoutedSession(
        {LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes},
        LidarWalesSource.DTM_URL,
        429,
    )
    source = LidarWalesSource(session=session, ostn15_cache_dir=cache_dir)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Exception) as excinfo:
        source.fetch(NEAR_TP06_BBOX, _tiles("t1", "t2"), work_dir, NullProgress())

    assert len(source.tile_failures) == 2
    for failure in source.tile_failures:
        assert failure.kind == FAILURE_RATE_LIMITED
        assert "429" in failure.reason
        assert "http" not in failure.reason.lower().replace("(http 429)", "")
        assert LidarWalesSource.DTM_URL not in failure.reason
    assert LidarWalesSource.DTM_URL not in str(excinfo.value)


def test_fetch_classifies_a_500_as_service_error_and_it_is_retryable(tmp_path, ostn15_fixture_grid):
    # classify_status_failure's own >=500 branch returns "answered HTTP
    # {code}", with no parentheses around the number, unlike its
    # 429/401/403/else siblings. A classifier that tried to recover the
    # status by pattern-matching that rendered sentence would miss this
    # one specifically, report FAILURE_UNKNOWN, and defeat the retry
    # policy: FAILURE_UNKNOWN is not in RETRYABLE_FAILURE_KINDS, so
    # package.py would never retry a transient 500 the shared vocabulary
    # explicitly designed as retryable.
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_bytes, dsm_bytes = _flat_mosaics(NEAR_TP06_BBOX, ostn15_fixture_grid, 1.0, 2.0)

    session = _StatusThenRoutedSession(
        {LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes},
        LidarWalesSource.DTM_URL,
        500,
    )
    source = LidarWalesSource(session=session, ostn15_cache_dir=cache_dir)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Exception) as excinfo:
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    failure = source.tile_failures[0]
    assert failure.kind == FAILURE_SERVICE_ERROR
    assert failure.kind in RETRYABLE_FAILURE_KINDS
    assert "500" in failure.reason
    assert LidarWalesSource.DTM_URL not in failure.reason
    assert LidarWalesSource.DTM_URL not in str(excinfo.value)


def test_fetch_classifies_a_503_as_service_error(tmp_path, ostn15_fixture_grid):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_bytes, dsm_bytes = _flat_mosaics(NEAR_TP06_BBOX, ostn15_fixture_grid, 1.0, 2.0)

    session = _StatusThenRoutedSession(
        {LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes},
        LidarWalesSource.DSM_URL,
        503,
    )
    source = LidarWalesSource(session=session, ostn15_cache_dir=cache_dir)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Exception) as excinfo:
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    failure = source.tile_failures[0]
    assert failure.kind == FAILURE_SERVICE_ERROR
    assert failure.kind in RETRYABLE_FAILURE_KINDS
    assert "503" in failure.reason
    assert LidarWalesSource.DSM_URL not in failure.reason
    assert LidarWalesSource.DSM_URL not in str(excinfo.value)


def test_fetch_classifies_an_unrecognised_cogerror_with_a_fixed_sentence(tmp_path, ostn15_fixture_grid):
    # The fallthrough: a CogError with no status_code and no recognised
    # transport phrase (here, a short-body content-length mismatch, which
    # HttpByteSource never retries at all). The reason must be the FIXED
    # phrase, never the raw CogError text: the raw message names byte
    # counts and could, in a differently shaped failure, name more than
    # that, and survey.json/the tile_failed event are files and streams
    # that outlive the run.
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_bytes, dsm_bytes = _flat_mosaics(NEAR_TP06_BBOX, ostn15_fixture_grid, 1.0, 2.0)

    session = _ShortBodyOnUrlSession(
        {LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes},
        LidarWalesSource.DTM_URL,
    )
    source = LidarWalesSource(session=session, ostn15_cache_dir=cache_dir)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Exception) as excinfo:
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    failure = source.tile_failures[0]
    assert failure.kind == FAILURE_UNKNOWN
    assert failure.reason == "Failed to download Welsh LiDAR: could not be read."
    # The raw CogError's own vocabulary ("bytes", "asked for", "short")
    # must not have leaked into the fixed reason, even though it is fine
    # in the exception itself.
    assert "bytes" not in failure.reason
    assert "sent" not in failure.reason
    assert LidarWalesSource.DTM_URL not in failure.reason


def test_fetch_classifies_a_dsm_timeout_and_never_leaks_a_url(tmp_path, ostn15_fixture_grid):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_bytes, dsm_bytes = _flat_mosaics(NEAR_TP06_BBOX, ostn15_fixture_grid, 3.0, 4.0)

    session = _TimesOutOnUrlSession(
        {LidarWalesSource.DTM_URL: dtm_bytes, LidarWalesSource.DSM_URL: dsm_bytes},
        LidarWalesSource.DSM_URL,
    )
    source = LidarWalesSource(session=session, ostn15_cache_dir=cache_dir)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Exception) as excinfo:
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    assert source.tile_failures[0].kind == FAILURE_TIMEOUT
    assert "did not answer in time" in source.tile_failures[0].reason
    assert LidarWalesSource.DSM_URL not in source.tile_failures[0].reason
    assert LidarWalesSource.DSM_URL not in str(excinfo.value)
    # The DTM window really was read first, and no work file survives a
    # failed pair: fetch() only writes once both windows are in hand.
    assert not (work_dir / DTM_WORK_NAME).exists()


def test_fetch_classifies_an_ostn15_download_failure_and_reraises(tmp_path):
    class RaisesConnectionError:
        def get(self, *args, **kwargs):
            raise __import__("requests").exceptions.ConnectionError(
                "connection refused talking to ordnancesurvey.co.uk"
            )

    source = LidarWalesSource(session=RaisesConnectionError(), ostn15_cache_dir=tmp_path / "cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Exception):
        source.fetch(NEAR_TP06_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    assert source.tile_failures[0].reason  # a plain sentence, not empty
    assert "ordnancesurvey" not in source.tile_failures[0].reason


# --------------------------------------------------------------------------
# merge(): naming, contours, stale-copy unlink.
# --------------------------------------------------------------------------


def _sloped_window(width: int = 30, height: int = 30) -> BngWindow:
    values = _grid_array(
        width, height, lambda x, y: float(x + y) * 0.4
    )
    return BngWindow(
        e_origin=_TP06_E - width / 2.0,
        n_top=_TP06_N + height / 2.0,
        pixel_size=1.0,
        width=width,
        height=height,
        values=values,
    )


def _grid_array(width, height, formula):
    from array import array

    return array("f", [formula(x, y) for y in range(height) for x in range(width)])


def _write_work_pair(work_dir: Path, dtm_window: BngWindow, dsm_window: BngWindow) -> list[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    dtm_path = work_dir / DTM_WORK_NAME
    dsm_path = work_dir / DSM_WORK_NAME
    write_bng_geotiff(dtm_path, dtm_window)
    write_bng_geotiff(dsm_path, dsm_window)
    return [dtm_path, dsm_path]


def test_merge_copies_both_rasters_under_the_stem_and_writes_all_four_contours(
    tmp_path, ostn15_fixture_grid
):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_window = _sloped_window()
    dsm_window = _sloped_window()
    parts = _write_work_pair(tmp_path / "work", dtm_window, dsm_window)

    source = LidarWalesSource(ostn15_cache_dir=cache_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-05"

    written = source.merge(parts, out_dir, stem)

    names = sorted(p.name for p in written)
    assert names == sorted(source.possible_outputs(stem))
    for path in written:
        assert path.parent == out_dir
        assert path.exists() and path.stat().st_size > 0

    import json

    contour_path = out_dir / f"{stem}_contours_1m.geojson"
    payload = json.loads(contour_path.read_text(encoding="utf-8"))
    assert payload["type"] == "FeatureCollection"
    for feature in payload["features"]:
        elevation = feature["properties"]["elevation"]
        assert elevation == elevation and math.isfinite(elevation)  # not NaN, not inf
        for lon, lat in feature["geometry"]["coordinates"]:
            assert -180.0 <= lon <= 180.0
            assert -90.0 <= lat <= 90.0


def test_merge_missing_dtm_returns_only_the_dsm_and_unlinks_stale_copies(tmp_path, ostn15_fixture_grid):
    cache_dir = tmp_path / "cache"
    _seed_cache(cache_dir, ostn15_fixture_grid)
    dtm_window = _sloped_window()
    dsm_window = _sloped_window()
    parts = _write_work_pair(tmp_path / "work", dtm_window, dsm_window)
    dsm_only = [part for part in parts if part.name == DSM_WORK_NAME]

    source = LidarWalesSource(ostn15_cache_dir=cache_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-05"

    # Stale leftovers from an earlier, complete run at this stem.
    stale_names = [
        f"{stem}_lidar_dtm.tif",
        f"{stem}_contours_5m.geojson",
        f"{stem}_contours_1m.geojson",
        f"{stem}_contours_0.5m.geojson",
        f"{stem}_contours_0.25m.geojson",
    ]
    for name in stale_names:
        (out_dir / name).write_bytes(b"stale")

    written = source.merge(dsm_only, out_dir, stem)

    assert [p.name for p in written] == [f"{stem}_lidar_dsm.tif"]
    for name in stale_names:
        assert not (out_dir / name).exists(), f"{name} should have been unlinked as stale"
    assert (out_dir / f"{stem}_lidar_dsm.tif").exists()


def test_merge_missing_both_work_files_returns_nothing(tmp_path):
    source = LidarWalesSource(ostn15_cache_dir=tmp_path / "cache")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    assert source.merge([], out_dir, "Barry-Waterfront_2026-08-05") == []


def test_merge_with_no_cached_grid_skips_contours_but_keeps_the_rasters(tmp_path):
    # No cache seeded at all: load_ostn15 returns None.
    dtm_window = _sloped_window()
    dsm_window = _sloped_window()
    parts = _write_work_pair(tmp_path / "work", dtm_window, dsm_window)

    source = LidarWalesSource(ostn15_cache_dir=tmp_path / "no-such-cache")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-05"

    # Stale contour files from an earlier run must still be unlinked, even
    # though this run cannot regenerate them: a package without contours
    # beats a crash, and it must not also be a package with WRONG contours
    # left over from a previous extent.
    stale_contour = out_dir / f"{stem}_contours_5m.geojson"
    stale_contour.write_bytes(b"stale")

    written = source.merge(parts, out_dir, stem)

    names = sorted(p.name for p in written)
    assert names == sorted([f"{stem}_lidar_dtm.tif", f"{stem}_lidar_dsm.tif"])
    assert not stale_contour.exists()


def test_possible_outputs_is_the_closed_list_of_six_names():
    source = LidarWalesSource()
    stem = "Barry-Waterfront_2026-08-05"
    assert sorted(source.possible_outputs(stem)) == sorted(
        [
            f"{stem}_lidar_dtm.tif",
            f"{stem}_lidar_dsm.tif",
            f"{stem}_contours_5m.geojson",
            f"{stem}_contours_1m.geojson",
            f"{stem}_contours_0.5m.geojson",
            f"{stem}_contours_0.25m.geojson",
        ]
    )


# --------------------------------------------------------------------------
# Class attributes, and registration.
# --------------------------------------------------------------------------


def test_declares_no_api_key_and_the_briefs_exact_strings():
    source = LidarWalesSource()
    assert source.id == "lidar_wales"
    assert source.display_name == "LiDAR terrain (Wales, 1 m)"
    assert source.licence == "Open Government Licence v3.0"
    assert source.attribution == (
        "Contains Welsh Government and Natural Resources Wales information "
        "licensed under the Open Government Licence v3.0"
    )
    assert source.requires_api_key is False
    assert not hasattr(source, "readiness_problem")


def test_register_default_sources_registers_lidar_wales_once_and_honours_the_type_check():
    register_default_sources()
    first = get_source("lidar_wales")
    assert isinstance(first, LidarWalesSource)

    register_default_sources()
    assert get_source("lidar_wales") is first


class _ProgressLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


# --------------------------------------------------------------------------
# The one live test: a real 400 x 400 m Barry extent, full fetch + merge.
# --------------------------------------------------------------------------


@pytest.mark.live
def test_live_fetch_and_merge_over_a_real_barry_extent(tmp_path):
    bbox = BBox.parse("-3.272,51.393,-3.268,51.397")  # roughly 400 x 400 m
    source = LidarWalesSource()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    tiles = [Tile(tile_id="whole-area", row=0, col=0, core_bbox=bbox, query_bbox=bbox)]

    paths = source.fetch(bbox, tiles, work_dir, NullProgress())
    assert len(paths) == 2
    assert source.tile_failures == []

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry_live-test"
    written = source.merge(paths, out_dir, stem)

    names = sorted(p.name for p in written)
    assert names == sorted(source.possible_outputs(stem))

    import json

    for suffix in ("5m", "1m", "0.5m", "0.25m"):
        payload = json.loads((out_dir / f"{stem}_contours_{suffix}.geojson").read_text(encoding="utf-8"))
        for feature in payload["features"]:
            elevation = feature["properties"]["elevation"]
            assert math.isfinite(elevation)
