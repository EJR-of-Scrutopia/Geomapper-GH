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

// --- theme -------------------------------------------------------------
//
// Task 22: dark and light mode, interface-wide, requested alongside the
// tile grid and answered with the one setting rather than two: "auto"
// (the default) follows the OS/browser preference, an explicit choice
// overrides it. The PAGE itself needs no JS to do this at all: styles.css
// carries both palettes as CSS variables, switched by a plain
// prefers-color-scheme media query for "auto" and by a data-theme
// attribute this function sets for an explicit choice. JS only has to
// resolve "auto" for itself where CSS variables cannot reach: the tile
// grid below is drawn as Leaflet SVG shapes with colours set directly in
// JS, not through a stylesheet, so effectiveTheme() is what lets it pick
// the right palette without duplicating a media query in script.
function applyTheme(theme) {
  const root = document.documentElement;
  if (!root) return;
  if (theme === "light" || theme === "dark") {
    root.setAttribute("data-theme", theme);
  } else {
    root.removeAttribute("data-theme");
  }
}

function effectiveTheme() {
  const setting = $("theme") ? $("theme").value : "auto";
  if (setting === "light" || setting === "dark") return setting;
  const prefersDark =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches;
  return prefersDark ? "dark" : "light";
}

// --- tile grid -----------------------------------------------------------
//
// Task 22: draws the actual tiling build_tiles computed as rectangles over
// the extent, from /api/extent's or /api/estimate's own tile_grid (see
// package.py's _geometry_summary), never recomputed here: the same
// tile_id progress events already carry is what keys each rectangle, so
// this can never disagree with the server about the geometry.
//
// Four states, not three: pending (not started), active (touched by at
// least one event but not yet settled), done, and failed. Failed is
// checked first everywhere below and is sticky (nothing ever downgrades
// it), because it is the one state the owner would want to notice before
// deciding they have enough, per the brief.
const TILE_COLOURS = {
  light: {
    pending: { color: "#9c9686", fillColor: "#9c9686", fillOpacity: 0.05, weight: 1 },
    active: { color: "#c98a2e", fillColor: "#c98a2e", fillOpacity: 0.25, weight: 1 },
    done: { color: "#2f5d4f", fillColor: "#2f5d4f", fillOpacity: 0.25, weight: 1 },
    failed: { color: "#8c3b2e", fillColor: "#8c3b2e", fillOpacity: 0.45, weight: 2 },
  },
  dark: {
    pending: { color: "#6b6656", fillColor: "#6b6656", fillOpacity: 0.1, weight: 1 },
    active: { color: "#e0a840", fillColor: "#e0a840", fillOpacity: 0.3, weight: 1 },
    done: { color: "#6fc3a3", fillColor: "#6fc3a3", fillOpacity: 0.3, weight: 1 },
    failed: { color: "#e0685a", fillColor: "#e0685a", fillOpacity: 0.5, weight: 2 },
  },
};
const TILE_STATE_LABELS = {
  pending: "Not started",
  active: "In progress",
  done: "Done",
  failed: "Failed",
};

let tileRectangles = new Map(); // tile_id -> Leaflet rectangle
let tileState = new Map(); // tile_id -> "pending" | "active" | "done" | "failed"

function tileStyleFor(state) {
  return TILE_COLOURS[effectiveTheme()][state] || TILE_COLOURS[effectiveTheme()].pending;
}

function clearTileGrid() {
  for (const rect of tileRectangles.values()) map.removeLayer(rect);
  tileRectangles = new Map();
  tileState = new Map();
  renderTileLegend();
}

// Drawn every time the extent, tile size or overlap changes (see
// refreshEstimate): a fresh tile_grid always means a fresh set of
// rectangles, never a resize of the previous ones, since a changed
// tiling can add, remove or move tiles rather than just reshape them.
function renderTileGrid(tileGrid) {
  clearTileGrid();
  for (const tile of tileGrid || []) {
    tileState.set(tile.tile_id, "pending");
    const bounds = [
      [tile.south, tile.west],
      [tile.north, tile.east],
    ];
    const rect = L.rectangle(bounds, tileStyleFor("pending"));
    rect.addTo(map);
    tileRectangles.set(tile.tile_id, rect);
  }
  renderTileLegend();
}

function paintTileStates(states) {
  for (const [tileId, state] of states) {
    if (tileState.get(tileId) === state) continue;
    tileState.set(tileId, state);
    const rect = tileRectangles.get(tileId);
    if (rect) rect.setStyle(tileStyleFor(state));
  }
}

// Repaints every rectangle already on the map in its current state, using
// whichever palette effectiveTheme() now resolves to: called when the
// theme setting changes, since the tile-by-tile state itself has not
// changed, only which colours represent it.
function repaintTileGridTheme() {
  for (const [tileId, rect] of tileRectangles) {
    rect.setStyle(tileStyleFor(tileState.get(tileId) || "pending"));
  }
  renderTileLegend();
}

function renderTileLegend() {
  const legend = $("tile-legend");
  if (tileRectangles.size === 0) {
    legend.hidden = true;
    legend.innerHTML = "";
    return;
  }
  legend.hidden = false;
  legend.innerHTML = Object.keys(TILE_STATE_LABELS)
    .map((state) => {
      const style = tileStyleFor(state);
      return (
        `<span class="tile-legend-item">` +
        `<span class="tile-legend-swatch" style="background:${style.fillColor}"></span>` +
        `${escapeHtml(TILE_STATE_LABELS[state])}</span>`
      );
    })
    .join("");
}

// Classifies every tile from the job's progress events alone: a pure
// function of (tile ids, selected source ids, events so far, whether the
// job is still running), recomputed fresh on every poll tick rather than
// updated incrementally, which is cheap even for the "hundreds of tiles"
// the brief warns this has to scale to and is simpler than reasoning
// about incremental state transitions across a poll that can, in
// principle, miss no events (the server holds the full history) but is
// still worth not depending on.
//
// tile_done/tile_skipped alone only ever reaches "active", never "done":
// Overture emits one such event per (tile, type), so a tile is not
// finished when the first one arrives (see the brief). "done" is reached
// two ways instead: every SELECTED source has reported its own
// source_done (package.py emits this once a source's whole fetch+merge
// pass, complete or a tolerated partial, is over), which is the strong,
// mid-run signal; or the job has stopped running at all (job.state left
// "running"), which is the closing signal for a run that ended before
// every source got a source_done of its own, a stopped or a hard-failed
// run in particular.
//
// "failed" is sticky: nothing here ever moves a tile OFF "failed" once a
// tile_failed event (see package.py's _record_tile_outcomes) has set it,
// which matters more than the other states per the brief. This falls out
// of the tile_done/tile_skipped branch's own guard rather than needing a
// separate check: it only ever promotes a tile FROM "pending", so once a
// tile has moved to "failed" (or "active"), a later tile_done/tile_skipped
// for it (Overture's own multiple per-tile events, most often) has
// nothing to do. tile_failed itself stays unconditional, deliberately: a
// genuine failure discovered after a tile was already marked done ought
// to still register as failed, not be masked by having arrived "too late".
// Task 27: one walk of the event stream, producing BOTH the per-tile
// states the grid paints and the fractions the progress bar and the
// countdown read. Deliberately one function and not two: the progress
// numbers obey exactly the rules documented above (dedupe per (tile,
// source), settle on source_done, failed is sticky), and a second walk
// with "slightly different" rules is how two views of the same run come
// to disagree with each other on screen. classifyTiles below is a thin
// wrapper kept for the grid's own call sites and its tests.
//
// The fractions are weighted by each source's own seconds_estimate from
// the estimate that enabled Download, not one equal share per source,
// because the sources are nothing like equal: on the owner's Barry
// extent at the default tiling, OpenStreetMap is about 144s of a 175s
// run (72 tiles at MAP_API_SECONDS_PER_TILE), Overture about 18s and
// elevation about 11s. Equal thirds would put the bar at 33% after 82%
// of the run had actually happened, and the countdown reads off the same
// fraction, so it would have projected minutes of remaining time onto a
// run with seconds left. Weights come from the estimate Task 25 made
// trustworthy; with no weights at all (an older or partial estimate
// response) this falls back to equal shares rather than refusing.
//
// A source's own fraction is the share of the PLAN's tiles it has
// reported, and 1 once it has emitted source_done. Two consequences are
// deliberate:
//
//   ElevationSource reports one event for the whole extent under
//   tile_id "whole-area", which is not a tile of the plan, so it shows
//   no partial progress and moves from 0 to its full share at
//   source_done. That is honest: a single whole-extent download has no
//   partial signal to report, and inventing one would be a bar that
//   moved because time passed rather than because work finished.
//
//   OvertureSource emits per tile AND per type, so its share is full
//   once its FIRST type has landed rather than after all eight. That
//   over-reads by at most its own weight (about 10% of a default run)
//   for the few seconds between the first type landing and source_done,
//   because the types download concurrently. The alternative, counting
//   (tile, type) pairs, needs a type count the browser is never told,
//   and guessing it would be a fabricated denominator.
//
// fetched and skipped are tracked apart because a resumed run is the
// case the owner actually hits: Stop, then Download again over the same
// extent, and every tile already on disk reports tile_skipped in the
// first second. Those cost nothing, so measuring a rate from them would
// project a run that is 90% "done" in one second as finishing
// immediately, when everything genuinely left is still to be fetched.
// The countdown therefore projects from fetched work only, and scales
// the static estimate by the work that is genuinely left.
function summariseJob(tileIds, sourceIds, events, jobRunning, sourceSeconds) {
  const state = new Map(tileIds.map((id) => [id, "pending"]));
  const finishedSources = new Set();
  const fetchedTiles = new Map(); // source id -> Set of plan tile ids
  const skippedTiles = new Map(); // source id -> Set of plan tile ids
  let subdivisions = 0;

  const setFor = (bucket, source) => {
    if (!bucket.has(source)) bucket.set(source, new Set());
    return bucket.get(source);
  };

  for (const event of events || []) {
    if (event.tile_id && state.has(event.tile_id)) {
      if (event.event === "tile_failed") {
        state.set(event.tile_id, "failed");
      } else if (
        (event.event === "tile_done" || event.event === "tile_skipped") &&
        state.get(event.tile_id) === "pending"
      ) {
        state.set(event.tile_id, "active");
      }
      // Counted per (tile, source), never per event, which is the whole
      // Overture problem: eight events for one tile are one tile's worth
      // of progress, not eight. A tile_failed counts as work done, not
      // as work outstanding: it took real time, it will not be attempted
      // again in this run, and the grid already shows failure distinctly
      // in the one place the owner is meant to notice it.
      if (event.source) {
        if (event.event === "tile_done" || event.event === "tile_failed") {
          setFor(fetchedTiles, event.source).add(event.tile_id);
        } else if (event.event === "tile_skipped") {
          setFor(skippedTiles, event.source).add(event.tile_id);
        }
      }
    }
    if (event.event === "source_done" && event.source) {
      finishedSources.add(event.source);
    }
    if (event.event === "tile_subdivided") {
      subdivisions += 1;
    }
  }

  const everySourceFinished =
    sourceIds.length > 0 && sourceIds.every((id) => finishedSources.has(id));
  if (everySourceFinished || !jobRunning) {
    for (const [tileId, value] of state) {
      if (value === "active") state.set(tileId, "done");
    }
  }

  // The tile states settle when the job stops running; the FRACTIONS
  // never do. A stopped run that got two sources through of three is
  // genuinely two thirds of the way through, and a bar that jumped to
  // 100% because the job ended would be claiming work that was never
  // done. Task 22's distinction between stopped and failed depends on
  // this bar being able to say "stopped at 68%" at all.
  const tileCount = tileIds.length;
  let weightTotal = 0;
  let doneTotal = 0;
  let skippedTotal = 0;
  for (const sourceId of sourceIds) {
    const weight = weightForSource(sourceId, sourceSeconds);
    const fetched = fetchedTiles.get(sourceId) || new Set();
    const skipped = skippedTiles.get(sourceId) || new Set();
    // A tile Overture skipped for one type and downloaded for another is
    // fetched work, not skipped work: the union decides how much of the
    // tile is finished, and only tiles with no fetched work at all count
    // towards the skipped share.
    let reported = fetched.size;
    let skippedOnly = 0;
    for (const tileId of skipped) {
      if (!fetched.has(tileId)) {
        reported += 1;
        skippedOnly += 1;
      }
    }
    const measured = tileCount > 0 ? Math.min(1, reported / tileCount) : 0;
    const done = finishedSources.has(sourceId) ? 1 : measured;
    const skippedShare =
      tileCount > 0 ? Math.min(done, skippedOnly / tileCount) : 0;
    weightTotal += weight;
    doneTotal += weight * done;
    skippedTotal += weight * skippedShare;
  }

  const fractionDone = weightTotal > 0 ? doneTotal / weightTotal : 0;
  const fractionSkipped = weightTotal > 0 ? skippedTotal / weightTotal : 0;
  return {
    tileStates: state,
    fractionDone,
    fractionSkipped,
    fractionFetched: Math.max(0, fractionDone - fractionSkipped),
    subdivisions,
  };
}

// A source with no estimate of its own weighs the same as every other
// such source rather than nothing at all: a zero would drop it out of
// the bar entirely, which is worse than weighting it roughly.
function weightForSource(sourceId, sourceSeconds) {
  const seconds = sourceSeconds ? Number(sourceSeconds[sourceId]) : NaN;
  return Number.isFinite(seconds) && seconds > 0 ? seconds : 1;
}

function classifyTiles(tileIds, sourceIds, events, jobRunning) {
  return summariseJob(tileIds, sourceIds, events, jobRunning).tileStates;
}

// --- the countdown -------------------------------------------------------
//
// Remaining time, never elapsed time: "how much longer" is the question
// the owner has while watching a download, and elapsed time answers a
// different one they can already answer by looking at the clock.
//
// Two branches, and the copy says which one it is on, because they are
// not equally good and pretending otherwise would be the dishonest part:
//
//   from the estimate    static seconds, scaled by the work this run
//                        genuinely has to do, minus elapsed. Used early,
//                        when there is not enough finished work to
//                        measure a rate from.
//   from the rate so far elapsed / fraction fetched, applied to the
//                        fraction left. Adapts to a run going faster or
//                        slower than estimated, which the static number
//                        cannot, and is what makes a stalled or
//                        subdivided run report a growing countdown
//                        rather than a frozen one.
//
// The crossover is 10% of the weighted work FETCHED in this run and at
// least 15 seconds of wall clock, both, and it is set from what the
// per-tile costs were actually measured at rather than picked round:
//
//   The default path (the OSM map API, which is what an unfiltered
//   category selection uses) is governed by this tool's own RateLimiter,
//   not by the network: three runs of six tiles measured 10.17s to
//   10.27s, so about 1.7s per tile with almost no spread. Ten percent of
//   a 72-tile run is 8 or 9 tiles, roughly 15 seconds, and the two
//   thresholds therefore cross at nearly the same moment on the run the
//   owner actually makes.
//
//   The Overpass path (any genuine category restriction) is the opposite:
//   four tiles measured 3.84s, 49.93s, 13.38s and 9.86s. No threshold
//   makes that predictable, which is exactly why the answer is rounded
//   coarsely below and labelled as coming from the run so far, rather
//   than a threshold tuned until one sample looked good.
//
// The 15 second floor is what stops the second poll of a fast-starting
// run projecting off two seconds of evidence, and the 10% floor is what
// stops a projection from noise; a projection from 2% done is noise.
const COUNTDOWN_CROSSOVER_FRACTION = 0.1;
const COUNTDOWN_CROSSOVER_SECONDS = 15;

// Never finer than a minute. The estimate underneath is +/- 20% at best,
// so at three minutes the honest band is already +/- 36 seconds, and a
// countdown reading "3 min 12 s" would be claiming a precision nothing
// in this tool has. Past ten minutes it coarsens again to five, because
// +/- 20% of twenty minutes is four.
function formatRemaining(seconds) {
  if (!(seconds > 0)) return "less than a minute";
  if (seconds < 60) return "less than a minute";
  const minutes = seconds / 60;
  if (minutes < 10) return `about ${Math.round(minutes)} min`;
  return `about ${Math.max(5, Math.round(minutes / 5) * 5)} min`;
}

// A pure function of the summary above plus elapsed wall clock and the
// static estimate, so the awkward shapes (a stall, a resume, a run that
// overruns) can be tested at the numbers rather than by waiting for a
// real download to misbehave. Returns the countdown line, an optional
// note, and which branch produced it.
function remainingLabel({
  fractionDone,
  fractionFetched,
  fractionSkipped,
  elapsedSeconds,
  staticSeconds,
  subdivisions,
}) {
  // Subdivision costs extra requests that no estimate made before the
  // run could have known about (density is only discoverable by asking
  // for the data), so the countdown says so rather than quietly
  // continuing to tick down against a total that has stopped being
  // true. The projection branch already absorbs the cost numerically;
  // this is what the estimate branch has instead of pretending.
  const note =
    subdivisions > 0
      ? `${subdivisions === 1 ? "One tile was" : `${subdivisions} tiles were`} too dense ` +
        `for one request and had to be split into pieces, so this run is longer than ` +
        `the estimate expected.`
      : "";

  if (fractionDone >= 0.999) {
    // Every selected source has reported. What is left is the merge into
    // the package, the Urbano bridge step and survey.json, which is real
    // work with no per-tile events of its own; a countdown here would be
    // counting down to something it cannot see.
    return { text: "Downloads finished, writing the package.", note, branch: "finishing" };
  }

  const fractionLeft = Math.max(0, 1 - fractionDone);

  if (
    fractionFetched >= COUNTDOWN_CROSSOVER_FRACTION &&
    elapsedSeconds >= COUNTDOWN_CROSSOVER_SECONDS
  ) {
    const remaining = (elapsedSeconds / fractionFetched) * fractionLeft;
    return { text: `${formatRemaining(remaining)} left, from the rate so far`, note, branch: "measured" };
  }

  if (!(staticSeconds > 0)) {
    return { text: "Working out how much longer.", note, branch: "unknown" };
  }

  // Scaled by the work already on disk, not the flat total: a resumed
  // run that skips four fifths of its tiles has a fifth of the estimate
  // left to spend, and "estimate minus elapsed" would have it finishing
  // minutes after it really does.
  const expected = staticSeconds * Math.max(0, 1 - fractionSkipped);
  const remaining = expected - elapsedSeconds;
  if (remaining <= 0) {
    // Never "0 seconds remaining" against a job that is still going. The
    // true thing to say is that the estimate has been passed, and the
    // grid and the log are what say how far along it actually is.
    return { text: "Taking longer than the estimate. Still running.", note, branch: "overrun" };
  }
  return { text: `${formatRemaining(remaining)} left, from the estimate`, note, branch: "estimate" };
}

// Starts with no grid and the legend hidden. Set explicitly here rather
// than left to index.html's own `hidden` attribute, the same reasoning
// closeSettingsPanel documents below for the settings panel: a real
// browser honours the markup's own attribute before any script runs, but
// the Node test harness's synthetic elements have no knowledge of the
// real markup's attributes at all, only of whatever a script sets.
clearTileGrid();

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
  const request = {
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
  // The elevation model, unlike the key above, DOES belong in the
  // payload: it is a per-request choice the CLI can also make with
  // --demtype, and SurveyRequest is where the two meet and where an
  // unknown one is refused. Added here rather than in the literal above,
  // and only when the select actually holds a model, never as an empty
  // string: the server reads an ABSENT key as "the default, COP30" and a
  // present-but-empty one as a malformed request, and that distinction is
  // worth preserving from this side too. Absent is what a page whose
  // /api/sources reply carried no choices sends, and such a page works
  // exactly as it did before this setting existed.
  const demtype = $("demtype").value;
  if (demtype) request.elevation_demtype = demtype;
  return request;
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

// Task 27: what the last successful estimate said, kept because two
// things now need it after the response has been rendered and thrown
// away. The tile size slider needs to show the time at the size it is
// currently sitting at, and it lives in the settings panel, which
// physically covers the estimate box, so it cannot just point at it. The
// progress bar needs the same run's total seconds and its per-source
// breakdown to weigh itself and to start its countdown from.
//
// tileSizeM is stored alongside so a reader can tell whether these
// numbers still describe the slider's current position: mid-drag they do
// not, and claiming a time for a size that was never estimated is the
// one thing this must not do. seconds is 0 for the geometry-only
// /api/extent reply, which knows the tile count but has no idea about
// time, since it is answered without ever consulting a source.
let lastSizing = null;

// tileSizeM is passed in, captured before the request went out, never
// re-read from the field here. The field is a slider now, and the owner
// can move it while a request for the previous position is still in
// flight: reading it back at this point would file the old size's answer
// under the new size's name, which is precisely the stale number
// tileSizeCost exists to refuse to show.
function recordSizing(tileSizeM, tiles, seconds, sources) {
  const sourceSeconds = {};
  for (const source of sources || []) {
    const value = Number(source.seconds_estimate);
    if (source.id && Number.isFinite(value)) sourceSeconds[source.id] = value;
  }
  lastSizing = {
    tileSizeM,
    tiles,
    seconds: Number.isFinite(Number(seconds)) ? Number(seconds) : 0,
    sourceSeconds,
  };
  renderTileSize();
}

function showEstimateError(message) {
  const box = $("estimate");
  box.className = "estimate error";
  box.textContent = message;
  $("download").disabled = true;
  hideFolderPreview();
  // A tiling or extent problem invalidates whatever grid was last drawn:
  // showing rectangles for a configuration that just failed would
  // mislead rather than help.
  clearTileGrid();
  // And it invalidates the tile count and time beside the slider for
  // exactly the same reason: an absurd tiling is one of the things the
  // slider itself can cause, so the numbers next to it must not go on
  // describing the last size that worked.
  lastSizing = null;
  renderTileSize();
}

async function refreshEstimate() {
  const missing = missingFieldsMessage();
  // Read once, up front, and used both for the request and for recording
  // what came back. The slider can move while this request is in flight,
  // so reading the field again after the await would describe the answer
  // by a size it was never asked about.
  const requestedTileSizeM = parseFloat($("tile-size").value);

  if (!bbox) {
    $("estimate").className = "estimate";
    $("estimate").textContent = missing;
    $("download").disabled = true;
    hideFolderPreview();
    clearTileGrid();
    lastSizing = null;
    renderTileSize();
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
          tile_size_m: requestedTileSizeM,
          overlap_m: parseFloat($("overlap").value),
        }),
      });
      $("estimate").innerHTML = `${formatGeometryLine(geometry)}<br />${escapeHtml(missing)}`;
      renderTileGrid(geometry.tile_grid);
      recordSizing(requestedTileSizeM, geometry.tiles, 0, []);
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
      lastSizing = null;
      renderTileSize();
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
    renderTileGrid(data.tile_grid);
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
    recordSizing(requestedTileSizeM, data.tiles, data.seconds_estimate, data.sources);
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

// --- tile size slider ----------------------------------------------------
//
// Task 27: the owner asked for a slider showing "time and failure risk"
// at each size. Time is real and comes from the estimate endpoint, so it
// is here. Failure risk is not shown, and that is a deliberate refusal
// rather than an omission: it would depend on how dense the OSM data is
// on this particular ground, which nothing in this tool can know before
// downloading it, and a percentage or a traffic light would look
// measured while being made up. Since Task 26 a too-dense tile is not a
// failure at all, it is split into quarters and the run continues, so
// the honest thing left to say is the trade itself, in a sentence that
// changes with the slider's position. See tileSizeTrade below.
//
// The range is NOT the number input's range narrowed. It starts at the
// same 500 m minimum the number input had, and the 10000 m top is new,
// because a slider must have a maximum where a number field did not: at
// a 10 km tile any site-scale extent is already one or two tiles, and a
// larger size stops changing anything at all. It is the one genuine
// narrowing this control introduces, which is why
// applyTileSizeBounds widens either end rather than
// clamping, so a saved setting outside this range keeps working and
// keeps its own value instead of being silently rewritten to the nearest
// end the first time the panel is opened.
const TILE_SIZE_MIN_M = 500;
const TILE_SIZE_MAX_M = 10000;
// Long enough that dragging across the whole track is one request rather
// than one per step, matched to the debounce the place search and the
// name suggestions already use rather than inventing a third number.
// Debounced rather than estimated from values already fetched: a
// client-side curve through previous answers would be a second, drifting
// copy of an estimate the server owns, and Task 25 has only just made
// the server's copy trustworthy.
const TILE_SIZE_DEBOUNCE_MS = 400;
let tileSizeDebounce = null;

function applyTileSizeBounds(savedMetres) {
  const saved = Number(savedMetres);
  const min = Number.isFinite(saved) && saved > 0 ? Math.min(TILE_SIZE_MIN_M, saved) : TILE_SIZE_MIN_M;
  const max = Number.isFinite(saved) && saved > 0 ? Math.max(TILE_SIZE_MAX_M, saved) : TILE_SIZE_MAX_M;
  // Set before the value is, not after: a browser clamps an out-of-range
  // value the moment it is assigned, so setting the value first would
  // lose the very setting these bounds exist to preserve.
  $("tile-size").min = String(min);
  $("tile-size").max = String(max);
}

// The trade, as a sentence, with no fabricated number in it. Three
// positions rather than a continuum because there are only three things
// true to say, and the middle one is the default the estimate panel and
// the README already recommend.
function tileSizeTrade(metres) {
  if (!Number.isFinite(metres)) return "";
  if (metres < 2000) {
    return (
      "Smaller than the 2000 m default: more requests up front, and less " +
      "chance that any one tile is dense enough to need splitting."
    );
  }
  if (metres > 2000) {
    return (
      "Larger than the 2000 m default: fewer requests, quicker on sparse " +
      "ground. On dense ground more tiles need splitting, and each split " +
      "costs the requests its pieces take."
    );
  }
  return "The default. Fewest requests that rarely need splitting, on most ground.";
}

// The time at THIS position, or an honest account of why there is not
// one yet. Never the last size's answer relabelled: lastSizing carries
// the size it was measured at precisely so a mid-drag position, whose
// estimate has not come back, says so instead of showing a number that
// belongs to a different tiling.
function tileSizeCost(metres) {
  if (!bbox) return "Draw or paste an extent to see the time at each size.";
  if (!lastSizing || lastSizing.tileSizeM !== metres) return "Working out the time at this size.";
  const tileWord = lastSizing.tiles === 1 ? "tile" : "tiles";
  if (!(lastSizing.seconds > 0)) {
    return `${lastSizing.tiles} ${tileWord}. Enter a region and site to see the time.`;
  }
  const minutes = Math.max(1, Math.round(lastSizing.seconds / 60));
  return `${lastSizing.tiles} ${tileWord}, about ${minutes} min.`;
}

function renderTileSize() {
  const metres = parseFloat($("tile-size").value);
  $("tile-size-readout").textContent = Number.isFinite(metres) ? `${Math.round(metres)} m` : "";
  $("tile-size-cost").textContent = tileSizeCost(metres);
  $("tile-size-trade").textContent = tileSizeTrade(metres);
}

// "input" fires on every step of a drag, "change" once on release. The
// readout follows every step, because a slider whose number lags the
// handle is unusable; the estimate request is debounced off the same
// event so a drag across the track costs one request instead of one per
// step.
$("tile-size").addEventListener("input", () => {
  renderTileSize();
  clearTimeout(tileSizeDebounce);
  tileSizeDebounce = setTimeout(refreshEstimate, TILE_SIZE_DEBOUNCE_MS);
});

// A release, or a click straight on the track, fires "change" as well as
// "input". refreshEstimate is already bound to "change" (above) and
// maybePersistFieldSettings is bound to it further down, both unchanged
// by this task, so the release is fully handled without this listener;
// all it does is cancel the debounce that would otherwise repeat the
// same request 400ms later. Order among the three does not matter,
// because cancelling a pending timer is the same act before or after the
// work it would have duplicated.
$("tile-size").addEventListener("change", () => {
  clearTimeout(tileSizeDebounce);
  renderTileSize();
});

// Populated at load, not only from boot(): a boot that fails on a stale
// token (see its own catch) would otherwise leave the slider sitting
// beside two blank lines of explanation.
renderTileSize();

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

// --- elevation model -----------------------------------------------------
//
// Task 28. The options are not written into index.html: they come from
// GET /api/sources, where the elevation source declares them, the same
// registry-driven convention the API key fields already use. A second
// hand-maintained copy of a vocabulary in markup is exactly the drift
// mapgen.categories' own docstring warns about.
//
// The whole control hides itself when no source offers a choice. That is
// not defensive habit: it is what makes this page keep working against a
// server that predates the setting, and what stops an empty select
// sending an empty model on the next estimate.

function hideElevationModel() {
  // Set here rather than left to a `hidden` attribute in index.html, the
  // same reasoning closeSettingsPanel and hideProgress already document:
  // a real browser honours the markup before any script runs, but the
  // Node harness's synthetic elements only ever see what a script sets.
  $("demtype-field").hidden = true;
  $("demtype").innerHTML = "";
}
hideElevationModel();

function elevationModelChoices(sources) {
  const source = (sources || []).find(
    (s) => Array.isArray(s.demtype_choices) && s.demtype_choices.length
  );
  return source ? source.demtype_choices : [];
}

// saved is whatever the config file actually holds, which is not
// guaranteed to be one of the offered models: it can be a value set by
// hand, or one this page's own server offers and a later one does not.
// A <select> handed a value none of its options carry silently reports
// "" instead, and payload() would then send an empty model and be
// refused. Keeping the saved value as an option of its own is the same
// answer applyTileSizeBounds gives for a tile size outside the slider's
// range: widen to fit the setting rather than quietly rewrite it, and
// say on screen that it is being kept.
function renderElevationModel(sources, saved) {
  const choices = elevationModelChoices(sources);
  if (!choices.length) {
    hideElevationModel();
    return;
  }
  const current = typeof saved === "string" ? saved : "";
  const offered = choices.some((choice) => choice.id === current);
  const options = choices.map(
    (choice) =>
      `<option value="${escapeHtml(choice.id)}">${escapeHtml(choice.label)}</option>`
  );
  if (current && !offered) {
    options.push(
      `<option value="${escapeHtml(current)}">` +
        `${escapeHtml(current)}, saved earlier and kept</option>`
    );
  }
  $("demtype").innerHTML = options.join("");
  $("demtype").value = current || choices[0].id;
  $("demtype-field").hidden = false;
}

// Persisted immediately and unconditionally, like the theme and the API
// key fields, because there is no "value the estimate just rejected"
// case to defer past the way output-root/tile-size/overlap have. Not
// awaited before refreshing, unlike the key fields: the model travels in
// the estimate's own payload (see payload()), so the estimate does not
// depend on the save having landed first, and the two are genuinely
// independent here.
$("demtype").addEventListener("change", () => {
  persistConfig({ elevation_demtype: $("demtype").value });
  refreshEstimate();
});

// The theme setting persists immediately, unconditionally, like an API
// key field: there is nothing here an estimate could reject, so there is
// no "value the estimate just rejected" case to defer past the way
// output-root/tile-size/overlap do. Applied to the page and repainted on
// the tile grid at once, not only on the next reload: a setting the
// owner just changed should look changed immediately.
$("theme").addEventListener("change", () => {
  const value = $("theme").value;
  applyTheme(value);
  repaintTileGridTheme();
  persistConfig({ theme: value });
});

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

// The sources this specific job was started with, for classifyTiles: read
// from the same payload() the job start request itself sent, never
// re-read from the checklist afterwards, since the owner is free to
// change ticks while a job runs and this must describe the job actually
// in flight, not whatever the form currently shows.
let activeJobSourceIds = [];
// The same reasoning applied to the estimate the progress bar weighs
// itself by and the countdown starts from: snapshotted when Download is
// pressed, so a later estimate for a different extent, which the owner
// is free to ask for while this job runs, cannot retune a bar that is
// describing the job already in flight.
let activeJobSeconds = 0;
let activeJobSourceSeconds = {};
let jobStartedAt = 0;

function hideProgress() {
  $("progress").hidden = true;
  $("progress").className = "progress";
  $("progress").setAttribute("aria-valuenow", "0");
  $("progress-fill").style.width = "0%";
  $("progress-text").textContent = "";
  $("progress-note").hidden = true;
  $("progress-note").textContent = "";
}
// Set here rather than left to index.html's own hidden attribute, the
// same reasoning clearTileGrid and closeSettingsPanel already document.
hideProgress();

// Draws the bar and its line of copy from the summary, the job's own
// state, and the clock. Split from the poll loop so the whole of it is
// reachable from a test with a fabricated job, and split from
// remainingLabel so the arithmetic can be tested without a DOM at all.
function renderProgress(summary, job, elapsedSeconds) {
  const percent = Math.max(0, Math.min(100, Math.round(summary.fractionDone * 100)));
  const progress = $("progress");
  progress.hidden = false;
  progress.className = "progress";
  $("progress-fill").style.width = `${percent}%`;
  progress.setAttribute("aria-valuenow", String(percent));

  let note = "";
  if (job.state === "running") {
    const label = remainingLabel({
      fractionDone: summary.fractionDone,
      fractionFetched: summary.fractionFetched,
      fractionSkipped: summary.fractionSkipped,
      elapsedSeconds,
      staticSeconds: activeJobSeconds,
      subdivisions: summary.subdivisions,
    });
    $("progress-text").textContent = `${percent}% done, ${label.text}`;
    note = label.note;
  } else if (job.state === "done") {
    // Forced to 100 from the job's own state rather than from the
    // fraction: the server has said the package is complete, and that is
    // the stronger fact. A bar left at 98% beside a finished download
    // would make the owner go looking for what was missing.
    $("progress-fill").style.width = "100%";
    progress.setAttribute("aria-valuenow", "100");
    $("progress-text").textContent = "Finished. Every tile downloaded.";
  } else if (job.state === "stopped") {
    // Never 100, and never the failure styling: a stop is a deliberate
    // partial (Task 22), and how partial is the one thing worth being
    // able to read afterwards.
    progress.className = "progress stopped";
    $("progress-text").textContent =
      `Stopped at ${percent}%. The package holds everything that had already downloaded.`;
  } else {
    progress.className = "progress failed";
    $("progress-text").textContent = `Failed at ${percent}%. See the log.`;
  }

  $("progress-note").textContent = note;
  $("progress-note").hidden = !note;
}

$("download").addEventListener("click", async () => {
  $("log").innerHTML = "";
  try {
    const requestPayload = payload();
    const started = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify(requestPayload),
    });
    jobId = started.id;
    activeJobSourceIds = requestPayload.sources;
    activeJobSeconds = lastSizing && lastSizing.seconds > 0 ? lastSizing.seconds : 0;
    activeJobSourceSeconds = (lastSizing && lastSizing.sourceSeconds) || {};
    // Started before the first poll rather than at the first event: a run
    // whose first tile takes half a minute has genuinely been running for
    // half a minute, and elapsed time that only starts counting once
    // something has happened would flatter every projection made from it.
    // The POST above is already answered by this point, so this never
    // includes time the server had not actually started the job.
    jobStartedAt = Date.now();
    hideProgress();
    // A fresh job restarts the grid's own bookkeeping (every rectangle
    // back to "pending") without redrawing the rectangles themselves:
    // the extent has not changed since the estimate that enabled
    // Download, so the geometry is still correct, only the progress is
    // new.
    for (const [tileId, rect] of tileRectangles) {
      tileState.set(tileId, "pending");
      rect.setStyle(tileStyleFor("pending"));
    }
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
        // The bar would otherwise sit frozen at whatever the last poll
        // saw, with a countdown still promising a number that nothing is
        // updating any more. The job may well still be running; this
        // page just cannot see it.
        $("progress-text").textContent =
          "Lost contact with the job. The bar has stopped updating.";
        $("progress-note").hidden = true;
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
      // One summary, read twice: the grid paints from its tile states and
      // the bar reads its fractions, so the two can never disagree about
      // the same run.
      const summary = summariseJob(
        [...tileRectangles.keys()],
        activeJobSourceIds,
        job.events,
        job.state === "running",
        activeJobSourceSeconds
      );
      paintTileStates(summary.tileStates);
      renderProgress(summary, job, (Date.now() - jobStartedAt) / 1000);
      if (job.state !== "running") {
        clearInterval(poller);
        $("cancel").hidden = true;
        $("download").disabled = false;
        if (job.state === "done") log(`Finished: ${job.result_root}`);
        // Stopped is a deliberate, successful outcome, not a failure: the
        // owner asked for this, and the package at result_root is real
        // and usable (see survey.json's own stopped field). Logged plainly,
        // never in the red "fail" styling the line below uses for an
        // actual failure.
        else if (job.state === "stopped") log(`Stopped: ${job.result_root}`);
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
    // Bounds first, value second: see applyTileSizeBounds. A saved size
    // outside the slider's own range widens the slider rather than being
    // rewritten by it.
    applyTileSizeBounds(config.tile_size_m);
    $("tile-size").value = config.tile_size_m;
    renderTileSize();
    $("overlap").value = config.overlap_m;
    if (config.last_region) $("region").value = config.last_region;
    $("theme").value = config.theme || "auto";
    applyTheme(config.theme || "auto");
    savedConfig = config;

    const sources = await api("/api/sources");
    currentSources = sources;
    renderSources(sources);
    renderApiKeys(sources, config);
    renderElevationModel(sources, config.elevation_demtype);
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
