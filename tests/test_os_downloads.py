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
import tracemalloc
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
# Review finding 1 (Critical): a malformed product id or entry url must
# never let a non-OsOpenError exception escape, and must never carry a URL
# once wrapped. Two of these hit the real, unpatched urllib machinery
# directly (a space or a non-ASCII character in what becomes a request
# line raises http.client.InvalidURL or UnicodeEncodeError before any
# socket is ever touched, confirmed by hand: both fail in under 20ms with
# no patched opener at all), so those run against the real _build_opener,
# not the fake, on purpose: this is the exact path the review's own
# reproduction used. A third test proves the wrapping is a genuine
# catch-all rather than a list of exception types that happens to include
# these two, by scripting a fake opener that raises something this module
# has never seen before.
# --------------------------------------------------------------------------


def test_product_downloads_wraps_a_space_in_the_product_id_with_no_url_leaking():
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_downloads("Open Greenspace")

    assert excinfo.value.kind == "listing"
    assert excinfo.value.status_code is None
    assert "/downloads/v1/products/" not in str(excinfo.value)
    assert "api.os.uk" not in str(excinfo.value)


def test_product_downloads_wraps_a_non_ascii_product_id():
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_downloads("ürban")

    assert excinfo.value.kind == "listing"
    assert excinfo.value.status_code is None


def test_product_version_wraps_a_space_in_the_product_id_with_no_url_leaking():
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_version("Open Greenspace")

    assert excinfo.value.kind == "listing"
    assert excinfo.value.status_code is None
    assert "api.os.uk" not in str(excinfo.value)


def test_product_downloads_wraps_any_unexpected_opener_exception(monkeypatch):
    failing_url_fragment = "https://api.os.uk/downloads/v1/products/OpenGreenspace/downloads"

    class _Unforeseen(Exception):
        pass

    class _BoomOpener:
        def open(self, request, timeout=None):
            raise _Unforeseen(f"kaboom while fetching {failing_url_fragment}")

    monkeypatch.setattr(os_downloads, "_build_opener", lambda: _BoomOpener())

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.product_downloads("OpenGreenspace")

    assert excinfo.value.kind == "listing"
    assert excinfo.value.status_code is None
    assert failing_url_fragment not in str(excinfo.value)


def test_download_entry_wraps_a_space_in_the_entry_url_with_no_url_leaking(tmp_path):
    entry = _entry(10, url="https://api.os.uk/downloads/v1/x y")
    dest = tmp_path / "out" / "x.zip"

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.download_entry(entry, dest)

    assert excinfo.value.kind == "download"
    assert excinfo.value.status_code is None
    assert "x y" not in str(excinfo.value)
    assert "api.os.uk" not in str(excinfo.value)
    assert list(dest.parent.glob("*.part")) == []
    assert not dest.exists()


def test_download_entry_wraps_a_non_ascii_entry_url(tmp_path):
    entry = _entry(10, url="https://api.os.uk/downloads/v1/ürban")
    dest = tmp_path / "out" / "x.zip"

    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.download_entry(entry, dest)

    assert excinfo.value.kind == "download"
    assert list(dest.parent.glob("*.part")) == []


def test_download_entry_wraps_any_unexpected_opener_exception(tmp_path, monkeypatch):
    failing_url_fragment = "https://api.os.uk/downloads/v1/x"

    class _Unforeseen(Exception):
        pass

    class _BoomOpener:
        def open(self, request, timeout=None):
            raise _Unforeseen(f"kaboom while fetching {failing_url_fragment}")

    monkeypatch.setattr(os_downloads, "_build_opener", lambda: _BoomOpener())

    dest = tmp_path / "out" / "x.zip"
    with pytest.raises(os_downloads.OsOpenError) as excinfo:
        os_downloads.download_entry(_entry(10, url=failing_url_fragment), dest)

    assert excinfo.value.kind == "download"
    assert excinfo.value.status_code is None
    assert failing_url_fragment not in str(excinfo.value)
    assert list(dest.parent.glob("*.part")) == []


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


def _build_lying_zip(
    path: Path, name: str, compressed_payload: bytes, declared_uncompressed_size: int
) -> None:
    """A one-member zip, built by hand rather than through `zipfile`, whose
    central directory (and local header) both declare
    `declared_uncompressed_size` for a member whose real, decompressed
    size may be far larger: `compressed_payload` is handed through
    untouched, a genuine raw-deflate stream (method 8) the caller built
    however it likes.

    Exists only to prove `ZipReader.read_member` bounds its own
    decompression rather than trusting the archive's own (here,
    deliberately dishonest) declared size; see
    `test_zipreader_bounds_deflate_decompression_and_flags_the_overrun`.
    Every field this reader itself ignores (crc32, mod time/date, disk
    numbers, attributes) is written as 0, since nothing in ZipReader reads
    them.
    """
    name_bytes = name.encode("ascii")
    local_header = b"PK\x03\x04" + struct.pack(
        "<HHHHHIIIHH",
        20, 0, 8, 0, 0, 0,
        len(compressed_payload), declared_uncompressed_size,
        len(name_bytes), 0,
    )
    local_record = local_header + name_bytes + compressed_payload

    central_entry = (
        b"PK\x01\x02"
        + struct.pack(
            "<HHHHHHIIIHHHHHII",
            20, 20, 0, 8, 0, 0, 0,
            len(compressed_payload), declared_uncompressed_size,
            len(name_bytes), 0, 0, 0, 0, 0, 0,
        )
        + name_bytes
    )
    eocd = b"PK\x05\x06" + struct.pack(
        "<HHHHIIH", 0, 0, 1, 1, len(central_entry), len(local_record), 0
    )
    path.write_bytes(local_record + central_entry + eocd)


def test_zipreader_bounds_deflate_decompression_and_flags_the_overrun(tmp_path):
    """Review finding 2: a member whose central directory LIES about its
    own uncompressed size (declaring 100 bytes for a member that really
    inflates to 64 MiB of zeros) must not make `read_member` materialise
    the full 64 MiB before its own length check catches the mismatch,
    mirroring cog.py's own zip-bomb defense (`decompressobj().decompress
    (payload, expected)`, bounded at the decompressor rather than after
    it; see that module's "The caps" section).

    Measured with `tracemalloc`, the same tool the review itself used:
    against the unbounded implementation this review found, this single
    call peaks at very roughly true_size worth of Python-level
    allocation (the review's own probe measured 601.7 MB peak for a
    comparable 300 MB bomb); against the bounded fix, the peak stays
    near the tiny DECLARED size instead. `true_size // 4` is a generous
    ceiling well under true_size, so this fails loudly if a future change
    quietly drops the bound rather than merely nudging a number.
    """
    name = "data/bomb.gml"
    true_size = 64 * 1024 * 1024  # 64 MiB of zeros: highly compressible.
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    bomb_payload = compressor.compress(b"\x00" * true_size) + compressor.flush()
    # A real bomb: compresses to well under 1% of what it inflates to.
    assert len(bomb_payload) < true_size // 100

    zip_path = tmp_path / "bomb.zip"
    _build_lying_zip(zip_path, name, bomb_payload, declared_uncompressed_size=100)

    reader = os_downloads.ZipReader(FileByteSource(zip_path))
    tracemalloc.start()
    try:
        with pytest.raises(os_downloads.OsOpenError) as excinfo:
            reader.read_member(name)
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert excinfo.value.kind == "range"
    assert peak < true_size // 4


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


def test_sweep_old_versions_does_not_cross_delete_a_product_whose_id_is_a_delimiter_prefix(
    monkeypatch, tmp_path
):
    """Review finding 3: the cache directory name is `f"{product}_{version}"`,
    and a naive `f"{product}_*"` glob also matches a DIFFERENT product
    whose own id literally begins with `"<this product>_"`. "Open" and
    "Open_Extra" are exactly that pair: sweeping "Open" must delete only
    its own stale version, never "Open_Extra"'s cache, which is a
    distinct product that merely happens to share the delimiter as a
    literal prefix.
    """
    fake_config_path = tmp_path / "config.json"
    monkeypatch.setattr(os_downloads, "CONFIG_PATH", fake_config_path)

    kept_dir = os_downloads.product_cache_dir("Open", "2026-04")
    old_dir = os_downloads.product_cache_dir("Open", "2026-01")
    foreign_dir = os_downloads.product_cache_dir("Open_Extra", "2026-01")
    (foreign_dir / "marker.txt").write_text("unrelated product", encoding="utf-8")

    os_downloads.sweep_old_versions("Open", keep_version="2026-04")

    assert kept_dir.is_dir()
    assert not old_dir.exists()
    assert foreign_dir.is_dir()
    assert (foreign_dir / "marker.txt").exists()


# --------------------------------------------------------------------------
# The one live test: the real OS Data Hub, no fakes, no patched opener.
# --------------------------------------------------------------------------


@pytest.mark.live
def test_live_downloads_listing_and_zipreader_over_the_real_openroads_gml():
    """`product_downloads` against the real OpenGreenspace listing, and
    `ZipReader` over `HttpByteSource` against the real OpenRoads national
    GML zip (608,511,751 bytes; downloaded by nothing here except its own
    central directory, a few hundred KB of range reads, never the member
    itself).

    `HttpByteSource(entry["url"])` is handed the entry's own url directly,
    with no redirect pre-resolution: probed by hand ahead of writing this
    test (see the task report), `HttpByteSource`'s default `requests.
    Session` already follows the 302 to Azure blob storage on every
    request it makes, Range header and all, so there is no separate final
    URL for this module to resolve first. If a future `requests` version,
    or a differently configured session, ever stopped doing that
    transparently, this test would start failing at the `members()` call
    below rather than silently reading the wrong bytes, because `size()`
    and every subsequent range read would then be answered by
    `api.os.uk` itself rather than by Azure.
    """
    from mapgen.cog import HttpByteSource

    greenspace_entries = os_downloads.product_downloads("OpenGreenspace")
    ss_entry = os_downloads.entry_for(greenspace_entries, area="SS", fmt="GML")
    assert ss_entry is not None
    assert isinstance(ss_entry.get("md5"), str) and ss_entry["md5"]
    assert isinstance(ss_entry.get("size"), int) and ss_entry["size"] > 0
    assert isinstance(ss_entry.get("url"), str) and ss_entry["url"].startswith("https://")

    roads_entries = os_downloads.product_downloads("OpenRoads")
    gml_entry = os_downloads.entry_for(roads_entries, area="GB", fmt="GML")
    assert gml_entry is not None

    reader = os_downloads.ZipReader(HttpByteSource(gml_entry["url"]))
    members = reader.members()
    assert "data/OSOpenRoads_SS.gml" in members
