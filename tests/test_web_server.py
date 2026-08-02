import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from mapgen.geo import BBox
from mapgen.jobs import EventLog
from mapgen.package import SurveyRequest
from mapgen.sources.base import Estimate, clear_registry, register
from mapgen.web.server import (
    JobBusyError,
    JobManager,
    JobRecord,
    build_server,
    make_handler,
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

    def merge(self, parts, out_dir):
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

    def merge(self, parts, out_dir):
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
