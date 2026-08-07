"""mapgen.resolver: covers()/tier() on every real source, and resolve().

No test here ever touches the network. lidar_wales.py, os_open.py and
os_uprn.py's own covers() reach for a cached OSTN15 grid before falling
back to a gridless projection (bng.best_effort_padded_bng_extent); every
instance here is built with `ostn15_cache_dir=tmp_path`, a fresh, empty
directory, so that fallback is always the path actually exercised, and
with `session=_PoisonedSession()` so a test would fail loudly if covers()
ever reached for the network despite that.
"""

from __future__ import annotations

import json

import pytest

from mapgen.geo import BBox
from mapgen.resolver import CATEGORIES, ROLES, resolve
from mapgen.sources.elevation import ElevationSource
from mapgen.sources.inspire import InspireSource
from mapgen.sources.lidar_wales import LidarWalesSource
from mapgen.sources.os_open import OsOpenSource
from mapgen.sources.os_uprn import OsUprnSource
from mapgen.sources.osm import OsmSource
from mapgen.sources.overture import OvertureSource


class _PoisonedSession:
    """Raises on any use. Proves covers() never touches the network."""

    def get(self, *args, **kwargs):
        raise AssertionError("covers() must never touch the network")


# Cardiff/Vale of Glamorgan border, the same bbox test_inspire.py's own
# test_bbox_spanning_cardiff_vale_border_returns_both uses: inside both an
# INSPIRE authority's own padded bbox and the Welsh LiDAR mosaic, and its
# padded BNG extent falls entirely inside grid square ST (verified: see
# this task's own report).
CARDIFF_BBOX = BBox(west=-3.263442, south=51.438073, east=-3.243442, north=51.458073)

# Central Paris: outside Great Britain and outside the National Grid's own
# usable envelope entirely, the same bbox test_sources_os_open.py's own
# module already uses for its "no GB squares" case.
PARIS_BBOX = BBox.parse("2.34,48.85,2.36,48.87")

# Central Edinburgh, the same bbox test_inspire.py's own
# test_scottish_bbox_returns_no_authorities uses: on the National Grid (OS
# Open covers Scotland) but outside every INSPIRE authority (England and
# Wales only).
EDINBURGH_BBOX = BBox(west=-3.30, south=55.90, east=-3.10, north=56.00)

# Straddles the Welsh LiDAR mosaic's own east edge (LidarWalesSource.
# MOSAIC_BOUNDS easting 356000.0): built by reprojecting a point sitting
# exactly on that easting back to WGS84 (bng.tm_inverse(356000.0,
# 250000.0) -> approximately 52.146 N, 2.643 W) and drawing a small bbox
# around it, so the padded BNG extent (a few hundred metres either side,
# plus the 200 m pad) genuinely spans both sides of the mosaic boundary
# rather than landing there by luck.
MOSAIC_EDGE_BBOX = BBox(west=-2.660, south=52.130, east=-2.625, north=52.160)


def _lidar_wales(tmp_path):
    return LidarWalesSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)


def _os_open(tmp_path):
    return OsOpenSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)


def _os_uprn(tmp_path):
    return OsUprnSource(session=_PoisonedSession(), ostn15_cache_dir=tmp_path)


def _all_sources(tmp_path):
    """Every real source this project ships, each with covers()/tier():
    the "all sources selected" case the brief's own worked examples use.
    """
    return [
        OsmSource(),
        OvertureSource(),
        ElevationSource(),
        _lidar_wales(tmp_path),
        InspireSource(),
        _os_open(tmp_path),
        _os_uprn(tmp_path),
    ]


# --------------------------------------------------------------------------
# covers()
# --------------------------------------------------------------------------


def test_cardiff_lidar_wales_covers_full(tmp_path):
    assert _lidar_wales(tmp_path).covers(CARDIFF_BBOX) == "full"


def test_cardiff_inspire_covers_full():
    assert InspireSource().covers(CARDIFF_BBOX) == "full"


def test_cardiff_os_open_covers_full(tmp_path):
    assert _os_open(tmp_path).covers(CARDIFF_BBOX) == "full"


def test_mosaic_edge_bbox_lidar_wales_covers_partial(tmp_path):
    assert _lidar_wales(tmp_path).covers(MOSAIC_EDGE_BBOX) == "partial"


def test_paris_lidar_wales_covers_none(tmp_path):
    assert _lidar_wales(tmp_path).covers(PARIS_BBOX) == "none"


def test_paris_inspire_covers_none():
    assert InspireSource().covers(PARIS_BBOX) == "none"


def test_paris_os_open_covers_none(tmp_path):
    # No GB squares: Paris projects well outside the National Grid's own
    # usable envelope, so squares_for over its padded extent is empty.
    assert _os_open(tmp_path).covers(PARIS_BBOX) == "none"


def test_paris_osm_covers_full():
    assert OsmSource().covers(PARIS_BBOX) == "full"


def test_paris_overture_covers_full():
    assert OvertureSource().covers(PARIS_BBOX) == "full"


def test_paris_elevation_covers_full():
    assert ElevationSource().covers(PARIS_BBOX) == "full"


def test_edinburgh_inspire_covers_none():
    # INSPIRE Index Polygons cover England and Wales only.
    assert InspireSource().covers(EDINBURGH_BBOX) == "none"


def test_edinburgh_os_open_covers_full(tmp_path):
    # OS Open covers the whole of Great Britain, Scotland included.
    assert _os_open(tmp_path).covers(EDINBURGH_BBOX) == "full"


def test_os_uprn_covers_matches_os_open_over_the_same_gb_squares(tmp_path):
    # Same GB_SQUARES constant, same national coverage; see os_shards.py's
    # own comment on why os_uprn.py's covers() is not independently probed.
    assert _os_uprn(tmp_path).covers(CARDIFF_BBOX) == "full"
    assert _os_uprn(tmp_path).covers(PARIS_BBOX) == "none"
    assert _os_uprn(tmp_path).covers(EDINBURGH_BBOX) == "full"


# --------------------------------------------------------------------------
# tier()
# --------------------------------------------------------------------------


def test_osm_tier_table():
    source = OsmSource()
    assert source.tier("buildings") == 1
    assert source.tier("roads") == 1
    assert source.tier("rail") == 1
    assert source.tier("greenspace") == 3
    assert source.tier("land_use") == 2
    assert source.tier("addresses") is None


def test_overture_tier_table():
    source = OvertureSource()
    assert source.tier("heights") == 2
    assert source.tier("buildings") == 2
    assert source.tier("land_use") == 1
    assert source.tier("water") == 1
    assert source.tier("places") == 1
    assert source.tier("greenspace") == 2
    assert source.tier("land") == 2
    assert source.tier("roads") is None


def test_elevation_tier_table():
    source = ElevationSource()
    assert source.tier("terrain") == 2
    assert source.tier("contours") is None


def test_lidar_wales_tier_table(tmp_path):
    source = _lidar_wales(tmp_path)
    assert source.tier("terrain") == 1
    assert source.tier("contours") == 1
    assert source.tier("heights") == 1
    assert source.tier("buildings") is None


def test_inspire_tier_table():
    source = InspireSource()
    assert source.tier("boundaries") == 1
    assert source.tier("buildings") is None


def test_os_open_tier_table(tmp_path):
    source = _os_open(tmp_path)
    assert source.tier("buildings") == 3
    assert source.tier("roads") == 2
    assert source.tier("rail") == 2
    assert source.tier("greenspace") == 1
    assert source.tier("sites") == 1
    assert source.tier("land") == 1
    assert source.tier("water") == 2
    assert source.tier("places") == 2
    assert source.tier("terrain") is None


def test_os_uprn_tier_table(tmp_path):
    source = _os_uprn(tmp_path)
    assert source.tier("addresses") == 1
    assert source.tier("buildings") is None


# --------------------------------------------------------------------------
# CATEGORIES / ROLES
# --------------------------------------------------------------------------


def test_categories_is_the_fourteen_in_spec_order():
    assert CATEGORIES == (
        "terrain",
        "contours",
        "heights",
        "buildings",
        "roads",
        "rail",
        "boundaries",
        "greenspace",
        "sites",
        "land",
        "addresses",
        "land_use",
        "water",
        "places",
    )


def test_roles_names_only_os_open_roads_and_rail_as_reference():
    assert ROLES == {
        ("roads", "os_open"): "reference",
        ("rail", "os_open"): "reference",
    }


# --------------------------------------------------------------------------
# resolve()
# --------------------------------------------------------------------------


def _by_category(result):
    return {entry["category"]: entry["sources"] for entry in result}


def test_resolve_cardiff_with_all_sources_selected(tmp_path):
    result = resolve(CARDIFF_BBOX, _all_sources(tmp_path))
    by_category = _by_category(result)

    buildings = [(s["id"], s["tier"], s["role"]) for s in by_category["buildings"]]
    assert buildings == [
        ("osm", 1, "base"),
        ("overture", 2, "fill"),
        ("os_open", 3, "fill"),
    ]

    roads = [(s["id"], s["role"]) for s in by_category["roads"]]
    assert roads == [("osm", "base"), ("os_open", "reference")]

    rail = [(s["id"], s["role"]) for s in by_category["rail"]]
    assert rail == [("osm", "base"), ("os_open", "reference")]

    assert [s["id"] for s in by_category["addresses"]] == ["os_uprn"]
    assert by_category["addresses"][0]["role"] == "base"


def test_resolve_cardiff_every_entry_carries_full_coverage(tmp_path):
    # Cardiff sits inside every source's own coverage in this project, so
    # every entry in every category reads "full", never "partial".
    result = resolve(CARDIFF_BBOX, _all_sources(tmp_path))
    for entry in result:
        for source_entry in entry["sources"]:
            assert source_entry["coverage"] == "full"


def test_resolve_paris_omits_categories_with_no_covering_source(tmp_path):
    result = resolve(PARIS_BBOX, _all_sources(tmp_path))
    by_category = _by_category(result)

    # os_open's own buildings tier (3) drops out with no coverage; osm and
    # overture, both global, remain.
    assert [s["id"] for s in by_category["buildings"]] == ["osm", "overture"]

    # boundaries (inspire only), sites (os_open only) and addresses
    # (os_uprn only) each have exactly one possible source, and that
    # source covers "none" here, so the category has nothing to list and
    # is omitted entirely rather than included empty.
    for category in ("boundaries", "sites", "addresses"):
        assert category not in by_category

    # greenspace is NOT in that list, on purpose: unlike boundaries/sites/
    # addresses, its tier table (os_open 1, overture 2, osm 3) also names
    # two global sources, so Paris still resolves it through them even
    # with os_open's own entry dropped. See this task's own report for
    # this exact divergence from the plan's own worked example, which
    # named greenspace alongside the three GB-exclusive categories above.
    assert [s["id"] for s in by_category["greenspace"]] == ["overture", "osm"]


def test_resolve_selection_matters_os_open_unselected_removes_its_entries(tmp_path):
    with_os_open = resolve(CARDIFF_BBOX, _all_sources(tmp_path))
    without_os_open = resolve(
        CARDIFF_BBOX,
        [s for s in _all_sources(tmp_path) if s.id != "os_open"],
    )

    with_ids = _by_category(with_os_open)["buildings"]
    without_ids = _by_category(without_os_open)["buildings"]
    assert "os_open" in [s["id"] for s in with_ids]
    assert "os_open" not in [s["id"] for s in without_ids]

    # sites has only one server at all (os_open, tier 1): removing it
    # removes the whole category.
    assert "sites" in _by_category(with_os_open)
    assert "sites" not in _by_category(without_os_open)


def test_resolve_is_json_ready(tmp_path):
    result = resolve(CARDIFF_BBOX, _all_sources(tmp_path))
    # Round-trips through json.dumps/json.loads with no TypeError and no
    # loss: plain dicts, lists, strs and ints throughout, exactly as it
    # must to land verbatim in both the estimate payload and survey.json.
    assert json.loads(json.dumps(result)) == result


def test_resolve_skips_a_source_missing_covers_or_tier_silently():
    class _NoExtensions:
        id = "bare"
        display_name = "Bare"

    class _CoversOnly:
        id = "covers-only"
        display_name = "Covers Only"

        def covers(self, bbox):
            return "full"

    class _TierOnly:
        id = "tier-only"
        display_name = "Tier Only"

        def tier(self, category):
            return 1

    # None of these three ever appears in any category: resolve() must
    # not raise over a source with no opinion on tiering, the same
    # optional-extension convention every other one in sources/base.py
    # follows.
    result = resolve(CARDIFF_BBOX, [_NoExtensions(), _CoversOnly(), _TierOnly()])
    assert result == []


def test_resolve_returns_empty_list_for_no_sources():
    assert resolve(CARDIFF_BBOX, []) == []


def test_resolve_cardiff_terrain_carries_lidar_and_elevation_detail(tmp_path):
    # Task 2 of the detail-preview plan: an entry's own detail(), when the
    # source defines one, flows straight through resolve() into the
    # entry dict. lidar_wales.detail() (Task 1, committed) is bbox-shaped,
    # so only its own leading words are pinned here, not the whole
    # sentence; elevation.detail() is the fixed spec-copy string this
    # task adds, and is pinned exactly.
    result = resolve(CARDIFF_BBOX, _all_sources(tmp_path))
    by_category = _by_category(result)
    terrain = {s["id"]: s for s in by_category["terrain"]}
    assert terrain["lidar_wales"]["detail"].startswith(("1 m", "2 m"))
    assert terrain["elevation"]["detail"] == "30 m (Copernicus GLO-30)"


def test_resolve_entry_omits_detail_key_when_source_has_no_detail_method():
    class _StubNoDetail:
        id = "stub-no-detail"
        display_name = "Stub No Detail"

        def covers(self, bbox):
            return "full"

        def tier(self, category):
            return 1 if category == "terrain" else None

    result = resolve(CARDIFF_BBOX, [_StubNoDetail()])
    entry = result[0]["sources"][0]
    assert "detail" not in entry


def test_resolve_entry_omits_detail_key_when_detail_returns_none():
    # The resolver's own defensive rule: "detail" is added only when
    # getattr(source, "detail", None) is callable AND returns non-None,
    # never a present key holding None. No real source in this project
    # reaches this branch through resolve() itself (a source whose
    # covers(bbox) is not "none" is exactly the source whose detail(bbox)
    # this project's own sources answer with a real string, never None),
    # so this is exercised with a stub built for exactly that shape.
    class _StubDetailNone:
        id = "stub-detail-none"
        display_name = "Stub Detail None"

        def covers(self, bbox):
            return "full"

        def tier(self, category):
            return 1 if category == "terrain" else None

        def detail(self, bbox):
            return None

    result = resolve(CARDIFF_BBOX, [_StubDetailNone()])
    entry = result[0]["sources"][0]
    assert "detail" not in entry


def test_resolve_ties_break_on_source_id():
    class _StubA:
        id = "zzz-stub"
        display_name = "ZZZ Stub"

        def covers(self, bbox):
            return "full"

        def tier(self, category):
            return 1 if category == "terrain" else None

    class _StubB:
        id = "aaa-stub"
        display_name = "AAA Stub"

        def covers(self, bbox):
            return "full"

        def tier(self, category):
            return 1 if category == "terrain" else None

    result = resolve(CARDIFF_BBOX, [_StubA(), _StubB()])
    terrain = _by_category(result)["terrain"]
    assert [s["id"] for s in terrain] == ["aaa-stub", "zzz-stub"]
    # Tied tier, so both are "base": role derivation is "best tier
    # present", not "first in the sorted list".
    assert all(s["role"] == "base" for s in terrain)
