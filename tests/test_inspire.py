import datetime
import re
import zipfile
from pathlib import Path

import pytest
import requests

import mapgen.sources.inspire as inspire_module
from mapgen.geo import BBox
from mapgen.sources.inspire import (
    AUTHORITY_INDEX_PATH,
    GML_MEMBER_NAME,
    InspireError,
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
