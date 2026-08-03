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
from urllib.parse import parse_qs, unquote, urlparse

from mapgen.config import load_config, save_config
from mapgen.geo import BBox, BBoxError, build_tiles
from mapgen.geocode import GeocodeError, GeocodeQueueFullError, NominatimClient
from mapgen.jobs import CancelToken, Cancelled, EventLog
from mapgen.naming import NamingError
from mapgen.package import (
    SurveyRequest,
    estimate_geometry,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import available_sources

# _survey_request() and the bare-geometry parsing in /api/extent both call
# float() on caller-supplied JSON values. A well-behaved client always
# sends a number, but JSON has no NaN/Infinity literal, so
# JSON.stringify(NaN) on the browser side produces the literal text null:
# the key stays present with value None, which dict.get(key, default)
# does not treat as absent, and float(None) raises TypeError, not
# ValueError. Both are request-shaped problems, not server bugs, and
# belong in the same 400 branch as BBoxError/NamingError rather than
# killing the handler thread.
_REQUEST_VALUE_ERRORS = (BBoxError, NamingError, KeyError, TypeError, ValueError)

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

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            # The worker never ran, so its own finally: self._busy = False
            # never fires either. Left alone, this wedges the manager
            # permanently busy with no job to cancel and no way to start
            # another, which needs a process restart to clear.
            self._busy = False
            raise
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
    request = SurveyRequest(
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
    # Validated synchronously, here, rather than left for run_survey to
    # discover inside JobManager's background worker thread. /api/jobs
    # would otherwise accept a request that can never be tiled (Task 18
    # review: tile_size_m <= 0, or an absurd tile count) with a 202 that
    # is certain to fail, only surfacing the problem later as an
    # asynchronous job failure instead of the same synchronous 400
    # /api/estimate gives for the identical input. TilingError is a
    # ValueError, already caught by _REQUEST_VALUE_ERRORS at both call
    # sites below, so this raises the same way BBox.parse already does
    # two lines up.
    build_tiles(request.bbox, request.tile_size_m, request.overlap_m)
    return request


def make_handler(
    manager: JobManager,
    token: str,
    static_dir: Path,
    geocode_client: NominatimClient | None = None,
):
    geocode_client = geocode_client if geocode_client is not None else NominatimClient()

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

        def _drain_body(self) -> bytes:
            """Read and return the full request body, always, before any
            routing or auth decision.

            protocol_version is HTTP/1.1, so a client (Task 16's browser
            fetch(), which reuses connections; the test suite's raw
            http.client checks below) may send another request on the same
            socket right after this one. If a route returns early, 403 for a
            bad token or 404 for an unknown path, without reading a body the
            client already sent, those bytes are still sitting on the wire
            and get parsed as the start of the next request on that same
            connection. Draining unconditionally here, before any branch
            that could return early, closes that off for every route at
            once rather than needing every early return to remember it.
            """
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                # An unparseable Content-Length means the number of pending
                # body bytes is unknown, so there is no safe amount to read
                # for a connection that is about to be reused. Close it
                # instead of guessing and desyncing the next request anyway.
                self.close_connection = True
                return b""
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _parse_json(self, body: bytes) -> dict:
            if not body:
                return {}
            return json.loads(body.decode("utf-8"))

        # --- routes --------------------------------------------------
        def do_GET(self) -> None:
            self._drain_body()
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

            if parsed.path == "/api/geocode":
                search_query = (query.get("q", [None])[0] or "").strip()
                if not search_query:
                    return self._send_json(400, {"error": "q is required."})
                try:
                    results = geocode_client.search(search_query)
                except GeocodeQueueFullError as exc:
                    return self._send_json(429, {"error": str(exc)})
                except GeocodeError as exc:
                    return self._send_json(502, {"error": str(exc)})
                if not results:
                    return self._send_json(
                        404, {"error": f'No match for "{search_query}"'}
                    )
                # A list, one entry per hit, each with display_name plus a
                # bbox: Task 18 turned this from a one-shot search that
                # jumped straight to a single guess into a typeahead,
                # which needs enough to tell same-named places apart.
                return self._send_json(
                    200,
                    [
                        {
                            "display_name": result.display_name,
                            "west": result.west,
                            "south": result.south,
                            "east": result.east,
                            "north": result.north,
                            "region": result.region,
                            "site": result.site,
                        }
                        for result in results
                    ],
                )

            if parsed.path == "/api/reverse":
                try:
                    lat = float(query.get("lat", [None])[0])
                    lon = float(query.get("lon", [None])[0])
                except (TypeError, ValueError):
                    return self._send_json(400, {"error": "lat and lon must both be numbers."})
                # float() accepts "nan", "inf" and overflowing literals like
                # "1e400" without raising, so a plain try/except above lets
                # every one of those through to spend a rate-limit slot on
                # a request Nominatim was never going to answer. The range
                # check below is what actually stops them, on its own: any
                # comparison against nan is False in Python, so
                # -90.0 <= nan <= 90.0 is already False, and inf/-inf just
                # fail the comparison outright. A separate isfinite() check
                # here was redundant dead weight, asserted by nothing, and
                # is not needed.
                if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
                    return self._send_json(
                        400,
                        {"error": "lat must be between -90 and 90, lon between -180 and 180."},
                    )
                try:
                    reverse_result = geocode_client.reverse(lat, lon)
                except GeocodeQueueFullError as exc:
                    return self._send_json(429, {"error": str(exc)})
                except GeocodeError as exc:
                    return self._send_json(502, {"error": str(exc)})
                return self._send_json(
                    200, {"region": reverse_result.region, "site": reverse_result.site}
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
            body = self._drain_body()
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._authorised(query):
                return self._send_json(403, {"error": "Invalid or missing token."})

            try:
                payload = self._parse_json(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._send_json(400, {"error": "Body was not valid JSON."})

            if parsed.path == "/api/estimate":
                try:
                    return self._send_json(200, estimate_survey(_survey_request(payload)))
                except _REQUEST_VALUE_ERRORS as exc:
                    return self._send_json(400, {"error": str(exc)})

            if parsed.path == "/api/extent":
                # The bare-geometry counterpart to /api/estimate: bbox,
                # tile_size_m and overlap_m only, no region, site or
                # output_root at all, so the interface can show a live
                # tile count and area while a rectangle is being drawn or
                # pasted, well before either name exists.
                try:
                    bbox = BBox.parse(payload["bbox"])
                    tile_size_m = float(payload.get("tile_size_m", 2000.0))
                    overlap_m = float(payload.get("overlap_m", 100.0))
                    return self._send_json(
                        200, estimate_geometry(bbox, tile_size_m, overlap_m)
                    )
                except _REQUEST_VALUE_ERRORS as exc:
                    return self._send_json(400, {"error": str(exc)})

            if parsed.path == "/api/jobs":
                try:
                    job_id = manager.start(_survey_request(payload))
                except JobBusyError as exc:
                    return self._send_json(409, {"error": str(exc)})
                except _REQUEST_VALUE_ERRORS as exc:
                    return self._send_json(400, {"error": str(exc)})
                return self._send_json(202, {"id": job_id})

            match = _JOB_CANCEL_RE.match(parsed.path)
            if match:
                if not manager.cancel(match.group(1)):
                    return self._send_json(404, {"error": "Unknown job."})
                return self._send_json(200, {"cancelled": True})

            return self._send_json(404, {"error": "Unknown endpoint."})

        def do_PUT(self) -> None:
            body = self._drain_body()
            parsed = urlparse(self.path)
            if not self._authorised(parse_qs(parsed.query)):
                return self._send_json(403, {"error": "Invalid or missing token."})
            if parsed.path != "/api/config":
                return self._send_json(404, {"error": "Unknown endpoint."})

            try:
                payload = self._parse_json(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._send_json(400, {"error": "Body was not valid JSON."})

            current = load_config()
            for key, value in payload.items():
                if hasattr(current, key):
                    setattr(current, key, value)
            save_config(current)
            return self._send_json(200, current.__dict__)

        def _serve_static(self, path: str) -> None:
            # unquote first, so a name like map%20pin.svg reaches an actual
            # file called "map pin.svg". The traversal guard below runs on
            # the fully resolved path regardless of how "relative" was
            # spelled, so decoding first does not reopen it: an encoded
            # ../ still collapses under resolve() and still fails
            # is_relative_to same as a literal ../ would.
            decoded = unquote(path)
            relative = "index.html" if decoded in ("/", "") else decoded.lstrip("/")
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
    # One NominatimClient per server process, not per request: its
    # GeocodeRateLimiter must be shared by every request-handling thread
    # for the one-per-second limit to hold across page reloads and across
    # two tabs, which it cannot do if a new one is built per call.
    geocode_client = NominatimClient()
    handler = make_handler(manager, resolved_token, STATIC_DIR, geocode_client)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.token = resolved_token
    httpd.manager = manager
    httpd.geocode_client = geocode_client
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
