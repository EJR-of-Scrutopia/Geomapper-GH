import xml.etree.ElementTree as ET

import pytest

from mapgen.geo import BBox, Tile, extent_metres
from mapgen.jobs import CancelToken, Cancelled
from mapgen.sources.base import NullProgress
from mapgen.sources.osm import (
    MAX_SUBDIVISION_DEPTH,
    SPLIT_DIR_NAME,
    NodeCapExceededError,
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
    """Replays a queued list of responses and records every call.

    A queued item that is an exception instance is RAISED, not returned:
    without this, a queued TimeoutError would come back as if it were a
    response object, and fail later on response.status_code with an
    unrelated AttributeError outside of _download_tile's own except
    Exception block, instead of exercising the retry path it exists to
    simulate. Matches test_geocode.py's FakeGeocodeSession, which already
    needed the same thing for the same reason.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


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


# --- Task 22: fetch()'s optional `cancel` parameter ------------------


def test_fetch_with_no_cancel_argument_behaves_exactly_as_before(tmp_path):
    # The optional-extension convention only works if omitting the
    # argument entirely is indistinguishable from the pre-Task-22 shape.
    source = _source([FakeResponse()])
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert paths[0].read_text(encoding="utf-8") == OSM_XML


def test_fetch_stops_before_the_first_tile_when_already_cancelled(tmp_path):
    token = CancelToken()
    token.cancel()
    source = _source([FakeResponse()])  # a request would raise IndexError
    with pytest.raises(Cancelled):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress(), cancel=token
        )
    assert source.session.calls == []


def test_fetch_stops_within_one_tile_once_cancelled_mid_loop(tmp_path):
    # Models a Stop click landing on the server the instant tile 0's own
    # request finishes, before tile 1 starts: the in-flight tile (r00_c00)
    # is paid for and kept, the next one (r00_c01) never starts.
    token = CancelToken()

    class CancellingSession(FakeSession):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            token.cancel()
            return response

    source = OsmSource(
        session=CancellingSession([FakeResponse(), FakeResponse()]),
        sleeper=lambda _seconds: None,
        min_interval_seconds=0.0,
    )
    tiles = [_tile("r00_c00"), _tile("r00_c01")]
    with pytest.raises(Cancelled):
        source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), tiles, tmp_path, NullProgress(), cancel=token)

    assert (tmp_path / "r00_c00.osm").read_text(encoding="utf-8") == OSM_XML
    assert not (tmp_path / "r00_c01.osm").exists()
    assert len(source.session.calls) == 1, "expected only the in-flight tile's own request"


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


def test_node_limit_failure_is_recognised_immediately_without_retrying(tmp_path):
    # One request per attempt, not max_retries of them: a node cap is not
    # a transient failure and retrying the identical request cannot clear
    # it. The three requests here are the tile, the first quarter, and the
    # first sixteenth, each recognised on its first response.
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")] * 3,
        max_retries=4,
    )
    with pytest.raises(OsmDownloadError, match="50000"):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert len(source.session.calls) == 3


def test_node_limit_message_does_not_suggest_a_tile_size_it_cannot_verify(tmp_path):
    # A coordinator review's Honesty finding 1: this used to say "reduce
    # to 1500 or 2000 metres", sizes osm.py has no way to check anything
    # against, since a Tile carries a bbox and no tile_size_m at all. The
    # figures the message may state are the ones it measured for itself,
    # the size of the piece that actually failed, and those come from the
    # tile in hand rather than from a guess about the setting that
    # produced it.
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")] * 3,
        max_retries=4,
    )
    with pytest.raises(NodeCapExceededError) as excinfo:
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    message = str(excinfo.value)
    assert "1500" not in message
    assert "2000" not in message
    # The measured size of the piece that failed IS present, and it is the
    # sixteenth's own ground, about 174 by 278 metres, not the tile's.
    width_m, height_m = extent_metres(_tile().query_bbox)
    assert f"{width_m / 4:.0f} by {height_m / 4:.0f} metres" in message


def test_node_limit_failure_is_specifically_a_node_cap_exceeded_error(tmp_path):
    # The subdivision handler needs to tell this failure apart from any
    # other kind of download failure by TYPE, not by matching on message
    # text (the same structural-over-textual lesson the elevation API key
    # redaction work had to learn the hard way).
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")] * 3,
        max_retries=4,
    )
    with pytest.raises(NodeCapExceededError):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_a_node_cap_error_carries_the_tile_that_was_too_dense(tmp_path):
    # Structural, not textual: the handler that has to split the tile must
    # be able to read which tile it is, not parse it out of a sentence.
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")] * 3,
        max_retries=4,
    )
    with pytest.raises(NodeCapExceededError) as excinfo:
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    # The piece that actually failed, two levels down, and its id names
    # the tile of the plan it came from and where in it.
    assert excinfo.value.tile.tile_id == "r00_c00_q00_q00"
    assert excinfo.value.tile.query_bbox.west == _tile().query_bbox.west


def test_a_different_400_is_not_a_node_cap_exceeded_error(tmp_path):
    # The narrow type must not fire on every 400: only the specific,
    # recognised "too many nodes" body counts as a retryable node-cap
    # failure.
    source = _source([FakeResponse(status_code=400, text="Bad request: malformed bbox")] * 4)
    with pytest.raises(OsmDownloadError) as excinfo:
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert not isinstance(excinfo.value, NodeCapExceededError)


def test_a_too_many_nodes_400_on_the_overpass_path_is_not_a_node_cap_exceeded_error(tmp_path):
    # Coordinator finding: the 50000-node cap belongs to the OSM map API.
    # Overpass is not subject to it, so even a response SHAPED like the
    # map API's own "too many nodes" body must not be classified as a
    # node-cap failure when this instance is actually configured for
    # Overpass, or mapgen.package's whole-run smaller-tile retry would
    # fire for a limit that run is not subject to. Node cap detection is
    # scoped to `not self.use_overpass`; on Overpass this falls through
    # to the generic, retried-then-reported branch instead.
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")] * 4,
        use_overpass=True,
    )
    with pytest.raises(OsmDownloadError) as excinfo:
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert not isinstance(excinfo.value, NodeCapExceededError)


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


# --- Coordinator finding: confirm Overpass etiquette (rate limiting,
# endpoint rotation) is genuinely applied on this path now that category
# selection can route real requests through it, rather than assumed
# because the code happens to be shared with the map API path. A 429 or
# a timeout from a busy Overpass instance must retry and, if it still
# cannot succeed, fail with a plain message naming the endpoint and
# status, never a stack trace. ------------------------------------------


def test_overpass_fetch_honours_retry_after_on_a_429(tmp_path):
    source = _source(
        [FakeResponse(status_code=429, text="rate limited", headers={"Retry-After": "5"}), FakeResponse()],
        use_overpass=True,
    )
    slept = []
    source._sleeper = slept.append
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())
    assert slept == [pytest.approx(5.0)]
    assert len(source.session.calls) == 2


def test_overpass_fetch_raises_a_plain_message_after_repeated_429s(tmp_path):
    source = _source(
        [FakeResponse(status_code=429, text="rate limited")] * 4,
        use_overpass=True,
    )
    with pytest.raises(OsmDownloadError) as excinfo:
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    # A plain sentence naming the tile and attempt count, not a stack
    # trace; the 429 and endpoint are chained as the cause, matching how
    # the map API's own exhausted-retry message already behaves.
    assert "Traceback" not in str(excinfo.value)
    assert "r00_c00" in str(excinfo.value)
    assert "429" in str(excinfo.value.__cause__)
    assert source.overpass_urls[1] in str(excinfo.value.__cause__)


def test_overpass_fetch_retries_after_a_transport_level_timeout(tmp_path):
    # A raised exception (a real timeout, a connection reset), not just a
    # bad status code: _download_tile's broad except already handles this
    # for the map API path; confirmed here for Overpass specifically.
    source = _source([TimeoutError("timed out"), FakeResponse()], use_overpass=True)
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())
    assert len(source.session.calls) == 2


def test_overpass_fetch_raises_a_plain_message_after_repeated_timeouts(tmp_path):
    source = _source([TimeoutError("timed out")] * 4, use_overpass=True)
    with pytest.raises(OsmDownloadError) as excinfo:
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert "Traceback" not in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, TimeoutError)


def test_overpass_fetch_waits_between_consecutive_tiles(tmp_path):
    # test_fetch_waits_between_consecutive_tiles (below) already pins this
    # for the default map API path; confirmed here specifically for
    # Overpass, since that is the path a category filter now genuinely
    # uses, and rate limiting silently only applying to the other path
    # would be exactly the kind of thing worth catching directly rather
    # than assuming shared code stayed shared.
    slept = []
    clock_values = iter([0.0, 0.0, 0.5, 0.5])
    source = _source(
        [FakeResponse(), FakeResponse()],
        use_overpass=True,
        min_interval_seconds=2.0,
        sleeper=slept.append,
        clock=lambda: next(clock_values),
    )
    tiles = [_tile("r00_c00"), _tile("r00_c01")]
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), tiles, tmp_path, NullProgress())
    assert slept == [pytest.approx(1.5)]


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


# --- Coordinator finding: configure() used to leave use_overpass exactly
# as constructed (always False on the registered instance, since
# register_default_sources() never sets it), so every category clause the
# Overpass query builder could produce was unreachable through the CLI or
# browser. A real category restriction must now route through Overpass
# automatically, since the map API has no way to honour one at all. -----


def test_configure_routes_through_overpass_for_a_genuine_category_restriction():
    original = OsmSource(use_overpass=False)
    configured = original.configure(["buildings"])
    assert configured.use_overpass is True
    assert original.use_overpass is False, "must not mutate the registered instance"


def test_configure_routes_through_overpass_for_an_empty_selection_too():
    # An empty (nothing ticked) selection is still a genuine restriction,
    # distinct from None: it must route to Overpass the same as any other
    # narrowed selection, not silently fall back to the unfiltered map API.
    original = OsmSource(use_overpass=False)
    configured = original.configure([])
    assert configured.use_overpass is True


def test_configure_keeps_the_map_api_when_nothing_is_restricted():
    original = OsmSource(use_overpass=False)
    assert original.configure(None).use_overpass is False
    from mapgen.categories import ALL_CATEGORY_IDS

    assert original.configure(list(ALL_CATEGORY_IDS)).use_overpass is False


def test_configure_never_downgrades_an_explicit_use_overpass_choice():
    # A Python caller's own whole-run use_overpass=True (documented in the
    # README as still available directly) must survive configure() being
    # called with no restriction at all: it is a standing choice, not
    # something category selection gets to take away.
    original = OsmSource(use_overpass=True)
    assert original.configure(None).use_overpass is True


def test_routing_note_is_none_on_the_default_map_api_path():
    source = OsmSource(use_overpass=False)
    assert source.routing_note() is None


def test_routing_note_states_the_overpass_routing_when_configured_for_a_category_filter():
    source = OsmSource(use_overpass=False).configure(["buildings"])
    note = source.routing_note()
    assert note is not None
    assert "Overpass" in note
    assert "category filter" in note


def test_routing_note_still_states_overpass_when_use_overpass_was_set_directly():
    # A Python caller who set use_overpass=True themselves (not via a
    # category filter) still gets told they are on a shared service, just
    # without claiming a category filter caused it.
    source = OsmSource(use_overpass=True)
    note = source.routing_note()
    assert note is not None
    assert "Overpass" in note
    assert "category filter" not in note


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


# --- Task 25: the per-tile second is measured, per endpoint ------------


def test_the_map_api_estimate_matches_the_rate_limiter_it_is_governed_by():
    # Measured 2026-08-04 through OsmSource.fetch against the live map
    # API: three runs of six tiles, Vale of Glamorgan farmland at 2000 m
    # and central Barry at 1000 m, every one of them 10.17s to 10.27s for
    # six tiles. The download itself is not what costs; the gaps between
    # tiles land on min_interval_seconds almost exactly, so per-tile time
    # approaches the rate limit as the tile count grows.
    #
    # This replaced a flat 14.0s per tile, which was 8x the measurement
    # and, on the owner's own 72-tile Barry extent, contributed 1008s to
    # a panel whose real total is nearer 175s.
    source = OsmSource()
    assert source.min_interval_seconds == 2.0
    twelve = source.estimate(
        BBox.parse("-3.29,51.38,-3.28,51.39"),
        [_tile(f"r00_c{index:02d}") for index in range(12)],
    )
    per_tile = twelve.seconds_estimate / 12
    assert source.min_interval_seconds * 0.9 <= per_tile <= source.min_interval_seconds * 1.3, (
        f"the map API path is rate limited to one call every "
        f"{source.min_interval_seconds}s and measured 1.71s per tile over six; "
        f"the estimate says {per_tile:.2f}s per tile. A number far above the "
        f"rate limit is not describing this endpoint"
    )


def test_a_category_filtered_run_is_estimated_at_the_overpass_price():
    # The routing decision configure() makes has a cost, and until Task
    # 25 the estimate did not know about it. Measured 2026-08-04: four
    # tiles of central Barry filtered to buildings took 19.25s per tile
    # against 1.71s on the map API, and a second identical run failed
    # outright after 217s with repeated HTTP 504s. Overpass is roughly
    # ten times the price per tile, when it works at all.
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    tiles = [_tile(f"r00_c{index:02d}") for index in range(4)]
    map_api = OsmSource().estimate(bbox, tiles)
    overpass = OsmSource().configure(["buildings"]).estimate(bbox, tiles)

    assert OsmSource().configure(["buildings"]).use_overpass is True
    assert overpass.seconds_estimate > 5 * map_api.seconds_estimate, (
        f"a category filter routes this run through Overpass, which measured "
        f"about ten times the map API's cost per tile; the estimate says "
        f"{overpass.seconds_estimate:.0f}s against {map_api.seconds_estimate:.0f}s"
    )
    assert overpass.seconds_estimate / len(tiles) >= 15.0, (
        "Overpass measured 19.25s per tile on the run that succeeded and "
        "failed on the run that did not, so this constant is a floor"
    )


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


def test_possible_outputs_names_exactly_the_stem_osm_file():
    assert OsmSource().possible_outputs("Stem_2026-08-01") == ["Stem_2026-08-01.osm"]


# =======================================================================
# Task 26: a tile over the node cap is split into quarters and recombined,
# instead of the whole run restarting at a smaller tile size.
# =======================================================================
#
# Every test below runs against GroundSession rather than a queue of
# canned responses. A queue answers in call order and knows nothing about
# what was asked for, which is exactly the stub that is more permissive
# than the real thing: it cannot tell a quarter from its parent, so it
# cannot show that splitting actually helped, and a queue that always
# answers "too many nodes" recurses to the depth cap and proves nothing
# about the success path.
#
# GroundSession models the ground instead. It holds nodes at known
# coordinates and ways over them, answers each request from the bbox that
# was actually asked for, and refuses with the map API's own "too many
# nodes" 400 when more than node_cap nodes fall inside it. Density is
# then a property of where the nodes are, so "dense in one corner, fine
# everywhere else", which is what a real city centre in a rural extent
# looks like, is something the test can simply state.


class GroundSession:
    """A fake OSM map API answering from a model of the ground.

    Reproduces the three parts of the real map API's contract that this
    feature depends on: every node inside the bbox, every way with at
    least one node inside it, and those ways' remaining nodes even where
    they fall outside. The last one is why a way lying across a seam
    comes back whole from whichever piece holds one of its nodes, and
    therefore why a split cannot clip anything.
    """

    def __init__(self, nodes, ways=None, node_cap=50):
        self.nodes = dict(nodes)
        self.ways = dict(ways or {})
        self.node_cap = node_cap
        self.calls = []
        self.bboxes = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        west, south, east, north = (
            float(value) for value in kwargs["params"]["bbox"].split(",")
        )
        self.bboxes.append((west, south, east, north))
        inside = {
            node_id
            for node_id, (lon, lat) in self.nodes.items()
            if west <= lon <= east and south <= lat <= north
        }
        if len(inside) > self.node_cap:
            return FakeResponse(status_code=400, text="You requested too many nodes")

        way_ids = sorted(
            way_id
            for way_id, refs in self.ways.items()
            if any(ref in inside for ref in refs)
        )
        returned_nodes = set(inside)
        for way_id in way_ids:
            returned_nodes.update(self.ways[way_id])
        return FakeResponse(text=self._document(sorted(returned_nodes), way_ids))

    def post(self, url, **kwargs):
        raise AssertionError("the node cap belongs to the map API, not Overpass")

    def _document(self, node_ids, way_ids):
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
        for node_id in node_ids:
            lon, lat = self.nodes[node_id]
            lines.append(f'  <node id="{node_id}" version="1" lat="{lat}" lon="{lon}"/>')
        for way_id in way_ids:
            refs = "".join(f'<nd ref="{ref}"/>' for ref in self.ways[way_id])
            lines.append(f'  <way id="{way_id}" version="1">{refs}</way>')
        lines.append("</osm>")
        return "\n".join(lines) + "\n"


def _element_keys(path):
    """Every element in an .osm file as type/id strings, IN FILE ORDER and
    with repeats kept, so a duplicate is visible rather than collapsed."""
    root = ET.parse(path).getroot()
    return [f"{el.tag}/{el.get('id')}" for el in root if el.tag in ("node", "way")]


def _split_tile():
    """A tile with a real overlap margin, the shape build_tiles produces
    for anything that is not on the edge of the extent."""
    return Tile(
        tile_id="r03_c04",
        row=3,
        col=4,
        core_bbox=BBox(-3.290, 51.380, -3.280, 51.390),
        query_bbox=BBox(-3.291, 51.379, -3.279, 51.391),
    )


def _spread(count, west, south, east, north, first_id=1):
    """count nodes spread evenly over a rectangle, on a square-ish grid."""
    import math

    per_side = math.ceil(math.sqrt(count))
    nodes = {}
    for index in range(count):
        row, col = divmod(index, per_side)
        lon = west + (east - west) * (col + 0.5) / per_side
        lat = south + (north - south) * (row + 0.5) / per_side
        nodes[first_id + index] = (lon, lat)
    return nodes


def _ground_source(session, **kwargs):
    kwargs.setdefault("sleeper", lambda _seconds: None)
    kwargs.setdefault("min_interval_seconds", 0.0)
    return OsmSource(session=session, **kwargs)


class Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append({"event": event, **fields})


def test_a_tile_over_the_node_cap_is_split_into_four_and_recombined(tmp_path):
    # 200 nodes over the whole tile, a cap of 120: the tile fails, and
    # each quarter holds about 50, which does not.
    tile = _split_tile()
    session = GroundSession(_spread(200, -3.291, 51.379, -3.279, 51.391), node_cap=120)
    source = _ground_source(session)

    paths = source.fetch(BBox.parse("-3.30,51.37,-3.27,51.40"), [tile], tmp_path, NullProgress())

    # The pipeline outside this module sees one ordinary tile file, named
    # after the tile of the plan, exactly as if it had never been split.
    assert paths == [tmp_path / "r03_c04.osm"]
    assert paths[0].exists()
    # One failed request for the tile, then four that worked.
    assert len(session.calls) == 5
    keys = _element_keys(paths[0])
    assert len(keys) == 200, "every node the ground holds should be in the recombined tile"


def test_a_subdivided_tile_says_so_through_the_progress_sink(tmp_path):
    tile = _split_tile()
    session = GroundSession(_spread(200, -3.291, 51.379, -3.279, 51.391), node_cap=120)
    progress = Recorder()

    _ground_source(session).fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"), [tile], tmp_path, progress
    )

    subdivided = [e for e in progress.events if e["event"] == "tile_subdivided"]
    assert len(subdivided) == 1
    assert subdivided[0]["source"] == "osm"
    assert subdivided[0]["tile_id"] == "r03_c04", "the parent tile, by the id the plan knows"
    assert subdivided[0]["pieces"] == 4
    # And it still finishes as an ordinary done tile: a subdivided tile is
    # not a failure and must not be shown as one.
    assert {"event": "tile_done", "source": "osm", "tile_id": "r03_c04"} in progress.events
    assert not any(e["event"] == "tile_failed" for e in progress.events)


def test_the_recombined_tile_holds_exactly_what_an_unsplit_fetch_would_have(tmp_path):
    # The seam question, answered by comparison rather than assertion: the
    # same ground, fetched once whole and once in quarters, must produce
    # the same set of elements. The ways here run straight across both
    # seams, so a clipped feature or a duplicated one would show up as a
    # difference.
    nodes = _spread(200, -3.291, 51.379, -3.279, 51.391)
    ways = {
        # Left edge to right edge, across the vertical seam.
        900: [1, 100, 200],
        # And a short one sitting right on the crossing of both seams.
        901: [95, 96, 105, 106],
    }
    tile = _split_tile()

    whole_session = GroundSession(nodes, ways, node_cap=10_000)
    whole_dir = tmp_path / "whole"
    whole_dir.mkdir()
    _ground_source(whole_session).fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"), [tile], whole_dir, NullProgress()
    )
    assert len(whole_session.calls) == 1, "this half of the comparison must not split"

    split_session = GroundSession(nodes, ways, node_cap=120)
    split_dir = tmp_path / "split"
    split_dir.mkdir()
    _ground_source(split_session).fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"), [tile], split_dir, NullProgress()
    )
    assert len(split_session.calls) == 5, "this half of the comparison must split"

    whole_keys = _element_keys(whole_dir / "r03_c04.osm")
    split_keys = _element_keys(split_dir / "r03_c04.osm")

    assert set(split_keys) == set(whole_keys), "the split fetch covered different ground"
    assert len(split_keys) == len(set(split_keys)), "a seam duplicate survived the merge"
    assert len(split_keys) == len(whole_keys)
    assert "way/900" in set(split_keys) and "way/901" in set(split_keys)


def test_a_quarter_that_is_still_too_dense_is_split_again(tmp_path):
    # The realistic shape: one dense corner and three quiet ones, not
    # uniform density. The south-west quarter needs a second round; the
    # other three are answered first time.
    dense_corner = _spread(200, -3.2905, 51.3795, -3.2845, 51.3855, first_id=1)
    sparse = _spread(30, -3.2845, 51.3855, -3.2795, 51.3905, first_id=1000)
    session = GroundSession({**dense_corner, **sparse}, node_cap=120)
    tile = _split_tile()
    progress = Recorder()

    _ground_source(session).fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"), [tile], tmp_path, progress
    )

    subdivided = [e for e in progress.events if e["event"] == "tile_subdivided"]
    assert [e["tile_id"] for e in subdivided] == ["r03_c04", "r03_c04_q00"], (
        "expected the tile split once and its dense quarter split again, "
        f"got {[e['tile_id'] for e in subdivided]}"
    )
    assert [e["depth"] for e in subdivided] == [1, 2]
    # The tile, its dense quarter, that quarter's four sixteenths, and the
    # three quarters that were fine first time: nine requests, depth
    # first, none of them repeated.
    assert len(session.calls) == 9
    keys = _element_keys(tmp_path / "r03_c04.osm")
    assert len(keys) == len(set(keys)) == 230


def test_a_piece_still_over_the_cap_at_the_depth_cap_fails_and_says_what_was_tried(tmp_path):
    # Every node in one tiny spot, so no amount of quartering thins it
    # out: the depth cap is what stops this, not the arithmetic.
    session = GroundSession(_spread(200, -3.2901, 51.3801, -3.2900, 51.3802), node_cap=120)
    source = _ground_source(session)

    with pytest.raises(NodeCapExceededError) as excinfo:
        source.fetch(
            BBox.parse("-3.30,51.37,-3.27,51.40"), [_split_tile()], tmp_path, NullProgress()
        )

    message = str(excinfo.value)
    assert "50000-node" in message
    assert f"quarters {MAX_SUBDIVISION_DEPTH} times" in message
    assert "metres" in message, "the size of the piece that failed should be stated"
    assert "Draw a smaller extent" in message
    # It never loops: the tile, one quarter, one sixteenth, then it stops.
    assert len(session.calls) == 1 + MAX_SUBDIVISION_DEPTH


def test_a_failed_subdivision_leaves_no_tile_file_behind(tmp_path):
    # The half-fetched set of quarters must not be recombined into a
    # parent file, which package.py would then read as a finished tile.
    session = GroundSession(_spread(200, -3.2901, 51.3801, -3.2900, 51.3802), node_cap=120)
    with pytest.raises(NodeCapExceededError):
        _ground_source(session).fetch(
            BBox.parse("-3.30,51.37,-3.27,51.40"), [_split_tile()], tmp_path, NullProgress()
        )
    assert not (tmp_path / "r03_c04.osm").exists()


def test_the_quarters_are_kept_on_disk_in_a_scratch_subdirectory(tmp_path):
    session = GroundSession(_spread(200, -3.291, 51.379, -3.279, 51.391), node_cap=120)
    _ground_source(session).fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"), [_split_tile()], tmp_path, NullProgress()
    )

    split_dir = tmp_path / SPLIT_DIR_NAME
    assert sorted(p.name for p in split_dir.iterdir()) == [
        "r03_c04_q00.osm",
        "r03_c04_q01.osm",
        "r03_c04_q10.osm",
        "r03_c04_q11.osm",
    ]
    # Not loose in the source's work directory beside the real tiles, and
    # not named like one either.
    assert sorted(p.name for p in tmp_path.iterdir()) == [SPLIT_DIR_NAME, "r03_c04.osm"]


def test_a_resumed_run_reuses_the_quarters_already_on_disk(tmp_path):
    # An interrupted run resumes INTO the subdivision. Two quarters are
    # already on disk; the resume must pay for the other two and no more.
    nodes = _spread(200, -3.291, 51.379, -3.279, 51.391)
    tile = _split_tile()

    first = GroundSession(nodes, node_cap=120)
    _ground_source(first).fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"), [tile], tmp_path, NullProgress()
    )
    (tmp_path / "r03_c04.osm").unlink()  # as if the run died before recombining
    (tmp_path / SPLIT_DIR_NAME / "r03_c04_q10.osm").unlink()
    (tmp_path / SPLIT_DIR_NAME / "r03_c04_q11.osm").unlink()

    second = GroundSession(nodes, node_cap=120)
    progress = Recorder()
    _ground_source(second).fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"), [tile], tmp_path, progress
    )

    # One request for the tile itself (still too dense, still true) plus
    # the two missing quarters. The two that survived are not refetched.
    assert len(second.calls) == 3
    skipped = [e["tile_id"] for e in progress.events if e["event"] == "tile_skipped"]
    assert skipped == ["r03_c04_q00", "r03_c04_q01"]
    keys = _element_keys(tmp_path / "r03_c04.osm")
    assert len(keys) == len(set(keys)) == 200, "the resumed tile must still be whole"


def test_a_stop_landing_mid_subdivision_keeps_the_quarters_and_writes_no_tile(tmp_path):
    # Task 22's contract, one level further in: a stop is allowed to
    # interrupt between quarters, never during one, and what it leaves
    # behind must not read as a finished tile.
    token = CancelToken()
    nodes = _spread(200, -3.291, 51.379, -3.279, 51.391)

    class StoppingSession(GroundSession):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if len(self.calls) == 3:  # the tile, one quarter, then stop
                token.cancel()
            return response

    session = StoppingSession(nodes, node_cap=120)
    with pytest.raises(Cancelled):
        _ground_source(session).fetch(
            BBox.parse("-3.30,51.37,-3.27,51.40"),
            [_split_tile()],
            tmp_path,
            NullProgress(),
            cancel=token,
        )

    # No recombined tile: package.py finds nothing for this tile and, on a
    # stopped run, records it pending rather than failed or ok.
    assert not (tmp_path / "r03_c04.osm").exists()
    # The quarters that landed are kept, including the one that was in
    # flight when the stop arrived: it was paid for.
    assert sorted(p.name for p in (tmp_path / SPLIT_DIR_NAME).iterdir()) == [
        "r03_c04_q00.osm",
        "r03_c04_q01.osm",
    ]
    assert len(session.calls) == 3, "the stop must not start the third quarter"


def test_a_run_resumed_after_a_stop_mid_subdivision_finishes_the_tile(tmp_path):
    # The other half of the same story, tested rather than reasoned: the
    # resume picks up the two quarters the stop left and completes the
    # tile, and the tile it produces is whole.
    token = CancelToken()
    nodes = _spread(200, -3.291, 51.379, -3.279, 51.391)

    class StoppingSession(GroundSession):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if len(self.calls) == 3:
                token.cancel()
            return response

    with pytest.raises(Cancelled):
        _ground_source(StoppingSession(nodes, node_cap=120)).fetch(
            BBox.parse("-3.30,51.37,-3.27,51.40"),
            [_split_tile()],
            tmp_path,
            NullProgress(),
            cancel=token,
        )

    resumed = GroundSession(nodes, node_cap=120)
    _ground_source(resumed).fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"),
        [_split_tile()],
        tmp_path,
        NullProgress(),
        cancel=CancelToken(),
    )

    assert len(resumed.calls) == 3, "the tile, and only the two missing quarters"
    keys = _element_keys(tmp_path / "r03_c04.osm")
    assert len(keys) == len(set(keys)) == 200


def test_the_quarters_are_rate_limited_like_any_other_request(tmp_path):
    # The quarters go through _download_tile, so they are spaced by the
    # same RateLimiter every other request is, and they go one at a time.
    # A tile that just failed for being too dense is not the place to
    # start making concurrent requests against a free public service.
    slept = []
    clock_values = iter([float(tick) for tick in range(0, 40)])
    session = GroundSession(_spread(200, -3.291, 51.379, -3.279, 51.391), node_cap=120)
    source = _ground_source(
        session,
        min_interval_seconds=2.0,
        sleeper=slept.append,
        clock=lambda: next(clock_values),
    )
    source.fetch(
        BBox.parse("-3.30,51.37,-3.27,51.40"), [_split_tile()], tmp_path, NullProgress()
    )
    # Five requests, so four gaps, every one of them waited on: the clock
    # advances 1s per reading against a 2s minimum interval.
    assert slept == [pytest.approx(1.0)] * 4


def test_an_overpass_run_never_subdivides_even_on_a_node_cap_shaped_response(tmp_path):
    # A category-filtered run goes to Overpass, Overpass has no node cap,
    # and subdivision answers that cap specifically. Even a response
    # SHAPED like the map API's own refusal must therefore be an ordinary
    # retried-then-reported failure here, with no quarters fetched: four
    # attempts at the one tile, and no scratch directory at all.
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")] * 4,
        use_overpass=True,
        max_retries=4,
    )
    with pytest.raises(OsmDownloadError) as excinfo:
        source.fetch(
            BBox.parse("-3.30,51.37,-3.27,51.40"), [_split_tile()], tmp_path, NullProgress()
        )
    assert not isinstance(excinfo.value, NodeCapExceededError)
    assert len(source.session.calls) == 4
    assert not (tmp_path / SPLIT_DIR_NAME).exists()
