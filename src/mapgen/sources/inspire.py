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

import requests

from mapgen.config import CONFIG_PATH
from mapgen.fsutil import ensure_dir
from mapgen.geo import BBox

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

    resolved_cache_dir = cache_dir if cache_dir is not None else _default_inspire_cache_dir()
    cache_path = resolved_cache_dir / f"{name}_{_current_month_stamp()}.zip"
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
                raise InspireError(
                    f"HM Land Registry's INSPIRE download for {name} answered "
                    f"HTTP {response.status_code} instead of 200; this usually "
                    f"means the session did not carry the cookie the service's "
                    f"own redirect sets, and the body received is not the zip."
                )
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
        except (OSError, zipfile.BadZipFile) as exc:
            raise InspireError(f"Could not open {zip_path} as a zip file.") from exc

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


def parcels_in(
    zip_path: Path, bbox_bng: tuple[float, float, float, float]
) -> ParcelStream:
    """Every parcel in `zip_path`'s GML member whose EXTERIOR ring's own
    bounding box intersects `bbox_bng`, as rings: a list per parcel,
    exterior ring first and any interior rings after it in document
    order, each ring a list of (easting, northing) float tuples in
    British National Grid metres.

    `bbox_bng` is `(e_min, n_min, e_max, n_max)`, the same shape and
    order `lidar_wales._padded_bng_extent` already returns; a caller
    starting from a WGS84 `BBox` projects it with `bng.to_bng` first,
    exactly as that module does.

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
