"""GB National Grid maths and the derive-once shard store for OS Open data.

## Grid maths: two nested 5x5 letter grids, not a lookup table

`grid_square` names the two-letter, 100 km Ordnance Survey National Grid
square containing a British National Grid coordinate (for example "SS",
"ST", "HP"), the same lettering scheme bng.py's own module docstring cites
by name ("A guide to coordinate systems in Great Britain"). The scheme is
two applications of one idea, a 5 by 5 super-grid of squares lettered A to
Z with I omitted (25 letters, since GB's own lettering never uses I), at
two different scales:

- The FIRST letter names which 500 km square the point falls in:
  `row = 3 - (northing // 500000)`, `col = (easting // 500000) + 2`. The
  `+2`/`3 -` offsets place the 500 km square containing Great Britain's own
  true origin (`easting // 500000 == 0`, `northing // 500000 == 0`) at row
  3, column 2 of the conceptual 5x5 letter grid, which is where OS's own
  six real 500 km squares for Great Britain actually sit, verified
  directly rather than assumed: S and T (columns 2 and 3) at row 3, N and
  O at row 2, H and J at row 1.
- The SECOND letter names which 100 km square within that 500 km square:
  `row = 4 - ((northing // 100000) % 5)`, `col = (easting // 100000) % 5`.
  No offset here, because a 500 km square's own south-west corner already
  sits at row 4, column 0 of its own 5x5 letter grid; the within-square
  remainder maps onto it directly.

Both formulas are verified, in this module's own tests, against three of
OS's real squares this project has independently confirmed elsewhere: SS
(299000, 179000) and ST (301000, 179000), across the SS/ST 100 km seam,
and HP (451000, 1215000), the Shetland square this project's own live
OpenRoads probe named. `cell_10km` needs no letter arithmetic of its own:
a 10 km cell is just its square plus the tens-of-km digit of each
coordinate (`(easting // 10000) % 10`, and the same for northing), pinned
against the real GreenspaceSite sample os_gml.py's own committed fixture
carries (easting 297393, northing 100106, cell "SS90").

`squares_for`/`cells_for` walk the 100 km/10 km lattice across a
rectangle and return every square/cell the rectangle touches, sorted; a
feature is written into every one of those cells its geometry's own
bounding box intersects (see `write_shards` below), never only the one
its first coordinate happens to fall in, so a feature straddling a seam
is found by a query on either side of it.

## The shard store: meta.json-last, the same discipline as inspire.py's

`write_shards` streams `OsFeature` records (Task 2's own dataclass) into
one gzip-compressed NDJSON file per 10 km cell (`<CELL>.ndjson.gz`),
lazily: a cell's own gzip handle is opened only the first time a feature
actually needs to be written into it, kept open across the whole walk,
and every open handle is closed, cleanly, in a `finally` block before this
function does anything else, success or failure alike. `meta.json`
(`{"total": n, "cells": {cell: count}}`) is written LAST, after every
handle has closed, via `fsutil.atomic_write_text`'s own temp-file-then-
replace discipline: exactly the completion-marker convention `sources/
inspire.py`'s own module docstring documents at length ("`meta.json` is
the one true completion marker... its mere existence already witnesses
that every write below it completed"), applied here at the granularity of
a whole shard directory rather than one authority's parcels file.

This matters for a reason specific to this task, not a generic caution:
Task 2's own `os_gml` readers (`_walk_features`) deliberately fail the
WHOLE stream, an `OsOpenError` escaping mid-iteration, on the first
feature they cannot parse (see `os_gml.py`'s own module docstring, "the
whole stream stops"), rather than counting and skipping the one bad
feature the way `inspire.py`'s own `_MalformedParcel` does. That means a
caller handing `write_shards` a live product's real feature iterator WILL
see that exception here, someday, mid-walk, in production; this function
is written and tested against that fact directly (see
`test_a_mid_stream_os_open_error_propagates_out_of_write_shards` and its
two siblings), not merely against a clean, exhaustible iterator. Two
things are guaranteed when that happens: the exception itself propagates
out of `write_shards` unchanged (never swallowed, never reported as a
different, wrapping error), and no `meta.json` is left behind, so
`shards_complete` reports the directory as incomplete and a caller that
checks it before trusting the cache rebuilds rather than serving a
partially-written survey silently. A stale `meta.json` left over from a
PREVIOUS, successful run into the same directory is removed at the very
start of `write_shards`, before any cell file is touched, for the same
reason: without that, a rebuild-in-place that fails partway could leave
an old, now-stale `meta.json` sitting beside newly truncated cell files,
falsely reporting completeness. In this task's own real call sites (Task
4/5's "ensure shards once per version" callers) a rebuild always targets
a fresh, version-stamped directory rather than an existing complete one,
so this case is not expected to fire in practice; it costs one `unlink`
to close anyway.

`gzip.GzipFile(..., mtime=0)` is used throughout (both here and in
`write_uprn_shards`), not the ordinary `gzip.open`, specifically to force
the MTIME field gzip writes into every stream's own header to zero rather
than the wall-clock time of the write: identical input then produces
byte-identical shard files across separate rebuilds, which is what makes
a shard file's own bytes diffable evidence of real content drift rather
than permanently noisy from a timestamp nobody asked about.

## The dedup contract in features_in: id "" is never a duplicate of id ""

`features_in` dedups by `id` ACROSS cells, because a feature whose
geometry straddles a seam is written into every cell it touches (see
above) and a caller asking for a bbox spanning that seam must see it
once, not twice. That dedup is keyed on `feature_id` for every NON-EMPTY
id, which real OS Open data always carries (`gml:id`; every real feature
this project has read, 371,490 features live-scanned over one whole real
OpenMapLocal square, carried one). Task 2's own reader accepts a blank
`gml:id` as `feature_id=""` rather than fabricating one (see `os_gml.py`'s
own docstring, "absent means None", and progress.md's own carried-forward
note: "absent gml:id silently becomes feature_id ''; dedup in Task 3 keys
on id; an empty id could over-dedup; watch in Task 3's review"). This
module answers that note directly: an empty id is never treated as a
duplicate of another empty id, so two genuinely different id-less
features read out of the same cell both come through, rather than the
second one silently vanishing because it happened to compare equal, under
Python's own `==`, to the first. The cost of this choice, accepted rather
than worked around, is that a single id-less feature whose OWN geometry
straddles a seam is not deduped either, and would be yielded once per
cell its bbox reaches; no such feature is known to exist in any real OS
Open product this project has read, and the alternative (deduping "" like
any other id) would silently drop distinct real features far more often
than it would silently double-count one.

## write_uprn_shards: the one CSV-shaped shard store

`write_uprn_shards` follows the identical meta.json-last discipline one
level coarser, by 100 km square rather than 10 km cell, because
OpenUPRN's own rows carry no geometry to compute a bbox from, only a
single point (`X_COORDINATE`/`Y_COORDINATE`), and one 100 km square is
already the coarsest partition this project's own OS Open products use
(see `grid_square`). It reads its input with the plain stdlib `csv`
module over a text stream the caller has already opened, but does not
trust that caller to have opened it with `encoding="utf-8-sig"`
specifically: the real OpenUPRN file carries a UTF-8 byte-order mark
before its own header row (this task's own plan, "Owner ground truth"
section), and a caller who opened it with plain `"utf-8"` instead would
otherwise leave that BOM character sitting as the first character of the
header row's first field. `_strip_bom_from_first_line` strips it here,
defensively, from whichever line arrives first, so this module's own
correctness does not depend on a caller's encoding choice one file open()
away.
"""

from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path
from typing import Iterable, Iterator, TextIO

from mapgen.fsutil import atomic_write_text, ensure_dir
from mapgen.os_gml import OsFeature

# The National Grid's own lettering alphabet: A to Z with I omitted (OS
# never uses I in either the 500 km or the 100 km letter), 25 letters
# indexed 0 to 24. Not a lookup table of squares to letters (which would
# be an opaque list nobody reading this module could check by eye); it is
# the plain alphabet minus one letter, and grid_square's own row/col
# arithmetic is what does the actual derivation.
_GRID_ALPHABET = "ABCDEFGHJKLMNOPQRSTUVWXYZ"

_METRES_PER_500KM_SQUARE = 500_000
_METRES_PER_100KM_SQUARE = 100_000
_METRES_PER_10KM_CELL = 10_000


def _grid_letter(row: int, col: int) -> str:
    if not (0 <= row <= 4 and 0 <= col <= 4):
        raise ValueError(
            f"Grid position (row={row}, col={col}) falls outside the 5x5 "
            f"National Grid letter square; this coordinate is not "
            f"representable in the two-letter grid."
        )
    return _GRID_ALPHABET[row * 5 + col]


def grid_square(easting: float, northing: float) -> str:
    """The two-letter, 100 km OS National Grid square containing
    (easting, northing), for example "SS" or "HP". See the module
    docstring's "Grid maths" section for the derivation.
    """
    row1 = 3 - int(northing // _METRES_PER_500KM_SQUARE)
    col1 = int(easting // _METRES_PER_500KM_SQUARE) + 2
    row2 = 4 - (int(northing // _METRES_PER_100KM_SQUARE) % 5)
    col2 = int(easting // _METRES_PER_100KM_SQUARE) % 5
    return _grid_letter(row1, col1) + _grid_letter(row2, col2)


def cell_10km(easting: float, northing: float) -> str:
    """The square plus a two-digit 10 km cell code, for example "SS90"."""
    square = grid_square(easting, northing)
    e_digit = int(easting // _METRES_PER_10KM_CELL) % 10
    n_digit = int(northing // _METRES_PER_10KM_CELL) % 10
    return f"{square}{e_digit}{n_digit}"


def squares_for(e_min: float, n_min: float, e_max: float, n_max: float) -> list[str]:
    """Every 100 km square the rectangle [e_min, e_max] x [n_min, n_max]
    touches, sorted and deduplicated.
    """
    e_lo = int(e_min // _METRES_PER_100KM_SQUARE)
    e_hi = int(e_max // _METRES_PER_100KM_SQUARE)
    n_lo = int(n_min // _METRES_PER_100KM_SQUARE)
    n_hi = int(n_max // _METRES_PER_100KM_SQUARE)
    squares = {
        grid_square(e100k * _METRES_PER_100KM_SQUARE, n100k * _METRES_PER_100KM_SQUARE)
        for e100k in range(e_lo, e_hi + 1)
        for n100k in range(n_lo, n_hi + 1)
    }
    return sorted(squares)


def cells_for(e_min: float, n_min: float, e_max: float, n_max: float) -> list[str]:
    """Every 10 km cell the rectangle [e_min, e_max] x [n_min, n_max]
    touches, sorted and deduplicated.
    """
    e_lo = int(e_min // _METRES_PER_10KM_CELL)
    e_hi = int(e_max // _METRES_PER_10KM_CELL)
    n_lo = int(n_min // _METRES_PER_10KM_CELL)
    n_hi = int(n_max // _METRES_PER_10KM_CELL)
    cells = {
        cell_10km(e10k * _METRES_PER_10KM_CELL, n10k * _METRES_PER_10KM_CELL)
        for e10k in range(e_lo, e_hi + 1)
        for n10k in range(n_lo, n_hi + 1)
    }
    return sorted(cells)


# --------------------------------------------------------------------------
# Geometry bbox: one recursive walk serves Point, LineString, Polygon and
# MultiPolygon alike, since GeoJSON coordinates nest the same way (a list
# of numbers at the leaves, an arbitrary number of list levels above it)
# regardless of which of the four shapes it is.
# --------------------------------------------------------------------------


def _iter_coordinate_pairs(coordinates) -> Iterator[tuple[float, float]]:
    if not coordinates:
        return
    if isinstance(coordinates[0], (int, float)):
        yield (coordinates[0], coordinates[1])
        return
    for item in coordinates:
        yield from _iter_coordinate_pairs(item)


def _geometry_bbox(geometry: dict) -> tuple[float, float, float, float]:
    pairs = list(_iter_coordinate_pairs(geometry["coordinates"]))
    if not pairs:
        # Nothing fabricated: a geometry with no coordinates at all has no
        # honest bbox to report, and Task 2's own os_gml readers never
        # produce one (a present-but-empty posList/pos is refused there
        # already; see os_gml.py's own "malformed feature content"
        # section). Reaching here means a caller handed write_shards a
        # feature dict this module cannot place on the grid at all.
        raise ValueError("Cannot compute a bounding box for empty geometry coordinates.")
    eastings = [pair[0] for pair in pairs]
    northings = [pair[1] for pair in pairs]
    return min(eastings), min(northings), max(eastings), max(northings)


def _bbox_intersects(
    bbox: tuple[float, float, float, float],
    e_min: float,
    n_min: float,
    e_max: float,
    n_max: float,
) -> bool:
    bbox_e_min, bbox_n_min, bbox_e_max, bbox_n_max = bbox
    return not (
        bbox_e_max < e_min
        or bbox_e_min > e_max
        or bbox_n_max < n_min
        or bbox_n_min > n_max
    )


def _write_meta(meta_path: Path, meta: dict) -> None:
    atomic_write_text(meta_path, json.dumps(meta, separators=(",", ":")))


# --------------------------------------------------------------------------
# The OsFeature shard store: write_shards, shards_complete, features_in.
# --------------------------------------------------------------------------


def write_shards(features: Iterable[OsFeature], shard_dir: Path) -> dict:
    """Streams `features` into `shard_dir/<CELL>.ndjson.gz`, one JSON
    object per line, a feature written into every 10 km cell its
    geometry's own bbox intersects. Writes `shard_dir/meta.json` LAST,
    `{"total": n, "cells": {cell: count}}`, and returns that same dict.

    See the module docstring's "shard store" section for what happens,
    and why, when `features` raises partway through (Task 2's readers do
    exactly this on a malformed feature): every open gzip handle is
    closed before this function does anything else, the exception then
    propagates unchanged, and no `meta.json` is written.
    """
    ensure_dir(shard_dir)
    meta_path = shard_dir / "meta.json"
    # Removed first, not last: see the module docstring's own paragraph on
    # why a stale meta.json from a previous successful run must not be
    # left standing while this run's cell files are being truncated and
    # rewritten underneath it.
    meta_path.unlink(missing_ok=True)

    handles: dict[str, gzip.GzipFile] = {}
    counts: dict[str, int] = {}
    total = 0
    try:
        for feature in features:
            total += 1
            bbox = _geometry_bbox(feature.geometry)
            record = json.dumps(
                {
                    "id": feature.feature_id,
                    "type": feature.feature_type,
                    "geometry": feature.geometry,
                    "properties": feature.properties,
                },
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            for cell in cells_for(*bbox):
                handle = handles.get(cell)
                if handle is None:
                    handle = gzip.GzipFile(
                        filename=str(shard_dir / f"{cell}.ndjson.gz"),
                        mode="wb",
                        # Fixed mtime, not wall-clock time: see the module
                        # docstring's own paragraph on reproducibility.
                        mtime=0,
                    )
                    handles[cell] = handle
                handle.write(record)
                counts[cell] = counts.get(cell, 0) + 1
    finally:
        for handle in handles.values():
            handle.close()

    meta = {"total": total, "cells": counts}
    _write_meta(meta_path, meta)
    return meta


def shards_complete(shard_dir: Path) -> bool:
    """True only when `shard_dir/meta.json` exists, parses, and every
    cell or square it names has its own shard file present on disk.

    Covers both shard shapes this module writes: `write_shards`'s own
    `{"cells": {...}}` naming `<CELL>.ndjson.gz` files, and
    `write_uprn_shards`'s own `{"squares": {...}}` naming `<SQ>.csv.gz`
    files. `meta.json`'s mere absence already answers False for both of
    the brief's own "meta absent" cases (meta genuinely missing, or cell
    files present with no meta at all): this function only ever looks at
    what meta.json itself claims, never at what other files happen to sit
    in the directory beside it.
    """
    meta_path = shard_dir / "meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict):
        return False

    if "cells" in meta:
        keys, suffix = meta["cells"], ".ndjson.gz"
    elif "squares" in meta:
        keys, suffix = meta["squares"], ".csv.gz"
    else:
        return False
    if not isinstance(keys, dict):
        return False

    return all((shard_dir / f"{key}{suffix}").exists() for key in keys)


def features_in(
    shard_dir: Path, e_min: float, n_min: float, e_max: float, n_max: float
) -> Iterator[dict]:
    """Every feature, as a plain dict (`{"id", "type", "geometry",
    "properties"}`), whose geometry bbox intersects [e_min, e_max] x
    [n_min, n_max], read from only the 10 km cells the rectangle touches.

    Deduplicated by `id` across cells for every NON-EMPTY id; an empty id
    (`feature_id == ""`, Task 2's own reading of a feature with no
    `gml:id`) is never deduped against another empty id. See the module
    docstring's "dedup contract" section for why.
    """
    seen_ids: set[str] = set()
    for cell in cells_for(e_min, n_min, e_max, n_max):
        path = shard_dir / f"{cell}.ndjson.gz"
        if not path.exists():
            continue
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                bbox = _geometry_bbox(record["geometry"])
                if not _bbox_intersects(bbox, e_min, n_min, e_max, n_max):
                    continue
                feature_id = record["id"]
                if feature_id != "":
                    if feature_id in seen_ids:
                        continue
                    seen_ids.add(feature_id)
                yield record


# --------------------------------------------------------------------------
# The UPRN variant: coarser (100 km square only), CSV rather than NDJSON,
# and no geometry to compute a bbox from, only a single point per row.
# --------------------------------------------------------------------------

_UPRN_ROW_LENGTH = 5


def _strip_bom_from_first_line(lines: Iterable[str]) -> Iterator[str]:
    is_first_line = True
    for line in lines:
        if is_first_line and line.startswith("﻿"):
            line = line[1:]
        is_first_line = False
        yield line


def write_uprn_shards(text_stream: TextIO, shard_dir: Path) -> dict:
    """Streams OpenUPRN CSV rows (`UPRN,X_COORDINATE,Y_COORDINATE,
    LATITUDE,LONGITUDE`, with a header row) from `text_stream` into
    `shard_dir/<SQ>.csv.gz`, one row per line, one file per 100 km
    square. Writes `shard_dir/meta.json` LAST, `{"total": n, "squares":
    {sq: count}}`, and returns that same dict.

    `text_stream` is read through `_strip_bom_from_first_line` before
    `csv.reader` ever sees it, so a caller that opened the real OpenUPRN
    file with plain `"utf-8"` rather than `"utf-8-sig"` still gets a
    clean header (see the module docstring's own "write_uprn_shards"
    section).
    """
    ensure_dir(shard_dir)
    meta_path = shard_dir / "meta.json"
    meta_path.unlink(missing_ok=True)  # Same reasoning as write_shards.

    reader = csv.reader(_strip_bom_from_first_line(text_stream))
    header = next(reader, None)
    if header is None or header[0] != "UPRN":
        raise ValueError(
            "This does not look like an OS Open UPRN CSV file: its header "
            "row does not start with 'UPRN'."
        )

    handles: dict[str, gzip.GzipFile] = {}
    counts: dict[str, int] = {}
    total = 0
    try:
        for row in reader:
            if not row:
                continue
            uprn, x_text, y_text, lat_text, lon_text = row[:_UPRN_ROW_LENGTH]
            easting, northing = float(x_text), float(y_text)
            square = grid_square(easting, northing)
            handle = handles.get(square)
            if handle is None:
                handle = gzip.GzipFile(
                    filename=str(shard_dir / f"{square}.csv.gz"),
                    mode="wb",
                    mtime=0,
                )
                handles[square] = handle
            line = f"{uprn},{x_text},{y_text},{lat_text},{lon_text}\n".encode("utf-8")
            handle.write(line)
            counts[square] = counts.get(square, 0) + 1
            total += 1
    finally:
        for handle in handles.values():
            handle.close()

    meta = {"total": total, "squares": counts}
    _write_meta(meta_path, meta)
    return meta


def uprn_in(
    shard_dir: Path, e_min: float, n_min: float, e_max: float, n_max: float
) -> Iterator[tuple[int, float, float, float, float]]:
    """Every `(uprn, easting, northing, latitude, longitude)` row whose
    easting/northing falls inside [e_min, e_max] x [n_min, n_max], read
    from only the 100 km squares the rectangle touches.
    """
    for square in squares_for(e_min, n_min, e_max, n_max):
        path = shard_dir / f"{square}.csv.gz"
        if not path.exists():
            continue
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if not row:
                    continue
                uprn_text, x_text, y_text, lat_text, lon_text = row[:_UPRN_ROW_LENGTH]
                easting, northing = float(x_text), float(y_text)
                if not (e_min <= easting <= e_max and n_min <= northing <= n_max):
                    continue
                yield (
                    int(uprn_text),
                    easting,
                    northing,
                    float(lat_text),
                    float(lon_text),
                )
