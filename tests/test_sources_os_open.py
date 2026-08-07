"""OsOpenSource's suite: OS Open map data (GB) as a LayerSource.

Ties together os_downloads.py (Task 1), os_gml.py (Task 2) and
os_shards.py (Task 3) into estimate()/fetch()/merge(), the same three-part
shape every sibling source in this package follows (see sources/inspire.py,
read end to end before this file was written, and sources/lidar_wales.py).

No test here ever touches the real network. Two seams are patched:
`mapgen.os_downloads._build_opener` (the urllib seam `product_downloads`,
`product_version` and `download_entry` all go through, the same seam
test_os_downloads.py's own `_FakeOpener` patches) and `self.session`, a
`requests.Session`-shaped fake this source hands to `ensure_ostn15`
(bng.py) and to `HttpByteSource` for OpenRoads' ranged reads. The OSTN15
grid itself is never downloaded: every fetch()-shaped test pre-seeds the
cache with tests/fixtures/ostn15's real committed slice around TP06
(Bridgend, South Wales, grid square SS), exactly the convention
test_lidar_wales.py and test_inspire.py already establish.

The one test that does touch the real network
(`test_live_fetch_and_merge_over_cowbridge`) is marked `live` and
deselected by default.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
import requests

from mapgen import bng, os_downloads
from mapgen.bng import from_bng, to_bng
from mapgen.egrid import PAD_METRES
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken, Cancelled
from mapgen.os_downloads import OsOpenError
from mapgen.os_shards import shards_complete, write_shards
from mapgen.sources.base import FAILURE_SERVICE_ERROR, NullProgress
from mapgen.sources.os_open import (
    GREENSPACE_BYTES_PER_SQUARE,
    OML_BYTES_PER_SQUARE,
    OSTN15_BYTES,
    ROADS_BYTES_PER_SQUARE,
    OsOpenSource,
    OsOpenSourceError,
)
from tests.fixtures.ostn15 import make_fixture

# TP06 (Bridgend, South Wales): the OSTN15 fixture's own 3x3 km block, and
# grid square SS (verified: grid_square(292184.87, 168003.465) == "SS").
_TP06_LAT = 51.4007822014
_TP06_LON = -3.5512834924
_TP06_E = 292184.87
_TP06_N = 168003.465

NEAR_TP06_BBOX = BBox.parse(
    f"{_TP06_LON - 0.0005},{_TP06_LAT - 0.0005},{_TP06_LON + 0.0005},{_TP06_LAT + 0.0005}"
)


@pytest.fixture
def ostn15_fixture_grid():
    return make_fixture.read_slice()


def _seed_ostn15_cache(cache_dir: Path, grid) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    bng._write_cache(cache_dir / bng._CACHE_FILENAME, grid)


def _tiles(*tile_ids: str) -> list[Tile]:
    bbox = NEAR_TP06_BBOX
    return [
        Tile(tile_id=tid, row=0, col=index, core_bbox=bbox, query_bbox=bbox)
        for index, tid in enumerate(tile_ids)
    ]


def _padded_extent(bbox: BBox, grid) -> tuple[float, float, float, float]:
    e1, n1 = to_bng(bbox.south, bbox.west, grid)
    e2, n2 = to_bng(bbox.north, bbox.east, grid)
    return (
        min(e1, e2) - PAD_METRES,
        min(n1, n2) - PAD_METRES,
        max(e1, e2) + PAD_METRES,
        max(n1, n2) + PAD_METRES,
    )


class _PoisonedSession:
    """Raises on any use. Proves estimate() never touches the network."""

    def get(self, *args, **kwargs):
        raise AssertionError("estimate() must never touch the network")


class _PoisonedOpener:
    def open(self, *args, **kwargs):
        raise AssertionError("estimate() must never touch the network")


@pytest.fixture(autouse=True)
def _isolate_osopen_cache(tmp_path, monkeypatch):
    """Every test gets its own ~/.mapgen/osopen: os_downloads.cache_root()
    reads mapgen.config.CONFIG_PATH.parent at call time (see os_downloads.py's
    own docstring), so patching CONFIG_PATH here is the same isolation
    mechanism test_os_downloads.py's own cache tests already use.
    """
    fake_home = tmp_path / "mapgen_home"
    fake_home.mkdir()
    monkeypatch.setattr(os_downloads, "CONFIG_PATH", fake_home / "config.json")


# --------------------------------------------------------------------------
# estimate(): never touches the network (poisoned session AND poisoned
# opener), prices three products x squares when cache is empty, prices 0 for
# a product whose fake shard dir already has a complete meta, and includes
# OSTN15_BYTES when no grid is cached.
# --------------------------------------------------------------------------


def test_estimate_never_touches_the_network_when_grid_is_cached(tmp_path, ostn15_fixture_grid, monkeypatch):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: _PoisonedOpener())
    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))  # must not raise


def test_estimate_never_touches_the_network_when_grid_is_not_cached(tmp_path, monkeypatch):
    # No OSTN15 cache seeded at all: estimate() must still never touch the
    # network even though it cannot place the extent on the National Grid.
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: _PoisonedOpener())
    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))  # must not raise


def test_estimate_prices_three_products_over_one_square_when_cache_empty(tmp_path, ostn15_fixture_grid):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    result = source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))

    expected = OML_BYTES_PER_SQUARE + ROADS_BYTES_PER_SQUARE + GREENSPACE_BYTES_PER_SQUARE
    assert result.bytes_estimate == expected
    assert result.seconds_estimate > 0


def test_estimate_prices_zero_for_a_product_whose_square_is_already_cached(tmp_path, ostn15_fixture_grid):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    # A fake, already-complete OpenMapLocal SS shard, built directly through
    # write_shards rather than by hand: this is os_shards.py's own
    # completion marker, and this test should trust the same mechanism
    # shards_complete() itself reads, not a hand-rolled meta.json shaped by
    # guesswork. The autouse _isolate_osopen_cache fixture already points
    # os_downloads.CONFIG_PATH at this test's own tmp_path, so
    # product_cache_dir resolves under it too.
    shard_dir = os_downloads.product_cache_dir("OpenMapLocal", "2026-04") / "shards" / "SS"
    write_shards(iter(()), shard_dir)
    assert shards_complete(shard_dir)

    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    result = source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))

    expected = ROADS_BYTES_PER_SQUARE + GREENSPACE_BYTES_PER_SQUARE
    assert result.bytes_estimate == expected


def test_estimate_includes_ostn15_bytes_when_grid_absent(tmp_path):
    # No cache seeded: load_ostn15() returns None, so the estimate must
    # include OSTN15_BYTES on top of one assumed square per product (see
    # the module's own estimate() docstring for why "one square" is the
    # honest fallback here, unlike lidar_wales's own area-only one).
    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)

    result = source.estimate(NEAR_TP06_BBOX, _tiles("r00_c00"))

    expected = (
        OML_BYTES_PER_SQUARE + ROADS_BYTES_PER_SQUARE + GREENSPACE_BYTES_PER_SQUARE
        + OSTN15_BYTES
    )
    assert result.bytes_estimate == expected


def test_estimate_is_zero_bytes_for_an_extent_with_no_gb_squares(tmp_path, ostn15_fixture_grid):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    # Nowhere near Great Britain at all.
    paris_bbox = BBox.parse("2.34,48.85,2.36,48.87")

    result = source.estimate(paris_bbox, _tiles("r00_c00"))

    assert result.bytes_estimate == 0


# --------------------------------------------------------------------------
# fetch(): real zipfile bytes served through the two seams (a routed urllib
# opener for os_downloads.py's own client, a routed ranged session for
# OpenRoads' HttpByteSource), building real shards, deleting the raw zip,
# filtering work parts to the query bbox, continuing past one product's
# failure, falling back to a complete cache when listing is unreachable,
# and touching neither seam at all on a fully warm resume.
# --------------------------------------------------------------------------

from tests.test_os_downloads import _FakeHTTPResponse  # noqa: E402
from tests.test_lidar_wales import _UrlRoutedSession  # noqa: E402

_OS_NS = "http://namespaces.os.uk/product/1.0"
_OML_NS = "http://namespaces.os.uk/open/oml/1.0"
_ROAD_NS = "http://namespaces.os.uk/Open/Roads/1.0"
_OGSP_NS = "http://namespaces.ordnancesurvey.co.uk/Open/Greenspace/1.0"
_GML_NS = "http://www.opengis.net/gml/3.2"

_OML_VERSION_URL = "https://api.os.uk/downloads/v1/products/OpenMapLocal"
_OML_LISTING_URL = "https://api.os.uk/downloads/v1/products/OpenMapLocal/downloads"
_OML_ZIP_URL = "https://blob.example.test/opmplc_gml3_ss.zip"

_ROADS_VERSION_URL = "https://api.os.uk/downloads/v1/products/OpenRoads"
_ROADS_LISTING_URL = "https://api.os.uk/downloads/v1/products/OpenRoads/downloads"
_ROADS_ZIP_URL = "https://blob.example.test/oproad_gml3_gb.zip"

_GREENSPACE_VERSION_URL = "https://api.os.uk/downloads/v1/products/OpenGreenspace"
_GREENSPACE_LISTING_URL = "https://api.os.uk/downloads/v1/products/OpenGreenspace/downloads"
_GREENSPACE_ZIP_URL = "https://blob.example.test/opgrsp_gml3_ss.zip"


def _oml_gml(buildings: list[tuple[str, float, float, float, float]]) -> bytes:
    """A minimal, real OpenMapLocal GML document: one Building per
    (gml_id, e_min, n_min, e_max, n_max), a plain closed rectangle ring.
    """
    members = []
    for gid, e0, n0, e1, n1 in buildings:
        pos_list = f"{e0} {n0} {e1} {n0} {e1} {n1} {e0} {n1} {e0} {n0}"
        members.append(
            f'<os:featureMember><oml:Building gml:id="{gid}">'
            f'<oml:geometry><gml:Surface srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">'
            f'<gml:patches><gml:PolygonPatch><gml:exterior><gml:LinearRing>'
            f"<gml:posList>{pos_list}</gml:posList>"
            f"</gml:LinearRing></gml:exterior></gml:PolygonPatch></gml:patches>"
            f"</gml:Surface></oml:geometry>"
            f"<oml:featureCode>15014</oml:featureCode>"
            f"</oml:Building></os:featureMember>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<os:FeatureCollection xmlns:os="{_OS_NS}" xmlns:oml="{_OML_NS}" xmlns:gml="{_GML_NS}">'
        + "".join(members)
        + "</os:FeatureCollection>"
    ).encode("utf-8")


def _roads_gml(links: list[tuple[str, float, float, float, float]]) -> bytes:
    members = []
    for gid, e0, n0, e1, n1 in links:
        members.append(
            f'<os:featureMember><road:RoadLink gml:id="{gid}">'
            f'<gml:LineString srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">'
            f"<gml:posList>{e0} {n0} {e1} {n1}</gml:posList>"
            f"</gml:LineString></road:RoadLink></os:featureMember>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<os:FeatureCollection xmlns:os="{_OS_NS}" xmlns:road="{_ROAD_NS}" xmlns:gml="{_GML_NS}">'
        + "".join(members)
        + "</os:FeatureCollection>"
    ).encode("utf-8")


def _greenspace_gml(sites: list[tuple[str, float, float]]) -> bytes:
    members = []
    for gid, e, n in sites:
        members.append(
            f'<os:featureMember><ogsp:GreenspaceSite gml:id="{gid}">'
            f'<gml:Point srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">'
            f"<gml:pos>{e} {n}</gml:pos>"
            f"</gml:Point></ogsp:GreenspaceSite></os:featureMember>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<os:FeatureCollection xmlns:os="{_OS_NS}" xmlns:ogsp="{_OGSP_NS}" xmlns:gml="{_GML_NS}">'
        + "".join(members)
        + "</os:FeatureCollection>"
    ).encode("utf-8")


def _zip_bytes(member_name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, content)
    return buffer.getvalue()


class _RoutedOpener:
    """Answers os_downloads.py's urllib calls by exact URL match, rather
    than a fixed call-order script: this drives three products' worth of
    calls whose exact order is fetch()'s own implementation detail, not
    something a test should have to predict to the call.

    Each route is a zero-argument callable so a URL requested more than
    once (not exercised today, but cheap to allow) always gets a fresh,
    unconsumed response rather than one whose body a previous `.read()`
    already drained.
    """

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


def _version_response(version: str) -> callable:
    body = json.dumps({"id": "x", "version": version}).encode("utf-8")
    return lambda: _FakeHTTPResponse(200, body)


def _listing_response(entries: list[dict]) -> callable:
    body = json.dumps(entries).encode("utf-8")
    return lambda: _FakeHTTPResponse(200, body)


def _zip_response(data: bytes) -> callable:
    return lambda: _FakeHTTPResponse(200, data)


def _oml_entry(url: str = _OML_ZIP_URL, size: int = 0) -> dict:
    return {"area": "SS", "format": "GML", "url": url, "size": size, "fileName": "opmplc_gml3_ss.zip", "md5": "0" * 32}


def _roads_entry(url: str = _ROADS_ZIP_URL, size: int = 0) -> dict:
    return {"area": "GB", "format": "GML", "url": url, "size": size, "fileName": "oproad_gml3_gb.zip", "md5": "0" * 32}


def _greenspace_entry(url: str = _GREENSPACE_ZIP_URL, size: int = 0) -> dict:
    return {"area": "SS", "format": "GML", "url": url, "size": size, "fileName": "opgrsp_gml3_ss.zip", "md5": "0" * 32}


def _healthy_routes(oml_zip: bytes, roads_zip: bytes, greenspace_zip: bytes, version: str = "2026-04") -> dict:
    return {
        _OML_VERSION_URL: _version_response(version),
        _OML_LISTING_URL: _listing_response([_oml_entry(size=len(oml_zip))]),
        _OML_ZIP_URL: _zip_response(oml_zip),
        _ROADS_VERSION_URL: _version_response(version),
        _ROADS_LISTING_URL: _listing_response([_roads_entry(size=len(roads_zip))]),
        _GREENSPACE_VERSION_URL: _version_response(version),
        _GREENSPACE_LISTING_URL: _listing_response([_greenspace_entry(size=len(greenspace_zip))]),
        _GREENSPACE_ZIP_URL: _zip_response(greenspace_zip),
    }


def test_fetch_shards_per_square_deletes_raw_zips_and_filters_work_parts_to_the_bbox(
    tmp_path, ostn15_fixture_grid, monkeypatch
):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    e_min, n_min, e_max, n_max = _padded_extent(NEAR_TP06_BBOX, ostn15_fixture_grid)
    inside = (e_min + 5, n_min + 5, e_min + 15, n_min + 15)
    outside = (e_max + 500, n_max + 500, e_max + 510, n_max + 510)

    oml_zip = _zip_bytes("data/SS.gml", _oml_gml([("id-inside", *inside), ("id-outside", *outside)]))
    roads_zip = _zip_bytes(
        "data/OSOpenRoads_SS.gml",
        _roads_gml([("id-road-inside", inside[0], inside[1], inside[2], inside[3])]),
    )
    greenspace_zip = _zip_bytes(
        "data/SS.gml",
        _greenspace_gml([("id-green-inside", inside[0], inside[1])]),
    )

    opener = _RoutedOpener(_healthy_routes(oml_zip, roads_zip, greenspace_zip))
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)
    session = _UrlRoutedSession({_ROADS_ZIP_URL: roads_zip})

    source = OsOpenSource(session=session, ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    parts = source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, NullProgress())

    assert sorted(p.name for p in parts) == [
        "os_open_OpenGreenspace.ndjson", "os_open_OpenMapLocal.ndjson", "os_open_OpenRoads.ndjson",
    ]

    oml_records = [
        json.loads(line)
        for line in (work_dir / "os_open_OpenMapLocal.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["id"] for r in oml_records] == ["id-inside"]

    # The shard is complete and the raw zip is gone.
    shard_dir = os_downloads.product_cache_dir("OpenMapLocal", "2026-04") / "shards" / "SS"
    assert shards_complete(shard_dir)
    raw_dir = os_downloads.product_cache_dir("OpenMapLocal", "2026-04") / "raw"
    if raw_dir.exists():
        assert list(raw_dir.glob("*")) == []


def test_fetch_records_a_503_against_one_product_and_still_completes_the_others(
    tmp_path, ostn15_fixture_grid, monkeypatch
):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    e_min, n_min, e_max, n_max = _padded_extent(NEAR_TP06_BBOX, ostn15_fixture_grid)
    inside = (e_min + 5, n_min + 5, e_min + 15, n_min + 15)

    oml_zip = _zip_bytes("data/SS.gml", _oml_gml([("id-oml", *inside)]))
    roads_zip = _zip_bytes(
        "data/OSOpenRoads_SS.gml", _roads_gml([("id-road", inside[0], inside[1], inside[2], inside[3])])
    )

    import urllib.error

    routes = {
        _OML_VERSION_URL: _version_response("2026-04"),
        _OML_LISTING_URL: _listing_response([_oml_entry(size=len(oml_zip))]),
        _OML_ZIP_URL: _zip_response(oml_zip),
        _ROADS_VERSION_URL: _version_response("2026-04"),
        _ROADS_LISTING_URL: _listing_response([_roads_entry(size=len(roads_zip))]),
        _GREENSPACE_VERSION_URL: _version_response("2026-04"),
        _GREENSPACE_LISTING_URL: urllib.error.HTTPError(
            _GREENSPACE_LISTING_URL, 503, "Service Unavailable", {}, None
        ),
    }

    opener = _RoutedOpener(routes)
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)
    session = _UrlRoutedSession({_ROADS_ZIP_URL: roads_zip})

    source = OsOpenSource(session=session, ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(OsOpenError):
        source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, NullProgress())

    assert (work_dir / "os_open_OpenMapLocal.ndjson").exists()
    assert (work_dir / "os_open_OpenRoads.ndjson").exists()
    assert not (work_dir / "os_open_OpenGreenspace.ndjson").exists()

    assert len(source.tile_failures) == 1
    failure = source.tile_failures[0]
    assert failure.kind == FAILURE_SERVICE_ERROR
    assert "OpenGreenspace" in failure.reason
    assert "SS" in failure.reason
    assert "https://" not in failure.reason
    assert "blob.example" not in failure.reason
    assert "api.os.uk" not in failure.reason


def test_fetch_falls_back_to_a_complete_cache_when_listing_is_unreachable(tmp_path, ostn15_fixture_grid, monkeypatch):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)

    # Pre-seed a complete shard for every product's SS square, under a
    # version this fetch() call will never itself learn from the (poisoned)
    # listing: only the cache can tell it "2026-04" is usable.
    for product in OsOpenSource.PRODUCTS:
        shard_dir = os_downloads.product_cache_dir(product, "2026-04") / "shards" / "SS"
        write_shards(iter(()), shard_dir)
        assert shards_complete(shard_dir)

    import urllib.error

    unreachable = urllib.error.URLError("the fake link is down")
    opener = _RoutedOpener(
        {
            _OML_VERSION_URL: unreachable,
            _ROADS_VERSION_URL: unreachable,
            _GREENSPACE_VERSION_URL: unreachable,
        }
    )
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    events: list[tuple[str, dict]] = []

    class _RecordingProgress:
        def emit(self, event, **fields):
            events.append((event, fields))

    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    parts = source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, _RecordingProgress())

    assert len(parts) == 3
    fallback_events = [fields for name, fields in events if name == "os_open_cache_fallback"]
    assert len(fallback_events) == 3
    assert all(fields["version"] == "2026-04" for fields in fallback_events)
    assert {fields["product"] for fields in fallback_events} == set(OsOpenSource.PRODUCTS)


def test_fetch_with_a_fully_warm_cache_touches_neither_seam(tmp_path, ostn15_fixture_grid, monkeypatch):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)

    for product in OsOpenSource.PRODUCTS:
        shard_dir = os_downloads.product_cache_dir(product, "2026-04") / "shards" / "SS"
        write_shards(iter(()), shard_dir)

    # Exactly one product_version() response per product, and nothing more:
    # a warm fetch() should never call product_downloads or download_entry
    # at all, and this opener has no route for either, so it would raise
    # loudly if it were ever asked.
    opener = _RoutedOpener(
        {
            _OML_VERSION_URL: _version_response("2026-04"),
            _ROADS_VERSION_URL: _version_response("2026-04"),
            _GREENSPACE_VERSION_URL: _version_response("2026-04"),
        }
    )
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    parts = source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, NullProgress())

    assert len(parts) == 3


def test_fetch_respects_cancel_before_starting(tmp_path, ostn15_fixture_grid, monkeypatch):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: _PoisonedOpener())
    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cancel = CancelToken()
    cancel.cancel()

    with pytest.raises(Cancelled):
        source.fetch(NEAR_TP06_BBOX, _tiles("r00_c00"), work_dir, NullProgress(), cancel=cancel)


# --------------------------------------------------------------------------
# merge(): fixture work parts (the raw {"id","type","geometry","properties"}
# shape os_shards.features_in itself yields, built by hand here rather than
# through a real fetch(), exactly the unit-test boundary Step 6 asks for)
# -> exactly the six named files, WGS84 coordinates verified against the
# OSTN15 fixture's own known station, exact properties per product, and no
# file at all for a product/bucket with zero features.
# --------------------------------------------------------------------------


def _feature(feature_id, feature_type, geometry, properties) -> dict:
    return {"id": feature_id, "type": feature_type, "geometry": geometry, "properties": properties}


def _write_part(work_dir: Path, product: str, features: list[dict]) -> Path:
    path = work_dir / f"os_open_{product}.ndjson"
    text = "".join(json.dumps(f, separators=(",", ":")) + "\n" for f in features)
    path.write_text(text, encoding="utf-8")
    return path


def _rect(e0, n0, e1, n1) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[e0, n0], [e1, n0], [e1, n1], [e0, n1], [e0, n0]]],
    }


def _line(e0, n0, e1, n1) -> dict:
    return {"type": "LineString", "coordinates": [[e0, n0], [e1, n1]]}


def _point(e, n) -> dict:
    return {"type": "Point", "coordinates": [e, n]}


@pytest.fixture
def merge_source(tmp_path, ostn15_fixture_grid):
    _seed_ostn15_cache(tmp_path, ostn15_fixture_grid)
    return OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)


def test_merge_writes_the_six_named_files_with_the_stem_embedded_and_exact_properties(
    tmp_path, merge_source
):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-01"

    oml_part = _write_part(
        work_dir, "OpenMapLocal",
        [
            _feature("id-building", "Building", _rect(_TP06_E, _TP06_N, _TP06_E + 10, _TP06_N + 10), {"code": "15014"}),
            _feature(
                "id-important", "ImportantBuilding",
                _rect(_TP06_E + 20, _TP06_N, _TP06_E + 30, _TP06_N + 10),
                {"code": "15025", "theme": "Religious Buildings", "class": "Place Of Worship"},
            ),
            _feature("id-track", "RailwayTrack", _line(_TP06_E, _TP06_N, _TP06_E + 10, _TP06_N + 10), {"class": "Multi Track"}),
            # A tunnel with no classification: os_gml.py's own fallback shape
            # is {"code": featureCode}, but the brief's exact os_rail.geojson
            # property map names only "class"; this tunnel's own "code"
            # must NOT leak into the merged output (see the module's own
            # _rail_properties).
            _feature("id-tunnel", "RailwayTunnel", _line(_TP06_E, _TP06_N, _TP06_E + 5, _TP06_N + 5), {"code": "15310"}),
            _feature(
                "id-site", "FunctionalSite", _rect(_TP06_E + 40, _TP06_N, _TP06_E + 50, _TP06_N + 10),
                {"name": "Silverton School", "theme": "Education", "class": "Primary Education"},
            ),
            _feature("id-named-place", "NamedPlace", _point(_TP06_E, _TP06_N), {"name": "Atlantic View", "class": "Populated Place"}),
            _feature("id-woodland", "Woodland", _rect(_TP06_E + 60, _TP06_N, _TP06_E + 70, _TP06_N + 10), {"code": "10071"}),
        ],
    )
    roads_part = _write_part(
        work_dir, "OpenRoads",
        [
            _feature(
                "id-road", "RoadLink", _line(_TP06_E, _TP06_N, _TP06_E + 100, _TP06_N + 50),
                {
                    "class": "A Road", "function": "A Road", "form": "Single Carriageway",
                    "name": "Culver Way", "number": "A4050", "trunk": True, "primary": False,
                    "length": 625.0,
                },
            ),
        ],
    )
    greenspace_part = _write_part(
        work_dir, "OpenGreenspace",
        [
            _feature(
                "id-green", "GreenspaceSite", _rect(_TP06_E, _TP06_N, _TP06_E + 5, _TP06_N + 5),
                {"function": "Public Park Or Garden", "name": "Killerton Gardens"},
            ),
            _feature("id-access", "AccessPoint", _point(_TP06_E + 1, _TP06_N + 1), {"access": "Pedestrian", "site": "id-green"}),
        ],
    )

    written = merge_source.merge([oml_part, roads_part, greenspace_part], out_dir, stem)

    names = sorted(p.name for p in written)
    assert names == sorted(
        f"{stem}_os_{suffix}.geojson"
        for suffix in ("buildings", "roads", "rail", "greenspace", "sites", "land")
    )
    for path in written:
        assert path.parent == out_dir

    def _load(suffix: str) -> dict:
        return json.loads((out_dir / f"{stem}_os_{suffix}.geojson").read_text(encoding="utf-8"))

    buildings = _load("buildings")
    assert buildings["type"] == "FeatureCollection"
    building_props = [f["properties"] for f in buildings["features"]]
    assert {"source": "os_openmap_local", "code": "15014"} in building_props
    assert {
        "source": "os_openmap_local", "code": "15025",
        "theme": "Religious Buildings", "class": "Place Of Worship",
    } in building_props

    rail = _load("rail")
    rail_props = [f["properties"] for f in rail["features"]]
    assert {"source": "os_openmap_local", "class": "Multi Track"} in rail_props
    # The classless tunnel: "code" must be absent, not merely None.
    assert {"source": "os_openmap_local"} in rail_props
    for props in rail_props:
        assert "code" not in props

    sites = _load("sites")
    sites_props = [f["properties"] for f in sites["features"]]
    assert {
        "source": "os_openmap_local", "name": "Silverton School",
        "theme": "Education", "class": "Primary Education",
    } in sites_props
    assert {"source": "os_openmap_local", "name": "Atlantic View", "class": "Populated Place"} in sites_props

    land = _load("land")
    assert land["features"] == [
        {
            "type": "Feature",
            "properties": {"source": "os_openmap_local", "kind": "woodland"},
            "geometry": land["features"][0]["geometry"],
        }
    ]

    roads = _load("roads")
    assert roads["features"][0]["properties"] == {
        "source": "os_open_roads", "class": "A Road", "function": "A Road",
        "form": "Single Carriageway", "name": "Culver Way", "number": "A4050",
        "trunk": True, "primary": False,
    }
    assert "length" not in roads["features"][0]["properties"]

    greenspace = _load("greenspace")
    greenspace_props = [f["properties"] for f in greenspace["features"]]
    assert {"source": "os_open_greenspace", "function": "Public Park Or Garden", "name": "Killerton Gardens"} in greenspace_props
    assert {"source": "os_open_greenspace", "access": "Pedestrian"} in greenspace_props

    # WGS84 reprojection, checked against the OSTN15 fixture's own known
    # station (tests/fixtures/ostn15/stations.txt), not merely re-derived
    # via from_bng a second time in this test.
    named_place = next(f for f in sites["features"] if f["properties"]["name"] == "Atlantic View")
    lon, lat = named_place["geometry"]["coordinates"]
    assert lon == pytest.approx(_TP06_LON, abs=1e-6)
    assert lat == pytest.approx(_TP06_LAT, abs=1e-6)
    assert named_place["geometry"]["type"] == "Point"


def test_merge_writes_no_file_for_a_product_with_zero_features(tmp_path, merge_source):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Empty-Greenspace_2026-08-01"

    oml_part = _write_part(
        work_dir, "OpenMapLocal",
        [_feature("id-building", "Building", _rect(_TP06_E, _TP06_N, _TP06_E + 10, _TP06_N + 10), {"code": "15014"})],
    )
    roads_part = _write_part(
        work_dir, "OpenRoads",
        [_feature("id-road", "RoadLink", _line(_TP06_E, _TP06_N, _TP06_E + 10, _TP06_N + 10), {"class": "A Road"})],
    )
    # A genuinely empty product: fetch() writes this file even when zero
    # features matched the extent (decision 3: legal, not fabricated).
    greenspace_part = _write_part(work_dir, "OpenGreenspace", [])

    written = merge_source.merge([oml_part, roads_part, greenspace_part], out_dir, stem)
    names = {p.name for p in written}

    assert f"{stem}_os_buildings.geojson" in names
    assert f"{stem}_os_roads.geojson" in names
    assert f"{stem}_os_greenspace.geojson" not in names
    assert not (out_dir / f"{stem}_os_greenspace.geojson").exists()
    # OpenMapLocal contributed nothing to rail/sites/land in this fixture.
    assert f"{stem}_os_rail.geojson" not in names
    assert f"{stem}_os_sites.geojson" not in names
    assert f"{stem}_os_land.geojson" not in names


def test_merge_raises_when_no_ostn15_grid_is_cached(tmp_path):
    source = OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    part = _write_part(
        work_dir, "OpenMapLocal",
        [_feature("id-building", "Building", _rect(0, 0, 10, 10), {"code": "15014"})],
    )

    with pytest.raises(OsOpenSourceError):
        source.merge([part], out_dir, "Some-Stem_2026-08-01")


def test_possible_outputs_names_the_six_files(tmp_path, merge_source):
    stem = "Barry-Waterfront_2026-08-01"
    assert sorted(merge_source.possible_outputs(stem)) == sorted(
        f"{stem}_os_{suffix}.geojson"
        for suffix in ("buildings", "roads", "rail", "greenspace", "sites", "land")
    )


# --------------------------------------------------------------------------
# Step 9: the one live test. Real OS Data Hub, real Azure blob storage, no
# fakes. First run downloads OpenMapLocal's SS+ST-shaped square(s), the
# OpenRoads member(s) for the same square(s), and the Greenspace zip(s):
# on the order of 200 MB the first time this cache is ever built, cached
# under ~/.mapgen/osopen from then on (the real default cache_root(), not a
# tmp_path override, for the same reason test_live_smoke.py's own live
# tests leave OSTN15's cache at its real default: a survey tool's own real
# cache is what this is actually proving, and repeating a ~200 MB download
# on every run of this one test would be a poor trade for that proof).
#
# Cowbridge, Vale of Glamorgan: the brief points at "the verified-facts
# section" for this bbox, but that section (see this task's own plan doc)
# names only building/curve COUNTS for a previous, much larger Cowbridge
# survey, never a bbox. No bbox for Cowbridge is committed anywhere in this
# repository. The box below is this task's own choice: the town's historic
# core plus its immediate residential streets, small enough to keep this
# test's own download and parse time well under the wall-time budget below,
# large enough that a market town this size clears 500 buildings.
# --------------------------------------------------------------------------

_COWBRIDGE_BBOX = "-3.460,51.455,-3.438,51.468"


class _CountingSession:
    """Wraps a real `requests.Session`, counting bytes actually read off
    the wire through `iter_content` (`HttpByteSource.read`'s own consumption
    path, cog.py). Used here only to measure OpenRoads' own ranged-read
    traffic: OpenMapLocal/OpenGreenspace's whole-zip downloads go through
    os_downloads.py's own urllib opener instead, measured via
    `download_progress` events below, so the two never double-count each
    other; `ensure_ostn15`'s own one-time, ~41 MB pack download DOES count
    here too when the OSTN15 cache is not already warm on this machine
    (this test leaves the real default cache in place precisely to make
    that the uncommon case; see the module docstring above).
    """

    def __init__(self, real_session: object) -> None:
        self._real = real_session
        self.bytes_fetched = 0

    def get(self, *args, **kwargs):
        response = self._real.get(*args, **kwargs)
        original_iter_content = response.iter_content

        def counted(chunk_size):
            for chunk in original_iter_content(chunk_size):
                self.bytes_fetched += len(chunk)
                yield chunk

        response.iter_content = counted
        return response


class _TimingProgress:
    def __init__(self) -> None:
        self.events: list[tuple[float, str, dict]] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((time.monotonic(), event, fields))


@pytest.mark.live
def test_live_fetch_and_merge_over_cowbridge(tmp_path):
    """Fetches all three products over a real, tiny Cowbridge extent,
    merges them, and prints measured bytes/seconds per product to stdout
    for Task 9's own constants refit (this task does not itself update
    OML_BYTES_PER_SQUARE/ROADS_BYTES_PER_SQUARE/GREENSPACE_BYTES_PER_SQUARE/
    SECONDS_FLOOR from what it measures here; see the task report).
    """
    session = _CountingSession(requests.Session())
    source = OsOpenSource(session=session)
    bbox = BBox.parse(_COWBRIDGE_BBOX)
    tile = Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    progress = _TimingProgress()

    started = time.monotonic()
    parts = source.fetch(bbox, [tile], work_dir, progress)
    fetch_elapsed = time.monotonic() - started
    assert fetch_elapsed < 15 * 60, f"fetch() took {fetch_elapsed:.1f}s, over the 15 minute budget"

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Cowbridge_2026-08-07"
    written = source.merge(parts, out_dir, stem)
    names = {p.name for p in written}

    # Five of the brief's six named outputs, not all six: run against the
    # real OS Data Hub, this exact extent produced no os_rail.geojson at
    # all, and that is real ground truth, not a bug. Cowbridge's own
    # railway (the Vale of Glamorgan Railway's Cowbridge branch) closed to
    # passengers in 1951 and to freight in 1965; OS Open's RailwayTrack/
    # RailwayTunnel simply have nothing to report over this town today.
    # Zero features for a category is legal (decision 3: "nothing
    # fabricated"), and this live run is the proof rather than an assumed
    # case; see the task report.
    for suffix in ("buildings", "roads", "greenspace", "sites", "land"):
        assert f"{stem}_os_{suffix}.geojson" in names, f"no {suffix} output for the Cowbridge extent"
    assert f"{stem}_os_rail.geojson" not in names, (
        "Cowbridge's own OS Open data now carries a rail feature this test "
        "did not expect; loosen this assertion back to the brief's original "
        "six-file claim if that is confirmed real"
    )

    buildings = json.loads((out_dir / f"{stem}_os_buildings.geojson").read_text(encoding="utf-8"))
    assert len(buildings["features"]) > 500

    # Per-product wall time: fetch() processes PRODUCTS strictly in order
    # (see its own docstring), so the timestamp of a product's own last
    # tile_done event marks its cumulative completion time.
    per_product_done_at: dict[str, float] = {}
    for timestamp, event, fields in progress.events:
        if event == "tile_done" and "product" in fields:
            per_product_done_at[fields["product"]] = timestamp

    per_product_seconds: dict[str, float] = {}
    previous = started
    for product in OsOpenSource.PRODUCTS:
        done_at = per_product_done_at.get(product)
        if done_at is not None:
            per_product_seconds[product] = done_at - previous
            previous = done_at

    # Bytes for OpenMapLocal/OpenGreenspace: this tiny extent can straddle
    # the SS/ST seam (Cowbridge does: padded_bng_extent above computes two
    # needed squares here, not one), so a product's own window can hold TWO
    # separate zip downloads, each its own download_progress stream ending
    # at that square's own bytes_total. Summing the DISTINCT bytes_total
    # values seen in the window is the true total downloaded; taking their
    # max (this test's first pass at this) silently reports only the
    # larger of the two squares' own sizes. See the task report.
    zip_download_bytes: dict[str, int] = {}
    window_start = started
    for product in OsOpenSource.PRODUCTS:
        done_at = per_product_done_at.get(product)
        if done_at is None:
            continue
        if product in ("OpenMapLocal", "OpenGreenspace"):
            totals_seen = {
                int(fields.get("bytes_total", 0))
                for timestamp, event, fields in progress.events
                if event == "download_progress" and window_start < timestamp <= done_at
            }
            zip_download_bytes[product] = sum(totals_seen)
        window_start = done_at

    print(
        f"\nTask 4 live OS Open probe (Cowbridge, {_COWBRIDGE_BBOX}): "
        f"fetch_wall_seconds={fetch_elapsed:.2f}, "
        f"per_product_seconds={per_product_seconds}, "
        f"OpenMapLocal_zip_bytes={zip_download_bytes.get('OpenMapLocal')}, "
        f"OpenGreenspace_zip_bytes={zip_download_bytes.get('OpenGreenspace')}, "
        f"OpenRoads_ranged_read_bytes={session.bytes_fetched} (may include a "
        f"one-time OSTN15 pack download if this machine's cache was cold), "
        f"buildings_features={len(buildings['features'])}"
    )
