"""The HMLR INSPIRE authority index: which zips cover a drawn extent.

HM Land Registry publishes INSPIRE Index Polygons as one zip per local
authority, 318 of them across England and Wales, with no whole-country
file and no live "which authority covers this point" service of its own.
Working out which zips a survey extent needs would otherwise mean
downloading and unzipping all 318 just to look inside, or scraping the
download page and an external boundary service on every single survey.
Neither is acceptable for a step that runs before the owner has committed
to downloading anything, so this module instead reads a small, committed
lookup table built once by `tests/fixtures/inspire/make_authority_index.py`
and never touches the network itself.

`AUTHORITY_INDEX_PATH` deliberately points into `tests/fixtures/inspire/`
rather than a data directory under this package: the table is generated
data with a documented generator beside it, exactly the
`tests/fixtures/ostn15/` convention, and `bridge.py`'s own
`UrbanoBridge.csproj` path already establishes that this project's
production code is read from a full checkout, not a package built for
separate distribution, so a second module reaching the same two
directories up is a continuation of an existing convention rather than a
new one.

Index loading and the intersection lookup were the first task in this
module. This file also carries the second: `fetch_authority_zip`, the
month-stamped disk cache and the cookie-session download that fills it,
and `parcels_in`, the streaming GML parser that turns one cached zip
into rings of British National Grid coordinates without ever holding the
whole file, compressed or not, in memory at once.

## The download: one session, one cookie handshake

`https://use-land-property-data.service.gov.uk/datasets/inspire/download`
sets a session cookie on a client's first hit and answers with a 302
back to itself; the retry, now carrying the cookie, gets the real page
(probed live 2026-08-06, and `make_authority_index.py` already leans on
it for the same reason). The zip endpoints these names resolve to sit
under the same host and the same mechanism, so `fetch_authority_zip`
makes exactly one `session.get` call per zip and relies on `requests.
Session`'s own automatic redirect and cookie handling to resolve that
hop invisibly, exactly like `bng.py`'s `_download_and_parse` already
does for the OSTN15 pack. A caller supplying a client that does not
carry cookies across a redirect (a bare `urllib` opener with no
`HTTPCookieProcessor`, in practice; never `requests.Session` itself)
would see that same 302 handed back as this function's own response,
which is why the explicit `status_code != 200` check below exists:
without it, that redirect's own small body would be written into the
cache as though it were the zip. test_inspire.py pins both shapes with
fakes built to the same two methods `requests.Session` exposes here
(`.get`, called with `stream=True` and a timeout, returning something
with `.status_code` and `.iter_content`), never a real network call.

## The parser: streaming in, streaming out

`parcels_in` opens the zip member with `zipfile.ZipFile.open` (a stream,
never `.read`, which would materialise the whole 81 MB GML before
parsing a byte of it) and feeds that stream to `xml.etree.ElementTree.
iterparse`. Every `wfs:member` element is cleared, and detached from the
collection root, the moment this module is done reading it (see
`ParcelStream`'s own docstring for how that is measured, not merely
asserted); nothing about this function's own memory use grows with
Bridgend's 69,506 parcels, or with any other authority's parcel count.

GML namespaces, exactly as the live file states them and exactly as this
module hard-codes them (no schema fetch, no negotiation): `wfs=
"http://www.opengis.net/wfs/2.0"`, `gml="http://www.opengis.net/gml/3.2"`,
`LR="www.landregistry.gov.uk"` (a bare string, not a URL: HM Land
Registry's own namespace declaration has no scheme on it, and this is
not this module's typo to fix). Features are `wfs:member/LR:PREDEFINED`;
geometry is `LR:GEOMETRY/gml:Polygon`, whose `srsName` must read
`"urn:ogc:def:crs:EPSG::27700"` (British National Grid) or the whole
parse refuses, because that attribute is a fact about the FILE, not
about any one parcel in it; rings are `gml:exterior/gml:LinearRing/
gml:posList` (space-separated easting/northing pairs) with `gml:
interior` rings, when present, carried after the exterior in document
order.
"""

from __future__ import annotations

import datetime
import json
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import requests

from mapgen.bng import BngError, ensure_ostn15, from_bng, load_ostn15, padded_bng_extent
from mapgen.boundary_curves import boundary_curves
from mapgen.config import CONFIG_PATH
from mapgen.egrid import PAD_METRES
from mapgen.fsutil import atomic_write_bytes, atomic_write_text, ensure_dir
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken
from mapgen.sources.base import (
    FAILURE_NO_OUTPUT,
    FAILURE_UNKNOWN,
    FAILURE_UNREACHABLE,
    Estimate,
    ProgressSink,
    TileFailure,
    classify_status_failure,
    classify_transport_failure,
)

AUTHORITY_INDEX_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "inspire" / "authority_index.json"
)

EXPECTED_AUTHORITY_COUNT = 318

# A hyphen is neither a space nor a path separator, which is the actual
# property that matters here: every one of these names becomes a URL path
# segment (the zip's download URL) and a cache filename stem in the tasks
# that follow this one. Five of the real 318 HMLR names carry a hyphen
# (Newcastle-under-Lyme and similar compound place names); see
# make_authority_index.py's module docstring for how that was found and
# why this is the right shape rather than a narrower one that would have
# to silently drop them.
_NAME_SHAPE = re.compile(r"^[A-Za-z0-9_.-]+$")


class InspireError(RuntimeError):
    """Raised when the committed authority index cannot be read or is not
    the shape this module and its callers depend on: present, exactly
    318 names, each a safe path segment, each a well-formed bbox. Every
    check here is about the committed fixture being trustworthy, not
    about anything a caller passed in; a bad bbox is BBoxError's job
    (see geo.py), not this module's.

    Also raised by `fetch_authority_zip` (a bad name, a non-200 response, a
    wrapped `requests.RequestException`) and by `parcels_in` (a zip this
    module cannot open at all, a zip that is corrupt or whose GML member is
    malformed whether discovered immediately or only partway through the
    read, a missing GML member, or a wrong `srsName`; see
    `_raise_corrupt_cache_file` for the corrupt-zip cases specifically,
    which also deletes the cache file before raising). A non-200 response
    sets `.status_code` (an int) on the instance before
    raising, the same structural convention `cog.py`'s `CogError.
    status_code` already establishes; every other raise site leaves it
    unset, which `getattr(exc, "status_code", None)` reads as None, "not
    a status failure". `InspireSource.fetch()` (Task 4) reads this
    directly, and reads `.__cause__` for the wrapped-RequestException
    case, rather than parsing either shape back out of this exception's
    own English sentence: see `classify_status_failure`/
    `classify_transport_failure` in `sources/base.py`.
    """


def load_authority_index(
    path: Path = AUTHORITY_INDEX_PATH,
) -> dict[str, tuple[float, float, float, float]]:
    """Every HMLR authority name to its padded WGS84 bbox (west, south,
    east, north), read from the committed JSON `make_authority_index.py`
    wrote. No network, no computation: the padding (1 km, see that
    script's own docstring) is already baked into every stored bbox, so
    this is a pure file read plus a shape check.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InspireError(f"Could not read the authority index at {path}.") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InspireError(f"The authority index at {path} is not valid JSON.") from exc

    if not isinstance(raw, dict):
        raise InspireError(f"The authority index at {path} must be a JSON object.")

    index: dict[str, tuple[float, float, float, float]] = {}
    for name, bbox in raw.items():
        if not _NAME_SHAPE.match(name):
            raise InspireError(
                f"Authority name {name!r} in {path} is not a safe path segment "
                f"(must match {_NAME_SHAPE.pattern})."
            )
        if not (isinstance(bbox, list) and len(bbox) == 4):
            raise InspireError(
                f"Authority {name!r} in {path} must map to a 4 element "
                f"[west, south, east, north] list, got {bbox!r}."
            )
        try:
            west, south, east, north = (float(value) for value in bbox)
        except (TypeError, ValueError) as exc:
            raise InspireError(
                f"Authority {name!r} in {path} has a non-numeric bbox value."
            ) from exc
        if not (west < east and south < north):
            raise InspireError(
                f"Authority {name!r} in {path} has a degenerate or inverted "
                f"bbox: {bbox!r}."
            )
        index[name] = (west, south, east, north)

    return index


def authorities_for(bbox: BBox) -> list[str]:
    """HMLR zip basenames (no `.zip`) for every authority whose padded
    bbox intersects `bbox`, sorted. Empty for an extent outside England
    and Wales entirely (Scotland, Northern Ireland, or open sea): INSPIRE
    Index Polygons only exist for the 318 authorities in the index, so an
    extent nowhere near any of them simply matches none, with no special
    case needed for "outside coverage".
    """
    index = load_authority_index()
    return sorted(
        name
        for name, (west, south, east, north) in index.items()
        if bbox.west <= east and bbox.east >= west and bbox.south <= north and bbox.north >= south
    )


# --------------------------------------------------------------------------
# Download and cache: fetch_authority_zip.
# --------------------------------------------------------------------------

INSPIRE_DOWNLOAD_URL_TEMPLATE = (
    "https://use-land-property-data.service.gov.uk/datasets/inspire/download/{name}.zip"
)

# 120 s, the same figure bng.py's own OSTN15 download uses for a
# similarly shaped one-shot GET: neither module has ever measured a
# reason to differ, and Bridgend's own 13.1 MB is a small fraction of
# what that timeout allows for even a slow link.
_DOWNLOAD_TIMEOUT_SECONDS = 120.0

_MONTH_STAMP_FORMAT = "%Y-%m"


def _default_inspire_cache_dir() -> Path:
    """`~/.mapgen/inspire/`: `CONFIG_PATH.parent` is the directory bng.py's
    own `_cache_path` already treats as this tool's one cache root, and
    this module gets its own subdirectory under it rather than crowding
    that root with up to 318 month-stamped zip names, one per authority.
    """
    return CONFIG_PATH.parent / "inspire"


def _current_month_stamp() -> str:
    return datetime.datetime.now().strftime(_MONTH_STAMP_FORMAT)


def _sweep_stale_months(cache_dir: Path, name: str, keep: Path) -> None:
    """Deletes every `<name>_*.zip` in `cache_dir` other than `keep`.

    Only ever called once a fresh download has already landed safely at
    `keep` (see `fetch_authority_zip`): an older month's copy is real,
    usable data right up until a newer one has fully replaced it, so
    this never runs before that replacement has succeeded, and never
    runs at all when a download attempt fails. `name` is safe to use as
    a literal glob prefix because `fetch_authority_zip` has already
    checked it against `_NAME_SHAPE`, which admits none of `*`, `?` or
    `[`.
    """
    for candidate in cache_dir.glob(f"{name}_*.zip"):
        if candidate != keep:
            candidate.unlink(missing_ok=True)


def _resolve_inspire_cache_dir(cache_dir: Path | None) -> Path:
    """`cache_dir` if given, otherwise `_default_inspire_cache_dir()`: the
    one-line ternary `fetch_authority_zip` itself resolves, factored out so
    `InspireSource.fetch()` (see its own `endpoints_used` bookkeeping) can
    resolve the SAME directory without a second, independently maintained
    copy of this ternary that could drift from the first.
    """
    return cache_dir if cache_dir is not None else _default_inspire_cache_dir()


def _authority_cache_path(name: str, resolved_cache_dir: Path) -> Path:
    """The month-stamped cache path `fetch_authority_zip` itself resolves
    `name` to, given an ALREADY-RESOLVED cache directory (see
    `_resolve_inspire_cache_dir`): pure path arithmetic, no filesystem
    access, no network.

    Factored out for one reason: `InspireSource.fetch()` needs to know,
    honestly, whether calling `fetch_authority_zip` is about to hit the
    network or a cache file already on disk, so it can record the
    authority's own download URL in `endpoints_used` only when a real
    request is about to happen (see that method's own docstring). Rather
    than have `fetch_authority_zip` change its own return shape to report
    that fact, which every one of this module's own tests and every other
    caller would then have to unpack, the source computes this SAME path
    with `.exists()` immediately before calling `fetch_authority_zip`. One
    function computing the path either way is what keeps the two call
    sites from ever quietly disagreeing about what "this month's cache
    file" means.
    """
    return resolved_cache_dir / f"{name}_{_current_month_stamp()}.zip"


def fetch_authority_zip(
    name: str, session: object, cache_dir: Path | None = None
) -> Path:
    """The month-stamped local copy of one HMLR authority's INSPIRE zip,
    downloading it through `session` if this month's copy is not
    already cached.

    `name` must be one of the committed authority index's own keys (see
    `load_authority_index`); it becomes both a URL path segment and this
    function's own cache filename, so it is checked against
    `_NAME_SHAPE` before anything else in this function runs, including
    before `cache_dir` is even resolved. A name that did not come from
    the committed index (a typo, a hand-built string) must never reach
    the network at all.

    A cache hit is a pure filesystem check: this month's
    `<name>_<YYYY-MM>.zip` already existing in `cache_dir` is returned
    immediately with no call to `session`, which test_inspire.py pins
    with a session that raises on any use. HM Land Registry republishes
    on the first Sunday of the month (the plan's own measured facts), so
    "this month" is the right grain to cache at: a survey run twice in
    the same month should never pay for the same download twice, and a
    survey run next month should not go on trusting a copy the service
    itself has since replaced.

    On a cache miss, the download streams straight to a temporary file
    beside the cache path and is renamed into place only once complete
    (`Path.replace`, atomic on the same filesystem), never
    `fsutil.atomic_write_bytes`. That helper takes the whole body as one
    `bytes` object, a fine trade for the 7 MB OSTN15 cache bng.py itself
    writes that way because that file has one fixed, known size; this
    function has no such bound (Bridgend measured 13.1 MB, but 318
    authorities exist and none of the rest has been measured), so it
    never holds a whole download in memory before writing it, matching
    the same streaming-to-a-temp-file shape bng.py's own
    `_download_and_parse` already uses for its own, larger, 41 MB pack.

    Stale months for this same name are swept only once the new
    download has landed successfully (`_sweep_stale_months`); a failed
    attempt leaves every existing cached zip, of any month, exactly as
    it found it.

    Raises `InspireError` for a bad name (before any network), a
    non-200 response (including the redirect shape a session that does
    not carry cookies across a hop would leave unresolved; see the
    module docstring's "one session, one cookie handshake" section), and
    any `requests.RequestException` the transfer itself raises. The
    latter is wrapped rather than left to propagate raw: a connection
    failure's own text from `requests` embeds the URL just requested,
    and this project's rule (see `sources/base.py`'s `TileFailure.reason`
    discipline) is that no URL crosses a module boundary in an exception
    message.
    """
    if not _NAME_SHAPE.match(name):
        raise InspireError(
            f"{name!r} is not a safe authority name (must match "
            f"{_NAME_SHAPE.pattern}); refusing to build a URL or a cache "
            f"filename from it."
        )

    resolved_cache_dir = _resolve_inspire_cache_dir(cache_dir)
    cache_path = _authority_cache_path(name, resolved_cache_dir)
    if cache_path.exists():
        return cache_path

    ensure_dir(resolved_cache_dir)
    url = INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name=name)
    # A random suffix, not just the pid: fetch_authority_zip is called
    # once per authority from the same process in Task 4's LayerSource,
    # so a bare pid-keyed name would collide with itself across the
    # authorities in one run.
    temp_path = cache_path.with_name(f"{cache_path.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        with session.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            if response.status_code != 200:
                # .status_code, set structurally rather than left for a
                # caller to regex out of the sentence above: Task 4's
                # InspireSource.fetch() classifies this failure through
                # classify_status_failure, and CogError.status_code
                # (cog.py) is the established precedent for exactly this
                # shape (see lidar_wales.py's own _classify_cog_error).
                # None (the ordinary case for every other InspireError
                # this module raises) means "not a status failure".
                error = InspireError(
                    f"HM Land Registry's INSPIRE download for {name} answered "
                    f"HTTP {response.status_code} instead of 200; this usually "
                    f"means the session did not carry the cookie the service's "
                    f"own redirect sets, and the body received is not the zip."
                )
                error.status_code = response.status_code
                raise error
            with temp_path.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        temp_path.replace(cache_path)
    except requests.RequestException as exc:
        temp_path.unlink(missing_ok=True)
        raise InspireError(
            f"Failed to download the INSPIRE Index Polygons for {name}."
        ) from exc
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    _sweep_stale_months(resolved_cache_dir, name, keep=cache_path)
    return cache_path


# --------------------------------------------------------------------------
# Streaming parcel parser: parcels_in.
# --------------------------------------------------------------------------

_WFS_NS = "http://www.opengis.net/wfs/2.0"
_GML_NS = "http://www.opengis.net/gml/3.2"
_LR_NS = "www.landregistry.gov.uk"

_MEMBER_TAG = f"{{{_WFS_NS}}}member"
_PREDEFINED_TAG = f"{{{_LR_NS}}}PREDEFINED"
_GEOMETRY_TAG = f"{{{_LR_NS}}}GEOMETRY"
_POLYGON_TAG = f"{{{_GML_NS}}}Polygon"
_EXTERIOR_TAG = f"{{{_GML_NS}}}exterior"
_INTERIOR_TAG = f"{{{_GML_NS}}}interior"
_LINEAR_RING_TAG = f"{{{_GML_NS}}}LinearRing"
_POS_LIST_TAG = f"{{{_GML_NS}}}posList"

# The one GML member every INSPIRE zip carries alongside its licence PDF
# (see the plan's own measured facts). Not discovered by scanning
# `archive.namelist()`: every zip probed carries exactly this name, and a
# fixed name that turns out to be wrong for some future authority fails
# loudly (`InspireError`, `_open_gml_member` below) rather than silently
# picking whichever member happens to sort first.
GML_MEMBER_NAME = "Land_Registry_Cadastral_Parcels.gml"

# The one srsName every parcel's geometry must carry (British National
# Grid). A whole-file property, not a per-parcel one (the plan's own
# binding detail): the first parcel whose Polygon disagrees ends the
# whole parse with InspireError, rather than being counted and skipped
# like a malformed posList, because a different projection is not one
# bad row, it is evidence that every coordinate already read out was
# never in the projection this function assumed.
_EXPECTED_SRS_NAME = "urn:ogc:def:crs:EPSG::27700"

_TIMESTAMP_YEAR_RE = re.compile(r"^(\d{4})-")


@dataclass
class ParseCounts:
    """A running, and, once iteration ends, final, tally of one
    `parcels_in` call. Mutable and updated in place by the `ParcelStream`
    that owns it, rather than replaced each time, so a caller holding the
    same `ParseCounts` object throughout a long parse always sees the
    current totals, whether read mid-stream or only once the loop over
    the parcels themselves has finished.
    """

    parcels_seen: int = 0
    parcels_kept: int = 0
    parcels_skipped_malformed: int = 0


class _MalformedParcel(Exception):
    """Raised internally to abandon and count ONE parcel; never seen
    outside this module (`ParcelStream._walk` always catches it). A
    wrong srsName is deliberately NOT this: see `_EXPECTED_SRS_NAME`'s
    own comment for why that raises `InspireError` instead.
    """


def _parse_ring(ring_container: ET.Element) -> list[tuple[float, float]]:
    """One `gml:exterior` or `gml:interior` element's own ring, as a list
    of (easting, northing) tuples.

    Raises `_MalformedParcel` for anything about the ring this module
    cannot trust: a missing `gml:LinearRing` or `gml:posList`, an odd
    number of coordinate tokens (a posList is always easting/northing
    pairs), or a token that will not parse as a float. Every one of
    these is "nothing fabricated" (the plan's own global constraint)
    written as code: a ring this function cannot fully understand is
    never partially returned.
    """
    linear_ring = ring_container.find(_LINEAR_RING_TAG)
    if linear_ring is None:
        raise _MalformedParcel("ring has no gml:LinearRing")
    pos_list = linear_ring.find(_POS_LIST_TAG)
    text = pos_list.text if pos_list is not None else None
    if not text or not text.strip():
        raise _MalformedParcel("ring has no gml:posList text")
    tokens = text.split()
    if len(tokens) % 2 != 0:
        raise _MalformedParcel("posList has an odd number of tokens")
    try:
        values = [float(token) for token in tokens]
    except ValueError:
        raise _MalformedParcel("posList has a non-numeric token") from None
    return [(values[i], values[i + 1]) for i in range(0, len(values), 2)]


def _extract_rings(member: ET.Element) -> list[list[tuple[float, float]]]:
    """One `wfs:member` element's rings, exterior first, then every
    interior ring in document order (see the module docstring's GML
    shape). Raises `_MalformedParcel` for anything missing along that
    path (`LR:PREDEFINED`, the geometry, the exterior ring itself) or an
    unparsable posList anywhere in it, and `InspireError` for a wrong
    srsName.
    """
    predefined = member.find(_PREDEFINED_TAG)
    if predefined is None:
        raise _MalformedParcel("no LR:PREDEFINED feature")
    polygon = predefined.find(f"{_GEOMETRY_TAG}/{_POLYGON_TAG}")
    if polygon is None:
        raise _MalformedParcel("no LR:GEOMETRY/gml:Polygon")

    srs_name = polygon.get("srsName")
    if srs_name != _EXPECTED_SRS_NAME:
        raise InspireError(
            f"This INSPIRE file's geometry uses srsName {srs_name!r}, not "
            f"the expected {_EXPECTED_SRS_NAME!r} (British National Grid); "
            f"refusing to read coordinates in an unrecognised projection."
        )

    exterior_container = polygon.find(_EXTERIOR_TAG)
    if exterior_container is None:
        raise _MalformedParcel("no gml:exterior ring")
    rings = [_parse_ring(exterior_container)]
    for interior_container in polygon.findall(_INTERIOR_TAG):
        rings.append(_parse_ring(interior_container))
    return rings


def _ring_bbox(ring: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    eastings = [point[0] for point in ring]
    northings = [point[1] for point in ring]
    return min(eastings), min(northings), max(eastings), max(northings)


def _bboxes_intersect(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    a_e_min, a_n_min, a_e_max, a_n_max = a
    b_e_min, b_n_min, b_e_max, b_n_max = b
    return (
        a_e_min <= b_e_max
        and a_e_max >= b_e_min
        and a_n_min <= b_n_max
        and a_n_max >= b_n_min
    )


def _parse_timestamp_year(raw: str | None) -> int:
    if not raw:
        raise InspireError(
            "This INSPIRE file's wfs:FeatureCollection root has no "
            "timeStamp attribute."
        )
    match = _TIMESTAMP_YEAR_RE.match(raw)
    if not match:
        raise InspireError(
            f"This INSPIRE file's timeStamp {raw!r} does not start with a "
            f"4-digit year."
        )
    return int(match.group(1))


def _open_gml_member(archive: zipfile.ZipFile):
    try:
        return archive.open(GML_MEMBER_NAME)
    except KeyError as exc:
        raise InspireError(
            f"This INSPIRE zip has no {GML_MEMBER_NAME!r} member."
        ) from exc


def _raise_corrupt_cache_file(zip_path: Path) -> None:
    """Deletes `zip_path` and raises `InspireError` naming it: the shared
    ending for every way `_walk` can learn that a zip it is reading is not
    a valid one, whether that shows up immediately (`zipfile.ZipFile`
    itself refuses the header) or only partway through the read (a CRC
    failure on a member's compressed bytes, or `ET.iterparse` refusing
    the GML inside once decompressed).

    `zip_path` is always `fetch_authority_zip`'s own month-stamped cache
    path (see its own docstring), whether THIS call just downloaded it or
    found it already there: a corrupt zip is a fact about that one file
    on disk, not about the network or about the source data HMLR actually
    publishes, and left in place it would go on answering every retry
    this month identically, since `fetch_authority_zip`'s own cache-hit
    check has no way to tell a corrupt file from a good one apart from
    reading it (the very thing that just failed). Deleting it here, before
    raising, is the same self-heal `bng.py`'s own `ensure_ostn15` already
    applies to a shift-grid cache that fails its own read checks, for the
    same reason that docstring gives: the alternative is a failure sticky
    for the rest of the month, until the owner is told, somewhere, to go
    and delete a file by hand.

    Raises with neither `.status_code` nor a chained cause (`from None`),
    deliberately: `_classify_inspire_error` reads both to tell a status or
    transport failure from a parse failure, and this is always the
    latter, the same as `_extract_rings`' own wrong-srsName raise a few
    lines above in this same file.
    """
    zip_path.unlink(missing_ok=True)
    raise InspireError(
        f"{zip_path} could not be read as a valid INSPIRE zip; the cached "
        f"copy has been deleted so the next attempt downloads it again."
    ) from None


class ParcelStream:
    """The iterator `parcels_in` returns.

    A small class wrapping a generator, rather than the generator
    itself, because `parcels_in`'s own brief asks for two facts that only
    make sense to read once the walk is under way or complete: how many
    parcels were seen, kept and skipped, and the collection's own
    `timeStamp` year. A bare generator has nowhere to hang either one
    that survives past the last `yield`; `for parcel in parcels_in(...):
    ...` would leave a caller holding only that loop variable's last
    value. `.counts` and `.timestamp_year` live on this object instead,
    updated as `_walk` advances, so a caller can read either one
    mid-stream (a progress indicator) or, as test_inspire.py's own tests
    do, once the `for` loop above has finished.

    `.peak_live_members` is the memory-guard hook the plan's own global
    constraints ask for: the largest number of `wfs:member` elements
    simultaneously attached to the tree `iterparse` is building, at any
    point during the walk. Measured directly, right before each member
    is cleared (see `_walk`), rather than assumed.

    It does NOT settle at 1. `ET.iterparse` reads its source in fixed
    16 KB chunks (see its own stdlib source, `xml/etree/ElementTree.py`)
    and feeds each whole chunk to the underlying C parser in one call,
    which attaches every element that closes within that chunk to the
    tree before this loop gets to clear any of them; this module's own
    experiment (see the Task 2 report) measured a real HMLR-shaped file
    settling at a peak of 54, matching 16 KB divided by that file's own
    ~311 byte average member size, and staying at 54 whether the file
    held 500 members or 20,000. THAT plateau, not a literal 1, is the
    property worth pinning: a parser that forgot to clear (or forgot to
    detach the cleared member from the root beside it) shows a peak
    equal to `parcels_seen` itself, growing linearly with the file, while
    a correctly streaming one plateaus at roughly one read chunk's worth
    and stays there regardless of how many more parcels follow.
    test_inspire.py's own memory-guard test compares two synthetic files
    of very different sizes for exactly this shape of difference, rather
    than asserting one fixed number that depends on average member size.
    """

    def __init__(self, zip_path: Path, bbox_bng: tuple[float, float, float, float]) -> None:
        self.counts = ParseCounts()
        self.timestamp_year: int | None = None
        self.peak_live_members = 0
        self._generator = self._walk(Path(zip_path), bbox_bng)

    def __iter__(self) -> "ParcelStream":
        return self

    def __next__(self) -> list[list[tuple[float, float]]]:
        return next(self._generator)

    def _walk(self, zip_path: Path, bbox_bng: tuple[float, float, float, float]):
        try:
            archive = zipfile.ZipFile(zip_path)
        except OSError as exc:
            # A file this module could not even open (missing, permission
            # denied): a fact about ACCESS, not about the cached bytes
            # themselves, so this is left exactly as it was, with no
            # self-heal unlink (there may be nothing there to unlink at
            # all, or unlinking on a permission error would just raise a
            # second, more confusing one).
            raise InspireError(f"Could not open {zip_path} as a zip file.") from exc
        except zipfile.BadZipFile:
            # A file that opened as bytes but is not a zip at all (a bad
            # header): the same "corrupt cache" case the iteration-body
            # catch below exists for, just caught earlier. See
            # _raise_corrupt_cache_file's own docstring.
            _raise_corrupt_cache_file(zip_path)

        # A review finding (Item 2): the try/except above used to be the
        # ONLY thing in this method that ever wrapped a raw exception into
        # InspireError. Everything from here down ran unguarded, so a
        # cached zip that opens fine as a CONTAINER but fails partway
        # through being READ (a CRC failure on the GML member's own
        # compressed bytes, `zipfile.BadZipFile`, raised lazily as
        # `member_stream` is pulled through by `iterparse`) or a GML
        # member whose XML is malformed (`ET.ParseError`, a truncated
        # download being the ordinary way this happens) escaped straight
        # out of this generator, past `parcels_in`'s own caller and past
        # `InspireSource.fetch()`'s `except InspireError`, ending the
        # whole survey with an unclassified exception and leaving
        # tile_failures empty. Both are exactly the same kind of fact as
        # the BadZipFile above, just discovered later, so both get the
        # same treatment.
        try:
            with archive:
                with _open_gml_member(archive) as member_stream:
                    context = ET.iterparse(member_stream, events=("start", "end"))
                    _, root = next(context)
                    self.timestamp_year = _parse_timestamp_year(root.get("timeStamp"))

                    for event, elem in context:
                        if event != "end" or elem.tag != _MEMBER_TAG:
                            continue

                        # Measured BEFORE this member is cleared: see
                        # ParcelStream's own docstring for why this plateaus
                        # at roughly one iterparse read chunk's worth of
                        # members rather than settling at 1, and why that
                        # plateau, not a literal 1, is what stays flat as
                        # the file grows.
                        self.peak_live_members = max(self.peak_live_members, len(root))

                        self.counts.parcels_seen += 1
                        try:
                            rings = _extract_rings(elem)
                        except _MalformedParcel:
                            self.counts.parcels_skipped_malformed += 1
                            rings = None
                        finally:
                            elem.clear()
                            root.clear()

                        if rings is not None and _bboxes_intersect(
                            _ring_bbox(rings[0]), bbox_bng
                        ):
                            self.counts.parcels_kept += 1
                            yield rings
        except (zipfile.BadZipFile, ET.ParseError):
            _raise_corrupt_cache_file(zip_path)


def parcels_in(
    zip_path: Path, bbox_bng: tuple[float, float, float, float]
) -> ParcelStream:
    """Every parcel in `zip_path`'s GML member whose EXTERIOR ring's own
    bounding box intersects `bbox_bng`, as rings: a list per parcel,
    exterior ring first and any interior rings after it in document
    order, each ring a list of (easting, northing) float tuples in
    British National Grid metres.

    `bbox_bng` is `(e_min, n_min, e_max, n_max)`, the same shape and
    order `bng.padded_bng_extent` returns (Task 4 promotes this from
    `lidar_wales._padded_bng_extent` into `bng.py` so `InspireSource.
    fetch()`, below, and `lidar_wales.py` share one arithmetic, rather
    than two copies of it); a caller starting from a WGS84 `BBox`
    projects it with `bng.to_bng` first, exactly as that module does.

    Streams the whole way: see the module docstring's "the parser:
    streaming in, streaming out" section, and `ParcelStream`'s own
    docstring for how the memory discipline that requires is measured
    rather than merely asserted.

    Returns a `ParcelStream`: an iterator with two extra attributes a
    caller can read mid-stream or after the loop ends, `.counts` (a
    `ParseCounts`) and `.timestamp_year` (the collection's own
    `timeStamp` year, read off the root element before the first parcel
    is even reached).

    A malformed parcel (see `_parse_ring`, `_extract_rings`) is counted
    in `.counts.parcels_skipped_malformed` and never raised: the
    ordinary case of one bad row among tens of thousands must not end
    the whole survey. A wrong `srsName` is different in kind, not
    degree, and raises `InspireError` immediately, naming the value
    found, rather than being folded into the malformed count.
    """
    return ParcelStream(zip_path, bbox_bng)


# --------------------------------------------------------------------------
# Task 4: InspireSource, the LayerSource wiring and the boundaries GeoJSON.
#
# fetch() writes exactly two work files, PARCELS_WORK_NAME and
# META_WORK_NAME, once ALL selected authorities have downloaded and parsed
# successfully: nothing is written mid-loop, so a failure partway through a
# multi-authority extent leaves work_dir exactly as empty as it found it,
# rather than a parcels.jsonl a later skip-on-resume check might mistake
# for a completed fetch. This is the same "partial files are worse than
# absent ones" rule fsutil.py's own module docstring states, applied at
# the granularity of "the whole per-extent fetch", not just one file's own
# write.
#
# merge() is the one place this module imports mapgen.boundary_curves: it
# reads PARCELS_WORK_NAME back, one JSON array-of-rings per line, runs the
# whole dedup-and-chain pipeline over every authority's kept parcels at
# once (a shared edge between two DIFFERENT authorities' own files, the
# documented boundary-crossing-parcel case, only collapses if both
# authorities' parcels are handed to boundary_curves TOGETHER), and
# unprojects the result through from_bng into the one GeoJSON the brief
# names.
# --------------------------------------------------------------------------

# The work_dir file names fetch() writes and merge() reads back BY NAME,
# never by position (the ElevationSource.merge lesson every other source
# in this project's sources/ package now follows).
PARCELS_WORK_NAME = "parcels.jsonl"
META_WORK_NAME = "meta.json"

# The brief's own sentence, verbatim. Doubles as the refusal for a corner
# authorities_for approved (a real England/Wales authority's own padded
# bbox) but that padded_bng_extent's own to_bng call cannot place on the
# National Grid at all: OSTN15's grid spans the whole of Great Britain, so
# this is unreachable for any real England/Wales extent in practice, and
# is handled the same defensive way lidar_wales.py treats the identical
# shape of BngError (see fetch()'s own docstring).
OUTSIDE_ENGLAND_AND_WALES_MESSAGE = (
    "INSPIRE Index Polygons cover England and Wales only, and this extent "
    "is outside both."
)

# The two GeoJSON feature properties the brief names verbatim (the third,
# "year", is per-package rather than a fixed string).
BOUNDARY_SOURCE_LABEL = "HM Land Registry INSPIRE Index Polygons"
BOUNDARY_INDICATIVE_NOTE = (
    "The extent of the land contained in any registered title cannot be "
    "established from the INSPIRE Index Polygons."
)

# --------------------------------------------------------------------------
# Measured constants, refit in Task 7 (2026-08-06) from every live sample on
# record: the plan's own probe of Bridgend, plus Task 2's and Task 4's own
# live tests of Vale of Glamorgan (see both tasks' reports in
# .superpowers/sdd/2026-08-06-mapgen-phase2-02-inspire-curves/). Still thin
# in the sense every other source's own comment names: two authorities of
# 318, both mid-sized Welsh unitary/county boroughs, nothing sampled from a
# large metropolitan authority (Cardiff, Birmingham, Leeds) that could plainly
# hold more parcels and more bytes than either. Both constants below carry a
# real MARGIN over the largest figure actually measured, not mere equality
# with it, which is the change this refit makes: the PREVIOUS figures were
# each pinned close to, or exactly at, a single sample, with no protection
# against a bigger authority or a slower day than the one that sample came
# from (see tests/test_inspire.py's own two covering tests for the exact
# arithmetic, RED against the pre-refit values and GREEN against these).
# --------------------------------------------------------------------------

# Two real authorities now measured: Bridgend_County_Borough_Council.zip
# 13,128,716 bytes (the plan's own live probe, 2026-08-06), and
# Vale_of_Glamorgan_Council.zip 13,689,747 bytes, measured twice on
# different days (Task 2's and Task 4's own live tests) and unchanged
# between them, so it is the more trustworthy of the two samples as well as
# the larger. The two are close (about 4% apart), which is some evidence
# that authorities of this kind cluster in this range, but with 316 of 318
# still unsampled and the tail entirely unknown, 16,000,000 keeps the same
# "round, deliberately generous" shape the previous figure had while
# clearing the new, larger confirmed sample by roughly 17% rather than the
# roughly 10% the old 15,000,000 gave it (14.2% over Bridgend alone, but
# only 9.6% once Vale of Glamorgan's own, independently confirmed, larger
# figure is the one that has to be cleared). An under-estimate is a
# countdown that runs out while the work is still going, which reads as a
# hang; see elevation.py's own SECONDS_FLOOR history for the same reasoning
# applied to time instead of bytes.
BYTES_PER_AUTHORITY = 16_000_000

# Task 2's live test (`pytest tests/test_inspire.py -m live`) measured
# 7.25 s of WALL TIME for one authority's whole fetch+parse, end to end:
# Vale_of_Glamorgan_Council.zip downloaded cold through the real cookie
# handshake (4.76 s, 13,689,747 bytes) plus parcels_in streamed over the
# real 65,276-member file down to the Llantwit Major bbox (2.06 s), inside
# one pytest invocation whose own collection and fixture overhead is folded
# into that 7.25 s along with the two measured phases (which sum to 6.82 s
# on their own). Task 4's own standalone script (no pytest overhead)
# measured fetch()+merge() together over the SAME zip, on a different day,
# at only 3.734 s: a swing of nearly 2x for what is meant to be the same
# piece of work, real variance in this one service's own response time
# rather than something a bigger sample would average away. The PREVIOUS
# figure was Task 2's own 7.25 s used verbatim, with no margin above itself
# at all; this refit keeps 7.25 s as the worse of the two real
# measurements and adds a deliberate margin on top of it, rather than
# trusting that no future run lands on a day slower than either one seen so
# far. 8.0 clears 7.25 by just over 10%, a clean number rather than a
# decimal that would read as more precise than two samples justify.
SECONDS_FLOOR = 8.0

# This term prices a LARGE, multi-authority extent's transfer time
# (bytes_estimate / this rate), so the safe choice is a rate genuinely
# BELOW the slowest download rate now on record, not an average of them
# and not that slowest rate's own rounded value. 13,689,747 bytes / 7.25 s
# (Task 2's own pytest-inclusive measurement) is about 1,888,000 bytes/s
# (1.89 MB/s to two significant figures), still the slowest rate this
# project has measured against this service even after Task 4's own, much
# faster, 13,689,747 bytes / 3.229 s (fetch() alone) at about 4.24 MB/s on
# a different day. A review finding: the previous figure here, 1,900,000,
# was marginally ABOVE that 1,888,000 slowest measurement, the wrong side
# of it despite this comment's own stated rationale, which UNDER-reads the
# time needed on a day that behaves like Task 2's rather than Task 4's,
# the same failure elevation.py's own SECONDS_FLOOR history warns an
# under-estimate always is. 1,800,000 clears 1,888,000 with a genuine
# margin below it instead, kept at two significant figures, the same
# convention every other thin-evidence rate constant in this project's
# sources/ package uses (see lidar_wales.py's BYTES_PER_SECOND_ESTIMATE).
BYTES_PER_SECOND_ESTIMATE = 1_800_000.0


def _classify_inspire_error(exc: InspireError) -> tuple[str, str, str]:
    """The (kind, verb, phrase) one authority's own download-or-parse
    failure implies, read structurally off `exc` rather than out of its
    own English sentence.

    `.status_code`, when `fetch_authority_zip` set it (a non-200
    response), goes straight to `classify_status_failure`: the same
    structural precedent `cog.py`'s `CogError.status_code` establishes,
    and read the same defensive way `lidar_wales.py`'s own
    `_classify_cog_error` reads it.

    Otherwise, `exc.__cause__`: every OTHER `InspireError`
    `fetch_authority_zip` raises wraps a real `requests.RequestException`
    with `raise ... from exc` (a bad name never reaches this function at
    all, since `authorities_for`'s own names always pass `_NAME_SHAPE`),
    so the original exception is still attached and `classify_transport_
    failure` (which accepts any `BaseException` and falls back to
    FAILURE_UNKNOWN for anything it does not recognise) is always safe to
    call on it. `InspireError`s from `parcels_in` (a corrupt zip, a wrong
    srsName, a missing GML member) have neither a status code nor a
    chained transport exception, and fall through to the last line: a
    parse failure is not a transport or status failure, and FAILURE_UNKNOWN
    is this project's own honest term for "cannot say which recognised
    cause this is", never retried blind.

    `verb` is the sentence's own opening word, distinguishing the two
    shapes of failure for the owner's benefit, not just for `kind`'s: a
    status or transport failure is a real download that did not complete
    ("Failed to download"), while a parse failure is a download that DID
    complete, of a file this module could not then make sense of ("Could
    not read"). Composed here, in the one place that already knows which
    branch fired, rather than as a second check in `fetch()`'s own except
    clause risking disagreeing with this function's own branch (a review
    finding: an earlier version always said "Failed to download", even
    for a wrong-srsName or corrupt-zip failure that had, in fact,
    downloaded successfully).
    """
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        kind, phrase = classify_status_failure(status_code)
        return kind, "Failed to download", phrase
    if exc.__cause__ is not None:
        kind, phrase = classify_transport_failure(exc.__cause__)
        return kind, "Failed to download", phrase
    return FAILURE_UNKNOWN, "Could not read", "could not be parsed"


def _read_parcels_jsonl(path: Path):
    """Every parcel `PARCELS_WORK_NAME` holds, one line at a time: fetch()'s
    own write shape, undone. Each line is a JSON array of rings (exterior
    first, then any interior rings), each ring an array of [easting,
    northing] pairs; `boundary_curves` only ever indexes a point's two
    coordinates positionally, so handing it JSON's own nested lists
    straight through, rather than rebuilding them as tuples first, is
    correct as well as simpler.
    """
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip():
            yield json.loads(line)


def _total_kept_from_meta(meta_path: Path) -> int | None:
    """The sum of every authority's own `parcels_kept` recorded in
    `meta_path`, or `None` if `meta_path` cannot be trusted as a completed
    fetch's own meta at all: missing, unparseable, not a JSON object, or
    missing or malformed `authorities`/`parcels_kept`/`year` keys (the
    same shape `fetch()` itself always writes and `merge()` itself always
    reads back).

    `None` means "cannot tell, so do not skip", never "assume zero": a
    corrupt or truncated `meta.json` must not let a real, non-empty
    `parcels.jsonl` skip re-validation on the strength of a file this
    function could not actually read, which is exactly the review finding
    this function exists to close (see `fetch()`'s own docstring, "the
    fix").
    """
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        authorities = meta["authorities"]
        if not isinstance(authorities, list):
            raise TypeError("authorities is not a list")
        total_kept = sum(int(entry["parcels_kept"]) for entry in authorities)
        int(meta["year"])  # merge()'s own requirement; checked here too.
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return total_kept


class InspireSource:
    id = "inspire"
    display_name = "Property boundaries (INSPIRE)"
    licence = "Open Government Licence v3.0"
    attribution = (
        "This information is subject to Crown copyright and database rights "
        "[year] and is reproduced with the permission of HM Land Registry. "
        "The polygons (including the associated geometry, namely x, y "
        "co-ordinates) are subject to Crown copyright and database rights "
        "[year] Ordnance Survey AC0000851063."
    )
    requires_api_key = False
    # A plain, source-specific attribute (see sources/base.py's own
    # documentation of this convention: LayerSource attributes beyond the
    # protocol are read defensively via getattr by whatever needs them).
    # package.py's `_source_provenance` (checked directly against its own
    # source, 2026-08-06) reads a FIXED set of such optional attributes
    # (endpoints_used, merged_features, fetched_types/types, demtype,
    # routing_note) and none of them is this one, so registering this
    # source alone does not put the conditions link into survey.json.
    # Task 5's fusion step is what carries it there, alongside the two
    # attribution statements above with `[year]` substituted; this task
    # deliberately does not extend `_source_provenance` speculatively to
    # read an attribute nothing yet consumes (see this task's own report).
    conditions_url = "https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions"

    def __init__(
        self,
        session: object | None = None,
        ostn15_cache_dir: Path | None = None,
        inspire_cache_dir: Path | None = None,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        # None means "the default" for each: bng.py's own ~/.mapgen
        # (ensure_ostn15/load_ostn15's own cache_dir=None resolution) and
        # this module's own ~/.mapgen/inspire (fetch_authority_zip's own
        # cache_dir=None resolution, _default_inspire_cache_dir). A test
        # or a caller wanting an isolated cache passes a tmp_path for
        # either, independently.
        self._ostn15_cache_dir = ostn15_cache_dir
        self._inspire_cache_dir = inspire_cache_dir
        # Reset at the top of every fetch(); see sources/base.py's own
        # documentation of this optional LayerSource extension.
        self.tile_failures: list[TileFailure] = []
        # Every authority zip's own download URL, but ONLY for an authority
        # this fetch() call actually downloaded: a month-stamped cache hit
        # (see fetch_authority_zip) appends nothing, matching OsmSource's
        # own endpoints_used docstring ("a tile skipped because it was
        # already downloaded... contributes nothing to this list") and its
        # own convention of recording only once the request is known to have
        # succeeded (see fetch()'s own docstring, "endpoints_used" section,
        # for how this source tells "about to hit the network" apart from
        # "about to read a cache file" without changing fetch_authority_zip's
        # own return shape).
        #
        # NOT reset at the top of fetch(), unlike tile_failures immediately
        # above (a review finding): tile_failures is documented PER-CALL
        # state, describing only the most recent fetch() so package.py's
        # retry pass sees just what THAT attempt could not deliver.
        # endpoints_used is PER-RUN state instead, initialised here in
        # __init__ and never touched again inside fetch() itself, because
        # package.py's retry pass calls fetch() again on this SAME
        # instance: a pass 1 that downloads Authority_A, records its URL,
        # then fails on Authority_B must not have A's own URL wiped by a
        # reset at the top of pass 2, in which A is now a cache hit that
        # records nothing new. What gives each SEPARATE survey a clean
        # start instead is InspireSource.configure() (below), the seam
        # package.py's _configured_sources uses to hand every request a
        # fresh instance rather than ever reusing the one
        # register_default_sources() built once for the life of the
        # process.
        self.endpoints_used: list[str] = []

    def configure(self) -> "InspireSource":
        """Returns a fresh InspireSource sharing this instance's transport
        and cache configuration, never mutating self.

        Unlike OvertureSource.configure(types), OsmSource.configure
        (categories) or ElevationSource.configure(demtype), InspireSource
        has no request-scoped SELECTION to pass through: every request
        that chooses "inspire" wants the same authorities for the same
        bbox, the same parser, the same output. What it needs a fresh
        copy for instead is `endpoints_used`'s own new lifetime (see
        fetch()'s docstring, "endpoints_used"): that list now survives
        package.py's retry pass calling fetch() again on one instance,
        which is a genuine WITHIN-one-survey requirement, but
        `register_default_sources()` builds and registers exactly one
        InspireSource for the life of the whole server process. Without
        this seam, that single registered instance would go on
        accumulating URLs across every survey ever run through it, not
        merely across one survey's own retries, the moment the reset this
        task removed from fetch() stopped clearing it between requests.

        package.py's `_configured_sources` calls this with no arguments at
        all (the one case among the four sources with a `configure`
        extension where "configure" means only "give me a clean copy",
        not "give me a copy scoped to X"): VERIFY the call shape there
        before assuming a future parameter belongs on this signature.

        `type(self)(...)`, not `InspireSource(...)`, for the same reason
        ElevationSource.configure gives at length: a subclass built to
        fail in a specific, reproducible way for a test must stay that
        subclass through configure(), not silently become a plain
        InspireSource that would make a real request.
        """
        return type(self)(
            session=self.session,
            ostn15_cache_dir=self._ostn15_cache_dir,
            inspire_cache_dir=self._inspire_cache_dir,
        )

    # -- covers / tier (mapgen.resolver) --------------------------------------

    def covers(self, bbox: BBox) -> str:
        """"full" when every one of `bbox`'s four corners falls inside at
        least one indexed authority's own padded bbox, "partial" when at
        least one corner does, "none" when none does.

        A bbox-level approximation of "does INSPIRE actually cover this
        ground", not an exact one: a genuinely irregular authority
        boundary could, in principle, leave a corner inside the
        authority's own bbox but outside its real shape, or the reverse.
        Safe at survey scale because every stored bbox is already 1 km
        padded (`make_authority_index.py`'s own module docstring), the
        same margin `authorities_for` itself relies on for its own
        rectangle-intersection test; this is that same committed index,
        read directly rather than through `authorities_for`, because
        `authorities_for` answers "does this AUTHORITY'S bbox intersect
        the survey bbox at all", a different, coarser question than "is
        this specific CORNER inside one", and a `BBox` cannot express a
        single point to ask `authorities_for` that question through
        (`BBox` refuses west == east or south == north as degenerate).
        `load_authority_index()` is a committed file read, never the
        network, matching `authorities_for`'s own "no network" guarantee.
        """
        index = load_authority_index()
        corners = (
            (bbox.west, bbox.south),
            (bbox.east, bbox.south),
            (bbox.west, bbox.north),
            (bbox.east, bbox.north),
        )
        covered = [
            any(west <= lon <= east and south <= lat <= north for west, south, east, north in index.values())
            for lon, lat in corners
        ]
        if all(covered):
            return "full"
        if any(covered):
            return "partial"
        return "none"

    def tier(self, category: str) -> int | None:
        """This source's own tier, mapgen.resolver's shared table:
        boundaries only, at tier 1 (the one source in this project that
        serves it at all).
        """
        return 1 if category == "boundaries" else None

    # -- estimate ------------------------------------------------------------

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        """Bytes and seconds for every authority `authorities_for(bbox)`
        names, with no network at all: the authority index is a committed
        file (`load_authority_index`), not a live lookup.

        Zero authorities is zero bytes and the floor's own seconds ONLY IF
        that would be honest, and it would not be: zero authorities means
        `fetch()` refuses before touching anything (see its own docstring),
        so zero real work is genuinely zero seconds, not SECONDS_FLOOR's
        "at least one handshake and one zip" floor, which describes work
        that is never attempted here. `max(..., SECONDS_FLOOR)` is
        deliberately not applied on this branch for exactly that reason.
        """
        authorities = authorities_for(bbox)
        if not authorities:
            return Estimate(bytes_estimate=0, seconds_estimate=0.0)
        bytes_estimate = len(authorities) * BYTES_PER_AUTHORITY
        seconds_estimate = max(bytes_estimate / BYTES_PER_SECOND_ESTIMATE, SECONDS_FLOOR)
        return Estimate(bytes_estimate=bytes_estimate, seconds_estimate=seconds_estimate)

    # -- fetch -----------------------------------------------------------------

    def _record_tile_failures(self, tiles: Sequence[Tile], kind: str, reason: str) -> None:
        """Record why this fetch() could not deliver, once per tile.

        Matches ElevationSource/LidarWalesSource exactly: one whole-extent
        failure (no authorities, an OSTN15 failure, one authority's own
        download or parse failure) did not arrive for every tile equally,
        and `reason` is always composed from the fixed vocabulary plus, at
        most, an authority NAME (never a URL) and an HTTP status.
        """
        self.tile_failures = [
            TileFailure(source=self.id, tile_id=tile.tile_id, kind=kind, reason=reason)
            for tile in tiles
        ]

    def _record_endpoint(self, endpoint: str) -> None:
        """Append `endpoint` to `endpoints_used` unless it is already
        there. Deduplicated, first-seen order, the same shape
        `OsmSource._record_endpoint` already establishes; a distinct
        method rather than an inline `if` at the one call site so a
        future second call site (a retried authority, say) cannot
        quietly duplicate the check.
        """
        if endpoint not in self.endpoints_used:
            self.endpoints_used.append(endpoint)

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        """Every authority `authorities_for(bbox)` names, downloaded through
        ONE `requests.Session` (this instance's own `self.session`, shared
        with the OSTN15 grid fetch below: both hosts are fine on one
        `requests.Session`, which keys cookies per-domain), parsed against
        the padded BNG extent, and written as PARCELS_WORK_NAME/
        META_WORK_NAME once every authority has succeeded.

        Refuses immediately, before writing anything and before any
        network call, when `authorities_for(bbox)` names none:
        `OUTSIDE_ENGLAND_AND_WALES_MESSAGE`, `FAILURE_NO_OUTPUT`, raised as
        `InspireError`. The SAME sentence and kind cover a corner
        `authorities_for` approved but `padded_bng_extent`'s own `to_bng`
        cannot place (see that constant's own comment): both mean, from
        this method's point of view, "no INSPIRE data for this extent
        either way".

        Skips before any of the above when `meta.json` witnesses a
        completed fetch AND `parcels.jsonl`'s own byte count agrees with
        it: an ordinary resumed run should not recompute
        `authorities_for`, let alone re-download, merely to reach a
        refusal or a repeat of last time's own successful download.

        THE FIX (a review finding): an earlier version skipped only when
        BOTH files were non-empty, on the reasoning that a completed
        fetch always writes real bytes to both. That reasoning was wrong
        for the ordinary case of a genuinely empty, successful result:
        every authority `authorities_for` names having zero kept parcels
        in this padded extent (a coastal extent whose padded authority
        bbox brushes sea, say) is a legitimate, complete fetch that
        writes a 0-byte `parcels.jsonl` by construction (`"".join(...)`
        over an empty list), and the old check re-downloaded the full
        authority zip and re-parsed it, forever, on every subsequent run
        over that same extent, for no reason: nothing about it was ever
        going to change.

        `meta.json` is the one true completion marker, not `parcels.
        jsonl`'s own byte count, because it is `atomic_write_text`'d
        LAST, after `parcels.jsonl` (see the code below): its mere
        existence already witnesses that every authority succeeded and
        every write below it completed. What its own byte count cannot
        do alone is tell a genuinely empty result apart from a truncated
        one (a process killed between the two atomic writes, leaving a
        real, non-empty `parcels.jsonl` from THIS attempt but the
        write that would have replaced it with more, or an OLD, stale
        `meta.json` from a previous attempt at a different bbox sitting
        beside a fresh, empty `parcels.jsonl`). `_total_kept_from_meta`
        reads `meta.json`'s own claimed total kept count, and the skip
        fires only when `parcels.jsonl` being empty agrees exactly with
        that claim being zero (`(parcels_path.stat().st_size == 0) ==
        (total_kept == 0)`): a 0-byte `parcels.jsonl` beside a meta
        claiming `kept > 0` does NOT skip (the truncation case the old
        non-empty check was actually guarding, still guarded), and,
        symmetrically, a non-empty `parcels.jsonl` beside a meta claiming
        `kept == 0` does not skip either (the same inconsistency, the
        other way round). A `meta.json` that will not parse, or is
        missing the keys this check or `merge()` itself needs, makes
        `_total_kept_from_meta` return `None`, which never skips.

        One authority's own download or parse failure (`InspireError` from
        `fetch_authority_zip` or `parcels_in`) ends the whole fetch
        immediately, classified via `_classify_inspire_error` into
        `tile_failures` against every handed tile, and re-raised unchanged:
        this mirrors ElevationSource/LidarWalesSource's own single-shot
        shape (no continue-past-failure retry loop across authorities, the
        way OsmSource continues across TILES), since a multi-authority
        extent's authorities are all needed for one coherent boundaries
        file, not independent, separately-useful outputs.

        Cancel checkpoints: top of the method (before even the skip
        check), after `authorities_for` succeeds and before the OSTN15
        grid fetch, and once per loop iteration before that authority's own
        download starts (so a stop lands between whole authorities, never
        mid-download, matching every other source's own convention).

        **endpoints_used** (a review finding: this was previously never
        populated at all, which made survey.json's own documented promise,
        "which real URLs were actually contacted this run", false for this
        source on every run, including one that genuinely downloaded a
        zip). Per authority, BEFORE calling `fetch_authority_zip`, this
        method checks whether that authority's own month-stamped cache
        file already exists (`_authority_cache_path`, the exact path
        `fetch_authority_zip` itself would resolve to): if it does not, a
        real network request is about to happen, and the authority's own
        download URL is appended to `endpoints_used` once `fetch_
        authority_zip` and `parcels_in` have both returned without raising.
        Recording only on that success, never merely on "a request was
        attempted", matches `OsmSource._record_endpoint`'s own convention
        (called only after a 200 response, never inside its retry loop).
        A cache hit records nothing, the same as a skipped OSM tile.
        `fetch_authority_zip`'s own return shape is untouched by this: the
        pre-check is a plain filesystem read of the same deterministic
        path, not a second, competing source of truth for what that
        function did.

        Survives package.py's retry pass on this same instance (a second
        review finding, proven by an executed probe): pass 1 downloading
        Authority_A, recording its URL, then raising on Authority_B's own
        500 must not have that URL erased when pass 2 calls fetch() again
        and finds A already cached. `endpoints_used` is therefore never
        reset inside this method (see __init__'s own comment on the
        attribute); it accumulates across every fetch() call this ONE
        instance ever serves. That is safe only because this instance
        itself is request-scoped: package.py's `_configured_sources` asks
        this class's own `configure()` for a fresh copy once per request,
        so the registered singleton `register_default_sources()` builds
        once for the whole process is never the thing fetch() is actually
        called on, and one survey's URLs can never accumulate into the
        next survey's.
        """
        self.tile_failures = []
        # endpoints_used is deliberately NOT reset here; see __init__'s own
        # comment beside it for why a per-call reset would wipe an earlier
        # pass's URL the moment package.py's retry pass re-invokes fetch()
        # on this same instance, and why configure() is what gives each
        # SEPARATE survey a clean list instead.
        if cancel is not None:
            cancel.raise_if_cancelled()

        parcels_path = work_dir / PARCELS_WORK_NAME
        meta_path = work_dir / META_WORK_NAME
        if parcels_path.exists() and meta_path.exists():
            total_kept = _total_kept_from_meta(meta_path)
            if total_kept is not None and (
                (parcels_path.stat().st_size == 0) == (total_kept == 0)
            ):
                progress.emit("tile_skipped", source=self.id, tile_id="whole-area")
                return [parcels_path, meta_path]

        authorities = authorities_for(bbox)
        if not authorities:
            self._record_tile_failures(tiles, FAILURE_NO_OUTPUT, OUTSIDE_ENGLAND_AND_WALES_MESSAGE)
            raise InspireError(OUTSIDE_ENGLAND_AND_WALES_MESSAGE)

        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            grid = ensure_ostn15(cache_dir=self._ostn15_cache_dir, session=self.session)
        except requests.RequestException as exc:
            # bng.py's own _download_and_parse currently wraps every
            # requests.RequestException into a BngError before it can
            # reach here (see the except clause below); kept for the same
            # reason lidar_wales.py keeps the identical, seemingly
            # unreachable clause: a future bng.py that let one through
            # must still be classified correctly rather than falling into
            # a bare, unclassified raise.
            kind, phrase = classify_transport_failure(exc)
            self._record_tile_failures(
                tiles, kind,
                f"Failed to download the OSTN15 shift grid needed to place "
                f"this extent on the National Grid: {phrase}.",
            )
            raise
        except BngError:
            self._record_tile_failures(
                tiles, FAILURE_UNREACHABLE,
                "Failed to download the OSTN15 shift grid needed to place "
                "this extent on the National Grid.",
            )
            raise

        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            bbox_bng = padded_bng_extent(bbox, grid, PAD_METRES)
        except BngError:
            self._record_tile_failures(tiles, FAILURE_NO_OUTPUT, OUTSIDE_ENGLAND_AND_WALES_MESSAGE)
            raise InspireError(OUTSIDE_ENGLAND_AND_WALES_MESSAGE) from None

        resolved_inspire_cache_dir = _resolve_inspire_cache_dir(self._inspire_cache_dir)
        all_parcels: list[list[list[tuple[float, float]]]] = []
        authority_meta: list[dict[str, object]] = []
        for name in authorities:
            if cancel is not None:
                cancel.raise_if_cancelled()
            # Computed BEFORE the call, from the same deterministic path
            # fetch_authority_zip itself resolves to (see this method's own
            # docstring, "endpoints_used"): True means this call is about
            # to hit the network, False means it is about to read this
            # month's cache file. Not re-checked after the call, since the
            # call itself is the only thing that could change it, and this
            # process is the only writer of its own cache directory.
            will_download = not _authority_cache_path(name, resolved_inspire_cache_dir).exists()
            try:
                zip_path = fetch_authority_zip(name, self.session, cache_dir=self._inspire_cache_dir)
                stream = parcels_in(zip_path, bbox_bng)
                kept = list(stream)
            except InspireError as exc:
                kind, verb, phrase = _classify_inspire_error(exc)
                self._record_tile_failures(
                    tiles, kind,
                    f"{verb} the INSPIRE Index Polygons for {name}: {phrase}.",
                )
                raise
            if will_download:
                self._record_endpoint(INSPIRE_DOWNLOAD_URL_TEMPLATE.format(name=name))
            all_parcels.extend(kept)
            authority_meta.append(
                {
                    "name": name,
                    "parcels_seen": stream.counts.parcels_seen,
                    "parcels_kept": stream.counts.parcels_kept,
                    "parcels_skipped_malformed": stream.counts.parcels_skipped_malformed,
                    "timestamp_year": stream.timestamp_year,
                }
            )

        # Nothing above this line writes anything: a failure on authority 3
        # of 4 leaves work_dir exactly as it found it (see the module
        # section's own docstring), which is what makes a resumed retry
        # start clean rather than skip on a half-written file.
        parcels_text = "".join(
            json.dumps(rings, separators=(",", ":")) + "\n" for rings in all_parcels
        )
        meta = {
            "authorities": authority_meta,
            # The newest of the authorities' own collection years, on the
            # ordinary assumption that every authority in one survey's
            # padded extent was downloaded in the same monthly HMLR
            # publication cycle (see the plan's own "first Sunday of the
            # month" cadence) and therefore agrees; the max, not the
            # first, is the honest choice on the rare chance a resumed
            # fetch spans a month boundary between authorities.
            "year": max(entry["timestamp_year"] for entry in authority_meta),
        }
        atomic_write_text(parcels_path, parcels_text)
        atomic_write_text(meta_path, json.dumps(meta, separators=(",", ":")))

        progress.emit("tile_done", source=self.id, tile_id="whole-area")
        return [parcels_path, meta_path]

    # -- merge -----------------------------------------------------------------

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Runs `boundary_curves` over every kept parcel `PARCELS_WORK_NAME`
        holds and writes `<stem>_boundaries.geojson`: a FeatureCollection of
        LineStrings, each carrying the brief's exact properties.

        Picks its two work files out of `parts` BY NAME, never by position
        (the ElevationSource.merge lesson). Unlike LidarWalesSource.merge
        (two independent rasters, each with its own output), this source's
        ONE output needs BOTH work files together (the parcel geometry AND
        the collection year), so "missing work files" here is all-or-
        nothing: either name absent from `parts` means nothing coherent can
        be written, the stale package copy of `<stem>_boundaries.geojson`
        is unlinked (the same I8 stale-output discipline every other
        source's merge follows), and this returns `[]`.

        The OSTN15 grid needed to unproject curves back to lon/lat is
        acquired `load_ostn15()` first (cache-only; fetch()'s own
        `ensure_ostn15` call, on every ordinary run, already cached one for
        this exact reason `_fuse_heights_step`'s own docstring gives), and
        only `ensure_ostn15()` (a real network attempt) if that misses:
        `mapgen bridge` re-running merge on a package moved to a machine
        that has never run a survey is the one path this actually exercises.
        If BOTH fail (`BngError`, or the defensive `requests.RequestException`
        clause lidar_wales.py's own fetch() also keeps), this skips with the
        same stale-unlink, documented the same way LidarWalesSource.merge
        skips its own contour generation when no grid is available: a
        package without boundaries beats a crash over something the
        package's other outputs do not depend on.
        """
        out_dir = Path(out_dir)
        output_path = out_dir / f"{stem}_boundaries.geojson"

        parcels_part = next((part for part in parts if part.name == PARCELS_WORK_NAME), None)
        meta_part = next((part for part in parts if part.name == META_WORK_NAME), None)
        if parcels_part is None or meta_part is None:
            output_path.unlink(missing_ok=True)
            return []

        try:
            meta = json.loads(meta_part.read_text(encoding="utf-8"))
            year = int(meta["year"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            # meta.json is only ever written by this class's own fetch();
            # corruption here means a genuinely broken work directory, and
            # the same "package without X beats a crash" principle applies
            # rather than raising over a file the owner never touched.
            output_path.unlink(missing_ok=True)
            return []

        grid = load_ostn15(cache_dir=self._ostn15_cache_dir)
        if grid is None:
            try:
                grid = ensure_ostn15(cache_dir=self._ostn15_cache_dir, session=self.session)
            except (requests.RequestException, BngError):
                output_path.unlink(missing_ok=True)
                return []

        parcels = _read_parcels_jsonl(parcels_part)
        curves = boundary_curves(parcels)

        features = []
        for curve in curves:
            coordinates = []
            for easting, northing in curve:
                latitude, longitude = from_bng(easting, northing, grid)
                coordinates.append([longitude, latitude])
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "source": BOUNDARY_SOURCE_LABEL,
                        "note": BOUNDARY_INDICATIVE_NOTE,
                        "year": year,
                    },
                    "geometry": {"type": "LineString", "coordinates": coordinates},
                }
            )
        payload = {"type": "FeatureCollection", "features": features}
        atomic_write_bytes(
            output_path, json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        return [output_path]

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root file merge() could ever write for this stem. Read by
        package.py's stale-output sweep; see sources/base.py."""
        return [f"{stem}_boundaries.geojson"]
