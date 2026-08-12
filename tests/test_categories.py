import pytest

from mapgen.categories import (
    ALL_CATEGORY_IDS,
    CARRIAGEWAY_HIGHWAY_VALUES,
    CATEGORY_GROUPS,
    PATH_HIGHWAY_VALUES,
    ROAD_FAMILY_CARRIAGEWAY,
    ROAD_FAMILY_PATH,
    ROAD_SUBTYPES,
    EmptyCategorySelectionError,
    UnknownCategoryError,
    osm_tag_clauses,
    overture_types_for_categories,
    road_family,
    validate_categories,
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
    assert clauses == ['["highway"~"^(footway|path|bridleway|steps)$"]']
    assert "footpath" not in clauses[0]


def test_osm_tag_clauses_footpath_covers_path_bridleway_and_steps_not_only_footway():
    # The defect this pins: "footpath" resolved to ("footway",) alone, so
    # a survey that narrowed its categories and ticked Footpath silently
    # lost highway=path, highway=bridleway and highway=steps. The
    # selection was honoured exactly as written, the filter matched
    # nothing for those three, and nothing anywhere said a category the
    # person ticked had come back short. Measured on the Cowbridge
    # benchmark package that is 28 of 156 path ways (23 path, 5 steps).
    clause = osm_tag_clauses(["footpath"])[0]
    for value in ("footway", "path", "bridleway", "steps"):
        assert value in clause


def test_the_sibling_road_entries_carry_every_value_their_own_label_promises():
    # Read at the same time as the footpath fix, so a second entry short
    # in the same way cannot sit unnoticed behind a passing suite.
    # cycleway, track and service each spell exactly one real OSM highway
    # value; residential spells one too (living_street is its own named
    # class, not a spelling of "residential"); the four classed roads
    # carry their own _link variants.
    assert osm_tag_clauses(["cycleway"]) == ['["highway"~"^(cycleway)$"]']
    assert osm_tag_clauses(["track"]) == ['["highway"~"^(track)$"]']
    assert osm_tag_clauses(["residential"]) == ['["highway"~"^(residential)$"]']
    assert osm_tag_clauses(["trunk"]) == ['["highway"~"^(trunk|trunk_link)$"]']
    assert osm_tag_clauses(["primary"]) == ['["highway"~"^(primary|primary_link)$"]']
    assert osm_tag_clauses(["secondary"]) == ['["highway"~"^(secondary|secondary_link)$"]']


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


# --- validate_categories -------------------------------------------------
#
# A coordinator review's Critical 1: before this, an unrecognised category
# id reached osm_tag_clauses and overture_types_for_categories unvalidated,
# where it silently matched nothing rather than being rejected. Reproduced
# live: --category building (missing its "s") completed as a legitimate
# "nothing selected" run (0 nodes, an 85-byte file, complete: true), with
# survey.json recording categories: ["building"] as though that had been a
# deliberate, honoured choice. These tests are about REJECTING that input,
# not about the mapping functions above, which were already correct for
# every id they were ever actually asked about.


def test_validate_categories_accepts_none():
    validate_categories(None)


def test_validate_categories_accepts_every_known_id():
    validate_categories(ALL_CATEGORY_IDS)


def test_validate_categories_rejects_an_empty_list():
    # Task 21: unticking every category in the browser, or otherwise
    # passing categories=[], used to be accepted silently here (see this
    # test's own prior form, which asserted the opposite). That let a
    # selection that matches nothing in either osm_tag_clauses or
    # overture_types_for_categories reach them unrefused, producing an
    # empty package reported as complete: true with nothing to say why.
    # None (untouched by this test, see test_validate_categories_accepts_
    # none above) is still the real, different "every category" case;
    # this is specifically the non-None, explicitly empty one.
    with pytest.raises(EmptyCategorySelectionError):
        validate_categories([])


def test_validate_categories_empty_list_message_says_nothing_is_selected_and_one_is_needed():
    with pytest.raises(EmptyCategorySelectionError) as excinfo:
        validate_categories([])
    message = str(excinfo.value).lower()
    assert "no categories are selected" in message or "nothing" in message, (
        f"expected the message to plainly say nothing is selected, got: {excinfo.value}"
    )
    assert "at least one category" in message, (
        f"expected the message to plainly say at least one category is needed, got: {excinfo.value}"
    )


def test_empty_category_selection_error_is_a_value_error():
    # Same reasoning as test_unknown_category_error_is_a_value_error
    # below: server.py's _REQUEST_VALUE_ERRORS catches ValueError
    # generically, so this needs no change there, only cli.py's own
    # explicit exception tuple.
    assert issubclass(EmptyCategorySelectionError, ValueError)


def test_empty_category_selection_error_is_distinct_from_unknown_category_error():
    # Deliberate: an empty selection is the vocabulary being consulted
    # correctly and truthfully reporting nothing was asked for, not an id
    # outside it. A caller that only catches UnknownCategoryError
    # specifically (rather than ValueError generally) must not silently
    # swallow this different condition.
    assert not issubclass(EmptyCategorySelectionError, UnknownCategoryError)
    assert not issubclass(UnknownCategoryError, EmptyCategorySelectionError)


def test_validate_categories_rejects_the_exact_typo_a_coordinator_review_reproduced():
    with pytest.raises(UnknownCategoryError, match="building"):
        validate_categories(["building"])


def test_validate_categories_error_names_every_unknown_id():
    with pytest.raises(UnknownCategoryError) as excinfo:
        validate_categories(["rail_typo", "another_typo"])
    assert "rail_typo" in str(excinfo.value)
    assert "another_typo" in str(excinfo.value)


def test_validate_categories_error_lists_every_valid_id():
    with pytest.raises(UnknownCategoryError) as excinfo:
        validate_categories(["not_a_real_category"])
    for valid_id in ALL_CATEGORY_IDS:
        assert valid_id in str(excinfo.value)


def test_validate_categories_rejects_a_mix_of_known_and_unknown_rather_than_narrowing():
    # A partially-valid selection is rejected outright, not silently
    # narrowed to only the ids it recognises: narrowing would run a
    # DIFFERENT selection than the one actually asked for, which is its
    # own kind of silent wrong answer, just a smaller one.
    with pytest.raises(UnknownCategoryError):
        validate_categories(["buildings", "not_a_real_category"])


def test_unknown_category_error_is_a_value_error():
    # So it needs no special handling anywhere already built to catch bad
    # request input by type: cli.py's main() and server.py's
    # _REQUEST_VALUE_ERRORS both already treat ValueError (BBoxError and
    # TilingError are also subclasses of it) as "a request problem to
    # report plainly, not a traceback."
    assert issubclass(UnknownCategoryError, ValueError)


# --------------------------------------------------------------------------
# The carriageway/path reading of the same table (road_family): what the
# OS benchmark needs so a pavement is never measured against a
# carriageway centreline as though the difference were a survey
# disagreement.
# --------------------------------------------------------------------------


def test_road_family_reads_every_carriageway_class_as_a_carriageway():
    for value in (
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "unclassified",
        "residential",
        "living_street",
        "service",
    ):
        assert road_family(value) == ROAD_FAMILY_CARRIAGEWAY, value


def test_road_family_reads_every_path_class_as_a_path():
    for value in ("footway", "path", "bridleway", "steps", "cycleway", "track"):
        assert road_family(value) == ROAD_FAMILY_PATH, value


def test_road_family_answers_none_for_a_highway_value_in_neither_family():
    # None is a real answer, not a failure: a caller counts these rather
    # than folding them into whichever family happened to be nearest.
    for value in ("construction", "proposed", "pedestrian", "bus_stop", "raceway"):
        assert road_family(value) is None, value


def test_no_highway_value_belongs_to_both_families():
    assert not (CARRIAGEWAY_HIGHWAY_VALUES & PATH_HIGHWAY_VALUES)


def test_the_two_families_are_derived_from_the_one_road_table_not_retyped():
    # Every value either family holds for a subtype that HAS a category
    # id comes from _ROAD_HIGHWAY_VALUES itself, which is what stops the
    # footpath fix above from needing to be made twice. The proof: the
    # four values that fix added are all in PATH_HIGHWAY_VALUES without
    # anything else having been edited to put them there.
    for value in ("footway", "path", "bridleway", "steps"):
        assert value in PATH_HIGHWAY_VALUES, value
    # And every selectable road subtype id lands in exactly one family.
    for subtype_id, _label in ROAD_SUBTYPES:
        clause = osm_tag_clauses([subtype_id])[0]
        families = {
            road_family(value)
            for value in clause.split('"^(')[1].split(')$"')[0].split("|")
        }
        assert families in ({ROAD_FAMILY_CARRIAGEWAY}, {ROAD_FAMILY_PATH}), subtype_id
