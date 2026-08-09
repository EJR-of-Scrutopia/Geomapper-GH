"""LidarCardiffSource: NRW's 2011 historic 25 cm LiDAR archive over ten
quarter-tiles in Creigiau and Pentyrch, north-west Cardiff, as an opt-in
LayerSource. `covers`, `tier`, `detail`, `estimate`, `routing_note` and
`cache_dir` are the source's disk-and-arithmetic half, every one of them
pure arithmetic and, at most, a cache-only OSTN15 read or a directory
listing. `fetch()` is the network half: it ensures the two archive zips
sit in `cache_dir()`, downloaded and verified once each, and hands their
paths back for `merge()` (below) to unpack the ESRI ASCII grid members
inside them (see probe-report.md's own "Zip members are ESRI ASCII
grids" section).

## Location honesty ruling

The phase 2b spec named this source's coverage "part of central
Cardiff". The plan-time probe (probe-report.md, 2026-08-08, live against
the NRW WFS catalogue) disproved that: the ten tiles this module actually
serves sit in the Creigiau and Pentyrch corner of north-west Cardiff,
about 2.5 sq km, nowhere near the civic centre "central Cardiff" implies.
Task zero had the location right; the spec's own wording drifted from
it. `display_name` below states the true place rather than repeating the
spec's premise, the same choice `sources/elevation.py`'s own module
docstring makes about which DEM model is actually fetched: state the
measured fact, not the plan's working assumption, once the two have come
apart.

## Terrain only, deliberately narrower than lidar_wales

`lidar_wales.py` (this module's closest sibling in shape) serves terrain,
contours and building heights from the same DTM/DSM raster pair; this
source serves terrain alone (`_TIERS = {"terrain": 0}`, nothing for
"contours" or "heights"). The spec keeps those two categories on
whichever later, higher-resolution Cardiff-area flight covers them: one
2011 archive's own derived contours sitting inside a package alongside a
different, newer flight's building heights would understate the vintage
of half of what the owner reads as one package. `FLOWN` ("flown 23 March
2011", the probed date, not an assumed one) appears in every user-facing
string this module writes for exactly that reason.

## The 500 m lattice, and why "full" is exact rather than approximate

The NRW catalogue's own `gb_ng` quarter-tile code (probe-report.md) tiles
the National Grid into 500 m cells at fixed multiples of 500 from grid
zero; every one of `COVERAGE_TILES`'s ten envelopes already sits on that
alignment (310500 = 621 * 500, and so on for every other edge). `covers()`
below compares the padded extent's OWN touched lattice cells against the
ten, cell for cell, rather than testing against the loose bounding
rectangle of the union: the covered footprint is not a solid rectangle
(it is a 3-by-3 block of nine cells plus one further cell, ST1277SW,
hanging off the east side of the middle row), and a bounding-rectangle
test would silently call the two missing corners of that rectangle
"full" when they are not covered at all. `_window_pixels` below, by
contrast, DOES use the loose bounding rectangle (`_ENVELOPE`), on
purpose: it prices the one HTTP-shaped question ("how big a window would
the merge actually build"), not the coverage question, and the tile
layout's own concave corners have no effect on how large that window is.

## No network except fetch()

`covers`, `tier`, `detail`, `estimate`, `routing_note` and `cache_dir`
never import `requests`, never call `mapgen.bng.ensure_ostn15`, and never
open a socket: `mapgen.bng.best_effort_padded_bng_extent` and
`mapgen.bng.load_ostn15` are both cache-only, the same guarantee
`lidar_wales.py`'s and `os_uprn.py`'s own `covers()`/`estimate()` carry,
so a settings panel can call `/api/estimate` with this source selected
before a single byte of either zip has ever been fetched. `fetch()` is
the one exception, and the whole reason `__init__` now takes a `session`:
mirrors `lidar_wales.py`'s own constructor exactly, a deliberate seam the
Task 2 review confirmed was left open for this task rather than an
oversight.

## Why zipfile, not the ranged ZipReader

`os_downloads.ZipReader` refuses these 2011-era zips (central directory
signature mismatch, probed 2026-08-08); stdlib zipfile reads them, and at
45 MB whole-download is the right shape anyway. `fetch()` therefore never
imports `os_downloads` at all: it streams each zip whole to a temp file
beside its own cache path, verifies the transfer's length against
`DSM_ZIP_BYTES`/`DTM_ZIP_BYTES`, and only then opens it with
`zipfile.ZipFile` to confirm `namelist()` is non-empty, so a truncated or
otherwise corrupt download is caught here, at fetch time, rather than
surfacing later inside `merge()`, which expects a readable archive and
would otherwise be handed a lie the byte count alone could not tell.

## merge(): no bbox in its own signature, so fetch() remembers it

`LayerSource.merge(parts, out_dir, stem)` (`sources/base.py`) carries no
bbox parameter; every other method on this class that needs one takes it
directly. `fetch()` stashes its own `bbox` argument on `self._bbox` for
exactly this reason, the same way `self._ostn15_cache_dir` is
constructor state `merge()` already reads for `_window_pixels`'s own
`best_effort_padded_bng_extent` call: package.py's `run_survey` always
calls `fetch()` and then `merge()` on the identical registered source
instance for one survey (never a fresh one in between), so instance
state is the one route available for a fact `merge()` needs but its own
protocol signature has no room for.

## Task 5's bridge: how merge() gets real parts from package.py's real pipeline

`package.py`'s `run_survey` never hands `merge()` `fetch()`'s own return
value: it builds `merge()`'s `parts` argument from a LISTING of whatever
real files `fetch()` left in the source's own `work_dir`
(`_existing_output_files`), because a resumed run's merge has to see
every file a PREVIOUS fetch() call on this same tiling ever produced,
not only the ones the most recent call happened to return. Every other
source in this package satisfies that listing by writing its own real
survey-scoped output straight into `work_dir` (`lidar_wales.py`'s two
rasters, `os_uprn.py`'s bbox-filtered CSV); this source cannot, because
its two zips are a shared, NATIONAL cache (`cache_dir()`), not
survey-scoped work product, and copying 84 MB into every survey's own
`work_dir` merely to satisfy that listing would undo the entire point of
caching them once.

`fetch()` therefore writes two tiny pointer files into `work_dir`
instead, `<DSM_ZIP_NAME>.cache_pointer` and `<DTM_ZIP_NAME>.cache_pointer`,
each holding nothing but the matching zip's own absolute path in
`cache_dir()` as plain text (never empty, so `mapgen.merge.
assert_inputs_present`'s own empty-file check never mistakes one for a
missing part). `merge()` resolves a pointer back to the real path it
names via `_resolve_cache_pointer` ONLY when `parts` does not already
hold the zip directly under its own real name (`DSM_ZIP_NAME`/
`DTM_ZIP_NAME`): that direct, by-name lookup is tried first and is left
completely unchanged, which is what keeps every Task 4 test, and any
future direct caller that already has the two zip paths in hand, working
with no knowledge that the pointer convention exists at all. Chosen over
copying, hardlinking or symlinking the whole zip into `work_dir`: a
pointer costs nothing to write or read and crosses the fetch()/merge()
process boundary the same way `os_uprn.py`'s own work part does
(`fetch()` and `merge()` may run in different processes), without
duplicating a single byte of the 84 MB the national cache already holds.

## The budget gate lives in fetch(), first, and again in merge()

The over-budget refusal (`_budget_refusal_reason`) fires in TWO places.
`fetch()` checks it first, before either zip is downloaded: this is the
gate that actually reaches the owner, because package.py's `run_survey`
wraps `fetch()` in the deferred-failure/retry machinery
(`tile_failures`, the same account `_record_tile_failures` already
populates for a download failure) but does not currently wrap `merge()`
in anything at all, so an exception raised only from `merge()` would
propagate out of `run_survey` uncaught rather than reaching the owner
through the ordinary source-failure event. `merge()` keeps its own copy
of the same gate regardless, as a second line of defence for a call
reached by some other route (a resumed run, a future caller); it must
never be the ONLY gate, but removing it would leave a `merge()` invoked
without a preceding `fetch()`'s own refusal building the full window's
worth of NaN for nothing.

Both gates use `_window_pixels`'s own, unsnapped figure, the exact
number `detail()` already previewed, rather than the pixel count
`merge()`'s own snapped window ends up with: snapping outward can only
grow a window by under one pixel per edge, and gating on the snapped
count instead would let the refusal and the preview disagree by that
same sliver, for no benefit.

## One failed member fails the whole merge

A member whose header will not parse, or whose full value grid will not
(`asc_grid.AscGridError` either way), takes the WHOLE merge down with it,
kind `"parse"`, regardless of whether that member's own bounds would
otherwise have placed it inside or outside the window: a header this
module cannot trust is not one it can use to decide "safe to skip" from,
and skipping a member on the strength of a header call that itself just
failed would be a silent hole dressed up as an honest gap. Neither
archive raster is written until BOTH have been fully assembled without
incident, so a corrupt member in either one leaves the package holding
neither `<stem>_lidar25_dsm.tif` nor `<stem>_lidar25_dtm.tif`, never one
without the other.

The same "never one without the other" claim also has to survive a
write failure between the two `write_bng_geotiff` calls, not only a
parse failure before them (a review finding: disk-full or an antivirus
lock partway through the second write used to leave the first file
sitting on disk, complete, with no sibling). Both rasters are therefore
written to temporary names in `out_dir` first and renamed into their
real, spec-pinned names only once BOTH writes have succeeded; a failure
on the second write deletes the first write's own temp file before
re-raising, the same temp-then-rename shape `fetch()`'s own `_ensure_zip`
already uses for the identical reason.
"""

from __future__ import annotations

import math
import uuid
import zipfile
from array import array
from pathlib import Path
from typing import Sequence

import requests

from mapgen.asc_grid import AscGridError, parse_asc, parse_asc_header
from mapgen.bng import best_effort_padded_bng_extent
from mapgen.cog import MAX_WINDOW_PIXELS, USER_AGENT, BngWindow
from mapgen.config import CONFIG_PATH
from mapgen.egrid import PAD_METRES
from mapgen.fsutil import atomic_write_text, ensure_dir
from mapgen.geo import BBox, Tile
from mapgen.geotiff_write import write_bng_geotiff
from mapgen.jobs import CancelToken
from mapgen.sources.base import (
    FAILURE_NODE_CAP,
    FAILURE_UNKNOWN,
    FAILURE_UNREACHABLE,
    Estimate,
    ProgressSink,
    TileFailure,
)

# The archive's own flight date (probe-report.md, live 2026-08-08),
# probed rather than assumed: appears verbatim in every user-facing
# string this module writes, matching the module docstring's "Terrain
# only" section.
FLOWN = "flown 23 March 2011"

# Task 5 of the phase 2b-D plan: the vintage clause survey.json's
# provenance entry carries for every package that selected this source,
# verbatim from the plan's own Global Constraints. Built from FLOWN
# rather than a second, independently typed date, for the identical
# reason DSM_ZIP_NAME/DTM_ZIP_NAME below are derived from their own URLs
# rather than retyped: two spellings of the same fact must never have a
# chance to drift apart. Read by package.py's `_source_provenance`
# through the same defensive, optional-attribute convention that
# function already applies to `demtype`, `types` and `routing_note`
# (`sources/base.py`'s own documented LayerSource convention), so a
# future source with its own vintage caveat gets identical treatment for
# free by defining the same attribute.
VINTAGE_NOTE = f"{FLOWN}; buildings and ground changed since"

# The ten quarter-tile envelopes this source serves, verbatim from the
# probe (probe-report.md), each with its own gb_ng tile code in a
# trailing comment. (e_min, n_min, e_max, n_max), British National Grid
# metres. Every edge is a multiple of 500 (the module docstring's "500 m
# lattice" section), which is what makes the lattice comparison in
# covers() exact rather than approximate.
COVERAGE_TILES: tuple[tuple[float, float, float, float], ...] = (
    (310500.0, 176500.0, 311000.0, 177000.0),  # ST1076NE
    (310500.0, 177000.0, 311000.0, 177500.0),  # ST1077SE
    (310500.0, 177500.0, 311000.0, 178000.0),  # ST1077NE
    (311000.0, 176500.0, 311500.0, 177000.0),  # ST1176NW
    (311000.0, 177000.0, 311500.0, 177500.0),  # ST1177SW
    (311000.0, 177500.0, 311500.0, 178000.0),  # ST1177NW
    (311500.0, 176500.0, 312000.0, 177000.0),  # ST1176NE
    (311500.0, 177000.0, 312000.0, 177500.0),  # ST1177SE
    (311500.0, 177500.0, 312000.0, 178000.0),  # ST1177NE
    (312000.0, 177000.0, 312500.0, 177500.0),  # ST1277SW
)

# Fast membership test for covers()'s own lattice comparison. A plain set
# rather than anything geometric: every element is already a 500 m
# lattice cell in the exact same (e_min, n_min, e_max, n_max) shape
# _touched_lattice_cells produces, so membership is the whole test.
_COVERAGE_SET = frozenset(COVERAGE_TILES)

# The bounding rectangle of the union of COVERAGE_TILES: 2000 x 1500 m,
# (310500, 176500) to (312500, 178000). NOT the coverage footprint itself
# (see the module docstring: the footprint has two missing corners inside
# this rectangle, ST1277NW-position and the cell south of it), but the
# window a real download over any covered extent would actually have to
# open, which is what _window_pixels prices.
_ENVELOPE = (
    min(tile[0] for tile in COVERAGE_TILES),
    min(tile[1] for tile in COVERAGE_TILES),
    max(tile[2] for tile in COVERAGE_TILES),
    max(tile[3] for tile in COVERAGE_TILES),
)

# The lattice cell size the NRW catalogue's own gb_ng code tiles the
# National Grid at (probe-report.md). Not the same number as PIXEL_METRES
# below by coincidence of naming; one is the coverage grid, the other is
# the raster's own pixel size, and they happen to both come from the same
# survey convention.
_LATTICE_CELL_METRES = 500.0

# The two zips, verbatim (probe-report.md, live 2026-08-08 against
# lle.blob.core.windows.net). Sizes are exact Content-Length answers on
# that date, not rounded: DSM_ZIP_BYTES + DTM_ZIP_BYTES is the one number
# estimate() charges a cold cache.
DSM_ZIP_URL = "https://lle.blob.core.windows.net/lidar/25cm_res_ST17_2011_dsm.zip"
DTM_ZIP_URL = "https://lle.blob.core.windows.net/lidar/25cm_res_ST17_2011_dtm.zip"
DSM_ZIP_BYTES = 45_011_591
DTM_ZIP_BYTES = 38_784_302

# Derived from the URLs above rather than a second, independently typed
# literal: a fetch() that saves under a different name than this reads
# back from would silently look cold forever, and there is no reason for
# the two spellings of the same file name to ever have a chance to drift
# apart.
DSM_ZIP_NAME = DSM_ZIP_URL.rsplit("/", 1)[-1]
DTM_ZIP_NAME = DTM_ZIP_URL.rsplit("/", 1)[-1]

# The work_dir pointer names fetch() writes and merge() resolves back to
# cache_dir() by (see the module docstring's "Task 5's bridge" section).
# Derived from DSM_ZIP_NAME/DTM_ZIP_NAME rather than a separate literal,
# for the same no-second-spelling reason those two are themselves derived
# from the URLs above.
_DSM_CACHE_POINTER_NAME = f"{DSM_ZIP_NAME}.cache_pointer"
_DTM_CACHE_POINTER_NAME = f"{DTM_ZIP_NAME}.cache_pointer"

# The archive's own published pixel size (probe-report.md: "2000x2000
# cells at 0.5m" for the 2012 50cm flight; this 2011 flight is the 25cm
# one the same catalogue lists at half that cell size).
PIXEL_METRES = 0.25

# Task 3's own live cold-run measurement (task-3-report.md): both zips,
# 83,795,893 bytes combined, in about 4.09 s (pytest-reported test
# duration for test_live_fetch_downloads_both_real_zips_into_the_real_cache),
# roughly 20.5 MB/s effective on this machine's link that day. The
# 2,000,000 placeholder this replaces was never checked against that run
# and sat ten times below it, the wrong side of honest for a term that
# prices a real transfer's time: an estimate this far under a measured
# rate makes a cold-cache survey's own countdown look far worse than the
# download will actually be. Following os_open.py's own
# BYTES_PER_SECOND_ESTIMATE rule (a rate genuinely AT OR BELOW the
# slowest clean measurement on record, not the measured figure itself and
# not an average): 15,000,000 sits a genuine ~27% below the 20.5 MB/s
# figure, a wider margin than os_open.py's own ~6.5% because this is a
# single run on a single day (one machine, one link, one day, no repeat
# run), thinner evidence than the three-product corroboration that
# measurement had.
BYTES_PER_SECOND_ESTIMATE = 15_000_000

# Task 6's own live measurement (task-6-report.md): five isolated warm-
# cache fetch() calls against the real ~/.mapgen/lidar_cardiff cache (both
# zips already present, right-sized), a fresh LidarCardiffSource built for
# each trial: 4.4, 4.5, 4.7, 4.9 and 5.0 ms. A warm fetch() here is
# nothing but two Path.stat() calls (see _cache_is_warm's and
# _ensure_zip's own warm branch), never a listing round trip or a shard
# read, so there is no equivalent of os_open.py's own network-overhead
# floor to allow for; the honest floor is a small multiple of the
# slowest observed trial, not a number that was never measured against
# anything. 0.1 s is twenty times the slowest of the five trials, margin
# for a slower stat() call on a different machine or a busier disk on a
# different day, while no longer overstating a near-instant warm check by
# two orders of magnitude the way the previous, unmeasured 3.0 did.
SECONDS_FLOOR = 0.1

# The os_uprn.py-style routing_note: a plain statement of the one-time
# download a cold cache implies, read by package.py's own
# estimate-warnings pass (see os_uprn.py's own `routing_note` docstring
# for the convention this matches).
_ROUTING_NOTE = (
    "LiDAR terrain (Creigiau and Pentyrch): first use downloads two zip "
    "files (about 84 MB total, cached for every later survey)."
)


class LidarCardiffError(RuntimeError):
    """Raised when `fetch()` cannot obtain or verify one of the two
    archive zips, refuses an over-budget extent, or is asked to `merge()`
    without one; or when `merge()` itself cannot turn the two zips into
    the 25 cm rasters.

    `kind` is `"download"` (a transport failure, a downloaded body whose
    length did not match `DSM_ZIP_BYTES`/`DTM_ZIP_BYTES` exactly, or, a
    review finding, a work_dir pointer file (Task 5's bridge;
    `_resolve_cache_pointer`) whose recorded `cache_dir()` target has
    since gone missing, healable the same way any other `"download"` is,
    by running the survey again so `fetch()` re-populates the cache),
    `"parse"` (a right-length body that did not open as a non-empty zip;
    a zip member whose header or values `asc_grid.AscGridError` refused;
    a pointer file whose own recorded text is not a usable path at all;
    or `merge()` missing an input it needs, either the two zip paths or
    `self._bbox`), or `"budget"` (the padded extent's own intersection
    with the coverage envelope needs more pixels at 25 cm than
    `cog.MAX_WINDOW_PIXELS` allows, the same number `detail()` already
    previewed). `"download"` is the only retryable kind
    (`_classify_lidar_cardiff_error`); `"budget"` maps to
    `FAILURE_NODE_CAP` there, the same "the answer is a smaller extent,
    not another identical request" vocabulary entry `sources/base.py`
    documents for OsmSource's own, unrelated failure, since asking again
    never shrinks an extent; `"parse"` falls through to `FAILURE_UNKNOWN`.

    `"budget"` is raised from two places: `fetch()`, BEFORE either zip is
    downloaded (the primary gate: this is what actually reaches
    package.py's ordinary source-failure/tile_failures path, since
    `merge()`'s own exceptions are not currently caught by `run_survey`),
    and `merge()` itself, kept as a second line of defence for a call
    reached by some other route. Both build their message from the same
    `_budget_refusal_reason` so the two can never disagree with each
    other or with `detail()`'s own preview.

    Every message names the zip's own file name (`DSM_ZIP_NAME` /
    `DTM_ZIP_NAME`) or the pixel arithmetic itself, never `DSM_ZIP_URL` /
    `DTM_ZIP_URL` and never `str()` on a caught `requests` exception,
    matching `os_downloads.py`'s own "no-URL rule": a `requests`
    exception's own message embeds the URL it was called with.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def _classify_lidar_cardiff_error(exc: LidarCardiffError) -> str:
    """The shared failure vocabulary's term (`sources/base.py`) for this
    source's own `LidarCardiffError`.

    `"download"` (a transport failure, or a length mismatch) is
    `FAILURE_UNREACHABLE`, retryable: the same bucket `os_uprn.py`'s own
    `_classify_os_open_error` gives `OsOpenError`'s `"download"`/
    `"listing"` kinds, which this mirrors for the identical reason.
    `"budget"` is `FAILURE_NODE_CAP`: the extent needs more pixels than
    the raster budget allows, and retrying the identical request asks
    the identical question of the identical extent, so nothing about a
    second attempt could ever answer differently, the same reasoning
    `osm.py`'s own `NodeCapExceededError` mapping already documents for
    an unrelated too-dense-to-serve failure. `"parse"` (a right-length
    body that will not open as a zip, a member `asc_grid.AscGridError`
    refused, or `merge()` missing a required input) falls through to
    `FAILURE_UNKNOWN`, the same catch-all `os_uprn.py`'s own classifier
    gives every kind it does not special-case.
    """
    if exc.kind == "download":
        return FAILURE_UNREACHABLE
    if exc.kind == "budget":
        return FAILURE_NODE_CAP
    return FAILURE_UNKNOWN


def cache_dir() -> Path:
    """`~/.mapgen/lidar_cardiff`.

    Resolved from `CONFIG_PATH` (`mapgen.config`), the same directory
    `bng.py`'s own `_cache_path` and `os_downloads.py`'s own `cache_root`
    already treat as this tool's cache root, rather than a second,
    independently chosen home. Never creates the directory itself:
    `_cache_is_warm` and `estimate()`/`routing_note()` only ever read it,
    and `LidarCardiffSource.fetch()`'s own `_ensure_zip` is what creates
    it before it writes anything, the same division `bng.py`'s own
    `_cache_path`/`ensure_ostn15` pair keeps.
    """
    return CONFIG_PATH.parent / "lidar_cardiff"


def _cache_is_warm(directory: Path) -> bool:
    """True only when BOTH zips already sit in `directory`, each exactly
    the size `DSM_ZIP_BYTES`/`DTM_ZIP_BYTES` says it should be.

    "Either zip missing, or the wrong size" counts as cold: a resumed run
    that has one of the two but not the other, or a stale partial left by
    an interrupted earlier version of this module, still has to make one
    more request. The exact-size test matches `LidarCardiffSource.
    _ensure_zip`'s own warm check precisely, on purpose (a review finding:
    this function used to accept any non-empty file, which let `estimate()`
    and `routing_note()` call a stale, wrong-size cache warm while
    `fetch()` itself would still, correctly, re-download it), so `estimate()`
    can never disagree with what `fetch()` is actually about to do.
    """
    dsm_path = directory / DSM_ZIP_NAME
    dtm_path = directory / DTM_ZIP_NAME
    return (
        dsm_path.exists() and dsm_path.stat().st_size == DSM_ZIP_BYTES
        and dtm_path.exists() and dtm_path.stat().st_size == DTM_ZIP_BYTES
    )


def _touched_lattice_cells(
    e_min: float, n_min: float, e_max: float, n_max: float
) -> list[tuple[float, float, float, float]]:
    """Every 500 m lattice cell the half-open rectangle
    `[e_min, e_max) x [n_min, n_max)` touches, as
    `(cell_e_min, cell_n_min, cell_e_max, cell_n_max)`.

    The lattice is aligned to plain multiples of 500 from grid zero, the
    same alignment every `COVERAGE_TILES` envelope already sits on (see
    the module docstring's "500 m lattice" section): a rectangle whose
    own edge lands exactly on a 500 m boundary touches only the cell on
    the near side of that edge, never the one starting at the boundary
    itself, which is what `math.ceil(...) - 1` (rather than a plain
    `math.floor` on the exclusive edge) gets right and is the reason
    `covers()`'s own "full" is exact at a shared tile boundary rather
    than one cell too generous.
    """
    i_min = math.floor(e_min / _LATTICE_CELL_METRES)
    i_max = math.ceil(e_max / _LATTICE_CELL_METRES) - 1
    j_min = math.floor(n_min / _LATTICE_CELL_METRES)
    j_max = math.ceil(n_max / _LATTICE_CELL_METRES) - 1
    cells: list[tuple[float, float, float, float]] = []
    for i in range(int(i_min), int(i_max) + 1):
        for j in range(int(j_min), int(j_max) + 1):
            cells.append(
                (
                    i * _LATTICE_CELL_METRES,
                    j * _LATTICE_CELL_METRES,
                    (i + 1) * _LATTICE_CELL_METRES,
                    (j + 1) * _LATTICE_CELL_METRES,
                )
            )
    return cells


def _window_pixels(bbox: BBox, ostn15_cache_dir: Path | None) -> int:
    """The pixel count at `PIXEL_METRES` of the padded extent's own
    intersection with `_ENVELOPE` (the bounding rectangle of the union of
    `COVERAGE_TILES`), zero when the two rectangles do not overlap at
    all.

    Shared between `detail()` and a later task's `merge()`, never
    computed twice: `detail()` previews this exact number and the merge
    refuses on it, so the preview and the refusal can never disagree
    about what a given extent would actually cost. Deliberately the loose
    bounding rectangle, not the ten-tile footprint itself: see the module
    docstring's "500 m lattice" section for why those two are different
    shapes and why this function needs the rectangle, not the footprint.

    Never touches the network: `best_effort_padded_bng_extent` reads at
    most a cached OSTN15 grid, the same guarantee `covers()` and
    `estimate()` both carry.
    """
    e_min, n_min, e_max, n_max = best_effort_padded_bng_extent(
        bbox, PAD_METRES, cache_dir=ostn15_cache_dir
    )
    env_e_min, env_n_min, env_e_max, env_n_max = _ENVELOPE
    width = max(0.0, min(e_max, env_e_max) - max(e_min, env_e_min))
    height = max(0.0, min(n_max, env_n_max) - max(n_min, env_n_min))
    return int((width / PIXEL_METRES) * (height / PIXEL_METRES))


def _budget_refusal_reason(pixels: int) -> str:
    """The plan's own pinned reason string, verbatim, with only the pixel
    count substituted (thousands separated: part of the pinned text, not
    incidental formatting).

    Shared by two gates: `fetch()`'s own, which runs first and refuses
    before either zip is downloaded, and `merge()`'s own, kept as the
    second line of defence for a `merge()` reached by some other route
    (a resumed run, a future caller that skips straight to it). One
    function is what keeps the two gates, and `detail()`'s own preview,
    from ever drifting apart in wording.
    """
    return (
        f"this extent needs {pixels:,} pixels at 25 cm and the raster "
        f"budget is 16,777,216; extents under about 1 x 1 km inside the "
        f"covered block come back at 25 cm"
    )


def _resolve_cache_pointer(parts: Sequence[Path], pointer_name: str) -> Path | None:
    """The real `cache_dir()` path a work_dir pointer file named
    `pointer_name` names, read straight out of its own text content, or
    `None` when `parts` holds no file by that name at all.

    See the module docstring's "Task 5's bridge" section: `fetch()`
    writes `_DSM_CACHE_POINTER_NAME`/`_DTM_CACHE_POINTER_NAME` into
    `work_dir` holding nothing but the matching zip's own absolute path
    as plain text, and `merge()` calls this only for whichever of the two
    zips it did not already find directly, by its own real name, in
    `parts`. Trusting the pointer's own recorded text rather than
    recomputing `cache_dir()` here again is deliberate: the path fetch()
    wrote is the one fact this function needs, and re-deriving it a
    second, independent way would risk the two falling out of step for
    no benefit, the identical reasoning `os_uprn.py`'s own module
    docstring gives for reading OS's published WGS84 columns directly
    rather than re-projecting them.

    A review finding: the pointer's own recorded text is trusted only
    after it is checked here, not handed back unchecked the way an
    earlier version of this function did. Two failure modes are
    validated for and wrapped in a named `LidarCardiffError`, rather than
    left to raise whatever the filesystem or `Path` itself happens to
    throw: without this, a STALE pointer (its own recorded cache target
    gone, the exact cross-process gap this bridge exists to survive: some
    other process cleared `cache_dir()` between a `fetch()` and a later,
    resumed `merge()`) used to surface as a bare `FileNotFoundError` two
    calls later, deep inside `_assemble_window`'s own
    `zipfile.ZipFile(zip_path)` call, and a pointer whose own text is not
    a usable path at all (an embedded NUL byte, the one case this
    project's own filesystem calls refuse outright) used to surface as a
    bare `ValueError`, both un-named and un-kinded unlike every other
    failure this module raises.

    `kind="download"` for a pointer whose named file is genuinely
    missing: the healing action is the same one `"download"` always
    implies elsewhere in this file, running the survey again so a fresh
    `fetch()` re-populates `cache_dir()` and overwrites this pointer with
    a good one; `merge()` alone can never fix this, only a `fetch()` can.
    `kind="parse"` for a pointer whose own recorded text is not a usable
    path at all: this is "the input made no sense", the same bucket
    every other malformed-input raise in this module falls into, checked
    EXPLICITLY for the embedded-NUL case before the path is ever touched,
    because `Path.is_file()` itself answers a NUL-bearing path with a
    plain `False` on this project's own Windows target rather than
    raising (probed directly against this interpreter), which would
    otherwise fold this case silently into `"download"` and lose the
    distinction the brief's own review asked this function to keep. The
    `try`/`except` around `is_file()` below is a second line of defence
    for whatever else a stranger platform or a future Python might raise
    there instead, kept in the same `"parse"` bucket for the identical
    reason.

    Neither message repeats the pointer's own (corruption- or, in
    principle, attacker-controlled) recorded text: both name only the
    pointer's own real file NAME, matching this file's established
    no-URL rule for every other message it raises.
    """
    pointer = next((part for part in parts if part.name == pointer_name), None)
    if pointer is None:
        return None
    text = pointer.read_text(encoding="utf-8").strip()
    if "\x00" in text:
        raise LidarCardiffError(
            f"{pointer.name} does not hold a usable file path. Run the "
            f"survey again so fetch() can rewrite it.",
            kind="parse",
        )
    target = Path(text)
    try:
        found = target.is_file()
    except (OSError, ValueError) as exc:
        raise LidarCardiffError(
            f"{pointer.name} does not hold a usable file path. Run the "
            f"survey again so fetch() can rewrite it.",
            kind="parse",
        ) from exc
    if not found:
        raise LidarCardiffError(
            f"{pointer.name} points to a cache file that is no longer "
            f"there. Run the survey again so fetch() can re-fill the "
            f"cache.",
            kind="download",
        )
    return target


class LidarCardiffSource:
    id = "lidar_cardiff"
    display_name = (
        "LiDAR terrain (Creigiau and Pentyrch, north-west Cardiff, 25 cm, "
        "flown 2011)"
    )
    licence = "Open Government Licence for Public Sector Information (OGL)"
    attribution = (
        "Contains Natural Resources Wales information © Natural "
        "Resources Wales and Database Right. All rights Reserved."
    )
    requires_api_key = False
    # Task 5 of the phase 2b-D plan: read by package.py's
    # `_source_provenance` the same defensive, optional-attribute way as
    # `demtype`/`types`/`routing_note` (see VINTAGE_NOTE's own comment
    # above), so survey.json's provenance entry for this source carries
    # the flight date beside its licence and attribution.
    vintage_note = VINTAGE_NOTE

    def __init__(
        self,
        session: object | None = None,
        timeout_seconds: float = 60.0,
        ostn15_cache_dir: Path | None = None,
    ) -> None:
        # Mirrors LidarWalesSource.__init__ exactly (parameter names,
        # order and defaults): the Task 2 review confirmed this
        # constructor's earlier, session-less shape was a deliberate seam
        # left for this task, not an oversight. session is used only by
        # fetch(); every other method on this class never touches it.
        self.session = session if session is not None else requests.Session()
        self.timeout_seconds = timeout_seconds
        # None means "the default, ~/.mapgen" (bng.py's own _cache_path);
        # a test or a caller that wants an isolated cache passes a
        # tmp_path, matching every other BNG-aware source in this
        # package.
        self._ostn15_cache_dir = ostn15_cache_dir
        # Reset at the top of every fetch(); see sources/base.py's own
        # documentation of this optional LayerSource extension, and the
        # module docstring's own note on why this task's review made this
        # non-optional in practice: without it, package.py's run_survey
        # has no account to defer a failure on, so an ordinary transient
        # download failure here would end the whole survey immediately
        # (SurveyRequest.force defaults to False) rather than letting
        # later-ordered sources still run and the run fail afterward, the
        # same deferred IncompleteSurveyError shape lidar_wales.py and
        # os_uprn.py already get from populating this exact attribute.
        self.tile_failures: list[TileFailure] = []
        # Set at the top of every fetch(); merge() reads it back. See the
        # module docstring's "merge(): no bbox in its own signature"
        # section: LayerSource.merge's own protocol carries no bbox
        # parameter, and this is the constructor-state route merge()
        # takes instead, the same kind of extra state lidar_wales.py's
        # own merge() already reads off self._ostn15_cache_dir for its
        # contour step. None only before the first fetch() call; a
        # merge() invoked without one first (a test doing so directly)
        # sets this itself before calling merge().
        self._bbox: BBox | None = None

    # category -> tier, this source's own row of mapgen.resolver's shared
    # table: terrain only (see the module docstring's "Terrain only"
    # section), at tier 0, above lidar_wales's own tier 1 for the same
    # category: the resolver sorts tiers ascending, so this 2011 25 cm
    # archive is preferred over 1 m Welsh LiDAR wherever both cover the
    # same ground, exactly the resolution-over-recency ordering an
    # architectural survey tool exists to make.
    _TIERS = {"terrain": 0}

    # -- covers / tier (mapgen.resolver) ------------------------------------

    def covers(self, bbox: BBox) -> str:
        """"full" when every 500 m lattice cell the padded extent touches
        is one of the ten `COVERAGE_TILES`, "partial" when at least one
        touched cell is, "none" otherwise.

        Exact, not approximate, at the lattice grain: see the module
        docstring's "500 m lattice" section for why a plain
        bounding-rectangle test over `_ENVELOPE` would be wrong here,
        where a bounding-rectangle test over `MOSAIC_BOUNDS` is exact
        enough for `lidar_wales.py`'s own `covers()` (that source's own
        coverage really is a solid rectangle; this one is not).

        Never touches the network, matching every other `covers()` in
        this project: `best_effort_padded_bng_extent` reads a cached
        OSTN15 grid when one is already on disk and falls back to a
        gridless projection otherwise (see that helper's own docstring in
        `bng.py`), close enough at the 500 m grain this decides "full",
        "partial" or "none" at.
        """
        e_min, n_min, e_max, n_max = best_effort_padded_bng_extent(
            bbox, PAD_METRES, cache_dir=self._ostn15_cache_dir
        )
        touched = _touched_lattice_cells(e_min, n_min, e_max, n_max)
        covered = [cell for cell in touched if cell in _COVERAGE_SET]
        if not covered:
            return "none"
        if len(covered) == len(touched):
            return "full"
        return "partial"

    def tier(self, category: str) -> int | None:
        """This source's own tier for `category`, or None when this
        source does not serve it. See `_TIERS` above.
        """
        return self._TIERS.get(category)

    # -- detail (mapgen.resolver) --------------------------------------------

    def detail(self, bbox: BBox) -> str | None:
        """The resolution and vintage this extent would actually get,
        computed from `covers()` and `_window_pixels`, never guessed:

        - "full" and within `MAX_WINDOW_PIXELS`:
          "25 cm at this extent, flown 2011"
        - "full" but over budget:
          "25 cm needs an extent under about 1 x 1 km here (flown 2011)"
        - "partial":
          "25 cm over part of this extent, flown 2011", with
          "; 25 cm needs an extent under about 1 x 1 km here" appended
          when the padded extent's own intersection with the coverage
          envelope is itself over budget
        - "none": `None`

        "About 1 x 1 km" is `MAX_WINDOW_PIXELS` at `PIXEL_METRES`, spelled
        out rather than computed inline so the sentence stays a fixed
        string: `sqrt(16_777_216) * 0.25` is 1024 m, which is "about 1 km"
        at the precision an owner reads a coverage sentence at.

        Never touches the network: `covers()` and `_window_pixels` are
        both cache-only, the same guarantee `estimate()` itself carries.
        """
        coverage = self.covers(bbox)
        if coverage == "none":
            return None
        pixels = _window_pixels(bbox, self._ostn15_cache_dir)
        over_budget = pixels > MAX_WINDOW_PIXELS
        if coverage == "full":
            if over_budget:
                return "25 cm needs an extent under about 1 x 1 km here (flown 2011)"
            return "25 cm at this extent, flown 2011"
        sentence = "25 cm over part of this extent, flown 2011"
        if over_budget:
            sentence += "; 25 cm needs an extent under about 1 x 1 km here"
        return sentence

    # -- estimate / routing_note ----------------------------------------------

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        """Bytes and seconds for the one-time archive download, with no
        network at all.

        Needs neither `bbox` nor `tiles` to answer honestly, the same
        os_uprn.py shape this mirrors: this archive's cost is a flat,
        whole-zip fact once the cache is cold (both zips are downloaded
        whole regardless of which part of the block a given extent
        touches; see probe-report.md's own "Whole-download is the sane
        route at these sizes"), not one that scales with the query
        extent. `_cache_is_warm` alone decides cold vs warm; `cache_dir()`
        is read as a bare module call (not cached on `self`) so a test
        can monkeypatch it directly.
        """
        if _cache_is_warm(cache_dir()):
            return Estimate(bytes_estimate=0, seconds_estimate=SECONDS_FLOOR)
        bytes_estimate = DSM_ZIP_BYTES + DTM_ZIP_BYTES
        seconds_estimate = max(bytes_estimate / BYTES_PER_SECOND_ESTIMATE, SECONDS_FLOOR)
        return Estimate(bytes_estimate=bytes_estimate, seconds_estimate=seconds_estimate)

    def routing_note(self) -> str | None:
        """A plain-English statement of the one-time archive download a
        cold cache implies, or `None` once both zips already sit in
        `cache_dir()`.

        Mirrors `os_uprn.py`'s own `routing_note()`: read the same
        defensive, optional-extension way (`sources/base.py`'s own
        docstring on `readiness_problem`), disk-only, matching
        `estimate()`'s own no-network rule above.
        """
        if _cache_is_warm(cache_dir()):
            return None
        return _ROUTING_NOTE

    # -- fetch -----------------------------------------------------------------

    def _ensure_zip(
        self, url: str, expected_bytes: int, dest: Path, progress: ProgressSink, tile_id: str
    ) -> None:
        """Ensures `dest` holds a right-size, zip-openable copy of the
        archive at `url`, downloading it first when `dest` is missing or
        the wrong size (a resumed run's own stale partial counts as the
        wrong size, and is re-downloaded whole rather than resumed: see
        the module docstring's "Whole-download is the sane route" note).

        Streams to a randomly-suffixed `.part` file beside `dest` (never
        `dest` itself, so a reader of `dest` never sees a partial body),
        the same atomic write-then-rename shape `inspire.py`'s own
        `fetch_authority_zip` and `os_downloads.download_entry` both use
        for their own whole-file downloads. The temp file is removed on
        every failure path this function can take: a `requests`
        transport failure, a length mismatch, or a right-length body that
        does not open as a non-empty zip. A file only ever lands at
        `dest`'s own real name once every one of those checks has
        already passed, so a later call's own `dest.stat().st_size ==
        expected_bytes` warm check can trust whatever it finds there
        without re-opening it.

        Raises `LidarCardiffError` kind `"download"` for a transport
        failure or a length mismatch, kind `"parse"` for a right-length
        body that fails the zip check. Never puts `url` in a message (see
        `LidarCardiffError`'s own docstring).
        """
        if dest.exists() and dest.stat().st_size == expected_bytes:
            progress.emit("tile_skipped", source=self.id, tile_id=tile_id)
            return

        ensure_dir(dest.parent)
        temp_path = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.part")
        written = 0
        try:
            with self.session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                stream=True,
                timeout=self.timeout_seconds,
            ) as response:
                response.raise_for_status()
                with temp_path.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            written += len(chunk)
        except requests.RequestException as exc:
            temp_path.unlink(missing_ok=True)
            raise LidarCardiffError(
                f"Could not download {dest.name}.", kind="download"
            ) from exc
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        if written != expected_bytes:
            temp_path.unlink(missing_ok=True)
            raise LidarCardiffError(
                f"{dest.name} downloaded {written} bytes, expected {expected_bytes}.",
                kind="download",
            )

        try:
            with zipfile.ZipFile(temp_path) as archive:
                has_members = bool(archive.namelist())
        except zipfile.BadZipFile:
            has_members = False
        if not has_members:
            temp_path.unlink(missing_ok=True)
            raise LidarCardiffError(
                f"{dest.name} did not open as a valid, non-empty zip file "
                f"once downloaded.",
                kind="parse",
            )

        temp_path.replace(dest)
        progress.emit("tile_done", source=self.id, tile_id=tile_id)

    def _record_tile_failures(self, tiles: Sequence[Tile], kind: str, reason: str) -> None:
        """Record why this fetch() could not deliver, once per tile.

        Matches `lidar_wales.py`'s and `os_uprn.py`'s own
        `_record_tile_failures` exactly, including the reasoning: neither
        of this source's two units of work (the DSM zip, the DTM zip) is
        tile-shaped, but package.py's failure handler needs one record per
        tile the survey actually asked for regardless, so it has something
        to defer a decision on instead of ending the run on the spot (see
        `__init__`'s own comment on `self.tile_failures`). `reason` is
        always composed from `LidarCardiffError`'s own message, which
        never carries a URL, matching every other source's rule.
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
        """Ensures both archive zips sit in `cache_dir()`, downloading and
        verifying whichever one is missing or the wrong size, writes a
        pointer to each one's real path into `work_dir` (Task 5's bridge;
        see the module docstring's own section), and returns their cached
        paths directly: `[DSM path, DTM path]`, in that order, documented
        here because a direct caller's own `merge()` call reads them by
        name.

        `work_dir` is where package.py's `run_survey` looks to build
        `merge()`'s real `parts` argument (a directory LISTING, not this
        return value: see the module docstring's "Task 5's bridge"
        section for why), so the two zips' own real, national cache
        paths never touch it, only a pointer to each. This archive is
        static 2011 data (the module docstring's own "500 m lattice"
        section), fetched and cached whole regardless of which part of
        the block a given survey's own extent touches, the same reason
        `estimate()` above needs neither `bbox` nor `tiles` to answer
        honestly. The two zips are read back from `cache_dir()` itself,
        never copied into `work_dir`: they are shared across every future
        survey that ever selects this source, the same national-cache
        shape `os_uprn.py`'s own `fetch()` gives its own one download for
        the identical reason. `bbox` IS used for two things: the budget gate
        immediately below, and remembered on `self._bbox` for `merge()`
        to read later, since that method's own protocol signature has no
        bbox parameter of its own (see the module docstring's "merge():
        no bbox in its own signature" section). `tiles` is used too,
        though only to know which tiles to blame a failure on
        (`_record_tile_failures`): see `__init__`'s own comment for why
        that accounting exists at all for a source with no tile-shaped
        work.

        The budget gate runs FIRST, before either zip is downloaded: an
        extent whose own intersection with the coverage envelope needs
        more pixels at 25 cm than `cog.MAX_WINDOW_PIXELS` allows is
        refused here, with a recorded `tile_failures` entry
        (`FAILURE_NODE_CAP`, never retryable) and a raised
        `LidarCardiffError` (kind `"budget"`), rather than only inside
        `merge()`: see the module docstring's "The budget gate lives in
        fetch(), first, and again in merge()" section for why this is
        the gate that actually has to reach the owner.

        `cancel` is checked before each of the two zips, never mid
        download: a unit already in flight always finishes, matching
        every other source's own reading of this optional parameter (see
        `sources/base.py`'s `LayerSource` docstring).

        A `LidarCardiffError` from either zip, or from the budget gate,
        is recorded against every tile in `tiles`
        (`_classify_lidar_cardiff_error` maps its own `kind` to the
        shared failure vocabulary) and then re-raised unchanged, the
        same "record, then raise" shape `os_uprn.py`'s own `fetch()` uses
        for its own `OsOpenError`.
        """
        self.tile_failures = []
        self._bbox = bbox
        if cancel is not None:
            cancel.raise_if_cancelled()

        pixels = _window_pixels(bbox, self._ostn15_cache_dir)
        if pixels > MAX_WINDOW_PIXELS:
            error = LidarCardiffError(_budget_refusal_reason(pixels), kind="budget")
            self._record_tile_failures(
                tiles, _classify_lidar_cardiff_error(error), str(error)
            )
            raise error

        directory = cache_dir()
        dsm_path = directory / DSM_ZIP_NAME
        dtm_path = directory / DTM_ZIP_NAME

        try:
            self._ensure_zip(DSM_ZIP_URL, DSM_ZIP_BYTES, dsm_path, progress, "dsm")
        except LidarCardiffError as exc:
            self._record_tile_failures(tiles, _classify_lidar_cardiff_error(exc), str(exc))
            raise

        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            self._ensure_zip(DTM_ZIP_URL, DTM_ZIP_BYTES, dtm_path, progress, "dtm")
        except LidarCardiffError as exc:
            self._record_tile_failures(tiles, _classify_lidar_cardiff_error(exc), str(exc))
            raise

        # Task 5's bridge (see the module docstring's own section): both
        # zips are confirmed present and right-sized at this point, so a
        # pointer to each one's real cache_dir() path is written into
        # work_dir, never the zip itself. Written only after both
        # _ensure_zip calls above have succeeded, matching every other
        # "nothing written on a failure path" guarantee in this file: a
        # budget refusal or a download failure raises before reaching
        # here, so no pointer is ever left behind for a fetch() that did
        # not actually finish.
        atomic_write_text(work_dir / _DSM_CACHE_POINTER_NAME, str(dsm_path))
        atomic_write_text(work_dir / _DTM_CACHE_POINTER_NAME, str(dtm_path))

        return [dsm_path, dtm_path]

    # -- merge -------------------------------------------------------------

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Assembles the two archive zips' own ASCII grid members into
        `<stem>_lidar25_dsm.tif` and `<stem>_lidar25_dtm.tif` (spec-pinned
        names), refusing the extent up front, with kind `"budget"`, when
        its own intersection with the coverage envelope needs more
        pixels at 25 cm than `cog.MAX_WINDOW_PIXELS` allows: the same
        figure `detail()` already previewed (`_window_pixels`), so the
        preview and the refusal can never disagree. `fetch()` checks the
        identical gate first, before either zip is even downloaded (see
        the module docstring's "The budget gate lives in fetch(), first,
        and again in merge()" section for why that copy, not this one, is
        the one that actually reaches the owner today); this copy stays
        as a second line of defence for a `merge()` reached by some other
        route.

        `parts` ordinarily holds `fetch()`'s own two zip paths, selected
        here BY NAME (`DSM_ZIP_NAME`/`DTM_ZIP_NAME`), never by position:
        the `ElevationSource.merge` lesson every other source's own
        merge() in this package already follows (`lidar_wales.py`,
        `os_uprn.py`). package.py's real pipeline hands this a different
        shape instead, the two work_dir POINTER files Task 5's bridge
        added (see the module docstring's own section): when neither zip
        is found directly by name, `_resolve_cache_pointer` reads a
        matching pointer's own text back to the real `cache_dir()` path
        it names. The direct, by-name lookup is tried first and always
        wins when it succeeds, so a caller that already hands this the
        two real zip paths (every Task 4 test still does) never touches
        the pointer path at all. `_resolve_cache_pointer` itself can
        raise a named `LidarCardiffError` (kind `"download"` for a
        pointer whose recorded cache target has since gone missing,
        kind `"parse"` for one whose own text is not a usable path at
        all: see that function's own docstring), which propagates
        straight out of this call unchanged, the same "record, then
        raise" shape every other named failure in this file already
        follows.

        `self._bbox`, set at the top of the most recent `fetch()` call on
        this same instance, is where the extent comes from: see the
        module docstring's "merge(): no bbox in its own signature"
        section for why this method's own protocol signature has nowhere
        else to carry it. A `merge()` called before any `fetch()` on this
        instance has no extent to read and raises a named
        `LidarCardiffError` (kind `"parse"`) rather than an unlabelled
        `AttributeError` from deep inside the BNG projection code that
        would otherwise follow from a bare `None`.

        Neither raster is written until both have been fully assembled:
        a member that fails to parse, in either zip, raises before either
        temp file below is ever written (see the module docstring's "One
        failed member fails the whole merge" section), so a failed
        assembly leaves neither `_lidar25_dsm.tif` nor `_lidar25_dtm.tif`
        behind. The same guarantee also covers a failure between the two
        WRITES themselves (disk full, an antivirus lock): both rasters
        are written to temporary names first and renamed into their real
        names only once both writes succeeded, so a failure on the second
        write cannot leave the first sitting on disk without its sibling.
        """
        if self._bbox is None:
            raise LidarCardiffError(
                "merge() needs the extent fetch() was called with, and no "
                "fetch() has set one on this source instance yet.",
                kind="parse",
            )

        out_dir = Path(out_dir)
        dsm_zip_path = next((part for part in parts if part.name == DSM_ZIP_NAME), None)
        dtm_zip_path = next((part for part in parts if part.name == DTM_ZIP_NAME), None)
        if dsm_zip_path is None:
            dsm_zip_path = _resolve_cache_pointer(parts, _DSM_CACHE_POINTER_NAME)
        if dtm_zip_path is None:
            dtm_zip_path = _resolve_cache_pointer(parts, _DTM_CACHE_POINTER_NAME)
        if dsm_zip_path is None or dtm_zip_path is None:
            raise LidarCardiffError(
                "Both archive zips are needed to build the 25 cm rasters, "
                "and this merge was not handed both.",
                kind="parse",
            )

        pixels = _window_pixels(self._bbox, self._ostn15_cache_dir)
        if pixels > MAX_WINDOW_PIXELS:
            raise LidarCardiffError(_budget_refusal_reason(pixels), kind="budget")

        window_e_min, window_n_max, window_width, window_height = (
            _snapped_merge_window(self._bbox, self._ostn15_cache_dir)
        )

        dsm_window = _assemble_window(
            dsm_zip_path, window_e_min, window_n_max, window_width, window_height
        )
        dtm_window = _assemble_window(
            dtm_zip_path, window_e_min, window_n_max, window_width, window_height
        )

        dsm_output = out_dir / f"{stem}_lidar25_dsm.tif"
        dtm_output = out_dir / f"{stem}_lidar25_dtm.tif"
        # Temp-then-rename, both files, and only after both writes have
        # succeeded: see the module docstring's own note on why a plain
        # pair of sequential writes to the real names would let a second-
        # write failure leave the first file behind without its sibling.
        # Mirrors _ensure_zip's own temp-file naming exactly.
        dsm_temp = dsm_output.with_name(f"{dsm_output.name}.{uuid.uuid4().hex[:8]}.part")
        dtm_temp = dtm_output.with_name(f"{dtm_output.name}.{uuid.uuid4().hex[:8]}.part")

        write_bng_geotiff(dsm_temp, dsm_window)
        try:
            write_bng_geotiff(dtm_temp, dtm_window)
        except BaseException:
            dsm_temp.unlink(missing_ok=True)
            raise

        dsm_temp.replace(dsm_output)
        dtm_temp.replace(dtm_output)
        return [dsm_output, dtm_output]


def _snapped_merge_window(
    bbox: BBox, ostn15_cache_dir: Path | None
) -> tuple[float, float, int, int]:
    """The window `merge()` builds: the padded extent's own intersection
    with `_ENVELOPE`, snapped OUTWARD to the 0.25 m lattice anchored at
    integer metres, as `(e_min, n_max, width_px, height_px)`.

    Every member's own `xllcorner`/`yllcorner` is an integer metre (the
    module docstring's own probed fact), so that lattice's pixel edges
    sit at exact multiples of `PIXEL_METRES`; snapping the window OUTWARD
    onto the same lattice, rather than leaving it at the padded extent's
    own arbitrary real-valued edges, is what makes every paste in
    `_paste_member` below an exact integer-offset copy rather than a
    resample. Outward, specifically, so the window never loses so much
    as a sliver of what was actually asked for: floor on the near edges,
    ceil on the far ones.

    Not used for the budget gate itself (`_window_pixels`, called
    separately in `merge()`, prices the unsnapped intersection): see the
    module docstring's own note on why gating on this function's
    slightly larger, snapped figure would let the refusal disagree with
    `detail()`'s own preview by the sliver snapping can add.
    """
    e_min, n_min, e_max, n_max = best_effort_padded_bng_extent(
        bbox, PAD_METRES, cache_dir=ostn15_cache_dir
    )
    env_e_min, env_n_min, env_e_max, env_n_max = _ENVELOPE
    win_e_min = max(e_min, env_e_min)
    win_n_min = max(n_min, env_n_min)
    win_e_max = min(e_max, env_e_max)
    win_n_max = min(n_max, env_n_max)

    window_e_min = math.floor(win_e_min / PIXEL_METRES) * PIXEL_METRES
    window_e_max = math.ceil(win_e_max / PIXEL_METRES) * PIXEL_METRES
    window_n_min = math.floor(win_n_min / PIXEL_METRES) * PIXEL_METRES
    window_n_max = math.ceil(win_n_max / PIXEL_METRES) * PIXEL_METRES
    window_width = max(0, round((window_e_max - window_e_min) / PIXEL_METRES))
    window_height = max(0, round((window_n_max - window_n_min) / PIXEL_METRES))
    return window_e_min, window_n_max, window_width, window_height


def _assemble_window(
    zip_path: Path,
    window_e_min: float,
    window_n_max: float,
    window_width: int,
    window_height: int,
) -> BngWindow:
    """The window's own rectangle, built from `zip_path`'s members: NaN
    everywhere at first (`array("f")`, so `write_bng_geotiff` sees a
    genuinely absent value rather than a fabricated zero), then every
    member whose own header-declared bounds overlap the window is fully
    parsed (`value_scale=0.001`: the archive's own millimetres) and
    pasted in. A pixel the window asks for that no member's own envelope
    reaches stays NaN and reads back as nodata: partial coverage is an
    honest raster with the uncovered area absent, never guessed at,
    matching how the Welsh mosaic already answers nodata over England
    (`cog.py`'s own module docstring).

    `parse_asc_header` runs on EVERY member first, whether or not it
    turns out to overlap the window, and any `AscGridError` it raises
    (or that the later `parse_asc` full parse raises, for a member that
    does overlap) fails this whole call: see the module docstring's "One
    failed member fails the whole merge" section for why a header this
    function cannot trust is not one it can use to decide "safe to skip"
    from.
    """
    window_e_max = window_e_min + window_width * PIXEL_METRES
    window_n_min = window_n_max - window_height * PIXEL_METRES
    values = array("f", [math.nan]) * (window_width * window_height)

    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            # latin-1 rather than ascii or utf-8: it never raises on any
            # byte value, so a genuinely corrupt member fails through
            # AscGridError's own, tested checks below rather than a
            # UnicodeDecodeError this function would otherwise have to
            # catch and translate separately for no benefit (the real
            # archive's own members are plain ASCII text throughout, so
            # this never changes what a well-formed member reads as).
            text = archive.read(name).decode("latin-1")
            try:
                header = parse_asc_header(text)
            except AscGridError as exc:
                raise LidarCardiffError(str(exc), kind="parse") from exc

            member_e_min = header["xllcorner"]
            member_n_min = header["yllcorner"]
            member_e_max = member_e_min + header["ncols"] * header["cellsize"]
            member_n_max = member_n_min + header["nrows"] * header["cellsize"]
            if (
                member_e_max <= window_e_min or member_e_min >= window_e_max
                or member_n_max <= window_n_min or member_n_min >= window_n_max
            ):
                continue  # This member's own bounds miss the window entirely.

            try:
                member = parse_asc(text, value_scale=0.001)
            except AscGridError as exc:
                raise LidarCardiffError(str(exc), kind="parse") from exc

            _paste_member(values, window_e_min, window_n_max, window_width, member)

    return BngWindow(
        e_origin=window_e_min,
        n_top=window_n_max,
        pixel_size=PIXEL_METRES,
        width=window_width,
        height=window_height,
        values=values,
    )


def _paste_member(
    values: array,
    window_e_min: float,
    window_n_max: float,
    window_width: int,
    member: BngWindow,
) -> None:
    """Copies `member`'s own overlap with the window straight into
    `values`, with no resampling and no interpolation: both rectangles
    sit on the identical 0.25 m lattice (every member's own xllcorner/
    yllcorner is an integer metre, and the window was snapped outward
    onto that same lattice by `_snapped_merge_window`), so every offset
    below lands on an exact pixel boundary rather than needing to be
    split across two source pixels.
    """
    window_height = len(values) // window_width
    window_e_max = window_e_min + window_width * PIXEL_METRES
    window_n_min = window_n_max - window_height * PIXEL_METRES
    member_e_min, member_n_min, member_e_max, member_n_max = member.bounds()

    overlap_e_min = max(member_e_min, window_e_min)
    overlap_e_max = min(member_e_max, window_e_max)
    overlap_n_min = max(member_n_min, window_n_min)
    overlap_n_max = min(member_n_max, window_n_max)
    if overlap_e_max <= overlap_e_min or overlap_n_max <= overlap_n_min:
        return  # Touches the window's own bounding rectangle, but not this slice.

    def _snap(raw: float) -> int:
        # Exact by construction (both rectangles share the one 0.25 m
        # lattice anchored at integer metres): this assert is what turns
        # a lattice mismatch, were one ever introduced upstream, into a
        # loud failure here rather than a silently resampled, wrong
        # pixel a plain round() would produce without comment.
        rounded = round(raw)
        assert abs(raw - rounded) < 1e-6, (
            "lidar_cardiff merge: member and window pixel lattices do not "
            "align; every member's own xllcorner/yllcorner must be an "
            "integer metre on the 0.25 m lattice the window is built on."
        )
        return rounded

    member_col0 = _snap((overlap_e_min - member_e_min) / PIXEL_METRES)
    member_row0 = _snap((member_n_max - overlap_n_max) / PIXEL_METRES)
    window_col0 = _snap((overlap_e_min - window_e_min) / PIXEL_METRES)
    window_row0 = _snap((window_n_max - overlap_n_max) / PIXEL_METRES)
    overlap_width = _snap((overlap_e_max - overlap_e_min) / PIXEL_METRES)
    overlap_height = _snap((overlap_n_max - overlap_n_min) / PIXEL_METRES)

    for row in range(overlap_height):
        src_start = (member_row0 + row) * member.width + member_col0
        dst_start = (window_row0 + row) * window_width + window_col0
        values[dst_start:dst_start + overlap_width] = (
            member.values[src_start:src_start + overlap_width]
        )
