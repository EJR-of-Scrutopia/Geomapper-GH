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
    GeocodeQueueFullError,
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


def test_reverse_treats_an_error_response_as_empty_even_if_address_is_also_present():
    # The previous version of this test used a payload with no "address"
    # key at all, so the "error" branch and the missing-address fallback
    # produced the identical empty result: deleting the "error" check
    # left the test green, because reverse() never needed it to pass.
    # This payload carries both keys, so only the explicit "error" check,
    # not the fallback, can be what stops "Should not be used" coming back.
    payload = {**REVERSE_NO_HIT, "address": {"town": "Should not be used"}}
    client = _client([FakeGeocodeResponse(payload=payload)])
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


# --- GeocodeRateLimiter: bounded queue -------------------------------------
#
# Reserving a slot with no bound on how many can queue is exactly how a
# handful of concurrent callers each end up waiting several seconds for a
# turn that, if Nominatim is down, was never going to succeed: the queue
# sheds no load, it just makes every excess caller wait before finding
# that out.
#
# A first version of this test started max_queue + 1 threads together and
# counted how many came back rejected, expecting exactly one. That is
# unreliable for the same reason the lock test above was: whichever
# caller happens to be first sees no existing reservation, computes a
# wait_for of 0, and returns through the no-op sleeper before the others
# have necessarily even reached their own check, so _pending can be back
# down to 0 again before a third caller ever looks at it, and nothing
# gets rejected at all depending on how the threads happen to be
# scheduled. This version uses a sleeper that blocks on a real Event
# instead of returning immediately, and a Barrier to know for certain
# that exactly max_queue callers are genuinely stuck mid-wait before the
# next one is attempted, so the outcome does not depend on timing.


def test_rate_limiter_rejects_concurrent_waiters_beyond_the_bound():
    max_queue = 2
    entered_wait = threading.Barrier(max_queue + 1)  # the two workers, plus this test
    release = threading.Event()

    def blocking_sleeper(_seconds):
        entered_wait.wait(timeout=5)
        assert release.wait(timeout=5), "never released"

    limiter = GeocodeRateLimiter(
        min_interval_seconds=1.0, sleeper=blocking_sleeper, max_queue=max_queue
    )
    # A fresh limiter's first caller always computes wait_for == 0 (nothing
    # reserved yet) and never reaches the sleeper at all. Priming a
    # reservation already in the future forces every caller below to
    # actually wait, and so to actually reach blocking_sleeper.
    limiter._next_allowed_at = time.monotonic() + 30.0

    worker_errors = []

    def worker():
        try:
            limiter.wait()
        except GeocodeQueueFullError as exc:
            worker_errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(max_queue)]
    for t in workers:
        t.start()
    # Blocks until both workers are inside blocking_sleeper, i.e. genuinely
    # holding a pending slot each, not merely started.
    entered_wait.wait(timeout=5)

    # _pending is now exactly max_queue, held there by the two workers
    # blocked on `release`. A third caller must be rejected immediately,
    # without ever reaching the sleeper.
    with pytest.raises(GeocodeQueueFullError):
        limiter.wait()

    release.set()
    for t in workers:
        t.join(timeout=5)
    assert worker_errors == [], "a legitimate waiter was itself rejected"

    # Once the two workers have finished, the queue has room again.
    limiter._sleeper = lambda _seconds: None
    limiter.wait()


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
# The fake-clock tests above prove the reservation arithmetic is right for
# calls made one after another, which is how mapgen.sources.osm.RateLimiter
# is tested because it is only ever called that way: one job, one worker
# thread. They prove nothing about the lock: a fake clock fed fixed values
# and a sleeper that just records a duration without advancing anything
# produce byte-identical results whether the reservation is written before
# or after the "sleep" (confirmed directly: both orderings recorded the
# same slept=[1.5] against the same two-call sequence). Only real
# concurrent execution, a real clock actually moving while threads race
# each other, can tell the two apart.
#
# A first version of this test used 3 threads. A review found it passed
# even with `with self._lock:` deleted outright, keeping only the correct
# reserve-before-sleep order: the critical section here is a handful of
# fast operations with no I/O in it, so 3 threads contending for it almost
# never actually get interrupted by the GIL mid-section, lock or no lock.
# Measured directly at 24 threads instead: the unlocked build collides at
# a ~0.00002s gap, the locked (committed) build lands at ~0.05s. This test
# now uses enough threads and a low enough threshold to sit clearly between
# those two numbers, so it actually discriminates a missing lock rather
# than passing for the same reason a 3-thread version passes regardless.


def test_rate_limiter_serialises_real_concurrent_threads():
    thread_count = 24
    limiter = GeocodeRateLimiter(min_interval_seconds=0.05, max_queue=thread_count)
    finished_at = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def call():
        barrier.wait(timeout=5)  # every thread starts waiting at once
        limiter.wait()
        with lock:
            finished_at.append(time.monotonic())

    threads = [threading.Thread(target=call) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(finished_at) == thread_count
    finished_at.sort()
    gaps = [finished_at[i + 1] - finished_at[i] for i in range(thread_count - 1)]
    smallest_gap = min(gaps)
    assert smallest_gap >= 0.03, (
        f"smallest gap between {thread_count} consecutive finishers was "
        f"{smallest_gap:.6f}s, expected each to be serialised roughly 0.05s "
        f"apart; a near-zero gap means two threads collided"
    )


def test_rate_limiter_lock_forces_a_second_caller_to_wait_for_the_first_to_finish():
    """Deterministic version of the same property, not dependent on GIL
    scheduling luck at all.

    The test above relies on natural OS/GIL contention among real threads
    to expose a missing lock, and re-measuring it directly on this machine
    found that unreliable: even at 24 threads, deleting `with self._lock:`
    while keeping the correct reserve-before-sleep order still passed 5/5
    runs here, because the critical section is only a handful of fast,
    non-blocking operations, too short for CPython's time-sliced thread
    switching to reliably land inside it. That contradicts an earlier,
    informal measurement elsewhere of a reliable collision at that thread
    count, which this project has no way to reproduce or verify further.

    Rather than tune a thread count against unreliable timing and hope,
    this forces the exact interleaving a missing lock would allow: an
    injected clock blocks the first caller mid-critical-section, and a
    second caller is only released once the first is confirmed to be
    inside. If wait() is genuinely serialised, the second caller cannot
    even reach its own clock call, since that call sits behind the same
    lock the first caller is still holding, until the first caller has
    finished and written its reservation. If it is not serialised, the
    second caller's clock call happens while the first is still blocked,
    before any reservation has been written at all. Checking what
    _next_allowed_at looks like at the exact moment the second caller's
    clock fires distinguishes the two unconditionally, with no dependence
    on how fast either thread happens to run.
    """
    limiter = GeocodeRateLimiter(min_interval_seconds=1.0, sleeper=lambda _s: None)
    first_is_inside = threading.Event()
    release_first = threading.Event()
    real_clock = time.monotonic
    call_count = [0]
    observed_reservation_at_second_call = []

    def controlled_clock():
        call_count[0] += 1
        if call_count[0] == 1:
            first_is_inside.set()
            assert release_first.wait(timeout=5), "first caller was never released"
        elif call_count[0] == 2:
            observed_reservation_at_second_call.append(limiter._next_allowed_at)
        return real_clock()

    limiter._clock = controlled_clock

    def first_caller():
        limiter.wait()

    def second_caller():
        assert first_is_inside.wait(timeout=5), "first caller never reached its clock call"
        limiter.wait()

    t1 = threading.Thread(target=first_caller)
    t2 = threading.Thread(target=second_caller)
    t1.start()
    t2.start()
    # Give the second caller every opportunity to reach its own clock call
    # before the first is released, which it can only do this early if
    # nothing is serialising the two of them.
    time.sleep(0.2)
    release_first.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert observed_reservation_at_second_call, "the second caller's clock was never reached"
    assert observed_reservation_at_second_call[0] is not None, (
        "the second caller's clock call observed _next_allowed_at as None, "
        "meaning it ran before the first caller had written its reservation: "
        "the two callers were not serialised"
    )
