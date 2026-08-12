"""ngd.py's suite: the in-memory OGC API Features client for the OS NGD
API, stubbed exactly the way test_os_downloads.py and
test_sources_os_uprn.py already stub urllib (`NgdClient.session` is an
object shaped like `urllib.request.OpenerDirector`, a `.open(request,
timeout=...)` method): every test below except the one `live` test at
the bottom hands `NgdClient` a scripted fake opener and never touches
the real network. `_FakeHTTPResponse` is imported straight from
test_os_downloads.py rather than duplicated, the same reuse
test_sources_os_uprn.py's own module docstring already establishes for
this exact class.

The firewall and the key are this module's whole reason to exist (see
ngd.py's own module docstring): every test that can prove the key never
reaches a URL, and that a next link pointing off api.os.uk is refused
before it is ever requested, does so here rather than trusting the
production code's own comments.
"""

from __future__ import annotations

import json
import os
import urllib.error

import pytest

from mapgen.ngd import (
    BUILDING_COLLECTION,
    DEV_MODE_TRANSACTIONS_PER_MINUTE,
    MAX_PAGES,
    MAX_RETRY_AFTER_SLEEP_SECONDS,
    MIN_REQUEST_INTERVAL_SECONDS,
    NGD_ROOT,
    NgdClient,
    NgdError,
)
from tests.test_os_downloads import _FakeHTTPResponse

_TEST_KEY = "sekrit-dev-mode-key-do-not-leak"


class _FakeClock:
    """A monotonic clock a test controls completely: starts at `start`,
    advances only when `advance()` is called (never on its own), so a
    test can pin exactly how much wall time `NgdClient._pace` believes
    has passed between two requests without a single real sleep.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _RecordingSleeper:
    """Records every requested sleep duration and, since the whole point
    of pacing is "time has now passed", advances the paired `_FakeClock`
    by exactly that amount rather than actually blocking: this is what
    lets `NgdClient._pace`'s own "how much time has passed since the
    last request" arithmetic see a realistic elapsed time on the very
    next call, with no test ever sleeping for real.
    """

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class _ScriptedOpener:
    """Answers `.open()` calls in order from a fixed script, exactly
    test_os_downloads.py's own `_FakeOpener` shape (kept local rather
    than imported, since this suite wants its own `requests_made` name
    read the same way throughout this file). Each scripted item is
    either a response to return or an exception instance to raise, once,
    in order. Raises IndexError, loudly, if asked for more than the
    script provides: that is exactly the proof a "must never see a
    second request" assertion below relies on, since an actual second
    call to `.open()` would fail this test rather than silently succeed.
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.requests_made: list = []

    def open(self, request, timeout=None):
        self.requests_made.append(request)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _feature_page(prefix: str, count: int, next_href: str | None) -> bytes:
    features = [
        {"type": "Feature", "id": f"{prefix}-{i}", "geometry": None, "properties": {}}
        for i in range(count)
    ]
    body: dict = {"type": "FeatureCollection", "features": features}
    body["links"] = [{"rel": "next", "href": next_href}] if next_href else [
        {"rel": "self", "href": "https://api.os.uk/features/ngd/ofa/v1/collections/x/items"}
    ]
    return json.dumps(body).encode("utf-8")


def _response(body: bytes) -> _FakeHTTPResponse:
    return _FakeHTTPResponse(200, body)


# --------------------------------------------------------------------------
# items(): two-page paging, the key in the header, never in a URL.
# --------------------------------------------------------------------------


def test_items_follows_the_next_link_and_the_key_travels_only_in_the_header():
    next_href = f"{NGD_ROOT}/collections/{BUILDING_COLLECTION}/items?cursor=page2"
    opener = _ScriptedOpener(
        [
            _response(_feature_page("p1", 100, next_href)),
            _response(_feature_page("p2", 100, None)),
        ]
    )
    clock = _FakeClock()
    sleeper = _RecordingSleeper(clock)
    client = NgdClient(key=_TEST_KEY, session=opener, sleeper=sleeper, clock=clock)

    features, pages = client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert len(features) == 200
    assert pages == 2
    assert len(opener.requests_made) == 2
    for request in opener.requests_made:
        # urllib.request.Request.add_header stores every header under
        # key.capitalize() regardless of the case a caller passed in
        # (verified directly against this Python's own urllib: "key" is
        # stored as "Key"), and get_header does NOT re-capitalize its
        # own argument before looking the stored name up, so a lookup
        # must use that exact stored form.
        assert request.get_header("Key") == _TEST_KEY
        assert _TEST_KEY not in request.full_url


# --------------------------------------------------------------------------
# items(): a next link pointing off NGD_ROOT is refused before it is ever
# requested, so the key never reaches a foreign host.
# --------------------------------------------------------------------------


def test_items_refuses_a_next_link_pointing_off_ngd_root():
    foreign_href = "https://evil.example.test/collections/x/items?key=stolen"
    opener = _ScriptedOpener([_response(_feature_page("p1", 10, foreign_href))])
    client = NgdClient(key=_TEST_KEY, session=opener)

    with pytest.raises(NgdError) as excinfo:
        client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert excinfo.value.kind == "parse"
    # Only the first page's own request happened: the opener's script had
    # exactly one entry, so a second .open() call (which would mean the
    # foreign link was actually requested) would raise IndexError instead
    # of quietly succeeding.
    assert len(opener.requests_made) == 1


# --------------------------------------------------------------------------
# items(): the HTTP status mapping. 401 -> auth, with the pinned message
# and no key or URL in it; 429 with Retry-After -> cap, with the seconds
# parsed through.
# --------------------------------------------------------------------------


def test_items_maps_401_to_auth_with_no_key_or_url_in_the_message():
    opener = _ScriptedOpener(
        [urllib.error.HTTPError("https://api.os.uk/x", 401, "Unauthorized", {}, None)]
    )
    client = NgdClient(key=_TEST_KEY, session=opener)

    with pytest.raises(NgdError) as excinfo:
        client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert excinfo.value.kind == "auth"
    message = str(excinfo.value)
    assert _TEST_KEY not in message
    assert "http" not in message.lower()
    assert message == (
        "the OS NGD key was refused; a dev-mode Premium project with the "
        "NGD Features API added is what answers here"
    )


def _http_429(retry_after: str | None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://api.os.uk/x", 429, "Too Many Requests", headers, None)


def test_items_429_with_retry_after_under_ceiling_sleeps_then_succeeds():
    """The one bounded retry: a 429 carrying a Retry-After under the
    ceiling is honoured (slept via the sleeper seam, never for real),
    then the same request is retried once and this time succeeds.
    """
    opener = _ScriptedOpener(
        [
            _http_429("7"),
            _response(_feature_page("p1", 10, None)),
        ]
    )
    clock = _FakeClock()
    sleeper = _RecordingSleeper(clock)
    client = NgdClient(key=_TEST_KEY, session=opener, sleeper=sleeper, clock=clock)

    features, pages = client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert len(features) == 10
    assert pages == 1
    assert len(opener.requests_made) == 2
    # Exactly the advertised 7 seconds, once: the pacing gate itself adds
    # nothing further here, since the retry sleep already moved the fake
    # clock well past MIN_REQUEST_INTERVAL_SECONDS.
    assert sleeper.calls == [7.0]


def test_items_two_consecutive_429s_raise_cap():
    """A second 429, arriving right after the one bounded retry already
    granted, is not retried again: it raises kind "cap" immediately.
    """
    opener = _ScriptedOpener([_http_429("3"), _http_429("3")])
    clock = _FakeClock()
    sleeper = _RecordingSleeper(clock)
    client = NgdClient(key=_TEST_KEY, session=opener, sleeper=sleeper, clock=clock)

    with pytest.raises(NgdError) as excinfo:
        client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert excinfo.value.kind == "cap"
    assert excinfo.value.retry_after_seconds == 3.0
    # The first 429's own Retry-After was honoured once (one sleep of 3
    # seconds); the second 429 was never slept for, it raised outright.
    assert sleeper.calls == [3.0]
    assert len(opener.requests_made) == 2


def test_items_429_retry_after_beyond_ceiling_raises_without_sleeping():
    """A Retry-After above MAX_RETRY_AFTER_SLEEP_SECONDS is not honoured
    at all: this client fails fast rather than blocking a benchmark run
    for an unbounded time.
    """
    opener = _ScriptedOpener([_http_429(str(MAX_RETRY_AFTER_SLEEP_SECONDS + 1.0))])
    clock = _FakeClock()
    sleeper = _RecordingSleeper(clock)
    client = NgdClient(key=_TEST_KEY, session=opener, sleeper=sleeper, clock=clock)

    with pytest.raises(NgdError) as excinfo:
        client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert excinfo.value.kind == "cap"
    assert excinfo.value.retry_after_seconds == MAX_RETRY_AFTER_SLEEP_SECONDS + 1.0
    assert sleeper.calls == []
    assert len(opener.requests_made) == 1


def test_items_429_without_retry_after_raises_cap_immediately():
    """No Retry-After header at all means this client has nothing to
    honour: it raises kind "cap" on the first 429, no retry attempted.
    """
    opener = _ScriptedOpener([_http_429(None)])
    clock = _FakeClock()
    sleeper = _RecordingSleeper(clock)
    client = NgdClient(key=_TEST_KEY, session=opener, sleeper=sleeper, clock=clock)

    with pytest.raises(NgdError) as excinfo:
        client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert excinfo.value.kind == "cap"
    assert excinfo.value.retry_after_seconds is None
    assert sleeper.calls == []
    assert len(opener.requests_made) == 1


# --------------------------------------------------------------------------
# Dev-mode pacing: NgdClient._pace, the monotonic-clock gate.
# --------------------------------------------------------------------------


def test_pacing_constants():
    assert DEV_MODE_TRANSACTIONS_PER_MINUTE == 50
    assert MIN_REQUEST_INTERVAL_SECONDS == pytest.approx(60.0 / 50 * 1.15)
    assert MAX_RETRY_AFTER_SLEEP_SECONDS == 120.0


def test_pacing_never_sleeps_before_the_first_request():
    opener = _ScriptedOpener([_response(_feature_page("p1", 10, None))])
    clock = _FakeClock()
    sleeper = _RecordingSleeper(clock)
    client = NgdClient(key=_TEST_KEY, session=opener, sleeper=sleeper, clock=clock)

    client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert sleeper.calls == []


def test_pacing_sleeps_the_expected_interval_between_successive_pages():
    """Two pages, no real time elapsing between them on the fake clock
    (the opener answers instantly): the gate must sleep exactly
    MIN_REQUEST_INTERVAL_SECONDS before the second page's own request,
    and nothing before the first.
    """
    next_href = f"{NGD_ROOT}/collections/{BUILDING_COLLECTION}/items?cursor=page2"
    opener = _ScriptedOpener(
        [
            _response(_feature_page("p1", 100, next_href)),
            _response(_feature_page("p2", 100, next_href)),
            _response(_feature_page("p3", 100, None)),
        ]
    )
    clock = _FakeClock()
    sleeper = _RecordingSleeper(clock)
    client = NgdClient(key=_TEST_KEY, session=opener, sleeper=sleeper, clock=clock)

    features, pages = client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert pages == 3
    assert len(features) == 300
    assert sleeper.calls == [MIN_REQUEST_INTERVAL_SECONDS, MIN_REQUEST_INTERVAL_SECONDS]


def test_pacing_does_not_sleep_when_enough_real_time_already_passed():
    """A caller-provided clock that shows more than MIN_REQUEST_INTERVAL_
    SECONDS already elapsed between two requests (real processing time,
    or a prior Retry-After sleep) owes no further wait.
    """
    next_href = f"{NGD_ROOT}/collections/{BUILDING_COLLECTION}/items?cursor=page2"
    opener = _ScriptedOpener(
        [
            _response(_feature_page("p1", 100, next_href)),
            _response(_feature_page("p2", 100, None)),
        ]
    )
    clock = _FakeClock()
    sleeper = _RecordingSleeper(clock)

    real_open = opener.open

    def _open_and_advance(request, timeout=None):
        clock.advance(MIN_REQUEST_INTERVAL_SECONDS * 2)
        return real_open(request, timeout=timeout)

    opener.open = _open_and_advance
    client = NgdClient(key=_TEST_KEY, session=opener, sleeper=sleeper, clock=clock)

    client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert sleeper.calls == []


# --------------------------------------------------------------------------
# items(): max_pages is a hard cap, not a suggestion.
# --------------------------------------------------------------------------


def test_items_raises_cap_when_max_pages_is_exceeded():
    next_href = f"{NGD_ROOT}/collections/{BUILDING_COLLECTION}/items?cursor=page2"
    opener = _ScriptedOpener([_response(_feature_page("p1", 100, next_href))])
    client = NgdClient(key=_TEST_KEY, session=opener)

    with pytest.raises(NgdError) as excinfo:
        client.items(
            BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0), max_pages=1
        )

    assert excinfo.value.kind == "cap"
    assert "1" in str(excinfo.value)
    # Only the one page allowed by max_pages was ever actually fetched.
    assert len(opener.requests_made) == 1


def test_max_pages_constant_is_five_hundred():
    assert MAX_PAGES == 500


# --------------------------------------------------------------------------
# items(): a body that is not valid JSON raises "parse".
# --------------------------------------------------------------------------


def test_items_raises_parse_for_garbage_json():
    opener = _ScriptedOpener([_response(b"not valid json at all {{{")])
    client = NgdClient(key=_TEST_KEY, session=opener)

    with pytest.raises(NgdError) as excinfo:
        client.items(BUILDING_COLLECTION, (317000.0, 176000.0, 317200.0, 176200.0))

    assert excinfo.value.kind == "parse"


# --------------------------------------------------------------------------
# verify_collections(): keyless, and a missing id names itself.
# --------------------------------------------------------------------------


def test_verify_collections_is_keyless_and_names_a_missing_id():
    listing = {
        "collections": [
            {"id": "bld-fts-buildingpart-2"},
            {"id": "some-other-collection-1"},
        ]
    }
    opener = _ScriptedOpener([_response(json.dumps(listing).encode("utf-8"))])
    client = NgdClient(key=_TEST_KEY, session=opener)

    with pytest.raises(NgdError) as excinfo:
        client.verify_collections(["bld-fts-buildingpart-2", "trn-ntwk-roadlink-5"])

    assert excinfo.value.kind == "listing"
    assert "trn-ntwk-roadlink-5" in str(excinfo.value)
    # Keyless: the one request made carried no key header at all.
    assert len(opener.requests_made) == 1
    assert opener.requests_made[0].get_header("Key") is None
    assert _TEST_KEY not in opener.requests_made[0].full_url


def test_verify_collections_passes_when_every_id_is_listed():
    listing = {"collections": [{"id": "bld-fts-buildingpart-2"}, {"id": "trn-ntwk-roadlink-5"}]}
    opener = _ScriptedOpener([_response(json.dumps(listing).encode("utf-8"))])
    client = NgdClient(key=_TEST_KEY, session=opener)

    client.verify_collections(["bld-fts-buildingpart-2", "trn-ntwk-roadlink-5"])  # must not raise


# --------------------------------------------------------------------------
# The one live test. Real OS NGD API, dev-mode key, honest skip (the
# os_uprn precedent) when OS_NGD_KEY is unset.
# --------------------------------------------------------------------------

_COWBRIDGE_BUILDING_BBOX = (317000.0, 176000.0, 317200.0, 176200.0)


@pytest.mark.live
def test_live_pulls_a_building_over_cowbridge_in_bng():
    """With OS_NGD_KEY set, a real limit-100, max_pages=1 pull of
    buildingparts over a 200 m Cowbridge bbox must come back with at
    least one feature whose first coordinate is a BNG easting in
    310000-320000, not a longitude (which would read roughly -3.x here):
    the proof that `crs`/`bbox-crs` actually took effect over the wire
    rather than silently defaulting to CRS84.
    """
    key = os.environ.get("OS_NGD_KEY")
    if not key:
        pytest.skip(
            "OS_NGD_KEY is not set; this live NGD pull needs the dev-mode "
            "Premium key with the NGD Features API added"
        )

    client = NgdClient(key=key)
    client.verify_collections([BUILDING_COLLECTION])

    features, pages = client.items(BUILDING_COLLECTION, _COWBRIDGE_BUILDING_BBOX, max_pages=1)

    assert pages >= 1
    assert len(features) >= 1
    node = features[0]["geometry"]["coordinates"]
    while isinstance(node[0], (list, tuple)):
        node = node[0]
    first_easting = node[0]
    assert 310000 <= first_easting <= 320000, (
        f"expected a BNG easting in 310000-320000, got {first_easting!r}; "
        f"crs/bbox-crs may not have taken effect"
    )
