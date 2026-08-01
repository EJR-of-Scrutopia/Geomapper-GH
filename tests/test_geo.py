import pytest

from mapgen.geo import BBox, BBoxError, build_tiles, extent_metres


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
