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
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

// --- extent selection ------------------------------------------------

// Debounced the same way the place field is (see below): drawing or
// pasting several extents in quick succession would otherwise fire one
// reverse-geocode lookup per change, each queuing for a rate-limit slot
// server-side. Waiting for a quiet moment before actually asking cuts
// that down to the one lookup that matters, the extent the user actually
// settles on.
const SUGGEST_DEBOUNCE_MS = 400;
let suggestDebounce = null;

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
  clearTimeout(suggestDebounce);
  suggestDebounce = setTimeout(suggestNames, SUGGEST_DEBOUNCE_MS);
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

// Place search and the region/site auto-suggest both need a geocoder, but
// neither calls Nominatim directly any more: /api/geocode and
// /api/reverse proxy it server-side instead. A browser script cannot set
// a real User-Agent (it is a forbidden header name in the Fetch
// standard), and a rate limit held in this page's own JavaScript resets
// on every reload and does not exist for a second tab; both are real
// requirements of Nominatim's usage policy, not just politeness, and the
// server, one process shared by every tab and every reload, is the only
// place that can actually enforce them. See mapgen.geocode for the
// enforcement itself, including its own bounded queue: a caller here
// waiting for a rate-limit slot can be told the queue is full (a clean
// 429) rather than queuing indefinitely.
//
// The debounce here is a separate, client-only concern: it stops a
// committed search firing in a burst (mashing Enter), which is not the
// same problem as the server's own one-per-second gate and is not
// replaced by it.
//
// Each function also aborts its own previous, still-in-flight request
// before starting a new one, so a superseded lookup stops this tab
// waiting on it. This is a client-side mitigation only: once a request
// has actually reached the server, that handler thread is already past
// the point where anything on this side of the connection can reach in
// and stop it (Python's http.server has no cooperative cancellation), so
// aborting here does not free the server's queue slot early. What it
// does do is guarantee this tab never has more than one outstanding
// geocode request of a given kind at once, which is what keeps a single
// tab from being the thing that fills the server's queue by itself; the
// queue bound itself, not this, is what actually protects against a
// failing or slow Nominatim piling up work, from any tab.

const PLACE_DEBOUNCE_MS = 400;
let placeDebounce = null;
let placeSearchController = null;

$("place").addEventListener("change", () => {
  clearTimeout(placeDebounce);
  const query = $("place").value.trim();
  if (!query) return;
  placeDebounce = setTimeout(() => runPlaceSearch(query), PLACE_DEBOUNCE_MS);
});

async function runPlaceSearch(query) {
  if (placeSearchController) placeSearchController.abort();
  const controller = new AbortController();
  placeSearchController = controller;
  try {
    const result = await api(`/api/geocode?q=${encodeURIComponent(query)}`, {
      signal: controller.signal,
    });
    setBBox({ west: result.west, south: result.south, east: result.east, north: result.north });
  } catch (error) {
    if (error.name === "AbortError") return; // superseded by a newer search
    if (error.status === 404) {
      // The server's own "no match for ..." message, worth showing as is.
      showEstimateError(error.message);
    } else if (error.status === 429) {
      showEstimateError("Too many geocoding requests right now. Try again in a moment.");
    } else {
      // Geocoding is down, rate-limited, or slow enough to have timed out
      // server-side. The map is exactly as usable as before the search:
      // draw the extent or paste coordinates.
      showEstimateError("Place search is unavailable right now. Draw the extent or paste coordinates instead.");
    }
  } finally {
    if (placeSearchController === controller) placeSearchController = null;
  }
}

// Auto-suggest region and site from the box centre. Never overwrites
// typing. A failed or rate-limited lookup leaves the fields blank for the
// user to fill by hand rather than surfacing as an error: this runs in
// the background on every extent change, not on an action the user is
// explicitly waiting on. requestedBBox is captured up front and checked
// again after the (possibly slow) request returns, so a reply for an
// extent the user has since replaced can't overwrite a newer,
// already-filled suggestion.
let suggestController = null;

async function suggestNames() {
  if (!bbox) return;
  if ($("region").value && $("site").value) return;
  const requestedBBox = bbox;
  const lat = (requestedBBox.south + requestedBBox.north) / 2;
  const lon = (requestedBBox.west + requestedBBox.east) / 2;
  if (suggestController) suggestController.abort();
  const controller = new AbortController();
  suggestController = controller;
  try {
    const result = await api(`/api/reverse?lat=${lat}&lon=${lon}`, { signal: controller.signal });
    if (bbox !== requestedBBox) return;
    if (!$("site").value) $("site").value = result.site || "";
    if (!$("region").value) $("region").value = result.region || "";
  } catch (error) {
    // Quiet on purpose, see the comment above. Includes an AbortError
    // from a newer extent superseding this one.
  } finally {
    if (suggestController === controller) suggestController = null;
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
    // Only persist output-root/tile-size/overlap once they have actually
    // passed an estimate, which is the same call that would reject, for
    // example, an output root too long for Windows' path limit. Wiring
    // this to the field's own "change" event directly, alongside
    // refreshEstimate, would persist a value the estimate had just
    // rejected: the interface would then reload next launch pre-filled
    // with a path it already knows is broken, with nothing on screen to
    // explain why.
    persistFieldSettings();
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

// No separate "change" listener here: persistFieldSettings is called from
// refreshEstimate's own success path above, deliberately, so a value the
// estimate has just rejected is never the one saved.

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
      let job;
      try {
        job = await api(`/api/jobs/${jobId}`);
      } catch (error) {
        // Without this, a single failed poll (a stale token, the server
        // restarting) becomes an unhandled rejection every 700ms forever:
        // setInterval keeps calling regardless, clearInterval is never
        // reached because it only happens below, and the buttons are left
        // showing a job that is, as far as this tab knows, still running.
        clearInterval(poller);
        $("cancel").hidden = true;
        $("download").disabled = false;
        log(`Lost contact with the job: ${error.message}`, true);
        return;
      }
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
  try {
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
  } catch (error) {
    // Without this, a wrong or stale token throws on the very first await
    // and boot() just stops: no config, no sources, no estimate, and
    // nothing on screen says why. The 403 this is usually caused by is
    // entirely correct server-side and completely invisible otherwise.
    log(
      `Could not load the interface: ${error.message} Reload from the mapgen ui command; the token may be stale.`,
      true
    );
  }
})();
