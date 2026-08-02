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

import requests

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


def resolve_api_key(
    explicit: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    env = os.environ if environ is None else environ
    key = explicit or env.get("OPENTOPOGRAPHY_API_KEY") or env.get("OPENTOPO_API_KEY")
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

        api_key = resolve_api_key(self._api_key, self._environ)
        params = {
            "demtype": self.demtype,
            "south": f"{bbox.south:.7f}",
            "north": f"{bbox.north:.7f}",
            "west": f"{bbox.west:.7f}",
            "east": f"{bbox.east:.7f}",
            "outputFormat": "GTiff",
            "API_Key": api_key,
        }

        try:
            with self.session.get(
                self.url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                stream=True,
                timeout=self.timeout_seconds,
            ) as response:
                response.raise_for_status()
                payload = b"".join(chunk for chunk in response.iter_content(1024 * 1024) if chunk)
        except (RuntimeError, requests.exceptions.HTTPError) as e:
            raise ElevationError(f"Failed to download DEM: {e}") from e

        if not is_tiff(payload[:16]):
            preview = payload[:300].decode("utf-8", errors="replace")
            raise ElevationError(
                f"OpenTopography did not return a TIFF. Response began: {preview}"
            )

        atomic_write_bytes(output_path, payload)
        progress.emit("tile_done", source=self.id, tile_id="whole-area")
        return [output_path]

    def merge(self, parts: Sequence[Path], out_dir: Path) -> list[Path]:
        return list(parts)
