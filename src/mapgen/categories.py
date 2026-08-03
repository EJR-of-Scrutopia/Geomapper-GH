"""The category vocabulary: buildings, roads (with road-class detail),
water, vegetation and landuse, rail, boundaries, points of interest.

One vocabulary, read from one place, by everything that needs to agree on
it: the CLI's --category flag, GET /api/categories (which the browser's
checklist is rendered from), OsmSource's Overpass tag filter, and the
Overture type mapping. None of those four keep their own copy of the list
of ids, which is what stops them drifting out of sync with each other the
same way the settings panel's registry-driven design avoids duplicating
source ids per key.

Selection is always a set of LEAF ids. "roads" itself is a grouping label
for its nine road-class children, not a selectable id of its own: there is
nothing an OSM tag filter or an Overture type could do with "every road"
that selecting all nine children does not already do, and a flat set of
leaves is simpler to reason about than a set that can contain both a group
and some of its own children at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CategoryGroup:
    id: str
    label: str
    # Leaf ids belonging to this group. Empty for a group that is itself
    # a leaf (every group except "roads", today).
    children: tuple[str, ...] = ()


ROAD_SUBTYPES: tuple[tuple[str, str], ...] = (
    ("motorway", "Motorway"),
    ("trunk", "Trunk"),
    ("primary", "Primary"),
    ("secondary", "Secondary"),
    ("residential", "Residential"),
    ("service", "Service"),
    ("footpath", "Footpath"),
    ("cycleway", "Cycleway"),
    ("track", "Track"),
)

CATEGORY_GROUPS: tuple[CategoryGroup, ...] = (
    CategoryGroup("buildings", "Buildings"),
    CategoryGroup("roads", "Roads", children=tuple(id_ for id_, _label in ROAD_SUBTYPES)),
    CategoryGroup("water", "Water"),
    CategoryGroup("vegetation_and_landuse", "Vegetation and landuse"),
    CategoryGroup("rail", "Rail"),
    CategoryGroup("boundaries", "Boundaries"),
    CategoryGroup("points_of_interest", "Points of interest"),
)

# Every selectable leaf id, in a stable, deliberate order (buildings, the
# nine road classes, then the remaining five groups): this is both the
# CLI's and the browser's "select everything" set, and the set
# osm_tag_clauses and overture_types_for_categories compare a selection
# against to recognise "nothing has actually been narrowed".
ALL_CATEGORY_IDS: tuple[str, ...] = tuple(
    id_ for group in CATEGORY_GROUPS for id_ in (group.children or (group.id,))
)


class UnknownCategoryError(ValueError):
    """Raised by validate_categories for an id outside ALL_CATEGORY_IDS.

    A coordinator review's Critical 1: nothing validated a category
    selection before it reached osm_tag_clauses and
    overture_types_for_categories below, and both of those correctly
    treat an unrecognised id exactly like a real one that just is not
    present in their own lookup tables, because distinguishing "not
    selected" from "does not exist" was never their job. The result,
    reproduced live: --category building (missing its "s") matched
    nothing in either function, which read back as a legitimate,
    deliberate "match nothing" selection, the same shape
    osm_tag_clauses' own docstring documents as a real, intended answer
    for a genuinely empty selection. The run completed: 0 nodes, an
    85-byte file, complete: true, and survey.json recording
    categories: ["building"] as though that had been honoured.

    A ValueError subclass, not a plain one, so it needs no special
    handling anywhere already built to catch bad input by type: cli.py's
    main() and server.py's _REQUEST_VALUE_ERRORS both already treat
    ValueError (BBoxError and TilingError are also subclasses of it) as
    "a request problem to report plainly, not a traceback."
    """


class EmptyCategorySelectionError(ValueError):
    """Raised by validate_categories for an explicitly empty selection.

    A Task 21 owner-reported defect, the same family as Critical 1 above
    and deliberately left open at the time: unticking every category in
    the browser, or otherwise passing categories=[], used to be accepted
    silently. osm_tag_clauses([]) and overture_types_for_categories([])
    both correctly treat an empty selection as "match nothing" (that is
    their own, correct job, see osm_tag_clauses' own docstring), which
    means the request that reaches them is honoured exactly as asked: an
    empty package, reported as complete: true, with nothing to say why.
    The owner hit this by accident, not by deliberately wanting an empty
    survey, and there is no real workflow this tool serves where "survey
    nothing" is a useful, intentional answer, unlike an unfiltered
    (categories=None) request, which is a real and common one.

    Raised from the exact same place, and the same way, as
    UnknownCategoryError: at SurveyRequest construction, via
    validate_categories, so the CLI and the browser both get this
    through the one code path they already share, and cli.py's main()
    needs the same one-line addition to its existing exception tuple
    that UnknownCategoryError itself already needed.

    A ValueError subclass, not a plain one, for the same reason
    UnknownCategoryError is: server.py's _REQUEST_VALUE_ERRORS already
    catches ValueError generically, so this needs no change there at
    all, only in cli.py's own explicit tuple.

    Deliberately a DIFFERENT class from UnknownCategoryError rather than
    reusing it: an empty selection is not "an id outside the vocabulary",
    it is the vocabulary being consulted correctly and truthfully
    reporting that nothing was asked for. Conflating the two would make
    UnknownCategoryError's own docstring, and any caller matching on it
    specifically, describe a condition it no longer only means.

    None is not this: see effective_categories on SurveyRequest for why
    "not asked about at all" and "asked for nothing" must never collapse
    into each other.
    """


def validate_categories(categories: Sequence[str] | None) -> None:
    """Raises UnknownCategoryError if categories contains any id this
    vocabulary does not recognise, or EmptyCategorySelectionError if
    categories is present but empty.

    Called once, from SurveyRequest.__post_init__ (see package.py), so
    the CLI's --category and the browser's checklist share this one
    check: this is validation of a person's own words becoming an id,
    which belongs at the point that happens, not repeated inside (or
    worse, half-inside) every function downstream that consumes a
    category selection.

    None passes silently: it means "not asked about at all", not "asked
    for nothing", and is not something a caller can misspell. An empty,
    non-None sequence is the thing this function refuses: see
    EmptyCategorySelectionError's own docstring for why that is a
    different, later-added case from the unknown-id one above, and why
    it is refused here rather than left to reach osm_tag_clauses and
    overture_types_for_categories, which would honour it silently.
    """
    if categories is None:
        return
    if len(categories) == 0:
        raise EmptyCategorySelectionError(
            "No categories are selected. At least one category is needed."
        )
    unknown = sorted(set(categories) - set(ALL_CATEGORY_IDS))
    if not unknown:
        return
    noun = "category" if len(unknown) == 1 else "categories"
    raise UnknownCategoryError(
        f"Unknown {noun}: {', '.join(unknown)}. "
        f"Valid categories are: {', '.join(ALL_CATEGORY_IDS)}."
    )


# --- OSM: Overpass tag filtering -------------------------------------------
#
# The UI's category ids do not always spell the real OSM tag value: OSM
# tags a footpath highway=footway, not highway=footpath. "footpath" is the
# id shown to a person; the translation to the real tag value lives here,
# once, so a filter built from these never silently matches nothing.
#
# motorway/trunk/primary/secondary each pull in their own "_link" variant
# (motorway_link, and so on): these are the short connecting roads at a
# junction or slip road, tagged as a continuation of the class they join,
# not their own category in this vocabulary. Omitting them would leave
# gaps at every junction in an otherwise-selected road class, which is a
# worse outcome than the alternative of a name that only approximately
# matches ("motorway" pulling in one closely related tag alongside it).
_ROAD_HIGHWAY_VALUES: dict[str, tuple[str, ...]] = {
    "motorway": ("motorway", "motorway_link"),
    "trunk": ("trunk", "trunk_link"),
    "primary": ("primary", "primary_link"),
    "secondary": ("secondary", "secondary_link"),
    "residential": ("residential",),
    "service": ("service",),
    "footpath": ("footway",),
    "cycleway": ("cycleway",),
    "track": ("track",),
}

# Overpass tag-filter clause bodies (the part of nwr[...] inside the
# brackets) for the six non-road leaf categories. A first-pass tag
# taxonomy, not an exhaustive survey of every OSM tag an architect might
# ever want: documented here, and in the README, as a starting point that
# can be refined, not a claim of completeness.
_NON_ROAD_OSM_TAG_CLAUSES: dict[str, tuple[str, ...]] = {
    "buildings": ('["building"]',),
    "water": ('["natural"="water"]', '["waterway"]'),
    "vegetation_and_landuse": (
        '["landuse"]',
        '["natural"~"^(wood|scrub|heath|grassland|wetland)$"]',
        '["leisure"~"^(park|garden|nature_reserve|golf_course)$"]',
    ),
    "rail": ('["railway"]',),
    "boundaries": ('["boundary"]',),
    "points_of_interest": ('["amenity"]', '["shop"]', '["tourism"]'),
}


def osm_tag_clauses(categories: Sequence[str] | None) -> list[str] | None:
    """Overpass tag-filter clause bodies for the selected leaf category
    ids, or None if categories is None or already covers every known id.

    None is a real, distinct answer, not merely an empty list: it tells
    the caller (build_overpass_query) to use the original, unfiltered
    query rather than a tag-filtered query that happens to match the
    same ground. That distinction is a correctness requirement, not an
    optimisation: "everything selected" must reproduce today's query
    byte for byte, since existing workflows must not silently start
    returning less (see the module's own callers for why an equivalent
    but differently-shaped query is not good enough).

    An empty list, in contrast, is a real "match nothing" answer: every
    category was considered and none is selected. Distinguishing an
    empty selection from an unfiltered one is what build_overpass_query
    needs to build a query that actually returns nothing, rather than
    silently falling back to unfiltered because an empty filter list
    looked the same as no filtering at all.
    """
    if categories is None:
        return None
    selected = set(categories)
    if selected >= set(ALL_CATEGORY_IDS):
        return None

    clauses: list[str] = []
    for category_id, tag_clauses in _NON_ROAD_OSM_TAG_CLAUSES.items():
        if category_id in selected:
            clauses.extend(tag_clauses)

    highway_values = [
        value
        for subtype, values in _ROAD_HIGHWAY_VALUES.items()
        if subtype in selected
        for value in values
    ]
    if highway_values:
        pattern = "|".join(highway_values)
        clauses.append(f'["highway"~"^({pattern})$"]')

    return clauses


# --- Overture: type selection -----------------------------------------------
#
# Overture's `overturemaps download` CLI selects by --type (one dataset
# per download), with no property-level filter: there is no way to ask it
# for "only rail" out of a segment download, or "only motorways" out of
# it either, the way an Overpass tag filter can. Road-class detail
# therefore has no effect on Overture at all: any road subtype being
# selected pulls in the whole segment/connector pair, and rail similarly
# has no type of its own to select (Overture tags rail as a subtype of
# segment, not a separate download). boundaries has no corresponding
# default type either (Overture's division/division_area/division_boundary
# types are not part of DEFAULT_OVERTURE_TYPES and are not fetched by this
# tool at all). Selecting only rail, or only boundaries, therefore
# contributes nothing to Overture: honest, not a bug, and the reason
# OsmSource's own Overpass filter is the one place these two categories
# actually do something.
_OVERTURE_TYPES_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "buildings": ("building",),
    "water": ("water",),
    "vegetation_and_landuse": ("land_use", "land_cover"),
    "points_of_interest": ("place", "infrastructure"),
}


def overture_types_for_categories(categories: Sequence[str]) -> list[str]:
    """The Overture types to fetch for a resolved category selection.

    Every road subtype maps to the same pair (segment, connector), since
    Overture cannot narrow within them by class; the id is included in
    the dict once per name it maps to, not once per subtype, to avoid
    adding "segment"/"connector" nine times over only to deduplicate them
    straight back out again.
    """
    selected = set(categories)
    types: set[str] = set()
    for category_id, overture_types in _OVERTURE_TYPES_BY_CATEGORY.items():
        if category_id in selected:
            types.update(overture_types)
    if selected & {subtype for subtype, _label in ROAD_SUBTYPES}:
        types.update(("segment", "connector"))
    # Stable, deterministic order rather than whatever set() iteration
    # happens to produce: matches DEFAULT_OVERTURE_TYPES' own ordering
    # convention, and keeps survey.json's recorded `types` list stable
    # across otherwise-identical runs.
    ordering = ["building", "place", "segment", "connector", "infrastructure", "land_use", "land_cover", "water"]
    return [t for t in ordering if t in types]
