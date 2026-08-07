"""OsUprnSource: OS Open UPRN addresses (GB) as a LayerSource.

The fifth OS Open product this plan reads (Task 5 of the OS Open pack
plan), built on the same three modules `os_open.py` (Task 4, this
module's closest sibling) already ties together: `os_downloads.py` (the
Downloads API client, versioned cache directories, atomic download),
`os_shards.py` (the derive-once shard store, specifically its
`write_uprn_shards`/`uprn_in` pair), and `bng.py` (OSTN15, needed only to
place a WGS84 query bbox on the National Grid so `uprn_in` can filter by
easting/northing; see "Merge needs no OSTN15 at all" below for the one
place this module deliberately does NOT reach for `bng.py`).

## One national file, not one per square

Every product `os_open.py` reads is tiled by the OS National Grid's
100km squares, because each one carries real geometry a survey extent
can intersect square by square. OS Open UPRN carries no geometry at all,
only a single easting/northing (and OS's own precomputed WGS84
latitude/longitude) per address, and OS accordingly publishes the whole
of Great Britain as ONE CSV inside one zip (`osopenuprn_202608_csv.zip`,
618,494,417 bytes; one member, `osopenuprn_202608.csv`, 2,271,141,910
bytes uncompressed; header `UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,
LONGITUDE` preceded by a UTF-8 BOM; all probed live 2026-08-07, this
task's own plan). There is no "0 bytes for the squares already cached,
the rest priced" middle ground the way `os_open.py`'s per-square
`estimate()` has: either the national shard cache is complete, or it is
not, and `estimate()`/`fetch()` are both built around that flatter fact
rather than pretending a per-square shape exists here that this product
does not have.

## Cache layout

Under `os_downloads.cache_root()` (`~/.mapgen/osopen`), this source gets
`os_downloads.product_cache_dir(PRODUCT, version)` (`PRODUCT =
"OpenUPRN"`), and under THAT:

- `raw/`: the temporary downloaded zip, deleted the moment the shard
  write finishes, success or failure alike (see `_shard_uprn`'s own
  docstring for the peak-disk accounting this leaves).
- `shards/`: ONE flat directory (not one per square, the way
  `os_open.py`'s `shards/<SQ>/` tree is one per PRODUCT-AND-square),
  holding `<SQ>.csv.gz` per 100km square plus `meta.json`,
  `os_shards.write_uprn_shards`'s own output shape: a single pass over
  the one national CSV already produces the finished per-square split as
  a side effect, so there is no reason to shard this the way a
  per-square PRODUCT with one zip per square naturally does.

## Ensuring the cache does not depend on this survey's own bbox

`fetch()` always resolves the whole national shard cache once per
version, regardless of whether THIS survey's own extent falls inside
Great Britain at all. The shard cache is shared across every future
survey that ever selects this source (the whole point of caching it
nationally rather than per-square), and a `fetch()` that skipped
building it for an out-of-GB bbox would not save any network traffic
overall, only move the SAME 619 MB download onto the very next survey
that actually does need it, at that survey's own wall time instead of
this one's: a worse trade for an identical total cost. The one thing
`fetch()` genuinely cannot do without placing the extent on the National
Grid first is compute WHICH rows belong to this survey's own work part,
which is why an extent OSTN15 cannot place at all (see
`NO_HONEST_EXTENT_MESSAGE`) is refused before the download is even
attempted: there would be nothing honest to filter the national cache
down to.

## Merge needs no OSTN15 at all

Unlike `os_open.py`'s `merge()`, which reprojects every feature's own
BNG geometry through `bng.from_bng` (a real transform, needing the
OSTN15 grid to be cache-loadable), this source's `merge()` reads the
CSV's own `LATITUDE`/`LONGITUDE` columns directly. Those are OS's own
computed WGS84 values, already sitting in the shard files `fetch()`
built (see `os_shards.py`'s own module docstring, "write_uprn_shards"),
authoritative for exactly this reason: re-deriving them a second time
from the row's easting/northing through `bng.py` would risk a transform
disagreement between two independently-computed WGS84 answers for the
same point, for no benefit, since OS has already done that computation
once, correctly, and published the result in the same row.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Sequence

import requests

from mapgen.bng import BngError, best_effort_padded_bng_extent, ensure_ostn15, padded_bng_extent
from mapgen.egrid import PAD_METRES
from mapgen.fsutil import atomic_write_bytes, atomic_write_text
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken
from mapgen.os_downloads import (
    OsOpenError,
    cache_root,
    download_entry,
    entry_for,
    product_cache_dir,
    product_downloads,
    product_version,
    safe_download_filename,
    sweep_old_versions,
)
from mapgen.os_shards import GB_SQUARES, shards_complete, squares_for, uprn_in, write_uprn_shards
from mapgen.sources.base import (
    FAILURE_NO_OUTPUT,
    FAILURE_UNKNOWN,
    FAILURE_UNREACHABLE,
    Estimate,
    ProgressSink,
    TileFailure,
    classify_status_failure,
    classify_transport_failure,
)

# The OS Data Hub Downloads API's own product id for OS Open UPRN,
# mirroring os_open.py's PRODUCTS entries exactly (a catalog name, never
# a URL; see os_downloads.py's own "no-URL rule").
PRODUCT = "OpenUPRN"

# osopenuprn_202608_csv.zip's exact listed size, probed live 2026-08-07
# against the real OS Data Hub downloads listing (this task's own plan,
# "Owner ground truth" / verified-facts section). Not rounded, not
# estimated: this is the one number this whole module prices its
# uncached estimate from, and it is the real figure the real 2026-08
# version answered on that date.
UPRN_BYTES_ONE_TIME = 618_494_417

# The same OS Data Hub Downloads API os_open.py's own OsOpenSource talks
# to (both modules go through os_downloads.py), so its own measured rate
# floor applies here too rather than being a second, independent guess:
# see os_open.py's own BYTES_PER_SECOND_ESTIMATE for the measurement this
# number is copied from (OpenGreenspace, 2,830,794 bytes / 3.1148959 s,
# the slowest CLEAN OS Data Hub rate on record as of 2026-08-07, itself
# already sitting below every other clean rate that task measured).
# Reusing it here, rather than re-deriving a third figure, is what keeps
# the two sibling sources from silently drifting apart about how fast
# the one service underneath both of them actually is.
BYTES_PER_SECOND_ESTIMATE = 850_000.0

# Still a placeholder after Task 9 (2026-08-07), and deliberately not
# refit alongside os_open.py's own SECONDS_FLOOR: this product's national
# shard cache does not exist on this machine at all, so there is no warm
# path here yet to measure honestly, only the cold, one-time, 619 MB
# national download `UPRN_BYTES_ONE_TIME` already prices. Task 9's own
# live test (`test_live_smoke.py`) excludes `os_uprn` from its selection
# for exactly this reason, rather than triggering that real download
# inside a test suite just to have a number. This will be refit from the
# first real owner run that ticks addresses, whenever that happens (see
# `docs/superpowers/HANDOFF.md`), the same way `os_open.py`'s own byte
# constants were refit from Task 4's first real cold run rather than a
# manufactured one.
SECONDS_FLOOR = 3.0

# An extent OSTN15 has no shift value for at all (genuinely outside
# Great Britain, or otherwise unplaceable on the National Grid): there is
# no honest bounding box to filter the national UPRN cache down to, the
# same "nothing fabricated" refusal os_open.py's own NO_GB_SQUARES_
# MESSAGE states for its own, per-square products.
NO_HONEST_EXTENT_MESSAGE = (
    "This extent could not be placed on the British National Grid, so "
    "there is no honest bounding box to filter OS Open UPRN addresses by."
)

_ROUTING_NOTE = (
    "Addresses: first use downloads the national OS Open UPRN file "
    "(619 MB, cached for every later survey)."
)

_WORK_PART_NAME = "os_uprn.csv"


class OsUprnSourceError(RuntimeError):
    """Raised when this extent has no honest way to be filtered against
    OS Open UPRN's own national cache (an extent OSTN15 cannot place at
    all).

    A source-level refusal, distinct from `os_downloads.OsOpenError`
    (that module's own transport/parse-layer exception), mirroring
    `OsOpenSourceError`/`LidarWalesError`/`InspireError`: each keeps its
    own source-level refusal separate from the lower-level exception its
    fetch() unwraps and re-raises unchanged for every other failure.
    """


def _any_complete_uprn_cache() -> bool:
    """True if ANY version directory this source has ever cached under
    `cache_root()` holds a shard-complete national shard directory,
    checked entirely on disk (a glob plus `shards_complete`, never the
    network).

    Mirrors os_open.py's own `_any_cached_square`, one grain coarser:
    that function asks "is THIS square done for THIS product", this one
    asks "is the WHOLE national file done for THIS product", since this
    source has only the one, unsplit unit of work to be done or not.
    `estimate()`/`routing_note()` must never touch the network to answer
    this (see `estimate()`'s own docstring); `fetch()` reconciles
    whatever this finds against whatever the live version turns out to
    be when it actually runs.
    """
    root = cache_root()
    if not root.exists():
        return False
    for candidate in root.iterdir():
        if not candidate.is_dir():
            continue
        base, separator, _version = candidate.name.rpartition("_")
        if not separator or base != PRODUCT:
            continue
        if shards_complete(candidate / "shards"):
            return True
    return False


def _latest_complete_uprn_version() -> str | None:
    """The newest version directory (lexicographic, which sorts
    correctly for OS Open's own "YYYY-MM" shape) whose national shard
    directory is already complete, or `None`.

    `fetch()`'s own cache-fallback path (`_resolve_version`) needs this
    when the live listing is unreachable; mirrors os_open.py's own
    `_latest_complete_version_for_squares`, without a squares argument,
    for the same reason `_any_complete_uprn_cache` has none: there is
    nothing to reconcile a coherent version against below "the whole
    file", since this source never shards a partial subset of it.
    """
    root = cache_root()
    if not root.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        base, separator, version = entry.name.rpartition("_")
        if not separator or base != PRODUCT:
            continue
        candidates.append((version, entry))
    for version, entry in sorted(candidates, reverse=True):
        if shards_complete(entry / "shards"):
            return version
    return None


def _classify_os_open_error(exc: OsOpenError) -> str:
    """The shared failure vocabulary's term (sources/base.py) for this
    source's own `OsOpenError`.

    Duplicated from os_open.py's own module-private `_classify_os_open_
    error` rather than imported (a leading underscore means "this
    module's own business"; reaching past it would couple two sibling
    sources' internals together for a four-line function): the reasoning
    behind each branch is identical and applies unchanged here, since
    both sources wrap the same os_downloads.py exception vocabulary.
    """
    if exc.status_code is not None:
        kind, _phrase = classify_status_failure(exc.status_code)
        return kind
    if exc.kind in ("listing", "download"):
        return FAILURE_UNREACHABLE
    return FAILURE_UNKNOWN


def _sole_csv_member(names: Sequence[str]) -> str:
    """The one zip member ending `.csv`, found by asking the zip itself
    rather than guessing a path this module has not actually verified
    (mirrors os_open.py's own `_sole_gml_member`, applied there to
    `.gml` members instead, for the identical reason: OS Open's own zips
    are not committed anywhere this project can probe their internal
    path structure from ahead of time)."""
    csv_names = [name for name in names if name.endswith(".csv")]
    if len(csv_names) != 1:
        raise OsOpenError(
            f"{PRODUCT}'s zip has {len(csv_names)} members ending '.csv', "
            f"expected exactly one.",
            kind="parse",
        )
    return csv_names[0]


def _shard_uprn(product_dir: Path, shard_dir: Path, progress: ProgressSink) -> None:
    """Downloads OS Open UPRN's own national CSV zip whole into
    `<product_dir>/raw/`, then streams its sole `.csv` member straight
    into `write_uprn_shards` without ever materialising the member's own
    2,271,141,910 uncompressed bytes at once: `zipfile.ZipFile.open`
    inflates on demand as the wrapping `io.TextIOWrapper` is read line by
    line (`encoding="utf-8-sig"` handles the real file's own leading
    BOM), and `write_uprn_shards` itself writes each row straight to its
    own 100km square's gzip handle as it arrives (os_shards.py's own
    module docstring, "write_uprn_shards").

    Peak disk during this call is the raw zip (618,494,417 bytes, still
    on disk right up until the `finally` below) plus the gzipped shards
    it is being turned into (about 600 MB once the write completes, this
    product's own real gzip ratio for CSV rows this shape), a little
    under 1.2 GB together; the brief's own "peak disk ~1.3 GB" headline
    figure is this same total with headroom, not a second, independently
    measured number. Steady-state disk cost, once this call returns and
    the raw zip is gone, is that same ~600 MB of gzipped shards alone.

    The raw zip is deleted in a `finally`, on every path: a failed
    download or a mid-stream parse failure both leave `shard_dir`
    incomplete (`write_uprn_shards` guarantees no `meta.json` is written
    until every row has been placed; see os_shards.py's own module
    docstring), which is what makes the next `fetch()` call retry safely
    from nothing, so there is no reason to keep a raw zip around either
    way.
    """
    entries = product_downloads(PRODUCT)
    entry = entry_for(entries, area="GB", fmt="CSV")
    if entry is None:
        raise OsOpenError(
            f"The OS Data Hub downloads listing for {PRODUCT!r} has no "
            f"GB/CSV entry.",
            kind="parse",
        )
    raw_dir = product_dir / "raw"
    dest = raw_dir / safe_download_filename(entry)
    try:
        download_entry(entry, dest, progress=progress)
        with zipfile.ZipFile(dest) as archive:
            member_name = _sole_csv_member(archive.namelist())
            with archive.open(member_name) as member_stream:
                text_stream = io.TextIOWrapper(member_stream, encoding="utf-8-sig")
                write_uprn_shards(text_stream, shard_dir)
    except zipfile.BadZipFile as exc:
        raise OsOpenError(
            f"{PRODUCT}'s downloaded zip could not be read as a valid zip "
            f"file.",
            kind="parse",
        ) from exc
    except OsOpenError:
        # Final review, Important I2. `OsOpenError` subclasses `ValueError`
        # (os_downloads.py), and the `except ValueError` clause immediately
        # below this one was written ONLY for `write_uprn_shards`' own
        # header-shape check, which raises a plain `ValueError`. Without
        # this guard, that same clause also caught `download_entry`'s and
        # `_sole_csv_member`'s own `OsOpenError`s (a 503 mid-download,
        # say) and re-wrapped them as kind "parse", status_code None, with
        # a misleading "did not look like an OS Open UPRN file" message,
        # destroying the real kind/status a caller needs to decide whether
        # a retry is worth it (`fetch()`'s own `except OsOpenError`, and
        # package.py's retry pass beyond it). Re-raised unchanged, before
        # the broader `except ValueError` below ever gets a look at it:
        # the exact `except OsOpenError: raise` pattern commit 1e99afb
        # established for os_downloads.py itself, applied here to close
        # the same class of bug one module over.
        raise
    except ValueError as exc:
        # write_uprn_shards' own header-shape check (os_shards.py) raises
        # plain ValueError, not OsOpenError: the one place a caller of it
        # crosses into a module whose own exception vocabulary is
        # OsOpenError throughout (os_downloads.py's own "no-URL rule"
        # section documents that vocabulary; nothing in this message
        # carries a URL either way). Translated here rather than left to
        # escape as a different exception type than every other failure
        # this function can raise, which would otherwise force fetch()'s
        # own `except OsOpenError` to miss it.
        raise OsOpenError(
            f"{PRODUCT}'s downloaded CSV did not look like an OS Open "
            f"UPRN file: {exc}",
            kind="parse",
        ) from exc
    finally:
        dest.unlink(missing_ok=True)


def _write_work_part(shard_dir: Path, extent: tuple[float, float, float, float], work_dir: Path) -> Path:
    """`work_dir/os_uprn.csv`: every `(uprn, latitude, longitude)` row
    `uprn_in` reads out of `shard_dir` whose own easting/northing falls
    inside `extent`, one per line, a header row first. Written even when
    empty (legal: an extent with no rows in it, or outside Great Britain
    entirely, is real ground truth, not a bug, the same "nothing
    fabricated" convention os_open.py's own `_write_work_part`
    documents), and the ONLY thing `merge()` ever reads: `fetch()` and
    `merge()` may run in different processes.
    """
    e_min, n_min, e_max, n_max = extent
    lines = ["uprn,latitude,longitude"]
    for uprn, _easting, _northing, latitude, longitude in uprn_in(shard_dir, e_min, n_min, e_max, n_max):
        lines.append(f"{uprn},{latitude},{longitude}")
    part_path = work_dir / _WORK_PART_NAME
    atomic_write_text(part_path, "".join(line + "\n" for line in lines))
    return part_path


class OsUprnSource:
    id = "os_uprn"
    display_name = "Addresses (OS Open UPRN, GB)"
    licence = "Open Government Licence v3.0"
    attribution = "Contains OS data © Crown copyright and database right [year]"
    requires_api_key = False

    def __init__(
        self,
        session: object | None = None,
        ostn15_cache_dir: Path | None = None,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        # None means "the default, ~/.mapgen" (bng.py's own _cache_path);
        # a test or a caller that wants an isolated cache passes a tmp_path.
        self._ostn15_cache_dir = ostn15_cache_dir
        # Reset at the top of every fetch(); see sources/base.py's own
        # documentation of this optional LayerSource extension.
        self.tile_failures: list[TileFailure] = []
        # Final review, Important I1. Mirrors os_open.py's own
        # `versions_used` exactly, at this source's single-product grain
        # (`{"OpenUPRN": version}` once fetch() confirms the national
        # shard set complete under that version): package.py's
        # `_enrich_os_open_provenance` reads this to substitute
        # `attribution`'s own "[year]" placeholder. Not reset inside
        # fetch(), for the identical reason os_open.py's own comment
        # gives (package.py's retry pass calling fetch() again on this
        # same instance must not lose a prior pass's record); `configure()`
        # below is what gives each SEPARATE survey a clean start instead.
        self.versions_used: dict[str, str] = {}

    def configure(self) -> "OsUprnSource":
        """Returns a fresh OsUprnSource sharing this instance's transport
        and cache configuration, never mutating self. Mirrors
        `OsOpenSource.configure()`/`InspireSource.configure()` exactly,
        for the identical reason: no per-request selection to pass
        through, but `versions_used` needs a fresh lifetime per survey,
        not per fetch() call, and `register_default_sources()` builds and
        registers exactly one `OsUprnSource` for the life of the whole
        process.
        """
        return type(self)(session=self.session, ostn15_cache_dir=self._ostn15_cache_dir)

    # -- optional LayerSource extensions --------------------------------

    def routing_note(self) -> str | None:
        """A plain-English statement of the one-time national download
        this source's first real use will trigger, or `None` once a
        complete cache already answers for it.

        Read the same defensive, optional-extension way as
        `readiness_problem` (sources/base.py's own docstring): called
        only if present, zero arguments, by `estimate_survey`, which
        folds a non-`None` return into the estimate's own `warnings`
        list. Disk-only, matching `estimate()`'s own no-network rule
        below: a settings panel can call `/api/estimate` before a single
        survey has ever run.
        """
        if _any_complete_uprn_cache():
            return None
        return _ROUTING_NOTE

    def covers(self, bbox: BBox) -> str:
        """"full" when every 100 km square the padded extent touches is
        one OS Open UPRN's own national file actually covers (`GB_
        SQUARES`, os_shards.py, the same set `os_open.py`'s own `covers()`
        checks against: see this module's own docstring, "One national
        file", and `GB_SQUARES`'s own comment for why one probe of
        OpenMapLocal's per-square listing already answers for this
        source's national one too), "partial" when some are, "none"
        otherwise.

        Never touches the network, and never touches the (potentially
        619 MB, not-yet-downloaded) national shard cache either:
        `bng.best_effort_padded_bng_extent` reads a cached OSTN15 grid
        when one exists and falls back to a gridless projection
        otherwise, exactly like `os_open.py`'s own `covers()`; `squares_
        for` is pure arithmetic over the projected extent alone.
        """
        e_min, n_min, e_max, n_max = best_effort_padded_bng_extent(
            bbox, PAD_METRES, cache_dir=self._ostn15_cache_dir
        )
        squares = squares_for(e_min, n_min, e_max, n_max)
        if not squares:
            return "none"
        served = [square in GB_SQUARES for square in squares]
        if not any(served):
            return "none"
        if all(served):
            return "full"
        return "partial"

    def tier(self, category: str) -> int | None:
        """This source's own tier, mapgen.resolver's shared table:
        addresses only, at tier 1 (the one source in this project that
        serves it at all).
        """
        return 1 if category == "addresses" else None

    # -- detail (mapgen.resolver) ----------------------------------------------

    def detail(self, bbox: BBox) -> str:
        """The spec's own resolution copy, verbatim.

        Fixed regardless of bbox: resolve() only ever calls this on a
        source whose covers(bbox) was not "none" for the same bbox (see
        sources/base.py's own docstring on this optional extension), and
        UPRN's own one-point-per-address shape is a property of the
        dataset, not of which extent a given survey happens to draw, so
        there is no bbox-dependent answer to give and no covers() gate
        needed here either. Never touches the network, trivially:
        nothing here reads anything at all.
        """
        return "one point per addressable location"

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        """Bytes and seconds for the one-time national UPRN download,
        with no network at all.

        Needs neither `bbox` nor `tiles` nor an OSTN15 grid to answer
        honestly, unlike os_open.py's own per-square `estimate()`: this
        product's cost is a single, flat, national fact (see the module
        docstring's "One national file" section), not one that scales
        with which 100km squares an extent happens to touch. `SECONDS_
        FLOOR` alone once a complete cache already exists on disk (a
        later survey selecting this source pays nothing further for it);
        `UPRN_BYTES_ONE_TIME` at `BYTES_PER_SECOND_ESTIMATE` otherwise.
        """
        bytes_estimate = 0 if _any_complete_uprn_cache() else UPRN_BYTES_ONE_TIME
        seconds_estimate = max(bytes_estimate / BYTES_PER_SECOND_ESTIMATE, SECONDS_FLOOR)
        return Estimate(bytes_estimate=bytes_estimate, seconds_estimate=seconds_estimate)

    # -- fetch -----------------------------------------------------------------

    def _resolve_version(self, progress: ProgressSink) -> str:
        """`PRODUCT`'s version to shard under: the live `product_
        version()` answer, ordinarily, or the newest cached version
        directory whose national shard set is already complete when the
        listing itself is unreachable.

        Mirrors os_open.py's own `_resolve_version` exactly, at this
        source's single-product grain: only a "listing"-kind failure
        falls back (a "parse"-kind failure is a fact about the service's
        own response shape, not about reachability, and propagates
        unchanged); raises the original `OsOpenError` unchanged when no
        cached version is complete either.
        """
        try:
            return product_version(PRODUCT)
        except OsOpenError as exc:
            if exc.kind != "listing":
                raise
            fallback = _latest_complete_uprn_version()
            if fallback is None:
                raise
            progress.emit("os_uprn_cache_fallback", source=self.id, version=fallback)
            return fallback

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        """Ensures OSTN15, places this extent on the National Grid,
        ensures the national UPRN shard cache is complete for the
        resolved version (downloading and sharding it once, the only
        time this ever costs real bytes), and writes one bbox-filtered
        `work_dir/os_uprn.csv` work part.

        The shard cache is ensured UNCONDITIONALLY, before this extent's
        own bbox is used for anything beyond validating it can be placed
        on the grid at all: see the module docstring's "Ensuring the
        cache does not depend on this survey's own bbox" section for why
        an extent outside Great Britain still gets the national cache
        built (for every LATER survey's benefit) rather than being
        skipped.

        An extent OSTN15 cannot place at all is refused before any
        download is attempted (`OsUprnSourceError`, `NO_HONEST_EXTENT_
        MESSAGE`): there is nothing to filter the resulting shard cache
        down to.

        `cancel` is checked between whole units of paid-for work (before
        the OSTN15 fetch, before the extent is computed, before the
        national download/shard step, before the work part is written),
        never mid-download: a unit already in flight always finishes,
        matching sources/base.py's own LayerSource docstring.
        """
        self.tile_failures = []
        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            grid = ensure_ostn15(cache_dir=self._ostn15_cache_dir, session=self.session)
        except requests.RequestException as exc:
            kind, phrase = classify_transport_failure(exc)
            self._record_tile_failures(
                tiles, kind,
                f"Failed to download the OSTN15 shift grid needed to place "
                f"this extent on the National Grid: {phrase}.",
            )
            raise
        except BngError:
            self._record_tile_failures(
                tiles, FAILURE_UNREACHABLE,
                "Failed to download the OSTN15 shift grid needed to place "
                "this extent on the National Grid.",
            )
            raise

        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            extent = padded_bng_extent(bbox, grid, PAD_METRES)
        except BngError:
            self._record_tile_failures(tiles, FAILURE_NO_OUTPUT, NO_HONEST_EXTENT_MESSAGE)
            raise OsUprnSourceError(NO_HONEST_EXTENT_MESSAGE) from None

        try:
            version = self._resolve_version(progress)
        except OsOpenError as exc:
            self._record_tile_failures(tiles, _classify_os_open_error(exc), str(exc))
            raise

        if cancel is not None:
            cancel.raise_if_cancelled()

        product_dir = product_cache_dir(PRODUCT, version)
        shard_dir = product_dir / "shards"

        if shards_complete(shard_dir):
            for tile in tiles:
                progress.emit("tile_skipped", source=self.id, tile_id=tile.tile_id)
        else:
            try:
                _shard_uprn(product_dir, shard_dir, progress)
            except OsOpenError as exc:
                self._record_tile_failures(tiles, _classify_os_open_error(exc), str(exc))
                raise
            for tile in tiles:
                progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)

        self.versions_used[PRODUCT] = version
        sweep_old_versions(PRODUCT, version)

        if cancel is not None:
            cancel.raise_if_cancelled()

        part_path = _write_work_part(shard_dir, extent, work_dir)
        return [part_path]

    def _record_tile_failures(self, tiles: Sequence[Tile], kind: str, reason: str) -> None:
        """Record why this fetch() could not deliver AT ALL, once per
        tile: the whole-extent refusals (OSTN15 itself, an unplaceable
        extent, or the national listing/download failing), which arrive
        before this source's single unit of work is even attempted.
        Matches os_open.py's own `_record_tile_failures` exactly.
        """
        self.tile_failures = [
            TileFailure(source=self.id, tile_id=tile.tile_id, kind=kind, reason=reason)
            for tile in tiles
        ]

    # -- merge -----------------------------------------------------------------

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Reads fetch()'s own work part BY NAME (never by position, the
        `ElevationSource.merge` lesson every source in this package
        follows), and writes `<stem>_os_uprn.geojson`: one Point feature
        per row, `{"uprn": int, "source": "os_open_uprn"}`.

        Geometry comes straight from the work part's own `latitude`/
        `longitude` columns, in GeoJSON's `[lon, lat]` order: see the
        module docstring's "Merge needs no OSTN15 at all" section for why
        these are used directly rather than re-derived from easting/
        northing through `bng.from_bng` a second time (OS's own columns
        are already WGS84 and authoritative; re-deriving risks a
        transform disagreement between two independently-computed
        answers for the same point, for no benefit).

        Zero rows (the work part missing, or present but empty, both
        legal: see `_write_work_part`'s own docstring) writes no file at
        all, and removes a stale one from an earlier attempt, mirroring
        os_open.py's own per-bucket "no features, no file" rule.
        """
        out_dir = Path(out_dir)
        part_path = next((p for p in parts if p.name == _WORK_PART_NAME), None)

        features: list[dict] = []
        if part_path is not None:
            lines = part_path.read_text(encoding="utf-8").splitlines()
            for line in lines[1:]:  # lines[0] is the header row.
                if not line.strip():
                    continue
                uprn_text, lat_text, lon_text = line.split(",")
                features.append(
                    {
                        "type": "Feature",
                        "properties": {"uprn": int(uprn_text), "source": "os_open_uprn"},
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(lon_text), float(lat_text)],
                        },
                    }
                )

        output_path = out_dir / f"{stem}_os_uprn.geojson"
        if not features:
            output_path.unlink(missing_ok=True)
            return []
        payload = {"type": "FeatureCollection", "features": features}
        atomic_write_bytes(output_path, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return [output_path]

    def possible_outputs(self, stem: str) -> list[str]:
        """The one root file merge() could ever write for this stem.
        Read by package.py's stale-output sweep; see sources/base.py."""
        return [f"{stem}_os_uprn.geojson"]
