import logging
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote_plus

import pytest
import requests

from mapgen.geo import BBox, Tile
from mapgen.jobs import Cancelled
from mapgen.sources.base import NullProgress
from mapgen.sources.elevation import (
    DEFAULT_OPENTOPOGRAPHY_URL,
    REDACTION_PLACEHOLDER,
    ElevationError,
    ElevationSource,
    MissingApiKeyError,
    _ApiKeyRedactingFilter,
    _redact,
    _scrub_exception_chain,
    is_tiff,
    resolve_api_key,
)

TIFF_LITTLE_ENDIAN = b"II*\x00" + b"\x00" * 128
TIFF_BIG_ENDIAN = b"MM\x00*" + b"\x00" * 128
BBOX = BBox.parse("-3.29,51.38,-3.28,51.39")


class FakeStreamResponse:
    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def test_declares_that_it_needs_an_api_key():
    source = ElevationSource(api_key="k")
    assert source.id == "elevation"
    assert source.requires_api_key is True
    assert source.licence


def test_is_tiff_accepts_both_byte_orders():
    assert is_tiff(TIFF_LITTLE_ENDIAN)
    assert is_tiff(TIFF_BIG_ENDIAN)


def test_is_tiff_rejects_html():
    assert not is_tiff(b"<html><body>error</body></html>")


def test_resolve_api_key_prefers_the_explicit_value():
    assert resolve_api_key("explicit", {"OPENTOPOGRAPHY_API_KEY": "env"}) == "explicit"


def test_resolve_api_key_reads_the_primary_variable():
    assert resolve_api_key(None, {"OPENTOPOGRAPHY_API_KEY": "env"}) == "env"


def test_resolve_api_key_reads_the_legacy_variable():
    assert resolve_api_key(None, {"OPENTOPO_API_KEY": "legacy"}) == "legacy"


def test_resolve_api_key_prefers_primary_over_legacy_variable():
    assert (
        resolve_api_key(None, {"OPENTOPOGRAPHY_API_KEY": "primary", "OPENTOPO_API_KEY": "legacy"})
        == "primary"
    )


def test_resolve_api_key_names_the_variable_when_absent():
    with pytest.raises(MissingApiKeyError, match="OPENTOPOGRAPHY_API_KEY"):
        resolve_api_key(None, {})


# --- Task 18: configured (the interface's saved key) is the third,
# lowest-priority tier ------------------------------------------------
#
# The owner's ruling was explicit: "an environment variable still tak[es]
# precedence if one is set" over the saved config value. explicit already
# outranked the environment before this task and that is unchanged
# (test_resolve_api_key_prefers_the_explicit_value, above); configured
# slots in below the environment, not above it.


def test_resolve_api_key_uses_configured_when_explicit_and_environment_are_absent():
    assert resolve_api_key(None, {}, "from-config") == "from-config"


def test_resolve_api_key_environment_variable_still_wins_over_configured():
    assert (
        resolve_api_key(None, {"OPENTOPOGRAPHY_API_KEY": "from-env"}, "from-config")
        == "from-env"
    )


def test_resolve_api_key_explicit_still_wins_over_configured():
    assert resolve_api_key("from-explicit", {}, "from-config") == "from-explicit"


def test_resolve_api_key_names_the_variable_when_configured_is_also_empty():
    with pytest.raises(MissingApiKeyError, match="OPENTOPOGRAPHY_API_KEY"):
        resolve_api_key(None, {}, None)


def test_fetch_writes_a_single_tiff(tmp_path):
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    source = ElevationSource(api_key="k", session=session)
    paths = source.fetch(BBOX, [], tmp_path, NullProgress())
    assert len(paths) == 1
    # Task 28 put the model's own name into the file's name; see
    # elevation_models.work_file_name for the resume this keeps honest.
    assert paths[0].name == "elevation_COP30.tif"
    assert paths[0].read_bytes() == TIFF_LITTLE_ENDIAN


def test_fetch_sends_the_bbox_and_key_as_parameters(tmp_path):
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    ElevationSource(api_key="secret", session=session).fetch(
        BBOX, [], tmp_path, NullProgress()
    )
    params = session.calls[0][1]["params"]
    assert params["API_Key"] == "secret"
    assert params["demtype"] == "COP30"
    assert params["south"].startswith("51.38")
    assert params["north"].startswith("51.39")
    assert params["west"].startswith("-3.29")
    assert params["east"].startswith("-3.28")


def test_fetch_reuses_an_existing_tiff(tmp_path):
    (tmp_path / "elevation_COP30.tif").write_bytes(TIFF_LITTLE_ENDIAN)
    session = FakeSession(FakeStreamResponse([]))
    ElevationSource(api_key="k", session=session).fetch(BBOX, [], tmp_path, NullProgress())
    assert session.calls == []


# --- Task 22: fetch()'s optional `cancel` parameter -------------------
#
# Elevation has no per-tile loop (one whole-area request, see the module
# docstring), so the only meaningful checkpoint is before that single
# request starts.


def test_fetch_with_no_cancel_argument_behaves_exactly_as_before(tmp_path):
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    paths = ElevationSource(api_key="k", session=session).fetch(BBOX, [], tmp_path, NullProgress())
    assert paths[0].read_bytes() == TIFF_LITTLE_ENDIAN


def test_fetch_stops_before_the_request_when_already_cancelled(tmp_path):
    from mapgen.jobs import CancelToken

    token = CancelToken()
    token.cancel()
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    with pytest.raises(Cancelled):
        ElevationSource(api_key="k", session=session).fetch(
            BBOX, [], tmp_path, NullProgress(), cancel=token
        )
    assert session.calls == []


def test_fetch_handles_http_errors(tmp_path):
    session = FakeSession(FakeStreamResponse([b"Server error"], status_code=500))
    source = ElevationSource(api_key="k", session=session)
    with pytest.raises(ElevationError, match="HTTP 500"):
        source.fetch(BBOX, [], tmp_path, NullProgress())


def test_fetch_rejects_a_non_tiff_response_and_leaves_no_file(tmp_path):
    session = FakeSession(FakeStreamResponse([b"<html>rate limited</html>"]))
    source = ElevationSource(api_key="k", session=session)
    with pytest.raises(ElevationError, match="did not return a TIFF"):
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert not (tmp_path / "elevation_COP30.tif").exists()
    # Nothing at all, under any name: a check for one specific filename
    # would pass for free if the name ever changed again.
    assert list(tmp_path.iterdir()) == []


def test_fetch_without_a_key_fails_before_any_request(tmp_path, monkeypatch):
    # Task 18 added a third fallback tier: fetch() now also consults the
    # saved config file when explicit and the environment are both empty
    # (see _configured_key). Without pinning CONFIG_PATH to an empty tmp
    # location, "no key anywhere" would only be true by accident of
    # whatever ~/.mapgen/config.json happens to hold on whoever's machine
    # runs this, which is exactly the fragility this project's own test
    # culture has fixed elsewhere (test_web_server.py's config tests all
    # pin CONFIG_PATH for the same reason).
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "no-such-config.json")
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    source = ElevationSource(api_key=None, session=session, environ={})
    with pytest.raises(MissingApiKeyError):
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert session.calls == []


def test_fetch_uses_the_configured_key_when_no_explicit_or_env_key_exists(tmp_path, monkeypatch):
    from mapgen.config import Config, save_config

    config_path = tmp_path / "config.json"
    save_config(Config(opentopography_api_key="from-settings"), config_path)
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", config_path)

    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    ElevationSource(api_key=None, session=session, environ={}).fetch(
        BBOX, [], tmp_path, NullProgress()
    )
    assert session.calls[0][1]["params"]["API_Key"] == "from-settings"


def test_fetch_prefers_the_environment_over_the_configured_key(tmp_path, monkeypatch):
    from mapgen.config import Config, save_config

    config_path = tmp_path / "config.json"
    save_config(Config(opentopography_api_key="from-settings"), config_path)
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", config_path)

    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    ElevationSource(
        api_key=None, session=session, environ={"OPENTOPOGRAPHY_API_KEY": "from-env"}
    ).fetch(BBOX, [], tmp_path, NullProgress())
    assert session.calls[0][1]["params"]["API_Key"] == "from-env"


# --- Review round 1: the key must not leak through ANY exception shape,
# not only HTTPError ------------------------------------------------------
#
# The round 1 finding: the previous fix wrapped only raise_for_status()
# and caught only (RuntimeError, requests.exceptions.HTTPError).
# session.get() itself sat outside that try, so a connection failure, a
# read timeout, or a mid-stream drop, none of which are HTTPError, all
# raised past the redaction entirely and reached run_survey's
# source_failed event and JobManager's record.error with the real key
# still in the message: the reviewer proved this end to end through a
# real server. The fix now wraps the whole request/response cycle in one
# broad except, and redacts by substring rather than by exception type, so
# it holds regardless of what raises. Six scenarios below, one per named
# failure path, each asserting the secret's ABSENCE directly rather than
# checking message wording, which is what a redaction fix must actually
# prove.

SECRET = "sk-real-secret-should-never-leak"
LEAKY_URL = f"https://portal.opentopography.org/API/globaldem?demtype=COP30&API_Key={SECRET}"


class ConnectFailureSession:
    """A session whose get() raises before any response exists at all, the
    way a real requests.Session does on a refused or unreachable
    connection. The exception message embeds the full request URL,
    exactly like a genuine requests.exceptions.ConnectionError does.
    """

    def __init__(self, exception_cls):
        self._exception_cls = exception_cls

    def get(self, url, **kwargs):
        raise self._exception_cls(
            f"HTTPSConnectionPool(host='portal.opentopography.org', port=443): "
            f"Max retries exceeded with url: {LEAKY_URL}"
        )


class MidStreamDropResponse(FakeStreamResponse):
    """Yields one real chunk, then raises mid-stream, the way a dropped
    connection during a large download actually behaves: some bytes
    already delivered, then a ChunkedEncodingError with the request URL
    embedded in its own message, as a genuine one would.
    """

    def __init__(self):
        super().__init__([], status_code=200)

    def iter_content(self, chunk_size=None):
        def generator():
            yield b"partial-tiff-bytes-before-the-drop"
            raise requests.exceptions.ChunkedEncodingError(
                f"Connection broken: InvalidChunkLength(got length b'', 0 bytes read) url: {LEAKY_URL}"
            )

        return generator()


def test_fetch_never_leaks_the_key_on_a_connect_failure(tmp_path):
    session = ConnectFailureSession(requests.exceptions.ConnectionError)
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert SECRET not in str(excinfo.value)


def test_fetch_never_leaks_the_key_on_a_read_timeout(tmp_path):
    session = ConnectFailureSession(requests.exceptions.ReadTimeout)
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert SECRET not in str(excinfo.value)


def test_fetch_never_leaks_the_key_on_a_mid_stream_drop(tmp_path):
    session = FakeSession(MidStreamDropResponse())
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert SECRET not in str(excinfo.value)


def test_fetch_never_leaks_the_key_on_a_401(tmp_path):
    session = FakeSession(FakeStreamResponse([], status_code=401))
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert SECRET not in str(excinfo.value)
    assert "401" in str(excinfo.value)


def test_fetch_never_leaks_the_key_on_a_500(tmp_path):
    session = FakeSession(FakeStreamResponse([], status_code=500))
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert SECRET not in str(excinfo.value)
    assert "500" in str(excinfo.value)


def test_fetch_never_leaks_the_key_in_a_non_tiff_200_body(tmp_path):
    # elevation.py's own non-TIFF preview dumps 300 bytes of the response
    # body verbatim. A misconfigured gateway or an API error page that
    # echoes the request URL back (a real, common shape for this kind of
    # failure) would otherwise put the key in that preview.
    leaky_body = f"<html>Bad request: {LEAKY_URL}</html>".encode("utf-8")
    session = FakeSession(FakeStreamResponse([leaky_body], status_code=200))
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert SECRET not in str(excinfo.value)


def test_fetch_redaction_also_catches_the_url_encoded_form_of_the_key(tmp_path):
    # A key containing characters that need percent-encoding (+, /, =, as
    # real base64-shaped tokens sometimes do) would appear URL-encoded
    # inside a requests exception's message, not verbatim: a redaction
    # that only stripped the raw string would miss it.
    from urllib.parse import quote

    secret_with_special_chars = "sk-real/secret+with=chars"
    encoded_secret = quote(secret_with_special_chars, safe="")

    class EncodedLeakSession:
        def get(self, url, **kwargs):
            leaky_url = f"{url}?API_Key={encoded_secret}&demtype=COP30"
            raise requests.exceptions.ConnectionError(
                f"Max retries exceeded with url: {leaky_url}"
            )

    source = ElevationSource(api_key=secret_with_special_chars, session=EncodedLeakSession())
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    message = str(excinfo.value)
    assert secret_with_special_chars not in message
    assert encoded_secret not in message


# --- Review round 2: the redacted MESSAGE was not the whole leak surface.
# `raise ... from exc` kept the ORIGINAL, unredacted exception attached as
# __cause__, one uncaught exception away from a full traceback showing the
# real key (confirmed against a real CLI run, see test_cli.py's own round 2
# test). Fixed by scrubbing exc's own args (and anything already chained to
# it) in place, then cutting the new exception loose with `from None`, so
# a standard traceback does not show the chain at all, and even code that
# reads __cause__/__context__ directly finds it already clean either way.


def test_fetch_cuts_the_new_exception_loose_from_its_cause(tmp_path):
    session = ConnectFailureSession(requests.exceptions.ConnectionError)
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_fetch_scrubs_the_original_exception_even_if_suppression_is_bypassed(tmp_path):
    # `from None` is what stops a STANDARD traceback from showing the
    # chain at all, via __suppress_context__. This proves the second,
    # independent layer: __context__ is still set internally (that is how
    # Python's runtime records "raised while handling another exception",
    # regardless of any `from` clause), so code that reads it directly,
    # bypassing the suppression flag entirely, must still find an
    # already-scrubbed exception there, not the raw one.
    session = ConnectFailureSession(requests.exceptions.ConnectionError)
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    chained = excinfo.value.__context__
    assert chained is not None, "expected the original exception still present as __context__"
    assert SECRET not in str(chained)
    assert all(SECRET not in arg for arg in chained.args if isinstance(arg, str))


def test_fetch_rendered_traceback_never_contains_the_key(tmp_path):
    # The exact check the review applied: not just str(exception), but
    # what traceback.format_exception actually produces, since that is
    # what an uncaught exception, logging.exception, or
    # traceback.format_exc() would show a human.
    session = ConnectFailureSession(requests.exceptions.ConnectionError)
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    rendered = "".join(traceback.format_exception(excinfo.type, excinfo.value, excinfo.tb))
    assert SECRET not in rendered, f"the key appeared in a rendered traceback: {rendered}"
    # Proves the chain is genuinely suppressed, not merely short: a
    # suppressed chain and a chain that never existed both satisfy "the
    # secret is absent" equally well by that assertion alone.
    assert "direct cause" not in rendered
    assert "another exception occurred" not in rendered


def test_fetch_preview_redacts_the_full_body_before_truncating_for_display(tmp_path):
    # Review round 2's own example: enough padding ahead of "API_Key="
    # pushes part of the key past a fixed-size truncation window, so
    # truncating BEFORE redacting leaves a partial, unredacted fragment
    # that still reads as most of the key. 280 bytes of filler plus
    # "API_Key=" (8 bytes) starts the key at byte 288; redacting only
    # payload[:300] would see just the first 12 characters of this
    # 33-character key, which is not a match for a whole-string
    # substitution and so is never touched.
    assert len(SECRET) > 20, "the test needs a key long enough to be split by the padding"
    padding = b"x" * 280
    leaky_body = padding + f"API_Key={SECRET}".encode("utf-8")
    session = FakeSession(FakeStreamResponse([leaky_body], status_code=200))
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    message = str(excinfo.value)
    assert SECRET not in message
    # The real property: no fragment of the key survives either. A bug
    # that truncated to exactly the first N bytes before redacting would
    # otherwise pass a bare "SECRET not in message" check by accident,
    # since the FULL secret genuinely never appears, only part of it.
    assert SECRET[:12] not in message, f"a partial key fragment survived: {message}"


def test_redact_strips_a_case_shifted_key():
    # str.replace is case-sensitive; nothing guarantees a key survives in
    # its original case by the time it reaches this function (a proxy or
    # gateway can title-case a header, for instance).
    text = f"failed: API_Key={SECRET.upper()} was rejected"
    redacted = _redact(text, SECRET)
    assert SECRET.upper() not in redacted
    assert "[REDACTED]" in redacted


def test_redact_withholds_text_when_the_key_survives_only_nul_interspersed():
    # A UTF-16 error page decoded with the UTF-8 codec (errors="replace")
    # does not raise: every byte of ASCII-range UTF-16LE text is
    # independently valid UTF-8 on its own, so it decodes to the original
    # characters each followed by a stray NUL. A human reading this (a
    # browser or a terminal renders NUL as invisible) sees the key in
    # plain sight; a plain substring search does not, since there is no
    # contiguous match.
    nul_interspersed = "".join(ch + "\x00" for ch in SECRET)
    text = f"error page: {nul_interspersed} appeared in the response"
    redacted = _redact(text, SECRET)
    assert SECRET not in redacted
    # The real property: nothing readable survives, not merely that the
    # exact original substring is no longer contiguous.
    assert SECRET not in redacted.replace("\x00", "")
    assert "withheld" in redacted.lower()


def test_redact_leaves_ordinary_text_alone_when_the_key_is_absent():
    text = "a perfectly ordinary error message with no secret in it"
    assert _redact(text, SECRET) == text


def test_scrub_exception_chain_redacts_every_link_in_a_real_chain():
    try:
        try:
            raise ValueError(f"root cause with {SECRET} embedded")
        except ValueError as root:
            raise RuntimeError(f"middle layer also has {SECRET}") from root
    except RuntimeError as top:
        _scrub_exception_chain(top, SECRET)
        assert SECRET not in str(top)
        assert SECRET not in str(top.__cause__)


def test_fetch_lets_cancelled_propagate_unwrapped(tmp_path):
    # The broad except Exception in fetch() would otherwise convert this
    # project's own cooperative-cancellation signal into an ElevationError.
    # Nothing calls anything that raises Cancelled inside that block
    # today; this pins that it would still propagate correctly if
    # something ever did, by construction, not by the accident of what
    # is called there now.
    #
    # There is deliberately no KeyboardInterrupt counterpart to this
    # test. Review round 3 called the previous one vacuous and it was
    # right to: KeyboardInterrupt is a BaseException, not an Exception,
    # so `except Exception` never catches it regardless of whether it is
    # also named in the tuple above it, in this code or in any code.
    # That test could not have failed for any change made to this
    # method; Cancelled is the real case, because it IS an Exception
    # subclass and so genuinely is at risk from the broad except below.
    class CancelsSession:
        def get(self, url, **kwargs):
            raise Cancelled("job was cancelled mid-request")

    source = ElevationSource(api_key="k", session=CancelsSession())
    with pytest.raises(Cancelled):
        source.fetch(BBOX, [], tmp_path, NullProgress())


def test_readiness_problem_is_none_when_a_key_is_available():
    assert ElevationSource(api_key="k").readiness_problem() is None


def test_readiness_problem_names_the_problem_when_no_key_is_available(monkeypatch, tmp_path):
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "no-such-config.json")
    source = ElevationSource(api_key=None, environ={})
    problem = source.readiness_problem()
    assert problem is not None
    assert "API key" in problem


def test_readiness_problem_never_raises_even_though_resolve_api_key_does(monkeypatch, tmp_path):
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "no-such-config.json")
    source = ElevationSource(api_key=None, environ={})
    # Must return a string, not propagate MissingApiKeyError: estimate_survey
    # calls this for every selected source and must not have the whole
    # estimate blow up just because one optional layer is unconfigured.
    assert isinstance(source.readiness_problem(), str)


def test_merge_copies_the_tiff_into_out_dir_named_after_the_stem(tmp_path):
    # Task 20 finding 2: merge() used to return parts unchanged, so the DEM
    # kept a fixed, unidentified "elevation.tif" and never left work_dir,
    # which a complete run then deletes (see run_survey). Now it is copied
    # into the finished package under the package stem, the same fix
    # applied consistently to OsmSource and OvertureSource.
    part = tmp_path / "elevation_COP30.tif"
    part.write_bytes(TIFF_LITTLE_ENDIAN)
    out_dir = tmp_path / "out"
    outputs = ElevationSource(api_key="k").merge([part], out_dir, "Barry-Waterfront_2026-08-01")
    assert [p.name for p in outputs] == ["Barry-Waterfront_2026-08-01.tif"]
    assert outputs[0].parent == out_dir
    assert outputs[0].read_bytes() == TIFF_LITTLE_ENDIAN


def test_merge_of_no_parts_produces_no_output(tmp_path):
    assert ElevationSource(api_key="k").merge([], tmp_path / "out", "Barry-Waterfront_2026-08-01") == []


# --- Task 28: the model is a choice, and the package must say which -----


def test_fetch_sends_the_chosen_model_as_the_demtype_parameter(tmp_path):
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    ElevationSource(api_key="k", demtype="EU_DTM", session=session).fetch(
        BBOX, [], tmp_path, NullProgress()
    )
    assert session.calls[0][1]["params"]["demtype"] == "EU_DTM"


def test_fetch_writes_each_model_under_its_own_name(tmp_path):
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    paths = ElevationSource(api_key="k", demtype="EU_DTM", session=session).fetch(
        BBOX, [], tmp_path, NullProgress()
    )
    assert paths[0].name == "elevation_EU_DTM.tif"


def test_fetch_does_not_reuse_a_different_models_download(tmp_path):
    # The exact resume this naming exists to stop being a lie: a stopped
    # COP30 run, the model changed in Settings, then Download again over
    # the same extent at the same tiling. work_dir is fingerprinted by
    # TILING, not by model, so the earlier file really is sitting there. A
    # fixed "elevation.tif" made that a skip, and survey.json would then
    # have recorded EU_DTM for a Copernicus surface model on disk.
    (tmp_path / "elevation_COP30.tif").write_bytes(TIFF_LITTLE_ENDIAN)
    session = FakeSession(FakeStreamResponse([TIFF_BIG_ENDIAN]))
    paths = ElevationSource(api_key="k", demtype="EU_DTM", session=session).fetch(
        BBOX, [], tmp_path, NullProgress()
    )
    assert len(session.calls) == 1, "the other model's file was treated as this one's"
    assert paths[0].name == "elevation_EU_DTM.tif"
    assert paths[0].read_bytes() == TIFF_BIG_ENDIAN
    # And the earlier model's file is left exactly as it was, so a run
    # that goes back to COP30 still resumes rather than refetching.
    assert (tmp_path / "elevation_COP30.tif").read_bytes() == TIFF_LITTLE_ENDIAN


def test_merge_takes_this_models_part_and_never_a_leftover(tmp_path):
    # package.py hands merge() everything it finds in the source's work
    # directory (see _existing_output_files), which on the resume above is
    # both files. parts[0] would sort to the leftover COP30 one.
    leftover = tmp_path / "elevation_COP30.tif"
    leftover.write_bytes(TIFF_LITTLE_ENDIAN)
    current = tmp_path / "elevation_EU_DTM.tif"
    current.write_bytes(TIFF_BIG_ENDIAN)
    out_dir = tmp_path / "out"

    outputs = ElevationSource(api_key="k", demtype="EU_DTM").merge(
        [leftover, current], out_dir, "Barry-Waterfront_2026-08-01"
    )

    assert [p.name for p in outputs] == ["Barry-Waterfront_2026-08-01.tif"]
    assert outputs[0].read_bytes() == TIFF_BIG_ENDIAN


def test_merge_produces_nothing_when_only_another_models_part_is_present(tmp_path):
    # Reachable only on a forced run whose own elevation fetch failed with
    # an older model's file lying about. A package with no DEM and a
    # survey.json that says so beats one holding a DEM it does not name.
    leftover = tmp_path / "elevation_COP30.tif"
    leftover.write_bytes(TIFF_LITTLE_ENDIAN)
    outputs = ElevationSource(api_key="k", demtype="EU_DTM").merge(
        [leftover], tmp_path / "out", "Barry-Waterfront_2026-08-01"
    )
    assert outputs == []


def test_the_registered_source_names_no_model_at_all():
    # GET /api/sources and `mapgen sources` read this off the one instance
    # registered at startup, whose own demtype is not what any request
    # will use. Naming COP30 here would contradict a settings panel
    # showing EU_DTM, in the layer checklist, permanently.
    assert ElevationSource.display_name == "Elevation (OpenTopography)"
    assert "COP30" not in ElevationSource().display_name


def test_configure_names_the_model_the_request_actually_asked_for():
    configured = ElevationSource(api_key="k").configure("EU_DTM")
    assert configured.demtype == "EU_DTM"
    assert configured.display_name == "Elevation (OpenTopography EU_DTM)"


def test_configure_keeps_the_subclass_it_was_called_on():
    # Review finding N1, and this is the source the reviewer was actually
    # bitten by: their first attempt at reproducing I8 subclassed
    # ElevationSource to make fetch() raise, configure() replaced it with
    # a plain ElevationSource, and the "double" made a real request to
    # OpenTopography with the saved key.
    class Subclassed(ElevationSource):
        pass

    configured = Subclassed(api_key="k").configure("EU_DTM")
    assert type(configured) is Subclassed
    assert configured.demtype == "EU_DTM"


def test_configure_never_mutates_the_registered_instance():
    # The reason OvertureSource.configure exists at all: /api/estimate has
    # no busy-guard, so it can be polled while a job using a different
    # selection is mid-fetch.
    registered = ElevationSource(api_key="k")
    registered.configure("SRTMGL1")
    assert registered.demtype == "COP30"
    assert registered.display_name == "Elevation (OpenTopography)"


def test_configure_shares_the_transport_rather_than_building_a_new_one():
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    registered = ElevationSource(
        api_key="k", session=session, url="http://example.invalid/dem", timeout_seconds=42
    )
    configured = registered.configure("NASADEM")
    assert configured.session is session
    assert configured.url == "http://example.invalid/dem"
    assert configured.timeout_seconds == 42


def test_licence_and_attribution_follow_the_chosen_model():
    # survey.json records both per source (see package._source_provenance).
    # A package that downloaded a JAXA or an OpenGeoHub product must not
    # claim Copernicus' copyright line.
    copernicus = ElevationSource(api_key="k", demtype="COP30")
    assert "Copernicus" in copernicus.licence
    assert "Airbus" in copernicus.attribution

    european = ElevationSource(api_key="k", demtype="EU_DTM")
    assert european.licence == "CC BY 4.0"
    assert "Hengl" in european.attribution
    assert "Airbus" not in european.attribution


def test_a_model_with_no_stated_licence_claims_none():
    srtm = ElevationSource(api_key="k", demtype="SRTMGL1")
    assert "No licence stated" in srtm.licence
    assert "Shuttle Radar" in srtm.attribution


def test_an_unrecognised_model_constructs_without_raising_and_claims_nothing():
    # Not a second validation site: SurveyRequest is where a person's
    # chosen model is refused (see mapgen.elevation_models).
    source = ElevationSource(api_key="k", demtype="SOMETHING_NEW")
    assert source.demtype == "SOMETHING_NEW"
    assert "Not established by mapgen" in source.licence


def test_estimate_sizes_the_download_by_the_models_own_resolution():
    # A large extent, well clear of SMALLEST_OBSERVED_BYTES, so the floor
    # is not what this is measuring. COP90 is three times the ground
    # sample distance of COP30, so a ninth of the pixels.
    big = BBox.parse("-4.0,51.0,-3.0,52.0")
    fine = ElevationSource(api_key="k", demtype="COP30").estimate(big, []).bytes_estimate
    coarse = ElevationSource(api_key="k", demtype="COP90").estimate(big, []).bytes_estimate
    assert coarse == pytest.approx(fine / 9.0, rel=0.01)


def test_estimate_scales_with_bbox_area():
    small_bbox = BBox.parse("-0.1,50.0,-0.05,50.05")
    large_bbox = BBox.parse("-10.0,40.0,0.0,50.0")

    source = ElevationSource(api_key="k")
    small_est = source.estimate(small_bbox, [])
    large_est = source.estimate(large_bbox, [])

    assert large_est.bytes_estimate > small_est.bytes_estimate
    assert large_est.seconds_estimate > small_est.seconds_estimate


def test_estimate_returns_positive_for_tiny_bbox():
    tiny_bbox = BBox.parse("-0.001,50.0,-0.0005,50.0005")
    source = ElevationSource(api_key="k")
    est = source.estimate(tiny_bbox, [])

    assert est.bytes_estimate > 0
    assert est.seconds_estimate > 0


# --- Task 25: both constants refitted from live measurement ------------


def test_the_time_estimate_matches_the_measured_floor():
    # Measured 2026-08-04 through ElevationSource.fetch against the live
    # OpenTopography API: 10.94s and 11.16s over 4.17 sq km, 11.26s and
    # 11.30s over 38.63, 11.58s and 11.60s over 259.60. Sixty-two times
    # the area for 1.04x the time, because what is being paid for is the
    # service's turnaround, not a transfer of at most a megabyte.
    #
    # The 5.0s this replaces was the only understatement in the whole
    # estimate panel. Every other source overstated. That asymmetry is
    # the reason it was worth fixing a six second error: a countdown
    # built on an under-estimate runs out while the download is still
    # going, which reads as a hang.
    source = ElevationSource(api_key="k")
    for spec in (
        "-3.2830,51.4000,-3.2530,51.4180",
        "-3.32,51.40,-3.22,51.45",
        "-3.3400,51.3600,-3.1000,51.5000",
    ):
        seconds = source.estimate(BBox.parse(spec), []).seconds_estimate
        assert 10.9 <= seconds <= 12.0, (
            f"every measured extent took 10.94s to 11.60s whatever its size; "
            f"{spec} is estimated at {seconds:.1f}s"
        )


def test_the_byte_estimate_matches_three_measured_downloads():
    # COP30 comes back as Float32, so 4 bytes per pixel, not the 16-bit
    # 2 bytes plus 20% that was here before and read 0.40x at the middle
    # extent. Measured file sizes, identical on both samples of each:
    #
    #      4.17 sq km      29,606 bytes
    #     38.63 sq km     254,479 bytes
    #    259.60 sq km   1,005,162 bytes
    #
    # The bands are wide because three points are three points, and
    # because compression against header overhead pulls in opposite
    # directions as the file grows: the smallest file is nearly all
    # header and the largest is nearly all data.
    source = ElevationSource(api_key="k")
    measured = {
        "-3.2830,51.4000,-3.2530,51.4180": 29_606,
        "-3.32,51.40,-3.22,51.45": 254_479,
        "-3.3400,51.3600,-3.1000,51.5000": 1_005_162,
    }
    for spec, actual in measured.items():
        estimate = source.estimate(BBox.parse(spec), []).bytes_estimate
        assert 0.6 <= estimate / actual <= 1.4, (
            f"{spec} downloaded {actual:,} bytes; the estimate says "
            f"{estimate:,}, which is {estimate / actual:.2f}x"
        )


# --- Review round 3: a third independent reviewer got the key out again,
# through a bare space in the key that requests encodes as "+" (quote_plus
# semantics), which the previous candidate list, quote() only, encoded as
# "%20" and so never matched. The reviewer's diagnosis was that value
# matching itself was the mistake, repeated for a third time under a new
# encoding each round: fixed by making a structural, parameter-name match
# ("API_Key=" and everything up to the next & or whitespace, regardless of
# its value) the PRIMARY defence, with value matching (now also including
# quote_plus) kept only as a secondary backstop for the one shape the
# parameter pattern cannot see: the key appearing somewhere not shaped like
# "API_Key=...". Four more findings outside the redaction function itself:
# urllib3 logs the full request line at DEBUG level with no exception
# involved at all; _scrub_exception_chain's own docstring falsely claimed
# completeness while leaving non-string carriers (.filename, .request.url,
# .response.url) untouched; api_key and params survived as frame locals
# inspectable by pytest --showlocals or Sentry even after the raised
# exception's own text and chain were clean; and the withhold safety net
# self-triggered on a secret that is a substring of the word "REDACTED"
# itself, by searching its own redacted output rather than the original.


def test_redact_parameter_pattern_catches_an_encoding_nobody_added_a_candidate_for():
    # The point of matching the parameter rather than the value: this
    # text carries a shape of "API_Key=..." nobody wrote a candidate
    # for (not secret, quote(secret), or quote_plus(secret) for ANY
    # secret, since it is not even a well-formed percent-encoding of
    # one), and the secret passed in does not even appear anywhere in
    # the text. It is still removed, because the primary defence never
    # looks at the secret's value to decide what to remove.
    text = (
        "GET /API/globaldem?demtype=COP30"
        "&API_Key=totally-unanticipated-shape%zz-not-a-real-encoding"
        "&outputFormat=GTiff HTTP/1.1"
    )
    redacted = _redact(text, "unrelated-secret-not-even-present-in-text")
    assert "totally-unanticipated-shape" not in redacted
    assert "API_Key=" + REDACTION_PLACEHOLDER in redacted
    # Proves the match stops at the next parameter rather than eating the
    # rest of the query string.
    assert "demtype=COP30" in redacted
    assert "outputFormat=GTiff" in redacted


def test_redact_strips_the_parameter_pattern_even_when_no_secret_is_known():
    # The primary defence does not need to know the secret's value at
    # all, so it must not be gated behind one being supplied: a caller
    # with no secret in hand (_ApiKeyRedactingFilter, below, is exactly
    # such a caller) must still get the same structural protection as a
    # caller that has one.
    text = "GET /API/globaldem?demtype=COP30&API_Key=whatever-this-is HTTP/1.1"
    redacted = _redact(text, None)
    assert "whatever-this-is" not in redacted
    assert "API_Key=" + REDACTION_PLACEHOLDER in redacted


def test_redact_backstop_catches_the_plus_encoded_form_of_a_trailing_space_key():
    # Isolates the SECONDARY (value-matching) backstop from the primary
    # parameter-pattern defence: this text does not contain "API_Key="
    # at all, so only the backstop's candidate list can catch it.
    # Round 3's own finding was that requests encodes a query-string
    # space as "+" (quote_plus semantics), which the old candidate
    # list, quote() only, encoded as "%20" and so never matched.
    secret_with_trailing_space = "sk-real-secret-with-trailing-space "
    encoded = quote_plus(secret_with_trailing_space)
    assert "+" in encoded, "the test needs quote_plus to actually produce a +"
    text = f"upstream rejected the token: {encoded}"
    redacted = _redact(text, secret_with_trailing_space)
    assert encoded not in redacted
    assert REDACTION_PLACEHOLDER in redacted


def test_fetch_never_leaks_a_trailing_space_key_encoded_by_a_real_prepared_request(tmp_path):
    # Every previous round's "never leaks" test hand-assembled the leaky
    # URL as an f-string. That is exactly how this bug hid for two
    # rounds: a hand-assembled URL always guesses an encoding, and every
    # guess so far happened to already be on the candidate list. This
    # one instead calls requests' own Request(...).prepare(), so
    # whatever encoding requests actually uses for a trailing space is
    # whatever ends up in the exception message, with no guessing at all.
    #
    # Review round 4's correction: this is the end-to-end proof that the
    # real-world scenario is fixed, and nothing more than that. It is NOT
    # a pin on either redaction layer individually, because "API_Key=...+"
    # is a shape BOTH layers independently recognise (the primary regex
    # because it starts with "API_Key=" regardless of what follows;
    # quote_plus(secret) because it is a byte-for-byte match), so this
    # test still passes with either layer disabled on its own and only
    # fails if both are gone at once. It reads like a pin on both layers
    # and is not one. The actual per-layer pins live elsewhere:
    # test_redact_parameter_pattern_catches_an_encoding_nobody_added_a_
    # candidate_for and test_redact_strips_the_parameter_pattern_even_
    # when_no_secret_is_known (plus, incidentally, both logging-filter
    # tests below, since the filter has no secret to fall back on) pin
    # the primary regex; test_redact_backstop_catches_the_plus_encoded_
    # form_of_a_trailing_space_key pins the quote_plus backstop on its
    # own, using text with no "API_Key=" in it at all so the primary
    # regex has nothing to catch. Confirmed by mutation: disabling the
    # primary regex fails four tests (the two above plus both logging
    # filter tests); disabling quote_plus fails exactly the one.
    secret_with_trailing_space = "sk-real-secret-with-trailing-space "
    prepared = requests.Request(
        "GET",
        DEFAULT_OPENTOPOGRAPHY_URL,
        params={"demtype": "COP30", "API_Key": secret_with_trailing_space},
    ).prepare()
    assert "+" in prepared.url, "needs requests' real space encoding for this test to be meaningful"

    class RealEncodingLeakSession:
        def get(self, url, **kwargs):
            raise requests.exceptions.ConnectionError(
                f"Max retries exceeded with url: {prepared.url}"
            )

    source = ElevationSource(api_key=secret_with_trailing_space, session=RealEncodingLeakSession())
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    message = str(excinfo.value)
    assert secret_with_trailing_space.strip() not in message
    assert "API_Key=" + REDACTION_PLACEHOLDER in message


def test_redact_does_not_self_trigger_withhold_on_a_short_common_letter_secret():
    # An earlier version of the withhold safety net searched its OWN
    # redacted output for the secret, rather than the original text. A
    # secret that is a common single letter is a case-insensitive
    # substring of the word "REDACTED" itself (the placeholder every
    # ordinary substitution inserts), so every ordinary redaction
    # produced output that still matched the candidate, and the safety
    # net destroyed a message that never contained the secret in any
    # unredacted form at all, and had no NUL bytes anywhere in it.
    secret = "a"
    text = "an ordinary message about a cat, no secret involved"
    redacted = _redact(text, secret)
    assert "withheld" not in redacted.lower(), (
        f"an innocent, NUL-free message was wrongly withheld: {redacted}"
    )


class _FakeUrlHolder:
    """Stands in for the parts of a requests PreparedRequest or Response
    that _scrub_exception_chain actually reads: nothing but a plain,
    settable .url string attribute.
    """

    def __init__(self, url):
        self.url = url


def test_scrub_exception_chain_redacts_oserror_filename_attributes():
    # requests' connection-level exceptions frequently wrap a raw
    # socket/OSError, whose own .filename/.filename2 have, in practice,
    # been used to carry the address or URL involved, entirely separate
    # from anything in .args.
    exc = OSError("connection failed")
    exc.filename = f"https://portal.opentopography.org/API/globaldem?API_Key={SECRET}"
    exc.filename2 = f"secondary path with {SECRET} embedded"
    _scrub_exception_chain(exc, SECRET)
    assert SECRET not in exc.filename
    assert SECRET not in exc.filename2


def test_scrub_exception_chain_redacts_request_and_response_url_attributes():
    # requests itself sets .request (a PreparedRequest) and .response (a
    # Response) on most of its own exception classes; each exposes a
    # plain .url string carrying the exact query string that was sent,
    # independent of whatever str(exc) says.
    exc = requests.exceptions.ConnectionError("connection reset")
    leaky_url = f"https://portal.opentopography.org/API/globaldem?API_Key={SECRET}"
    exc.request = _FakeUrlHolder(leaky_url)
    exc.response = _FakeUrlHolder(leaky_url)
    _scrub_exception_chain(exc, SECRET)
    assert SECRET not in exc.request.url
    assert SECRET not in exc.response.url


def test_scrub_exception_chain_redacts_url_attributes_through_the_whole_chain():
    # Not only the newly-raised exception: __cause__/__context__ can
    # themselves be genuine requests exceptions carrying their own
    # populated .request, and the walk must reach it too.
    root = requests.exceptions.ConnectionError("root connection error")
    root.request = _FakeUrlHolder(
        f"https://portal.opentopography.org/API/globaldem?API_Key={SECRET}"
    )
    try:
        try:
            raise root
        except requests.exceptions.ConnectionError as caught:
            raise ElevationError("wrapped") from caught
    except ElevationError as top:
        _scrub_exception_chain(top, SECRET)
        assert SECRET not in top.__cause__.request.url


def test_fetch_scrubs_request_url_attribute_on_the_original_exception(tmp_path):
    # Proves the fix through fetch() itself, not only _scrub_exception_chain
    # in isolation: a real ConnectionError carrying a populated .request,
    # the way requests' own exceptions actually do, must come out clean
    # on the chain fetch() leaves behind as __context__.
    class LeakyAttributeSession:
        def get(self, url, **kwargs):
            exc = requests.exceptions.ConnectionError("connection reset by peer")
            exc.request = _FakeUrlHolder(f"{url}?API_Key={SECRET}")
            raise exc

    source = ElevationSource(api_key=SECRET, session=LeakyAttributeSession())
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    original = excinfo.value.__context__
    assert original is not None, "expected the original exception still present as __context__"
    assert SECRET not in original.request.url


class _UrlAwareStreamResponse(FakeStreamResponse):
    """Like FakeStreamResponse, but with a real .url and .request.url, the
    way an actual requests.Response object has, to exercise the non-TIFF
    branch's own response.url redaction specifically.
    """

    def __init__(self, chunks, url, status_code=200):
        super().__init__(chunks, status_code=status_code)
        self.url = url
        self.request = _FakeUrlHolder(url)


def test_fetch_redacts_the_response_url_before_it_can_survive_as_a_frame_local(tmp_path):
    # response (and response.request) are still bound in fetch()'s frame
    # after the `with` block closes the connection; .url on each is the
    # literal request URL, API_Key included, and no exception exists in
    # this branch for _scrub_exception_chain to ever reach. If fetch()
    # did not scrub them in place, the SAME object this test holds a
    # reference to would still carry the raw key long after the call
    # returns, regardless of what the raised exception's own message says.
    leaky_url = f"https://portal.opentopography.org/API/globaldem?demtype=COP30&API_Key={SECRET}"
    response = _UrlAwareStreamResponse([b"<html>not a tiff</html>"], url=leaky_url)
    source = ElevationSource(api_key=SECRET, session=FakeSession(response))
    with pytest.raises(ElevationError):
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert SECRET not in response.url
    assert SECRET not in response.request.url


def test_fetch_does_not_leave_the_raw_key_in_any_frame_local_on_the_broad_except_path(tmp_path):
    # pytest --showlocals, Sentry's local-variable capture, and a
    # debugger's postmortem all read tb_frame.f_locals directly: none of
    # that goes through _redact or _scrub_exception_chain, which only
    # ever touch an exception's OWN text and attributes, never a frame's
    # local variables. api_key and params must not still be bound in
    # elevation.py's fetch() frame by the time the exception has
    # propagated out of it.
    session = ConnectFailureSession(requests.exceptions.ConnectionError)
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())

    tb = excinfo.tb
    checked_fetch_frame = False
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_code.co_name == "fetch":
            checked_fetch_frame = True
            for name, value in frame.f_locals.items():
                if isinstance(value, str):
                    assert SECRET not in value, f"local {name!r} in fetch() still holds the key"
                elif isinstance(value, dict):
                    assert not any(
                        isinstance(v, str) and SECRET in v for v in value.values()
                    ), f"local {name!r} in fetch() still holds the key in a dict value"
        tb = tb.tb_next
    assert checked_fetch_frame, "the traceback did not include elevation.py's fetch() frame"


def test_fetch_does_not_leave_the_raw_key_in_any_frame_local_on_the_non_tiff_path(tmp_path):
    leaky_body = f"<html>Bad request: API_Key={SECRET}</html>".encode("utf-8")
    session = FakeSession(FakeStreamResponse([leaky_body], status_code=200))
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())

    tb = excinfo.tb
    checked_fetch_frame = False
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_code.co_name == "fetch":
            checked_fetch_frame = True
            for name, value in frame.f_locals.items():
                if isinstance(value, str):
                    assert SECRET not in value, f"local {name!r} in fetch() still holds the key"
                elif isinstance(value, bytes):
                    assert SECRET.encode("utf-8") not in value, (
                        f"local {name!r} in fetch() still holds the key"
                    )
                elif isinstance(value, dict):
                    assert not any(
                        isinstance(v, str) and SECRET in v for v in value.values()
                    ), f"local {name!r} in fetch() still holds the key in a dict value"
        tb = tb.tb_next
    assert checked_fetch_frame, "the traceback did not include elevation.py's fetch() frame"


def test_resolve_api_key_strips_trailing_whitespace_from_the_explicit_value():
    assert resolve_api_key("explicit-key  \n", {}) == "explicit-key"


def test_resolve_api_key_strips_whitespace_from_environment_values():
    assert resolve_api_key(None, {"OPENTOPOGRAPHY_API_KEY": "  env-key\t"}) == "env-key"


def test_resolve_api_key_strips_whitespace_from_the_configured_value():
    assert resolve_api_key(None, {}, "  from-config  ") == "from-config"


def test_resolve_api_key_treats_an_all_whitespace_explicit_value_as_absent():
    # A blank-but-present explicit value must fall through to the next
    # tier, exactly as an unset one would, not win the precedence chain
    # with a value that resolves to nothing once trimmed.
    assert resolve_api_key("   ", {"OPENTOPOGRAPHY_API_KEY": "env-key"}) == "env-key"


def test_resolve_api_key_treats_an_all_whitespace_configured_value_as_a_missing_key():
    with pytest.raises(MissingApiKeyError):
        resolve_api_key(None, {}, "   ")


def test_api_key_redacting_filter_scrubs_a_record_in_place():
    # Fast, deterministic companion to the real-server test below: proves
    # the filter class itself, isolated from any actual networking.
    filter_ = _ApiKeyRedactingFilter()
    record = logging.LogRecord(
        name="urllib3.connectionpool",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg='%s "GET /API/globaldem?demtype=COP30&API_Key=%s HTTP/1.1" 200 None',
        args=("https://portal.opentopography.org:443", SECRET),
        exc_info=None,
    )
    result = filter_.filter(record)
    assert result is True
    assert SECRET not in record.getMessage()
    assert record.args == ()


def test_api_key_redacting_filter_is_installed_on_the_urllib3_connectionpool_logger():
    filters = logging.getLogger("urllib3.connectionpool").filters
    assert any(isinstance(f, _ApiKeyRedactingFilter) for f in filters)


class _OkHandler(BaseHTTPRequestHandler):
    """Answers every GET with a plain 200, deliberately quiet: the point
    of this server is only to make urllib3 log a real request line, not
    to exercise anything about the response.
    """

    def log_message(self, format, *args):  # noqa: A002 - matches BaseHTTPRequestHandler's own signature
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture
def local_http_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_urllib3_debug_logging_never_reveals_the_key(local_http_server, caplog):
    # Confirmed empirically before writing the fix: urllib3.connectionpool
    # logs the full request line, key included, at DEBUG level for every
    # request it makes, success or failure alike, entirely independent of
    # any exception. Nothing else in this module runs on this path at
    # all: _redact and _scrub_exception_chain only ever run once
    # something has already gone wrong, and here nothing does.
    real_key = "sk-real-secret-in-a-debug-log-line"
    with caplog.at_level(logging.DEBUG, logger="urllib3.connectionpool"):
        requests.get(local_http_server, params={"API_Key": real_key}, timeout=10)
    assert "API_Key=" in caplog.text, (
        "sanity check failed: urllib3 apparently did not log the request "
        "line at all, so this test cannot be proving the filter works"
    )
    assert real_key not in caplog.text, f"the key leaked into urllib3's own debug log: {caplog.text}"


# --- Review round 4: a fourth reviewer found the leak this module's own
# test file should already have caught. fetch() has three raise sites, not
# two: the status check used to raise from INSIDE the try block, where the
# bare `raise` in except (ElevationError, Cancelled, KeyboardInterrupt):
# re-raised it with api_key and params both still bound in this frame,
# uncleared. Round 3 added a frame-locals test for the broad-except path
# and one for the non-TIFF path, and none for this one, so the gap between
# "two raise sites are fixed" and "there are three" walked straight past
# three rounds of otherwise-thorough work. str(exc) was never affected
# (this path never builds a message from anything containing the key), so
# CLI stdout, CLI stderr and the browser's polled job record were never at
# risk; the exposure was always limited to pytest --showlocals, Sentry-style
# local capture and a postmortem debugger, which is exactly why three
# rounds of mutation-testing str(exc)/traceback text never surfaced it.
#
# Fixed by restructuring rather than by adding a fourth `del`: the status
# check now happens AFTER the try, in the same straight-line
# compute-then-delete-then-raise shape as the non-TIFF check beneath it,
# so it no longer hides inside a catch-and-reraise clause. See
# _redact_response_urls and the status_code handling in fetch() for the
# actual fix; the two tests below are its frame-locals proof, mirroring
# round 3's own two tests for the other raise sites exactly, so this class
# of gap cannot recur silently: any future raise site added to fetch()
# without a matching frame-locals test is now the odd one out against a
# pattern of three, not one exception alongside two.
#
# Round 4 also left behind a claim of its own, corrected in round 5: that
# the bare `raise` in except (ElevationError, Cancelled, KeyboardInterrupt)
# "cannot clean up on its way through", offered here as the reason that
# clause was left as the one raise site in fetch() with no del. That was
# never true. `del` removes a name from this frame's own locals; it does
# not touch the exception object a bare `raise` sends on its way, so nothing
# stops it running immediately before that `raise` the same as at the other
# three sites. Fixed there directly, and proven by the fourth frame-locals
# test below, completing the set: all four of fetch()'s raise sites now
# clear api_key and params first, and all four have a test that fails if
# one stops.


def test_fetch_redacts_the_response_url_on_a_bad_status_before_it_can_survive_as_a_frame_local(
    tmp_path,
):
    # Same property, and the same reason, as
    # test_fetch_redacts_the_response_url_before_it_can_survive_as_a_frame_local
    # above, for the status-check raise site instead of the non-TIFF one:
    # response (and response.request) are still bound in fetch()'s frame
    # after the `with` block closes the connection, and .url on each is
    # the literal request URL, API_Key included. If fetch() did not scrub
    # them in place, the SAME object this test holds a reference to would
    # still carry the raw key long after the call returns, regardless of
    # what the raised exception's own message says.
    leaky_url = f"https://portal.opentopography.org/API/globaldem?demtype=COP30&API_Key={SECRET}"
    response = _UrlAwareStreamResponse([], url=leaky_url, status_code=401)
    source = ElevationSource(api_key=SECRET, session=FakeSession(response))
    with pytest.raises(ElevationError, match="HTTP 401"):
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert SECRET not in response.url
    assert SECRET not in response.request.url


def test_fetch_does_not_leave_the_raw_key_in_any_frame_local_on_the_bad_status_path(tmp_path):
    # The missing third test review round 4 asked for, mirroring the
    # broad-except and non-TIFF versions above exactly. A 401 is the
    # scenario the coordinator singled out deliberately: a wrong or
    # expired key produces one on every single attempt, which makes this
    # the most frequently reached failure path in the whole module, not
    # an edge case.
    session = FakeSession(FakeStreamResponse([], status_code=401))
    source = ElevationSource(api_key=SECRET, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())

    tb = excinfo.tb
    checked_fetch_frame = False
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_code.co_name == "fetch":
            checked_fetch_frame = True
            for name, value in frame.f_locals.items():
                if isinstance(value, str):
                    assert SECRET not in value, f"local {name!r} in fetch() still holds the key"
                elif isinstance(value, dict):
                    assert not any(
                        isinstance(v, str) and SECRET in v for v in value.values()
                    ), f"local {name!r} in fetch() still holds the key in a dict value"
        tb = tb.tb_next
    assert checked_fetch_frame, "the traceback did not include elevation.py's fetch() frame"


def test_fetch_does_not_leave_the_raw_key_in_any_frame_local_on_the_cancelled_path(tmp_path):
    # The fourth and last raise site: the bare `raise` in
    # except (ElevationError, Cancelled, KeyboardInterrupt): below,
    # mirroring the three tests above exactly. Cancelled is the member of
    # that tuple this test can actually reach; see
    # test_fetch_lets_cancelled_propagate_unwrapped for why there is no
    # KeyboardInterrupt counterpart, and this file's own round-3 note for
    # why that omission does not weaken this test.
    class CancelsSession:
        def get(self, url, **kwargs):
            raise Cancelled("job was cancelled mid-request")

    source = ElevationSource(api_key=SECRET, session=CancelsSession())
    with pytest.raises(Cancelled) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())

    tb = excinfo.tb
    checked_fetch_frame = False
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_code.co_name == "fetch":
            checked_fetch_frame = True
            for name, value in frame.f_locals.items():
                if isinstance(value, str):
                    assert SECRET not in value, f"local {name!r} in fetch() still holds the key"
                elif isinstance(value, dict):
                    assert not any(
                        isinstance(v, str) and SECRET in v for v in value.values()
                    ), f"local {name!r} in fetch() still holds the key in a dict value"
        tb = tb.tb_next
    assert checked_fetch_frame, "the traceback did not include elevation.py's fetch() frame"


def test_possible_outputs_names_exactly_the_stem_tif_file():
    source = ElevationSource(api_key="k")
    assert source.possible_outputs("Stem_2026-08-01") == ["Stem_2026-08-01.tif"]


# --- Task 32: elevation says WHY it failed, so a transient one can be
# retried and a rejected key never is ---------------------------------------
#
# Before this, elevation failed as a whole layer with no per-tile cause,
# and package.py never retries a failure with no kind. So the one keyed
# source, whose service really does time out and really does rate limit,
# got exactly one attempt at everything, while its most common failure by
# a wide margin (a wrong or missing key) was indistinguishable from a
# service having a bad minute.
#
# Every test here asserts against the RECORDED failure, not against the
# raised exception, because the record is the thing that is new and it is
# the thing that reaches survey.json and the browser.

def _tile(tile_id, row=0, col=0):
    return Tile(tile_id=tile_id, row=row, col=col, core_bbox=BBOX, query_bbox=BBOX)


_TILES = [_tile("r00_c00")]


class HeaderedResponse(FakeStreamResponse):
    """A response that carries headers, which FakeStreamResponse does not.

    Deliberately a separate double rather than a header dict added to the
    existing one. Every test above was written against a response with no
    headers at all, and that is a real shape this module has to survive:
    it is what proves the happy path never reads a header it does not
    need. Retry-After needs the opposite, so it gets its own double and
    both shapes stay covered.
    """

    def __init__(self, chunks, status_code=200, headers=None):
        super().__init__(chunks, status_code=status_code)
        self.headers = dict(headers or {})


def test_a_successful_fetch_records_no_failure_at_all(tmp_path):
    source = ElevationSource(
        api_key="k", session=FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    )
    source.fetch(BBOX, _TILES, tmp_path, NullProgress())
    assert source.tile_failures == []


def test_a_rejected_key_is_recorded_as_not_authorised_and_never_retryable(tmp_path):
    from mapgen.package import RETRYABLE_FAILURE_KINDS

    source = ElevationSource(
        api_key=SECRET, session=FakeSession(FakeStreamResponse([], status_code=401))
    )
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())

    failure = source.tile_failures[0]
    assert failure.source == "elevation"
    assert failure.tile_id == "r00_c00"
    assert failure.kind == "not_authorised"
    assert failure.kind not in RETRYABLE_FAILURE_KINDS
    assert "401" in failure.reason


def test_a_forbidden_response_is_also_never_retryable(tmp_path):
    from mapgen.package import RETRYABLE_FAILURE_KINDS

    source = ElevationSource(
        api_key="k", session=FakeSession(FakeStreamResponse([], status_code=403))
    )
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())
    assert source.tile_failures[0].kind not in RETRYABLE_FAILURE_KINDS


def test_a_service_error_and_a_timeout_are_both_retryable(tmp_path):
    from mapgen.package import RETRYABLE_FAILURE_KINDS

    source = ElevationSource(
        api_key="k", session=FakeSession(FakeStreamResponse([], status_code=503))
    )
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())
    assert source.tile_failures[0].kind == "service_error"
    assert source.tile_failures[0].kind in RETRYABLE_FAILURE_KINDS

    timing_out = ElevationSource(
        api_key="k", session=ConnectFailureSession(requests.exceptions.ReadTimeout)
    )
    with pytest.raises(ElevationError):
        timing_out.fetch(BBOX, _TILES, tmp_path, NullProgress())
    assert timing_out.tile_failures[0].kind == "timeout"
    assert timing_out.tile_failures[0].kind in RETRYABLE_FAILURE_KINDS


def test_a_rate_limit_carries_the_period_the_service_asked_for(tmp_path):
    source = ElevationSource(
        api_key="k",
        session=FakeSession(
            HeaderedResponse([], status_code=429, headers={"Retry-After": "30"})
        ),
    )
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())

    failure = source.tile_failures[0]
    assert failure.kind == "rate_limited"
    assert failure.retry_after_seconds == pytest.approx(30.0)


def test_a_rate_limit_with_no_retry_after_header_carries_none(tmp_path):
    # The ordinary case, and the one that must not become a zero-second
    # wait dressed up as an instruction the service never gave.
    source = ElevationSource(
        api_key="k", session=FakeSession(HeaderedResponse([], status_code=429))
    )
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())
    assert source.tile_failures[0].kind == "rate_limited"
    assert source.tile_failures[0].retry_after_seconds is None


def test_a_missing_key_is_recorded_rather_than_left_unexplained(tmp_path, monkeypatch):
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "config.json")
    source = ElevationSource(api_key=None, environ={})
    with pytest.raises(MissingApiKeyError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())

    failure = source.tile_failures[0]
    assert failure.kind == "not_authorised"
    assert "portal.opentopography.org" in failure.reason


def test_a_non_tiff_response_is_recorded_as_unknown_and_never_retried(tmp_path):
    from mapgen.package import RETRYABLE_FAILURE_KINDS

    source = ElevationSource(
        api_key="k", session=FakeSession(FakeStreamResponse([b"<html>nope</html>"]))
    )
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())
    assert source.tile_failures[0].kind == "unknown"
    assert source.tile_failures[0].kind not in RETRYABLE_FAILURE_KINDS


def test_a_stop_records_no_failure_because_a_stop_is_not_a_failure(tmp_path):
    from mapgen.jobs import CancelToken

    token = CancelToken()
    token.cancel()
    source = ElevationSource(
        api_key="k", session=FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    )
    with pytest.raises(Cancelled):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress(), cancel=token)
    assert source.tile_failures == []


def test_the_record_is_reset_by_the_next_fetch_and_never_accumulates(tmp_path):
    # The convention sources/base.py documents. A retry that succeeds must
    # not leave the previous attempt's reason attached to a layer that is
    # now on disk.
    source = ElevationSource(
        api_key="k", session=FakeSession(FakeStreamResponse([], status_code=503))
    )
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())
    assert len(source.tile_failures) == 1

    source.session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    source.fetch(BBOX, _TILES, tmp_path, NullProgress())
    assert source.tile_failures == []


def test_one_failure_is_recorded_against_every_tile_it_covers(tmp_path):
    # Elevation makes one whole-area request, so a failure is a failure
    # for every tile equally. This is what lets package.py's retry pass,
    # which is written in tiles, reach a source that has none.
    tiles = [
        _tile(f"r{r:02d}_c{c:02d}", row=r, col=c) for r in range(2) for c in range(2)
    ]
    source = ElevationSource(
        api_key="k", session=FakeSession(FakeStreamResponse([], status_code=503))
    )
    with pytest.raises(ElevationError):
        source.fetch(BBOX, tiles, tmp_path, NullProgress())

    assert [f.tile_id for f in source.tile_failures] == [
        "r00_c00", "r00_c01", "r01_c00", "r01_c01"
    ]
    assert len({f.reason for f in source.tile_failures}) == 1


# --- The proof the brief asks for: a key cannot reach a recorded failure ---
#
# The redaction work in this module took four rounds and four adversarial
# reviews, and every round closed a path nobody had thought of, including
# one through an exception chain and one through requests encoding a space
# as "+". A recorded failure is a NEW path out of this module: it goes to
# survey.json, which is a file that outlives the run, and to the tile_failed
# event, which goes to the browser.
#
# It is closed structurally rather than by redaction. The reason is
# composed from the shared failure vocabulary plus an HTTP status and
# nothing else, so there is no value in it for a redaction to have to
# catch. These tests hold that line: every failure path, with a key that
# is deliberately present in the URL, in the exception text and in the
# response body.


class BodyEchoesTheKeySession:
    """A service that quotes the key back inside its own error body, which
    is the shape _redact's value-matching backstop exists for. If a reason
    were ever composed from the response, this is what would land in
    survey.json."""

    def __init__(self, status_code=200):
        self._status_code = status_code

    def get(self, url, **kwargs):
        body = f"<html>Bad key: {SECRET} for {LEAKY_URL}</html>".encode("utf-8")
        return HeaderedResponse([body], status_code=self._status_code)


@pytest.mark.parametrize(
    "session_factory",
    [
        pytest.param(
            lambda: ConnectFailureSession(requests.exceptions.ConnectionError),
            id="connect_failure",
        ),
        pytest.param(
            lambda: ConnectFailureSession(requests.exceptions.ReadTimeout),
            id="read_timeout",
        ),
        pytest.param(lambda: FakeSession(MidStreamDropResponse()), id="mid_stream_drop"),
        pytest.param(
            lambda: FakeSession(
                HeaderedResponse([], status_code=401, headers={"Retry-After": "5"})
            ),
            id="unauthorised",
        ),
        pytest.param(
            lambda: FakeSession(HeaderedResponse([], status_code=500)), id="server_error"
        ),
        pytest.param(lambda: BodyEchoesTheKeySession(), id="non_tiff_body_echoes_key"),
        pytest.param(
            lambda: BodyEchoesTheKeySession(status_code=400), id="bad_request_echoes_key"
        ),
    ],
)
def test_no_recorded_failure_can_ever_contain_the_key(session_factory, tmp_path):
    source = ElevationSource(api_key=SECRET, session=session_factory())
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())

    assert source.tile_failures, "the failure path recorded nothing to check"
    for failure in source.tile_failures:
        # The key itself, in every encoding _redact has ever had to learn.
        assert SECRET not in failure.reason
        assert quote_plus(SECRET) not in failure.reason
        assert SECRET.replace("-", "%2D") not in failure.reason
        # And the two things a key travels inside: a URL and a parameter
        # name. Neither belongs in a reason at all, which is what makes
        # the first three assertions structurally guaranteed rather than
        # merely observed.
        assert "API_Key" not in failure.reason
        assert "://" not in failure.reason
        # No query string can exist without one of these, whatever the
        # parameter happens to be called next year.
        assert "=" not in failure.reason
        assert "&" not in failure.reason
        # Not vacuous: the reason really does say something.
        assert failure.reason.startswith("Failed to download DEM: ")
        assert len(failure.reason) > len("Failed to download DEM: ")


def test_the_key_reaches_the_recorded_failure_by_no_route_at_all(tmp_path):
    # The paranoid version of the test above, and the one that would catch
    # a future edit composing a reason out of str(exc). Walks the WHOLE
    # record, not just the reason: every field of it, stringified.
    source = ElevationSource(api_key=SECRET, session=BodyEchoesTheKeySession())
    with pytest.raises(ElevationError):
        source.fetch(BBOX, _TILES, tmp_path, NullProgress())

    for failure in source.tile_failures:
        record = failure.to_record()
        blob = repr(record) + repr(failure)
        assert SECRET not in blob
        assert quote_plus(SECRET) not in blob
        # REDACTION_PLACEHOLDER present would mean a redaction had to run,
        # which would mean the reason had been composed from text that
        # held the key. It must never have to.
        assert REDACTION_PLACEHOLDER not in blob
