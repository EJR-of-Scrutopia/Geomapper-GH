import pytest

from mapgen.geo import BBox, Tile
from mapgen.sources.base import NullProgress
from mapgen.sources.osm import (
    OsmDownloadError,
    OsmSource,
    RateLimiter,
    build_overpass_query,
    retry_delay_seconds,
)

OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="51.38" lon="-3.29"/>
</osm>
"""


class FakeResponse:
    def __init__(self, status_code=200, text=OSM_XML, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Replays a queued list of responses and records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)


def _tile(tile_id="r00_c00"):
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    return Tile(tile_id=tile_id, row=0, col=0, core_bbox=bbox, query_bbox=bbox)


def _source(responses, **kwargs):
    kwargs.setdefault("sleeper", lambda _seconds: None)
    kwargs.setdefault("min_interval_seconds", 0.0)
    return OsmSource(session=FakeSession(responses), **kwargs)


def test_declares_its_identity_and_licence():
    source = OsmSource()
    assert source.id == "osm"
    assert source.display_name
    assert "ODbL" in source.licence
    assert "OpenStreetMap" in source.attribution
    assert source.requires_api_key is False


def test_overpass_query_contains_the_bbox_in_south_west_north_east_order():
    query = build_overpass_query(BBox.parse("-3.29,51.38,-3.28,51.39"), 180)
    assert "51.3800000,-3.2900000,51.3900000,-3.2800000" in query
    assert "[out:xml][timeout:180]" in query


def test_fetch_writes_one_file_per_tile(tmp_path):
    source = _source([FakeResponse()])
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert len(paths) == 1
    assert paths[0].name == "r00_c00.osm"
    assert paths[0].read_text(encoding="utf-8") == OSM_XML


def test_fetch_records_the_endpoint_that_served_each_tile(tmp_path):
    source = _source([FakeResponse()])
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())
    assert source.endpoints_used == [source.osm_api_url]


def test_endpoints_used_is_deduplicated_across_tiles(tmp_path):
    source = _source([FakeResponse(), FakeResponse()])
    tiles = [_tile("r00_c00"), _tile("r00_c01")]
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), tiles, tmp_path, NullProgress())
    # Both tiles were served by the same endpoint: one entry, not two.
    assert source.endpoints_used == [source.osm_api_url]


def test_endpoints_used_is_empty_when_every_tile_is_skipped(tmp_path):
    (tmp_path / "r00_c00.osm").write_text(OSM_XML, encoding="utf-8")
    source = _source([])  # no responses queued: a request would raise IndexError
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())
    # Intended, not a bug: a fully resumed fetch contacts nothing new, so there
    # is no endpoint to attribute the on-disk data to. See the class docstring.
    assert source.endpoints_used == []


def test_fetch_emits_progress_per_tile(tmp_path):
    events = []

    class Recorder:
        def emit(self, event, **fields):
            events.append(event)

    source = _source([FakeResponse()])
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, Recorder())
    assert "tile_done" in events


def test_fetch_skips_a_tile_that_is_already_downloaded(tmp_path):
    (tmp_path / "r00_c00.osm").write_text(OSM_XML, encoding="utf-8")
    source = _source([])  # no responses queued: a request would raise IndexError
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert paths[0].exists()
    assert source.session.calls == []


def test_fetch_retries_then_succeeds(tmp_path):
    source = _source([FakeResponse(status_code=504, text="gateway"), FakeResponse()])
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert paths[0].read_text(encoding="utf-8") == OSM_XML
    assert len(source.session.calls) == 2


def test_fetch_raises_after_exhausting_retries(tmp_path):
    source = _source([FakeResponse(status_code=504, text="gateway")] * 4, max_retries=4)
    with pytest.raises(OsmDownloadError, match="r00_c00"):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_failure_message_names_the_osm_api_url_on_the_default_path(tmp_path):
    source = _source([FakeResponse(status_code=504, text="gateway")] * 4, max_retries=4)
    with pytest.raises(OsmDownloadError) as exc_info:
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    # The per-attempt diagnostic is chained on as the cause; it must name the
    # service that was actually contacted, not a rotating Overpass URL that
    # was never called on the default (non-Overpass) path.
    assert source.osm_api_url in str(exc_info.value.__cause__)


def test_failure_message_names_the_overpass_url_when_use_overpass_is_true(tmp_path):
    source = _source(
        [FakeResponse(status_code=504, text="gateway")] * 4,
        max_retries=4,
        use_overpass=True,
    )
    with pytest.raises(OsmDownloadError) as exc_info:
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    # 4 attempts rotating through 2 Overpass URLs: the last attempt (4) used
    # index (4-1) % 2 == 1, so that is the URL the final diagnostic must name.
    assert source.overpass_urls[1] in str(exc_info.value.__cause__)


def test_node_limit_failure_is_reported_immediately_without_retrying(tmp_path):
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")], max_retries=4
    )
    with pytest.raises(OsmDownloadError, match="50000"):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert len(source.session.calls) == 1


def test_overpass_fetch_issues_a_post_with_the_query_body_and_content_type(tmp_path):
    source = _source([FakeResponse()], use_overpass=True)
    tile = _tile()
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [tile], tmp_path, NullProgress())

    assert len(source.session.calls) == 1
    method, url, kwargs = source.session.calls[0]
    assert method == "POST"
    assert url == source.overpass_urls[0]
    expected_query = build_overpass_query(tile.query_bbox, source.timeout_seconds)
    assert kwargs["data"] == expected_query.encode("utf-8")
    assert kwargs["headers"]["Content-Type"] == "text/plain; charset=utf-8"


def test_overpass_retries_rotate_through_the_endpoint_list(tmp_path):
    source = _source(
        [FakeResponse(status_code=504, text="gateway"), FakeResponse()],
        use_overpass=True,
    )
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())

    urls_called = [call[1] for call in source.session.calls]
    assert urls_called == [source.overpass_urls[0], source.overpass_urls[1]]


def test_overpass_fetch_records_the_overpass_endpoint(tmp_path):
    source = _source([FakeResponse()], use_overpass=True)
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())
    assert source.endpoints_used == [source.overpass_urls[0]]


# --- Task 19: category selection, Overpass path only -----------------------


def test_overpass_fetch_sends_a_tag_filtered_query_for_a_narrowed_category_selection(tmp_path):
    source = _source([FakeResponse()], use_overpass=True, categories=["buildings"])
    tile = _tile()
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [tile], tmp_path, NullProgress())
    _method, _url, kwargs = source.session.calls[0]
    assert b'["building"]' in kwargs["data"]
    assert b"node(" not in kwargs["data"], "expected the filtered form, not the unfiltered query"


def test_overpass_fetch_sends_the_original_unfiltered_query_when_categories_is_none(tmp_path):
    source = _source([FakeResponse()], use_overpass=True)
    tile = _tile()
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [tile], tmp_path, NullProgress())
    _method, _url, kwargs = source.session.calls[0]
    assert kwargs["data"] == build_overpass_query(tile.query_bbox, source.timeout_seconds).encode(
        "utf-8"
    )


def test_configure_returns_a_fresh_instance_with_the_given_categories(tmp_path):
    original = OsmSource(use_overpass=True)
    configured = original.configure(["buildings"])
    assert configured is not original
    assert configured.categories == ["buildings"]
    assert original.categories is None, "configure() must not mutate the original instance"


def test_configure_preserves_transport_settings():
    session = object()
    original = OsmSource(
        session=session,
        overpass_urls=["https://example.invalid/api"],
        osm_api_url="https://example.invalid/map",
        max_retries=7,
        timeout_seconds=99,
        min_interval_seconds=3.5,
        use_overpass=True,
    )
    configured = original.configure(["water"])
    assert configured.session is session
    assert configured.overpass_urls == ["https://example.invalid/api"]
    assert configured.osm_api_url == "https://example.invalid/map"
    assert configured.max_retries == 7
    assert configured.timeout_seconds == 99
    assert configured.min_interval_seconds == 3.5
    assert configured.use_overpass is True


def test_filtering_caveat_is_none_on_the_overpass_path_regardless_of_selection():
    source = OsmSource(use_overpass=True)
    assert source.filtering_caveat(["buildings"]) is None


def test_filtering_caveat_is_none_on_the_default_path_when_everything_is_selected():
    source = OsmSource(use_overpass=False)
    from mapgen.categories import ALL_CATEGORY_IDS

    assert source.filtering_caveat(list(ALL_CATEGORY_IDS)) is None
    assert source.filtering_caveat(None) is None


def test_filtering_caveat_warns_on_the_default_path_for_a_narrowed_selection():
    # The default OSM map API has no server-side filtering at all: a
    # narrowed selection is silently ignored unless this is surfaced.
    source = OsmSource(use_overpass=False)
    caveat = source.filtering_caveat(["buildings"])
    assert caveat is not None
    assert "no effect" in caveat


def test_retry_delay_honours_retry_after_in_seconds():
    assert retry_delay_seconds({"Retry-After": "42"}, attempt=1) == pytest.approx(42.0)


def test_retry_delay_ignores_an_unparseable_retry_after():
    assert retry_delay_seconds({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 1) > 0


def test_retry_delay_backs_off_exponentially_without_a_header():
    first = retry_delay_seconds(None, attempt=1)
    second = retry_delay_seconds(None, attempt=2)
    third = retry_delay_seconds(None, attempt=3)
    assert first < second < third


def test_rate_limiter_waits_between_calls():
    slept = []
    clock = iter([0.0, 0.0, 0.5, 0.5])
    limiter = RateLimiter(
        min_interval_seconds=2.0, sleeper=slept.append, clock=lambda: next(clock)
    )
    limiter.wait()
    limiter.wait()
    assert slept == [pytest.approx(1.5)]


def test_rate_limiter_does_not_wait_when_enough_time_has_passed():
    slept = []
    clock = iter([0.0, 0.0, 10.0, 10.0])
    limiter = RateLimiter(
        min_interval_seconds=2.0, sleeper=slept.append, clock=lambda: next(clock)
    )
    limiter.wait()
    limiter.wait()
    assert slept == []


def test_fetch_waits_between_consecutive_tiles(tmp_path):
    """Regression guard for the rate limiter actually being wired into fetch.

    Every other fetch-level test forces min_interval_seconds=0.0 so it never
    has to wait, which means none of them would notice if self._limiter.wait()
    were deleted from _download_tile. This test uses a non-zero interval with
    an injected fake clock and a recording sleeper, so it fails if the wiring
    is removed.
    """
    slept = []
    clock_values = iter([0.0, 0.0, 0.5, 0.5])
    source = _source(
        [FakeResponse(), FakeResponse()],
        min_interval_seconds=2.0,
        sleeper=slept.append,
        clock=lambda: next(clock_values),
    )
    tiles = [_tile("r00_c00"), _tile("r00_c01")]
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), tiles, tmp_path, NullProgress())
    assert slept == [pytest.approx(1.5)]


def test_estimate_scales_with_tile_count():
    source = OsmSource()
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    one = source.estimate(bbox, [_tile("r00_c00")])
    two = source.estimate(bbox, [_tile("r00_c00"), _tile("r00_c01")])
    assert two.bytes_estimate > one.bytes_estimate
    assert two.seconds_estimate > one.seconds_estimate


def test_merge_produces_a_single_osm_file(tmp_path):
    part = tmp_path / "r00_c00.osm"
    part.write_text(OSM_XML, encoding="utf-8")
    outputs = OsmSource().merge([part], tmp_path / "out", "Barry-Waterfront_2026-08-01")
    assert len(outputs) == 1
    assert outputs[0].name == "Barry-Waterfront_2026-08-01.osm"
    assert outputs[0].exists()


def test_merge_names_the_output_after_whatever_stem_it_is_given(tmp_path):
    # Task 20 finding 2: the merged file must carry THIS package's stem, not
    # a fixed name, so two different surveys never produce the same-named
    # reference file that Urbano needs to read.
    part = tmp_path / "r00_c00.osm"
    part.write_text(OSM_XML, encoding="utf-8")
    outputs = OsmSource().merge([part], tmp_path / "out", "Cardiff-Bay_2026-09-01")
    assert outputs[0].name == "Cardiff-Bay_2026-09-01.osm"
