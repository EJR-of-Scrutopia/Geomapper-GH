"""Elevation as a LayerSource, via the OpenTopography global DEM API.

Requested for the whole study bbox in one call rather than per tile, because the
API already handles arbitrary extents and stitching tiled DEMs is needless work.

Phase 2 note: NRW LiDAR at 1 m will be a sibling module here, and is a far better
source than COP30 for anywhere in Wales.
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
from mapgen.fsutil import atomic_write_bytes
from mapgen.geo import BBox, Tile, extent_metres
from mapgen.jobs import Cancelled
from mapgen.sources.base import Estimate, ProgressSink

DEFAULT_OPENTOPOGRAPHY_URL = "https://portal.opentopography.org/API/globaldem"
USER_AGENT = "mapgen/1.0 (architectural survey tool)"
OUTPUT_NAME = "elevation.tif"


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
    display_name = "Elevation (OpenTopography COP30)"
    licence = "Copernicus DEM, free for any use with attribution"
    attribution = "(c) DLR e.V. 2010-2014, (c) Airbus Defence and Space GmbH"
    requires_api_key = True
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
        demtype: str = "COP30",
        session: object | None = None,
        url: str = DEFAULT_OPENTOPOGRAPHY_URL,
        timeout_seconds: int = 600,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self.demtype = demtype
        self.session = session if session is not None else requests.Session()
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._environ = environ

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
        # Estimate based on bbox area. COP30 resolution is 30 m per pixel.
        # Model: pixel_count * 2 bytes per pixel (16-bit elevation) * 1.2 for
        # GeoTIFF overhead. Time estimate scales with data size with a sensible floor.
        width_m, height_m = extent_metres(bbox)
        pixel_count = (width_m / 30.0) * (height_m / 30.0)
        # 2 bytes per pixel + 20% GeoTIFF/compression overhead
        bytes_estimate = max(int(pixel_count * 2 * 1.2), 100_000)
        # Rough model: 500 KB/second download rate, minimum 5 seconds
        seconds_estimate = max(bytes_estimate / 500_000.0, 5.0)
        return Estimate(bytes_estimate=bytes_estimate, seconds_estimate=seconds_estimate)

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
    ) -> list[Path]:
        output_path = work_dir / OUTPUT_NAME
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
        """Copies the single whole-area DEM into out_dir under the package
        stem, for example Barry-Waterfront_2026-08-03.tif.

        There is nothing to combine (fetch() always produces exactly one
        whole-area file, never one per tile), so earlier versions of this
        method just returned parts unchanged, still sitting under work_dir
        as the fixed, unidentified name OUTPUT_NAME. That had two problems,
        not one: Task 20 finding 2's unidentified-filename issue, the same
        as OsmSource's and OvertureSource's, AND the file never actually
        reached the finished package, since a complete run deletes work_dir
        (see run_survey). Copying rather than moving, because parts may
        still be read again on a subsequent run's resume/skip check.
        """
        if not parts:
            return []
        output = out_dir / f"{stem}.tif"
        atomic_write_bytes(output, parts[0].read_bytes())
        return [output]

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root file merge() could ever write for this stem. Read by
        package.py's stale-output sweep; see sources/base.py."""
        return [f"{stem}.tif"]
