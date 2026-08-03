"""Overture Maps as a LayerSource.

Downloads run through the overturemaps CLI, which handles the cloud-hosted
parquet release. Each type is fetched separately per tile, then merged into one
GeoJSON per type.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from mapgen.fsutil import ensure_dir
from mapgen.geo import BBox, Tile
from mapgen.merge import merge_geojson
from mapgen.sources.base import Estimate, ProgressSink

DEFAULT_OVERTURE_TYPES = [
    "building",
    "place",
    "segment",
    "connector",
    "infrastructure",
    "land_use",
    "land_cover",
    "water",
]

# Overture type to the friendly filename written into the package layers folder.
LAYER_FILENAMES = {
    "water": "water.geojson",
    "land_cover": "vegetation.geojson",
    "land_use": "landuse.geojson",
}

BYTES_PER_TILE_TYPE_ESTIMATE = 900_000
SECONDS_PER_TILE_TYPE_ESTIMATE = 6.0


class OvertureError(RuntimeError):
    """Raised when the overturemaps CLI is missing or fails."""


class OvertureSource:
    id = "overture"
    display_name = "Overture Maps"
    licence = "Overture Maps Foundation, mixed source licences (ODbL and CDLA-Permissive-2.0)"
    attribution = "(c) Overture Maps Foundation"
    requires_api_key = False

    def __init__(
        self,
        types: Sequence[str] | None = None,
        release: str | None = None,
        runner: Callable[..., object] = subprocess.run,
        executable_finder: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.types = list(types or DEFAULT_OVERTURE_TYPES)
        self.release = release
        self._runner = runner
        self._find = executable_finder

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        units = len(tiles) * len(self.types)
        return Estimate(
            bytes_estimate=BYTES_PER_TILE_TYPE_ESTIMATE * units,
            seconds_estimate=SECONDS_PER_TILE_TYPE_ESTIMATE * units,
        )

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
    ) -> list[Path]:
        paths: list[Path] = []
        for tile in tiles:
            for overture_type in self.types:
                output_path = work_dir / overture_type / f"{tile.tile_id}.geojson"
                if output_path.exists() and output_path.stat().st_size > 0:
                    progress.emit(
                        "tile_skipped",
                        source=self.id,
                        tile_id=tile.tile_id,
                        overture_type=overture_type,
                    )
                    paths.append(output_path)
                    continue
                self._download(tile, overture_type, output_path)
                progress.emit(
                    "tile_done",
                    source=self.id,
                    tile_id=tile.tile_id,
                    overture_type=overture_type,
                )
                paths.append(output_path)
        return paths

    def _download(self, tile: Tile, overture_type: str, output_path: Path) -> None:
        executable = self._find("overturemaps")
        if not executable:
            raise OvertureError(
                "Could not find the overturemaps CLI on PATH. Run bootstrap.ps1, "
                "or install it with: pip install overturemaps"
            )

        ensure_dir(output_path.parent)
        # The CLI writes wherever we point it, and a killed process leaves a
        # truncated file that resume would later mistake for a finished tile.
        # Point it at a .part path and rename only once it exits cleanly.
        temp_path = output_path.with_suffix(".geojson.part")
        temp_path.unlink(missing_ok=True)
        command = [
            executable,
            "download",
            f"--bbox={tile.query_bbox.to_query_string()}",
            "-f",
            "geojson",
            "--type",
            overture_type,
            "--output",
            str(temp_path),
        ]
        if self.release:
            command.extend(["--release", self.release])

        result = self._runner(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            temp_path.unlink(missing_ok=True)
            detail = (result.stderr or result.stdout or "").strip()
            raise OvertureError(
                f"overturemaps failed for tile {tile.tile_id}, type {overture_type}: {detail}"
            )

        if not temp_path.exists():
            raise OvertureError(
                f"overturemaps exited cleanly but wrote nothing for tile "
                f"{tile.tile_id}, type {overture_type}."
            )
        temp_path.replace(output_path)

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        by_type: dict[str, list[Path]] = {}
        for part in parts:
            by_type.setdefault(part.parent.name, []).append(part)

        # Named after the package stem plus the type, not a bare
        # "<type>.geojson": Task 20 finding 2 found the same unidentified
        # merged-output problem here as in OsmSource.merge and asked for a
        # consistent fix. package.py's _write_layer_files matches on this
        # exact composed name to copy the three phase 1 layers into layers/.
        outputs: list[Path] = []
        for overture_type, type_parts in sorted(by_type.items()):
            output = out_dir / f"{stem}_{overture_type}.geojson"
            merge_geojson(type_parts, output)
            outputs.append(output)
        return outputs
