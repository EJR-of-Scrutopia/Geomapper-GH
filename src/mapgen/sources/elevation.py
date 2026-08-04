"""Elevation as a LayerSource, via the OpenTopography global DEM API.

Requested for the whole study bbox in one call rather than per tile, because the
API already handles arbitrary extents and stitching tiled DEMs is needless work.

Which DEM is fetched is a choice as of Task 28, not the fixed COP30 this
module was written around. The vocabulary of models, and an honest account
of what choosing between them actually buys (a surface model or bare earth
terrain, never a finer resolution), lives in mapgen.elevation_models.

Phase 2 note: NRW LiDAR at 1 m will be a sibling module here, and is a far better
source than any of them for anywhere in Wales. Nothing OpenTopography's
global API serves is finer than 30 m.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote, quote_plus

import requests

from mapgen.config import load_config
from mapgen.elevation_models import (
    DEFAULT_DEMTYPE,
    model_for,
    offered_choices,
    work_file_name,
)
from mapgen.fsutil import atomic_write_bytes
from mapgen.geo import BBox, Tile, extent_metres
from mapgen.jobs import CancelToken, Cancelled
from mapgen.sources.base import Estimate, ProgressSink

DEFAULT_OPENTOPOGRAPHY_URL = "https://portal.opentopography.org/API/globaldem"
USER_AGENT = "mapgen/1.0 (architectural survey tool)"

# Measured 2026-08-04 (Task 25) through ElevationSource.fetch against the
# live OpenTopography API, three Welsh extents, two samples each:
#
#      4.17 sq km    10.94s, 11.16s      29,606 bytes
#     38.63 sq km    11.26s, 11.30s     254,479 bytes
#    259.60 sq km    11.58s, 11.60s   1,005,162 bytes
#
# Sixty-two times the area for 1.04x the time. What is being paid for is
# the service's own turnaround on a request, not a transfer: even the
# largest of these is a megabyte, which is nothing on this link. The
# estimate said 5.0s for all three, understating by 2.2x, and it was the
# only source in the panel understating rather than overstating. That
# matters more than the six seconds do, because a countdown built on an
# under-estimate runs out while the work is still going.
#
# Two significant figures, from six samples over one afternoon against
# one API. The spread within an extent is hundredths of a second, which
# is suspiciously tight for a network measurement and should be read as
# "one machine on one day", not as a property of the service.
SECONDS_FLOOR = 11.0

# 4 bytes per pixel, not 2 with a fudge for overhead. Measured against
# COP30 and applied to every model since Task 28 made the model a choice,
# which errs in the honest direction: a model that comes back as Int16
# instead is overstated by this, never understated. COP30 comes back as
# Float32 and the arithmetic says so: 42,943 pixels at 38.63 sq km
# against 254,479 bytes measured is 5.9 bytes per pixel with the header
# in it, and 288,384 pixels at 259.60 sq km against 1,005,162 bytes is
# 3.5, either side of 4 as compression starts paying for itself on the
# larger file. The old 2 bytes plus 20% was a 16-bit assumption and read
# 0.40x at the middle extent.
BYTES_PER_PIXEL = 4.0

# The floor is the smallest real file this has ever been seen to return,
# rounded down: 29,606 bytes over 4.17 sq km, where the GeoTIFF header
# and tiling structure cost more than the 4,629 pixels inside it. The
# 100,000 it replaces was 3.4x that.
SMALLEST_OBSERVED_BYTES = 29_000

# Never observed to bind. The largest DEM measured is a megabyte, which
# this rate would put at 2s, well under SECONDS_FLOOR. It is kept for an
# extent far larger than anything the owner has drawn, where transfer
# would eventually dominate the service's turnaround, and it is honest to
# say that no measurement supports the particular value.
BYTES_PER_SECOND_ESTIMATE = 500_000.0


class ElevationError(RuntimeError):
    """Raised when the DEM could not be downloaded or was not a TIFF."""


class MissingApiKeyError(ElevationError):
    """Raised when no OpenTopography API key is configured."""


def is_tiff(header: bytes) -> bool:
    return header.startswith(b"II*\x00") or header.startswith(b"MM\x00*")


REDACTION_PLACEHOLDER = "[REDACTED]"
# Used only when redaction itself cannot be trusted: the secret was found
# in the text in some form _redact's own substitutions did not already
# catch (today: hiding behind interspersed NUL bytes). Withholding the
# whole text is the honest answer there, not a best-effort scrub of a
# shape nothing here anticipated.
WITHHELD_PLACEHOLDER = "[response withheld: contained the API key in an unexpected form]"

# The PRIMARY defence, per review round 3: match the PARAMETER, not the
# value. Three rounds of value-matching each closed one gap and opened
# the next (round 1 enumerated exception types, round 2 enumerated
# message sources, and round 3's own finding, a bare space becoming `+`
# under requests' query-string encoding rather than the `%20` a plain
# quote() produces, is a THIRD encoding nobody had added a candidate
# for). This pattern does not look at the value at all: whatever comes
# after "API_Key=", encoded however, cased however, containing whatever,
# up to the next & or whitespace, is removed sight unseen. That is what
# makes it robust to an encoding nobody has thought of yet, which no
# amount of adding more candidates to a value-matching list can be.
_API_KEY_PARAM_RE = re.compile(r"API_Key=[^&\s]*", re.IGNORECASE)


def _redact(text: str, secret: str | None) -> str:
    """Removes the API_Key parameter from text structurally, and the raw
    secret value by content as a secondary backstop.

    The primary defence is _API_KEY_PARAM_RE: it matches on the fixed,
    known parameter name this module itself sends, never on the value,
    so it does not care how that value was encoded, what case it is in,
    whether it contains a space, or any other detail of some future
    request library's own query-encoding choices. That is the whole
    point: matching the value was the mistake being repeated every round.
    It runs unconditionally, even when secret is None or empty, because
    it does not need to know the value to recognise the parameter: a
    caller with no secret in hand (this module's own logging filter,
    below, is exactly such a caller) still gets the same structural
    protection as a caller that has one.

    The secondary backstop still matches the value, for the one case the
    parameter pattern cannot cover: the key appearing somewhere that is
    not shaped like "API_Key=...", for instance quoted back inside a
    JSON error body's own message text. It only runs when a secret is
    actually supplied, is not the primary defence, and is not trusted
    alone: both the raw key and its percent-encoded forms are tried
    (quote() and quote_plus(), since requests itself encodes a
    query-string space as "+", which quote() alone does not produce),
    matched case-insensitively, since nothing guarantees a key survives
    in its original case or encoding scheme by the time it reaches this
    function.

    One more defensive check, past the substitutions above: a UTF-16
    error page decoded with the UTF-8 codec (errors="replace") does not
    raise, since every byte of ASCII-range UTF-16LE text is independently
    valid UTF-8 on its own; it produces the original characters each
    followed by a stray NUL ("s\\x00k\\x00-\\x00..."), which reads as the
    key to a human, since a terminal or a browser renders NUL as
    invisible, while containing no contiguous match for a plain substring
    search. Checked by comparing the ORIGINAL text (NUL-stripped) against
    the ORIGINAL text (untouched): if stripping NULs reveals a match that
    was not already there, the ordinary substitutions above never had
    anything contiguous to work with, and the honest answer is to
    withhold the text entirely rather than publish a redaction that
    cannot have actually caught it. Deliberately compared against the
    ORIGINAL text on both sides, not against this function's OWN redacted
    output: an earlier version of this check searched the already-redacted
    text, which meant a secret that happens to be a case-insensitive
    substring of the word "REDACTED" itself, the placeholder every
    ordinary substitution inserts, self-triggered on completely innocent,
    NUL-free text that never contained the secret in any form at all,
    merely because the text now contained the word used to mark that a
    redaction had happened.
    """
    redacted = _API_KEY_PARAM_RE.sub("API_Key=" + REDACTION_PLACEHOLDER, text)
    if not secret:
        return redacted

    candidates = {secret, quote(secret, safe=""), quote_plus(secret)}
    for candidate in candidates:
        redacted = re.sub(re.escape(candidate), REDACTION_PLACEHOLDER, redacted, flags=re.IGNORECASE)

    original_stripped = text.replace("\x00", "")
    for candidate in candidates:
        pattern = re.escape(candidate)
        if re.search(pattern, original_stripped, flags=re.IGNORECASE) and not re.search(
            pattern, text, flags=re.IGNORECASE
        ):
            return WITHHELD_PLACEHOLDER
    return redacted


def _scrub_exception_chain(exc: BaseException, secret: str | None) -> None:
    """Redacts secret from exc and every exception chained to it via
    __cause__ or __context__, in place: from each one's own .args, AND
    from the non-string attributes that carry a URL or a path rather
    than text, which is where a genuine `requests` failure actually
    keeps the query string.

    Review round 3 corrected a false claim this docstring used to make:
    an earlier version of this function redacted .args only, and said
    the chain was "already clean" afterward. That was not true. A
    ConnectionError's most informative content is very often not in
    args at all: `.request` and `.response` (set by requests itself on
    most of its own exception classes) each carry a `.url` that
    duplicates the exact query string, including API_Key, independent
    of whatever str(exc) says, and the lower-level OSError/socket.error
    requests frequently wraps carries the same information again in
    `.filename`/`.filename2`. A reader that inspects those attributes
    directly, which is exactly what a "rich" traceback renderer or a
    debugger's exception inspector does, saw the raw key regardless of
    how thoroughly .args was scrubbed. All four attributes are checked
    on every exception in the chain now, not only the newly-raised one,
    since __cause__/__context__ can themselves be genuine requests
    exceptions carrying their own populated .request/.response.

    `raise ... from None` at the call site is what actually stops a
    standard traceback from printing the chain at all, by telling
    Python's own formatting machinery to suppress it regardless of what
    it contains: that is what closes this for every ordinary rendering
    path (an uncaught exception reaching the interpreter's own top level,
    logging.exception, traceback.format_exc). This function is the
    second, independent layer underneath it: if anything ever reads
    __cause__ or __context__ directly instead of going through that
    machinery, or a future call site on this path forgets the
    `from None`, the chained exceptions' own text and attributes are
    already clean rather than depending on that not happening. Bounded
    against a cyclical chain (which should not be possible in practice)
    with a seen-set, since this walks the chain in a plain loop rather
    than recursion.
    """
    if not secret:
        return
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        current.args = tuple(
            _redact(arg, secret) if isinstance(arg, str) else arg for arg in current.args
        )
        # OSError's own path attributes: harmless on a plain file-system
        # error, but requests' connection-level exceptions frequently
        # wrap a socket/OSError whose .filename has been used, in
        # practice, to stash the address or URL involved.
        for attr in ("filename", "filename2"):
            value = getattr(current, attr, None)
            if isinstance(value, str):
                setattr(current, attr, _redact(value, secret))
        # requests' own .request (a PreparedRequest) and .response (a
        # Response): both expose a plain, directly-settable .url string
        # attribute holding the exact query string that was sent,
        # unrelated to anything in .args.
        for holder_attr in ("request", "response"):
            holder = getattr(current, holder_attr, None)
            url = getattr(holder, "url", None) if holder is not None else None
            if isinstance(url, str):
                holder.url = _redact(url, secret)
        current = current.__cause__ or current.__context__


class _ApiKeyRedactingFilter(logging.Filter):
    """Redacts API_Key=... from every log record this filter sees,
    whether or not any exception is involved.

    urllib3.connectionpool logs the full request line, api key and all,
    at DEBUG level for every request it makes, success or failure alike
    ("Starting new HTTPS connection", then the GET line with the full
    query string). Confirmed directly against a real local HTTP server
    and a real requests.Session with DEBUG logging enabled: the key
    appeared in caplog.text with no exception raised anywhere. Nothing
    above this point in the module touches it, because _redact and
    _scrub_exception_chain only ever run once something has already
    gone wrong; this leak path does not depend on anything going wrong
    at all; whenever DEBUG logging (or any tool that captures log
    records as it runs, such as Sentry's breadcrumbs) is active, it
    fires on the happy path too.

    Delegates to _redact with no secret, rather than repeating the
    pattern substitution inline: this filter has no secret value in
    hand at all (it is installed once, at import time, for every
    request this process will ever make), which is a live demonstration
    of why the parameter-based defence had to become primary rather than
    staying a value-matching backstop. _redact's own primary step runs
    unconditionally for exactly this caller.

    record.msg is overwritten with the fully rendered, already-redacted
    text and record.args is cleared, rather than leaving record.msg as
    the original %-style format string: logging defers "%" formatting
    until a handler actually emits the record, by calling
    record.getMessage(), which formats msg against args again from
    scratch. Changing msg alone and leaving the original args in place
    would have the raw key reappear the moment anything downstream
    called getMessage().
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _redact(message, None)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


# Installed at import time, not per-ElevationSource-instance: the leak is
# in urllib3's own logger, which exists independently of anything this
# module constructs, and DEBUG logging can be enabled by the process
# hosting this code (a test runner, a `--verbose` CLI flag, Sentry) at
# any time. Guarded so re-importing this module (as happens under some
# test-reload setups) cannot install the same filter twice.
_urllib3_logger = logging.getLogger("urllib3.connectionpool")
if not any(isinstance(existing, _ApiKeyRedactingFilter) for existing in _urllib3_logger.filters):
    _urllib3_logger.addFilter(_ApiKeyRedactingFilter())


def _clean_key_candidate(value: str | None) -> str | None:
    """Strips a key candidate and treats an all-whitespace result as
    absent, at the boundary where each candidate enters this function.

    Round 3's finding: a trailing space, invisible in most editors and
    terminal displays, pasted into the settings field or left in an
    exported environment variable, survived resolve_api_key untouched
    and reached requests as a real character of the query value. Under
    requests' own query-string encoding that space becomes a literal
    "+", a THIRD encoding (after the raw character and %20) that a
    value-matching redaction candidate list had not been told to expect,
    which is exactly the kind of gap _redact's parameter-based primary
    defence no longer depends on avoiding by enumeration. Stripping
    here, once, at the point a candidate is considered, means no
    downstream code has to know or remember that a key might be padded.
    An all-whitespace candidate strips to "", which is falsy, so it
    falls through to the next source in the precedence chain exactly as
    an unset one would, rather than being treated as a configured, valid
    key that happens to be blank.
    """
    if value is None:
        return None
    return value.strip() or None


def resolve_api_key(
    explicit: str | None = None,
    environ: Mapping[str, str] | None = None,
    configured: str | None = None,
) -> str:
    """explicit, then the environment, then configured, then failure.

    explicit and the two environment variables are the original,
    unchanged precedence. configured is Task 18's addition: the key saved
    via the interface's settings field. The owner's ruling was explicit
    that "an environment variable still tak[es] precedence if one is
    set", so it sits above configured, not below it: a key set in the
    shell for one machine or one debugging session overrides whatever is
    saved in ~/.mapgen/config.json, not the other way round.
    """
    env = os.environ if environ is None else environ
    key = (
        _clean_key_candidate(explicit)
        or _clean_key_candidate(env.get("OPENTOPOGRAPHY_API_KEY"))
        or _clean_key_candidate(env.get("OPENTOPO_API_KEY"))
        or _clean_key_candidate(configured)
    )
    if not key:
        raise MissingApiKeyError(
            "Elevation download needs an OpenTopography API key. Set the "
            "OPENTOPOGRAPHY_API_KEY environment variable, or clear the elevation "
            "layer to skip it. Keys are free from portal.opentopography.org."
        )
    return key


def _redact_response_urls(response: object, secret: str) -> None:
    """Redacts .url on response and on response.request, in place.

    Factored out because review round 4 found the first of what would
    otherwise have been three near-identical copies of this loop: one in
    fetch()'s status-check branch, one in its non-TIFF branch, both
    reachable with no exception in play, so _scrub_exception_chain (which
    only ever walks an exception's own chain) never gets a chance at
    either. One function used twice cannot drift out of step with itself
    the way two independently maintained copies eventually would.
    """
    for holder in (response, getattr(response, "request", None)):
        url = getattr(holder, "url", None) if holder is not None else None
        if isinstance(url, str):
            holder.url = _redact(url, secret)


class ElevationSource:
    id = "elevation"
    # Names no model, on purpose. This is the attribute GET /api/sources
    # and `mapgen sources` read off the ONE instance
    # register_default_sources() builds at startup, and that instance's own
    # demtype is not what any particular request will use: the request's
    # is (see configure below). "Elevation (OpenTopography COP30)", which
    # this said before Task 28, would therefore go on claiming COP30 in
    # the layer checklist while the settings panel beside it showed
    # EU_DTM. A configured, request-scoped copy overrides this with the
    # model that request actually asked for, which is the one place naming
    # a model is true rather than merely plausible.
    display_name = "Elevation (OpenTopography)"
    # Class-level defaults matching DEFAULT_DEMTYPE, so the LayerSource
    # protocol's attributes exist on the class exactly as they always
    # have. __init__ replaces both with the chosen model's own terms: a
    # package that downloaded EU_DTM must not record Copernicus'
    # copyright line in its survey.json, and one that downloaded SRTMGL1
    # must not claim a licence OpenTopography does not state for it.
    licence = "Copernicus DEM, free for any use with attribution"
    attribution = "(c) DLR e.V. 2010-2014, (c) Airbus Defence and Space GmbH"
    requires_api_key = True
    # Task 28: the settings panel's model select is built from this rather
    # than from a hardcoded list in index.html, the same registry-driven
    # convention api_key_config_field established for the key fields (see
    # sources/base.py). GET /api/sources reads it the same defensive way;
    # a source with no models to choose between does not define it at all.
    demtype_choices = offered_choices()
    # Task 19: the settings panel is driven from the source registry
    # rather than one hard-coded field per key, so a source that needs a
    # key must say which mapgen.config.Config field holds it. Not
    # "elevation_api_key": that field was already named
    # opentopography_api_key by Task 18 and is already saved in real
    # config.json files on the owner's machine, so the field itself is
    # not renamed to fit a tidier convention after the fact, only made
    # discoverable from the source that owns it. GET /api/sources reads
    # this the same defensive, optional-attribute way as
    # requires_api_key's siblings elsewhere in this codebase; a source
    # with no key does not define it at all.
    api_key_config_field = "opentopography_api_key"

    def __init__(
        self,
        api_key: str | None = None,
        demtype: str = DEFAULT_DEMTYPE,
        session: object | None = None,
        url: str = DEFAULT_OPENTOPOGRAPHY_URL,
        timeout_seconds: int = 600,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self.demtype = demtype
        # Never raises for a demtype this project has no record of: see
        # model_for's own docstring. Validation of a person's chosen model
        # happens once, at SurveyRequest construction (mapgen.package),
        # which is where the CLI's --demtype and the browser's select
        # meet, matching what Task 21 established for categories. A
        # constructor is not a second validation site.
        model = model_for(demtype)
        self.licence = model.licence
        self.attribution = model.attribution
        self._resolution_m = model.resolution_m
        self.session = session if session is not None else requests.Session()
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._environ = environ

    def configure(self, demtype: str) -> "ElevationSource":
        """Returns a fresh ElevationSource scoped to this request's chosen
        model, sharing this instance's transport configuration and never
        mutating self.

        The same optional LayerSource extension OvertureSource.configure
        and OsmSource.configure already implement, for the same reason its
        docstring gives at length: register_default_sources() builds ONE
        instance and registers it process-wide, /api/estimate has no
        busy-guard so it can be polled while a job using a different
        selection is mid-fetch, and mutating shared state underneath a
        running job is the kind of race that would be very hard to
        reproduce afterwards.

        This is also the one place a request is genuinely in view, so it is
        the one place display_name is allowed to name a model. The
        registered instance keeps the model-free class attribute, so the
        layer checklist never claims a model the next download may not
        use; the copy package.py actually estimates and fetches with says
        exactly which one it is, which is what `mapgen estimate` prints
        beside the licence.
        """
        configured = ElevationSource(
            api_key=self._api_key,
            demtype=demtype,
            session=self.session,
            url=self.url,
            timeout_seconds=self.timeout_seconds,
            environ=self._environ,
        )
        configured.display_name = f"Elevation (OpenTopography {demtype})"
        return configured

    def output_name(self) -> str:
        """The file this source downloads to inside its work directory.

        Carries the model's own name; see elevation_models.work_file_name
        for why that is what keeps a resumed run's survey.json honest.
        """
        return work_file_name(self.demtype)

    def _configured_key(self) -> str | None:
        """The key saved via the interface's settings field, if any.

        Read fresh on every call rather than cached at construction: this
        source is registered once per server process, but the owner can
        change the saved key at any point through PUT /api/config, and
        that must take effect on the next estimate or job without a
        restart. load_config() defaults to str, so a config file with no
        key at all, or a corrupt one, resolves to "" here, normalised to
        None so it never wins resolve_api_key's `or` chain by accident.
        """
        return load_config().opentopography_api_key or None

    def readiness_problem(self) -> str | None:
        """A plain-English problem if this source cannot run right now,
        else None.

        This is the optional LayerSource extension documented on the
        protocol itself (see sources/base.py: attributes and methods
        beyond the protocol are source-specific and read defensively via
        getattr). package.py's estimate_survey calls this, if present,
        for every selected source, so a missing key is visible on the
        estimate rather than discovered only after OSM and Overture have
        already finished downloading.
        """
        try:
            resolve_api_key(self._api_key, self._environ, self._configured_key())
        except MissingApiKeyError as exc:
            return str(exc)
        return None

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        """One whole-area request, so nothing here counts tiles.

        Both constants were refitted from measurement in Task 25; see
        their definitions for the runs behind them and for how thin the
        evidence is. The shape is unchanged: pixels at the chosen model's
        own ground sample distance, and a time that is a floor until the
        file gets big enough for transfer to matter, which nothing the
        owner has drawn comes close to.

        The 30 m this used to hardcode was COP30's resolution, and Task 28
        made the model a choice, so it reads the model's own figure now.
        Picking COP90 really is a ninth of the pixels, and an estimate
        that went on quoting COP30's size for it would overstate the
        download by nine times. The TIME is unaffected in practice either
        way: SECONDS_FLOOR dominates for anything the owner draws, because
        what is being paid for is the service's turnaround, not the
        transfer.
        """
        width_m, height_m = extent_metres(bbox)
        pixel_count = (width_m / self._resolution_m) * (height_m / self._resolution_m)
        bytes_estimate = max(
            int(pixel_count * BYTES_PER_PIXEL), SMALLEST_OBSERVED_BYTES
        )
        seconds_estimate = max(
            bytes_estimate / BYTES_PER_SECOND_ESTIMATE, SECONDS_FLOOR
        )
        return Estimate(bytes_estimate=bytes_estimate, seconds_estimate=seconds_estimate)

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        # Elevation has no per-tile loop to check between iterations of
        # (see the module docstring: one whole-area request, not one per
        # tile), so the only meaningful checkpoint is before that single
        # request starts. Checked before the already-downloaded skip
        # branch too: a resumed, already-satisfied elevation fetch costs
        # nothing either way, but a stop requested while this source is
        # merely next in line should not start a slow DEM download at all.
        if cancel is not None:
            cancel.raise_if_cancelled()
        # Named after the model, so "already downloaded" means "already
        # downloaded THIS model" rather than "some DEM is sitting here".
        # See elevation_models.work_file_name for the exact run that used
        # to record a model in survey.json which was never the one on disk.
        output_path = work_dir / self.output_name()
        if output_path.exists() and output_path.stat().st_size > 0:
            progress.emit("tile_skipped", source=self.id, tile_id="whole-area")
            return [output_path]

        api_key = resolve_api_key(self._api_key, self._environ, self._configured_key())
        params = {
            "demtype": self.demtype,
            "south": f"{bbox.south:.7f}",
            "north": f"{bbox.north:.7f}",
            "west": f"{bbox.west:.7f}",
            "east": f"{bbox.east:.7f}",
            "outputFormat": "GTiff",
            "API_Key": api_key,
        }

        # The whole request/response cycle is inside one try, including
        # session.get() itself: a connection refused or DNS failure raises
        # before any response object exists at all, so a boundary that
        # only wrapped the body-reading step (as an earlier version of
        # this method did) never saw that failure to redact it. Review
        # round 1 proved this end to end: a fake connection failure, read
        # timeout and mid-stream drop, each carrying the real key in its
        # own message the way a genuine requests exception does, all
        # reached run_survey's source_failed event and JobManager's
        # record.error unredacted under the previous, HTTPError-only fix.
        #
        # A bad status is deliberately NOT raised from inside this try.
        # Review round 4's finding: it used to be, and the
        # except (ElevationError, ...): raise clause immediately below
        # re-raised it with api_key and params still bound in this frame,
        # uncleared. Three rounds of work fixed the other two raise sites
        # in this method and missed this one precisely because it did
        # not look like a third site: it was hiding inside a try/except
        # that reads, at a glance, as already handled. Only status_code,
        # a plain int with nothing in it to redact, is captured here; the
        # actual raise happens below, after the try, in the same
        # straight-line shape as the non-TIFF check beneath it, so every
        # raise site in this method now looks the same and none of them
        # hide inside a catch-and-reraise clause.
        #
        # Review round 5 corrected a false claim round 4 left behind
        # here: that a bare `raise` "re-raises the exception unchanged
        # and must not run cleanup code of its own, that being exactly
        # what re-raise unchanged means." That conflated two different
        # things. A bare `raise` re-raises the same exception OBJECT:
        # unchanged type, unchanged message, unchanged __traceback__.
        # `del` only removes a name from this frame's OWN local
        # bindings; it never touches the exception object in flight, so
        # it cannot be what changes "unchanged" here. The except clause
        # immediately below now clears api_key and params before its own
        # `raise`, same as every other raise site in this method.
        try:
            with self.session.get(
                self.url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                stream=True,
                timeout=self.timeout_seconds,
            ) as response:
                status_code = response.status_code
                payload = (
                    b"".join(chunk for chunk in response.iter_content(1024 * 1024) if chunk)
                    if status_code < 400
                    else b""
                )
        except (ElevationError, Cancelled, KeyboardInterrupt):
            # Left exactly as raised: type, message and __traceback__ all
            # unchanged. Nothing inside the block above raises any of
            # these three today: the one thing that used to raise
            # ElevationError from in here, the status check, has moved
            # below, out of this try entirely. Kept anyway, for
            # ElevationError alongside Cancelled (this project's own
            # cooperative-cancellation signal) and KeyboardInterrupt
            # (Python's), so a future change to what this block calls
            # cannot silently start swallowing any of the three into a
            # new, differently-worded ElevationError.
            #
            # api_key and params cleared before the raise anyway, same as
            # every other raise site in this method: del only drops
            # these two names from this frame's own locals, which is all
            # pytest --showlocals, Sentry's local-variable capture, and a
            # debugger's postmortem ever read directly, and none of that
            # is the exception object being re-raised, so "left exactly
            # as raised" above still holds byte for byte.
            del api_key, params
            raise
        except Exception as exc:
            # Deliberately broad, and deliberately not a list of specific
            # requests exception classes: see _redact's docstring for why
            # enumerating types is the trap this replaces.
            #
            # Round 1 redacted only the NEW message this raises. That
            # left exc itself, attached unredacted as __cause__ via
            # `from exc`, one uncaught exception away from a full
            # traceback showing the real key: main() does not catch
            # ElevationError, so Python's own default exception printer
            # renders the whole chain, and the browser's own safety here
            # was never a fix, only the accident that JobManager stops at
            # str(exc) and never renders a cause. Closed at the boundary,
            # not at either throw site: exc (and anything already chained
            # to IT) is scrubbed in place first, and the new exception is
            # cut loose from it with `from None`, so a standard traceback
            # does not show the chain at all regardless of what it
            # contains, and even a reader that walks __cause__/__context__
            # directly, bypassing that suppression, finds it already clean.
            _scrub_exception_chain(exc, api_key)
            message = f"Failed to download DEM: {_redact(str(exc), api_key)}"
            # Cleared before the raise, not after: a frame that is still
            # on the stack when an exception propagates through it stays
            # inspectable exactly as it was at that point, to
            # pytest --showlocals, to Sentry's local-variable capture, to
            # a debugger's postmortem, or to any other rich-traceback
            # renderer that reads tb_frame.f_locals, none of which go
            # through _redact or _scrub_exception_chain at all: those
            # only ever touch an exception's OWN text and attributes,
            # never a frame's local variables. api_key is the raw
            # secret itself and params embeds it again as a dict value;
            # deleting the names removes them from f_locals from this
            # point on, so nothing that inspects this frame after the
            # raise finds either one.
            del api_key, params
            raise ElevationError(message) from None

        if status_code >= 400:
            # Review round 4's finding, closed here, moved from inside
            # the try above. response and response.request are still
            # bound in this frame (the `with` block only closes the
            # connection, it does not unbind the name), and .url on each
            # is the literal request URL, API_Key included. No exception
            # exists in this branch for _scrub_exception_chain to ever
            # reach, so it is redacted in place directly through the same
            # helper the non-TIFF branch below uses. api_key and params
            # are deleted before the raise for the same reason as every
            # other raise site in this method: pytest --showlocals,
            # Sentry's local-variable capture, and a debugger's
            # postmortem all read a frame's locals directly, bypassing
            # _redact and _scrub_exception_chain entirely, neither of
            # which touches a frame's own variables. This is also, in
            # practice, the single most frequently reached failure path
            # in the whole module: a wrong or expired key produces a 401
            # on every attempt, which is exactly what made this the
            # finding worth catching before a fifth round had to.
            _redact_response_urls(response, api_key)
            del api_key, params
            raise ElevationError(f"Failed to download DEM: HTTP {status_code}")

        if not is_tiff(payload[:16]):
            # Redacted before truncating, not after: truncating to a
            # fixed byte window first can cut a real key in half (enough
            # padding ahead of "API_Key=" pushes the back part of the key
            # past the window), and that surviving fragment matches
            # nothing a whole-string redaction looks for, so it reaches
            # the browser log exactly as readable as the full key would
            # have been. Decoding and redacting the complete body first,
            # then cutting the ALREADY-SAFE result down to a preview
            # length, leaves no window for a partial key to survive in.
            decoded = payload.decode("utf-8", errors="replace")
            preview = _redact(decoded, api_key)[:300]
            # response and response.request are still bound here (the
            # `with` block above only closes the connection, it does not
            # unbind the name), and .url on each is the literal request
            # URL, API_Key included, entirely untouched by the preview
            # redaction two lines up. No exception exists yet at this
            # point for _scrub_exception_chain to have a chance to reach,
            # so it is scrubbed in place directly, the same way the
            # status-check branch above it does.
            _redact_response_urls(response, api_key)
            # api_key and params for the same reason as the except
            # branch above; payload and decoded because both are the
            # raw, un-redacted response body, which can itself echo the
            # key back verbatim (the exact scenario the WITHHELD
            # fallback in _redact exists to catch) and neither is needed
            # past this point now that preview already holds the safe,
            # truncated text the error message actually uses.
            del api_key, params, payload, decoded
            raise ElevationError(
                f"OpenTopography did not return a TIFF. Response began: {preview}"
            )

        atomic_write_bytes(output_path, payload)
        progress.emit("tile_done", source=self.id, tile_id="whole-area")
        return [output_path]

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Copies THIS model's whole-area DEM into out_dir under the package
        stem, for example Barry-Waterfront_2026-08-03.tif.

        There is nothing to combine (fetch() always produces exactly one
        whole-area file, never one per tile), so earlier versions of this
        method just returned parts unchanged, still sitting under work_dir
        as a fixed, unidentified "elevation.tif". That had two problems,
        not one: Task 20 finding 2's unidentified-filename issue, the same
        as OsmSource's and OvertureSource's, AND the file never actually
        reached the finished package, since a complete run deletes work_dir
        (see run_survey). Copying rather than moving, because parts may
        still be read again on a subsequent run's resume/skip check.

        parts[0] is no longer good enough, and that is Task 28's doing. A
        resumed run whose model changed since the last attempt has BOTH
        files in its work directory, and package.py hands over everything
        it finds there (see _existing_output_files); parts[0] would take
        whichever sorted first, which for a run that just downloaded
        EU_DTM beside a leftover COP30 is the leftover. Matching on this
        instance's own output name is what makes the file that leaves with
        the package the file this run actually fetched.

        Returning nothing when no part matches is deliberate, and it is
        only reachable on a forced run whose elevation fetch failed while
        an older model's file happened to be lying about. A package with
        no DEM, and a survey.json that says so, is a better outcome there
        than one holding a DEM of a model it does not name.

        Review finding I8: that last paragraph described an outcome
        nothing produced. Returning [] removed nothing, and package.py's
        stale-output sweep never runs on an incomplete package, so a
        PREVIOUS model's <stem>.tif stayed in the root while
        _source_provenance recorded this run's configured model and
        __init__ had already replaced licence and attribution with that
        model's terms. The owner's reproducible case: an incomplete
        package holding a COP30 DEM, the model changed to EU_DTM in
        Settings, --force, and OpenTopography answers 401. The folder
        then held a Copernicus surface model while survey.json recorded
        demtype EU_DTM, CC BY 4.0 and the Hengl et al. citation. That is
        a licence statement about the wrong dataset, in the record
        elevation_models.py's own docstring says the owner may one day
        have to stand behind.

        So the stale copy is removed here, which is what makes the
        paragraph above true. This is the same closed-list rule
        package.py's sweep applies (<stem>.tif is the one and only name
        this method ever writes, and possible_outputs below says so), and
        it is applied at the one moment that sweep cannot run. A file the
        owner dropped in themselves can never match it, and a run whose
        DEM did land is untouched, since that takes the branch below.
        """
        wanted = self.output_name()
        chosen = next((part for part in parts if part.name == wanted), None)
        output = out_dir / f"{stem}.tif"
        if chosen is None:
            output.unlink(missing_ok=True)
            return []
        atomic_write_bytes(output, chosen.read_bytes())
        return [output]

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root file merge() could ever write for this stem. Read by
        package.py's stale-output sweep; see sources/base.py."""
        return [f"{stem}.tif"]
