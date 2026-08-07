"""Streaming feature readers for three OS Open GML products.

`iter_oml_features`, `iter_road_features` and `iter_greenspace_features`
each turn one already-open GML stream (a plain file handle, a
`zipfile.ZipExtFile`, or `io.BytesIO` over bytes `os_downloads.ZipReader.
read_member` returned; anything with a `.read()` method `xml.etree.
ElementTree.iterparse` accepts) into `OsFeature` records, one at a time,
never holding the whole parsed tree in memory: this module never opens a
file or a zip itself and never talks to the network, only `xml.etree.
ElementTree` and `dataclasses` from the standard library, per this task's
own stdlib-only constraint.

## The common wrapper, and why one walk serves all three products

Every OS Open GML file, whichever of these three products it comes from,
shares one outer shape (verified live 2026-08-07 against real downloads
of all three): root `os:FeatureCollection` (`os=
"http://namespaces.os.uk/product/1.0"`), a `gml:boundedBy` envelope, an
`os:metadata` element, then one `os:featureMember` per feature, each
wrapping exactly one product-specific element (`oml:Building`, `road:
RoadLink`, `ogsp:GreenspaceSite`, and so on). `_walk_features` is the one
generator that walks that shared shape; each public function only
supplies WHICH local feature-type names it wants read out of it (`OML_
TYPES` and the two private, equivalent maps below), and dispatches on the
GML LOCAL name only (`elem.tag.rsplit("}", 1)[-1]`), never the raw
Clark-notation tag, since the one thing that varies between products is
the namespace URI in front of it.

Mirrors `sources/inspire.py`'s own `parcels_in`/`ParcelStream` in every
way that module's own docstring documents deliberately: `ET.iterparse`
with BOTH `"start"` and `"end"` events so a reference to the root element
can be grabbed off the very first event (`_, root = next(context)`), then
`elem.clear(); root.clear()` for every `os:featureMember`, matched or
skipped alike, in a `finally` block that runs before the loop moves on to
the next one. A plain `events=("end",)` iterparse (which the rest of this
module's own task brief describes as its Step 4 shape) never hands back
a usable reference to the root at all in stdlib ElementTree: only a
`"start"` event fires while the root's own children list is still empty
and reachable, which is the ONE moment `root.clear()`'s discipline
depends on existing. This module takes inspire.py's own two-event shape
instead, precisely so `root.clear()` here means what it means there:
that a featureMember's own now-empty Element, not just its content, is
detached from the tree that keeps growing underneath `iterparse` as the
file streams past.

## Geometry: one shared reader for four GML shapes

`_geometry` walks a feature element's own subtree (`elem.iter()`, which
yields the feature element first and then every descendant in document
order) looking for the first `gml:Point`, `gml:LineString`, `gml:Surface`
or `gml:MultiSurface` it finds, regardless of what product-specific
wrapper element (`oml:geometry`, `net:centrelineGeometry`, `ogsp:
geometry`) holds it: every feature in every one of these three products
carries exactly one geometry, so "the first GML geometry element found
anywhere under this feature" is unambiguous, and reading it this way
means the three products' three different wrapper element names never
need their own separate case here.

The GeoJSON `type` each shape becomes follows the GML element that wraps
it, not the feature type: `gml:Surface` is always `"Polygon"` (one ring
list: exterior first, then any interior rings, `gml:interior` in document
order, exactly `parcels_ring`'s own convention in inspire.py), and
`gml:MultiSurface` is always `"MultiPolygon"` (one entry per `gml:
surfaceMember`, each entry itself a ring list shaped the same way a plain
Surface's own coordinates are), even for a MultiSurface wrapping only one
`gml:surfaceMember` (real, live-verified: `FunctionalSite` and
`GreenspaceSite` both do this for every real sample this task's own facts
recorded). A feature carrying none of the four known shapes raises
`OsOpenError` kind `"parse"` naming the feature's own local type: nothing
this project has verified live ever reaches this branch, but a fabricated
`{"type": None}` GeoJSON dict handed silently to Task 3's tiling code
would be a far worse failure to track down later.

## Property maps: absent means None, and RailwayTrack/RailwayTunnel's
## one asymmetry

Every property extractor reads a fixed, named set of child elements by
their Clark-notation tag and returns `None` for whichever one a given
feature simply does not carry (`_child_text`): OS Open's own GML never
writes an empty placeholder element for a field with no value, it leaves
the element out entirely, so "no such child" and "this property is not
set" are the same fact read the same way everywhere in this module.

RailwayTrack and RailwayTunnel are the one place the shape of the
returned dict itself, not just a value inside it, depends on what the
feature carries: RailwayTrack always returns `{"class": classification}`
(every real sample this project has seen carries one), but RailwayTunnel
falls back to `{"code": featureCode}` on the tunnels that carry no
classification at all (task-2-brief.md's own parenthetical, "tunnel may
have none"). The two are separate functions, `_railway_track_properties`
and `_railway_tunnel_properties`, rather than one shared one, precisely
because that fallback is documented as a TUNNEL-specific fact, not a
general one this module should also start applying to tracks the day one
turns up with no classification of its own.

`RoadLink`'s `"trunk"` and `"primary"` booleans follow the OS Roads
codelist's own convention directly: the element's text is the literal
string `"true"` or `"false"`, never absent on a real RoadLink (task-2-
facts.md's own census: present on all 363 of one real square), so
`trunkRoad_text == "true"` already reads correctly as False on a genuinely
absent element too, with no separate None-handling needed for either
field.

## Truncated files: OsOpenError, never a raw ParseError

`_walk_features` wraps its whole walk in one `except ET.ParseError`,
mirroring `ParcelStream._walk`'s own `except (zipfile.BadZipFile, ET.
ParseError)` in inspire.py: a GML stream truncated mid-transfer (Task 1's
own `download_entry` already guards against writing a short file to
DISK, but a caller streaming straight from a zip member the network cut
short has no such guard) raises `xml.etree.ElementTree.ParseError` only
once `iterparse` has read far enough to notice the document never closed,
which can be well after this generator has already yielded several good
features; those already-yielded features are real and are not withdrawn,
but the failure that ends the stream is always `OsOpenError` kind
`"parse"`, naming the OS Open product being read, never a URL (this
module is never given one to begin with: see the module's own inputs,
above) and never the raw stdlib exception.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Iterator

from mapgen.os_downloads import OsOpenError

_OS_NS = "http://namespaces.os.uk/product/1.0"
_GML_NS = "http://www.opengis.net/gml/3.2"
_OML_NS = "http://namespaces.os.uk/open/oml/1.0"
_ROAD_NS = "http://namespaces.os.uk/Open/Roads/1.0"
_OGSP_NS = "http://namespaces.ordnancesurvey.co.uk/Open/Greenspace/1.0"

_FEATURE_MEMBER_TAG = f"{{{_OS_NS}}}featureMember"

_GML_ID = f"{{{_GML_NS}}}id"
_GML_POINT = f"{{{_GML_NS}}}Point"
_GML_LINESTRING = f"{{{_GML_NS}}}LineString"
_GML_SURFACE = f"{{{_GML_NS}}}Surface"
_GML_MULTISURFACE = f"{{{_GML_NS}}}MultiSurface"
_GML_POS = f"{{{_GML_NS}}}pos"
_GML_POSLIST = f"{{{_GML_NS}}}posList"
_GML_EXTERIOR = f"{{{_GML_NS}}}exterior"
_GML_INTERIOR = f"{{{_GML_NS}}}interior"
_GML_LINEARRING = f"{{{_GML_NS}}}LinearRing"
_GML_PATCHES = f"{{{_GML_NS}}}patches"
_GML_POLYGONPATCH = f"{{{_GML_NS}}}PolygonPatch"
_GML_SURFACEMEMBER = f"{{{_GML_NS}}}surfaceMember"


@dataclass(frozen=True)
class OsFeature:
    """One feature read out of an OS Open GML stream.

    `feature_id` is the feature's own `gml:id` attribute verbatim (for
    example `"id6798B687-8592-42FB-929F-A62D5CCD3EA9"`), `feature_type`
    its local element name with no namespace prefix (for example
    `"Building"`), `geometry` a GeoJSON-shaped dict with British National
    Grid coordinates (`{"type": "Point" | "LineString" | "Polygon" |
    "MultiPolygon", "coordinates": [...]}`; see `_geometry`), and
    `properties` the fixed, per-type field map each reader documents (see
    `OML_TYPES` and the module docstring's "property maps" section).
    """

    feature_id: str
    feature_type: str
    geometry: dict
    properties: dict[str, object]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(elem: ET.Element, tag: str) -> str | None:
    child = elem.find(tag)
    if child is None:
        return None
    return child.text


# --------------------------------------------------------------------------
# Geometry: one reader shared by all three products.
# --------------------------------------------------------------------------


def _parse_pos_list(text: str) -> list[list[float]]:
    values = [float(token) for token in text.split()]
    return [[values[i], values[i + 1]] for i in range(0, len(values), 2)]


def _ring(container: ET.Element) -> list[list[float]]:
    linear_ring = container.find(_GML_LINEARRING)
    pos_list = linear_ring.find(_GML_POSLIST)
    return _parse_pos_list(pos_list.text)


def _surface_rings(surface_elem: ET.Element) -> list[list[list[float]]]:
    patch = surface_elem.find(f"{_GML_PATCHES}/{_GML_POLYGONPATCH}")
    rings = [_ring(patch.find(_GML_EXTERIOR))]
    for interior in patch.findall(_GML_INTERIOR):
        rings.append(_ring(interior))
    return rings


def _point_coordinates(point_elem: ET.Element) -> list[float]:
    x_text, y_text = point_elem.find(_GML_POS).text.split()
    return [float(x_text), float(y_text)]


def _geometry(feature_elem: ET.Element) -> dict:
    for candidate in feature_elem.iter():
        if candidate.tag == _GML_POINT:
            return {"type": "Point", "coordinates": _point_coordinates(candidate)}
        if candidate.tag == _GML_LINESTRING:
            pos_list = candidate.find(_GML_POSLIST)
            return {"type": "LineString", "coordinates": _parse_pos_list(pos_list.text)}
        if candidate.tag == _GML_SURFACE:
            return {"type": "Polygon", "coordinates": _surface_rings(candidate)}
        if candidate.tag == _GML_MULTISURFACE:
            polygons = [
                _surface_rings(member.find(_GML_SURFACE))
                for member in candidate.findall(_GML_SURFACEMEMBER)
            ]
            return {"type": "MultiPolygon", "coordinates": polygons}
    raise OsOpenError(
        f"{_local_name(feature_elem.tag)} {feature_elem.get(_GML_ID, '')!r} has no "
        f"recognised GML geometry (Point, LineString, Surface or MultiSurface).",
        kind="parse",
    )


# --------------------------------------------------------------------------
# OpenMapLocal property maps: OML_TYPES, the brief's own exact map.
# --------------------------------------------------------------------------


def _oml_code_only_properties(elem: ET.Element) -> dict[str, object]:
    return {"code": _child_text(elem, f"{{{_OML_NS}}}featureCode")}


def _important_building_properties(elem: ET.Element) -> dict[str, object]:
    return {
        "code": _child_text(elem, f"{{{_OML_NS}}}featureCode"),
        "theme": _child_text(elem, f"{{{_OML_NS}}}buildingTheme"),
        "class": _child_text(elem, f"{{{_OML_NS}}}classification"),
    }


def _functional_site_properties(elem: ET.Element) -> dict[str, object]:
    return {
        "name": _child_text(elem, f"{{{_OML_NS}}}distinctiveName"),
        "theme": _child_text(elem, f"{{{_OML_NS}}}siteTheme"),
        "class": _child_text(elem, f"{{{_OML_NS}}}classification"),
    }


def _named_and_classified_properties(elem: ET.Element) -> dict[str, object]:
    """NamedPlace and RailwayStation: both real elements captured live
    from the saved SS zip (task-2-report.md) confirm `distinctiveName`/
    `classification` exactly, so no property-map deviation from the
    plan's own assumption was needed for either type.
    """
    return {
        "name": _child_text(elem, f"{{{_OML_NS}}}distinctiveName"),
        "class": _child_text(elem, f"{{{_OML_NS}}}classification"),
    }


def _railway_track_properties(elem: ET.Element) -> dict[str, object]:
    return {"class": _child_text(elem, f"{{{_OML_NS}}}classification")}


def _railway_tunnel_properties(elem: ET.Element) -> dict[str, object]:
    classification = _child_text(elem, f"{{{_OML_NS}}}classification")
    if classification is not None:
        return {"class": classification}
    return {"code": _child_text(elem, f"{{{_OML_NS}}}featureCode")}


OML_TYPES: dict[str, Callable[[ET.Element], dict[str, object]]] = {
    "Building": _oml_code_only_properties,
    "ImportantBuilding": _important_building_properties,
    "FunctionalSite": _functional_site_properties,
    "Woodland": _oml_code_only_properties,
    "SurfaceWater_Area": _oml_code_only_properties,
    "SurfaceWater_Line": _oml_code_only_properties,
    "TidalWater": _oml_code_only_properties,
    "Foreshore": _oml_code_only_properties,
    "RailwayTrack": _railway_track_properties,
    "RailwayTunnel": _railway_tunnel_properties,
    "RailwayStation": _named_and_classified_properties,
    "NamedPlace": _named_and_classified_properties,
}


# --------------------------------------------------------------------------
# OpenRoads property map: RoadLink only.
# --------------------------------------------------------------------------


def _road_link_properties(elem: ET.Element) -> dict[str, object]:
    length_text = _child_text(elem, f"{{{_ROAD_NS}}}length")
    trunk_text = _child_text(elem, f"{{{_ROAD_NS}}}trunkRoad")
    primary_text = _child_text(elem, f"{{{_ROAD_NS}}}primaryRoute")
    return {
        "class": _child_text(elem, f"{{{_ROAD_NS}}}roadClassification"),
        "function": _child_text(elem, f"{{{_ROAD_NS}}}roadFunction"),
        "form": _child_text(elem, f"{{{_ROAD_NS}}}formOfWay"),
        "name": _child_text(elem, f"{{{_ROAD_NS}}}name1"),
        "number": _child_text(elem, f"{{{_ROAD_NS}}}roadClassificationNumber"),
        "trunk": trunk_text == "true",
        "primary": primary_text == "true",
        "length": float(length_text) if length_text is not None else None,
    }


_ROAD_TYPES: dict[str, Callable[[ET.Element], dict[str, object]]] = {
    "RoadLink": _road_link_properties,
}


# --------------------------------------------------------------------------
# OpenGreenspace property maps: GreenspaceSite and AccessPoint.
# --------------------------------------------------------------------------


def _greenspace_site_properties(elem: ET.Element) -> dict[str, object]:
    return {
        "function": _child_text(elem, f"{{{_OGSP_NS}}}function"),
        "name": _child_text(elem, f"{{{_OGSP_NS}}}distinctiveName1"),
    }


def _access_point_properties(elem: ET.Element) -> dict[str, object]:
    return {
        "access": _child_text(elem, f"{{{_OGSP_NS}}}accessType"),
        "site": _child_text(elem, f"{{{_OGSP_NS}}}refToGreenspaceSite"),
    }


_GREENSPACE_TYPES: dict[str, Callable[[ET.Element], dict[str, object]]] = {
    "GreenspaceSite": _greenspace_site_properties,
    "AccessPoint": _access_point_properties,
}


# --------------------------------------------------------------------------
# The shared walk, and the three public readers.
# --------------------------------------------------------------------------


def _walk_features(
    fh, wanted: dict[str, Callable[[ET.Element], dict[str, object]]], product: str
) -> Iterator[OsFeature]:
    try:
        context = ET.iterparse(fh, events=("start", "end"))
        _, root = next(context)
        for event, elem in context:
            if event != "end" or elem.tag != _FEATURE_MEMBER_TAG:
                continue
            try:
                if len(elem) == 0:
                    continue
                feature_elem = elem[0]
                local_name = _local_name(feature_elem.tag)
                extractor = wanted.get(local_name)
                if extractor is None:
                    continue
                feature = OsFeature(
                    feature_id=feature_elem.get(_GML_ID, ""),
                    feature_type=local_name,
                    geometry=_geometry(feature_elem),
                    properties=extractor(feature_elem),
                )
            finally:
                elem.clear()
                root.clear()
            yield feature
    except ET.ParseError:
        raise OsOpenError(
            f"This {product} GML file is not well-formed XML and could not "
            f"be parsed (the download is likely truncated or corrupt).",
            kind="parse",
        ) from None


def iter_oml_features(fh) -> Iterator[OsFeature]:
    """Every OpenMapLocal feature in `fh` whose local element name is a key
    of `OML_TYPES`, in document order; every other feature type present
    (`Road`, `CarChargingPoint`, `Roundabout` and others; see task-2-
    facts.md's own SS census) is skipped.
    """
    yield from _walk_features(fh, OML_TYPES, "OpenMapLocal")


def iter_road_features(fh) -> Iterator[OsFeature]:
    """Every `road:RoadLink` in `fh`; `road:RoadNode` and `road:
    MotorwayJunction`, the other two feature types OpenRoads publishes,
    are skipped.
    """
    yield from _walk_features(fh, _ROAD_TYPES, "OpenRoads")


def iter_greenspace_features(fh) -> Iterator[OsFeature]:
    """Every `ogsp:GreenspaceSite` and `ogsp:AccessPoint` in `fh`."""
    yield from _walk_features(fh, _GREENSPACE_TYPES, "OpenGreenspace")
