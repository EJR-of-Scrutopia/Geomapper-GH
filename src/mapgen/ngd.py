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

## Dev-mode pacing

OS Data Hub's own plans FAQ states development mode throttles a project
to `DEV_MODE_TRANSACTIONS_PER_MINUTE` (50) transactions per minute per
API (live mode raises this to 600). A Cowbridge-sized benchmark pull is
roughly 40 requests across the two collections (a few hundred to a
couple-thousand features each at `_ITEMS_LIMIT`), which sits close
enough to that ceiling that two runs inside a minute, or one run whose
own paging happens to burst, would plausibly trip it; the API answers a
tripped ceiling with 429, which `_fetch` used to turn straight into
`NgdError` kind `"cap"` with no attempt to pace or wait it out.

`MIN_REQUEST_INTERVAL_SECONDS` is the gate: `60 / 50 = 1.2` seconds is
the interval that would land a client EXACTLY on the ceiling with no
margin at all, so it is widened by 15 percent (`* 1.15`) for the two
clocks that matter here (this process's own and OS's own request-
counting window) never being perfectly aligned. `NgdClient._pace`, a
monotonic-clock gate in the same shape as `mapgen.sources.osm.
RateLimiter` (the same "space calls so an API is not hammered" job,
here on the OS NGD API instead of the free OSM one), sleeps only as
much as the elapsed time since this instance's own last request still
falls short of that interval, and never sleeps before a client's first
request ever: an instance that pages 40 times paces itself to roughly
48 seconds of `sleeper` calls total, spread across the run, not paid up
front. `clock`/`sleeper` are constructor seams, defaulting to
`time.monotonic`/`time.sleep`, the same convention `RateLimiter` and
`geocode.GeocodeRateLimiter` both already use, so a test can prove the
pacing arithmetic without a single real sleep.

A 429 that DOES arrive (the ceiling was hit despite pacing, or another
process on the same project used up the minute's own budget) is worth
one bounded retry rather than an immediate failure, since the service
told this client exactly how long to wait in its own `Retry-After`
header. `_fetch` honours that once: sleep the advertised interval (via
the same `sleeper` seam), then retry the same request exactly once. A
`Retry-After` above `MAX_RETRY_AFTER_SLEEP_SECONDS` (120, a ceiling this
client will not simply sit and wait past, since that is no longer
"paced" behaviour, it is the service telling this client to come back
later) is not honoured at all: the request fails immediately as
`"cap"` rather than blocking a benchmark run for an unbounded time. A
second consecutive 429, or the absence of a `Retry-After` header
entirely, also fails as `"cap"` immediately: this is ONE retry, not a
backoff loop.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Sequence
from urllib.parse import quote, urlencode

from mapgen.sources.base import parse_retry_after

USER_AGENT = "mapgen/1.0 (architectural survey tool)"

NGD_ROOT = "https://api.os.uk/features/ngd/ofa/v1"

# The OS Data Hub plans FAQ's own stated development-mode ceiling: 50
# transactions per minute per API per project (live mode: 600). See the
# module docstring's "Dev-mode pacing" section for the arithmetic this
# feeds and why a 15 percent margin is added on top of it.
DEV_MODE_TRANSACTIONS_PER_MINUTE = 50

# 60 seconds / 50 transactions/minute = 1.2 s/request, the interval that
# would land exactly on the ceiling with no margin; widened by 15 percent
# (module docstring's own reasoning) so this client's own clock and OS's
# own request-counting window never having to agree exactly still leaves
# room to spare.
MIN_REQUEST_INTERVAL_SECONDS = 60.0 / DEV_MODE_TRANSACTIONS_PER_MINUTE * 1.15

# The longest Retry-After this client will actually sleep out before
# retrying a 429 once. Above this, waiting it out is no longer pacing,
# it is the service asking to be left alone for a long time, and this
# client fails fast instead of blocking a benchmark run indefinitely.
MAX_RETRY_AFTER_SLEEP_SECONDS = 120.0

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

    `min_interval_seconds`, `sleeper` and `clock` are the dev-mode
    pacing seam (see the module docstring's "Dev-mode pacing" section):
    `sleeper`/`clock` default to `time.sleep`/`time.monotonic`, the same
    convention `mapgen.sources.osm.RateLimiter` and
    `mapgen.geocode.GeocodeRateLimiter` both already use, so a test can
    inject a fake pair and prove the pacing arithmetic without ever
    sleeping for real.
    """

    def __init__(
        self,
        key: str,
        session: object | None = None,
        timeout_seconds: float = 30.0,
        min_interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.key = key
        self.session = session if session is not None else urllib.request.build_opener()
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self._sleeper = sleeper
        self._clock = clock
        # None until this instance's first request; _pace() reads that
        # as "never sleep before the first request" rather than as an
        # elapsed time of zero.
        self._last_request_at: float | None = None

    def _pace(self) -> None:
        """Sleeps just long enough, since this instance's own last
        request, to respect `min_interval_seconds`; sleeps nothing
        before the first request this instance ever makes, and nothing
        at all when enough real time has already passed on its own
        (an instance that pages slowly, or one that just served a
        `Retry-After` sleep in `_fetch`'s own retry, typically owes no
        further wait here).

        Exactly `mapgen.sources.osm.RateLimiter.wait`'s own shape,
        reused as a private method here rather than imported: that
        class is built to be shared by unrelated call sites (osm.py
        constructs one directly), while this gate is intrinsic to one
        `NgdClient` instance's own request stream, the same reasoning
        `NgdClient.__init__`'s own docstring already gives for the
        session seam being a constructor parameter rather than a
        module-level function.
        """
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self.min_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self._sleeper(remaining)
        self._last_request_at = self._clock()

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

        Every attempt, including a retried one, is paced through
        `_pace()` first: the retry sleep below already waits out
        whatever the service asked for, so `_pace()` on the following
        loop iteration ordinarily adds nothing further, but it is what
        keeps a *rejected-without-Retry-After* 429 (which raises
        immediately, no retry) from leaving this instance's own pacing
        clock stale for whatever request comes after it.
        """
        retried = False
        while True:
            self._pace()
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
                    retry_after = parse_retry_after(getattr(exc, "headers", None))
                    if (
                        not retried
                        and retry_after is not None
                        and retry_after <= MAX_RETRY_AFTER_SLEEP_SECONDS
                    ):
                        # One bounded retry (module docstring's "Dev-mode
                        # pacing" section): sleep exactly what the service
                        # asked for, then go around the loop once more.
                        # `retried` guarantees this branch can fire at
                        # most once per _fetch() call, so two consecutive
                        # 429s always fall through to the raise below on
                        # the second one.
                        self._sleeper(retry_after)
                        retried = True
                        continue
                    raise NgdError(
                        "the OS NGD API asked mapgen to slow down.",
                        kind="cap",
                        status_code=status,
                        retry_after_seconds=retry_after,
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
            break

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
