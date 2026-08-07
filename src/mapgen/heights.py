"""Fusing Welsh LiDAR into building heights, the owner's founding complaint.

An OSM building left flat has always been the loudest thing wrong with a
mapgen package: every `<way building=*>` mapgen's own OSM merge writes
(see `mapgen.sources.osm`/`mapgen.merge`) carries no `height` tag at all,
because OSM itself rarely does, and Grasshopper extrudes it to nothing.
Task 6 packages a 1 m DTM and DSM beside the merged `.osm`; this module
turns the two rasters and the footprints already on disk into the one
number Urbano needs, `height`, with no network call and with no height
invented for a tile that has no evidence to give one.

## The fusion rule, and why it is what it is

`height = p90(DSM samples) - median(DTM samples)`, under the footprint,
rounded to one decimal. The 90th percentile of the DSM rather than its
maximum: a roof is rarely perfectly flat in a 1 m raster (aerials,
chimney stacks, parapets, a single noisy return), and the maximum answers
"the single tallest pixel found", which on real LiDAR is an outlier more
often than it is the ridge. The 90th percentile sits close enough to the
top of the roof to read as its height and far enough from the extreme not
to be decided by one bad return. The median of the DTM, not its mean or
minimum, for the matching reason on the ground side: a driveway dip or a
doorway step inside the footprint should not pull the ground reading
down.

Heights under 2.0 m are refused, not rounded up, and recorded as
`no_data` exactly like a tile with no coverage: a slab, a low wall or a
misregistered footprint reads as a few tens of centimetres of DSM-minus-
DTM, and writing that as a building height would put fabricated data into
a package. 2.0 m sits comfortably under the shortest real single storey
this owner surveys and comfortably over what registration noise alone
produces.

## Percentile and median, made exact

Linear interpolation between order statistics (numpy's own default
"linear" method for `percentile`), not nearest rank: it is the more
common convention, and its hand check is exactly numpy's own.
`_percentile([1, 2, ..., 10], 0.9)` sits 0.9 of the way from index 8 to
index 9 of the sorted list (`0.9 * 9 = 8.1`), giving
`9 * 0.9 + 10 * 0.1 = 9.1`; `_percentile([1, ..., 10], 0.5)` gives `5.5`,
the ordinary median of an even-length list. `_p90` and `_median` are both
this one function at 0.9 and 0.5, so a roof whose every sample happens to
agree answers the same, unremarkable value at both ends of the
subtraction.

## Sampling a footprint: the grid, and why even-odd

An OSM building's own outline is a plain polygon of lat/lon node refs;
projected to BNG (`mapgen.bng.to_bng`) they bound a small rectangle, and
every point of a 1 m grid across that rectangle is tested against the
footprint with the ordinary even-odd (ray casting, PNPOLY) rule and, if
inside, sampled from both rasters. `BngWindow.sample_bng` (`cog.py`)
already answers None for a point either raster has no data for or that
falls outside its own sampleable rectangle, so "no LiDAR here" and "off
the edge of the packaged window" are the same outcome here: fewer than 3
points where BOTH rasters answered is `no_data`, and nothing is written.
That floor is the owner's own ruling, verbatim: "if a tile has no data
then it has no data, we should not add something random."

## Rewriting the file mapgen itself produced

`mapgen.merge.merge_osm_xml` is the only writer of a mapgen `.osm`,
always in this exact shape: one XML declaration line, `<osm ...>`, one
indented line per top level element in file order, `</osm>`. This module
reads that declaration line back off the file rather than assuming a
fixed string (`_declaration_line`), parses the body with `xml.etree`, and
reconstructs the same shape from the parsed tree, element by element, so
a way this run does not touch is written back exactly what
`ElementTree` parsed it into. That is what makes a second run, and a
building the owner tagged by hand, `kept_existing` rather than rewritten:
nothing here compares old bytes to new, the element itself is simply
never altered.

Nothing is written at all, not even the declaration and the closing tag,
when no way's tags actually changed: a run over a package that already
carries every height, or that carries none it could add, leaves the file
untouched rather than rewriting it byte-for-byte identically for no
reason (see `fuse_building_heights`'s own `changed` guard).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from mapgen.bng import BngError, Ostn15Grid, to_bng
from mapgen.cog import BngWindow
from mapgen.fsutil import atomic_write_bytes

# A footprint needs at least this many points where BOTH rasters answer
# before a height is trusted at all. Three rather than one or two: a
# single point is one pixel's opinion, indistinguishable from noise, and
# this is the owner's own floor for "enough to call it a measurement".
MIN_SAMPLE_POINTS = 3

# Below this, a DSM-minus-DTM reading is refused as noise rather than
# reported as a building (see the module docstring).
MIN_HEIGHT_METRES = 2.0

# The interior sample grid's spacing, matching the rasters' own 1 m pixels.
SAMPLE_STEP_METRES = 1.0

# The exact tag values the brief specifies, verbatim.
HEIGHT_TAG_KEY = "height"
SOURCE_HEIGHT_TAG_KEY = "source:height"
SOURCE_HEIGHT_ATTRIBUTION = "Welsh Government LiDAR 2020 to 2023 (DSM minus DTM)"

_BUILDING_TAG_KEY = "building"


class HeightsError(RuntimeError):
    """Raised when a package's own `.osm` cannot be read or rewritten.

    A RuntimeError, matching `ElevationGridError` and every other refusal
    a post-step in `package.py` records rather than propagates: a building
    left flat is exactly the package the owner already had, and the run
    finishes either way.
    """


@dataclass(frozen=True)
class HeightsRecord:
    """What one pass over an `.osm` did, in the five numbers `package.py`
    needs and nothing else.

    `buildings` is every way tagged `building=*`, whatever happened to it
    next, so `written + kept_existing + no_data == buildings` always.
    `relations_skipped` is counted separately because a multipolygon
    relation is never touched at all, not even looked at for a height.
    """

    buildings: int
    written: int
    kept_existing: int
    no_data: int
    relations_skipped: int


# --------------------------------------------------------------------------
# Percentile and median (see the module docstring's hand check).
# --------------------------------------------------------------------------


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Linear interpolation between order statistics, numpy's own "linear"
    method for `percentile`.

    `values` is never empty when this is called (the caller's own
    `MIN_SAMPLE_POINTS` floor), so there is nothing here to refuse. A
    single value returns itself at every fraction, which is the right
    answer for a footprint whose every sample happened to agree.
    """
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    position = fraction * (n - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _p90(values: Sequence[float]) -> float:
    return _percentile(values, 0.9)


def _median(values: Sequence[float]) -> float:
    return _percentile(values, 0.5)


# --------------------------------------------------------------------------
# Footprint sampling.
# --------------------------------------------------------------------------


def _point_in_polygon(
    easting: float, northing: float, ring: Sequence[tuple[float, float]]
) -> bool:
    """The ordinary even-odd (ray casting, PNPOLY) test.

    `ring` need not be explicitly closed (last point equal to first): the
    edge from the last vertex back to the first is included by starting
    `prev` at index -1, so a footprint whose way already repeats its
    first node id (the usual OSM convention) and one that does not are
    tested identically. A horizontal edge (`prev_n == n`) never satisfies
    the `!=` test below unless `northing` sits exactly on it, so the
    division by `(prev_n - n)` is never asked to divide by zero here.
    """
    inside = False
    prev_e, prev_n = ring[-1]
    for e, n in ring:
        if (n > northing) != (prev_n > northing):
            x_at = (prev_e - e) * (northing - n) / (prev_n - n) + e
            if easting < x_at:
                inside = not inside
        prev_e, prev_n = e, n
    return inside


def _grid_points(
    e_min: float, n_min: float, e_max: float, n_max: float, step: float = SAMPLE_STEP_METRES
):
    """Every point of a `step`-spaced grid covering a bounding box, from
    its low corner up to and including a point at or past the high one.
    """
    e_count = int(math.floor((e_max - e_min) / step)) + 1
    n_count = int(math.floor((n_max - n_min) / step)) + 1
    for row in range(n_count):
        northing = n_min + row * step
        for col in range(e_count):
            easting = e_min + col * step
            yield easting, northing


def _footprint_samples(
    ring: Sequence[tuple[float, float]], dtm: BngWindow, dsm: BngWindow
) -> list[tuple[float, float]]:
    """Every point of the footprint's 1 m interior grid where BOTH rasters
    answer, as `(dtm height, dsm height)` pairs.

    The bounding box comes first (implicit in the grid's own range), the
    polygon test second, and the rasters are asked last: a footprint
    mostly outside the packaged window never queries a raster for a point
    that the polygon test, or the bounding box itself, was going to
    discard anyway.
    """
    eastings = [e for e, _ in ring]
    northings = [n for _, n in ring]
    e_min, e_max = min(eastings), max(eastings)
    n_min, n_max = min(northings), max(northings)
    samples: list[tuple[float, float]] = []
    for easting, northing in _grid_points(e_min, n_min, e_max, n_max):
        if not _point_in_polygon(easting, northing, ring):
            continue
        dtm_height = dtm.sample_bng(easting, northing)
        dsm_height = dsm.sample_bng(easting, northing)
        if dtm_height is None or dsm_height is None:
            continue
        samples.append((dtm_height, dsm_height))
    return samples


def _footprint_ring(
    way: ET.Element, nodes: dict[str, tuple[float, float]]
) -> list[tuple[float, float]] | None:
    """A way's own nodes as `(lat, lon)` pairs, or None if it cannot be
    read as a footprint at all.

    None for fewer than 3 `nd` refs (not a polygon) and for any `nd` ref
    this file's own node table does not resolve. Both are Task 7's
    "malformed way" case, folded into the caller's `no_data` count rather
    than given a counter of their own: the record shape is five keys,
    fixed by the brief.
    """
    refs = [nd.get("ref") for nd in way.findall("nd")]
    if len(refs) < 3:
        return None
    coords: list[tuple[float, float]] = []
    for ref in refs:
        latlon = nodes.get(ref) if ref is not None else None
        if latlon is None:
            return None
        coords.append(latlon)
    return coords


# --------------------------------------------------------------------------
# Reading and rewriting the .osm itself.
# --------------------------------------------------------------------------


def _declaration_line(text: str) -> str:
    """The file's own first line, verbatim, checked rather than assumed.

    `mapgen.merge.merge_osm_xml` always writes
    `'<?xml version="1.0" encoding="UTF-8"?>'` as this line, but this
    reads it back rather than hard-coding that string, so a rewrite
    "preserves the encoding declaration" in fact and not only in the
    ordinary case.
    """
    first_line = text.split("\n", 1)[0]
    if not first_line.lstrip().startswith("<?xml"):
        raise HeightsError(
            "This .osm does not start with an XML declaration, which is not "
            "the shape mapgen's own writer produces, so it cannot be "
            "rewritten with confidence that its structure is understood."
        )
    return first_line


def _escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
    )


def _open_tag(tag: str, attrib) -> str:
    """The root element's own opening tag, rebuilt from what `ElementTree`
    parsed rather than assumed: whatever `<osm ...>` this file actually
    carries (its own `version` and `generator`, in their own order) is
    what comes back out.
    """
    parts = [f"<{tag}"]
    for key, value in attrib.items():
        parts.append(f' {key}="{_escape_attribute(value)}"')
    parts.append(">")
    return "".join(parts)


def _rewrite_osm(osm_path: Path, declaration: str, root: ET.Element) -> None:
    """Write `root` back to `osm_path`, in exactly the shape
    `merge_osm_xml` itself writes: one line per top level element, in
    file order.

    Atomic (`fsutil.atomic_write_bytes`): the file either holds the
    fusion's own output complete, or is untouched, never half written.
    """
    lines = [declaration, _open_tag(root.tag, root.attrib)]
    for element in root:
        lines.append("  " + ET.tostring(element, encoding="unicode"))
    lines.append(f"</{root.tag}>")
    atomic_write_bytes(osm_path, ("\n".join(lines) + "\n").encode("utf-8"))


def _minimum_existing_id(root: ET.Element) -> int:
    """The smallest id any `<node>`, `<way>` or `<relation>` in `root`
    already carries, or 0 if none carries a parseable one.

    Moved here from `package.py` (this module's own home for `.osm`
    id-and-rewrite plumbing, alongside `_declaration_line`/`_rewrite_osm`
    above) so `mapgen.buildings` can share this exact function too,
    without `package.py` and `buildings.py` importing one another: every
    module in this pair that touches raw `.osm` mechanics reaches into
    `heights.py` for it, never sideways into each other.

    Collision safety, not merely readability, is why the counter a
    caller derives from this is never simply hard-coded to start at -1
    (see `package.py`'s `_fuse_boundaries_step`, "collision safety"
    section, for the full reasoning, still accurate at this function's
    new address). This function is the fix: it looks at every element
    the file actually holds, of any type and either sign, rather than
    assuming nothing already sits at or below -1.
    """
    minimum = 0
    for element in root:
        if element.tag not in ("node", "way", "relation"):
            continue
        raw_id = element.get("id")
        if raw_id is None:
            continue
        try:
            value = int(raw_id)
        except ValueError:
            continue
        minimum = min(minimum, value)
    return minimum


def fuse_building_heights(
    osm_path: Path, dtm: BngWindow, dsm: BngWindow, grid: Ostn15Grid
) -> HeightsRecord:
    """Sample DSM minus DTM under every flat building in `osm_path` and
    write `height`, in place, atomically.

    Ways tagged `building=*` only (see the module docstring); a
    multipolygon relation is counted in `relations_skipped` and never
    looked at. A way that already carries a `height` tag is
    `kept_existing` and is never re-touched, which is what makes a second
    run over the same package idempotent by construction.

    Raises `HeightsError` for an `.osm` that cannot be read as the shape
    `mapgen.merge.merge_osm_xml` produces at all (missing declaration,
    unparseable XML); a footprint this module cannot make sense of on its
    own (unresolvable node, no OSTN15 coverage, too little raster data) is
    never an exception, only `no_data`, per the owner's own ruling that a
    tile with no data is not this module's failure to report.
    """
    path = Path(osm_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HeightsError(f"{path.name} could not be read: {exc}") from None
    declaration = _declaration_line(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise HeightsError(f"{path.name} is not valid XML: {exc}") from None

    nodes: dict[str, tuple[float, float]] = {}
    for element in root:
        if element.tag != "node":
            continue
        node_id = element.get("id")
        lat = element.get("lat")
        lon = element.get("lon")
        if node_id is None or lat is None or lon is None:
            continue
        try:
            nodes[node_id] = (float(lat), float(lon))
        except ValueError:
            continue

    buildings = 0
    written = 0
    kept_existing = 0
    no_data = 0
    relations_skipped = 0
    changed = False

    for element in root:
        if element.tag == "relation":
            relations_skipped += 1
            continue
        if element.tag != "way":
            continue
        tags = element.findall("tag")
        if not any(tag.get("k") == _BUILDING_TAG_KEY for tag in tags):
            continue
        buildings += 1
        if any(tag.get("k") == HEIGHT_TAG_KEY for tag in tags):
            kept_existing += 1
            continue

        ring = _footprint_ring(element, nodes)
        if ring is None:
            no_data += 1
            continue
        try:
            projected = [to_bng(lat, lon, grid) for lat, lon in ring]
        except BngError:
            # No OSTN15 coverage for this footprint: the same practical
            # fact as a raster with nothing under it, so it reads the same
            # way (see the module docstring).
            no_data += 1
            continue

        samples = _footprint_samples(projected, dtm, dsm)
        if len(samples) < MIN_SAMPLE_POINTS:
            no_data += 1
            continue

        dtm_values = [dtm_height for dtm_height, _ in samples]
        dsm_values = [dsm_height for _, dsm_height in samples]
        height = round(_p90(dsm_values) - _median(dtm_values), 1)
        if height < MIN_HEIGHT_METRES:
            no_data += 1
            continue

        height_tag = ET.SubElement(element, "tag")
        height_tag.set("k", HEIGHT_TAG_KEY)
        height_tag.set("v", f"{height:.1f}")
        source_tag = ET.SubElement(element, "tag")
        source_tag.set("k", SOURCE_HEIGHT_TAG_KEY)
        source_tag.set("v", SOURCE_HEIGHT_ATTRIBUTION)
        written += 1
        changed = True

    if changed:
        _rewrite_osm(path, declaration, root)

    return HeightsRecord(
        buildings=buildings,
        written=written,
        kept_existing=kept_existing,
        no_data=no_data,
        relations_skipped=relations_skipped,
    )
