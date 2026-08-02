"""Local HTTP server for the map picker.

Binds loopback only and requires a per-launch token, so nothing else on the
machine can drive a job. One job at a time, because concurrent Overpass jobs
from a single machine are how you get rate limited.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mapgen.config import load_config, save_config
from mapgen.geo import BBox, BBoxError
from mapgen.jobs import CancelToken, Cancelled, EventLog
from mapgen.naming import NamingError
from mapgen.package import (
    SurveyRequest,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import available_sources

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Matches /api/jobs/<id> and /api/jobs/<id>/cancel. The id itself is anything
# without a slash, so a stray extra segment falls through to the 404 branch
# instead of being sliced out by position and raising IndexError on a short,
# malformed path such as /api/cancel.
_JOB_STATUS_RE = re.compile(r"^/api/jobs/([^/]+)$")
_JOB_CANCEL_RE = re.compile(r"^/api/jobs/([^/]+)/cancel$")


class JobBusyError(RuntimeError):
    """Raised when a second job is requested while one is running."""


@dataclass
class JobRecord:
    id: str
    state: str = "running"
    log: EventLog = field(default_factory=EventLog)
    error: str | None = None
    result_root: str | None = None
    cancel: CancelToken = field(default_factory=CancelToken)


class JobManager:
    """Runs at most one survey job at a time in a background thread.

    A job's events are written by that worker thread and read by whichever
    HTTP handler thread answers a status poll. JobRecord holds the EventLog
    itself, never a second, unguarded copy of its events, so every read goes
    through EventLog's own lock via snapshot(). See EventLog for the reason
    that matters.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._busy = False
        self._lock = threading.Lock()

    def is_busy(self) -> bool:
        return self._busy

    def ensure_free(self) -> None:
        if self._busy:
            raise JobBusyError(
                "A download is already running. Wait for it to finish or cancel it."
            )

    def start(self, request: SurveyRequest) -> str:
        with self._lock:
            self.ensure_free()
            self._busy = True

        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(id=job_id)
        self._jobs[job_id] = record

        def worker() -> None:
            try:
                result = run_survey(
                    request, progress=record.log, cancel=record.cancel
                )
                record.result_root = str(result.paths.root)
                record.state = "done" if result.complete else "failed"
                if not result.complete:
                    record.error = "Some tiles failed. See survey.json."
            except Cancelled:
                record.state = "cancelled"
            except Exception as exc:
                record.state = "failed"
                record.error = str(exc)
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()
        return job_id

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        record = self._jobs.get(job_id)
        if record is None:
            return False
        record.cancel.cancel()
        return True


def _survey_request(payload: dict) -> SurveyRequest:
    return SurveyRequest(
        bbox=BBox.parse(payload["bbox"]),
        region=payload["region"],
        site=payload["site"],
        output_root=Path(payload["output_root"]),
        tile_size_m=float(payload.get("tile_size_m", 2000.0)),
        overlap_m=float(payload.get("overlap_m", 100.0)),
        source_ids=tuple(payload.get("sources") or ("osm", "overture")),
        keep_work=bool(payload.get("keep_work", False)),
        coordinate_stem=bool(payload.get("coordinate_stem", False)),
        run_bridge_step=bool(payload.get("run_bridge", True)),
    )


def make_handler(manager: JobManager, token: str, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the console clean
            return

        # --- helpers -------------------------------------------------
        def _send_json(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self, query: dict) -> bool:
            return query.get("token", [None])[0] == token

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        # --- routes --------------------------------------------------
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            if not parsed.path.startswith("/api/"):
                return self._serve_static(parsed.path)

            if not self._authorised(query):
                return self._send_json(403, {"error": "Invalid or missing token."})

            if parsed.path == "/api/config":
                config = load_config()
                return self._send_json(200, config.__dict__)

            if parsed.path == "/api/sources":
                return self._send_json(
                    200,
                    [
                        {
                            "id": s.id,
                            "display_name": s.display_name,
                            "licence": s.licence,
                            "requires_api_key": s.requires_api_key,
                        }
                        for s in available_sources()
                    ],
                )

            match = _JOB_STATUS_RE.match(parsed.path)
            if match:
                record = manager.get(match.group(1))
                if record is None:
                    return self._send_json(404, {"error": "Unknown job."})
                return self._send_json(
                    200,
                    {
                        "id": record.id,
                        "state": record.state,
                        "events": record.log.snapshot(),
                        "error": record.error,
                        "result_root": record.result_root,
                    },
                )

            return self._send_json(404, {"error": "Unknown endpoint."})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._authorised(query):
                return self._send_json(403, {"error": "Invalid or missing token."})

            try:
                payload = self._read_json()
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "Body was not valid JSON."})

            if parsed.path == "/api/estimate":
                try:
                    return self._send_json(200, estimate_survey(_survey_request(payload)))
                except (BBoxError, NamingError, KeyError) as exc:
                    return self._send_json(400, {"error": str(exc)})

            if parsed.path == "/api/jobs":
                try:
                    job_id = manager.start(_survey_request(payload))
                except JobBusyError as exc:
                    return self._send_json(409, {"error": str(exc)})
                except (BBoxError, NamingError, KeyError) as exc:
                    return self._send_json(400, {"error": str(exc)})
                return self._send_json(202, {"id": job_id})

            match = _JOB_CANCEL_RE.match(parsed.path)
            if match:
                if not manager.cancel(match.group(1)):
                    return self._send_json(404, {"error": "Unknown job."})
                return self._send_json(200, {"cancelled": True})

            return self._send_json(404, {"error": "Unknown endpoint."})

        def do_PUT(self) -> None:
            parsed = urlparse(self.path)
            if not self._authorised(parse_qs(parsed.query)):
                return self._send_json(403, {"error": "Invalid or missing token."})
            if parsed.path != "/api/config":
                return self._send_json(404, {"error": "Unknown endpoint."})

            try:
                payload = self._read_json()
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "Body was not valid JSON."})

            current = load_config()
            for key, value in payload.items():
                if hasattr(current, key):
                    setattr(current, key, value)
            save_config(current)
            return self._send_json(200, current.__dict__)

        def _serve_static(self, path: str) -> None:
            relative = "index.html" if path in ("/", "") else path.lstrip("/")
            resolved_static_dir = static_dir.resolve()
            target = (static_dir / relative).resolve()
            if not target.is_relative_to(resolved_static_dir) or not target.is_file():
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            content_types = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }
            body = target.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type", content_types.get(target.suffix, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def build_server(
    host: str = "127.0.0.1", port: int = 0, token: str | None = None
) -> ThreadingHTTPServer:
    register_default_sources()
    resolved_token = token or secrets.token_urlsafe(24)
    manager = JobManager()
    handler = make_handler(manager, resolved_token, STATIC_DIR)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.token = resolved_token
    httpd.manager = manager
    return httpd


def serve(open_browser: bool = True, port: int = 0, host: str = "127.0.0.1") -> None:
    httpd = build_server(host=host, port=port)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/?token={httpd.token}"
    print(f"mapgen UI: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
