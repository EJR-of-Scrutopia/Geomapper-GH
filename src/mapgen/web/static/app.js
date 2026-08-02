const token = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);

let bbox = null;
let rectangle = null;
let drawing = false;
let jobId = null;
let poller = null;

const map = L.map("map").setView([51.48, -3.18], 11);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

// --- API -------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(`${path}?token=${encodeURIComponent(token)}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

// --- Nominatim ---------------------------------------------------------
//
// Place search and the region/site auto-suggest both call Nominatim
// (nominatim.openstreetmap.org). That is a second host beyond the map
// tile servers, but Nominatim is OpenStreetMap's own geocoder, the same
// project as the basemap tiles, so it is treated as within the same
// allowance. Its usage policy is binding and is enforced here, not just
// noted:
//
// - At most one request per second. nominatimFetch() enforces this across
//   both callers with a single shared reservation clock, since the policy
//   limits total load on their server, not a per-feature budget. The
//   reservation is made synchronously, before the wait, so two calls
//   arriving together queue one after another instead of both reading the
//   same stale "ready at" time and firing together.
// - The place field is additionally debounced (see placeDebounce below)
//   so committing a search cannot itself fire in a tight burst, for
//   example from mashing Enter. It already only listens for "change", not
//   "input", so it was never firing on every keystroke.
// - Identify the calling application. The policy accepts a descriptive
//   User-Agent or a valid HTTP Referer. Browsers do not let a script set
//   the User-Agent header (it is a forbidden header name in the Fetch
//   standard; setting it is silently dropped and the browser's own value
//   goes out unchanged, on every current engine), so this relies on
//   Referer instead. referrerPolicy is pinned to "origin" so only
//   http://127.0.0.1:<port> is ever sent, never the page's full URL. The
//   full URL carries the per-launch token in its query string, and that
//   must never reach a third party.
// - Never let the interface break because Nominatim is slow, down, or
//   rate-limiting us. Both call sites below fail into a plain, recoverable
//   state instead of an unhandled rejection, and a request that just never
//   answers is cut off by NOMINATIM_TIMEOUT_MS rather than left to hang
//   forever: a slow server should look like a failure, not a stall.

const NOMINATIM_MIN_INTERVAL_MS = 1000;
const NOMINATIM_TIMEOUT_MS = 8000;
let nominatimReadyAt = 0;

async function nominatimFetch(url) {
  const now = Date.now();
  const wait = Math.max(0, nominatimReadyAt - now);
  nominatimReadyAt = now + wait + NOMINATIM_MIN_INTERVAL_MS;
  if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), NOMINATIM_TIMEOUT_MS);
  try {
    const response = await fetch(url, { referrerPolicy: "origin", signal: controller.signal });
    if (!response.ok) throw new Error(`Nominatim returned HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

// --- extent selection ------------------------------------------------

function setBBox(next, fit = true) {
  bbox = next;
  if (rectangle) map.removeLayer(rectangle);
  const bounds = [
    [bbox.south, bbox.west],
    [bbox.north, bbox.east],
  ];
  rectangle = L.rectangle(bounds, { color: "#2f5d4f", weight: 2, fillOpacity: 0.08 });
  rectangle.addTo(map);
  if (fit) map.fitBounds(bounds);
  $("bbox").value = `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`;
  suggestNames();
  refreshEstimate();
}

$("draw").addEventListener("click", () => {
  drawing = true;
  $("draw").textContent = "Click two corners";
  map.getContainer().style.cursor = "crosshair";
});

let firstCorner = null;
map.on("click", (event) => {
  if (!drawing) return;
  if (!firstCorner) {
    firstCorner = event.latlng;
    return;
  }
  const a = firstCorner;
  const b = event.latlng;
  setBBox(
    {
      west: Math.min(a.lng, b.lng),
      south: Math.min(a.lat, b.lat),
      east: Math.max(a.lng, b.lng),
      north: Math.max(a.lat, b.lat),
    },
    false
  );
  firstCorner = null;
  drawing = false;
  $("draw").textContent = "Draw extent";
  map.getContainer().style.cursor = "";
});

$("bbox").addEventListener("change", () => {
  const parts = $("bbox").value.split(",").map((v) => parseFloat(v.trim()));
  if (parts.length !== 4 || parts.some(Number.isNaN)) {
    showEstimateError("Paste four numbers: west,south,east,north");
    return;
  }
  const [w, s, e, n] = parts;
  setBBox({
    west: Math.min(w, e),
    south: Math.min(s, n),
    east: Math.max(w, e),
    north: Math.max(s, n),
  });
});

const PLACE_DEBOUNCE_MS = 400;
let placeDebounce = null;

$("place").addEventListener("change", () => {
  clearTimeout(placeDebounce);
  const query = $("place").value.trim();
  if (!query) return;
  placeDebounce = setTimeout(() => runPlaceSearch(query), PLACE_DEBOUNCE_MS);
});

async function runPlaceSearch(query) {
  try {
    const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(query)}`;
    const [hit] = await nominatimFetch(url);
    if (!hit) return showEstimateError(`No match for "${query}"`);
    const [south, north, west, east] = hit.boundingbox.map(parseFloat);
    setBBox({ west, south, east, north });
  } catch (error) {
    // Nominatim down, rate-limited, or unreachable. The map is exactly as
    // usable as before the search: draw the extent or paste coordinates.
    showEstimateError("Place search is unavailable right now. Draw the extent or paste coordinates instead.");
  }
}

// Auto-suggest region and site from the box centre. Never overwrites
// typing. A failed or rate-limited lookup leaves the fields blank for the
// user to fill by hand rather than surfacing as an error: this runs in
// the background on every extent change, not on an action the user is
// explicitly waiting on. requestedBBox is captured up front and checked
// again after the (possibly queued, possibly slow) request returns, so a
// reply for an extent the user has since replaced can't overwrite a
// newer, already-filled suggestion.
async function suggestNames() {
  if (!bbox) return;
  if ($("region").value && $("site").value) return;
  const requestedBBox = bbox;
  const lat = (requestedBBox.south + requestedBBox.north) / 2;
  const lon = (requestedBBox.west + requestedBBox.east) / 2;
  try {
    const url = `https://nominatim.openstreetmap.org/reverse?format=json&zoom=12&lat=${lat}&lon=${lon}`;
    const data = await nominatimFetch(url);
    if (bbox !== requestedBBox) return;
    const a = data.address || {};
    if (!$("site").value) $("site").value = a.suburb || a.town || a.village || a.city || "";
    if (!$("region").value) $("region").value = a.county || a.state_district || a.state || "";
  } catch (error) {
    // Quiet on purpose, see the comment above.
  }
}

// --- estimate --------------------------------------------------------

function payload() {
  return {
    bbox: `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`,
    region: $("region").value.trim(),
    site: $("site").value.trim(),
    output_root: $("output-root").value.trim(),
    tile_size_m: parseFloat($("tile-size").value),
    overlap_m: parseFloat($("overlap").value),
    keep_work: $("keep-work").checked,
    sources: [...document.querySelectorAll("#sources input:checked")].map((i) => i.value),
  };
}

function showEstimateError(message) {
  const box = $("estimate");
  box.className = "estimate error";
  box.textContent = message;
  $("download").disabled = true;
}

async function refreshEstimate() {
  if (!bbox || !$("region").value.trim() || !$("site").value.trim()) {
    $("estimate").className = "estimate";
    $("estimate").textContent = "Enter a region and site to see an estimate.";
    $("download").disabled = true;
    return;
  }
  try {
    const data = await api("/api/estimate", {
      method: "POST",
      body: JSON.stringify(payload()),
    });
    const minutes = Math.max(1, Math.round(data.seconds_estimate / 60));
    $("estimate").className = "estimate";
    $("estimate").innerHTML =
      `<strong>${data.extent_km.width.toFixed(2)} x ${data.extent_km.height.toFixed(2)} km</strong><br />` +
      `${data.tiles} tiles (${data.rows} x ${data.cols})<br />` +
      `around ${Math.round(data.bytes_estimate / 1e6)} MB, about ${minutes} min`;
    $("download").disabled = false;
  } catch (error) {
    showEstimateError(error.message);
  }
}

["region", "site", "tile-size", "overlap", "output-root"].forEach((id) =>
  $(id).addEventListener("change", refreshEstimate)
);

// --- settings persistence ---------------------------------------------
//
// GET /api/config only runs once, at boot, so without this the output
// root, tile size and overlap would reset to whatever is on disk every
// time the page reloads. PUT /api/config is partial by design: only keys
// that actually changed since the last known saved state are sent here,
// never the whole object.
//
// Every numeric value is sent as an actual JS number, never the raw
// string sitting in an <input>.value. That matters because the endpoint
// applies whatever it is given with no type check of its own (a known,
// separately tracked defect, not fixed here): a string would round-trip
// into config.json as "2000" instead of 2000.0, and load_config() would
// then reject the type mismatch on the next launch and silently fall back
// to the class default, with no error surfaced anywhere. Sending real
// numbers, and dropping a NaN/Infinity rather than sending it, keeps this
// build from ever triggering that.

let savedConfig = null;

async function persistConfig(changes) {
  if (savedConfig === null) return; // boot() has not finished loading yet
  const diff = {};
  for (const [key, value] of Object.entries(changes)) {
    if (typeof value === "number" && !Number.isFinite(value)) continue;
    if (savedConfig[key] !== value) diff[key] = value;
  }
  if (Object.keys(diff).length === 0) return;
  try {
    savedConfig = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify(diff),
    });
  } catch (error) {
    // A failed save only affects the next launch, not this session, so it
    // stays quiet rather than interrupting whatever the user is doing.
  }
}

function persistFieldSettings() {
  persistConfig({
    output_root: $("output-root").value.trim(),
    tile_size_m: parseFloat($("tile-size").value),
    overlap_m: parseFloat($("overlap").value),
  });
}

["tile-size", "overlap", "output-root"].forEach((id) =>
  $(id).addEventListener("change", persistFieldSettings)
);

// --- job -------------------------------------------------------------

function log(message, failed = false) {
  const line = document.createElement("div");
  if (failed) line.className = "fail";
  line.textContent = message;
  $("log").appendChild(line);
  $("log").scrollTop = $("log").scrollHeight;
}

$("download").addEventListener("click", async () => {
  $("log").innerHTML = "";
  try {
    const started = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify(payload()),
    });
    jobId = started.id;
    persistConfig({ last_region: $("region").value.trim() });
    $("download").disabled = true;
    $("cancel").hidden = false;
    let seen = 0;
    poller = setInterval(async () => {
      const job = await api(`/api/jobs/${jobId}`);
      job.events.slice(seen).forEach((e) => {
        const detail = Object.entries(e)
          .filter(([k]) => k !== "event")
          .map(([k, v]) => `${k}=${v}`)
          .join(" ");
        log(`${e.event} ${detail}`.trim());
      });
      seen = job.events.length;
      if (job.state !== "running") {
        clearInterval(poller);
        $("cancel").hidden = true;
        $("download").disabled = false;
        if (job.state === "done") log(`Finished: ${job.result_root}`);
        else log(`${job.state}: ${job.error || ""}`, true);
      }
    }, 700);
  } catch (error) {
    showEstimateError(error.message);
  }
});

$("cancel").addEventListener("click", async () => {
  if (jobId) await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
});

// --- boot ------------------------------------------------------------

(async function boot() {
  const config = await api("/api/config");
  $("output-root").value = config.output_root;
  $("tile-size").value = config.tile_size_m;
  $("overlap").value = config.overlap_m;
  if (config.last_region) $("region").value = config.last_region;
  savedConfig = config;

  const sources = await api("/api/sources");
  $("sources").innerHTML = sources
    .map(
      (s) => `
      <label title="${s.licence}">
        <input type="checkbox" value="${s.id}" ${s.id === "elevation" ? "" : "checked"} />
        <span>${s.display_name}${s.requires_api_key ? " (needs an API key)" : ""}</span>
      </label>`
    )
    .join("");
  $("sources").addEventListener("change", refreshEstimate);
})();
