import pytest
import requests

from mapgen.geo import BBox, Tile
from mapgen.sources.base import (
    FAILURE_TIMEOUT,
    FAILURE_UNKNOWN,
    FAILURE_UNREACHABLE,
    RETRYABLE_FAILURE_KINDS,
    DuplicateSourceError,
    Estimate,
    NullProgress,
    UnknownSourceError,
    available_sources,
    classify_status_failure,
    classify_transport_failure,
    clear_registry,
    get_source,
    parse_retry_after,
    register,
)
from mapgen.sources.osm import retry_delay_seconds


class FakeSource:
    display_name = "Fake Source"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self, id="fake"):
        self.id = id

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=1024 * len(tiles), seconds_estimate=2.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            progress.emit("tile_done", source="fake", tile_id=tile.tile_id)
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / "fake.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _tile(tile_id, col=0):
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    return Tile(tile_id=tile_id, row=0, col=col, core_bbox=bbox, query_bbox=bbox)


def test_registry_starts_empty():
    assert available_sources() == []


def test_register_then_get_returns_the_same_object():
    source = FakeSource()
    register(source)
    assert get_source("fake") is source


def test_available_sources_lists_registered_sources():
    register(FakeSource())
    assert [s.id for s in available_sources()] == ["fake"]


def test_get_source_raises_a_named_error_for_an_unknown_id():
    with pytest.raises(UnknownSourceError, match="nope"):
        get_source("nope")


def test_unknown_source_error_lists_what_is_available():
    register(FakeSource())
    with pytest.raises(UnknownSourceError, match="fake"):
        get_source("nope")


def test_registering_a_duplicate_id_raises_an_error():
    register(FakeSource(id="fake"))
    replacement = FakeSource(id="fake")
    with pytest.raises(DuplicateSourceError, match="'fake'"):
        register(replacement)
    assert get_source("fake") is not replacement
    assert len(available_sources()) == 1


def test_null_progress_accepts_any_event_without_error():
    NullProgress().emit("anything", a=1, b="two")


def test_a_conforming_source_round_trips_through_the_protocol(tmp_path):
    source = FakeSource()
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    tiles = [_tile("r00_c00", 0), _tile("r00_c01", 1)]

    assert source.estimate(bbox, tiles).bytes_estimate == 2048

    parts = source.fetch(bbox, tiles, tmp_path / "work", NullProgress())
    assert len(parts) == 2

    outputs = source.merge(parts, tmp_path / "out", "test-stem")
    assert outputs[0].read_text(encoding="utf-8") == "r00_c00\nr00_c01"


def test_progress_sink_receives_emitted_events(tmp_path):
    events = []

    class Recorder:
        def emit(self, event, **fields):
            events.append((event, fields))

    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    FakeSource().fetch(bbox, [_tile("r00_c00")], tmp_path / "work", Recorder())

    assert events == [("tile_done", {"source": "fake", "tile_id": "r00_c00"})]


def test_registry_isolation_proves_clear_registry_runs():
    source = FakeSource(id="unique_id_only_in_this_test")
    register(source)
    assert available_sources() == [source]


# --- Task 32: one classifier, shared by all three sources ------------------
#
# Task 30 wrote classify_transport_failure and classify_status_failure in
# osm.py, because OSM was the only source that produced a per-tile reason.
# Task 32 gave elevation and Overture the same need. Two copies of "what
# does a 429 mean" is how one of them quietly stops matching the retry
# policy that reads the answer, so there is one copy and it lives beside
# the vocabulary it returns.


def test_the_classifiers_are_still_importable_from_osm():
    # Moving them must not break an importer. The re-export in osm.py is
    # what keeps mapgen.sources.osm.classify_status_failure working, and
    # it must be the SAME function, not a second definition that has
    # drifted.
    from mapgen.sources import osm
    from mapgen.sources import base

    assert osm.classify_status_failure is base.classify_status_failure
    assert osm.classify_transport_failure is base.classify_transport_failure


def test_a_rate_limit_is_retryable_and_an_unauthorised_request_is_not():
    # The split the whole retry policy rests on, asserted at the one place
    # that decides it. 429 is a 4xx and is retryable; 401 is a 4xx and is
    # never retryable, which is elevation's most common failure by a wide
    # margin.
    from mapgen.package import RETRYABLE_FAILURE_KINDS

    rate_limited, _ = classify_status_failure(429)
    unauthorised, _ = classify_status_failure(401)
    forbidden, _ = classify_status_failure(403)
    server_error, _ = classify_status_failure(503)
    bad_request, _ = classify_status_failure(400)

    assert rate_limited in RETRYABLE_FAILURE_KINDS
    assert server_error in RETRYABLE_FAILURE_KINDS
    assert unauthorised not in RETRYABLE_FAILURE_KINDS
    assert forbidden not in RETRYABLE_FAILURE_KINDS
    assert bad_request not in RETRYABLE_FAILURE_KINDS


@pytest.mark.parametrize(
    "exception, expected_kind",
    [
        pytest.param(
            requests.exceptions.ConnectionError(
                "Max retries exceeded with url: https://x/?API_Key=sk-secret"
            ),
            FAILURE_UNREACHABLE,
            id="connection_error",
        ),
        pytest.param(
            requests.exceptions.ReadTimeout(
                "Read timed out for url: https://x/?API_Key=sk-secret"
            ),
            FAILURE_TIMEOUT,
            id="read_timeout",
        ),
        pytest.param(
            OSError("socket died talking to https://x/?API_Key=sk-secret"),
            FAILURE_UNREACHABLE,
            id="os_error",
        ),
        pytest.param(
            ValueError("boom at https://x/?API_Key=sk-secret"),
            FAILURE_UNKNOWN,
            id="unrecognised",
        ),
    ],
)
def test_a_transport_failure_is_classified_by_type_never_by_its_message(
    exception, expected_kind
):
    # The message is where a URL lives and a URL is where an API key
    # lives. A classifier shared by the keyed source cannot read one, and
    # EVERY branch of it has to hold that line, not only the branch that
    # happened to be written first: elevation composes its recorded
    # reason out of this phrase, and that reason reaches survey.json.
    #
    # Parametrised for exactly that reason. A single test that checked
    # only the connection-error branch let a mutation putting str(exc)
    # into the TIMEOUT branch survive it.
    kind, phrase = classify_transport_failure(exception)

    assert kind == expected_kind
    assert "sk-secret" not in phrase
    assert "API_Key" not in phrase
    assert "://" not in phrase
    assert "=" not in phrase


def test_an_unrecognised_exception_contributes_its_class_name_and_nothing_else():
    unrecognised, phrase = classify_transport_failure(ValueError("boom"))
    assert unrecognised == FAILURE_UNKNOWN
    assert phrase == "failed with ValueError"


def test_every_requests_exception_lands_on_a_kind_this_project_chose():
    """The unknown branch is unreachable for anything requests can raise,
    and that is a fact about the library rather than about this function:
    RequestException subclasses OSError, so the clause above absorbs the
    lot, including the ones that are not connection failures.

    Pinned rather than left to be rediscovered. The consequence is which
    failures get a second attempt, decided by RETRYABLE_FAILURE_KINDS off
    these kinds, so a reorder of those isinstance clauses would quietly
    change the retry policy for five exception types at once. Here it is a
    failing assertion instead.
    """
    assert issubclass(requests.exceptions.RequestException, OSError)

    # Not connection failures, and described as one anyway: a second
    # attempt is what fixes a truncated body, and one wasted request is
    # what the rest cost. See classify_transport_failure on why the
    # sentence is what gives rather than the retry.
    answered_but_unusable = [
        requests.exceptions.TooManyRedirects("looped"),
        requests.exceptions.ChunkedEncodingError("cut short"),
        requests.exceptions.ContentDecodingError("bad gzip"),
        requests.exceptions.HTTPError("raised for status"),
        requests.exceptions.InvalidURL("no host"),
    ]
    for exc in answered_but_unusable:
        kind, phrase = classify_transport_failure(exc)
        assert (kind, phrase) == (FAILURE_UNREACHABLE, "could not be reached")
        assert kind in RETRYABLE_FAILURE_KINDS

    # And the timeout clause still wins over it, which is the ordering
    # that matters: ReadTimeout is a RequestException too.
    assert classify_transport_failure(requests.exceptions.ReadTimeout("slow")) == (
        FAILURE_TIMEOUT,
        "did not answer in time",
    )


def test_parse_retry_after_reads_the_seconds_form_and_nothing_else():
    assert parse_retry_after({"Retry-After": "42"}) == pytest.approx(42.0)
    assert parse_retry_after({"Retry-After": "0.5"}) == pytest.approx(0.5)
    # The HTTP-date form is deliberately not parsed: the caller's own
    # backoff is a better answer than a date read against the wrong clock.
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert parse_retry_after({}) is None
    assert parse_retry_after(None) is None


def test_a_negative_retry_after_can_never_reach_time_sleep():
    # time.sleep(-5) raises ValueError, so a malformed header used to be
    # able to end a whole run through osm.py's retry_delay_seconds.
    assert parse_retry_after({"Retry-After": "-5"}) == 0.0
    assert retry_delay_seconds({"Retry-After": "-5"}, attempt=1) == 0.0
