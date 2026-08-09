"""An in-memory OGC API Features client for the OS National Geographic
Database (NGD), built for one job: `mapgen.benchmark` (Task 3) pulls OS's
own survey-grade buildings and roads over a package's bbox through this
client, computes aggregate statistics against mapgen's own open-stack
outputs, and discards every premium byte. This module never learns how
to do anything else with what it fetches.

## The firewall

This module performs NO disk write of any kind, anywhere, ever: no
cache, no shard, no log file, no temp file. Every feature `items()` and
`verify_collections()` read lives only in the Python objects handed back
to the caller, for exactly as long as that caller keeps a reference to
them. This is not an oversight to fix later; it is the whole reason this
module is safe to point at a Premium product at all. The plan this
module ships under (`docs/superpowers/plans/2026-08-09-mapgen-phase2b-f-
os-benchmark.md`) states the rule this module exists to make true by
construction: nothing from any OS premium product may enter a package, a
fusion, a shard cache, or any output a client could receive. A client
that never learns to write anywhere cannot be the leak, whatever its
caller later does wrong; Task 3's `benchmark.py` is the one place that
computes and writes aggregates, never the geometry or attribute values
themselves.

## The key is radioactive

`NgdClient` takes its own OS Data Hub Premium key as a plain string and
sends it in the `key` HTTP HEADER on every `/items` request, never in
the query string: a key in a query string reappears in any echoed URL,
and this API's own pagination carries the query string forward into
every `links[rel="next"]` href it hands back (probed 2026-08-09; see the
plan's probed-facts table). `verify_collections()`'s own `/collections`
pull needs no key at all (probed keyless, same table) and sends none.

No exception this module raises ever contains the key or a URL, ever:
see `NgdError`'s own docstring. This mirrors `os_downloads.py`'s own
"no-URL rule" (a URL is where an API key lives) for a client that,
unlike `os_downloads.py`'s keyless OS Open endpoints, actually carries
one on every request that matters.

## Following next links safely

A `links[rel="next"]` href is a URL the SERVICE handed back, inside a
response body this client already trusted enough to read. Before this
client requests it, `items()` checks that it starts with `NGD_ROOT`: a
next link pointing anywhere else is refused, raised as `NgdError` kind
`"parse"`, and never requested, because requesting it would send the
`key` header, this client's one secret, to a host that is not
api.os.uk. Nothing about the OGC API Features spec promises a
same-origin next link; this check is what makes that true regardless of
what a (possibly compromised, possibly merely buggy) response says.

## stdlib only, urllib not requests

Deliberately urllib, matching `os_downloads.py`'s own module docstring
on the reasoning: this whole plan is explicitly stdlib-only (see the
plan's Tech Stack section). Every request this class makes goes through
`self.session`, an object shaped like `urllib.request.OpenerDirector`
(a `.open(request, timeout=...)` method), defaulting to a real one built
by `urllib.request.build_opener()` when the caller passes none. A test
swaps in a stub session that records what it was asked for, the same
seam convention `os_downloads.py`'s own module-level `_build_opener()`
establishes, here as a constructor parameter instead of a module-level
function because this module's natural shape is one client object per
key, not a set of free functions sharing no state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Sequence
from urllib.parse import quote, urlencode

from mapgen.sources.base import parse_retry_after

USER_AGENT = "mapgen/1.0 (architectural survey tool)"

NGD_ROOT = "https://api.os.uk/features/ngd/ofa/v1"

# Physical building footprints and road centrelines: the plan's own two
# collections (probed-facts table, "94 collections. The benchmark uses
# bld-fts-buildingpart-2 ... and trn-ntwk-roadlink-5"). Version suffixes
# can bump; verify_collections() is how a caller finds out before either
# is ever fetched, naming the drift rather than failing deep inside
# paging.
BUILDING_COLLECTION = "bld-fts-buildingpart-2"
ROAD_COLLECTION = "trn-ntwk-roadlink-5"

# The runaway guard: a Cowbridge-sized extent holds a few thousand
# building parts and about a thousand road links, under 60 pages at
# limit=100 (probed-facts table, "Volume sanity"). 500 is comfortably
# above every real pull this project makes and comfortably below
# "silently loops forever against a service that never stops paging".
MAX_PAGES = 500

# The one limit this client ever asks for: the API's own stated maximum
# (probed-facts table, "limit (min 1, MAX 100)"). Not a caller-tunable
# knob: Task 3 wants every feature over a bbox, and the fewer distinct
# limits this client has ever sent, the fewer shapes its own paging loop
# has to have been proven against.
_ITEMS_LIMIT = 100

# The OGC API Features "CRS by reference" URI form (OGC API - Features -
# Part 2: Coordinate Reference Systems by Reference), used for both
# `crs` and `bbox-crs`: EPSG 27700, British National Grid, the version
# segment "0" meaning "whichever version of the EPSG register the
# authority currently publishes". This is the URI form OS's own NGD
# OpenAPI document specifies for these two parameters (probed-facts
# table: "CRS: EPSG 27700 is requestable for both crs and bbox-crs").
# items()'s own live test is what actually proves this over the wire: a
# feature whose first coordinate is not a plausible BNG easting would
# mean this URI, or its acceptance by the service, was assumed wrong.
_CRS_27700_URI = "http://www.opengis.net/def/crs/EPSG/0/27700"


class NgdError(RuntimeError):
    """Raised for anything this client could not verify, fetch, or parse.

    `kind` is one of:
      "auth"    401 or 403: the key was refused.
      "listing" verify_collections()'s own keyless /collections pull
                could not be reached, answered a non-2xx status, was not
                shaped like a collections listing, or did not carry one
                of the requested collection ids.
      "query"   an /items pull answered a non-2xx status this mapping
                has no more specific kind for, or could not be reached
                at all.
      "parse"   a body that came back was not valid JSON, was not shaped
                like the GeoJSON or collections listing expected, or a
                next link pointed away from NGD_ROOT.
      "cap"     429 (rate limited), or max_pages was reached with
                another page still on offer.

    `status_code` is the HTTP status when one is known, else None.
    `retry_after_seconds` is the Retry-After header's own value in
    seconds, parsed only for a 429, else None.

    Every message here is composed from a fixed English vocabulary plus,
    at most, a collection id or a page count: never a URL, and never the
    key. See the module docstring's "the key is radioactive" section.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def _next_link(links: object) -> str | None:
    """The `href` of the first entry in `links` whose `rel` is `"next"`,
    or None when there is no such entry (paging is over) or `links`
    itself is not a list (a malformed or absent field is treated the
    same as "no next page" here; a genuinely broken body has already
    failed the features-list shape check by the time this is called).
    """
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("rel") == "next":
            href = link.get("href")
            return href if isinstance(href, str) else None
    return None


class NgdClient:
    """One OS Data Hub Premium key, one OGC API Features client.

    `session` is an object shaped like `urllib.request.OpenerDirector`
    (a `.open(request, timeout=...)` method returning a context-manager
    response with `.read()`), defaulting to a real one when None. See
    the module docstring's "stdlib only" section for why this is urllib
    rather than requests, and why the seam is a constructor parameter
    rather than a module-level function the way os_downloads.py's own
    `_build_opener()` is.
    """

    def __init__(
        self,
        key: str,
        session: object | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.key = key
        self.session = session if session is not None else urllib.request.build_opener()
        self.timeout_seconds = timeout_seconds

    def _fetch(self, url: str, *, headers: dict[str, str], default_kind: str) -> object:
        """GET `url`, decode the body as JSON, return whatever it parsed
        to.

        `default_kind` names the `NgdError` kind for a non-2xx status
        this shared mapping has no more specific kind for, and for a
        transport failure that never produced a response at all: it is
        `"listing"` for `verify_collections()`'s own call and `"query"`
        for `items()`'s own paged calls, the two `kind`s the module
        docstring documents as endpoint-specific. 401/403 (`"auth"`) and
        429 (`"cap"`) are universal regardless of which endpoint asked,
        since a refused or throttled key means the same thing wherever
        it happens.
        """
        request = urllib.request.Request(url, headers=headers)
        try:
            with self.session.open(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                raise NgdError(
                    "the OS NGD key was refused; a dev-mode Premium "
                    "project with the NGD Features API added is what "
                    "answers here",
                    kind="auth",
                    status_code=status,
                ) from None
            if status == 429:
                raise NgdError(
                    "the OS NGD API asked mapgen to slow down.",
                    kind="cap",
                    status_code=status,
                    retry_after_seconds=parse_retry_after(getattr(exc, "headers", None)),
                ) from None
            raise NgdError(
                f"the OS NGD API answered HTTP {status}.",
                kind=default_kind,
                status_code=status,
            ) from None
        except NgdError:
            raise
        except Exception:
            raise NgdError(
                "could not reach the OS NGD API.",
                kind=default_kind,
            ) from None

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise NgdError(
                "the OS NGD API did not answer with valid JSON.",
                kind="parse",
            ) from None

    def verify_collections(self, ids: Sequence[str]) -> None:
        """Raises `NgdError` kind `"listing"` naming the first of `ids`
        that is not in the service's own keyless `/collections` listing.

        Sends no key: probed keyless (see the module docstring and the
        plan's probed-facts table). Version suffixes on OS's own
        collection ids can bump between the plan being written and this
        client being run, so this is what a caller runs before either
        `items()` pull, to fail with a named collection rather than
        deep inside paging.
        """
        url = f"{NGD_ROOT}/collections"
        body = self._fetch(url, headers={"User-Agent": USER_AGENT}, default_kind="listing")
        if not isinstance(body, dict) or not isinstance(body.get("collections"), list):
            raise NgdError(
                "the OS NGD API's collections listing was not the JSON "
                "shape expected.",
                kind="parse",
            )
        listed_ids = {
            entry.get("id") for entry in body["collections"] if isinstance(entry, dict)
        }
        for collection_id in ids:
            if collection_id not in listed_ids:
                raise NgdError(
                    f"collection {collection_id!r} is not in the "
                    f"service's own listing; the version suffix may "
                    f"have moved.",
                    kind="listing",
                )

    def items(
        self,
        collection_id: str,
        bbox_bng: tuple[float, float, float, float],
        max_pages: int = MAX_PAGES,
    ) -> tuple[list[dict], int]:
        """Every feature over `bbox_bng` in `collection_id`, as
        GeoJSON-shaped dicts held in memory, plus the number of pages it
        took to fetch them all.

        `bbox_bng` is `(e_min, n_min, e_max, n_max)` in EPSG 27700
        metres. `limit` is fixed at 100 (the API's own maximum), and
        both `crs` and `bbox-crs` are the EPSG 27700 URI (see
        `_CRS_27700_URI`): the reference side of the benchmark comparison
        is BNG-native, never reprojected on the way in.

        Follows `links[rel="next"]` from each page's own body until it
        is absent. A next link that does not start with `NGD_ROOT` is
        refused before it is ever requested (see the module docstring's
        "following next links safely"). Raises `NgdError` kind `"cap"`,
        naming the page count, if `max_pages` is reached with another
        page still on offer: this is checked BEFORE each page beyond the
        first is fetched, so a caller's own cap is never exceeded by so
        much as one extra request.
        """
        e_min, n_min, e_max, n_max = bbox_bng
        query = urlencode(
            {
                "limit": _ITEMS_LIMIT,
                "bbox": f"{e_min},{n_min},{e_max},{n_max}",
                "bbox-crs": _CRS_27700_URI,
                "crs": _CRS_27700_URI,
            }
        )
        url = f"{NGD_ROOT}/collections/{quote(collection_id, safe='')}/items?{query}"

        features: list[dict] = []
        page_count = 0
        while True:
            if page_count >= max_pages:
                raise NgdError(
                    f"stopped after {page_count} page"
                    f"{'s' if page_count != 1 else ''} against the "
                    f"max_pages={max_pages} cap, with another page "
                    f"still on offer.",
                    kind="cap",
                )
            headers = {"User-Agent": USER_AGENT, "key": self.key}
            body = self._fetch(url, headers=headers, default_kind="query")
            page_count += 1

            page_features = body.get("features") if isinstance(body, dict) else None
            if not isinstance(page_features, list):
                raise NgdError(
                    "the OS NGD API's items response was not a GeoJSON "
                    "FeatureCollection.",
                    kind="parse",
                )
            features.extend(page_features)

            next_url = _next_link(body.get("links") if isinstance(body, dict) else None)
            if next_url is None:
                break
            if not next_url.startswith(NGD_ROOT):
                raise NgdError(
                    "a next link pointed away from the OS NGD API; "
                    "refusing to follow it with the key attached.",
                    kind="parse",
                )
            url = next_url

        return features, page_count
