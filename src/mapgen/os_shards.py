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
partially-written survey silently.

That guarantee alone is not enough for a RETRY into the same directory,
which review round 1 found and this module now closes (Critical
finding): a failed first attempt can leave perfectly valid, readable
cell files behind for whatever cells it reached before raising, and a
second, corrected attempt that happens to touch a DIFFERENT set of cells
(the realistic shape of "the malformed feature that killed the first
attempt is now fixed or dropped, so the stream's own cell membership
shifted") never revisits the first attempt's own cells at all, because a
cell's own gzip handle is opened, `"wb"` truncate-on-open, lazily, only
the first time THIS run actually needs it. The first attempt's orphaned
file then survives, untouched, invisible to `shards_complete` (which
only ever checks what the SUCCESSFUL run's own `meta.json` names, never
what else happens to sit beside it in the directory) and, before this
fix, directly readable by `features_in`/`uprn_in` regardless of whether
the current generation's own data ever put anything there. `_clear_shard_
dir` closes this at the writer: every existing shard file of the type
this call writes, not only `meta.json`, is removed before anything new
is written, so a shard directory holds exactly one generation's data at
a time, replaced whole on every call rather than merged into whatever an
earlier attempt, successful or not, left behind. `features_in`/`uprn_in`
close it again, independently, at the reader (`_listed_keys`): a
cell/square file is only ever opened when it is BOTH reachable by the
query's own `cells_for`/`squares_for` walk AND named in `shard_dir/meta.
json`'s own list, so a file present on disk that the manifest does not
name is never read regardless of how it got there or whether the writer's
own sweep should have already removed it. Belt and braces, per the
review's own recommendation: either mechanism alone closes the reviewer's
reproduced fail-then-retry leak; both together mean the reader's own
correctness never actually depends on the writer's sweep having run.

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

# The National Grid's own usable coordinate envelope: 700 km east by
# 1,300 km north, covering the whole of the H/J, N/O and S/T 500 km
# square rows this module's own "Grid maths" section describes (Scilly
# in the south-west corner, Shetland and the sea beyond it in the
# north-east), the extent Ordnance Survey's own reference system is
# defined over. Review round 1 (Important finding) found that without
# this check, `_grid_letter`'s own [0, 4] bound alone lets a negative or
# moderately out-of-range coordinate still land inside the 5x5 letter
# grid by floor division, silently returning a plausible-looking,
# fabricated two-letter code (for example (300000, -50000) -> "XD",
# (300000, 1400000) -> "HD") instead of failing loud. Checked here, up
# front, rather than relying on `_grid_letter`'s own narrower check to
# catch it: `_grid_letter`'s check is left in place anyway as a second,
# independent guard on the row/col arithmetic itself, not removed just
# because this wider check now makes it unreachable for every input that
# passes here.
_MAX_EASTING = 700_000
_MAX_NORTHING = 1_300_000


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

    Raises ValueError for a coordinate outside the National Grid's own
    usable envelope (0 <= easting < 700,000, 0 <= northing < 1,300,000):
    a caller error (a sign error, a unit mixup, a bbox built from the
    wrong projection), never something this module papers over with a
    plausible-looking letter pair for ground that letter pair does not
    actually name. See `_MAX_EASTING`/`_MAX_NORTHING`'s own comment.
    """
    if not (0 <= easting < _MAX_EASTING):
        raise ValueError(
            f"Easting {easting} is outside the National Grid's own usable "
            f"envelope (0 <= easting < {_MAX_EASTING}); this is not a "
            f"coordinate the two-letter grid can honestly place."
        )
    if not (0 <= northing < _MAX_NORTHING):
        raise ValueError(
            f"Northing {northing} is outside the National Grid's own usable "
            f"envelope (0 <= northing < {_MAX_NORTHING}); this is not a "
            f"coordinate the two-letter grid can honestly place."
        )
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


def _clamped_index_range(v_min: float, v_max: float, step: int, max_value: int) -> range:
    """The inclusive range of `step`-sized grid indices between `v_min`
    and `v_max`, clamped to `[0, max_value)`.

    `grid_square` (see its own docstring) raises for a single point
    outside the National Grid's own usable envelope, because a caller
    asking "what square is THIS point in" has no honest answer there.
    `squares_for`/`cells_for` ask a different question, "what squares
    does this RECTANGLE touch", and a rectangle (or query bbox) that
    extends beyond the envelope, or lies entirely outside it, has an
    honest answer too: no squares out there, the same "nothing
    fabricated" principle applied at the coordinate system's own edges
    rather than at grid_square's single-point contract. Clamping the
    walk here, instead of calling grid_square on every raw candidate and
    letting an out-of-envelope one raise, is what lets a query bbox that
    happens to reach past GB (or sit entirely outside it, exercised by
    `test_features_in_over_a_missing_cell_yields_nothing` and its UPRN
    sibling) answer "no data here" rather than crash on a question this
    module can answer honestly either way.
    """
    lo = max(0, int(v_min // step))
    hi = min((max_value - 1) // step, int(v_max // step))
    return range(lo, hi + 1)


def squares_for(e_min: float, n_min: float, e_max: float, n_max: float) -> list[str]:
    """Every 100 km square the rectangle [e_min, e_max] x [n_min, n_max]
    touches, sorted and deduplicated. The portion of the rectangle (if
    any) outside the National Grid's own usable envelope contributes no
    squares, rather than raising; see `_clamped_index_range`'s docstring.
    """
    e_range = _clamped_index_range(e_min, e_max, _METRES_PER_100KM_SQUARE, _MAX_EASTING)
    n_range = _clamped_index_range(n_min, n_max, _METRES_PER_100KM_SQUARE, _MAX_NORTHING)
    squares = {
        grid_square(e100k * _METRES_PER_100KM_SQUARE, n100k * _METRES_PER_100KM_SQUARE)
        for e100k in e_range
        for n100k in n_range
    }
    return sorted(squares)


def cells_for(e_min: float, n_min: float, e_max: float, n_max: float) -> list[str]:
    """Every 10 km cell the rectangle [e_min, e_max] x [n_min, n_max]
    touches, sorted and deduplicated. Same clamping as `squares_for`, one
    grid size finer; see `_clamped_index_range`'s docstring.
    """
    e_range = _clamped_index_range(e_min, e_max, _METRES_PER_10KM_CELL, _MAX_EASTING)
    n_range = _clamped_index_range(n_min, n_max, _METRES_PER_10KM_CELL, _MAX_NORTHING)
    cells = {
        cell_10km(e10k * _METRES_PER_10KM_CELL, n10k * _METRES_PER_10KM_CELL)
        for e10k in e_range
        for n10k in n_range
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


def _clear_shard_dir(shard_dir: Path, suffix: str) -> None:
    """Removes every existing `*<suffix>` shard file in `shard_dir`, plus
    any `meta.json`, before a fresh write begins.

    Review round 1 (Critical finding): the front-of-function `meta.json`
    unlink alone (this module's own first pass) only protects a rebuild
    over an already-COMPLETE directory. It does nothing for two attempts
    into a directory that was never complete to begin with: a first
    attempt writes cells A and B, then the feature iterator raises (Task
    2's own fail-whole-stream shape) before `meta.json` is ever written,
    leaving A and B's own files, valid and readable, on disk; a second,
    corrected attempt writes only cell A and succeeds, and now `meta.
    json` lists only A. Because a cell/square is opened `"wb"` (truncate-
    on-open) lazily, only the first time THIS run actually needs it, B's
    file from the first attempt is never touched by the second and
    survives, invisible to `shards_complete` (which only ever checks
    what the SUCCESSFUL run's own `meta.json` lists, never what else sits
    beside it) and, before this fix, readable straight off disk by
    `features_in`/`uprn_in` regardless. Reproduced by the reviewer with a
    fail-then-retry pair whose two attempts touch differing cells, and
    fixed here at the writer: a shard directory is one generation's data
    at a time, "derive-once" meaning replaced whole on every call, not
    merged into whatever an earlier attempt, successful or not, happened
    to leave. Paired with `features_in`/`uprn_in`'s own manifest-only
    reading below (belt and braces, per the review's own suggestion): even
    if this sweep were ever bypassed, a file `meta.json` does not name is
    still never opened.
    """
    for path in shard_dir.glob(f"*{suffix}"):
        path.unlink()
    (shard_dir / "meta.json").unlink(missing_ok=True)


def _listed_keys(shard_dir: Path, list_field: str) -> set[str]:
    """The set of cell/square names `shard_dir/meta.json` actually lists
    under `list_field` ("cells" or "squares"), or an empty set when
    `meta.json` is absent, unreadable, or does not carry that field.

    `features_in`/`uprn_in` intersect this against their own query-driven
    `cells_for`/`squares_for` walk so that a file present on disk but NOT
    named in the current `meta.json` (an orphan from an earlier, failed
    or superseded attempt; see `_clear_shard_dir`'s own docstring) is
    never opened, even if something upstream of this module (a sweep
    that did not run, a file dropped in by hand) left one sitting there.
    An empty set on any failure to read `meta.json` is deliberate, not a
    best-effort fallback to reading whatever files happen to exist: a
    shard directory this function cannot positively confirm the manifest
    of has nothing this module will vouch for, matching the project-wide
    "nothing fabricated" rule at the level of "which files are trusted",
    not just "which values are computed".
    """
    meta_path = shard_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(meta, dict):
        return set()
    keys = meta.get(list_field)
    if not isinstance(keys, dict):
        return set()
    return set(keys)


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
    # Cleared first, not last: see _clear_shard_dir's own docstring for
    # why removing only meta.json is not enough (review round 1,
    # Critical finding).
    _clear_shard_dir(shard_dir, ".ndjson.gz")

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

    Only reads a cell that is BOTH in the query's own `cells_for` walk
    AND named in `shard_dir/meta.json`'s own `"cells"` list; a file
    sitting in `shard_dir` that meta.json does not list (an orphan from
    an earlier, failed or superseded write; see `_clear_shard_dir`'s own
    docstring) is never opened, regardless of whether it exists on disk.
    """
    listed_cells = _listed_keys(shard_dir, "cells")
    seen_ids: set[str] = set()
    for cell in cells_for(e_min, n_min, e_max, n_max):
        if cell not in listed_cells:
            continue
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
    _clear_shard_dir(shard_dir, ".csv.gz")  # Same reasoning as write_shards.

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

    Only reads a square that is BOTH in the query's own `squares_for`
    walk AND named in `shard_dir/meta.json`'s own `"squares"` list, the
    same manifest-only discipline `features_in` applies; see that
    function's own docstring and `_clear_shard_dir`'s.
    """
    listed_squares = _listed_keys(shard_dir, "squares")
    for square in squares_for(e_min, n_min, e_max, n_max):
        if square not in listed_squares:
            continue
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
