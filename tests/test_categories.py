from mapgen.categories import (
    ALL_CATEGORY_IDS,
    CATEGORY_GROUPS,
    ROAD_SUBTYPES,
    osm_tag_clauses,
    overture_types_for_categories,
)
from mapgen.sources.overture import DEFAULT_OVERTURE_TYPES


def test_all_category_ids_has_no_duplicates():
    assert len(ALL_CATEGORY_IDS) == len(set(ALL_CATEGORY_IDS))


def test_all_category_ids_excludes_the_roads_group_label_itself():
    # "roads" is a grouping label for its nine children, never a
    # selectable leaf of its own.
    assert "roads" not in ALL_CATEGORY_IDS


def test_all_category_ids_includes_every_road_subtype():
    for subtype_id, _label in ROAD_SUBTYPES:
        assert subtype_id in ALL_CATEGORY_IDS


def test_category_groups_roads_children_match_road_subtypes():
    roads_group = next(g for g in CATEGORY_GROUPS if g.id == "roads")
    assert roads_group.children == tuple(id_ for id_, _label in ROAD_SUBTYPES)


# --- osm_tag_clauses ---------------------------------------------------


def test_osm_tag_clauses_is_none_when_categories_is_none():
    # None means "unknown, use the original unfiltered query": the
    # caller must be able to tell this apart from an empty selection.
    assert osm_tag_clauses(None) is None


def test_osm_tag_clauses_is_none_when_every_category_is_selected():
    # Today's behaviour must be reproduced exactly when nothing has been
    # deselected, not merely approximated by an equivalent filtered query.
    assert osm_tag_clauses(list(ALL_CATEGORY_IDS)) is None


def test_osm_tag_clauses_is_an_empty_list_when_nothing_is_selected():
    # Distinct from None: a caller must be able to build a query that
    # genuinely matches nothing, not silently fall back to unfiltered.
    assert osm_tag_clauses([]) == []


def test_osm_tag_clauses_includes_a_plain_building_filter():
    clauses = osm_tag_clauses(["buildings"])
    assert clauses == ['["building"]']


def test_osm_tag_clauses_translates_footpath_to_the_real_osm_tag_value():
    # OSM tags a footpath highway=footway, not highway=footpath: the UI
    # label is not the tag value, and a filter built from the label
    # literally would match nothing in real OSM data.
    clauses = osm_tag_clauses(["footpath"])
    assert clauses == ['["highway"~"^(footway)$"]']
    assert "footpath" not in clauses[0]


def test_osm_tag_clauses_includes_link_variants_for_classed_roads():
    clauses = osm_tag_clauses(["motorway"])
    assert clauses == ['["highway"~"^(motorway|motorway_link)$"]']


def test_osm_tag_clauses_does_not_add_a_link_variant_for_a_class_without_one():
    clauses = osm_tag_clauses(["service"])
    assert clauses == ['["highway"~"^(service)$"]']


def test_osm_tag_clauses_combines_multiple_road_subtypes_into_one_highway_filter():
    clauses = osm_tag_clauses(["motorway", "footpath"])
    assert len(clauses) == 1
    assert "motorway" in clauses[0]
    assert "footway" in clauses[0]


def test_osm_tag_clauses_combines_buildings_and_roads_as_separate_clauses():
    clauses = osm_tag_clauses(["buildings", "footpath"])
    assert '["building"]' in clauses
    assert any("footway" in c for c in clauses)
    assert len(clauses) == 2


def test_osm_tag_clauses_water_covers_natural_water_and_waterway():
    clauses = osm_tag_clauses(["water"])
    assert '["natural"="water"]' in clauses
    assert '["waterway"]' in clauses


def test_osm_tag_clauses_rail_is_a_plain_railway_filter():
    assert osm_tag_clauses(["rail"]) == ['["railway"]']


def test_osm_tag_clauses_boundaries_is_a_plain_boundary_filter():
    assert osm_tag_clauses(["boundaries"]) == ['["boundary"]']


def test_osm_tag_clauses_points_of_interest_covers_amenity_shop_and_tourism():
    clauses = osm_tag_clauses(["points_of_interest"])
    assert set(clauses) == {'["amenity"]', '["shop"]', '["tourism"]'}


# --- overture_types_for_categories --------------------------------------


def test_overture_types_for_categories_selecting_everything_matches_the_full_default():
    # The property that makes "select everything" a true no-op for
    # Overture too: selecting every category must reproduce exactly the
    # same 8 default types, not a superset or subset of them.
    assert overture_types_for_categories(ALL_CATEGORY_IDS) == DEFAULT_OVERTURE_TYPES


def test_overture_types_for_categories_buildings_only():
    assert overture_types_for_categories(["buildings"]) == ["building"]


def test_overture_types_for_categories_any_road_subtype_pulls_in_segment_and_connector():
    assert overture_types_for_categories(["footpath"]) == ["segment", "connector"]
    assert overture_types_for_categories(["motorway"]) == ["segment", "connector"]


def test_overture_types_for_categories_rail_contributes_nothing():
    # Overture has no separate rail type: rail is a subtype of segment,
    # not something --type can select on its own. Honest, not a bug.
    assert overture_types_for_categories(["rail"]) == []


def test_overture_types_for_categories_boundaries_contributes_nothing():
    # Overture's division types are not part of DEFAULT_OVERTURE_TYPES
    # and are not fetched by this tool at all.
    assert overture_types_for_categories(["boundaries"]) == []


def test_overture_types_for_categories_vegetation_and_landuse_maps_to_both_types():
    assert overture_types_for_categories(["vegetation_and_landuse"]) == ["land_use", "land_cover"]


def test_overture_types_for_categories_points_of_interest_maps_to_place_and_infrastructure():
    assert overture_types_for_categories(["points_of_interest"]) == ["place", "infrastructure"]


def test_overture_types_for_categories_returns_a_stable_deterministic_order():
    a = overture_types_for_categories(["water", "buildings", "points_of_interest"])
    b = overture_types_for_categories(["points_of_interest", "buildings", "water"])
    assert a == b
