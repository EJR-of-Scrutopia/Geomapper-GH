"""Tests for mapgen.heights: the DSM-minus-DTM building height fusion.

Every footprint test below uses a hand-built `.osm`, an `Ostn15Grid` whose
every shift is exactly zero (so `to_bng` degenerates to the bare
`tm_forward` projection: no real OSTN15 pack is needed and no network is
touched), and synthetic `BngWindow`s built directly as the dataclasses
they are. Node coordinates are derived from a chosen BNG rectangle via
`tm_inverse` so that, once `fuse_building_heights` projects them back with
`to_bng` (== `tm_forward` under the zero-shift grid), the footprint lands
almost exactly where it was drawn: `tm_forward`/`tm_inverse` round trip to
a fraction of a millimetre (see bng.py's own module docstring), far under
the 1 m sampling grid.

The rasters are filled with one constant value each, so the exact
sub-metre position of a sample point never matters to the computed
height: only whether enough of the footprint's 1 m grid points fall
inside the polygon and inside the raster's own sampleable rectangle.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from array import array
from pathlib import Path

import pytest

from mapgen.bng import _NODE_COUNT, Ostn15Grid, tm_inverse
from mapgen.cog import BngWindow
from mapgen.heights import (
    HeightsError,
    HeightsRecord,
    SOURCE_HEIGHT_ATTRIBUTION,
    _median,
    _p90,
    _point_in_polygon,
    fuse_building_heights,
)

# --------------------------------------------------------------------------
# Shared geometry: one window, one rectangle well inside it.
# --------------------------------------------------------------------------

WINDOW_E0 = 300_000.0
WINDOW_N_TOP = 180_030.0
WINDOW_SIZE = 30
WINDOW_PIXEL = 1.0

# A 10 x 8 m box, comfortably inside the window's sampleable rectangle
# (which insets half a pixel from every edge; see cog.py's sample_bng).
_BOX_BNG = [
    (300_010.0, 180_010.0),
    (300_020.0, 180_010.0),
    (300_020.0, 180_018.0),
    (300_010.0, 180_018.0),
]


def _zero_shift_grid() -> Ostn15Grid:
    """An OSTN15 grid every node of which carries a (0, 0) shift.

    `to_bng` then equals `tm_forward` exactly, and `tm_inverse` is its
    exact inverse, which is what lets a test choose BNG coordinates
    directly and get them back out of the fusion almost unchanged. This is
    not a real OSTN15 grid (the real one never shifts by exactly zero
    anywhere), and it does not need to be: nothing under test cares what
    the shift IS, only that projecting a node succeeds.
    """
    shifts = array("f", [0.0]) * (_NODE_COUNT * 2)
    return Ostn15Grid(shifts)


def _constant_window(value: float, *, size: int = WINDOW_SIZE) -> BngWindow:
    values = array("f", [value]) * (size * size)
    return BngWindow(
        e_origin=WINDOW_E0,
        n_top=WINDOW_N_TOP,
        pixel_size=WINDOW_PIXEL,
        width=size,
        height=size,
        values=values,
    )


# --------------------------------------------------------------------------
# Hand-built OSM XML, in exactly the shape mapgen.merge.merge_osm_xml
# itself writes, so an element this module never touches round-trips
# byte-identically through ElementTree (see heights.py's own docstring).
# --------------------------------------------------------------------------

_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'


def _node(node_id: int, lat: float, lon: float) -> ET.Element:
    element = ET.Element("node")
    element.set("id", str(node_id))
    element.set("lat", f"{lat:.7f}")
    element.set("lon", f"{lon:.7f}")
    return element


def _way(way_id: int, refs, tags) -> ET.Element:
    element = ET.Element("way")
    element.set("id", str(way_id))
    for ref in refs:
        nd = ET.SubElement(element, "nd")
        nd.set("ref", str(ref))
    for key, value in tags:
        tag = ET.SubElement(element, "tag")
        tag.set("k", key)
        tag.set("v", value)
    return element


def _relation(relation_id: int, members, tags) -> ET.Element:
    element = ET.Element("relation")
    element.set("id", str(relation_id))
    for member_type, ref, role in members:
        member = ET.SubElement(element, "member")
        member.set("type", member_type)
        member.set("ref", str(ref))
        member.set("role", role)
    for key, value in tags:
        tag = ET.SubElement(element, "tag")
        tag.set("k", key)
        tag.set("v", value)
    return element


def _dump_osm(elements) -> str:
    lines = [_DECLARATION, '<osm version="0.6" generator="mapgen">']
    for element in elements:
        lines.append("  " + ET.tostring(element, encoding="unicode"))
    lines.append("</osm>")
    return "\n".join(lines) + "\n"


def _nodes_for(corners_bng, start_id: int) -> list[ET.Element]:
    return [
        _node(start_id + index, *tm_inverse(easting, northing))
        for index, (easting, northing) in enumerate(corners_bng)
    ]


def _find_way(root_or_tree, way_id: str) -> ET.Element:
    root = root_or_tree.getroot() if hasattr(root_or_tree, "getroot") else root_or_tree
    for element in root:
        if element.tag == "way" and element.get("id") == way_id:
            return element
    raise AssertionError(f"way {way_id!r} not found in the rewritten file")


def _extract_line(text: str, needle: str) -> str:
    for line in text.split("\n"):
        if needle in line:
            return line
    raise AssertionError(f"no line containing {needle!r}")


def _write_single_building_osm(
    tmp_path: Path,
    corners_bng,
    extra_tags=(),
    way_id: int = 9001,
    filename: str = "single.osm",
) -> Path:
    nodes = _nodes_for(corners_bng, start_id=1)
    refs = list(range(1, len(corners_bng) + 1)) + [1]
    way = _way(way_id, refs, [("building", "yes"), *extra_tags])
    path = tmp_path / filename
    path.write_text(_dump_osm([*nodes, way]), encoding="utf-8")
    return path


def _build_three_building_fixture(tmp_path: Path) -> Path:
    """The brief's own fixture: three buildings and a multipolygon relation.

    Way 1001 (flat roof, 6.0 m DSM-minus-DTM under the constant windows
    below) is well inside the window. Way 1002 already carries height=9
    and must come back untouched. Way 1003 sits 1000 m east of the
    window, so both rasters answer None everywhere under it. Relation
    2001 is a separate multipolygon footprint (way 1004, untagged on its
    own, the way real OSM multipolygons often carry their tags on the
    relation rather than the outer member) and must never be inspected at
    all, whatever it contains.
    """
    way1_nodes = _nodes_for(_BOX_BNG, start_id=1)
    way1 = _way(1001, [1, 2, 3, 4, 1], [("building", "yes")])

    b2_bng = [(300_012.0, 180_020.0), (300_014.0, 180_020.0), (300_013.0, 180_022.0)]
    way2_nodes = _nodes_for(b2_bng, start_id=11)
    way2 = _way(1002, [11, 12, 13, 11], [("building", "yes"), ("height", "9")])

    b3_bng = [
        (301_000.0, 180_010.0), (301_010.0, 180_010.0),
        (301_010.0, 180_018.0), (301_000.0, 180_018.0),
    ]
    way3_nodes = _nodes_for(b3_bng, start_id=21)
    way3 = _way(1003, [21, 22, 23, 24, 21], [("building", "yes")])

    b4_bng = [(300_015.0, 180_024.0), (300_017.0, 180_024.0), (300_016.0, 180_026.0)]
    way4_nodes = _nodes_for(b4_bng, start_id=31)
    way4 = _way(1004, [31, 32, 33, 31], [])
    relation = _relation(
        2001, [("way", 1004, "outer")], [("type", "multipolygon"), ("building", "yes")]
    )

    elements = [
        *way1_nodes, *way2_nodes, *way3_nodes, *way4_nodes,
        way1, way2, way3, way4, relation,
    ]
    path = tmp_path / "site.osm"
    path.write_text(_dump_osm(elements), encoding="utf-8")
    return path


def _fixture_windows():
    return _constant_window(100.0), _constant_window(106.0)


# --------------------------------------------------------------------------
# Percentile and median (the hand-computed cases the brief asks for).
# --------------------------------------------------------------------------


def test_p90_of_one_to_ten_is_nine_point_one():
    # 0.9 of the way from index 8 (value 9) to index 9 (value 10) of the
    # sorted list: 9 * 0.9 + 10 * 0.1 = 9.1, numpy's own "linear" method.
    assert _p90([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == pytest.approx(9.1)


def test_median_of_one_to_ten_is_five_point_five():
    assert _median([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == pytest.approx(5.5)


def test_percentile_of_a_single_value_is_that_value():
    assert _p90([42.0]) == 42.0
    assert _median([42.0]) == 42.0


# --------------------------------------------------------------------------
# Point in polygon.
# --------------------------------------------------------------------------

_SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def test_a_point_inside_a_square_is_inside():
    assert _point_in_polygon(5.0, 5.0, _SQUARE) is True


def test_a_point_outside_a_square_is_outside():
    assert _point_in_polygon(15.0, 5.0, _SQUARE) is False


def test_point_in_polygon_does_not_need_the_ring_explicitly_closed():
    closed = _SQUARE + [_SQUARE[0]]
    assert _point_in_polygon(5.0, 5.0, closed) is True
    assert _point_in_polygon(15.0, 5.0, closed) is False


# --------------------------------------------------------------------------
# The three-building fixture: tag values, kept_existing, no_data, relation
# skip, atomicity, idempotence.
# --------------------------------------------------------------------------


def test_the_flat_roofed_box_gets_the_exact_known_height(tmp_path):
    osm_path = _build_three_building_fixture(tmp_path)
    dtm, dsm = _fixture_windows()
    grid = _zero_shift_grid()

    record = fuse_building_heights(osm_path, dtm, dsm, grid)

    assert record == HeightsRecord(
        buildings=3, written=1, kept_existing=1, no_data=1, relations_skipped=1,
    )
    way1 = _find_way(ET.parse(osm_path), "1001")
    tags = {tag.get("k"): tag.get("v") for tag in way1.findall("tag")}
    assert tags["height"] == "6.0"
    assert tags["source:height"] == SOURCE_HEIGHT_ATTRIBUTION
    assert SOURCE_HEIGHT_ATTRIBUTION == (
        "Welsh Government LiDAR 2020 to 2023 (DSM minus DTM)"
    )


def test_a_way_that_already_has_height_is_kept_byte_identical(tmp_path):
    osm_path = _build_three_building_fixture(tmp_path)
    original_line = _extract_line(osm_path.read_text(encoding="utf-8"), 'id="1002"')
    dtm, dsm = _fixture_windows()

    fuse_building_heights(osm_path, dtm, dsm, _zero_shift_grid())

    rewritten_line = _extract_line(osm_path.read_text(encoding="utf-8"), 'id="1002"')
    assert rewritten_line == original_line
    assert 'k="height" v="9"' in rewritten_line


def test_a_multipolygon_relation_is_never_touched(tmp_path):
    osm_path = _build_three_building_fixture(tmp_path)
    before = _extract_line(osm_path.read_text(encoding="utf-8"), 'id="2001"')
    dtm, dsm = _fixture_windows()

    record = fuse_building_heights(osm_path, dtm, dsm, _zero_shift_grid())

    assert record.relations_skipped == 1
    after = _extract_line(osm_path.read_text(encoding="utf-8"), 'id="2001"')
    assert after == before


def test_a_footprint_off_the_window_is_no_data_and_untagged(tmp_path):
    osm_path = _build_three_building_fixture(tmp_path)
    dtm, dsm = _fixture_windows()

    record = fuse_building_heights(osm_path, dtm, dsm, _zero_shift_grid())

    assert record.no_data == 1
    way3 = _find_way(ET.parse(osm_path), "1003")
    assert "height" not in {tag.get("k") for tag in way3.findall("tag")}


def test_no_temp_file_is_left_behind_after_a_run_that_writes(tmp_path):
    osm_path = _build_three_building_fixture(tmp_path)
    dtm, dsm = _fixture_windows()

    fuse_building_heights(osm_path, dtm, dsm, _zero_shift_grid())

    assert list(tmp_path.glob("*.part")) == []
    assert osm_path.is_file()


def test_a_second_run_is_idempotent(tmp_path):
    osm_path = _build_three_building_fixture(tmp_path)
    dtm, dsm = _fixture_windows()
    grid = _zero_shift_grid()

    first = fuse_building_heights(osm_path, dtm, dsm, grid)
    assert first.written == 1

    second = fuse_building_heights(osm_path, dtm, dsm, grid)

    assert second.written == 0
    assert second.kept_existing == 2  # way 1001 (now tagged) and way 1002.
    assert second.no_data == 1
    assert second.buildings == 3
    assert second.relations_skipped == 1


def test_a_run_that_changes_nothing_never_calls_the_writer(tmp_path, monkeypatch):
    """Every way already resolved (kept_existing or no_data): the file is
    never handed to the writer at all, not merely rewritten unchanged.
    """
    osm_path = _build_three_building_fixture(tmp_path)
    dtm, dsm = _fixture_windows()
    grid = _zero_shift_grid()
    fuse_building_heights(osm_path, dtm, dsm, grid)  # First run: writes.

    def _boom(*args, **kwargs):
        raise AssertionError("atomic_write_bytes should not have been called")

    monkeypatch.setattr("mapgen.heights.atomic_write_bytes", _boom)
    second = fuse_building_heights(osm_path, dtm, dsm, grid)  # Second: idempotent.

    assert second.written == 0


# --------------------------------------------------------------------------
# The individual fusion rules, each isolated from the three-building
# fixture so a failure here points at exactly one rule.
# --------------------------------------------------------------------------


def test_a_height_under_two_metres_is_no_data_not_a_tiny_building(tmp_path):
    osm_path = _write_single_building_osm(tmp_path, _BOX_BNG, way_id=9001)
    dtm = _constant_window(100.0)
    dsm = _constant_window(101.5)  # 1.5 m: real, but under the floor.

    record = fuse_building_heights(osm_path, dtm, dsm, _zero_shift_grid())

    assert record == HeightsRecord(
        buildings=1, written=0, kept_existing=0, no_data=1, relations_skipped=0,
    )


def test_a_way_with_an_unresolvable_node_ref_is_no_data(tmp_path):
    # Nodes 1..3 exist; the way's own ring also references node 4, which
    # this file never defines.
    nodes = _nodes_for(_BOX_BNG[:3], start_id=1)
    way = _way(9002, [1, 2, 3, 4, 1], [("building", "yes")])
    path = tmp_path / "malformed.osm"
    path.write_text(_dump_osm([*nodes, way]), encoding="utf-8")

    record = fuse_building_heights(
        path, _constant_window(100.0), _constant_window(106.0), _zero_shift_grid()
    )

    assert record == HeightsRecord(
        buildings=1, written=0, kept_existing=0, no_data=1, relations_skipped=0,
    )


def test_a_way_with_fewer_than_three_nodes_is_no_data(tmp_path):
    nodes = _nodes_for(_BOX_BNG[:2], start_id=1)
    way = _way(9003, [1, 2], [("building", "yes")])
    path = tmp_path / "two_nodes.osm"
    path.write_text(_dump_osm([*nodes, way]), encoding="utf-8")

    record = fuse_building_heights(
        path, _constant_window(100.0), _constant_window(106.0), _zero_shift_grid()
    )

    assert record.no_data == 1
    assert record.written == 0


def test_a_footprint_too_small_for_three_sample_points_is_no_data(tmp_path):
    tiny = [
        (300_010.0, 180_010.0), (300_010.6, 180_010.0), (300_010.3, 180_010.5),
    ]
    osm_path = _write_single_building_osm(tmp_path, tiny, way_id=9004, filename="tiny.osm")

    record = fuse_building_heights(
        osm_path, _constant_window(100.0), _constant_window(106.0), _zero_shift_grid()
    )

    assert record.no_data == 1
    assert record.written == 0


def test_a_way_that_is_not_tagged_building_is_ignored_entirely(tmp_path):
    nodes = _nodes_for(_BOX_BNG, start_id=1)
    way = _way(9005, [1, 2, 3, 4, 1], [("landuse", "residential")])
    path = tmp_path / "not_a_building.osm"
    path.write_text(_dump_osm([*nodes, way]), encoding="utf-8")

    record = fuse_building_heights(
        path, _constant_window(100.0), _constant_window(106.0), _zero_shift_grid()
    )

    assert record == HeightsRecord(
        buildings=0, written=0, kept_existing=0, no_data=0, relations_skipped=0,
    )
    assert "height" not in {
        tag.get("k") for tag in _find_way(ET.parse(path), "9005").findall("tag")
    }


# --------------------------------------------------------------------------
# Failure shape: a file that is not the shape mapgen's own writer produces.
# --------------------------------------------------------------------------


def test_a_file_without_an_xml_declaration_raises_heights_error(tmp_path):
    path = tmp_path / "no_declaration.osm"
    path.write_text('<osm version="0.6" generator="mapgen"></osm>\n', encoding="utf-8")

    with pytest.raises(HeightsError):
        fuse_building_heights(
            path, _constant_window(100.0), _constant_window(106.0), _zero_shift_grid()
        )


def test_unparseable_xml_raises_heights_error(tmp_path):
    path = tmp_path / "truncated.osm"
    path.write_text(f"{_DECLARATION}\n<osm><way>\n", encoding="utf-8")

    with pytest.raises(HeightsError):
        fuse_building_heights(
            path, _constant_window(100.0), _constant_window(106.0), _zero_shift_grid()
        )


# --------------------------------------------------------------------------
# The one test that goes through real, written-and-read-back GeoTIFFs
# rather than hand-built BngWindow objects, to prove the file path works.
# --------------------------------------------------------------------------


def test_fusion_works_through_real_written_and_read_back_tifs(tmp_path):
    from mapgen.cog import CogReader, FileByteSource, read_full_window
    from mapgen.geotiff_write import write_bng_geotiff

    dtm_path = tmp_path / "dtm.tif"
    dsm_path = tmp_path / "dsm.tif"
    write_bng_geotiff(dtm_path, _constant_window(100.0))
    write_bng_geotiff(dsm_path, _constant_window(106.0))

    dtm = read_full_window(CogReader.open(FileByteSource(dtm_path)))
    dsm = read_full_window(CogReader.open(FileByteSource(dsm_path)))

    osm_path = _write_single_building_osm(tmp_path, _BOX_BNG, way_id=9006)

    record = fuse_building_heights(osm_path, dtm, dsm, _zero_shift_grid())

    assert record.written == 1
    way = _find_way(ET.parse(osm_path), "9006")
    tags = {tag.get("k"): tag.get("v") for tag in way.findall("tag")}
    assert tags["height"] == "6.0"
    assert tags["source:height"] == SOURCE_HEIGHT_ATTRIBUTION
