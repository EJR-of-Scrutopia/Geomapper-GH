"""The LayerSource contract.

Every data source implements this protocol, and package.py orchestrates without
knowing about any of them specifically. Adding a source is one new module plus
one register call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

import requests

from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken


@dataclass(frozen=True)
class Estimate:
    bytes_estimate: int
    seconds_estimate: float


# Why a tile did not arrive, as a fixed vocabulary rather than a sentence
# (Task 30).
#
# The sentence is for the owner and is carried separately, in TileFailure.
# reason. This is the part code is allowed to branch on, and it exists
# because package.py has to decide which failures a retry could plausibly
# fix without reading English: matching on "timed out" or "503" in some
# later, differently-worded message is the same textual-over-structural
# mistake NodeCapExceededError's own docstring documents, one layer out.
#
# A source that cannot tell which of these applies says UNKNOWN and is
# never retried. That is the safe direction: an unrecognised cause retried
# blind is how a rate-limited public API gets hammered, and how an
# authentication failure becomes four authentication failures.
FAILURE_TIMEOUT = "timeout"
FAILURE_UNREACHABLE = "unreachable"
FAILURE_RATE_LIMITED = "rate_limited"
FAILURE_SERVICE_ERROR = "service_error"
FAILURE_NOT_AUTHORISED = "not_authorised"
FAILURE_REFUSED = "refused"
FAILURE_NODE_CAP = "node_cap"
FAILURE_NO_OUTPUT = "no_output"
FAILURE_UNKNOWN = "unknown"

# Which causes a retry could plausibly fix. Everything else is asked once
# and reported, because asking again is either useless or harmful.
#
# Retried:
#   timeout        the service or the link was too slow this time; the
#                  next request is a different roll of the dice
#   unreachable    a dropped connection or a DNS blip, the same
#   rate_limited   the service asked for less traffic, and the whole
#                  point of the retry layer is that it comes back later
#   service_error  a 5xx is the service saying the fault is its own
#
# Not retried, and each for its own reason:
#   not_authorised a wrong, missing or expired key answers 401 every
#                  time. Retrying it wastes the owner's time and, on a
#                  keyed service, can count against them. This is
#                  elevation's most common failure by a wide margin.
#   refused        the request itself was rejected, by a 4xx that is not
#                  a rate limit or by a command line tool that would not
#                  accept the invocation. The same request will be
#                  rejected again.
#   node_cap       the tile is too dense, and OsmSource has already
#                  split it as far as splitting goes (Task 26). The
#                  answer is a smaller extent, not another identical
#                  request. Retrying this would also quietly undo that
#                  whole mechanism by turning a bounded subdivision into
#                  an unbounded re-ask.
#   no_output      the layer finished and left nothing, with no reason
#                  given. mapgen does not know what to fix.
#   unknown        by construction the kind a source uses when it cannot
#                  say what happened. An unrecognised cause retried
#                  blind is how a rate-limited API gets hammered.
#
# Lives here rather than in package.py, where Task 30 wrote it, because
# Task 32 gave a source a reason to read it: OvertureSource can have
# several types fail at once with different kinds, one record has to
# stand for all of them, and the one it reports must be the one that
# lets the recoverable type be recovered. package.py re-exports the
# name, so this is still the ONE policy every layer is judged by; what
# moved is only which module it is written in. package.py remains the
# only place that ACTS on it.
RETRYABLE_FAILURE_KINDS = frozenset(
    {
        FAILURE_TIMEOUT,
        FAILURE_UNREACHABLE,
        FAILURE_RATE_LIMITED,
        FAILURE_SERVICE_ERROR,
    }
)


def classify_transport_failure(exc: BaseException) -> tuple[str, str]:
    """Why a request never produced a response at all, as (kind, phrase).

    Classified by exception TYPE, never by the text of the exception, and
    the phrase is composed here rather than taken from str(exc). Both of
    those are the same decision: a requests exception's own message
    embeds the URL it was called with, and a URL is where an API key
    lives. OSM's endpoints carry no key, but elevation's carry one on
    every single request, and a classifier shared by both cannot be
    allowed to depend on which of them called it.

    An unrecognised exception contributes its class name and nothing
    else. A class name is not a traceback and cannot carry a query
    string, and "the request failed with ConnectionResetError" is at
    least a fact the owner can quote at someone.

    Lives here, in the module that already owns the failure vocabulary,
    rather than in osm.py where Task 30 first wrote it. Task 32 gave
    elevation the same need, and two sources classifying the same
    transport failures from two separately maintained copies is how one
    of them quietly stops matching the vocabulary the retry policy reads.
    osm.py re-exports both of these so every existing importer is
    unaffected.
    """
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return FAILURE_TIMEOUT, "did not answer in time"
    if isinstance(exc, (requests.exceptions.ConnectionError, ConnectionError, OSError)):
        return FAILURE_UNREACHABLE, "could not be reached"
    return FAILURE_UNKNOWN, f"failed with {type(exc).__name__}"


def classify_status_failure(status_code: int) -> tuple[str, str]:
    """Why a response was not usable, as (kind, phrase).

    The split that matters is not 4xx against 5xx, it is "a retry could
    plausibly fix this" against "a retry will get the same answer four
    times". 429 sits on the 4xx side of the HTTP line and the retryable
    side of this one, which is the whole reason this returns a kind
    rather than letting package.py look at the number.

    401 and 403 are the pair this exists for as much as any: elevation is
    the one keyed source in this project, a wrong or expired key answers
    401 on every attempt, and retrying it wastes the owner's own quota
    while delaying the honest error.
    """
    if status_code == 429:
        return FAILURE_RATE_LIMITED, f"asked mapgen to slow down (HTTP {status_code})"
    if status_code in (401, 403):
        return FAILURE_NOT_AUTHORISED, f"refused the request as not allowed (HTTP {status_code})"
    if status_code >= 500:
        return FAILURE_SERVICE_ERROR, f"answered HTTP {status_code}"
    return FAILURE_REFUSED, f"rejected the request (HTTP {status_code})"


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    """The seconds a service asked to be left alone for, or None.

    One parser, shared, because two of them would eventually disagree
    about the same header. osm.py's retry_delay_seconds calls it for its
    own per-request backoff (which has honoured Retry-After since long
    before Task 32) and elevation attaches the result to its failure
    record so package.py's retry pass can wait it out rather than
    ignoring an instruction the service gave in writing.

    None for an absent header and for the HTTP-date form, which this
    deliberately does not parse: the delta-seconds form is what every
    service in play here sends, and a caller that gets None falls back on
    its own generic backoff, which is a better answer than a date parsed
    against a clock that may not agree with the server's.

    Never negative. A malformed "Retry-After: -5" used to reach
    time.sleep(-5) through retry_delay_seconds, which raises ValueError
    and would take the whole run down over a bad header. Floored at zero
    instead, which is the same as "do not wait".
    """
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)


@dataclass(frozen=True)
class TileFailure:
    """One tile, one source, one reason it is not here.

    Composed by the source that failed, because that is the only place
    that knows what the service actually said, and read by package.py,
    which knows nothing about any individual source. The pair of fields is
    deliberate: `kind` is for code (see the retry policy in package.py),
    `reason` is for the owner and must be a plain sentence they can act
    on. A raw traceback is not a reason and neither is an HTTP status on
    its own.

    reason must never carry a URL. Not because a URL is unreadable, but
    because a URL is where an API key lives: mapgen.sources.elevation
    spent five review rounds learning that every path a request's own text
    can take out of a source is a path a key can take with it. Composing
    these sentences from a fixed vocabulary plus a status code, never from
    str(exc) and never from the endpoint that was called, makes the leak
    structurally impossible here rather than merely absent today.
    """

    source: str
    tile_id: str
    kind: str
    reason: str
    # How long the service asked to be left alone for, in seconds, when
    # it said so in a Retry-After header (Task 32). None means it did not
    # say, which is the ordinary case and every case before this field
    # existed.
    #
    # Set only by a source whose OWN retry has not already waited it out.
    # OsmSource deliberately leaves it None: its per-request loop honours
    # Retry-After between its four attempts (see retry_delay_seconds), so
    # by the time a tile reaches package.py the wait has already been
    # served, and attaching it here would have the outer pass serve it a
    # second time. ElevationSource sets it, because elevation makes one
    # request and has no inner loop to wait in.
    retry_after_seconds: float | None = None

    def to_record(self) -> dict[str, str]:
        """The plain-dict form that reaches survey.json and the progress
        event stream. One shape in both places, so a reader of the file
        and a reader of the live event stream never have to learn two.

        retry_after_seconds is deliberately NOT in it. It is a fact about
        how this run should behave next, not about what happened, and
        package.py's ledger keeps it separately for exactly that reason;
        putting it here would add a mostly-null field to every record in
        every survey.json to serve one decision made seconds later in the
        same process.
        """
        return {
            "source": self.source,
            "tile_id": self.tile_id,
            "kind": self.kind,
            "reason": self.reason,
        }


class UnknownSourceError(KeyError):
    """Raised when a source id is not in the registry."""


class DuplicateSourceError(ValueError):
    """Raised when two sources try to register the same id."""


class EmptySourceSelectionError(ValueError):
    """Raised for an explicitly empty layer selection.

    The exact counterpart of mapgen.categories.EmptyCategorySelectionError,
    and open for the same reason it was: server.py built source_ids with
    `payload.get("sources") or ("osm", "overture")`, three lines above its
    own comment explaining why `or` is wrong for the sibling field. A
    browser that sends sources: [] (which app.js does, since it maps the
    ticked boxes and an empty checklist maps to an empty array) therefore
    got the full default set back. Unticking every layer downloaded both
    of them: on the owner's Barry extent that is 72 rate-limited OSM tiles
    plus eight whole-extent Overture downloads of data they had just
    switched off.

    Refused rather than honoured, matching the category ruling exactly:
    there is no workflow this tool serves where "survey no layers at all"
    is a useful, intentional request, and the owner reaches this state by
    accident. A survey with no sources would write a package holding
    nothing but a survey.json, reported complete: true.

    A ValueError subclass so server.py's _REQUEST_VALUE_ERRORS already
    turns it into the same plain 400 an unknown category gets, and
    cli.py's own exception tuple needs the same one-line addition
    UnknownCategoryError and EmptyCategorySelectionError each needed.

    Absent is not this, exactly as it is not for categories: a request
    that never mentions sources at all (an older client, or the CLI
    without --source) means "the default set", and only a present,
    empty list means "none". server.py and cli.py are what keep those
    two apart before a SurveyRequest is ever built.
    """


@runtime_checkable
class ProgressSink(Protocol):
    def emit(self, event: str, **fields: object) -> None: ...


class NullProgress:
    """Discards every event. Used by tests and non-interactive CLI runs."""

    def emit(self, event: str, **fields: object) -> None:
        return None


@runtime_checkable
class LayerSource(Protocol):
    """A survey data source.

    Sources may expose additional read-only attributes beyond this protocol for
    survey.json provenance tracking. These are deliberately not part of the
    protocol because they are source-specific: for example, OsmSource exposes
    endpoints_used (WMS server addresses), but ElevationSource has no equivalent.
    Consumers must read such attributes with getattr(source, 'attribute_name',
    default_value) to handle sources that do not expose them.

    The same convention covers optional zero-argument methods, not only
    attributes: ElevationSource defines readiness_problem() -> str | None,
    read the same defensive way (getattr(source, 'readiness_problem', None),
    called only if present), which package.py's estimate_survey uses to
    surface a missing API key as a warning on the estimate without knowing
    "elevation" by name. Any future source with its own pre-flight
    prerequisite gets the same treatment for free by defining the same
    method; a source with nothing to check simply omits it. OsmSource's
    configure() and routing_note() (Task 19: request-scoped category
    selection) are the same convention again, for a different purpose.

    api_key_config_field is the same convention for a different job (Task
    19's settings panel): a source with requires_api_key = True names the
    mapgen.config.Config field its own key is saved under (ElevationSource:
    "opentopography_api_key"), so the panel is built from this registry
    rather than one hard-coded input per key. A source with no key omits
    it, the same as any other optional attribute here.

    possible_outputs(stem) -> list[str] is the same convention once more,
    for the stale-output sweep: every package-root-relative file this
    source could EVER merge for the given stem, across all selections, not
    only the ones the current request asked for. package.py deletes any of
    these names it finds on disk that the current, complete run did not
    itself produce, which is what stops a resumed run with a narrower
    selection leaving the previous attempt's wider output beside a
    survey.json that never mentions it. The closed list is the safety
    property: a file the user dropped into the package folder can never
    match it, so the sweep cannot touch anything mapgen did not write.

    tile_failures is the same convention again, for Task 30: a list of
    TileFailure records describing the tiles THIS source's most recent
    fetch() call attempted and could not deliver, reset at the top of
    every fetch() so it always describes that call and never accumulates
    across a retry. A source that keeps one sets it; a source that does
    not simply never defines it, and package.py's getattr reads an empty
    list, which means "this layer failed as a whole and cannot say which
    tiles", the behaviour every source had before this existed.

    Setting it does NOT mean fetch() stops raising. A tiled source should
    continue past a failed tile, collect the failures, and raise once at
    the end of the loop, so a direct Python caller still learns that
    something went wrong rather than getting a short list back in
    silence. What package.py does with the exception is different when
    tile_failures is populated: it has an account it can retry from, so
    it defers the decision until after the retry pass instead of ending
    the layer on the first bad tile.

    fetch()'s own `cancel` parameter (Task 22) is different in kind from
    everything above: those are all optional EXTENSIONS, read defensively
    with getattr because a source that has no opinion on them can simply
    not define them at all. fetch() is not optional, every source has one,
    only its willingness to be interrupted mid-loop varies. Adding a
    required parameter to it would break every source, real or a test
    double, that predates this task in one stroke, which is exactly the
    "control that appears to work and does not" failure this codebase
    keeps naming as the thing to avoid. So `cancel` is optional in the
    other sense instead: a keyword argument, default None, threaded
    through package.py's own call site only when
    inspect.signature(source.fetch) actually names it (see package.py's
    _fetch_accepts_cancel). A source that accepts it should call
    cancel.raise_if_cancelled() between whole units of paid-for work, the
    tile loop for OsmSource, the type loop for OvertureSource (Task 23:
    it was the (tile, type) loop while Overture was still tiled), so a
    request already in flight is always allowed to finish and be kept,
    never interrupted mid-request. A source that runs its units
    concurrently checks in two places instead of one, before submitting
    each unit and again at the start of each worker, and what a stop can
    then skip is only what has not started: OvertureSource since Task 24
    downloads up to eight types at once, so a stop landing once every
    selected type is already in flight skips nothing and its fetch()
    returns normally, leaving package.py's own checkpoint after the source
    to end the run. That is this same convention applied honestly, not a
    hole in it, but it does mean a stop costs more downloading than it
    used to. A source that omits the parameter
    entirely, including every stub in this project's own test suite, is
    still called exactly as before and still works: it is simply not
    interruptible mid-fetch, and the next checkpoint (between sources, or
    before the bridge step, both in package.py) is what actually stops
    the run for it.
    """

    id: str
    display_name: str
    licence: str
    attribution: str
    requires_api_key: bool

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate: ...

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]: ...

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """merge()'s output filename(s) must embed stem (PackagePaths.stem: the
        site and date, for example Barry-Waterfront_2026-08-03), not a bare
        generic name. Task 20 finding 2: OsmSource.merge used to write a fixed
        out_dir / "all.osm" regardless of which survey it belonged to, which
        gave the owner no way to tell one package's reference file from
        another's once copied elsewhere, and did not identify the file Urbano
        needs to read. Every source's merge follows the same rule now, so a
        bare, unidentifiable output name does not reappear the next time a
        source is added.
        """
        ...


_REGISTRY: dict[str, LayerSource] = {}


def register(source: LayerSource) -> None:
    if source.id in _REGISTRY:
        raise DuplicateSourceError(
            f"id {source.id!r} is already registered. Each source needs a unique "
            f"id; if you copied an existing source module, change its id attribute."
        )
    _REGISTRY[source.id] = source


def get_source(source_id: str) -> LayerSource:
    try:
        return _REGISTRY[source_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise UnknownSourceError(
            f"Unknown source {source_id!r}. Available sources: {known}."
        ) from None


def available_sources() -> list[LayerSource]:
    return list(_REGISTRY.values())


def clear_registry() -> None:
    _REGISTRY.clear()
