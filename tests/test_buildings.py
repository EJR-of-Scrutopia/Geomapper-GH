from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mapgen.bng import tm_inverse
from mapgen.buildings import (
    CELL_SIZE_DEGREES,
    BuildingsFusionRecord,
    _Footprint,
    _ring_centroid,
    _SpatialIndex,
    _point_in_ring,
    fuse_missing_buildings,
)
from mapgen.heights import HeightsError, fuse_building_heights
from tests.test_heights import (
    _BOX_BNG,
    _constant_window,
    _write_single_building_osm,
    _zero_shift_grid,
)

# --------------------------------------------------------------------------
# Shared fixture geometry: one OSM base with three existing buildings
# (positive and negative ids mixed, to exercise `_minimum_existing_id`),
# and small square candidate footprints well away from each other and
# from the three existing buildings unless a test deliberately wants an
# overlap.
# --------------------------------------------------------------------------

_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'
BASE_LON = -3.280
BASE_LAT = 51.400
_SQUARE_SIZE = 0.0002


def _square(lon0: float, lat0: float, size: float = _SQUARE_SIZE) -> list[tuple[float, float]]:
    return [
        (lon0, lat0),
        (lon0 + size, lat0),
        (lon0 + size, lat0 + size),
        (lon0, lat0 + size),
    ]


def _node_el(node_id: int, lon: float, lat: float) -> ET.Element:
    element = ET.Element("node")
    element.set("id", str(node_id))
    element.set("lat", f"{lat:.7f}")
    element.set("lon", f"{lon:.7f}")
    return element


def _way_el(way_id: int, refs, tags) -> ET.Element:
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


def _dump_osm(elements) -> str:
    lines = [_DECLARATION, '<osm version="0.6" generator="mapgen">']
    for element in elements:
        lines.append("  " + ET.tostring(element, encoding="unicode"))
    lines.append("</osm>")
    return "\n".join(lines) + "\n"


def _write_three_building_osm(tmp_path: Path) -> tuple[Path, list[tuple[float, float]]]:
    """The brief's own fixture: three existing OSM building ways, ids
    mixed positive and negative, so a regression that starts the injected
    id counter at a bare -1 (rather than `_minimum_existing_id(root) - 1`)
    reproduces as a real, checkable id collision rather than a
    hypothetical one, matching `test_package.py`'s own boundaries-fusion
    fixture for the identical reason.

    Returns the file path and building 1's own ring (in file order,
    unclosed), so a test can build an exact duplicate candidate from it
    without hand-copying coordinates a second time.
    """
    b1 = _square(BASE_LON, BASE_LAT)
    b1_nodes = [_node_el(index + 1, lon, lat) for index, (lon, lat) in enumerate(b1)]
    b1_way = _way_el(501, [1, 2, 3, 4, 1], [("building", "yes")])

    b2 = _square(BASE_LON + 0.05, BASE_LAT + 0.05)
    b2_ids = [-6, -7, -8, -9]
    b2_nodes = [_node_el(node_id, lon, lat) for node_id, (lon, lat) in zip(b2_ids, b2)]
    b2_way = _way_el(-5, [*b2_ids, b2_ids[0]], [("building", "house")])

    b3 = _square(BASE_LON + 0.06, BASE_LAT + 0.06)
    b3_ids = [100, 101, 102, 103]
    b3_nodes = [_node_el(node_id, lon, lat) for node_id, (lon, lat) in zip(b3_ids, b3)]
    b3_way = _way_el(300, [*b3_ids, b3_ids[0]], [("building", "yes")])

    elements = [*b1_nodes, *b2_nodes, *b3_nodes, b1_way, b2_way, b3_way]
    path = tmp_path / "site.osm"
    path.write_text(_dump_osm(elements), encoding="utf-8")
    return path, b1


def _polygon_feature(ring, properties=None) -> dict:
    closed = [*ring, ring[0]]
    return {
        "type": "Feature",
        "properties": properties or {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[lon, lat] for lon, lat in closed]],
        },
    }


def _multipolygon_feature(rings, properties=None) -> dict:
    polygons = []
    for ring in rings:
        closed = [*ring, ring[0]]
        polygons.append([[[lon, lat] for lon, lat in closed]])
    return {
        "type": "Feature",
        "properties": properties or {},
        "geometry": {"type": "MultiPolygon", "coordinates": polygons},
    }


def _all_ids(root: ET.Element) -> list[int]:
    return [
        int(element.get("id"))
        for element in root
        if element.tag in ("node", "way", "relation")
    ]


def _ways_tagged(root: ET.Element, key: str, value: str) -> list[ET.Element]:
    return [
        element
        for element in root
        if element.tag == "way"
        and any(tag.get("k") == key and tag.get("v") == value for tag in element.findall("tag"))
    ]


# --------------------------------------------------------------------------
# Step 2: geometry core.
# --------------------------------------------------------------------------

_SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def test_a_point_inside_a_square_is_inside():
    assert _point_in_ring(5.0, 5.0, _SQUARE) is True


def test_a_point_outside_a_square_is_outside():
    assert _point_in_ring(15.0, 5.0, _SQUARE) is False


def test_a_point_on_a_vertex_follows_the_strict_inequality_convention():
    """Matching `contours.py`'s own crossing rule ("a corner's state is
    always `value > level`, never `>=`") and `heights._point_in_polygon`'s
    identical choice: every comparison here is a strict `>`, so a vertex
    exactly on the ray belongs to at most one of its two edges. Hand
    checked rather than merely asserted, both corners of the same square:
    (0, 0) reads inside, (10, 0) reads outside, which is the concrete,
    checkable consequence of that one convention rather than an
    accident of this particular square.
    """
    assert _point_in_ring(0.0, 0.0, _SQUARE) is True
    assert _point_in_ring(10.0, 0.0, _SQUARE) is False


def test_point_in_ring_does_not_need_the_ring_explicitly_closed():
    closed = [*_SQUARE, _SQUARE[0]]
    assert _point_in_ring(5.0, 5.0, closed) is True
    assert _point_in_ring(15.0, 5.0, closed) is False


def test_centroid_of_a_square_is_its_middle():
    assert _ring_centroid(_SQUARE) == pytest.approx((5.0, 5.0))


def test_centroid_drops_the_last_equals_first_vertex_before_averaging():
    closed = [*_SQUARE, _SQUARE[0]]
    # Averaging all 5 points (the duplicate corner counted twice) would
    # pull the result away from (5, 5); the brief's own rule ("last-equals-
    # first vertex dropped") is what keeps it exact.
    assert _ring_centroid(closed) == pytest.approx((5.0, 5.0))


def test_spatial_hash_finds_a_footprint_across_a_cell_border():
    """A footprint whose bounding box spans two grid cells must be found
    by a query landing in EITHER cell, not only the one its own centroid
    happens to sit in: `_cells_for_bbox` registers a footprint under every
    cell its box reaches, which is what this test exercises directly.
    """
    # A footprint straddling the boundary between cell column 0 and 1.
    half = CELL_SIZE_DEGREES / 2
    ring = [
        (half, 0.0),
        (half + CELL_SIZE_DEGREES, 0.0),
        (half + CELL_SIZE_DEGREES, half),
        (half, half),
    ]
    index = _SpatialIndex()
    index.add(_Footprint(ring=ring))

    # A point inside the footprint but sitting in the FAR cell from its
    # own bounding box's low corner.
    far_point = (half + CELL_SIZE_DEGREES * 0.75, half * 0.5)
    assert index.candidate_centroid_is_covered(far_point) is True


def test_spatial_hash_does_not_find_a_footprint_in_an_unrelated_cell():
    ring = [(0.0, 0.0), (0.0001, 0.0), (0.0001, 0.0001), (0.0, 0.0001)]
    index = _SpatialIndex()
    index.add(_Footprint(ring=ring))
    far_away = (CELL_SIZE_DEGREES * 10, CELL_SIZE_DEGREES * 10)
    assert index.candidate_centroid_is_covered(far_away) is False


# --------------------------------------------------------------------------
# Step 3: fusion, over the three-building fixture.
# --------------------------------------------------------------------------


def test_fusion_injects_new_footprints_and_skips_duplicates_by_geometry(tmp_path):
    osm_path, b1_ring = _write_three_building_osm(tmp_path)

    overture_new_ring = _square(BASE_LON + 0.02, BASE_LAT + 0.02)
    multipolygon_ring_a = _square(BASE_LON + 0.03, BASE_LAT + 0.03)
    multipolygon_ring_b = _square(BASE_LON + 0.04, BASE_LAT + 0.04)
    os_new_ring = _square(BASE_LON + 0.07, BASE_LAT + 0.07)

    overture_features = [
        # A re-trace of the existing OSM building: skipped by rule (a),
        # exactly like an Overture feature whose own `sources` name only
        # OpenStreetMap (see the module docstring's "no special-casing by
        # dataset" section: nothing here even reads `sources`).
        _polygon_feature(b1_ring, {"sources": [{"dataset": "OpenStreetMap"}]}),
        # Genuinely new, carrying a height that needs rounding to one
        # decimal (7.42 -> "7.4", not a value already shaped that way).
        _polygon_feature(overture_new_ring, {"height": 7.42}),
        # Two exteriors, each its own way, at two more new locations.
        _multipolygon_feature([multipolygon_ring_a, multipolygon_ring_b]),
    ]
    os_features = [
        # A duplicate of the OVERTURE candidate just accepted, not of
        # anything originally in the OSM base: this is the
        # candidate-versus-candidate half of the dedup rule.
        _polygon_feature(overture_new_ring, {"source": "os_openmap_local"}),
        # Genuinely new, carrying OS's own `class` property, which must
        # land as its own `class=` tag and never override `building=yes`.
        _polygon_feature(os_new_ring, {"source": "os_openmap_local", "class": "Agricultural"}),
    ]

    record = fuse_missing_buildings(
        osm_path, [("overture", overture_features), ("os_openmap_local", os_features)]
    )

    assert record == BuildingsFusionRecord(
        written=4,
        per_source={"overture": 3, "os_openmap_local": 1},
        skipped_overlap=2,
        kept_existing=3,
    )

    root = ET.fromstring(osm_path.read_text(encoding="utf-8"))
    all_ids = _all_ids(root)
    assert len(all_ids) == len(set(all_ids)), "every id, old and new, must be unique"
    # The fixture's own minimum id is -9 (building 2's nodes); every
    # injected id must sit strictly below it, not merely below -1.
    injected_ways = [*_ways_tagged(root, "source", "overture"), *_ways_tagged(root, "source", "os_openmap_local")]
    assert len(injected_ways) == 4
    injected_ids: set[int] = set()
    for way in injected_ways:
        injected_ids.add(int(way.get("id")))
        for nd in way.findall("nd"):
            injected_ids.add(int(nd.get("ref")))
    assert max(injected_ids) < -9

    # Four new squares, four vertices each, no cross-way node sharing
    # (module docstring): 16 new node elements, not fewer.
    existing_node_ids = {1, 2, 3, 4, -6, -7, -8, -9, 100, 101, 102, 103}
    new_nodes = [
        element
        for element in root
        if element.tag == "node" and int(element.get("id")) not in existing_node_ids
    ]
    assert len(new_nodes) == 16

    overture_written = _ways_tagged(root, "source", "overture")
    height_tags = {
        way.get("id"): tag.get("v")
        for way in overture_written
        for tag in way.findall("tag")
        if tag.get("k") == "height"
    }
    assert list(height_tags.values()) == ["7.4"], "one Overture way carries a height, one decimal"

    os_written = _ways_tagged(root, "source", "os_openmap_local")
    assert len(os_written) == 1
    class_tags = [
        tag.get("v")
        for tag in os_written[0].findall("tag")
        if tag.get("k") == "class"
    ]
    assert class_tags == ["Agricultural"]
    building_tags = [
        tag.get("v")
        for tag in os_written[0].findall("tag")
        if tag.get("k") == "building"
    ]
    assert building_tags == ["yes"], "an OS class must never override building=yes"

    # Existing buildings are read, never rewritten: their own tags survive
    # byte for byte.
    existing_way_501 = next(w for w in root if w.tag == "way" and w.get("id") == "501")
    assert {t.get("k"): t.get("v") for t in existing_way_501.findall("tag")} == {"building": "yes"}


def test_fusion_writes_nothing_when_every_candidate_is_a_duplicate(tmp_path):
    osm_path, b1_ring = _write_three_building_osm(tmp_path)
    before = osm_path.read_bytes()

    record = fuse_missing_buildings(
        osm_path,
        [("overture", [_polygon_feature(b1_ring)])],
    )

    assert record == BuildingsFusionRecord(
        written=0, per_source={"overture": 0}, skipped_overlap=1, kept_existing=3
    )
    assert osm_path.read_bytes() == before, "nothing written means the file is untouched"


def test_fusion_is_idempotent_on_a_second_run_over_the_same_candidates(tmp_path):
    """The idempotency this module relies on (see its own docstring): a
    footprint this function injected on the first run is, by the second,
    just another existing OSM building way, so offering the identical
    candidate again fails rule (a) against that new "existing" footprint
    and is skipped rather than duplicated.
    """
    osm_path, _ = _write_three_building_osm(tmp_path)
    new_ring = _square(BASE_LON + 0.02, BASE_LAT + 0.02)
    candidates = [("overture", [_polygon_feature(new_ring, {"height": 5.0})])]

    first = fuse_missing_buildings(osm_path, candidates)
    assert first.written == 1
    assert first.kept_existing == 3

    second = fuse_missing_buildings(osm_path, candidates)
    assert second.written == 0
    assert second.per_source == {"overture": 0}
    assert second.skipped_overlap == 1
    # The one way the first run injected is now counted as an existing
    # building, on top of the fixture's original three.
    assert second.kept_existing == 4


def test_candidates_with_invalid_or_empty_geometry_are_skipped_and_counted(tmp_path):
    osm_path, _ = _write_three_building_osm(tmp_path)
    not_a_polygon = {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0.0, 0.0]}}
    too_few_points = {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[[0.0, 0.0], [0.0, 0.0]]]},
    }

    record = fuse_missing_buildings(
        osm_path, [("overture", [not_a_polygon, too_few_points])]
    )

    assert record.written == 0
    assert record.skipped_overlap == 2
    assert record.kept_existing == 3


def test_a_package_with_no_candidates_at_all_writes_nothing(tmp_path):
    osm_path, _ = _write_three_building_osm(tmp_path)
    before = osm_path.read_bytes()

    record = fuse_missing_buildings(osm_path, [])

    assert record == BuildingsFusionRecord(
        written=0, per_source={}, skipped_overlap=0, kept_existing=3
    )
    assert osm_path.read_bytes() == before


def test_an_osm_with_no_xml_declaration_is_refused(tmp_path):
    osm_path = tmp_path / "bad.osm"
    osm_path.write_text('<osm version="0.6"></osm>', encoding="utf-8")

    with pytest.raises(HeightsError):
        fuse_missing_buildings(osm_path, [])


# --------------------------------------------------------------------------
# Step 5's own "cheap seam" check: buildings fusion runs before heights
# fusion so an injected footprint with no height of its own gets one from
# a subsequent, real heights-fusion pass over the same `.osm`.
# --------------------------------------------------------------------------

# A 6 x 6 m box inside the same fake DTM/DSM window tests/test_heights.py's
# own `_constant_window`/`_zero_shift_grid` cover, clearly separated from
# `_BOX_BNG` (E 300010-300020, N 180010-180018) rather than merely
# adjacent to it, so the two footprints share no node and cannot be
# mistaken for one shape by any test assertion below.
_SEAM_BOX_BNG = [
    (300_003.0, 180_020.0),
    (300_009.0, 180_020.0),
    (300_009.0, 180_026.0),
    (300_003.0, 180_026.0),
]


def test_an_injected_footprint_with_no_height_gains_one_from_heights_fusion(tmp_path):
    """The brief's own step 5 "cheap seam" check, done for real rather
    than only by shape: `fuse_missing_buildings` and
    `heights.fuse_building_heights` are both plain functions over one
    `.osm` path, so proving the seam between them costs only calling both
    in sequence, reusing tests/test_heights.py's own fake DTM/DSM windows
    and zero-shift OSTN15 grid rather than a live raster or network call.

    `_write_single_building_osm`'s own building (`_BOX_BNG`, way id 501)
    is the fixture tests/test_heights.py already proves gets an exact
    6.0 m height under these same two constant windows; this test's own
    candidate is a second, disjoint box inside the identical window, so
    the SAME known-good height arithmetic applies to it once buildings
    fusion has injected it.
    """
    osm_path = _write_single_building_osm(tmp_path, _BOX_BNG, way_id=501, filename="site.osm")
    seam_ring = [tuple(reversed(tm_inverse(easting, northing))) for easting, northing in _SEAM_BOX_BNG]

    fuse_missing_buildings(osm_path, [("overture", [_polygon_feature(seam_ring)])])
    root = ET.fromstring(osm_path.read_text(encoding="utf-8"))
    injected = _ways_tagged(root, "source", "overture")
    assert len(injected) == 1
    assert not any(tag.get("k") == "height" for tag in injected[0].findall("tag")), (
        "no height property on the candidate means no height tag yet, before "
        "heights fusion has had its own turn"
    )

    # Both the fixture's own pre-existing building (way 501) and the
    # freshly injected one lack a height tag at this point, so a real
    # heights-fusion pass writes both: `written == 2`, not 1, is what
    # proves the injected way was already visible to it, not merely that
    # heights fusion still works on its own long-standing fixture.
    dtm, dsm = _constant_window(100.0), _constant_window(106.0)
    heights_record = fuse_building_heights(osm_path, dtm, dsm, _zero_shift_grid())
    assert heights_record.written == 2
    assert heights_record.buildings == 2

    root = ET.fromstring(osm_path.read_text(encoding="utf-8"))
    injected = _ways_tagged(root, "source", "overture")
    height_tags = [tag.get("v") for tag in injected[0].findall("tag") if tag.get("k") == "height"]
    assert height_tags == ["6.0"], "the identical known-good height tests/test_heights.py proves"
