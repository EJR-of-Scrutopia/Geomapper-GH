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
    assert len(source.endpoints_used) == 1


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


def test_node_limit_failure_is_reported_immediately_without_retrying(tmp_path):
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")], max_retries=4
    )
    with pytest.raises(OsmDownloadError, match="50000"):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert len(source.session.calls) == 1


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
    outputs = OsmSource().merge([part], tmp_path / "out")
    assert len(outputs) == 1
    assert outputs[0].name == "all.osm"
    assert outputs[0].exists()
