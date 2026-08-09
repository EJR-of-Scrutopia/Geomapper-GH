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

## The window, and why the budget gate reads _window_pixels rather than
## the window it is about to build

`merge()`'s own window is the padded extent's intersection with
`_ENVELOPE`, snapped OUTWARD to the 0.25 m lattice so every edge lands on
an exact pixel boundary (every member's own `xllcorner`/`yllcorner` is an
integer metre, hence already on that lattice). The budget gate runs
BEFORE that window is built at all, and it gates on `_window_pixels`'s
own, unsnapped figure, the exact number `detail()` already previewed:
snapping outward can only grow a window by under one pixel per edge, and
gating on the snapped count instead would let the refusal and the
preview disagree by that same sliver, for no benefit, since the number
the owner already read in `detail()`'s own sentence is `_window_pixels`'s.

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
from mapgen.fsutil import ensure_dir
from mapgen.geo import BBox, Tile
from mapgen.geotiff_write import write_bng_geotiff
from mapgen.jobs import CancelToken
from mapgen.sources.base import (
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

# The archive's own published pixel size (probe-report.md: "2000x2000
# cells at 0.5m" for the 2012 50cm flight; this 2011 flight is the 25cm
# one the same catalogue lists at half that cell size).
PIXEL_METRES = 0.25

# No live measurement yet: a placeholder in the same sense os_uprn.py's
# own BYTES_PER_SECOND_ESTIMATE is a placeholder before that source's
# first real cold run, to be corrected against measurement once a later
# task's live run actually times the two downloads (fetch()'s own live
# test times a real cold run, but does not yet feed that measurement
# back into this constant).
BYTES_PER_SECOND_ESTIMATE = 2_000_000

# No warm path has ever been measured either (nothing has downloaded
# either zip on this machine yet): kept only so a warm estimate never
# claims exactly zero seconds. Refit alongside BYTES_PER_SECOND_ESTIMATE
# above from the same later live run.
SECONDS_FLOOR = 3.0

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
    archive zips, or when `merge()` cannot turn them into the 25 cm
    rasters.

    `kind` is `"download"` (a transport failure, or a downloaded body
    whose length did not match `DSM_ZIP_BYTES`/`DTM_ZIP_BYTES` exactly)
    or `"parse"` (a right-length body that did not open as a non-empty
    zip, OR, from `merge()`, a zip member whose header or values
    `asc_grid.AscGridError` refused), the same two-way slice of
    `os_downloads.OsOpenError`'s own kind vocabulary applied here without
    importing that class: this source's own download failures are never
    a listing or a ranged read, so `fetch()` never needs the other two
    kinds that vocabulary carries. `merge()` also raises a third kind,
    `"budget"`: the padded extent's own intersection with the coverage
    envelope needs more pixels at 25 cm than `cog.MAX_WINDOW_PIXELS`
    allows, the same number `detail()` already previewed. `"budget"` is
    never retryable (the extent does not get smaller by asking again),
    the same practical fact `sources/base.py`'s own `FAILURE_NODE_CAP`
    documents for OsmSource's unrelated failure; it is left to fall
    through `_classify_lidar_cardiff_error`'s existing default rather
    than added as a third branch there, since that classifier is read
    only from `fetch()`'s own except blocks and a merge()-raised error
    never reaches it.

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

    Mirrors `os_uprn.py`'s own `_classify_os_open_error` for the identical
    two-kind split: `LidarCardiffError.kind` is a two-way slice of
    `os_downloads.OsOpenError`'s own vocabulary (see that class's own
    docstring), and `os_uprn.py`'s classifier already answers exactly this
    question for both of the kinds this source can raise. `"download"` (a
    transport failure, or a length mismatch) is `FAILURE_UNREACHABLE`,
    retryable: the same bucket `os_uprn.py`'s own classifier gives
    `OsOpenError`'s `"download"`/`"listing"` kinds. `"parse"` (a
    right-length body that will not open as a zip) falls through to
    `FAILURE_UNKNOWN`, the same catch-all `os_uprn.py`'s own classifier
    gives every kind it does not special-case.
    """
    if exc.kind == "download":
        return FAILURE_UNREACHABLE
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


class LidarCardiffSource:
    id = "lidar_cardiff"
    display_name = (
        "LiDAR terrain (Creigiau and Pentyrch, north-west Cardiff, 25 cm, "
        "flown 2011)"
    )
    licence = "Open Government Licence for Public Sector Information"
    attribution = (
        "Contains Natural Resources Wales information © Natural "
        "Resources Wales and Database Right. All rights Reserved."
    )
    requires_api_key = False

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
        verifying whichever one is missing or the wrong size, and returns
        their cached paths: `[DSM path, DTM path]`, in that order,
        documented here because `merge()` reads them.

        `work_dir` is accepted only to satisfy the LayerSource protocol
        signature and is not otherwise used: this archive is static 2011
        data (the module docstring's own "500 m lattice" section),
        fetched and cached whole regardless of which part of the block a
        given survey's own extent touches, the same reason `estimate()`
        above needs neither `bbox` nor `tiles` to answer honestly. The
        two zips are read back from `cache_dir()` itself, never copied
        into `work_dir`: they are shared across every future survey that
        ever selects this source, the same national-cache shape
        `os_uprn.py`'s own `fetch()` gives its own one download for the
        identical reason. `bbox` IS used, but only remembered
        (`self._bbox`) rather than acted on here: `merge()` is where the
        actual window this extent needs gets built, and its own protocol
        signature has no bbox parameter of its own (see the module
        docstring's "merge(): no bbox in its own signature" section).
        `tiles` is used too, though only to know which tiles to blame a
        failure on (`_record_tile_failures`): see `__init__`'s own
        comment for why that accounting exists at all for a source with
        no tile-shaped work.

        `cancel` is checked before each of the two zips, never mid
        download: a unit already in flight always finishes, matching
        every other source's own reading of this optional parameter (see
        `sources/base.py`'s `LayerSource` docstring).

        A `LidarCardiffError` from either zip is recorded against every
        tile in `tiles` (`_classify_lidar_cardiff_error` maps its own
        `kind` to the shared failure vocabulary) and then re-raised
        unchanged, the same "record, then raise" shape `os_uprn.py`'s own
        `fetch()` uses for its own `OsOpenError`.
        """
        self.tile_failures = []
        self._bbox = bbox
        if cancel is not None:
            cancel.raise_if_cancelled()

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

        return [dsm_path, dtm_path]

    # -- merge -------------------------------------------------------------

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Assembles the two archive zips' own ASCII grid members into
        `<stem>_lidar25_dsm.tif` and `<stem>_lidar25_dtm.tif` (spec-pinned
        names), refusing the extent up front, with kind `"budget"`, when
        its own intersection with the coverage envelope needs more
        pixels at 25 cm than `cog.MAX_WINDOW_PIXELS` allows: the same
        figure `detail()` already previewed (`_window_pixels`), so the
        preview and the refusal can never disagree.

        `parts` holds `fetch()`'s own two zip paths, selected here BY
        NAME (`DSM_ZIP_NAME`/`DTM_ZIP_NAME`), never by position: the
        `ElevationSource.merge` lesson every other source's own merge()
        in this package already follows (`lidar_wales.py`, `os_uprn.py`).

        `self._bbox`, set at the top of the most recent `fetch()` call on
        this same instance, is where the extent comes from: see the
        module docstring's "merge(): no bbox in its own signature"
        section for why this method's own protocol signature has nowhere
        else to carry it.

        Neither raster is written until both have been fully assembled:
        a member that fails to parse, in either zip, raises before
        `write_bng_geotiff` is ever called for either one (see the module
        docstring's "One failed member fails the whole merge" section),
        so a failed merge leaves neither `_lidar25_dsm.tif` nor
        `_lidar25_dtm.tif` behind, never one without the other.
        """
        out_dir = Path(out_dir)
        dsm_zip_path = next((part for part in parts if part.name == DSM_ZIP_NAME), None)
        dtm_zip_path = next((part for part in parts if part.name == DTM_ZIP_NAME), None)
        if dsm_zip_path is None or dtm_zip_path is None:
            raise LidarCardiffError(
                "Both archive zips are needed to build the 25 cm rasters, "
                "and this merge was not handed both.",
                kind="parse",
            )

        pixels = _window_pixels(self._bbox, self._ostn15_cache_dir)
        if pixels > MAX_WINDOW_PIXELS:
            # Verbatim, with only the pixel count substituted (the plan's
            # own pinned reason string): the thousands separator is part
            # of the pinned text, not an incidental formatting choice.
            raise LidarCardiffError(
                f"this extent needs {pixels:,} pixels at 25 cm and the "
                f"raster budget is 16,777,216; extents under about 1 x 1 "
                f"km inside the covered block come back at 25 cm",
                kind="budget",
            )

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
        write_bng_geotiff(dsm_output, dsm_window)
        write_bng_geotiff(dtm_output, dtm_window)
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
