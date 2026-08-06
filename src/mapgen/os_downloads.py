"""The OS Data Hub Downloads API: listing, versioning, and a ranged zip reader.

`https://api.os.uk/downloads/v1` serves every OS Open product keyless:
`GET /products/{id}/downloads` answers a JSON list of entries (one per
area/format/subformat combination a product publishes), `GET
/products/{id}` answers the product's own metadata including its current
`version` string (for example `"2026-04"`), and each entry's own `url`
field 302-redirects to Azure blob storage, which honours HTTP Range
requests (verified live 2026-08-07; see this task's brief). Everything in
this module that talks to that API goes through `_build_opener()`, a
module-level seam mirroring the one every other network-touching module in
this project already establishes (`inspire.py`'s own session, `cog.py`'s
own `HttpByteSource.session`): tests patch this one function rather than
reaching into urllib's own global state, and nothing here ever builds a
second opener anywhere else.

Deliberately urllib, not `requests`: this project's own dependency list is
`requests` plus `overturemaps`, both already present, but this whole task
is explicitly stdlib-only (see the plan's global constraints), and urllib's
default opener already does the one thing that matters here for free,
following the 302 to Azure blob storage without being told to.

## The no-URL rule

Every OsOpenError message is composed from a fixed vocabulary plus, at
most, a product id (a short catalog name, never a URL) or a file name (an
entry's own `fileName`, also never a URL): never `str(exc)` on a caught
urllib exception, and never an entry's own `url` field. This matches
`sources/base.py`'s own stated reason (a URL is where an API key lives)
even though these particular endpoints carry no key at all: the rule is
structural, not conditional on what happens to be true of this one API
today.

## The zip reader

`ZipReader` reads a zip's own central directory and local headers over a
`ByteSource` (see `cog.py`), never `zipfile` itself, because the file it
is built to read (OpenRoads' national GML zip, 608,511,751 bytes) is far
too large to hold or to download whole just to reach one 100km square's
member. It looks up the End Of Central Directory record in the final
65,536 bytes (the maximum a zip comment can push it back by), walks the
central directory once to learn every member's name and where its local
header is, and reads a member's compressed span only when `read_member`
is actually called for it, exactly the way `cog.py`'s tile index is read
without ever pulling a tile that was not asked for.

Zip64 is out of scope: none of the products this project reads need it
(the brief's own probe of OpenRoads, at 608,511,751 bytes, is nowhere near
the 4 GiB boundary that would force it), and a zip declaring the zip64
sentinel (0xFFFFFFFF in the plain EOCD's own size or offset fields) is
refused rather than misread, because the zip64 extra fields that would
make those numbers meaningful are a different record this reader does not
parse.

`CogError` (see `cog.py`) is caught at this module's own boundary and
re-raised as `OsOpenError` kind `"range"`: `HttpByteSource` already
enforces Range support and Content-Range agreement, and already keeps a
URL out of its own messages, so this module's job at that boundary is only
to translate the exception type, carrying `status_code` across when
`CogError` set one.
"""

from __future__ import annotations

import json
import os
import struct
import urllib.error
import urllib.request
import zlib
from collections import namedtuple
from pathlib import Path

from mapgen.cog import ByteSource, CogError
from mapgen.config import CONFIG_PATH
from mapgen.fsutil import best_effort_rmtree, ensure_dir
from mapgen.sources.base import ProgressSink

# Matching cog.py's own USER_AGENT exactly: one identifying string for
# every request mapgen makes to any of these hosts, rather than a second
# copy of the same sentence that could drift from it.
USER_AGENT = "mapgen/1.0 (architectural survey tool)"

OS_DOWNLOADS_BASE = "https://api.os.uk/downloads/v1"

_LISTING_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_TIMEOUT_SECONDS = 120.0
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024

# One member entry out of a zip's central directory, exactly as much of it
# as ZipReader needs: where the member's own local header is
# (`header_offset`, the central directory's "relative offset of local
# header" field, from which read_member finds the compressed span), its
# compression method (0 stored, 8 deflate; nothing else is read by any
# product this project reads), and both sizes.
ZipMember = namedtuple(
    "ZipMember", "name method compressed_size uncompressed_size header_offset"
)

# The largest a zip comment can be (a 2 byte length field), which bounds
# how far before the file's own end the End Of Central Directory record
# can start. Matches the brief's own stated search window rather than the
# few bytes more (`_EOCD_FIXED_SIZE`) a maximally-commented real zip would
# in principle need; every zip this project reads or writes in a test
# carries no comment at all, so this window is never actually exercised at
# its edge.
_EOCD_SEARCH_WINDOW = 65536

_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_LOCAL_HEADER_SIGNATURE = b"PK\x03\x04"

# Every struct below is the ZIP format's own fixed-size record, with its
# 4 byte signature sliced off and checked separately (a plain byte
# comparison rather than one more struct field), because the three
# records share no other field in common and unpacking the signature as
# an integer just to compare it back to a constant would be one extra,
# needless conversion at every one of these three call sites.
_EOCD_STRUCT = "<HHHHIIH"  # 18 bytes, after the 4 byte signature.
_CENTRAL_DIRECTORY_STRUCT = "<HHHHHHIIIHHHHHII"  # 42 bytes, after the signature.
_LOCAL_HEADER_NAME_EXTRA_LEN_STRUCT = "<HH"  # name_len, extra_len, at offset 26.

_ZIP_METHOD_STORED = 0
_ZIP_METHOD_DEFLATED = 8

# raw deflate: no zlib/gzip header or trailer, exactly what a zip member's
# own compressed span holds. -15 is zlib's own spelling of "raw, 15 bit
# window", the same value cog.py would use if it ever read a deflated tile
# raw (it does not: TIFF's own deflate tiles carry a zlib header, unlike a
# zip member's).
_RAW_DEFLATE_WBITS = -15


class OsOpenError(ValueError):
    """Raised for anything this module cannot fetch, verify, or parse.

    `kind` is one of `"listing"` (the downloads-or-product-info catalog
    calls could not be reached, or answered a non-2xx status), `"parse"`
    (a catalog call answered but its body was not the JSON shape expected),
    `"download"` (an entry's own bytes could not be fetched, or arrived the
    wrong length), or `"range"` (the ranged zip reader). `status_code` is
    the HTTP status when one is known (an `HTTPError`, or a `CogError` that
    carried one from `HttpByteSource`), and None otherwise: a transport
    failure, a JSON decode failure, or a zip structure failure never had a
    status to carry.

    Every message is composed here, in English, from a fixed vocabulary
    plus a product id or file name, never from `str()` on the exception
    this wraps: see the module docstring's "no-URL rule".
    """

    def __init__(self, message: str, *, kind: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


def _build_opener() -> urllib.request.OpenerDirector:
    """The one urllib opener every network call in this module goes
    through. A function, not a module-level instance, so a test can
    monkeypatch it wholesale without this module having to expose a
    setter; see the module docstring.
    """
    return urllib.request.build_opener()


def _get_json(url: str, *, what: str) -> object:
    """GET `url`, decode the body as JSON, return whatever it parsed to.

    `what` is a short phrase for the error message only ("downloads
    listing for 'OpenGreenspace'"), never the URL itself: see the module
    docstring's "no-URL rule". Raises OsOpenError kind "listing" for
    anything that stopped this from becoming a 2xx response with a body
    (an HTTPError, a URLError, a timeout, all subclasses of URLError), and
    kind "parse" for a body that came back but was not valid JSON.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        opener = _build_opener()
        with opener.open(request, timeout=_LISTING_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise OsOpenError(
            f"The OS Data Hub {what} answered HTTP {exc.code}.",
            kind="listing",
            status_code=exc.code,
        ) from None
    except urllib.error.URLError:
        raise OsOpenError(
            f"Could not reach the OS Data Hub {what}.",
            kind="listing",
        ) from None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise OsOpenError(
            f"The OS Data Hub {what} did not answer with valid JSON.",
            kind="parse",
        ) from None


def product_downloads(product: str) -> list[dict]:
    """Every download entry `product` publishes, JSON-decoded.

    `GET {OS_DOWNLOADS_BASE}/products/{product}/downloads`, no key. Raises
    OsOpenError kind "listing" for a network or status failure (status_code
    carried when the failure was an HTTP status), kind "parse" if the
    response was not a JSON list.
    """
    url = f"{OS_DOWNLOADS_BASE}/products/{product}/downloads"
    data = _get_json(url, what=f"downloads listing for {product!r}")
    if not isinstance(data, list):
        raise OsOpenError(
            f"The OS Data Hub downloads listing for {product!r} was not a "
            f"JSON list.",
            kind="parse",
        )
    return data


def entry_for(
    entries: list[dict], *, area: str, fmt: str, subformat: str | None = None
) -> dict | None:
    """The one entry matching `area` and `fmt` (and `subformat`, when
    given), or None.

    `subformat=None` means "do not care": it matches an entry regardless
    of what its own `subformat` field holds, including entries with no
    such field at all. Passing an explicit `subformat` only matches an
    entry whose own field equals it exactly. The first match wins, which
    is fine for every real listing this module reads: a product never
    publishes two entries for the same area, format and subformat.
    """
    for entry in entries:
        if entry.get("area") != area:
            continue
        if entry.get("format") != fmt:
            continue
        if subformat is not None and entry.get("subformat") != subformat:
            continue
        return entry
    return None


def product_version(product: str) -> str:
    """`product`'s current version string, e.g. `"2026-04"`.

    `GET {OS_DOWNLOADS_BASE}/products/{product}`, no key. Raises
    OsOpenError kind "listing" for a network or status failure, kind
    "parse" if the response was not a JSON object carrying a string
    `version` field.
    """
    url = f"{OS_DOWNLOADS_BASE}/products/{product}"
    data = _get_json(url, what=f"product info for {product!r}")
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        raise OsOpenError(
            f"The OS Data Hub product info for {product!r} has no string "
            f"'version' field.",
            kind="parse",
        )
    return data["version"]


def download_entry(entry: dict, dest: Path, progress: ProgressSink | None = None) -> Path:
    """Streams `entry["url"]` to `dest`, atomically, verifying its length.

    Writes to `dest` with a `.part` suffix and `os.replace`s it into place
    only once the whole body has arrived and its length matches
    `entry["size"]` exactly: a short OR a long body is refused (a server
    that sent more than it said it would is exactly as untrustworthy as
    one that sent less), and the `.part` file is removed on every failure
    path, including a cancelled or crashed run, so a retry never mistakes
    a partial file for a complete one.

    Emits `"download_progress"` events with `bytes_done`/`bytes_total`
    fields to `progress` as the body streams in, when `progress` is given;
    a caller that does not want progress passes None.

    Raises OsOpenError kind "download" for a network or status failure
    fetching the entry, or for a length mismatch once the transfer ends.
    Never puts `entry["url"]` in the message: see the module docstring's
    "no-URL rule". `entry["fileName"]` (never a URL) names the file in
    every message instead.
    """
    name = entry.get("fileName") or "the OS Open file"
    expected_size = entry["size"]
    ensure_dir(dest.parent)
    temp_path = dest.with_name(f"{dest.name}.part")

    request = urllib.request.Request(entry["url"], headers={"User-Agent": USER_AGENT})
    written = 0
    try:
        opener = _build_opener()
        with opener.open(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            with temp_path.open("wb") as handle:
                while True:
                    chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress.emit(
                            "download_progress",
                            bytes_done=written,
                            bytes_total=expected_size,
                        )
    except urllib.error.HTTPError as exc:
        temp_path.unlink(missing_ok=True)
        raise OsOpenError(
            f"Downloading {name} answered HTTP {exc.code}.",
            kind="download",
            status_code=exc.code,
        ) from None
    except urllib.error.URLError:
        temp_path.unlink(missing_ok=True)
        raise OsOpenError(
            f"Could not download {name}.",
            kind="download",
        ) from None
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    if written != expected_size:
        temp_path.unlink(missing_ok=True)
        raise OsOpenError(
            f"{name} downloaded {written} bytes, expected {expected_size}.",
            kind="download",
        )

    os.replace(temp_path, dest)
    return dest


class ZipReader:
    """A zip, read one member at a time over a `ByteSource`.

    Never `zipfile`: the one zip this reader exists for (OpenRoads' own
    national GML zip) is 608,511,751 bytes, and downloading or holding the
    whole thing just to reach one 100km square's member would defeat the
    entire point of a ranged reader. Construction reads the End Of Central
    Directory record and walks the central directory once, which on every
    product this project reads is a few hundred entries at most; nothing
    about a member's own compressed bytes is fetched until `read_member`
    is called for it by name.

    Every failure here is `OsOpenError` kind `"range"`: a missing or
    malformed EOCD, a zip64 archive (out of scope; see the module
    docstring), a central directory entry that does not start with its own
    signature, an unknown member name, an unsupported compression method,
    or a decompressed length that does not match what the central
    directory promised. `CogError` raised by the underlying `ByteSource`
    (`HttpByteSource`'s own Range and Content-Range enforcement; see
    cog.py) is caught at this class's boundary and re-raised the same way,
    carrying `status_code` across when `CogError` set one, so a caller
    never has to catch two exception types to learn about one failure.
    """

    def __init__(self, source: ByteSource) -> None:
        self.source = source
        self._members = self._read_central_directory()

    def members(self) -> dict[str, ZipMember]:
        """Every member's own name, method and sizes, as read from the
        central directory at construction time. A fresh dict each call
        (the same convention `cog.py`'s own read-only accessors use), so a
        caller mutating what it gets back cannot corrupt this reader's own
        state.
        """
        return dict(self._members)

    def read_member(self, name: str) -> bytes:
        """`name`'s whole uncompressed content.

        Reads the member's own local header first (30 fixed bytes plus its
        name and extra field lengths, which is where the compressed data
        actually starts: not necessarily the same as what the central
        directory's own extra field length would suggest, since some
        writers pad the two differently), then the compressed span itself,
        one range read each. Stored (method 0) data is returned as-is;
        deflated (method 8) data is inflated with a raw deflate stream
        (`zlib.decompressobj(-15)`: a zip member's compressed bytes carry
        no zlib header or trailer, unlike the deflated tiles cog.py
        reads). Either way the result's length is checked against the
        central directory's own `uncompressed_size` before it is returned,
        which is what catches a truncated or otherwise short transfer
        rather than handing back a partial member silently.
        """
        member = self._members.get(name)
        if member is None:
            raise OsOpenError(
                f"{self.source.name} has no member named {name!r}.",
                kind="range",
            )
        try:
            local_header = self.source.read(member.header_offset, 30)
            if local_header[:4] != _LOCAL_HEADER_SIGNATURE:
                raise OsOpenError(
                    f"{self.source.name}'s local header for {name!r} does "
                    f"not start with the expected signature, so it is "
                    f"corrupt.",
                    kind="range",
                )
            name_len, extra_len = struct.unpack_from(
                _LOCAL_HEADER_NAME_EXTRA_LEN_STRUCT, local_header, 26
            )
            data_offset = member.header_offset + 30 + name_len + extra_len
            payload = self.source.read(data_offset, member.compressed_size)
        except CogError as exc:
            raise OsOpenError(
                f"Reading {name!r} out of {self.source.name} failed: a "
                f"range request this reader depends on did not answer as "
                f"expected.",
                kind="range",
                status_code=getattr(exc, "status_code", None),
            ) from None

        if member.method == _ZIP_METHOD_STORED:
            data = payload
        elif member.method == _ZIP_METHOD_DEFLATED:
            decompressor = zlib.decompressobj(_RAW_DEFLATE_WBITS)
            try:
                data = decompressor.decompress(payload) + decompressor.flush()
            except zlib.error:
                raise OsOpenError(
                    f"{name!r} in {self.source.name} could not be "
                    f"inflated: its deflate stream is corrupt.",
                    kind="range",
                ) from None
        else:
            raise OsOpenError(
                f"{name!r} in {self.source.name} uses zip compression "
                f"method {member.method}, which this reader does not "
                f"support (only stored and deflate are read).",
                kind="range",
            )

        if len(data) != member.uncompressed_size:
            raise OsOpenError(
                f"{name!r} in {self.source.name} decompressed to "
                f"{len(data)} bytes, expected {member.uncompressed_size}.",
                kind="range",
            )
        return data

    def _read_central_directory(self) -> dict[str, ZipMember]:
        try:
            size = self.source.size()
            tail_size = min(size, _EOCD_SEARCH_WINDOW)
            tail = self.source.read(size - tail_size, tail_size)
        except CogError as exc:
            raise OsOpenError(
                f"Reading {self.source.name}'s own end failed: a range "
                f"request this reader depends on did not answer as "
                f"expected.",
                kind="range",
                status_code=getattr(exc, "status_code", None),
            ) from None

        eocd_pos = tail.rfind(_EOCD_SIGNATURE)
        if eocd_pos == -1:
            raise OsOpenError(
                f"{self.source.name} has no End Of Central Directory "
                f"record in its final {_EOCD_SEARCH_WINDOW} bytes, so it "
                f"is not a zip this reader can read.",
                kind="range",
            )
        (
            _disk_no, _disk_with_cd, _records_this_disk, total_records,
            cd_size, cd_offset, _comment_len,
        ) = struct.unpack_from(_EOCD_STRUCT, tail, eocd_pos + 4)

        if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
            raise OsOpenError(
                f"{self.source.name} is a zip64 archive (its End Of "
                f"Central Directory record carries a 0xFFFFFFFF marker); "
                f"this reader does not support zip64, which none of the "
                f"products it reads actually need.",
                kind="range",
            )

        members: dict[str, ZipMember] = {}
        offset = cd_offset
        for index in range(total_records):
            try:
                header = self.source.read(offset, 46)
            except CogError as exc:
                raise OsOpenError(
                    f"Reading {self.source.name}'s central directory "
                    f"failed: a range request this reader depends on did "
                    f"not answer as expected.",
                    kind="range",
                    status_code=getattr(exc, "status_code", None),
                ) from None
            if header[:4] != _CENTRAL_DIRECTORY_SIGNATURE:
                raise OsOpenError(
                    f"{self.source.name}'s central directory is corrupt: "
                    f"entry {index} does not start with the expected "
                    f"signature.",
                    kind="range",
                )
            (
                _version_made_by, _version_needed, flag, method,
                _mod_time, _mod_date, _crc32,
                compressed_size, uncompressed_size,
                name_len, extra_len, comment_len,
                _disk_start, _internal_attr, _external_attr,
                header_offset,
            ) = struct.unpack_from(_CENTRAL_DIRECTORY_STRUCT, header, 4)

            try:
                name_bytes = self.source.read(offset + 46, name_len)
            except CogError as exc:
                raise OsOpenError(
                    f"Reading {self.source.name}'s central directory "
                    f"failed: a range request this reader depends on did "
                    f"not answer as expected.",
                    kind="range",
                    status_code=getattr(exc, "status_code", None),
                ) from None
            # UTF-8 when the general purpose bit flag's language encoding
            # bit (11) is set, cp437 otherwise: the same rule `zipfile`
            # itself applies, and the one that matters for every real name
            # this project reads is moot either way (plain ASCII paths
            # like "data/OSOpenRoads_SS.gml" decode identically under
            # both).
            encoding = "utf-8" if flag & 0x0800 else "cp437"
            name = name_bytes.decode(encoding)

            members[name] = ZipMember(
                name=name,
                method=method,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                header_offset=header_offset,
            )
            offset += 46 + name_len + extra_len + comment_len

        return members


# --------------------------------------------------------------------------
# Versioned cache directories.
#
# One product can have several versions on disk at once, briefly: a
# survey is already reading last month's shards under the old version's
# directory while this month's fetch downloads and shards the new one
# into a directory of its own, and only once THAT has fully landed does
# anything delete the old one. `product_cache_dir` never collides across
# versions for exactly this reason, and `sweep_old_versions` is a separate
# call a caller makes only once it knows the new version is complete (see
# its own docstring), the same two-step shape bng.py's ensure_ostn15 and
# inspire.py's fetch_authority_zip both already use for their own caches.
# --------------------------------------------------------------------------


def cache_root() -> Path:
    """`~/.mapgen/osopen`.

    Resolved from `CONFIG_PATH`, imported into this module's own
    namespace above, rather than a second, independently chosen home:
    `CONFIG_PATH.parent` is the one directory `bng.py`'s own `_cache_path`
    and `inspire.py`'s own `_default_inspire_cache_dir` already treat as
    this tool's cache root, and every one of this module's own callers
    gets a subdirectory under it rather than crowding that root directly.
    A test isolating this cache monkeypatches `os_downloads.CONFIG_PATH`,
    the same mechanism test_inspire.py's own
    `test_fetch_authority_zip_default_cache_dir_is_under_the_mapgen_home`
    already uses for inspire.py's sibling cache: this reads the name at
    call time, so patching it after import still takes effect.
    """
    return CONFIG_PATH.parent / "osopen"


def product_cache_dir(product: str, version: str) -> Path:
    """`cache_root()/f"{product}_{version}"`, created (with any missing
    parent, including `cache_root()` itself) before it is returned.

    Every caller of this function is about to write files under the path
    it gets back (a raw downloaded zip, gzipped shards); creating it here
    means none of them has to remember its own `ensure_dir` call first.
    """
    path = cache_root() / f"{product}_{version}"
    ensure_dir(path)
    return path


def sweep_old_versions(product: str, keep_version: str) -> None:
    """Deletes every `f"{product}_*"` sibling of `product_cache_dir(product,
    keep_version)` under `cache_root()`, best-effort.

    Must only be called once a caller has finished building `keep_version`'s
    own shards successfully: an older version's cache is real, usable data
    right up until a newer one has fully replaced it, matching the same
    rule `inspire.py`'s own `_sweep_stale_months` documents for its
    month-stamped zips (and, one layer up, `fsutil.py`'s own "partial files
    are worse than absent ones"). This function itself does not check that
    `keep_version` is actually complete; that is the caller's own
    responsibility, exactly as it is for `_sweep_stale_months`.

    Survives `cache_root()` not existing at all: nothing to sweep is not a
    failure, and this is called from `fetch()`-shaped code that may run
    against a completely fresh cache.
    """
    root = cache_root()
    if not root.exists():
        return
    keep_dir = product_cache_dir(product, keep_version)
    for candidate in root.glob(f"{product}_*"):
        if candidate != keep_dir:
            best_effort_rmtree(candidate)
