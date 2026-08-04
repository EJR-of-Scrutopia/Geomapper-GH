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
// least one event but not yet settled), done, and failed. Failed is the
// one state the owner would want to notice before deciding they have
// enough, per the brief, so it wins over the other three wherever a tile
// could be read as more than one.
//
// Still four after Task 31, which added the explanation behind a failed
// tile and deliberately added no colour to carry it. A fifth state would
// have to MEAN something the other four do not, and "failed, and there
// is a sentence about it" is not a different condition from failed: every
// red tile has a reason, so a colour that marked the ones that do would
// mark all of them. The explanation is text, and it is in two places that
// are text: a tooltip on the tile and a list beside the progress bar.
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

// Task 36, item 4: "if a tile needs to subdivide show the subdivision
// too". Drawn as a dashed, heavier outline OVER whichever of the four
// state colours the tile already has, and deliberately neither a fifth
// state nor four smaller rectangles.
//
// Not the quarters themselves, and that is the argued part. The client
// has never computed a single piece of tile geometry: every rectangle on
// this map comes from the server's own tile_grid (see package.py's
// _geometry_summary, which sends the plan's rectangles keyed by the same
// tile_id the progress events carry), and that is what makes it
// impossible for the grid to disagree with the run about where a tile
// is. tile_subdivided carries a count and a depth, never four
// rectangles, so drawing the quarters would mean this file working out
// where the halfway lines of a tile fall from bounds it was handed for a
// different purpose. The first time that arithmetic and build_tiles
// disagreed, the map would be quietly wrong with nothing to say so. The
// parent is marked instead, and the tooltip says into how many pieces.
//
// Not a colour either, for the reason Task 31 already settled when it
// refused one for "failed, and there is a sentence about it": a fifth
// colour has to MEAN a fifth condition. Being split is not a state a
// tile is in INSTEAD of pending, active, done or failed. It is a second
// fact about a tile that is in one of those four, it outlives the state
// it happened in, and a split tile can and usually does finish
// perfectly well. So it is drawn as a second property of the same
// rectangle, which is what lets one read as "in progress, and doing four
// times the work of its neighbours" at a glance. That is the question
// the owner actually asked this to answer: why one square is taking so
// long.
const TILE_SPLIT_STYLE = { dashArray: "5 4", weight: 2 };
const TILE_SPLIT_LABEL = "Split into pieces";

let tileRectangles = new Map(); // tile_id -> Leaflet rectangle
let tileState = new Map(); // tile_id -> "pending" | "active" | "done" | "failed"
// tile_id -> { pieces, depth } for a tile this run had to split. Kept
// beside the state rather than folded into it for the reason above: the
// two are independent, and a tile carries its split through every state
// it goes on to reach.
let tileSubdivided = new Map();
// The tooltip html currently bound to each rectangle, so a poll that
// changes nothing rebinds nothing: bindTooltip on a layer whose tooltip
// is open closes it first (see Leaflet's own implementation), and doing
// that every 700ms would make a tooltip the owner is reading flicker or
// vanish under the cursor.
let tileTooltips = new Map(); // tile_id -> bound tooltip html
// The failure records the panel is currently showing, kept because a
// click on the map arrives between polls and has to be able to look up
// what it just selected without waiting for the next one.
let lastTileFailures = [];
let selectedFailedTile = null;

function tileStyleFor(state, subdivided = false) {
  const palette = TILE_COLOURS[effectiveTheme()];
  const base = palette[state] || palette.pending;
  // dashArray is always present, null when this tile did not split.
  // Leaflet's SVG renderer sets stroke-dasharray when the option is
  // truthy and REMOVES it when it is not, so the explicit null is what
  // actually takes the dashes back off a rectangle being repainted for
  // some other reason, a second run over the same extent in particular.
  return subdivided ? { ...base, ...TILE_SPLIT_STYLE } : { ...base, dashArray: null };
}

function clearTileGrid() {
  for (const rect of tileRectangles.values()) map.removeLayer(rect);
  tileRectangles = new Map();
  tileState = new Map();
  tileSubdivided = new Map();
  tileTooltips = new Map();
  renderTileFailures([]);
  renderTileLegend();
}

// --- why a tile is red ---------------------------------------------------
//
// Task 31. The colour was Task 22's and it says a tile did not arrive;
// what it never said is why, and "this tile failed" is not an
// explanation, it is the colour written out in words. Task 30 made the
// reasons exist: every tile_failed event now carries a `kind` for code
// and a `reason` written for the owner, and the same records reach
// survey.json as tile_failures. This is the browser reading them.
//
// One sentence per failed tile, in the same shape package.py's own
// describe_tile_failures composes for the terminal and for
// IncompleteSurveyError, because the owner should not have to learn two
// readings of the same fact depending on which window it appears in.
//
// Composed once and read by both places that show it, the map's tooltip
// and the list beside the progress bar. The first draft of this file had
// the two assembling the same sentence separately, and a mutation that
// took the retried clause out of one of them left every check green off
// the other: two copies of one sentence is how two views of the same run
// come to disagree, which is the argument summariseJob already makes
// about the grid and the bar.
function tileFailureReason(record) {
  // The reason itself, never a substitute for it. The fallback names the
  // gap rather than restating the colour: a run that failed a tile and
  // recorded no sentence for it is a different thing from a timeout, and
  // saying "this tile failed" here would hide that behind the one string
  // this whole feature exists to stop showing. package.py's own ledger
  // makes the same distinction and for the same reason.
  const reason = record.reason || "No reason was recorded for this tile.";
  // Retried is the difference between a service that was busy and one
  // that is still saying no, which is the difference between waiting and
  // going to do something else.
  const again = record.retried > 0 ? " Retried, and it failed again." : "";
  return `${reason}${again}`;
}

function tileFailureLine(record) {
  return `${record.source} ${record.tile_id}: ${tileFailureReason(record)}`;
}

// Hover, and on a touchscreen tap, since Leaflet opens a non-permanent
// tooltip on click as well as on mouseover. Sticky so it follows the
// pointer across a rectangle that can be a good fraction of the map
// rather than sitting at a fixed corner of it.
const TILE_FAILURE_TOOLTIP = {
  sticky: true,
  direction: "top",
  opacity: 1,
  className: "tile-failure-tooltip",
};

// The same box for a tile that has a note but no failure, in the page's
// ordinary line colour rather than the warning one.
const TILE_NOTE_TOOLTIP = { ...TILE_FAILURE_TOOLTIP, className: "tile-note-tooltip" };

// A split tile's own sentence, Task 36 item 4. The dashes say a tile was
// split; this says into how many pieces, which is the difference between
// "something is happening here" and "this square is four requests, which
// is why it has gone quiet". Same shape as the failure sentence, and in
// the same tooltip, because a tile can be both and the owner should not
// have to find two places to read about one rectangle.
//
// depth is on the event and is deliberately not said. For a tile OF THE
// PLAN it is always 1: a quarter that is itself over the cap reports its
// own tile_subdivided under the quarter's id ("<parent>_q10"), which is
// not a tile of the plan at all and is dropped by the same
// state.has(tile_id) guard that keeps the subdivision COUNT honest (see
// review finding I5 in summariseJob). Printing a number that is 1 in
// every run this server can produce would be dressing a constant up as
// information.
function tileSubdivisionLine(record) {
  const count =
    record.pieces > 0
      ? `${record.pieces} ${record.pieces === 1 ? "piece" : "pieces"}`
      : "pieces";
  return `Too dense for one request: split into ${count} and fetched a piece at a time.`;
}

// Leaflet assigns a string tooltip through innerHTML, so this escapes.
// The reasons are composed server-side from a fixed vocabulary and never
// from a URL or a raw exception (see TileFailure's own docstring), so
// there is nothing hostile expected here; escaping is what makes that a
// property of this line rather than of a promise made somewhere else.
//
// One composer for both facts about a rectangle, rather than two that
// each bind their own tooltip: bindTooltip REPLACES whatever was bound
// before, so two owners of one tooltip would mean whichever ran last won
// and the other fact vanished.
function tileTooltipHtml(records, split) {
  const lines = [];
  if (split) lines.push(escapeHtml(tileSubdivisionLine(split)));
  for (const record of records || []) lines.push(escapeHtml(tileFailureLine(record)));
  return lines.join("<br />");
}

function failuresByTile(records) {
  const byTile = new Map();
  for (const record of records) {
    if (!byTile.has(record.tile_id)) byTile.set(record.tile_id, []);
    byTile.get(record.tile_id).push(record);
  }
  return byTile;
}

// Binds a tooltip to every rectangle that has something to say and takes
// it off every rectangle that does not. The second half is the half that
// matters: a tile whose retry succeeded, or one the verify pass found on
// disk after all, must not be left carrying the sentence explaining a
// problem it no longer has.
function paintTileTooltips(records, subdivisions) {
  const byTile = failuresByTile(records);
  for (const [tileId, rect] of tileRectangles) {
    const forTile = byTile.get(tileId);
    const split = subdivisions ? subdivisions.get(tileId) : null;
    const html = tileTooltipHtml(forTile, split);
    if ((tileTooltips.get(tileId) || "") === html) continue;
    if (html) {
      // The warning border belongs to a reason a tile is red. A tile that
      // only split is not a problem at all, so it gets the neutral box
      // rather than borrowing the colour that means something did not
      // arrive.
      rect.bindTooltip(html, forTile ? TILE_FAILURE_TOOLTIP : TILE_NOTE_TOOLTIP);
      tileTooltips.set(tileId, html);
    } else {
      rect.unbindTooltip();
      tileTooltips.delete(tileId);
    }
  }
}

// The list beside the progress bar. The map answers "why is THIS one
// red"; this answers "what went wrong in this run", which is the question
// the owner has without having to first find a small red rectangle among
// seventy-two and know that hovering it does anything. It appears on its
// own the moment a run has a failure and stays afterwards, like the bar.
function renderTileFailures(records) {
  lastTileFailures = records || [];
  if (!lastTileFailures.some((record) => record.tile_id === selectedFailedTile)) {
    selectedFailedTile = null;
  }
  const box = $("tile-failures");
  const list = $("tile-failures-list");
  if (!lastTileFailures.length) {
    box.hidden = true;
    $("tile-failures-heading").textContent = "";
    list.innerHTML = "";
    return;
  }
  box.hidden = false;
  const noun = lastTileFailures.length === 1 ? "tile" : "tiles";
  $("tile-failures-heading").textContent =
    `${lastTileFailures.length} ${noun} did not download. ` +
    `Hover or click a red tile on the map to find it here.`;
  list.innerHTML = lastTileFailures
    .map((record) => {
      const selected = record.tile_id === selectedFailedTile ? " selected" : "";
      return (
        `<li class="tile-failure${selected}">` +
        `<span class="tile-failure-tile">${escapeHtml(record.source)} ${escapeHtml(record.tile_id)}</span>` +
        `<span class="tile-failure-reason">${escapeHtml(tileFailureReason(record))}</span></li>`
      );
    })
    .join("");
}

// Clicking a red tile marks its entry in the list, which is what turns a
// rectangle on a grid of seventy-two into a row of text the owner can
// actually read. Clicking anything else clears the mark rather than
// leaving a stale one pointing at a tile they are no longer looking at.
function selectFailedTile(tileId) {
  // While the draw tool is armed, a press on the map belongs to it: it is
  // placing a corner, not asking about a failure. Leaflet passes a click
  // on a rectangle through to the map as well, which is what makes
  // drawing a new extent over an old grid work at all, so this guard is
  // what keeps the two from both acting on the same gesture.
  //
  // suppressNextMapClick covers the other half of that, added with the
  // press-drag-release tool (Task 36, item 2): a completed drag disarms
  // on the release, and the click the browser then fires would arrive
  // with `drawing` already false. Leaflet does suppress a click after a
  // drag of its own, but only for a drag IT handled, and map dragging is
  // switched off for the whole time this tool is armed.
  if (drawing || suppressNextMapClick) return;
  const failed = lastTileFailures.some((record) => record.tile_id === tileId);
  selectedFailedTile = failed ? tileId : null;
  renderTileFailures(lastTileFailures);
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
    // Bound once, at creation, for every tile rather than only the ones
    // that later fail: a rectangle's failure comes and goes across a run
    // (a retry can clear it, the verify pass can clear it), and adding or
    // removing a listener each time it changes is a second piece of
    // bookkeeping that can drift from the first. selectFailedTile decides
    // what a click means from the failures the panel is currently showing.
    rect.on("click", () => selectFailedTile(tile.tile_id));
    rect.addTo(map);
    tileRectangles.set(tile.tile_id, rect);
  }
  renderTileLegend();
}

function paintTileStates(states, subdivisions = new Map()) {
  const hadSplits = tileSubdivided.size > 0;
  // Splits first, and in their own pass, because a tile can split without
  // its state changing at all: the event arrives while the tile is
  // active and it stays active for however long the pieces take. A
  // single loop keyed on the state map would skip exactly that tile, on
  // the "nothing changed" line, which is the one moment the owner is
  // asking the grid about.
  //
  // A tile never un-splits within a run, so this only ever grows. A new
  // run empties it (see the Download handler) and a new extent empties it
  // with the whole grid (see clearTileGrid).
  for (const [tileId, record] of subdivisions) {
    if (tileSubdivided.has(tileId)) continue;
    tileSubdivided.set(tileId, record);
    const rect = tileRectangles.get(tileId);
    if (rect) rect.setStyle(tileStyleFor(tileState.get(tileId) || "pending", true));
  }
  for (const [tileId, state] of states) {
    if (tileState.get(tileId) === state) continue;
    tileState.set(tileId, state);
    const rect = tileRectangles.get(tileId);
    if (rect) rect.setStyle(tileStyleFor(state, tileSubdivided.has(tileId)));
  }
  // Only when the legend's own content would actually change, not every
  // 700ms poll: rebuilding its innerHTML is what would make a legend the
  // owner is reading flicker, the same argument the tooltip memo above
  // makes.
  if (hadSplits !== tileSubdivided.size > 0) renderTileLegend();
}

// Repaints every rectangle already on the map in its current state, using
// whichever palette effectiveTheme() now resolves to: called when the
// theme setting changes, since the tile-by-tile state itself has not
// changed, only which colours represent it.
function repaintTileGridTheme() {
  for (const [tileId, rect] of tileRectangles) {
    rect.setStyle(tileStyleFor(tileState.get(tileId) || "pending", tileSubdivided.has(tileId)));
  }
  renderTileLegend();
}

// Task 36, item 5. The legend and the progress bar now share one strip
// under the map, and each of them hides on its own account: there is no
// legend before an extent has been drawn, and no bar before a download
// has been started. The strip itself has to go when NEITHER has anything
// to show, or the page carries an empty band under the map for the whole
// of the time before the first estimate.
//
// Read back off the two children rather than kept as a third piece of
// state, so this cannot come to disagree with what is actually on screen.
// Called from every place that hides or shows either one.
function syncMapStatus() {
  $("map-status").hidden = $("tile-legend").hidden && $("progress").hidden;
}

function renderTileLegend() {
  const legend = $("tile-legend");
  if (tileRectangles.size === 0) {
    legend.hidden = true;
    legend.innerHTML = "";
    syncMapStatus();
    return;
  }
  legend.hidden = false;
  syncMapStatus();
  const items = Object.keys(TILE_STATE_LABELS).map((state) => {
    const style = tileStyleFor(state);
    return (
      `<span class="tile-legend-item">` +
      `<span class="tile-legend-swatch" style="background:${style.fillColor}"></span>` +
      `${escapeHtml(TILE_STATE_LABELS[state])}</span>`
    );
  });
  // Only once a tile has actually split. A legend entry for something
  // that has not happened is a fifth thing to read past on every run,
  // and this strip has one line to work with (see the progress bar
  // beside it): the entry appears when there is something to explain and
  // takes its width back when there is not.
  if (tileSubdivided.size > 0) {
    items.push(
      `<span class="tile-legend-item">` +
        `<span class="tile-legend-swatch split"></span>` +
        `${escapeHtml(TILE_SPLIT_LABEL)}</span>`
    );
  }
  legend.innerHTML = items.join("");
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
// --- when a tile is done, Task 36 item 6 --------------------------------
//
// This used to settle a tile only once EVERY selected source had emitted
// source_done, or the job had stopped. That was a blunt stand-in for a
// real rule and it was put there for a real reason: Overture emits one
// event per tile PER TYPE, and an earlier version made a tile look
// finished after the first of eight. The consequence was what the owner
// saw on their first real survey, which is that the whole grid went
// green at once at the very end, and a grid that only tells you a run
// has finished is telling you the one thing you already know.
//
// The honest rule is per tile and per source: a tile is DONE when every
// selected source has finished with THAT TILE, not when every source has
// finished with everything. A source has finished with a tile when
// either of these is true, and they are different strengths of the same
// claim:
//
//   it emitted source_done       it is finished with everything, so with
//                                this tile. package.py emits this once a
//                                source's whole fetch+merge pass is over.
//   it emitted an outcome for    tile_done, tile_skipped or tile_failed
//   this tile, unqualified       naming this tile of the plan, with no
//                                field saying it is one PART of that
//                                source's work for the tile.
//
// The qualifier is what keeps Overture honest, and it is a field on the
// events rather than a source id hardcoded here: every Overture tile
// event carries overture_type, because it is one type's worth of one
// tile, and the browser is never told how many types are in play (the
// selection is composed server-side from the categories). So Overture's
// per-tile events can never complete a tile and only its source_done
// can, which is exactly the behaviour the old rule was protecting.
//
// The other awkward shapes, and what this does with each:
//
//   elevation reports "whole-area", which is not a tile of the plan at
//   all, so it never reaches this bookkeeping and only elevation's own
//   source_done completes a tile for it. That is honest: a single
//   whole-extent download has no per-tile signal to report, which is the
//   same argument the fractions below already make for it, and it is
//   also what keeps Overture's decision NOT to adopt that convention
//   (see OvertureSource.fetch) meaning what it says.
//
//   a resume skips tiles, and package.py emits tile_skipped per source
//   for every tile its state.json already records as ok, before that
//   source's fetch is called. Those are unqualified, so a tile already
//   on disk for every layer is done in the first seconds of a resume
//   rather than at the end of it. That is the biggest single improvement
//   here for the way the owner actually works.
//
//   a tile no source has said anything about is pending, and a tile some
//   but not all of them have reported is active. "Active" is still
//   reached by any per-tile event including a qualified one, so an
//   Overture-only run still lights up from its first type, as it did.
//
// What this does NOT fix, and the report says so plainly: sources are
// fetched in the order the request lists them, which is osm, overture,
// elevation, and the last two have no per-tile granularity at all. In a
// three-layer run every tile therefore still becomes done within a
// second or two of the end, because that is genuinely when the last
// layer covering it lands. The rule is right; the ordering is what
// limits what it can show. An OpenStreetMap-only run, an elevation-only
// run and every resume all now move tile by tile.
//
// The second way to "done" is unchanged: the job has stopped running at
// all (job.state left "running"), which is the closing signal for a run
// that ended before every source got a source_done of its own, a stopped
// or a hard-failed run in particular.
//
// "failed" was sticky, and Task 31 stopped it being so, because Task 30
// made a later event for a failed tile mean something it could not mean
// before. A tile can now be fetched again, at the end of the run, and
// land: the stream is tile_failed, then tile_retrying, then tile_done,
// and a sticky rule leaves that tile red for a run that is complete. The
// brief is explicit that a tile which was retried and then succeeded is
// not a failure.
//
// So failure is no longer a state written into the state map as events go
// past. It is a LEDGER of live reasons, keyed by (source, tile), applied
// over the finished state map at the end, and it is kept by exactly the
// rules package.py's own _FailureLedger keeps:
//
//   tile_failed              records a reason for that source and tile,
//                            replacing any earlier one, so the retry's
//                            answer settles over the first pass's.
//   tile_done, tile_skipped  forget that source's reason for that tile.
//                            This is ledger.forget: a tile that is
//                            genuinely on disk must not keep the sentence
//                            explaining why it once was not.
//   verify_done              replaces the ledger wholesale with the
//                            server's own verdict for that phase.
//
// Keyed per SOURCE, not per tile, which is what makes the second rule
// safe: an Overture tile_done cannot clear an OpenStreetMap failure for
// the same ground. And a tile is red if any live reason names it, so a
// tile that failed one layer and finished another stays red, which is
// what it should be.
//
// verify_done is not decoration on top of the other two. It is the only
// event that reports the reverse correction: a tile recorded failed whose
// file the verify pass then found on disk is put right there and nowhere
// else, with no tile_done to announce it. Without reading it, that tile
// stays red on the map against a survey.json that says it is fine.
//
// A tile_failed arriving AFTER a tile_done for the same source still
// reads failed, and that has not changed: it is the ordinary shape of an
// OSM tile whose file turned out not to be there, and of every Overture
// type failure, since _record_tile_outcomes runs after fetch() has
// emitted its own events.
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
//
// "every tile already on disk reports tile_skipped" read as a
// description of the world and was, for two tasks, a wish. package.py
// filtered every tile its state.json already recorded as ok out of the
// list handed to fetch(), so no source ever saw those tiles and none of
// them reported anything, and a Stop-then-resume, the exact case this
// paragraph is about, arrived here with fractionSkipped stuck at 0. See
// review finding I6: package.py now emits the event where it does the
// skipping. Nothing in this function changed for it. The note is here so
// the next reader knows the sentence above rests on package.py's own
// resume loop rather than on each source remembering to be helpful.
function summariseJob(tileIds, sourceIds, events, jobRunning, sourceSeconds) {
  const state = new Map(tileIds.map((id) => [id, "pending"]));
  const finishedSources = new Set();
  const fetchedTiles = new Map(); // source id -> Set of plan tile ids
  const skippedTiles = new Map(); // source id -> Set of plan tile ids
  const subdividedTiles = new Map(); // plan tile id -> { pieces, depth }
  const liveFailures = new Map(); // failureKey(source, tile_id) -> record
  // Task 36, item 6. Plan tiles any source has said anything at all
  // about, and, per source, the plan tiles that source has FINISHED with.
  // The first decides pending from active, the second decides done. See
  // the rule written out above this function.
  const touchedTiles = new Set();
  const reportedTiles = new Map(); // source id -> Set of plan tile ids
  let phase = null; // { kind, source } for the status line, Task 36 item 5

  const setFor = (bucket, source) => {
    if (!bucket.has(source)) bucket.set(source, new Set());
    return bucket.get(source);
  };

  for (const event of events || []) {
    const nextPhase = PHASE_FOR_EVENT[event.event];
    if (nextPhase) {
      phase = {
        kind: nextPhase,
        source: event.source || "",
        // Task 38, item 5: which tile this event is about, and only when
        // it is the WHOLE of a tile of the plan. The two exclusions are
        // the ones this file already draws everywhere else: "whole-area"
        // is elevation's one event for the whole extent and is not a tile
        // at all (state.has says so), and an event carrying overture_type
        // is one type's share of a tile rather than work on that tile, so
        // naming it would say the run is on one square when it is
        // downloading the whole extent. Both fall back to the layer's own
        // name, which is what the owner asked for during a whole-extent
        // layer.
        tile:
          event.tile_id && state.has(event.tile_id) && !reportsPartOfTile(event)
            ? event.tile_id
            : "",
      };
    }
    if (event.tile_id && state.has(event.tile_id)) {
      if (event.event === "tile_failed") {
        liveFailures.set(failureKey(event.source, event.tile_id), failureRecord(event));
      } else if (event.event === "tile_done" || event.event === "tile_skipped") {
        liveFailures.delete(failureKey(event.source, event.tile_id));
        // Something happened to this tile. Enough for "active", never
        // enough on its own for "done": a qualified Overture event is
        // one type of eight and says nothing about the other seven.
        touchedTiles.add(event.tile_id);
      }
      // And this is the stronger claim: a source that has emitted an
      // outcome for this tile of the plan, with no field saying it is
      // one part of that source's work for it, has finished with it. See
      // reportsPartOfTile below for what the qualifier is and why it is
      // read off the event rather than hardcoded per source id.
      if (event.source && TILE_OUTCOME_EVENTS.has(event.event) && !reportsPartOfTile(event)) {
        setFor(reportedTiles, event.source).add(event.tile_id);
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
    // The run's own verdict, and the only place a failure the server has
    // since resolved is ever taken back. Replaces rather than merges, for
    // the same reason survey.json's tile_failures is a list and not a
    // history: this is what is wrong NOW, and a browser that kept
    // everything it had ever been told would go on showing a tile the
    // server has already put right.
    if (event.event === "verify_done") {
      liveFailures.clear();
      for (const record of event.failures || []) {
        if (!record || !state.has(record.tile_id)) continue;
        liveFailures.set(failureKey(record.source, record.tile_id), failureRecord(record));
      }
    }
    // Counted per TILE OF THE PLAN, and this sat outside the
    // state.has(event.tile_id) guard above, incrementing per EVENT, until
    // review finding I5. OsmSource emits one tile_subdivided per SPLIT,
    // and a quarter that is itself over the cap emits another under
    // tile_id "<parent>_q10", which is not a tile of the plan at all. One
    // dense tile split twice therefore read as "2 tiles were too dense",
    // and at the depth cap a single plan tile can produce five events, so
    // the note would have said "5 tiles were". A Set for the same reason
    // every other per-tile figure here uses one: the note claims a number
    // of TILES, so counting one twice is the single thing it must not do.
    // A Map rather than a Set since Task 36, item 4: the grid now says
    // into how many pieces as well as that it happened. Keyed the same
    // way and guarded the same way, so the count the note beside the bar
    // quotes (subdividedTiles.size) is exactly the number of TILES it
    // always was.
    if (event.event === "tile_subdivided" && state.has(event.tile_id)) {
      if (!subdividedTiles.has(event.tile_id)) {
        subdividedTiles.set(event.tile_id, subdivisionRecord(event));
      }
    }
  }

  // Per tile, per source. A source has finished with a tile if it has
  // finished with everything, or if it has reported that tile in full.
  const hasFinishedWith = (sourceId, tileId) =>
    finishedSources.has(sourceId) || (reportedTiles.get(sourceId) || EMPTY_TILE_SET).has(tileId);

  for (const tileId of state.keys()) {
    // sourceIds.length > 0 guards the vacuous case: [].every() is true,
    // and a summary asked for with no selected sources at all must not
    // read as a finished run.
    if (sourceIds.length > 0 && sourceIds.every((id) => hasFinishedWith(id, tileId))) {
      state.set(tileId, "done");
    } else if (touchedTiles.has(tileId) || sourceIds.some((id) => hasFinishedWith(id, tileId))) {
      state.set(tileId, "active");
    }
  }

  // The closing signal, unchanged: a run that has left "running" settles
  // whatever it reached. A stopped run's half-finished tiles are as
  // finished as they are ever going to be, and leaving them orange
  // against a bar that has stopped moving would read as still working.
  if (!jobRunning) {
    for (const [tileId, value] of state) {
      if (value === "active") state.set(tileId, "done");
    }
  }

  // The ledger, applied last, so failure wins over every other reading of
  // the same tile no matter which order the events arrived in. Sorted by
  // source then tile id, exactly as describe_tile_failures sorts them for
  // the terminal, so the same run reads the same way in both places
  // rather than in whatever order the failures happened to land.
  const tileFailures = [...liveFailures.values()].sort((a, b) =>
    a.source === b.source
      ? (a.tile_id < b.tile_id ? -1 : a.tile_id > b.tile_id ? 1 : 0)
      : a.source < b.source
      ? -1
      : 1
  );
  for (const record of tileFailures) state.set(record.tile_id, "failed");

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
    tileFailures,
    fractionDone,
    fractionSkipped,
    fractionFetched: Math.max(0, fractionDone - fractionSkipped),
    subdivisions: subdividedTiles.size,
    tileSubdivisions: subdividedTiles,
    phase,
  };
}

// --- what is happening now ------------------------------------------------
//
// Task 36, item 5. The bar says how far along a run is and the countdown
// says how much longer; neither says what the run is actually DOING, and
// "the description of whats happening" is what the owner asked to have
// beside them.
//
// This is the only place in this file that reads an event BY NAME, and
// the shape is what makes that safe. An event this table does not know
// leaves the phase exactly as it was, so a new event from the Python side
// can never make this line say something untrue; it can only leave it
// saying the last true thing until the next one it does know. The log at
// the bottom of the page still formats nothing by name and still shows
// every event in full, which is where a new event genuinely needs to
// appear.
//
// source_done is a phase of its own rather than a return to nothing:
// package.py emits it after that source's merge, so between one source
// finishing and the next one's first tile there is a real gap with a
// true thing to say about it.
const PHASE_FOR_EVENT = {
  job_started: "starting",
  tile_skipped: "downloading",
  tile_done: "downloading",
  tile_failed: "downloading",
  tile_subdivided: "downloading",
  source_failed: "finished-source",
  source_done: "finished-source",
  tile_retrying: "retrying",
  retry_waiting: "retrying",
  retry_postponed: "retrying",
  retry_done: "retrying",
  verify_done: "checking",
  bridge_started: "bridge",
  bridge_done: "bridge",
  project_setting_started: "setting",
  project_setting_written: "setting",
  job_finished: "finishing",
};

// Short, and short on purpose: this has one line on a strip it shares
// with the tile legend, and a sentence here would push the map up, which
// is the one thing the owner asked this move not to do.
//
// Task 38, item 5: "so that it just says which tile its doing and what
// package its downloading". Where there is a tile of the plan being
// worked, the line is that tile, by the same short id the log and the
// tooltips use: "osm r02_c05" is what the owner asked for, and the layer
// id beside a tile id reads as one address rather than a sentence. Where
// there is not, because the layer downloads the whole extent at once,
// the layer's own name is the true thing to say and the line says that
// instead.
const PHASE_WORDS = {
  starting: () => "Starting",
  downloading: (source, tile) =>
    tile ? `${source} ${tile}`.trim() : `Downloading ${sourceLabel(source)}`.trim(),
  "finished-source": (source) => `Finished ${sourceLabel(source)}`.trim(),
  // A retry names its tile too, and for the same reason: a run that has
  // gone back for one square should say which square.
  retrying: (source, tile) =>
    tile ? `Retrying ${source} ${tile}`.trim() : `Retrying ${sourceLabel(source)}`.trim(),
  checking: () => "Checking the files",
  bridge: () => "Writing the Urbano bridge",
  setting: () => "Writing the project setting",
  finishing: () => "Writing the package",
};

// The package this run is building, as its own name rather than the whole
// path: the folder is <output root>/<region>/<date>_<site>, and the last
// segment is the one part that says which survey this is. Everything
// before it is the same for every run the owner makes, and this line has
// one row of a shared strip to work in.
//
// Both separators, because the path is composed server-side by pathlib on
// Windows and arrives with backslashes, and because nothing here should
// depend on which platform the server is running on.
function packageStem(path) {
  const trimmed = String(path || "").replace(/[\\/]+$/, "");
  if (!trimmed) return "";
  const parts = trimmed.split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

// The name the owner picked the layer by, from GET /api/sources, not an
// id invented here: "OpenStreetMap" is what the checklist says and what
// the licence tooltip says, so it is what this should say too. Falls back
// to the id, which is what a page whose /api/sources call failed has.
function sourceLabel(sourceId) {
  const source = (currentSources || []).find((entry) => entry.id === sourceId);
  return (source && source.display_name) || sourceId || "";
}

function phaseLabel(phase) {
  if (!phase) return "Starting";
  const words = PHASE_WORDS[phase.kind];
  return words ? words(phase.source, phase.tile || "") : "";
}

// pieces and depth, read once and defaulted, for the same reason
// failureRecord reads a failure's fields once: everything downstream can
// then assume numbers. A missing or nonsensical count is carried as 0 and
// the sentence says "pieces" rather than inventing a number, which is
// what a page served against an older server would meet.
function subdivisionRecord(event) {
  const pieces = Number(event.pieces);
  const depth = Number(event.depth);
  return {
    pieces: Number.isFinite(pieces) && pieces > 0 ? pieces : 0,
    depth: Number.isFinite(depth) && depth > 0 ? depth : 0,
  };
}

// --- what a source's own event claims about a tile, Task 36 item 6 ------

// The three events that are an OUTCOME for a tile, as opposed to
// something happening to it on the way (tile_subdivided, tile_retrying).
// tile_failed is one of them: a source that failed a tile has finished
// with that tile, it will not attempt it again in this pass, and the
// failure ledger is what decides the colour afterwards.
const TILE_OUTCOME_EVENTS = new Set(["tile_done", "tile_skipped", "tile_failed"]);

// Allocated once rather than per tile per poll: hasFinishedWith asks for
// this on every (tile, source) pair a source has said nothing about, and
// on a 72-tile three-layer run that is a couple of hundred empty Sets a
// second for no reason.
const EMPTY_TILE_SET = new Set();

// Whether this event is one PART of its source's work for the tile,
// rather than the whole of it.
//
// Read off a field the server actually sets, not from a list of source
// ids: OvertureSource stamps overture_type on every tile event it emits,
// because each one is one type's whole-extent download reported across
// the plan's tiles, and there are eight of them in a default run. The
// browser is never told how many, since the type selection is composed
// server-side from the categories, so an Overture event can never be the
// last word on a tile and only its source_done can be.
//
// A source id list here would have been shorter and would have been the
// wrong thing: it would say "Overture is special" when what is actually
// true is "an event that names a part is not an outcome for the whole",
// and the next source that downloads a tile in pieces would have to
// remember to add itself to a list in a browser file. The field is
// already in the stream; this reads it.
//
// package.py's own resume skips are emitted by package.py and carry no
// overture_type even for Overture, which is right and deliberate: they
// come from state.json recording that tile as ok for that source, which
// is a claim about the whole of that source's work for it.
const PART_OF_TILE_FIELDS = ["overture_type"];

function reportsPartOfTile(event) {
  return PART_OF_TILE_FIELDS.some(
    (field) => event[field] !== undefined && event[field] !== null && event[field] !== ""
  );
}

// A tile with no source on the event is keyed under the empty string
// rather than dropped: the real event stream always carries one, and a
// test fixture or an older server that does not should still have its
// failures cleared by its own later tile_done, which keying on undefined
// consistently achieves. The separator is a character no source id or
// tile id can contain, so two different pairs can never collide into one
// key by accident.
function failureKey(source, tileId) {
  return `${source || ""}\u0000${tileId}`;
}

// Every field read once, here, and defaulted, so nothing downstream has
// to guard: a record out of this function always has five fields of the
// right type. Task 30 guarantees the event carries all of them, and this
// is what keeps a page served against an older server from rendering
// "undefined" at the owner as though it were a reason.
function failureRecord(event) {
  const retried = Number(event.retried);
  return {
    source: typeof event.source === "string" ? event.source : "",
    tile_id: String(event.tile_id),
    kind: typeof event.kind === "string" ? event.kind : "",
    reason: typeof event.reason === "string" ? event.reason : "",
    retried: Number.isFinite(retried) && retried > 0 ? retried : 0,
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
    // Task 38, item 5: "then the time '1 minute left' instead of
    // additional text after that time like in the screen shot". The
    // branch this came from used to be spelled out here, ", from the rate
    // so far" against ", from the estimate". It is the routine suffix the
    // owner asked to lose and nothing else went with it: the branch is
    // still returned, still decided by the same crossover, and still what
    // the tests read. What the honesty rules actually ask for survives in
    // full, because they are about the NUMBER: formatRemaining still
    // refuses to say anything finer than a minute, and the two branches
    // below still refuse to count down to zero or to invent a figure
    // there is no evidence for.
    return { text: `${formatRemaining(remaining)} left`, note, branch: "measured" };
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
    //
    // "Still running." went with the routine suffixes above, and only
    // that: the line beside this one now names the tile being worked, so
    // a second sentence saying the run is still going would be the same
    // fact twice on a strip with room for neither.
    return { text: "Taking longer than the estimate.", note, branch: "overrun" };
  }
  return { text: `${formatRemaining(remaining)} left`, note, branch: "estimate" };
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
  // Task 38, item 2. Every way of setting an extent comes through this
  // one function, which is why the corner handles are put in step here
  // and nowhere else: a drawn rectangle, a pasted bbox, a captured
  // viewport, a chosen place and an edit of the rectangle itself all end
  // on this line, so all five get handles without any of them having to
  // know that handles exist.
  syncExtentHandles();
  clearTimeout(suggestDebounce);
  suggestDebounce = setTimeout(suggestNames, SUGGEST_DEBOUNCE_MS);
  refreshEstimate();
}

// --- draw extent: press, drag, release ---------------------------------
//
// Task 36, item 2. This was click a corner, move, click the opposite
// corner, and the owner asked for "a more standard way of doing this
// with click and drag", which is what every other map tool does and what
// their hand expects. The button click still only ARMS the tool; nothing
// is drawn by pressing it. The press on the map fixes one corner, every
// mousemove while the button is held redraws a live preview rectangle
// from that corner to the cursor, and the release fixes the opposite
// corner and commits it via setBBox, the one place that actually
// replaces the committed extent. The preview rectangle is still its own
// separate layer specifically so a cancelled draw (Escape) can remove
// only the preview and never touch whatever extent was already
// committed.
//
// Map dragging has to be switched off while the tool is armed, and that
// is not a nicety: Leaflet's own Drag handler is watching the same
// mousedown, so without this the map would pan under the cursor for the
// whole length of every rectangle the owner tried to draw. Switched off
// on arming rather than on the press itself, so the very first pixel of
// movement is already the rectangle's and never the map's, and switched
// back on by disarmDrawing, which every exit from the tool goes through.

let firstCorner = null;
let previewRectangle = null;
// A press-drag-release ends with the browser firing a click as well, on
// whatever ancestor the press and the release have in common. That click
// is part of the gesture that has just finished, not a new question
// about a tile, so selectFailedTile has to stay out of its way in
// exactly the sense it already stays out of an armed tool's way. Set on
// the release, and cleared by the next click OR the next press, so it
// can never outlive one interaction even if the release happened
// somewhere no click ever followed.
let suppressNextMapClick = false;

function disarmDrawing() {
  drawing = false;
  firstCorner = null;
  if (previewRectangle) {
    map.removeLayer(previewRectangle);
    previewRectangle = null;
  }
  map.dragging.enable();
  $("draw").textContent = "Draw extent";
  $("draw").className = "";
  map.getContainer().style.cursor = "";
  // Task 38, item 2: the corner handles come back once the tool is put
  // away, and go while it is armed. Called here rather than only in the
  // button's own handler because every exit from the tool goes through
  // this function, which is the same reason panning is switched back on
  // here.
  syncExtentHandles();
}

$("draw").addEventListener("click", () => {
  // Always starts from a clean slate: clicking the button again mid-draw
  // discards whatever corner and preview rectangle already existed,
  // rather than leaving them stranded with no way back short of Escape.
  disarmDrawing();
  drawing = true;
  // Task 38, item 2: an armed tool means a new rectangle, so the old
  // one's handles are out of the way for the whole of the gesture.
  syncExtentHandles();
  map.dragging.disable();
  $("draw").textContent = "Drag a rectangle";
  $("draw").className = "armed";
  map.getContainer().style.cursor = "crosshair";
});

map.on("mousedown", (event) => {
  suppressNextMapClick = false;
  if (!drawing) return;
  firstCorner = event.latlng;
  $("draw").textContent = "Release to finish";
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

map.on("mouseup", (event) => {
  if (!drawing || !firstCorner) return;
  suppressNextMapClick = true;
  const a = firstCorner;
  const b = event.latlng;
  if (a.lat === b.lat || a.lng === b.lng) {
    // A drag that ended where it started, or that only ever moved along
    // one axis: a degenerate, zero-width or zero-height box. The server
    // would reject this outright (BBox.validated()), and it is not
    // something the estimate panel can safely surface while names are
    // still incomplete (see refreshEstimate's own /api/extent error
    // handling). Rejected here instead, at the release: disarm and leave
    // whatever extent was already committed untouched, exactly like
    // Escape. This is the case a press and release on the same point
    // produces, which is now the ordinary way to change your mind
    // mid-gesture rather than an unlikely double click on one pixel.
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

map.on("click", () => {
  suppressNextMapClick = false;
});

// --- select viewport ------------------------------------------------------
//
// Task 36, item 3. "so it will rectangle exactly what is on the viewport,
// thats a one press per time capture, so if i then scroll about after it
// doesnt just keep remaking the rectangle it stay on that one capture and
// if i press it again it recaptures that new viewport".
//
// A snapshot, and deliberately not a binding. There is a Leaflet event
// for exactly the tempting version of this (moveend, which fires after
// every pan and every zoom), and wiring this to it would give the owner
// a rectangle they cannot look away from: every scroll to check a
// neighbouring street would silently replace the extent, and the tile
// grid, the estimate and the folder preview would all be recomputed for
// ground they were only passing over. So this reads map.getBounds() once,
// here, at the press, and nothing in this file listens to the map moving
// at all.
//
// fit: false for the same reason. The rectangle IS the current view, so
// fitting to it would move the map to frame something that already fills
// it, and Leaflet's fitBounds pads the bounds and snaps to a whole zoom
// level, so the one thing it would reliably do is step the view the owner
// just captured back out again.
$("viewport").addEventListener("click", () => {
  // An armed draw tool is put away first: the owner has just said which
  // of the two ways of choosing an extent they meant, and leaving a
  // half-drawn preview and an unpannable map behind would answer neither.
  disarmDrawing();
  const bounds = map.getBounds();
  const captured = {
    west: bounds.getWest(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    north: bounds.getNorth(),
  };
  if (captured.west === captured.east || captured.south === captured.north) {
    // A view with no width or height at all is not something a laid-out
    // map produces, and a zero-area extent is refused everywhere else on
    // this page (the draw tool's own release, the pasted bbox). Refused
    // here too rather than left as the one way in.
    return;
  }
  setBBox(captured, false);
});

// --- editing the extent that is already there ---------------------------
//
// Task 38, item 2. "add an ability once a rectangle is drawn to drag it
// and resize it with just 4 corner nodes on the rectangle". Four corners
// and no edge midpoints, exactly as asked: a midpoint moves one side,
// which is a thing this owner has never wanted to do and four more things
// to hit by accident.
//
// Two gestures, and they are deliberately built on two different
// mechanisms, because the two halves are not the same problem:
//
//   a corner   is a real Leaflet marker with draggable: true, so
//              Leaflet's own Draggable does the pointer work. That
//              matters for more than tidiness. Draggable binds its
//              mousemove and mouseup on the DOCUMENT, so a release
//              outside the map still ends the gesture; it handles touch;
//              and its own static _dragging guard is what stops the map
//              panning underneath. None of that is worth re-writing here.
//
//   the body   has no element of its own to grab (the rectangle is a
//              path under the tile grid, which is drawn after it and
//              therefore over it), so it is driven from the map's own
//              mousedown/mousemove/mouseup with a plain "is this latlng
//              inside the box" test. That is immune to which layer
//              happens to be on top, which a listener on the rectangle
//              itself would not be.
//
// The handles are markers with a DIV icon rather than image markers or
// CircleMarkers, for three reasons that all point the same way: the
// vendored Leaflet ships without its marker PNGs (see vendor/README.md),
// a div is styled by styles.css and therefore themed by the same two
// palettes as the rest of the page rather than a second palette in JS the
// way the tile grid needs, and markers live in Leaflet's markerPane,
// which is above the overlayPane every path is drawn in. That last one is
// not a nicety either: the tile grid is redrawn on every estimate, after
// the extent, so anything drawn as a path would end up underneath it and
// be unclickable exactly when a grid is on screen.
const EXTENT_CORNERS = ["nw", "ne", "se", "sw"];
const OPPOSITE_CORNER = { nw: "se", ne: "sw", se: "nw", sw: "ne" };
// The div's own size, mirrored in styles.css. Both are needed: the icon
// size is what Leaflet positions and hit-tests, the CSS is what draws it.
const EXTENT_HANDLE_PX = 14;

let extentHandles = new Map(); // corner name -> Leaflet marker
// The corner gesture in flight, with the anchor captured once at its
// start. Recomputing "the opposite corner" on each move would be wrong
// the moment a drag crosses the anchor: past that point the corner the
// hand is holding is a different named corner of the normalised box, and
// the anchor would walk away under the cursor.
let extentCornerDrag = null; // { corner, anchorLat, anchorLng, box }
let extentBodyDrag = null; // { fromLat, fromLng, box, next }
// Whether a download is in flight. Editing the extent mid-download must
// not be possible: the grid on screen belongs to the run, the estimate
// the bar and countdown are weighed by belongs to the run, and the server
// has already been told what to fetch, so an edit could only ever make
// the page describe something the run is not doing.
let jobRunning = false;

// Handles are shown only when there is something to edit and only when
// editing means anything: no extent, a running download, or an armed
// draw tool (the owner has just said they are replacing this rectangle,
// not adjusting it) each take them away.
function extentEditable() {
  return Boolean(bbox) && !jobRunning && !drawing;
}

function cornerLatLng(box, corner) {
  return [
    corner === "nw" || corner === "ne" ? box.north : box.south,
    corner === "nw" || corner === "sw" ? box.west : box.east,
  ];
}

function boxFromCorners(aLat, aLng, bLat, bLng) {
  return {
    west: Math.min(aLng, bLng),
    south: Math.min(aLat, bLat),
    east: Math.max(aLng, bLng),
    north: Math.max(aLat, bLat),
  };
}

function insideBox(box, latlng) {
  return (
    latlng.lat >= box.south &&
    latlng.lat <= box.north &&
    latlng.lng >= box.west &&
    latlng.lng <= box.east
  );
}

function sameBox(a, b) {
  return a.west === b.west && a.south === b.south && a.east === b.east && a.north === b.north;
}

function removeExtentHandles() {
  for (const handle of extentHandles.values()) map.removeLayer(handle);
  extentHandles = new Map();
}

// Positions all four handles from whatever box is being shown, which is
// the committed one at rest and the live one mid-drag. exceptCorner is
// the handle currently under the hand: Leaflet is placing that one and
// setting its position from here would fight the drag.
function syncExtentHandles(box = bbox, exceptCorner = null) {
  if (!extentEditable() || !box) {
    removeExtentHandles();
    return;
  }
  for (const corner of EXTENT_CORNERS) {
    if (corner === exceptCorner) continue;
    const at = cornerLatLng(box, corner);
    const existing = extentHandles.get(corner);
    if (existing) existing.setLatLng(at);
    else extentHandles.set(corner, addExtentHandle(corner, at));
  }
}

function addExtentHandle(corner, at) {
  const handle = L.marker(at, {
    draggable: true,
    // No keyboard focus: these are four more tab stops in front of the
    // form for a gesture that has no keyboard equivalent anyway.
    keyboard: false,
    title: "Drag to resize the extent",
    icon: L.divIcon({
      // The corner is in the class name so styles.css can give each one
      // its own resize cursor, which is the only difference between them.
      className: `extent-handle extent-handle-${corner}`,
      iconSize: [EXTENT_HANDLE_PX, EXTENT_HANDLE_PX],
      iconAnchor: [EXTENT_HANDLE_PX / 2, EXTENT_HANDLE_PX / 2],
    }),
  });
  // This listener is not decoration and it is not a no-op: it is what
  // keeps a press ON a handle from also starting a move of the body
  // underneath it, and every corner of the rectangle is a point inside
  // the rectangle, so without it every resize would also be a move.
  //
  // Leaflet's Map._findEventTargets walks up the DOM from the pressed
  // element and ends at the map container, which is registered as a
  // target for the map itself, so a mousedown on a marker fires on the
  // marker AND on the map. bubblingMouseEvents does not help: the option
  // is only consulted for click, dblclick, mouseover, mouseout and
  // contextmenu (Map._mouseEvents), never for mousedown. stopPropagation
  // on the Leaflet event sets originalEvent._stopped, which is exactly
  // what Map._fireDOMEvent's own loop checks before firing on the next
  // target, and it is the documented way to say "this press was mine".
  // Leaflet's marker drag is unaffected: that runs off its own DOM
  // listeners on the icon element, not off this dispatch.
  handle.on("mousedown", (event) => L.DomEvent.stopPropagation(event));
  handle.on("dragstart", () => beginCornerDrag(corner));
  handle.on("drag", (event) => dragCorner(event && event.latlng));
  handle.on("dragend", () => endCornerDrag());
  handle.addTo(map);
  return handle;
}

// What the map shows mid-gesture: the rectangle, the other handles and
// the bbox field, all following the cursor, and nothing else. No estimate
// is requested from here, which is how "an edit mid-drag must not fire an
// estimate request per pixel" is answered: not by debouncing the request
// but by not making one until the gesture is over. The tile-size slider's
// own pairing is the model, one step further on: its "input" updates the
// readout on every step and its "change" is what asks the server, and a
// mousemove is the input while the release is the change.
//
// The committed bbox is deliberately not touched until the release, so a
// gesture abandoned halfway (Escape, or a press that never moved) leaves
// the page describing exactly the extent it described before.
function previewExtentEdit(box, exceptCorner = null) {
  if (rectangle) {
    rectangle.setBounds([
      [box.south, box.west],
      [box.north, box.east],
    ]);
  }
  syncExtentHandles(box, exceptCorner);
  $("bbox").value = `${box.west},${box.south},${box.east},${box.north}`;
}

// The one way an edit becomes the extent. setBBox is the only thing on
// this page that replaces the committed box, and it is what re-estimates
// and therefore what redraws the tile grid over the new ground, so the
// grid follows an edit exactly as it follows a freshly drawn rectangle.
// fit is false for the same reason Select viewport passes false: the
// owner is looking at the rectangle they just dragged, and fitBounds
// would pad it and snap the zoom, moving the view out from under them.
function commitExtentEdit(box) {
  if (!bbox) return;
  if (!box || box.west === box.east || box.south === box.north || sameBox(box, bbox)) {
    // A zero-area result is refused here exactly as the draw tool refuses
    // one at its release and the bbox field refuses a pasted one: the
    // server would reject it, and the previously committed extent is a
    // better thing to be left with than nothing. An unchanged box takes
    // the same path, because there is no reason to spend an estimate on
    // the extent that is already estimated.
    previewExtentEdit(bbox);
    return;
  }
  setBBox(box, false);
}

function beginCornerDrag(corner) {
  if (!bbox) return;
  const [anchorLat, anchorLng] = cornerLatLng(bbox, OPPOSITE_CORNER[corner]);
  extentCornerDrag = { corner, anchorLat, anchorLng, box: bbox };
}

function dragCorner(latlng) {
  if (!extentCornerDrag || !latlng) return;
  // A corner dragged exactly onto the anchor's own latitude or longitude
  // is a zero-area box. The last good geometry is kept rather than the
  // rectangle collapsing to a line under the cursor and springing back.
  if (latlng.lat === extentCornerDrag.anchorLat || latlng.lng === extentCornerDrag.anchorLng) {
    return;
  }
  extentCornerDrag.box = boxFromCorners(
    extentCornerDrag.anchorLat,
    extentCornerDrag.anchorLng,
    latlng.lat,
    latlng.lng
  );
  previewExtentEdit(extentCornerDrag.box, extentCornerDrag.corner);
}

function endCornerDrag() {
  const drag = extentCornerDrag;
  extentCornerDrag = null;
  if (!drag) return;
  commitExtentEdit(drag.box);
}

// The body. A press inside the committed rectangle takes hold of it and
// the whole box follows the cursor until the release.
//
// The cost is real and worth naming rather than discovering: dragging
// inside the rectangle no longer pans the map, because the same gesture
// cannot mean two things. Panning from outside the rectangle, the scroll
// wheel and the keyboard are all unaffected, and an extent that fills the
// window can still be moved off the ground it is on.
map.on("mousedown", (event) => {
  // A press on a handle never reaches here (see addExtentHandle), and the
  // draw tool owns the press whenever it is armed.
  if (drawing || extentCornerDrag || !extentEditable()) return;
  if (!event.latlng || !insideBox(bbox, event.latlng)) return;
  extentBodyDrag = { fromLat: event.latlng.lat, fromLng: event.latlng.lng, box: bbox, next: null };
  // Leaflet's own Drag handler is watching this same mousedown from a
  // listener registered after the map's event dispatch, so taking it off
  // here, during the dispatch, is what stops the map panning under a
  // rectangle being moved. The same reasoning the draw tool documents,
  // one step later in the gesture because there is no arming step.
  map.dragging.disable();
});

map.on("mousemove", (event) => {
  if (!extentBodyDrag || !event.latlng) return;
  // A release that happened somewhere this map never hears about (off the
  // window, over a native dialog) leaves no mouseup behind, and without
  // this the rectangle would follow the cursor with no button held. The
  // browser tells us on the next move that nothing is pressed; the work
  // done so far is kept, since the owner did mean to move it.
  if (event.originalEvent && event.originalEvent.buttons === 0) {
    endBodyDrag();
    return;
  }
  const dLat = event.latlng.lat - extentBodyDrag.fromLat;
  const dLng = event.latlng.lng - extentBodyDrag.fromLng;
  const box = {
    west: extentBodyDrag.box.west + dLng,
    south: extentBodyDrag.box.south + dLat,
    east: extentBodyDrag.box.east + dLng,
    north: extentBodyDrag.box.north + dLat,
  };
  // Off the top or bottom of the world is not a survey extent and is not
  // something the server would accept. The box stays where it was rather
  // than being silently clamped into a different shape than the one the
  // hand is describing.
  if (box.north > 90 || box.south < -90) return;
  extentBodyDrag.next = box;
  previewExtentEdit(box);
});

map.on("mouseup", () => {
  if (extentBodyDrag) endBodyDrag();
});

function endBodyDrag() {
  const drag = extentBodyDrag;
  extentBodyDrag = null;
  // Never while the draw tool is armed: it switched panning off itself
  // and expects it to stay off until it disarms.
  if (!drawing) map.dragging.enable();
  if (!drag) return;
  if (!drag.next) {
    // A press inside the rectangle that never moved. Nothing is
    // committed and, just as importantly, the click that follows is left
    // alone: that click is how a red tile is selected in the failure
    // list, and swallowing it would break the one thing on this map that
    // answers to a plain click.
    return;
  }
  suppressNextMapClick = true;
  commitExtentEdit(drag.next);
}

// Escape gets out of a body drag, the same way it gets out of a
// half-drawn rectangle, and leaves the committed extent exactly as it
// was. A corner drag is Leaflet's gesture rather than this file's and
// ends on the release wherever that happens, so there is nothing here to
// cancel for it.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !extentBodyDrag) return;
  extentBodyDrag = null;
  if (!drawing) map.dragging.enable();
  previewExtentEdit(bbox);
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
    let filled = false;
    if (!$("site").value && result.site) {
      $("site").value = result.site;
      filled = true;
    }
    if (!$("region").value && result.region) {
      $("region").value = result.region;
      filled = true;
    }
    // Task 36, item 1. The owner reported Download sitting dead at the
    // start of every session "even though it had a path saved", and
    // reported that reopening the folder picker brought it back. The
    // saved path was never what gated it: output_root is not in
    // missingFieldsMessage at all, and an empty one would not disable
    // Download either. What actually gates Download is refreshEstimate's
    // own success path, the single line `$("download").disabled = false`,
    // and the only things that re-run refreshEstimate are setBBox and a
    // "change" event on region, site, tile-size, overlap, output-root,
    // #sources or #categories.
    //
    // Assigning .value from a script fires no change event (the same fact
    // the folder picker's own dispatchEvent already documents), so the two
    // lines above used to fill in the very names the missing-fields
    // message was complaining about and leave the gate un-rechecked. Draw
    // an extent, wait for the reverse lookup, and the page showed a filled
    // region and site, an estimate box still asking for a site, and a dead
    // Download button, for as long as it took to touch some other field.
    // Reopening the picker is exactly such a touch, which is why it looked
    // like the path.
    //
    // Not a second gate, and not a weakening of the first: this asks the
    // one gate to look again now that its own inputs have changed. An
    // empty category or layer selection still disables Download, because
    // it is still missingFieldsMessage that decides.
    //
    // refreshEstimate() directly rather than a dispatched change event on
    // each field: region and site each carry exactly one change listener,
    // refreshEstimate itself, so dispatching would fire two estimates for
    // one lookup and race their two renders against each other.
    if (filled) refreshEstimate();
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

// The same check for the layer checklist, added by review finding C1.
// Unticking every layer used to send sources: [], which the server
// coalesced back into the full default set, so the owner got an
// OpenStreetMap and Overture download of exactly the data they had just
// switched off. The server now refuses an empty list (see
// mapgen.sources.base.EmptySourceSelectionError), and this is what keeps
// the owner from ever reaching a refusal: Download sits disabled with the
// same missing-fields sentence a blank site name already produces.
//
// Empty reads as "nothing to say yet" for the same reason it does above:
// boot() renders this checklist with every box ticked, so zero boxes only
// ever means GET /api/sources has not landed yet.
function noLayerSelected() {
  const boxes = [...document.querySelectorAll("#sources input")];
  return boxes.length > 0 && boxes.every((box) => !box.checked);
}

// Names the field or fields actually missing, rather than a fixed message
// regardless of which ones are empty: a region already filled by the
// reverse lookup must not be told to "enter a region" alongside a genuinely
// empty site. Returns null once bbox, region, site, at least one layer and
// at least one category are all present.
function missingFieldsMessage() {
  const clauses = [];
  if (!bbox) clauses.push("draw or paste an extent");
  const namesMissing = [];
  if (!$("region").value.trim()) namesMissing.push("region");
  if (!$("site").value.trim()) namesMissing.push("site");
  if (namesMissing.length) clauses.push(`enter a ${namesMissing.join(" and ")}`);
  if (noLayerSelected()) clauses.push("select at least one layer");
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

// The exact folder the last successful estimate said this run would
// create, kept for the same reason lastSizing below is kept: the estimate
// response is rendered and thrown away, and the progress line needs the
// package's own name out of it once a job is running. The preview element
// is not read back instead, because a page whose estimate has since been
// replaced would answer with a different survey's folder.
let lastFolder = "";

function hideFolderPreview() {
  $("folder-preview").hidden = true;
  $("folder-preview-path").textContent = "";
  // And with it the name the progress line would quote. Every caller of
  // this is a case where there is no folder this run would create: no
  // extent, no names yet, or an estimate that failed.
  lastFolder = "";
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
      // The same one string the preview shows, kept for the progress line
      // to take the package's own name out of (Task 38, item 5). One
      // source, so the line under the map and the line above Download can
      // never name two different folders for one run.
      lastFolder = data.folder;
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

// The slider's own grid, matching index.html's step="100". A saved value
// is only ever measured against this, never rewritten to fit it.
const TILE_SIZE_STEP_M = 100;

function applyTileSizeBounds(savedMetres) {
  const saved = Number(savedMetres);
  const usable = Number.isFinite(saved) && saved > 0;
  const min = usable ? Math.min(TILE_SIZE_MIN_M, saved) : TILE_SIZE_MIN_M;
  const max = usable ? Math.max(TILE_SIZE_MAX_M, saved) : TILE_SIZE_MAX_M;
  // Set before the value is, not after: a browser clamps an out-of-range
  // value the moment it is assigned, so setting the value first would
  // lose the very setting these bounds exist to preserve.
  $("tile-size").min = String(min);
  $("tile-size").max = String(max);
  // And the same argument for step, which review finding I9 found this
  // function never touched. A range input does not only clamp on
  // assignment, it also SNAPS to its step grid (HTML's value sanitization
  // algorithm for the range state: "when the element is suffering from a
  // step mismatch, the user agent must round the element's value to the
  // nearest number for which the element would not", ties going to the
  // larger). Before Task 27 this control was <input type="number"
  // min="500" step="100">, which does not snap, and nothing here calls
  // checkValidity, so a typed 1250 was read and saved. On the next launch
  // the range input rounded it to 1300, the estimate ran at 1300, and
  // persistFieldSettings wrote 1300 back over config.json. tile_size_m is
  // hashed into naming.tiling_fingerprint, so an in-progress
  // _work/<fingerprint>/ at 1250 was orphaned and every tile in it
  // refetched, for a setting the owner never changed.
  //
  // The grid goes away for the session rather than the value being bent
  // to fit it, which is the same ruling the min/max widening above
  // already makes: the control adapts to the saved setting, never the
  // other way round. The cost is real and worth naming: while the slider
  // is continuous, dragging it can produce a size that is not a round
  // hundred. That is a size the owner chose by moving the control, not
  // one this page invented behind their back, and it lasts only until
  // they land back on the grid.
  const onGrid = usable && Number.isInteger((saved - min) / TILE_SIZE_STEP_M);
  $("tile-size").step = onGrid || !usable ? String(TILE_SIZE_STEP_M) : "any";
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
// Tile size and overlap are defaults the owner sets once and rarely
// revisits; the theme, the elevation model and the API keys are the same
// kind of thing. They live here rather than in the main flow, which is
// otherwise entirely per-survey choices (region, site, layers,
// categories). Nothing about the FIELDS themselves changes: their ids,
// their persistence and their effect on the next estimate are unchanged
// by which panel currently shows them.
//
// The output root was here too, and Task 31 moved it back out. It is not
// a default of the same kind: it is where this download is about to land,
// which is the one thing worth being able to read and change without
// opening anything. Nothing about that field changed either, which is the
// point of ids being stable across a move.

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

// --- picking the output folder -------------------------------------------
//
// Task 28. A browser cannot hand a page a real filesystem path, and no
// amount of webkitdirectory changes that: the page can learn a file's
// name and its bytes, never where it lives. So the dialog is opened
// server-side, by the local server already running as the owner, and the
// path comes back over the same token-gated API as everything else. See
// mapgen.folderpicker for why it runs in a child process rather than in
// a request handler thread.
//
// This is help beside a control that already works, never a replacement
// for it. Every outcome that is not a chosen path leaves #output-root
// exactly as it was, including the two that are genuinely failures: the
// owner can always type the path, which is what they did before this
// button existed and what they will go on doing if the picker turns out
// not to work on some future machine.
//
// Task 31 moved both the field and this button into the main form, above
// Download. Not one line below changed for it, and that is worth saying
// rather than leaving to be noticed: everything here is addressed by id,
// so the whole picker, its cancel, its timeout, its unavailable and its
// busy handling came across the page intact.

function setOutputRootNote(message) {
  const note = $("output-root-note");
  note.textContent = message || "";
  note.hidden = !message;
}
// Set here rather than left to the markup, the same reasoning
// closeSettingsPanel and hideProgress already document for the Node
// harness's synthetic elements.
setOutputRootNote("");

$("output-root-browse").addEventListener("click", async () => {
  // Said before the request goes out, not after it comes back: the
  // dialog is a native window and can open behind a maximised browser,
  // which looks exactly like nothing having happened. This line is the
  // only thing on screen that would explain it.
  setOutputRootNote("A folder picker is open. If you cannot see it, look behind this window.");
  $("output-root-browse").disabled = true;
  try {
    const chosen = await api("/api/folder-dialog", {
      method: "POST",
      // Where to open: the field's current value, so the dialog starts
      // where the owner already is rather than at some default.
      body: JSON.stringify({ initial: $("output-root").value.trim() }),
    });
    if (typeof chosen.path === "string" && chosen.path) {
      $("output-root").value = chosen.path;
      setOutputRootNote("");
      // Assigning .value never fires a change event in a browser, and
      // this field already has TWO change listeners on it:
      // refreshEstimate (which is also what persists the value once an
      // estimate has accepted it) and maybePersistFieldSettings.
      // Dispatching a real change event puts a picked path through
      // exactly what a typed path goes through, which is what was asked
      // for and, more to the point, is one wiring rather than a second
      // copy of it here that would quietly stop matching the first.
      $("output-root").dispatchEvent(new Event("change"));
    } else {
      // A cancel. A real answer, and never a reason to clear the field.
      setOutputRootNote("No folder chosen. The path above is unchanged.");
    }
  } catch (error) {
    // The picker could not run, or was open too long and was closed.
    // Both arrive as the server's own plain sentence, which already ends
    // by saying to type the path instead.
    setOutputRootNote(error.message);
  } finally {
    // In a finally, so a picker that failed once does not leave the
    // button dead for the rest of the session.
    $("output-root-browse").disabled = false;
  }
});

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

// Every event is logged as its name plus its fields, by name, with no
// per-event formatting anywhere: that is what makes a new event from the
// Python side safe to add. Task 30 added the first events carrying a
// LIST, though (verify_done's corrections and failures), and a list
// interpolated into a template literal reads "[object Object]", which is
// not a field the owner can do anything with. JSON for anything that is
// not a scalar, and nothing else changes: a string, a number or a boolean
// still prints exactly as it always did, which is what keeps the rest of
// the log looking like itself.
function describeEventValue(value) {
  if (value === null || typeof value !== "object") return String(value);
  try {
    return JSON.stringify(value);
  } catch (error) {
    // A structure that cannot be serialised (a cycle, in principle) must
    // not take the whole log line down with it.
    return String(value);
  }
}

function log(message, failed = false) {
  const line = document.createElement("div");
  if (failed) line.className = "fail";
  line.textContent = message;
  $("log").appendChild(line);
  $("log").scrollTop = $("log").scrollHeight;
}

// --- how much of the column the log gets ---------------------------------
//
// Task 38, item 4. "allow for a height adjustment to the bottom of the
// viewport to allow me to drag it up and down, this is in aid of allowing
// more visibility of the log".
//
// The log and the map share one flex column (item 3). The map is flex: 1
// and the log holds whatever height it is given, so setting the log's
// height is the whole of the split: everything the log takes the map
// gives up, and nothing else on the page moves.
//
// Two floors, and neither is arbitrary. The log is worth having only if a
// line or two of it is readable, and a map under about 160px is not one
// an extent can be drawn on. Both are enforced against the height the two
// of them actually share, measured when the drag starts, so the pair can
// never be asked for more than the column has.
//
// Leaflet has to be told. A map whose container has changed size and has
// not been told keeps the tile layout it had, which shows as tiles
// missing from the newly revealed strip and a centre that is no longer
// the centre. map.invalidateSize() is the vendored build's own answer
// (Map.invalidateSize, 1.9.4), and it is called on every step of the drag
// rather than once at the end: that is what Leaflet itself does for a
// window resize, one call per animation frame, and a mousemove cannot
// arrive more often than that.
const LOG_MIN_PX = 60;
const MAP_MIN_PX = 160;
const LOG_DEFAULT_PX = 120;

// Kept in localStorage, and this is the only setting in this file that
// lives anywhere but config.json, so the reason belongs here rather than
// being left to be discovered.
//
// PUT /api/config ignores any key that is not a declared field of
// mapgen.config.Config (server.py: `if key in
// current.__dataclass_fields__`), and GET /api/config only ever returns
// that dataclass. A browser-invented key is therefore accepted with a
// 200, silently dropped, and gone by the next launch. Keeping the split
// there needs a Config field, which is a Python change this task was
// scoped out of; the report says what the one line is.
//
// What this does instead survives what the brief asked it to survive, a
// reload of the page. What it does not survive is a relaunch: serve()
// binds port 0, an ephemeral port per launch, and localStorage is keyed
// by origin including the port, so the next launch reads a different
// store. That gap is real, it is named in the report, and it closes the
// moment the field exists.
const LOG_HEIGHT_KEY = "mapgen.log-height-px";

// { startY, logHeight } for as long as the divider is being dragged.
let logResize = null;

function elementHeight(id) {
  const measured = Number($(id).offsetHeight);
  return Number.isFinite(measured) && measured > 0 ? measured : 0;
}

// The log's height as it is now: what this file last set, or what the
// stylesheet gave it, or the stylesheet's own number when neither can be
// read, which is what a page whose layout has not happened yet has.
function logHeight() {
  const styled = parseFloat(String($("log").style.height || ""));
  if (Number.isFinite(styled) && styled > 0) return styled;
  return elementHeight("log") || LOG_DEFAULT_PX;
}

// The height the map and the log share, read fresh on every call rather
// than captured when a drag starts. Capturing it looks like the careful
// thing to do and is not: the previous step of the drag has already been
// applied by the time this runs, so the log's height and the map's add up
// to the same column either way, and the captured version is a second
// copy of a number that has to be kept true. It would also be saving a
// forced layout read that is not being saved, since map.invalidateSize()
// four lines below reads the container's size in the same breath.
function setLogHeight(px) {
  const shared = logHeight() + elementHeight("map");
  // max() rather than a bare subtraction: on a window too short for both
  // minimums the log's own floor wins and the map keeps whatever is left,
  // which is what happened before this control existed.
  const most = Math.max(LOG_MIN_PX, shared - MAP_MIN_PX);
  const next = Math.min(Math.max(px, LOG_MIN_PX), most);
  $("log").style.height = `${Math.round(next)}px`;
  map.invalidateSize();
  return next;
}

function storedLogHeight() {
  try {
    const raw = window.localStorage && window.localStorage.getItem(LOG_HEIGHT_KEY);
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? value : 0;
  } catch (error) {
    // Storage can be disabled or full, and neither is a reason for the
    // page not to open. The stylesheet's own height is a fine answer.
    return 0;
  }
}

function storeLogHeight(px) {
  try {
    if (window.localStorage) window.localStorage.setItem(LOG_HEIGHT_KEY, String(Math.round(px)));
  } catch (error) {
    // A failed save costs the next reload its remembered split and
    // nothing else, the same way a failed PUT /api/config costs the next
    // launch its settings, and it stays quiet for the same reason.
  }
}

$("log-resizer").addEventListener("mousedown", (event) => {
  logResize = { startY: Number(event.clientY), logHeight: logHeight() };
  // Or the drag selects the log's text on its way past.
  if (event.preventDefault) event.preventDefault();
});

// Bound on the document rather than on the divider: a 7px target is easy
// to leave behind mid-gesture, and a drag that stopped following the
// cursor the moment it left the line would be unusable. Leaflet's own
// Draggable binds its move and up the same way and for the same reason.
document.addEventListener("mousemove", (event) => {
  if (!logResize) return;
  // Up the screen is a SMALLER clientY and a TALLER log, which is why
  // this is the start minus now rather than the other way round.
  setLogHeight(logResize.logHeight + (logResize.startY - Number(event.clientY)));
});

document.addEventListener("mouseup", () => {
  if (!logResize) return;
  logResize = null;
  // Saved once, at the end. This is one setting the owner chose, not the
  // sixty positions they passed through on the way to it.
  storeLogHeight(logHeight());
});

// The remembered split, put through the same clamp a live drag goes
// through, so a height saved on a taller window does not come back on a
// shorter one and leave no map.
(function restoreLogHeight() {
  const saved = storedLogHeight();
  if (saved) setLogHeight(saved);
})();

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
// And the same reasoning again for the folder this job is building, whose
// name the progress line quotes: snapshotted at the press, so drawing a
// new extent while a download runs cannot rename the package the line is
// reporting on. The server has no answer of its own to use instead until
// the run is over: JobRecord.result_root is only set once run_survey has
// returned (see web/server.py), so a running job's status carries null.
let activeJobFolder = "";
let jobStartedAt = 0;

function hideProgress() {
  $("progress").hidden = true;
  $("progress").className = "progress";
  $("progress").setAttribute("aria-valuenow", "0");
  $("progress-fill").style.width = "0%";
  $("progress-status").textContent = "";
  $("progress-text").textContent = "";
  $("progress-note").hidden = true;
  $("progress-note").textContent = "";
  syncMapStatus();
}
// Set here rather than left to index.html's own hidden attribute, the
// same reasoning clearTileGrid and closeSettingsPanel already document.
hideProgress();

// What is being worked on, and which package it is going into. Task 38,
// item 5: "so that it just says which tile its doing and what package
// its downloading".
//
// The two are joined by the same separator the estimate line already
// uses, and either half can be missing without leaving a stray divider:
// there is no package name until an estimate has produced a folder, and
// there is no phase to name in the moment between the job starting and
// its first event.
function progressStatusLine(phase, job) {
  const folder = activeJobFolder || (job && job.result_root) || "";
  return [phaseLabel(phase), packageStem(folder)].filter(Boolean).join(" · ");
}

// Draws the bar and its line of copy from the summary, the job's own
// state, and the clock. Split from the poll loop so the whole of it is
// reachable from a test with a fabricated job, and split from
// remainingLabel so the arithmetic can be tested without a DOM at all.
function renderProgress(summary, job, elapsedSeconds) {
  const percent = Math.max(0, Math.min(100, Math.round(summary.fractionDone * 100)));
  const progress = $("progress");
  progress.hidden = false;
  progress.className = "progress";
  syncMapStatus();
  $("progress-fill").style.width = `${percent}%`;
  progress.setAttribute("aria-valuenow", String(percent));

  // The phase, while there is one. A run that has stopped, finished or
  // failed says so in the line beside this one, and repeating it here in
  // fewer words would be two labels for one fact on a strip with room
  // for neither.
  //
  // Task 38, item 5: and the package this run is building, after it. The
  // owner's own word order ("which tile its doing and what package its
  // downloading") and also the order this strip needs: .progress-status
  // is the line that gives up its width first on a narrow window, and it
  // ellipsises from the END, so the tile, which changes every few
  // seconds, has to come before the folder name, which does not change
  // at all. The other way round and the first thing lost is the only
  // thing moving.
  $("progress-status").textContent =
    job.state === "running" ? progressStatusLine(summary.phase, job) : "";

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
    // Task 38, item 5. This read "72% done, less than a minute left, from
    // the rate so far", and the owner asked for the time and nothing
    // after it. The percentage went with the suffix rather than being
    // kept, because it is the one thing on this strip that is already
    // drawn: the bar is 140px of exactly that number, immediately to the
    // left, and aria-valuenow carries it for a screen reader. A run that
    // has ENDED still spells it out below, where there is no longer a
    // moving bar to read it off and where Task 22's difference between
    // stopping at 68% and failing at 68% lives.
    $("progress-text").textContent = label.text;
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
    // Task 38, item 2. The extent is now the server's business for as
    // long as this job lasts: the handles come off the map and every
    // edit gesture is refused until it ends, whichever way it ends. Set
    // after the POST has been answered, so a job the server refused (a
    // 409 from a busy server) never takes the handles away.
    jobRunning = true;
    extentCornerDrag = null;
    extentBodyDrag = null;
    syncExtentHandles();
    activeJobSourceIds = requestPayload.sources;
    activeJobSeconds = lastSizing && lastSizing.seconds > 0 ? lastSizing.seconds : 0;
    activeJobSourceSeconds = (lastSizing && lastSizing.sourceSeconds) || {};
    activeJobFolder = lastFolder;
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
    // And the previous run's splits go with the previous run's states:
    // whether a tile was too dense is a fact about a run, not about the
    // ground, and a resumed run that finds the pieces already on disk
    // never splits it again.
    tileSubdivided = new Map();
    for (const [tileId, rect] of tileRectangles) {
      tileState.set(tileId, "pending");
      rect.setStyle(tileStyleFor("pending"));
      rect.unbindTooltip();
    }
    // Which also takes the split entry back out of the legend, since
    // nothing has split in this run yet.
    renderTileLegend();
    // And the previous run's reasons go with its colours. A second
    // download over the same extent is a fresh account of that ground,
    // and last run's failures sitting beside a bar that has gone back to
    // zero would be the clearest way to make the owner chase a problem
    // that has already been fixed.
    tileTooltips = new Map();
    renderTileFailures([]);
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
        // Nothing is watching this job any more, so nothing here can be
        // made inconsistent by an edit: the extent goes back to being the
        // owner's to change, exactly as it does when a run ends normally.
        jobRunning = false;
        syncExtentHandles();
        log(`Lost contact with the job: ${error.message}`, true);
        // The bar would otherwise sit frozen at whatever the last poll
        // saw, with a countdown still promising a number that nothing is
        // updating any more. The job may well still be running; this
        // page just cannot see it.
        $("progress-text").textContent =
          "Lost contact with the job. The bar has stopped updating.";
        // The phase would otherwise sit there naming whatever this page
        // last saw happening, as though it were still happening.
        $("progress-status").textContent = "";
        $("progress-note").hidden = true;
        return;
      }
      job.events.slice(seen).forEach((e) => {
        const detail = Object.entries(e)
          .filter(([k]) => k !== "event")
          .map(([k, v]) => `${k}=${describeEventValue(v)}`)
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
      paintTileStates(summary.tileStates, summary.tileSubdivisions);
      // The same records twice, on purpose: on the map, where the owner
      // is already looking at the red rectangle, and in the list, which
      // is the only one of the two that can be read without knowing it
      // is there. renderTileFailures first, so a click that lands
      // between this tick and the next has the current set to look up.
      renderTileFailures(summary.tileFailures);
      paintTileTooltips(summary.tileFailures, summary.tileSubdivisions);
      renderProgress(summary, job, (Date.now() - jobStartedAt) / 1000);
      if (job.state !== "running") {
        clearInterval(poller);
        $("cancel").hidden = true;
        $("download").disabled = false;
        jobRunning = false;
        syncExtentHandles();
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

// Review finding I4. This was the one request in the file with no
// try/catch: download, stop-server and output-root-browse all wrap theirs
// and report the failure. api() throws on a 403, on a 404 (an unknown job
// id after a server restart) and on an unreachable server, so a Stop that
// did not land became an unhandled promise rejection with nothing on
// screen and nothing in the log. The bar kept counting down, the button
// stayed visible, and the button looked dead.
//
// api() already composes the right sentence for the commonest case here
// ("Cannot reach the mapgen server..."); this is what finally displays
// it. Logged in the red "fail" styling rather than shown as an estimate
// error, because the estimate box is not what the owner is looking at
// while a download is running, and the log line sits beside the progress
// bar that is still moving.
$("cancel").addEventListener("click", async () => {
  if (!jobId) return;
  try {
    await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
  } catch (error) {
    log(`Could not stop the download: ${error.message}`, true);
  }
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
// field just gave no honest signal either way. This function never echoes
// the key itself, only whether one is present in the saved config.
//
// It used to add "matching the redaction discipline mapgen.sources.
// elevation already applies to this exact value everywhere else it could
// ever reach a log or a response". Review finding N11: that is not true
// of the page as a whole and the claim reads as though it were.
// renderApiKeys twelve lines below writes the real key into the DOM as
// value="...", because it is an editable field and has to, and GET
// /api/config returns it in the response body. Both are loopback-only and
// token gated and both predate this branch, so this is a comment that
// overclaimed rather than a leak; elevation.py's redaction is about the
// key reaching a LOG or an ERROR MESSAGE, which is a different question
// from a field the owner is meant to be able to edit.
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
