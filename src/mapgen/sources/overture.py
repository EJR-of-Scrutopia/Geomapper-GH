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
from mapgen.jobs import CancelToken
from mapgen.merge import merge_geojson
from mapgen.procutil import run_hidden
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
        # A coordinator review's Critical 2: `types or DEFAULT_OVERTURE_TYPES`
        # treats an EMPTY list the same as no list at all, because both are
        # falsy. types=[] is not "unspecified", it is a genuine, deliberate
        # "fetch nothing", the exact shape overture_types_for_categories
        # returns for a category selection like ["rail"] or ["boundaries"]
        # that maps to no Overture type at all (see that function's own
        # docstring). Under the old line, every one of those selections
        # silently fetched the full 8-type default instead, undetected by
        # any test because the mutation this review proposed, replacing the
        # line with the one below, left all 561 tests at the time green:
        # nothing exercised configure() with an empty (as opposed to
        # unspecified) type list and then checked what reached self.types.
        # None is the only value this treats as "use the default"; anything
        # else, including [], is taken exactly as given.
        self.types = list(types) if types is not None else list(DEFAULT_OVERTURE_TYPES)
        self.release = release
        self._runner = runner
        self._find = executable_finder

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        units = len(tiles) * len(self.types)
        return Estimate(
            bytes_estimate=BYTES_PER_TILE_TYPE_ESTIMATE * units,
            seconds_estimate=SECONDS_PER_TILE_TYPE_ESTIMATE * units,
        )

    def configure(self, types: Sequence[str]) -> "OvertureSource":
        """Returns a fresh OvertureSource scoped to exactly these types,
        sharing this instance's release/runner/executable_finder, and
        never mutating self.

        This is the fix for the bug Task 20 found and deliberately left
        for this task: register_default_sources() builds ONE OvertureSource
        with the 8-type default and registers it into the process-wide
        registry once; every request, whatever its own --overture-type or
        category selection, was fetching through that same shared
        instance's fixed .types, so the owner downloaded all 8 datasets
        every time regardless of what they asked for. The fix is not to
        mutate the registered instance's .types in place: estimate_survey
        can be polled from a browser while a job using a DIFFERENT
        selection is already running (there is no busy-guard on
        /api/estimate, only on /api/jobs), and mutating shared state that
        an in-flight fetch() loop is actively reading from underneath it
        is exactly the kind of race that would corrupt a running job's
        output for a reason that would be very hard to reproduce. Handing
        back a new, independently-configured instance instead means the
        registered instance available_sources()/GET /api/sources lists
        keeps behaving identically no matter what any single request
        selects, while package.py uses the returned copy for the actual
        estimate/fetch/merge work.
        """
        return OvertureSource(
            types=types, release=self.release, runner=self._runner, executable_finder=self._find
        )

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        paths: list[Path] = []
        for tile in tiles:
            for overture_type in self.types:
                # Checked before each (tile, type) request, the finest
                # unit of paid-for work this source has: a tile is never
                # interrupted mid-type, and since Overture fetches every
                # type for a tile before moving to the next tile, this
                # still guarantees a stop lands within the tile it was
                # asked to stop within, never spilling into a later one.
                if cancel is not None:
                    cancel.raise_if_cancelled()
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

        # run_hidden, not a bare runner call: on Windows this is one console
        # executable per tile per type, and without CREATE_NO_WINDOW each one
        # opens a real window that the owner can close, killing the download
        # inside it. See mapgen.procutil.
        result = run_hidden(command, runner=self._runner)
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

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root-relative file this source could ever produce for this
        stem, across ALL selections, which is why this unions the full
        default type list with the instance's own (possibly narrowed, or
        exotically widened via --overture-type) selection rather than
        reading either alone. Includes the layers/ copies package.py's
        _write_layer_files derives from the merged output, so a stale
        layers/water.geojson is swept together with the stale
        <stem>_water.geojson it was copied from. Read by package.py's
        stale-output sweep; see sources/base.py.
        """
        every_type = sorted(set(DEFAULT_OVERTURE_TYPES) | set(self.types))
        names = [f"{stem}_{overture_type}.geojson" for overture_type in every_type]
        names += [f"layers/{filename}" for filename in LAYER_FILENAMES.values()]
        return names
