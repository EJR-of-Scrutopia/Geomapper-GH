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

// Place search and reverse-geocode name suggestions would normally call
// Nominatim (nominatim.openstreetmap.org) from the page. That host is not
// the map tile server this interface is allowed to reach, so the field
// stays in the layout, its id and placeholder are part of the fixed
// markup, but is never wired to a network call. Region and site are always
// typed by hand; see the task report for why this differs from the sample.
$("place").addEventListener("change", () => {
  const query = $("place").value.trim();
  if (!query) return;
  log(
    "Place search is disabled: this build only talks to localhost and the map tile servers. Draw the extent or paste coordinates instead.",
    true
  );
});

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
