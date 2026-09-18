import math

import pytest

from mapgen.constraints import (
    LAYER_BY_TYPE,
    LAYERS,
    MARGIN_METRES,
    OUTPUT_GROUPS,
    Layer,
    LayerResult,
    assess,
    classify_feature,
    group_collections,
    padded_rect,
)

SITE = (-3.46, 51.455, -3.438, 51.468)  # Cowbridge
CONSERVATION = LAYER_BY_TYPE["geonode:conservation_areas_wales"]
LISTED = LAYER_BY_TYPE["inspire-wg:Cadw_ListedBuildings"]
FLOOD_RIVERS = LAYER_BY_TYPE["inspire-nrw:NRW_FLOODZONE_RIVERS"]
SAC = LAYER_BY_TYPE["inspire-nrw:NRW_SAC"]


def _polygon(west, south, east, north):
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}


def _feature(geometry, **properties):
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def test_the_table_has_the_spec_s_twenty_layers_in_order():
    assert len(LAYERS) == 20
    assert LAYERS[0].type_name == "inspire-nrw:NRW_FLOODZONE_RIVERS"
    assert LAYERS[-1].type_name == "geonode:conservation_areas_wales"
    assert {layer.group for layer in LAYERS} == set(OUTPUT_GROUPS)
    assert {layer.attribution for layer in LAYERS} == {"A", "B", "C", "D"}
    assert LISTED.group == "listed_buildings" and LISTED.grade_field == "Grade"


def test_padded_rect_adds_250_m_each_side():
    west, south, east, north = padded_rect(SITE)
    assert MARGIN_METRES == 250.0
    assert south == pytest.approx(SITE[1] - 250 / 111_320)
    lat0 = math.radians((SITE[1] + SITE[3]) / 2)
    assert west == pytest.approx(SITE[0] - 250 / (111_320 * math.cos(lat0)))


def test_a_polygon_on_the_site_is_on_the_site():
    padded = padded_rect(SITE)
    found = classify_feature(CONSERVATION, _feature(_polygon(-3.455, 51.458, -3.445, 51.465), sitename=" Cowbridge "), SITE, padded)
    assert found.where == "site"
    assert found.name == "Cowbridge"
    assert found.site_polygons


def test_a_polygon_only_in_the_margin_is_in_the_margin():
    padded = padded_rect(SITE)
    # South of the site, inside the 250 m margin (0.00224 degrees of latitude).
    found = classify_feature(CONSERVATION, _feature(_polygon(-3.455, 51.4535, -3.445, 51.4545), sitename="Llanblethian"), SITE, padded)
    assert found.where == "margin"
    assert found.site_polygons == ()


def test_the_loose_bbox_trap_is_dropped():
    """A triangle whose envelope overlaps the padded extent but whose
    shape stays south-east of it: GeoServer's loose bounding box returns
    such features (a Heritage Coast 7 km from Cowbridge, in the research
    query), and they must reach no file or line."""
    padded = padded_rect(SITE)
    triangle = {"type": "Polygon", "coordinates": [[[-3.50, 51.40], [-3.30, 51.40], [-3.30, 51.50], [-3.50, 51.40]]]}
    assert classify_feature(SAC, _feature(triangle, SAC_name="Far away"), SITE, padded) is None


def test_points_are_tested_one_by_one():
    padded = padded_rect(SITE)
    on_site = _feature({"type": "MultiPoint", "coordinates": [[-3.45, 51.46]]}, Name="House", Grade="II", RecordNumber=7)
    found = classify_feature(LISTED, on_site, SITE, padded)
    assert found.where == "site" and found.grade_or_zone == "II" and found.reference == "7"
    outside = _feature({"type": "Point", "coordinates": [-3.30, 51.30]}, Name="Far", Grade="II")
    assert classify_feature(LISTED, outside, SITE, padded) is None


def test_null_and_blank_text_reads_as_none():
    padded = padded_rect(SITE)
    found = classify_feature(CONSERVATION, _feature(_polygon(-3.455, 51.458, -3.445, 51.465), sitename="null", inspireid="  "), SITE, padded)
    assert found.name is None and found.reference is None


def test_output_geometry_is_clipped_to_the_padded_extent_and_closed():
    padded = padded_rect(SITE)
    huge = _polygon(-4.0, 51.0, -3.0, 52.0)
    found = classify_feature(CONSERVATION, _feature(huge, sitename="Big"), SITE, padded)
    ring = found.geometry["coordinates"][0][0]
    assert ring[0] == ring[-1]
    xs = [p[0] for p in ring]
    assert min(xs) == pytest.approx(padded[0]) and max(xs) == pytest.approx(padded[2])


def test_assess_keeps_failures_apart_and_counts_unsupported_geometry():
    line = _feature({"type": "LineString", "coordinates": [[-3.45, 51.46], [-3.44, 51.46]]})
    results = [
        LayerResult(FLOOD_RIVERS, ok=True, features=(_feature(_polygon(-3.455, 51.458, -3.445, 51.465), risk="Flood Zone 3"), line)),
        LayerResult(SAC, ok=False, kind="timeout", reason="DataMapWales did not answer in time"),
    ]
    assessment = assess(SITE, results)
    assert [item.layer for item in assessment.found] == [FLOOD_RIVERS]
    assert assessment.checked == [FLOOD_RIVERS]
    assert assessment.failed == [(SAC, "DataMapWales did not answer in time")]
    assert assessment.skipped_geometry == 1


def test_group_collections_carry_the_filterable_properties():
    results = [
        LayerResult(CONSERVATION, ok=True, features=(_feature(_polygon(-3.455, 51.458, -3.445, 51.465), sitename="Cowbridge", inspireid="W1"),)),
        LayerResult(LISTED, ok=True, features=(_feature({"type": "MultiPoint", "coordinates": [[-3.45, 51.46]]}, Name="House", Grade="II*", RecordNumber=9),)),
    ]
    collections = group_collections(assess(SITE, results))
    assert set(collections) == {"heritage", "listed_buildings"}
    properties = collections["heritage"]["features"][0]["properties"]
    assert properties == {
        "designation": "Conservation area",
        "name": "Cowbridge",
        "grade_or_zone": None,
        "reference": "W1",
        "where": "site",
        "source": "geonode:conservation_areas_wales",
    }
    assert collections["listed_buildings"]["features"][0]["properties"]["grade_or_zone"] == "II*"
