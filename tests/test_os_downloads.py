"""os_downloads.py's suite: the Downloads API client, the versioned cache
directories, and the ranged zip reader.

The client half (`product_downloads`, `product_version`, `download_entry`)
never touches the network in this file except in the one `live` test at
the bottom: every other test patches `os_downloads._build_opener` with a
fake urllib opener (`_FakeOpener` below), the same seam
`download_entry`/`product_downloads`/`product_version` all read through.
The zip reader half is driven against real zips `zipfile` builds in
`tmp_path`, read back through `FileByteSource` (see cog.py), never a fake.

No test here ever puts a URL into an assertion string it expects an
OsOpenError's own message to contain: see the "no URL in message" tests
specifically, which are the ones enforcing that rule structurally rather
than trusting every call site to remember it by hand.
"""

from __future__ import annotations

import json
import os
import struct
import urllib.error
import zipfile
import zlib
from pathlib import Path

import pytest

from mapgen import os_downloads
from mapgen.cog import FileByteSource
from mapgen.sources.base import ProgressSink

_LISTING_PATH = Path(__file__).resolve().parent / "fixtures" / "osopen" / "downloads_listing.json"


def _load_listing() -> list[dict]:
    return json.loads(_LISTING_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The opener seam: a fake urllib opener, patched in place of
# os_downloads._build_opener for every test in this file except the live
# one. Shaped like urllib.request.OpenerDirector's own `.open(request,
# timeout=...)`, returning something supporting the response context
# manager protocol (`.status`, `.read`, `.headers`, `.geturl`), because
# that is the one method os_downloads.py itself ever calls on it.
# --------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes, url: str | None = None, headers=None):
        self.status = status
        self._body = body
        self._url = url
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:amt], self._body[amt:]
        return data

    def geturl(self) -> str:
        return self._url or ""


class _FakeOpener:
    """Answers `.open()` calls in order from a fixed script.

    Each scripted item is either a response to return or an exception
    instance to raise, exactly once, in the order given: this file never
    needs an opener that answers the same call differently depending on
    how many times it has already been asked, unlike test_cog.py's own
    `_RangeSession` (which distinguishes first attempt from retry).
    `requests_made` records every `urllib.request.Request` handed to
    `.open()`, so a test can assert what URL and headers a call actually
    used without the fake having to guess in advance.
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


class _RecordingProgress:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


# --------------------------------------------------------------------------
# entry_for
# --------------------------------------------------------------------------


def test_entry_for_picks_the_ss_gml_entry():
    entries = _load_listing()
    entry = os_downloads.entry_for(entries, area="SS", fmt="GML")
    assert entry is not None
    assert entry["area"] == "SS"
    assert entry["fileName"] == "opgrsp_gml3_ss.zip"


def test_entry_for_returns_none_for_a_missing_area():
    entries = _load_listing()
    assert os_downloads.entry_for(entries, area="ZZ", fmt="GML") is None


def test_entry_for_matches_an_explicit_subformat():
    entries = _load_listing()
    entry = os_downloads.entry_for(entries, area="SS", fmt="GML", subformat="3")
    assert entry is not None
    assert entry["area"] == "SS"


def test_entry_for_rejects_a_subformat_that_does_not_match():
    entries = _load_listing()
    assert os_downloads.entry_for(entries, area="SS", fmt="GML", subformat="9") is None


def test_entry_for_ignores_subformat_when_not_asked_for():
    entries = _load_listing()
    # subformat defaults to None: "don't care", so the SS/GML entry (which
    # does carry a subformat) is still matched.
    entry = os_downloads.entry_for(entries, area="SS", fmt="GML")
    assert entry is not None


# --------------------------------------------------------------------------
# product_downloads
# --------------------------------------------------------------------------


def test_product_downloads_decodes_a_canned_listing(monkeypatch):
    listing = _load_listing()
    body = json.dumps(listing).encode("utf-8")
    opener = _FakeOpener([_FakeHTTPResponse(200, body)])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    result = os_downloads.product_downloads("OpenGreenspace")

    assert result == listing


def test_product_downloads_wraps_a_503_as_listing_kind_with_status_and_no_url(monkeypatch):
    failing_url = "https://api.os.uk/downloads/v1/products/OpenGreenspace/downloads"
    http_error = urllib.error.HTTPError(failing_url, 503, "Service Unavailable", {}, None)
    opener = _FakeOpener([http_error])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_downloads("OpenGreenspace")

    assert excinfo.value.kind == "listing"
    assert excinfo.value.status_code == 503
    assert failing_url not in str(excinfo.value)
    assert "https://" not in str(excinfo.value)
    assert "api.os.uk" not in str(excinfo.value)


def test_product_downloads_wraps_a_transport_failure_as_listing_kind_with_no_status(monkeypatch):
    opener = _FakeOpener([urllib.error.URLError("the fake link is down")])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_downloads("OpenGreenspace")

    assert excinfo.value.kind == "listing"
    assert excinfo.value.status_code is None


def test_product_downloads_rejects_a_non_list_body_as_parse_kind(monkeypatch):
    opener = _FakeOpener([_FakeHTTPResponse(200, b'{"not": "a list"}')])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_downloads("OpenGreenspace")

    assert excinfo.value.kind == "parse"


def test_product_downloads_rejects_invalid_json_as_parse_kind(monkeypatch):
    opener = _FakeOpener([_FakeHTTPResponse(200, b"not json at all")])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_downloads("OpenGreenspace")

    assert excinfo.value.kind == "parse"


# --------------------------------------------------------------------------
# product_version
# --------------------------------------------------------------------------


def test_product_version_reads_the_version_field(monkeypatch):
    body = json.dumps({"id": "OpenGreenspace", "version": "2026-04"}).encode("utf-8")
    opener = _FakeOpener([_FakeHTTPResponse(200, body)])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    assert os_downloads.product_version("OpenGreenspace") == "2026-04"


def test_product_version_wraps_a_503_as_listing_kind(monkeypatch):
    http_error = urllib.error.HTTPError(
        "https://api.os.uk/downloads/v1/products/OpenGreenspace", 503, "Service Unavailable", {}, None
    )
    opener = _FakeOpener([http_error])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_version("OpenGreenspace")

    assert excinfo.value.kind == "listing"
    assert excinfo.value.status_code == 503


def test_product_version_rejects_a_missing_version_field_as_parse_kind(monkeypatch):
    body = json.dumps({"id": "OpenGreenspace"}).encode("utf-8")
    opener = _FakeOpener([_FakeHTTPResponse(200, body)])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_version("OpenGreenspace")

    assert excinfo.value.kind == "parse"


# --------------------------------------------------------------------------
# download_entry
# --------------------------------------------------------------------------


def _entry(size: int, url: str = "https://api.os.uk/downloads/v1/x", file_name: str = "x.zip") -> dict:
    return {"url": url, "size": size, "fileName": file_name, "md5": "0" * 32, "format": "GML", "area": "SS"}


def test_download_entry_writes_atomically_with_no_part_left(tmp_path, monkeypatch):
    body = b"x" * 100
    opener = _FakeOpener([_FakeHTTPResponse(200, body)])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    dest = tmp_path / "out" / "x.zip"
    result = os_downloads.download_entry(_entry(len(body)), dest)

    assert result == dest
    assert dest.read_bytes() == body
    assert list(dest.parent.glob("*.part")) == []


def test_download_entry_fails_on_short_body_with_download_kind(tmp_path, monkeypatch):
    body = b"x" * 50  # declared size below is 100: a short body.
    opener = _FakeOpener([_FakeHTTPResponse(200, body)])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    dest = tmp_path / "out" / "x.zip"
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.download_entry(_entry(100), dest)

    assert excinfo.value.kind == "download"
    assert not dest.exists()
    assert list(dest.parent.glob("*.part")) == []


def test_download_entry_fails_on_long_body_with_download_kind(tmp_path, monkeypatch):
    body = b"x" * 150  # declared size below is 100: too much came back.
    opener = _FakeOpener([_FakeHTTPResponse(200, body)])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    dest = tmp_path / "out" / "x.zip"
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.download_entry(_entry(100), dest)

    assert excinfo.value.kind == "download"
    assert not dest.exists()
    assert list(dest.parent.glob("*.part")) == []


def test_download_entry_wraps_an_http_error_as_download_kind_with_no_url(tmp_path, monkeypatch):
    failing_url = "https://api.os.uk/downloads/v1/x"
    http_error = urllib.error.HTTPError(failing_url, 404, "Not Found", {}, None)
    opener = _FakeOpener([http_error])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    dest = tmp_path / "out" / "x.zip"
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.download_entry(_entry(100, url=failing_url), dest)

    assert excinfo.value.kind == "download"
    assert excinfo.value.status_code == 404
    assert failing_url not in str(excinfo.value)
    assert list(dest.parent.glob("*.part")) == []


def test_download_entry_emits_progress_events(tmp_path, monkeypatch):
    body = b"y" * 40
    opener = _FakeOpener([_FakeHTTPResponse(200, body)])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    progress = _RecordingProgress()
    dest = tmp_path / "out" / "y.zip"
    os_downloads.download_entry(_entry(len(body)), dest, progress=progress)

    assert progress.events
    last_event, last_fields = progress.events[-1]
    assert last_fields["bytes_done"] == len(body)
    assert last_fields["bytes_total"] == len(body)


def test_download_entry_works_with_no_progress_sink(tmp_path, monkeypatch):
    body = b"z" * 10
    opener = _FakeOpener([_FakeHTTPResponse(200, body)])
    monkeypatch.setattr(os_downloads, "_build_opener", lambda: opener)

    dest = tmp_path / "out" / "z.zip"
    result = os_downloads.download_entry(_entry(len(body)), dest)

    assert result == dest
