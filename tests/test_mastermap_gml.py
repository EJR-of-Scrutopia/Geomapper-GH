"""mastermap_gml.py's suite: the reusable Topography Layer GML reader.

No committed fixture file: the build-time hunt for OS's own keyless
MasterMap Topography sample data (task-4-report.md records what was
tried) found only documentation pages, never a direct, unauthenticated
zip a plain `urllib` call could pull, so this suite follows the task's
own pinned fallback and writes its fixture inline, informed by the real
element and namespace names OS's own published GML-examples page shows
verbatim (`osgb:topographicMember`, the `fid` attribute as TOID, the
`osgb:polygon`/`osgb:polyline`/`osgb:point` geometry wrappers, `gml:
outerBoundaryIs`/`innerBoundaryIs`, and the plain, version-less GML 2.1.2
namespace `http://www.opengis.net/gml`, distinct from OpenMapLocal's GML
3.2 `http://www.opengis.net/gml/3.2`).

No test here ever puts a URL into an assertion string it expects a
MasterMapError's own message to contain: this module never has one to
begin with (see its own module docstring).
"""

from __future__ import annotations

import io
import tracemalloc
from pathlib import Path

import pytest

from mapgen import mastermap_gml
from mapgen.mastermap_gml import MasterMapError
from mapgen.os_gml import OsFeature

_GML_NS = "http://www.opengis.net/gml"

# The real MasterMap namespace URI, live-verified against OS's own
# published GML-examples page (docs.os.uk).
_OSGB_NS = "http://www.ordnancesurvey.co.uk/xml/namespaces/osgb"

_WRAPPER_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<osgb:FeatureCollection xmlns:osgb="{_OSGB_NS}" xmlns:gml="{_GML_NS}">'
)
_WRAPPER_TAIL = "</osgb:FeatureCollection>"

# One TopographicArea (outer ring + one inner ring, gml:coordinates) and
# one TopographicLine (gml:posList): the task's own pinned two-feature
# fixture, deliberately using one of each coordinate encoding.
_AREA_OUTER_RING = (
    "400000.00,180000.00 400060.00,180000.00 400060.00,180040.00 "
    "400000.00,180040.00 400000.00,180000.00"
)
_AREA_INNER_RING = (
    "400010.00,180010.00 400020.00,180010.00 400020.00,180020.00 "
    "400010.00,180020.00 400010.00,180010.00"
)

_TOPOGRAPHIC_AREA = (
    '<osgb:topographicMember><osgb:TopographicArea fid="osgb1000000000001">'
    "<osgb:descriptiveGroup>General Surface</osgb:descriptiveGroup>"
    "<osgb:descriptiveTerm>Natural Ground</osgb:descriptiveTerm>"
    "<osgb:theme>Land</osgb:theme>"
    "<osgb:polygon><gml:Polygon srsName='osgb:BNG'>"
    "<gml:outerBoundaryIs><gml:LinearRing>"
    f"<gml:coordinates>{_AREA_OUTER_RING}</gml:coordinates>"
    "</gml:LinearRing></gml:outerBoundaryIs>"
    "<gml:innerBoundaryIs><gml:LinearRing>"
    f"<gml:coordinates>{_AREA_INNER_RING}</gml:coordinates>"
    "</gml:LinearRing></gml:innerBoundaryIs>"
    "</gml:Polygon></osgb:polygon>"
    "</osgb:TopographicArea></osgb:topographicMember>"
)

_LINE_POSLIST = "400100.00 180100.00 400150.00 180120.00 400200.00 180150.00"

_TOPOGRAPHIC_LINE = (
    '<osgb:topographicMember><osgb:TopographicLine fid="osgb1000000000002">'
    "<osgb:descriptiveGroup>General Feature</osgb:descriptiveGroup>"
    "<osgb:theme>Land</osgb:theme>"
    "<osgb:polyline><gml:LineString srsName='osgb:BNG'>"
    f"<gml:posList>{_LINE_POSLIST}</gml:posList>"
    "</gml:LineString></osgb:polyline>"
    "</osgb:TopographicLine></osgb:topographicMember>"
)

_MAIN_FIXTURE = (_WRAPPER_HEAD + _TOPOGRAPHIC_AREA + _TOPOGRAPHIC_LINE + _WRAPPER_TAIL).encode(
    "utf-8"
)

# A third feature type, TopographicPoint, kept out of the main two-feature
# fixture (the task's own pinned shape) and exercised on its own, exactly
# the way test_os_gml.py keeps its own RailwayTunnel edge case separate
# from the committed OML sample.
_TOPOGRAPHIC_POINT = (
    '<osgb:topographicMember><osgb:TopographicPoint fid="osgb1000000000003">'
    "<osgb:descriptiveGroup>Inland Water</osgb:descriptiveGroup>"
    "<osgb:descriptiveTerm>Culvert</osgb:descriptiveTerm>"
    "<osgb:theme>Water</osgb:theme>"
    "<osgb:point><gml:Point srsName='osgb:BNG'>"
    "<gml:coordinates>451492.79,1204378.76</gml:coordinates>"
    "</gml:Point></osgb:point>"
    "</osgb:TopographicPoint></osgb:topographicMember>"
)

_POINT_FIXTURE = (_WRAPPER_HEAD + _TOPOGRAPHIC_POINT + _WRAPPER_TAIL).encode("utf-8")


def _main_features():
    return list(mastermap_gml.iter_topography_features(io.BytesIO(_MAIN_FIXTURE)))


# --------------------------------------------------------------------------
# OsFeature is imported, never redefined.
# --------------------------------------------------------------------------


def test_iter_topography_features_yields_the_shared_os_feature_dataclass():
    features = _main_features()
    assert all(isinstance(feature, OsFeature) for feature in features)
    assert mastermap_gml.OsFeature is OsFeature


# --------------------------------------------------------------------------
# The main two-feature fixture: feature types, ring counts, exact
# coordinates, properties including toid.
# --------------------------------------------------------------------------


def test_main_fixture_yields_exactly_area_then_line_in_document_order():
    features = _main_features()
    assert [f.feature_type for f in features] == ["TopographicArea", "TopographicLine"]


def test_topographic_area_polygon_has_outer_and_one_inner_ring():
    area = next(f for f in _main_features() if f.feature_type == "TopographicArea")
    assert area.geometry["type"] == "Polygon"
    rings = area.geometry["coordinates"]
    assert len(rings) == 2


def test_topographic_area_outer_ring_matches_the_fixture_exactly():
    area = next(f for f in _main_features() if f.feature_type == "TopographicArea")
    outer_ring = area.geometry["coordinates"][0]
    assert outer_ring == [
        [400000.0, 180000.0],
        [400060.0, 180000.0],
        [400060.0, 180040.0],
        [400000.0, 180040.0],
        [400000.0, 180000.0],
    ]
    assert outer_ring[0] == outer_ring[-1]


def test_topographic_area_inner_ring_is_the_second_ring_and_matches_exactly():
    # Interfaces requirement: inner rings land as ADDITIONAL rings in the
    # Polygon geometry, in document order after the outer ring.
    area = next(f for f in _main_features() if f.feature_type == "TopographicArea")
    inner_ring = area.geometry["coordinates"][1]
    assert inner_ring == [
        [400010.0, 180010.0],
        [400020.0, 180010.0],
        [400020.0, 180020.0],
        [400010.0, 180020.0],
        [400010.0, 180010.0],
    ]


def test_topographic_area_properties_carry_descriptive_group_term_theme_and_toid():
    area = next(f for f in _main_features() if f.feature_type == "TopographicArea")
    assert area.properties == {
        "descriptiveGroup": "General Surface",
        "descriptiveTerm": "Natural Ground",
        "theme": "Land",
        "toid": "osgb1000000000001",
    }


def test_topographic_area_feature_id_is_the_fid_attribute_verbatim():
    area = next(f for f in _main_features() if f.feature_type == "TopographicArea")
    assert area.feature_id == "osgb1000000000001"


def test_topographic_line_geometry_is_a_linestring_from_poslist():
    line = next(f for f in _main_features() if f.feature_type == "TopographicLine")
    assert line.geometry == {
        "type": "LineString",
        "coordinates": [
            [400100.0, 180100.0],
            [400150.0, 180120.0],
            [400200.0, 180150.0],
        ],
    }


def test_topographic_line_first_and_last_coordinates_are_exact():
    line = next(f for f in _main_features() if f.feature_type == "TopographicLine")
    coords = line.geometry["coordinates"]
    assert coords[0] == [400100.0, 180100.0]
    assert coords[-1] == [400200.0, 180150.0]


def test_topographic_line_properties_have_no_descriptive_term_and_carry_toid():
    line = next(f for f in _main_features() if f.feature_type == "TopographicLine")
    assert line.properties == {
        "descriptiveGroup": "General Feature",
        "descriptiveTerm": None,
        "theme": "Land",
        "toid": "osgb1000000000002",
    }


# --------------------------------------------------------------------------
# Both coordinate encodings parse: gml:coordinates (Area, above) and
# gml:posList (Line, above) already both exercised by the main fixture;
# this test states that cross-check explicitly rather than leaving it
# implicit in the two tests above.
# --------------------------------------------------------------------------


def test_both_coordinate_encodings_parse_in_the_same_stream():
    features = _main_features()
    area = next(f for f in features if f.feature_type == "TopographicArea")
    line = next(f for f in features if f.feature_type == "TopographicLine")
    assert area.geometry["type"] == "Polygon"
    assert line.geometry["type"] == "LineString"


# --------------------------------------------------------------------------
# TopographicPoint: the third member type, kept in its own small fixture.
# --------------------------------------------------------------------------


def test_topographic_point_geometry_and_properties():
    (point,) = list(mastermap_gml.iter_topography_features(io.BytesIO(_POINT_FIXTURE)))
    assert point.feature_type == "TopographicPoint"
    assert point.geometry == {"type": "Point", "coordinates": [451492.79, 1204378.76]}
    assert point.properties == {
        "descriptiveGroup": "Inland Water",
        "descriptiveTerm": "Culvert",
        "theme": "Water",
        "toid": "osgb1000000000003",
    }
    assert point.feature_id == "osgb1000000000003"


# --------------------------------------------------------------------------
# source accepts a file path (str or Path) as well as a binary stream.
# --------------------------------------------------------------------------


def test_iter_topography_features_accepts_a_str_path(tmp_path):
    target = tmp_path / "sample.gml"
    target.write_bytes(_MAIN_FIXTURE)
    features = list(mastermap_gml.iter_topography_features(str(target)))
    assert [f.feature_type for f in features] == ["TopographicArea", "TopographicLine"]


def test_iter_topography_features_accepts_a_path_object(tmp_path):
    target = tmp_path / "sample.gml"
    target.write_bytes(_MAIN_FIXTURE)
    features = list(mastermap_gml.iter_topography_features(Path(target)))
    assert [f.feature_type for f in features] == ["TopographicArea", "TopographicLine"]


def test_iter_topography_features_is_a_generator_function():
    import inspect

    assert inspect.isgeneratorfunction(mastermap_gml.iter_topography_features)


# --------------------------------------------------------------------------
# Truncated stream: MasterMapError, never a raw xml.etree.ElementTree.
# ParseError.
# --------------------------------------------------------------------------


def test_truncated_stream_raises_master_map_error_not_a_raw_parse_error():
    truncated = _MAIN_FIXTURE[: len(_MAIN_FIXTURE) // 2]
    with pytest.raises(MasterMapError):
        list(mastermap_gml.iter_topography_features(io.BytesIO(truncated)))


def test_truncated_stream_error_names_the_stream_never_a_url():
    truncated = _MAIN_FIXTURE[: len(_MAIN_FIXTURE) // 2]
    with pytest.raises(MasterMapError) as exc_info:
        list(mastermap_gml.iter_topography_features(io.BytesIO(truncated)))
    message = str(exc_info.value)
    assert "http://" not in message
    assert "https://" not in message


# --------------------------------------------------------------------------
# Empty coordinates/posList: nothing fabricated, MasterMapError raised.
# --------------------------------------------------------------------------

_AREA_WITH_EMPTY_COORDINATES = (
    '<osgb:topographicMember><osgb:TopographicArea fid="osgbBAD0000000001">'
    "<osgb:descriptiveGroup>General Surface</osgb:descriptiveGroup>"
    "<osgb:theme>Land</osgb:theme>"
    "<osgb:polygon><gml:Polygon srsName='osgb:BNG'>"
    "<gml:outerBoundaryIs><gml:LinearRing>"
    "<gml:coordinates></gml:coordinates>"
    "</gml:LinearRing></gml:outerBoundaryIs>"
    "</gml:Polygon></osgb:polygon>"
    "</osgb:TopographicArea></osgb:topographicMember>"
)

_LINE_WITH_EMPTY_POSLIST = (
    '<osgb:topographicMember><osgb:TopographicLine fid="osgbBAD0000000002">'
    "<osgb:descriptiveGroup>General Feature</osgb:descriptiveGroup>"
    "<osgb:theme>Land</osgb:theme>"
    "<osgb:polyline><gml:LineString srsName='osgb:BNG'>"
    "<gml:posList></gml:posList>"
    "</gml:LineString></osgb:polyline>"
    "</osgb:TopographicLine></osgb:topographicMember>"
)


def _features_from_body(body: str):
    data = (_WRAPPER_HEAD + body + _WRAPPER_TAIL).encode("utf-8")
    return list(mastermap_gml.iter_topography_features(io.BytesIO(data)))


def test_empty_coordinates_element_raises_master_map_error():
    with pytest.raises(MasterMapError):
        _features_from_body(_AREA_WITH_EMPTY_COORDINATES)


def test_empty_poslist_element_raises_master_map_error():
    with pytest.raises(MasterMapError):
        _features_from_body(_LINE_WITH_EMPTY_POSLIST)


def test_empty_coordinates_error_names_the_feature_and_toid_never_a_url():
    with pytest.raises(MasterMapError) as exc_info:
        _features_from_body(_AREA_WITH_EMPTY_COORDINATES)
    message = str(exc_info.value)
    assert "TopographicArea" in message
    assert "osgbBAD0000000001" in message
    assert "http://" not in message
    assert "https://" not in message


def test_empty_coordinates_never_fabricates_an_empty_ring():
    # The global "nothing fabricated" rule os_gml.py's own review found
    # missing on its first pass: a present-but-blank coordinates/posList
    # element must never silently become {"coordinates": [[]]}.
    with pytest.raises(MasterMapError):
        _features_from_body(_AREA_WITH_EMPTY_COORDINATES)


def test_a_malformed_feature_does_not_withhold_earlier_good_features():
    body = _TOPOGRAPHIC_AREA + _AREA_WITH_EMPTY_COORDINATES
    seen = []
    with pytest.raises(MasterMapError):
        for feature in mastermap_gml.iter_topography_features(
            io.BytesIO((_WRAPPER_HEAD + body + _WRAPPER_TAIL).encode("utf-8"))
        ):
            seen.append(feature)
    assert len(seen) == 1
    assert seen[0].feature_type == "TopographicArea"
    assert seen[0].feature_id == "osgb1000000000001"


# --------------------------------------------------------------------------
# Wrong root: MasterMapError naming what was expected.
# --------------------------------------------------------------------------


def test_wrong_root_raises_master_map_error():
    data = (
        '<?xml version="1.0" encoding="UTF-8"?><foo:Bar xmlns:foo="urn:foo"><foo:Baz/></foo:Bar>'
    ).encode("utf-8")
    with pytest.raises(MasterMapError):
        list(mastermap_gml.iter_topography_features(io.BytesIO(data)))


def test_wrong_root_error_names_the_expectation_never_a_url():
    data = (
        '<?xml version="1.0" encoding="UTF-8"?><foo:Bar xmlns:foo="urn:foo"><foo:Baz/></foo:Bar>'
    ).encode("utf-8")
    with pytest.raises(MasterMapError) as exc_info:
        list(mastermap_gml.iter_topography_features(io.BytesIO(data)))
    message = str(exc_info.value)
    assert "FeatureCollection" in message
    assert "Bar" in message
    assert "http://" not in message
    assert "https://" not in message


# --------------------------------------------------------------------------
# MasterMapError is a RuntimeError, per the brief's own interface line.
# --------------------------------------------------------------------------


def test_master_map_error_is_a_runtime_error():
    assert issubclass(MasterMapError, RuntimeError)


# --------------------------------------------------------------------------
# Review fix-round, Important finding: descriptiveGroup, descriptiveTerm
# and theme are documented "Multiple" cardinality in OS's own technical
# specification; a feature carrying more than one of a field must yield
# ALL of them, in document order, as a list, not silently keep the first
# and drop the rest.
# --------------------------------------------------------------------------

_MULTI_VALUED_AREA = (
    '<osgb:topographicMember><osgb:TopographicArea fid="osgb1000000000099">'
    "<osgb:descriptiveGroup>General Surface</osgb:descriptiveGroup>"
    "<osgb:descriptiveGroup>Structure</osgb:descriptiveGroup>"
    "<osgb:descriptiveTerm>Natural Ground</osgb:descriptiveTerm>"
    "<osgb:theme>Land</osgb:theme>"
    "<osgb:theme>Water</osgb:theme>"
    "<osgb:polygon><gml:Polygon srsName='osgb:BNG'>"
    "<gml:outerBoundaryIs><gml:LinearRing>"
    f"<gml:coordinates>{_AREA_OUTER_RING}</gml:coordinates>"
    "</gml:LinearRing></gml:outerBoundaryIs>"
    "</gml:Polygon></osgb:polygon>"
    "</osgb:TopographicArea></osgb:topographicMember>"
)


def test_multi_valued_descriptive_group_and_theme_yield_lists_in_document_order():
    (feature,) = _features_from_body(_MULTI_VALUED_AREA)
    assert feature.properties["descriptiveGroup"] == ["General Surface", "Structure"]
    assert feature.properties["theme"] == ["Land", "Water"]
    # Only one descriptiveTerm on this feature: still a plain string, not
    # a single-item list, matching every existing test built before this
    # finding and the brief's own singular key names.
    assert feature.properties["descriptiveTerm"] == "Natural Ground"


def test_single_valued_properties_are_still_plain_strings_not_lists():
    area = next(f for f in _main_features() if f.feature_type == "TopographicArea")
    assert area.properties["descriptiveGroup"] == "General Surface"
    assert isinstance(area.properties["descriptiveGroup"], str)


# --------------------------------------------------------------------------
# Review fix-round, Critical finding: a real MasterMap Topography file can
# interleave OTHER member wrapper names (cartographicMember, boundaryMember,
# departedMember) between topographicMembers, sharing one root the way
# os_gml.py's own OS Open products never do (one shared featureMember for
# every feature, in every one of those three products). The reviewer's own
# executed demonstration found unbounded, linear memory growth when a long
# run of a skipped wrapper type sits between two real topographic features
# (their own numbers: ~212 MB peak over a 300,000-member run against a flat
# ~170 KB band on a homogeneous stream), because the old cleanup fired only
# on "end" events matching "topographicMember" by name, never on any other
# wrapper's own "end" event. The fix keys the sweep to iterparse DEPTH
# instead: any direct child of root, whatever its own name, is cleared on
# its own "end" event. This test reproduces the reviewer's own shape at a
# scale a fast unit test can carry (a few thousand skipped members, not
# 300,000), asserting both that memory stays in a small, bounded band and
# that the two real features either side of the run still parse correctly.
# --------------------------------------------------------------------------


def _cartographic_member(n: int) -> str:
    # osgb:cartographicMember wrapping osgb:CartographicText: a real,
    # documented MasterMap Topography Layer member type this module never
    # yields features out of, sharing the same root as topographicMember.
    return (
        '<osgb:cartographicMember><osgb:CartographicText '
        f'fid="osgbCARTO{n:012d}">'
        f"<osgb:textString>Padding label text for member {n:012d}, kept long "
        "enough that thousands of these unswept would show up clearly under "
        "tracemalloc if this module ever regressed to sweeping only "
        "topographicMember by name again.</osgb:textString>"
        "<osgb:anchorPosition>8</osgb:anchorPosition>"
        "</osgb:CartographicText></osgb:cartographicMember>"
    )


def test_interleaved_cartographic_members_are_swept_and_features_still_parse():
    count = 5000
    body = (
        _TOPOGRAPHIC_AREA
        + "".join(_cartographic_member(n) for n in range(count))
        + _TOPOGRAPHIC_LINE
    )
    data = (_WRAPPER_HEAD + body + _WRAPPER_TAIL).encode("utf-8")
    assert len(data) > 1_000_000  # a run large enough for unswept growth to show

    tracemalloc.start()
    try:
        features = list(mastermap_gml.iter_topography_features(io.BytesIO(data)))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # The two real features either side of 5,000 skipped members still
    # parse correctly, in document order, unaffected by what sits between
    # them.
    assert [f.feature_type for f in features] == ["TopographicArea", "TopographicLine"]
    assert features[0].feature_id == "osgb1000000000001"
    assert features[1].feature_id == "osgb1000000000002"

    # A bounded streamer holds roughly one member's worth of tracked
    # memory at a time, regardless of how many cartographicMembers
    # separate the two real features. This bound is generous (the
    # reviewer's own homogeneous-stream control held under 200 KB) but
    # still far below where 5,000 unswept ~300-byte-plus-overhead members
    # would land if the old by-name-only sweep regressed back in.
    assert peak < 3_000_000
