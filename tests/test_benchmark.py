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
from pathlib import Path

import pytest

from mapgen import benchmark as benchmark_module
from mapgen import cli as cli_module
from mapgen.benchmark import BenchmarkError, run_benchmark
from mapgen.bng import tm_inverse
from mapgen.cli import main
from mapgen.geo import BBox
from mapgen.ngd import BUILDING_COLLECTION, ROAD_COLLECTION, NgdError
from tests.test_heights import _dump_osm, _nodes_for, _relation, _way, _zero_shift_grid

_TEST_KEY = "sekrit-dev-mode-key-do-not-leak"
_SENTINEL_ID = "SENTINEL-PREMIUM-FEATURE-ID-771"
_SENTINEL_COORD = 918273.456

_BBOX = BBox(west=-3.45, south=51.45, east=-3.44, north=51.46)

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


def _build_package(tmp_path: Path, *, stem: str = "Test-Site_2026-08-09", with_os_roads: bool = True) -> Path:
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
    # overture), the relation's own building tag never counted.
    assert report["counts"]["buildings"]["ours"] == {"osm": 1, "overture": 1}
    assert report["counts"]["buildings"]["ngd"] == 2
    assert report["counts"]["roads"]["ours_osm"] == 2
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

    # The two-orientation OSM road population recovers the true shift via
    # least squares; the naive mean understates it (see test_benchstats.py).
    osm = report["roads"]["osm"]
    assert osm["lsq_de"] == pytest.approx(_SHIFT_E, abs=0.05)
    assert osm["lsq_dn"] == pytest.approx(_SHIFT_N, abs=0.05)
    assert osm["mean_de"] < osm["lsq_de"]

    assert "os_open" in report["roads"]
    assert isinstance(report["epoch_verdict"], str) and report["epoch_verdict"]


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
    assert "Epoch verdict:" in text
    assert "## Class names" in text
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
    assert report["roads"]["osm"]["count"] == 0
    assert report["roads"]["osm"]["lsq_de"] is None
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
