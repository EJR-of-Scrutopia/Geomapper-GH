"""Overture Maps as a LayerSource.

Downloads run through the overturemaps CLI, which handles the cloud-hosted
parquet release. Each type is fetched once, over the whole request bbox, and
merged into one GeoJSON per type.

Not tiled, unlike OsmSource (Task 23). OSM is tiled because the OSM map API
has a hard 50,000-node cap per request, and the whole node-cap retry ladder
in package.py exists to service that cap. Overture is a bbox-filtered read of
cloud-hosted parquet with no equivalent cap; it only ever inherited OSM's
constraint because it was added alongside it. Measured on this machine over
the same extent, same type, 16 tiles at 600 m with 20 m overlap, in two
independent samples: one whole-bbox call took 4.66s and 4.51s, the 16 tiled
calls took 68.78s and 93.38s, and both returned the identical 9,910 unique
features while the tiled version wrote 956,404 extra bytes of overlapping
duplicate data. The counts match exactly because geo.build_tiles clamps every
tile's query_bbox to the parent extent on all four sides, so the tiled union
covers exactly the parent bbox and never more. Per-call cost is dominated by
fixed overhead (process spawn, parquet metadata read, connection setup), not
data volume: a 10 km x 14 km bbox returning 89,722 features and 55 MB still
completed in one call in 43.73s, so a single call scales to real survey
extents comfortably.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from mapgen.fsutil import best_effort_rmtree, ensure_dir
from mapgen.geo import BBox, Tile, extent_metres
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

# Real measurements on this machine against the live release, not a model.
#
# The per-type base comes from a full EIGHT-type run over a 2.08 x 2.00 km
# extent (4.17 sq km), sampled twice back to back: 77.84s and 98.30s in
# total, 23,587,730 bytes, which is 9.73s and 12.29s per type and about
# 2.95 MB per type.
#
# Calibrating "per type" from ONE type is the easy mistake here and an
# expensive one. `building` alone over that same extent takes 4.5s and
# 5.96 MB, so it is roughly 2.4 times cheaper in time and 2 times larger
# in bytes than the average of the eight. The first cut of these constants
# was fitted to `building` measurements and understated a real 8-type run
# by 2.2x, which the 8-type run above is what caught.
#
# The area slope is the weakest number here and is flagged as such. The
# only large-extent measurement available is `building` alone over
# 10 x 14 km (43.73s, 55 MB, 89,722 features), so the slope is derived
# from that one type and scaled to an average type by the ratio measured
# at the small extent. It is a line through two points, one of which had
# to be adjusted to compare like with like.
#
# No more precision than that is claimed, and none would be honest:
# run-to-run variance on the SAME query has been seen at 4.66s against
# 28.48s, and the two 8-type samples above differ from each other by 26%.
# That is wider than any refinement this data could justify. Treat the
# output as "seconds, not minutes" or "minutes, not hours", never as a
# countdown.
BASE_SECONDS_PER_TYPE = 9.8
SECONDS_PER_TYPE_PER_SQ_KM = 0.28
BASE_BYTES_PER_TYPE = 2_230_000
BYTES_PER_TYPE_PER_SQ_KM = 172_000


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
        """One unit per type, not per tile per type (Task 23).

        fetch() makes exactly one overturemaps call per type over the whole
        bbox, so tiles no longer multiplies anything here. It stays in the
        signature because the LayerSource protocol defines it and OsmSource
        genuinely needs it; this source simply has nothing to do with it.

        The shape is a fixed per-type cost plus a mild area term, because
        that is what the two measurements behind BASE_SECONDS_PER_TYPE and
        its three companions actually show: per-call overhead dominates,
        and area matters but only mildly. See those constants for how few
        data points support them and how wide the run-to-run variance is.
        """
        width_m, height_m = extent_metres(bbox)
        area_sq_km = (width_m / 1000.0) * (height_m / 1000.0)
        # Rounded to whole bytes once, per type, and only then multiplied.
        # Rounding the product instead would make the estimate for two
        # types differ from twice the estimate for one by a byte or two,
        # which is meaningless in itself but makes the "one call per type"
        # shape impossible to assert cleanly and invites a future reader to
        # go looking for a scaling subtlety that is not there.
        per_type_bytes = int(BASE_BYTES_PER_TYPE + BYTES_PER_TYPE_PER_SQ_KM * area_sq_km)
        per_type_seconds = BASE_SECONDS_PER_TYPE + SECONDS_PER_TYPE_PER_SQ_KM * area_sq_km
        type_count = len(self.types)
        return Estimate(
            bytes_estimate=per_type_bytes * type_count,
            seconds_estimate=per_type_seconds * type_count,
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
        """One download per type, over the whole request bbox.

        bbox, not each tile's query_bbox: this source is not tiled (see the
        module docstring for the measurements). tiles is still used, for
        progress reporting only.

        Progress events stay per tile AND per type, exactly as they were
        when the download itself was per tile and per type. tile_done (or
        tile_skipped on a resume) is emitted once per tile for a type, after
        that type's single whole-extent download lands, which is a truthful
        claim: once the whole extent for a type is on disk, every tile's
        area genuinely does have that type's data.

        Deliberately NOT switched to ElevationSource's tile_id="whole-area"
        convention, which would look like the tidier match for a source
        that no longer tiles. The browser's classifyTiles (web/static/
        app.js) ignores any event whose tile_id is not in the grid it built
        from the plan, so "whole-area" would be silently dropped and an
        Overture-only run, which the owner can and does select, would show
        a dead grid from the first second to the last. classifyTiles also
        only ever promotes a tile pending -> active on tile_done/
        tile_skipped, and settles active -> done only once every selected
        source has emitted source_done, so emitting per tile per type does
        not make a tile look finished after the first of eight types.
        """
        # Debris from the superseded per-tile layout, swept once, up front,
        # before anything is downloaded or read (Task 23). Not migration:
        # nothing here reuses those files, and the fresh whole-extent
        # download below covers every one of them. They are removed because
        # leaving them in place actively corrupts this package. work_dir is
        # fingerprinted on the type selection (naming.tiling_fingerprint),
        # so a part-downloaded package from before this change resumes into
        # this same directory with <type>/<tile_id>.geojson files still
        # sitting in it, and package.py's own _existing_output_files hands
        # every file it finds under work_dir straight to merge() and to
        # _record_tile_outcomes. merge() would then read each stale tile id
        # as though it were a TYPE and write a <stem>_r00_c00.geojson into
        # the package root beside the real layers, and _record_tile_outcomes
        # would see a tile-stamped directory again and mark every tile the
        # old run never reached FAILED, leaving a package that is in fact
        # complete permanently reporting complete: false and painting a red
        # tile the owner has no way to clear. Both were reproduced before
        # this sweep was written. The closed list is the safety property, as
        # it is for package.py's own stale-output sweep: only a directory
        # named exactly after a type this source fetches, inside this
        # source's own fingerprinted scratch tree, is ever removed.
        self._remove_superseded_tile_layout(work_dir)

        paths: list[Path] = []
        for overture_type in self.types:
            # Checked before each type's download, which is now the finest
            # unit of paid-for work this source has. Coarser than the old
            # (tile, type) checkpoint in name only: a stop now lands within
            # one download instead of within one of sixteen downloads of
            # the same data, so it arrives sooner in wall-clock terms, not
            # later. An in-flight download is still always allowed to
            # finish and be kept, per the LayerSource cancel convention.
            if cancel is not None:
                cancel.raise_if_cancelled()
            output_path = work_dir / f"{overture_type}.geojson"
            if output_path.exists() and output_path.stat().st_size > 0:
                event = "tile_skipped"
            else:
                self._download(bbox, overture_type, output_path)
                event = "tile_done"
            for tile in tiles:
                progress.emit(
                    event,
                    source=self.id,
                    tile_id=tile.tile_id,
                    overture_type=overture_type,
                )
            paths.append(output_path)
        return paths

    def _remove_superseded_tile_layout(self, work_dir: Path) -> None:
        """Remove any <work_dir>/<type>/ directory the per-tile layout left.

        See the comment at the call site in fetch() for why this exists and
        why it is a defect fix rather than a migration.
        """
        for overture_type in self.types:
            legacy_dir = work_dir / overture_type
            if legacy_dir.is_dir():
                best_effort_rmtree(legacy_dir)

    def _download(self, bbox: BBox, overture_type: str, output_path: Path) -> None:
        executable = self._find("overturemaps")
        if not executable:
            raise OvertureError(
                "Could not find the overturemaps CLI on PATH. Run bootstrap.ps1, "
                "or install it with: pip install overturemaps"
            )

        ensure_dir(output_path.parent)
        # The CLI writes wherever we point it, and a killed process leaves a
        # truncated file that resume would later mistake for a finished
        # download. Point it at a .part path and rename only once it exits
        # cleanly. Untouched by Task 23: one whole-extent file is a much
        # bigger thing to half-write than one tile was, so the risk this
        # guards against is if anything larger now, not smaller.
        temp_path = output_path.with_suffix(".geojson.part")
        temp_path.unlink(missing_ok=True)
        command = [
            executable,
            "download",
            f"--bbox={bbox.to_query_string()}",
            "-f",
            "geojson",
            "--type",
            overture_type,
            "--output",
            str(temp_path),
        ]
        if self.release:
            command.extend(["--release", self.release])

        # run_hidden, not a bare runner call: on Windows this is a console
        # executable, and without CREATE_NO_WINDOW it opens a real window
        # that the owner can close, killing the download inside it. See
        # mapgen.procutil. Far fewer windows than before Task 23 (one per
        # type rather than one per tile per type), which is a reason the
        # owner meets this less often, not a reason to stop hiding them.
        result = run_hidden(command, runner=self._runner)
        if result.returncode != 0:
            temp_path.unlink(missing_ok=True)
            detail = (result.stderr or result.stdout or "").strip()
            raise OvertureError(
                f"overturemaps failed for type {overture_type}: {detail}"
            )

        if not temp_path.exists():
            raise OvertureError(
                f"overturemaps exited cleanly but wrote nothing for type "
                f"{overture_type}."
            )
        temp_path.replace(output_path)

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        # Grouped by the file's own stem, which IS the type now that fetch()
        # writes one flat <type>.geojson per type (Task 23). It used to be
        # part.parent.name, because the type was a directory holding one file
        # per tile; that directory no longer exists, so that key would group
        # every part under the single work directory's name and merge all
        # eight types into one output. Still a grouping rather than a plain
        # one-part-per-type walk, because merge() is handed whatever
        # package.py's _existing_output_files found on disk, and a source
        # that later regains a second file per type should not need this
        # rewritten again.
        by_type: dict[str, list[Path]] = {}
        for part in parts:
            by_type.setdefault(part.stem, []).append(part)

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
