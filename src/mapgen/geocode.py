"""Server-side Nominatim access: place search and reverse geocoding.

Deliberately kept out of the browser entirely, on both a policy and a
security point:

- Nominatim's usage policy requires a descriptive User-Agent or a valid
  HTTP Referer identifying the calling application. A browser script
  cannot set the User-Agent header at all (it is a forbidden header name
  in the Fetch standard; any attempt is silently overridden with the
  browser's own value, on every current engine), which leaves Referer as
  the only lever a page has, and a bare http://127.0.0.1:<port> Referer
  identifies nothing useful to Nominatim's operators. A real header,
  naming this project and where to find it, is only possible from server
  code, which is what this module is for.
- The usage policy also caps requests at one per second, measured across
  the whole application, not per browser tab or per page load. A limit
  held in page-local JavaScript resets on every reload and is invisible
  to a second tab; a limit held here, in the one server process every tab
  and every reload talks to, is the only place that can actually be true.

Nothing here calls Nominatim directly from a route handler: server.py
only ever asks a NominatimClient, so the rate limit, timeout, and User-
Agent are always applied the same way regardless of which route needed
them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

from mapgen import __version__

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
DEFAULT_MAX_QUEUE = 3
# Task 18: place search became a typeahead, which needs enough hits to let
# someone tell two similarly-named places apart, not just the single best
# guess the old one-shot search jumped to. 5 is the brief's own figure.
DEFAULT_SEARCH_LIMIT = 5

# "AppName/version (+url)" is the conventional shape for a self-identifying
# bot/tool User-Agent, and is explicitly acceptable under Nominatim's
# policy ("Provide ... a descriptive User-Agent ... stock User-Agents as
# set by http libraries will not do"). The URL is this project's actual
# GitHub remote, so a human on Nominatim's side who wants to reach out has
# somewhere real to look.
USER_AGENT = f"mapgen/{__version__} (+https://github.com/EJR-of-Scrutopia/Geomapper-GH)"

DEFAULT_MIN_INTERVAL_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 8.0


class GeocodeError(RuntimeError):
    """Raised when Nominatim cannot answer: unreachable, slow, rate limited,
    a non-200 status, or a response that is not shaped the way this client
    expects.

    Deliberately one exception type for all of these. server.py's route
    handlers do not need to tell a timeout apart from a 429 apart from a
    malformed body: every case means the same thing to the interface,
    geocoding did not work right now, show a plain message, and the map
    stays usable.
    """


class GeocodeQueueFullError(GeocodeError):
    """Raised when too many callers are already waiting for a rate-limit
    slot.

    A subclass of GeocodeError, not a sibling, so a caller that only wants
    "did geocoding work" still gets a true answer with a single except
    clause; server.py distinguishes it only to answer with 429 instead of
    502, since this caller never even reached Nominatim to fail there.

    Reserving a slot with no bound on how many can queue is exactly how a
    handful of concurrent requests turn into several seconds of every one
    of them waiting its turn, even when Nominatim is failing instantly and
    every one of those waits was always going nowhere: a failing upstream
    otherwise sheds no load at all, it just makes everyone wait for a
    turn that was never going to succeed. Rejecting outright once the
    queue is already full answers the excess callers fast instead.
    """


class GeocodeRateLimiter:
    """Serialises calls to at most one per min_interval_seconds, and
    refuses outright once max_queue callers are already waiting for a
    turn rather than queuing a further, unbounded number of them.

    Guarded by a lock, unlike the superficially similar RateLimiter in
    mapgen.sources.osm. That one is only ever called from a single job's
    own worker thread, one call at a time by construction, so it never
    needed one. This one is shared by every request-handling thread
    ThreadingHTTPServer spawns: two browser tabs asking to geocode at the
    same moment land on two different threads running at the same time,
    and a plain read-then-write (read the last call time, decide whether
    to wait, then update the last call time) lets both threads read the
    same stale value and both proceed together. The fix is the same shape
    as the client-side version this replaced: reserve the next slot
    synchronously, inside the lock, before waiting for it, so a second
    thread arriving mid-wait sees the reservation the first thread already
    made rather than the value from before it existed. The wait itself
    happens outside the lock; only the bookkeeping is exclusive, so
    waiting threads don't serialise on holding it.

    The queue bound only covers time spent here, waiting for a turn, not
    the outbound call that follows: once released, how long that call
    itself takes is timeout_seconds' concern, on NominatimClient, not
    this class's. A caller already released cannot be recalled by
    anything on this side of the connection either; see app.js's own
    AbortController handling and its documented limit for why that is a
    client-side mitigation, not a server-side guarantee.
    """

    def __init__(
        self,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_queue: int = DEFAULT_MAX_QUEUE,
    ) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.max_queue = max_queue
        self._sleeper = sleeper
        self._clock = clock
        self._lock = threading.Lock()
        self._next_allowed_at: float | None = None
        self._pending = 0

    def wait(self) -> None:
        with self._lock:
            if self._pending >= self.max_queue:
                raise GeocodeQueueFullError(
                    f"Too many geocoding requests are already waiting "
                    f"(limit {self.max_queue}). Try again shortly."
                )
            self._pending += 1
            now = self._clock()
            base = now if self._next_allowed_at is None else max(now, self._next_allowed_at)
            wait_for = base - now
            self._next_allowed_at = base + self.min_interval_seconds
        try:
            if wait_for > 0:
                self._sleeper(wait_for)
        finally:
            with self._lock:
                self._pending -= 1


@dataclass(frozen=True)
class GeocodeResult:
    """One place-search hit: a bounding box (field names match BBox's) plus
    Nominatim's own display_name, which is what lets a person tell two
    hits called the same thing apart in a typeahead list.

    region and site (Task 19): the same best-effort names reverse() derives
    for a bbox centroid, read directly from THIS hit's own address
    breakdown instead. Picking a result from the typeahead is better
    evidence of the site name than a reverse lookup on the centroid of
    whatever rectangle the pick produces, so the caller should prefer
    these over a second, separate reverse() call whenever they are
    present. Default to empty strings, matching ReverseResult's own
    convention, so a hit whose address Nominatim did not break down (rare,
    but the same "cannot be derived" case reverse() already handles) is
    simply absent rather than raising.
    """

    display_name: str
    west: float
    south: float
    east: float
    north: float
    region: str = ""
    site: str = ""


@dataclass(frozen=True)
class ReverseResult:
    """Best-effort region and site names for a point. Either may be empty
    when Nominatim has no address data there; that is not an error, see
    NominatimClient.reverse.
    """

    region: str
    site: str


def _region_and_site(address: object) -> tuple[str, str]:
    """Best-effort (region, site) from a Nominatim address breakdown dict.

    Shared by search() and reverse() so the two never drift apart: a
    typeahead pick and a bbox-centroid reverse lookup are two different
    ways of arriving at the same address dict shape, and both should read
    it the same way. address is typed as object rather than dict because
    both call sites already have to guard against Nominatim returning
    something other than a dict (a missing "address" key entirely, or a
    non-dict value under it); doing that check once here rather than
    twice at each call site is the point.
    """
    if not isinstance(address, dict):
        address = {}
    site = (
        address.get("suburb")
        or address.get("town")
        or address.get("village")
        or address.get("city")
        or ""
    )
    region = address.get("county") or address.get("state_district") or address.get("state") or ""
    return str(region), str(site)


class NominatimClient:
    """The only thing in this codebase that is allowed to call Nominatim.

    session is injectable the same way mapgen.sources.osm.OsmSource takes
    one: a real requests.Session by default, a fake with queued responses
    in tests.
    """

    def __init__(
        self,
        session: object | None = None,
        limiter: GeocodeRateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.limiter = limiter if limiter is not None else GeocodeRateLimiter()
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def _get(self, url: str, params: dict) -> object:
        self.limiter.wait()
        try:
            response = self.session.get(
                url,
                params=params,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            # requests raises its own exception hierarchy (Timeout,
            # ConnectionError, and others) for exactly the failures this
            # client needs to survive: unreachable, too slow, connection
            # reset. Caught broadly and rewrapped, because server.py only
            # needs to know that geocoding failed, not which of requests'
            # many exception classes it failed with.
            raise GeocodeError(f"Could not reach Nominatim: {exc}") from exc
        if response.status_code != 200:
            raise GeocodeError(f"Nominatim returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise GeocodeError("Nominatim's response was not valid JSON.") from exc

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[GeocodeResult]:
        """Up to `limit` matching hits, most relevant first, or an empty
        list if nothing matched.

        Nominatim answers "nothing matched" with HTTP 200 and an empty
        JSON array, not an error status, so that case is distinguished
        here rather than by _get() and is not a GeocodeError. Anything
        that is not a JSON array at all is a genuine shape mismatch and
        does raise: conflating "no results" with "the response was not
        even the right kind of thing" would hide a real break behind a
        message that reads exactly like a normal empty search. A single
        malformed hit anywhere in an otherwise-good list raises for the
        whole call rather than silently dropping just that one entry,
        matching how a single-hit shape mismatch has always been treated.
        """
        # addressdetails=1: Nominatim's default for /search is 0 (unlike
        # /reverse, which already defaults it on). Without this, hit.get
        # ("address") below is always absent, and region/site come back
        # empty for every hit regardless of what Nominatim actually knows
        # about it, which defeats the whole point of reading them from the
        # search hit directly (see GeocodeResult's own docstring).
        payload = self._get(
            NOMINATIM_SEARCH_URL,
            {"format": "json", "addressdetails": 1, "limit": limit, "q": query},
        )
        if not isinstance(payload, list):
            raise GeocodeError("Nominatim's search response was not in the expected shape.")
        results = []
        for hit in payload:
            try:
                # Nominatim's own field order, and the values are strings,
                # not numbers: ["south", "north", "west", "east"], each a
                # decimal string such as "51.3800000".
                south, north, west, east = (float(v) for v in hit["boundingbox"])
                display_name = str(hit["display_name"])
            except (KeyError, TypeError, ValueError) as exc:
                raise GeocodeError(
                    "Nominatim's search response was not in the expected shape."
                ) from exc
            region, site = _region_and_site(hit.get("address"))
            results.append(
                GeocodeResult(
                    display_name=display_name,
                    west=west,
                    south=south,
                    east=east,
                    north=north,
                    region=region,
                    site=site,
                )
            )
        return results

    def reverse(self, lat: float, lon: float) -> ReverseResult:
        """Best-effort region and site names for a point.

        zoom=14 ("neighbourhood" in Nominatim's own documented zoom-to-
        address-rank table), not the zoom=12 ("town/borough") this used
        to send. Task 19's reported bug: a real reverse lookup near Barry
        Waterfront's dock at zoom=12 came back with only
        {"county": "Vale of Glamorgan"}, no suburb, town or village at
        all, because zoom=12 asks Nominatim for the enclosing polygon at
        town/borough rank, and a dockland or coastal point is often not
        inside any polygon that coarse. Checked directly against Nominatim
        (not merely reasoned about) across four representative cases
        before picking this value: a city centre (Cardiff, zoom=12
        already resolved "Cardiff" as a city; zoom=14 resolves the finer
        "Castle" suburb instead, an improvement not a regression), a
        village (zoom=12 gave county only; zoom=14 gave suburb "St
        Nicholas and Bonvilston" and village "St Nicholas"), open
        countryside (zoom=12 gave county only; zoom=14 still resolved the
        containing civil parish, "Llanwrthwl", which is the correct UK
        administrative answer for a rural point with no settlement
        directly on it, not a fabrication), and the reported coastal/dock
        case itself (zoom=12 jumped to a same-named neighbouring city;
        zoom=14 correctly resolved suburb "Barry Island", town "Barry").
        zoom=14 is a genuinely finer request than zoom=12, not merely a
        bigger number tried until one case passed: Nominatim's own address
        breakdown grows monotonically finer as zoom increases, so a field
        present at zoom=12 stays present at zoom=14, and this never
        regresses a case that already worked.

        Nominatim answers a point with no address data (open water, deep
        countryside) with HTTP 200 and a JSON object containing an
        "error" key instead of the usual "address" one, again not an
        error status. That is treated the same as no data at all: an
        empty result, not a GeocodeError, matching how the caller already
        treats a failed lookup as something to leave blank for the user
        to fill in, not something to report.
        """
        payload = self._get(
            NOMINATIM_REVERSE_URL, {"format": "json", "zoom": 14, "lat": lat, "lon": lon}
        )
        if not isinstance(payload, dict):
            raise GeocodeError("Nominatim's reverse response was not in the expected shape.")
        if "error" in payload:
            return ReverseResult(region="", site="")
        region, site = _region_and_site(payload.get("address"))
        return ReverseResult(region=region, site=site)
