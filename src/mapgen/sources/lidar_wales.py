"""Welsh Government 1 m LiDAR as a LayerSource, via the two whole-Wales
Cloud Optimized GeoTIFF mosaics `cog.py` reads by HTTP range request.

Requested for the whole padded extent in one pair of windows, DTM then DSM,
never per tile: the mosaics already answer arbitrary rectangles (`cog.py`),
so tiling the request the way OsmSource tiles Overpass calls would only add
requests for no benefit. `tile_failures` and the progress events still use
`tile_id="whole-area"`, the same convention `elevation.py` established for
its own single whole-area request.

## Why two rasters, one padded extent, and a grid at all

`egrid.py`'s own `PAD_METRES` is reused rather than a value chosen for this
module: the padded rectangle this source fetches has to be at least as
large as the one the egrid step will later sample, or a resumed run that
downloads LiDAR first and builds the grid second finds gaps at its own
padded edge that a 1 m raster covering only the unpadded bbox never had
data for. British National Grid, not WGS84, is the working frame
throughout: the mosaics are EPSG:27700, `to_bng`/`from_bng` (`bng.py`) are
the only bridge to the WGS84 world the rest of mapgen lives in, and every
one of them needs an `Ostn15Grid`, so `fetch()` always ensures one before it
does anything else with the extent.

## What "outside Wales" and "all nodata" mean here, and why they differ

`MOSAIC_BOUNDS` is the mosaic's own header rectangle (E 164,993 to 356,000,
N 164,000 to 397,000; see `cog.py`'s own probe): a padded extent wholly
outside it is refused before either mosaic is even opened, because no
request to either one could ever answer it. A point that OSTN15 itself
cannot place (`BngError`, `to_bng`) is refused the same way and with the
same sentence: a coordinate with no OSTN15 shift certainly has no Welsh
LiDAR either, and the owner does not need two different explanations for
the same practical fact, "this is not on the National Grid at all". Inside
`MOSAIC_BOUNDS`, an extent can still come back entirely nodata (open water,
or a genuine gap in the survey flights, see `cog.py`'s own docstring on
`GDAL_NODATA`); that is a different, later refusal with its own sentence,
because by that point both requests have actually been made and answered.

## Failure classification without a raw exception in hand

`HttpByteSource` (`cog.py`) already classifies every transport and status
failure it meets, through the exact same `classify_transport_failure`/
`classify_status_failure` this module also uses. `CogError.status_code`
(cog.py) is how that classification reaches here structurally: set on
every raise site that actually saw an HTTP status (a 200 answered to a
range request, or any other non-206 status after `HttpByteSource`'s own
retry), left None everywhere else (a short body, a mismatched
Content-Range, a transport exception). `_classify_cog_error` below reads
that attribute directly and calls `classify_status_failure` itself when it
is set, which is correct for every status that function recognises, not
only the ones whose rendered phrase happens to carry the status in a
parenthesis a regex could find; an earlier version of this function tried
exactly that regex and missed `classify_status_failure`'s own >=500 branch
(`"answered HTTP {code}"`, no parentheses, unlike its 429/401/403/else
siblings), silently reporting every real 5xx as unrecognised and
defeating the retry policy `RETRYABLE_FAILURE_KINDS` exists to drive.

A `CogError` with no status (transport-shaped) is matched on its two
fixed phrases (`"did not answer in time"`, `"could not be reached"`)
literally, because they are this project's own closed, tested strings,
pinned by `test_sources_base.py`, not a third party service's wording that
could be reworded without warning. Anything else, including a protocol
violation `HttpByteSource` itself refuses on with no status at all (a
mismatched Content-Range, a short body), comes back `FAILURE_UNKNOWN`
with a FIXED phrase, never the raw `CogError` text: the full, already
URL-safe detail still reaches the owner through the raised exception
itself (`fetch()` re-raises every `CogError` unwrapped), the same
separation of "reason" from "exception message" `elevation.py`'s own
`_record_tile_failures` keeps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import requests

from mapgen.bng import BngError, ensure_ostn15, load_ostn15, to_bng
from mapgen.cog import (
    CogError,
    CogReader,
    FileByteSource,
    HttpByteSource,
    MAX_WINDOW_PIXELS,
    read_full_window,
)
from mapgen.contours import write_contour_files
from mapgen.egrid import PAD_METRES
from mapgen.fsutil import atomic_write_bytes
from mapgen.geo import BBox, Tile, extent_metres
from mapgen.geotiff_write import write_bng_geotiff
from mapgen.jobs import CancelToken
from mapgen.sources.base import (
    FAILURE_NO_OUTPUT,
    FAILURE_TIMEOUT,
    FAILURE_UNKNOWN,
    FAILURE_UNREACHABLE,
    Estimate,
    ProgressSink,
    TileFailure,
    classify_status_failure,
    classify_transport_failure,
)

# The work_dir file names fetch() writes and merge() reads back by name,
# never by position (the ElevationSource.merge lesson: never parts[0]).
DTM_WORK_NAME = "lidar_dtm.tif"
DSM_WORK_NAME = "lidar_dsm.tif"

# Sentences from the brief, verbatim. Two different practical facts (see
# the module docstring), so two different sentences, both fixed strings
# with nothing interpolated into them: neither can carry a URL because
# neither is built from one.
WALES_ONLY_MESSAGE = (
    "The Welsh LiDAR mosaic has no data for this extent. It covers Wales only."
)
EMPTY_EXTENT_MESSAGE = (
    "The Welsh LiDAR mosaic is empty over this extent, which usually means "
    "open water."
)

# --------------------------------------------------------------------------
# Measured constants (Task 3's live probe, 2026-08-05; see
# .superpowers/sdd/2026-08-05-mapgen-phase2-01-wales-lidar/task-3-report.md).
# Thin evidence in the same sense elevation.py's own comment names: one
# machine, one link, one day, two extents (a 500 x 500 m Barry window and a
# 20 x 20 km extent that falls to the 8 m overview level).
# --------------------------------------------------------------------------

# 3.05 bytes/pixel for the DTM and 3.64 for the DSM, both measured over the
# same 500 x 500 m Barry Island window at 1 m (745,892 and 894,151 bytes
# over 250,000 pixels each). This is their average, replacing the plan's
# provisional 2.6 (a plain 4 bytes deflated guess): built-up Barry compresses
# less than a guess assumed, because the terrain is not the flat, highly
# repetitive signal deflate does best on. Falls to 1.75 at the 20 km extent's
# 8 m overview level, so 3.4 is an upper bound at full resolution and
# generous once a large extent forces an overview; an all-nodata extent
# measured 0 bytes for the window, which this constant does not model (see
# estimate()'s own docstring for why that is the direction this estimate is
# allowed to be wrong in).
BYTES_PER_WINDOW_PIXEL = 3.4

# What is being paid for is the padded extent's own two mosaic opens plus
# two window reads, not a transfer: even the largest byte figure this
# module has ever measured is a few megabytes, nothing on this link (see
# BYTES_PER_SECOND_ESTIMATE below). Task 6 (2026-08-05) measured the
# WHOLE of that, live, end to end, over the real 400 x 400 m Barry extent
# also used for this module's own live test: 2.16 s, with OSTN15 already
# cached (so no grid download is folded into this number either); DTM 32
# requests / 1,544,250 bytes, DSM 32 requests / 1,864,315 bytes.
#
# The 1.3 s this replaces was never a measurement of a whole fetch. It
# was Task 3's own component arithmetic: two ~0.15 s mosaic opens plus a
# 500 x 500 m window's 0.50 s (DTM) and 0.42 s (DSM) reads, summed. That
# window was UNPADDED, and fetch() always reads a window padded by
# 2 * PAD_METRES on every side (see the module docstring), which the
# component arithmetic had no way to account for because it was never
# measured against a padded fetch at all. The result under-read a real,
# ordinary survey's whole fetch by nearly half, and an estimate that
# under-reads is worse than one that over-reads: a countdown built on it
# runs out while the download is still going, which reads as a hang (the
# same asymmetry elevation.py's own SECONDS_FLOOR history records).
#
# One machine, one link, one day, ONE live extent: thinner evidence than
# even Task 3's own two-extent probe. 2.2 is 2.16 rounded to two
# significant figures and kept as a floor, not restated as the exact
# figure, since nothing here claims more precision than one measurement
# can support.
SECONDS_FLOOR = 2.2

# From the one large-extent measurement available: the 20 x 20 km extent
# (falls to the 8 m overview level) moved 10,942,384 bytes in 3.24 s, about
# 3.4 MB/s. Never observed to bind for anything the owner is likely to draw
# at site scale, the same as elevation.py's own BYTES_PER_SECOND_ESTIMATE:
# SECONDS_FLOOR dominates until bytes_estimate clears several megabytes.
BYTES_PER_SECOND_ESTIMATE = 3_400_000.0


class LidarWalesError(RuntimeError):
    """Raised when this extent has no Welsh LiDAR to give it.

    Both refusals in fetch() (wholly outside MOSAIC_BOUNDS, or an
    all-nodata pair inside it) raise this with one of the two fixed
    sentences above; a download or read failure raises whatever cog.py or
    bng.py itself raised (CogError, BngError), unwrapped, since neither of
    those ever carries a URL either and wrapping them would only cost the
    caller the more specific type.
    """


def _classify_cog_error(exc: CogError) -> tuple[str, str]:
    """The (kind, phrase) an HttpByteSource's own CogError implies.

    See the module docstring's "Failure classification without a raw
    exception in hand" section. `exc.status_code` is read directly and
    handed to `classify_status_failure` when set, never re-derived from
    `exc`'s own rendered message: a status this module was told about
    structurally cannot go stale the way a pattern matched against that
    message's English could.

    The fallback phrase is fixed, never `str(exc)`: `exc` is still the
    exception fetch() re-raises unwrapped, so the full detail is not lost,
    only kept out of the `reason` string survey.json and the tile_failed
    event carry, matching every other source's own rule that `reason` is
    composed from the fixed vocabulary and nothing read off a caught
    exception's own text.
    """
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        return classify_status_failure(status_code)
    message = str(exc)
    if "did not answer in time" in message:
        return FAILURE_TIMEOUT, "did not answer in time"
    if "could not be reached" in message:
        return FAILURE_UNREACHABLE, "could not be reached"
    return FAILURE_UNKNOWN, "could not be read"


def _padded_bng_extent(bbox: BBox, grid) -> tuple[float, float, float, float]:
    """The survey bbox's two corners projected to BNG, padded on every side.

    Both corners projected and min/maxed, not just (south, west) assumed to
    be the low corner: OSTN15's shift is spatially varying, so a
    "rotated-ish" projection can in principle leave either corner the more
    easterly or northerly of the two. Raises BngError (via to_bng) for a
    corner OSTN15 does not cover, which the caller reads as "no Welsh LiDAR
    here either" (see the module docstring).
    """
    e1, n1 = to_bng(bbox.south, bbox.west, grid)
    e2, n2 = to_bng(bbox.north, bbox.east, grid)
    e_min, e_max = min(e1, e2) - PAD_METRES, max(e1, e2) + PAD_METRES
    n_min, n_max = min(n1, n2) - PAD_METRES, max(n1, n2) + PAD_METRES
    return e_min, n_min, e_max, n_max


def _all_nodata(values) -> bool:
    """True if every sample in a window's values is NaN.

    `any(...)` short-circuits on the first real sample, which is the
    ordinary case (some data present) and costs almost nothing; only a
    genuinely empty window (the case this function exists to detect) pays
    for the full scan.
    """
    return not any(value == value for value in values)


class LidarWalesSource:
    id = "lidar_wales"
    display_name = "LiDAR terrain (Wales, 1 m)"
    licence = "Open Government Licence v3.0"
    attribution = (
        "Contains Welsh Government and Natural Resources Wales information "
        "licensed under the Open Government Licence v3.0"
    )
    requires_api_key = False
    # The two whole-Wales mosaics, probed live 2026-08-05 (see cog.py's own
    # module docstring for the header facts read off them). Verbatim, so a
    # copy of this string anywhere else in the codebase is the one to
    # suspect first if these ever need to change.
    DTM_URL = "https://dmwproductionblob.blob.core.windows.net/cogs/lidar/wales_dtm_32bit_cog.tif"
    DSM_URL = "https://dmwproductionblob.blob.core.windows.net/cogs/lidar/wales_dsm_32bit_cog.tif"
    # The mosaic's own bounds, from its header: E 164993..356000, N 164000..397000.
    MOSAIC_BOUNDS = (164_993.0, 164_000.0, 356_000.0, 397_000.0)

    def __init__(
        self,
        session: object | None = None,
        timeout_seconds: float = 60.0,
        ostn15_cache_dir: Path | None = None,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.timeout_seconds = timeout_seconds
        # None means "the default, ~/.mapgen" (see bng.py's own _cache_path);
        # a test or a caller that wants an isolated cache passes a tmp_path.
        self._ostn15_cache_dir = ostn15_cache_dir
        # Reset at the top of every fetch(); see sources/base.py's own
        # documentation of this optional LayerSource extension. Two rasters,
        # one whole-area request each, so a failure anywhere in fetch() is
        # recorded against every tile the caller handed in, the same
        # reasoning ElevationSource applies for its own single request.
        self.tile_failures: list[TileFailure] = []

    # -- estimate ----------------------------------------------------------

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        """Bytes and seconds for the padded extent, with no network at all.

        Placing the padded extent on the National Grid needs an
        `Ostn15Grid`, and estimate() may run with none cached yet (the
        settings panel can call /api/estimate before a single survey has
        ever run): `load_ostn15()` is cache-only, and when it returns None
        this falls back to an unprojected approximation, `extent_metres`
        (an equirectangular metres-per-degree figure, not BNG) plus the
        same pad. That approximation is close enough for an estimate:
        South Wales sits well clear of the poles and this tool's own
        extents are small enough that the equirectangular approximation
        and the true BNG area agree to a few percent, which is inside the
        "thin evidence" band the byte and second constants already carry.

        `pixels = min(padded_area_m2, MAX_WINDOW_PIXELS)` per raster is the
        shape the brief sanctions explicitly: it does not model
        read_window's own overview fallback (Task 6 has no reason to
        duplicate CogReader's level-selection arithmetic just to estimate),
        so a padded extent large enough to fall to an overview level is
        systematically OVERESTIMATED here, in the safe direction, never
        understated. BYTES_PER_WINDOW_PIXEL and BYTES_PER_SECOND_ESTIMATE
        are both documented above with the measurement and the thinness of
        the evidence behind them; Task 9 refits `SECONDS_FLOOR` from the
        live test.

        Honest about its own scope and nothing past it: this prices
        `fetch()` alone, and `merge()`'s own contour generation, which
        Task 6's live test measured at a further 9.3 s and up to 8.8 MB
        per contour file on the very same extent, is not in this number
        at all, so the owner's actual wait for a `lidar_wales` package is
        longer than whatever this method reports.
        """
        grid = load_ostn15(cache_dir=self._ostn15_cache_dir)
        padded_area_m2 = None
        if grid is not None:
            try:
                e_min, n_min, e_max, n_max = _padded_bng_extent(bbox, grid)
            except BngError:
                padded_area_m2 = None
            else:
                padded_area_m2 = (e_max - e_min) * (n_max - n_min)
        if padded_area_m2 is None:
            width_m, height_m = extent_metres(bbox)
            padded_area_m2 = (width_m + 2.0 * PAD_METRES) * (height_m + 2.0 * PAD_METRES)

        pixels_per_raster = min(padded_area_m2, float(MAX_WINDOW_PIXELS))
        bytes_estimate = int(pixels_per_raster * 2.0 * BYTES_PER_WINDOW_PIXEL)
        seconds_estimate = max(bytes_estimate / BYTES_PER_SECOND_ESTIMATE, SECONDS_FLOOR)
        return Estimate(bytes_estimate=bytes_estimate, seconds_estimate=seconds_estimate)

    # -- fetch ---------------------------------------------------------------

    def _record_tile_failures(self, tiles: Sequence[Tile], kind: str, reason: str) -> None:
        """Record why this fetch() could not deliver, once per tile.

        Matches ElevationSource._record_tile_failures exactly, including
        the reasoning: a request that never returns is a request that
        never returned for every tile equally, and `reason` is always
        composed from the fixed vocabulary plus, at most, an HTTP status,
        never from a URL.
        """
        self.tile_failures = [
            TileFailure(source=self.id, tile_id=tile.tile_id, kind=kind, reason=reason)
            for tile in tiles
        ]

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        self.tile_failures = []
        if cancel is not None:
            cancel.raise_if_cancelled()

        dtm_path = work_dir / DTM_WORK_NAME
        dsm_path = work_dir / DSM_WORK_NAME
        if (
            dtm_path.exists() and dtm_path.stat().st_size > 0
            and dsm_path.exists() and dsm_path.stat().st_size > 0
        ):
            progress.emit("tile_skipped", source=self.id, tile_id="whole-area")
            return [dtm_path, dsm_path]

        try:
            grid = ensure_ostn15(cache_dir=self._ostn15_cache_dir, session=self.session)
        except requests.RequestException as exc:
            # bng.py's own _download_and_parse currently wraps every
            # requests.RequestException into a BngError before it can
            # reach here (see the except clause below); this branch is
            # kept for the same reason elevation.py enumerates transport
            # exception types rather than assuming its own upstream never
            # changes: a future bng.py that let one through must still be
            # classified correctly rather than falling into a bare `raise`.
            kind, phrase = classify_transport_failure(exc)
            self._record_tile_failures(
                tiles, kind,
                f"Failed to download the OSTN15 shift grid needed to place "
                f"this extent on the National Grid: {phrase}.",
            )
            raise
        except BngError:
            # "acceptable if the sentence stays plain" per the brief: every
            # BngError raise site on this path (a network failure, a
            # corrupt zip, a missing data file member) is a download
            # problem from this call's point of view, and none of them
            # carries a URL, so one fixed, plain sentence covers all of
            # them without needing to distinguish which.
            self._record_tile_failures(
                tiles, FAILURE_UNREACHABLE,
                "Failed to download the OSTN15 shift grid needed to place "
                "this extent on the National Grid.",
            )
            raise

        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            e_min, n_min, e_max, n_max = _padded_bng_extent(bbox, grid)
        except BngError:
            # A corner OSTN15 cannot place is, for this source's purposes,
            # the same fact as a corner outside the mosaic: see the module
            # docstring's "What outside Wales and all nodata mean" section.
            self._record_tile_failures(tiles, FAILURE_NO_OUTPUT, WALES_ONLY_MESSAGE)
            raise LidarWalesError(WALES_ONLY_MESSAGE) from None

        mosaic_e_min, mosaic_n_min, mosaic_e_max, mosaic_n_max = self.MOSAIC_BOUNDS
        if (
            e_max <= mosaic_e_min or e_min >= mosaic_e_max
            or n_max <= mosaic_n_min or n_min >= mosaic_n_max
        ):
            self._record_tile_failures(tiles, FAILURE_NO_OUTPUT, WALES_ONLY_MESSAGE)
            raise LidarWalesError(WALES_ONLY_MESSAGE)

        try:
            dtm_reader = CogReader.open(
                HttpByteSource(self.DTM_URL, session=self.session, timeout_seconds=self.timeout_seconds)
            )
            dtm_window = dtm_reader.read_window(e_min, n_min, e_max, n_max)
        except CogError as exc:
            kind, phrase = _classify_cog_error(exc)
            self._record_tile_failures(
                tiles, kind, f"Failed to download Welsh LiDAR: {phrase}."
            )
            raise

        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            dsm_reader = CogReader.open(
                HttpByteSource(self.DSM_URL, session=self.session, timeout_seconds=self.timeout_seconds)
            )
            dsm_window = dsm_reader.read_window(e_min, n_min, e_max, n_max)
        except CogError as exc:
            kind, phrase = _classify_cog_error(exc)
            self._record_tile_failures(
                tiles, kind, f"Failed to download Welsh LiDAR: {phrase}."
            )
            raise

        if _all_nodata(dtm_window.values) and _all_nodata(dsm_window.values):
            self._record_tile_failures(tiles, FAILURE_NO_OUTPUT, EMPTY_EXTENT_MESSAGE)
            raise LidarWalesError(EMPTY_EXTENT_MESSAGE)

        write_bng_geotiff(dtm_path, dtm_window)
        write_bng_geotiff(dsm_path, dsm_window)
        progress.emit("tile_done", source=self.id, tile_id="whole-area")
        return [dtm_path, dsm_path]

    # -- merge ---------------------------------------------------------------

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Copies the two work rasters into the package and generates
        contour files from the packaged DTM.

        Picks its own two files out of `parts` BY NAME (`DTM_WORK_NAME`,
        `DSM_WORK_NAME`), never by position: the `ElevationSource.merge`
        lesson (Task 28's finding) is that `parts[0]` takes whichever a
        resumed run's directory listing happens to sort first, which is
        not necessarily the file this call actually wants.

        Missing work files mean returning only what exists, and unlinking
        the stale package copy of what is missing, mirroring
        `ElevationSource.merge`'s own I8 discipline (read its docstring):
        package.py's stale-output sweep only ever runs on a COMPLETE run,
        so a run that ends short with this source's fetch having failed,
        beside an older attempt's raster still sitting in the package
        root, would otherwise leave that stale file behind, unlisted in a
        survey.json that never mentions it.

        Contour generation needs both the packaged DTM (to read its window
        back) and a cached `Ostn15Grid` (to unproject vertices); fetch()
        already ensured one, so `load_ostn15` here is cache-only and
        should always hit. "Should" is not "will": a cache directory the
        owner deleted between fetch and merge, or a merge invoked directly
        in a test without a prior fetch, both leave it empty, and a
        package without contours beats a crash in merge over something
        the package's own rasters do not depend on. Either way, any stale
        contour files from an earlier run at this stem are unlinked, for
        the same I8 reason as the rasters above.
        """
        out_dir = Path(out_dir)
        written: list[Path] = []

        dtm_part = next((part for part in parts if part.name == DTM_WORK_NAME), None)
        dsm_part = next((part for part in parts if part.name == DSM_WORK_NAME), None)

        dtm_output = out_dir / f"{stem}_lidar_dtm.tif"
        if dtm_part is not None:
            atomic_write_bytes(dtm_output, dtm_part.read_bytes())
            written.append(dtm_output)
        else:
            dtm_output.unlink(missing_ok=True)

        dsm_output = out_dir / f"{stem}_lidar_dsm.tif"
        if dsm_part is not None:
            atomic_write_bytes(dsm_output, dsm_part.read_bytes())
            written.append(dsm_output)
        else:
            dsm_output.unlink(missing_ok=True)

        contour_names = [
            f"{stem}_contours_{suffix}.geojson" for suffix in ("5m", "1m", "0.5m", "0.25m")
        ]
        grid = load_ostn15(cache_dir=self._ostn15_cache_dir) if dtm_part is not None else None
        if dtm_part is not None and grid is not None:
            reader = CogReader.open(FileByteSource(dtm_part))
            window = read_full_window(reader)
            written.extend(write_contour_files(window, out_dir, stem, grid))
        else:
            for name in contour_names:
                (out_dir / name).unlink(missing_ok=True)

        return written

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root file merge() could ever write for this stem. Read by
        package.py's stale-output sweep; see sources/base.py."""
        return [
            f"{stem}_lidar_dtm.tif",
            f"{stem}_lidar_dsm.tif",
            f"{stem}_contours_5m.geojson",
            f"{stem}_contours_1m.geojson",
            f"{stem}_contours_0.5m.geojson",
            f"{stem}_contours_0.25m.geojson",
        ]
