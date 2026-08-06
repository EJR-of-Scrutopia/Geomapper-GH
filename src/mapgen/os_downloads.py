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
from mapgen.fsutil import ensure_dir
from mapgen.sources.base import ProgressSink

# Matching cog.py's own USER_AGENT exactly: one identifying string for
# every request mapgen makes to any of these hosts, rather than a second
# copy of the same sentence that could drift from it.
USER_AGENT = "mapgen/1.0 (architectural survey tool)"

OS_DOWNLOADS_BASE = "https://api.os.uk/downloads/v1"

_LISTING_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_TIMEOUT_SECONDS = 120.0
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


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
