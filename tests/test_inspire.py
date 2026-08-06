import datetime
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
import requests

import mapgen.sources.inspire as inspire_module
from mapgen.bng import _NODE_COUNT, Ostn15Grid, tm_inverse
from mapgen.boundary_curves import boundary_curves
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken, Cancelled
from mapgen.package import get_source, register_default_sources
from mapgen.sources.base import (
    FAILURE_NO_OUTPUT,
    FAILURE_RATE_LIMITED,
    FAILURE_SERVICE_ERROR,
    FAILURE_TIMEOUT,
    NullProgress,
)
from mapgen.sources.inspire import (
    AUTHORITY_INDEX_PATH,
    GML_MEMBER_NAME,
    INSPIRE_DOWNLOAD_URL_TEMPLATE,
    META_WORK_NAME,
    OUTSIDE_ENGLAND_AND_WALES_MESSAGE,
    PARCELS_WORK_NAME,
    BYTES_PER_AUTHORITY,
    SECONDS_FLOOR,
    InspireError,
    InspireSource,
    ParseCounts,
    authorities_for,
    fetch_authority_zip,
    load_authority_index,
    parcels_in,
)

# See make_authority_index.py's module docstring: a hyphen is neither a
# space nor a path separator, and five real HMLR authority names carry
# one (Newcastle-under-Lyme and friends), so the safety property this
# checks is the one the brief's own parenthetical states, not its
# hyphen-free character class.
_NAME_SHAPE = re.compile(r"^[A-Za-z0-9_.-]+$")


def test_llantwit_major_bbox_returns_only_vale_of_glamorgan():
    bbox = BBox(west=-3.495, south=51.395, east=-3.475, north=51.410)
    assert authorities_for(bbox) == ["Vale_of_Glamorgan_Council"]


def test_bbox_spanning_cardiff_vale_border_returns_both():
    bbox = BBox(west=-3.263442, south=51.438073, east=-3.243442, north=51.458073)
    assert authorities_for(bbox) == ["Cardiff_Council", "Vale_of_Glamorgan_Council"]


def test_scottish_bbox_returns_no_authorities():
    # INSPIRE Index Polygons cover England and Wales only. Central
    # Edinburgh, nowhere near any England/Wales authority even with the
    # 1 km pad.
    bbox = BBox(west=-3.30, south=55.90, east=-3.10, north=56.00)
    assert authorities_for(bbox) == []


def test_result_is_sorted():
    # Cardiff then Vale of Glamorgan alphabetically, not fetch order.
    bbox = BBox(west=-3.263442, south=51.438073, east=-3.243442, north=51.458073)
    result = authorities_for(bbox)
    assert result == sorted(result)


def test_index_file_has_318_entries():
    index = load_authority_index()
    assert len(index) == 318


def test_every_name_is_a_safe_path_segment_and_filename():
    index = load_authority_index()
    bad = [name for name in index if not _NAME_SHAPE.match(name)]
    assert bad == []


def test_every_bbox_is_four_floats_west_south_east_north():
    index = load_authority_index()
    for name, bbox in index.items():
        assert len(bbox) == 4, name
        west, south, east, north = bbox
        assert west < east, name
        assert south < north, name


def test_the_four_home_councils_are_present_with_plausible_geography():
    index = load_authority_index()

    # South Wales, this tool's own reference area. Roughly ordered west
    # to east: Bridgend, Vale of Glamorgan/Cardiff (Cardiff to the east
    # of Vale), Merthyr Tydfil to the north of both.
    bridgend = index["Bridgend_County_Borough_Council"]
    vale = index["Vale_of_Glamorgan_Council"]
    cardiff = index["Cardiff_Council"]
    merthyr = index["Merthyr_Tydfil_County_Borough_Council"]

    assert bridgend[2] < cardiff[0]  # Bridgend's east sits west of Cardiff's west
    assert vale[3] < merthyr[3]  # Vale sits south of Merthyr Tydfil's north
    assert cardiff[0] < cardiff[2] and cardiff[1] < cardiff[3]


def test_authorities_for_intersecting_bbox_but_no_authority_returns_empty_list():
    # Mid-Atlantic: not a malformed bbox, just genuinely nowhere near
    # England or Wales.
    bbox = BBox(west=-30.0, south=40.0, east=-29.0, north=41.0)
    assert authorities_for(bbox) == []


def test_authority_index_path_points_at_the_committed_fixture():
    assert AUTHORITY_INDEX_PATH.name == "authority_index.json"
    assert AUTHORITY_INDEX_PATH.exists()


def test_load_authority_index_raises_inspire_error_on_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(InspireError):
        load_authority_index(missing)


def test_load_authority_index_raises_inspire_error_on_malformed_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(InspireError):
        load_authority_index(bad)


def test_load_authority_index_raises_inspire_error_on_wrong_shape(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"Some_Council": [1.0, 2.0, 3.0]}', encoding="utf-8")
    with pytest.raises(InspireError):
        load_authority_index(bad)


# ==========================================================================
# Task 2: fetch_authority_zip and parcels_in.
# ==========================================================================
#
# The real committed fixture (tests/fixtures/inspire/parcels_sample.gml,
# see make_gml_fixture.py for its provenance) covers every "does the real
# GML shape parse" test: counts, bbox filtering, the shared-edge pair,
# the interior ring, the timestamp year. Synthetic in-memory GML covers
# every edge case that fixture, built from a real and therefore
# well-formed download, has no reason to carry: malformed posLists, a
# missing exterior, a wrong srsName. Neither kind of GML is ever written
# to tests/fixtures/inspire itself; the synthetic ones are built fresh by
# each test that needs one, in tmp_path.

FIXTURE_GML_PATH = Path(__file__).resolve().parent / "fixtures" / "inspire" / "parcels_sample.gml"

# Covers the whole committed fixture's own extent (E 313526..313587, N
# 168911..168987; see make_gml_fixture.py's own module docstring for the
# member-by-member breakdown this was checked against).
FIXTURE_BBOX_ALL = (313_500.0, 168_900.0, 313_600.0, 169_000.0)
# Overlaps only the fixture's first member's own exterior bbox (E
# 313526.5..313548.47, N 168911.18..168929.25); its second member starts
# at E 313531.15, past this bbox's own 313530 east edge.
FIXTURE_BBOX_MEMBER_1_ONLY = (313_500.0, 168_905.0, 313_530.0, 168_925.0)
# Nowhere near the fixture (which sits in South Wales, BNG E/N in the
# 300,000s/160,000s).
FIXTURE_BBOX_ELSEWHERE = (0.0, 0.0, 100.0, 100.0)

# A bbox wide enough to hold anything the small synthetic GML snippets
# below use (their own posLists are toy 0..10 coordinates, nowhere near
# real BNG values), so those tests can assert on parse counts without
# also having to reason about bbox filtering.
WORLD_BBOX = (-1.0e9, -1.0e9, 1.0e9, 1.0e9)


def _fixture_zip(tmp_path: Path) -> Path:
    """The committed fixture GML, zipped up under the real member name,
    exactly the shape a real HMLR download has. Built fresh per test
    (cheap: the fixture is a few KB) rather than committing a .zip
    itself, so the fixture stays a plain, diffable text file.
    """
    zip_path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(FIXTURE_GML_PATH, arcname=GML_MEMBER_NAME)
    return zip_path


_GML_NS_DECLARATIONS = (
    'xmlns:wfs="http://www.opengis.net/wfs/2.0" '
    'xmlns:gml="http://www.opengis.net/gml/3.2" '
    'xmlns:LR="www.landregistry.gov.uk"'
)


def _one_member_gml(
    *,
    pos_list: str | None = "0 0 10 0 10 10 0 10 0 0",
    srs_name: str = "urn:ogc:def:crs:EPSG::27700",
    include_exterior: bool = True,
    interior_pos_list: str | None = None,
    time_stamp: str = "2026-08-02T03:47:41.090Z",
) -> bytes:
    """A synthetic one-member wfs:FeatureCollection for the malformed and
    srsName edge cases the real, well-formed committed fixture has no
    reason to carry. Same namespaces, same element shape as the real
    file (see the module docstring in inspire.py), a toy square for
    coordinates.
    """
    exterior = ""
    if include_exterior:
        pos_list_xml = f"<gml:posList>{pos_list}</gml:posList>" if pos_list is not None else ""
        exterior = f"<gml:exterior><gml:LinearRing>{pos_list_xml}</gml:LinearRing></gml:exterior>"
    interior = ""
    if interior_pos_list is not None:
        interior = (
            "<gml:interior><gml:LinearRing>"
            f"<gml:posList>{interior_pos_list}</gml:posList>"
            "</gml:LinearRing></gml:interior>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<wfs:FeatureCollection {_GML_NS_DECLARATIONS} "
        f'numberMatched="1" numberReturned="1" timeStamp="{time_stamp}">'
        "<wfs:member><LR:PREDEFINED>"
        f'<LR:GEOMETRY><gml:Polygon srsName="{srs_name}" srsDimension="2">'
        f"{exterior}{interior}"
        "</gml:Polygon></LR:GEOMETRY>"
        "</LR:PREDEFINED></wfs:member>"
        "</wfs:FeatureCollection>"
    ).encode("utf-8")


def _write_synthetic_zip(path: Path, gml_bytes: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(GML_MEMBER_NAME, gml_bytes)
    return path


def _ring_bbox_for_test(ring: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    eastings = [point[0] for point in ring]
    northings = [point[1] for point in ring]
    return min(eastings), min(northings), max(eastings), max(northings)


# --------------------------------------------------------------------------
# parcels_in against the real fixture.
# --------------------------------------------------------------------------


def test_parcels_in_reads_every_member_and_keeps_counts(tmp_path):
    zip_path = _fixture_zip(tmp_path)
    stream = parcels_in(zip_path, FIXTURE_BBOX_ALL)
    parcels = list(stream)

    assert len(parcels) == 10
    assert stream.counts == ParseCounts(
        parcels_seen=10, parcels_kept=10, parcels_skipped_malformed=0
    )


def test_parcels_in_reports_the_collections_timestamp_year(tmp_path):
    zip_path = _fixture_zip(tmp_path)
    stream = parcels_in(zip_path, FIXTURE_BBOX_ALL)
    list(stream)

    assert stream.timestamp_year == 2026


def test_parcels_in_bbox_filters_to_only_the_intersecting_parcel(tmp_path):
    zip_path = _fixture_zip(tmp_path)
    stream = parcels_in(zip_path, FIXTURE_BBOX_MEMBER_1_ONLY)
    parcels = list(stream)

    assert len(parcels) == 1
    assert stream.counts.parcels_seen == 10
    assert stream.counts.parcels_kept == 1


def test_parcels_in_bbox_with_no_overlap_keeps_nothing_but_still_sees_everything(tmp_path):
    zip_path = _fixture_zip(tmp_path)
    stream = parcels_in(zip_path, FIXTURE_BBOX_ELSEWHERE)
    parcels = list(stream)

    assert parcels == []
    assert stream.counts.parcels_seen == 10
    assert stream.counts.parcels_kept == 0


def test_parcels_in_carries_exterior_ring_first_then_interior(tmp_path):
    zip_path = _fixture_zip(tmp_path)
    parcels = list(parcels_in(zip_path, FIXTURE_BBOX_ALL))

    with_interior = [rings for rings in parcels if len(rings) > 1]
    assert len(with_interior) == 1

    exterior, interior = with_interior[0]
    assert all(isinstance(point, tuple) and len(point) == 2 for point in exterior)
    assert all(isinstance(point, tuple) and len(point) == 2 for point in interior)

    # A real courtyard: the interior ring's bbox sits inside the
    # exterior ring's own bbox.
    ext_e_min, ext_n_min, ext_e_max, ext_n_max = _ring_bbox_for_test(exterior)
    int_e_min, int_n_min, int_e_max, int_n_max = _ring_bbox_for_test(interior)
    assert ext_e_min <= int_e_min and ext_e_max >= int_e_max
    assert ext_n_min <= int_n_min and ext_n_max >= int_n_max


def test_parcels_in_fixture_has_a_pair_of_parcels_sharing_an_edge(tmp_path):
    # Task 3's own dedup test needs this fixture property; pinned here
    # too so a future regeneration cannot silently drop it. frozenset
    # over the two endpoints makes the edge direction-independent, which
    # is exactly how the two parcels actually share it (one ring walks
    # it one way, the neighbour's ring walks it the other).
    zip_path = _fixture_zip(tmp_path)
    parcels = list(parcels_in(zip_path, FIXTURE_BBOX_ALL))

    edge_sets = []
    for rings in parcels:
        exterior = rings[0]
        edges = {
            frozenset((exterior[i], exterior[i + 1])) for i in range(len(exterior) - 1)
        }
        edge_sets.append(edges)

    shared_pairs = [
        (i, j)
        for i in range(len(edge_sets))
        for j in range(i + 1, len(edge_sets))
        if edge_sets[i] & edge_sets[j]
    ]
    assert shared_pairs, "expected at least one pair of parcels sharing an edge"


def _many_members_gml(count: int) -> bytes:
    """`count` tiny, valid members, for the memory-guard test below. Not
    built from the real committed fixture (10 members is too small to
    exceed `ET.iterparse`'s own internal 16 KB read chunk even once; see
    ParcelStream's own docstring for why that matters), and not
    committed itself: built fresh, in memory, by the one test that needs
    a file bigger than a single read chunk.
    """
    members = []
    for i in range(count):
        x = float(i)
        members.append(
            "<wfs:member><LR:PREDEFINED>"
            '<LR:GEOMETRY><gml:Polygon srsName="urn:ogc:def:crs:EPSG::27700" '
            'srsDimension="2"><gml:exterior><gml:LinearRing><gml:posList>'
            f"{x} {x} {x + 1} {x} {x + 1} {x + 1} {x} {x + 1} {x} {x}"
            "</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>"
            "</LR:GEOMETRY></LR:PREDEFINED></wfs:member>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<wfs:FeatureCollection {_GML_NS_DECLARATIONS} "
        f'numberMatched="{count}" numberReturned="{count}" '
        'timeStamp="2026-08-02T03:47:41.090Z">'
        + "".join(members)
        + "</wfs:FeatureCollection>"
    ).encode("utf-8")


def test_parcels_in_peak_live_members_does_not_grow_with_file_size(tmp_path):
    # The memory guard the plan's own global constraints ask for: peak
    # element count must not grow with input size. It is NOT pinned at a
    # literal 1 (see ParcelStream's own docstring for why iterparse's
    # fixed 16 KB read chunks make that the wrong target): instead, two
    # files with the same average member size but very different total
    # counts must show close to the SAME peak, because that peak is
    # bounded by the read chunk, not by how many parcels follow it. If
    # elem.clear() (and the root-detach beside it) were ever removed,
    # the large file's peak would instead equal its own parcels_seen,
    # 25x the small file's rather than close to it.
    small_zip = _write_synthetic_zip(tmp_path / "small.zip", _many_members_gml(200))
    large_zip = _write_synthetic_zip(tmp_path / "large.zip", _many_members_gml(5000))

    small_stream = parcels_in(small_zip, WORLD_BBOX)
    list(small_stream)
    large_stream = parcels_in(large_zip, WORLD_BBOX)
    list(large_stream)

    assert small_stream.counts.parcels_seen == 200
    assert large_stream.counts.parcels_seen == 5000
    assert large_stream.peak_live_members <= small_stream.peak_live_members * 2
    assert large_stream.peak_live_members < 200


# --------------------------------------------------------------------------
# parcels_in: malformed parcels, and the whole-file srsName refusal.
# --------------------------------------------------------------------------


def test_parcels_in_skips_and_counts_an_odd_length_poslist(tmp_path):
    zip_path = _write_synthetic_zip(
        tmp_path / "odd.zip", _one_member_gml(pos_list="0 0 10 0 10")
    )
    stream = parcels_in(zip_path, WORLD_BBOX)

    assert list(stream) == []
    assert stream.counts == ParseCounts(
        parcels_seen=1, parcels_kept=0, parcels_skipped_malformed=1
    )


def test_parcels_in_skips_and_counts_a_non_numeric_token(tmp_path):
    zip_path = _write_synthetic_zip(
        tmp_path / "nonnumeric.zip",
        _one_member_gml(pos_list="0 0 10 0 abc 10 0 10 0 0"),
    )
    stream = parcels_in(zip_path, WORLD_BBOX)

    assert list(stream) == []
    assert stream.counts.parcels_skipped_malformed == 1


def test_parcels_in_skips_and_counts_a_missing_exterior_ring(tmp_path):
    zip_path = _write_synthetic_zip(
        tmp_path / "no_exterior.zip", _one_member_gml(include_exterior=False)
    )
    stream = parcels_in(zip_path, WORLD_BBOX)

    assert list(stream) == []
    assert stream.counts.parcels_skipped_malformed == 1


def test_parcels_in_a_malformed_parcel_is_skipped_not_raised(tmp_path):
    # "never raised mid-stream" (the brief's own words), pinned directly:
    # a malformed posList must not surface as an exception at all.
    zip_path = _write_synthetic_zip(
        tmp_path / "odd.zip", _one_member_gml(pos_list="0 0 10 0 10")
    )
    list(parcels_in(zip_path, WORLD_BBOX))  # does not raise


def test_parcels_in_interior_ring_malformed_skips_the_whole_parcel(tmp_path):
    zip_path = _write_synthetic_zip(
        tmp_path / "bad_interior.zip",
        _one_member_gml(interior_pos_list="1 1 2 1 3"),  # odd count
    )
    stream = parcels_in(zip_path, WORLD_BBOX)

    assert list(stream) == []
    assert stream.counts.parcels_skipped_malformed == 1


def test_parcels_in_refuses_a_wrong_srs_name_naming_the_value(tmp_path):
    zip_path = _write_synthetic_zip(
        tmp_path / "wrong_srs.zip",
        _one_member_gml(srs_name="urn:ogc:def:crs:EPSG::4326"),
    )
    stream = parcels_in(zip_path, WORLD_BBOX)

    with pytest.raises(InspireError) as excinfo:
        list(stream)
    assert "urn:ogc:def:crs:EPSG::4326" in str(excinfo.value)


def test_parcels_in_raises_when_the_zip_has_no_gml_member(tmp_path):
    zip_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("nothing.txt", b"not a gml file")

    with pytest.raises(InspireError):
        list(parcels_in(zip_path, WORLD_BBOX))


def test_parcels_in_raises_when_the_path_is_not_a_zip_file_at_all(tmp_path):
    not_a_zip = tmp_path / "not_a_zip.zip"
    not_a_zip.write_bytes(b"this is plainly not a zip file")

    with pytest.raises(InspireError):
        list(parcels_in(not_a_zip, WORLD_BBOX))


# --------------------------------------------------------------------------
# fetch_authority_zip: fakes shaped like requests' own Session/Response.
# --------------------------------------------------------------------------


class _FakeResponse:
    """Shaped like the two things fetch_authority_zip actually calls on a
    `requests.Response`: `.status_code` and `.iter_content`, plus the
    context-manager protocol `with session.get(...) as response:` needs.
    """

    def __init__(self, status_code: int, content: bytes = b"", headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._content = content

    def iter_content(self, chunk_size: int):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _CookieCarryingSession:
    """Stands in for a real `requests.Session`: the plan's own probe
    found that the live service sets a cookie and 302s to itself on a
    cookieless first hit, and that a Session resolves this invisibly,
    inside its own single `.get()` call, because it carries the cookie
    across the redirect automatically. This fake represents exactly that
    resolved, terminal response: one call, one 200, the zip bytes.
    """

    def __init__(self, zip_bytes: bytes):
        self.zip_bytes = zip_bytes
        self.calls: list[str] = []

    def get(self, url, stream=True, timeout=None):
        self.calls.append(url)
        return _FakeResponse(200, self.zip_bytes)


class _CookielessSession:
    """Stands in for a client with no cookie jar at all (a bare `urllib`
    opener with no `HTTPCookieProcessor`): the live service's own
    302-to-itself, handed straight back, because nothing here ever
    carries a cookie to a retry. This is the shape fetch_authority_zip
    must refuse rather than accept as though it were the zip.
    """

    def __init__(self):
        self.calls: list[str] = []

    def get(self, url, stream=True, timeout=None):
        self.calls.append(url)
        return _FakeResponse(302, b"", headers={"Location": url})


class _RaisesOnAnyCall:
    """Proves a code path never touches the network at all."""

    def get(self, *args, **kwargs):
        raise AssertionError("fetch_authority_zip must not call session.get here")


# --------------------------------------------------------------------------
# fetch_authority_zip: name validation, cache hit, download, sweep.
# --------------------------------------------------------------------------


def test_fetch_authority_zip_refuses_a_bad_name_before_any_network(tmp_path):
    with pytest.raises(InspireError):
        fetch_authority_zip("not a safe name!", _RaisesOnAnyCall(), cache_dir=tmp_path)


def test_fetch_authority_zip_cache_hit_never_touches_the_network(tmp_path):
    stamp = datetime.datetime.now().strftime("%Y-%m")
    cached = tmp_path / f"Vale_of_Glamorgan_Council_{stamp}.zip"
    cached.write_bytes(b"already cached")

    result = fetch_authority_zip(
        "Vale_of_Glamorgan_Council", _RaisesOnAnyCall(), cache_dir=tmp_path
    )

    assert result == cached
    assert result.read_bytes() == b"already cached"


def test_fetch_authority_zip_downloads_through_a_cookie_carrying_session(tmp_path):
    session = _CookieCarryingSession(zip_bytes=b"zip contents here")

    result = fetch_authority_zip("Vale_of_Glamorgan_Council", session, cache_dir=tmp_path)

    stamp = datetime.datetime.now().strftime("%Y-%m")
    assert result == tmp_path / f"Vale_of_Glamorgan_Council_{stamp}.zip"
    assert result.read_bytes() == b"zip contents here"
    assert session.calls == [
        "https://use-land-property-data.service.gov.uk/datasets/inspire/download/"
        "Vale_of_Glamorgan_Council.zip"
    ]


def test_fetch_authority_zip_refuses_a_cookieless_sessions_redirect(tmp_path):
    session = _CookielessSession()

    with pytest.raises(InspireError):
        fetch_authority_zip("Vale_of_Glamorgan_Council", session, cache_dir=tmp_path)

    # A failed attempt must not leave a redirect's own small body cached
    # as though it were the zip, nor a stray temp file behind.
    assert list(tmp_path.glob("*.zip")) == []
    assert list(tmp_path.glob("*.part")) == []


def test_fetch_authority_zip_wraps_a_transport_failure_without_a_url(tmp_path):
    class _BrokenSession:
        def get(self, *args, **kwargs):
            raise requests.exceptions.ConnectionError(
                "simulated failure naming https://example.invalid/should-not-leak"
            )

    with pytest.raises(InspireError) as excinfo:
        fetch_authority_zip("Vale_of_Glamorgan_Council", _BrokenSession(), cache_dir=tmp_path)

    assert "example.invalid" not in str(excinfo.value)


def test_fetch_authority_zip_sweeps_stale_months_after_a_successful_download(tmp_path):
    stale = tmp_path / "Vale_of_Glamorgan_Council_2020-01.zip"
    stale.write_bytes(b"old")
    session = _CookieCarryingSession(zip_bytes=b"new zip bytes")

    result = fetch_authority_zip("Vale_of_Glamorgan_Council", session, cache_dir=tmp_path)

    assert result.read_bytes() == b"new zip bytes"
    assert not stale.exists()


def test_fetch_authority_zip_never_sweeps_on_a_failed_download(tmp_path):
    stale = tmp_path / "Vale_of_Glamorgan_Council_2020-01.zip"
    stale.write_bytes(b"old")
    session = _CookielessSession()

    with pytest.raises(InspireError):
        fetch_authority_zip("Vale_of_Glamorgan_Council", session, cache_dir=tmp_path)

    assert stale.exists()
    assert stale.read_bytes() == b"old"


def test_fetch_authority_zip_only_sweeps_the_same_authoritys_own_files(tmp_path):
    other_authority = tmp_path / "Cardiff_Council_2020-01.zip"
    other_authority.write_bytes(b"unrelated")
    session = _CookieCarryingSession(zip_bytes=b"vale bytes")

    fetch_authority_zip("Vale_of_Glamorgan_Council", session, cache_dir=tmp_path)

    assert other_authority.exists()


def test_fetch_authority_zip_non_200_sets_status_code_structurally(tmp_path):
    # Task 4's InspireSource.fetch() classifies this failure through
    # classify_status_failure, which needs the raw int, not a regex over
    # this exception's own English sentence (see InspireError's own
    # docstring, and cog.py's CogError.status_code precedent).
    class _RateLimitedSession:
        def get(self, url, stream=True, timeout=None):
            return _FakeResponse(429, b"")

    with pytest.raises(InspireError) as excinfo:
        fetch_authority_zip("Vale_of_Glamorgan_Council", _RateLimitedSession(), cache_dir=tmp_path)

    assert excinfo.value.status_code == 429


def test_fetch_authority_zip_transport_failure_leaves_status_code_unset(tmp_path):
    class _BrokenSession:
        def get(self, *args, **kwargs):
            raise requests.exceptions.ConnectionError("simulated")

    with pytest.raises(InspireError) as excinfo:
        fetch_authority_zip("Vale_of_Glamorgan_Council", _BrokenSession(), cache_dir=tmp_path)

    assert getattr(excinfo.value, "status_code", None) is None
    assert isinstance(excinfo.value.__cause__, requests.exceptions.ConnectionError)


def test_fetch_authority_zip_default_cache_dir_is_under_the_mapgen_home(monkeypatch, tmp_path):
    fake_config_path = tmp_path / ".mapgen" / "config.json"
    monkeypatch.setattr(inspire_module, "CONFIG_PATH", fake_config_path)
    session = _CookieCarryingSession(zip_bytes=b"data")

    result = fetch_authority_zip("Vale_of_Glamorgan_Council", session)

    assert result.parent == tmp_path / ".mapgen" / "inspire"


# --------------------------------------------------------------------------
# The one live test: the real handshake, the real Vale of Glamorgan zip,
# a real Llantwit Major-sized BNG bbox.
# --------------------------------------------------------------------------


@pytest.mark.live
def test_live_fetch_and_parse_vale_of_glamorgan_over_llantwit_major(tmp_path):
    session = requests.Session()
    zip_path = fetch_authority_zip("Vale_of_Glamorgan_Council", session, cache_dir=tmp_path)
    assert zip_path.exists()

    # Roughly Llantwit Major, BNG metres (the plan's own brief).
    bbox_bng = (296_000.0, 165_000.0, 300_000.0, 169_000.0)
    stream = parcels_in(zip_path, bbox_bng)
    parcels = list(stream)

    assert len(parcels) > 0
    assert stream.counts.parcels_kept == len(parcels)
    assert stream.counts.parcels_seen >= stream.counts.parcels_kept
    assert 2020 <= stream.timestamp_year <= 2030


# ==========================================================================
# Task 4: InspireSource, the LayerSource wiring and the boundaries GeoJSON.
# ==========================================================================
#
# fetch()/merge() unit tests use a ZERO-SHIFT Ostn15Grid throughout (mirrors
# tests/test_heights.py's own `_zero_shift_grid`), never the real
# tests/fixtures/ostn15 slice: that slice covers only three named 3x3 km
# blocks (TP06/TP03/TP40; see make_fixture.py), and this task needs to
# place synthetic parcels at coordinates of its own choosing, decoupled
# from any real station's tiny coverage window. A zero-shift grid makes
# to_bng/from_bng degenerate to the bare tm_forward/tm_inverse pair
# (exact inverses of each other), which is what lets a test pick a BNG
# rectangle first and derive the matching WGS84 bbox from it, or the other
# way round, with no risk of OutsideOstn15Error. `authorities_for` itself
# is monkeypatched to a fixed list for every fetch()/merge() test below,
# for the same reason: which real authorities a bbox resolves to is
# already Task 1's own suite's job, not this one's, and it would otherwise
# force every synthetic bbox here to also land inside a real committed
# authority's own footprint.


def _zero_shift_grid() -> Ostn15Grid:
    from array import array

    shifts = array("f", [0.0]) * (_NODE_COUNT * 2)
    return Ostn15Grid(shifts)


def _seed_ostn15_cache(cache_dir: Path, grid: Ostn15Grid) -> None:
    from mapgen import bng as bng_module

    cache_dir.mkdir(parents=True, exist_ok=True)
    bng_module._write_cache(cache_dir / bng_module._CACHE_FILENAME, grid)


def _bbox_for_bng_rectangle(
    e_min: float, n_min: float, e_max: float, n_max: float
) -> BBox:
    lat1, lon1 = tm_inverse(e_min, n_min)
    lat2, lon2 = tm_inverse(e_max, n_max)
    return BBox(
        west=min(lon1, lon2), south=min(lat1, lat2),
        east=max(lon1, lon2), north=max(lat1, lat2),
    )


def _zip_bytes(gml_bytes: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(GML_MEMBER_NAME, gml_bytes)
    return buffer.getvalue()


def _tiles(*tile_ids: str) -> list[Tile]:
    bbox = BBox(west=-3.3, south=51.4, east=-3.29, north=51.41)
    return [
        Tile(tile_id=tid, row=0, col=index, core_bbox=bbox, query_bbox=bbox)
        for index, tid in enumerate(tile_ids)
    ]


def _write_parcels_jsonl(path: Path, parcels) -> None:
    text = "".join(json.dumps(rings) + "\n" for rings in parcels)
    path.write_text(text, encoding="utf-8")


def _write_meta_json(path: Path, authorities: list[dict], year: int) -> None:
    path.write_text(
        json.dumps({"authorities": authorities, "year": year}), encoding="utf-8"
    )


class _ProgressLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class _AuthorityZipSession:
    """Serves `fetch_authority_zip`'s own `session.get(url, stream=True,
    timeout=...)` call, keyed by exact URL, refusing anything else
    outright: a session that answered any URL would hide a typo in
    INSPIRE_DOWNLOAD_URL_TEMPLATE, since the wrong URL would still get
    real bytes back and every test would still pass.
    """

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = dict(blobs)
        self.calls: list[str] = []

    def get(self, url, stream=True, timeout=None):
        self.calls.append(url)
        if url not in self.blobs:
            raise AssertionError(f"no fake authority zip registered for {url!r}")
        return _FakeResponse(200, self.blobs[url])


class _CancelAfterFirstAuthoritySession(_AuthorityZipSession):
    """Cancels `token` as a side effect of serving `trigger_url`, so a
    test can prove the cancel checkpoint BETWEEN authorities fires
    without a second authority's zip ever being requested.
    """

    def __init__(self, blobs: dict[str, bytes], token: CancelToken, trigger_url: str) -> None:
        super().__init__(blobs)
        self._token = token
        self._trigger_url = trigger_url

    def get(self, url, stream=True, timeout=None):
        response = super().get(url, stream=stream, timeout=timeout)
        if url == self._trigger_url:
            self._token.cancel()
        return response


class _FailsOnUrlSession(_AuthorityZipSession):
    """Answers `target_url` with either a fixed status or a raised
    transport exception, persistently (fetch_authority_zip has no retry
    of its own to absorb a single bad answer, unlike cog.py's
    HttpByteSource, but persistent for the same defensive reason every
    other fake session in this project's suites is), and behaves like an
    ordinary `_AuthorityZipSession` for anything else.
    """

    def __init__(
        self,
        blobs: dict[str, bytes],
        target_url: str,
        status_code: int | None = None,
        transport_exc: BaseException | None = None,
    ) -> None:
        super().__init__(blobs)
        self._target_url = target_url
        self._status_code = status_code
        self._transport_exc = transport_exc

    def get(self, url, stream=True, timeout=None):
        self.calls.append(url)
        if url == self._target_url:
            if self._transport_exc is not None:
                raise self._transport_exc
            return _FakeResponse(self._status_code, b"")
        if url not in self.blobs:
            raise AssertionError(f"no fake authority zip registered for {url!r}")
        return _FakeResponse(200, self.blobs[url])


class _RefusesOstn15Session:
    """A session whose `.get()` always raises a real
    `requests.RequestException`, so `bng._download_and_parse` wraps it
    into a `BngError` the way the real library does, rather than an
    `AssertionError` that would never reach InspireSource's own except
    clauses at all.
    """

    def get(self, *args, **kwargs):
        raise requests.exceptions.ConnectionError("simulated: no OSTN15 network")


# A 400 x 400 m square in BNG metres, well inside the valid 701 x 1251 km
# OSTN15 grid rectangle, used as this section's one fixed "authority 1"
# parcel location.
_PARCEL_RING = (
    "299900 179900 300000 179900 300000 180000 299900 180000 299900 179900"
)
_PARCEL_BBOX = _bbox_for_bng_rectangle(299_700.0, 179_700.0, 300_300.0, 180_300.0)


def _build_gml(
    pos_list: str,
    time_stamp: str = "2026-08-02T03:47:41.090Z",
    srs_name: str = "urn:ogc:def:crs:EPSG::27700",
) -> bytes:
    ns = (
        'xmlns:wfs="http://www.opengis.net/wfs/2.0" '
        'xmlns:gml="http://www.opengis.net/gml/3.2" '
        'xmlns:LR="www.landregistry.gov.uk"'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<wfs:FeatureCollection {ns} "
        f'numberMatched="1" numberReturned="1" timeStamp="{time_stamp}">'
        "<wfs:member><LR:PREDEFINED>"
        f'<LR:GEOMETRY><gml:Polygon srsName="{srs_name}" '
        'srsDimension="2"><gml:exterior><gml:LinearRing>'
        f"<gml:posList>{pos_list}</gml:posList>"
        "</gml:LinearRing></gml:exterior></gml:Polygon></LR:GEOMETRY>"
        "</LR:PREDEFINED></wfs:member></wfs:FeatureCollection>"
    ).encode("utf-8")


# --------------------------------------------------------------------------
# Class attributes and registration.
# --------------------------------------------------------------------------


def test_declares_the_briefs_exact_class_attributes():
    source = InspireSource()
    assert source.id == "inspire"
    assert source.display_name == "Property boundaries (INSPIRE)"
    assert source.licence == "Open Government Licence v3.0"
    assert source.attribution == (
        "This information is subject to Crown copyright and database rights "
        "[year] and is reproduced with the permission of HM Land Registry. "
        "The polygons (including the associated geometry, namely x, y "
        "co-ordinates) are subject to Crown copyright and database rights "
        "[year] Ordnance Survey AC0000851063."
    )
    assert source.requires_api_key is False
    assert source.conditions_url == (
        "https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions"
    )
    assert not hasattr(source, "readiness_problem")


def test_register_default_sources_registers_inspire_once_and_honours_the_type_check():
    register_default_sources()
    first = get_source("inspire")
    assert isinstance(first, InspireSource)

    register_default_sources()
    assert get_source("inspire") is first


# --------------------------------------------------------------------------
# estimate(): no network, ever; zero authorities is honestly zero.
# --------------------------------------------------------------------------


class _RefusesToConnect:
    def get(self, *args, **kwargs):
        raise AssertionError("estimate() must not touch the network")


def test_estimate_zero_authorities_is_zero_bytes_and_zero_seconds():
    source = InspireSource(session=_RefusesToConnect())
    scotland = BBox(west=-3.30, south=55.90, east=-3.10, north=56.00)

    estimate = source.estimate(scotland, [])

    assert estimate.bytes_estimate == 0
    assert estimate.seconds_estimate == 0.0


def test_estimate_one_authority_touches_no_network_and_floors_at_seconds_floor():
    source = InspireSource(session=_RefusesToConnect())
    llantwit = BBox(west=-3.495, south=51.395, east=-3.475, north=51.410)

    estimate = source.estimate(llantwit, [])

    assert estimate.bytes_estimate == BYTES_PER_AUTHORITY
    assert estimate.seconds_estimate >= SECONDS_FLOOR


def test_estimate_scales_linearly_with_authority_count():
    source = InspireSource(session=_RefusesToConnect())
    llantwit = BBox(west=-3.495, south=51.395, east=-3.475, north=51.410)
    border = BBox(west=-3.263442, south=51.438073, east=-3.243442, north=51.458073)

    one = source.estimate(llantwit, [])
    two = source.estimate(border, [])

    assert len(authorities_for(llantwit)) == 1
    assert len(authorities_for(border)) == 2
    assert two.bytes_estimate == 2 * one.bytes_estimate


# --------------------------------------------------------------------------
# Task 7: the constants refit from every measurement now on record (the
# plan's own live probe, plus Tasks 2 and 4's own live tests; see both
# reports and inspire.py's own "Measured constants" comment for the full
# arithmetic). Both tests below assert a real MARGIN over the largest
# figure actually measured, not mere equality with it: a constant pinned
# exactly to one sample carries no protection against a bigger authority
# or a slower day than the one it was measured on, which is what "an
# estimate that under-reads is worse than one that over-reads" means in
# practice. Both are genuine RED against the pre-refit values (15,000,000
# and 7.25): 15,000,000 clears Bridgend's 13,128,716 alone by 14.2%, but
# only 9.6% over Vale of Glamorgan's own 13,689,747, under the 10% floor
# these tests hold every future authority sample to; 7.25 IS Task 2's own
# measurement, with no margin over itself at all.
# --------------------------------------------------------------------------


def test_bytes_per_authority_carries_a_margin_over_both_measured_authorities():
    # Bridgend_County_Borough_Council.zip: 13,128,716 bytes (the plan's own
    # live probe, 2026-08-06). Vale_of_Glamorgan_Council.zip: 13,689,747
    # bytes, measured twice on different days (Task 2's and Task 4's own
    # live tests) and unchanged between them, the larger and the more
    # trustworthy of the two samples since it is independently confirmed
    # stable rather than a single read.
    largest_measured_bytes = 13_689_747
    assert BYTES_PER_AUTHORITY > largest_measured_bytes
    assert BYTES_PER_AUTHORITY >= round(largest_measured_bytes * 1.1)


def test_seconds_floor_carries_a_margin_over_every_measured_wall_time():
    # Task 2's live test measured 7.25 s (one pytest invocation's whole
    # wall time, collection overhead included) for one authority's cold
    # download plus parse. Task 4's own standalone script measured only
    # 3.734 s for fetch()+merge() together, over the SAME zip, on a
    # different day: a swing of nearly 2x for what is meant to be the same
    # piece of work, which is real network variance to this one service
    # rather than noise a bigger sample would average away. The floor must
    # clear the LARGER of the two real measurements with margin, since a
    # slower day than either one already seen is the case this constant
    # exists to protect against.
    largest_measured_seconds = 7.25
    assert SECONDS_FLOOR > largest_measured_seconds
    assert SECONDS_FLOOR >= round(largest_measured_seconds * 1.1, 2)


# --------------------------------------------------------------------------
# fetch(): zero authorities, skip-on-resume, cancel checkpoints.
# --------------------------------------------------------------------------


def test_fetch_zero_authorities_refuses_before_any_network(tmp_path):
    source = InspireSource(session=_RefusesToConnect())
    scotland = BBox(west=-3.30, south=55.90, east=-3.10, north=56.00)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(InspireError) as excinfo:
        source.fetch(scotland, _tiles("t1", "t2"), work_dir, NullProgress())

    assert str(excinfo.value) == OUTSIDE_ENGLAND_AND_WALES_MESSAGE
    assert len(source.tile_failures) == 2
    for failure in source.tile_failures:
        assert failure.kind == FAILURE_NO_OUTPUT
        assert failure.reason == OUTSIDE_ENGLAND_AND_WALES_MESSAGE
    assert not list(work_dir.iterdir())


def test_fetch_skips_when_both_work_files_already_exist(tmp_path):
    # parcels.jsonl's own line count (1) is consistent with meta's own
    # claimed parcels_kept total (1): the ordinary, non-empty resume case.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / PARCELS_WORK_NAME).write_text('[[[1.0, 2.0]]]\n', encoding="utf-8")
    _write_meta_json(
        work_dir / META_WORK_NAME,
        authorities=[
            {"name": "Test_Authority", "parcels_seen": 1, "parcels_kept": 1, "parcels_skipped_malformed": 0, "timestamp_year": 2026}
        ],
        year=2026,
    )

    source = InspireSource(session=_RefusesToConnect())
    progress = _ProgressLog()

    paths = source.fetch(BBox(west=-3.3, south=51.4, east=-3.29, north=51.41), _tiles("t1"), work_dir, progress)

    assert sorted(p.name for p in paths) == sorted([PARCELS_WORK_NAME, META_WORK_NAME])
    assert ("tile_skipped", {"source": "inspire", "tile_id": "whole-area"}) in progress.events
    assert source.tile_failures == []


def test_fetch_skips_a_completed_zero_parcel_fetch(tmp_path):
    # The review finding this fix closes: every authority found had zero
    # kept parcels, a legitimate, complete result that writes a 0-byte
    # parcels.jsonl by construction. meta's own claimed total (0) agrees
    # with that, so this must skip, with a session that raises on any use
    # proving no network is touched at all.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / PARCELS_WORK_NAME).write_text("", encoding="utf-8")
    _write_meta_json(
        work_dir / META_WORK_NAME,
        authorities=[
            {"name": "Test_Authority", "parcels_seen": 5, "parcels_kept": 0, "parcels_skipped_malformed": 0, "timestamp_year": 2026}
        ],
        year=2026,
    )

    source = InspireSource(session=_RefusesToConnect())
    progress = _ProgressLog()

    paths = source.fetch(BBox(west=-3.3, south=51.4, east=-3.29, north=51.41), _tiles("t1"), work_dir, progress)

    assert sorted(p.name for p in paths) == sorted([PARCELS_WORK_NAME, META_WORK_NAME])
    assert ("tile_skipped", {"source": "inspire", "tile_id": "whole-area"}) in progress.events
    assert source.tile_failures == []


def test_fetch_does_not_skip_when_parcels_jsonl_is_empty_but_meta_claims_kept_parcels(tmp_path, monkeypatch):
    # The truncation case the old non-empty check was actually guarding,
    # still guarded: a 0-byte parcels.jsonl inconsistent with meta's own
    # claimed kept=3 must not skip.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / PARCELS_WORK_NAME).write_text("", encoding="utf-8")
    _write_meta_json(
        work_dir / META_WORK_NAME,
        authorities=[
            {"name": "Test_Authority", "parcels_seen": 5, "parcels_kept": 3, "parcels_skipped_malformed": 0, "timestamp_year": 2026}
        ],
        year=2026,
    )

    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])
    source = InspireSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path / "no-cache")

    with pytest.raises(AssertionError):
        # Falls through past the (refused) skip to ensure_ostn15, which
        # _RefusesToConnect refuses.
        source.fetch(BBox(west=-3.3, south=51.4, east=-3.29, north=51.41), _tiles("t1"), work_dir, NullProgress())


def test_fetch_does_not_skip_when_parcels_jsonl_has_content_but_meta_claims_zero_kept(tmp_path, monkeypatch):
    # The same inconsistency, the other way round: a non-empty
    # parcels.jsonl beside a meta claiming kept=0 must not skip either.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / PARCELS_WORK_NAME).write_text('[[[1.0, 2.0]]]\n', encoding="utf-8")
    _write_meta_json(
        work_dir / META_WORK_NAME,
        authorities=[
            {"name": "Test_Authority", "parcels_seen": 5, "parcels_kept": 0, "parcels_skipped_malformed": 0, "timestamp_year": 2026}
        ],
        year=2026,
    )

    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])
    source = InspireSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path / "no-cache")

    with pytest.raises(AssertionError):
        source.fetch(BBox(west=-3.3, south=51.4, east=-3.29, north=51.41), _tiles("t1"), work_dir, NullProgress())


def test_fetch_does_not_skip_when_meta_json_is_corrupt(tmp_path, monkeypatch):
    # Non-empty parcels.jsonl deliberately: the OLD "both files non-empty"
    # check would have skipped here (meta.json is non-empty bytes, just
    # not valid JSON), which is exactly the review finding. meta.json
    # cannot be trusted to say what parcels.jsonl actually holds, so this
    # must not skip regardless of parcels.jsonl's own size.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / PARCELS_WORK_NAME).write_text('[[[1.0, 2.0]]]\n', encoding="utf-8")
    (work_dir / META_WORK_NAME).write_text("not json", encoding="utf-8")

    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])
    source = InspireSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path / "no-cache")

    with pytest.raises(AssertionError):
        source.fetch(BBox(west=-3.3, south=51.4, east=-3.29, north=51.41), _tiles("t1"), work_dir, NullProgress())


def test_fetch_does_not_skip_when_meta_json_is_missing_the_expected_keys(tmp_path, monkeypatch):
    # Same discriminating shape as the corrupt-JSON test above: valid
    # JSON, non-empty, but missing the "authorities"/"year" keys this
    # class's own fetch() always writes and merge() always reads.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / PARCELS_WORK_NAME).write_text('[[[1.0, 2.0]]]\n', encoding="utf-8")
    (work_dir / META_WORK_NAME).write_text('{"foo": "bar"}', encoding="utf-8")

    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])
    source = InspireSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path / "no-cache")

    with pytest.raises(AssertionError):
        source.fetch(BBox(west=-3.3, south=51.4, east=-3.29, north=51.41), _tiles("t1"), work_dir, NullProgress())


def test_fetch_does_not_skip_when_one_work_file_is_empty(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / PARCELS_WORK_NAME).write_text("real", encoding="utf-8")
    (work_dir / META_WORK_NAME).write_text("", encoding="utf-8")  # empty: not valid JSON at all

    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])
    source = InspireSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path / "no-cache")

    with pytest.raises(AssertionError):
        # Falls through past the skip check to ensure_ostn15, which
        # _RefusesToConnect refuses; proves an unparseable meta.json never
        # skips, regardless of what parcels.jsonl itself holds.
        source.fetch(BBox(west=-3.3, south=51.4, east=-3.29, north=51.41), _tiles("t1"), work_dir, NullProgress())


def test_fetch_stops_before_any_request_when_already_cancelled(tmp_path):
    token = CancelToken()
    token.cancel()
    source = InspireSource(session=_RefusesToConnect())
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Cancelled):
        source.fetch(
            BBox(west=-3.3, south=51.4, east=-3.29, north=51.41),
            _tiles("t1"), work_dir, NullProgress(), cancel=token,
        )


# --------------------------------------------------------------------------
# fetch(): the happy path, one and two authorities, cancel between them.
# --------------------------------------------------------------------------


def test_fetch_happy_path_writes_both_work_files(tmp_path, monkeypatch):
    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())
    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])

    url = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Test_Authority")
    session = _AuthorityZipSession({url: _zip_bytes(_build_gml(_PARCEL_RING))})
    source = InspireSource(session=session, ostn15_cache_dir=cache_dir, inspire_cache_dir=tmp_path / "zip-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    progress = _ProgressLog()

    paths = source.fetch(_PARCEL_BBOX, _tiles("t1"), work_dir, progress)

    assert sorted(p.name for p in paths) == sorted([PARCELS_WORK_NAME, META_WORK_NAME])
    assert source.tile_failures == []
    assert ("tile_done", {"source": "inspire", "tile_id": "whole-area"}) in progress.events

    parcels_lines = (work_dir / PARCELS_WORK_NAME).read_text(encoding="utf-8").splitlines()
    assert len(parcels_lines) == 1
    rings = json.loads(parcels_lines[0])
    assert len(rings) == 1  # exterior only, no interior

    meta = json.loads((work_dir / META_WORK_NAME).read_text(encoding="utf-8"))
    assert meta == {
        "authorities": [
            {
                "name": "Test_Authority",
                "parcels_seen": 1,
                "parcels_kept": 1,
                "parcels_skipped_malformed": 0,
                "timestamp_year": 2026,
            }
        ],
        "year": 2026,
    }


def test_fetch_two_authorities_accumulates_both_into_one_parcels_file(tmp_path, monkeypatch):
    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())
    monkeypatch.setattr(
        inspire_module, "authorities_for", lambda bbox: ["Authority_A", "Authority_B"]
    )

    url_a = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Authority_A")
    url_b = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Authority_B")
    session = _AuthorityZipSession(
        {
            url_a: _zip_bytes(_build_gml(_PARCEL_RING)),
            url_b: _zip_bytes(_build_gml(_PARCEL_RING)),
        }
    )
    source = InspireSource(session=session, ostn15_cache_dir=cache_dir, inspire_cache_dir=tmp_path / "zip-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    paths = source.fetch(_PARCEL_BBOX, _tiles("t1"), work_dir, NullProgress())

    parcels_lines = (work_dir / PARCELS_WORK_NAME).read_text(encoding="utf-8").splitlines()
    assert len(parcels_lines) == 2

    meta = json.loads((work_dir / META_WORK_NAME).read_text(encoding="utf-8"))
    assert [entry["name"] for entry in meta["authorities"]] == ["Authority_A", "Authority_B"]
    assert meta["year"] == 2026


def test_fetch_cancel_between_authorities_leaves_no_failure_records(tmp_path, monkeypatch):
    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())
    monkeypatch.setattr(
        inspire_module, "authorities_for", lambda bbox: ["Authority_A", "Authority_B"]
    )

    url_a = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Authority_A")
    url_b = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Authority_B")
    token = CancelToken()
    session = _CancelAfterFirstAuthoritySession(
        {url_a: _zip_bytes(_build_gml(_PARCEL_RING)), url_b: _zip_bytes(_build_gml(_PARCEL_RING))},
        token,
        url_a,
    )
    source = InspireSource(session=session, ostn15_cache_dir=cache_dir, inspire_cache_dir=tmp_path / "zip-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Cancelled):
        source.fetch(_PARCEL_BBOX, _tiles("t1"), work_dir, NullProgress(), cancel=token)

    assert source.tile_failures == []
    assert url_b not in session.calls
    assert not list(work_dir.iterdir())


# --------------------------------------------------------------------------
# fetch(): per-authority failure classification, and the OSTN15 grid path.
# --------------------------------------------------------------------------


def test_fetch_classifies_a_rate_limited_authority_and_never_leaks_a_url(tmp_path, monkeypatch):
    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())
    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])

    url = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Test_Authority")
    session = _FailsOnUrlSession({}, url, status_code=429)
    source = InspireSource(session=session, ostn15_cache_dir=cache_dir, inspire_cache_dir=tmp_path / "zip-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(InspireError) as excinfo:
        source.fetch(_PARCEL_BBOX, _tiles("t1", "t2"), work_dir, NullProgress())

    assert len(source.tile_failures) == 2
    for failure in source.tile_failures:
        assert failure.kind == FAILURE_RATE_LIMITED
        assert "429" in failure.reason
        assert url not in failure.reason
    assert url not in str(excinfo.value)


def test_fetch_classifies_a_500_as_service_error_and_it_is_retryable(tmp_path, monkeypatch):
    from mapgen.sources.base import RETRYABLE_FAILURE_KINDS

    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())
    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])

    url = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Test_Authority")
    session = _FailsOnUrlSession({}, url, status_code=500)
    source = InspireSource(session=session, ostn15_cache_dir=cache_dir, inspire_cache_dir=tmp_path / "zip-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(InspireError):
        source.fetch(_PARCEL_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    failure = source.tile_failures[0]
    assert failure.kind == FAILURE_SERVICE_ERROR
    assert failure.kind in RETRYABLE_FAILURE_KINDS
    assert "500" in failure.reason
    assert url not in failure.reason


def test_fetch_classifies_a_timeout_and_never_leaks_a_url(tmp_path, monkeypatch):
    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())
    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])

    url = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Test_Authority")
    session = _FailsOnUrlSession(
        {}, url, transport_exc=requests.exceptions.Timeout("simulated stall")
    )
    source = InspireSource(session=session, ostn15_cache_dir=cache_dir, inspire_cache_dir=tmp_path / "zip-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(InspireError) as excinfo:
        source.fetch(_PARCEL_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    assert source.tile_failures[0].kind == FAILURE_TIMEOUT
    assert "did not answer in time" in source.tile_failures[0].reason
    assert url not in source.tile_failures[0].reason
    assert url not in str(excinfo.value)
    # No partial work file survives a failed authority.
    assert not (work_dir / PARCELS_WORK_NAME).exists()


def test_fetch_classifies_an_ostn15_download_failure_and_reraises(tmp_path, monkeypatch):
    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])
    source = InspireSource(session=_RefusesOstn15Session(), ostn15_cache_dir=tmp_path / "no-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(Exception):
        source.fetch(_PARCEL_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    assert source.tile_failures[0].reason  # a plain sentence, not empty
    assert "ordnancesurvey" not in source.tile_failures[0].reason.lower()


def test_fetch_classifies_a_parse_failure_as_could_not_read_not_failed_to_download(tmp_path, monkeypatch):
    # A review Minor: the zip downloaded fine (a real 200, real bytes);
    # what failed is parcels_in's own read of it (a wrong srsName). The
    # sentence's own opening verb must say so, not claim a download that
    # actually succeeded failed.
    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())
    monkeypatch.setattr(inspire_module, "authorities_for", lambda bbox: ["Test_Authority"])

    url = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name="Test_Authority")
    bad_srs_gml = _build_gml(_PARCEL_RING, srs_name="urn:ogc:def:crs:EPSG::4326")
    session = _AuthorityZipSession({url: _zip_bytes(bad_srs_gml)})
    source = InspireSource(session=session, ostn15_cache_dir=cache_dir, inspire_cache_dir=tmp_path / "zip-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(InspireError):
        source.fetch(_PARCEL_BBOX, _tiles("t1"), work_dir, NullProgress())

    assert len(source.tile_failures) == 1
    failure = source.tile_failures[0]
    assert failure.reason.startswith("Could not read the INSPIRE Index Polygons for Test_Authority")
    assert "Failed to download" not in failure.reason
    assert url not in failure.reason


# --------------------------------------------------------------------------
# merge(): the boundaries GeoJSON, dedup, missing inputs, OSTN15 skip.
# --------------------------------------------------------------------------


def test_merge_produces_the_boundaries_geojson_with_exact_properties_and_dedup(tmp_path):
    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())

    # Two squares sharing one edge, the same hand-built case
    # test_boundary_curves.py's own suite uses, in BNG metres.
    square_a = [
        [(300_000.0, 180_000.0), (300_010.0, 180_000.0), (300_010.0, 180_010.0), (300_000.0, 180_010.0)]
    ]
    square_b = [
        [(300_010.0, 180_000.0), (300_020.0, 180_000.0), (300_020.0, 180_010.0), (300_010.0, 180_010.0)]
    ]
    expected_curves = boundary_curves([square_a, square_b])
    assert len(expected_curves) > 0

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _write_parcels_jsonl(work_dir / PARCELS_WORK_NAME, [square_a, square_b])
    _write_meta_json(
        work_dir / META_WORK_NAME,
        authorities=[
            {"name": "Test_Authority", "parcels_seen": 2, "parcels_kept": 2, "parcels_skipped_malformed": 0, "timestamp_year": 2026}
        ],
        year=2026,
    )

    source = InspireSource(ostn15_cache_dir=cache_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-06"

    written = source.merge([work_dir / PARCELS_WORK_NAME, work_dir / META_WORK_NAME], out_dir, stem)

    assert [p.name for p in written] == [f"{stem}_boundaries.geojson"]
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == len(expected_curves)
    for feature, curve in zip(payload["features"], expected_curves):
        assert feature["type"] == "Feature"
        assert feature["properties"] == {
            "source": "HM Land Registry INSPIRE Index Polygons",
            "note": (
                "The extent of the land contained in any registered title "
                "cannot be established from the INSPIRE Index Polygons."
            ),
            "year": 2026,
        }
        assert feature["geometry"]["type"] == "LineString"
        assert len(feature["geometry"]["coordinates"]) == len(curve)
        for lon, lat in feature["geometry"]["coordinates"]:
            assert -180.0 <= lon <= 180.0
            assert -90.0 <= lat <= 90.0


def test_merge_over_the_real_fixture_produces_curves_matching_boundary_curves(tmp_path):
    # "fixture GML via Task 2's own machinery for parse-dependent tests":
    # the real committed 10-parcel fixture, run through the real parcels_in,
    # proving merge() wires real parsed geometry through to real curves,
    # not just the hand-built squares above.
    from mapgen.boundary_curves import boundary_curves_with_counts

    zip_path = _fixture_zip(tmp_path)
    parcels = list(parcels_in(zip_path, FIXTURE_BBOX_ALL))
    result = boundary_curves_with_counts(parcels)
    expected_curves = result.curves
    assert len(expected_curves) > 0
    # Task 2/3's own reports: this fixture's members 1-3 share a 3-edge
    # run of boundary, which collapses at the edge level (curve COUNT is
    # not guaranteed to drop, since chaining can split the outer boundary
    # into more pieces at a junction; see Task 3's own two-squares case).
    assert result.edges_deduped < result.edges_in, (
        "dedup should collapse at least one shared edge across this fixture's "
        "known shared-boundary pair (see Task 2/3's own reports)"
    )

    cache_dir = tmp_path / "ostn15-cache"
    _seed_ostn15_cache(cache_dir, _zero_shift_grid())
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _write_parcels_jsonl(work_dir / PARCELS_WORK_NAME, parcels)
    _write_meta_json(
        work_dir / META_WORK_NAME,
        authorities=[
            {"name": "Vale_of_Glamorgan_Council", "parcels_seen": 10, "parcels_kept": 10, "parcels_skipped_malformed": 0, "timestamp_year": 2026}
        ],
        year=2026,
    )

    source = InspireSource(ostn15_cache_dir=cache_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    written = source.merge([work_dir / PARCELS_WORK_NAME, work_dir / META_WORK_NAME], out_dir, "Fixture_2026-08-06")

    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert len(payload["features"]) == len(expected_curves)


def test_merge_missing_both_work_files_returns_nothing_and_unlinks_stale(tmp_path):
    source = InspireSource()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-06"
    stale = out_dir / f"{stem}_boundaries.geojson"
    stale.write_bytes(b"stale")

    written = source.merge([], out_dir, stem)

    assert written == []
    assert not stale.exists()


def test_merge_missing_meta_json_returns_nothing_and_unlinks_stale(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _write_parcels_jsonl(work_dir / PARCELS_WORK_NAME, [])
    parcels_only = [work_dir / PARCELS_WORK_NAME]

    source = InspireSource()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-06"
    stale = out_dir / f"{stem}_boundaries.geojson"
    stale.write_bytes(b"stale")

    written = source.merge(parcels_only, out_dir, stem)

    assert written == []
    assert not stale.exists()


def test_merge_skips_with_stale_unlink_when_ostn15_grid_is_unavailable(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _write_parcels_jsonl(
        work_dir / PARCELS_WORK_NAME,
        [[[(300_000.0, 180_000.0), (300_010.0, 180_000.0), (300_010.0, 180_010.0), (300_000.0, 180_010.0)]]],
    )
    _write_meta_json(
        work_dir / META_WORK_NAME,
        authorities=[{"name": "Test_Authority", "parcels_seen": 1, "parcels_kept": 1, "parcels_skipped_malformed": 0, "timestamp_year": 2026}],
        year=2026,
    )

    source = InspireSource(session=_RefusesOstn15Session(), ostn15_cache_dir=tmp_path / "no-such-cache")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Barry-Waterfront_2026-08-06"
    stale = out_dir / f"{stem}_boundaries.geojson"
    stale.write_bytes(b"stale")

    written = source.merge([work_dir / PARCELS_WORK_NAME, work_dir / META_WORK_NAME], out_dir, stem)

    assert written == []
    assert not stale.exists()


def test_possible_outputs_is_the_closed_list_of_one_name():
    source = InspireSource()
    stem = "Barry-Waterfront_2026-08-06"
    assert source.possible_outputs(stem) == [f"{stem}_boundaries.geojson"]


# --------------------------------------------------------------------------
# The one live test: a real fetch + merge over Llantwit Major.
# --------------------------------------------------------------------------


@pytest.mark.live
def test_live_fetch_and_merge_over_llantwit_major(tmp_path):
    bbox = BBox(west=-3.495, south=51.395, east=-3.475, north=51.410)
    source = InspireSource(inspire_cache_dir=tmp_path / "inspire-cache")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    tiles = [Tile(tile_id="whole-area", row=0, col=0, core_bbox=bbox, query_bbox=bbox)]

    paths = source.fetch(bbox, tiles, work_dir, NullProgress())
    assert source.tile_failures == []

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stem = "Llantwit_live-test"
    written = source.merge(paths, out_dir, stem)

    assert [p.name for p in written] == source.possible_outputs(stem)
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert len(payload["features"]) > 0
    for feature in payload["features"]:
        assert 2020 <= feature["properties"]["year"] <= 2030
        assert feature["properties"]["source"] == "HM Land Registry INSPIRE Index Polygons"
