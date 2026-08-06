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


# --------------------------------------------------------------------------
# ZipReader: real zips, built with `zipfile`, read back through
# FileByteSource (see cog.py). Never a fake ByteSource here: what is under
# test is the EOCD scan, the central directory walk and the local header
# read, against real zip bytes.
# --------------------------------------------------------------------------


def _build_sample_zip(path: Path) -> dict[str, bytes]:
    """A small zip with one stored and one deflated member, both under a
    `data/` prefix matching the real OpenRoads member naming
    (`data/OSOpenRoads_<SQ>.gml`).
    """
    stored_content = b"<gml>stored content, easy to eyeball on a diff</gml>" * 3
    # Long and repetitive on purpose, so ZIP_DEFLATED actually shrinks it;
    # a short or high-entropy body can come back the same size or larger,
    # which would not exercise the deflate path at all.
    deflated_content = b"<gml>" + b"repeat this text " * 300 + b"</gml>"
    contents = {
        "data/OSOpenRoads_SS.gml": stored_content,
        "data/OSOpenRoads_ST.gml": deflated_content,
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("data/OSOpenRoads_SS.gml"),
            stored_content,
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            zipfile.ZipInfo("data/OSOpenRoads_ST.gml"),
            deflated_content,
            compress_type=zipfile.ZIP_DEFLATED,
        )
    return contents


def test_zipreader_lists_members_with_names_and_sizes(tmp_path):
    zip_path = tmp_path / "roads.zip"
    contents = _build_sample_zip(zip_path)

    reader = os_downloads.ZipReader(FileByteSource(zip_path))
    members = reader.members()

    assert set(members) == set(contents)
    for name, data in contents.items():
        assert members[name].uncompressed_size == len(data)
        assert members[name].name == name
    assert members["data/OSOpenRoads_SS.gml"].method == 0
    assert members["data/OSOpenRoads_ST.gml"].method == 8
    # The deflated member's own compressed span is genuinely smaller than
    # its uncompressed size, for the repetitive body _build_sample_zip
    # writes; a reader that mixed up the two fields would still pass every
    # other assertion here.
    deflated = members["data/OSOpenRoads_ST.gml"]
    assert deflated.compressed_size < deflated.uncompressed_size


def test_zipreader_round_trips_the_stored_member(tmp_path):
    zip_path = tmp_path / "roads.zip"
    contents = _build_sample_zip(zip_path)

    reader = os_downloads.ZipReader(FileByteSource(zip_path))
    assert reader.read_member("data/OSOpenRoads_SS.gml") == contents["data/OSOpenRoads_SS.gml"]


def test_zipreader_round_trips_the_deflated_member(tmp_path):
    zip_path = tmp_path / "roads.zip"
    contents = _build_sample_zip(zip_path)

    reader = os_downloads.ZipReader(FileByteSource(zip_path))
    assert reader.read_member("data/OSOpenRoads_ST.gml") == contents["data/OSOpenRoads_ST.gml"]


def test_zipreader_read_member_raises_range_for_an_unknown_name(tmp_path):
    zip_path = tmp_path / "roads.zip"
    _build_sample_zip(zip_path)

    reader = os_downloads.ZipReader(FileByteSource(zip_path))
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        reader.read_member("data/OSOpenRoads_ZZ.gml")
    assert excinfo.value.kind == "range"


def test_zipreader_raises_range_for_a_missing_eocd_signature(tmp_path):
    zip_path = tmp_path / "roads.zip"
    _build_sample_zip(zip_path)
    data = bytearray(zip_path.read_bytes())
    pos = bytes(data).rfind(b"PK\x05\x06")
    assert pos != -1
    data[pos:pos + 4] = b"XXXX"
    zip_path.write_bytes(bytes(data))

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.ZipReader(FileByteSource(zip_path))
    assert excinfo.value.kind == "range"


def test_zipreader_raises_range_for_a_zip64_size_marker(tmp_path):
    zip_path = tmp_path / "roads.zip"
    _build_sample_zip(zip_path)
    data = bytearray(zip_path.read_bytes())
    pos = bytes(data).rfind(b"PK\x05\x06")
    assert pos != -1
    # Size of the central directory (a 4 byte field) sits 12 bytes into the
    # EOCD's own fixed 22 byte record; 0xFFFFFFFF there is the zip64
    # sentinel this reader refuses rather than misreads.
    struct.pack_into("<I", data, pos + 12, 0xFFFFFFFF)
    zip_path.write_bytes(bytes(data))

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.ZipReader(FileByteSource(zip_path))
    assert excinfo.value.kind == "range"


class _TruncatingSource:
    """Wraps a real `FileByteSource` but shortens exactly one read's own
    answer, the first one asked for the given `length`: everything else,
    including every read the central-directory walk itself makes, passes
    through unmodified. Used to make one member's compressed span arrive
    short without hand-deriving its file offset, which the length match
    keeps this test independent of.
    """

    def __init__(self, inner, target_length: int, truncate_by: int) -> None:
        self._inner = inner
        self.name = inner.name
        self._target_length = target_length
        self._truncate_by = truncate_by
        self._matched = False

    def size(self) -> int:
        return self._inner.size()

    def read(self, start: int, length: int) -> bytes:
        data = self._inner.read(start, length)
        if not self._matched and length == self._target_length:
            self._matched = True
            return data[: max(0, len(data) - self._truncate_by)]
        return data


def test_zipreader_raises_range_when_a_members_compressed_span_arrives_short(tmp_path):
    zip_path = tmp_path / "roads.zip"
    _build_sample_zip(zip_path)

    reader = os_downloads.ZipReader(FileByteSource(zip_path))
    member = reader.members()["data/OSOpenRoads_SS.gml"]

    truncated_source = _TruncatingSource(FileByteSource(zip_path), member.compressed_size, 1)
    truncated_reader = os_downloads.ZipReader(truncated_source)
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        truncated_reader.read_member("data/OSOpenRoads_SS.gml")
    assert excinfo.value.kind == "range"


# --------------------------------------------------------------------------
# cache_root / product_cache_dir / sweep_old_versions.
#
# Every test here monkeypatches os_downloads.CONFIG_PATH, the name this
# module imports from mapgen.config, exactly the mechanism
# test_inspire.py's own
# test_fetch_authority_zip_default_cache_dir_is_under_the_mapgen_home
# already establishes for inspire.py's sibling cache: patching the NAME a
# module imported, not mapgen.config.CONFIG_PATH itself (which os_downloads
# already read once, at import time, into its own module namespace).
# --------------------------------------------------------------------------


def test_cache_root_is_under_config_paths_own_parent(monkeypatch, tmp_path):
    fake_config_path = tmp_path / "config.json"
    monkeypatch.setattr(os_downloads, "CONFIG_PATH", fake_config_path)

    assert os_downloads.cache_root() == tmp_path / "osopen"


def test_product_cache_dir_is_named_and_created(monkeypatch, tmp_path):
    fake_config_path = tmp_path / "config.json"
    monkeypatch.setattr(os_downloads, "CONFIG_PATH", fake_config_path)

    path = os_downloads.product_cache_dir("OpenGreenspace", "2026-04")

    assert path == tmp_path / "osopen" / "OpenGreenspace_2026-04"
    assert path.is_dir()


def test_sweep_old_versions_removes_only_same_product_siblings(monkeypatch, tmp_path):
    fake_config_path = tmp_path / "config.json"
    monkeypatch.setattr(os_downloads, "CONFIG_PATH", fake_config_path)

    old_dir = os_downloads.product_cache_dir("OpenGreenspace", "2026-01")
    kept_dir = os_downloads.product_cache_dir("OpenGreenspace", "2026-04")
    other_product_dir = os_downloads.product_cache_dir("OpenRoads", "2026-01")
    (old_dir / "marker.txt").write_text("stale", encoding="utf-8")
    (kept_dir / "marker.txt").write_text("current", encoding="utf-8")

    os_downloads.sweep_old_versions("OpenGreenspace", keep_version="2026-04")

    assert not old_dir.exists()
    assert kept_dir.is_dir()
    assert (kept_dir / "marker.txt").exists()
    assert other_product_dir.is_dir()


def test_sweep_old_versions_survives_a_missing_cache_root(monkeypatch, tmp_path):
    fake_config_path = tmp_path / "nested" / "does-not-exist" / "config.json"
    monkeypatch.setattr(os_downloads, "CONFIG_PATH", fake_config_path)

    os_downloads.sweep_old_versions("OpenGreenspace", keep_version="2026-04")  # must not raise
