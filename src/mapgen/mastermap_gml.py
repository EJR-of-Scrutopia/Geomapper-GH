"""A streaming reader for OS MasterMap Topography Layer GML.

`iter_topography_features(source)` turns one MasterMap Topography Layer
GML file, either a file path (`str`/`Path`, which this module opens and
closes itself) or an already-open binary stream (anything `xml.etree.
ElementTree.iterparse` accepts), into `OsFeature` records, one at a time,
never holding the whole parsed tree in memory. `OsFeature` is `os_gml.py`'s
own dataclass, imported here, never redefined: MasterMap Topography and
the three OS Open products this project already reads (`os_gml.py`) hand
back the same shape so a caller never has to branch on which product a
feature came from. Only `xml.etree.ElementTree`, `dataclasses` (by way of
importing `OsFeature`) and `pathlib` from the standard library; this
module never talks to the network and never writes to disk.

## Wired into nothing, on purpose, for two future consumers

This module is not called from anywhere in this plan: no CLI subcommand,
no package-build step. It exists for two consumers that come later, both
named in `docs/superpowers/specs/2026-08-07-mapgen-phase2b-addendum-
design.md`'s own "Shared machinery" section:

1. Item F's benchmark (`benchmark.py`) may, in a later task, compare
   mapgen's open-stack outputs against an OWNER-PURCHASED MasterMap
   extract as a second, licensed ground truth alongside OS NGD Premium,
   the same way it already compares against NGD. That comparison would
   read the extract through this module.
2. A deferred, not-yet-built premium IMPORT path (a client-licensed
   MasterMap extract dropped into a package as its top service tier)
   will consume this module's features directly. Import stays deferred;
   only the parser is built now, ahead of it.

Both consumers are downstream of the same binding rule this project's own
benchmark work already lives under, repeated here because this module is
the one piece of MasterMap-reading machinery that predates the consumer
that will finally need it: nothing from any OS premium product may enter
a package, a fusion, a shard cache, or any output a client could receive.
A MasterMap extract read through this module is licensed, owner-purchased
data; nothing this module returns is written to a package by this module
itself, and nothing calls it yet to do so by accident either.

## GML 2.1.2, not GML 3.2: two coordinate encodings, one geometry shape

MasterMap Topography Layer GML predates OpenMapLocal's GML 3.2 (`os_gml.
py`'s own product family): OS's own published GML-examples page shows
coordinates as `<gml:coordinates>x,y x,y ...</gml:coordinates>` (a comma
WITHIN a pair, a space BETWEEN pairs), the GML 2.1.2 convention, rather
than GML 3.2's flat, space-only `<gml:posList>x y x y ...</gml:posList>`.
Real extracts vary by era and export tool, so `_coordinate_pairs` reads
BOTH forms (and, for a single point, `<gml:pos>x y</gml:pos>` too),
dispatching on the coordinate element's own local name, never assuming
one or the other. A present-but-blank element of either kind (`""` or
whitespace only) raises rather than silently becoming a fabricated empty
ring or empty LineString: `str.split()` never raises on its own for either
shape, so this module checks explicitly before ever building a token list,
exactly the gap `os_gml.py`'s own `_parse_pos_list` documents finding and
closing in itself.

Geometry follows GML 2's ring vocabulary, not GML 3's: a `TopographicArea`
carries a `gml:Polygon` with `gml:outerBoundaryIs`/`gml:innerBoundaryIs`
(GML 2.1.2), not `gml:Surface` with `gml:exterior`/`gml:interior` (GML
3.2, `os_gml.py`'s own). One outer ring, then zero or more inner rings in
document order, exactly `os_gml.py`'s own `[exterior, *interiors]`
convention for what a GeoJSON `"Polygon"`'s ring list holds, just reached
by a different pair of GML element names underneath.

## Namespace tolerance: local names, all the way down

MasterMap's own namespace URI (`http://www.ordnancesurvey.co.uk/xml/
namespaces/osgb`, live-verified against OS's own docs) has held steady
for years, but the GML namespace it wraps has not: GML 2.1.2's own
namespace URI is the plain, version-less `http://www.opengis.net/gml`,
distinct from OpenMapLocal's versioned `http://www.opengis.net/gml/3.2`
that `os_gml.py` hardcodes into every one of its own Clark-notation
constants. Hardcoding either GML URI here would make this module correct
for exactly one vintage of MasterMap export and silently blind to any
other. Every element this module looks for, the root, `osgb:
topographicMember`, the three feature types, the geometry shapes, the ring
wrappers, and the coordinate elements, is therefore matched on its LOCAL
name only (`_local_name`, `tag.rsplit("}", 1)[-1]`), the same discipline
`os_gml.py`'s own module docstring describes for tolerating three
DIFFERENT OS Open product namespaces from one shared walk; here the
namespace that varies is GML's own, not the product's.

## toid: the fid attribute is the TOID, not a separate element

OS's own documentation states this plainly: "The TOID of the feature is
provided in the XML attribute 'fid' of the osgb:Feature element." A real
MasterMap extract therefore never carries a distinct `osgb:toid` element;
this module's own `_toid` still checks for one, after `fid` and after a
GML 3.2-style `gml:id` attribute (in that order), purely so a feature from
some future export tool that DOES carry one is not silently read as
toid-less. `feature_id` (the shared `OsFeature` field every one of this
project's GML readers fills) and `properties["toid"]` therefore carry the
same value on every real feature this module has been shown: this is
deliberate, not a redundancy to trim, since a caller reading `properties`
alone (the shape `benchmark.py`'s own report-building code already reads
for every other property) should never have to reach into `feature_id`
just to find the one property MasterMap features are always keyed by.

## Root validation and whole-stream failure

Mirrors `os_gml.py`'s own two contracts exactly: the root element's local
name is checked against `"FeatureCollection"` immediately after obtaining
it, before looking at a single `topographicMember`, raising `MasterMapError`
naming what was found instead of naming a URL; and a feature whose GML
content cannot be read (an empty coordinate element, a non-numeric token,
an odd-length posList, a feature carrying none of the three recognised
geometry shapes) fails the WHOLE stream rather than being counted and
skipped, on the same reasoning `os_gml.py`'s own module docstring gives at
length: a single corrupted feature is evidence a transfer went wrong, not
evidence about one row, so continuing past it risks handing a caller
silently-wrong geometry a bit-flip corrupted without tripping any
exception at all. A stream that is not well-formed XML at all (`xml.etree.
ElementTree.ParseError`) is likewise never re-raised raw; it becomes
`MasterMapError` naming the stream, never a URL (this module is never
given one to begin with).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

from mapgen.os_gml import OsFeature

_WANTED_TYPES = {"TopographicArea", "TopographicLine", "TopographicPoint"}


class MasterMapError(RuntimeError):
    """Raised for a MasterMap Topography GML stream this module cannot
    parse: malformed XML, a wrong root element, or feature content that
    cannot be read (an empty coordinate element, a non-numeric token, an
    odd-length posList, or a feature with no recognised geometry). Never
    carries a URL: this module is never handed one to begin with.
    """


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _local_child(elem: ET.Element, *local_names: str) -> ET.Element | None:
    for child in elem:
        if _local_name(child.tag) in local_names:
            return child
    return None


def _local_attr(elem: ET.Element, local_name: str) -> str | None:
    for key, value in elem.attrib.items():
        if _local_name(key) == local_name:
            return value
    return None


def _child_text_by_local_name(elem: ET.Element, local_name: str) -> str | None:
    child = _local_child(elem, local_name)
    if child is None:
        return None
    return child.text


# --------------------------------------------------------------------------
# Coordinates: gml:coordinates (comma within a pair, space between pairs),
# gml:posList and gml:pos (space-separated, flat), all in one reader.
# --------------------------------------------------------------------------


def _coordinate_pairs(coord_elem: ET.Element) -> list[list[float]]:
    local = _local_name(coord_elem.tag)
    text = coord_elem.text
    if text is None or not text.strip():
        # A present-but-blank element ("" or whitespace only) splits to an
        # empty token list either way, which would otherwise silently
        # become a fabricated empty ring or empty LineString rather than
        # an error: str.split() never raises on its own. See the module
        # docstring's own "two coordinate encodings" section.
        raise ValueError(f"a {local!r} element carries no coordinate values")
    if local in ("posList", "pos"):
        values = [float(token) for token in text.split()]
        if len(values) % 2 != 0:
            raise ValueError(f"a {local!r} element carries an odd number of coordinate values")
        return [[values[i], values[i + 1]] for i in range(0, len(values), 2)]
    if local == "coordinates":
        pairs = []
        for token in text.split():
            x_text, y_text = token.split(",")
            pairs.append([float(x_text), float(y_text)])
        return pairs
    raise ValueError(f"{local!r} is not a recognised coordinate element")


def _point_coordinates(point_elem: ET.Element) -> list[float]:
    coord_elem = _local_child(point_elem, "coordinates", "pos")
    return _coordinate_pairs(coord_elem)[0]


def _ring(boundary_elem: ET.Element) -> list[list[float]]:
    linear_ring = _local_child(boundary_elem, "LinearRing")
    coord_elem = _local_child(linear_ring, "coordinates", "posList")
    return _coordinate_pairs(coord_elem)


def _polygon_rings(polygon_elem: ET.Element) -> list[list[list[float]]]:
    outer = _local_child(polygon_elem, "outerBoundaryIs")
    rings = [_ring(outer)]
    for child in polygon_elem:
        if _local_name(child.tag) == "innerBoundaryIs":
            rings.append(_ring(child))
    return rings


def _geometry(feature_elem: ET.Element) -> dict:
    for candidate in feature_elem.iter():
        local = _local_name(candidate.tag)
        if local == "Point":
            return {"type": "Point", "coordinates": _point_coordinates(candidate)}
        if local == "LineString":
            coord_elem = _local_child(candidate, "coordinates", "posList")
            return {"type": "LineString", "coordinates": _coordinate_pairs(coord_elem)}
        if local == "Polygon":
            return {"type": "Polygon", "coordinates": _polygon_rings(candidate)}
    raise MasterMapError(
        f"{_local_name(feature_elem.tag)} {_local_attr(feature_elem, 'fid') or ''!r} has "
        "no recognised GML geometry (Point, LineString or Polygon)."
    )


# --------------------------------------------------------------------------
# toid: the fid attribute (real MasterMap's own convention), a gml:id
# attribute (GML 3.2-style, tolerated in case some export carries one
# instead), or an osgb:toid child element, whichever the feature carries.
# --------------------------------------------------------------------------


def _toid(feature_elem: ET.Element) -> str | None:
    fid = _local_attr(feature_elem, "fid")
    if fid is not None:
        return fid
    gml_id = _local_attr(feature_elem, "id")
    if gml_id is not None:
        return gml_id
    return _child_text_by_local_name(feature_elem, "toid")


def _properties(feature_elem: ET.Element) -> dict[str, object]:
    return {
        "descriptiveGroup": _child_text_by_local_name(feature_elem, "descriptiveGroup"),
        "descriptiveTerm": _child_text_by_local_name(feature_elem, "descriptiveTerm"),
        "theme": _child_text_by_local_name(feature_elem, "theme"),
        "toid": _toid(feature_elem),
    }


# --------------------------------------------------------------------------
# The walk: iterparse with start+end events, root validation, whole-stream
# failure on malformed content, element cleanup after every member.
# --------------------------------------------------------------------------


def _walk_features(fh) -> Iterator[OsFeature]:
    try:
        context = ET.iterparse(fh, events=("start", "end"))
        _, root = next(context)
        if _local_name(root.tag) != "FeatureCollection":
            raise MasterMapError(
                "This does not look like a MasterMap Topography GML file: its root "
                f"element is {_local_name(root.tag)!r}, not 'FeatureCollection' in the "
                "MasterMap namespace every MasterMap Topography GML file uses."
            )
        for event, elem in context:
            if event != "end" or _local_name(elem.tag) != "topographicMember":
                continue
            try:
                if len(elem) == 0:
                    continue
                feature_elem = elem[0]
                local_name = _local_name(feature_elem.tag)
                if local_name not in _WANTED_TYPES:
                    continue
                try:
                    feature = OsFeature(
                        feature_id=_toid(feature_elem) or "",
                        feature_type=local_name,
                        geometry=_geometry(feature_elem),
                        properties=_properties(feature_elem),
                    )
                except MasterMapError:
                    raise
                except Exception:
                    # A deliberate catch-all, not a named list of exception
                    # types, for the same reason os_gml.py's own
                    # _walk_features gives at length in its module
                    # docstring's "malformed feature content" section:
                    # enumerating "the exception types we happened to
                    # think of" has already been proven, on that exact
                    # task, to miss one.
                    raise MasterMapError(
                        f"This MasterMap Topography GML file's {local_name} "
                        f"{_local_attr(feature_elem, 'fid') or ''!r} carries GML content "
                        "this reader could not parse (a malformed geometry or property "
                        "value)."
                    ) from None
            finally:
                elem.clear()
                root.clear()
            yield feature
    except ET.ParseError:
        raise MasterMapError(
            "This MasterMap Topography GML stream is not well-formed XML and could not "
            "be parsed (the stream is likely truncated or corrupt)."
        ) from None


def iter_topography_features(source) -> Iterator[OsFeature]:
    """Every `osgb:TopographicArea`, `osgb:TopographicLine` and `osgb:
    TopographicPoint` in `source`, in document order, as `OsFeature`
    records (`feature_type` the member's own local element name;
    `geometry` GeoJSON-shaped with BNG coordinates; `properties` carrying
    `descriptiveGroup`, `descriptiveTerm`, `theme` and `toid`, `None` for
    whichever a given feature does not carry). Every other MasterMap
    Topography member type (`boundaryMember`, `cartographicMember`,
    `departedMember`) is skipped, exactly like `os_gml.py`'s own readers
    skip feature types not in their own wanted set.

    `source` is either a file path (`str` or `pathlib.Path`, which this
    module opens for reading and closes itself, unlike `os_gml.py`'s own
    readers, which never open a file of their own) or an already-open
    binary stream (anything `xml.etree.ElementTree.iterparse` accepts);
    a caller handing this a stream still owns closing it.
    """
    if isinstance(source, (str, Path)):
        with open(source, "rb") as fh:
            yield from _walk_features(fh)
    else:
        yield from _walk_features(source)
