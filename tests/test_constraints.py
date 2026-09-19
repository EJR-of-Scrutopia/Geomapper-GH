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


from mapgen.constraints import (
    ATTRIBUTIONS,
    COAL_LINE,
    CONSERVATION_CAVEAT,
    PARTIAL_LINE,
    TPO_LINE,
    attribution_statements,
    render_text,
    share_text,
    summarise,
    to_record,
)

NATIONAL_PARK = LAYER_BY_TYPE["inspire-nrw:NRW_NATIONAL_PARK"]


def _site_left_half():
    """A polygon covering the west half of SITE exactly."""
    middle = (SITE[0] + SITE[2]) / 2
    return _polygon(SITE[0] - 0.01, SITE[1] - 0.01, middle, SITE[3] + 0.01)


def _all_checked_but(*results):
    """Every layer checked and empty, except the given results."""
    given = {result.layer for result in results}
    return list(results) + [LayerResult(layer, ok=True) for layer in LAYERS if layer not in given]


@pytest.mark.parametrize(
    "share,text",
    [
        (0.0, "under 1% of the site"),
        (0.0049, "under 1% of the site"),
        (0.005, "1% of the site"),
        (0.5, "50% of the site"),
        (0.125, "13% of the site"),
        (0.994, "99% of the site"),
        (0.995, "the whole site"),
        (1.0, "the whole site"),
    ],
)
def test_share_text_rules(share, text):
    assert share_text(share) == text


def test_an_area_on_the_site_gets_its_share():
    results = _all_checked_but(LayerResult(CONSERVATION, ok=True, features=(_feature(_site_left_half(), sitename="Cowbridge"),)))
    summary = summarise(assess(SITE, results), ["Vale of Glamorgan"], "full")
    assert [entry["line"] for entry in summary.on_site] == ["Conservation area: Cowbridge (50% of the site)"]
    assert summary.on_site[0]["share"] == pytest.approx(0.5, abs=0.01)


def test_a_nameless_flood_zone_is_labelled_by_its_zone():
    results = _all_checked_but(LayerResult(FLOOD_RIVERS, ok=True, features=(_feature(_site_left_half(), risk="Flood Zone 3"),)))
    summary = summarise(assess(SITE, results), [], "full")
    assert summary.on_site[0]["line"] == "Flood zone, rivers: Flood Zone 3 (50% of the site)"


def test_listed_buildings_are_counted_by_grade_in_order():
    points = [
        _feature({"type": "Point", "coordinates": [-3.45, 51.46]}, Name="A", Grade="II"),
        _feature({"type": "Point", "coordinates": [-3.449, 51.46]}, Name="B", Grade="II*"),
        _feature({"type": "Point", "coordinates": [-3.448, 51.46]}, Name="C", Grade="II"),
        _feature({"type": "Point", "coordinates": [-3.447, 51.46]}, Name="D", Grade="I"),
    ]
    summary = summarise(assess(SITE, _all_checked_but(LayerResult(LISTED, ok=True, features=tuple(points)))), [], "full")
    assert summary.on_site[0]["line"] == "Listed buildings: 4 (Grade I 1, Grade II* 1, Grade II 2)"
    assert summary.on_site[0]["grades"] == {"II": 2, "II*": 1, "I": 1}


def test_the_margin_lists_only_what_is_not_on_the_site():
    on_site = _feature(_polygon(-3.455, 51.458, -3.445, 51.465), sitename="Cowbridge")
    margin_same = _feature(_polygon(-3.455, 51.4535, -3.445, 51.4545), sitename="Cowbridge")
    margin_other = _feature(_polygon(-3.455, 51.4535, -3.445, 51.4545), sitename="Llanblethian")
    results = _all_checked_but(LayerResult(CONSERVATION, ok=True, features=(on_site, margin_same, margin_other)))
    summary = summarise(assess(SITE, results), [], "full")
    assert [entry["line"] for entry in summary.margin] == ["Conservation area: Llanblethian"]


def test_a_failed_layer_is_not_checked_and_never_none_found():
    results = _all_checked_but(LayerResult(SAC, ok=False, kind="timeout", reason="DataMapWales did not answer in time"))
    summary = summarise(assess(SITE, results), [], "full")
    assert "Special Area of Conservation" not in summary.none_found
    lines = [entry["line"] for entry in summary.not_checked]
    assert lines[0] == "Special Area of Conservation: DataMapWales did not answer in time"
    assert COAL_LINE in lines and TPO_LINE in lines


def test_a_layer_answering_only_unreadable_geometry_is_not_checked():
    # A layer whose every feature is a LineString or GeometryCollection is
    # counted per assessment only were it not also counted per layer, and
    # would read as "Checked, none found", indistinguishable from a layer
    # that genuinely has nothing here.
    line = _feature({"type": "LineString", "coordinates": [[-3.45, 51.46], [-3.44, 51.46]]})
    results = _all_checked_but(LayerResult(SAC, ok=True, features=(line,)))
    assessment = assess(SITE, results)
    summary = summarise(assessment, [], "full")
    assert "Special Area of Conservation" not in summary.none_found
    lines = [entry["line"] for entry in summary.not_checked]
    assert "Special Area of Conservation: DataMapWales answered with geometry mapgen does not read" in lines
    record = to_record(
        summary, assessment, councils=[], coverage="full", coverage_share=1.0,
        margin_m=250.0, summary_file="x_constraints.txt", fetched_on="2026-09-18",
    )
    # The layers map must agree with not_checked: a reader of `layers`
    # alone must not take this layer as checked and empty (re-review N6).
    assert record["layers"]["inspire-nrw:NRW_SAC"] == {
        "designation": "Special Area of Conservation", "status": "unreadable", "features": 0,
        "reason": "DataMapWales answered with geometry mapgen does not read", "skipped_geometry": 1,
    }
    assert record["layers"]["inspire-nrw:NRW_SSSI"]["skipped_geometry"] == 0
    assert record["skipped_geometry"] == 1


def test_everything_checked_and_absent_is_none_found_in_table_order():
    summary = summarise(assess(SITE, _all_checked_but()), [], "full")
    assert summary.none_found == [layer.designation for layer in LAYERS]
    assert [entry["line"] for entry in summary.not_checked] == [COAL_LINE, TPO_LINE]


def test_partial_coverage_adds_the_outside_wales_line():
    summary = summarise(assess(SITE, _all_checked_but()), [], "partial")
    assert summary.not_checked[-1]["line"] == PARTIAL_LINE


def test_the_authority_is_the_park_when_a_national_park_covers_the_site():
    park = LayerResult(NATIONAL_PARK, ok=True, features=(_feature(_site_left_half(), np_name="Bannau Brycheiniog"),))
    assert summarise(assess(SITE, _all_checked_but(park)), ["Powys"], "full").authority == (
        "the National Park authority (Bannau Brycheiniog)"
    )
    assert summarise(assess(SITE, _all_checked_but()), ["Vale of Glamorgan"], "full").authority == (
        "the council (Vale of Glamorgan)"
    )
    assert summarise(assess(SITE, _all_checked_but()), [], "full").authority == "the local planning authority"


def test_cadw_attribution_carries_the_fetch_date():
    statements = attribution_statements(["C"], "2026-09-18")
    assert statements == [ATTRIBUTIONS["C"].format(date="2026-09-18")]
    assert "2026-09-18" in statements[0]


def test_attribution_d_names_the_dataset_title_and_publisher():
    assert ATTRIBUTIONS["D"] == (
        "Contains public sector information licensed under the Open Government Licence v3.0. "
        "Datasets: World Heritage Sites (Site Boundary) and Conservation Area Boundaries, "
        "published on DataMapWales by the Welsh Government."
    )


def test_render_text_layout():
    results = _all_checked_but(LayerResult(CONSERVATION, ok=True, features=(_feature(_site_left_half(), sitename="Cowbridge"),)))
    summary = summarise(assess(SITE, results), ["Vale of Glamorgan"], "full")
    text = render_text(summary, "Cowbridge_2026-09-18", "2026-09-18")
    lines = text.splitlines()
    assert lines[0] == "Planning constraints: Cowbridge_2026-09-18"
    assert lines[1] == "Indicative, from open data checked 2026-09-18. Confirm with the council (Vale of Glamorgan)."
    assert "On the site" in lines and "Within 250 m" in lines
    assert "Checked, none found" in lines and "Not checked" in lines and "Sources" in lines
    assert "- Conservation area: Cowbridge (50% of the site)" in lines
    assert lines[lines.index("Within 250 m") + 1] == "- nothing found"
    assert CONSERVATION_CAVEAT in lines
    assert text.endswith("\n")
    assert "\u2014" not in text


def test_to_record_shape():
    results = _all_checked_but(LayerResult(SAC, ok=False, kind="timeout", reason="DataMapWales did not answer in time"))
    assessment = assess(SITE, results)
    summary = summarise(assessment, ["Vale of Glamorgan"], "full")
    record = to_record(
        summary, assessment, councils=["Vale of Glamorgan"], coverage="full", coverage_share=0.9991,
        margin_m=250.0, summary_file="x_constraints.txt", fetched_on="2026-09-18",
    )
    assert record["checked"] is True
    assert record["coverage"] == "full" and record["coverage_share"] == 0.9991
    assert record["layers"]["inspire-nrw:NRW_SAC"] == {
        "designation": "Special Area of Conservation", "status": "failed", "features": None,
        "reason": "DataMapWales did not answer in time",
    }
    assert record["layers"]["inspire-nrw:NRW_SSSI"]["status"] == "ok"
    assert record["layers"]["inspire-nrw:NRW_SSSI"]["features"] == 0
    assert record["summary_file"] == "x_constraints.txt"
