"""Tests for mapgen.benchmark: `mapgen benchmark`, and the derived-data
firewall it runs behind.

Every test below stubs the NGD client out entirely (`_StubNgdClient`,
handed to `run_benchmark` via its own `client_factory` seam): no test
here, except none at all, ever touches the real OS NGD API. The synthetic
package fixture (`_build_package`) is a hand-built `survey.json` +
`<stem>.osm` (+ optionally `<stem>_os_roads.geojson`), the same
`_zero_shift_grid`/`tm_inverse` round-trip convention `test_heights.py`
already established for choosing BNG coordinates directly and getting
them back out unchanged: `mapgen.benchmark.load_ostn15` is monkeypatched
to that zero-shift grid so no real OSTN15 cache or network is needed
either.

The firewall tests (this module's own reason to exist, per the brief) use
a `_StubNgdClient` that returns synthetic "premium" features carrying
sentinel coordinates and a sentinel feature id, chosen to be numbers and
strings this suite can grep for in the written report afterwards: if
either ever appears in `report.json` or `report.md`, the firewall has a
hole.
"""

from __future__ import annotations

import json
import socket
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

import pytest

from mapgen import benchmark as benchmark_module
from mapgen import cli as cli_module
from mapgen.benchmark import BenchmarkError, _ngd_building_rings, _read_buildings, run_benchmark
from mapgen.bng import tm_inverse
from mapgen.cli import main
from mapgen.geo import BBox
from mapgen.ngd import BUILDING_COLLECTION, ROAD_COLLECTION, NgdError
from tests.test_heights import _dump_osm, _nodes_for, _relation, _way, _zero_shift_grid

_TEST_KEY = "sekrit-dev-mode-key-do-not-leak"
_SENTINEL_ID = "SENTINEL-PREMIUM-FEATURE-ID-771"
_SENTINEL_COORD = 918273.456

# The survey bbox, and therefore (via `padded_bng_extent`) the exact
# rectangle `run_benchmark` pulls NGD over and now CLIPS our own building
# population to. Chosen by projecting two BNG corners back out through
# `tm_inverse` (the same zero-shift round trip `_nodes_for` uses) and
# rounding to the 7 decimal places `BBox.to_dict` writes into
# survey.json, so the rectangle this fixture's own coordinates are
# checked against is a known BNG box rather than an accident of where
# -3.45/51.45 happens to land:
#
#     easting  299_800 to 310_000   (_BASE_E - 200 to _BASE_E + 10_000)
#     northing 179_800 to 190_000   (_BASE_N - 200 to _BASE_N + 10_000)
#
# Every building and road this module places sits inside it (the furthest
# is the tiny outbuilding at +8_012 m), so the clipping excludes nothing
# by accident; `_OUT_OF_EXTENT_CORNERS` below is the one fixture written
# to fall outside it deliberately.
_BBOX = BBox(west=-3.4438096, south=51.5075354, east=-3.2994883, north=51.6009432)

# The two-orientation road shift (+0.9, +0.9), the exact construction
# test_benchstats.py's own LSQ-recovery test uses: `theirs` runs well past
# `ours` on each axis so every projection lands interior, never clamped.
_SHIFT_E, _SHIFT_N = 0.9, 0.9

# Every "ours" fixture coordinate below is offset by this base before it
# is turned into lat/lon (`_shift`, `tm_inverse`): a coordinate near the
# true BNG origin round-trips back out of `to_bng` with a northing a
# hair's width below exactly 0.0 (ordinary float noise in the
# tm_forward/tm_inverse pair, harmless everywhere except here), and a
# northing that reads as -0.0000044 floors to grid ROW -1, one cell
# outside the OSTN15 rectangle's own [0, 1251) range: `OutsideOstn15Error`
# even under the all-zero fixture grid. Real packages never sit at the
# false origin (it is out at sea, southwest of the Scillies), so this is
# a fixture-construction hazard, not a `mapgen.bng` defect; a comfortable
# offset well clear of the grid's own edges is test_heights.py's own
# WINDOW_N_TOP = 180_030.0 convention, reused here.
_BASE_E, _BASE_N = 300_000.0, 180_000.0


def _shift(points_bng, de: float = _BASE_E, dn: float = _BASE_N):
    return [(e + de, n + dn) for e, n in points_bng]


# --------------------------------------------------------------------------
# Building/road way helpers, following test_heights.py's own convention
# (_node/_way/_nodes_for/_dump_osm, imported rather than duplicated).
# --------------------------------------------------------------------------


def _closed_way(way_id: int, corners_bng, start_node_id: int, tags):
    nodes = _nodes_for(corners_bng, start_id=start_node_id)
    refs = list(range(start_node_id, start_node_id + len(corners_bng))) + [start_node_id]
    return nodes, _way(way_id, refs, tags)


def _open_way(way_id: int, points_bng, start_node_id: int, tags):
    nodes = _nodes_for(points_bng, start_id=start_node_id)
    refs = list(range(start_node_id, start_node_id + len(points_bng)))
    return nodes, _way(way_id, refs, tags)


def _os_roads_geojson_text(polylines_bng) -> str:
    """`<stem>_os_roads.geojson`'s own shape (OsOpenSource.merge): WGS84
    `[lon, lat]` coordinates, via the same zero-shift `tm_inverse` round
    trip `_nodes_for` uses for the `.osm` fixture, so a BNG polyline
    chosen by hand comes back out of `mapgen.benchmark` unchanged.
    """
    features = []
    for polyline in polylines_bng:
        coordinates = []
        for easting, northing in polyline:
            lat, lon = tm_inverse(easting, northing)
            coordinates.append([lon, lat])
        features.append(
            {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": coordinates}}
        )
    return json.dumps({"type": "FeatureCollection", "features": features})


def _build_package(
    tmp_path: Path,
    *,
    stem: str = "Test-Site_2026-08-09",
    with_os_roads: bool = True,
    extra_buildings: Sequence[tuple[Sequence[tuple[float, float]], list[tuple[str, str]]]] = (),
    extra_roads: Sequence[tuple[Sequence[tuple[float, float]], list[tuple[str, str]]]] = (),
) -> Path:
    """A package directory holding `survey.json`, `<stem>.osm`, and
    (unless `with_os_roads` is False) `<stem>_os_roads.geojson`.

    Building A: OSM-native (no `source` tag), `building=house`, a 10x10 m
    square at the BNG origin. Building B: an Overture injection
    (`source=overture`), `building=yes`, a 10x10 m square far away. A
    relation carrying its own `building=yes` tag, over a separate member
    way: must never be counted, since only `way` elements are read as
    buildings (a relation's own tag is invisible to this module by
    construction, the same as `heights.py`'s own `relations_skipped`).

    Roads: a horizontal and a vertical OSM `highway=residential` way (two
    orientations, matching test_benchstats.py's own LSQ-recovery
    construction so a real, non-None least-squares offset comes out the
    other end of a full `run_benchmark` call, not only out of
    `benchstats.polyline_offsets` in isolation).

    `extra_buildings` appends further `(corners, tags)` building ways,
    each corner sequence in the same UNSHIFTED BNG convention as the two
    above (`_shift` is applied here). Empty by default, so every test
    written against the two-building fixture keeps its own counts
    unchanged; the containment tests use it to place a footprint of ours
    precisely enough to pin a classification, and the extent test uses it
    to place one deliberately outside the pulled rectangle.

    `extra_roads` does the same for open `highway=*` ways, which is what
    the carriageway/path split test needs: the two roads above are both
    `highway=residential` (carriageways), so a test that wants a path
    population adds its own.
    """
    root = tmp_path / "package"
    root.mkdir()
    (root / "survey.json").write_text(
        json.dumps({"urbano_stem": stem, "bbox": _BBOX.to_dict()}), encoding="utf-8"
    )

    a_nodes, a_way = _closed_way(
        9001,
        _shift([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]),
        1,
        [("building", "house")],
    )
    b_nodes, b_way = _closed_way(
        9002,
        _shift([(5_000.0, 5_000.0), (5_010.0, 5_000.0), (5_010.0, 5_010.0), (5_000.0, 5_010.0)]),
        11,
        [("building", "yes"), ("source", "overture")],
    )
    rel_nodes, rel_way = _closed_way(
        9003, _shift([(7_000.0, 7_000.0), (7_010.0, 7_000.0), (7_005.0, 7_010.0)]), 21, []
    )
    relation = _relation(
        5001, [("way", 9003, "outer")], [("type", "multipolygon"), ("building", "yes")]
    )

    h_nodes, h_way = _open_way(
        9101, _shift([(0.0, 0.0), (100.0, 0.0)]), 31, [("highway", "residential")]
    )
    v_nodes, v_way = _open_way(
        9102, _shift([(500.0, 0.0), (500.0, 100.0)]), 41, [("highway", "residential")]
    )

    elements = [
        *a_nodes, a_way,
        *b_nodes, b_way,
        *rel_nodes, rel_way, relation,
        *h_nodes, h_way,
        *v_nodes, v_way,
    ]
    for offset, (corners, tags) in enumerate(extra_buildings):
        extra_nodes, extra_way = _closed_way(
            9301 + offset, _shift(corners), 101 + offset * 20, tags
        )
        elements.extend([*extra_nodes, extra_way])
    for offset, (points, tags) in enumerate(extra_roads):
        road_nodes, road_way = _open_way(
            9401 + offset, _shift(points), 501 + offset * 20, tags
        )
        elements.extend([*road_nodes, road_way])
    (root / f"{stem}.osm").write_text(_dump_osm(elements), encoding="utf-8")

    if with_os_roads:
        # A third, BNG-native road population, offset (0.3, 0.0) from the
        # horizontal OSM way above: distinct from the OSM offset so a test
        # can tell the two populations apart in the report.
        polyline = _shift([(0.3, 5.0), (100.3, 5.0)])
        (root / f"{stem}_os_roads.geojson").write_text(
            _os_roads_geojson_text([polyline]), encoding="utf-8"
        )

    return root


# --------------------------------------------------------------------------
# The stub NGD client: run_benchmark's own client_factory seam.
# --------------------------------------------------------------------------


class _StubNgdClient:
    def __init__(
        self,
        key=None,
        *,
        buildings=None,
        roads=None,
        verify_error: Exception | None = None,
        building_error: Exception | None = None,
        road_error: Exception | None = None,
    ) -> None:
        self.key = key
        self._buildings = list(buildings) if buildings is not None else []
        self._roads = list(roads) if roads is not None else []
        self._verify_error = verify_error
        self._building_error = building_error
        self._road_error = road_error
        self.verify_calls: list[list[str]] = []
        self.items_calls: list[tuple[str, tuple, int]] = []

    def verify_collections(self, ids):
        self.verify_calls.append(list(ids))
        if self._verify_error is not None:
            raise self._verify_error

    def items(self, collection_id, bbox_bng, max_pages=500):
        self.items_calls.append((collection_id, bbox_bng, max_pages))
        if collection_id == BUILDING_COLLECTION:
            if self._building_error is not None:
                raise self._building_error
            return list(self._buildings), 1
        if self._road_error is not None:
            raise self._road_error
        return list(self._roads), 1


def _matched_ngd_building_feature() -> dict:
    # NGD's own coordinates are already BNG (see ngd.py's own docstring:
    # `crs`/`bbox-crs` are both requested as EPSG 27700), never round
    # tripped through to_bng, so this needs no _shift-vs-edge-case
    # treatment of its own; it is offset by the same _BASE_E/_BASE_N as
    # building A purely to actually sit on top of it and score a real
    # match, not to dodge the OSTN15 fixture hazard `_shift`'s own
    # docstring explains.
    e0, n0 = _BASE_E, _BASE_N
    return {
        "type": "Feature",
        "id": "ordinary-matched-building",
        "properties": {"description": "Detached"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[e0, n0], [e0 + 10.0, n0], [e0 + 10.0, n0 + 10.0], [e0, n0 + 10.0], [e0, n0]]
            ],
        },
    }


def _sentinel_ngd_building_feature() -> dict:
    """An unmatched NGD building far from anything of ours, carrying a
    sentinel id and sentinel coordinates: the firewall test's own proof
    that neither ever reaches a written report, matched or not.
    """
    c = _SENTINEL_COORD
    return {
        "type": "Feature",
        "id": _SENTINEL_ID,
        "properties": {"description": "Shed"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[c, c], [c + 5.0, c], [c + 5.0, c + 5.0], [c, c + 5.0], [c, c]]],
        },
    }


def _ngd_road_features() -> list[dict]:
    """Roads matching the fixture's own horizontal and vertical OSM ways,
    both shifted (+0.9, +0.9), one of them carrying the sentinel id: the
    firewall test's own proof that an id on a feature that DOES
    contribute to the statistics still never leaks.

    Each one runs well past its own matching OSM way at both ends
    (-200 m to +300 m along the way's own extent), the same
    test_benchstats.py construction that keeps every projection interior
    rather than clamped to an endpoint, so the least-squares recovery is
    as clean here as it is in that module's own unit tests.
    """
    horizontal = [
        (_BASE_E - 200.0 + _SHIFT_E, _BASE_N + _SHIFT_N),
        (_BASE_E + 300.0 + _SHIFT_E, _BASE_N + _SHIFT_N),
    ]
    vertical = [
        (_BASE_E + 500.0 + _SHIFT_E, _BASE_N - 200.0 + _SHIFT_N),
        (_BASE_E + 500.0 + _SHIFT_E, _BASE_N + 300.0 + _SHIFT_N),
    ]
    return [
        {
            "type": "Feature",
            "id": "ordinary-matched-road",
            "properties": {},
            "geometry": {"type": "LineString", "coordinates": [list(p) for p in horizontal]},
        },
        {
            "type": "Feature",
            "id": _SENTINEL_ID,
            "properties": {},
            "geometry": {"type": "LineString", "coordinates": [list(p) for p in vertical]},
        },
    ]


def _stub_client_factory(client: _StubNgdClient):
    return lambda key: client


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(autouse=True)
def _zero_shift_ostn15(monkeypatch):
    """Every test in this module reads a package through a zero-shift
    OSTN15 grid, never the real cache or network: `load_ostn15` is
    monkeypatched at `mapgen.benchmark`'s own import of it, the identical
    seam `test_package.py` already uses for `mapgen.package.load_ostn15`.
    """
    monkeypatch.setattr(benchmark_module, "load_ostn15", lambda: _zero_shift_grid())

    def _boom():
        raise AssertionError("ensure_ostn15 must never be reached: load_ostn15 already hits")

    monkeypatch.setattr(benchmark_module, "ensure_ostn15", _boom)


# --------------------------------------------------------------------------
# Basic plumbing: package -> stats -> report, schema and sane numbers.
# --------------------------------------------------------------------------


def test_run_benchmark_writes_report_md_and_report_json(tmp_path):
    package_dir = _build_package(tmp_path)
    client = _StubNgdClient(
        buildings=[_matched_ngd_building_feature(), _sentinel_ngd_building_feature()],
        roads=_ngd_road_features(),
    )
    out_root = tmp_path / "benchmarks"

    md_path, json_path = run_benchmark(
        package_dir, _TEST_KEY, out_root, client_factory=_stub_client_factory(client)
    )

    assert md_path.is_file()
    assert json_path.is_file()
    assert md_path.parent == json_path.parent
    assert md_path.parent.name.startswith("Test-Site_2026-08-09_")
    assert client.verify_calls == [[BUILDING_COLLECTION, ROAD_COLLECTION]]
    assert len(client.items_calls) == 2


def test_report_json_schema_and_counts(tmp_path):
    package_dir = _build_package(tmp_path)
    client = _StubNgdClient(
        buildings=[_matched_ngd_building_feature(), _sentinel_ngd_building_feature()],
        roads=_ngd_road_features(),
    )

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert report["source"] == "OS NGD (dev-mode evaluation key)"
    assert report["package"] == "Test-Site_2026-08-09"
    assert report["bbox"] == [_BBOX.west, _BBOX.south, _BBOX.east, _BBOX.north]
    assert report["pages"] == {"buildings": 1, "roads": 1}

    # Two OSM buildings written by way (house, no source; yes, source
    # overture), the relation's own building tag never counted. Both sit
    # inside the pulled rectangle, so the clipped headline and the
    # pre-clip population agree and nothing was excluded.
    assert report["counts"]["buildings"]["ours"] == {"osm": 1, "overture": 1}
    assert report["counts"]["buildings"]["ours_all"] == {"osm": 1, "overture": 1}
    assert report["counts"]["buildings"]["ours_out_of_extent"] == {}
    assert report["counts"]["buildings"]["ngd"] == 2
    # Both fixture roads are highway=residential, so both land in the
    # carriageway leg and the path leg is empty.
    assert report["counts"]["roads"]["ours_osm_carriageway"] == 2
    assert report["counts"]["roads"]["ours_osm_path"] == 0
    assert report["counts"]["roads"]["ours_osm_other_values"] == {}
    assert report["counts"]["roads"]["ours_os_open"] == 1
    assert report["counts"]["roads"]["ngd"] == 2

    # Building A matches the NGD feature at the same square almost
    # exactly; the sentinel building sits far away and matches nothing.
    assert report["buildings"]["matched"] == 1
    assert report["buildings"]["unmatched_ours"] == 1
    assert report["buildings"]["unmatched_theirs"] == 1
    assert report["buildings"]["iou"]["p50"] > 0.9

    assert report["classes"]["ngd_only"] == {"Detached": 1, "Shed": 1}
    assert report["classes"]["counts"] == {"house": 1, "yes": 1}

    # The two-orientation OSM carriageway population recovers the true
    # shift via least squares; the naive mean understates it (see
    # test_benchstats.py).
    osm = report["roads"]["osm_carriageway"]
    assert osm["lsq_de"] == pytest.approx(_SHIFT_E, abs=0.05)
    assert osm["lsq_dn"] == pytest.approx(_SHIFT_N, abs=0.05)
    assert osm["mean_de"] < osm["lsq_de"]

    assert "osm_path" in report["roads"]
    assert "os_open" in report["roads"]
    assert isinstance(report["epoch_verdict"], str) and report["epoch_verdict"]
    assert report["epoch_basis"] == "osm_carriageway"


def test_report_md_mirrors_the_json_sections(tmp_path):
    package_dir = _build_package(tmp_path)
    client = _StubNgdClient(
        buildings=[_matched_ngd_building_feature()], roads=_ngd_road_features()
    )

    md_path, _ = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    text = md_path.read_text(encoding="utf-8")

    assert "OS NGD (dev-mode evaluation key)" in text
    assert "internal calibration only" in text
    assert "## Counts" in text
    assert "## Building footprint matching" in text
    assert "## Road offsets and the epoch question" in text
    assert "Least-squares offset" in text
    assert "Epoch verdict (carriageway leg alone):" in text
    assert "## Class names" in text
    assert "## What this says" in text
    assert "—" not in text  # no em dashes anywhere in the report


def test_an_empty_ngd_pull_reports_zeros_honestly_never_invents(tmp_path):
    package_dir = _build_package(tmp_path, with_os_roads=False)
    client = _StubNgdClient(buildings=[], roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert report["buildings"]["matched"] == 0
    assert report["buildings"]["matched_fraction_ours"] == 0.0
    assert report["buildings"]["matched_fraction_theirs"] == 0.0
    assert report["buildings"]["iou"] == {"p10": 0.0, "p50": 0.0, "p90": 0.0}
    assert report["roads"]["osm_carriageway"]["count"] == 0
    assert report["roads"]["osm_carriageway"]["lsq_de"] is None
    assert report["roads"]["osm_path"]["count"] == 0
    assert report["roads"]["os_open"]["count"] == 0
    assert "no matched road samples" in report["epoch_verdict"]


def test_a_building_relation_tag_is_never_counted(tmp_path):
    # The fixture's own relation 5001 carries building=yes over member way
    # 9003; only ways are read as buildings, so "ours" totals 2 (A and B),
    # never 3.
    package_dir = _build_package(tmp_path)
    client = _StubNgdClient(buildings=[], roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert sum(report["counts"]["buildings"]["ours"].values()) == 2


# --------------------------------------------------------------------------
# task-3-review.md Important finding 1: an upstream OSM source=* tag (an
# ordinary provenance convention, `source=Bing` included) is not the same
# thing as an Overture/OS-OpenMap-Local injection, and must never be
# counted as one.
# --------------------------------------------------------------------------


def test_read_buildings_counts_an_upstream_source_bing_tag_as_osm():
    # The review's own executed demonstration, reproduced directly against
    # _read_buildings: an ordinary OSM-native building=house way carrying
    # its own unrelated source=Bing tag (a standard OSM provenance
    # convention this project's own OSM merge never strips) must bucket
    # as "osm", not "Bing".
    nodes_elems, way = _closed_way(
        9201,
        _shift([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]),
        51,
        [("building", "house"), ("source", "Bing")],
    )
    root = ET.fromstring(_dump_osm([*nodes_elems, way]))
    nodes = {
        element.get("id"): (float(element.get("lat")), float(element.get("lon")))
        for element in root
        if element.tag == "node"
    }

    rings, ring_sources, counts_by_source, tag_value_counts = _read_buildings(
        root, nodes, _zero_shift_grid()
    )

    assert counts_by_source == {"osm": 1}
    assert tag_value_counts == {"house": 1}
    assert len(rings) == 1
    assert ring_sources == ["osm"]


def test_report_counts_an_upstream_source_bing_building_as_osm(tmp_path):
    root = tmp_path / "package"
    root.mkdir()
    stem = "Bing-Site_2026-08-10"
    (root / "survey.json").write_text(
        json.dumps({"urbano_stem": stem, "bbox": _BBOX.to_dict()}), encoding="utf-8"
    )
    nodes_elems, way = _closed_way(
        9202,
        _shift([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]),
        61,
        [("building", "house"), ("source", "Bing")],
    )
    (root / f"{stem}.osm").write_text(_dump_osm([*nodes_elems, way]), encoding="utf-8")
    client = _StubNgdClient(buildings=[], roads=[])

    _, json_path = run_benchmark(
        root, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert report["counts"]["buildings"]["ours"] == {"osm": 1}


def test_read_buildings_still_buckets_the_two_known_injection_labels():
    # The generic-presence bug this fix removes must not take the real
    # values down with it: overture and os_openmap_local (package.py's
    # own BUILDINGS_SOURCE_OVERTURE/BUILDINGS_SOURCE_OS_OPEN) still bucket
    # by name, exactly as before.
    overture_nodes, overture_way = _closed_way(
        9203, _shift([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]), 71,
        [("building", "yes"), ("source", "overture")],
    )
    os_open_nodes, os_open_way = _closed_way(
        9204, _shift([(50.0, 50.0), (55.0, 50.0), (55.0, 55.0), (50.0, 55.0)]), 81,
        [("building", "yes"), ("source", "os_openmap_local")],
    )
    root = ET.fromstring(
        _dump_osm([*overture_nodes, overture_way, *os_open_nodes, os_open_way])
    )
    nodes = {
        element.get("id"): (float(element.get("lat")), float(element.get("lon")))
        for element in root
        if element.tag == "node"
    }

    _, ring_sources, counts_by_source, _ = _read_buildings(root, nodes, _zero_shift_grid())

    assert counts_by_source == {"overture": 1, "os_openmap_local": 1}
    # The per-ring labels carry the same two values, in ring order: this
    # is what lets the extent clipping report what it excluded per layer.
    assert ring_sources == ["overture", "os_openmap_local"]


# --------------------------------------------------------------------------
# task-3-review.md Minor finding 2: a MultiPolygon NGD building must
# contribute every constituent polygon, not only its first.
# --------------------------------------------------------------------------


def test_ngd_multipolygon_building_contributes_every_part():
    feature = {
        "type": "Feature",
        "id": "two-part-multipolygon",
        "properties": {"description": "Terrace"},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
                [[[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0], [100.0, 100.0]]],
            ],
        },
    }

    rings = _ngd_building_rings([feature])

    assert len(rings) == 2
    assert rings[0][0] == (0.0, 0.0)
    assert rings[1][0] == (100.0, 100.0)


def test_report_counts_every_part_of_an_ngd_multipolygon_building(tmp_path):
    package_dir = _build_package(tmp_path, with_os_roads=False)
    e0, n0 = _BASE_E, _BASE_N
    multipolygon_feature = {
        "type": "Feature",
        "id": "two-part-multipolygon",
        "properties": {"description": "Terrace"},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                # Part 1 matches building A almost exactly: a real match.
                [[[e0, n0], [e0 + 10.0, n0], [e0 + 10.0, n0 + 10.0], [e0, n0 + 10.0], [e0, n0]]],
                # Part 2 sits far from anything of ours: unmatched, but
                # still counted.
                [
                    [
                        [e0 + 9_000.0, n0 + 9_000.0],
                        [e0 + 9_010.0, n0 + 9_000.0],
                        [e0 + 9_010.0, n0 + 9_010.0],
                        [e0 + 9_000.0, n0 + 9_010.0],
                        [e0 + 9_000.0, n0 + 9_000.0],
                    ]
                ],
            ],
        },
    }
    client = _StubNgdClient(buildings=[multipolygon_feature], roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert report["counts"]["buildings"]["ngd"] == 2
    assert report["buildings"]["matched"] == 1
    assert report["buildings"]["unmatched_theirs"] == 1


# --------------------------------------------------------------------------
# Containment and size: explaining the unmatched footprints rather than
# only counting them (see benchmark.py's own "explaining the unmatched"
# section). The fixture below is the real problem in miniature.
# --------------------------------------------------------------------------

# A 2 m by 2 m outbuilding of ours, 4 m2, sitting inside the footprint of
# the 100 m by 100 m NGD building below: unmatchable by sampled IoU
# (0.0004, far under MATCH_IOU_FLOOR) yet plainly standing on ground OS
# already covers, which is exactly the SPURIOUS_OR_NEWER case.
_TINY_OURS_CORNERS = [(8_010.0, 8_010.0), (8_012.0, 8_010.0), (8_012.0, 8_012.0), (8_010.0, 8_012.0)]


def _square_feature(feature_id: str, e0: float, n0: float, width: float, height: float) -> dict:
    """A BNG-coordinate NGD building feature, offset from the fixture's own
    _BASE_E/_BASE_N exactly like `_matched_ngd_building_feature`.
    """
    e, n = _BASE_E + e0, _BASE_N + n0
    return {
        "type": "Feature",
        "id": feature_id,
        "properties": {"description": "Building"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[e, n], [e + width, n], [e + width, n + height], [e, n + height], [e, n]]
            ],
        },
    }


def _containment_ngd_buildings() -> list[dict]:
    """Four NGD footprints over the fixture package, one per outcome.

    The first two are building A of ours (a 10 m square) cut into two 10 m
    by 5 m PARTS, which is precisely what NGD does to a terrace this
    project holds whole: one part wins the greedy pairing at IoU 0.5, the
    other cannot pair with anything and lands unmatched with nothing
    actually missing. The third stands on empty ground (a genuine gap).
    The fourth is a 100 m square that swallows the tiny outbuilding of
    ours, whose own interior point sits nowhere near it.
    """
    return [
        _square_feature("lower-half-of-A", 0.0, 0.0, 10.0, 5.0),
        _square_feature("upper-half-of-A", 0.0, 5.0, 10.0, 5.0),
        _square_feature("far-from-everything", 9_000.0, 9_000.0, 10.0, 10.0),
        _square_feature("swallows-our-outbuilding", 7_950.0, 7_950.0, 100.0, 100.0),
    ]


def test_containment_splits_the_unmatched_into_subdivisions_and_real_gaps(tmp_path):
    package_dir = _build_package(
        tmp_path,
        with_os_roads=False,
        extra_buildings=[(_TINY_OURS_CORNERS, [("building", "shed")])],
    )
    client = _StubNgdClient(buildings=_containment_ngd_buildings(), roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    # Building A pairs with one of its own two NGD halves at IoU 0.5.
    assert report["buildings"]["matched"] == 1
    assert report["buildings"]["unmatched_theirs"] == 3
    assert report["buildings"]["unmatched_ours"] == 2

    containment = report["containment"]
    # The other half of A stands inside A: a bookkeeping difference, not a
    # gap. The far square and the 100 m square stand on ground we hold
    # nothing on.
    assert containment["theirs_unmatched"] == {"subdivision": 1, "absent": 2}
    # Our outbuilding stands inside their 100 m square; building B stands
    # on ground they hold nothing on.
    assert containment["ours_unmatched"] == {"spurious_or_newer": 1, "absent_from_os": 1}

    # Both splits add back to the unmatched counts above: nothing is
    # dropped between the two classes.
    assert sum(containment["theirs_unmatched"].values()) == report["buildings"]["unmatched_theirs"]
    assert sum(containment["ours_unmatched"].values()) == report["buildings"]["unmatched_ours"]


def test_size_breakdown_buckets_every_population_and_cross_tabulates(tmp_path):
    package_dir = _build_package(
        tmp_path,
        with_os_roads=False,
        extra_buildings=[(_TINY_OURS_CORNERS, [("building", "shed")])],
    )
    client = _StubNgdClient(buildings=_containment_ngd_buildings(), roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    size = json.loads(json_path.read_text(encoding="utf-8"))["size"]

    assert size["buckets"] == [
        "under_10",
        "10_to_30",
        "30_to_80",
        "80_to_200",
        "200_to_1000",
        "over_1000",
    ]

    # Ours: A (100 m2) matched; B (100 m2) and the 4 m2 outbuilding not.
    assert size["ours_matched"]["80_to_200"] == 1
    assert sum(size["ours_matched"].values()) == 1
    assert size["ours_unmatched"] == {
        "under_10": 1,
        "10_to_30": 0,
        "30_to_80": 0,
        "80_to_200": 1,
        "200_to_1000": 0,
        "over_1000": 0,
    }

    # Theirs: one 50 m2 half matched; the other 50 m2 half, a 100 m2
    # square and a 10,000 m2 square not.
    assert size["theirs_matched"]["30_to_80"] == 1
    assert sum(size["theirs_matched"].values()) == 1
    assert size["theirs_unmatched"] == {
        "under_10": 0,
        "10_to_30": 0,
        "30_to_80": 1,
        "80_to_200": 1,
        "200_to_1000": 0,
        "over_1000": 1,
    }

    # The cross-tabulation: which sizes fall in which containment class.
    assert size["theirs_unmatched_by_class"]["subdivision"]["30_to_80"] == 1
    assert sum(size["theirs_unmatched_by_class"]["subdivision"].values()) == 1
    assert size["theirs_unmatched_by_class"]["absent"]["80_to_200"] == 1
    assert size["theirs_unmatched_by_class"]["absent"]["over_1000"] == 1
    assert size["ours_unmatched_by_class"]["spurious_or_newer"]["under_10"] == 1
    assert size["ours_unmatched_by_class"]["absent_from_os"]["80_to_200"] == 1

    # Every cross-tabulated class adds back to its own unmatched row.
    for bucket in size["buckets"]:
        assert size["theirs_unmatched"][bucket] == (
            size["theirs_unmatched_by_class"]["subdivision"][bucket]
            + size["theirs_unmatched_by_class"]["absent"][bucket]
        )
        assert size["ours_unmatched"][bucket] == (
            size["ours_unmatched_by_class"]["spurious_or_newer"][bucket]
            + size["ours_unmatched_by_class"]["absent_from_os"][bucket]
        )


def test_report_md_carries_the_containment_and_size_sections(tmp_path):
    package_dir = _build_package(
        tmp_path,
        with_os_roads=False,
        extra_buildings=[(_TINY_OURS_CORNERS, [("building", "shed")])],
    )
    client = _StubNgdClient(buildings=_containment_ngd_buildings(), roads=[])

    md_path, _ = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    text = md_path.read_text(encoding="utf-8")

    assert "## What the unmatched footprints are standing on" in text
    assert "## Footprint size, square metres" in text
    assert "Subdivision (stands inside a footprint we hold): 1" in text
    assert "Absent (we hold nothing on that ground): 2" in text
    assert "| NGD, subdivision |" in text
    assert "| ours, absent from OS |" in text
    assert "—" not in text  # no em dashes anywhere in the report


def test_an_empty_ngd_pull_reports_empty_containment_and_size_honestly(tmp_path):
    package_dir = _build_package(tmp_path, with_os_roads=False)
    client = _StubNgdClient(buildings=[], roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    # Both of ours matched nothing, and both stand on ground OS holds
    # nothing on, because OS returned nothing at all: an honest reading of
    # an empty pull, not a special case.
    assert report["containment"]["theirs_unmatched"] == {"subdivision": 0, "absent": 0}
    assert report["containment"]["ours_unmatched"] == {
        "spurious_or_newer": 0,
        "absent_from_os": 2,
    }
    assert sum(report["size"]["theirs_unmatched"].values()) == 0
    assert sum(report["size"]["ours_unmatched"].values()) == 2
    # Every bucket is still present, zeros included.
    assert len(report["size"]["theirs_matched"]) == len(report["size"]["buckets"])


# --------------------------------------------------------------------------
# FIX 1: our own population is CLIPPED to the rectangle the NGD pull was
# actually made over, and what the clipping excluded is reported per
# source layer rather than dropped in silence.
#
# The defect this pins: a package covers roughly 1.8 times the NGD query
# extent (the survey pads its own extent, and injected OS OpenMap Local
# footprints arrive over a wider footprint again), and every footprint
# outside the rectangle landed in "absent from OS" by construction, since
# it could neither match nor be contained. On the real Cowbridge package
# that was 367 of 1755 footprints, 346 of them from one injection layer.
# --------------------------------------------------------------------------

# 500 m past the fixture rectangle's own north-east corner (which sits at
# _BASE + 10_000 m; see `_BBOX`). Far enough out that the 7-decimal-place
# rounding survey.json applies to the bbox cannot move it back inside.
_OUT_OF_EXTENT_CORNERS = [
    (10_500.0, 10_500.0),
    (10_510.0, 10_500.0),
    (10_510.0, 10_510.0),
    (10_500.0, 10_510.0),
]


def test_a_footprint_outside_the_pulled_rectangle_is_excluded_and_counted(tmp_path):
    package_dir = _build_package(
        tmp_path,
        with_os_roads=False,
        extra_buildings=[
            (_OUT_OF_EXTENT_CORNERS, [("building", "yes"), ("source", "os_openmap_local")])
        ],
    )
    client = _StubNgdClient(buildings=[_matched_ngd_building_feature()], roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    counts = report["counts"]["buildings"]

    # Three building=* ways in the package; only the two inside the pulled
    # rectangle are compared, and the excluded one is named by its own
    # source layer rather than folded into a single opaque total.
    assert counts["ours_all"] == {"osm": 1, "overture": 1, "os_openmap_local": 1}
    assert counts["ours"] == {"osm": 1, "overture": 1}
    assert counts["ours_out_of_extent"] == {"os_openmap_local": 1}
    # Nothing is lost between the two: kept plus excluded is the whole
    # projectable population.
    assert sum(counts["ours"].values()) + sum(counts["ours_out_of_extent"].values()) == sum(
        counts["ours_all"].values()
    )

    # The out-of-extent footprint never reaches the comparison at all, so
    # it cannot inflate "absent from OS" the way it did before the clip:
    # only building B (in extent, unmatched) is counted there.
    assert report["buildings"]["unmatched_ours"] == 1
    assert report["containment"]["ours_unmatched"]["absent_from_os"] == 1
    # And the matched fraction is taken over the clipped population (1 of
    # 2), never over the whole package (which would read 1 of 3).
    assert report["buildings"]["matched"] == 1
    assert report["buildings"]["matched_fraction_ours"] == pytest.approx(0.5)


def test_the_clipping_is_printed_in_the_markdown_report(tmp_path):
    package_dir = _build_package(
        tmp_path,
        with_os_roads=False,
        extra_buildings=[
            (_OUT_OF_EXTENT_CORNERS, [("building", "yes"), ("source", "os_openmap_local")])
        ],
    )
    client = _StubNgdClient(buildings=[_matched_ngd_building_feature()], roads=[])

    md_path, _ = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    text = md_path.read_text(encoding="utf-8")

    assert "CLIPPED to the rectangle the NGD pull was made over" in text

    # The excluded layer is named UNDER the excluded heading, not merely
    # somewhere on the page: a report that clipped nothing would print
    # the same layer name under the compared population instead, and an
    # anywhere-in-the-text assertion could not tell the two apart.
    excluded = text.split("Buildings, ours EXCLUDED as out of extent, by source:")[1]
    excluded = excluded.split("\n\n")[0]
    assert "- os_openmap_local: 1" in excluded
    assert "- total: 1" in excluded

    compared = text.split("Buildings, ours by source (in extent, the compared population):")[1]
    compared = compared.split("\n\n")[0]
    assert "os_openmap_local" not in compared
    assert "- total: 2" in compared


def test_a_package_wholly_inside_the_rectangle_excludes_nothing(tmp_path):
    # The clip must be inert when there is nothing to clip: every fixture
    # footprint sits inside the pulled rectangle, so the excluded table is
    # empty and the report says so in words rather than printing a blank.
    package_dir = _build_package(tmp_path, with_os_roads=False)
    client = _StubNgdClient(buildings=[], roads=[])

    md_path, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert report["counts"]["buildings"]["ours_out_of_extent"] == {}
    assert report["counts"]["buildings"]["ours"] == report["counts"]["buildings"]["ours_all"]
    assert "every footprint of ours falls inside the pulled rectangle" in md_path.read_text(
        encoding="utf-8"
    )


# --------------------------------------------------------------------------
# FIX 2: the SUBDIVISION label is earned, not assumed. The containment
# test compares no areas at all, so "OS split a building we hold whole"
# and "our polygon is drawn oversized" are the same test result; one
# aggregate integer separates them.
# --------------------------------------------------------------------------


def test_a_subdivision_whose_ngd_part_is_larger_than_our_footprint_is_counted(tmp_path):
    # Our footprint here is a 10 m square, 100 m2 (building A). Two NGD
    # parts stand inside it: a 3 m square, 9 m2 (smaller, the ordinary
    # subdivision reading, and deliberately under the 0.1 IoU floor so it
    # does not simply pair with A) and a 40 m square whose own interior
    # point also falls inside A but which is sixteen times A's area. The
    # second is the REVERSED reading: not OS splitting a building we hold
    # whole, but our own polygon drawn small inside theirs, which the
    # containment test cannot tell from the first because it compares no
    # areas at all.
    package_dir = _build_package(tmp_path, with_os_roads=False)
    small_part = _square_feature("small-part-inside-A", 1.0, 1.0, 3.0, 3.0)
    # Centred on A so its own representative point lands inside A.
    large_part = _square_feature("large-part-over-A", -15.0, -15.0, 40.0, 40.0)
    client = _StubNgdClient(buildings=[small_part, large_part], roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    containment = json.loads(json_path.read_text(encoding="utf-8"))["containment"]

    assert containment["theirs_unmatched"]["subdivision"] == 2
    assert containment["theirs_unmatched"]["absent"] == 0
    # Exactly one of the two is the reversed reading.
    assert containment["theirs_subdivision_larger_than_ours"] == 1


def test_subdivisions_all_smaller_than_ours_count_zero_reversed(tmp_path):
    # The ordinary case, and the one the label was written for: building A
    # of ours (a 10 m square) cut into two 10 m by 5 m parts. One wins the
    # pairing, the other is a subdivision, and neither is larger than A.
    package_dir = _build_package(tmp_path, with_os_roads=False)
    client = _StubNgdClient(
        buildings=[
            _square_feature("lower-half-of-A", 0.0, 0.0, 10.0, 5.0),
            _square_feature("upper-half-of-A", 0.0, 5.0, 10.0, 5.0),
        ],
        roads=[],
    )

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    containment = json.loads(json_path.read_text(encoding="utf-8"))["containment"]

    assert containment["theirs_unmatched"]["subdivision"] == 1
    assert containment["theirs_subdivision_larger_than_ours"] == 0


def test_the_reversed_subdivision_count_is_printed_beside_the_subdivision_count(tmp_path):
    package_dir = _build_package(tmp_path, with_os_roads=False)
    client = _StubNgdClient(
        buildings=[
            _square_feature("small-part-inside-A", 1.0, 1.0, 3.0, 3.0),
            _square_feature("large-part-over-A", -15.0, -15.0, 40.0, 40.0),
        ],
        roads=[],
    )

    md_path, _ = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    text = md_path.read_text(encoding="utf-8")

    assert "Subdivision (stands inside a footprint we hold): 2" in text
    assert (
        "of those, NGD part LARGER than the footprint of ours containing it: 1" in text
    )


# --------------------------------------------------------------------------
# FIX 3: carriageways and paths are separate populations, and the epoch
# verdict is read off the carriageway leg alone.
#
# NGD's roadlink collection holds carriageway centrelines only, so a
# pavement has no counterpart in it: measured against the nearest
# carriageway it contributes the width of the street, with a sign that
# flips by which side of the street it runs down, and enough of those
# cancel and drag the least-squares estimate toward zero.
# --------------------------------------------------------------------------

_PATH_HIGHWAY_FIXTURES = ("footway", "path", "bridleway", "steps", "cycleway", "track")
_CARRIAGEWAY_HIGHWAY_FIXTURES = (
    "motorway",
    "trunk",
    "primary_link",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "service",
    "living_street",
)


def test_osm_roads_split_into_carriageway_and_path_by_highway_value(tmp_path):
    # One way per highway value, each a short line of its own well away
    # from the others, so the split is decided by the tag alone.
    extra_roads = [
        ([(1_000.0 + 20.0 * i, 1_000.0), (1_000.0 + 20.0 * i + 10.0, 1_000.0)], [("highway", value)])
        for i, value in enumerate(_PATH_HIGHWAY_FIXTURES)
    ] + [
        ([(2_000.0 + 20.0 * i, 2_000.0), (2_000.0 + 20.0 * i + 10.0, 2_000.0)], [("highway", value)])
        for i, value in enumerate(_CARRIAGEWAY_HIGHWAY_FIXTURES)
    ]
    package_dir = _build_package(tmp_path, with_os_roads=False, extra_roads=extra_roads)
    client = _StubNgdClient(buildings=[], roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    counts = json.loads(json_path.read_text(encoding="utf-8"))["counts"]["roads"]

    # The two fixture roads are highway=residential, so they join the
    # carriageway leg alongside the nine added here.
    assert counts["ours_osm_carriageway"] == len(_CARRIAGEWAY_HIGHWAY_FIXTURES) + 2
    assert counts["ours_osm_path"] == len(_PATH_HIGHWAY_FIXTURES)
    assert counts["ours_osm_other_values"] == {}


def test_a_highway_value_in_neither_family_is_named_and_counted_never_folded_in(tmp_path):
    extra_roads = [
        ([(1_000.0, 1_000.0), (1_010.0, 1_000.0)], [("highway", "construction")]),
        ([(1_000.0, 1_100.0), (1_010.0, 1_100.0)], [("highway", "pedestrian")]),
        ([(1_000.0, 1_200.0), (1_010.0, 1_200.0)], [("highway", "construction")]),
    ]
    package_dir = _build_package(tmp_path, with_os_roads=False, extra_roads=extra_roads)
    client = _StubNgdClient(buildings=[], roads=[])

    md_path, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    counts = json.loads(json_path.read_text(encoding="utf-8"))["counts"]["roads"]

    assert counts["ours_osm_carriageway"] == 2
    assert counts["ours_osm_path"] == 0
    assert counts["ours_osm_other_values"] == {"construction": 2, "pedestrian": 1}
    assert "construction 2, pedestrian 1" in md_path.read_text(encoding="utf-8")


def test_the_epoch_verdict_comes_from_the_carriageway_leg_alone(tmp_path):
    # The construction that makes the defect visible. The two carriageways
    # of the fixture are shifted (+0.9, +0.9) from their NGD counterparts,
    # which is squarely inside the CONSISTENT band. Alongside them run
    # four pavements, three down the south side of the horizontal
    # carriageway and one down the north side: each finds that carriageway
    # inside the 15 m search radius and contributes an offset of several
    # metres pointing at it, positive from the south side and negative
    # from the north, which is the sign flip that makes a mixed
    # pavement population partly cancel. None of it is a survey
    # disagreement; all of it is the width of the street. Folded into one
    # population these drag the least-squares estimate right off the real
    # shift; read separately, the carriageway verdict is untouched.
    pavements = [
        ([(0.0, offset_n), (100.0, offset_n)], [("highway", "footway")])
        for offset_n in (-6.0, -8.0, -10.0, 6.0)
    ]
    package_dir = _build_package(tmp_path, with_os_roads=False, extra_roads=pavements)
    client = _StubNgdClient(buildings=[], roads=_ngd_road_features())

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    carriageway = report["roads"]["osm_carriageway"]
    path = report["roads"]["osm_path"]

    assert report["counts"]["roads"]["ours_osm_carriageway"] == 2
    assert report["counts"]["roads"]["ours_osm_path"] == 4

    # The carriageway leg still recovers the true shift.
    assert carriageway["lsq_de"] == pytest.approx(_SHIFT_E, abs=0.05)
    assert carriageway["lsq_dn"] == pytest.approx(_SHIFT_N, abs=0.05)
    assert report["epoch_basis"] == "osm_carriageway"
    assert "CONSISTENT with the epoch-shift hypothesis" in report["epoch_verdict"]

    # The path leg measured something else entirely: pavement-to-
    # carriageway distance, metres of it, nothing like a sub-metre epoch
    # shift.
    assert path["count"] > 0
    assert path["p50_abs"] > 4.0
    assert path["mean_dn"] > 1.5

    # And the same samples folded back into one population, the way this
    # benchmark used to read them, do not produce that verdict at all:
    # this is the continuity break, measured rather than asserted.
    osm_root = benchmark_module._read_osm(package_dir / "Test-Site_2026-08-09.osm")
    populations = benchmark_module._read_osm_roads(
        osm_root, benchmark_module._osm_nodes(osm_root), _zero_shift_grid()
    )
    combined = benchmark_module.polyline_offsets(
        populations.carriageway + populations.path,
        benchmark_module._ngd_road_polylines(_ngd_road_features()),
    )
    assert combined.lsq_magnitude is not None
    assert abs(combined.lsq_magnitude - carriageway["lsq_magnitude"]) > 1.0


def test_the_report_says_the_split_breaks_continuity_and_labels_the_path_leg(tmp_path):
    package_dir = _build_package(
        tmp_path,
        with_os_roads=False,
        extra_roads=[([(0.0, 6.0), (100.0, 6.0)], [("highway", "footway")])],
    )
    client = _StubNgdClient(buildings=[], roads=_ngd_road_features())

    md_path, _ = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    text = md_path.read_text(encoding="utf-8")

    assert "### OSM carriageway (the epoch measurement)" in text
    assert (
        "### OSM path (pavement-to-carriageway distance, NOT survey disagreement)" in text
    )
    assert "DELIBERATELY breaks continuity" in text
    assert "Epoch verdict (carriageway leg alone):" in text


# --------------------------------------------------------------------------
# FIX 5: the closing framing. The subdivision half is not bookkeeping.
# --------------------------------------------------------------------------


def test_the_closing_section_calls_the_subdivisions_lines_an_architect_draws(tmp_path):
    package_dir = _build_package(
        tmp_path,
        with_os_roads=False,
        extra_buildings=[(_TINY_OURS_CORNERS, [("building", "shed")])],
    )
    client = _StubNgdClient(buildings=_containment_ngd_buildings(), roads=[])

    md_path, _ = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    text = md_path.read_text(encoding="utf-8")

    assert "## What this says" in text
    assert "party walls, house-to-garage joins, terrace divisions" in text
    assert "lines an" in text and "architect draws at 1:500" in text
    assert "where one building stops and the next begins" in text
    # The overclaim this section replaced, and the word it must not use
    # for the subdivision half.
    assert "not anything you would draw" not in text
    assert "bookkeeping" not in text


def test_missing_os_roads_file_is_an_empty_control_population_not_an_error(tmp_path):
    package_dir = _build_package(tmp_path, with_os_roads=False)
    assert not (package_dir / "Test-Site_2026-08-09_os_roads.geojson").exists()
    client = _StubNgdClient(buildings=[], roads=[])

    _, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["counts"]["roads"]["ours_os_open"] == 0
    assert report["roads"]["os_open"]["count"] == 0


def test_missing_survey_json_is_a_benchmark_error(tmp_path):
    package_dir = tmp_path / "empty-package"
    package_dir.mkdir()
    with pytest.raises(BenchmarkError):
        run_benchmark(package_dir, _TEST_KEY, tmp_path / "benchmarks")


def test_missing_osm_file_is_a_benchmark_error(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "survey.json").write_text(
        json.dumps({"urbano_stem": "Nothing_2026-08-09", "bbox": _BBOX.to_dict()}),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError):
        run_benchmark(package_dir, _TEST_KEY, tmp_path / "benchmarks")


# --------------------------------------------------------------------------
# FIREWALL TESTS (the brief's own binding list).
# --------------------------------------------------------------------------


def test_firewall_out_root_holds_exactly_two_files_after_a_run(tmp_path):
    package_dir = _build_package(tmp_path)
    client = _StubNgdClient(
        buildings=[_matched_ngd_building_feature(), _sentinel_ngd_building_feature()],
        roads=_ngd_road_features(),
    )
    out_root = tmp_path / "benchmarks"

    run_benchmark(package_dir, _TEST_KEY, out_root, client_factory=_stub_client_factory(client))

    written = [path for path in out_root.rglob("*") if path.is_file()]
    assert len(written) == 2


def test_firewall_report_json_carries_no_sentinel_coordinate_no_sentinel_id_no_key(tmp_path):
    package_dir = _build_package(tmp_path)
    client = _StubNgdClient(
        buildings=[_matched_ngd_building_feature(), _sentinel_ngd_building_feature()],
        roads=_ngd_road_features(),
    )

    md_path, json_path = run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )
    json_text = json_path.read_text(encoding="utf-8")
    md_text = md_path.read_text(encoding="utf-8")

    for text in (json_text, md_text):
        assert _SENTINEL_ID not in text
        assert str(_SENTINEL_COORD) not in text
        assert _TEST_KEY not in text


def test_firewall_no_write_anywhere_under_the_package_dir(tmp_path):
    package_dir = _build_package(tmp_path)
    before = _snapshot(package_dir)
    client = _StubNgdClient(
        buildings=[_matched_ngd_building_feature()], roads=_ngd_road_features()
    )

    run_benchmark(
        package_dir, _TEST_KEY, tmp_path / "benchmarks", client_factory=_stub_client_factory(client)
    )

    after = _snapshot(package_dir)
    assert after == before


def test_firewall_an_ngd_error_mid_run_leaves_no_partial_report(tmp_path):
    package_dir = _build_package(tmp_path)
    boom = NgdError("the OS NGD API answered HTTP 500.", kind="query")
    client = _StubNgdClient(buildings=[_matched_ngd_building_feature()], road_error=boom)
    out_root = tmp_path / "benchmarks"

    with pytest.raises(NgdError):
        run_benchmark(
            package_dir, _TEST_KEY, out_root, client_factory=_stub_client_factory(client)
        )

    assert not out_root.exists() or not any(out_root.rglob("*"))


def test_firewall_an_ngd_error_on_verify_collections_leaves_no_partial_report(tmp_path):
    package_dir = _build_package(tmp_path)
    boom = NgdError("collection 'x' is not in the service's own listing.", kind="listing")
    client = _StubNgdClient(verify_error=boom)
    out_root = tmp_path / "benchmarks"

    with pytest.raises(NgdError):
        run_benchmark(
            package_dir, _TEST_KEY, out_root, client_factory=_stub_client_factory(client)
        )

    assert not out_root.exists() or not any(out_root.rglob("*"))
    assert client.items_calls == []


# --------------------------------------------------------------------------
# CLI: the key resolution and its own two-line, no-network refusal.
# --------------------------------------------------------------------------


class _RefusesToOpenASocket:
    def __call__(self, *args, **kwargs):
        raise AssertionError(
            "mapgen benchmark with no key must never open a network socket"
        )


def test_cli_refuses_with_no_key_two_lines_nonzero_exit_no_network(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OS_NGD_KEY", raising=False)
    monkeypatch.setattr(socket, "socket", _RefusesToOpenASocket())
    package_dir = _build_package(tmp_path)

    exit_code = main(["benchmark", str(package_dir)])

    assert exit_code != 0
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(err_lines) == 2
    for line in err_lines:
        assert "http" not in line.lower()


def test_cli_resolves_key_from_environment_variable(monkeypatch):
    monkeypatch.setenv("OS_NGD_KEY", _TEST_KEY)
    args = cli_module.build_parser().parse_args(["benchmark", "somewhere"])
    assert args.key is None
    import os

    assert (args.key or os.environ.get("OS_NGD_KEY")) == _TEST_KEY


def test_cli_flag_key_wins_over_environment_variable(monkeypatch):
    monkeypatch.setenv("OS_NGD_KEY", "env-key-should-lose")
    args = cli_module.build_parser().parse_args(["benchmark", "somewhere", "--key", _TEST_KEY])
    import os

    assert (args.key or os.environ.get("OS_NGD_KEY")) == _TEST_KEY


def test_cli_runs_the_benchmark_end_to_end_with_a_stubbed_client(tmp_path, monkeypatch, capsys):
    package_dir = _build_package(tmp_path)
    client = _StubNgdClient(
        buildings=[_matched_ngd_building_feature()], roads=_ngd_road_features()
    )
    monkeypatch.setattr(
        cli_module, "run_benchmark",
        lambda pkg, key, out_root: run_benchmark(
            pkg, key, out_root, client_factory=_stub_client_factory(client)
        ),
    )

    exit_code = main(
        ["benchmark", str(package_dir), "--key", _TEST_KEY, "--out", str(tmp_path / "benchmarks")]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Report:" in out
    assert "Data:" in out
    assert _TEST_KEY not in out
