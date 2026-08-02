import pytest
import requests

from mapgen.geo import BBox
from mapgen.sources.base import NullProgress
from mapgen.sources.elevation import (
    ElevationError,
    ElevationSource,
    MissingApiKeyError,
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
    assert paths[0].name == "elevation.tif"
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
    (tmp_path / "elevation.tif").write_bytes(TIFF_LITTLE_ENDIAN)
    session = FakeSession(FakeStreamResponse([]))
    ElevationSource(api_key="k", session=session).fetch(BBOX, [], tmp_path, NullProgress())
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
    assert not (tmp_path / "elevation.tif").exists()


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


def test_fetch_http_error_never_leaks_the_api_key_via_the_request_url(tmp_path):
    # requests' own HTTPError message includes response.url, and this
    # endpoint sends the key as a query parameter (API_Key=...), so a
    # bare str(exc) would put a real, rejected key into ElevationError's
    # message, and from there into survey.json's source_failed event, the
    # job's error field, and the log panel. This proves the fix: a
    # genuinely leaky HTTPError message must not survive into what
    # fetch() raises.
    secret = "sk-real-secret-should-never-leak"
    leaky_url = (
        f"https://portal.opentopography.org/API/globaldem?demtype=COP30&API_Key={secret}"
    )

    class LeakyHttpErrorResponse(FakeStreamResponse):
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(
                    f"{self.status_code} Client Error: Forbidden for url: {leaky_url}"
                )

    session = FakeSession(LeakyHttpErrorResponse([], status_code=403))
    source = ElevationSource(api_key=secret, session=session)
    with pytest.raises(ElevationError) as excinfo:
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert secret not in str(excinfo.value)
    assert "403" in str(excinfo.value)


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


def test_merge_passes_the_tiff_through(tmp_path):
    part = tmp_path / "elevation.tif"
    part.write_bytes(TIFF_LITTLE_ENDIAN)
    assert ElevationSource(api_key="k").merge([part], tmp_path / "out") == [part]


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
