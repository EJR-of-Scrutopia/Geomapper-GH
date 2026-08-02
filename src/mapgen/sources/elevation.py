"""Elevation as a LayerSource, via the OpenTopography global DEM API.

Requested for the whole study bbox in one call rather than per tile, because the
API already handles arbitrary extents and stitching tiled DEMs is needless work.

Phase 2 note: NRW LiDAR at 1 m will be a sibling module here, and is a far better
source than COP30 for anywhere in Wales.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote

import requests

from mapgen.config import load_config
from mapgen.fsutil import atomic_write_bytes
from mapgen.geo import BBox, Tile, extent_metres
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


def _redact(text: str, secret: str | None) -> str:
    """Removes every occurrence of secret from text, by content rather than
    by exception type.

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
    """
    if not secret:
        return text
    redacted = text.replace(secret, "[REDACTED]")
    encoded = quote(secret, safe="")
    if encoded != secret:
        redacted = redacted.replace(encoded, "[REDACTED]")
    return redacted


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
        except ElevationError:
            raise
        except Exception as exc:
            # Deliberately broad, and deliberately not a list of specific
            # requests exception classes: see _redact's docstring for why
            # enumerating types is the trap this replaces. Whatever exc
            # is, whatever it says, the key is stripped from its text
            # before any of it is allowed into this source's own
            # exception, which is the only thing run_survey and
            # JobManager ever see.
            raise ElevationError(
                f"Failed to download DEM: {_redact(str(exc), api_key)}"
            ) from exc

        if not is_tiff(payload[:16]):
            preview = _redact(payload[:300].decode("utf-8", errors="replace"), api_key)
            raise ElevationError(
                f"OpenTopography did not return a TIFF. Response began: {preview}"
            )

        atomic_write_bytes(output_path, payload)
        progress.emit("tile_done", source=self.id, tile_id="whole-area")
        return [output_path]

    def merge(self, parts: Sequence[Path], out_dir: Path) -> list[Path]:
        return list(parts)
