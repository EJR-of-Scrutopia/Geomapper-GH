"""Tests for mapgen.geocode: the server-side Nominatim client.

Fixtures below are modelled on Nominatim's documented, long-stable
response shape (https://nominatim.org/release-docs/latest/api/Search/ and
.../Reverse/), not captured from a live call: this machine's outbound
network tools are policy-blocked, and the suite must not hit the real
service regardless. Every field this client actually reads is given the
real type Nominatim uses, not a convenient one: boundingbox entries are
decimal strings, not numbers, and both endpoints answer "nothing here"
with HTTP 200 and a body shaped unlike a normal result, not an error
status. A stub that only included the fields under test, as numbers,
would have missed exactly the mismatch these tests exist to catch.
"""

import threading
import time

import pytest
import requests

from mapgen.geocode import (
    NOMINATIM_REVERSE_URL,
    NOMINATIM_SEARCH_URL,
    GeocodeError,
    GeocodeRateLimiter,
    GeocodeResult,
    NominatimClient,
    ReverseResult,
)

# A realistic /search hit: Barry, Vale of Glamorgan. boundingbox is
# [south, north, west, east], each a decimal string, Nominatim's own
# order and type, not this client's.
SEARCH_HIT = [
    {
        "place_id": 257422255,
        "licence": "Data © OpenStreetMap contributors, ODbL 1.0. https://osm.org/copyright",
        "osm_type": "relation",
        "osm_id": 171179,
        "boundingbox": ["51.3800000", "51.4300000", "-3.3100000", "-3.2500000"],
        "lat": "51.4055",
        "lon": "-3.2833",
        "display_name": "Barry, Vale of Glamorgan, Wales, United Kingdom",
        "class": "boundary",
        "type": "administrative",
        "importance": 0.61,
    }
]

# Nominatim answers "nothing matched" with 200 and an empty array, not an
# error status.
SEARCH_NO_HIT = []

REVERSE_HIT = {
    "place_id": 98765432,
    "licence": "Data © OpenStreetMap contributors, ODbL 1.0. https://osm.org/copyright",
    "osm_type": "way",
    "osm_id": 4544442,
    "lat": "51.4051",
    "lon": "-3.2836",
    "display_name": "Broad Street, Barry, Vale of Glamorgan, Wales, CF62 7EA, United Kingdom",
    "address": {
        "road": "Broad Street",
        "suburb": "Barry",
        "town": "Barry",
        "county": "Vale of Glamorgan",
        "ISO3166-2-lvl6": "GB-VGL",
        "state": "Wales",
        "postcode": "CF62 7EA",
        "country": "United Kingdom",
        "country_code": "gb",
    },
    "boundingbox": ["51.4041", "51.4061", "-3.2846", "-3.2826"],
}

# Nominatim answers a point with no address data (open water, far
# countryside) with 200 and this shape, not an error status.
REVERSE_NO_HIT = {"error": "Unable to geocode"}


class FakeGeocodeResponse:
    def __init__(self, status_code=200, payload=None, not_json=False):
        self.status_code = status_code
        self._payload = payload
        self._not_json = not_json

    def json(self):
        if self._not_json:
            raise ValueError("not json")
        return self._payload


class FakeGeocodeSession:
    """Replays a queued list of responses, or raises a queued exception to
    simulate a transport failure (a stalled connection, a timeout, a
    reset), and records every call the client made.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _client(responses, **kwargs):
    kwargs.setdefault("limiter", GeocodeRateLimiter(min_interval_seconds=0.0))
    return NominatimClient(session=FakeGeocodeSession(responses), **kwargs)


# --- search --------------------------------------------------------------


def test_search_returns_a_bbox_from_a_real_shaped_hit():
    client = _client([FakeGeocodeResponse(payload=SEARCH_HIT)])
    result = client.search("Barry, Wales")
    assert result == GeocodeResult(west=-3.31, south=51.38, east=-3.25, north=51.43)


def test_search_sends_the_query_and_a_real_user_agent():
    client = _client([FakeGeocodeResponse(payload=SEARCH_HIT)])
    client.search("Barry, Wales")
    url, kwargs = client.session.calls[0]
    assert url == NOMINATIM_SEARCH_URL
    assert kwargs["params"]["q"] == "Barry, Wales"
    assert kwargs["headers"]["User-Agent"].startswith("mapgen/")
    assert "github.com" in kwargs["headers"]["User-Agent"]


def test_search_passes_the_configured_timeout():
    client = _client([FakeGeocodeResponse(payload=SEARCH_HIT)], timeout_seconds=3.5)
    client.search("Barry")
    _url, kwargs = client.session.calls[0]
    assert kwargs["timeout"] == 3.5


def test_search_returns_none_for_no_hit():
    client = _client([FakeGeocodeResponse(payload=SEARCH_NO_HIT)])
    assert client.search("nowhere at all") is None


def test_search_raises_for_a_non_200_status():
    client = _client([FakeGeocodeResponse(status_code=429)])
    with pytest.raises(GeocodeError, match="429"):
        client.search("Barry")


def test_search_raises_when_the_session_cannot_connect():
    client = _client([requests.exceptions.ConnectionError("connection refused")])
    with pytest.raises(GeocodeError, match="Could not reach Nominatim"):
        client.search("Barry")


def test_search_raises_when_the_response_is_not_a_list():
    # A real error body, not the documented empty-array "no results" shape.
    client = _client([FakeGeocodeResponse(payload={"error": "something broke"})])
    with pytest.raises(GeocodeError, match="expected shape"):
        client.search("Barry")


def test_search_raises_when_boundingbox_is_missing():
    hit = [{**SEARCH_HIT[0]}]
    del hit[0]["boundingbox"]
    client = _client([FakeGeocodeResponse(payload=hit)])
    with pytest.raises(GeocodeError, match="expected shape"):
        client.search("Barry")


def test_search_raises_when_the_body_is_not_json():
    client = _client([FakeGeocodeResponse(not_json=True)])
    with pytest.raises(GeocodeError, match="not valid JSON"):
        client.search("Barry")


# --- reverse ---------------------------------------------------------------


def test_reverse_prefers_town_and_county_from_a_real_shaped_hit():
    client = _client([FakeGeocodeResponse(payload=REVERSE_HIT)])
    result = client.reverse(51.405, -3.283)
    assert result == ReverseResult(region="Vale of Glamorgan", site="Barry")


def test_reverse_falls_back_through_the_address_hierarchy():
    payload = {**REVERSE_HIT, "address": {"village": "Small Place", "state": "Wales"}}
    client = _client([FakeGeocodeResponse(payload=payload)])
    result = client.reverse(51.4, -3.3)
    assert result == ReverseResult(region="Wales", site="Small Place")


def test_reverse_returns_empty_strings_for_a_point_with_no_address_data():
    client = _client([FakeGeocodeResponse(payload=REVERSE_NO_HIT)])
    result = client.reverse(0.0, 0.0)
    assert result == ReverseResult(region="", site="")


def test_reverse_sends_lat_lon_and_a_real_user_agent():
    client = _client([FakeGeocodeResponse(payload=REVERSE_HIT)])
    client.reverse(51.405, -3.283)
    url, kwargs = client.session.calls[0]
    assert url == NOMINATIM_REVERSE_URL
    assert kwargs["params"]["lat"] == 51.405
    assert kwargs["params"]["lon"] == -3.283
    assert kwargs["headers"]["User-Agent"].startswith("mapgen/")


def test_reverse_raises_for_a_non_200_status():
    client = _client([FakeGeocodeResponse(status_code=503)])
    with pytest.raises(GeocodeError, match="503"):
        client.reverse(51.4, -3.3)


def test_reverse_raises_when_the_session_times_out():
    # requests.exceptions.Timeout is what a real timeout_seconds actually
    # raises inside requests; a bare TimeoutError would not prove this
    # client survives what the library it depends on really throws.
    client = _client([requests.exceptions.Timeout("timed out")])
    with pytest.raises(GeocodeError, match="Could not reach Nominatim"):
        client.reverse(51.4, -3.3)


def test_reverse_raises_when_the_response_is_not_an_object():
    client = _client([FakeGeocodeResponse(payload=["not", "an", "object"])])
    with pytest.raises(GeocodeError, match="expected shape"):
        client.reverse(51.4, -3.3)


# --- the limiter is actually wired in, not just present -------------------


def test_search_waits_via_the_limiter_before_calling_out():
    waited = []
    limiter = GeocodeRateLimiter(min_interval_seconds=0.0)
    limiter.wait = lambda: waited.append(True)
    client = _client([FakeGeocodeResponse(payload=SEARCH_HIT)], limiter=limiter)
    client.search("Barry")
    assert waited == [True]


def test_reverse_waits_via_the_limiter_before_calling_out():
    waited = []
    limiter = GeocodeRateLimiter(min_interval_seconds=0.0)
    limiter.wait = lambda: waited.append(True)
    client = _client([FakeGeocodeResponse(payload=REVERSE_HIT)], limiter=limiter)
    client.reverse(51.4, -3.3)
    assert waited == [True]


# --- GeocodeRateLimiter: reservation math, fake clock, mirrors how
# mapgen.sources.osm.RateLimiter is tested elsewhere in this suite -------


def test_rate_limiter_waits_between_calls():
    slept = []
    clock = iter([0.0, 0.5])
    limiter = GeocodeRateLimiter(
        min_interval_seconds=2.0, sleeper=slept.append, clock=lambda: next(clock)
    )
    limiter.wait()
    limiter.wait()
    assert slept == [pytest.approx(1.5)]


def test_rate_limiter_does_not_wait_when_enough_time_has_passed():
    slept = []
    clock = iter([0.0, 10.0])
    limiter = GeocodeRateLimiter(
        min_interval_seconds=2.0, sleeper=slept.append, clock=lambda: next(clock)
    )
    limiter.wait()
    limiter.wait()
    assert slept == []


def test_rate_limiter_first_call_never_waits():
    slept = []
    limiter = GeocodeRateLimiter(min_interval_seconds=5.0, sleeper=slept.append, clock=lambda: 100.0)
    limiter.wait()
    assert slept == []


# --- GeocodeRateLimiter: real concurrency, real threads --------------------
#
# The fake-clock tests above prove the reservation arithmetic is right when
# called one after another, which is how mapgen.sources.osm.RateLimiter is
# tested because it is only ever called that way: one job, one worker
# thread. GeocodeRateLimiter exists specifically because this one is not
# called that way, ThreadingHTTPServer hands two concurrent browser
# requests to two concurrent OS threads, so what actually needs proving is
# that a lock around the reservation, not just correct arithmetic, holds
# under real concurrent callers. A fake clock cannot show that: it would
# need to be thread-safe itself to be read from two real threads, at which
# point the lock under test would no longer be the only thing serialising
# them. This uses a real small interval and real threads instead.


def test_rate_limiter_serialises_real_concurrent_threads():
    limiter = GeocodeRateLimiter(min_interval_seconds=0.2)
    finished_at = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def call():
        barrier.wait(timeout=5)  # all three start waiting at once
        limiter.wait()
        with lock:
            finished_at.append(time.monotonic())

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(finished_at) == 3
    finished_at.sort()
    gap_a = finished_at[1] - finished_at[0]
    gap_b = finished_at[2] - finished_at[1]
    # 0.15s threshold against a 0.2s interval: generous slack for OS
    # scheduling jitter while still discriminating from the unlocked bug
    # (reserve-after-sleep), where two threads reading the same pending
    # slot finish together and a gap comes back near 0.
    assert gap_a >= 0.15, f"first pair only {gap_a:.3f}s apart, expected >= 0.2s"
    assert gap_b >= 0.15, f"second pair only {gap_b:.3f}s apart, expected >= 0.2s"
