"""OsOpenSource: OS Open map data (GB) as a LayerSource.

Ties together three modules built earlier in this same plan: os_downloads.py
(the OS Data Hub Downloads API client, its versioned cache directories, and
the ranged zip reader for OpenRoads' national zip), os_gml.py (streaming
feature readers for the three products' GML), and os_shards.py (the
derive-once, gzipped NDJSON shard store keyed by 100km square / 10km cell).
Read all three end to end before this module, and read sources/inspire.py
end to end too: its estimate/fetch/merge shape, session seam, progress
events, tile_failures handling and configure() docstrings are this module's
structural model.

## Three products, one source

`PRODUCTS = ("OpenMapLocal", "OpenRoads", "OpenGreenspace")`. All three are
tiled by the OS National Grid's 100km squares; OpenMapLocal and
OpenGreenspace publish one small zip per square, while OpenRoads publishes
one 608 MB national zip whose members are per-square GML files, read
through `os_downloads.ZipReader` over `cog.py`'s `HttpByteSource` so only
the needed squares' bytes are ever pulled across the wire (see
`_openroads_byte_source`'s own docstring for why no redirect resolution is
needed for that).

## Cache layout

Under `os_downloads.cache_root()` (`~/.mapgen/osopen`), each product/version
gets `os_downloads.product_cache_dir(product, version)`, and under THAT:

- `raw/`: temporary downloaded zips (OpenMapLocal, OpenGreenspace) or
  temporary spooled per-square GML files (OpenRoads), deleted the moment
  the square's own shard write finishes, success or failure alike.
- `shards/<SQ>/`: one `os_shards.write_shards`-shaped directory per 100km
  square, meta.json-last, exactly the completeness discipline os_shards.py's
  own module docstring documents.

## Per-(product, square) failure is that UNIT's failure

A mid-stream parse failure (os_gml.py's readers fail the WHOLE stream on
the first malformed feature) or a download failure while sharding one
(product, square) unit is recorded against that unit alone: `write_shards`
already guarantees the shard directory is left incomplete (no meta.json)
when its own feature iterator raises partway through, which is exactly
what makes retrying that one square safe by design on the next fetch() call
(`shards_complete` reports it as incomplete, so nothing trusts a
half-written directory). fetch() records the failure and continues to the
next (product, square) unit, raising once at the end, matching
`sources/base.py`'s documented convention for a tiled source and mirroring
`OvertureSource`'s own continue-past-failure shape (its per-type pool) more
closely than InspireSource's own fail-fast-per-authority shape, since this
source's many independent (product, square) units are the closer analogue
to Overture's independent per-type downloads.

## Work parts: the fetch/merge handoff

fetch() writes exactly one `work_dir/os_open_<product>.ndjson` per product
that finished with every needed square shard-complete, holding the
bbox-filtered features `os_shards.features_in` reads back out of that
product's own shard directories. merge() reads ONLY these parts, never the
shard cache directly: fetch() and merge() may run in different processes,
and the parts are the complete handoff between them. Zero features for a
product over the extent is legal (a genuinely empty area, or the
documented zero-yield limitation os_gml.py's own module docstring names for
a wrong-reader-for-product mismatch, neither of which this source can tell
apart from "there is really nothing here"): an empty parts file is written,
merge() writes no output file for that product's own categories, and
nothing raises.

## Attribution: [year] stays a placeholder here

The class attribute `attribution` keeps the literal `"[year]"` OS's own
licence text uses; merge() does NOT substitute it, mirroring
`InspireSource.merge` exactly (that source's own `[year]` substitution
happens in package.py's fusion step, not inside merge() itself). The
version year for each product this run actually used is recorded nowhere
but the cache directory name (`product_cache_dir`'s own
`f"{product}_{version}"`): this task deliberately does not add a
speculative attribute for a future package.py enrichment step to read,
following the same restraint `InspireSource.conditions_url`'s own comment
states for `_source_provenance` ("deliberately does not extend... to read
an attribute nothing yet consumes").
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Mapping, Sequence

import requests

from mapgen.bng import (
    BngError,
    best_effort_padded_bng_extent,
    ensure_ostn15,
    from_bng,
    load_ostn15,
    padded_bng_extent,
)
from mapgen.cog import HttpByteSource
from mapgen.egrid import PAD_METRES
from mapgen.fsutil import atomic_write_bytes, atomic_write_text, ensure_dir
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken
from mapgen.os_downloads import (
    OsOpenError,
    ZipReader,
    cache_root,
    download_entry,
    entry_for,
    product_cache_dir,
    product_downloads,
    product_version,
    sweep_old_versions,
)
from mapgen.os_gml import iter_greenspace_features, iter_oml_features, iter_road_features
from mapgen.os_shards import GB_SQUARES, features_in, shards_complete, squares_for, write_shards
from mapgen.sources.base import (
    FAILURE_NO_OUTPUT,
    FAILURE_UNKNOWN,
    FAILURE_UNREACHABLE,
    RETRYABLE_FAILURE_KINDS,
    Estimate,
    ProgressSink,
    TileFailure,
    classify_status_failure,
    classify_transport_failure,
)

PRODUCTS = ("OpenMapLocal", "OpenRoads", "OpenGreenspace")

# --------------------------------------------------------------------------
# Estimate constants. Each dated: see this task's own report for the live
# measurements behind them, and every sibling source's own comment style
# (lidar_wales.py, sources/inspire.py) for why a constant like this always
# carries margin over the single figure actually measured, not equality
# with it.
# --------------------------------------------------------------------------

# SS zip 48.9 MB, ST zip 121.5 MB, both measured live 2026-08-07 (this
# task's own probe). National squares vary and only two of them have
# actually been measured; thin evidence in the same sense every other
# per-square/per-authority constant in this project's sources/ package
# carries the same caveat for (see InspireSource.BYTES_PER_AUTHORITY).
#
# Review finding (Minor 3, 2026-08-07): the first cut of this constant,
# 120,000,000, sat about 1.25% BELOW the one real ST figure in the comment
# above it, the opposite of "carries margin over the single figure actually
# measured", the convention this whole block states and every sibling
# constant here and in inspire.py/lidar_wales.py follows. 130,000,000 sits
# a genuine ~7% above the measured 121.5 MB instead of a rounding error
# below it.
OML_BYTES_PER_SQUARE = 130_000_000

# The ST member's own uncompressed size (428.6 MB, os_downloads.py's own
# module docstring) at the HP square's own measured deflate ratio (0.082,
# same docstring) comes to about 35 MB; headroom added on top of that
# rather than pinning the single computed figure exactly.
ROADS_BYTES_PER_SQUARE = 40_000_000

# SS zip 0.6 MB, ST zip 2.2 MB (os_downloads.py's own module docstring
# names the same two figures for the per-square Greenspace zips).
GREENSPACE_BYTES_PER_SQUARE = 3_000_000

# Review finding (Important 1, 2026-08-07): the first cut of this constant
# reused InspireSource.BYTES_PER_SECOND_ESTIMATE verbatim (1,800,000.0), a
# figure measured against a DIFFERENT service (HM Land Registry, not the OS
# Data Hub) and never checked against this task's own live numbers, which
# contradict it. This task's own live probe (task-4-report.md, the
# Cowbridge run) measured, from clean download_progress totals:
#
#   OpenMapLocal:    166,818,477 bytes / 112.557121 s  ~ 1,482,000 bytes/s
#   OpenGreenspace:    2,830,794 bytes /   3.114896 s  ~   908,793 bytes/s
#   aggregate (all three products): 213,681,893 bytes / 153.90 s ~ 1,388,000 bytes/s
#
# (OpenRoads' own ~1,150,000 bytes/s figure is excluded: the live test's own
# comment flags it as possibly including a one-time, unrelated OSTN15 pack
# download on a cold cache, so it is not a clean OS Data Hub measurement.)
#
# This term prices a LARGE transfer's time (bytes_estimate / this rate), so
# the safe choice is a rate genuinely AT OR BELOW the slowest CLEAN rate now
# on record, not an average of the three and not that slowest rate's own
# rounded value (the same rule inspire.py's own BYTES_PER_SECOND_ESTIMATE
# history states and the reused 1,800,000.0 figure violated against these
# numbers: it sat 50% ABOVE the slowest measured rate, the wrong side of
# it). OpenGreenspace's own 908,793 bytes/s is the slowest, plausibly BECAUSE
# it is the smallest transfer (fixed per-request overhead, TLS handshake and
# all, dominates a 2.8 MB download far more than a 167 MB one), not because
# the link itself is slower for that product specifically; that does not
# change which figure this constant must clear, since a survey's own
# smallest OS Open transfer is exactly the case this rate has to price
# honestly. 850,000.0 sits a genuine ~6.5% below 908,793, two significant
# figures, the same margin-not-equality shape every other thin-evidence rate
# constant in this project's sources/ package uses. One machine, one link,
# one day, three products: Task 9's own full-pipeline run re-verifies this
# rather than trusting a second, still-thin sample.
BYTES_PER_SECOND_ESTIMATE = 850_000.0

# Placeholder until Task 9 refits this from a live run's own measured wall
# time (this task's own Step 9 live test measures real numbers and prints
# them for that refit, but does not feed them back into this constant
# itself; see the task report for what was actually measured).
SECONDS_FLOOR = 3.0

# The OSTN15 developers pack itself: bng.py's own module docstring names it
# "a 41 MB CSV of one row per node" (OSTN15_URL). lidar_wales.py's own
# estimate() has no equivalent constant to import: it falls back to an
# AREA-only approximation when no grid is cached (extent_metres), since it
# never needs to know WHICH grid squares an extent touches, only how big it
# is, so there is nothing there to reuse. Mirrored here from bng.py's own
# documented pack size instead of a fresh, independent guess.
OSTN15_BYTES = 41_000_000

# No honest answer exists for a padded extent that touches no 100km square
# at all (open sea beyond the National Grid's own envelope, or a location
# outside Great Britain entirely): OS Open products simply do not exist out
# there, the same fact InspireSource's own OUTSIDE_ENGLAND_AND_WALES_MESSAGE
# states for its own, narrower, England-and-Wales-only coverage.
NO_GB_SQUARES_MESSAGE = (
    "OS Open products cover Great Britain only, and this extent's padded "
    "National Grid extent touches no 100km square."
)

_BYTES_PER_SQUARE = {
    "OpenMapLocal": OML_BYTES_PER_SQUARE,
    "OpenRoads": ROADS_BYTES_PER_SQUARE,
    "OpenGreenspace": GREENSPACE_BYTES_PER_SQUARE,
}

# OpenMapLocal and OpenGreenspace share the same "download the whole square
# zip, open it with zipfile, read the sole .gml member" shape; only which
# os_gml.py reader to run over that member differs. OpenRoads is handled
# separately in fetch() itself (a ranged read over one shared national
# zip, never a whole-zip download).
_ZIP_DOWNLOAD_READERS = {
    "OpenMapLocal": iter_oml_features,
    "OpenGreenspace": iter_greenspace_features,
}


class OsOpenSourceError(RuntimeError):
    """Raised when this extent has no OS Open data to give it at all
    (wholly outside the National Grid's own usable envelope) or when merge()
    is asked to run with no OSTN15 grid cached.

    A source-level refusal, distinct from `os_downloads.OsOpenError` (that
    module's own transport/parse-layer exception): mirrors
    `LidarWalesError`/`InspireError`, both of which keep their own
    source-level refusal separate from the lower-level exception their
    fetch() unwraps and re-raises unchanged for every other failure.
    """


def _squares_for_estimate(bbox: BBox, grid) -> list[str] | None:
    """The 100km squares estimate() would need to price, or `None` when no
    OSTN15 grid is cached and there is therefore no honest way yet to say
    which squares this extent even touches (see `estimate()`'s own
    docstring for why "assume one square" is what `None` means to that
    caller, rather than "assume none").

    An empty list (as opposed to `None`) is a real, different answer: the
    extent projects fine but touches no 100km square at all (open sea past
    the National Grid's own envelope), which is genuinely zero squares to
    price, not "unknown, so guess a default".
    """
    if grid is None:
        return None
    try:
        e_min, n_min, e_max, n_max = padded_bng_extent(bbox, grid, PAD_METRES)
    except BngError:
        return []
    return squares_for(e_min, n_min, e_max, n_max)


def _any_cached_square(product: str, square: str) -> bool:
    """True if ANY version directory this product has ever cached under
    `cache_root()` holds a shard-complete directory for `square`, checked
    entirely on disk (a glob plus `shards_complete`, never the network).

    Deliberately not scoped to "the current version": estimate() must never
    touch the network to find out what the current version even is (see its
    own docstring), so "cached" here means "this square's data is sitting
    on disk under SOME version mapgen has fetched before", which fetch()
    itself will reconcile against whatever the current version turns out to
    be when it actually runs.

    The directory-name split mirrors `os_downloads.sweep_old_versions`'s own
    `rsplit("_", 1)` rule exactly, for the same reason that function's own
    docstring gives: a product id that is itself a prefix of another
    product's id (`"Open"` inside `"Open_Extra"`) must never cross-match.
    """
    root = cache_root()
    if not root.exists():
        return False
    for candidate in root.iterdir():
        if not candidate.is_dir():
            continue
        base, separator, _version = candidate.name.rpartition("_")
        if not separator or base != product:
            continue
        if shards_complete(candidate / "shards" / square):
            return True
    return False


def _latest_complete_version_for_squares(product: str, squares: Sequence[str]) -> str | None:
    """The newest version directory (by lexicographic version string,
    which sorts correctly for OS Open's own `"YYYY-MM"` shape) under which
    EVERY one of `squares` already has a shard-complete directory, or
    `None` if no single version directory covers all of them.

    Disk-only, like `_any_cached_square`, but answers a stricter question:
    fetch()'s own cache-fallback path (see its docstring, "listing
    unreachable") needs ONE coherent version to shard the rest of this run
    under, not a patchwork of several different versions' own squares.
    """
    root = cache_root()
    if not root.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        base, separator, version = entry.name.rpartition("_")
        if not separator or base != product:
            continue
        candidates.append((version, entry))
    for version, entry in sorted(candidates, reverse=True):
        if all(shards_complete(entry / "shards" / square) for square in squares):
            return version
    return None


def _sole_gml_member(names: Sequence[str], product: str, square: str) -> str:
    """The one zip member ending `.gml`, or `OsOpenError` kind "parse"
    naming the product and square (never a path guessed from a pattern
    this module has not actually verified for every real zip): OS Open's
    own per-square zips are not committed anywhere this project can probe
    their internal path structure from ahead of time, so the member is
    found by asking the zip itself, the same way a caller would open it
    by hand.
    """
    gml_names = [name for name in names if name.endswith(".gml")]
    if len(gml_names) != 1:
        raise OsOpenError(
            f"{product} {square}'s zip has {len(gml_names)} members ending "
            f"'.gml', expected exactly one.",
            kind="parse",
        )
    return gml_names[0]


def _openroads_byte_source(entry: Mapping[str, object], session: object) -> HttpByteSource:
    """`HttpByteSource` over OpenRoads' own national GML zip entry.

    `entry["url"]` is handed to `HttpByteSource` directly, with no manual
    redirect resolution: `HttpByteSource.read` (cog.py) calls
    `session.get(...)` with none of `requests.Session.get`'s own
    redirect-following defaults overridden, so the 302 to Azure blob
    storage this project's own probe found (see the module docstring) is
    already followed transparently, Range header and all, on every
    request `HttpByteSource` makes. Verified directly, not merely assumed:
    `test_os_downloads.py`'s own live test
    (`test_live_downloads_listing_and_zipreader_over_the_real_openroads_gml`)
    reads a real member straight off this exact construction with no
    separate redirect handling anywhere in this project, and its own
    docstring records the probe.

    No URL crosses into an error message either way, regardless of
    redirects: `HttpByteSource`'s own `.name` is always the last URL path
    segment (cog.py), never the full URL, and every `CogError`/
    `OsOpenError` it or `ZipReader` raise is composed from that plus a
    fixed sentence, never `str(exc)` on a caught transport exception (see
    os_downloads.py's own module docstring, "the no-URL rule").
    """
    return HttpByteSource(entry["url"], session=session)


def _shard_zip_download_square(
    product: str,
    entries: list[dict],
    square: str,
    shard_dir: Path,
    product_dir: Path,
    progress: ProgressSink,
) -> None:
    """OpenMapLocal/OpenGreenspace: download `square`'s own zip whole,
    open it with `zipfile`, and stream its sole `.gml` member into
    `write_shards`.

    The raw zip is deleted in a `finally`, on every path: a failed
    download or a mid-stream parse failure both leave `shard_dir`
    incomplete (`write_shards` guarantees that; see the module docstring),
    which is what makes the next fetch() call retry this square safely
    from nothing, so there is no reason to keep a raw zip around either
    way.
    """
    entry = entry_for(entries, area=square, fmt="GML")
    if entry is None:
        raise OsOpenError(
            f"The OS Data Hub downloads listing for {product!r} has no "
            f"{square}/GML entry.",
            kind="parse",
        )
    raw_dir = product_dir / "raw"
    dest = raw_dir / (entry.get("fileName") or f"{product}_{square}.zip")
    try:
        download_entry(entry, dest, progress=progress)
        with zipfile.ZipFile(dest) as archive:
            member_name = _sole_gml_member(archive.namelist(), product, square)
            reader = _ZIP_DOWNLOAD_READERS[product]
            with archive.open(member_name) as member_stream:
                write_shards(reader(member_stream), shard_dir)
    except zipfile.BadZipFile as exc:
        raise OsOpenError(
            f"{product} {square}'s downloaded zip could not be read as a "
            f"valid zip file.",
            kind="parse",
        ) from exc
    finally:
        dest.unlink(missing_ok=True)


def _shard_openroads_square(zip_reader: ZipReader, square: str, shard_dir: Path, product_dir: Path) -> None:
    """OpenRoads: pull `data/OSOpenRoads_<SQ>.gml` out of the shared
    ranged `zip_reader`, spool it to a temp file under `<cache>/raw/`
    (never held whole in memory: the ST member alone is 428.6 MB
    uncompressed), and stream that file into `write_shards`.

    The temp file is deleted in a `finally`, the same "no reason to keep
    it either way" logic `_shard_zip_download_square` follows.
    """
    raw_dir = product_dir / "raw"
    ensure_dir(raw_dir)
    temp_path = raw_dir / f"OSOpenRoads_{square}.gml"
    member_name = f"data/OSOpenRoads_{square}.gml"
    try:
        data = zip_reader.read_member(member_name)
        temp_path.write_bytes(data)
        with temp_path.open("rb") as handle:
            write_shards(iter_road_features(handle), shard_dir)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_work_part(
    product: str,
    product_dir: Path,
    needed_squares: Sequence[str],
    extent: tuple[float, float, float, float],
    work_dir: Path,
) -> Path:
    """`work_dir/os_open_<product>.ndjson`: every feature `features_in`
    reads out of this product's own needed squares' shard directories,
    bbox-filtered, deduplicated by id ACROSS squares (a feature is
    already deduplicated across CELLS within one square by `features_in`
    itself; this is the same guard one level up, in case a feature ever
    turns out to be shard-written under two different squares' own zips).

    Written even when empty (`"".join(())`  is `""`): zero features for a
    product over this extent is legal, and an empty parts file is exactly
    how merge() learns that honestly, rather than the file's absence
    being ambiguous with "this product never even ran".
    """
    e_min, n_min, e_max, n_max = extent
    seen_ids: set[str] = set()
    lines: list[str] = []
    for square in needed_squares:
        shard_dir = product_dir / "shards" / square
        for record in features_in(shard_dir, e_min, n_min, e_max, n_max):
            feature_id = record["id"]
            if feature_id != "":
                if feature_id in seen_ids:
                    continue
                seen_ids.add(feature_id)
            lines.append(json.dumps(record, separators=(",", ":")))
    part_path = work_dir / f"os_open_{product}.ndjson"
    atomic_write_text(part_path, "".join(line + "\n" for line in lines))
    return part_path


def _classify_os_open_error(exc: OsOpenError) -> str:
    """The shared failure vocabulary's term (sources/base.py) for one
    (product, square) unit's own `OsOpenError`.

    `.status_code`, when set (a non-2xx response `os_downloads.py` itself
    saw), goes straight to `classify_status_failure`, the same structural
    precedent `CogError.status_code`/`InspireError.status_code` already
    establish. Without one, `os_downloads.py` never chains the original
    transport exception (every one of its own raises uses `from None`; see
    its module docstring), so there is nothing to read `classify_
    transport_failure` off: a "listing"/"download" failure with no status
    is treated as FAILURE_UNREACHABLE, the same generic bucket lidar_wales.py
    and inspire.py both fall back to for an equally under-specified
    transport failure. A "range" or "parse" failure with no status is a
    read/parse problem, not a transport one, and is FAILURE_UNKNOWN, the
    same as inspire.py's own parse-failure classification.
    """
    if exc.status_code is not None:
        kind, _phrase = classify_status_failure(exc.status_code)
        return kind
    if exc.kind in ("listing", "download"):
        return FAILURE_UNREACHABLE
    return FAILURE_UNKNOWN


def _combined_kind(failures: Mapping[tuple[str, str], OsOpenError]) -> str:
    """One kind for however many (product, square) units failed in one
    fetch() call: retryable if ANY of them is (the same direction
    OvertureSource's own `_combined_kind` picks, and for the same reason
    given there: a retry re-runs fetch(), which skips every unit already
    shard-complete, so what it repeats is exactly the set that failed, and
    a genuinely retryable failure sitting beside an unretryable one should
    not lose its own chance at recovery).
    """
    kinds = [_classify_os_open_error(exc) for exc in failures.values()]
    for kind in kinds:
        if kind in RETRYABLE_FAILURE_KINDS:
            return kind
    return kinds[0] if kinds else FAILURE_UNKNOWN


def _tile_failures_for(
    source_id: str, tiles: Sequence[Tile], failures: Mapping[tuple[str, str], OsOpenError]
) -> list[TileFailure]:
    """One record per tile, naming every failed (product, square) unit and
    why: `OsOpenError`'s own message is already guaranteed free of URLs
    (os_downloads.py's own "no-URL rule"), so it is safe to embed directly,
    unlike OvertureSource's own `_failure_phrase`, which has to guard
    against the overturemaps CLI's own, unconstrained stderr text.
    """
    reason = " ".join(f"{product} {square}: {exc}" for (product, square), exc in failures.items())
    kind = _combined_kind(failures)
    return [
        TileFailure(source=source_id, tile_id=tile.tile_id, kind=kind, reason=reason)
        for tile in tiles
    ]


def _combined_failure(failures: Mapping[tuple[str, str], OsOpenError]) -> OsOpenError:
    """One exception for however many (product, square) units failed.

    A single failure is re-raised exactly as it came out (matching
    OvertureSource's own `_combined_failure`); more than one is summarised
    naming every failed unit, since which of several units failed matters
    to the owner reading this exception directly (a CLI run, or a test),
    just as it does in `_tile_failures_for`'s own record.
    """
    if len(failures) == 1:
        return next(iter(failures.values()))
    names = ", ".join(f"{product}:{square}" for product, square in failures)
    detail = "; ".join(f"{product} {square}: {exc}" for (product, square), exc in failures.items())
    return OsOpenError(
        f"OS Open failed for {len(failures)} (product, square) units ({names}). {detail}",
        kind=next(iter(failures.values())).kind,
    )


# --------------------------------------------------------------------------
# merge(): the work-part -> six-file dispatch.
#
# Every property builder below follows one rule throughout: copy a fixed,
# named set of keys straight out of the feature's own os_gml-produced
# properties, dropping any whose value is None (Task 2's own convention for
# "this field was not present on this feature"; see os_gml.py's module
# docstring, "absent means None"). A boolean False is not None and is
# always kept, which is what makes RoadLink's trunk/primary need no special
# case here at all. Nothing here re-types or re-derives a value os_gml.py
# did not already produce (the brief's own "do not re-type beyond what
# os_gml produced").
# --------------------------------------------------------------------------

# feature_type -> which of the six output categories it belongs to. Every
# key here is one of OML_TYPES' own keys (os_gml.py) except "Glasshouse",
# which the brief's own os_buildings.geojson property map names but Task 2's
# OML_TYPES deliberately never reads (three real features across a whole
# 100km square; see os_gml.py's own module docstring and this task's
# report): kept here anyway, harmlessly unreachable, so a future Task 2
# widening its own scope to include Glasshouse needs no change here to
# start working.
_BUILDING_TYPES = frozenset({"Building", "ImportantBuilding", "Glasshouse"})
_RAIL_TYPES = frozenset({"RailwayTrack", "RailwayTunnel"})
_SITE_TYPES = frozenset({"FunctionalSite", "RailwayStation", "NamedPlace"})
_LAND_KIND_BY_TYPE = {
    "Woodland": "woodland",
    "SurfaceWater_Area": "water_area",
    "SurfaceWater_Line": "water_line",
    "TidalWater": "tidal_water",
    "Foreshore": "foreshore",
}

_OUTPUT_SUFFIXES = ("buildings", "roads", "rail", "greenspace", "sites", "land")


def _copy_present(raw: Mapping[str, object], keys: Sequence[str]) -> dict[str, object]:
    """`raw`'s own values for `keys`, omitting any key whose value is
    `None` (absent or explicitly null; os_gml.py never distinguishes the
    two). A value of `False`, `0` or `""` is a real value and is kept.
    """
    return {key: raw[key] for key in keys if raw.get(key) is not None}


def _building_properties(raw: Mapping[str, object]) -> dict[str, object]:
    return {"source": "os_openmap_local", **_copy_present(raw, ("code", "theme", "class"))}


def _rail_properties(raw: Mapping[str, object]) -> dict[str, object]:
    # Only "class", per the brief's own exact os_rail.geojson property map:
    # RailwayTunnel's own os_gml.py fallback ({"code": featureCode} when it
    # carries no classification, see OML_TYPES's own docstring) is real
    # data, but this output's closed property list does not name "code",
    # so a classless tunnel merges with "source" alone.
    return {"source": "os_openmap_local", **_copy_present(raw, ("class",))}


def _site_properties(raw: Mapping[str, object]) -> dict[str, object]:
    return {"source": "os_openmap_local", **_copy_present(raw, ("theme", "class", "name"))}


def _land_properties(feature_type: str) -> dict[str, object]:
    return {"source": "os_openmap_local", "kind": _LAND_KIND_BY_TYPE[feature_type]}


def _road_properties(raw: Mapping[str, object]) -> dict[str, object]:
    # "length" is real os_gml.py output but is not in the brief's own
    # exact os_roads.geojson property list, so it is not copied here.
    return {
        "source": "os_open_roads",
        **_copy_present(raw, ("class", "function", "form", "name", "number", "trunk", "primary")),
    }


def _greenspace_properties(raw: Mapping[str, object]) -> dict[str, object]:
    # GreenspaceSite carries "function"/"name"; AccessPoint carries
    # "access" (and "site", not in the brief's own property list and so
    # not copied). One shared copy list handles both without a per-type
    # branch: whichever key a feature does not carry is simply absent from
    # its own raw properties and _copy_present drops it the same way.
    return {"source": "os_open_greenspace", **_copy_present(raw, ("function", "access", "name"))}


def _oml_bucket_and_properties(feature_type: str, raw: Mapping[str, object]) -> tuple[str, dict] | None:
    if feature_type in _BUILDING_TYPES:
        return "buildings", _building_properties(raw)
    if feature_type in _RAIL_TYPES:
        return "rail", _rail_properties(raw)
    if feature_type in _SITE_TYPES:
        return "sites", _site_properties(raw)
    if feature_type in _LAND_KIND_BY_TYPE:
        return "land", _land_properties(feature_type)
    return None


def _read_ndjson(path: Path):
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip():
            yield json.loads(line)


def _reproject_geometry(geometry: Mapping[str, object], grid) -> dict[str, object]:
    """`geometry`'s own BNG coordinates, unprojected to WGS84 via
    `from_bng`, in GeoJSON's own `[lon, lat]` order (see `bng.from_bng`'s
    own docstring for the order it returns latitude/longitude in, which is
    the opposite way round).

    One recursive walk handles Point (a flat `[e, n]` pair), LineString (a
    list of pairs), Polygon (a list of rings) and MultiPolygon (a list of
    ring-lists) alike, the same trick `os_shards._iter_coordinate_pairs`
    already uses for the same reason: GeoJSON coordinates nest arbitrarily
    deep with numbers only ever at the leaves, regardless of which of the
    four shapes it is.
    """
    def convert(coordinates):
        if coordinates and isinstance(coordinates[0], (int, float)):
            easting, northing = coordinates[0], coordinates[1]
            latitude, longitude = from_bng(easting, northing, grid)
            return [longitude, latitude]
        return [convert(item) for item in coordinates]

    return {"type": geometry["type"], "coordinates": convert(geometry["coordinates"])}


class OsOpenSource:
    id = "os_open"
    display_name = "OS Open map data (GB)"
    licence = "Open Government Licence v3.0"
    attribution = "Contains OS data © Crown copyright and database right [year]"
    requires_api_key = False
    PRODUCTS = PRODUCTS

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

    # category -> tier, this source's own row of mapgen.resolver's shared
    # table (the phase 2 spec's tier tables).
    _TIERS = {
        "buildings": 3,
        "roads": 2,
        "rail": 2,
        "greenspace": 1,
        "sites": 1,
        "land": 1,
        "water": 2,
        "places": 2,
    }

    # -- covers / tier (mapgen.resolver) --------------------------------------

    def covers(self, bbox: BBox) -> str:
        """"full" when every 100 km square the padded extent touches is
        one OS Open's own per-square products actually serve (`GB_
        SQUARES`, os_shards.py), "partial" when some are, "none" when the
        extent touches no square OS Open serves at all (including
        touching no square on the National Grid whatsoever).

        Never touches the network: `bng.best_effort_padded_bng_extent`
        reads a cached OSTN15 grid when one exists and falls back to a
        gridless projection otherwise, exactly like `lidar_wales.py`'s own
        `covers()`; `squares_for` is pure arithmetic. `squares_for` can
        return real, arithmetically valid two-letter codes for a rectangle
        that is technically inside the National Grid's own 700 km by
        1300 km envelope but genuinely over open sea, past every coastline
        OS actually publishes for (the letter grid is denser than Great
        Britain's own coastline): a square OS never served is treated as
        no coverage there at all, the same as a square outside the
        envelope altogether, which is why "none of the touched squares are
        served" answers "none" here rather than "partial", even though
        `squares_for` itself returned a non-empty list.
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
        """This source's own tier for `category`, or None when this
        source does not serve it. See _TIERS above.
        """
        return self._TIERS.get(category)

    # -- estimate ------------------------------------------------------------

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        """Bytes and seconds for every product, over every 100km square the
        padded extent touches, with no network at all.

        `load_ostn15()` is cache-only (bng.py), matching every sibling
        source's own estimate(): the settings panel can call /api/estimate
        before a single survey has ever run, so a missing grid must not
        become a network call here. When a grid IS cached, this prices
        each product per square: 0 bytes for a square `_any_cached_square`
        already finds complete on disk for that product (under any
        version), the product's own per-square constant otherwise.

        When NO grid is cached, there is no honest way to say which 100km
        squares this extent even touches (unlike lidar_wales's own
        estimate(), which only ever needs an AREA and can fall back to an
        equirectangular approximation for that): British National Grid
        squares are a fact about real BNG coordinates, not something an
        unprojected WGS84 bbox can approximate its way to. mapgen's own
        survey extents are always small next to a 100km square (a building
        site, never a county), so this assumes exactly one, uncached,
        square per product rather than refusing to estimate at all;
        `OSTN15_BYTES` is added on top of that to price fetching the grid
        itself, the one piece of network work this whole estimate is
        allowed to assume.

        An extent whose padded BNG rectangle touches no 100km square at all
        (open sea past the National Grid's own envelope, or somewhere
        outside Great Britain entirely) prices at 0 bytes for every
        product: `_squares_for_estimate` returns an empty list rather than
        `None` for that case, and the per-square loop below simply has
        nothing to iterate.
        """
        grid = load_ostn15(cache_dir=self._ostn15_cache_dir)
        squares = _squares_for_estimate(bbox, grid)

        bytes_estimate = 0
        if squares is None:
            bytes_estimate += sum(_BYTES_PER_SQUARE.values())
        else:
            for product in self.PRODUCTS:
                rate = _BYTES_PER_SQUARE[product]
                for square in squares:
                    if not _any_cached_square(product, square):
                        bytes_estimate += rate

        if grid is None:
            bytes_estimate += OSTN15_BYTES

        seconds_estimate = max(bytes_estimate / BYTES_PER_SECOND_ESTIMATE, SECONDS_FLOOR)
        return Estimate(bytes_estimate=bytes_estimate, seconds_estimate=seconds_estimate)

    # -- fetch -----------------------------------------------------------------

    def _resolve_version(self, product: str, needed_squares: Sequence[str], progress: ProgressSink) -> str:
        """`product`'s version to shard under: the live `product_version()`
        answer, ordinarily, or the newest cached version directory whose
        shards are already complete for every one of `needed_squares` when
        the listing itself is unreachable.

        Only a "listing"-kind failure falls back; a "parse"-kind failure
        (the listing answered but not in the expected shape) is a fact
        about the SERVICE's own response, not about reachability, and
        propagates unchanged, the same distinction `_classify_inspire_
        error` draws between a status/transport failure and a parse one.

        Raises the original `OsOpenError` unchanged when no cached version
        covers every needed square either: there is nothing left this
        method can do without a live listing.
        """
        try:
            return product_version(product)
        except OsOpenError as exc:
            if exc.kind != "listing":
                raise
            fallback = _latest_complete_version_for_squares(product, needed_squares)
            if fallback is None:
                raise
            progress.emit("os_open_cache_fallback", source=self.id, product=product, version=fallback)
            return fallback

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
        cancel: CancelToken | None = None,
    ) -> list[Path]:
        """Ensures OSTN15, then, per product, ensures a shard-complete
        directory for every 100km square the padded extent touches, and
        writes one bbox-filtered NDJSON work part per product that
        finished complete.

        A (product, square) unit already `shards_complete` is skipped with
        no network call of any kind for that unit: no `product_downloads`,
        no `download_entry`, no `ZipReader`/`HttpByteSource` construction.
        `product_version(product)` is still attempted once per product
        (see `_resolve_version`), which is the one call a fully warm resume
        still makes; every heavier call beyond it is reached only lazily,
        the first time some square for that product genuinely needs it, so
        a product whose every needed square is already complete never
        reaches `product_downloads` or a `ZipReader` at all.

        A "listing"-kind failure resolving the version falls back to a
        complete cached version directory when one exists (see
        `_resolve_version`); the OsOpenError propagates unchanged, ending
        that whole product for this call, when it does not.

        Once a version is resolved, `product_downloads(product)` (or, for
        OpenRoads, a `ZipReader` over `HttpByteSource`) is built lazily, on
        the first square that genuinely needs it, and STICKILY reused, or
        stickily FAILED, across the rest of that product's own squares:
        a failure building the listing or the ranged reader is a fact
        about the whole product for this call, and repeating an identical
        failing network request once per remaining square would cost
        nothing but time and confusion. A failure reading or parsing one
        specific square's own member, once the listing/reader itself was
        built successfully, is that UNIT's own failure only, and does not
        stop the next square in the same product from being attempted (see
        the module docstring, "per-(product, square) failure").

        `tile_failures` is populated, and the combined failure raised, only
        once every product has been attempted; a product that finished
        with every needed square complete writes its own NDJSON work part
        (`_write_work_part`) and has `sweep_old_versions` run for it,
        regardless of whether any OTHER product in this same call failed.

        `cancel` is checked between products and between squares, never
        mid-download: a unit already in flight is always allowed to finish,
        the same convention every other tiled source in this project's
        sources/ package follows (see sources/base.py's own LayerSource
        docstring).
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
            self._record_tile_failures(tiles, FAILURE_NO_OUTPUT, NO_GB_SQUARES_MESSAGE)
            raise OsOpenSourceError(NO_GB_SQUARES_MESSAGE) from None

        e_min, n_min, e_max, n_max = extent
        needed_squares = squares_for(e_min, n_min, e_max, n_max)
        if not needed_squares:
            self._record_tile_failures(tiles, FAILURE_NO_OUTPUT, NO_GB_SQUARES_MESSAGE)
            raise OsOpenSourceError(NO_GB_SQUARES_MESSAGE)

        failures: dict[tuple[str, str], OsOpenError] = {}
        work_parts: list[Path] = []

        for product in self.PRODUCTS:
            if cancel is not None:
                cancel.raise_if_cancelled()

            try:
                version = self._resolve_version(product, needed_squares, progress)
            except OsOpenError as exc:
                for square in needed_squares:
                    failures[(product, square)] = exc
                continue

            product_dir = product_cache_dir(product, version)
            entries: list[dict] | None = None
            zip_reader: ZipReader | None = None
            # Sticky once set: a failure building the listing or the ranged
            # zip reader is a fact about the whole product for this call
            # (see this method's own docstring), so every remaining square
            # is failed the same way without repeating the network call
            # that already failed.
            listing_error: OsOpenError | None = None

            for square in needed_squares:
                if cancel is not None:
                    cancel.raise_if_cancelled()

                shard_dir = product_dir / "shards" / square
                if shards_complete(shard_dir):
                    for tile in tiles:
                        progress.emit(
                            "tile_skipped", source=self.id, tile_id=tile.tile_id,
                            product=product, square=square,
                        )
                    continue

                if listing_error is not None:
                    failures[(product, square)] = listing_error
                    continue

                try:
                    if product == "OpenRoads":
                        if zip_reader is None:
                            roads_entries = product_downloads(product)
                            entry = entry_for(roads_entries, area="GB", fmt="GML")
                            if entry is None:
                                raise OsOpenError(
                                    f"The OS Data Hub downloads listing for "
                                    f"{product!r} has no GB/GML entry.",
                                    kind="parse",
                                )
                            zip_reader = ZipReader(_openroads_byte_source(entry, self.session))
                        _shard_openroads_square(zip_reader, square, shard_dir, product_dir)
                    else:
                        if entries is None:
                            entries = product_downloads(product)
                        _shard_zip_download_square(product, entries, square, shard_dir, product_dir, progress)
                except OsOpenError as exc:
                    failures[(product, square)] = exc
                    if product == "OpenRoads":
                        if zip_reader is None:
                            listing_error = exc
                    elif entries is None:
                        listing_error = exc
                    continue

                for tile in tiles:
                    progress.emit(
                        "tile_done", source=self.id, tile_id=tile.tile_id,
                        product=product, square=square,
                    )

            if all(shards_complete(product_dir / "shards" / square) for square in needed_squares):
                sweep_old_versions(product, version)
                work_parts.append(_write_work_part(product, product_dir, needed_squares, extent, work_dir))

        if failures:
            self.tile_failures = _tile_failures_for(self.id, tiles, failures)
            raise _combined_failure(failures)

        if cancel is not None:
            cancel.raise_if_cancelled()
        return work_parts

    def _record_tile_failures(self, tiles: Sequence[Tile], kind: str, reason: str) -> None:
        """Record why this fetch() could not deliver AT ALL, once per
        tile: the whole-extent refusals (OSTN15 itself, or no GB squares
        at all), which arrive before any per-(product, square) unit is
        even attempted. Matches ElevationSource/LidarWalesSource/
        InspireSource exactly.
        """
        self.tile_failures = [
            TileFailure(source=self.id, tile_id=tile.tile_id, kind=kind, reason=reason)
            for tile in tiles
        ]

    # -- merge -----------------------------------------------------------------

    def merge(self, parts: Sequence[Path], out_dir: Path, stem: str) -> list[Path]:
        """Reads fetch()'s own work parts BY NAME (never by position, the
        `ElevationSource.merge` lesson every source in this package
        follows), reprojects every feature's BNG geometry to WGS84, and
        writes the six named GeoJSON files, one per bucket, each holding
        only the buckets that actually received at least one feature.

        merge() never re-reads the shard cache, only `parts`: fetch() and
        merge() may run in different processes, and the parts are the
        complete handoff between them (see the module docstring).

        The OSTN15 grid is `load_ostn15()`, cache-only: fetch() already
        ensured one for this exact extent, so this should always hit in
        the ordinary case. A cache the owner deleted between fetch() and
        merge(), or a merge() invoked directly without a prior fetch(), is
        a genuine error here and NOT the same "package without X beats a
        crash" tolerance `InspireSource.merge`/`LidarWalesSource.merge`
        apply to their own optional contour/boundary generation: unlike
        those, every one of this source's six outputs needs the grid to
        exist at all, so nothing this method could still usefully write.
        Raises `OsOpenSourceError`, naming what fetch() itself already
        guarantees ("run fetch() before merge()"), rather than trying
        `ensure_ostn15` itself, which would need a live network call this
        method has never otherwise needed.

        `attribution`'s own `"[year]"` placeholder is left untouched here,
        mirroring `InspireSource.merge` exactly: the substitution is a
        package.py-level enrichment (a later task's own responsibility;
        see the module docstring, "Attribution").
        """
        out_dir = Path(out_dir)
        grid = load_ostn15(cache_dir=self._ostn15_cache_dir)
        if grid is None:
            raise OsOpenSourceError(
                "No OSTN15 shift grid is cached; merge() never downloads "
                "one itself. Run fetch() first, which always ensures one "
                "before it writes any work part."
            )

        buckets: dict[str, list[dict]] = {suffix: [] for suffix in _OUTPUT_SUFFIXES}

        for product in self.PRODUCTS:
            part_path = next((p for p in parts if p.name == f"os_open_{product}.ndjson"), None)
            if part_path is None:
                continue
            for raw in _read_ndjson(part_path):
                feature_type = raw["type"]
                raw_properties = raw["properties"]
                if product == "OpenRoads":
                    bucket, properties = "roads", _road_properties(raw_properties)
                elif product == "OpenGreenspace":
                    bucket, properties = "greenspace", _greenspace_properties(raw_properties)
                else:
                    dispatched = _oml_bucket_and_properties(feature_type, raw_properties)
                    if dispatched is None:
                        continue
                    bucket, properties = dispatched
                geometry = _reproject_geometry(raw["geometry"], grid)
                buckets[bucket].append(
                    {"type": "Feature", "properties": properties, "geometry": geometry}
                )

        written: list[Path] = []
        for suffix in _OUTPUT_SUFFIXES:
            output_path = out_dir / f"{stem}_os_{suffix}.geojson"
            features = buckets[suffix]
            if not features:
                output_path.unlink(missing_ok=True)
                continue
            payload = {"type": "FeatureCollection", "features": features}
            atomic_write_bytes(output_path, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            written.append(output_path)
        return written

    def possible_outputs(self, stem: str) -> list[str]:
        """Every root file merge() could ever write for this stem. Read by
        package.py's stale-output sweep; see sources/base.py."""
        return [f"{stem}_os_{suffix}.geojson" for suffix in _OUTPUT_SUFFIXES]
