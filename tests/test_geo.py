import json
import re
import time

import pytest

from mapgen.geo import (
    BBox,
    BBoxError,
    Tile,
    TilingError,
    build_tiles,
    extent_metres,
    split_tile_into_quarters,
)


def test_parse_accepts_west_south_east_north():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    assert bbox.west == pytest.approx(-3.6626)
    assert bbox.south == pytest.approx(51.3709)
    assert bbox.east == pytest.approx(-3.1483)
    assert bbox.north == pytest.approx(51.5476)


def test_parse_normalises_reversed_pairs():
    bbox = BBox.parse("-3.1483,51.5476,-3.6626,51.3709")
    assert bbox.west == pytest.approx(-3.6626)
    assert bbox.south == pytest.approx(51.3709)
    assert bbox.east == pytest.approx(-3.1483)
    assert bbox.north == pytest.approx(51.5476)


def test_parse_rejects_wrong_field_count():
    with pytest.raises(BBoxError, match="4 comma-separated"):
        BBox.parse("-3.66,51.37,-3.14")


def test_parse_rejects_non_numeric():
    with pytest.raises(BBoxError, match="Invalid bbox"):
        BBox.parse("-3.66,51.37,-3.14,north")


def test_parse_rejects_out_of_range_longitude():
    with pytest.raises(BBoxError, match="Longitude"):
        BBox.parse("-200,51.37,-3.14,51.54")


def test_parse_rejects_out_of_range_latitude():
    with pytest.raises(BBoxError, match="Latitude"):
        BBox.parse("-3.66,-91,-3.14,51.54")


def test_parse_rejects_zero_area():
    with pytest.raises(BBoxError, match="zero width or height"):
        BBox.parse("-3.66,51.37,-3.66,51.54")


def test_query_string_is_seven_decimal_places():
    bbox = BBox.parse("-3.5,51.5,-3.4,51.6")
    assert bbox.to_query_string() == "-3.5000000,51.5000000,-3.4000000,51.6000000"


def test_centre_is_the_midpoint():
    lon, lat = BBox.parse("-4,51,-2,53").centre
    assert lon == pytest.approx(-3.0)
    assert lat == pytest.approx(52.0)


def test_extent_metres_is_plausible_for_a_known_box():
    width_m, height_m = extent_metres(BBox.parse("-3.6626,51.3709,-3.1483,51.5476"))
    assert width_m == pytest.approx(35_600, rel=0.02)
    assert height_m == pytest.approx(19_700, rel=0.02)


def test_build_tiles_covers_the_area_in_a_grid():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    tiles = build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0)
    assert len(tiles) == 32
    assert tiles[0].tile_id == "r00_c00"
    assert tiles[0].row == 0 and tiles[0].col == 0
    assert tiles[-1].tile_id == "r03_c07"


def test_build_tiles_query_bbox_is_never_smaller_than_core():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    for tile in build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0):
        assert tile.query_bbox.west <= tile.core_bbox.west
        assert tile.query_bbox.south <= tile.core_bbox.south
        assert tile.query_bbox.east >= tile.core_bbox.east
        assert tile.query_bbox.north >= tile.core_bbox.north


def test_build_tiles_query_bbox_is_clamped_to_the_study_area():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    for tile in build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0):
        assert tile.query_bbox.west >= bbox.west
        assert tile.query_bbox.south >= bbox.south
        assert tile.query_bbox.east <= bbox.east
        assert tile.query_bbox.north <= bbox.north


def test_build_tiles_single_tile_when_area_is_smaller_than_tile_size():
    tiles = build_tiles(BBox.parse("-3.29,51.38,-3.28,51.39"), 5000.0, 100.0)
    assert len(tiles) == 1
    assert tiles[0].tile_id == "r00_c00"


# --- Review round 1: tile_size_m must be validated, and an absurd tiling
# refused cheaply, before build_tiles's own nested loop runs -------------
#
# A zero tile_size_m previously raised an uncaught ZeroDivisionError deep
# in the row/col arithmetic, which server.py's except tuple did not
# catch, dropping the connection on /api/estimate and /api/extent, and
# on /api/jobs being accepted with a 202 that then failed asynchronously
# in the worker instead of being rejected up front. A negative tile_size_m
# did not raise at all: ceil() of a negative quotient is a negative int,
# range() of a negative count is empty, so it silently returned "tiles":
# 0, 200 OK, with nothing to say the input was nonsensical.


def test_build_tiles_rejects_a_zero_tile_size():
    with pytest.raises(TilingError, match="greater than zero"):
        build_tiles(BBox.parse("-3.29,51.38,-3.28,51.39"), tile_size_m=0.0, overlap_m=50.0)


def test_build_tiles_rejects_a_negative_tile_size():
    with pytest.raises(TilingError, match="greater than zero"):
        build_tiles(BBox.parse("-3.29,51.38,-3.28,51.39"), tile_size_m=-600.0, overlap_m=50.0)


def test_build_tiles_refuses_a_tiling_over_the_max_tiles_cap():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")  # the reference bbox: 32 tiles at 5000m
    # A tiny cap makes this cheap and deterministic to test without
    # actually building a many-thousand-tile bbox: the real default
    # (10,000) is unaffected, this only overrides it for the test.
    with pytest.raises(TilingError, match="over the 10 limit"):
        build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0, max_tiles=10)


def test_build_tiles_at_or_under_the_cap_is_unaffected():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    tiles = build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0, max_tiles=32)
    assert len(tiles) == 32


def test_build_tiles_cap_check_runs_before_the_expensive_loop():
    # A previous version of this test only checked that TilingError is
    # eventually raised, which is equally true whether the cap is checked
    # on the cheap row*col arithmetic or only after the nested loop has
    # already built every Tile: moving the check to run after the loop
    # still passed it. Reviewer-confirmed: the web-level tests only
    # noticed that mutation as a 60-second socket timeout, twice, not as
    # a failing assertion, which is a hang dressed up as a passing test,
    # not a test of cheapness. Measured directly instead: this exact bbox
    # and tile size take a confirmed 8.6s to actually build (1,341,256
    # Tile objects), so a cheap, pre-loop rejection and an after-the-fact
    # one are trivially distinguishable by elapsed time, with a wide
    # margin on either side of that measurement.
    bbox = BBox.parse("-10.0,35.0,30.0,60.0")  # roughly Western Europe
    started = time.perf_counter()
    with pytest.raises(TilingError):
        build_tiles(bbox, tile_size_m=2500.0, overlap_m=250.0)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, (
        f"expected a cheap, pre-loop rejection (milliseconds), took {elapsed:.3f}s: "
        f"the cap check may have moved to run after the tile-building loop"
    )


def test_bbox_to_dict_returns_four_keys():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    result = bbox.to_dict()
    assert set(result.keys()) == {"west", "south", "east", "north"}
    assert isinstance(result["west"], float)
    assert isinstance(result["south"], float)
    assert isinstance(result["east"], float)
    assert isinstance(result["north"], float)


def test_bbox_to_dict_rounds_to_seven_decimals():
    bbox = BBox(
        west=-3.66265432123456,
        south=51.37090987654321,
        east=-3.14830555555555,
        north=51.54760123456789,
    )
    result = bbox.to_dict()
    assert result["west"] == -3.6626543
    assert result["south"] == 51.3709099
    assert result["east"] == -3.1483056
    assert result["north"] == 51.5476012


def test_tile_to_dict_returns_required_keys():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    tiles = build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0)
    tile_dict = tiles[0].to_dict()
    assert set(tile_dict.keys()) == {"tile_id", "row", "col", "core_bbox", "query_bbox"}
    assert tile_dict["tile_id"] == "r00_c00"
    assert tile_dict["row"] == 0
    assert tile_dict["col"] == 0


def test_tile_to_dict_nested_bboxes_are_dicts():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    tiles = build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0)
    tile_dict = tiles[0].to_dict()
    assert isinstance(tile_dict["core_bbox"], dict)
    assert isinstance(tile_dict["query_bbox"], dict)
    assert set(tile_dict["core_bbox"].keys()) == {"west", "south", "east", "north"}
    assert set(tile_dict["query_bbox"].keys()) == {"west", "south", "east", "north"}


def test_tile_to_dict_is_json_serialisable():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    tiles = build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0)
    tile_dict = tiles[5].to_dict()
    json_str = json.dumps(tile_dict)
    assert isinstance(json_str, str)
    assert "tile_id" in json_str
    assert tiles[5].tile_id in json_str


# --- Task 26: one tile cut into quarters, for the OSM node cap ------------
#
# The recombined parent file must cover the same ground as an unsplit fetch
# of that tile would have. These pin the two properties the recombine rests
# on (exact cover, never wider than the parent) at the arithmetic level,
# where they can be checked against the numbers rather than inferred from a
# downloaded file.


def _middle_tile():
    """A tile with a real overlap margin on all four sides: row 1, col 1 of
    a grid, so no side of it is clamped against the extent."""
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    tiles = build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0)
    tile = next(t for t in tiles if t.row == 1 and t.col == 1)
    assert tile.query_bbox.west < tile.core_bbox.west
    assert tile.query_bbox.east > tile.core_bbox.east
    assert tile.query_bbox.south < tile.core_bbox.south
    assert tile.query_bbox.north > tile.core_bbox.north
    return tile


def test_a_tile_splits_into_four_quarters():
    quarters = split_tile_into_quarters(_middle_tile())
    assert len(quarters) == 4


def test_quarter_ids_name_their_parent_and_their_place_in_it():
    quarters = split_tile_into_quarters(_middle_tile())
    assert [q.tile_id for q in quarters] == [
        "r01_c01_q00",
        "r01_c01_q01",
        "r01_c01_q10",
        "r01_c01_q11",
    ]


def test_a_quarter_id_is_not_shaped_like_a_tile_id_of_the_plan():
    # package.py's _existing_output_files treats anything matching
    # ^r\d+_c\d+$ that is not in the current plan as stale debris, and
    # anything NOT matching it as a legitimate non-tile output. A quarter
    # is neither, so it must not be able to pass for either: the suffix is
    # what keeps it out of that regex entirely.
    from mapgen.package import _TILE_ID_SHAPE

    for quarter in split_tile_into_quarters(_middle_tile()):
        assert not _TILE_ID_SHAPE.match(quarter.tile_id)
        assert re.match(r"^r\d+_c\d+(_q\d\d)+$", quarter.tile_id)


def test_quarter_cores_tile_the_parent_core_exactly():
    parent = _middle_tile()
    quarters = split_tile_into_quarters(parent)
    cores = [q.core_bbox for q in quarters]
    # Exact equality, not approx: the outer sides are the parent's own
    # floats, copied rather than recomputed.
    assert min(c.west for c in cores) == parent.core_bbox.west
    assert max(c.east for c in cores) == parent.core_bbox.east
    assert min(c.south for c in cores) == parent.core_bbox.south
    assert max(c.north for c in cores) == parent.core_bbox.north
    # And they meet on a single shared seam rather than overlapping or
    # leaving a strip between them: every quarter's inner side is the
    # midpoint, one value, shared.
    mid_lon = (parent.core_bbox.west + parent.core_bbox.east) / 2.0
    mid_lat = (parent.core_bbox.south + parent.core_bbox.north) / 2.0
    assert {c.east for c in cores} == {mid_lon, parent.core_bbox.east}
    assert {c.west for c in cores} == {mid_lon, parent.core_bbox.west}
    assert {c.north for c in cores} == {mid_lat, parent.core_bbox.north}
    assert {c.south for c in cores} == {mid_lat, parent.core_bbox.south}


def test_the_quarters_together_ask_for_exactly_the_parents_own_ground():
    # The property the recombine depends on: no gap at the outer edge (the
    # union reaches the parent's query_bbox on all four sides) and no
    # reach beyond it (nothing outside the ground the plan asked for).
    parent = _middle_tile()
    queries = [q.query_bbox for q in split_tile_into_quarters(parent)]
    assert min(q.west for q in queries) == parent.query_bbox.west
    assert max(q.east for q in queries) == parent.query_bbox.east
    assert min(q.south for q in queries) == parent.query_bbox.south
    assert max(q.north for q in queries) == parent.query_bbox.north
    for query in queries:
        assert query.west >= parent.query_bbox.west
        assert query.east <= parent.query_bbox.east
        assert query.south >= parent.query_bbox.south
        assert query.north <= parent.query_bbox.north


def test_quarters_overlap_each_other_at_the_seam_by_the_parents_own_overlap():
    parent = _middle_tile()
    by_id = {q.tile_id[-3:]: q for q in split_tile_into_quarters(parent)}
    overlap_lon = parent.core_bbox.west - parent.query_bbox.west
    overlap_lat = parent.core_bbox.south - parent.query_bbox.south
    assert overlap_lon > 0 and overlap_lat > 0

    # The west quarter reaches past the seam by the overlap, and the east
    # quarter reaches back past it by the same, so a feature standing on
    # the seam is whole in both, exactly as build_tiles' own neighbours do.
    mid_lon = (parent.core_bbox.west + parent.core_bbox.east) / 2.0
    mid_lat = (parent.core_bbox.south + parent.core_bbox.north) / 2.0
    assert by_id["q00"].query_bbox.east == pytest.approx(mid_lon + overlap_lon)
    assert by_id["q01"].query_bbox.west == pytest.approx(mid_lon - overlap_lon)
    assert by_id["q00"].query_bbox.north == pytest.approx(mid_lat + overlap_lat)
    assert by_id["q10"].query_bbox.south == pytest.approx(mid_lat - overlap_lat)


def test_a_quarter_of_an_edge_tile_still_stops_at_the_extent():
    # An edge tile's query_bbox is clamped against the extent on its
    # outward side (build_tiles), so its quarters must be too: the whole
    # point of clamping is that a survey never downloads ground outside
    # the rectangle the owner drew.
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    tiles = build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0)
    corner = next(t for t in tiles if t.row == 0 and t.col == 0)
    assert corner.query_bbox.west == bbox.west
    assert corner.query_bbox.south == bbox.south

    quarters = split_tile_into_quarters(corner)
    for quarter in quarters:
        assert quarter.query_bbox.west >= bbox.west
        assert quarter.query_bbox.south >= bbox.south
    # The clamped side reads no margin at all, but the tile still has one
    # on its inward side, and that is the figure the seam inherits.
    assert min(q.query_bbox.west for q in quarters) == bbox.west
    overlap_lon = corner.query_bbox.east - corner.core_bbox.east
    mid_lon = (corner.core_bbox.west + corner.core_bbox.east) / 2.0
    by_id = {q.tile_id[-3:]: q for q in quarters}
    assert by_id["q00"].query_bbox.east == pytest.approx(mid_lon + overlap_lon)


def test_splitting_a_quarter_again_keeps_both_properties():
    # Depth 2 is a split of a split, so the arithmetic has to hold for a
    # tile it produced itself, not only for one build_tiles produced.
    parent = _middle_tile()
    quarter = split_tile_into_quarters(parent)[0]
    sixteenths = split_tile_into_quarters(quarter)

    assert len(sixteenths) == 4
    assert sixteenths[0].tile_id == "r01_c01_q00_q00"
    queries = [s.query_bbox for s in sixteenths]
    assert min(q.west for q in queries) == quarter.query_bbox.west
    assert max(q.east for q in queries) == quarter.query_bbox.east
    assert min(q.south for q in queries) == quarter.query_bbox.south
    assert max(q.north for q in queries) == quarter.query_bbox.north


def test_a_tile_with_no_overlap_splits_into_quarters_that_meet_edge_to_edge():
    # overlap_m=0 is a legal tiling (the field's own min is 0), and a Tile
    # built by hand in a test often has query_bbox is core_bbox. Neither
    # has an overlap to inherit, and the quarters must then partition the
    # tile exactly rather than inverting a seam.
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    tile = Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox)
    quarters = split_tile_into_quarters(tile)
    mid_lon = (bbox.west + bbox.east) / 2.0
    assert {q.query_bbox.west for q in quarters} == {bbox.west, mid_lon}
    assert {q.query_bbox.east for q in quarters} == {bbox.east, mid_lon}
    for quarter in quarters:
        assert quarter.query_bbox.west >= bbox.west
        assert quarter.query_bbox.east <= bbox.east


def test_each_quarter_is_about_a_quarter_of_the_parents_area():
    # Half the linear dimension in BOTH axes, which is the point of
    # splitting into four rather than two: a density problem is an area
    # problem, and this quarters the area in one step.
    #
    # rel=1e-3, not exact: extent_metres references each box to its OWN
    # centre latitude, so measuring a northern quarter and its parent uses
    # two slightly different values of cos(lat) and lands about half a
    # metre apart over 2.5 km. That is the measuring function's frame of
    # reference, not a wobble in the split, which is exact in degrees (see
    # the seam and cover tests above, which assert exact equality).
    parent = _middle_tile()
    parent_width, parent_height = extent_metres(parent.core_bbox)
    for quarter in split_tile_into_quarters(parent):
        width, height = extent_metres(quarter.core_bbox)
        assert width == pytest.approx(parent_width / 2.0, rel=1e-3)
        assert height == pytest.approx(parent_height / 2.0, rel=1e-3)
