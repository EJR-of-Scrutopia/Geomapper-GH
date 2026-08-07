"""os_gml.py's suite: the three streaming GML feature readers.

Every test here reads a committed fixture (tests/fixtures/osopen/*.gml,
built by make_gml_fixtures.py) from a plain `open(path, "rb")` handle,
never a zip: Task 1's ZipReader and the OS Downloads client are what hand
this module a member stream in production, but this module itself takes
any binary file-like object, so a plain file handle exercises the same
interface without dragging a zip into every test.

No test here ever puts a URL into an assertion string it expects an
OsOpenError's own message to contain, matching test_os_downloads.py's own
rule for the same reason (this module never has a URL to begin with; see
os_gml.py's own module docstring).
"""

from __future__ import annotations

import inspect
import io
from pathlib import Path

import pytest

from mapgen import os_gml
from mapgen.os_downloads import OsOpenError

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "osopen"
_OML_SAMPLE = _FIXTURE_DIR / "oml_sample.gml"
_ROADS_SAMPLE = _FIXTURE_DIR / "roads_sample.gml"
_GREENSPACE_SAMPLE = _FIXTURE_DIR / "greenspace_sample.gml"


# --------------------------------------------------------------------------
# OML_TYPES itself: the exact set of feature type names the brief names.
# --------------------------------------------------------------------------


def test_oml_types_names_exactly_the_brief_s_twelve_feature_types():
    assert set(os_gml.OML_TYPES) == {
        "Building",
        "ImportantBuilding",
        "FunctionalSite",
        "Woodland",
        "SurfaceWater_Area",
        "SurfaceWater_Line",
        "TidalWater",
        "Foreshore",
        "RailwayTrack",
        "RailwayTunnel",
        "RailwayStation",
        "NamedPlace",
    }


# --------------------------------------------------------------------------
# Laziness: each public reader must itself be a generator function (Step 2's
# own marker), not a wrapper that returns a pre-built generator or list.
# --------------------------------------------------------------------------


def test_iter_oml_features_is_a_generator_function():
    assert inspect.isgeneratorfunction(os_gml.iter_oml_features)


def test_iter_road_features_is_a_generator_function():
    assert inspect.isgeneratorfunction(os_gml.iter_road_features)


def test_iter_greenspace_features_is_a_generator_function():
    assert inspect.isgeneratorfunction(os_gml.iter_greenspace_features)


# --------------------------------------------------------------------------
# iter_oml_features: feature counts, skipping, geometry and properties.
# --------------------------------------------------------------------------


def _oml_features():
    with _OML_SAMPLE.open("rb") as fh:
        return list(os_gml.iter_oml_features(fh))


def test_iter_oml_features_yields_exactly_the_six_wanted_feature_types_once_each():
    features = _oml_features()
    counts = {}
    for feature in features:
        counts[feature.feature_type] = counts.get(feature.feature_type, 0) + 1
    assert counts == {
        "Building": 1,
        "ImportantBuilding": 1,
        "FunctionalSite": 1,
        "Woodland": 1,
        "RailwayTrack": 1,
        "NamedPlace": 1,
    }


def test_iter_oml_features_skips_a_feature_type_not_in_oml_types():
    # The fixture also carries one oml:Road, OpenMapLocal's own second most
    # common real feature type (66,189 over the real SS tile), which is not
    # in OML_TYPES and must never be yielded.
    feature_types = {feature.feature_type for feature in _oml_features()}
    assert "Road" not in feature_types


def test_building_polygon_exterior_ring_closes_and_matches_the_fixture_verbatim():
    building = next(f for f in _oml_features() if f.feature_type == "Building")
    assert building.feature_id == "id6798B687-8592-42FB-929F-A62D5CCD3EA9"
    assert building.geometry["type"] == "Polygon"
    exterior_ring = building.geometry["coordinates"][0]
    assert exterior_ring == [
        [286268.63, 190853.9],
        [286258.67, 190848.07],
        [286282.66, 190804.33],
        [286293.15, 190810.13],
        [286268.63, 190853.9],
    ]
    assert exterior_ring[0] == exterior_ring[-1]
    assert building.properties == {"code": "15014"}


def test_important_building_properties_carry_theme_and_classification():
    feature = next(f for f in _oml_features() if f.feature_type == "ImportantBuilding")
    assert feature.properties == {
        "code": "15025",
        "theme": "Religious Buildings",
        "class": "Place Of Worship",
    }


def test_functional_site_hole_arrives_as_a_second_ring():
    feature = next(f for f in _oml_features() if f.feature_type == "FunctionalSite")
    assert feature.geometry["type"] == "MultiPolygon"
    rings = feature.geometry["coordinates"][0]
    assert len(rings) == 2
    exterior_ring, interior_ring = rings
    assert exterior_ring == [
        [300000.0, 180000.0],
        [300050.0, 180000.0],
        [300050.0, 180040.0],
        [300000.0, 180040.0],
        [300000.0, 180000.0],
    ]
    assert interior_ring == [
        [300010.0, 180010.0],
        [300020.0, 180010.0],
        [300020.0, 180020.0],
        [300010.0, 180020.0],
        [300010.0, 180010.0],
    ]
    assert feature.properties == {
        "name": "Silverton Church of England Primary School",
        "theme": "Education",
        "class": "Primary Education",
    }


def test_woodland_properties_are_code_only():
    feature = next(f for f in _oml_features() if f.feature_type == "Woodland")
    assert feature.properties == {"code": "10071"}
    assert feature.geometry["type"] == "Polygon"


def test_railway_track_geometry_is_a_linestring_with_class_only():
    feature = next(f for f in _oml_features() if f.feature_type == "RailwayTrack")
    assert feature.geometry == {
        "type": "LineString",
        "coordinates": [
            [297006.92, 168951.45],
            [296887.86, 169139.32],
            [296833.32, 169201.14],
            [296762.79, 169258.93],
        ],
    }
    assert feature.properties == {"class": "Multi Track"}


def test_named_place_is_a_point_with_name_and_class():
    feature = next(f for f in _oml_features() if f.feature_type == "NamedPlace")
    assert feature.geometry == {"type": "Point", "coordinates": [295661.0, 197917.0]}
    assert feature.properties == {"name": "Atlantic View", "class": "Populated Place"}


# --------------------------------------------------------------------------
# RailwayTrack/RailwayTunnel's either/or property shape: classification
# when present, featureCode when a tunnel has none. Not in the committed
# fixture (task-2-brief.md's Step 1 fixture list has no RailwayTunnel), so
# built inline, minimally, purely to exercise the fallback branch.
# --------------------------------------------------------------------------

_TUNNEL_WRAPPER_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<os:FeatureCollection xmlns:os="http://namespaces.os.uk/product/1.0" '
    'xmlns:oml="http://namespaces.os.uk/open/oml/1.0" '
    'xmlns:gml="http://www.opengis.net/gml/3.2">'
)
_TUNNEL_WRAPPER_TAIL = "</os:FeatureCollection>"

_TUNNEL_WITH_CLASSIFICATION = (
    "<os:featureMember><oml:RailwayTunnel gml:id=\"idT0000001-0000-0000-0000-000000000000\">"
    '<oml:classification codeSpace="http://www.os.uk/xml/codelists/map/RailwayTrackClassificationOML.xml">'
    "Single Track</oml:classification>"
    "<oml:geometry><gml:LineString "
    'gml:id="idT0000001-0000-0000-0000-000000000000-0" '
    'srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">'
    "<gml:posList>290000 165000 290010 165005</gml:posList>"
    "</gml:LineString></oml:geometry>"
    "</oml:RailwayTunnel></os:featureMember>"
)

_TUNNEL_WITHOUT_CLASSIFICATION = (
    "<os:featureMember><oml:RailwayTunnel gml:id=\"idT0000002-0000-0000-0000-000000000000\">"
    "<oml:geometry><gml:LineString "
    'gml:id="idT0000002-0000-0000-0000-000000000000-0" '
    'srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">'
    "<gml:posList>291000 165000 291010 165005</gml:posList>"
    "</gml:LineString></oml:geometry>"
    "<oml:featureCode>15304</oml:featureCode>"
    "</oml:RailwayTunnel></os:featureMember>"
)


def test_railway_tunnel_uses_classification_when_present():
    body = _TUNNEL_WRAPPER_HEAD + _TUNNEL_WITH_CLASSIFICATION + _TUNNEL_WRAPPER_TAIL
    (feature,) = list(os_gml.iter_oml_features(io.BytesIO(body.encode("utf-8"))))
    assert feature.properties == {"class": "Single Track"}


def test_railway_tunnel_falls_back_to_feature_code_when_classification_is_absent():
    body = _TUNNEL_WRAPPER_HEAD + _TUNNEL_WITHOUT_CLASSIFICATION + _TUNNEL_WRAPPER_TAIL
    (feature,) = list(os_gml.iter_oml_features(io.BytesIO(body.encode("utf-8"))))
    assert feature.properties == {"code": "15304"}


# --------------------------------------------------------------------------
# iter_road_features: RoadLink only, boolean parsing, absent name1/number.
# --------------------------------------------------------------------------


def _road_features():
    with _ROADS_SAMPLE.open("rb") as fh:
        return list(os_gml.iter_road_features(fh))


def test_iter_road_features_yields_exactly_two_roadlinks():
    features = _road_features()
    assert len(features) == 2
    assert all(feature.feature_type == "RoadLink" for feature in features)


def test_roadlink_without_name_or_number_parses_trunk_and_primary_false_and_name_none():
    feature = next(
        f for f in _road_features() if f.feature_id == "idFCAB4420-B9EA-42E2-923C-BAAFA790F1AC"
    )
    assert feature.properties == {
        "class": "Unknown",
        "function": "Restricted Local Access Road",
        "form": "Single Carriageway",
        "name": None,
        "number": None,
        "trunk": False,
        "primary": False,
        "length": 625.0,
    }
    assert feature.geometry["type"] == "LineString"
    assert feature.geometry["coordinates"][0] == [300000.0, 170000.0]
    assert feature.geometry["coordinates"][-1] == [300369.0, 170495.14]


def test_roadlink_with_name_and_number_parses_trunk_and_primary_true():
    feature = next(
        f for f in _road_features() if f.feature_id == "id0A1B2C3D-4E5F-6789-ABCD-EF0123456789"
    )
    assert feature.properties == {
        "class": "A Road",
        "function": "A Road",
        "form": "Single Carriageway",
        "name": "Culver Way",
        "number": "A4050",
        "trunk": True,
        "primary": True,
        "length": 812.0,
    }


# --------------------------------------------------------------------------
# iter_greenspace_features: GreenspaceSite and AccessPoint.
# --------------------------------------------------------------------------


def _greenspace_features():
    with _GREENSPACE_SAMPLE.open("rb") as fh:
        return list(os_gml.iter_greenspace_features(fh))


def test_iter_greenspace_features_yields_site_and_access_point():
    features = _greenspace_features()
    types = sorted(feature.feature_type for feature in features)
    assert types == ["AccessPoint", "GreenspaceSite"]


def test_greenspace_site_properties_and_multipolygon_geometry():
    feature = next(f for f in _greenspace_features() if f.feature_type == "GreenspaceSite")
    assert feature.properties == {"function": "Public Park Or Garden", "name": "Killerton Gardens"}
    assert feature.geometry["type"] == "MultiPolygon"
    assert feature.geometry["coordinates"] == [
        [
            [
                [297393.08, 170106.74],
                [297420.0, 170106.74],
                [297420.0, 170130.0],
                [297393.08, 170130.0],
                [297393.08, 170106.74],
            ]
        ]
    ]


def test_access_point_yields_a_point_with_access_and_site_properties():
    feature = next(f for f in _greenspace_features() if f.feature_type == "AccessPoint")
    assert feature.geometry == {"type": "Point", "coordinates": [300000.0, 199670.71]}
    assert feature.properties == {
        "access": "Pedestrian",
        "site": "id49E9C737-AEA2-A491-E063-8CCAA00A0EF3",
    }


# --------------------------------------------------------------------------
# Truncated GML: never a raw xml.etree.ElementTree.ParseError.
# --------------------------------------------------------------------------


def test_truncated_oml_gml_raises_os_open_error_kind_parse_not_a_raw_parse_error():
    data = _OML_SAMPLE.read_bytes()
    truncated = data[: len(data) // 2]
    with pytest.raises(OsOpenError) as exc_info:
        list(os_gml.iter_oml_features(io.BytesIO(truncated)))
    assert exc_info.value.kind == "parse"


def test_truncated_roads_gml_raises_os_open_error_kind_parse():
    data = _ROADS_SAMPLE.read_bytes()
    truncated = data[: len(data) // 2]
    with pytest.raises(OsOpenError) as exc_info:
        list(os_gml.iter_road_features(io.BytesIO(truncated)))
    assert exc_info.value.kind == "parse"


def test_truncated_greenspace_gml_raises_os_open_error_kind_parse():
    data = _GREENSPACE_SAMPLE.read_bytes()
    truncated = data[: len(data) // 2]
    with pytest.raises(OsOpenError) as exc_info:
        list(os_gml.iter_greenspace_features(io.BytesIO(truncated)))
    assert exc_info.value.kind == "parse"


# --------------------------------------------------------------------------
# Memory discipline, behavioural half: a large synthetic fixture is not
# fully materialised before the first feature is available. Step 2's own
# marker (inspect.isgeneratorfunction, above) already covers this
# structurally; this is the behavioural half the task's own instructions
# ask for as a cross-check, consuming exactly one feature from a fixture
# built large enough that "the whole thing parsed eagerly" and "one
# feature parsed lazily" would visibly differ in how much of the source
# stream has been read.
# --------------------------------------------------------------------------


def _large_oml_gml_bytes(building_count: int) -> bytes:
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<os:FeatureCollection xmlns:os="http://namespaces.os.uk/product/1.0" '
        'xmlns:oml="http://namespaces.os.uk/open/oml/1.0" '
        'xmlns:gml="http://www.opengis.net/gml/3.2">'
    )
    building_template = (
        '<os:featureMember><oml:Building gml:id="id{n:012d}">'
        "<oml:geometry><gml:Surface "
        'gml:id="id{n:012d}-0" srsName="urn:ogc:def:crs:EPSG::27700" srsDimension="2">'
        "<gml:patches><gml:PolygonPatch><gml:exterior><gml:LinearRing>"
        "<gml:posList>290000 165000 290010 165000 290010 165010 290000 165010 290000 165000"
        "</gml:posList></gml:LinearRing></gml:exterior></gml:PolygonPatch></gml:patches>"
        "</gml:Surface></oml:geometry>"
        "<oml:featureCode>15014</oml:featureCode>"
        "</oml:Building></os:featureMember>"
    )
    tail = "</os:FeatureCollection>"
    body = "".join(building_template.format(n=n) for n in range(building_count))
    return (head + body + tail).encode("utf-8")


def test_iter_oml_features_yields_one_feature_without_reading_the_whole_stream():
    # 20,000 Buildings is far bigger than any single ET.iterparse read
    # chunk (16 KB): if this reader built the whole tree eagerly before
    # yielding its first feature, fh.tell() after one next() would already
    # be at or near the end of the stream. A correctly lazy generator
    # advances the parser only as far as the first complete feature needs.
    data = _large_oml_gml_bytes(20_000)
    assert len(data) > 1_000_000
    fh = io.BytesIO(data)
    iterator = os_gml.iter_oml_features(fh)
    first = next(iterator)
    assert first.feature_type == "Building"
    position_after_one = fh.tell()
    assert position_after_one < len(data) // 2
