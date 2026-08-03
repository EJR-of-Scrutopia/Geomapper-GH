const token = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);

// Nominatim display names are untrusted external text that ends up in
// innerHTML rather than textContent. Escaped once, here, rather than
// trusted at each call site.
function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );
}

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

// Every /api/ route needs ?token= on its query string. Building that by
// gluing on "?token=..." with a template literal broke the moment a route
// itself took a query string (/api/geocode?q=..., /api/reverse?lat=&lon=):
// the result was two "?" characters in one URL
// (/api/geocode?q=Barry?token=abc), and a server-side query parser reads
// everything after the FIRST "?" as the query string, so "q" comes out as
// "Barry?token=abc" and "token" does not exist at all. Every geocode and
// reverse call was therefore 403 on every single request, silently, since
// _authorised() just sees a missing token like any other missing token.
// The URL constructor merges parameters correctly regardless of whether
// path already has any, which is what "in one place" means here: every
// caller goes through this one function, so there is nowhere else a route
// with its own query string could make the same mistake again.
async function api(path, options = {}) {
  const url = new URL(path, location.origin);
  url.searchParams.set("token", token);
  let response;
  try {
    response = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (networkError) {
    // fetch() rejects with a bare "Failed to fetch" when it cannot reach
    // the server at all, which reads to the owner as though the tool is
    // broken. It usually means the server has stopped: this page holds a
    // token and a port belonging to one specific run, so a stopped server
    // makes every request here fail permanently, and no amount of
    // retrying or re-ticking anything will help. Say that, and say the
    // one thing that does help, rather than passing the browser's own
    // wording through to a red box.
    const error = new Error(
      "Cannot reach the mapgen server. It has stopped, so this page is now " +
        "out of date. Start mapgen again from the desktop shortcut; your " +
        "settings and API key are saved."
    );
    error.serverGone = true;
    throw error;
  }
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

// --- draw extent: click a corner, move, click the opposite corner ------
//
// The button click only arms the tool; nothing is drawn by pressing it.
// The first map click fixes one corner. Every mousemove after that
// redraws a live preview rectangle from that corner to the cursor, the
// way any ordinary map tool's rubber-band selection behaves; the second
// click fixes the opposite corner and commits it via setBBox, the one
// place that actually replaces the committed extent. The preview
// rectangle is its own separate layer specifically so a cancelled draw
// (Escape) can remove only the preview and never touch whatever extent
// was already committed.

let firstCorner = null;
let previewRectangle = null;

function disarmDrawing() {
  drawing = false;
  firstCorner = null;
  if (previewRectangle) {
    map.removeLayer(previewRectangle);
    previewRectangle = null;
  }
  $("draw").textContent = "Draw extent";
  $("draw").className = "";
  map.getContainer().style.cursor = "";
}

$("draw").addEventListener("click", () => {
  // Always starts from a clean slate: clicking the button again mid-draw
  // discards whatever corner and preview rectangle already existed,
  // rather than leaving them stranded with no way back short of Escape.
  disarmDrawing();
  drawing = true;
  $("draw").textContent = "Click a corner";
  $("draw").className = "armed";
  map.getContainer().style.cursor = "crosshair";
});

map.on("click", (event) => {
  if (!drawing) return;
  if (!firstCorner) {
    firstCorner = event.latlng;
    $("draw").textContent = "Click the opposite corner";
    return;
  }
  const a = firstCorner;
  const b = event.latlng;
  if (a.lat === b.lat || a.lng === b.lng) {
    // A degenerate, zero-width or zero-height box: the server would
    // reject this outright (BBox.validated()), and it is not something
    // the estimate panel can safely surface while names are still
    // incomplete (see refreshEstimate's own /api/extent error handling,
    // fixed alongside this to stop swallowing that case too). Rejected
    // here instead, at the click: disarm and leave whatever extent was
    // already committed untouched, exactly like Escape.
    disarmDrawing();
    return;
  }
  const finished = {
    west: Math.min(a.lng, b.lng),
    south: Math.min(a.lat, b.lat),
    east: Math.max(a.lng, b.lng),
    north: Math.max(a.lat, b.lat),
  };
  disarmDrawing();
  setBBox(finished, false);
});

map.on("mousemove", (event) => {
  if (!drawing || !firstCorner) return;
  const bounds = [
    [firstCorner.lat, firstCorner.lng],
    [event.latlng.lat, event.latlng.lng],
  ];
  if (previewRectangle) {
    previewRectangle.setBounds(bounds);
  } else {
    previewRectangle = L.rectangle(bounds, {
      color: "#2f5d4f",
      weight: 1,
      dashArray: "4",
      fillOpacity: 0.04,
    });
    previewRectangle.addTo(map);
  }
});

// Escape cancels a drawing in progress and disarms, leaving any previous
// extent untouched: disarmDrawing only ever removes the PREVIEW
// rectangle, never the committed one setBBox owns. Bound on document
// rather than the map container, so it fires regardless of which element
// currently has focus, including none.
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && drawing) {
    disarmDrawing();
  }
});

$("bbox").addEventListener("change", () => {
  const parts = $("bbox").value.split(",").map((v) => parseFloat(v.trim()));
  if (parts.length !== 4 || parts.some(Number.isNaN)) {
    showEstimateError("Paste four numbers: west,south,east,north");
    return;
  }
  const [w, s, e, n] = parts;
  if (w === e || s === n) {
    // Same degenerate-box guard as the draw tool's click handler: a
    // pasted "10,20,10,25" is exactly as zero-area as a second click on
    // the first point, and the server would reject it the same way.
    showEstimateError("west/east and south/north must each be different: this box has zero area.");
    return;
  }
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

// Task 18 turned this from a one-shot search (fetch one result, jump) into
// a typeahead: as you type, up to five matches appear below the field and
// narrow with each character. Clicking one, or choosing it with the arrow
// keys and Enter, moves the map and sets the extent. The request shape and
// staleness guards below are unchanged from the one-shot version; only
// what happens with the response (a list to browse, not a single bbox to
// jump to immediately) is new.

const PLACE_DEBOUNCE_MS = 400;
let placeDebounce = null;
let placeSearchController = null;
let placeMatches = [];
let placeHighlighted = -1;

function closePlaceResults() {
  placeMatches = [];
  placeHighlighted = -1;
  const list = $("place-results");
  list.hidden = true;
  list.innerHTML = "";
}

function renderPlaceResults() {
  const list = $("place-results");
  list.innerHTML = placeMatches
    .map(
      (match, index) =>
        `<li data-index="${index}" class="${index === placeHighlighted ? "active" : ""}">` +
        `${escapeHtml(match.display_name)}</li>`
    )
    .join("");
  list.hidden = false;
}

function showPlaceMessage(message) {
  const list = $("place-results");
  list.innerHTML = `<li class="empty">${escapeHtml(message)}</li>`;
  list.hidden = false;
}

function choosePlaceMatch(match) {
  $("place").value = match.display_name;
  closePlaceResults();
  // Fill region/site from the chosen result's own address breakdown
  // before setBBox ever runs. This is better evidence than a reverse
  // lookup on the centroid of the rectangle that follows: the owner
  // picked this specific place by name, so its own address is what the
  // site should be named after, not a second, separately-derived guess
  // at the middle of whatever box it turns into. Guarded the same way
  // suggestNames guards itself: never overwrites something already
  // typed. setBBox's own debounced suggestNames() still runs afterward
  // and fills whichever of the two, if either, this result's address did
  // not have: not a redundant lookup, since it only acts on a field this
  // block left blank.
  if (!$("site").value) $("site").value = match.site || "";
  if (!$("region").value) $("region").value = match.region || "";
  setBBox({ west: match.west, south: match.south, east: match.east, north: match.north });
}

$("place").addEventListener("input", () => {
  clearTimeout(placeDebounce);
  const query = $("place").value.trim();
  if (!query) {
    closePlaceResults();
    return;
  }
  placeDebounce = setTimeout(() => runPlaceSearch(query), PLACE_DEBOUNCE_MS);
});

$("place").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    // Cancels a pending debounced search too, not just the visible list:
    // otherwise a query typed right before Escape still fires moments
    // later with nothing on screen to explain why the map just moved.
    clearTimeout(placeDebounce);
    if (placeSearchController) placeSearchController.abort();
    closePlaceResults();
    return;
  }
  if (!placeMatches.length) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    placeHighlighted = (placeHighlighted + 1) % placeMatches.length;
    renderPlaceResults();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    placeHighlighted = (placeHighlighted - 1 + placeMatches.length) % placeMatches.length;
    renderPlaceResults();
  } else if (event.key === "Enter") {
    event.preventDefault();
    choosePlaceMatch(placeMatches[placeHighlighted >= 0 ? placeHighlighted : 0]);
  }
});

// Clicking away closes the list. A plain blur-closes-immediately would
// fire before a click on a result finishes registering (blur precedes
// click when the click target is outside the field), so this waits a
// moment first; choosePlaceMatch, above, closes the list itself the
// instant a choice is made, so the delay is never visible on the
// successful path.
$("place").addEventListener("blur", () => {
  setTimeout(closePlaceResults, 150);
});

$("place-results").addEventListener("click", (event) => {
  const item = event.target.closest("li[data-index]");
  if (!item) return;
  choosePlaceMatch(placeMatches[Number(item.dataset.index)]);
});

async function runPlaceSearch(query) {
  if (placeSearchController) placeSearchController.abort();
  const controller = new AbortController();
  placeSearchController = controller;
  try {
    const results = await api(`/api/geocode?q=${encodeURIComponent(query)}`, {
      signal: controller.signal,
    });
    placeMatches = results;
    placeHighlighted = -1;
    if (placeMatches.length) {
      renderPlaceResults();
    } else {
      showPlaceMessage(`No matches for "${query}"`);
    }
  } catch (error) {
    if (error.name === "AbortError") return; // superseded by a newer search
    placeMatches = [];
    placeHighlighted = -1;
    if (error.status === 404) {
      // The server's own "no match for ..." message, worth showing as is.
      showPlaceMessage(error.message);
    } else if (error.status === 429) {
      showPlaceMessage("Too many searches right now. Try again in a moment.");
    } else {
      // Geocoding is down, rate-limited, or slow enough to have timed out
      // server-side. The map is exactly as usable as before the search:
      // draw the extent or paste coordinates. Shown in the results list,
      // not the estimate panel: a place-search hiccup should not disable
      // an otherwise-valid download.
      showPlaceMessage("Place search is unavailable right now. Draw the extent or paste coordinates instead.");
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
    // Always the full, current set of checked boxes, never omitted when
    // everything happens to be ticked: the server treats an ABSENT key
    // as "every category" and a genuinely empty array as "none", and
    // only ever sees an absent key from an older client or the CLI
    // without --category, never from this page.
    categories: [...document.querySelectorAll("#categories input:checked")].map((i) => i.value),
    // The OpenTopography key deliberately never travels through here.
    // ElevationSource reads it server-side from the saved config (or the
    // environment) at estimate/fetch time, so it never needs to appear in
    // a job's request payload, and from there never in survey.json or the
    // job log the payload's fields could otherwise end up echoed into.
  };
}

// Task 21: unticking every category checkbox used to be accepted right
// through to the server, which refused it (see mapgen.categories.
// EmptyCategorySelectionError) but only after the owner had already
// pressed Download and seen an error. Checked here instead, alongside
// the extent and the region/site names, so the same missing-fields
// message and disabled Download that already cover a blank site name
// cover this too: the owner never reaches the state where pressing
// Download does anything but disabled sit there.
//
// [...] first: a real browser's querySelectorAll returns a NodeList,
// which has forEach but not every, the same reason payload() below
// spreads #sources and #categories before calling .map on them. Empty
// (boot() has not rendered the checklist yet, or index.html's markup is
// momentarily bare) reads as "nothing to say yet", not "nothing
// selected": the checklist is ticked by default the instant it renders,
// so a genuinely empty list here only ever means boot() has not reached
// GET /api/categories yet, never a deliberate empty selection.
function noCategorySelected() {
  const boxes = [...document.querySelectorAll("#categories input")];
  return boxes.length > 0 && boxes.every((box) => !box.checked);
}

// Names the field or fields actually missing, rather than a fixed message
// regardless of which ones are empty: a region already filled by the
// reverse lookup must not be told to "enter a region" alongside a genuinely
// empty site. Returns null once bbox, region, site and at least one
// category are all present.
function missingFieldsMessage() {
  const clauses = [];
  if (!bbox) clauses.push("draw or paste an extent");
  const namesMissing = [];
  if (!$("region").value.trim()) namesMissing.push("region");
  if (!$("site").value.trim()) namesMissing.push("site");
  if (namesMissing.length) clauses.push(`enter a ${namesMissing.join(" and ")}`);
  if (noCategorySelected()) clauses.push("select at least one category");
  if (!clauses.length) return null;
  const sentence = `${clauses.join(" and ")} to see an estimate.`;
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

function formatGeometryLine(geometry) {
  const area = (geometry.extent_km.width * geometry.extent_km.height).toFixed(2);
  const tileWord = geometry.tiles === 1 ? "tile" : "tiles";
  return `${area} km² · ${geometry.tiles} ${tileWord} (${geometry.rows} x ${geometry.cols})`;
}

function hideFolderPreview() {
  $("folder-preview").hidden = true;
  $("folder-preview-path").textContent = "";
}

function showEstimateError(message) {
  const box = $("estimate");
  box.className = "estimate error";
  box.textContent = message;
  $("download").disabled = true;
  hideFolderPreview();
}

async function refreshEstimate() {
  const missing = missingFieldsMessage();

  if (!bbox) {
    $("estimate").className = "estimate";
    $("estimate").textContent = missing;
    $("download").disabled = true;
    hideFolderPreview();
    return;
  }

  if (missing) {
    // A region or site is still missing, but the extent alone is enough
    // for a live geometry preview: tile count and area, the same numbers
    // a full estimate would report, from the one endpoint that needs no
    // name at all. Drawing a rectangle used to tell you nothing until a
    // region and site were typed; this is what fixes that.
    $("estimate").className = "estimate";
    $("download").disabled = true;
    hideFolderPreview();
    try {
      const geometry = await api("/api/extent", {
        method: "POST",
        body: JSON.stringify({
          bbox: `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`,
          tile_size_m: parseFloat($("tile-size").value),
          overlap_m: parseFloat($("overlap").value),
        }),
      });
      $("estimate").innerHTML = `${formatGeometryLine(geometry)}<br />${escapeHtml(missing)}`;
    } catch (error) {
      // A genuine problem with the extent or tiling itself (an absurd
      // tiling, a zero-area box that slipped through some other path, a
      // network hiccup) must not hide behind the missing-fields message:
      // that previously left a real /api/extent failure looking
      // identical to an ordinary "type a region and site" prompt, with
      // nothing on screen until both names were filled in and a full
      // /api/estimate finally surfaced the same error. Shown alongside
      // the still-true missing-fields line, not instead of it.
      $("estimate").innerHTML = `${escapeHtml(error.message)}<br />${escapeHtml(missing)}`;
    }
    return;
  }

  try {
    const data = await api("/api/estimate", {
      method: "POST",
      body: JSON.stringify(payload()),
    });
    const minutes = Math.max(1, Math.round(data.seconds_estimate / 60));
    $("estimate").className = "estimate";
    let html =
      `<strong>${data.extent_km.width.toFixed(2)} x ${data.extent_km.height.toFixed(2)} km</strong><br />` +
      `${data.tiles} tiles (${data.rows} x ${data.cols})<br />` +
      `around ${Math.round(data.bytes_estimate / 1e6)} MB, about ${minutes} min`;
    if (data.warnings && data.warnings.length) {
      html += `<span class="estimate-warning">${data.warnings.map(escapeHtml).join("<br />")}</span>`;
    }
    $("estimate").innerHTML = html;
    $("download").disabled = false;
    // The exact path naming.build_package_paths composed for this request,
    // not a guess assembled here: this is read straight into Grasshopper,
    // so it has to be the same path the download itself will create, from
    // the same server-side code that creates it.
    if (data.folder) {
      $("folder-preview-path").textContent = data.folder;
      $("folder-preview").hidden = false;
    } else {
      hideFolderPreview();
    }
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
// API key fields are deliberately not in the list above: see their own
// delegated listener further down, which must persist before refreshing,
// not alongside it as an independent, unordered listener.

// --- settings panel -----------------------------------------------------
//
// Output root, tile size and overlap are defaults the owner sets once and
// rarely revisits; API keys belong to a source and are exactly the same
// kind of thing. All three move here rather than sitting in the main
// flow, which is otherwise entirely per-survey choices (region, site,
// layers, categories). Nothing about the FIELDS themselves changes: their
// ids, their persistence and their effect on the next estimate are
// unchanged by which panel currently shows them.

function closeSettingsPanel() {
  $("settings-panel").hidden = true;
}
// Set explicitly rather than left to the markup's own `hidden` attribute:
// index.html's copy of that attribute is what a real browser honours
// before any script runs, but this line is what the Node test harness's
// synthetic elements (built fresh on first getElementById, with no
// knowledge of the real markup's own attributes) actually see, and it is
// also just plainly clearer to have the panel's closed state asserted in
// one place rather than split between markup and script.
closeSettingsPanel();

$("settings-toggle").addEventListener("click", () => {
  $("settings-panel").hidden = false;
});

$("settings-close").addEventListener("click", closeSettingsPanel);
$("settings-backdrop").addEventListener("click", closeSettingsPanel);

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
// Set once, in boot(), from GET /api/sources: the api-keys change handler
// below needs the same list to re-render the saved/unsaved indicator
// after a save, and it has no other way to get it, since the fields it is
// re-rendering carry no memory of which source they belong to beyond
// data-config-field.
let currentSources = [];

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

// Persisting output-root/tile-size/overlap only from refreshEstimate's
// success path (above) closes one problem, a value the estimate rejects
// being saved anyway, by opening another: refreshEstimate returns early,
// before ever calling persistFieldSettings, whenever bbox, region or site
// are not set yet. Setting a preferred output root before ever drawing an
// extent, an entirely ordinary way to use this, would then never be saved
// at all until some unrelated later estimate happened to succeed. There
// is nothing to validate a field-in-isolation against before an extent
// and names exist (check_path_length needs the region and site slugs
// too, not just the output root), so there is nothing an early value could
// have failed, and no reason to withhold it: persist immediately in
// exactly that case, and defer to refreshEstimate's success path only
// once there is an actual estimate that could reject it.
function maybePersistFieldSettings() {
  if (!bbox || !$("region").value.trim() || !$("site").value.trim()) {
    persistFieldSettings();
  }
}

["tile-size", "overlap", "output-root"].forEach((id) =>
  $(id).addEventListener("change", maybePersistFieldSettings)
);

// API keys have no equivalent to check_path_length: estimate_survey never
// rejects the estimate over one, only adds a warning alongside a still-
// successful estimate (see readiness_problem in mapgen.sources.elevation),
// so there is no "value the estimate just rejected" case to defer past.
// Persisted immediately, unconditionally, unlike the three settings fields
// above.
//
// One delegated listener on the #api-keys container, not one listener per
// rendered field: the fields themselves are built from the source
// registry (see renderApiKeys), so their number and identity are not
// known here, only that each one that exists carries a data-config-field
// attribute naming which mapgen.config.Config field it belongs to. A
// second or third keyed source in phase 2 is picked up by this
// unchanged.
//
// Persisted AND AWAITED before refreshEstimate re-checks readiness, not
// fired as a second, independent listener racing it: the server's own
// readiness check reads the SAVED config file, not the field's live
// value, so a key typed in for the first time could otherwise still be
// reported as "not configured" by the very estimate meant to reflect it,
// if that estimate's request happened to reach the server before this
// one's PUT did.
//
// renderApiKeys is called again after the save, not just refreshEstimate:
// that is what makes the saved/unsaved indicator (Task 21, defect 2)
// actually update the moment a key is saved or cleared, rather than only
// reflecting whatever boot() saw once at load. It reads savedConfig, not
// the field's own live value, so a save that genuinely failed (the catch
// branch in persistConfig, savedConfig left unchanged) re-renders back to
// the last confirmed truth instead of a field quietly claiming a state
// that was never actually written to disk.
$("api-keys").addEventListener("change", async (event) => {
  const field = event.target && event.target.getAttribute && event.target.getAttribute("data-config-field");
  if (!field) return;
  await persistConfig({ [field]: event.target.value });
  renderApiKeys(currentSources, savedConfig);
  refreshEstimate();
});

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

// --- keep-alive and shutdown -------------------------------------------
//
// A ping every HEARTBEAT_INTERVAL_MS while this page is open, telling the
// server's watchdog (see mapgen.web.server, only wired in for a
// --windowless launch) that someone is still here. Sent unconditionally,
// launch mode unknown to this page: harmless against a plain `mapgen ui`
// session, which records the ping but has no watchdog reading it.
//
// An earlier version of this comment claimed "there is nothing to do on
// unload, since a closed or crashed tab simply stops sending these". That
// was wrong in a way that cost the owner a working session. A BACKGROUNDED
// tab also stops sending these, because browsers throttle timers in hidden
// tabs to around once a minute; the two states are indistinguishable from
// silence alone. The owner switched away to register for an API key, came
// back, and every request failed against a server that had shut itself
// down. Silence is now the weakest of three signals, not the only one:
//
//   pagehide           a genuine close or navigation, reported immediately
//                      via sendBeacon, which is the one thing browsers
//                      guarantee will still be delivered as a page dies.
//   visibilitychange   coming back to the foreground pings AT ONCE rather
//                      than waiting up to a full interval, so a returning
//                      page cannot be killed in the gap.
//   the interval       the idle-session backstop it always was, now read
//                      against a timeout well clear of throttling.
//
// Started at script load, not inside boot(): boot() awaits /api/config and
// /api/sources before it can do anything else, and the first ping should
// not wait on those, particularly since a slow or failed boot() is exactly
// when the owner is most likely to be looking at a blank page.
const HEARTBEAT_INTERVAL_MS = 5000;

function ping() {
  api("/api/heartbeat", { method: "POST" }).catch(() => {
    // A missed ping is well within the server's grace period; the next
    // interval, or the next return to the foreground, tries again.
  });
}

setInterval(ping, HEARTBEAT_INTERVAL_MS);

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") ping();
});

// sendBeacon rather than fetch: a page being closed is not guaranteed to
// live long enough to finish a normal request, and a beacon is queued by
// the browser and delivered regardless. The token goes on the query string
// exactly as api() would put it, because this bypasses api() entirely.
window.addEventListener("pagehide", () => {
  const url = new URL("/api/closing", location.origin);
  url.searchParams.set("token", token);
  navigator.sendBeacon(url.toString());
});

$("stop-server").addEventListener("click", async () => {
  try {
    await api("/api/shutdown", { method: "POST" });
    log("Server stopped. This page will no longer work; you can close it.");
  } catch (error) {
    log(`Could not stop the server: ${error.message}`, true);
  }
});

// --- boot ------------------------------------------------------------

// Split out of boot() itself now that there are three registry-driven
// checklists to render (sources, categories, and settings' API keys):
// each function's only job is turning one API response into markup, so
// boot() reads as "fetch these, render each", not one long block mixing
// three unrelated templates together.

function renderSources(sources) {
  // Every source, elevation included, is ticked by default. Elevation
  // needing a key is not a reason to untick it here: the owner's own
  // ruling was to keep it selected and surface a plain warning on the
  // estimate instead (see readiness_problem/estimate_survey), so a key
  // typed in the settings panel takes effect without ever having to
  // remember to re-tick a layer that was silently switched off.
  // Escaped even though /api/sources is this same server's own data,
  // not third-party input: it costs nothing here, and it is one fewer
  // thing to have to reason about correctly if a future source's
  // licence or display_name string ever comes from somewhere less
  // trusted than a hardcoded class attribute.
  $("sources").innerHTML = sources
    .map(
      (s) => `
        <label title="${escapeHtml(s.licence)}">
          <input type="checkbox" value="${escapeHtml(s.id)}" checked />
          <span>${escapeHtml(s.display_name)}${s.requires_api_key ? " (needs an API key)" : ""}</span>
        </label>`
    )
    .join("");
}

function renderCategories(groups) {
  // A leaf group (every group except "roads" today) gets its own
  // checkbox. A group with children ("roads") is a plain label over its
  // children instead, never a checkbox of its own: there is nothing a
  // tag filter or an Overture type selection could do with "every road"
  // that ticking all nine children does not already do (see
  // mapgen.categories's own module docstring), so it is not given a
  // control that would only ever need to mirror them.
  $("categories").innerHTML = groups
    .map((group) => {
      if (!group.children.length) {
        return `
        <label>
          <input type="checkbox" value="${escapeHtml(group.id)}" checked />
          <span>${escapeHtml(group.label)}</span>
        </label>`;
      }
      const children = group.children
        .map(
          (child) => `
        <label class="category-child">
          <input type="checkbox" value="${escapeHtml(child.id)}" checked />
          <span>${escapeHtml(child.label)}</span>
        </label>`
        )
        .join("");
      return `<div class="category-group-label">${escapeHtml(group.label)}</div>${children}`;
    })
    .join("");
}

// Task 21, defect 2: a saved key and an unsaved one both render as the
// same row of dots in a type="password" field, which is what convinced
// the owner a save that had genuinely worked had not. The persistence
// itself was never broken (see mapgen.config, unchanged by this fix); the
// field just gave no honest signal either way. This never echoes the key
// itself, only whether one is present in the saved config, matching the
// redaction discipline mapgen.sources.elevation already applies to this
// exact value everywhere else it could ever reach a log or a response.
function apiKeyStatusMarkup(hasSavedKey) {
  return hasSavedKey
    ? '<span class="api-key-status saved">Key saved</span>'
    : '<span class="api-key-status unsaved">No key saved</span>';
}

function renderApiKeys(sources, config) {
  // Driven from the source registry, not one hard-coded field per key:
  // a source that needs one names its own mapgen.config.Config field via
  // api_key_config_field (see sources/base.py's own documented
  // convention), so a second or third keyed source in phase 2 appears
  // here with no change to this function at all. data-config-field
  // carries that name into the DOM for the delegated persistence
  // listener below to read back; there is no id per field, the same
  // convention #sources' own checkboxes already use, since the number
  // of keyed sources is not fixed at markup time.
  const keyed = sources.filter((s) => s.requires_api_key && s.api_key_config_field);
  $("api-keys").innerHTML = keyed
    .map((s) => {
      const value = escapeHtml(config[s.api_key_config_field] || "");
      return `
        <label class="api-key-field">
          <span>${escapeHtml(s.display_name)}</span>
          <input type="password" data-config-field="${escapeHtml(s.api_key_config_field)}"
                 value="${value}" autocomplete="off"
                 placeholder="Only needed for this layer" />
          ${apiKeyStatusMarkup(Boolean(config[s.api_key_config_field]))}
        </label>`;
    })
    .join("");
}

(async function boot() {
  try {
    const config = await api("/api/config");
    $("output-root").value = config.output_root;
    $("tile-size").value = config.tile_size_m;
    $("overlap").value = config.overlap_m;
    if (config.last_region) $("region").value = config.last_region;
    savedConfig = config;

    const sources = await api("/api/sources");
    currentSources = sources;
    renderSources(sources);
    renderApiKeys(sources, config);
    $("sources").addEventListener("change", refreshEstimate);

    const categories = await api("/api/categories");
    renderCategories(categories);
    $("categories").addEventListener("change", refreshEstimate);
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
