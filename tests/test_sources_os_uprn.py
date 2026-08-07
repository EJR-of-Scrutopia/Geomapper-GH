"""OsUprnSource's suite: OS Open UPRN addresses (GB) as a LayerSource.

Ties os_downloads.py (Task 1) and os_shards.py's own write_uprn_shards/
uprn_in pair (Task 3) into estimate()/fetch()/merge(), the same
three-part shape os_open.py already established for this plan; read that
module and its own test file end to end first (this file reuses several
of its fixtures and helpers directly, imported rather than duplicated:
NEAR_TP06_BBOX and the TP06 station constants, _tiles, _seed_ostn15_cache,
_padded_extent, and _is_live_marked, all from tests/test_sources_os_open.py).

No test here (other than the one `live`-marked test) ever touches the real
network. Two seams are patched exactly as test_sources_os_open.py's own
suite patches them: `mapgen.os_downloads._build_opener` (the urllib seam
`product_downloads`, `product_version` and `download_entry` all go
through) and `self.session`, a `requests.Session`-shaped fake this source
hands to `ensure_ostn15` (bng.py). The OSTN15 grid itself is never
downloaded: every fetch()-shaped test pre-seeds the cache with
tests/fixtures/ostn15's real committed slice around TP06, the same
convention test_sources_os_open.py already establishes.

The one test that does touch the real network
(`test_live_fetch_over_cowbridge_yields_over_1000_uprn_rows`) is marked
`live` and deselected by default, and skips itself outright unless a
complete national UPRN cache already exists on the machine running it:
this source's own one-time download is 619 MB, and the test suite must
never be what triggers it.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
import requests

from mapgen import os_downloads
from mapgen.bng import from_bng
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken, Cancelled
from mapgen.os_downloads import OsOpenError
from mapgen.os_shards import shards_complete, write_uprn_shards
from mapgen.sources.base import NullProgress
from mapgen.sources.os_uprn import (
    BYTES_PER_SECOND_ESTIMATE,
    PRODUCT,
    SECONDS_FLOOR,
    UPRN_BYTES_ONE_TIME,
    OsUprnSource,
)
from tests.fixtures.ostn15 import make_fixture
from tests.test_os_downloads import _FakeHTTPResponse
from tests.test_sources_os_open import (
    NEAR_TP06_BBOX,
    _is_live_marked,
    _padded_extent,
    _seed_ostn15_cache,
    _tiles,
)


class _PoisonedSession:
    """Raises on any use. Proves estimate()/a warm fetch() never touch
    the network through `self.session`."""

    def get(self, *args, **kwargs):
        raise AssertionError("must never touch the network")


class _PoisonedOpener:
    def open(self, *args, **kwargs):
        raise AssertionError("must never touch the network")


@pytest.fixture
def ostn15_fixture_grid():
    return make_fixture.read_slice()


@pytest.fixture(autouse=True)
def _isolate_osuprn_cache(request, tmp_path, monkeypatch):
    """Same isolation mechanism, and the same live exemption, as
    test_sources_os_open.py's own `_isolate_osopen_cache`: patching
    `os_downloads.CONFIG_PATH` is what makes `cache_root()` resolve under
    a throwaway `tmp_path` rather than the real `~/.mapgen`, and a
    `live`-marked test must be exempt from that patch (see that fixture's
    own docstring for the review finding this guards against), so that
    the one live test in this module genuinely reads and writes the real
    default `cache_root()`.
    """
    if _is_live_marked(request):
        return
    fake_home = tmp_path / "mapgen_home"
    fake_home.mkdir()
    monkeypatch.setattr(os_downloads, "CONFIG_PATH", fake_home / "config.json")


_UPRN_VERSION_URL = "https://api.os.uk/downloads/v1/products/OpenUPRN"
_UPRN_LISTING_URL = "https://api.os.uk/downloads/v1/products/OpenUPRN/downloads"
_UPRN_ZIP_URL = "https://blob.example.test/osopenuprn_202608_csv.zip"

_EMPTY_UPRN_CSV = "UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE\n"


def _seed_complete_uprn_cache(version: str = "2026-08") -> Path:
    """A national shard directory that `shards_complete` already
    reports True for, with zero rows: the same trick
    test_sources_os_open.py's own tests use (`write_shards(iter(()), ...)`),
    at this module's own header-only-CSV grain.
    """
    shard_dir = os_downloads.product_cache_dir(PRODUCT, version) / "shards"
    write_uprn_shards(io.StringIO(_EMPTY_UPRN_CSV), shard_dir)
    assert shards_complete(shard_dir)
    return shard_dir


def _version_response(version: str) -> callable:
    body = json.dumps({"id": "x", "version": version}).encode("utf-8")
    return lambda: _FakeHTTPResponse(200, body)


def _listing_response(entries: list[dict]) -> callable:
    body = json.dumps(entries).encode("utf-8")
    return lambda: _FakeHTTPResponse(200, body)


def _zip_response(data: bytes) -> callable:
    return lambda: _FakeHTTPResponse(200, data)


def _uprn_entry(url: str = _UPRN_ZIP_URL, size: int = 0) -> dict:
    return {
        "area": "GB", "format": "CSV", "url": url, "size": size,
        "fileName": "osopenuprn_202608_csv.zip", "md5": "0" * 32,
    }


def _uprn_csv_bytes(rows: list[tuple[int, float, float, float, float]]) -> bytes:
    lines = ["UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE"]
    for uprn, easting, northing, lat, lon in rows:
        lines.append(f"{uprn},{easting},{northing},{lat},{lon}")
    text = "\n".join(lines) + "\n"
    # A real leading UTF-8 BOM (EF BB BF), matching the real OpenUPRN
    # file (os_shards.py's own module docstring, "write_uprn_shards").
    return ("﻿" + text).encode("utf-8")


def _zip_bytes(member_name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, content)
    return buffer.getvalue()


class _RoutedOpener:
    """Answers os_downloads.py's urllib calls by exact URL match; see
    test_sources_os_open.py's own `_RoutedOpener` for the identical
    shape and the same reasoning (call order is fetch()'s own
    implementation detail, not something a test should predict)."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"no route registered for {url!r}")
        item = self.routes[url]
        if isinstance(item, BaseException):
            raise item
        return item()


# --------------------------------------------------------------------------
# estimate(): never touches the network; prices the one-time national
# download when uncached, SECONDS_FLOOR alone when a complete cache
# already exists on disk.
# --------------------------------------------------------------------------


def test_estimate_never_touches_the_network(tmp_path, monkeypatch):
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: _PoisonedOpener())
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))  # must not raise


def test_estimate_prices_the_one_time_download_when_uncached(tmp_path):
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    result = source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))

    assert result.bytes_estimate == UPRN_BYTES_ONE_TIME
    assert result.seconds_estimate == pytest.approx(
        max(UPRN_BYTES_ONE_TIME / BYTES_PER_SECOND_ESTIMATE, SECONDS_FLOOR)
    )


def test_estimate_is_seconds_floor_only_once_a_complete_cache_exists(tmp_path):
    _seed_complete_uprn_cache()
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    result = source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))

    assert result.bytes_estimate == 0
    assert result.seconds_estimate == SECONDS_FLOOR


def test_estimate_is_cached_if_any_version_directory_is_complete(tmp_path):
    # An OLDER version than whatever fetch() would resolve live: estimate()
    # never touches the network to find out which version is current (see
    # its own docstring), so "cached" means "some version's own national
    # shard set is complete on disk", full stop.
    _seed_complete_uprn_cache(version="2025-11")
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    result = source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))

    assert result.bytes_estimate == 0


# --------------------------------------------------------------------------
# routing_note(): the estimate-panel warning, present until a complete
# cache exists and gone once one does.
# --------------------------------------------------------------------------


def test_routing_note_warns_when_no_complete_cache_exists(tmp_path):
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    assert source.routing_note() == (
        "Addresses: first use downloads the national OS Open UPRN file "
        "(619 MB, cached for every later survey)."
    )


def test_routing_note_is_none_once_a_complete_cache_exists(tmp_path):
    _seed_complete_uprn_cache()
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    assert source.routing_note() is None


# --------------------------------------------------------------------------
# fetch(): real zipfile bytes served through the opener seam, building
# real shards, deleting the raw zip, filtering the work part to the query
# bbox, falling back to a complete cache when listing is unreachable, and
# touching neither download route at all on a fully warm resume.
# --------------------------------------------------------------------------


def test_fetch_shards_the_national_csv_deletes_the_raw_zip_and_filters_to_the_bbox(
    tmp_path, ostn15_fixture_grid, monkeypatch
):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    e_min, n_min, e_max, n_max = _padded_extent(NEAR_TP06_BBOX, ostn15_fixture_grid)
    inside_e, inside_n = e_min + 5, n_min + 5
    outside_e, outside_n = e_max + 500, n_max + 500
    inside_lat, inside_lon = from_bng(inside_e, inside_n, ostn15_fixture_grid)
    outside_lat, outside_lon = from_bng(outside_e, outside_n, ostn15_fixture_grid)

    csv_bytes = _uprn_csv_bytes(
        [
            (100023336956, inside_e, inside_n, inside_lat, inside_lon),
            (100023336957, outside_e, outside_n, outside_lat, outside_lon),
        ]
    )
    zip_bytes = _zip_bytes("osopenuprn_202608.csv", csv_bytes)

    opener = _RoutedOpener(
        {
            _UPRN_VERSION_URL: _version_response("2026-08"),
            _UPRN_LISTING_URL: _listing_response([_uprn_entry(size=len(zip_bytes))]),
            _UPRN_ZIP_URL: _zip_response(zip_bytes),
        }
    )
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    parts = source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, NullProgress())

    assert [p.name for p in parts] == ["os_uprn.csv"]
    lines = (work_dir / "os_uprn.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "uprn,latitude,longitude"
    data_rows = lines[1:]
    assert len(data_rows) == 1
    assert data_rows[0].startswith("100023336956,")

    shard_dir = os_downloads.product_cache_dir(PRODUCT, "2026-08") / "shards"
    assert shards_complete(shard_dir)
    raw_dir = os_downloads.product_cache_dir(PRODUCT, "2026-08") / "raw"
    if raw_dir.exists():
        assert list(raw_dir.glob("*")) == []


def test_fetch_records_a_503_with_no_url_in_the_reason(tmp_path, ostn15_fixture_grid, monkeypatch):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)

    import urllib.error

    opener = _RoutedOpener(
        {
            _UPRN_VERSION_URL: _version_response("2026-08"),
            _UPRN_LISTING_URL: urllib.error.HTTPError(
                _UPRN_LISTING_URL, 503, "Service Unavailable", {}, None
            ),
        }
    )
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(OsOpenError):
        source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    failure = source.tile_failures[0]
    assert "https://" not in failure.reason
    assert "api.os.uk" not in failure.reason
    assert not (work_dir / "os_uprn.csv").exists()


def test_fetch_falls_back_to_a_complete_cache_when_listing_is_unreachable(
    tmp_path, ostn15_fixture_grid, monkeypatch
):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    _seed_complete_uprn_cache(version="2026-08")

    import urllib.error

    unreachable = urllib.error.URLError("the fake link is down")
    opener = _RoutedOpener({_UPRN_VERSION_URL: unreachable})
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    events: list[tuple[str, dict]] = []

    class _RecordingProgress:
        def emit(self, event, **fields):
            events.append((event, fields))

    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    parts = source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, _RecordingProgress())

    assert [p.name for p in parts] == ["os_uprn.csv"]
    fallback_events = [fields for name, fields in events if name == "os_uprn_cache_fallback"]
    assert len(fallback_events) == 1
    assert fallback_events[0]["version"] == "2026-08"
    assert fallback_events[0]["source"] == "os_uprn"


def test_fetch_with_a_fully_warm_cache_touches_neither_listing_nor_download(
    tmp_path, ostn15_fixture_grid, monkeypatch
):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    _seed_complete_uprn_cache(version="2026-08")

    # Exactly one product_version() route, and nothing more: a warm
    # fetch() should never call product_downloads or download_entry at
    # all, and this opener has no route for either, so it would raise
    # loudly (AssertionError, not silently pass) if it were ever asked.
    opener = _RoutedOpener({_UPRN_VERSION_URL: _version_response("2026-08")})
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    parts = source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, NullProgress())

    assert [p.name for p in parts] == ["os_uprn.csv"]


def test_fetch_respects_cancel_before_starting(tmp_path, ostn15_fixture_grid, monkeypatch):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: _PoisonedOpener())
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cancel = CancelToken()
    cancel.cancel()

    with pytest.raises(Cancelled):
        source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, NullProgress(), cancel=cancel)


# --------------------------------------------------------------------------
# merge(): a hand-built work part -> exactly one geojson, Point features
# with an int uprn, using the CSV's own lat/lon columns directly; zero
# rows writes no file at all.
# --------------------------------------------------------------------------


def test_merge_writes_points_with_int_uprn(tmp_path):
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-01"

    part_path = work_dir / "os_uprn.csv"
    part_path.write_text(
        "uprn,latitude,longitude\n100023336956,51.4526038,-2.6020703\n",
        encoding="utf-8",
    )

    written = source.merge([part_path], out_dir, stem)

    assert [p.name for p in written] == [f"{stem}_os_uprn.geojson"]
    payload = json.loads((out_dir / f"{stem}_os_uprn.geojson").read_text(encoding="utf-8"))
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 1
    feature = payload["features"][0]
    assert feature["type"] == "Feature"
    assert feature["properties"] == {"uprn": 100023336956, "source": "os_open_uprn"}
    assert isinstance(feature["properties"]["uprn"], int)
    assert feature["geometry"] == {
        "type": "Point",
        "coordinates": [-2.6020703, 51.4526038],
    }


def test_merge_writes_no_file_when_the_work_part_has_zero_rows(tmp_path):
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Empty-Extent_2026-08-01"

    part_path = work_dir / "os_uprn.csv"
    part_path.write_text("uprn,latitude,longitude\n", encoding="utf-8")

    written = source.merge([part_path], out_dir, stem)

    assert written == []
    assert not (out_dir / f"{stem}_os_uprn.geojson").exists()


def test_merge_writes_no_file_when_no_part_is_given_at_all(tmp_path):
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    written = source.merge([], out_dir, "Stem_2026-08-01")

    assert written == []


def test_possible_outputs_names_the_one_file(tmp_path):
    source = OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    assert source.possible_outputs("Barry-Waterfront_2026-08-01") == [
        "Barry-Waterfront_2026-08-01_os_uprn.geojson"
    ]


# --------------------------------------------------------------------------
# The one live test. Real OS Data Hub, no fakes, and NEVER the trigger for
# the 619 MB national download: skipped outright unless a complete UPRN
# cache already exists on this machine, built by a prior real survey that
# selected os_uprn.
# --------------------------------------------------------------------------

_COWBRIDGE_BBOX = "-3.460,51.455,-3.438,51.468"


@pytest.mark.live
def test_live_fetch_over_cowbridge_yields_over_1000_uprn_rows(tmp_path):
    """`routing_note()` is the same public signal package.py's own
    estimate warnings already read (see estimate_survey), so this test
    reuses it, rather than reaching into the cache layout by hand, to
    decide whether a complete national cache exists. A non-None note
    means the 619 MB download has never completed on this machine, and
    this test must not be what triggers it.

    With a complete cache present, fetch() over a real Cowbridge extent
    makes only the small `product_version` listing call (already proven
    network-free of the big download by the non-live tests above) and
    writes a work part filtered to the extent by the real, cached
    national shard set: the brief's own bar is more than 1000 rows for
    this real market town.
    """
    source = OsUprnSource(session=requests.Session())
    if source.routing_note() is not None:
        pytest.skip(
            "national UPRN cache not present; run a real survey with "
            "os_uprn selected to build it"
        )

    bbox = BBox.parse(_COWBRIDGE_BBOX)
    tile = Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    parts = source.fetch(bbox, [tile], work_dir, NullProgress())

    lines = parts[0].read_text(encoding="utf-8").splitlines()
    row_count = len(lines) - 1  # minus the header row.
    assert row_count > 1000, f"expected over 1000 Cowbridge addresses, got {row_count}"
