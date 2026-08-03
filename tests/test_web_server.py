import hashlib
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from mapgen.geo import BBox
from mapgen.geocode import GeocodeError, GeocodeQueueFullError, GeocodeResult, ReverseResult
from mapgen.jobs import EventLog
from mapgen.package import SurveyRequest
from mapgen.sources.base import Estimate, clear_registry, register
from mapgen.sources.elevation import ElevationSource
from mapgen.web.server import (
    STATIC_DIR,
    JobBusyError,
    JobManager,
    JobRecord,
    _watch_heartbeat,
    build_server,
    make_handler,
    serve,
)

TOKEN = "test-token"


class StubSource:
    id = "stub"
    display_name = "Stub"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / "stub.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("merged", encoding="utf-8")
        return [out]


class BlockingSource:
    """Like StubSource, but fetch() waits until the test releases it.

    Used wherever a test needs to observe a job while it is genuinely still
    running, or needs a second job attempt to land while the first is
    still busy, without racing real wall-clock timing to do it.
    """

    id = "blocking"
    display_name = "Blocking"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=1, seconds_estimate=0.1)

    def fetch(self, bbox, tiles, work_dir, progress):
        self.started.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError("test did not release the blocking source in time")
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / "blocking.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("merged", encoding="utf-8")
        return [out]


class _CountingEventLog(EventLog):
    """Counts snapshot() calls, to prove the handler actually calls it.

    JobRecord holding an EventLog (asserted elsewhere) is a necessary shape,
    but not sufficient: a handler that reaches past snapshot() straight at
    the underlying `.events` list would have the exact right shape and
    still bypass the lock. This catches that specific mistake directly,
    since JobRecord's shape alone does not.
    """

    def __init__(self):
        super().__init__()
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return super().snapshot()


class StubGeocodeClient:
    """A geocode_client double that returns or raises a canned result.

    Mirrors what StubSource is for LayerSource: server.py's routing and
    status-code translation is tested here, independent of NominatimClient's
    own HTTP call and Nominatim-shape parsing, which have their own tests in
    test_geocode.py. search_results/reverse_result and search_error/
    reverse_error are read per call, not snapshotted at construction, so a
    test can flip them between requests against the same running server.
    search_results matches NominatimClient.search's real contract: a list,
    empty rather than None when nothing matched.
    """

    def __init__(self):
        self.search_results: list[GeocodeResult] = []
        self.search_error: Exception | None = None
        self.reverse_result: ReverseResult = ReverseResult(region="", site="")
        self.reverse_error: Exception | None = None
        self.search_calls: list[str] = []
        self.reverse_calls: list[tuple[float, float]] = []

    def search(self, query: str) -> list[GeocodeResult]:
        self.search_calls.append(query)
        if self.search_error is not None:
            raise self.search_error
        return self.search_results

    def reverse(self, lat: float, lon: float) -> ReverseResult:
        self.reverse_calls.append((lat, lon))
        if self.reverse_error is not None:
            raise self.reverse_error
        return self.reverse_result


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_registry()
    register(StubSource())
    yield
    clear_registry()


@pytest.fixture
def server():
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def geocode_server():
    """Like `server`, but wires in a StubGeocodeClient instead of a real
    NominatimClient, and hands the test the stub so it can set up a canned
    result or error before making a request.
    """
    manager = JobManager()
    stub = StubGeocodeClient()
    handler = make_handler(manager, TOKEN, STATIC_DIR, stub)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", stub
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base, path, payload, token=TOKEN):
    request = urllib.request.Request(
        f"{base}{path}?token={token}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get(base, path, token=TOKEN):
    with urllib.request.urlopen(f"{base}{path}?token={token}", timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _put(base, path, payload, token=TOKEN):
    request = urllib.request.Request(
        f"{base}{path}?token={token}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _wait_for_state(base, job_id, timeout=5.0):
    """Poll a job until it leaves 'running', or fail loudly on timeout.

    A fixed deadline rather than a fixed sleep: the jobs under test finish in
    well under a second, so this only ever burns the full timeout when the
    job is genuinely stuck, which is exactly when the test should fail.
    """
    deadline = time.time() + timeout
    payload = None
    while time.time() < deadline:
        _, payload = _get(base, f"/api/jobs/{job_id}")
        if payload["state"] != "running":
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not leave 'running' within {timeout}s: {payload}")


def test_requests_without_a_token_are_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{server}/api/config", timeout=10)
    assert excinfo.value.code == 403


def test_requests_with_the_wrong_token_are_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server, "/api/config", token="wrong")
    assert excinfo.value.code == 403


def test_config_endpoint_returns_the_saved_defaults(server, tmp_path, monkeypatch):
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "config.json")
    status, payload = _get(server, "/api/config")
    assert status == 200
    assert "output_root" in payload
    assert payload["tile_size_m"] > 0


def test_sources_endpoint_lists_the_registry(server):
    status, payload = _get(server, "/api/sources")
    assert status == 200
    assert payload[0]["id"] == "stub"
    assert payload[0]["requires_api_key"] is False


def test_sources_endpoint_omits_the_api_key_field_for_a_source_with_no_key(server):
    status, payload = _get(server, "/api/sources")
    assert status == 200
    assert payload[0]["api_key_config_field"] is None


def test_sources_endpoint_names_the_config_field_for_a_source_that_needs_a_key(server):
    # Task 19: the settings panel is driven from this rather than a
    # hard-coded field per key, so a real keyed source must actually
    # advertise which Config field its key is saved under.
    from mapgen.sources.elevation import ElevationSource

    clear_registry()
    register(ElevationSource())
    status, payload = _get(server, "/api/sources")
    assert status == 200
    elevation_entry = next(s for s in payload if s["id"] == "elevation")
    assert elevation_entry["api_key_config_field"] == "opentopography_api_key"


def test_categories_endpoint_requires_a_token(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{server}/api/categories", timeout=10)
    assert excinfo.value.code == 403


def test_categories_endpoint_lists_every_leaf_id(server):
    from mapgen.categories import ALL_CATEGORY_IDS

    status, payload = _get(server, "/api/categories")
    assert status == 200
    leaf_ids = set()
    for group in payload:
        if group["children"]:
            leaf_ids.update(child["id"] for child in group["children"])
        else:
            leaf_ids.add(group["id"])
    assert leaf_ids == set(ALL_CATEGORY_IDS)


def test_categories_endpoint_nests_road_subtypes_under_the_roads_group(server):
    status, payload = _get(server, "/api/categories")
    assert status == 200
    roads_group = next(g for g in payload if g["id"] == "roads")
    child_ids = [c["id"] for c in roads_group["children"]]
    assert "footpath" in child_ids
    assert "motorway" in child_ids
    # "roads" itself is a grouping label, never a real category id.
    assert "roads" not in {c["id"] for c in roads_group["children"]}


def test_estimate_endpoint_returns_tile_count(server, tmp_path):
    status, payload = _post(
        server,
        "/api/estimate",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "tile_size_m": 600,
            "overlap_m": 50,
            "sources": ["stub"],
        },
    )
    assert status == 200
    assert payload["tiles"] >= 1


def test_estimate_returns_400_for_a_bad_bbox(server, tmp_path):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(
            server,
            "/api/estimate",
            {
                "bbox": "nonsense",
                "region": "R",
                "site": "S",
                "output_root": str(tmp_path),
                "sources": ["stub"],
            },
        )
    assert excinfo.value.code == 400


def test_estimate_endpoint_includes_the_real_folder_path(server, tmp_path):
    # Task 18 item 7: the folder preview the interface shows before
    # downloading must come from this response, produced by the same
    # naming.build_package_paths call that plans the job itself.
    status, payload = _post(
        server,
        "/api/estimate",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "Vale of Glamorgan",
            "site": "Barry Waterfront",
            "output_root": str(tmp_path),
            "sources": ["stub"],
        },
    )
    assert status == 200
    assert payload["folder"].startswith(str(tmp_path))
    assert "Vale-of-Glamorgan" in payload["folder"]
    assert "Barry-Waterfront" in payload["folder"]


def test_estimate_endpoint_reports_no_warnings_for_a_source_with_no_readiness_check(
    server, tmp_path
):
    status, payload = _post(
        server,
        "/api/estimate",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "R",
            "site": "S",
            "output_root": str(tmp_path),
            "sources": ["stub"],
        },
    )
    assert status == 200
    assert payload["warnings"] == []


def test_estimate_returns_400_rather_than_crashing_on_a_null_tile_size(server, tmp_path):
    # Mirrors what a real browser sends when the tile-size field is
    # empty: JSON.stringify(NaN) is the literal "null", so the key is
    # PRESENT with value None, which payload.get(key, default) does not
    # treat as absent. float(None) raises TypeError, which the original
    # except (BBoxError, NamingError, KeyError) tuple did not catch.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(
            server,
            "/api/estimate",
            {
                "bbox": "-3.29,51.38,-3.28,51.39",
                "region": "R",
                "site": "S",
                "output_root": str(tmp_path),
                "tile_size_m": None,
                "sources": ["stub"],
            },
        )
    assert excinfo.value.code == 400


def test_jobs_endpoint_returns_400_rather_than_crashing_on_a_null_overlap(server, tmp_path):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(
            server,
            "/api/jobs",
            {
                "bbox": "-3.29,51.38,-3.28,51.39",
                "region": "R",
                "site": "S",
                "output_root": str(tmp_path),
                "overlap_m": None,
                "sources": ["stub"],
                "run_bridge": False,
            },
        )
    assert excinfo.value.code == 400


# --- Review round 1: tile_size_m must be validated, and an absurd tiling
# refused, identically on all three routes that can be asked to tile ------


@pytest.mark.parametrize("path", ["/api/estimate", "/api/extent"])
def test_a_zero_tile_size_returns_400_rather_than_dropping_the_connection(server, tmp_path, path):
    body = {"bbox": "-3.29,51.38,-3.28,51.39", "tile_size_m": 0}
    if path == "/api/estimate":
        body.update(region="R", site="S", output_root=str(tmp_path), sources=["stub"])
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(server, path, body)
    assert excinfo.value.code == 400


def test_a_negative_tile_size_is_rejected_rather_than_silently_reporting_zero_tiles(server, tmp_path):
    # Previously: HTTP 200, {"tiles": 0}, and the client's refreshEstimate
    # success path would have enabled Download for a request that could
    # never produce anything.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(
            server,
            "/api/estimate",
            {
                "bbox": "-3.29,51.38,-3.28,51.39",
                "region": "R",
                "site": "S",
                "output_root": str(tmp_path),
                "tile_size_m": -600,
                "sources": ["stub"],
            },
        )
    assert excinfo.value.code == 400


def test_jobs_endpoint_rejects_a_zero_tile_size_synchronously_rather_than_202_then_failing(
    server, tmp_path
):
    # The specific "worse" case the review named: /api/jobs must not
    # accept a request that can never be tiled with a 202 and only fail
    # it later, asynchronously, in the worker thread.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(
            server,
            "/api/jobs",
            {
                "bbox": "-3.29,51.38,-3.28,51.39",
                "region": "R",
                "site": "S",
                "output_root": str(tmp_path),
                "tile_size_m": 0,
                "sources": ["stub"],
                "run_bridge": False,
            },
        )
    assert excinfo.value.code == 400
    # A job must never have been created for the rejected request: if it
    # had, the server's manager would still be busy and this legitimate
    # follow-up would come back 409 instead of 202, the same technique
    # test_posting_a_job_without_a_token_has_no_side_effects already uses
    # to prove an unauthorised POST has no side effects.
    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "R",
            "site": "S",
            "output_root": str(tmp_path),
            "sources": ["stub"],
            "run_bridge": False,
        },
    )
    assert status == 202
    _wait_for_state(server, payload["id"])


@pytest.mark.parametrize("path", ["/api/estimate", "/api/extent"])
def test_an_absurd_tiling_is_refused_rather_than_hanging(server, tmp_path, path):
    # The live-feedback path (/api/extent) fires on every draw with no
    # names required, so this is the one an accidental zoomed-out drag
    # hits hardest, but the same underlying build_tiles call protects
    # /api/estimate identically. A whole-Europe-sized bbox at a small
    # tile size is refused from row/col counts alone, not by actually
    # building the tiles.
    body = {
        "bbox": "-10.0,35.0,30.0,60.0",  # roughly Western Europe
        "tile_size_m": 100,
    }
    if path == "/api/estimate":
        body.update(region="R", site="S", output_root=str(tmp_path), sources=["stub"])
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(server, path, body)
    assert excinfo.value.code == 400


# --- Task 18 item 6: /api/extent, a bare-geometry endpoint needing no
# region, site or output_root at all ---------------------------------------


def test_extent_requires_a_token(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            urllib.request.Request(
                f"{server}/api/extent",
                data=json.dumps({"bbox": "-3.29,51.38,-3.28,51.39"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=10,
        )
    assert excinfo.value.code == 403


def test_extent_endpoint_returns_tile_geometry_with_no_region_site_or_output_root(server):
    status, payload = _post(
        server,
        "/api/extent",
        {"bbox": "-3.29,51.38,-3.28,51.39", "tile_size_m": 600, "overlap_m": 50},
    )
    assert status == 200
    assert set(payload) == {"tiles", "rows", "cols", "extent_km"}
    # Review round 1: "tiles >= 1" alone survives _geometry_summary
    # returning len(tiles) + 1, or rows/cols swapped, or any other
    # off-by-something. This exact bbox/tile_size/overlap combination is
    # independently confirmed elsewhere in this suite (test_geo.py's own
    # build_tiles tests) to be a 2x2 grid, four tiles named r00_c00
    # through r01_c01: asserted against those known numbers directly.
    assert payload["tiles"] == 4
    assert payload["rows"] == 2
    assert payload["cols"] == 2


def test_extent_endpoint_defaults_tile_size_and_overlap_like_estimate_does(server):
    status, payload = _post(server, "/api/extent", {"bbox": "-3.29,51.38,-3.28,51.39"})
    assert status == 200
    assert payload["tiles"] >= 1


def test_extent_endpoint_returns_400_for_a_bad_bbox(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(server, "/api/extent", {"bbox": "nonsense"})
    assert excinfo.value.code == 400


def test_extent_endpoint_returns_400_rather_than_crashing_on_a_null_tile_size(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(
            server,
            "/api/extent",
            {"bbox": "-3.29,51.38,-3.28,51.39", "tile_size_m": None},
        )
    assert excinfo.value.code == 400


def test_extent_endpoint_matches_estimate_endpoint_tiling_for_the_same_inputs(server, tmp_path):
    bbox = "-3.29,51.38,-3.28,51.39"
    _, extent_payload = _post(
        server, "/api/extent", {"bbox": bbox, "tile_size_m": 600, "overlap_m": 50}
    )
    _, estimate_payload = _post(
        server,
        "/api/estimate",
        {
            "bbox": bbox,
            "region": "R",
            "site": "S",
            "output_root": str(tmp_path),
            "tile_size_m": 600,
            "overlap_m": 50,
            "sources": ["stub"],
        },
    )
    assert extent_payload["tiles"] == estimate_payload["tiles"]
    assert extent_payload["rows"] == estimate_payload["rows"]
    assert extent_payload["cols"] == estimate_payload["cols"]
    assert extent_payload["extent_km"] == estimate_payload["extent_km"]


def test_index_is_served_without_a_token(server):
    with urllib.request.urlopen(f"{server}/", timeout=10) as response:
        assert response.status == 200
        assert b"<html" in response.read().lower()


def test_static_path_traversal_is_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{server}/../pyproject.toml", timeout=10)
    assert excinfo.value.code == 404


def test_job_manager_rejects_a_second_concurrent_job():
    manager = JobManager()
    manager._busy = True  # simulate a running job
    with pytest.raises(JobBusyError):
        manager.ensure_free()


def test_job_manager_reports_when_it_is_free():
    manager = JobManager()
    manager.ensure_free()
    assert manager.is_busy() is False


def _wait_for_manager_job(manager, job_id, timeout=5.0):
    deadline = time.time() + timeout
    record = manager.get(job_id)
    while time.time() < deadline:
        if record.state != "running":
            return record
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not leave 'running' within {timeout}s")


def test_job_record_holds_the_event_log_itself_not_a_bare_list(tmp_path):
    """Binding requirement: JobRecord.log must be the EventLog, not a copy.

    A bare events: list[dict] field fed by a listener= would duplicate the
    history into something nothing guards, which the handler would then
    serialise while the worker appends to it. That specific mistake does not
    reliably fail under an actual race in CPython: list.append versus list
    iteration under the GIL happens not to corrupt memory or raise, so a
    timing-based test above can pass even against that exact anti-pattern.
    The invariant that actually closes the gap is asserted directly on
    JobRecord's shape here, rather than on timing.
    """
    manager = JobManager()
    job_id = manager.start(
        SurveyRequest(
            bbox=BBox.parse("-3.29,51.38,-3.28,51.39"),
            region="R",
            site="S",
            output_root=tmp_path,
            source_ids=("stub",),
            run_bridge_step=False,
        )
    )
    record = manager.get(job_id)
    assert isinstance(record.log, EventLog)
    assert not hasattr(record, "events")
    _wait_for_manager_job(manager, job_id)


# --- Coverage beyond the brief's baseline ---------------------------------
#
# The tests above establish auth, config, sources, estimate and static
# serving, plus a unit-level check of JobManager.ensure_free(). None of them
# ever start a real job over HTTP, so none of them exercise the thing this
# task is actually bound to get right: a worker thread appending job events
# while a handler thread reads them back mid-run. The tests below start real
# jobs through JobManager and the HTTP routes so that path is proven, not
# just assumed from EventLog's own unit tests in test_jobs.py.


def test_estimate_returns_400_for_an_unknown_source(server, tmp_path):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(
            server,
            "/api/estimate",
            {
                "bbox": "-3.29,51.38,-3.28,51.39",
                "region": "R",
                "site": "S",
                "output_root": str(tmp_path),
                "sources": ["does-not-exist"],
            },
        )
    assert excinfo.value.code == 400


def test_posting_a_job_without_a_token_has_no_side_effects(server, tmp_path):
    request = urllib.request.Request(
        f"{server}/api/jobs",
        data=json.dumps(
            {
                "bbox": "-3.29,51.38,-3.28,51.39",
                "region": "South Wales",
                "site": "Barry",
                "output_root": str(tmp_path),
                "sources": ["stub"],
                "run_bridge": False,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 403

    # If the unauthorised POST above had actually started a job, the manager
    # would still be busy and this legitimate request would come back 409
    # instead of 202.
    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "sources": ["stub"],
            "run_bridge": False,
        },
    )
    assert status == 202
    _wait_for_state(server, payload["id"])


def test_starting_a_job_reaches_done_with_events_and_result_root(server, tmp_path):
    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "tile_size_m": 600,
            "overlap_m": 50,
            "sources": ["stub"],
            "run_bridge": False,
        },
    )
    assert status == 202
    job_id = payload["id"]

    final = _wait_for_state(server, job_id)
    assert final["state"] == "done"
    assert final["error"] is None
    event_names = [event["event"] for event in final["events"]]
    assert "job_started" in event_names
    assert "job_finished" in event_names
    # The output_root round-tripped through JSON and back on a Windows path;
    # if it had been mangled this directory would not exist.
    assert Path(final["result_root"]).is_dir()


def test_a_jobs_category_selection_is_recorded_in_survey_json(server, tmp_path):
    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "sources": ["stub"],
            "categories": ["buildings", "water"],
            "run_bridge": False,
        },
    )
    assert status == 202
    final = _wait_for_state(server, payload["id"])
    assert final["state"] == "done"
    survey_json = Path(final["result_root"]) / "survey.json"
    recorded = json.loads(survey_json.read_text(encoding="utf-8"))
    assert sorted(recorded["categories"]) == ["buildings", "water"]


def test_an_empty_category_selection_is_not_coalesced_into_the_default(server, tmp_path):
    # payload.get(key) is used deliberately, not payload.get(key) or
    # default: an explicit [] (every checkbox unticked) must survive as
    # a genuine "nothing selected", not be treated the same as an absent
    # key (which means "every category", today's behaviour).
    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "sources": ["stub"],
            "categories": [],
            "run_bridge": False,
        },
    )
    assert status == 202
    final = _wait_for_state(server, payload["id"])
    survey_json = Path(final["result_root"]) / "survey.json"
    recorded = json.loads(survey_json.read_text(encoding="utf-8"))
    assert recorded["categories"] == []


def test_a_missing_category_selection_defaults_to_every_category(server, tmp_path):
    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "sources": ["stub"],
            "run_bridge": False,
        },
    )
    assert status == 202
    final = _wait_for_state(server, payload["id"])
    survey_json = Path(final["result_root"]) / "survey.json"
    recorded = json.loads(survey_json.read_text(encoding="utf-8"))
    from mapgen.categories import ALL_CATEGORY_IDS

    assert sorted(recorded["categories"]) == sorted(ALL_CATEGORY_IDS)


def test_events_can_be_read_while_the_job_is_still_running(server, tmp_path):
    blocking = BlockingSource()
    register(blocking)

    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "sources": ["blocking"],
            "run_bridge": False,
        },
    )
    assert status == 202
    job_id = payload["id"]

    assert blocking.started.wait(timeout=5)
    # The worker thread is inside fetch() right now, blocked on the release
    # event below. This read races the worker's next emit() by design: it
    # must return a clean, parseable snapshot regardless of who wins.
    status, running_payload = _get(server, f"/api/jobs/{job_id}")
    assert status == 200
    assert running_payload["state"] == "running"
    assert any(event["event"] == "job_started" for event in running_payload["events"])

    blocking.release.set()
    final = _wait_for_state(server, job_id)
    assert final["state"] == "done"


def test_a_second_job_is_rejected_while_one_is_running(server, tmp_path):
    blocking = BlockingSource()
    register(blocking)

    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "sources": ["blocking"],
            "run_bridge": False,
        },
    )
    assert status == 202
    first_job_id = payload["id"]
    assert blocking.started.wait(timeout=5)

    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(
                server,
                "/api/jobs",
                {
                    "bbox": "-3.29,51.38,-3.28,51.39",
                    "region": "South Wales",
                    "site": "Barry",
                    "output_root": str(tmp_path),
                    "sources": ["stub"],
                    "run_bridge": False,
                },
            )
        assert excinfo.value.code == 409
    finally:
        blocking.release.set()
        _wait_for_state(server, first_job_id)


def test_getting_an_unknown_job_returns_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server, "/api/jobs/does-not-exist")
    assert excinfo.value.code == 404


def test_cancelling_an_unknown_job_returns_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(server, "/api/jobs/does-not-exist/cancel", {})
    assert excinfo.value.code == 404


def test_cancel_endpoint_stops_a_running_job(server, tmp_path):
    # run_survey only checks for cancellation between sources (and once more
    # before the bridge step), never mid-fetch. Two sources make this
    # deterministic: cancel while "blocking" is stuck in fetch(), release it,
    # and the cancellation is guaranteed to be observed before "stub" starts.
    blocking = BlockingSource()
    register(blocking)

    status, payload = _post(
        server,
        "/api/jobs",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "sources": ["blocking", "stub"],
            "run_bridge": False,
        },
    )
    assert status == 202
    job_id = payload["id"]
    assert blocking.started.wait(timeout=5)

    status, cancel_payload = _post(server, f"/api/jobs/{job_id}/cancel", {})
    assert status == 200
    assert cancel_payload["cancelled"] is True

    blocking.release.set()
    final = _wait_for_state(server, job_id)
    assert final["state"] == "cancelled"


def test_a_malformed_job_path_returns_404_rather_than_crashing(server):
    # Ends with "/cancel" but is too short to be a real job-cancel path. A
    # route matched by slicing path.split("/") at a fixed index would raise
    # IndexError here instead of answering cleanly.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(server, "/api/cancel", {})
    assert excinfo.value.code == 404


def test_config_put_without_a_token_makes_no_change(server, tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", config_path)

    request = urllib.request.Request(
        f"{server}/api/config",
        data=json.dumps({"tile_size_m": 750.0}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 403
    assert not config_path.exists()


def test_config_put_saves_and_round_trips_through_get(server, tmp_path, monkeypatch):
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "config.json")

    status, payload = _put(
        server, "/api/config", {"tile_size_m": 750.0, "last_region": "Cardiff"}
    )
    assert status == 200
    assert payload["tile_size_m"] == 750.0

    status, reloaded = _get(server, "/api/config")
    assert status == 200
    assert reloaded["tile_size_m"] == 750.0
    assert reloaded["last_region"] == "Cardiff"


def test_config_put_saves_and_round_trips_the_api_key(server, tmp_path, monkeypatch):
    # Task 18 item 5: the settings field's whole point is that a saved key
    # survives a reload without the owner retyping it. Generic PUT/GET
    # plumbing already covers every field the same way (see the test
    # above); this one names the new field directly since it is the
    # reason this task added it.
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "config.json")

    status, payload = _put(
        server, "/api/config", {"opentopography_api_key": "sk-real-key-value"}
    )
    assert status == 200
    assert payload["opentopography_api_key"] == "sk-real-key-value"

    status, reloaded = _get(server, "/api/config")
    assert status == 200
    assert reloaded["opentopography_api_key"] == "sk-real-key-value"


# --- Review round 1: the key must not leak through a real job end to end --
#
# test_sources_elevation.py proves the redaction at the unit level: six
# failure shapes, each asserting fetch() itself never raises with the
# secret present. This proves the property that actually matters to the
# owner: a real job that fails this way, driven through the real HTTP
# routes and JobManager exactly as a browser would, must not surface the
# key in ANYTHING /api/jobs/<id> returns, since that is what the browser
# polls every 700ms and renders into the visible log panel.


def test_a_leaking_elevation_failure_never_surfaces_the_key_through_a_real_job(tmp_path):
    SECRET = "sk-real-secret-should-never-leak-anywhere"
    leaky_url = f"https://portal.opentopography.org/API/globaldem?API_Key={SECRET}&demtype=COP30"

    class LeakySession:
        def get(self, url, **kwargs):
            raise requests.exceptions.ConnectionError(
                f"HTTPSConnectionPool: Max retries exceeded with url: {leaky_url}"
            )

    # Registered before build_server(): register_default_sources() skips
    # "elevation" if a same-type instance already holds that id (see its
    # own docstring), so this specific, leak-configured instance is the
    # one the server actually uses, not a fresh default one.
    register(ElevationSource(api_key=SECRET, session=LeakySession()))
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, payload = _post(
            base,
            "/api/jobs",
            {
                "bbox": "-3.29,51.38,-3.28,51.39",
                "region": "R",
                "site": "S",
                "output_root": str(tmp_path),
                "sources": ["elevation"],
                "run_bridge": False,
            },
        )
        assert status == 202
        final = _wait_for_state(base, payload["id"])
        assert final["state"] == "failed"
        # The whole payload, not just .error: source_failed events land in
        # .events too, and the browser's job poller renders both into the
        # visible log. Asserting on the dumped JSON catches the secret
        # appearing ANYWHERE in the response, not just in the one field a
        # narrower assertion happened to think to check.
        dumped = json.dumps(final)
        assert SECRET not in dumped, f"the api key leaked into the job response: {dumped}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_config_put_with_invalid_json_returns_400(server, tmp_path, monkeypatch):
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "config.json")

    request = urllib.request.Request(
        f"{server}/api/config?token={TOKEN}",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 400


# --- Round 1 review findings -----------------------------------------------
#
# The tests above (both the brief's nine and the fourteen added afterward)
# all passed against a version of the handler that read record.log.events
# directly instead of calling record.log.snapshot(), against a version of
# EventLog.snapshot() with the lock deleted, and against the original
# keep-alive body-draining bug, confirmed independently. The tests below
# close those specific gaps.


def test_job_status_endpoint_reads_through_snapshot_not_the_live_list():
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        counting_log = _CountingEventLog()
        record = JobRecord(id="counting-job", state="done", log=counting_log)
        httpd.manager._jobs[record.id] = record

        status, _payload = _get(base, f"/api/jobs/{record.id}")

        assert status == 200
        assert counting_log.snapshot_calls == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_keep_alive_connection_survives_an_unauthorised_post_with_a_body(
    server, tmp_path, monkeypatch
):
    # Reproduces the review's manual finding on a single socket, using
    # http.client rather than urllib.request: urllib opens a fresh
    # connection per call, so it cannot observe a connection left desynced
    # by an early return that skipped the request body.
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "config.json")
    parsed = urlsplit(server)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        body = json.dumps(
            {"bbox": "-3.29,51.38,-3.28,51.39", "region": "R", "site": "S"}
        ).encode("utf-8")
        conn.request(
            "POST",
            "/api/jobs",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        first = conn.getresponse()
        assert first.status == 403
        first.read()

        # Same connection. If the POST's body was left on the wire, these
        # leftover bytes get parsed as the start of this request instead.
        conn.request("GET", f"/api/config?token={TOKEN}")
        second = conn.getresponse()
        assert second.status == 200
        json.loads(second.read().decode("utf-8"))
    finally:
        conn.close()


def test_keep_alive_connection_survives_a_put_to_an_unknown_path_with_a_body(
    server, tmp_path, monkeypatch
):
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "config.json")
    parsed = urlsplit(server)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        body = json.dumps({"tile_size_m": 750.0}).encode("utf-8")
        conn.request(
            "PUT",
            f"/api/not-config?token={TOKEN}",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        first = conn.getresponse()
        assert first.status == 404
        first.read()

        conn.request("GET", f"/api/config?token={TOKEN}")
        second = conn.getresponse()
        assert second.status == 200
        json.loads(second.read().decode("utf-8"))
    finally:
        conn.close()


def test_a_non_numeric_content_length_does_not_crash_the_handler(server):
    parsed = urlsplit(server)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        conn.putrequest("POST", f"/api/estimate?token={TOKEN}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "abc")
        conn.endheaders()
        response = conn.getresponse()
        # A malformed Content-Length is treated as no body, so /api/estimate
        # then fails on its own missing required fields. What matters here
        # is that this is a clean HTTP response at all, not a dropped
        # connection from an unhandled ValueError inside the handler.
        assert response.status == 400
        response.read()
    finally:
        conn.close()


def test_a_body_that_is_not_utf8_does_not_crash_the_handler(server):
    # A correct Content-Length with undecodable bytes reaches the decode in
    # _parse_json, where a UnicodeDecodeError would kill the handler thread
    # and drop the connection. json.JSONDecodeError never fires here: the
    # bytes fail to become a string at all.
    body = b"\xff\xfe\x00invalid"
    parsed = urlsplit(server)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        conn.putrequest("POST", f"/api/estimate?token={TOKEN}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        conn.send(body)
        response = conn.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "Body was not valid JSON."
    finally:
        conn.close()


def test_build_server_defaults_to_loopback_only():
    httpd = build_server(port=0, token=TOKEN)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


def test_busy_flag_clears_if_the_worker_thread_fails_to_start(tmp_path, monkeypatch):
    manager = JobManager()

    def _boom(self):
        raise RuntimeError("simulated thread start failure")

    monkeypatch.setattr(threading.Thread, "start", _boom)

    with pytest.raises(RuntimeError):
        manager.start(
            SurveyRequest(
                bbox=BBox.parse("-3.29,51.38,-3.28,51.39"),
                region="R",
                site="S",
                output_root=tmp_path,
                source_ids=("stub",),
                run_bridge_step=False,
            )
        )

    assert manager.is_busy() is False


def test_static_serving_decodes_percent_encoded_names(tmp_path):
    (tmp_path / "map pin.svg").write_text("<svg></svg>", encoding="utf-8")
    manager = JobManager()
    handler = make_handler(manager, TOKEN, tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(f"{base}/map%20pin.svg", timeout=10) as response:
            assert response.status == 200
            assert response.read() == b"<svg></svg>"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_static_traversal_is_still_rejected_after_percent_decoding(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("do not serve me", encoding="utf-8")

    manager = JobManager()
    handler = make_handler(manager, TOKEN, static_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"{base}/%2e%2e/outside_secret.txt", timeout=10)
        assert excinfo.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_static_path_traversal_cannot_reach_a_prefix_sharing_sibling_directory(tmp_path):
    # The reviewer's confirmed exploit against the original string-prefix
    # guard: static2 shares a string prefix with static, so
    # str(target).startswith(str(static_dir)) let it through. is_relative_to
    # does not. Reverting to the prefix check leaves every other test in
    # this file green, so this is the only thing that discriminates it.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    sibling = tmp_path / "static2"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("do not serve me", encoding="utf-8")

    manager = JobManager()
    handler = make_handler(manager, TOKEN, static_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"{base}/../static2/secret.txt", timeout=10)
        assert excinfo.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- Task 16: the static interface is actually served ----------------------
#
# Task 16 built the map picker as plain files under static/: index.html plus
# the vendored Leaflet, styles.css and app.js it links to. None of that is
# exercised by a unit test, since there is no browser in this harness to
# drive it, so a typo in a href or a file that never got committed would
# otherwise be invisible until a person opens the page. These tests catch
# exactly that: the page loads, and every local asset index.html references
# is itself servable. The reference list is parsed out of index.html rather
# than hard-coded, so it keeps checking whatever the markup actually points
# at instead of a fixed guess that quietly stops matching reality.


def _local_assets_referenced_by(html: str) -> list[str]:
    """Every href/src on a <link> or <script> tag that names a local file.

    A scheme prefix (http:, https:, ...) or a protocol-relative //host form
    marks a reference as external, not a local static asset, and is
    excluded. Nothing here is specific to Leaflet or to today's markup: any
    local file index.html is made to depend on, now or later, is picked up.
    """
    tags = re.findall(r"<(?:link|script)\b[^>]*>", html, re.IGNORECASE)
    refs = []
    for tag in tags:
        match = re.search(r'(?:href|src)="([^"]+)"', tag, re.IGNORECASE)
        if not match:
            continue
        ref = match.group(1)
        if ref.startswith("//") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref):
            continue
        refs.append(ref)
    return sorted(set(refs))


_INDEX_HTML_SOURCE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
_REFERENCED_ASSETS = _local_assets_referenced_by(_INDEX_HTML_SOURCE)


def test_index_html_references_at_least_one_local_asset():
    # Guards the parser itself. If this were ever empty, for example because
    # a markup rewrite stopped using href="..."/src="..." with plain double
    # quotes, the parametrised test below would silently collect zero cases
    # and report nothing to check instead of failing loudly.
    assert _REFERENCED_ASSETS, "expected index.html to reference at least one local file"


# test_api_key_field_is_type_password used to live here, grepping index.html
# directly for a static <input id="opentopo-key"> element. Task 19 moved API
# key fields out of static markup entirely: they are now rendered by app.js's
# renderApiKeys, one per source that exposes api_key_config_field, so there is
# no longer a fixed id or a static tag for a Python-side grep to find. The
# same property (type="password", so a saved key is never shown in plain
# text) is now pinned in tests/js/test_app.js's "boot() pre-fills a keyed
# source's field from the saved config", against the REAL rendered output of
# the real app.js, which is a stronger check than a static-text grep ever
# was: it would catch renderApiKeys emitting the wrong type just as surely
# as a hand-edited markup regression, matching this project's own stated
# split (server behaviour in pytest, client logic in the Node harness).


def test_root_is_served_as_html(server):
    with urllib.request.urlopen(f"{server}/", timeout=10) as response:
        assert response.status == 200
        assert "html" in response.headers.get("Content-Type", "").lower()
        body = response.read()
        assert len(body) > 0
        assert b"<html" in body.lower()


@pytest.mark.parametrize("asset_path", _REFERENCED_ASSETS)
def test_index_referenced_asset_is_served(server, asset_path):
    with urllib.request.urlopen(f"{server}/{asset_path}", timeout=10) as response:
        assert response.status == 200
        body = response.read()
        assert len(body) > 0, f"{asset_path} was served with an empty body"


# --- geocoding moved server-side --------------------------------------------
#
# app.js used to call Nominatim directly from the browser. That made the
# page's own network footprint bigger than "localhost plus the map tile
# servers", and a browser script cannot set a real User-Agent (it is a
# forbidden header name in the Fetch standard) or hold a rate limit that
# means anything across two tabs. Both endpoints below are routed through
# geocode_client, a StubGeocodeClient here so what is under test is
# server.py's own routing, auth and status-code translation, not
# NominatimClient's HTTP handling, which is covered on its own in
# test_geocode.py.


def test_geocode_requires_a_token(geocode_server):
    base, stub = geocode_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/api/geocode?q=Barry", timeout=10)
    assert excinfo.value.code == 403
    # Not just "the response was 403": if the auth check ever moved below
    # the geocode call, or a route were added that skipped it, this would
    # still see a 403 from some other check while a real outbound call
    # (and a real rate-limit slot) had already happened. This is the same
    # gap the existing test_posting_a_job_without_a_token_has_no_side_effects
    # closes for jobs, applied to the geocode client specifically.
    assert stub.search_calls == []


def test_geocode_requires_a_query(geocode_server):
    base, _stub = geocode_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/api/geocode?token={TOKEN}", timeout=10)
    assert excinfo.value.code == 400


def test_geocode_returns_a_bbox_for_a_match(geocode_server):
    base, stub = geocode_server
    stub.search_results = [
        GeocodeResult(
            display_name="Barry, Vale of Glamorgan, Wales, United Kingdom",
            west=-3.31,
            south=51.38,
            east=-3.25,
            north=51.43,
        )
    ]
    with urllib.request.urlopen(
        f"{base}/api/geocode?q=Barry%2C+Wales&token={TOKEN}", timeout=10
    ) as response:
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
    assert payload == [
        {
            "display_name": "Barry, Vale of Glamorgan, Wales, United Kingdom",
            "west": -3.31,
            "south": 51.38,
            "east": -3.25,
            "north": 51.43,
            "region": "",
            "site": "",
        }
    ]
    assert stub.search_calls == ["Barry, Wales"]


def test_geocode_returns_the_hits_own_region_and_site_when_present(geocode_server):
    # Task 19: the typeahead is the client's evidence for deriving region
    # and site directly from a chosen result, without a second reverse
    # lookup. The route must pass these through, not drop them.
    base, stub = geocode_server
    stub.search_results = [
        GeocodeResult(
            display_name="Barry Island, Barry, Vale of Glamorgan, Wales, United Kingdom",
            west=-3.29,
            south=51.37,
            east=-3.25,
            north=51.41,
            region="Vale of Glamorgan",
            site="Barry Island",
        )
    ]
    with urllib.request.urlopen(
        f"{base}/api/geocode?q=Barry+Island&token={TOKEN}", timeout=10
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload[0]["region"] == "Vale of Glamorgan"
    assert payload[0]["site"] == "Barry Island"


def test_geocode_returns_several_matches_for_disambiguation(geocode_server):
    # Task 18's whole point: a typeahead needs enough hits to tell two
    # same-named places apart, not just the single best guess a one-shot
    # search used to jump straight to.
    base, stub = geocode_server
    stub.search_results = [
        GeocodeResult(display_name="Barry, Wales", west=-3.31, south=51.38, east=-3.25, north=51.43),
        GeocodeResult(display_name="Barrie, Ontario", west=-79.72, south=44.36, east=-79.63, north=44.42),
        GeocodeResult(display_name="Barry, California", west=-118.5, south=34.0, east=-118.4, north=34.1),
    ]
    with urllib.request.urlopen(
        f"{base}/api/geocode?q=Barr&token={TOKEN}", timeout=10
    ) as response:
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
    assert len(payload) == 3
    assert [p["display_name"] for p in payload] == [
        "Barry, Wales",
        "Barrie, Ontario",
        "Barry, California",
    ]


def test_geocode_returns_404_for_no_match(geocode_server):
    base, stub = geocode_server
    stub.search_results = []
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/api/geocode?q=nowhere&token={TOKEN}", timeout=10)
    assert excinfo.value.code == 404
    payload = json.loads(excinfo.value.read().decode("utf-8"))
    assert "nowhere" in payload["error"]


def test_geocode_returns_502_when_nominatim_is_unreachable(geocode_server):
    base, stub = geocode_server
    stub.search_error = GeocodeError("Could not reach Nominatim: connection refused")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/api/geocode?q=Barry&token={TOKEN}", timeout=10)
    assert excinfo.value.code == 502
    payload = json.loads(excinfo.value.read().decode("utf-8"))
    assert "error" in payload


def test_geocode_returns_429_when_the_rate_limit_queue_is_full(geocode_server):
    # GeocodeQueueFullError is a GeocodeError subclass, so this also proves
    # the route checks for it specifically rather than only the general
    # case: catching GeocodeError first would send this back as a 502,
    # burying "come back shortly" under "something is broken".
    base, stub = geocode_server
    stub.search_error = GeocodeQueueFullError("Too many geocoding requests are already waiting.")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/api/geocode?q=Barry&token={TOKEN}", timeout=10)
    assert excinfo.value.code == 429


def test_reverse_requires_a_token(geocode_server):
    base, stub = geocode_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/api/reverse?lat=51.4&lon=-3.3", timeout=10)
    assert excinfo.value.code == 403
    assert stub.reverse_calls == []


def test_reverse_requires_numeric_lat_and_lon(geocode_server):
    base, _stub = geocode_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"{base}/api/reverse?lat=nope&lon=-3.3&token={TOKEN}", timeout=10
        )
    assert excinfo.value.code == 400


def test_reverse_requires_both_lat_and_lon(geocode_server):
    base, _stub = geocode_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base}/api/reverse?lat=51.4&token={TOKEN}", timeout=10)
    assert excinfo.value.code == 400


@pytest.mark.parametrize("garbage_lat", ["nan", "inf", "-inf", "1e400"])
def test_reverse_rejects_non_finite_lat(geocode_server, garbage_lat):
    # Python's float() parses "nan", "inf" and an overflowing literal like
    # "1e400" (which becomes inf) without raising, so a bare try/except
    # ValueError around the float() call lets every one of these through
    # to spend a rate-limit slot on a call to Nominatim that was always
    # going to be meaningless.
    base, stub = geocode_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"{base}/api/reverse?lat={garbage_lat}&lon=-3.3&token={TOKEN}", timeout=10
        )
    assert excinfo.value.code == 400
    assert stub.reverse_calls == []


def test_reverse_rejects_an_out_of_world_but_finite_lat(geocode_server):
    base, stub = geocode_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"{base}/api/reverse?lat=200&lon=-3.3&token={TOKEN}", timeout=10
        )
    assert excinfo.value.code == 400
    assert stub.reverse_calls == []


def test_reverse_returns_region_and_site(geocode_server):
    base, stub = geocode_server
    stub.reverse_result = ReverseResult(region="Vale of Glamorgan", site="Barry")
    with urllib.request.urlopen(
        f"{base}/api/reverse?lat=51.405&lon=-3.283&token={TOKEN}", timeout=10
    ) as response:
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
    assert payload == {"region": "Vale of Glamorgan", "site": "Barry"}
    assert stub.reverse_calls == [(51.405, -3.283)]


def test_reverse_returns_502_when_nominatim_is_unreachable(geocode_server):
    base, stub = geocode_server
    stub.reverse_error = GeocodeError("Nominatim returned HTTP 429")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"{base}/api/reverse?lat=51.4&lon=-3.3&token={TOKEN}", timeout=10
        )
    assert excinfo.value.code == 502


def test_reverse_returns_429_when_the_rate_limit_queue_is_full(geocode_server):
    base, stub = geocode_server
    stub.reverse_error = GeocodeQueueFullError("Too many geocoding requests are already waiting.")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"{base}/api/reverse?lat=51.4&lon=-3.3&token={TOKEN}", timeout=10
        )
    assert excinfo.value.code == 429


def test_build_server_wires_a_real_nominatim_client_by_default():
    # Regression guard for make_handler's geocode_client=None default: a
    # server built the normal way (build_server, or the CLI's `mapgen ui`)
    # must not silently end up with no geocode client at all just because
    # nothing was passed in explicitly.
    from mapgen.geocode import NominatimClient

    httpd = build_server(port=0, token=TOKEN)
    try:
        assert isinstance(httpd.geocode_client, NominatimClient)
    finally:
        httpd.server_close()


# --- the page must never call out beyond loopback and the map tiles -------
#
# This is the one constraint the whole geocoding-moved-server-side round
# existed to satisfy, and until now nothing in the repository checked it
# as a fact about the committed files: a reviewer pasted a live
# fetch("https://nominatim.openstreetmap.org/...") back into
# runPlaceSearch and the full suite passed unchanged, 329 green, because
# every other test only exercises behaviour through a stub and none of
# them would ever actually reach a real fetch call to notice it was
# there. This greps the committed source itself, so a stray URL is
# caught even in a code path no test happens to execute.
#
# Scoped to files this project authors (STATIC_DIR, excluding vendor/):
# Leaflet's own source is third-party and not written here, and auditing
# its comments or sourcemap references for incidental URL-shaped strings
# is a different question from the one this test asks, which is whether
# OUR code calls out to an unexpected host. .svg and .json are scanned
# too: .svg is XML text the server serves and could carry an embedded
# <image href="https://..."> the same as HTML can, and .json is served by
# _serve_static the same as any other extension even though nothing under
# static/ happens to be JSON today (like .svg, this is inert until one
# exists, which is fine: the point is that adding one does not also
# require remembering to extend this list). .png is not scanned, since a
# raster image is binary with nothing meaningfully greppable as a URL in
# the sense that anything in this codebase's rendering path would ever
# fetch from.
#
# This catches an honestly reintroduced call, the actual regression that
# happened here, and a protocol-relative host in any of its ordinary
# forms: quoted ("//host", '//host', `//host`), an unquoted HTML
# attribute (src=//host), or an unquoted CSS url(//host). Those three are
# honest, common ways to write a URL, not concealment, so leaving them
# unmatched was a gap more likely to be hit by accident than found by
# someone hiding something. It is still not a security boundary against
# actual concealment: string concatenation split across literals
# ("https:/" + "/host") and a template literal with the hostname held in
# a variable rather than written out both defeat any text-level check,
# since in the second case the actual host is not present as text in this
# file at all until the page runs. Closing those needs evaluating the
# script, not grepping it, which is a different and much larger tool than
# a regression guard for an honest mistake warrants.

_ALLOWED_STATIC_HOSTS = {"tile.openstreetmap.org"}
# Matches a URL host after "http(s)://" anywhere in the text (deliberately
# unanchored: this is what already catches a form action, a CSS url(...),
# or an ES import, none of which need their own special case, simply
# because the scheme makes the reference unambiguous wherever it appears),
# or after a bare "//" opened by a quote, a backtick, an unescaped "(" (an
# unquoted CSS url(//host)), or "=" (an unquoted HTML src=//host). The
# anchor set is what stops this also matching "//" as it starts an
# ordinary line comment, which every file here otherwise has many of.
#
# Whitespace is allowed between a quote or a "(" and the "//", because a
# browser resolves url( //host ) and "   //host" exactly as it resolves the
# tight forms. It is deliberately NOT allowed after "=": in JavaScript,
# `const x = //comment` is an assignment followed by a line comment, and
# tolerating a gap there would report the comment text as a hostname. That
# leaves `src = //host` in HTML, with spaces around the equals, unmatched.
# It is legal markup and would evade this guard, but it is rare enough in
# hand-written HTML to be worth less than the false failures the looser
# pattern would cause in every JavaScript file here.
_URL_HOST_RE = re.compile(
    r'(?:https?:|["\'`(]\s*|=)//([a-zA-Z0-9.-]+)', re.IGNORECASE
)


def _authored_static_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.html", "*.css", "*.js", "*.svg", "*.json"):
        files.extend(STATIC_DIR.rglob(pattern))
    return sorted(p for p in files if "vendor" not in p.relative_to(STATIC_DIR).parts)


def test_authored_static_files_reference_no_host_outside_the_tile_allowlist():
    files = _authored_static_files()
    assert files, "expected at least one authored static file to scan"
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for host in _URL_HOST_RE.findall(text):
            if host not in _ALLOWED_STATIC_HOSTS:
                offenders.append(f"{path.relative_to(STATIC_DIR)}: {host}")
    assert not offenders, f"found references to disallowed hosts: {offenders}"


# --- the vendor README's recorded hashes must stay true ---------------------
#
# .gitattributes guarantees the committed bytes of vendor/leaflet.js and
# vendor/leaflet.css survive a checkout unchanged; nothing guarantees the
# SHA256 table in vendor/README.md still describes those same bytes after
# some future commit touches either file. This hashes the files as
# actually committed and checks them against the table, parsed out of the
# README rather than duplicated here: the README is the single source of
# truth this test exists to keep honest, so a second, hand-copied set of
# hashes in the test would just be one more place to forget to update.
#
# Driven off the actual files on disk, not off the README's rows: an
# earlier version of this test only walked the parsed table, so deleting
# or reformatting a row (the leaflet.js one, say) past what the regex
# recognises would have left that file listed nowhere and checked by
# nothing, and a tampered file with no surviving row would pass silently.
# Every file actually in vendor/ (other than the README itself) must have
# a row before any hash is even compared.

_VENDOR_HASH_ROW_RE = re.compile(
    r"^\|\s*`([^`]+)`\s*\|.*\|\s*`([0-9a-f]{64})`\s*\|\s*$", re.IGNORECASE
)


def _vendor_readme_hash_table() -> dict[str, str]:
    text = (STATIC_DIR / "vendor" / "README.md").read_text(encoding="utf-8")
    table = {}
    for line in text.splitlines():
        match = _VENDOR_HASH_ROW_RE.match(line.strip())
        if match:
            table[match.group(1)] = match.group(2).lower()
    return table


def _vendor_files_on_disk() -> list[str]:
    vendor_dir = STATIC_DIR / "vendor"
    return sorted(p.name for p in vendor_dir.iterdir() if p.is_file() and p.name != "README.md")


def test_every_vendor_file_is_listed_in_the_readme_and_hashes_match():
    table = _vendor_readme_hash_table()
    assert table, "expected vendor/README.md to list at least one file/hash pair"
    files = _vendor_files_on_disk()
    assert files, "expected at least one vendored file on disk"

    unlisted = [name for name in files if name not in table]
    assert not unlisted, (
        f"these vendor files exist on disk but have no row in vendor/README.md, "
        f"so tampering with them would not be caught: {unlisted}"
    )

    mismatches = []
    for filename in files:
        expected_hash = table[filename]
        actual_hash = hashlib.sha256((STATIC_DIR / "vendor" / filename).read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            mismatches.append(
                f"{filename}: README says {expected_hash}, actual file hashes to {actual_hash}"
            )
    assert not mismatches, "\n".join(mismatches)


# --- Task 19 item 4: the windowless launch stops when the page closes -----
#
# Two layers, tested separately: _watch_heartbeat's own timing logic
# (fast, deterministic, using a short real timeout and a short real poll
# interval rather than the 20s/1s production values), and the real HTTP
# routes (/api/heartbeat, /api/shutdown) plus serve()'s own wiring of the
# heartbeat_timeout_seconds parameter end to end.


def test_heartbeat_endpoint_requires_a_token(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            urllib.request.Request(f"{server}/api/heartbeat", method="POST"), timeout=10
        )
    assert excinfo.value.code == 403


def test_heartbeat_endpoint_updates_last_heartbeat_at():
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        before = httpd.last_heartbeat_at
        time.sleep(0.05)
        status, payload = _post(base, "/api/heartbeat", {})
        assert status == 200
        assert payload == {"ok": True}
        assert httpd.last_heartbeat_at > before
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_shutdown_endpoint_requires_a_token(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            urllib.request.Request(f"{server}/api/shutdown", method="POST"), timeout=10
        )
    assert excinfo.value.code == 403


def test_shutdown_endpoint_without_a_token_has_no_side_effects(server):
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(
            urllib.request.Request(f"{server}/api/shutdown", method="POST"), timeout=10
        )
    # If the unauthorised attempt had actually triggered a shutdown, this
    # legitimate, authorised follow-up would fail to connect at all,
    # the same technique already used to prove the same property for
    # /api/jobs.
    status, _payload = _get(server, "/api/config")
    assert status == 200


def test_shutdown_endpoint_stops_the_server():
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, payload = _post(base, "/api/shutdown", {})
        assert status == 200
        assert payload == {"stopping": True}
        thread.join(timeout=5)
        assert not thread.is_alive(), "expected serve_forever() to return after /api/shutdown"
    finally:
        httpd.server_close()


def test_build_server_initialises_last_heartbeat_at_before_any_ping():
    httpd = build_server(port=0, token=TOKEN)
    try:
        assert httpd.last_heartbeat_at is not None
        assert time.monotonic() - httpd.last_heartbeat_at < 5.0
    finally:
        httpd.server_close()


def test_watch_heartbeat_shuts_down_the_server_once_the_timeout_elapses_with_no_pings():
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    stop_event = threading.Event()
    watchdog = threading.Thread(
        target=_watch_heartbeat, args=(httpd, 0.05, stop_event, 0.01), daemon=True
    )
    watchdog.start()
    try:
        thread.join(timeout=5)
        assert not thread.is_alive(), "expected the heartbeat timeout to shut the server down"
    finally:
        stop_event.set()
        httpd.server_close()


def test_watch_heartbeat_does_not_shut_down_while_pings_keep_arriving():
    # The "survives a reload" property: as long as SOMETHING keeps
    # last_heartbeat_at recent, the watchdog must never fire, no matter
    # how long the server has been up in total.
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    stop_event = threading.Event()
    watchdog = threading.Thread(
        target=_watch_heartbeat, args=(httpd, 0.1, stop_event, 0.02), daemon=True
    )
    watchdog.start()
    try:
        deadline = time.time() + 0.4  # several multiples of the 0.1s timeout
        while time.time() < deadline:
            httpd.last_heartbeat_at = time.monotonic()
            time.sleep(0.02)
        assert thread.is_alive(), "expected the server to still be running while pings kept arriving"
    finally:
        stop_event.set()
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_a_reload_sized_heartbeat_gap_does_not_kill_a_job_genuinely_still_running(tmp_path):
    # The brief's own named requirement, tested literally rather than only
    # inferred from the timing tests above: a real job, started through the
    # real HTTP route and JobManager exactly as a browser would, must run
    # to completion even though the heartbeat watchdog is live and several
    # reload-sized gaps pass while it is still blocked in fetch(). The
    # watchdog's own timeout (0.15s) is deliberately much shorter than the
    # blocking source's own hold time, so a broken implementation that
    # shut the server down mid-job would fail this well before the source
    # is ever released.
    blocking = BlockingSource()
    clear_registry()
    register(blocking)
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    stop_event = threading.Event()
    watchdog = threading.Thread(
        target=_watch_heartbeat, args=(httpd, 0.15, stop_event, 0.02), daemon=True
    )
    watchdog.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, payload = _post(
            base,
            "/api/jobs",
            {
                "bbox": "-3.29,51.38,-3.28,51.39",
                "region": "South Wales",
                "site": "Barry",
                "output_root": str(tmp_path),
                "sources": ["blocking"],
                "run_bridge": False,
            },
        )
        assert status == 202
        job_id = payload["id"]
        assert blocking.started.wait(timeout=5)

        # Several reload-sized gaps, each shorter than the 0.15s timeout,
        # spanning a total well past it: exactly the "page reloads a few
        # times while a big download keeps running in the background"
        # scenario, not one lucky ping.
        for _ in range(6):
            status, _ = _post(base, "/api/heartbeat", {})
            assert status == 200
            time.sleep(0.08)
        assert thread.is_alive(), "expected the server to survive reload-sized gaps"
        assert httpd.server_address, "server must still be bound, not shut down"

        blocking.release.set()
        final = _wait_for_state(base, job_id)
        assert final["state"] == "done", f"expected the job to finish cleanly, got {final}"
    finally:
        stop_event.set()
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
        clear_registry()


def test_watch_heartbeat_stops_promptly_once_told_to_via_stop_event():
    # Proves the interruptible-sleep property directly: a stop_event set
    # immediately must not leave this thread polling a server that
    # something else is already closing down.
    httpd = build_server(port=0, token=TOKEN)
    stop_event = threading.Event()
    watchdog = threading.Thread(
        target=_watch_heartbeat, args=(httpd, 100.0, stop_event, 0.01), daemon=True
    )
    watchdog.start()
    stop_event.set()
    watchdog.join(timeout=2)
    assert not watchdog.is_alive(), "expected the watchdog thread to exit promptly once stopped"
    httpd.server_close()


def test_serve_never_auto_shuts_down_when_heartbeat_timeout_is_not_given(monkeypatch):
    # The default: a plain `mapgen ui` must keep behaving exactly as it
    # does today, console and all, closed tab or not.
    import mapgen.web.server as server_module

    captured = {}
    real_build_server = server_module.build_server

    def capturing_build_server(*args, **kwargs):
        httpd = real_build_server(*args, **kwargs)
        captured["httpd"] = httpd
        return httpd

    monkeypatch.setattr(server_module, "build_server", capturing_build_server)

    thread = threading.Thread(
        target=server_module.serve,
        kwargs={"open_browser": False, "port": 0, "heartbeat_timeout_seconds": None},
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 5
    while "httpd" not in captured and time.time() < deadline:
        time.sleep(0.01)
    assert "httpd" in captured, "expected serve() to have built a server by now"
    time.sleep(0.2)
    assert thread.is_alive(), "expected no auto-shutdown with heartbeat_timeout_seconds=None"
    captured["httpd"].shutdown()
    thread.join(timeout=5)


def test_serve_auto_shuts_down_end_to_end_when_the_heartbeat_times_out(capsys):
    # The real, public entry point the CLI's --windowless flag calls,
    # proving the parameter is actually wired through to the watchdog,
    # not just that _watch_heartbeat works in isolation.
    thread = threading.Thread(
        target=serve,
        kwargs={"open_browser": False, "port": 0, "heartbeat_timeout_seconds": 0.05},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "expected serve() to return once the heartbeat timed out"
    # Found by running this for real, launched windowless with no browser
    # ever opened against it: it exited cleanly (confirmed by the process
    # disappearing) but the log file never said why, indistinguishable
    # from a silent crash short of noticing the process itself was gone.
    out = capsys.readouterr().out
    assert "Stopped" in out, f"expected an explanatory line on a watchdog-triggered exit, got: {out!r}"
