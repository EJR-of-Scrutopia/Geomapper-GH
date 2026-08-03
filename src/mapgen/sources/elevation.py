"""Elevation as a LayerSource, via the OpenTopography global DEM API.

Requested for the whole study bbox in one call rather than per tile, because the
API already handles arbitrary extents and stitching tiled DEMs is needless work.

Phase 2 note: NRW LiDAR at 1 m will be a sibling module here, and is a far better
source than COP30 for anywhere in Wales.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote

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


def _redact(text: str, secret: str | None) -> str:
    """Removes every occurrence of secret from text, by content rather
    than by exception type, and case-insensitively, and defensively
    against an encoding that would otherwise defeat the match outright.

    This is the boundary fix for a real leak: API_Key travels as a plain
    query parameter to OpenTopography, so requests' own exception text
    embeds it for essentially every failure shape that can happen before
    a response exists (ConnectionError, ReadTimeout, ...) and several that
    can happen after (ChunkedEncodingError mid-stream), not only the
    HTTPError case a status-code check replaces. Enumerating "safe"
    exception types is a trap: the next type nobody thought to add stays
    unredacted. Scrubbing the secret's own text, regardless of what
    raised or what shape it arrived in, does not have that failure mode.
    Both the raw key and its URL-percent-encoded form are stripped, since
    a key containing characters that need encoding would otherwise survive
    inside a URL-shaped message unredacted.

    Two more properties, past a plain str.replace:

    - Case: str.replace is case-sensitive, and nothing guarantees a key
      always reaches this function in the exact case it was issued in (a
      proxy or gateway can title-case a header, for instance). Matched
      with re.IGNORECASE instead.
    - Encoding: a UTF-16 error page decoded with the UTF-8 codec
      (errors="replace") does not raise, since every byte of ASCII-range
      UTF-16LE text is independently valid UTF-8 on its own; it produces
      the original characters each followed by a stray NUL
      ("s\\x00k\\x00-\\x00..."), which reads as the key to a human, since
      a terminal or a browser renders NUL as invisible, while containing
      no contiguous match for a plain substring search. Checked for
      directly, by stripping NULs from the already-redacted text and
      searching again, rather than by guessing or enumerating source
      encodings. If that second check still finds the secret, the honest
      answer is to withhold the text entirely rather than publish a
      "redacted" string that may still be hiding it in a shape this
      function did not anticipate.
    """
    if not secret:
        return text
    candidates = [secret]
    encoded = quote(secret, safe="")
    if encoded != secret:
        candidates.append(encoded)

    redacted = text
    for candidate in candidates:
        redacted = re.sub(re.escape(candidate), REDACTION_PLACEHOLDER, redacted, flags=re.IGNORECASE)

    stripped = redacted.replace("\x00", "")
    for candidate in candidates:
        if re.search(re.escape(candidate), stripped, flags=re.IGNORECASE):
            return WITHHELD_PLACEHOLDER
    return redacted


def _scrub_exception_chain(exc: BaseException, secret: str | None) -> None:
    """Redacts secret from exc's own args, and from every exception
    chained to it via __cause__ or __context__, in place.

    `raise ... from None` at the call site is what actually stops a
    standard traceback from printing the chain at all, by telling
    Python's own formatting machinery to suppress it regardless of what
    it contains: that is what closes this for every ordinary rendering
    path (an uncaught exception reaching the interpreter's own top level,
    logging.exception, traceback.format_exc). This is the second,
    independent layer underneath it: if anything ever reads __cause__ or
    __context__ directly instead of going through that machinery, or a
    future call site on this path forgets the `from None`, the chained
    exceptions' own text is already clean rather than depending on that
    not happening. Bounded against a cyclical chain (which should not be
    possible in practice) with a seen-set, since this walks the chain in
    a plain loop rather than recursion.
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
        current = current.__cause__ or current.__context__


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
        explicit
        or env.get("OPENTOPOGRAPHY_API_KEY")
        or env.get("OPENTOPO_API_KEY")
        or configured
    )
    if not key:
        raise MissingApiKeyError(
            "Elevation download needs an OpenTopography API key. Set the "
            "OPENTOPOGRAPHY_API_KEY environment variable, or clear the elevation "
            "layer to skip it. Keys are free from portal.opentopography.org."
        )
    return key


class ElevationSource:
    id = "elevation"
    display_name = "Elevation (OpenTopography COP30)"
    licence = "Copernicus DEM, free for any use with attribution"
    attribution = "(c) DLR e.V. 2010-2014, (c) Airbus Defence and Space GmbH"
    requires_api_key = True

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
        try:
            with self.session.get(
                self.url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                stream=True,
                timeout=self.timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    raise ElevationError(f"Failed to download DEM: HTTP {response.status_code}")
                payload = b"".join(chunk for chunk in response.iter_content(1024 * 1024) if chunk)
        except (ElevationError, Cancelled, KeyboardInterrupt):
            # Left exactly as raised. Cancelled is this project's own
            # cooperative-cancellation signal and KeyboardInterrupt is
            # Python's; neither is raised anywhere inside the block above
            # today, but that must stay true because nothing in this
            # method calls anything that raises them, not because the
            # broad except below happens not to catch them. Named here
            # explicitly, so a future change to what this block calls
            # cannot silently start swallowing either one into an
            # ElevationError.
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
            raise ElevationError(
                f"Failed to download DEM: {_redact(str(exc), api_key)}"
            ) from None

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
            raise ElevationError(
                f"OpenTopography did not return a TIFF. Response began: {preview}"
            )

        atomic_write_bytes(output_path, payload)
        progress.emit("tile_done", source=self.id, tile_id="whole-area")
        return [output_path]

    def merge(self, parts: Sequence[Path], out_dir: Path) -> list[Path]:
        return list(parts)
